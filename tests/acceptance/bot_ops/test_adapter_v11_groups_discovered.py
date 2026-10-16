# -*- coding: utf-8 -*-
"""适配服务 v1.1 验收：刚发现的群 GET /adapter/v1/bots/{bot_id}/groups/discovered（约定 v1.1 5.20）。

网关把 bot 在群里的身份变化、没加入的群里被挡下的消息记进 <bot>/state/groups-seen.json（形状见
gateway/src/telegram/groups_seen.ts）；适配服务据此列给管理台，去掉已经加入的、已经不在群里的。
"""
import json

import pytest

from . import adapter_util as au
from .adapter_util import adapter, console_guard  # noqa: F401  夹具

JOINED = "-1001000000001"
NEW_A = "-1002000000002"
NEW_B = "-1003000000003"
GONE = "-1004000000004"
KICKED = "-1005000000005"
LIMITED = "-1006000000006"


def discovered_url(bot_id):
    return "%s/discovered" % au.groups_url(bot_id)


def seen(at, **over):
    row = {"title": "群", "type": "supergroup", "status": "member", "added_by_owner": None,
           "first_seen": "2026-10-15T08:00:00Z", "last_seen": at, "migrated_from": None}
    row.update(over)
    return row


def write_seen(bot, data):
    p = bot.botdir / "state" / "groups-seen.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    p.chmod(0o600)


def serve_caps(gw, can_read_all, can_join_groups):
    return gw.on("GET", au.GW_HEALTH, 200, {"ok": True, "telegram": {
        "conflict": False, "username": "alpha_fake_bot", "can_read_all": can_read_all, "can_join_groups": can_join_groups}})


@pytest.fixture
def alpha(adapter):
    gw = serve_caps(adapter.model_gateway(), False, True)
    bot = adapter.running_bot("botalpha", gw)
    au.write_access(bot, {"allowFrom": [au.CHAT_ID], "groups": {JOINED: {"requireMention": True}}})
    return bot


def test_列出没加入的群_去掉已加入的和已经不在群里的_按最后看到从新到旧(adapter, alpha):
    write_seen(alpha, {
        JOINED: seen("2026-10-16T09:00:00Z"),
        NEW_A: seen("2026-10-16T07:00:00Z", title="家人群", added_by_owner=True),
        NEW_B: seen("2026-10-16T08:30:00Z", title="同事群", status="administrator", added_by_owner=False,
                    migrated_from="-555"),
        GONE: seen("2026-10-16T08:40:00Z", status="left"),
        KICKED: seen("2026-10-16T08:50:00Z", status="kicked"),
        LIMITED: seen("2026-10-16T06:00:00Z", status="restricted"),
    })
    body = adapter.ok(adapter.call("GET", discovered_url("botalpha")))
    assert body["can_read_all"] is False and body["can_join_groups"] is True, body
    rows = body["groups"]
    assert [r["chat_id"] for r in rows] == [NEW_B, NEW_A, LIMITED], rows
    b, a, lim = rows
    assert a == {"chat_id": NEW_A, "title": "家人群", "type": "supergroup", "status": "member", "added_by_owner": True,
                 "sees_all": False, "first_seen": "2026-10-15T08:00:00Z", "last_seen": "2026-10-16T07:00:00Z",
                 "migrated_from": None, "left_in_console_at": None}, a
    assert b["sees_all"] is True and b["added_by_owner"] is False and b["migrated_from"] == "-555", b   # 管理员看得到全部
    assert lim["status"] == "restricted" and lim["sees_all"] is False, lim


def test_隐私模式关了_所有群都看得到全部消息(adapter):
    gw = serve_caps(adapter.model_gateway(), True, True)
    bot = adapter.running_bot("botalpha", gw)
    au.write_access(bot, {"allowFrom": [au.CHAT_ID]})
    write_seen(bot, {NEW_A: seen("2026-10-16T07:00:00Z")})
    body = adapter.ok(adapter.call("GET", discovered_url("botalpha")))
    assert body["groups"][0]["sees_all"] is True, body


