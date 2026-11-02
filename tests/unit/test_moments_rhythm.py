# -*- coding: utf-8 -*-
"""发圈三道闸与每日上限读 bot 覆盖（PLAN dsh-rhythm ④ T3）。

- should_post_moment：最小间隔 / 同类型写 0 不拦；没覆盖时原因串与改动前逐字相同；
- maybe_post_moment：每日上限写 0 全拦（含 force=True）；ignore_daily_limit=True 当天已满 5 条仍能发；
- scripts/bot_post_moment.py、scripts/force_moment.py 当天已满（含 bot 覆盖 0）仍能发。

库用 tmp 下的真实 sqlite（`db._connect` 每次现读 `db.DB_PATH`），只替换文案模型 `call_aux`
与脚本收集情境的四个输入函数；闸门、计数、落库都走真实代码。
"""
import datetime
import json
import sys
import time

import pytest

NOW = datetime.datetime(2026, 10, 28, 14, 0)
NOW_TS = int(NOW.timestamp())
BOT = "testlulu"


def _situation(hobby=None):
    from generators.situation import Activity, SituationContext
    return SituationContext(
        recurring=Activity(name="自由时间", description="在家放松", state="free"),
        sporadic=None, hobby=hobby)


def _world():
    from generators.world import WorldSnapshot
    return WorldSnapshot()


class StubDb:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def list_moments(self, *args, **kwargs):
        since = kwargs.get("since_ts", 0) or 0
        return [r for r in self.rows if r["ts"] >= since]


def _row(minutes_ago, kind):
    return {"id": 1, "bot_id": BOT, "ts": NOW_TS - minutes_ago * 60, "moment_kind": kind}


def _should(bot_cfg, rows, global_cfg=None, force=False):
    from moments import post
    # mood 0.9 → kind = mood_extreme；距上次聊天 120 分钟，静默期不拦
    return tuple(post.should_post_moment(
        situation=_situation(), world=_world(), wildcard=None, mood=0.9, jiwen_info=None,
        bot_cfg={**bot_cfg, "id": BOT}, since_user_min=120, now=NOW, db_module=StubDb(rows),
        force=force, global_cfg=global_cfg or {}))


# ---------------------------------------------------------------- should_post_moment


def test_min_interval_default_reason_unchanged_and_zero_skips_gate():
    rows = [_row(10, "wildcard")]
    assert _should({}, rows) == (False, "skip", "min_interval(60min since last moment)")
    assert _should({"moments_min_interval_minutes": 0}, rows) == (True, "mood_extreme", "mood=0.90")
    assert _should({}, rows, {"moments": {"min_interval_minutes": 0}})[0] is True     # 全局 0 也跳过
    assert _should({"moments_min_interval_minutes": 5}, rows)[0] is True              # 10 分钟前 > 5
    assert _should({"moments_min_interval_minutes": 15}, rows) == (
        False, "skip", "min_interval(15min since last moment)")
    assert _should({"moments_min_interval_minutes": "abc"}, rows) == (               # 坏值当没设
        False, "skip", "min_interval(60min since last moment)")


def test_same_kind_default_reason_unchanged_and_zero_skips_gate():
    rows = [_row(120, "mood_extreme")]          # 2 小时前同类：不触发 60 分钟间隔
    assert _should({}, rows) == (False, "skip", "same_kind(mood_extreme) within 4h")
    assert _should({"moments_same_kind_hours": 0}, rows)[0] is True
    assert _should({}, rows, {"moments": {"same_kind_hours": 0}})[0] is True
    assert _should({"moments_same_kind_hours": 1}, rows)[0] is True
    assert _should({}, [_row(5 * 60, "mood_extreme")], {"moments": {"same_kind_hours": 6}}) == (
        False, "skip", "same_kind(mood_extreme) within 6h")


def test_silence_zero_and_force_still_skips_three_gates():
    from moments import post
    got = post.should_post_moment(
        situation=_situation(), world=_world(), wildcard=None, mood=0.9, jiwen_info=None,
        bot_cfg={"id": BOT, "moments_silence_threshold_minutes": 0}, since_user_min=1, now=NOW,
        db_module=StubDb(), force=False, global_cfg={"moments": {"silence_threshold_minutes": 30}})
    assert got[0] is True                               # 0 = 不设静默期
    rows = [_row(10, "mood_extreme")]
    assert _should({"moments_min_interval_minutes": 120, "moments_same_kind_hours": 6}, rows,
                   force=True)[0] is True                # force 含义不变：三道闸都跳过


# ---------------------------------------------------------------- maybe_post_moment（真实 tmp 库）


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    import db
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "state.db"))
    db.init()
    return db


@pytest.fixture
def fake_llm(monkeypatch):
    from moments import post
    calls = []

    def fake_aux(*a, **k):
        calls.append(a)
        return json.dumps({"text": "今天天气不错", "visibility": "public"}, ensure_ascii=False)

    monkeypatch.setattr(post, "call_aux", fake_aux)
    return calls


def _seed_today(db, bot, n, ts):
    for i in range(n):
        db.insert_moment(bot, ts, "旧圈%d" % i, None, {}, "news_react")


