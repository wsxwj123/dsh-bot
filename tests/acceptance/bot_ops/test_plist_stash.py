# -*- coding: utf-8 -*-
"""§5c 停用要跨登录会话有效：收起与放回 plist（接口约定第 8 稿）。

依据 `INTERFACE-新系统bot运维.md` §5c，以及它牵动的 §3 第 2、3 步、§4b、§5、§8。

用户看得见的结果：在管理台上把一只 bot 停用之后，注销重登或重启电脑它也不会自己回来；
再点启用，它与它的主动消息定时任务一起回来；「重启全部」不会把停用的拉起来。

名词（照 §5c）：
- 主单元 `com.dsh-bot.<id>`，定时单元 `com.dsh-bot.self-initiate.<id>`；
- 收起 = 把 `<名>.plist` 在同一个目录里改名成 `<名>.plist.disabled`；放回 = 改回来；
- 装着 / 收起 / 没装三种状态只看文件。

隔离：plist 目录是每条用例自己的 `tmp_path/LaunchAgents`，假 launchctl 与假 bun 都在位。
本文件的任何改名都只发生在这个临时目录里（夹具开头有自检）。

测不到的：launchd 在真机上到底认不认 `.plist.disabled`、注销重登之后的表现。这里只能断言
文件名怎么变、假 launchctl 收到了什么。
"""
import os

import pytest

from . import botops_util as u

BOT = "xiaoyu"
MAIN = "com.dsh-bot.%s" % BOT
TIMER = "com.dsh-bot.self-initiate.%s" % BOT

IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
needs_perm = pytest.mark.skipif(os.name == "nt" or IS_ROOT,
                                reason="要靠目录权限造「改名失败」：Windows 与 root 下造不出来")


# ------------------------------------------------------------ 小件

def _domain():
    return "gui/%d" % os.getuid()


def _target(label):
    return "%s/%s" % (_domain(), label)


def _plist(agents, label):
    return agents / (label + ".plist")


def _stash(agents, label):
    return agents / (label + ".plist.disabled")


def _put(path, tag):
    """写一份内容各不相同的假 plist，返回它的字节（之后拿来逐字节比）。"""
    path.write_text('<?xml version="1.0"?>\n<plist><!-- %s --></plist>\n' % tag, encoding="utf-8")
    return path.read_bytes()


def _install_both(agents, bot=BOT):
    """两份 plist 都装着。返回 (主单元那份的字节, 定时单元那份的字节)。"""
    return (_put(_plist(agents, "com.dsh-bot.%s" % bot), "main-%s" % bot),
            _put(_plist(agents, "com.dsh-bot.self-initiate.%s" % bot), "timer-%s" % bot))


def _stash_both(agents, bot=BOT):
    """两份 plist 都处于收起状态。返回 (主单元那份的字节, 定时单元那份的字节)。"""
    return (_put(_stash(agents, "com.dsh-bot.%s" % bot), "main-%s" % bot),
            _put(_stash(agents, "com.dsh-bot.self-initiate.%s" % bot), "timer-%s" % bot))


def _state(agents, label):
    """§5c 的三种状态，只看文件。"""
    if _plist(agents, label).exists():
        return "装着"
    if _stash(agents, label).exists():
        return "收起"
    return "没装"


def _names(agents):
    return sorted(p.name for p in agents.iterdir())


def _toggle(client, enabled, bot=BOT, timeout=20.0):
    """点一次开关并把它起的同步等到终态，返回 job 的终态体。"""
    r = client.post("/hub/api/bots/%s/enabled" % bot, json={"enabled": enabled})
    assert r.status_code == 200, (r.status_code, r.get_json())
    body = r.get_json()
    assert body["sync"]["state"] == "started", body
    return u.finish_sync(client, body, timeout=timeout)


def _restart_all(client):
    r = client.post("/hub/api/bots/restart")
    assert r.status_code == 202, (r.status_code, r.get_json())
    return u.wait_job(client, r.get_json()["job_id"])


def _last_line(st):
    lines = [ln for ln in st["tail"].splitlines() if ln.strip()]
    assert lines, st
    return lines[-1]


def _line_of(st, bot=BOT):
    lines = [ln for ln in st["tail"].splitlines() if ln.startswith(bot + " ")]
    assert len(lines) == 1, ("tail 里这只应当恰好一行", st["tail"])
    return lines[0]


def _fail_line(sandbox, reason, bot=BOT):
    """§8 第 4 条的失败行：`<id> 失败：<原因>；日志 <DSH_BOT_HOME>/bots/<id>/logs/launchd.log`。"""
    return "%s 失败：%s；日志 %s" % (
        bot, reason, sandbox.dsh_home / "bots" / bot / "logs" / "launchd.log")


