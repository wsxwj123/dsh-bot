# -*- coding: utf-8 -*-
"""§3 POST /hub/api/bots/restart：重启全部。

两种执行路径分开覆盖：launchd 路径（假 launchctl）与覆盖命令路径（HUB_RESTART_CMD）。
"""
import re

from . import botops_util as u


def _post(client):
    return client.post("/hub/api/bots/restart")


def _post_accepted(client):
    r = _post(client)
    assert r.status_code == 202, (r.status_code, r.get_json())
    body = r.get_json()
    assert re.fullmatch(r"[0-9a-f]{8}", body["job_id"]), body
    assert isinstance(body["started_at"], int), body
    return body


# ------------------------------------------------------------ 受理与拒绝

def test_重启全部_无命令且非macOS_409_restart_unsupported(client, as_linux, launchctl):
    r = _post(client)
    assert r.status_code == 409, r.get_json()
    body = r.get_json()
    assert body["error"] == "restart_unsupported"
    assert body.get("detail"), "要给人话 detail"
    assert launchctl.calls() == [], "不可用却动了 launchd"


def test_重启全部_名单空_直接done且不动任何单元(client, as_darwin, launchctl):
    body = _post_accepted(client)
    st = u.wait_job(client, body["job_id"])
    assert st["state"] == "done"
    assert st["exit_code"] is None
    assert st["tail"].strip() == "没有可重启的 bot"
    assert launchctl.calls() == [], "空名单却调了 launchctl"


# ------------------------------------------------------------ launchd 路径

def test_重启全部_启用的一只_kickstart后判活通过_就是done(
        client, sandbox, heartbeat, launchctl, as_darwin):
    u.dsh_bot(sandbox.dsh_cfg, "xiaoyu")
    launchctl.set_default(installed=True, running=False, pid=4242)
    heartbeat.tick("xiaoyu", pid=4242)
    body = _post_accepted(client)
    st = u.wait_job(client, body["job_id"])
    assert st["state"] == "done", st
    assert st["exit_code"] is None, "launchd 路径的 exit_code 恒为 null"
    assert launchctl.subcommands()[0] == "kickstart", launchctl.calls()
    assert launchctl.labels()[0] == "com.dsh-bot.xiaoyu", launchctl.calls()
    first_line = st["tail"].strip().splitlines()[0]
    assert first_line == "重启 1 只：成功 1", st["tail"]
    assert "xiaoyu 已重启" in st["tail"], st["tail"]


def test_重启全部_kickstart找不到服务时回落bootstrap并成功(
        client, sandbox, heartbeat, launchctl, as_darwin):
    u.dsh_bot(sandbox.dsh_cfg, "xiaoyu")
    (sandbox.agents / "com.dsh-bot.xiaoyu.plist").write_text("<plist/>", encoding="utf-8")
    launchctl.set_default(installed=False)          # kickstart 会报找不到
    heartbeat.tick("xiaoyu", pid=4242)
    body = _post_accepted(client)
    st = u.wait_job(client, body["job_id"])
    assert st["state"] == "done", st
    cmds = launchctl.subcommands()
    assert cmds[:2] == ["kickstart", "bootstrap"], cmds
    assert "重启 1 只：成功 1" in st["tail"]


def test_重启全部_kickstart找不到且plist不在_该只失败且tail带日志路径(
        client, sandbox, heartbeat, launchctl, as_darwin):
    u.dsh_bot(sandbox.dsh_cfg, "xiaoyu")
    launchctl.set_default(installed=False)          # kickstart 报找不到
    heartbeat.tick("xiaoyu", pid=4242)
    body = _post_accepted(client)
    st = u.wait_job(client, body["job_id"])
    assert st["state"] == "failed", st
    assert st["exit_code"] is None
    tail = st["tail"]
    assert tail.strip().splitlines()[0] == "重启 1 只：成功 0，失败 1", tail
    line = [ln for ln in tail.splitlines() if ln.startswith("xiaoyu ")][0]
    assert "失败" in line, tail
    assert str(sandbox.dsh_home / "bots" / "xiaoyu" / "logs" / "launchd.log") in line, tail


def test_重启全部_停用的一只_bootout且tail说已停止(
        client, sandbox, heartbeat, launchctl, as_darwin):
    u.dsh_bot(sandbox.dsh_cfg, "xiaoyu", enabled=False)
    launchctl.set_default(installed=True, running=True, pid=4242)
    heartbeat.tick("xiaoyu", pid=4242)
    body = _post_accepted(client)
    st = u.wait_job(client, body["job_id"])
    assert st["state"] == "done", st
    # 第 8 稿 §5c：停用方向恰好两条 bootout，先主单元、后主动消息的定时单元
    assert launchctl.subcommands() == ["bootout", "bootout"], launchctl.calls()
    assert launchctl.labels() == ["com.dsh-bot.xiaoyu",
                                  "com.dsh-bot.self-initiate.xiaoyu"], launchctl.calls()
    assert "xiaoyu 已停止" in st["tail"], st["tail"]


def test_重启全部_bootout报No_such_process也算成功(
        client, sandbox, heartbeat, launchctl, as_darwin):
    u.dsh_bot(sandbox.dsh_cfg, "xiaoyu", enabled=False)
    launchctl.set_default(installed=False)          # bootout → No such process
    heartbeat.tick("xiaoyu", pid=4242)
    body = _post_accepted(client)
    st = u.wait_job(client, body["job_id"])
    assert st["state"] == "done", st
    assert "xiaoyu 已停止" in st["tail"], st["tail"]


