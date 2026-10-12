# -*- coding: utf-8 -*-
"""§1a 受理时定下接缝，后台不再读环境（接口约定第 6 稿追加）。

依据 `INTERFACE-新系统bot运维.md` §1a「可断言的场景」甲、乙、丙，逐步照做。
BRIEF 2026-10-12 追加第 3 条：后台那一段要用的命令路径与目录，必须在受理那一刻定下来，
之后不许再去读环境变量。

本文件的铁律（§1a 原话，也是 §0 第 4 条）：受理之后改环境，一律「改成另一个假件或另一个
临时目录」，绝不删变量。在还没按这一条实现的代码上，删掉 HUB_LAUNCHCTL 会让后台真的
调到系统那支 launchctl。改成另一支假件 B，已经足以说明后台有没有再读环境。

黑盒测不到的一点（如实说）：「处理请求的线程里就定好」与「后台线程一启动就定好」，
从外面分不出来 —— 测试能改环境的最早时刻是拿到响应之后。
"""
import pytest

from . import botops_util as u

KINDS = ["restart-all", "restart-one", "toggle-on"]        # 三种 job 的启用方向


@pytest.fixture
def elsewhere(sandbox, monkeypatch, spare_launchctl):
    """「另一套环境」：另一支假 launchctl（B）、另一个新系统家目录（H2）、另外几个目录。

    全部在本用例的 tmp 里。`apply()` 把 §1a 场景甲第 3 步点到的六个变量一次改过去。
    """
    b = spare_launchctl("b")
    b.set_default(installed=True, running=True, pid=4242)
    dirs = {}
    for name in ("dsh-home-2", "LaunchAgents-2", "configs-dsh-2", "configs-legacy-2", "fakehome-2"):
        d = sandbox.tmp / name
        d.mkdir()
        dirs[name] = d

    class Elsewhere:
        launchctl = b
        home = dirs["dsh-home-2"]
        agents = dirs["LaunchAgents-2"]
        dsh_cfg = dirs["configs-dsh-2"]

        @staticmethod
        def apply():
            monkeypatch.setenv("HUB_LAUNCHCTL", str(b.path))
            monkeypatch.setenv("DSH_BOT_HOME", str(dirs["dsh-home-2"]))
            monkeypatch.setenv("HUB_LAUNCHAGENTS_DIR", str(dirs["LaunchAgents-2"]))
            monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(dirs["configs-dsh-2"]))
            monkeypatch.setenv("HUB_CONFIGS_DIR", str(dirs["configs-legacy-2"]))
            monkeypatch.setenv("HOME", str(dirs["fakehome-2"]))

    return Elsewhere


def _prepare(sandbox, kind):
    """造好那只 bot：开关启用要它原先停着，另两种要它原先启用。"""
    u.dsh_bot(sandbox.dsh_cfg, "xiaoyu", **({"enabled": False} if kind == "toggle-on" else {}))


def _fire(client, kind):
    """发请求并确认被受理，返回 job_id（§1a：202；开关是 200 且 sync.state 为 started）。"""
    if kind == "restart-all":
        r = client.post("/hub/api/bots/restart")
        assert r.status_code == 202, (r.status_code, r.get_json())
        return r.get_json()["job_id"]
    if kind == "restart-one":
        r = client.post("/hub/api/bots/xiaoyu/restart")
        assert r.status_code == 202, (r.status_code, r.get_json())
        return r.get_json()["job_id"]
    r = client.post("/hub/api/bots/xiaoyu/enabled", json={"enabled": True})
    assert r.status_code == 200, (r.status_code, r.get_json())
    sync = r.get_json()["sync"]
    assert sync["state"] == "started", sync
    return sync["job_id"]


# ============================================================ 场景甲：判活

