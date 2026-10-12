# -*- coding: utf-8 -*-
"""总闸（tests/conftest.py）的自测。

总闸要是坏了，后果是测试悄悄去动真实的 launchd，所以它自己必须有测试。
跑法（仓库根下）：python3 -m pytest tests/gate_selftest

两类测法：
- 进程内：直接看本进程里总闸设好的环境与它造的毒丸。要调毒丸时，用总闸同一个工厂在 tmp 里
  另造一颗（`spare_poison`），不动会话级记录，自测本身不会被总闸判失败。
- 子进程探针：`probes/` 下的文件不以 test_ 开头，平时不会被收集；这里起一个新的 pytest 进程
  点名运行它们，再看退出码与输出。「故意碰闸的用例确实会失败」只能这么验，否则自测自己就红了。
  探针只用只读子命令（print / --version），执行前还会确认目标确实是毒丸。
"""
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

HERE = Path(__file__).resolve().parent
PROBES = HERE / "probes"
TESTS_DIR = HERE.parent
REPO_ROOT = TESTS_DIR.parent

EXEC_SEAMS = ("HUB_LAUNCHCTL", "HUB_BUN")
DIR_SEAMS = ("HUB_LAUNCHAGENTS_DIR", "DSH_BOT_HOME", "HUB_CONFIGS_DSH_DIR", "HUB_CHANNELS_DIR",
             "HUB_CONFIGS_DIR")
TELEGRAM_SEAM = "HUB_TELEGRAM_API_BASE"
ALL_SEAMS = EXEC_SEAMS + DIR_SEAMS + (TELEGRAM_SEAM,)
SNAPSHOT_OVERRIDE = "DSH_TEST_GATE_SNAPSHOT_LAUNCHCTL"

# 模块顶层 = 收集期。此刻没有任何夹具跑过，读到的只可能是 conftest 导入期设的值。
AT_IMPORT = {k: os.environ.get(k) for k in ALL_SEAMS}

HIT_TEXT = "这条用例碰了真实系统接缝"
TG_TOKEN = "123456789:test-AAaaBBccDDddEEeeFFggHHhh11"
SK_KEY = "sk-test-0000000000000000000000"


# ------------------------------------------------------------------ 小工具

def run_probe(target, *args, env=None, cwd=None):
    """起一个新的 pytest 进程跑探针，返回 (退出码, 合并后的输出)。

    子进程环境先抹掉所有接缝变量，等于从一个干净的 shell 启动：
    子进程里出现的毒丸只能是它自己的总闸设的，不是从本进程继承的。
    """
    child_env = dict(os.environ)
    for key in ALL_SEAMS + ("HUB_RESTART_CMD", SNAPSHOT_OVERRIDE, "PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        child_env.pop(key, None)
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env.update(env or {})
    cmd = [sys.executable, "-m", "pytest", str(PROBES / target), "-p", "no:cacheprovider"]
    cmd += list(args)
    r = subprocess.run(cmd, cwd=str(cwd or REPO_ROOT), env=child_env, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=180)
    return r.returncode, r.stdout.decode("utf-8", "replace")


def tally(out):
    """从 pytest 最后一行汇总里取各类计数，例如 {'failed': 1, 'passed': 2}。"""
    lines = [ln for ln in out.splitlines() if re.search(r"\bin \d+(\.\d+)?s\b", ln)]
    assert lines, "输出里没有 pytest 的汇总行：\n" + out
    return {kind: int(n) for n, kind in re.findall(
        r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed)", lines[-1])}


def make_exec(dirpath, name, pycode):
    """把一段 python 包成当前平台可以直接执行的文件。"""
    dirpath = Path(dirpath)
    dirpath.mkdir(parents=True, exist_ok=True)
    script = dirpath / (name + ".py")
    script.write_text(pycode, encoding="utf-8")
    if os.name == "nt":
        exe = dirpath / (name + ".cmd")
        exe.write_text('@"%s" "%s" %%*\r\n' % (sys.executable, script), encoding="utf-8")
    else:
        exe = dirpath / name
        exe.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, script),
                       encoding="utf-8")
        exe.chmod(0o755)
    return exe


