# -*- coding: utf-8 -*-
"""适配服务验收：等空闲重启（graceful restart）。

依据 `.devflow/graceful-restart-接口.md`。只走对外接口：
- `POST /adapter/v1/bots/<id>/restart`
- `GET /adapter/v1/meta`
- bot 网关上的 `GET /v1/health`（用本目录现成的假网关）
不读实现。

排队后的后台轮询间隔约 1 秒，所以「等空闲再重启」的断言给 10 秒上限；
接口写明接口响应本身要 5 秒内返回，这条用墙钟断言。
"""
import json
import time

import pytest

from . import adapter_util as au
from .adapter_util import adapter, console_guard  # noqa: F401  夹具


# ------------------------------------------------------------ 小件

def _post(adapter, body=au.NO_BODY, *, bot="botalpha", raw=None):
    return adapter.call("POST", au.restart_url(bot), body, raw=raw)


def _health_requests(gw):
    """假网关收到的健康接口请求：用来确认「忙/闲」这个前提真被问过，断言不空转。"""
    return [r for r in gw.requests if (r["method"], r["path"]) == ("GET", au.GW_HEALTH)]


def _ready(adapter, turns_running):
    """摆一只可重启的 bot：网关健康可改、启动项不在跑、心跳一直在。

    返回 (网关, 健康状态字典)；把 health["turns_running"] 改掉即可切换忙/闲。
    """
    gw = adapter.gateway()
    adapter.bot("botalpha", gw_port=gw.port)
    adapter.launchctl.set_default(installed=True, running=False, pid=4242)
    adapter.heartbeat.tick("botalpha", pid=4242)
    health = {"turns_running": turns_running}
    gw.on_fn("GET", au.GW_HEALTH,
             lambda rec: (200, {"ok": True, "turns_running": health["turns_running"]}))
    return gw, health


