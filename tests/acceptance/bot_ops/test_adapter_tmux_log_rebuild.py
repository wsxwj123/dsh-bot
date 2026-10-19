# -*- coding: utf-8 -*-
"""tmux-log-rebuild-on-enable 验收：启用适配服务 bot 后异步触发日志看板重建。

依据：`.devflow/tmux-log-rebuild-on-enable-接口.md` 的行为清单 1-6 与错误契约。
只经适配服务 HTTP 入口（create_app 的测试客户端）与假 launchctl 接缝；不读实现。

约定（接口文件）：看板重建 = `launchctl kickstart gui/<uid>/com.dsh-bot.tmux-logs`，
假 launchctl 把每次调用记进 calls.jsonl（cmd / label / argv / t）。
"""
import os
import time

from . import adapter_util as au
from .adapter_util import adapter, console_guard  # noqa: F401  夹具

TMUX_LABEL = "com.dsh-bot.tmux-logs"


def _enable(adapter, bot_id, enabled=True):
    return adapter.call("POST", au.enabled_url(bot_id), {"enabled": enabled})


def _tmux_kickstarts(calls):
    """记录里针对看板任务的 kickstart。"""
    return [c for c in calls
            if c.get("cmd") == "kickstart" and c.get("label") == TMUX_LABEL]


def _wait_for(predicate, timeout=1.0):
    """轮询到 predicate() 为真或超时；超时返回 False。"""
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def _wait_tmux_since(fake, marker=0, timeout=1.0):
    """等第 marker 条之后的记录里出现看板重建；返回那些记录（可能为空）。"""
    _wait_for(lambda: _tmux_kickstarts(fake.calls()[marker:]), timeout)
    return _tmux_kickstarts(fake.calls()[marker:])


def test_行为1_停用后再启用_回200且看板重建kickstart出现一次(adapter):
    adapter.bot("botalpha")                                   # 本来就是启用中的 bot
    au.put_plists(adapter.sandbox, "botalpha")
    adapter.launchctl.set_default(installed=True, running=True, pid=4242)
    adapter.heartbeat.write("botalpha")

    adapter.ok(_enable(adapter, "botalpha", False))           # 先停用
    body = adapter.ok(_enable(adapter, "botalpha", True))     # 再启用
    assert body.get("enabled") is True, body

    kicks = _wait_tmux_since(adapter.launchctl)
    assert len(kicks) == 1, \
        "看板重建应恰好触发一次 kickstart，实际记录：%r" % adapter.launchctl.calls()
    assert kicks[0]["argv"] == ["kickstart", "gui/%d/%s" % (os.getuid(), TMUX_LABEL)], kicks[0]


def test_行为2_已启用的bot幂等启用_回200且不触发看板重建(adapter):
    adapter.bot("botalpha")                                   # 已启用
    au.put_plists(adapter.sandbox, "botalpha")
    adapter.launchctl.set_default(installed=True, running=True, pid=4242)
    adapter.heartbeat.tick("botalpha", pid=4242)

    marker = len(adapter.launchctl.calls())
    body = adapter.ok(_enable(adapter, "botalpha", True))
    assert (body.get("enabled"), body.get("running")) == (True, True), body

    assert not _wait_for(lambda: _tmux_kickstarts(adapter.launchctl.calls()[marker:]), timeout=1.0), \
        "幂等启用不该触发看板重建，实际记录：%r" % adapter.launchctl.calls()[marker:]


def test_行为3_看板任务不存在_启用停用的bot仍200且回包与正常启用一致(adapter):
    # 对照：看板任务正常时，一只停用的 bot 走完整启动路径的回包
    adapter.bot("botalpha")
    au.put_plists(adapter.sandbox, "botalpha")
    adapter.launchctl.set_default(installed=True, running=True, pid=4242)
    adapter.heartbeat.write("botalpha")
    adapter.ok(_enable(adapter, "botalpha", False))           # 先停用
    normal = adapter.ok(_enable(adapter, "botalpha", True))   # 再启用

    # 让假 launchctl 对看板任务返回非 0（单元不存在，kickstart 退 3）
    adapter.launchctl.set_unit("tmux-logs", installed=False, running=False, pid=4242)
    adapter.bot("botbravo")
    au.put_plists(adapter.sandbox, "botbravo")
    adapter.heartbeat.write("botbravo")
    adapter.ok(_enable(adapter, "botbravo", False))           # 先停用，准备走完整启动路径
    marker = len(adapter.launchctl.calls())
    resp = _enable(adapter, "botbravo", True)                 # 再启用
    failed = au.body_of(resp)
    assert resp.status_code == 200, \
        "看板任务不存在时启用仍应回 200，实际 %s：%r" % (resp.status_code, failed)
    assert failed == normal, "回包应与正常启用一致：正常 %r，看板任务不存在时 %r" % (normal, failed)
    assert failed.get("ok") is True and "code" not in failed, \
        "看板触发失败不该让成功回包带错误码：%r" % failed

    kicks = _wait_tmux_since(adapter.launchctl, marker)
    assert len(kicks) == 1, \
        "看板任务不存在时也应尝试一次 kickstart，实际新增记录：%r" % adapter.launchctl.calls()[marker:]


