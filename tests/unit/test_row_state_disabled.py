# -*- coding: utf-8 -*-
"""停用的行不凭心跳说它在线的白盒单测（INTERFACE-新系统bot运维 §2 状态规则第 2 步，第 8 稿）。

刚停掉的 bot 心跳还没过期，列表里会顶着「已停用」再显示最长 15 秒的在线。第 8 稿：停用的
行先问一次 `launchctl print`，它说没在运行就直接离线；它说在运行才接着按心跳往下判。
启用的行不变，心跳新鲜时一次 launchctl 都不调。

直接调 `bot_launchd.row_state` 与 `bots_client.probe_bots`。假 launchctl 是 tmp 里的 sh
脚本：只回答 print（答什么由一个文件决定），每次调用记一行；别的子命令一律以 97 拒绝。
bot 名全是假名。
"""
import json
import os
import time
from types import SimpleNamespace

import pytest

from moments import bot_launchd as bl
from moments import bots_client

pytestmark = pytest.mark.skipif(os.name == "nt", reason="假 launchctl 是 sh 脚本")

BOT = "rowbot"
RUNNING = "\tstate = running\n\tpid = 5555\n"
NOT_RUNNING = "\tstate = not running\n"
RUNNING_NO_PID = "\tstate = running\n"


@pytest.fixture(scope="module")
def fakes(tmp_path_factory):
    """假 launchctl 与假 bun，整个模块只造一次（新建的可执行文件头一回被执行，系统会多查
    几百毫秒，每条用例各造一份就白等几十次）。记录文件与 print 的回答由环境变量指到各用例
    自己的 tmp 目录。返回 (假 launchctl, 假 bun) 的路径。"""
    d = tmp_path_factory.mktemp("row-fakes")
    exe, bun = d / "launchctl", d / "bun"
    exe.write_text(
        "#!/bin/sh\n"
        "echo \"$@\" >> \"$FAKE_LAUNCHCTL_LOG\"\n"
        "[ \"$1\" = print ] || exit 97\n"
        "[ -f \"$FAKE_LAUNCHCTL_PRINT\" ] || { echo 'Could not find service' >&2; exit 113; }\n"
        "cat \"$FAKE_LAUNCHCTL_PRINT\"\n", encoding="utf-8")
    # 假 bun：谁调它都拒绝并留痕（本文件没有该调它的路径）
    bun.write_text("#!/bin/sh\necho \"$@\" >> \"$FAKE_BUN_LOG\"\nexit 97\n", encoding="utf-8")
    exe.chmod(0o755)
    bun.chmod(0o755)
    return str(exe), str(bun)


@pytest.fixture
def w(tmp_path, monkeypatch, fakes):
    fake_dir = tmp_path / "fake"
    fake_dir.mkdir()
    answer, log = fake_dir / "print.txt", fake_dir / "calls.log"
    monkeypatch.setenv("FAKE_LAUNCHCTL_LOG", str(log))
    monkeypatch.setenv("FAKE_LAUNCHCTL_PRINT", str(answer))
    monkeypatch.setenv("FAKE_BUN_LOG", str(fake_dir / "bun.log"))
    monkeypatch.setenv("HUB_BUN", fakes[1])
    home, dsh, legacy = tmp_path / "dsh-home", tmp_path / "configs-dsh", tmp_path / "configs-legacy"
    for d in (home, dsh, legacy):
        d.mkdir()
    monkeypatch.setenv("HUB_LAUNCHCTL", fakes[0])
    monkeypatch.setenv("DSH_BOT_HOME", str(home))
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(dsh))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(legacy))
    monkeypatch.setenv("HUB_LAUNCHAGENTS_DIR", str(tmp_path / "agents"))
    monkeypatch.delenv("HUB_BOTS_FILE", raising=False)
    for k in list(os.environ):
        if k.startswith("DISPATCHER_PORT_"):
            monkeypatch.delenv(k, raising=False)

    def says(text):
        """launchd 对 print 的回答；None = 找不到这个服务。"""
        if text is None:
            if answer.exists():
                answer.unlink()
        else:
            answer.write_text(text, encoding="utf-8")

    def beat(bot=BOT, pid=4242, age_ms=0):
        p = home / "bots" / bot / "state" / "heartbeat"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"at": int(time.time() * 1000) - age_ms, "pid": pid}), encoding="utf-8")

    def bot(bot_id=BOT, enabled=None, port=17991):
        text = "id: %s\ndispatcher_port: %d\n" % (bot_id, port)
        if enabled is not None:
            text += "enabled: %s\n" % ("true" if enabled else "false")
        (dsh / ("%s.yml" % bot_id)).write_text(text, encoding="utf-8")

    def calls():
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    yield SimpleNamespace(says=says, beat=beat, bot=bot, calls=calls, home=home, dsh=dsh)
    assert not (fake_dir / "bun.log").exists(), "列表与判活的路径上不该有人调 bun"