@pytest.fixture(autouse=True)
def _fakes_in_place(sandbox, launchctl, bun, as_darwin, monkeypatch):
    """假 launchctl、假 bun 都在位；plist 目录确实是这条用例自己的临时目录。"""
    assert os.environ["HUB_LAUNCHAGENTS_DIR"] == str(sandbox.agents), "plist 目录没钉在夹具给的目录"
    assert str(sandbox.agents).startswith(str(sandbox.tmp)), "plist 目录不在 tmp_path 下"
    assert _names(sandbox.agents) == [], "夹具自检：plist 目录开始时应当是空的"
    u.tighten_constant(monkeypatch, value=45, new=3.0)      # 重启等待上限（§1，测试可改）


@pytest.fixture
def alive(heartbeat, launchctl):
    """启用方向要等活：主单元在 launchd 里装着、kickstart 之后在跑，心跳一直新鲜且 pid 对得上。"""
    launchctl.set_unit(BOT, installed=True, running=False, pid=4242)
    heartbeat.tick(BOT, pid=4242)
    return launchctl


# ============================================================ 场景 1：停用，两份 plist 都装着

def test_停用_两份都装着_job是done且tail末行是已停止(client, sandbox, launchctl):
    u.dsh_bot(sandbox.dsh_cfg, BOT)
    _install_both(sandbox.agents)
    st = _toggle(client, False)
    assert st["state"] == "done", st
    assert _last_line(st) == "%s 已停止" % BOT, st["tail"]


def test_停用_两份都装着_launchctl恰好两条bootout_先主单元后定时单元(client, sandbox, launchctl):
    u.dsh_bot(sandbox.dsh_cfg, BOT)
    _install_both(sandbox.agents)
    _toggle(client, False)
    argvs = [c["argv"] for c in launchctl.calls()]
    assert argvs == [["bootout", _target(MAIN)], ["bootout", _target(TIMER)]], argvs


def test_停用_两份都装着_两个plist都不在了(client, sandbox, launchctl):
    u.dsh_bot(sandbox.dsh_cfg, BOT)
    _install_both(sandbox.agents)
    _toggle(client, False)
    assert not _plist(sandbox.agents, MAIN).exists(), "主单元的 .plist 还在：重登之后 launchd 会把它拉起来"
    assert not _plist(sandbox.agents, TIMER).exists(), "定时单元的 .plist 还在"


def test_停用_两份都装着_各多出一份disabled且内容逐字节相同(client, sandbox, launchctl):
    u.dsh_bot(sandbox.dsh_cfg, BOT)
    main_bytes, timer_bytes = _install_both(sandbox.agents)
    _toggle(client, False)
    assert _stash(sandbox.agents, MAIN).is_file(), _names(sandbox.agents)
    assert _stash(sandbox.agents, TIMER).is_file(), _names(sandbox.agents)
    assert _stash(sandbox.agents, MAIN).read_bytes() == main_bytes, "主单元那份收起之后内容变了"
    assert _stash(sandbox.agents, TIMER).read_bytes() == timer_bytes, "定时单元那份收起之后内容变了"


def test_停用_两份都装着_目录里只剩那两份disabled(client, sandbox, launchctl):
    """反向：没有留下临时文件，也没有碰出别的名字。"""
    u.dsh_bot(sandbox.dsh_cfg, BOT)
    _install_both(sandbox.agents)
    _toggle(client, False)
    assert _names(sandbox.agents) == sorted([MAIN + ".plist.disabled",
                                             TIMER + ".plist.disabled"]), _names(sandbox.agents)


def test_停用_只动这一只的plist_别的bot的两份原样(client, sandbox, launchctl):
    u.dsh_bot(sandbox.dsh_cfg, BOT)
    u.dsh_bot(sandbox.dsh_cfg, "otherbot")
    _install_both(sandbox.agents)
    other = _install_both(sandbox.agents, "otherbot")
    _toggle(client, False)
    assert _plist(sandbox.agents, "com.dsh-bot.otherbot").read_bytes() == other[0]
    assert _plist(sandbox.agents, "com.dsh-bot.self-initiate.otherbot").read_bytes() == other[1]
    assert not _stash(sandbox.agents, "com.dsh-bot.otherbot").exists(), _names(sandbox.agents)


# ============================================================ 场景 2：再启用，两份都收起着

def test_启用_两份都收起_两个plist放回且disabled不在了(client, sandbox, alive):
    u.dsh_bot(sandbox.dsh_cfg, BOT, enabled=False)
    main_bytes, timer_bytes = _stash_both(sandbox.agents)
    _toggle(client, True)
    assert _names(sandbox.agents) == sorted([MAIN + ".plist", TIMER + ".plist"]), _names(sandbox.agents)
    assert _plist(sandbox.agents, MAIN).read_bytes() == main_bytes, "主单元那份放回之后内容变了"
    assert _plist(sandbox.agents, TIMER).read_bytes() == timer_bytes, "定时单元那份放回之后内容变了"


def test_启用_两份都收起_动作依次是kickstart主单元_bootout定时单元_bootstrap定时单元(
        client, sandbox, alive):
    u.dsh_bot(sandbox.dsh_cfg, BOT, enabled=False)
    _stash_both(sandbox.agents)
    _toggle(client, True)
    assert alive.actions() == [
        ["kickstart", "-k", _target(MAIN)],
        ["bootout", _target(TIMER)],
        ["bootstrap", _domain(), str(_plist(sandbox.agents, TIMER))],
    ], alive.calls()


