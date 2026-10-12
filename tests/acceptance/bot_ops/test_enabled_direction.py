# -*- coding: utf-8 -*-
"""§4b 开关同步的方向与对象（接口约定第 6 稿）。

依据 `INTERFACE-新系统bot运维.md` §4b「单只同步 job 的对象恒为新系统的单元
`com.dsh-bot.<bot_id>`，执行方向 = 写盘后新系统那份配置的 enabled」那几段，以及写死的那个
要紧情形：新旧两份都启用、这次请求停用。

BRIEF 的成功标准：在管理台上把一只 bot 停用，它真的会停下来，只有这一只被停。
"""
import pytest

from . import botops_util as u

LABEL = "com.dsh-bot.twin"
#: 第 8 稿 §5c：停用方向在主单元之后，还要对这只的主动消息定时单元发一条 bootout
TIMER_LABEL = "com.dsh-bot.self-initiate.twin"


def _post(client, bot_id, enabled):
    return client.post("/hub/api/bots/%s/enabled" % bot_id, json={"enabled": enabled})


def _twin(sandbox, *, dsh_enabled, legacy_enabled):
    """新旧两个配置根下各放一份同名的 twin。返回 (新系统那份, 旧系统那份)。"""
    new = u.write_bot_yml(sandbox.dsh_cfg, "twin", display_name="新twin",
                          dispatcher_port=17951, enabled=dsh_enabled)
    old = u.write_bot_yml(sandbox.legacy_cfg, "twin", display_name="旧twin",
                          dispatcher_port=17899, enabled=legacy_enabled)
    return new, old


def _toggle_to_terminal(client, bot_id, enabled):
    """点一次开关并把它起的同步等到终态。返回 (200 的体, job 终态体)。"""
    r = _post(client, bot_id, enabled)
    assert r.status_code == 200, (r.status_code, r.get_json())
    body = r.get_json()
    assert body["sync"]["state"] == "started", body
    return body, u.finish_sync(client, body)


@pytest.fixture(autouse=True)
def _short_wait(monkeypatch):
    """重启等待上限（§1：45 秒，测试可改）收到 3 秒：方向走错时不用陪着等满。"""
    u.tighten_constant(monkeypatch, value=45, new=3.0)


@pytest.fixture
def alive(heartbeat, launchctl):
    """启用方向要等活：单元已装、kickstart 后在跑，心跳一直新鲜且 pid 对得上。"""
    launchctl.set_default(installed=True, running=False, pid=4242)
    heartbeat.tick("twin", pid=4242)
    return launchctl


# ============================================================ 要紧情形：两份都启用，请求停用

def test_同名都启用_请求停用_只对新系统的单元发bootout_不发kickstart(
        client, sandbox, launchctl, as_darwin):
    """写盘后生效的那份变成旧系统的，但这次同步仍然走停用方向（第 5 稿在这里是跳过）。"""
    _twin(sandbox, dsh_enabled=True, legacy_enabled=True)
    launchctl.set_default(installed=True, running=True, pid=4242)
    _, st = _toggle_to_terminal(client, "twin", False)
    assert st["state"] == "done", st
    assert launchctl.subcommands() == ["bootout", "bootout"], launchctl.calls()
    assert launchctl.labels() == [LABEL, TIMER_LABEL], launchctl.calls()


def test_同名都启用_请求停用_job的tail末行是已停止(client, sandbox, launchctl, as_darwin):
    _twin(sandbox, dsh_enabled=True, legacy_enabled=True)
    launchctl.set_default(installed=True, running=True, pid=4242)
    _, st = _toggle_to_terminal(client, "twin", False)
    lines = [ln for ln in st["tail"].splitlines() if ln.strip()]
    assert lines and lines[-1] == "twin 已停止", st["tail"]


def test_同名都启用_请求停用_新系统那份写成停用_旧系统那份字节不变(
        client, sandbox, launchctl, as_darwin):
    new, _old = _twin(sandbox, dsh_enabled=True, legacy_enabled=True)
    frozen_legacy = u.freeze_roots(sandbox.legacy_cfg)
    r = _post(client, "twin", False)
    assert r.status_code == 200, (r.status_code, r.get_json())
    u.finish_sync(client, r.get_json())         # 同步往哪走是上面两条的事，这里只看写盘
    assert u.read_yaml(new)["enabled"] is False, new.read_text(encoding="utf-8")
    u.assert_roots_untouched(frozen_legacy)


