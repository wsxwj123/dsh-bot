# -*- coding: utf-8 -*-
"""适配服务 v0.6 验收：启停 POST /adapter/v1/bots/{bot_id}/enabled（补充文件 9.2，约定 5.8），
以及三个新接口（启停、读群设置、改群设置）共用的入口校验。

依据：INTERFACE-适配服务.md 第 2、3 节与 9.2、9.3；INTERFACE-适配约定-v0.7摘录.md 5.8、5.9。不读实现。
9.2 说"停"与管理台的停用开关是同一种效果。管理台那边的效果借自本目录现成用例（test_enabled_sync.py、
test_plist_stash.py）：配置写 enabled: false；主单元与主动消息定时单元的 plist 收起成 .plist.disabled。
"停不下来"用假 launchctl 的 print 原文钉成 running 造出来（bootout 照常成功）。
"""
import threading
import time

import pytest

from . import adapter_util as au
from . import botops_util as u
from .adapter_util import adapter, console_guard  # noqa: F401  夹具

A_MAIN, A_TIMER = au.unit_labels("botalpha")


def post_enabled(adapter, payload, raw=None, bot_id="botalpha"):
    return adapter.call("POST", au.enabled_url(bot_id), payload, raw=raw)


# ============================================================ 停

def test_停_在跑的bot_回200两个假_配置记停用_两份plist收起_只碰这一只(adapter):
    alpha, bravo = adapter.bot("botalpha"), adapter.bot("botbravo")
    au.put_plists(adapter.sandbox, "botalpha")
    au.put_plists(adapter.sandbox, "botbravo")
    bravo_before = bravo.yml.read_bytes()
    adapter.launchctl.set_default(installed=True, running=True, pid=4242)
    adapter.heartbeat.write("botalpha")                  # 心跳新鲜，但只写这一次（停了就不再刷新）
    started = time.time()
    body = adapter.ok(post_enabled(adapter, {"enabled": False}))
    took = time.time() - started
    assert body.get("enabled") is False and body.get("running") is False, body
    assert au.read_cfg(alpha).get("enabled") is False, "配置里应记成 enabled: false"
    for label in (A_MAIN, A_TIMER):
        assert au.plist_state(adapter.sandbox, label) == "收起", \
            "%s 的 plist 应收起成 .plist.disabled（开机不再自启，同管理台停用开关）" % label
    assert "bootout" in adapter.launchctl.subcommands(), adapter.launchctl.calls()
    labels = [lb for lb in adapter.launchctl.labels() if lb]
    assert labels and set(labels) <= {A_MAIN, A_TIMER}, "启动项调用里出现了别的单元：%r" % labels
    u.assert_units_never_named(adapter.launchctl, "botbravo", "self-initiate.botbravo")
    assert bravo.yml.read_bytes() == bravo_before, "停 botalpha 却改了 botbravo 的配置"
    assert {au.plist_state(adapter.sandbox, lb) for lb in au.unit_labels("botbravo")} == {"装着"}
    assert took < 10, "bootout 后启动项立刻就不在跑了，却等了 %.1f 秒（应每秒查一次启动项）" % took


@pytest.mark.parametrize("wait_env,lo,hi", [("2", 1.0, 12.0), ("120", 45.0, 56.0)],
                         ids=["STOP_WAIT为2_约2秒后放弃", "STOP_WAIT大于50按50_在55秒封顶内放弃"])
def test_停_启动项一直还在跑_到时回503_说明仍在跑_配置已先记停用(adapter, monkeypatch, wait_env, lo, hi):
    alpha = adapter.bot("botalpha")
    au.put_plists(adapter.sandbox, "botalpha")
    adapter.launchctl.set_default(installed=True, running=True, pid=4242)
    adapter.launchctl.set_print_text(au.print_text(A_MAIN, running=True))   # 一查总说在跑
    monkeypatch.setenv("DSH_CONSOLE_STOP_WAIT", wait_env)
    started = time.time()
    resp = post_enabled(adapter, {"enabled": False})
    took = time.time() - started
    body = adapter.expect_error(resp, 503, "unavailable")
    assert any(w in body["message"] for w in ("在跑", "运行", "没停")), "503 应说明仍在跑：%r" % body["message"]
    assert lo <= took <= hi, "STOP_WAIT=%s 时应在 %.0f 到 %.0f 秒之间放弃，实际 %.1f 秒" % (wait_env, lo, hi, took)
    assert au.read_cfg(alpha).get("enabled") is False, "约定 5.8 先记成停用再停进程：停不下来也已记成停用"