def test_启用_两份都收起_三条动作之后才是print(client, sandbox, alive):
    """§5c 场景 2：先把该发的命令发完，再去等活。"""
    u.dsh_bot(sandbox.dsh_cfg, BOT, enabled=False)
    _stash_both(sandbox.agents)
    _toggle(client, True)
    cmds = alive.subcommands()
    assert cmds[:3] == ["kickstart", "bootout", "bootstrap"], cmds
    assert len(cmds) > 3 and set(cmds[3:]) == {"print"}, cmds


def test_启用_两份都收起_job是done且tail说已重启(client, sandbox, alive):
    u.dsh_bot(sandbox.dsh_cfg, BOT, enabled=False)
    _stash_both(sandbox.agents)
    st = _toggle(client, True)
    assert st["state"] == "done", st
    assert _last_line(st).startswith("%s 已重启（pid 4242，" % BOT), st["tail"]


def test_启用_主单元在launchd里已卸载_回落bootstrap带的是放回之后的主单元plist(
        client, sandbox, heartbeat, launchctl):
    """真机上停用做过 bootout，再启用时 kickstart 会说找不到服务：要先放回，回落才找得到 plist。"""
    u.dsh_bot(sandbox.dsh_cfg, BOT, enabled=False)
    _stash_both(sandbox.agents)
    launchctl.set_unit(BOT, installed=False, running=False, pid=None)
    heartbeat.tick(BOT, pid=4242)
    st = _toggle(client, True)
    assert launchctl.actions() == [
        ["kickstart", "-k", _target(MAIN)],
        ["bootstrap", _domain(), str(_plist(sandbox.agents, MAIN))],
        ["bootout", _target(TIMER)],
        ["bootstrap", _domain(), str(_plist(sandbox.agents, TIMER))],
    ], launchctl.calls()
    assert st["state"] == "done", st


def test_先停用再启用_一圈下来两份plist与最初逐字节相同(client, sandbox, heartbeat, launchctl):
    u.dsh_bot(sandbox.dsh_cfg, BOT)
    before = _install_both(sandbox.agents)
    assert _toggle(client, False)["state"] == "done"
    heartbeat.tick(BOT, pid=4242)
    assert _toggle(client, True)["state"] == "done"
    assert _names(sandbox.agents) == sorted([MAIN + ".plist", TIMER + ".plist"]), _names(sandbox.agents)
    assert (_plist(sandbox.agents, MAIN).read_bytes(),
            _plist(sandbox.agents, TIMER).read_bytes()) == before


# ============================================================ 场景 3：没有定时单元

def test_停用_只有主单元_仍发两条bootout且job是done(client, sandbox, launchctl):
    """定时单元那条 bootout 得到「找不到」，算成功（容错同 §3 第 3 步）。"""
    u.dsh_bot(sandbox.dsh_cfg, BOT)
    _put(_plist(sandbox.agents, MAIN), "main")
    launchctl.set_timer_unit(BOT, installed=False, running=False, pid=None)
    st = _toggle(client, False)
    argvs = [c["argv"] for c in launchctl.calls()]
    assert argvs == [["bootout", _target(MAIN)], ["bootout", _target(TIMER)]], argvs
    assert st["state"] == "done", st


def test_停用_只有主单元_只收起主单元那份(client, sandbox, launchctl):
    u.dsh_bot(sandbox.dsh_cfg, BOT)
    main_bytes = _put(_plist(sandbox.agents, MAIN), "main")
    launchctl.set_timer_unit(BOT, installed=False, running=False, pid=None)
    _toggle(client, False)
    assert _names(sandbox.agents) == [MAIN + ".plist.disabled"], _names(sandbox.agents)
    assert _stash(sandbox.agents, MAIN).read_bytes() == main_bytes


def test_启用_只有主单元收起着_动作只有一条kickstart_不碰定时单元(client, sandbox, alive):
    u.dsh_bot(sandbox.dsh_cfg, BOT, enabled=False)
    _put(_stash(sandbox.agents, MAIN), "main")
    st = _toggle(client, True)
    assert alive.actions() == [["kickstart", "-k", _target(MAIN)]], alive.calls()
    assert st["state"] == "done", st


def test_启用_只有主单元收起着_放回主单元且不凭空多出定时单元的文件(client, sandbox, alive):
    u.dsh_bot(sandbox.dsh_cfg, BOT, enabled=False)
    _put(_stash(sandbox.agents, MAIN), "main")
    _toggle(client, True)
    assert _names(sandbox.agents) == [MAIN + ".plist"], _names(sandbox.agents)


# ============================================================ 两个单元各自的状态互不牵连

def test_停用_主单元装着定时单元已收起_只收起主单元_定时单元那份不动(client, sandbox, launchctl):
    u.dsh_bot(sandbox.dsh_cfg, BOT)
    _put(_plist(sandbox.agents, MAIN), "main")
    timer_bytes = _put(_stash(sandbox.agents, TIMER), "timer-old")
    _toggle(client, False)
    assert (_state(sandbox.agents, MAIN), _state(sandbox.agents, TIMER)) == ("收起", "收起")
    assert _stash(sandbox.agents, TIMER).read_bytes() == timer_bytes


