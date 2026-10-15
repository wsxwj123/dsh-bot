# -*- coding: utf-8 -*-
"""适配服务 v1.0 补充验收：bots() 的四个新键（13.1）、5.8 启停的 op_id 与迁出标记（13.2）。

依据：INTERFACE-适配服务.md 9.2、13.1、13.2。不读实现。
标记一行里 at= 的时间写法接口没写死，这里只验它是一段不带空白的字。
管理台页面的启用开关经适配服务挂着的 /hub/api 那条（与 8765 同一个处理函数）来按。
"""
import os
import re

import pytest

from . import adapter_util as au
from .adapter_util import adapter, console_guard  # noqa: F401  夹具

MARK_CLAUDE = "to=claude op=- at=20261015T051700Z\n"


def test_bots_四个新键_在跑的bot_username取网关(adapter):
    gw = au.serve_health(adapter.model_gateway(), username="alpha_live_bot")
    bot = adapter.running_bot("botalpha", gw)
    au.set_cfg(bot, telegram_username="alpha_cfg_bot")
    adapter.launchctl.set_unit("botalpha", installed=True, running=True, pid=4242)
    row = au.rows_by_id(adapter)["botalpha"]
    assert au.ROW_KEYS_V10S <= set(row), "13.1 的四个键缺了：%r" % (au.ROW_KEYS_V10S - set(row))
    assert (row["enabled"], row["polling"], row["username"], row["moved_to"]) == \
        (True, True, "alpha_live_bot", None), row


MARKERS = [
    ("没有标记", None, None),
    ("去向claude", "to=claude op=%s at=20261015T051700Z\n" % au.op_id(), "claude"),
    ("去向pending", "to=pending op=- at=20261015T051700Z\n", "pending"),
    ("去向不认识", "to=dsh op=- at=20261015T051700Z\n", "pending"),
    ("格式不对", "moved somewhere\n", "pending"),
]


@pytest.mark.parametrize("text,want", [m[1:] for m in MARKERS], ids=[m[0] for m in MARKERS])
def test_bots_停用的bot_polling为假_username取配置_moved_to按标记(adapter, text, want):
    alpha = adapter.bot("botalpha", enabled=False)
    au.set_cfg(alpha, telegram_username="alpha_cfg_bot")
    adapter.bot("botbravo", enabled=False)
    if text is not None:
        au.put_file(au.moved_marker(adapter.sandbox, "botalpha"), text)
    rows = au.rows_by_id(adapter)
    a, b = rows["botalpha"], rows["botbravo"]
    assert (a["enabled"], a["polling"], a["username"], a["moved_to"]) == \
        (False, False, "alpha_cfg_bot", want), a
    assert (b["username"], b["moved_to"]) == (None, None), "没记用户名、没有标记的都应为 null：%r" % (b,)


@pytest.mark.parametrize("moved_to,kind", [("pending", None), ("claude", "switch")],
                         ids=["pending_不带op_id", "claude_带op_id"])
def test_停用_带moved_to_写标记0600_一行to_op_at(adapter, moved_to, kind):
    bot = adapter.bot("botalpha")
    body = {"enabled": False, "moved_to": moved_to}
    if kind:
        body["op_id"] = au.op_id(kind)
    got = adapter.ok(adapter.call("POST", au.enabled_url("botalpha"), body))
    assert (got["enabled"], got["running"]) == (False, False), got
    marker = au.moved_marker(adapter.sandbox, "botalpha")
    assert au.mode_of(marker) == 0o600, "标记应是 0600"
    text = marker.read_text(encoding="utf-8")
    want = r"^to=%s op=%s at=\S+\n?\Z" % (moved_to, re.escape(body.get("op_id", "-")))
    assert re.match(want, text), "标记应是一行 to=<去向> op=<op_id 或 -> at=<UTC 时间>：%r" % text
    assert au.read_cfg(bot).get("enabled") is False
    assert au.rows_by_id(adapter)["botalpha"]["moved_to"] == moved_to