@pytest.mark.parametrize("wait_env", ["0", "-5", "abc"], ids=["零", "负数", "不是数"])
def test_停_STOP_WAIT取值不合规按50秒_3秒后才停下也照样回200(adapter, monkeypatch, wait_env):
    adapter.bot("botalpha")
    au.put_plists(adapter.sandbox, "botalpha")
    adapter.launchctl.set_default(installed=True, running=True, pid=4242)
    adapter.launchctl.set_print_text(au.print_text(A_MAIN, running=True))
    monkeypatch.setenv("DSH_CONSOLE_STOP_WAIT", wait_env)
    adapter.client                                       # 先在主线程建好测试客户端
    box, started = {}, time.time()

    def go():
        box["resp"] = post_enabled(adapter, {"enabled": False})
        box["took"] = time.time() - started

    t = threading.Thread(target=go, daemon=True)
    t.start()
    time.sleep(3.0)
    still_waiting = t.is_alive()
    adapter.launchctl.set_print_text(au.print_text(A_MAIN, running=False))  # 3 秒后终于停下了
    t.join(20.0)
    assert still_waiting, "STOP_WAIT=%r 应按 50 秒算，却不到 3 秒就回了：%r" % (wait_env, box)
    assert not t.is_alive(), "启动项已停下，20 秒内仍没回"
    body = adapter.ok(box["resp"])
    assert body.get("enabled") is False and body.get("running") is False, body


# ============================================================ 启

@pytest.mark.parametrize("up", [True, False], ids=["启动项此刻在跑", "启动项此刻还没跑起来"])
def test_启_停用的bot_回200_配置记启用_plist放回_running照启动项此刻_不等心跳(adapter, up):
    alpha = adapter.bot("botalpha", enabled=False)
    adapter.bot("botbravo", enabled=False)
    au.put_plists(adapter.sandbox, "botalpha", stashed=True)
    au.put_plists(adapter.sandbox, "botbravo", stashed=True)
    if up:
        adapter.launchctl.set_default(installed=False, running=False, pid=4242)
    else:
        adapter.launchctl.set_default(installed=True, running=False, pid=4242)
        adapter.launchctl.set_print_text(au.print_text(A_MAIN, running=False))
    started = time.time()
    body = adapter.ok(post_enabled(adapter, {"enabled": True}))       # 全程没有心跳
    took = time.time() - started
    assert body.get("enabled") is True and body.get("running") is up, body
    assert au.read_cfg(alpha).get("enabled") is not False, "配置应记成启用"
    assert au.plist_state(adapter.sandbox, A_MAIN) == "装着", "主单元的 plist 应放回"
    assert au.plist_state(adapter.sandbox, A_TIMER) == "装着", "主动消息定时单元的 plist 应一起放回"
    subs = adapter.launchctl.subcommands()
    assert "bootstrap" in subs or "kickstart" in subs, "没有装载并启动启动项：%r" % adapter.launchctl.calls()
    u.assert_units_never_named(adapter.launchctl, "botbravo", "self-initiate.botbravo")
    assert {au.plist_state(adapter.sandbox, lb) for lb in au.unit_labels("botbravo")} == {"收起"}
    assert took < 8, "启不等心跳，却等了 %.1f 秒" % took


ALREADY = {"已停用又要停": False, "已在跑又要启": True}


@pytest.mark.parametrize("target", list(ALREADY.values()), ids=list(ALREADY))
def test_已是目标状态_照常回200_不重复操作(adapter, target):
    alpha = adapter.bot("botalpha", enabled=target)
    au.put_plists(adapter.sandbox, "botalpha", stashed=not target)
    adapter.launchctl.set_default(installed=target, running=target, pid=4242)
    if target:
        adapter.heartbeat.tick("botalpha", pid=4242)
    cfg_before = alpha.yml.read_bytes()
    agents = u.freeze_roots(adapter.sandbox.agents)
    body = adapter.ok(post_enabled(adapter, {"enabled": target}))
    assert body.get("enabled") is target and body.get("running") is target, body
    assert adapter.launchctl.actions() == [], "已是目标状态，却又动了启动项：%r" % adapter.launchctl.actions()
    assert alpha.yml.read_bytes() == cfg_before, "已是目标状态，配置文件却变了"
    u.assert_roots_untouched(agents)


# ============================================================ 不合规

