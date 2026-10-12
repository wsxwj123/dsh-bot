# -*- coding: utf-8 -*-
"""tests/ 的总闸：会动真实系统的接缝，默认一律拒绝。

为什么有这个文件
----------------
管理台有些流程会执行系统命令：经 `HUB_LAUNCHCTL` 调 launchctl，经 `HUB_BUN` 跑安装脚本
（脚本内部也会调 launchctl）。出过一次事故：部分测试目录的夹具只换了假 HOME，没换这两个
变量，测试就往这台机器真实的 launchd 用户域里注册了 12 个假 bot 的启动项。launchctl 不看
HOME，假 HOME 拦不住它；plist 文件删掉以后 launchd 也不会自己卸载。

所以规矩反过来定：**默认就是拦死的**，要用假件的目录自己覆盖，别指望每个目录各自记得换。
依据是 `.devflow/INTERFACE-新系统bot运维.md` §1「环境接缝」那张表。

本文件做的六件事
----------------
1. 导入期（模块顶层，早于任何用例、任何子目录 conftest）把 `HUB_LAUNCHCTL`、`HUB_BUN`
   指向「毒丸」：一个可执行文件，被调用时把 argv 追加到会话级记录文件，然后以 97 退出，
   绝不转发给真实命令。被测代码在导入时读变量还是调用时读变量，都拦得住。
2. 每条用例开始前（autouse 夹具）把这两个变量重新设回毒丸，上一条用例、上一个目录漏过来
   的设置到此为止。各目录自己的夹具排在本夹具之后执行，可以再覆盖成假件
   （`acceptance/bot_ops`、`acceptance/hub2` 就是这么做的）。覆盖必须写在函数级夹具里：
   模块级、会话级夹具先于本夹具执行，它们设的值会被这里重置掉。
3. 默认值指向真实用户资产或仓库内真实文件的接缝也改道：`HUB_LAUNCHAGENTS_DIR`、
   `DSH_BOT_HOME`、`HUB_CONFIGS_DSH_DIR`、`HUB_CHANNELS_DIR`、`HUB_CONFIGS_DIR` 指到会话临时
   目录（每条用例一套全新的空目录），`HUB_TELEGRAM_API_BASE` 指到必然拒绝连接的回环地址。
4. 每条用例结束后归因：毒丸记录有新增，这条用例就判失败，错误信息带上被拦下的 argv
   （像 token 的长串会打码）。落在用例窗口之外的调用（收集期、两条用例之间）归不到具体
   用例，会话结束时让整场失败。
5. 会话级兜底：会话开始、结束各取一次 `launchctl list` 里 `com.dsh-bot.` 开头的标签集合，
   结束时多出任何一个，整场以失败退出并列出多出来的标签。
6. 没有 launchctl 的机器（Linux、Windows 的 CI）上第 5 条自动跳过，其余照常。只用标准库
   与 pytest，没有新依赖。

本文件自己唯一会执行的系统命令是只读的 `/bin/launchctl list`（第 5 条）。

总闸的局限（别误以为全拦住了）
------------------------------
- 测试自己起子进程、又给子进程传了精简环境（不带 `HUB_LAUNCHCTL`）的场合，子进程里的代码
  拿到的是默认的真 launchctl。这种路径只有第 5 条能事后发现。
- 直接用真 bun 跑真安装脚本的用例，毒丸拦不住脚本内部的 launchctl 调用。
- pytest 只加载「启动目录及其下」的 conftest：在 `tests/` 的某个子目录**里面**启动 pytest
  （例如 `cd tests/acceptance && pytest hub_ui`）时本文件不会被加载。请从仓库根或 `tests/`
  启动。
- 用例留下的后台线程（例如重启 job）拖到夹具撤掉假件之后才去调接缝的，照样打到毒丸，但归因
  不准：落在下一条用例期间就记到它头上（失败信息会点明多半不是它调的），落在两条用例之间
  就让整场失败。根子在那条没等后台任务结束就收尾的用例。

自测在 `tests/gate_selftest/`。
"""
import atexit
import os
import re
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

# 进程内唯一的一份状态挂在 sys 上：pytest 在导入每个不是包的 conftest 前会把
# sys.modules['conftest'] 删掉，本模块万一被二次导入，也不能再造一套毒丸与记录文件。
_STATE_ATTR = "_dsh_bot_test_gate"

