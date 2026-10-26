# -*- coding: utf-8 -*-
"""停用时收起启动项文件、启用时放回的白盒单测（INTERFACE-新系统bot运维 §5c、§5b 第 5 条，第 8 稿）。

停用以前只做 bootout，plist 还留在目录里；网关那份带着「登录即启动」，注销重登之后 launchd
会把它重新装载并拉起来。第 8 稿：停用时 bootout 之后把 plist 在同一个目录里改名（原名后加
`.disabled`），启用时先改回来再拉起；主动消息的定时单元一并处理。

验收那层量的是接口上看得见的结果。这里量的是它保守在哪：
- 只动 plist 目录里这只 bot 的两个名字，别的文件一个不碰；
- 改名是原子的重命名（同一个 inode），不复制、不删除；
- 文件名与单元名同一个把关入口，不合格的 id（含保留名）拿不到名字，不调 launchctl、不动文件；
- 任何一步没成，这只都按失败报，原因是人话。

隔离：plist 目录是每条用例自己的 tmp_path 下的假目录；假 launchctl 与假 bun 都是 tmp 里的 sh
脚本（只记参数、按规则回退出码）。任何改名都只发生在 tmp_path 里，夹具收尾时再核一遍。
bot 名全是假名；保留名只出现在两个假件都在位的用例里。
"""
import os
import subprocess
from types import SimpleNamespace

import pytest

from moments import bot_launchd as bl

pytestmark = pytest.mark.skipif(os.name == "nt", reason="假 launchctl 是 sh 脚本，改名失败靠 POSIX 权限")

BOT = "stashbot"
MAIN = "com.dsh-bot.%s" % BOT
TIMER = "com.dsh-bot.self-initiate.%s" % BOT
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
needs_perm = pytest.mark.skipif(IS_ROOT, reason="root 下目录权限拦不住改名")
#: 目录列出来是按名字排的（`self-initiate` 排在 bot 名前面还是后面看 bot 叫什么），比的时候两边都排
LIVE = sorted([MAIN + ".plist", TIMER + ".plist"])
STASHED = sorted([MAIN + ".plist.disabled", TIMER + ".plist.disabled"])
#: 造超时用：单次 launchctl 上限收到这么短，被拖慢的那条睡得比它久得多
SHORT = 0.5
TIMED_OUT = "launchctl 超时（0.5 秒）"

FAKE_LAUNCHCTL = """#!/bin/sh
# 假 launchctl：记一行参数；规则目录里 <子命令>.<单元名> 写着退出码就按它回（同名 .err 是报错
# 原文，同名 .sleep 是先睡几秒）；没有规则时一律成功，print 报 running / pid 4242。
# 记录文件与规则目录由环境变量给：脚本整个模块只造一次，每条用例各用各的记录与规则。
echo "$@" >> "$FAKE_LAUNCHCTL_LOG"
case "$1" in kickstart|bootstrap) target="$3";; *) target="$2";; esac
rule="$FAKE_LAUNCHCTL_RULES/$1.$(basename "$target" .plist)"
[ -f "$rule.sleep" ] && sleep "$(cat "$rule.sleep")"
if [ -f "$rule" ]; then
  [ -f "$rule.err" ] && cat "$rule.err" >&2
  exit "$(cat "$rule")"
fi
[ "$1" = print ] && printf '\\tstate = running\\n\\tpid = 4242\\n'
exit 0
"""


