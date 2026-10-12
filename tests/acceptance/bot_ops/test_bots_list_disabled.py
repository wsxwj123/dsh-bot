# -*- coding: utf-8 -*-
"""§2 GET /hub/api/bots：停用的行不凭心跳说它在线（接口约定第 8 稿，状态规则第 2 步）。

起因：刚停掉的 bot 心跳还没过期，列表里会同时显示「已停用」和「在线」，最长要等 15 秒。
第 8 稿：停用的行先问一次 `launchctl print`。它说没在运行 → 离线，心跳再新鲜也不看；
它说在运行 → 接着按心跳往下判（配置说停、进程却还在，就该如实显示）。

判定输入：配置里的 `enabled`、心跳文件、假 launchctl 的 print 输出。
"""
import time

import pytest

from . import botops_util as u

BOT = "xiaoyu"
UNIT = "com.dsh-bot.%s" % BOT


def _row(client, bot_id=BOT):
    r = client.get("/hub/api/bots")
    assert r.status_code == 200, (r.status_code, r.get_json())
    body = r.get_json()
    rows = [b for b in body["bots"] if b["id"] == bot_id]
    assert len(rows) == 1, body["bots"]
    return rows[0]


@pytest.fixture(autouse=True)
def _fakes_in_place(launchctl, bun):
    """假 launchctl、假 bun 都在位：停用的行会问 print，不许落到毒丸上。"""


# ============================================================ 约定点名的三例

def test_停用且心跳新鲜_print说找不到服务_是offline(client, sandbox, heartbeat, launchctl):
    u.dsh_bot(sandbox.dsh_cfg, BOT, enabled=False)
    heartbeat.write(BOT, pid=4242)
    launchctl.set_default(installed=False)
    assert _row(client)["state"] == "offline", "刚停掉的 bot 凭着还没过期的心跳显示成了在线"


def test_停用且心跳新鲜_print说找不到服务_pid是null(client, sandbox, heartbeat, launchctl):
    u.dsh_bot(sandbox.dsh_cfg, BOT, enabled=False)
    heartbeat.write(BOT, pid=4242)
    launchctl.set_default(installed=False)
    assert _row(client)["pid"] is None


def test_停用且心跳新鲜_print说在运行_是online(client, sandbox, heartbeat, launchctl):
    """配置说停、进程却还在：如实显示在线（页面据此给出「再停一次」，§13a 第 7 条）。"""
    u.dsh_bot(sandbox.dsh_cfg, BOT, enabled=False)
    heartbeat.write(BOT, pid=4242)
    launchctl.set_default(installed=True, running=True, pid=4242)
    row = _row(client)
    assert row["state"] == "online", row
    assert row["pid"] == 4242, row


def test_启用且心跳新鲜_是online且launchctl一次都没被调用(client, sandbox, heartbeat, launchctl):
    """对照：启用的行与第 7 稿相同，心跳新鲜就是在线，不问 launchd。"""
    u.dsh_bot(sandbox.dsh_cfg, BOT)
    heartbeat.write(BOT, pid=4242)
    assert _row(client)["state"] == "online"
    assert launchctl.calls() == [], ("启用的行心跳新鲜时不该问 launchd", launchctl.calls())


# ============================================================ 第 2 步的几个边

def test_停用且心跳新鲜_print说装着但没在跑_是offline(client, sandbox, heartbeat, launchctl):
    """「状态不是 running」同样算没在运行。"""
    u.dsh_bot(sandbox.dsh_cfg, BOT, enabled=False)
    heartbeat.write(BOT, pid=4242)
    launchctl.set_default(installed=True, running=False, pid=None)
    row = _row(client)
    assert row["state"] == "offline", row
    assert row["pid"] is None, row


def test_停用且心跳新鲜_问的是这只的主单元且只用print(client, sandbox, heartbeat, launchctl):
    u.dsh_bot(sandbox.dsh_cfg, BOT, enabled=False)
    heartbeat.write(BOT, pid=4242)
    launchctl.set_default(installed=False)
    _row(client)
    calls = launchctl.calls()
    assert calls, "停用的行没有问 launchd"
    assert {c["cmd"] for c in calls} == {"print"}, ("列表只许发只读的 print", calls)
    assert {c["label"] for c in calls} == {UNIT}, calls


def test_停用且没有心跳_print说在运行_是starting且pid取launchd的(
        client, sandbox, launchctl):
    """第 2 步放行之后照旧往下判：心跳不新鲜、launchd 说 running → starting。"""
    u.dsh_bot(sandbox.dsh_cfg, BOT, enabled=False)
    launchctl.set_default(installed=True, running=True, pid=5555)
    row = _row(client)
    assert row["state"] == "starting", row
    assert row["pid"] == 5555, row


def test_停用且心跳新鲜_print在2秒预算内没回来_按offline(client, sandbox, heartbeat, launchctl):
    """§2：停用行多出来的这一次 print 同样走 2 秒预算，预算内没拿到结果的按 offline。"""
    u.dsh_bot(sandbox.dsh_cfg, BOT, enabled=False)
    heartbeat.write(BOT, pid=4242)
    launchctl.set_default(installed=True, running=True, pid=4242)
    launchctl.set_delay("print", 3.5)
    t0 = time.time()
    row = _row(client)
    took = time.time() - t0
    time.sleep(max(0.0, 4.0 - took))        # 收尾：等那次被拖慢的假 print 自己结束，别拖到下一条用例
    assert row["state"] == "offline", row
    assert took < 3.3, "列表被拖慢的 print 卡住了（%.1f 秒），应当在 2 秒预算后就回" % took


def test_停用的行_四列照旧是null且不发本机探活(client, sandbox, heartbeat, launchctl, dispatcher):
    u.dsh_bot(sandbox.dsh_cfg, BOT, enabled=False, dispatcher_port=dispatcher.port)
    heartbeat.write(BOT, pid=4242)
    launchctl.set_default(installed=False)
    row = _row(client)
    assert (row["phase"], row["queue_depth"], row["in_flight"]) == (None, None, None), row
    assert dispatcher.hits == [], "dsh 行发了本机探活（契约说不发）"