def _wait_for(predicate, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def _wait_first_action(adapter, timeout=10.0):
    assert _wait_for(lambda: bool(adapter.launchctl.actions()), timeout), (
        "%.0f 秒内启动项一直没收到重启调用：%r" % (timeout, adapter.launchctl.calls()))


def _stable_action_count(adapter, window=1.0, timeout=8.0):
    """等启动项调用条数连续 window 秒不变，返回稳定后的条数。"""
    deadline = time.time() + timeout
    last = len(adapter.launchctl.actions())
    while time.time() < deadline:
        time.sleep(window)
        now = len(adapter.launchctl.actions())
        if now == last:
            return now
        last = now
    return last


# ------------------------------------------------------------ 行为清单 1

def test_强制重启_回ok且启动项收到这一只的调用(adapter):
    _ready(adapter, 0)
    body = adapter.ok(_post(adapter, {"force": True}))
    assert body["status"] == "ok", body
    assert body["message"] == "已重启", body
    actions = adapter.launchctl.actions()
    assert actions, "回了已重启，却没有对启动项管理程序发出任何动作"
    blob = json.dumps(actions, ensure_ascii=False)
    assert "botalpha" in blob, "启动项调用里没有这只 bot：%s" % blob


# ------------------------------------------------------------ 行为清单 2

def test_不带force_网关空闲_立即重启回ok且启动项收到调用(adapter):
    gw, _health = _ready(adapter, 0)
    body = adapter.ok(_post(adapter, {}))
    assert body["status"] == "ok", body
    adapter.human_message(body.get("message"))
    assert _health_requests(gw), "没有向网关问是否空闲，「网关空闲」这个前提没被验证：%r" % gw.requests
    assert adapter.launchctl.actions(), "网关空闲，却没有对启动项发出重启调用"


# ------------------------------------------------------------ 行为清单 3

def test_不带force_网关忙_5秒内回queued且当场不重启(adapter):
    gw, health = _ready(adapter, 2)
    started = time.time()
    resp = _post(adapter, {})
    took = time.time() - started
    body = adapter.ok(resp)
    assert body["status"] == "queued", body
    adapter.human_message(body.get("message"))
    assert took < 5.0, "网关忙时应 5 秒内回 queued，实际等了 %.1f 秒" % took
    assert _health_requests(gw), "没有向网关问是否空闲，「网关忙」这个前提没被验证：%r" % gw.requests
    assert adapter.launchctl.actions() == [], \
        "刚回 queued 就动了启动项：%r" % adapter.launchctl.calls()
    # 收尾：放成空闲让排队的这次落地，别把后台轮询留到用例之外。
    health["turns_running"] = 0
    _wait_for(lambda: bool(adapter.launchctl.actions()), 10.0)


# ------------------------------------------------------------ 行为清单 4

def test_排队后网关变空闲_10秒内后台重启(adapter):
    _gw, health = _ready(adapter, 2)
    body = adapter.ok(_post(adapter, {}))
    assert body["status"] == "queued", body
    assert adapter.launchctl.actions() == [], "还没空闲就重启了"
    health["turns_running"] = 0
    _wait_first_action(adapter, timeout=10.0)


# ------------------------------------------------------------ 行为清单 5

@pytest.mark.parametrize("case", ["没有端口文件", "端口没人听"])
def test_不带force_问不到网关_照常立即重启回ok(adapter, case):
    if case == "没有端口文件":
        adapter.bot("botalpha")                      # state 里只有 api.key，没有 api.port
    else:
        adapter.bot("botalpha", gw_port=au.free_port())      # 端口没人监听
    adapter.launchctl.set_default(installed=True, running=False, pid=4242)
    adapter.heartbeat.tick("botalpha", pid=4242)
    body = adapter.ok(_post(adapter, {}))
    assert body["status"] == "ok", body
    adapter.human_message(body.get("message"))
    assert adapter.launchctl.actions(), "问不到网关，却没有照常重启"


# ------------------------------------------------------------ 行为清单 6

BAD_HEALTH = {
    "缺turns_running": {"ok": True},
    "turns_running是字符串": {"ok": True, "turns_running": "2"},
    "turns_running是null": {"ok": True, "turns_running": None},
}


@pytest.mark.parametrize("reply", list(BAD_HEALTH.values()), ids=list(BAD_HEALTH))
def test_不带force_健康应答没有整数turns_running_按空闲立即重启(adapter, reply):
    gw = adapter.gateway()
    adapter.bot("botalpha", gw_port=gw.port)
    adapter.launchctl.set_default(installed=True, running=False, pid=4242)
    adapter.heartbeat.tick("botalpha", pid=4242)
    gw.on("GET", au.GW_HEALTH, 200, reply)
    body = adapter.ok(_post(adapter, {}))
    assert body["status"] == "ok", body
    assert _health_requests(gw), "没有向网关问是否空闲，这条用例的输入没被用到：%r" % gw.requests
    assert adapter.launchctl.actions(), "按空闲处理了，却没有照常重启"


# ------------------------------------------------------------ 行为清单 7

def test_force不是布尔_回422_invalid且不重启(adapter):
    _ready(adapter, 0)
    adapter.expect_error(_post(adapter, {"force": "yes"}), 422, "invalid")
    assert adapter.launchctl.actions() == [], \
        "校验没过却动了启动项：%r" % adapter.launchctl.calls()


# ------------------------------------------------------------ 行为清单 8

def test_meta_能力表声明支持等空闲重启(adapter):
    body = adapter.ok(adapter.call("GET", au.META))
    capabilities = body["capabilities"]
    assert capabilities["bot.restart.graceful"] is True, capabilities


# ------------------------------------------------------------ 行为清单 9

def test_网关一直忙时连发两次非force_都回queued且最终只重启一次(adapter):
    _gw, health = _ready(adapter, 2)
    first = adapter.ok(_post(adapter, {}))
    second = adapter.ok(_post(adapter, {}))
    assert first["status"] == "queued", first
    assert second["status"] == "queued", second
    assert adapter.launchctl.actions() == [], "还没空闲就重启了"
    health["turns_running"] = 0
    _wait_first_action(adapter, timeout=10.0)
    settled = _stable_action_count(adapter)
    time.sleep(3.0)                     # 排队轮询约 1 秒一次，留三轮
    now = len(adapter.launchctl.actions())
    assert now == settled, (
        "同一只 bot 排了第二次：空闲后启动项动作从 %d 条变成 %d 条：%r" % (
            settled, now, adapter.launchctl.calls()))


# ------------------------------------------------------------ 行为清单 10

def test_排队期间force_立即重启且排队那次不再执行(adapter):
    _gw, health = _ready(adapter, 2)
    queued = adapter.ok(_post(adapter, {}))
    assert queued["status"] == "queued", queued
    forced = adapter.ok(_post(adapter, {"force": True}))
    assert forced["status"] == "ok", forced
    assert adapter.launchctl.actions(), "强制重启没有动启动项"
    settled = _stable_action_count(adapter)
    health["turns_running"] = 0
    time.sleep(4.0)                     # 排队轮询约 1 秒一次，留四轮
    now = len(adapter.launchctl.actions())
    assert now == settled, (
        "强制重启后，排队的那一次仍在空闲后执行了：启动项动作从 %d 条变成 %d 条：%r" % (
            settled, now, adapter.launchctl.calls()))


# ------------------------------------------------------------ 错误契约（现状）

def test_停用的bot_非force重启_回409_stopped且不重启(adapter):
    gw = adapter.gateway()
    adapter.bot("botalpha", enabled=False, gw_port=gw.port)
    adapter.launchctl.set_default(installed=True, running=False, pid=4242)
    adapter.expect_error(_post(adapter, {}), 409, "stopped")
    assert adapter.launchctl.actions() == [], \
        "停用的 bot 被拒了还动了启动项：%r" % adapter.launchctl.calls()


def test_没有这只bot_非force重启_回404_unknown_bot且不重启(adapter):
    adapter.bot("botalpha")
    adapter.launchctl.set_default(installed=True, running=False, pid=4242)
    adapter.expect_error(_post(adapter, {}, bot="ghost"), 404, "unknown_bot")
    assert adapter.launchctl.actions() == [], \
        "没有这只 bot，却动了启动项：%r" % adapter.launchctl.calls()
