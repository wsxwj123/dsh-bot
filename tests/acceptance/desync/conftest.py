"""desync 验收：夹具注册 + 生产护栏。

- 进程入口即设 CLAUDEBOTLIFE_TEST=1 / CLAUDEBOTLIFE_TEST_ROOT / HOME（§11.1）；iso 夹具再按用例覆盖到 tmp_path。
- 会话前后被动记录生产状态（tmux tg-* 会话数、17801-17804 是否在听，用 lsof 只读，不连接）；不一致 → 整批判失败。
"""


def _require_total_gate():
    """自检：tests/conftest.py 的总闸没加载，就拒绝运行本目录的用例。

    pytest 只加载「启动目录及其下」的 conftest。cd 到本目录（或它的某个上级子目录）里面
    再起 pytest，总闸不会被加载：HUB_LAUNCHCTL / HUB_BUN 不是毒丸，用例可能碰到真实的
    launchd。总闸导入时在 sys 上留了带本进程 pid 的标记（tests/conftest.py 的
    _STATE_ATTR），这里只认它。自测在 tests/gate_selftest/。
    """
    import os
    import sys
    gate = getattr(sys, "_dsh_bot_test_gate", None)
    if isinstance(gate, dict) and gate.get("pid") == os.getpid():
        return
    here = os.path.dirname(os.path.abspath(__file__))
    tests = here
    while os.path.basename(tests) != "tests" and os.path.dirname(tests) != tests:
        tests = os.path.dirname(tests)
    root = os.path.dirname(tests)
    raise RuntimeError(
        "测试总闸（tests/conftest.py）没有加载，拒绝运行本目录的用例。\n"
        "在子目录里面启动 pytest 时总闸不生效，用例可能碰到真实的 launchd。\n"
        "请从仓库根或 tests/ 目录启动，例如：\n"
        "    cd %s && python3 -m pytest %s" % (root, os.path.relpath(here, root)))


_require_total_gate()
del _require_total_gate

import atexit
import importlib
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_ROOT = tempfile.mkdtemp(prefix="desync-session-")
atexit.register(shutil.rmtree, _ROOT, ignore_errors=True)  # --co 等不跑夹具的场合也清理
os.environ["CLAUDEBOTLIFE_TEST"] = "1"
os.environ["CLAUDEBOTLIFE_TEST_ROOT"] = _ROOT
os.makedirs(os.path.join(_ROOT, "home"), exist_ok=True)
os.environ["HOME"] = os.path.join(_ROOT, "home")
os.environ["USERPROFILE"] = os.path.join(_ROOT, "home")   # Windows 上 expanduser 读它
# 兜底：db.DB_PATH 是导入期读 BOTLIFE_STATE_DB 定死的；任何早于夹具的 import
# （收集期等）都不许落到仓库根的 state.db。逐用例再收紧到 tmp_path，见 _botlife_state_db。
os.environ["BOTLIFE_STATE_DB"] = os.path.join(_ROOT, "state.db")

_TMUX = shutil.which("tmux")
_LSOF = shutil.which("lsof")

# ---- 会话级网络安全网（§11.2(3) 之外的纵深）：socket 层拦截出站连接，覆盖 urllib/requests/http.client 等一切走 socket 的库 ----
# 放行：回环地址上的非生产端口（测试进程内的假服务）、测试根之下的 unix socket。
# 拦截并抛 RuntimeError：回环地址的生产端口（dispatcher 17801-17804 / 语音桥 7788 / 管理台 8765 / cliproxy 8317 / 代理 7897 与系统代理端口）、任何非回环地址。
# 被测代码吞掉异常也逃不掉：每次拦截记入 NET_BLOCKS，该用例在 call 阶段强制失败，会话结束逐条打印。
# 人工跑外网实验：CLAUDEBOTLIFE_NET_GUARD=0 关闭（默认开启）。必须装在 import _helpers/director 之前。
NET_GUARD = os.environ.get("CLAUDEBOTLIFE_NET_GUARD", "1") != "0"
# 代理端口也拦（INTERFACE §11.2(3) 的 7897 + scutil --proxy 报告的系统代理端口）：回环上的代理能转发到任何地方，等于外网/生产的后门；
# 同时清掉 *_proxy 环境变量并设 no_proxy=*，让 urllib/requests 不再自动走系统代理。
NET_BLOCKED_PORTS = {17801, 17802, 17803, 17804, 7788, 8765, 8317, 7897}
try:
    NET_BLOCKED_PORTS |= {int(p) for p in re.findall(r"Port\s*:\s*(\d+)", subprocess.run(
        ["/usr/sbin/scutil", "--proxy"], capture_output=True, text=True, timeout=5).stdout)}
