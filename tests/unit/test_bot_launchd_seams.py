# -*- coding: utf-8 -*-
"""受理时定下接缝的白盒单测（INTERFACE-新系统bot运维 §1a、§5b 第 2 条，第 6 稿追加）。

重启全部、单只重启、开关同步都是先回响应、后台线程接着做。后台那一段要用的 launchctl
路径、plist 目录、新系统家目录、执行范围与方向，必须在受理那一刻（构造 runner 的工厂里）
定下，之后不许再读环境变量与配置文件。这里直接调 `bot_launchd`，量三件事：

- 解析对象 `Seams` 装的是什么、之后会不会跟着环境变；
- 三个取路径的函数与下游的动作、判活函数，给了对象就只用对象；
- 工厂返回的执行体跑起来时，方向与范围是受理时的，环境一个相关变量都没读。

「受理之后环境被改」一律改成另一支假 launchctl、另一套临时目录，不删变量。
假 launchctl 是 tmp 里的 sh 脚本，只记录参数；bot 名全是假名。
"""
import contextlib
import json
import os
import time
from types import SimpleNamespace

import pytest

from moments import bot_launchd as bl

pytestmark = pytest.mark.skipif(os.name == "nt", reason="假 launchctl 是 sh 脚本")

#: §1a 点名的那几个：后台那一段原先会读到的环境变量，加上覆盖命令的命令串
WATCHED = {"HUB_LAUNCHCTL", "HUB_LAUNCHAGENTS_DIR", "DSH_BOT_HOME", "HUB_CONFIGS_DSH_DIR",
           "HUB_CONFIGS_DIR", "HUB_BOTS_FILE", "HOME", "HUB_RESTART_CMD"}


def _fake_launchctl(dirpath, name, kickstart_missing=False):
    """造一支假 launchctl：每次调用把参数记一行；print 报 running / pid 4242。

    `kickstart_missing` 时 kickstart 照真机的样子报找不到服务（触发回落 bootstrap）。
    """
    dirpath.mkdir(parents=True, exist_ok=True)
    log = dirpath / (name + ".calls")
    kick = "echo 'Could not find service' >&2; exit 113" if kickstart_missing else "exit 0"
    path = dirpath / name
    path.write_text(
        "#!/bin/sh\n"
        "echo \"$@\" >> '%s'\n"
        "case \"$1\" in\n"
        "  print) printf '\\tstate = running\\n\\tpid = 4242\\n'; exit 0;;\n"
        "  kickstart) %s;;\n"
        "  *) exit 0;;\n"
        "esac\n" % (log, kick), encoding="utf-8")
    path.chmod(0o755)

    def calls():
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    return SimpleNamespace(path=str(path), calls=calls,
                           verbs=lambda: [c.split()[0] for c in calls()])