BAD_BODIES = {
    "缺enabled": ({}, None),
    "enabled是字符串": ({"enabled": "false"}, None),
    "enabled是数字0": ({"enabled": 0}, None),
    "enabled是null": ({"enabled": None}, None),
    "停时带import_from": ({"enabled": False, "import_from": "telegram"}, None),
    "多了别的键": ({"enabled": True, "force": True}, None),
    "请求体是数组": (au.NO_BODY, "[]"),
    "坏JSON": (au.NO_BODY, "{bad"),
    "空体": (au.NO_BODY, ""),
    "import_from两个点": ({"enabled": True, "import_from": ".."}, None),
    "import_from一个点": ({"enabled": True, "import_from": "."}, None),
    "import_from带斜杠": ({"enabled": True, "import_from": "a/b"}, None),
    "import_from有65个字符": ({"enabled": True, "import_from": "t" * 65}, None),
    "import_from空串": ({"enabled": True, "import_from": ""}, None),
    "import_from不是字符串": ({"enabled": True, "import_from": 123}, None),
    "import_from是中文": ({"enabled": True, "import_from": "频道"}, None),
    "import_from带空格": ({"enabled": True, "import_from": "tele gram"}, None),
}


@pytest.mark.parametrize("payload,raw", list(BAD_BODIES.values()), ids=list(BAD_BODIES))
def test_请求体不合规_回422_invalid_什么都不做(adapter, payload, raw):
    alpha = adapter.bot("botalpha", enabled=False)
    au.set_cfg(alpha, chat_id=int(au.CHAT_ID))
    au.put_plists(adapter.sandbox, "botalpha", stashed=True)
    adapter.launchctl.set_default(installed=False, running=False, pid=4242)
    frozen = u.freeze_roots(adapter.sandbox.dsh_cfg, adapter.sandbox.agents, alpha.botdir)
    adapter.expect_error(post_enabled(adapter, payload, raw=raw), 422, "invalid")
    assert adapter.launchctl.calls() == [], "校验没过却调了启动项管理程序：%r" % adapter.launchctl.calls()
    u.assert_roots_untouched(frozen)


GROUP_LEAVE = {"chat_id": "-100123", "join": False}
ENTRY = {
    "启停_没带令牌": ("POST", au.enabled_url, "botalpha", {"enabled": False}, None, 401, "unauthorized"),
    "读群设置_令牌不对": ("GET", au.groups_url, "botalpha", au.NO_BODY,
                     "Bearer " + au.WRONG_TOKEN, 401, "unauthorized"),
    "改群设置_不是Bearer写法": ("POST", au.groups_url, "botalpha", GROUP_LEAVE,
                         "Basic " + au.ADAPTER_TOKEN, 401, "unauthorized"),
    "启停_bot_id两个点": ("POST", au.enabled_url, "..", {"enabled": False}, au.GOOD, 422, "invalid"),
    "读群设置_bot_id编码的斜杠": ("GET", au.groups_url, "a%2Fb", au.NO_BODY, au.GOOD, 422, "invalid"),
    "改群设置_bot_id有65个字符": ("POST", au.groups_url, "a" * 65, GROUP_LEAVE, au.GOOD, 422, "invalid"),
    "启停_没有这只bot": ("POST", au.enabled_url, "ghost", {"enabled": False}, au.GOOD, 404, "unknown_bot"),
    "读群设置_没有这只bot": ("GET", au.groups_url, "ghost", au.NO_BODY, au.GOOD, 404, "unknown_bot"),
    "改群设置_没有这只bot": ("POST", au.groups_url, "ghost", GROUP_LEAVE, au.GOOD, 404, "unknown_bot"),
}


@pytest.mark.parametrize("method,make_url,bot_id,payload,auth,status,code", list(ENTRY.values()), ids=list(ENTRY))
def test_新接口入口_鉴权与bot_id校验_没过就什么都不做(adapter, method, make_url, bot_id, payload, auth,
                                       status, code):
    gw = adapter.gateway()
    alpha = adapter.running_bot("botalpha", gw)
    au.write_access(alpha, {"groups": {"-100123": {"requireMention": True}}})
    au.put_plists(adapter.sandbox, "botalpha")
    frozen = u.freeze_roots(adapter.sandbox.dsh_cfg, adapter.sandbox.agents, alpha.botdir / "channel")
    resp = adapter.call(method, make_url(bot_id), payload, auth=auth)
    if status == 401:
        au.assert_unauthorized(resp)
    else:
        adapter.expect_error(resp, status, code)
    assert adapter.launchctl.calls() == [], "没过入口校验却调了启动项管理程序：%r" % adapter.launchctl.calls()
    assert gw.requests == [], "没过入口校验却找了网关：%r" % gw.requests
    u.assert_roots_untouched(frozen)
