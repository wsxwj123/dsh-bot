# 验收测试专用桩（只用于 tests/acceptance/forced_early_initiate，经 PYTHONPATH 带进每个 Python 子进程）：
#   1. 拒绝一切域名解析和 AF_INET / AF_INET6 的连接与发送（含 127.0.0.1），不分 TCP/UDP；
#   2. 掷骰子可控：所有掷骰都经仓库根的 rng 模块，这里整体替换；
#   3. 可选：记下打开过的、路径里含指定子串的文件（审计钩子，事件 open / sqlite3.connect）。
#
# 标记文件（TEST_STUB_MARK）里每行一条：
#   loaded <pid>              该进程加载了本桩，说明断网在生效
#   blocked <what> <addr>     被 Python 层拦下的一次网络尝试
#   allowed <event> <addr>    绕过了 Python 层、靠审计钩子兜底拦下的尝试（正常情况下一条都不该有）
import os
import socket
import sys

_mark = os.environ.get("TEST_STUB_MARK")


def _note(what):
    if _mark:
        fd = os.open(_mark, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, ("%s\n" % what).encode("utf-8", "replace"))
        finally:
            os.close(fd)


_note("loaded %d" % os.getpid())


def _deny_gai(host, *a, **k):
    _note("blocked getaddrinfo %s" % (host,))
    raise socket.gaierror("test-offline: resolve %s" % (host,))


def _deny_resolve(name, *a, **k):
    _note("blocked resolve %s" % (name,))
    raise socket.gaierror("test-offline: resolve %s" % (name,))


socket.getaddrinfo = _deny_gai
socket.gethostbyname = _deny_resolve
socket.gethostbyname_ex = _deny_resolve
socket.gethostbyaddr = _deny_resolve
socket.getnameinfo = _deny_resolve

_INET = (socket.AF_INET, socket.AF_INET6)
_c, _cx, _st, _sm = (socket.socket.connect, socket.socket.connect_ex,
                     socket.socket.sendto, socket.socket.sendmsg)


def _deny_connect(self, addr):
    if self.family in _INET:
        _note("blocked connect %s" % (addr,))
        raise OSError("test-offline: connect %s" % (addr,))
    return _c(self, addr)


def _deny_connect_ex(self, addr):
    if self.family in _INET:
        _note("blocked connect_ex %s" % (addr,))
        raise OSError("test-offline: connect_ex %s" % (addr,))
    return _cx(self, addr)


def _deny_sendto(self, *a):
    if self.family in _INET:
        _note("blocked sendto")
        raise OSError("test-offline: sendto")
    return _st(self, *a)


def _deny_sendmsg(self, *a):
    if self.family in _INET:
        _note("blocked sendmsg")
        raise OSError("test-offline: sendmsg")
    return _sm(self, *a)


socket.socket.connect = _deny_connect
socket.socket.connect_ex = _deny_connect_ex
socket.socket.sendto = _deny_sendto
socket.socket.sendmsg = _deny_sendmsg


def _net_hook(event, args):
    # 兜底：Python 层的替换没拦住、却走到了底层的网络调用——记一笔"allowed"并直接拦掉
    if event in ("socket.connect", "socket.sendto", "socket.sendmsg"):
        try:
            family = args[0].family
        except Exception:
            return
        if family in _INET:
            _note("allowed %s %s" % (event, args[1] if len(args) > 1 else ""))
            raise OSError("test-offline: audit %s" % event)
    elif event in ("socket.getaddrinfo", "socket.gethostbyname",
                   "socket.gethostbyaddr", "socket.getnameinfo"):
        _note("allowed %s %s" % (event, args[0] if args else ""))
        raise socket.gaierror("test-offline: audit %s" % event)


sys.addaudithook(_net_hook)

_mode = os.environ.get("TEST_RNG", "threshold")
if os.environ.get("TEST_REPO"):
    sys.path.append(os.environ["TEST_REPO"])
    import rng
    # threshold：只有 p>=1.0 的才算中；always：一律中。uniform 取下限，choice 取第一个。
    rng.chance = (lambda p: p >= 1.0) if _mode == "threshold" else (lambda p: True)
    rng.uniform = lambda lo, hi: lo
    rng.choice = lambda seq: seq[0] if seq else None

_log, _watch = os.environ.get("TEST_OPEN_LOG"), os.environ.get("TEST_WATCH")
if _log and _watch:
    _fd = os.open(_log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)

    def _open_hook(event, args):
        if event in ("open", "sqlite3.connect") and args and isinstance(args[0], (str, bytes, os.PathLike)):
            s = os.fsdecode(args[0])
            if _watch in s:
                os.write(_fd, ("%s %s\n" % (event, s)).encode("utf-8", "replace"))

    sys.addaudithook(_open_hook)