def test_启用_主单元装着定时单元收起着_只放回定时单元并把它装载(client, sandbox, alive):
    u.dsh_bot(sandbox.dsh_cfg, BOT, enabled=False)
    main_bytes = _put(_plist(sandbox.agents, MAIN), "main")
    _put(_stash(sandbox.agents, TIMER), "timer")
    _toggle(client, True)
    assert _names(sandbox.agents) == sorted([MAIN + ".plist", TIMER + ".plist"]), _names(sandbox.agents)
    assert _plist(sandbox.agents, MAIN).read_bytes() == main_bytes
    assert alive.actions()[-1] == ["bootstrap", _domain(), str(_plist(sandbox.agents, TIMER))], \
        alive.calls()


def test_停用_装着且另有一份旧的disabled_旧的被新的覆盖(client, sandbox, launchctl):
    """§5c「收起」：已有同名的 `.disabled` 文件被新的覆盖。"""
    u.dsh_bot(sandbox.dsh_cfg, BOT)
    fresh = _put(_plist(sandbox.agents, MAIN), "main-fresh")
    stale = _put(_stash(sandbox.agents, MAIN), "main-stale")
    assert fresh != stale, "夹具自检：两份内容要不一样才量得出谁盖了谁"
    _toggle(client, False)
    assert not _plist(sandbox.agents, MAIN).exists()
    assert _stash(sandbox.agents, MAIN).read_bytes() == fresh, "留下的是旧的那份，新的丢了"


def test_启用_装着且另有一份disabled_现役那份不被旧的盖掉(client, sandbox, alive):
    """§5c「放回」只在没有 `.plist` 时做；多出来的那份不管。"""
    u.dsh_bot(sandbox.dsh_cfg, BOT, enabled=False)
    live = _put(_plist(sandbox.agents, MAIN), "main-live")
    stale = _put(_stash(sandbox.agents, MAIN), "main-stale")
    _toggle(client, True)
    assert _plist(sandbox.agents, MAIN).read_bytes() == live, "现役的 plist 被那份旧的盖掉了"
    assert _stash(sandbox.agents, MAIN).read_bytes() == stale, "多出来的那份不该被动"


# ============================================================ 幂等：对已经收起的再停一次
# 第 9 稿把这件事写成了 §5c 场景 8。下面三条各量一项；整个场景逐项量一遍的那条在后面「场景 8、9」。

def test_停用_已经收起的再停一次_job仍是done且tail是已停止(client, sandbox, launchctl):
    u.dsh_bot(sandbox.dsh_cfg, BOT, enabled=False)
    _stash_both(sandbox.agents)
    launchctl.set_default(installed=False)              # 已经卸载过：bootout 报找不到
    st = _toggle(client, False)
    assert st["state"] == "done", st
    assert _last_line(st) == "%s 已停止" % BOT, st["tail"]


def test_停用_已经收起的再停一次_两份disabled逐字节不变也不多出文件(client, sandbox, launchctl):
    u.dsh_bot(sandbox.dsh_cfg, BOT, enabled=False)
    before = _stash_both(sandbox.agents)
    launchctl.set_default(installed=False)
    _toggle(client, False)
    assert _names(sandbox.agents) == sorted([MAIN + ".plist.disabled",
                                             TIMER + ".plist.disabled"]), _names(sandbox.agents)
    assert (_stash(sandbox.agents, MAIN).read_bytes(),
            _stash(sandbox.agents, TIMER).read_bytes()) == before


def test_停用_已经收起的再停一次_仍然恰好两条bootout(client, sandbox, launchctl):
    u.dsh_bot(sandbox.dsh_cfg, BOT, enabled=False)
    _stash_both(sandbox.agents)
    launchctl.set_default(installed=False)
    _toggle(client, False)
    argvs = [c["argv"] for c in launchctl.calls()]
    assert argvs == [["bootout", _target(MAIN)], ["bootout", _target(TIMER)]], argvs


# ============================================================ 场景 8、9（第 9 稿）：「再停一次」在后端做的事

def _stop_again(client, bot=BOT):
    """对一只已经停用的 bot 再请求一次停用。返回 (响应, 200 的体)。"""
    r = client.post("/hub/api/bots/%s/enabled" % bot, json={"enabled": False})
    return r, r.get_json()