def test_行为4_停用方向不触发看板重建(adapter):
    adapter.bot("botalpha")
    au.put_plists(adapter.sandbox, "botalpha")
    adapter.launchctl.set_default(installed=True, running=True, pid=4242)
    adapter.heartbeat.write("botalpha")

    body = adapter.ok(_enable(adapter, "botalpha", False))
    assert body.get("enabled") is False, body

    assert not _wait_for(lambda: _tmux_kickstarts(adapter.launchctl.calls()), timeout=1.0), \
        "停用方向不该触发看板重建：%r" % adapter.launchctl.calls()


def test_行为5_看板触发不干扰启停本身的其它调用(adapter):
    adapter.bot("botalpha", enabled=False)
    au.put_plists(adapter.sandbox, "botalpha", stashed=True)
    adapter.launchctl.set_default(installed=False, running=False, pid=4242)

    body = adapter.ok(_enable(adapter, "botalpha", True))
    assert body.get("enabled") is True, body

    main, timer = au.unit_labels("botalpha")
    others = [c for c in adapter.launchctl.calls() if c.get("label") != TMUX_LABEL]
    labels = {c.get("label") for c in others}
    assert labels <= {main, timer}, \
        "启用时碰了不属于这只 bot 的单元（与改动前不一致）：%r" % others
    assert not any(TMUX_LABEL in " ".join(c.get("argv") or []) for c in others), \
        "看板任务混进了启停本身的调用：%r" % others
    started = [c for c in others
               if c.get("label") == main and c.get("cmd") in ("bootstrap", "kickstart")]
    assert started, "启用却没有启动主单元（与改动前的调用不一致）：%r" % others

    kicks = _wait_tmux_since(adapter.launchctl)
    assert len(kicks) == 1, \
        "看板重建应触发一次，实际记录：%r" % adapter.launchctl.calls()


def test_行为6_看板触发异步但不丢_接口返回后1秒内记录可见(adapter):
    adapter.bot("botalpha")
    adapter.launchctl.set_default(installed=True, running=True, pid=4242)
    adapter.heartbeat.tick("botalpha", pid=4242)

    resp = _enable(adapter, "botalpha", True)
    returned = time.time()                                    # 接口返回的时刻
    body = adapter.ok(resp)
    assert body.get("enabled") is True, body

    found = _wait_for(lambda: _tmux_kickstarts(adapter.launchctl.calls()), timeout=1.0)
    kicks = _tmux_kickstarts(adapter.launchctl.calls())
    assert found and kicks, \
        "接口返回后 1 秒内记录里没有看板重建：%r" % adapter.launchctl.calls()
    assert kicks[0]["t"] <= returned + 1.0, \
        "看板重建在接口返回 %.3f 秒后才出现，超过 1 秒" % (kicks[0]["t"] - returned)


def test_错误契约1_非法请求仍422_且不触发看板重建(adapter):
    adapter.bot("botalpha")
    resp = adapter.call("POST", au.enabled_url("botalpha"), {"enabled": "true"})
    adapter.expect_error(resp, 422, "invalid")

    assert not _wait_for(lambda: _tmux_kickstarts(adapter.launchctl.calls()), timeout=1.0), \
        "非法请求不该触发看板重建：%r" % adapter.launchctl.calls()
    assert adapter.launchctl.calls() == [], \
        "校验没过却调了启动项管理程序：%r" % adapter.launchctl.calls()


def test_错误契约2_停用失败仍503_且不触发看板重建(adapter, monkeypatch):
    adapter.bot("botalpha")
    au.put_plists(adapter.sandbox, "botalpha")
    adapter.launchctl.set_default(installed=True, running=True, pid=4242)
    main = au.unit_labels("botalpha")[0]
    adapter.launchctl.set_print_text(au.print_text(main, running=True))  # 一查总说在跑
    monkeypatch.setenv("DSH_CONSOLE_STOP_WAIT", "1")

    adapter.expect_error(_enable(adapter, "botalpha", False), 503, "unavailable")
    assert not _wait_for(lambda: _tmux_kickstarts(adapter.launchctl.calls()), timeout=1.0), \
        "停用失败时不该触发看板重建：%r" % adapter.launchctl.calls()
