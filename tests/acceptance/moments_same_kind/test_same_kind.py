# -*- coding: utf-8 -*-
"""moments-same-kind-force 验收测试（黑盒）。

依据：`.devflow/moments-same-kind-force-接口.md` 的行为清单与错误契约，一条行为一个用例。

怎么做到"只走对外接口"：
  * 只调用公开入口 `moments.post.maybe_post_moment`；
  * 数据库按接口文件的测试指引替换 `db.list_moments` / `db.count_moments_today` /
    `db.insert_moment`，返回构造好的历史数据，不碰真实库；
  * 文案生成用 `call_aux` 桩替代，返回固定 JSON，绝不发真实 LLM 请求；
  * 断言口径是 `maybe_post_moment` 的返回值、是否发生生成/落库、以及公开的诊断输出
    （例如 "same_kind(wildcard) within 4h" / "no material"）。

注意：接口文件表格里 skip 分支写的是返回 `(False, "skip", ...)` 元组，但当前实现对
skip 的实际行为是打印原因并返回 None（"缺省 / False 与现在完全一致"）。本文件按可观测
行为断言：结果为 None、原因出现在输出里、且没有发生生成与落库。
"""
import datetime
import inspect
import json

import db
import moments.post as post_module
from moments.post import maybe_post_moment
from generators.situation import Activity, SituationContext
from generators.wildcard import WildcardPick
from generators.world import WorldSnapshot

NOW = datetime.datetime(2026, 10, 28, 14, 0)
NOW_TS = int(NOW.timestamp())
BOT_ID = "tbot"
FOUR_HOURS = 4 * 3600
DEFAULT_MOMENT_ID = 4242


def _moment_row(ts_offset_seconds):
    """距离 now 指定秒数的一条 wildcard 圈。"""
    return {
        "id": 1,
        "bot_id": BOT_ID,
        "ts": NOW_TS - ts_offset_seconds,
        "created_at": NOW - datetime.timedelta(seconds=ts_offset_seconds),
        "kind": "wildcard",
        "moment_kind": "wildcard",
        "text": "历史朋友圈",
        "visibility": "public",
    }


def _recent_same_kind_row():
    """2 小时前的一条同 kind（wildcard）圈：在 4 小时闸内、在 60 分钟静默期外。"""
    return _moment_row(2 * 3600)


class StubDb:
    """`maybe_post_moment` 依赖的数据库替身：只实现接口约定的查询与写入。"""

    def __init__(self, rows=(), count_today=0,
                 insert_id=DEFAULT_MOMENT_ID, raise_on_same_kind_query=False):
        self.rows = list(rows)
        self.count_today = count_today
        self.insert_id = insert_id
        self.raise_on_same_kind_query = raise_on_same_kind_query
        self.insert_calls = []

    def list_moments(self, bot_id=None, limit=50, since_ts=0):
        window = NOW_TS - since_ts
        if self.raise_on_same_kind_query and FOUR_HOURS - 300 <= window <= FOUR_HOURS + 300:
            raise RuntimeError("stub: database query failed")
        rows = [row for row in self.rows if row["ts"] >= since_ts]
        return rows[:limit] if limit else rows

    def count_moments_today(self, bot_id, date_str):
        return self.count_today

    def insert_moment(self, *args, **kwargs):
        self.insert_calls.append((args, kwargs))
        return self.insert_id


class StubLlm:
    """文案生成依赖桩：记录调用并返回合法 JSON，绝不发真实请求。"""

    def __init__(self, text="今天天气不错"):
        self.reply = json.dumps({"text": text, "visibility": "public"}, ensure_ascii=False)
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.reply


def _bot_cfg(tmp_path):
    channel = tmp_path / "channel"
    channel.mkdir(exist_ok=True)
    return {
        "id": BOT_ID,
        "display_name": "测试",
        "chat_id": "42",
        "bot_channel_path": str(channel),
        "moments": {"enabled": True, "daily_post_limit_per_bot": 5,
                    "image_generation": {"enabled": False}},
        "moments_daily_limit": 5,
    }