@pytest.mark.parametrize("kind", KINDS)
def test_甲_受理后换掉环境_心跳写在受理时的家目录_job照样done且另一支假件零调用(
        client, sandbox, heartbeat, launchctl, elsewhere, as_darwin, monkeypatch, kind):
    u.tighten_constant(monkeypatch, value=45, new=6.0)
    _prepare(sandbox, kind)
    launchctl.set_default(installed=True, running=False, pid=4242)      # A；H1 下此刻没有心跳
    job_id = _fire(client, kind)                                        # 第 2 步：受理
    elsewhere.apply()                                                   # 第 3 步：环境全换
    seen = len(launchctl.calls())
    heartbeat.tick("xiaoyu", pid=4242)                                  # 第 4 步：心跳写在 H1
    st = u.wait_job(client, job_id, timeout=30.0)                       # 第 5 步
    b_calls = elsewhere.launchctl.calls()
    assert b_calls == [], ("受理之后后台又读了 HUB_LAUNCHCTL，打到了另一支假件", b_calls)
    assert st["state"] == "done", ("心跳写在受理时的家目录里，后台却没等到", st)
    later = [c["cmd"] for c in launchctl.calls()[seen:]]
    assert "print" in later, ("环境换掉之后，受理时那支假件应当还收得到 print", later)


@pytest.mark.parametrize("kind", KINDS)
def test_甲对照_受理后换掉环境_心跳只写在新换的家目录_job应failed且日志路径仍在受理时的家目录(
        client, sandbox, heartbeat, launchctl, elsewhere, as_darwin, monkeypatch, kind):
    u.tighten_constant(monkeypatch, value=45, new=2.0)
    _prepare(sandbox, kind)
    launchctl.set_default(installed=True, running=False, pid=4242)
    job_id = _fire(client, kind)
    elsewhere.apply()
    heartbeat.tick("xiaoyu", pid=4242, home=elsewhere.home)             # 第 6 步：只写 H2，H1 不写
    st = u.wait_job(client, job_id, timeout=30.0)
    assert st["state"] == "failed", ("后台去读了受理之后才换上的家目录", st)
    assert "心跳" in st["tail"], st["tail"]
    assert str(sandbox.dsh_home / "bots" / "xiaoyu" / "logs" / "launchd.log") in st["tail"], st["tail"]
    assert str(elsewhere.home) not in st["tail"], st["tail"]
    assert elsewhere.launchctl.calls() == [], elsewhere.launchctl.calls()


# ============================================================ 场景乙：后续的动作命令（重启全部）

def _scene_b(client, sandbox, heartbeat, launchctl, elsewhere, monkeypatch):
    """三只：alpha 启用且已装；beta 启用但单元没装（kickstart 找不到 → 回落 bootstrap）；
    gamma 停用（bootout）。A 的第一条 kickstart 卡住，期间把环境全换掉，再放行。

    返回 job 的终态体。受理时的 plist 目录是 sandbox.agents；新换的那个目录里也放一份
    同名 plist —— 后台要是又读了环境，bootstrap 会带上那一份的路径。
    """
    u.tighten_constant(monkeypatch, value=45, new=5.0)
    u.dsh_bot(sandbox.dsh_cfg, "alpha")
    u.dsh_bot(sandbox.dsh_cfg, "beta")
    u.dsh_bot(sandbox.dsh_cfg, "gamma", enabled=False)
    a, b = launchctl, elsewhere.launchctl
    for fake in (a, b):
        fake.set_unit("alpha", installed=True, running=False, pid=4242)
        fake.set_unit("beta", installed=False, running=False, pid=None)
        fake.set_unit("gamma", installed=True, running=True, pid=5151)
    (sandbox.agents / "com.dsh-bot.beta.plist").write_text("<plist/>", encoding="utf-8")
    (elsewhere.agents / "com.dsh-bot.beta.plist").write_text("<plist/>", encoding="utf-8")
    heartbeat.tick("alpha", pid=4242)                   # 心跳在受理时的家目录（H1）
    heartbeat.tick("beta", pid=4242)
    hold = a.hold_first("kickstart")
    r = client.post("/hub/api/bots/restart")
    assert r.status_code == 202, (r.status_code, r.get_json())
    hold.wait_reached()                                 # 第一条 kickstart 已发出、正卡着
    elsewhere.apply()                                   # 受理之后、放行之前：环境全换
    hold.release()
    return u.wait_job(client, r.get_json()["job_id"], timeout=40.0)