def test_场景8_已停用且两份都已收起_再请求一次停用_照样受理做完_只有两条bootout_文件与配置一个字节不变(
        client, sandbox, launchctl):
    """§5c 场景 8：再停一次是幂等的。约定写明的每一项逐条量。

    对已经停干净的行点「再停一次」就是这个请求：没有副作用，最后说的是「已停止」。
    """
    yml = u.dsh_bot(sandbox.dsh_cfg, BOT, enabled=False)
    _stash_both(sandbox.agents)
    launchctl.set_default(installed=False)      # 早就卸载了：两条 bootout 假件都回 No such process
    yml_before = yml.read_bytes()
    agents_before = u.tree_bytes(sandbox.agents)

    r, body = _stop_again(client)
    assert r.status_code == 200, (r.status_code, body)
    assert body["sync"]["state"] == "started", ("同步应当重新登记", body)
    st = u.finish_sync(client, body)

    assert st["state"] == "done", st
    assert _last_line(st) == "%s 已停止" % BOT, st["tail"]
    argvs = [c["argv"] for c in launchctl.calls()]
    assert argvs == [["bootout", _target(MAIN)], ["bootout", _target(TIMER)]], (
        "应当恰好是那两条 bootout，没有别的调用", argvs)
    assert _names(sandbox.agents) == sorted([MAIN + ".plist.disabled", TIMER + ".plist.disabled"]), (
        "启动项目录里的文件名变了", _names(sandbox.agents))
    assert u.tree_bytes(sandbox.agents) == agents_before, "收起着的那两份文件内容变了"
    assert yml.read_bytes() == yml_before, "这只的配置文件字节变了（同一个值重写一遍应当一字不差）"


def test_场景9_已停用而两份plist还装着进程没在跑_再请求一次停用_做完且两份都收起(
        client, sandbox, launchctl):
    """§5c 场景 9：装着却没在跑的也能停干净。页面上对「已停用、离线」的行点「再停一次」做的就是这件事。

    走到这个样子的两条路见 §13a 第 7 条：停用的同步被别的任务挡住了；或者卸载成了、收起文件
    那一步没成。结局相同：它一恢复或下次登录就会跑起来。
    """
    yml = u.dsh_bot(sandbox.dsh_cfg, BOT, enabled=False)
    main_bytes, timer_bytes = _install_both(sandbox.agents)
    launchctl.set_default(installed=True, running=False, pid=None)   # launchd 里还装载着，进程此刻没在跑
    yml_before = yml.read_bytes()

    listing = client.get("/hub/api/bots").get_json()
    rows = [b for b in listing["bots"] if b["id"] == BOT]
    assert len(rows) == 1 and rows[0]["state"] == "offline" and BOT in listing["disabled"], (
        "前提不成立：列表里这一行应当是「已停用、离线」", listing)
    seen = len(launchctl.calls())               # 列表问过的那次只读 print 不算在下面的动作里

    r, body = _stop_again(client)
    assert r.status_code == 200, (r.status_code, body)
    assert body["sync"]["state"] == "started", body
    st = u.finish_sync(client, body)

    assert st["state"] == "done", st
    assert _last_line(st) == "%s 已停止" % BOT, st["tail"]
    # 只数会改状态的动作：列表那次只读的 print 机器忙时可能晚到，落在 seen 之后。
    # 「除了两条 bootout 没有别的调用」由上面场景 8 那条按全量来量。
    acts = [c["argv"] for c in launchctl.calls()[seen:] if c["cmd"] != "print"]
    assert acts == [["bootout", _target(MAIN)], ["bootout", _target(TIMER)]], (
        "会改状态的动作应当恰好是那两条 bootout", acts)
    assert (_state(sandbox.agents, MAIN), _state(sandbox.agents, TIMER)) == ("收起", "收起"), (
        "两份启动项文件没有都收起：下次登录它还会自己起来", _names(sandbox.agents))
    assert _names(sandbox.agents) == sorted([MAIN + ".plist.disabled", TIMER + ".plist.disabled"]), (
        "启动项目录里多了或少了文件", _names(sandbox.agents))
    assert _stash(sandbox.agents, MAIN).read_bytes() == main_bytes, "主单元那份收起之后内容变了"
    assert _stash(sandbox.agents, TIMER).read_bytes() == timer_bytes, "定时单元那份收起之后内容变了"
    assert yml.read_bytes() == yml_before, "这只的配置文件字节变了（同一个值重写一遍应当一字不差）"


# ============================================================ 场景 4：存量收敛（重启全部）

def test_重启全部_配置已停用而两份plist还在_收起两份(client, sandbox, launchctl):
    """旧逻辑停下来的 bot：配置写着停用，plist 还留在目录里。重启全部把它收回到该有的样子。"""
    u.dsh_bot(sandbox.dsh_cfg, BOT, enabled=False)
    _install_both(sandbox.agents)
    _restart_all(client)
    assert (_state(sandbox.agents, MAIN), _state(sandbox.agents, TIMER)) == ("收起", "收起"), \
        _names(sandbox.agents)


def test_重启全部_配置已停用而两份plist还在_job是done且这只的tail行是已停止(
        client, sandbox, launchctl):
    u.dsh_bot(sandbox.dsh_cfg, BOT, enabled=False)
    _install_both(sandbox.agents)
    st = _restart_all(client)
    assert st["state"] == "done", st
    assert _line_of(st) == "%s 已停止" % BOT, st["tail"]


# ============================================================ 场景 5：重启全部不把停用的拉起来