def _global_cfg():
    return {
        "user_display_name": "我",
        "user_bio": "签名",
        "moments": {"enabled": True, "daily_post_limit_per_bot": 5,
                    "image_generation": {"enabled": False}},
    }


def _situation():
    return SituationContext(
        recurring=Activity(name="自由时间", description="在家放松", state="free"),
        sporadic=None, hobby=None)


def _wildcard():
    return WildcardPick(card_id=1, card="今天心情很好", emotion="开心")


def _invoke(monkeypatch, tmp_path, stub_db, stub_llm, *, force,
            wildcard, mood=0.9, mood_factors=None):
    monkeypatch.setattr(db, "list_moments", stub_db.list_moments)
    monkeypatch.setattr(db, "count_moments_today", stub_db.count_moments_today)
    monkeypatch.setattr(db, "insert_moment", stub_db.insert_moment)
    monkeypatch.setattr(post_module, "call_aux", stub_llm)

    kwargs = {
        "bot_id": BOT_ID,
        "bot_cfg": _bot_cfg(tmp_path),
        "now": NOW,
        "situation": _situation(),
        "world": WorldSnapshot(),
        "wildcard": wildcard,
        "mood": mood,
        "mood_factors": ["开心"] if mood_factors is None else mood_factors,
        "since_user_min": 120,
        "global_cfg": _global_cfg(),
        "jiwen_info": None,
        "force": force,
    }
    # 接口说明 db_module 是既有依赖注入点；实现接受该参数就传桩，避免碰真实库。
    # 实现不接受时，上面已把 db 模块的查询/写入函数换成了桩。
    if "db_module" in inspect.signature(maybe_post_moment).parameters:
        kwargs["db_module"] = stub_db
    return maybe_post_moment(**kwargs)


def _output(capsys):
    captured = capsys.readouterr()
    return captured.out + captured.err


# ── 行为 1：force=True，4 小时内有同 kind 的圈 → 不被第 6 步挡住，继续生成 ──

def test_force_true_四小时内有同kind_不被第6步挡住(monkeypatch, tmp_path, capsys):
    stub_db = StubDb(rows=[_recent_same_kind_row()])
    stub_llm = StubLlm()

    result = _invoke(monkeypatch, tmp_path, stub_db, stub_llm,
                     force=True, wildcard=_wildcard())

    output = _output(capsys)
    assert result == DEFAULT_MOMENT_ID
    assert result != (False, "skip", "same_kind(wildcard) within 4h")
    assert "same_kind" not in output, "force=True 仍被同 kind 4 小时闸挡住：%r" % output
    assert len(stub_llm.calls) == 1, "没有被挡住时应调用一次文案生成"


# ── 行为 2：force=False，4 小时内有同 kind 的圈 → 被第 6 步挡住 ──

def test_force_false_四小时内有同kind_返回None且不生成(monkeypatch, tmp_path, capsys):
    stub_db = StubDb(rows=[_recent_same_kind_row()])
    stub_llm = StubLlm()

    result = _invoke(monkeypatch, tmp_path, stub_db, stub_llm,
                     force=False, wildcard=_wildcard())

    output = _output(capsys)
    assert result is None
    assert "same_kind(wildcard) within 4h" in output, "没有出现同 kind 闸的诊断：%r" % output
    assert stub_llm.calls == [], "force=False 不该走到文案生成"
    assert stub_db.insert_calls == [], "force=False 不该落库"


# ── 行为 3：force=True，无契机 → 仍被契机判定挡住（只跳过 4 小时闸） ──

def test_force_true_无契机_仍按无素材挡住(monkeypatch, tmp_path, capsys):
    stub_db = StubDb()  # 没有历史圈，排除第 6 步干扰
    stub_llm = StubLlm()

    result = _invoke(monkeypatch, tmp_path, stub_db, stub_llm,
                     force=True, wildcard=None, mood=0.5, mood_factors={})

    output = _output(capsys)
    assert result is None
    assert "no material" in output, "force=True 绕过了契机判定：%r" % output
    assert stub_llm.calls == [], "无契机时不该走到文案生成"