def test_网关不在跑_两项能力为null_照样列记录(adapter):
    bot = adapter.bot("botalpha", gw_port=au.free_port())
    au.write_access(bot, {"allowFrom": [au.CHAT_ID]})
    write_seen(bot, {NEW_A: seen("2026-10-16T07:00:00Z", status="administrator")})
    body = adapter.ok(adapter.call("GET", discovered_url("botalpha")))
    assert body["can_read_all"] is None and body["can_join_groups"] is None, body
    assert [r["chat_id"] for r in body["groups"]] == [NEW_A] and body["groups"][0]["sees_all"] is True, body


def test_停用的bot_不去问网关_两项能力为null_照样列记录(adapter):
    gw = serve_caps(adapter.model_gateway(), True, True)
    bot = adapter.running_bot("botalpha", gw)
    au.set_cfg(bot, enabled=False)
    au.write_access(bot, {"allowFrom": [au.CHAT_ID]})
    write_seen(bot, {NEW_A: seen("2026-10-16T07:00:00Z")})
    body = adapter.ok(adapter.call("GET", discovered_url("botalpha")))
    assert body["can_read_all"] is None and body["can_join_groups"] is None, body
    assert [r["chat_id"] for r in body["groups"]] == [NEW_A], body
    assert not [r for r in gw.requests if au.GW_HEALTH in str(r)], gw.requests


@pytest.mark.parametrize("content", [None, "{坏的", "[]"], ids=["没有记录文件", "文件坏了", "不是对象"])
def test_没有记录或记录坏了_回空列表(adapter, alpha, content):
    if content is not None:
        (alpha.botdir / "state" / "groups-seen.json").write_text(content, encoding="utf-8")
    body = adapter.ok(adapter.call("GET", discovered_url("botalpha")))
    assert body["groups"] == [], body


def test_最多回50条_留最近看到的(adapter, alpha):
    data = {"-10070000%05d" % i: seen("2026-10-16T%02d:%02d:00Z" % (i // 60, i % 60)) for i in range(60)}
    write_seen(alpha, data)
    rows = adapter.ok(adapter.call("GET", discovered_url("botalpha")))["groups"]
    assert len(rows) == 50, len(rows)
    assert rows[0]["chat_id"] == "-1007000000059" and rows[-1]["chat_id"] == "-1007000000010", (rows[0], rows[-1])


def test_在管理台退群_群重新出现并带上退群时间_再加入就不列了(adapter, alpha):
    write_seen(alpha, {JOINED: seen("2026-10-16T09:00:00Z", title="已加入的群")})
    assert adapter.ok(adapter.call("GET", discovered_url("botalpha")))["groups"] == []
    adapter.ok(adapter.call("POST", au.groups_url("botalpha"), {"chat_id": JOINED, "join": False}))
    rows = adapter.ok(adapter.call("GET", discovered_url("botalpha")))["groups"]
    assert [r["chat_id"] for r in rows] == [JOINED], rows
    left_at = rows[0]["left_in_console_at"]
    assert isinstance(left_at, str) and left_at.endswith("Z") and len(left_at) == 20, rows
    adapter.ok(adapter.call("POST", au.groups_url("botalpha"), {"chat_id": JOINED, "join": True}))
    assert adapter.ok(adapter.call("GET", discovered_url("botalpha")))["groups"] == []
    adapter.ok(adapter.call("POST", au.groups_url("botalpha"), {"chat_id": JOINED, "join": False}))
    again = adapter.ok(adapter.call("GET", discovered_url("botalpha")))["groups"]
    assert again[0]["left_in_console_at"] is not None, again


def test_没有这只bot回404_能力键报true_只许GET(adapter, alpha):
    adapter.expect_error(adapter.call("GET", discovered_url("nobody")), 404, "unknown_bot")
    meta = adapter.ok(adapter.call("GET", au.PREFIX + "/meta"))
    assert meta["capabilities"].get("bot.groups_discovered") is True, meta
    resp = adapter.call("POST", discovered_url("botalpha"), {})
    assert resp.status_code == 405, resp.status_code


def test_记退群时间失败_退群照样生效_不回500(adapter, alpha):
    (alpha.botdir / "state" / "groups-console-left.json").mkdir(parents=True)
    adapter.ok(adapter.call("POST", au.groups_url("botalpha"), {"chat_id": JOINED, "join": False}))
    assert JOINED not in (au.read_access(alpha).get("groups") or {})