@pytest.fixture(scope="module")
def fakes(tmp_path_factory):
    """假 launchctl 与假 bun，整个模块只造一次。返回 (假 launchctl, 假 bun) 的路径。"""
    d = tmp_path_factory.mktemp("stash-fakes")
    exe, bun = d / "launchctl", d / "bun"
    exe.write_text(FAKE_LAUNCHCTL, encoding="utf-8")
    bun.write_text("#!/bin/sh\necho \"$@\" >> \"$FAKE_BUN_LOG\"\nexit 97\n", encoding="utf-8")
    exe.chmod(0o755)
    bun.chmod(0o755)
    # 预热：新建的可执行文件头一回被执行，系统会多查几百毫秒。不预热的话，收短了超时的
    # 用例里头一条调用会无缘无故被判超时。
    env = dict(os.environ, FAKE_LAUNCHCTL_LOG=os.devnull, FAKE_LAUNCHCTL_RULES=str(d))
    subprocess.run([str(exe), "print", "gui/0/warmup"], stdin=subprocess.DEVNULL, env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    return str(exe), str(bun)


@pytest.fixture
def w(tmp_path, monkeypatch, fakes):
    fake, rules, agents, home = (tmp_path / n for n in ("fake", "rules", "LaunchAgents", "dsh-home"))
    for d in (fake, rules, agents, home, tmp_path / "configs-dsh", tmp_path / "configs-legacy"):
        d.mkdir()
    log = fake / "launchctl.log"
    monkeypatch.setenv("FAKE_LAUNCHCTL_LOG", str(log))
    monkeypatch.setenv("FAKE_LAUNCHCTL_RULES", str(rules))
    monkeypatch.setenv("FAKE_BUN_LOG", str(fake / "bun.log"))
    monkeypatch.setenv("HUB_LAUNCHCTL", fakes[0])
    monkeypatch.setenv("HUB_BUN", fakes[1])
    monkeypatch.setenv("HUB_LAUNCHAGENTS_DIR", str(agents))
    monkeypatch.setenv("DSH_BOT_HOME", str(home))
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(tmp_path / "configs-dsh"))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(tmp_path / "configs-legacy"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))   # Windows 上 expanduser 读它
    monkeypatch.delenv("HUB_BOTS_FILE", raising=False)
    monkeypatch.setattr(bl, "POLL_INTERVAL", 0.02)
    monkeypatch.setattr(bl, "RESTART_WAIT", 1.0)
    assert str(agents).startswith(str(tmp_path)), "plist 目录没落在 tmp_path 下，停手"

    def rule(cmd, label, code, err="", sleep=None):
        (rules / ("%s.%s" % (cmd, label))).write_text(str(code), encoding="utf-8")
        if err:
            (rules / ("%s.%s.err" % (cmd, label))).write_text(err + "\n", encoding="utf-8")
        if sleep is not None:
            (rules / ("%s.%s.sleep" % (cmd, label))).write_text(str(sleep), encoding="utf-8")

    def calls():
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    def put(name, tag=None):
        p = agents / name
        p.write_text("<plist><!-- %s --></plist>\n" % (tag or name), encoding="utf-8")
        return p

    def names(d=agents):
        return sorted(p.name for p in d.iterdir())

    yield SimpleNamespace(tmp=tmp_path, agents=agents, home=home, rule=rule, calls=calls, put=put,
                          names=names, actions=lambda: [c for c in calls() if not c.startswith("print ")])
    assert not (fake / "bun.log").exists(), "启停与收起放回的路径上不该有人调 bun"


def _gui(label):
    return "gui/%d/%s" % (os.getuid(), label)


def _boot(label):
    return "bootout %s" % _gui(label)


def _strap(w, label):
    return "bootstrap gui/%d %s" % (os.getuid(), w.agents / (label + ".plist"))


# ============================================================ 名字：与单元名同一个把关入口

def test_定时单元的名字_与主单元经同一个入口():
    assert bl.label_of(BOT) == MAIN
    assert bl.label_of(BOT, timer=True) == TIMER


@pytest.mark.parametrize("bad", ["jiwen", "director", "voicecall", "Bad-Name", "a", "stashbot\n",
                                 " stashbot", "../x", "a/b", "self-initiate.x", "", None, 7])
def test_不合格的id_单元名与两份文件名都不给(w, bad):
    for timer in (False, True):
        assert bl.label_of(bad, timer) is None
        assert bl.plist_paths(bad, timer) is None
        assert bl.unit_state(bad, timer) is None


def test_两份文件名_恰好是单元名加plist与再加disabled_都在plist目录里(w):
    assert bl.plist_paths(BOT) == (str(w.agents / (MAIN + ".plist")),
                                   str(w.agents / (MAIN + ".plist.disabled")))
    assert bl.plist_paths(BOT, timer=True) == (str(w.agents / (TIMER + ".plist")),
                                               str(w.agents / (TIMER + ".plist.disabled")))


def test_给了受理时的接缝_文件名拼在那个目录下_之后环境怎么变都不跟着变(w, monkeypatch):
    seams = bl.resolve_seams()
    other = w.tmp / "LaunchAgents-later"
    other.mkdir()
    monkeypatch.setenv("HUB_LAUNCHAGENTS_DIR", str(other))
    assert bl.plist_paths(BOT, False, seams)[0] == str(w.agents / (MAIN + ".plist"))
    assert bl.plist_paths(BOT)[0] == str(other / (MAIN + ".plist"))