def test_重启全部_launchctl调用超时_该只失败且互斥照常释放(
        client, sandbox, heartbeat, launchctl, as_darwin):
    u.dsh_bot(sandbox.dsh_cfg, "xiaoyu")
    launchctl.set_delay("kickstart", 3.0)
    heartbeat.tick("xiaoyu", pid=4242)
    # 半秒的超时只罩住要它超时的第一次重启，出了块就放回契约的原值（§1：10 秒）。
    # 这条守的是「超时之后互斥照常释放」，不是「第二次重启在半秒超时下也跑得完」。
    with u.tightened(name="LAUNCHCTL_TIMEOUT", new=0.5):
        body = _post_accepted(client)
        st = u.wait_job(client, body["job_id"], timeout=15.0)
    assert st["state"] == "failed", st
    assert "超时" in st["tail"], st["tail"]
    # 互斥照常释放：撤掉延时，下一次重启还能受理并跑完
    launchctl.set_delay("kickstart", 0)
    body2 = _post_accepted(client)
    assert u.wait_job(client, body2["job_id"])["state"] == "done"


def test_重启全部_两只一成一败_tail首行汇总(
        client, sandbox, heartbeat, launchctl, as_darwin):
    u.dsh_bot(sandbox.dsh_cfg, "alpha")
    u.dsh_bot(sandbox.dsh_cfg, "beta")
    launchctl.set_default(installed=False)              # beta：kickstart 找不到、plist 也不在
    launchctl.set_unit("alpha", installed=True, running=False, pid=4242)
    heartbeat.tick("alpha", pid=4242)
    body = _post_accepted(client)
    st = u.wait_job(client, body["job_id"])
    assert st["state"] == "failed", st
    assert st["tail"].strip().splitlines()[0] == "重启 2 只：成功 1，失败 1", st["tail"]


def test_重启全部_旧系统的bot不被碰(client, sandbox, heartbeat, launchctl, as_darwin):
    """旧根里的 bot 不在这一页的名单里，任何 launchctl 调用都不许提到它。"""
    # 假名：这台机器上真有 bot2 / bot4，会走到 launchctl 的流程一律不用真名
    u.write_bot_yml(sandbox.legacy_cfg, "oldbot", display_name="旧二", dispatcher_port=17899)
    u.dsh_bot(sandbox.dsh_cfg, "newbot")
    launchctl.set_default(installed=True, running=False, pid=4242)
    heartbeat.tick("newbot", pid=4242)
    legacy_before = u.snapshot_tree(sandbox.legacy_cfg)
    body = _post_accepted(client)
    st = u.wait_job(client, body["job_id"])
    assert st["state"] == "done", st
    assert set(launchctl.labels()) == {"com.dsh-bot.newbot"}, launchctl.calls()
    assert u.snapshot_tree(sandbox.legacy_cfg) == legacy_before, "旧配置根被动过"


# ------------------------------------------------------------ 覆盖命令路径

def test_重启全部_覆盖命令_执行且done_exit_code为0(client, sandbox, monkeypatch):
    marker = sandbox.tmp / "ran.txt"
    monkeypatch.setenv("BOTOPS_MARKER", str(marker))
    monkeypatch.setenv("HUB_RESTART_CMD",
                       u.python_cmd("import os;open(os.environ['BOTOPS_MARKER'],'w')"
                                    ".write('ran')"))
    body = _post_accepted(client)
    st = u.wait_job(client, body["job_id"])
    assert st["state"] == "done", st
    assert st["exit_code"] == 0
    assert marker.read_text(encoding="utf-8") == "ran"


def test_重启全部_覆盖命令失败_退出码如实带出(client, monkeypatch):
    monkeypatch.setenv("HUB_RESTART_CMD", u.python_cmd("raise SystemExit(3)"))
    body = _post_accepted(client)
    st = u.wait_job(client, body["job_id"])
    assert st["state"] == "failed", st
    assert st["exit_code"] == 3


def test_重启全部_进行中_第二次409且带正在跑的job_id(client, monkeypatch):
    monkeypatch.setenv("HUB_RESTART_CMD", u.python_cmd("import time;time.sleep(2)"))
    first = _post_accepted(client)
    r = _post(client)
    assert r.status_code == 409, r.get_json()
    body = r.get_json()
    assert body["error"] == "restart_in_progress"
    assert body["job_id"] == first["job_id"]
    assert u.wait_job(client, first["job_id"])["state"] == "done"


# ------------------------------------------------------------ tail 规约

def test_重启全部_tail里不许出现token(client, monkeypatch):
    monkeypatch.setenv("BOTOPS_TOKEN", u.FAKE_TOKEN)
    monkeypatch.setenv("HUB_RESTART_CMD",
                       u.python_cmd("import os;print(os.environ['BOTOPS_TOKEN'])"))
    body = _post_accepted(client)
    st = u.wait_job(client, body["job_id"])
    assert u.FAKE_TOKEN not in repr(st), st["tail"]
    assert "***" in st["tail"], "输出根本没被采集：%r" % st["tail"]


def test_重启全部_tail截尾2000字(client, monkeypatch):
    monkeypatch.setenv("HUB_RESTART_CMD", u.python_cmd("print('x' * 5000)"))
    body = _post_accepted(client)
    st = u.wait_job(client, body["job_id"])
    assert len(st["tail"]) <= 2000, len(st["tail"])
    assert st["tail"].startswith("x"), st["tail"][:50]