def fake_list_program(tmp_path, outputs, exit_code=0):
    """假的只读 launchctl：第 N 次被调用时报出 outputs[N] 里的标签；只认 `list` 子命令。

    返回 (可执行文件路径, 调用计数文件)。被以别的子命令调用时退 64 且不计数。
    """
    counter = tmp_path / "list-calls.count"
    code = (
        "import json, os, sys\n"
        "counter = %r\n"
        "outputs = json.loads(%r)\n"
        "if sys.argv[1:] != ['list']:\n"
        "    sys.exit(64)\n"
        "n = int(open(counter).read()) if os.path.exists(counter) else 0\n"
        "open(counter, 'w').write(str(n + 1))\n"
        "print('PID\\tStatus\\tLabel')\n"
        "for label in outputs[min(n, len(outputs) - 1)]:\n"
        "    print('-\\t0\\t' + label)\n"
        "sys.exit(%d)\n"
    ) % (str(counter), json.dumps(outputs), exit_code)
    return make_exec(tmp_path / "fake-list", "launchctl", code), counter


@pytest.fixture
def spare_poison(total_gate, tmp_path):
    """用总闸同一个工厂在 tmp 里另造一颗毒丸。调它不会写会话级记录，自测不会因此被判失败。"""
    def _make(name="launchctl"):
        rec = tmp_path / (name + "-calls.rec")
        exe = total_gate.write_poison(str(tmp_path / "poison"), name, str(rec))
        return SimpleNamespace(exe=exe, rec=rec, read=lambda: total_gate.read_records(str(rec)))
    return _make


def _run(exe, *args, **kw):
    return subprocess.run([str(exe)] + list(args), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, **kw)


# ------------------------------------------------------------------ 导入期就生效

def test_导入期_HUB_LAUNCHCTL已是总闸的毒丸(total_gate):
    assert AT_IMPORT["HUB_LAUNCHCTL"] == total_gate.poison_launchctl


def test_导入期_HUB_BUN已是总闸的毒丸(total_gate):
    assert AT_IMPORT["HUB_BUN"] == total_gate.poison_bun


@pytest.mark.parametrize("seam", EXEC_SEAMS)
def test_导入期_毒丸是总闸目录下一个可执行文件(total_gate, seam):
    path = AT_IMPORT[seam]
    assert path.startswith(total_gate.dir + os.sep)
    assert os.path.isfile(path) and os.access(path, os.X_OK)


@pytest.mark.parametrize("seam", DIR_SEAMS)
def test_导入期_目录接缝已指到总闸目录之下(total_gate, seam):
    assert AT_IMPORT[seam].startswith(total_gate.dir + os.sep)


def test_导入期_Telegram接缝已是拒绝连接的回环地址():
    assert AT_IMPORT[TELEGRAM_SEAM] == "http://127.0.0.1:1"


def test_导入期_读到的值就是会话默认值(total_gate):
    assert AT_IMPORT == total_gate.session_env


def test_总闸目录_建在系统临时目录下(total_gate):
    gate = Path(total_gate.dir)
    assert gate.name.startswith("dsh-test-gate-")
    assert gate.parent == Path(os.path.realpath(tempfile.gettempdir()))


def test_子进程_仅收集不执行_导入期读到的也是毒丸与改道值(tmp_path):
    out_file = tmp_path / "import-time.json"
    code, out = run_probe("probe_import_time.py", "--collect-only",
                          env={"GATE_PROBE_OUT": str(out_file)})
    assert code == 0, out
    seen = json.loads(out_file.read_text(encoding="utf-8"))
    assert seen["executable"] == {"HUB_LAUNCHCTL": True, "HUB_BUN": True}
    assert seen["isdir"] == {k: True for k in DIR_SEAMS}
    for seam in EXEC_SEAMS + DIR_SEAMS:
        assert "dsh-test-gate-" in seen["env"][seam], seam
    assert seen["env"][TELEGRAM_SEAM] == "http://127.0.0.1:1"


def test_子进程_外部环境带着别的值_导入期一律被总闸盖掉(tmp_path):
    out_file = tmp_path / "import-time.json"
    inherited = {"HUB_LAUNCHCTL": "/nonexistent/inherited-launchctl",
                 "HUB_BUN": "/nonexistent/inherited-bun",
                 "DSH_BOT_HOME": "/nonexistent/inherited-dsh-home",
                 "HUB_TELEGRAM_API_BASE": "http://127.0.0.1:2/inherited"}
    code, out = run_probe("probe_import_time.py", "--collect-only",
                          env=dict(inherited, GATE_PROBE_OUT=str(out_file)))
    assert code == 0, out
    seen = json.loads(out_file.read_text(encoding="utf-8"))["env"]
    for key, value in inherited.items():
        assert seen[key] != value, "%s 还是外部继承来的值" % key
    assert "dsh-test-gate-" in seen["HUB_LAUNCHCTL"] and "dsh-test-gate-" in seen["HUB_BUN"]