def test_乙_第一条kickstart卡着时换掉环境_放行后另一支假件零调用(
        client, sandbox, heartbeat, launchctl, elsewhere, as_darwin, monkeypatch):
    _scene_b(client, sandbox, heartbeat, launchctl, elsewhere, monkeypatch)
    b_calls = elsewhere.launchctl.calls()
    assert b_calls == [], ("放行之后的命令打到了受理之后才换上的那支假件", b_calls)


def test_乙_第一条kickstart卡着时换掉环境_两只启用的bot的kickstart都打到受理时那支(
        client, sandbox, heartbeat, launchctl, elsewhere, as_darwin, monkeypatch):
    _scene_b(client, sandbox, heartbeat, launchctl, elsewhere, monkeypatch)
    kicked = {c["label"] for c in launchctl.calls_of("kickstart")}
    assert kicked == {"com.dsh-bot.alpha", "com.dsh-bot.beta"}, launchctl.calls()


def test_乙_第一条kickstart卡着时换掉环境_回落bootstrap带的plist在受理时那个目录下(
        client, sandbox, heartbeat, launchctl, elsewhere, as_darwin, monkeypatch):
    _scene_b(client, sandbox, heartbeat, launchctl, elsewhere, monkeypatch)
    boots = launchctl.calls_of("bootstrap")
    assert len(boots) == 1, ("beta 的回落 bootstrap 应当恰好一条、打到受理时那支", launchctl.calls())
    argv = boots[0]["argv"]
    assert str(sandbox.agents / "com.dsh-bot.beta.plist") in argv, argv
    assert not any(str(elsewhere.agents) in str(x) for x in argv), argv


def test_乙_第一条kickstart卡着时换掉环境_停用那只的bootout打到受理时那支(
        client, sandbox, heartbeat, launchctl, elsewhere, as_darwin, monkeypatch):
    _scene_b(client, sandbox, heartbeat, launchctl, elsewhere, monkeypatch)
    outs = [c["label"] for c in launchctl.calls_of("bootout")]
    # 第 8 稿 §5c：停用那只有两条 bootout（主单元、主动消息的定时单元），都打到受理时那支
    assert outs == ["com.dsh-bot.gamma", "com.dsh-bot.self-initiate.gamma"], launchctl.calls()


def test_乙_第一条kickstart卡着时换掉环境_三只都按受理时定下的样子做完(
        client, sandbox, heartbeat, launchctl, elsewhere, as_darwin, monkeypatch):
    st = _scene_b(client, sandbox, heartbeat, launchctl, elsewhere, monkeypatch)
    assert st["tail"].strip().splitlines()[0] == "重启 3 只：成功 3", st["tail"]
    assert st["state"] == "done", st


# ============================================================ 受理之后也不再读配置（§1a 首段、§3、§4b）

def test_重启全部_第一条kickstart卡着时把另一只改成停用_它仍按受理时的方向被kickstart(
        client, sandbox, heartbeat, launchctl, as_darwin, monkeypatch):
    """执行范围与每只的方向在受理时算好；后台不再读配置文件。"""
    u.tighten_constant(monkeypatch, value=45, new=5.0)
    u.dsh_bot(sandbox.dsh_cfg, "alpha")
    u.dsh_bot(sandbox.dsh_cfg, "beta")
    launchctl.set_default(installed=True, running=False, pid=4242)
    heartbeat.tick("alpha", pid=4242)
    heartbeat.tick("beta", pid=4242)
    hold = launchctl.hold_first("kickstart")
    r = client.post("/hub/api/bots/restart")
    assert r.status_code == 202, (r.status_code, r.get_json())
    hold.wait_reached()
    first = launchctl.calls_of("kickstart")[0]["label"].rsplit(".", 1)[-1]
    other = "beta" if first == "alpha" else "alpha"
    u.dsh_bot(sandbox.dsh_cfg, other, enabled=False)    # 受理之后改配置：另一只写成停用
    hold.release()
    u.wait_job(client, r.get_json()["job_id"], timeout=40.0)
    label = "com.dsh-bot.%s" % other
    assert label in [c["label"] for c in launchctl.calls_of("kickstart")], launchctl.calls()
    assert label not in [c["label"] for c in launchctl.calls_of("bootout")], launchctl.calls()