class _SpyEnv(dict):
    """`os.environ` 的替身：值照旧，读过哪些键记下来。"""

    def __init__(self, base):
        super().__init__(base)
        self.read = set()

    def get(self, key, default=None):
        self.read.add(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.read.add(key)
        return super().__getitem__(key)

    def __contains__(self, key):
        self.read.add(key)
        return super().__contains__(key)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """两套互不相干的环境。开始时指着第一套（a / h1 / agents1 / dsh / legacy）；
    `swap()` 把 §1a 场景甲第 3 步那几个变量一次改到第二套。"""
    a = _fake_launchctl(tmp_path / "fakes", "launchctl-a")
    b = _fake_launchctl(tmp_path / "fakes", "launchctl-b")
    names = ("h1", "h2", "agents1", "agents2", "dsh", "dsh2", "legacy", "legacy2", "home", "home2")
    d = {n: tmp_path / n for n in names}
    for p in d.values():
        p.mkdir()
    monkeypatch.setenv("HOME", str(d["home"]))
    monkeypatch.setenv("HUB_LAUNCHCTL", a.path)
    monkeypatch.setenv("HUB_LAUNCHAGENTS_DIR", str(d["agents1"]))
    monkeypatch.setenv("DSH_BOT_HOME", str(d["h1"]))
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(d["dsh"]))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(d["legacy"]))
    monkeypatch.delenv("HUB_BOTS_FILE", raising=False)
    monkeypatch.delenv("HUB_RESTART_CMD", raising=False)
    monkeypatch.setattr(bl, "POLL_INTERVAL", 0.02)
    monkeypatch.setattr(bl, "RESTART_WAIT", 2.0)    # 哪天接缝漏传了，红用例别陪着等满 45 秒

    def swap():
        monkeypatch.setenv("HUB_LAUNCHCTL", b.path)
        monkeypatch.setenv("DSH_BOT_HOME", str(d["h2"]))
        monkeypatch.setenv("HUB_LAUNCHAGENTS_DIR", str(d["agents2"]))
        monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(d["dsh2"]))
        monkeypatch.setenv("HUB_CONFIGS_DIR", str(d["legacy2"]))
        monkeypatch.setenv("HOME", str(d["home2"]))

    def bot(root, bot_id, enabled=None, **fields):
        text = "id: %s\n" % bot_id
        if enabled is not None:
            text += "enabled: %s\n" % ("true" if enabled else "false")
        for key in sorted(fields):
            text += "%s: %s\n" % (key, fields[key])
        (root / ("%s.yml" % bot_id)).write_text(text, encoding="utf-8")

    def beat(home, bot_id, pid=4242):
        """写一份「稍后才到期」的心跳：at 取 5 秒后，晚于任何动作的开始时刻，又在新鲜阈值内。"""
        p = home / "bots" / bot_id / "state" / "heartbeat"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"at": int(time.time() * 1000) + 5000, "pid": pid}),
                     encoding="utf-8")

    def plist(agents, bot_id):
        p = agents / ("com.dsh-bot.%s.plist" % bot_id)
        p.write_text("<plist/>", encoding="utf-8")
        return p

    @contextlib.contextmanager
    def spy():
        """窗口内把 `os.environ` 换成记账的替身，交出「读过的键」那个集合。"""
        env = _SpyEnv(os.environ)
        with monkeypatch.context() as m:
            m.setattr(os, "environ", env)
            yield env.read

    return SimpleNamespace(a=a, b=b, swap=swap, bot=bot, beat=beat, plist=plist, spy=spy,
                           tmp=tmp_path, **d)


# ------------------------------------------------------------ 探针自己得灵

def test_探针_不带对象的取路径函数确实会被它记到(world):
    with world.spy() as read:
        bl.launchctl_path()
        bl.launchagents_dir()
        bl.bot_home("xbot")
    assert {"HUB_LAUNCHCTL", "HUB_LAUNCHAGENTS_DIR", "DSH_BOT_HOME"} <= read


# ------------------------------------------------------------ 解析对象

def test_解析_三条路径取自那一刻的环境(world):
    assert bl.resolve_seams() == bl.Seams(world.a.path, str(world.agents1), world.h1)


def test_解析_两个目录没设时跟HOME走_波浪号已经展开(world, monkeypatch):
    monkeypatch.delenv("HUB_LAUNCHAGENTS_DIR")
    monkeypatch.delenv("DSH_BOT_HOME")
    s = bl.resolve_seams()
    assert s.agents_dir == str(world.home / "Library" / "LaunchAgents")
    assert s.home == world.home / ".dsh-bot"
    assert "~" not in s.agents_dir and "~" not in str(s.home)


def test_解析_相对路径落成绝对路径(world, monkeypatch):
    monkeypatch.chdir(world.tmp)
    monkeypatch.setenv("HUB_LAUNCHAGENTS_DIR", "rel-agents")
    monkeypatch.setenv("DSH_BOT_HOME", "rel-home")
    s = bl.resolve_seams()
    assert os.path.isabs(s.agents_dir) and s.home.is_absolute()
    assert os.path.realpath(s.agents_dir) == os.path.realpath(str(world.tmp / "rel-agents"))
    assert os.path.realpath(str(s.home)) == os.path.realpath(str(world.tmp / "rel-home"))


def test_解析_之后环境再变_对象里的值不变_也改不动(world):
    s = bl.resolve_seams()
    world.swap()
    assert s == bl.Seams(world.a.path, str(world.agents1), world.h1)
    with pytest.raises(AttributeError):
        s.launchctl = world.b.path


# ------------------------------------------------------------ 三个取路径的函数