def test_子进程_外部继承的HUB_RESTART_CMD_导入期被清掉(tmp_path):
    out_file = tmp_path / "import-time.json"
    code, out = run_probe("probe_import_time.py", "--collect-only",
                          env={"GATE_PROBE_OUT": str(out_file),
                               "HUB_RESTART_CMD": "/nonexistent/inherited-restart-cmd"})
    assert code == 0, out
    assert json.loads(out_file.read_text(encoding="utf-8"))["env"]["HUB_RESTART_CMD"] is None
    assert "已清掉从外部环境继承来的 HUB_RESTART_CMD" in out


# ------------------------------------------------------------------ 每条用例前重置

def test_用例期_HUB_LAUNCHCTL仍是毒丸(total_gate):
    assert os.environ["HUB_LAUNCHCTL"] == total_gate.poison_launchctl


def test_用例期_HUB_BUN仍是毒丸(total_gate):
    assert os.environ["HUB_BUN"] == total_gate.poison_bun


def test_用例期_目录接缝是本用例专属的一套空目录(total_gate):
    cases = os.path.join(total_gate.dir, "cases") + os.sep
    dsh_home = os.environ["DSH_BOT_HOME"]
    assert dsh_home.startswith(cases)
    case_dir = os.path.dirname(dsh_home)
    assert os.environ["HUB_CONFIGS_DSH_DIR"] == os.path.join(dsh_home, "configs")
    assert os.environ["HUB_LAUNCHAGENTS_DIR"] == os.path.join(case_dir, "LaunchAgents")
    assert os.environ["HUB_CHANNELS_DIR"] == os.path.join(case_dir, "channels")
    assert os.environ["HUB_CONFIGS_DIR"] == os.path.join(case_dir, "configs-legacy")
    assert os.listdir(dsh_home) == ["configs"]
    assert os.listdir(os.environ["HUB_CONFIGS_DSH_DIR"]) == []
    assert os.listdir(os.environ["HUB_LAUNCHAGENTS_DIR"]) == []
    assert os.listdir(os.environ["HUB_CHANNELS_DIR"]) == []
    assert os.listdir(os.environ["HUB_CONFIGS_DIR"]) == []


@pytest.mark.parametrize("seam", DIR_SEAMS)
def test_用例期_目录接缝不是会话共用的那套(total_gate, seam):
    assert os.environ[seam] != total_gate.session_env[seam]


def test_用例期_Telegram接缝仍是拒绝连接的回环地址():
    assert os.environ[TELEGRAM_SEAM] == "http://127.0.0.1:1"


def test_Telegram接缝_真去连会被拒绝(total_gate):
    url = urlparse(total_gate.refusing_telegram_base)
    assert url.hostname == "127.0.0.1"
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection((url.hostname, url.port), timeout=5)


def test_子进程_上一条用例泄漏了环境变量_下一条开始前已重置():
    code, out = run_probe("probe_leak_then_reset.py")
    assert code == 0, out
    assert tally(out) == {"passed": 2}


def test_子进程_模块级夹具改了执行接缝_用例开始前仍被重置回毒丸():
    code, out = run_probe("probe_leak_from_module_fixture.py")
    assert code == 0, out
    assert tally(out) == {"passed": 1}


def test_子进程_上一条用例泄漏之后才启动的模块级夹具_看到的已是毒丸():
    code, out = run_probe("probe_leak_seen_by_module_fixture.py")
    assert code == 0, out
    assert tally(out) == {"passed": 2}


def test_子进程_目录夹具换成假件后调用_不算碰闸():
    code, out = run_probe("probe_override_with_fake.py", "-s")
    assert code == 0, out
    assert tally(out) == {"passed": 1}
    assert "PROBE_FAKE_OK" in out
    assert HIT_TEXT not in out


# ------------------------------------------------------------------ 毒丸本身

def test_毒丸_被调用_以97退出(spare_poison):
    p = spare_poison()
    assert _run(p.exe, "bootstrap", "gui/501", "/x/com.dsh-bot.ghost.plist").returncode == 97


def test_毒丸_被调用_argv原样进记录(spare_poison):
    p = spare_poison()
    _run(p.exe, "bootstrap", "gui/501", "/x/com.dsh-bot.ghost.plist")
    assert p.read() == [["launchctl", "bootstrap", "gui/501", "/x/com.dsh-bot.ghost.plist"]]