# ============================================================ row_state：停用的行

def test_停用_心跳新鲜_launchd说找不到服务_离线(w):
    w.beat()
    w.says(None)
    assert bl.row_state(BOT, disabled=True) == ("offline", None)


def test_停用_心跳新鲜_launchd说装着但没在跑_离线(w):
    w.beat()
    w.says(NOT_RUNNING)
    assert bl.row_state(BOT, disabled=True) == ("offline", None)


def test_停用_心跳新鲜_launchd说在运行_在线且pid取心跳的(w):
    """配置说停、进程却还在：如实显示。"""
    w.beat(pid=4242)
    w.says(RUNNING)
    assert bl.row_state(BOT, disabled=True) == ("online", 4242)


def test_停用_心跳过期_launchd说在运行_启动中且pid取launchd的(w):
    w.beat(age_ms=60_000)
    w.says(RUNNING)
    assert bl.row_state(BOT, disabled=True) == ("starting", 5555)


def test_停用_没有心跳_launchd说在运行却给不出pid_离线(w):
    w.says(RUNNING_NO_PID)
    assert bl.row_state(BOT, disabled=True) == ("offline", None)


@pytest.mark.parametrize("answer", [None, NOT_RUNNING, RUNNING, RUNNING_NO_PID],
                         ids=["missing", "not-running", "running", "running-no-pid"])
@pytest.mark.parametrize("fresh", [True, False], ids=["fresh", "stale"])
def test_停用的行_不管落到哪一档_恰好只问一次print(w, answer, fresh):
    """第 2 步问到的结果往下复用：落到第 4 步时不问第二遍。"""
    w.beat(age_ms=0 if fresh else 60_000)
    w.says(answer)
    bl.row_state(BOT, disabled=True)
    assert w.calls() == ["print gui/%d/com.dsh-bot.%s" % (os.getuid(), BOT)]


def test_停用的行_只发只读的print_问的是这只的主单元(w):
    w.beat()
    w.says(RUNNING)
    bl.row_state(BOT, disabled=True)
    assert all(c.startswith("print ") and c.endswith("/com.dsh-bot." + BOT) for c in w.calls()), w.calls()


@pytest.mark.parametrize("bad", ["jiwen", "Bad-Name", "x", "rowbot\n", ""])
def test_停用且id不合格_不拼单元名_零调用_离线(w, bad):
    """保留名与形状非法的 id 拿不到单元名：只读的 print 也不发（§5b 第 5 条）。"""
    assert bl.row_state(bad, disabled=True) == ("offline", None)
    assert w.calls() == []


# ============================================================ row_state：启用的行与第 7 稿相同

def test_启用_心跳新鲜_在线_一次launchctl都不调(w):
    w.beat(pid=4242)
    w.says(None)                         # launchd 说什么都不该被问到
    assert bl.row_state(BOT) == ("online", 4242)
    assert bl.row_state(BOT, disabled=False) == ("online", 4242)
    assert w.calls() == []


def test_启用_心跳过期_launchd说在运行_启动中_只问一次(w):
    w.beat(age_ms=60_000)
    w.says(RUNNING)
    assert bl.row_state(BOT) == ("starting", 5555)
    assert len(w.calls()) == 1


def test_启用_没有心跳_launchd说找不到_离线(w):
    w.says(None)
    assert bl.row_state(BOT) == ("offline", None)