def _one_on_one_off(sandbox, heartbeat, launchctl):
    """alpha 启用；beta 停用且两份 plist 已经收起。"""
    u.dsh_bot(sandbox.dsh_cfg, "alpha")
    u.dsh_bot(sandbox.dsh_cfg, "beta", enabled=False, dispatcher_port=17952)
    launchctl.set_unit("alpha", installed=True, running=False, pid=4242)
    launchctl.set_unit("beta", installed=False, running=False, pid=None)
    launchctl.set_timer_unit("beta", installed=False, running=False, pid=None)
    heartbeat.tick("alpha", pid=4242)
    return _stash_both(sandbox.agents, "beta")


def test_重启全部_停用且已收起的那只_两个disabled还在且没有plist(
        client, sandbox, heartbeat, launchctl):
    before = _one_on_one_off(sandbox, heartbeat, launchctl)
    _restart_all(client)
    assert _names(sandbox.agents) == sorted(["com.dsh-bot.beta.plist.disabled",
                                             "com.dsh-bot.self-initiate.beta.plist.disabled"]), \
        _names(sandbox.agents)
    assert (_stash(sandbox.agents, "com.dsh-bot.beta").read_bytes(),
            _stash(sandbox.agents, "com.dsh-bot.self-initiate.beta").read_bytes()) == before


def test_重启全部_停用且已收起的那只_没有对它发过kickstart与bootstrap(
        client, sandbox, heartbeat, launchctl):
    _one_on_one_off(sandbox, heartbeat, launchctl)
    _restart_all(client)
    beta = {"com.dsh-bot.beta", "com.dsh-bot.self-initiate.beta"}
    hits = [c["argv"] for c in launchctl.calls()
            if c["label"] in beta and c["cmd"] in ("kickstart", "bootstrap")]
    assert hits == [], ("重启全部把停用的 bot 拉起来了", hits)


def test_重启全部_一只启用一只停用且已收起_job是done且汇总是成功2(
        client, sandbox, heartbeat, launchctl):
    _one_on_one_off(sandbox, heartbeat, launchctl)
    st = _restart_all(client)
    assert st["state"] == "done", st
    assert st["tail"].strip().splitlines()[0] == "重启 2 只：成功 2", st["tail"]


# ============================================================ 启用中的 bot：重启时把收起的放回

def test_重启全部_启用的那只两份都收起着_放回两份(client, sandbox, alive):
    """§3 第 2 步：拉起之前先把收起的 plist 放回。"""
    u.dsh_bot(sandbox.dsh_cfg, BOT)
    before = _stash_both(sandbox.agents)
    st = _restart_all(client)
    assert _names(sandbox.agents) == sorted([MAIN + ".plist", TIMER + ".plist"]), _names(sandbox.agents)
    assert (_plist(sandbox.agents, MAIN).read_bytes(),
            _plist(sandbox.agents, TIMER).read_bytes()) == before
    assert st["state"] == "done", st


def test_单只重启_启用的那只两份都收起着_放回两份并装回定时单元(client, sandbox, alive):
    """§4：执行内容与开关的单只同步相同，名单收成这一只。"""
    u.dsh_bot(sandbox.dsh_cfg, BOT)
    _stash_both(sandbox.agents)
    r = client.post("/hub/api/bots/%s/restart" % BOT)
    assert r.status_code == 202, (r.status_code, r.get_json())
    st = u.wait_job(client, r.get_json()["job_id"])
    assert _names(sandbox.agents) == sorted([MAIN + ".plist", TIMER + ".plist"]), _names(sandbox.agents)
    assert alive.actions() == [
        ["kickstart", "-k", _target(MAIN)],
        ["bootout", _target(TIMER)],
        ["bootstrap", _domain(), str(_plist(sandbox.agents, TIMER))],
    ], alive.calls()
    assert st["state"] == "done", st


# ============================================================ 场景 6：主单元没装

def test_启用_主单元没装_job失败且原因是单元没装(client, sandbox, launchctl):
    """两个文件都没有，launchd 里也没有这个服务。"""
    u.dsh_bot(sandbox.dsh_cfg, BOT, enabled=False)
    launchctl.set_unit(BOT, installed=False, running=False, pid=None)
    st = _toggle(client, True)
    assert st["state"] == "failed", st
    assert _last_line(st) == _fail_line(sandbox, "单元没装（plist 不存在）"), st["tail"]


def test_启用_主单元没装_plist目录里什么都没建出来(client, sandbox, launchctl):
    u.dsh_bot(sandbox.dsh_cfg, BOT, enabled=False)
    launchctl.set_unit(BOT, installed=False, running=False, pid=None)
    _toggle(client, True)
    assert _names(sandbox.agents) == [], _names(sandbox.agents)


# ============================================================ 场景 7：放回失败

@needs_perm
def test_启用_plist目录不可写_job失败且原因是plist没能放回(client, sandbox, alive):
    u.dsh_bot(sandbox.dsh_cfg, BOT, enabled=False)
    _stash_both(sandbox.agents)
    os.chmod(sandbox.agents, 0o555)
    try:
        st = _toggle(client, True)
    finally:
        os.chmod(sandbox.agents, 0o755)
    assert st["state"] == "failed", st
    assert _last_line(st) == _fail_line(sandbox, "plist 没能放回"), st["tail"]