def test_毒丸_被调用_标准输出为空_说明没转发给任何命令(spare_poison):
    p = spare_poison()
    r = _run(p.exe, "list")
    assert r.stdout == b""
    assert b"poison launchctl was called and refused" in r.stderr


def test_毒丸_不带参数调用_记录里只有名字(spare_poison):
    p = spare_poison()
    assert _run(p.exe).returncode == 97
    assert p.read() == [["launchctl"]]


@pytest.mark.skipif(os.name == "nt", reason="cmd 的参数转义规则不同，这组边界只在 POSIX 上验")
def test_毒丸_参数带空格中文引号和换行_原样记录(spare_poison):
    p = spare_poison()
    odd = ["a b", "中文 参数", "it's", 'say "hi"', "line1\nline2", "", "$HOME `id` \\n"]
    _run(p.exe, *odd)
    assert p.read() == [["launchctl"] + odd]


def test_毒丸_连调两次_按先后记两条(spare_poison):
    p = spare_poison()
    _run(p.exe, "print", "first")
    _run(p.exe, "kickstart", "second")
    assert p.read() == [["launchctl", "print", "first"], ["launchctl", "kickstart", "second"]]


@pytest.mark.skipif(os.name == "nt", reason="Windows 上空环境起不了 cmd")
def test_毒丸_子进程环境是空的_照样记录并以97退出(spare_poison):
    p = spare_poison()
    assert _run(p.exe, "print", "x", env={}).returncode == 97
    assert p.read() == [["launchctl", "print", "x"]]


def test_毒丸_bun那一颗_记录里的名字是bun(spare_poison):
    p = spare_poison("bun")
    _run(p.exe, "/repo/gateway/scripts/autostart.ts", "install")
    assert p.read() == [["bun", "/repo/gateway/scripts/autostart.ts", "install"]]


@pytest.mark.skipif(os.name == "nt", reason="Windows 上毒丸是 .cmd，全文不同")
@pytest.mark.parametrize("name,attr", [("launchctl", "poison_launchctl"), ("bun", "poison_bun")])
def test_会话级毒丸_与工厂造出来的一字不差(total_gate, name, attr):
    on_disk = Path(getattr(total_gate, attr)).read_text(encoding="utf-8")
    assert on_disk == total_gate.poison_script_text(name, total_gate.record_path)


def test_调用备用毒丸_会话级记录一条不多(total_gate, spare_poison):
    before = len(total_gate.records())
    _run(spare_poison().exe, "print", "x")
    assert len(total_gate.records()) == before


# ------------------------------------------------------------------ 每条用例后归因

def test_子进程_没换假件就调HUB_LAUNCHCTL的用例_被判失败():
    code, out = run_probe("probe_calls_launchctl.py")
    assert code == 1, out
    assert tally(out) == {"failed": 1}
    assert HIT_TEXT in out


def test_子进程_被判失败的信息里带着被拦下的argv():
    _, out = run_probe("probe_calls_launchctl.py")
    assert "1) launchctl print gui/501/com.dsh-bot.probe-ghost" in out


def test_子进程_毒丸确实以97返回给了调用方():
    # 探针体里 `assert r.returncode == 97` 是通过的：输出里没有它的断言失败，只有总闸的改判
    _, out = run_probe("probe_calls_launchctl.py")
    assert "PROBE_RC=97" in out
    assert "AssertionError" not in out


def test_子进程_没换假件就调HUB_BUN的用例_同样被判失败():
    code, out = run_probe("probe_calls_bun.py")
    assert code == 1, out
    assert tally(out) == {"failed": 1}
    assert HIT_TEXT in out
    assert "1) bun --version" in out


def test_子进程_不碰接缝的用例_照常通过():
    code, out = run_probe("probe_clean.py")
    assert code == 0, out
    assert tally(out) == {"passed": 1}
    assert HIT_TEXT not in out
    assert "毒丸被调用 0 次" in out


def test_子进程_argv里的密钥_在失败信息里被打码():
    code, out = run_probe("probe_token_in_argv.py")
    assert code == 1, out
    assert HIT_TEXT in out
    assert TG_TOKEN not in out and TG_TOKEN.split(":")[1] not in out
    assert SK_KEY not in out
    assert "TELEGRAM_BOT_TOKEN=***(已打码" in out
    assert "gui/501/com.dsh-bot.probe-ghost" in out     # 不是密钥的参数原样保留