except Exception:  # scutil 不存在/超时：只用静态表
    pass
for _k in list(os.environ):
    if _k.lower().endswith("_proxy"):
        del os.environ[_k]
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "*"
NET_BLOCKS: list = []  # [(用例 nodeid, 被拦目标)]


def _net_check(addr):
    if isinstance(addr, (str, bytes, os.PathLike)):  # AF_UNIX
        path = os.fsdecode(addr)
        if path.startswith(_ROOT):
            return
        why = f"unix socket {path!r}"
    elif isinstance(addr, tuple) and len(addr) >= 2:
        host, port = str(addr[0]), addr[1]
        try:
            ip = ipaddress.ip_address(host.split("%")[0])
            loop = (getattr(ip, "ipv4_mapped", None) or ip).is_loopback or ip.is_unspecified
        except ValueError:
            loop = host.lower() in ("localhost", "")
        if loop and port not in NET_BLOCKED_PORTS:
            return
        why = f"{host}:{port}（{'生产/代理端口' if loop else '非回环地址'}）"
    else:
        return
    NET_BLOCKS.append((os.environ.get("PYTEST_CURRENT_TEST", "?"), why))
    raise RuntimeError(f"test_mode: net_guard 拦截出站连接 {why}；人工外网实验请设 CLAUDEBOTLIFE_NET_GUARD=0")


def _net_guard(name, real=None):
    real = real or getattr(socket.socket, name)

    def f(self, *a, **k):
        _net_check(a[-1] if name == "sendto" else a[0])  # sendto(data[, flags], addr)：地址在最后
        return real(self, *a, **k)
    f.__name__ = name
    return f


if NET_GUARD:
    for _n in ("connect", "connect_ex", "sendto"):
        setattr(socket.socket, _n, _net_guard(_n))


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    item._net_n = len(NET_BLOCKS)


@pytest.hookimpl(trylast=True)
def pytest_runtest_call(item):
    blocks = NET_BLOCKS[getattr(item, "_net_n", 0):]
    if blocks:
        pytest.fail("net_guard 拦截（被测代码吞掉了异常）: " + "; ".join(w for _, w in blocks), pytrace=False)


def pytest_sessionfinish(session, exitstatus):
    print(f"\nnet_guard={'on' if NET_GUARD else 'OFF'}  net_blocks={len(NET_BLOCKS)}")
    for _t, _w in NET_BLOCKS:
        print(f"  net_guard 拦截: {_t} -> {_w}")


from _helpers import iso, registry, director  # noqa: E402,F401  夹具注册


def _prod_state():
    n = -1
    if _TMUX:
        r = subprocess.run([_TMUX, "ls", "-F", "#S"], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
        n = sum(1 for l in r.stdout.splitlines() if l.startswith("tg-"))
    listening = ()
    if _LSOF:
        r = subprocess.run([_LSOF, "-nP", "-iTCP", "-sTCP:LISTEN"], capture_output=True, text=True)
        listening = tuple(p for p in (17801, 17802, 17803, 17804) if f":{p} (LISTEN)" in r.stdout)
    return n, listening


@pytest.fixture(scope="session", autouse=True)
def _production_guard():
    before = _prod_state()
    yield
    after = _prod_state()
    shutil.rmtree(_ROOT, ignore_errors=True)
    assert before == after, f"生产状态在测试期间改变: before={before} after={after}"


@pytest.fixture(autouse=True)
def _botlife_state_db(tmp_path, monkeypatch):
    """朋友圈库钉在用例 tmp：`db.DB_PATH` 是导入期读 `BOTLIFE_STATE_DB` 定死的。

    不钉住就会往仓库根的 state.db 写东西（发生过一次）。db 若已被前一条用例
    导入，模块级 DB_PATH 停在旧值，必须 reload 让它跟着 env 走，并当场断言它
    真在 tmp。子进程继承 os.environ，setenv 同样盖住 subprocess 里的 db 调用。
    """
    want = (tmp_path / "state.db").resolve()
    monkeypatch.setenv("BOTLIFE_STATE_DB", str(want))
    mod = sys.modules.get("db")
    if mod is None:
        import db as mod                          # import 期读 env，落当前 tmp
    else:
        mod = importlib.reload(mod)
    got = Path(mod.DB_PATH).resolve()
    assert got == want, (
        "朋友圈库没钉在 tmp：DB_PATH=%s（应 %s）—— db.DB_PATH 是导入期定死的，"
        "不跟着 env 重载就会写到仓库根的 state.db" % (got, want))
    return mod