def test_取路径_给了对象就不看环境_没给才现读(world):
    s = bl.resolve_seams()
    world.swap()
    assert (bl.launchctl_path(s), bl.launchctl_path()) == (world.a.path, world.b.path)
    assert (bl.launchagents_dir(s), bl.launchagents_dir()) == (str(world.agents1),
                                                               str(world.agents2))
    assert bl.bot_home("xbot", s) == world.h1 / "bots" / "xbot"
    assert bl.bot_home("xbot") == world.h2 / "bots" / "xbot"
    assert bl.log_path("xbot", s) == str(world.h1 / "bots" / "xbot" / "logs" / "launchd.log")


@pytest.mark.parametrize("bad", ["Xbot", "a", "_x", "1x", "有中文", "../x", "a/b", "x-1",
                                 "x" * 33, "", None, 7])
def test_取路径_带对象时bot_id形状那道闸照旧(world, bad):
    s = bl.resolve_seams()
    assert bl.bot_home(bad, s) is None
    assert bl.log_path(bad, s) == ""
    assert bl.read_heartbeat(bad, s) is None


# ------------------------------------------------------------ 下游：只用传下来的对象

def test_动作与print_带对象时全打到受理时那支(world):
    s = bl.resolve_seams()
    world.swap()
    assert bl.kickstart("xbot", s)[0] == "ok"
    assert bl.bootout("xbot", s)[0] == "ok"
    assert bl.print_state("xbot", s) == ("running", 4242)
    assert world.a.verbs() == ["kickstart", "bootout", "print"]
    assert world.b.calls() == []


def test_回落bootstrap_plist取自受理时的目录(world):
    world.plist(world.agents1, "xbot")
    world.plist(world.agents2, "xbot")              # 换上的目录里也有一份，不该被用上
    s = bl.resolve_seams()
    world.swap()
    assert bl.bootstrap("xbot", s)[0] == "ok"
    call = world.a.calls()[-1]
    assert call.startswith("bootstrap ")
    assert str(world.agents1 / "com.dsh-bot.xbot.plist") in call
    assert str(world.agents2) not in call
    assert world.b.calls() == []


def test_回落bootstrap_受理时的目录里没有plist就不动手(world):
    world.plist(world.agents2, "xbot")              # 只有换上的目录里有
    s = bl.resolve_seams()
    world.swap()
    assert bl.bootstrap("xbot", s) == ("no_plist", "")
    assert world.a.calls() == [] and world.b.calls() == []


def test_心跳_带对象时读受理时的家目录(world):
    s = bl.resolve_seams()
    world.swap()
    world.beat(world.h2, "xbot", pid=22)            # 只写在换上的家目录
    assert bl.read_heartbeat("xbot", s) is None
    assert bl.fresh_heartbeat("xbot", seams=s) is None
    assert bl.read_heartbeat("xbot")["pid"] == 22   # 不带对象：现读，读到的是换上的那个
    world.beat(world.h1, "xbot", pid=11)
    assert bl.read_heartbeat("xbot", s)["pid"] == 11
    assert bl.fresh_heartbeat("xbot", seams=s)["pid"] == 11
    assert bl.row_state("xbot", s) == ("online", 11)
    assert world.a.calls() == [] and world.b.calls() == []


def test_状态列_心跳不新鲜时print打到受理时那支(world):
    s = bl.resolve_seams()
    world.swap()
    assert bl.row_state("xbot", s) == ("starting", 4242)
    assert world.a.verbs() == ["print"] and world.b.calls() == []


def test_判活_带对象时心跳与print都用受理时的(world):
    s = bl.resolve_seams()
    world.swap()
    world.beat(world.h2, "xbot")
    assert bl.alive_now("xbot", None, s)[:2] == (False, "没有新心跳")
    ok, reason, _pid, _age = bl.wait_alive("xbot", None, 0.1, s)
    assert (ok, reason) == (False, "0.1 秒内没有新心跳")
    world.beat(world.h1, "xbot")
    assert bl.alive_now("xbot", None, s)[:3] == (True, None, 4242)
    assert bl.wait_alive("xbot", None, 0.1, s)[:3] == (True, None, 4242)
    assert world.b.calls() == []


def test_失败行的日志路径在受理时的家目录下(world):
    s = bl.resolve_seams()
    world.swap()
    line = bl._fail_line("xbot", "没起来", s)
    assert str(world.h1 / "bots" / "xbot" / "logs" / "launchd.log") in line
    assert str(world.h2) not in line