def test_子进程_本来就红的用例碰了接缝_原失败保留且附上总闸信息():
    code, out = run_probe("probe_already_failing.py")
    assert code == 1, out
    assert tally(out) == {"failed": 1}
    assert "PROBE_ORIGINAL_FAILURE" in out
    assert HIT_TEXT in out


def test_子进程_夹具准备阶段碰了接缝_用例判失败():
    code, out = run_probe("probe_call_in_setup.py")
    assert code == 1, out
    assert tally(out) == {"failed": 1}
    assert "launchctl print gui/501/com.dsh-bot.probe-setup" in out


def test_子进程_夹具收尾阶段碰了接缝_整场不为零并报出来():
    code, out = run_probe("probe_call_in_teardown.py")
    assert code == 1, out
    assert tally(out) == {"passed": 1, "error": 1}
    assert "launchctl print gui/501/com.dsh-bot.probe-teardown" in out


STALE_TEXT = "前面某条用例留下的后台线程"
FORWARD_TEXT = "本目录的默认假件不答这个子命令"


@pytest.mark.skipif(os.name == "nt", reason="探针里的假件是 sh 脚本")
def test_子进程_假件已就位毒丸却被调用_失败信息列出假件转交与后台线程两种可能():
    code, out = run_probe("probe_stale_thread_call.py")
    assert code == 1, out
    assert tally(out) == {"failed": 1}
    assert HIT_TEXT in out
    assert STALE_TEXT in out
    assert FORWARD_TEXT in out
    assert "HUB_LAUNCHCTL 已经被本目录的夹具换成了别的值" in out
    assert "launchctl print gui/501/com.dsh-bot.probe-stale" in out


def test_子进程_没换假件的用例碰闸_不出现后台线程那段提示():
    _, out = run_probe("probe_calls_launchctl.py")
    assert STALE_TEXT not in out and FORWARD_TEXT not in out
    assert "把 HUB_LAUNCHCTL / HUB_BUN 换成假件" in out


def test_子进程_预期失败的用例碰了接缝_不许记成xfailed():
    code, out = run_probe("probe_xfail_and_skip.py::test_预期失败的用例碰了接缝")
    assert code == 1, out
    assert tally(out) == {"failed": 1}
    assert HIT_TEXT in out


def test_子进程_碰了接缝再跳过的用例_不许记成skipped():
    code, out = run_probe("probe_xfail_and_skip.py::test_碰了接缝之后跳过")
    assert code == 1, out
    assert tally(out) == {"failed": 1}
    assert HIT_TEXT in out


def test_子进程_收集导入期的调用_归不到用例_整场判失败():
    code, out = run_probe("probe_call_at_import.py")
    assert code == 1, out
    assert tally(out) == {"passed": 1}          # 用例本身是干净的，红的是整场
    assert "落在用例窗口之外" in out
    assert "launchctl print gui/501/com.dsh-bot.probe-import" in out


# ------------------------------------------------------------------ 打码

def test_打码_telegram_token_整串不留(total_gate):
    masked = total_gate.mask(TG_TOKEN)
    assert masked == "***(已打码,%d字符)" % len(TG_TOKEN)


def test_打码_键值形式的密钥_留键名打掉值(total_gate):
    assert total_gate.mask("HUB_ADMIN_PASSWORD=hunter2") == "HUB_ADMIN_PASSWORD=***(已打码,7字符)"


def test_打码_sk前缀的key(total_gate):
    assert total_gate.mask(SK_KEY) == "***(已打码,%d字符)" % len(SK_KEY)


def test_打码_32个字符的连续长串_打码(total_gate):
    blob = "a1" * 16
    assert total_gate.mask(blob) == "***(已打码,32字符)"


def test_打码_31个字符的连续串_不动(total_gate):
    blob = "a1" * 15 + "a"
    assert total_gate.mask(blob) == blob


@pytest.mark.parametrize("arg", [
    "print", "bootstrap", "kickstart", "-k", "gui/501",
    "gui/501/com.dsh-bot.xiaoyu",
    "/Users/someone/Library/LaunchAgents/com.dsh-bot.self-initiate.bot4.plist",
    "/repo/gateway/scripts/autostart.ts", "install-jobs", "--bot", "小雨",
])
def test_打码_launchctl与bun的常见参数_原样不动(total_gate, arg):
    assert total_gate.mask(arg) == arg