def test_接缝照旧只用受理时那份_停用的行也一样(w, tmp_path, monkeypatch):
    seams = bl.resolve_seams()
    w.beat()
    w.says(RUNNING)
    monkeypatch.setenv("HUB_LAUNCHCTL", str(tmp_path / "no-such-launchctl"))
    monkeypatch.setenv("DSH_BOT_HOME", str(tmp_path / "other-home"))
    assert bl.row_state(BOT, seams, True) == ("online", 4242)


# ============================================================ probe_bots：把「停用的」交到判定手里

def _row(rows, bot_id=BOT):
    return [r for r in rows if r["id"] == bot_id][0]


def _entries(*ids):
    return [{"id": i, "display_name": i} for i in ids]


def test_探活_传进来的停用集合里有它_刚停掉的显示离线(w):
    w.bot(enabled=False)
    w.beat()
    w.says(None)
    row = _row(bots_client.probe_bots(_entries(BOT), disabled={BOT}))
    assert (row["state"], row["pid"]) == ("offline", None)


def test_探活_没传停用集合_自己按同一个读法现读(w):
    w.bot(enabled=False)
    w.beat()
    w.says(None)
    assert _row(bots_client.probe_bots(_entries(BOT)))["state"] == "offline"


def test_探活_没传停用集合_自己那份yml没写enabled的算启用_不问launchd(w):
    w.bot()
    w.beat(pid=4242)
    w.says(None)
    row = _row(bots_client.probe_bots(_entries(BOT)))
    assert (row["state"], row["pid"]) == ("online", 4242) and w.calls() == []


def test_探活_传的集合说了算_不再另读配置(w, monkeypatch):
    w.bot(enabled=False)
    w.beat(pid=4242)
    w.says(None)
    monkeypatch.setattr(bl, "enabled_of", lambda b: pytest.fail("给了停用集合还去读配置"))
    assert _row(bots_client.probe_bots(_entries(BOT), disabled=set()))["state"] == "online"
    assert w.calls() == []


def test_探活_一只启用一只停用_各按各的判_只对停用那只发print(w):
    w.bot("onbot", port=17992)
    w.bot("offbot", enabled=False, port=17993)
    w.beat("onbot", pid=1)
    w.beat("offbot", pid=2)
    w.says(None)
    rows = bots_client.probe_bots(_entries("onbot", "offbot"))
    assert (_row(rows, "onbot")["state"], _row(rows, "offbot")["state"]) == ("online", "offline")
    assert [c.rsplit("/", 1)[1] for c in w.calls()] == ["com.dsh-bot.offbot"]


def test_探活_停用集合里的旧系统行不受影响_照旧发本机探活不问launchd(w, monkeypatch):
    """集合只对来源是新系统的行起作用。"""
    legacy = os.environ["HUB_CONFIGS_DIR"]
    with open(os.path.join(legacy, "oldbot.yml"), "w", encoding="utf-8") as f:
        f.write("id: oldbot\ndispatcher_port: 17994\nenabled: false\n")
    probed = []
    monkeypatch.setattr(bots_client, "_probe_one", lambda port: probed.append(port))
    rows = bots_client.probe_bots(_entries("oldbot"), disabled={"oldbot"})
    assert _row(rows, "oldbot")["state"] == "offline" and probed == [17994] and w.calls() == []


def test_探活_现读停用的那一步读不出来_不抛_按都启用探(w, monkeypatch):
    w.bot(enabled=False)
    w.beat(pid=4242)
    monkeypatch.setattr(bl, "enabled_of", lambda b: (_ for _ in ()).throw(OSError("boom")))
    assert _row(bots_client.probe_bots(_entries(BOT)))["state"] == "online"


def test_探活_停用行的print超出预算_按离线_列表不被拖住(w, monkeypatch):
    w.bot(enabled=False)
    w.beat()
    w.says(RUNNING)
    monkeypatch.setattr(bots_client, "PROBE_BUDGET", 0.2)
    real = bl.print_state
    monkeypatch.setattr(bl, "print_state", lambda *a, **k: (time.sleep(0.8), real(*a, **k))[1])
    t0 = time.time()
    row = _row(bots_client.probe_bots(_entries(BOT), disabled={BOT}))
    assert row["state"] == "offline" and time.time() - t0 < 0.7
    time.sleep(0.8)                      # 等那条被拖慢的探活线程自己收尾，别拖进下一条用例