@needs_perm
def test_启用_plist目录不可写_launchctl零调用(client, sandbox, alive):
    """§5c 启用方向第 1 步：改名出错，后面的 launchctl 一条都不发。"""
    u.dsh_bot(sandbox.dsh_cfg, BOT, enabled=False)
    _stash_both(sandbox.agents)
    os.chmod(sandbox.agents, 0o555)
    try:
        _toggle(client, True)
    finally:
        os.chmod(sandbox.agents, 0o755)
    assert alive.calls() == [], ("放回失败之后还发了 launchctl", alive.calls())


@needs_perm
def test_启用_plist目录不可写_两份仍是收起状态且逐字节不变(client, sandbox, alive):
    u.dsh_bot(sandbox.dsh_cfg, BOT, enabled=False)
    before = _stash_both(sandbox.agents)
    os.chmod(sandbox.agents, 0o555)
    try:
        _toggle(client, True)
    finally:
        os.chmod(sandbox.agents, 0o755)
    assert _names(sandbox.agents) == sorted([MAIN + ".plist.disabled",
                                             TIMER + ".plist.disabled"]), _names(sandbox.agents)
    assert (_stash(sandbox.agents, MAIN).read_bytes(),
            _stash(sandbox.agents, TIMER).read_bytes()) == before


# ============================================================ 收起失败

@needs_perm
def test_停用_plist目录不可写_job失败且原因是plist没能收起(client, sandbox, launchctl):
    u.dsh_bot(sandbox.dsh_cfg, BOT)
    _install_both(sandbox.agents)
    os.chmod(sandbox.agents, 0o555)
    try:
        st = _toggle(client, False)
    finally:
        os.chmod(sandbox.agents, 0o755)
    assert st["state"] == "failed", st
    assert _last_line(st) == _fail_line(sandbox, "plist 没能收起"), st["tail"]


@needs_perm
def test_停用_plist目录不可写_两条bootout照发(client, sandbox, launchctl):
    """收起排在两条 bootout 之后：改名失败不等于进程没停。"""
    u.dsh_bot(sandbox.dsh_cfg, BOT)
    _install_both(sandbox.agents)
    os.chmod(sandbox.agents, 0o555)
    try:
        _toggle(client, False)
    finally:
        os.chmod(sandbox.agents, 0o755)
    argvs = [c["argv"] for c in launchctl.calls()]
    assert argvs == [["bootout", _target(MAIN)], ["bootout", _target(TIMER)]], argvs


# ============================================================ bootout 出错时，收起照做

def test_停用_主单元bootout报错_两份plist照样收起(client, sandbox, launchctl):
    """§5c：第 1 或第 2 步失败时，第 3 步照做。"""
    u.dsh_bot(sandbox.dsh_cfg, BOT)
    _install_both(sandbox.agents)
    launchctl.set_bootout_fail(MAIN)
    _toggle(client, False)
    assert (_state(sandbox.agents, MAIN), _state(sandbox.agents, TIMER)) == ("收起", "收起"), \
        _names(sandbox.agents)


def test_停用_主单元bootout报错_这只按失败报且不说已停止(client, sandbox, launchctl):
    u.dsh_bot(sandbox.dsh_cfg, BOT)
    _install_both(sandbox.agents)
    launchctl.set_bootout_fail(MAIN)
    st = _toggle(client, False)
    assert st["state"] == "failed", st
    line = _last_line(st)
    assert line.startswith("%s 失败：" % BOT), st["tail"]
    assert line.endswith("；日志 %s" % (sandbox.dsh_home / "bots" / BOT / "logs" / "launchd.log")), \
        st["tail"]
    assert "已停止" not in st["tail"], st["tail"]


def test_停用_主单元bootout报错_定时单元那条bootout仍然发出(client, sandbox, launchctl):
    u.dsh_bot(sandbox.dsh_cfg, BOT)
    _install_both(sandbox.agents)
    launchctl.set_bootout_fail(MAIN)
    _toggle(client, False)
    argvs = [c["argv"] for c in launchctl.calls_of("bootout")]
    assert argvs == [["bootout", _target(MAIN)], ["bootout", _target(TIMER)]], argvs


def test_停用_定时单元bootout报错_两份plist照样收起且这只按失败报(client, sandbox, launchctl):
    u.dsh_bot(sandbox.dsh_cfg, BOT)
    _install_both(sandbox.agents)
    launchctl.set_bootout_fail(TIMER)
    st = _toggle(client, False)
    assert (_state(sandbox.agents, MAIN), _state(sandbox.agents, TIMER)) == ("收起", "收起"), \
        _names(sandbox.agents)
    assert st["state"] == "failed", st