def test_排版_带空白的参数加引号_一眼看得出参数边界(total_gate):
    assert total_gate.format_call(["launchctl", "print", "a b", ""]) == "launchctl print 'a b' ''"


# ------------------------------------------------------------------ 会话级兜底

def test_子进程_会话结束时多出一个dsh标签_整场判失败并列出它(tmp_path):
    exe, counter = fake_list_program(tmp_path, [
        ["com.dsh-bot.bot4", "com.apple.other"],
        ["com.dsh-bot.bot4", "com.apple.other", "com.dsh-bot.ghost-from-test"],
    ])
    code, out = run_probe("probe_clean.py", env={SNAPSHOT_OVERRIDE: str(exe)})
    assert code == 1, out
    assert tally(out) == {"passed": 1}          # 用例都过了，红的是整场
    assert "多出了 1 个 com.dsh-bot.* 标签" in out
    assert "+ com.dsh-bot.ghost-from-test" in out


def test_子进程_会话兜底只用list子命令_且恰好开始结束各取一次(tmp_path):
    # 假程序被以 list 之外的任何子命令调用都会退 64 且不计数
    exe, counter = fake_list_program(tmp_path, [["com.dsh-bot.bot4"]])
    code, out = run_probe("probe_clean.py", env={SNAPSHOT_OVERRIDE: str(exe)})
    assert code == 0, out
    assert counter.read_text() == "2"
    assert "开始 1 个 / 结束 1 个，多出 0 个" in out


def test_子进程_前后标签一样_整场照常通过(tmp_path):
    exe, _ = fake_list_program(tmp_path, [["com.dsh-bot.bot4", "com.dsh-bot.jiwen"]])
    code, out = run_probe("probe_clean.py", env={SNAPSHOT_OVERRIDE: str(exe)})
    assert code == 0, out
    assert "多出了" not in out


def test_子进程_多出来的不是dsh前缀的标签_不算(tmp_path):
    exe, _ = fake_list_program(tmp_path, [
        ["com.dsh-bot.bot4"],
        ["com.dsh-bot.bot4", "com.apple.new", "com.wsxwj.other", "com.dsh-botx.lookalike"],
    ])
    code, out = run_probe("probe_clean.py", env={SNAPSHOT_OVERRIDE: str(exe)})
    assert code == 0, out
    assert "多出 0 个" in out


def test_子进程_会话期间少了一个dsh标签_只提醒不判失败(tmp_path):
    exe, _ = fake_list_program(tmp_path, [
        ["com.dsh-bot.bot4", "com.dsh-bot.jiwen"],
        ["com.dsh-bot.bot4"],
    ])
    code, out = run_probe("probe_clean.py", env={SNAPSHOT_OVERRIDE: str(exe)})
    assert code == 0, out
    assert "会话期间少了这些标签" in out and "com.dsh-bot.jiwen" in out


def test_子进程_机器上没有launchctl_兜底跳过_其余照常(tmp_path):
    code, out = run_probe("probe_calls_launchctl.py",
                          env={SNAPSHOT_OVERRIDE: str(tmp_path / "no-such-launchctl")})
    assert "launchd 兜底：跳过（这台机器没有 launchctl）" in out
    assert code == 1 and HIT_TEXT in out        # 毒丸与归因不受影响


def test_子进程_机器上没有launchctl_干净的用例整场为零(tmp_path):
    code, out = run_probe("probe_clean.py",
                          env={SNAPSHOT_OVERRIDE: str(tmp_path / "no-such-launchctl")})
    assert code == 0, out
    assert tally(out) == {"passed": 1}


def test_子进程_launchctl_list自己报错_兜底跳过不误判(tmp_path):
    exe, _ = fake_list_program(tmp_path, [["com.dsh-bot.bot4"], ["com.dsh-bot.bot4", "com.dsh-bot.x"]],
                               exit_code=1)
    code, out = run_probe("probe_clean.py", env={SNAPSHOT_OVERRIDE: str(exe)})
    assert code == 0, out
    assert "launchd 兜底：跳过（launchctl list 没取到结果）" in out


# ------------------------------------------------------------------ 从哪儿启动

def test_子进程_从仓库根启动_总闸已加载():
    code, out = run_probe("probe_clean.py", cwd=REPO_ROOT)
    assert code == 0, out
    assert "测试总闸：已生效" in out


def test_子进程_从tests目录启动_总闸已加载():
    code, out = run_probe("probe_clean.py", cwd=TESTS_DIR)
    assert code == 0, out
    assert "测试总闸：已生效" in out