def test_主单元与定时单元的名字不会互相撞(w):
    """bot id 里没有点与连字符，`self-initiate.<x>` 不可能是谁的主单元名。"""
    assert bl.label_of("self_initiate") == "com.dsh-bot.self_initiate"
    assert bl.label_of("self_initiate") != bl.label_of("x", timer=True)


# ============================================================ 三种状态，只看文件

def test_状态_有plist是装着_另有一份disabled也算装着(w):
    w.put(MAIN + ".plist")
    assert bl.unit_state(BOT) == "installed"
    w.put(MAIN + ".plist.disabled")
    assert bl.unit_state(BOT) == "installed"


def test_状态_只有disabled是收起_两个都没有是没装(w):
    assert bl.unit_state(BOT) == "absent"
    w.put(MAIN + ".plist.disabled")
    assert bl.unit_state(BOT) == "stashed"
    assert bl.unit_state(BOT, timer=True) == "absent", "两个单元各看各的文件"


def test_状态_plist这个名字被目录占着_按没装_不拿它去装载也不往上放回(w):
    (w.agents / (MAIN + ".plist")).mkdir()
    w.put(MAIN + ".plist.disabled")
    assert bl.unit_state(BOT) == "absent"
    assert bl.restore_plist(BOT) is True and bl.stash_plist(BOT) is True
    assert bl.bootstrap(BOT) == ("no_plist", "") and w.calls() == []
    assert (w.agents / (MAIN + ".plist")).is_dir() and (w.agents / (MAIN + ".plist.disabled")).is_file()


def test_状态_plist是断掉的软链_按没装_收起的那份不往上放回(w):
    os.symlink(str(w.tmp / "no-such-target"), str(w.agents / (MAIN + ".plist")))
    w.put(MAIN + ".plist.disabled")
    assert bl.unit_state(BOT) == "absent"
    assert bl.restore_plist(BOT) is True
    assert os.path.islink(str(w.agents / (MAIN + ".plist")))


def test_状态_plist目录不存在_没装(w, monkeypatch):
    monkeypatch.setenv("HUB_LAUNCHAGENTS_DIR", str(w.tmp / "no-such-agents"))
    assert bl.unit_state(BOT) == "absent" and bl.stash_plist(BOT) is True and bl.restore_plist(BOT) is True
    assert not (w.tmp / "no-such-agents").exists(), "不该顺手把目录建出来"


# ============================================================ 收起：原子改名，只动这一个名字

def test_收起_是同一个文件改了名_不是复制一份再删(w):
    p = w.put(MAIN + ".plist")
    ino, data = p.stat().st_ino, p.read_bytes()
    assert bl.stash_plist(BOT) is True
    stash = w.agents / (MAIN + ".plist.disabled")
    assert w.names() == [MAIN + ".plist.disabled"]
    assert (stash.stat().st_ino, stash.read_bytes()) == (ino, data)


def test_收起_已有同名的disabled被新的盖掉(w):
    fresh = w.put(MAIN + ".plist", "fresh").read_bytes()
    w.put(MAIN + ".plist.disabled", "stale")
    assert bl.stash_plist(BOT) is True
    assert w.names() == [MAIN + ".plist.disabled"]
    assert (w.agents / (MAIN + ".plist.disabled")).read_bytes() == fresh


@pytest.mark.parametrize("setup", ["stashed", "absent"])
def test_收起_已经收起的与没装的_不动文件_也不算出错(w, setup):
    if setup == "stashed":
        w.put(MAIN + ".plist.disabled")
    before = {n: (w.agents / n).stat().st_mtime_ns for n in w.names()}
    assert bl.stash_plist(BOT) is True
    assert {n: (w.agents / n).stat().st_mtime_ns for n in w.names()} == before


def test_收起_plist是指向别处的软链_只改软链的名字_那个文件不动(w):
    target = w.tmp / "elsewhere.plist"
    target.write_text("真身", encoding="utf-8")
    os.symlink(str(target), str(w.agents / (MAIN + ".plist")))
    assert bl.stash_plist(BOT) is True
    assert w.names() == [MAIN + ".plist.disabled"]
    assert os.path.islink(str(w.agents / (MAIN + ".plist.disabled")))
    assert target.read_text(encoding="utf-8") == "真身"