def test_停用_不给moved_to标记不动_给null删标记_已停着照常200(adapter):
    adapter.bot("botalpha", enabled=False)
    marker = au.put_file(au.moved_marker(adapter.sandbox, "botalpha"), MARK_CLAUDE)
    before = au.file_mark(marker)
    adapter.ok(adapter.call("POST", au.enabled_url("botalpha"), {"enabled": False, "op_id": au.op_id()}))
    assert au.file_mark(marker) == before, "没给 moved_to 时标记不该动"
    got = adapter.ok(adapter.call("POST", au.enabled_url("botalpha"), {"enabled": False, "moved_to": None}))
    assert (got["enabled"], got["running"]) == (False, False), got
    assert not os.path.lexists(str(marker)), "moved_to 为 null 应删掉标记"
    assert adapter.launchctl.actions() == [], "已经停着的再停不该动启动项"
    assert au.rows_by_id(adapter)["botalpha"]["moved_to"] is None


@pytest.mark.parametrize("text,body", [
    (MARK_CLAUDE, {"enabled": True}),
    ("not a marker\n", {"enabled": True}),
    ("to=pending op=- at=20261015T051700Z\n", {"enabled": True, "import_from": "oldchan"}),
], ids=["去向claude", "标记读不懂也拦", "带import_from的切进"])
def test_启用_有标记_409_moved_什么都不做(adapter, text, body):
    sb = adapter.sandbox
    bot = adapter.bot("botalpha", enabled=False)
    au.put_file(au.moved_marker(sb, "botalpha"), text)
    before = au.file_mark(bot.yml), au.tree_mark(sb.agents), au.file_mark(au.moved_marker(sb, "botalpha"))
    adapter.expect_error(adapter.call("POST", au.enabled_url("botalpha"), body), 409, "moved")
    assert (au.file_mark(bot.yml), au.tree_mark(sb.agents), au.file_mark(au.moved_marker(sb, "botalpha"))) == before
    assert adapter.launchctl.actions() == [], "有标记时不该动启动项"


BAD = [
    ("启用带moved_to字符串", {"enabled": True, "moved_to": "claude"}),
    ("启用带moved_to为null", {"enabled": True, "moved_to": None}),
    ("停用moved_to是本侧名字", {"enabled": False, "moved_to": "dsh"}),
    ("停用moved_to是别的值", {"enabled": False, "moved_to": "other"}),
    ("停用moved_to不是字符串", {"enabled": False, "moved_to": 1}),
    ("op_id格式不对", {"enabled": False, "op_id": "switch-1a2b3c4d"}),
    ("op_id种类不认识", {"enabled": False, "op_id": au.op_id("switchx")}),
    ("op_id十六进制大写", {"enabled": False, "op_id": au.op_id("switch", "1A2B3C4D")}),
    ("op_id为null", {"enabled": False, "op_id": None}),
]


@pytest.mark.parametrize("body", [b for _n, b in BAD], ids=[n for n, _b in BAD])
def test_启停_请求体不合规_422_什么都不做(adapter, body):
    bot = adapter.bot("botalpha")
    before = au.file_mark(bot.yml)
    adapter.expect_error(adapter.call("POST", au.enabled_url("botalpha"), body), 422, "invalid")
    assert au.file_mark(bot.yml) == before, "422 时配置不该动"
    assert not os.path.lexists(str(au.moved_marker(adapter.sandbox, "botalpha"))), "422 时不该写标记"
    assert adapter.launchctl.calls() == []


def test_启停_op_id五种种类都认(adapter):
    adapter.bot("botalpha", enabled=False)
    for kind in au.OP_KINDS:
        got = adapter.ok(adapter.call("POST", au.enabled_url("botalpha"),
                                      {"enabled": False, "op_id": au.op_id(kind)}))
        assert got["enabled"] is False, (kind, got)


def test_管理台页面启用开关_有标记拒绝_配置与启动项不动(adapter):
    bot = adapter.bot("botalpha", enabled=False)
    au.put_file(au.moved_marker(adapter.sandbox, "botalpha"), MARK_CLAUDE)
    before = au.file_mark(bot.yml)
    resp = adapter.call("POST", "/hub/api/bots/botalpha/enabled", {"enabled": True})
    assert resp.status_code == 409, "有标记时页面开关应拒绝启用：%s %s" % (
        resp.status_code, resp.get_data(as_text=True)[:200])
    assert au.file_mark(bot.yml) == before, "拒绝时配置不该动"
    assert adapter.launchctl.calls() == [], "拒绝时不该动启动项"