# ------------------------------------------------------------ 单只执行体

def test_sync_one_给了方向与对象_不读配置也不读环境(world, monkeypatch):
    def boom(_bot_id):
        raise AssertionError("给了方向还去读配置")

    world.beat(world.h1, "xbot")
    s = bl.resolve_seams()
    world.swap()
    monkeypatch.setattr(bl, "enabled_of", boom)
    with world.spy() as read:
        state, code, tail = bl.sync_one("xbot", True, s)
    assert (state, code) == ("done", None), tail
    assert tail.splitlines()[-1].startswith("xbot 已重启（pid 4242")
    assert world.a.verbs() == ["kickstart", "print"] and world.b.calls() == []
    assert read & WATCHED == set(), read & WATCHED


def test_sync_one_停用方向_只发两条bootout_主单元与定时单元(world):
    """第 8 稿 §5c：停用方向由一条 bootout 变成两条（主单元、主动消息的定时单元）。"""
    s = bl.resolve_seams()
    world.swap()
    with world.spy() as read:
        assert bl.sync_one("xbot", False, s) == ("done", None, "重启 1 只：成功 1\nxbot 已停止")
    uid = os.getuid()
    assert world.a.calls() == ["bootout gui/%d/com.dsh-bot.xbot" % uid,
                               "bootout gui/%d/com.dsh-bot.self-initiate.xbot" % uid]
    assert world.b.calls() == []
    assert read & WATCHED == set(), read & WATCHED


def test_sync_one_方向缺省时现读配置(world):
    world.bot(world.dsh, "offbot", enabled=False)
    assert bl.sync_one("offbot")[0] == "done"
    assert world.a.verbs() == ["bootout", "bootout"]


# ------------------------------------------------------------ one_runner：受理时读一次方向

def test_one_runner_受理后改配置改环境_仍按受理时的方向与接缝做完(world):
    world.bot(world.dsh, "xbot")                    # 受理时：启用
    world.beat(world.h1, "xbot")
    runner = bl.one_runner("xbot")
    world.bot(world.dsh, "xbot", enabled=False)     # 受理之后：文件改成停用
    world.swap()
    with world.spy() as read:
        state, code, tail = runner()
    assert (state, code) == ("done", None), tail
    assert "xbot 已重启" in tail
    assert world.a.verbs() == ["kickstart", "print"] and world.b.calls() == []
    assert read & WATCHED == set(), read & WATCHED


def test_one_runner_受理时是停用_之后改成启用_仍走bootout(world):
    world.bot(world.dsh, "xbot", enabled=False)
    runner = bl.one_runner("xbot")
    world.bot(world.dsh, "xbot", enabled=True)
    world.swap()
    assert runner() == ("done", None, "重启 1 只：成功 1\nxbot 已停止")
    assert world.a.verbs() == ["bootout", "bootout"] and world.b.calls() == []


def test_one_runner_同名且生效的是旧系统那份_方向是停用(world):
    """开关把新系统那份停掉之后的局面：对新系统的单元 bootout，不许 kickstart。"""
    world.bot(world.legacy, "twinbot", enabled=True)
    world.bot(world.dsh, "twinbot", enabled=False)
    assert bl.one_runner("twinbot")()[2].splitlines()[-1] == "twinbot 已停止"
    uid = os.getuid()
    assert world.a.calls() == ["bootout gui/%d/com.dsh-bot.twinbot" % uid,
                               "bootout gui/%d/com.dsh-bot.self-initiate.twinbot" % uid]


def test_one_runner_调用方给了方向就不读配置(world, monkeypatch):
    def boom(_bot_id):
        raise AssertionError("给了方向还去读配置")

    monkeypatch.setattr(bl, "enabled_of", boom)
    world.beat(world.h1, "xbot")
    assert bl.one_runner("xbot", True)()[0] == "done"
    assert world.a.verbs() == ["kickstart", "print"]


def test_one_runner_读方向时抛了_照样给执行体_直接failed且tail只有类名(world, monkeypatch):
    def boom(_bot_id):
        raise RuntimeError("读不出 /secret/path/xbot.yml")

    monkeypatch.setattr(bl, "enabled_of", boom)
    assert bl.one_runner("xbot")() == ("failed", None, "RuntimeError")
    assert world.a.calls() == []