BYSTANDERS = ["com.dsh-bot.otherbot.plist", "com.dsh-bot.self-initiate.otherbot.plist",
              MAIN + "x.plist", MAIN + ".plist.bak", MAIN + ".plist.disabled.old",
              "com.dsh-bot.jiwen.plist", "com.dsh-bot.memory-keeper.plist", "com.example.thing.plist",
              "com.dsh-bot.stashbo.plist", "notes.txt"]


def test_收起与放回_只动这只的两个名字_目录里别的文件一个不碰(w):
    """名字相近的、别的 bot 的、保留名的、与 bot 无关的，都在旁边看着。"""
    for n in BYSTANDERS:
        w.put(n)
    before = {n: ((w.agents / n).read_bytes(), (w.agents / n).stat().st_mtime_ns) for n in BYSTANDERS}
    w.put(MAIN + ".plist")
    w.put(TIMER + ".plist")
    assert bl._stop_one(BOT) == (True, None)
    assert w.names() == sorted(BYSTANDERS + STASHED)
    assert bl._start_one(BOT) == (True, None)
    assert w.names() == sorted(BYSTANDERS + LIVE)
    assert {n: ((w.agents / n).read_bytes(), (w.agents / n).stat().st_mtime_ns)
            for n in BYSTANDERS} == before


@needs_perm
def test_收起_目录不可写_回报出错_文件名不变(w):
    w.put(MAIN + ".plist")
    os.chmod(w.agents, 0o555)
    try:
        assert bl.stash_plist(BOT) is False
    finally:
        os.chmod(w.agents, 0o755)
    assert w.names() == [MAIN + ".plist"]


def test_收起_disabled那个名字被目录占着_回报出错_plist还在原处(w):
    w.put(MAIN + ".plist")
    (w.agents / (MAIN + ".plist.disabled")).mkdir()
    assert bl.stash_plist(BOT) is False
    assert (w.agents / (MAIN + ".plist")).is_file()


# ============================================================ 放回

def test_放回_是同一个文件改了名_内容原样(w):
    p = w.put(MAIN + ".plist.disabled")
    ino, data = p.stat().st_ino, p.read_bytes()
    assert bl.restore_plist(BOT) is True
    live = w.agents / (MAIN + ".plist")
    assert w.names() == [MAIN + ".plist"] and (live.stat().st_ino, live.read_bytes()) == (ino, data)


def test_放回_现役的plist在_那份旧的disabled不许盖上来(w):
    live = w.put(MAIN + ".plist", "live").read_bytes()
    stale = w.put(MAIN + ".plist.disabled", "stale").read_bytes()
    assert bl.restore_plist(BOT) is True
    assert (w.agents / (MAIN + ".plist")).read_bytes() == live
    assert (w.agents / (MAIN + ".plist.disabled")).read_bytes() == stale


@needs_perm
def test_放回_目录不可写_回报出错_仍是收起状态(w):
    w.put(MAIN + ".plist.disabled")
    os.chmod(w.agents, 0o555)
    try:
        assert bl.restore_plist(BOT) is False
    finally:
        os.chmod(w.agents, 0o755)
    assert w.names() == [MAIN + ".plist.disabled"]


def test_先收起再放回_一圈下来还是原来那个文件(w):
    p = w.put(MAIN + ".plist")
    ino, data = p.stat().st_ino, p.read_bytes()
    assert bl.stash_plist(BOT) and bl.restore_plist(BOT)
    assert w.names() == [MAIN + ".plist"] and (p.stat().st_ino, p.read_bytes()) == (ino, data)


# ============================================================ 停用方向

def test_停用_两条bootout先主单元后定时单元_再把两份都收起(w):
    w.put(MAIN + ".plist")
    w.put(TIMER + ".plist")
    assert bl._stop_one(BOT) == (True, None)
    assert w.calls() == [_boot(MAIN), _boot(TIMER)]
    assert w.names() == STASHED


def test_停用_定时单元本来就没装载_launchd说找不到_算成功(w):
    w.put(MAIN + ".plist")
    w.rule("bootout", TIMER, 3, "Boot-out failed: 3: No such process")
    assert bl._stop_one(BOT) == (True, None)
    assert w.calls() == [_boot(MAIN), _boot(TIMER)] and w.names() == [MAIN + ".plist.disabled"]