# ── 行为 4：force=True，每日上限已到 → 仍返回 None（只跳过 4 小时闸） ──

def test_force_true_每日上限已到_返回None且不生成(monkeypatch, tmp_path, capsys):
    # 每日上限是 5（bot_cfg / global_cfg 里都写死 5），count_today=5 即"已到"
    stub_db = StubDb(recent_same_kind=_recent_same_kind_row(), count_today=5)
    stub_llm = StubLlm()

    result = _invoke(monkeypatch, tmp_path, stub_db, stub_llm,
                     force=True, wildcard=_wildcard())

    output = _output(capsys)
    assert result is None
    assert "same_kind" not in output, "第 6 步没有被跳过：%r" % output
    assert stub_llm.calls == [], "每日上限已到不该走到文案生成"


# ── 行为补充：force=True 仍不跳过静默期（接口「其余闸不变」） ──

def test_force_true_静默期内_仍按min_interval挡住(monkeypatch, tmp_path, capsys):
    # 30 分钟前刚发过圈，落在 60 分钟静默期内；force 只跳过 4 小时同 kind 闸
    stub_db = StubDb(rows=[_moment_row(30 * 60)])
    stub_llm = StubLlm()

    result = _invoke(monkeypatch, tmp_path, stub_db, stub_llm,
                     force=True, wildcard=_wildcard())

    output = _output(capsys)
    assert result is None
    assert "min_interval" in output, "force=True 绕过了静默期：%r" % output
    assert stub_llm.calls == [], "静默期内不该走到文案生成"


# ── 行为 5：force=True 且成功 → 返回新的 moment_id ──

def test_force_true_成功_返回新moment_id(monkeypatch, tmp_path):
    stub_db = StubDb(insert_id=7777)
    stub_llm = StubLlm()

    result = _invoke(monkeypatch, tmp_path, stub_db, stub_llm,
                     force=True, wildcard=_wildcard())

    assert result == 7777, "返回的应是本次落库产生的新 moment_id"
    assert len(stub_db.insert_calls) == 1, "成功后应落库一次"


# ── 错误契约 1：第 6 步数据库查询异常 → 吞掉，继续往下走 ──

def test_数据库查询异常_被吞掉并继续生成(monkeypatch, tmp_path, capsys):
    stub_db = StubDb(insert_id=5555, raise_on_same_kind_query=True)
    stub_llm = StubLlm()

    result = _invoke(monkeypatch, tmp_path, stub_db, stub_llm,
                     force=False, wildcard=_wildcard())

    output = _output(capsys)
    assert result == 5555, "数据库查询异常应被吞掉并继续生成，而不是中断或返回 None"
    assert "same_kind" not in output
    assert len(stub_llm.calls) == 1


# ── 错误契约 2：force 非布尔 → 按 truthy/falsy 判断，不额外校验 ──

def test_force真值字符串_按True处理_跳过第6步(monkeypatch, tmp_path, capsys):
    stub_db = StubDb(rows=[_recent_same_kind_row()])
    stub_llm = StubLlm()

    result = _invoke(monkeypatch, tmp_path, stub_db, stub_llm,
                     force="yes", wildcard=_wildcard())

    output = _output(capsys)
    assert result == DEFAULT_MOMENT_ID
    assert "same_kind" not in output, "真值字符串 force 仍被同 kind 闸挡住：%r" % output


def test_force假值字符串_按False处理_同kind仍挡住(monkeypatch, tmp_path, capsys):
    stub_db = StubDb(rows=[_recent_same_kind_row()])
    stub_llm = StubLlm()

    result = _invoke(monkeypatch, tmp_path, stub_db, stub_llm,
                     force="", wildcard=_wildcard())

    output = _output(capsys)
    assert result is None
    assert "same_kind(wildcard) within 4h" in output, "假值字符串 force 没有按 False 处理：%r" % output
    assert stub_llm.calls == []