def test_one_runner_判活失败_日志路径仍在受理时的家目录(world, monkeypatch):
    monkeypatch.setattr(bl, "RESTART_WAIT", 0.2)
    world.bot(world.dsh, "xbot")
    runner = bl.one_runner("xbot")
    world.swap()
    world.beat(world.h2, "xbot")                    # 心跳只写在换上的家目录
    state, _code, tail = runner()
    assert state == "failed", tail
    assert "0.2 秒内没有新心跳" in tail
    assert str(world.h1 / "bots" / "xbot" / "logs" / "launchd.log") in tail
    assert str(world.h2) not in tail
    assert world.b.calls() == []


# ------------------------------------------------------------ 重启全部：范围与方向受理时算好

def test_restart_pairs_范围只留过放行闸的_方向逐只读(world):
    world.bot(world.dsh, "onbot")
    world.bot(world.dsh, "offbot", enabled=False)
    world.bot(world.legacy, "oldbot")                       # 旧系统的：不在范围里
    world.bot(world.legacy, "twinbot", enabled=True)        # 同名，生效的是旧系统那份：不在
    world.bot(world.dsh, "twinbot", enabled=False)
    world.bot(world.legacy, "bothoff", enabled=False)       # 同名，两份都停：生效的是新系统那份
    world.bot(world.dsh, "bothoff", enabled=False)
    assert sorted(bl.restart_pairs()) == [("bothoff", False), ("offbot", False), ("onbot", True)]


def test_restart_pairs_名单注入时为空(world, monkeypatch):
    world.bot(world.dsh, "onbot")
    p = world.tmp / "bots.json"
    p.write_text('[{"id": "onbot"}]', encoding="utf-8")
    monkeypatch.setenv("HUB_BOTS_FILE", str(p))
    assert bl.restart_pairs() == []


def test_all_runner_受理后改配置改环境_仍按受理时的范围与方向做完(world):
    world.bot(world.dsh, "onbot")
    world.bot(world.dsh, "offbot", enabled=False)
    world.beat(world.h1, "onbot")
    runner = bl.all_runner()
    world.bot(world.dsh, "onbot", enabled=False)    # 受理之后：两只对调，再多出一只
    world.bot(world.dsh, "offbot", enabled=True)
    world.bot(world.dsh, "latebot")
    world.swap()
    with world.spy() as read:
        state, code, tail = runner()
    assert (state, code) == ("done", None), tail
    assert tail.splitlines()[0] == "重启 2 只：成功 2"
    uid = os.getuid()
    calls = world.a.calls()
    assert "kickstart -k gui/%d/com.dsh-bot.onbot" % uid in calls
    assert "bootout gui/%d/com.dsh-bot.offbot" % uid in calls
    assert "bootout gui/%d/com.dsh-bot.self-initiate.offbot" % uid in calls   # 第 8 稿：定时单元也停
    assert not any("latebot" in c for c in calls), calls
    assert len([c for c in calls if c.startswith(("kickstart", "bootout"))]) == 3, calls
    assert world.b.calls() == []
    assert read & WATCHED == set(), read & WATCHED


def test_all_runner_范围为空_done且一条launchctl都不发(world):
    world.bot(world.legacy, "oldbot")
    assert bl.all_runner()() == ("done", None, "没有可重启的 bot")
    assert world.a.calls() == []


def test_all_runner_算范围时抛了_照样给执行体_直接failed且tail只有类名(world, monkeypatch):
    p = world.tmp / "bots.json"
    p.write_text("{}", encoding="utf-8")            # 顶层不是数组：读名单这一步就抛
    monkeypatch.setenv("HUB_BOTS_FILE", str(p))
    assert bl.all_runner()() == ("failed", None, "TypeError")
    assert world.a.calls() == []


def test_restart_group_并行等活的子线程也只用受理时的接缝(world):
    world.beat(world.h1, "abot")
    world.beat(world.h1, "bbot")
    s = bl.resolve_seams()
    world.swap()
    with world.spy() as read:
        state, _code, tail = bl.restart_group([("abot", True), ("bbot", True)], s)
    assert state == "done", tail
    prints = [c for c in world.a.calls() if c.startswith("print ")]
    assert {c.rsplit(".", 1)[-1] for c in prints} == {"abot", "bbot"}
    assert world.b.calls() == []
    assert read & WATCHED == set(), read & WATCHED