@pytest.mark.parametrize("failing", [MAIN, TIMER], ids=["main", "timer"])
def test_停用_哪条bootout报错_另一条照发_两份照样收起_这只按失败报(w, failing):
    w.put(MAIN + ".plist")
    w.put(TIMER + ".plist")
    w.rule("bootout", failing, 5, "Boot-out failed: 5: Input/output error")
    assert bl._stop_one(BOT) == (False, "launchctl bootout 失败")
    assert w.calls() == [_boot(MAIN), _boot(TIMER)]
    assert w.names() == STASHED


def test_停用_bootout超时_原因是超时_另一条照发_两份照样收起(w, monkeypatch):
    w.put(MAIN + ".plist")
    w.put(TIMER + ".plist")
    w.rule("bootout", MAIN, 0, sleep=2)
    monkeypatch.setattr(bl, "LAUNCHCTL_TIMEOUT", SHORT)
    assert bl._stop_one(BOT) == (False, TIMED_OUT)
    assert w.calls() == [_boot(MAIN), _boot(TIMER)]
    assert w.names() == STASHED


@needs_perm
def test_停用_收起失败_两条bootout照发_原因是plist没能收起(w):
    w.put(MAIN + ".plist")
    w.put(TIMER + ".plist")
    os.chmod(w.agents, 0o555)
    try:
        assert bl._stop_one(BOT) == (False, "plist 没能收起")
    finally:
        os.chmod(w.agents, 0o755)
    assert w.calls() == [_boot(MAIN), _boot(TIMER)]
    assert w.names() == LIVE


@needs_perm
def test_停用_bootout与收起都没成_原因取最先出的那个(w):
    w.put(MAIN + ".plist")
    w.rule("bootout", MAIN, 5, "Boot-out failed: 5: Input/output error")
    os.chmod(w.agents, 0o555)
    try:
        assert bl._stop_one(BOT) == (False, "launchctl bootout 失败")
    finally:
        os.chmod(w.agents, 0o755)


def test_停用_主单元那份收起失败_定时单元那份照样收起(w):
    """一份没收成不是不去收另一份的理由。"""
    w.put(MAIN + ".plist")
    w.put(TIMER + ".plist")
    (w.agents / (MAIN + ".plist.disabled")).mkdir()          # 主单元的收起位被目录占着
    assert bl._stop_one(BOT) == (False, "plist 没能收起")
    assert (w.agents / (MAIN + ".plist")).is_file()
    assert (w.agents / (TIMER + ".plist.disabled")).is_file() and not (w.agents / (TIMER + ".plist")).exists()


def test_停用_已经收起的再停一次_仍是两条bootout_文件不动_成功(w):
    w.put(MAIN + ".plist.disabled")
    w.put(TIMER + ".plist.disabled")
    for label in (MAIN, TIMER):
        w.rule("bootout", label, 113, 'Could not find service "%s" in domain for user gui: 501' % label)
    before = {n: (w.agents / n).stat().st_mtime_ns for n in w.names()}
    assert bl._stop_one(BOT) == (True, None)
    assert w.calls() == [_boot(MAIN), _boot(TIMER)]
    assert {n: (w.agents / n).stat().st_mtime_ns for n in w.names()} == before


@pytest.mark.parametrize("reserved", ["jiwen", "director", "voicecall"])
def test_停用_保留名_零launchctl_它名下的两份plist文件名不变_按失败报(w, reserved):
    names = ["com.dsh-bot.%s.plist" % reserved, "com.dsh-bot.self-initiate.%s.plist" % reserved]
    for n in names:
        w.put(n)
    assert bl._stop_one(reserved) == (False, "launchctl bootout 失败")
    assert w.calls() == [] and w.names() == sorted(names)


# ============================================================ 启用方向

def test_启用_两份都收起_先放回_再kickstart主单元_再bootout加bootstrap定时单元(w):
    w.put(MAIN + ".plist.disabled")
    w.put(TIMER + ".plist.disabled")
    assert bl._start_one(BOT) == (True, None)
    assert w.names() == LIVE
    assert w.calls() == ["kickstart -k %s" % _gui(MAIN), _boot(TIMER), _strap(w, TIMER)]


def test_启用_主单元在launchd里已经卸载_回落bootstrap用的是放回之后的那份(w):
    w.put(MAIN + ".plist.disabled")
    w.rule("kickstart", MAIN, 113, "Could not find service")
    assert bl._start_one(BOT) == (True, None)
    assert w.calls() == ["kickstart -k %s" % _gui(MAIN), _strap(w, MAIN)]
    assert w.names() == [MAIN + ".plist"]