def test_开关启用_kickstart卡着时把配置改回停用_这次同步仍走启用方向(
        client, sandbox, heartbeat, launchctl, as_darwin, monkeypatch):
    """§4b：同步方向在受理时读一次（写盘之后、回 200 之前），后台不再读配置。"""
    u.tighten_constant(monkeypatch, value=45, new=5.0)
    u.dsh_bot(sandbox.dsh_cfg, "xiaoyu", enabled=False)
    launchctl.set_default(installed=True, running=False, pid=4242)
    heartbeat.tick("xiaoyu", pid=4242)
    hold = launchctl.hold_first("kickstart")
    r = client.post("/hub/api/bots/xiaoyu/enabled", json={"enabled": True})
    assert r.status_code == 200, (r.status_code, r.get_json())
    assert r.get_json()["sync"]["state"] == "started", r.get_json()
    hold.wait_reached()
    u.dsh_bot(sandbox.dsh_cfg, "xiaoyu", enabled=False)                 # 受理之后把文件改回停用
    hold.release()
    st = u.finish_sync(client, r.get_json(), timeout=40.0)
    assert "bootout" not in launchctl.subcommands(), launchctl.calls()
    assert st["state"] == "done", st
    assert "xiaoyu 已重启" in st["tail"], st["tail"]


# ============================================================ 场景丙：覆盖命令

def _override_cmd(out_file):
    """覆盖命令：把自己看到的三个环境变量写进 out_file（文件出现 = 这条命令跑过）。

    路径烘焙在命令串里，不经环境传 —— 要测的正是子进程拿到的环境。
    """
    return u.python_cmd(
        "import os;open('%s','w').write('|'.join(os.environ.get(k,'<unset>') for k in "
        "('HUB_CONFIGS_DIR','BOTOPS_EARLY','BOTOPS_LATE')))" % out_file)


def _fire_override(client, kind):
    if kind == "toggle-on":
        r = client.post("/hub/api/bots/xiaoyu/enabled", json={"enabled": True})
        assert r.status_code == 200, (r.status_code, r.get_json())
        assert r.get_json()["sync"]["state"] == "started", r.get_json()
        return r.get_json()["sync"]["job_id"]
    return _fire(client, kind)


def _scene_c(client, sandbox, elsewhere, monkeypatch, kind):
    """C1 受理之后立刻：命令串换成 C2，新系统配置根改掉，再新设一个变量。返回 (M1, M2)。"""
    _prepare(sandbox, kind)
    m1, m2 = sandbox.tmp / "m1.txt", sandbox.tmp / "m2.txt"
    monkeypatch.setenv("BOTOPS_EARLY", "early")
    monkeypatch.setenv("HUB_RESTART_CMD", _override_cmd(m1))
    job_id = _fire_override(client, kind)
    monkeypatch.setenv("HUB_RESTART_CMD", _override_cmd(m2))            # 受理之后才换上的命令
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(elsewhere.dsh_cfg))
    monkeypatch.setenv("BOTOPS_LATE", "late")
    st = u.wait_job(client, job_id, timeout=30.0)
    assert st["state"] == "done", st
    return m1, m2


@pytest.mark.parametrize("kind", KINDS)
def test_丙_受理后换掉覆盖命令_跑的是受理时那条_后换的那条不跑(
        client, sandbox, launchctl, elsewhere, as_darwin, monkeypatch, kind):
    m1, m2 = _scene_c(client, sandbox, elsewhere, monkeypatch, kind)
    assert m1.exists(), "受理时的那条覆盖命令没跑"
    assert not m2.exists(), "受理之后才换上的那条覆盖命令跑了"


@pytest.mark.parametrize("kind", KINDS)
def test_丙_受理后改环境_覆盖命令的子进程看到的是受理那一刻的环境(
        client, sandbox, launchctl, elsewhere, as_darwin, monkeypatch, kind):
    """HUB_CONFIGS_DIR 注入成受理时的新系统配置根（§3）；受理前设的变量带得过去，受理后设的带不过去。"""
    m1, _m2 = _scene_c(client, sandbox, elsewhere, monkeypatch, kind)
    assert m1.exists(), "受理时的那条覆盖命令没跑"
    seen = m1.read_text(encoding="utf-8").split("|")
    assert seen == [str(sandbox.dsh_cfg), "early", "<unset>"], seen
