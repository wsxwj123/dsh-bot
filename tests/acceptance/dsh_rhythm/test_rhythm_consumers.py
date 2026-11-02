# -*- coding: utf-8 -*-
"""各消费方按这只 bot 的覆盖值判（INTERFACE 3.5 表格与补充）。

只调 INTERFACE 3.5 点名的对外函数：
  jiwen.tick.merge_rates、jiwen.merge.threshold_values、moments.post.should_post_moment、
  moments.post.maybe_post_moment（新关键字 ignore_daily_limit）、prefilter.hard_prefilter（bot_cfg=）、
  quota.check_proactive_quota。
`bot_cfg` 直接给「load_bot 浅合并后的结果」那种字典（生活设定文件的键在顶层）。
数据库与文案生成的替身照 tests/acceptance/moments_same_kind 的写法：db_module 注入或替换 db 的
三个函数、call_aux 换成固定 JSON，绝不碰真实库、绝不发真实 LLM 请求。
覆盖值都选成「全局/默认不会拦、只有覆盖会拦」，拦住即证明覆盖真生效。
被测函数在用例里才导入：还没实现时逐条红，不影响收集。
"""
import datetime
import inspect
import json

NOW = datetime.datetime(2026, 10, 28, 14, 0)
NOW_TS = int(NOW.timestamp())
BOT_ID = "testlulu"


def _situation():
    from generators.situation import Activity, SituationContext
    return SituationContext(
        recurring=Activity(name="自由时间", description="在家放松", state="free"),
        sporadic=None, hobby=None)


def _world():
    from generators.world import WorldSnapshot
    return WorldSnapshot()


class StubDb:
    """朋友圈历史替身：只答「最近的圈」「今天发了几条」，记下落库。"""

    def __init__(self, rows=(), count_today=0):
        self.rows = list(rows)
        self.count_today = count_today
        self.insert_calls = []

    def list_moments(self, *args, **kwargs):
        since = kwargs.get("since_ts", args[2] if len(args) > 2 else 0) or 0
        return [r for r in self.rows if r["ts"] >= since]

    def count_moments_today(self, *args, **kwargs):
        return self.count_today

    def insert_moment(self, *args, **kwargs):
        self.insert_calls.append((args, kwargs))
        return 4242


def _moment(minutes_ago, kind):
    return {"id": 1, "bot_id": BOT_ID, "ts": NOW_TS - minutes_ago * 60,
            "created_at": NOW - datetime.timedelta(minutes=minutes_ago),
            "kind": kind, "moment_kind": kind, "text": "历史朋友圈", "visibility": "public"}


def _should_post(bot_cfg, since_user_min, db, global_cfg=None):
    from moments import post
    return post.should_post_moment(
        situation=_situation(), world=_world(), wildcard=None, mood=0.9, jiwen_info=None,
        bot_cfg=bot_cfg, since_user_min=since_user_min, now=NOW, db_module=db, force=False,
        global_cfg=global_cfg or {})


# ======================================================================== 情绪

def test_情绪速率_覆盖写0按0算_越界的覆盖当没设回到全局值():
    from jiwen.tick import merge_rates
    rates = merge_rates({"rates": {"connection_per_min": 0.002, "connection_accel": 2.0}},
                        {"rates_override": {"connection_per_min": 0, "connection_accel": 9}})
    assert rates.connection_per_min == 0.0, "覆盖 0 应生效（0 也算设了）"
    assert rates.connection_accel == 2.0, "9 超出 [0, 5]，应当没设、回到全局 2.0"


def test_情绪阈值_新旧写法都在时旧写法生效_没设的回默认():
    from jiwen.merge import threshold_values
    got = threshold_values({"thresholds": {"notice": 0.3}},
                           {"thresholds_override": {"notice": 0.5}, "thresholds": {"notice": 0.9}})
    assert got["notice"] == 0.9, "两种写法都在时旧写法 jiwen.thresholds 生效"
    assert got["consider"] == 0.35 and got["forced"] == 0.5, "没设的阈值应回 dataclass 默认"


# ======================================================================== 发圈

def test_发圈静默期_bot覆盖60分钟_距上条消息40分钟被拦_原因带覆盖值():
    got = _should_post({"moments_silence_threshold_minutes": 60}, 40, StubDb(),
                       {"moments": {"silence_threshold_minutes": 5}})
    assert tuple(got) == (False, "skip", "silence(40min<60)")