def test_启用_目录里没有主单元的文件但launchd里服务在_kickstart成功就算成功_与以前相同(w):
    assert bl._start_one(BOT) == (True, None)
    assert w.calls() == ["kickstart -k %s" % _gui(MAIN)] and w.names() == []


def test_启用_文件没有服务也没有_才是单元没装_不凭空建文件(w):
    w.rule("kickstart", MAIN, 113, "Could not find service")
    assert bl._start_one(BOT) == (False, "单元没装（plist 不存在）")
    assert w.calls() == ["kickstart -k %s" % _gui(MAIN)] and w.names() == []


def test_启用_没有定时单元_不出现针对它的bootout与bootstrap(w):
    w.put(MAIN + ".plist")
    assert bl._start_one(BOT) == (True, None)
    assert w.calls() == ["kickstart -k %s" % _gui(MAIN)]


def test_启用_定时单元装着_先bootout再bootstrap(w):
    w.put(MAIN + ".plist")
    w.put(TIMER + ".plist")
    assert bl._start_one(BOT) == (True, None)
    assert w.calls() == ["kickstart -k %s" % _gui(MAIN), _boot(TIMER), _strap(w, TIMER)]


def test_启用_定时单元的bootout说本来就没装载_照样bootstrap(w):
    w.put(TIMER + ".plist")
    w.rule("bootout", TIMER, 3, "Boot-out failed: 3: No such process")
    assert bl._start_one(BOT) == (True, None)
    assert w.calls()[-2:] == [_boot(TIMER), _strap(w, TIMER)]


def test_启用_定时单元bootstrap失败_原因是主动消息的定时任务没装回去(w):
    w.put(TIMER + ".plist")
    w.rule("bootstrap", TIMER, 5, "Bootstrap failed: 5: Input/output error")
    assert bl._start_one(BOT) == (False, "主动消息的定时任务没装回去")


def test_启用_定时单元bootout报了不在容错范围内的错_不再发bootstrap_按没装回去报(w):
    """约定没写这一格：那个单元此刻什么状态说不清，取保守的一边。"""
    w.put(TIMER + ".plist")
    w.rule("bootout", TIMER, 5, "Boot-out failed: 5: Input/output error")
    assert bl._start_one(BOT) == (False, "主动消息的定时任务没装回去")
    assert w.calls() == ["kickstart -k %s" % _gui(MAIN), _boot(TIMER)]


@pytest.mark.parametrize("slow", ["bootout", "bootstrap"])
def test_启用_定时单元哪条超时_原因是超时(w, monkeypatch, slow):
    w.put(TIMER + ".plist")
    w.rule(slow, TIMER, 0, sleep=2)
    monkeypatch.setattr(bl, "LAUNCHCTL_TIMEOUT", SHORT)
    assert bl._start_one(BOT) == (False, TIMED_OUT)
    assert ("bootstrap" in " ".join(w.calls())) is (slow == "bootstrap")


def test_启用_主单元没起来_不去碰定时单元(w):
    w.put(MAIN + ".plist")
    w.put(TIMER + ".plist")
    w.rule("kickstart", MAIN, 1, "kickstart failed")
    assert bl._start_one(BOT) == (False, "launchctl kickstart 失败")
    assert w.calls() == ["kickstart -k %s" % _gui(MAIN)]


@needs_perm
def test_启用_放回失败_launchctl一条都不发_原因是plist没能放回(w):
    w.put(MAIN + ".plist.disabled")
    w.put(TIMER + ".plist.disabled")
    os.chmod(w.agents, 0o555)
    try:
        assert bl._start_one(BOT) == (False, "plist 没能放回")
    finally:
        os.chmod(w.agents, 0o755)
    assert w.calls() == [] and w.names() == STASHED


def test_启用_只有定时单元那份放不回_同样一条launchctl都不发(w, monkeypatch):
    w.put(MAIN + ".plist")
    w.put(TIMER + ".plist.disabled")
    renamed = []

    def denied(src, dst):
        renamed.append(os.path.basename(src))
        raise PermissionError(13, "denied")
    monkeypatch.setattr(bl.os, "rename", denied)
    assert bl._start_one(BOT) == (False, "plist 没能放回")
    assert renamed == [TIMER + ".plist.disabled"] and w.calls() == []