POISON_EXIT = 97
#: 必然拒绝连接的回环地址：1 号端口要 root 才能监听，普通进程占不了。
REFUSING_TELEGRAM_BASE = "http://127.0.0.1:1"
#: 只给总闸自测用：把第 5 条取快照的 launchctl 换成假的只读程序。只会被以 `list` 调用。
SNAPSHOT_OVERRIDE_ENV = "DSH_TEST_GATE_SNAPSHOT_LAUNCHCTL"
LABEL_PREFIX = "com.dsh-bot."
#: 改道到临时目录的目录类接缝（§1）
DIR_SEAMS = ("HUB_LAUNCHAGENTS_DIR", "DSH_BOT_HOME", "HUB_CONFIGS_DSH_DIR", "HUB_CHANNELS_DIR",
             "HUB_CONFIGS_DIR")

_US = "\x1f"    # 一条记录里各个 argv 之间的分隔符
_RS = "\x1e"    # 一条记录的结束符（不用换行：参数里可能带换行）


# ------------------------------------------------------------------ 毒丸

def poison_script_text(name, record_path):
    """POSIX 毒丸的全文。纯 sh、只用内建命令：被测代码给子进程的环境再精简也跑得起来。"""
    quoted = "'" + str(record_path).replace("'", "'\\''") + "'"
    return (
        "#!/bin/sh\n"
        "# dsh-bot 测试总闸的毒丸：只记录 argv，然后以 %(code)d 退出；绝不转发给真实命令。\n"
        "US=$(printf '\\037')\n"
        "rec='%(name)s'\n"
        'for a in "$@"; do rec="$rec$US$a"; done\n'
        "printf '%%s\\036' \"$rec\" >> %(record)s 2>/dev/null\n"
        "echo 'dsh-test-gate: poison %(name)s was called and refused;"
        " tests must not touch the real system' >&2\n"
        "exit %(code)d\n"
    ) % {"name": name, "record": quoted, "code": POISON_EXIT}


_WINDOWS_RECORDER = (
    "import sys\n"
    "rec, argv = sys.argv[1], sys.argv[2:]\n"
    "with open(rec, 'ab') as f:\n"
    "    f.write('\\x1f'.join(argv).encode('utf-8', 'replace') + b'\\x1e')\n"
)