def test_停用_bootout超时_两份plist照样收起且tail说超时(client, sandbox, launchctl, monkeypatch):
    u.dsh_bot(sandbox.dsh_cfg, BOT)
    _install_both(sandbox.agents)
    launchctl.set_delay("bootout", 2.0)
    u.tighten_constant(monkeypatch, name="LAUNCHCTL_TIMEOUT", new=0.5)
    st = _toggle(client, False)
    assert st["state"] == "failed", st
    assert "超时" in _last_line(st), st["tail"]
    assert (_state(sandbox.agents, MAIN), _state(sandbox.agents, TIMER)) == ("收起", "收起"), \
        _names(sandbox.agents)


# ============================================================ 定时单元没装回去

def test_启用_定时单元bootstrap失败_job失败且原因是主动消息的定时任务没装回去(
        client, sandbox, alive):
    u.dsh_bot(sandbox.dsh_cfg, BOT, enabled=False)
    _stash_both(sandbox.agents)
    alive.set_bootstrap_fail(True)          # 主单元走 kickstart 就成了，bootstrap 只有定时单元那一条
    st = _toggle(client, True)
    assert st["state"] == "failed", st
    assert _last_line(st) == _fail_line(sandbox, "主动消息的定时任务没装回去"), st["tail"]


def test_启用_定时单元装着_先bootout它再bootstrap它(client, sandbox, alive):
    """§5c 启用方向第 3 步：定时单元装着，就先 bootout 再 bootstrap。"""
    u.dsh_bot(sandbox.dsh_cfg, BOT)
    _install_both(sandbox.agents)
    st = _toggle(client, True)
    assert alive.actions() == [
        ["kickstart", "-k", _target(MAIN)],
        ["bootout", _target(TIMER)],
        ["bootstrap", _domain(), str(_plist(sandbox.agents, TIMER))],
    ], alive.calls()
    assert st["state"] == "done", st


# ============================================================ plist 目录用受理时定下的那个（§1a）

def test_停用_bootout卡着时换掉plist目录_收起的是受理时那个目录里的(
        client, sandbox, launchctl, monkeypatch):
    u.dsh_bot(sandbox.dsh_cfg, BOT)
    _install_both(sandbox.agents)
    other = sandbox.tmp / "LaunchAgents-later"
    other.mkdir()
    later = _install_both(other)
    hold = launchctl.hold_first("bootout")
    r = client.post("/hub/api/bots/%s/enabled" % BOT, json={"enabled": False})
    assert r.status_code == 200, (r.status_code, r.get_json())
    hold.wait_reached()
    monkeypatch.setenv("HUB_LAUNCHAGENTS_DIR", str(other))      # 受理之后才换：另一个临时目录
    hold.release()
    st = u.finish_sync(client, r.get_json(), timeout=40.0)
    assert st["state"] == "done", st
    assert _names(sandbox.agents) == sorted([MAIN + ".plist.disabled",
                                             TIMER + ".plist.disabled"]), _names(sandbox.agents)
    assert _names(other) == sorted([MAIN + ".plist", TIMER + ".plist"]), _names(other)
    assert (_plist(other, MAIN).read_bytes(), _plist(other, TIMER).read_bytes()) == later


# ============================================================ 保留名：不给名字，也不动文件

def test_重启全部_保留名的配置写着停用_它名下的两份plist文件名不变(client, sandbox, launchctl):
    """§5b 第 5 条：两份 plist 的文件名同样先过合格 bot id 的校验，不合格就不动文件。"""
    u.dsh_bot(sandbox.dsh_cfg, "jiwen", enabled=False)
    before = _install_both(sandbox.agents, "jiwen")
    st = _restart_all(client)
    assert _names(sandbox.agents) == sorted(["com.dsh-bot.jiwen.plist",
                                             "com.dsh-bot.self-initiate.jiwen.plist"]), \
        _names(sandbox.agents)
    assert (_plist(sandbox.agents, "com.dsh-bot.jiwen").read_bytes(),
            _plist(sandbox.agents, "com.dsh-bot.self-initiate.jiwen").read_bytes()) == before
    assert launchctl.calls() == [], launchctl.calls()
    assert st["tail"].strip() == "没有可重启的 bot", st["tail"]


# ============================================================ 新建 bot 不受这一节影响

def test_新建_碰巧留着一份同名的disabled_照常建成且那份文件不动(
        client, sandbox, bun, telegram, launchctl, heartbeat, monkeypatch):
    """§5c 末段：新建的装启动项那一步自己写 `.plist` 并装载，留着的 `.disabled` 不管它。"""
    u.tighten_constant(monkeypatch, value=30, new=3.0)          # 新建探活上限（§1，测试可改）
    stale = _put(_stash(sandbox.agents, MAIN), "main-stale")
    bun.expect(exit=0).expect(exit=0)
    launchctl.set_default(installed=True, running=True, pid=4242)
    heartbeat.tick(BOT, pid=4242)
    r = client.post("/hub/api/bots", json={
        "bot_id": BOT, "telegram_token": u.FAKE_TOKEN, "dispatcher_port": u.free_port(),
        "chat_id": 123456789, "display_name": "小雨", "persona_text": "我是小雨的人设正文。"})
    assert r.status_code == 201, (r.status_code, r.get_json())
    assert _stash(sandbox.agents, MAIN).read_bytes() == stale