@pytest.mark.parametrize("reserved", ["jiwen", "director", "voicecall"])
def test_启用_保留名_零launchctl_收起着的那份也不放回_按失败报(w, reserved):
    names = ["com.dsh-bot.%s.plist.disabled" % reserved,
             "com.dsh-bot.self-initiate.%s.plist.disabled" % reserved]
    for n in names:
        w.put(n)
    assert bl._start_one(reserved) == (False, "launchctl kickstart 失败")
    assert w.calls() == [] and w.names() == sorted(names)


# ============================================================ 编组与接缝

def _beat(w, bot, pid=4242):
    import json
    import time
    p = w.home / "bots" / bot / "state" / "heartbeat"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"at": int(time.time() * 1000) + 3000, "pid": pid}), encoding="utf-8")


def test_单只同步_停用成功_tail是已停止(w):
    w.put(MAIN + ".plist")
    assert bl.sync_one(BOT, False) == ("done", None, "重启 1 只：成功 1\n%s 已停止" % BOT)


def test_单只同步_收起失败_按失败报_失败行带原因与日志路径(w):
    w.put(MAIN + ".plist")
    (w.agents / (MAIN + ".plist.disabled")).mkdir()
    state, code, tail = bl.sync_one(BOT, False)
    assert (state, code) == ("failed", None)
    assert tail.splitlines()[0] == "重启 1 只：成功 0，失败 1" and len(tail.splitlines()) == 2
    assert tail.splitlines()[-1] == "%s 失败：plist 没能收起；日志 %s" % (
        BOT, w.home / "bots" / BOT / "logs" / "launchd.log")


def test_单只同步_启用_三条动作发完才开始等活(w):
    w.put(MAIN + ".plist.disabled")
    w.put(TIMER + ".plist.disabled")
    _beat(w, BOT)
    state, _code, tail = bl.sync_one(BOT, True)
    assert state == "done" and tail.splitlines()[-1].startswith("%s 已重启（pid 4242" % BOT)
    verbs = [c.split()[0] for c in w.calls()]
    assert verbs[:3] == ["kickstart", "bootout", "bootstrap"] and set(verbs[3:]) == {"print"}


def test_重启全部的编组_停用的收起_启用的放回_停用那只没有kickstart与bootstrap(w):
    w.put("com.dsh-bot.onbot.plist.disabled")                 # 启用的那只：收起着，要放回
    w.put("com.dsh-bot.offbot.plist")                         # 停用的那只：存量，plist 还装着
    w.put("com.dsh-bot.self-initiate.offbot.plist")
    _beat(w, "onbot")
    state, _code, tail = bl.restart_group([("onbot", True), ("offbot", False)])
    assert state == "done" and tail.splitlines()[0] == "重启 2 只：成功 2"
    assert w.names() == ["com.dsh-bot.offbot.plist.disabled", "com.dsh-bot.onbot.plist",
                         "com.dsh-bot.self-initiate.offbot.plist.disabled"]
    off = [c for c in w.actions() if "offbot" in c]
    assert off == [_boot("com.dsh-bot.offbot"), _boot("com.dsh-bot.self-initiate.offbot")]


def test_受理之后换掉plist目录_收起的仍是受理时那个目录里的(w, monkeypatch):
    w.put(MAIN + ".plist")
    other = w.tmp / "LaunchAgents-later"
    other.mkdir()
    (other / (MAIN + ".plist")).write_text("later", encoding="utf-8")
    seams = bl.resolve_seams()
    monkeypatch.setenv("HUB_LAUNCHAGENTS_DIR", str(other))
    assert bl._stop_one(BOT, seams) == (True, None)
    assert w.names() == [MAIN + ".plist.disabled"] and w.names(other) == [MAIN + ".plist"]


def test_受理之后换掉plist目录_放回与装回定时单元用的也是受理时那个目录(w, monkeypatch):
    w.put(MAIN + ".plist.disabled")
    w.put(TIMER + ".plist.disabled")
    other = w.tmp / "LaunchAgents-later"
    other.mkdir()
    seams = bl.resolve_seams()
    monkeypatch.setenv("HUB_LAUNCHAGENTS_DIR", str(other))
    assert bl._start_one(BOT, seams) == (True, None)
    assert w.names() == LIVE and w.names(other) == []
    assert w.calls()[-1] == _strap(w, TIMER)