def _maybe(bot_cfg, global_cfg=None, **kw):
    from moments import post
    out = {}
    mid = post.maybe_post_moment(
        BOT, bot_cfg, NOW, _situation(hobby={"name": "想分享：测试", "effect": "", "kind": "obsession"}),
        _world(), None, 0.9, {}, 120, global_cfg or {}, out=out, **kw)
    return mid, out


def test_daily_zero_blocks_even_force(tmp_db, fake_llm):
    for force in (False, True):
        mid, out = _maybe({"moments_daily_limit": 0}, force=force)
        assert mid is None and out["skip"] == "daily_limit(0)", force
    mid, out = _maybe({}, {"moments": {"daily_post_limit_per_bot": 0}}, force=True)
    assert mid is None and out["skip"] == "daily_limit(0)"
    assert fake_llm == [] and tmp_db.count_moments_today(BOT, NOW.strftime("%Y-%m-%d")) == 0


def test_daily_full_force_still_limited_ignore_daily_limit_posts(tmp_db, fake_llm):
    _seed_today(tmp_db, BOT, 5, NOW_TS - 6 * 3600)
    mid, out = _maybe({}, force=True)
    assert mid is None and out["skip"] == "daily_limit(3)"            # force 仍受当天上限（行为 4）
    mid, out = _maybe({"moments_daily_limit": 999}, force=True)       # 旧旁门 999 越界 → 当没设
    assert mid is None and out["skip"] == "daily_limit(3)"
    assert fake_llm == []
    for force in (False, True):
        mid, out = _maybe({"moments_daily_limit": 0}, force=force, ignore_daily_limit=True)
        assert isinstance(mid, int), (force, out)
    assert len(fake_llm) == 2
    assert tmp_db.count_moments_today(BOT, NOW.strftime("%Y-%m-%d")) == 7


def test_daily_bot_override_wins_over_global(tmp_db, fake_llm):
    _seed_today(tmp_db, BOT, 2, NOW_TS - 6 * 3600)
    mid, out = _maybe({"moments_daily_limit": 2}, {"moments": {"daily_post_limit_per_bot": 10}})
    assert mid is None and out["skip"] == "daily_limit(2)"
    mid, _ = _maybe({"moments_daily_limit": "5"}, {"moments": {"daily_post_limit_per_bot": 1}})
    assert isinstance(mid, int)


# ---------------------------------------------------------------- 两个脚本：当天已满仍能发


@pytest.fixture
def script_env(tmp_path, monkeypatch, tmp_db, fake_llm):
    configs = tmp_path / "configs"
    configs.mkdir()
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(configs))
    (configs / "_global.yml").write_text("moments:\n  enabled: true\n", encoding="utf-8")
    from generators import situation as sit_mod, world as world_mod, wildcard as wc_mod
    monkeypatch.setattr(sit_mod, "collect", lambda *a, **k: _situation())
    monkeypatch.setattr(world_mod, "collect_world", lambda *a, **k: _world())
    monkeypatch.setattr(wc_mod, "pick_today_wildcard", lambda *a, **k: None)
    return configs


def _write_bot(configs, extra=""):
    (configs / (BOT + ".yml")).write_text("name: 测试\n" + extra, encoding="utf-8")


@pytest.mark.parametrize("bot_extra", ["", "moments_daily_limit: 0\n"], ids=["default_limit", "override_0"])
def test_bot_post_moment_script_posts_when_daily_full(script_env, tmp_db, fake_llm, monkeypatch,
                                                      capsys, bot_extra):
    from scripts import bot_post_moment as script
    monkeypatch.setattr(script, "mood_at", lambda *a, **k: (0.5, {}))
    _write_bot(script_env, bot_extra)
    _seed_today(tmp_db, BOT, 5, int(time.time()))
    monkeypatch.setattr(sys, "argv", ["bot_post_moment.py", BOT, "下午的咖啡"])
    script.main()                                        # 失败会 sys.exit(3)
    assert "OK moment_id=" in capsys.readouterr().out
    assert len(fake_llm) == 1
    assert tmp_db.count_moments_today(BOT, datetime.datetime.now().strftime("%Y-%m-%d")) == 6


@pytest.mark.parametrize("bot_extra", ["", "moments_daily_limit: 0\n"], ids=["default_limit", "override_0"])
def test_force_moment_script_posts_when_daily_full(script_env, tmp_db, fake_llm, monkeypatch,
                                                   capsys, bot_extra):
    from scripts import force_moment as script
    monkeypatch.setattr(script, "mood_at", lambda *a, **k: (0.9, {}))   # 0.9 → mood_extreme 有素材
    _write_bot(script_env, bot_extra)
    _seed_today(tmp_db, BOT, 5, int(time.time()))
    monkeypatch.setattr(sys, "argv", ["force_moment.py", BOT])
    script.main()
    assert "朋友圈 id=" in capsys.readouterr().out
    assert len(fake_llm) == 1
    assert tmp_db.count_moments_today(BOT, datetime.datetime.now().strftime("%Y-%m-%d")) == 6