def test_发圈最小间隔_bot覆盖120分钟_90分钟前发过圈被拦():
    got = _should_post({"moments_min_interval_minutes": 120}, 120,
                       StubDb([_moment(90, "wildcard")]))
    assert tuple(got) == (False, "skip", "min_interval(120min since last moment)")


def test_发圈同类型间隔_bot覆盖6小时_5小时前同类圈被拦():
    got = _should_post({"moments_same_kind_hours": 6}, 120,
                       StubDb([_moment(5 * 60, "mood_extreme")]))
    assert tuple(got) == (False, "skip", "same_kind(mood_extreme) within 6h")


def _maybe_post(monkeypatch, tmp_path, stub_db, **extra):
    import importlib
    from pathlib import Path
    import db
    import moments.post as post_module
    # 朋友圈库钉在 tmp 并建好表（同 bot_ops 的 botlife_db 夹具：DB_PATH 导入期定死，要重载）
    importlib.reload(db)
    assert Path(db.DB_PATH).resolve() == (tmp_path / "state.db").resolve(), "朋友圈库没钉在 tmp"
    db.init()
    from generators.wildcard import WildcardPick
    llm_calls = []

    def fake_aux(*a, **k):
        llm_calls.append((a, k))
        return json.dumps({"text": "今天天气不错", "visibility": "public"}, ensure_ascii=False)

    monkeypatch.setattr(db, "list_moments", stub_db.list_moments)
    monkeypatch.setattr(db, "count_moments_today", stub_db.count_moments_today)
    monkeypatch.setattr(db, "insert_moment", stub_db.insert_moment)
    monkeypatch.setattr(post_module, "call_aux", fake_aux)
    channel = tmp_path / "channel"
    channel.mkdir(exist_ok=True)
    kwargs = dict(
        bot_id=BOT_ID,
        bot_cfg={"id": BOT_ID, "display_name": "测试", "chat_id": "123456789",
                 "bot_channel_path": str(channel),
                 "moments": {"enabled": True, "image_generation": {"enabled": False}},
                 "moments_daily_limit": 0},
        now=NOW, situation=_situation(), world=_world(),
        wildcard=WildcardPick(card_id=1, card="今天心情很好", emotion="开心"),
        mood=0.9, mood_factors=["开心"], since_user_min=120,
        global_cfg={"user_display_name": "我",
                    "moments": {"enabled": True, "daily_post_limit_per_bot": 5,
                                "image_generation": {"enabled": False}}},
        jiwen_info=None, force=True)
    kwargs.update(extra)
    if "db_module" in inspect.signature(post_module.maybe_post_moment).parameters:
        kwargs["db_module"] = stub_db
    result = post_module.maybe_post_moment(**kwargs)
    return result, llm_calls


def test_每天上限_bot覆盖0_force也跳过_只有ignore_daily_limit才发(monkeypatch, tmp_path, capsys):
    blocked_db = StubDb(count_today=0)
    result, llm = _maybe_post(monkeypatch, tmp_path, blocked_db)
    out = capsys.readouterr()
    shown = result.get("skip") if isinstance(result, dict) else (out.out + out.err)
    assert "daily_limit(0)" in str(shown), "覆盖 0：force=True 也应以 daily_limit(0) 跳过：%r" % (shown,)
    assert llm == [] and blocked_db.insert_calls == [], "被每天上限拦住就不该生成、不该落库"

    free_db = StubDb(count_today=0)
    _, llm = _maybe_post(monkeypatch, tmp_path, free_db, ignore_daily_limit=True)
    assert len(llm) == 1 and len(free_db.insert_calls) == 1, \
        "ignore_daily_limit=True 不受每天上限约束，应生成并落库一次"


# ======================================================================== 主动开口

def test_主动开口静默期_bot覆盖60分钟_距上条消息40分钟被拦_原因带覆盖值():
    import prefilter
    reason = prefilter.hard_prefilter(NOW, _situation(), _world(), None, 0.9, 40, [],
                                      {"silence_threshold_minutes": 5},
                                      bot_cfg={"proactive_silence_threshold_minutes": 60})
    assert reason == "非静默期(距上次40min<60min)"


def test_主动上限_没设覆盖_全局也没写_返回不限且不建库(sandbox):
    import quota
    db_file = sandbox.tmp / "state.db"
    existed = db_file.exists()          # 导入期的副作用不算在这次调用头上
    got = quota.check_proactive_quota(BOT_ID, {}, {})
    assert tuple(got) == ("ok", 0, 0)
    assert db_file.exists() == existed, "不限时不该去查库（更不该因此建库）"