@pytest.mark.parametrize("again", [True, False], ids=["enable", "disable"])    # 再点启用 / 再点停用
def test_同名都启用_停掉之后这只归旧系统_再点开关404且两边都不再变(
        client, sandbox, launchctl, as_darwin, monkeypatch, again):
    """§4b：停掉之后这只 bot 归旧系统，再对它调本端点回 404。"""
    _twin(sandbox, dsh_enabled=True, legacy_enabled=True)
    first = _post(client, "twin", False)
    assert first.status_code == 200, (first.status_code, first.get_json())
    u.finish_sync(client, first.get_json())     # 第一次停用收尾；它的方向不归这条管
    frozen = u.freeze_roots(sandbox.legacy_cfg, sandbox.dsh_cfg)
    calls_before = len(launchctl.calls())
    r = _post(client, "twin", again)
    u.assert_gate_rejected(r, frozen, launchctl, client, monkeypatch, sandbox.tmp,
                           calls_before=calls_before)


# ============================================================ 方向只看新系统那份

def test_新系统那份启用旧系统那份停着_请求启用_走kickstart不发bootout(
        client, sandbox, alive, as_darwin):
    """同名的旧系统那份写的是什么不参与方向：它停着，这只照样被拉起。"""
    _twin(sandbox, dsh_enabled=True, legacy_enabled=False)
    _, st = _toggle_to_terminal(client, "twin", True)
    assert st["state"] == "done", st
    cmds = alive.subcommands()
    assert "kickstart" in cmds and "bootout" not in cmds, alive.calls()
    assert set(alive.labels()) == {LABEL}, alive.calls()


def test_新系统那份启用旧系统那份停着_请求停用_走bootout(
        client, sandbox, launchctl, as_darwin):
    _twin(sandbox, dsh_enabled=True, legacy_enabled=False)
    launchctl.set_default(installed=True, running=True, pid=4242)
    _, st = _toggle_to_terminal(client, "twin", False)
    assert st["state"] == "done", st
    assert launchctl.subcommands() == ["bootout", "bootout"], launchctl.calls()
    assert launchctl.labels() == [LABEL, TIMER_LABEL], launchctl.calls()


def test_同名都启用_请求启用_走kickstart不发bootout(client, sandbox, alive, as_darwin):
    _twin(sandbox, dsh_enabled=True, legacy_enabled=True)
    _, st = _toggle_to_terminal(client, "twin", True)
    assert st["state"] == "done", st
    cmds = alive.subcommands()
    assert "kickstart" in cmds and "bootout" not in cmds, alive.calls()
    assert set(alive.labels()) == {LABEL}, alive.calls()


def test_同名两份都停着_请求启用_走kickstart_对象是新系统的单元(
        client, sandbox, alive, as_darwin):
    _twin(sandbox, dsh_enabled=False, legacy_enabled=False)
    _, st = _toggle_to_terminal(client, "twin", True)
    assert st["state"] == "done", st
    cmds = alive.subcommands()
    assert "kickstart" in cmds and "bootout" not in cmds, alive.calls()
    assert set(alive.labels()) == {LABEL}, alive.calls()


def test_同名两份都停着_请求停用_走bootout(client, sandbox, launchctl, as_darwin):
    _twin(sandbox, dsh_enabled=False, legacy_enabled=False)
    launchctl.set_default(installed=True, running=True, pid=4242)
    _, st = _toggle_to_terminal(client, "twin", False)
    assert st["state"] == "done", st
    assert launchctl.subcommands() == ["bootout", "bootout"], launchctl.calls()
    assert launchctl.labels() == [LABEL, TIMER_LABEL], launchctl.calls()


# ============================================================ 对象：只动这一只

def test_停用一只_别的新系统bot一条launchctl都不沾(client, sandbox, launchctl, as_darwin):
    """BRIEF：停用一只新系统的 bot，只有这一只被停，别的 bot 不被重启。"""
    u.dsh_bot(sandbox.dsh_cfg, "xiaoyu")
    u.dsh_bot(sandbox.dsh_cfg, "otherbot")
    u.dsh_bot(sandbox.dsh_cfg, "thirdbot", enabled=False)
    launchctl.set_default(installed=True, running=True, pid=4242)
    body, st = _toggle_to_terminal(client, "xiaoyu", False)
    assert st["state"] == "done", st
    assert launchctl.labels() == ["com.dsh-bot.xiaoyu",
                                  "com.dsh-bot.self-initiate.xiaoyu"], launchctl.calls()


def test_启用一只_别的新系统bot一条launchctl都不沾(
        client, sandbox, heartbeat, launchctl, as_darwin):
    u.dsh_bot(sandbox.dsh_cfg, "xiaoyu", enabled=False)
    u.dsh_bot(sandbox.dsh_cfg, "otherbot")
    u.dsh_bot(sandbox.dsh_cfg, "thirdbot", enabled=False)
    launchctl.set_default(installed=True, running=False, pid=4242)
    heartbeat.tick("xiaoyu", pid=4242)
    body, st = _toggle_to_terminal(client, "xiaoyu", True)
    assert st["state"] == "done", st
    assert set(launchctl.labels()) == {"com.dsh-bot.xiaoyu"}, launchctl.calls()