def test_restart_group_回落bootstrap与失败行都用受理时的目录(world, monkeypatch):
    """kickstart 找不到服务 → 回落 bootstrap：abot 的 plist 在受理时的目录里，起得来；
    bbot 的只在换上的目录里，按「单元没装」失败，日志路径在受理时的家目录下。"""
    missing = _fake_launchctl(world.tmp / "fakes", "launchctl-missing", kickstart_missing=True)
    monkeypatch.setenv("HUB_LAUNCHCTL", missing.path)
    world.plist(world.agents1, "abot")
    world.plist(world.agents2, "bbot")
    world.beat(world.h1, "abot")
    s = bl.resolve_seams()
    world.swap()
    state, _code, tail = bl.restart_group([("abot", True), ("bbot", True)], s)
    assert state == "failed", tail
    lines = tail.splitlines()
    assert lines[0] == "重启 2 只：成功 1，失败 1"
    assert lines[1].startswith("abot 已重启")
    assert lines[2] == "bbot 失败：单元没装（plist 不存在）；日志 %s" % (
        world.h1 / "bots" / "bbot" / "logs" / "launchd.log")
    boots = [c for c in missing.calls() if c.startswith("bootstrap ")]
    assert boots == ["bootstrap gui/%d %s" % (os.getuid(),
                                              world.agents1 / "com.dsh-bot.abot.plist")]
    assert world.b.calls() == []


# ------------------------------------------------------------ 列表的 dsh 行判活（§2 / §1a 末段）

def _swap_when_probe_starts(world, monkeypatch):
    """让每个 dsh 探活线程一开跑就先把环境换掉，再去判活。

    列表接口有 2 秒预算，超出预算的探活线程会在响应之后才收尾，那时环境可能已经变了。
    这里把「环境变了」提前到线程开跑的那一刻，量它用的是不是请求线程里解析的那一份。
    """
    from moments import bots_client as bc
    real = bc._dsh_probe

    def late(bot_id, *rest):
        world.swap()
        return real(bot_id, *rest)

    monkeypatch.setattr(bc, "_dsh_probe", late)
    return bc


def test_列表探活_心跳读的是请求线程里解析的家目录(world, monkeypatch):
    world.bot(world.dsh, "xbot", dispatcher_port=17951)
    world.beat(world.h1, "xbot", pid=11)
    world.beat(world.h2, "xbot", pid=22)
    bc = _swap_when_probe_starts(world, monkeypatch)
    rows = bc.probe_bots(bc.list_bots(), bc.bot_sources())
    assert [(r["id"], r["state"], r["pid"]) for r in rows] == [("xbot", "online", 11)]
    assert world.a.calls() == [] and world.b.calls() == []


def test_列表探活_print打到请求线程里解析的那支(world, monkeypatch):
    world.bot(world.dsh, "xbot", dispatcher_port=17951)
    bc = _swap_when_probe_starts(world, monkeypatch)
    rows = bc.probe_bots(bc.list_bots(), bc.bot_sources())
    assert [(r["state"], r["pid"]) for r in rows] == [("starting", 4242)]
    assert world.a.verbs() == ["print"] and world.b.calls() == []


def test_列表探活_几只dsh行共用一次解析_没有dsh行就不解析(world, monkeypatch):
    from moments import bots_client as bc
    real, seen = bl.resolve_seams, []

    def counting():
        seen.append(1)
        return real()

    monkeypatch.setattr(bl, "resolve_seams", counting)
    world.bot(world.dsh, "abot", dispatcher_port=17951)
    world.bot(world.dsh, "bbot", dispatcher_port=17952)
    world.beat(world.h1, "abot")
    world.beat(world.h1, "bbot")
    rows = bc.probe_bots(bc.list_bots(), bc.bot_sources())
    assert [r["state"] for r in rows] == ["online", "online"]
    assert len(seen) == 1
    del seen[:]
    monkeypatch.setattr(bc, "_probe_one", lambda port: None)        # 其余行：不真发本机探活
    assert bc.probe_bots([{"id": "plainbot", "display_name": "plainbot"}], {})[0]["state"] in (
        "offline", "unknown")
    assert seen == []