def write_poison(dirpath, name, record_path):
    """在 dirpath 下造一个名叫 name 的毒丸，返回可执行文件路径。"""
    os.makedirs(dirpath, exist_ok=True)
    if os.name == "nt":
        recorder = os.path.join(dirpath, "_recorder.py")
        with open(recorder, "w", encoding="utf-8") as f:
            f.write(_WINDOWS_RECORDER)
        path = os.path.join(dirpath, name + ".cmd")
        with open(path, "w", encoding="utf-8") as f:
            f.write('@echo off\r\n"%s" "%s" "%s" %s %%*\r\nexit /b %d\r\n'
                    % (sys.executable, recorder, record_path, name, POISON_EXIT))
        return path
    path = os.path.join(dirpath, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(poison_script_text(name, record_path))
    os.chmod(path, 0o755)
    return path


def read_records(record_path):
    """读毒丸记录：[[名字, 参数1, 参数2, ...], ...]，按发生顺序。"""
    try:
        with open(record_path, "rb") as f:
            raw = f.read()
    except OSError:
        return []
    out = []
    for chunk in raw.split(_RS.encode()):
        chunk = chunk.strip(b"\r\n")
        if chunk:
            out.append([p.decode("utf-8", "replace") for p in chunk.split(_US.encode())])
    return out


# ------------------------------------------------------------------ 打码

_TG_TOKEN = re.compile(r"\d{6,}:[A-Za-z0-9_-]{20,}")
_SECRET_KV = re.compile(
    r"(?i)([A-Za-z0-9_.-]*(?:token|secret|passw(?:or)?d|api[_-]?key|credential|authorization)"
    r"[A-Za-z0-9_.-]*)(\s*[=:]\s*)(\S+)")
_PREFIXED_KEY = re.compile(r"\b(?:sk|pk|rk|ghp|gho|ghs|xox[abprs])[-_][A-Za-z0-9_-]{8,}")
_LONG_BLOB = re.compile(r"[A-Za-z0-9_+=-]{32,}")


def _stars(secret):
    return "***(已打码,%d字符)" % len(secret)


def mask(text):
    """把像密钥的串打码。宁可多打，不可漏打。

    四种形态：Telegram bot token（数字段:长串）；名字里带 token/secret/password/api_key 的
    「键=值」；sk- 这类带前缀的 key；32 个字符以上不带路径分隔符的连续长串。
    """
    text = str(text)
    text = _TG_TOKEN.sub(lambda m: _stars(m.group(0)), text)
    text = _SECRET_KV.sub(
        lambda m: m.group(0) if m.group(3).startswith("***(已打码")
        else m.group(1) + m.group(2) + _stars(m.group(3)), text)
    text = _PREFIXED_KEY.sub(lambda m: _stars(m.group(0)), text)
    text = _LONG_BLOB.sub(lambda m: _stars(m.group(0)), text)
    return text


def format_call(record):
    """把一条记录排成一行给人看的命令（逐个参数打码）。"""
    parts = []
    for arg in record:
        arg = mask(arg)
        parts.append(arg if arg and not re.search(r"\s", arg) else repr(arg))
    return " ".join(parts)


# ------------------------------------------------------------------ 启动（导入期）

def _find_snapshot_bin():
    """第 5 条用的只读 launchctl。只认 macOS 的固定位置，不走 PATH。找不到返回 None。"""
    cand = os.environ.get(SNAPSHOT_OVERRIDE_ENV) or "/bin/launchctl"
    return cand if (os.path.isfile(cand) and os.access(cand, os.X_OK)) else None


def _cleanup(gate_dir, owner_pid):
    if os.getpid() == owner_pid:        # fork 出去的子进程退出时不许删父进程的总闸目录
        shutil.rmtree(gate_dir, ignore_errors=True)


def _boot():
    """建会话级的总闸目录、毒丸、记录文件。同一进程内重复导入时复用，返回 (状态, 是否新建)。"""
    st = getattr(sys, _STATE_ATTR, None)
    if st is not None and st.get("pid") == os.getpid() and os.path.isdir(st["dir"]):
        return st, False
    gate_dir = os.path.realpath(tempfile.mkdtemp(prefix="dsh-test-gate-"))
    record = os.path.join(gate_dir, "poison-calls.rec")
    open(record, "ab").close()
    poison_dir = os.path.join(gate_dir, "poison")
    st = {
        "pid": os.getpid(),
        "dir": gate_dir,
        "record": record,
        "poison_dir": poison_dir,
        "poison": {name: write_poison(poison_dir, name, record) for name in ("launchctl", "bun")},
        "session_base": os.path.join(gate_dir, "session"),
        "case_seq": 0,
        "attributed": 0,        # 已经归过因的记录条数
        "hits": [],             # [(nodeid, 阶段, [记录...])]：碰了接缝的用例
        "orphans": [],          # [(发现时机, [记录...])]：落在用例窗口之外的调用
        "snapshot_bin": _find_snapshot_bin(),
        "labels_start": None,
        "labels_end": None,
        "extra_labels": [],
        "missing_labels": [],
        "inherited_cleared": [],
    }
    setattr(sys, _STATE_ATTR, st)
    atexit.register(_cleanup, gate_dir, os.getpid())
    return st, True


def _ensure_poison():
    """毒丸文件万一被哪条用例删了，补回来（变量仍指着这个路径，删了也到不了真命令）。"""
    for name, path in _GATE["poison"].items():
        if not os.access(path, os.X_OK):
            _GATE["poison"][name] = write_poison(_GATE["poison_dir"], name, _GATE["record"])


def route_env(base):
    """把 §1 里会碰真实系统、真实用户资产的接缝全部指到 base 之下，返回这次设的值。"""
    dsh_home = os.path.join(base, "dsh-home")
    env = {
        "HUB_LAUNCHCTL": _GATE["poison"]["launchctl"],
        "HUB_BUN": _GATE["poison"]["bun"],
        "HUB_LAUNCHAGENTS_DIR": os.path.join(base, "LaunchAgents"),
        "DSH_BOT_HOME": dsh_home,
        # 契约里它的默认值就是 <DSH_BOT_HOME>/configs，这里保持同一层级关系
        "HUB_CONFIGS_DSH_DIR": os.path.join(dsh_home, "configs"),
        "HUB_CHANNELS_DIR": os.path.join(base, "channels"),
        # 旧系统配置根，默认是仓库里的 configs/
        "HUB_CONFIGS_DIR": os.path.join(base, "configs-legacy"),
        "HUB_TELEGRAM_API_BASE": REFUSING_TELEGRAM_BASE,
    }
    for key in DIR_SEAMS:
        os.makedirs(env[key], exist_ok=True)
    os.environ.update(env)
    return env


_GATE, _FRESH = _boot()
if _FRESH:
    # 导入期生效：此刻还没有任何用例、任何子目录 conftest、任何被测模块被导入。
    _GATE["session_env"] = route_env(_GATE["session_base"])
    # §1：HUB_RESTART_CMD 设了就会被当成命令串执行。从外部环境继承来的值来路不明，不执行。
    # 需要它的目录在自己的夹具里显式设（hub_ui、provider_wizard 设的是 /usr/bin/true）。
    if os.environ.pop("HUB_RESTART_CMD", None) is not None:
        _GATE["inherited_cleared"].append("HUB_RESTART_CMD")


# ------------------------------------------------------------------ 每条用例前重置

@pytest.fixture(autouse=True)
def _total_gate_reset():
    """每条用例开始前：两个执行接缝回到毒丸，目录接缝换成本用例专属的空目录。

    故意不依赖 monkeypatch：本夹具在根 conftest，比任何子目录的 autouse 夹具都先执行，
    子目录夹具随后用 monkeypatch 覆盖成假件；用例结束时它们先还原，本夹具最后把环境
    放回会话默认值。
    """
    _GATE["case_seq"] += 1
    _ensure_poison()
    route_env(os.path.join(_GATE["dir"], "cases", "%05d" % _GATE["case_seq"]))
    yield
    route_env(_GATE["session_base"])


@pytest.fixture(scope="session")
def total_gate():
    """总闸的只读句柄（给 tests/gate_selftest 用，别的用例用不着）。"""
    return SimpleNamespace(
        dir=_GATE["dir"],
        record_path=_GATE["record"],
        poison_launchctl=_GATE["poison"]["launchctl"],
        poison_bun=_GATE["poison"]["bun"],
        session_env=dict(_GATE["session_env"]),
        refusing_telegram_base=REFUSING_TELEGRAM_BASE,
        poison_exit=POISON_EXIT,
        records=lambda: read_records(_GATE["record"]),
        read_records=read_records,
        write_poison=write_poison,
        poison_script_text=poison_script_text,
        mask=mask,
        format_call=format_call,
    )


# ------------------------------------------------------------------ 每条用例后归因

def _sweep_orphans(where):
    """把还没归因的记录记成「用例窗口之外的调用」。"""
    recs = read_records(_GATE["record"])
    n = _GATE["attributed"]
    if len(recs) > n:
        _GATE["orphans"].append((where, recs[n:]))
        _GATE["attributed"] = len(recs)


_SEAM_OF = {"launchctl": "HUB_LAUNCHCTL", "bun": "HUB_BUN"}


def _overridden_seams(records):
    """这些记录对应的接缝里，哪些此刻已经不是总闸的毒丸了（被目录夹具换成了别的）。"""
    out = []
    for rec in records:
        seam = _SEAM_OF.get(rec[0]) if rec else None
        if seam and os.environ.get(seam) != _GATE["poison"][rec[0]] and seam not in out:
            out.append(seam)
    return out


def _hit_message(records, overridden=()):
    lines = [
        "测试总闸：这条用例碰了真实系统接缝。",
        "HUB_LAUNCHCTL / HUB_BUN 默认指向毒丸，下面这些调用被拦下了，没有到达真实命令（共 %d 次）："
        % len(records),
    ]
    for i, rec in enumerate(records, 1):
        lines.append("  %d) %s" % (i, format_call(rec)))
    if overridden:
        lines += [
            "注意：这条用例运行时 %s 已经被本目录的夹具换成了别的值，被调用的却是总闸的毒丸。"
            % " / ".join(overridden),
            "多半是前面某条用例留下的后台线程（例如重启 job）在夹具撤掉假件之后才发出的，"
            "不是这条用例自己调的。",
            "怎么修：去查前面几条用例里谁没等后台任务结束就收了尾。"
            "没有总闸的话，这类调用打到的是真实的 launchctl。",
        ]
    else:
        lines += [
            "怎么修：在本目录的夹具里按 .devflow/INTERFACE-新系统bot运维.md §1 把 HUB_LAUNCHCTL / "
            "HUB_BUN 换成假件",
            "（现成的写法见 tests/acceptance/bot_ops/conftest.py 与 "
            "tests/acceptance/hub2/conftest.py）。",
            "提示：上一条用例如果留了后台线程（例如重启 job），这里的调用也可能是它发出的。",
        ]
    return "\n".join(lines)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    # 用例窗口从这里开始。在此之前就有、又没归过因的记录，不算到这条用例头上。
    _sweep_orphans("在 %s 开始之前" % item.nodeid)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if call.when == "setup" and rep.passed:
        return      # 夹具阶段的调用留到 call 的报告里一起判，结果算 FAILED 而不是 ERROR
    recs = read_records(_GATE["record"])
    n = _GATE["attributed"]
    if len(recs) <= n:
        return
    new = recs[n:]
    _GATE["attributed"] = len(recs)
    _GATE["hits"].append((item.nodeid, call.when, new))
    # call 阶段出报告时夹具还没拆，环境就是用例运行时的样子；别的阶段看不准，不下这个判断
    msg = _hit_message(new, _overridden_seams(new) if call.when == "call" else ())
    if rep.failed:
        # 用例本来就红：原来的失败原样保留，总闸的信息附在后面
        addsection = getattr(rep.longrepr, "addsection", None)
        if callable(addsection):
            addsection("测试总闸", msg)
        else:
            rep.sections.append(("测试总闸", msg))
        return
    if hasattr(rep, "wasxfail"):
        del rep.wasxfail            # 预期失败、跳过都不能替「碰了真实系统接缝」打掩护
    rep.outcome = "failed"
    rep.longrepr = msg


# ------------------------------------------------------------------ 会话级兜底

def snapshot_labels():
    """只读：`launchctl list` 里 com.dsh-bot. 开头的标签集合。拿不到返回 None。

    整个总闸只有这一处执行 launchctl，子命令写死是 list。
    """
    exe = _GATE["snapshot_bin"]
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "list"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    labels = set()
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if parts and parts[-1].startswith(LABEL_PREFIX):
            labels.add(parts[-1])
    return labels


def pytest_sessionstart(session):
    _GATE["labels_start"] = snapshot_labels()


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    _sweep_orphans("会话收尾时")
    start = _GATE["labels_start"]
    end = _GATE["labels_end"] = snapshot_labels()
    if start is not None and end is not None:
        _GATE["extra_labels"] = sorted(end - start)
        _GATE["missing_labels"] = sorted(start - end)
    if _GATE["extra_labels"] or _GATE["orphans"]:
        if session.exitstatus in (pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED):
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
        if session.config.pluginmanager.get_plugin("terminalreporter") is None:
            sys.stderr.write("\n".join(_session_problem_lines()) + "\n")


def _session_problem_lines():
    lines = []
    if _GATE["extra_labels"]:
        lines.append("测试总闸：会话期间 launchd 用户域里多出了 %d 个 %s* 标签，整场判失败："
                     % (len(_GATE["extra_labels"]), LABEL_PREFIX))
        lines += ["  + %s" % label for label in _GATE["extra_labels"]]
        lines.append("  这些是真实注册进系统的启动项，plist 删了也不会自己消失，需要人工核对后卸载。")
    if _GATE["orphans"]:
        total = sum(len(recs) for _, recs in _GATE["orphans"])
        lines.append("测试总闸：有 %d 次毒丸调用落在用例窗口之外（收集/导入期，或用例留下的后台任务），"
                     "归不到具体用例，整场判失败：" % total)
        for where, recs in _GATE["orphans"]:
            lines.append("  %s：" % where)
            lines += ["    %s" % format_call(rec) for rec in recs]
    return lines


def _snapshot_status_line():
    start, end = _GATE["labels_start"], _GATE["labels_end"]
    if _GATE["snapshot_bin"] is None:
        return "launchd 兜底：跳过（这台机器没有 launchctl）"
    if start is None or end is None:
        return "launchd 兜底：跳过（launchctl list 没取到结果）"
    return "launchd 兜底：%s* 标签 开始 %d 个 / 结束 %d 个，多出 %d 个" % (
        LABEL_PREFIX, len(start), len(end), len(_GATE["extra_labels"]))


def pytest_report_header(config):
    lines = ["测试总闸：已生效（HUB_LAUNCHCTL / HUB_BUN 默认是毒丸；总闸目录 %s）" % _GATE["dir"]]
    if _GATE["inherited_cleared"]:
        lines.append("测试总闸：已清掉从外部环境继承来的 %s" % "、".join(_GATE["inherited_cleared"]))
    return lines


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    tr = terminalreporter
    hits = _GATE["hits"]
    bad = bool(hits or _GATE["orphans"] or _GATE["extra_labels"])
    tr.write_sep("=", "测试总闸", red=bad, green=not bad)
    tr.write_line("毒丸被调用 %d 次，涉及 %d 条用例；%s" % (
        len(read_records(_GATE["record"])), len({nodeid for nodeid, _, _ in hits}),
        _snapshot_status_line()))
    if hits:
        tr.write_line("碰了真实系统接缝的用例：")
        for nodeid, when, recs in hits:
            tr.write_line("  [%s] %s（%d 次）" % (when, nodeid, len(recs)))
    for line in _session_problem_lines():
        tr.write_line(line, red=True)
    if _GATE["missing_labels"]:
        tr.write_line("注意：会话期间少了这些标签（不判失败，可能是你自己停的；不是的话要查）：%s"
                      % "、".join(_GATE["missing_labels"]), yellow=True)
