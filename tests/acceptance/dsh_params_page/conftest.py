# -*- coding: utf-8 -*-
"""dsh 参数页验收的公共件：起真实适配服务、发原始 HTTP 请求、摆配置文件。

依据只有 `.devflow/BRIEF-dsh-params-page.md` 与 `.devflow/INTERFACE-dsh-params-page.md`。
被测对象是 `python3 -m moments.adapter_service` 这层对外接口（127.0.0.1 上的临时端口），
本目录不 import、不 mock 被测模块内部，不去动真实的 ~/.dsh-bot 与 17990 端口。

每个用例一套全新的临时根：
- 临时令牌文件（DSH_CONSOLE_TOKEN_FILE）；
- HUB_CONFIGS_DSH_DIR 指向临时目录，`_global.yml` 与备份都在这里；
- HUB_CONFIGS_DIR 指向一个故意不创建的临时子路径（确认不会回落到 claude 侧配置）；
- 临时 HOME / DSH_BOT_HOME / HUB_CHANNELS_DIR / HUB_LAUNCHAGENTS_DIR，防止碰到真实用户资产。
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

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

#: 临时令牌；任何响应、日志里都不许出现。
TOKEN = "TESTTOKEN-dsh-params-7f3a9c1e5b2d4860-do-not-leak"

NO_AUTH = object()      # request(auth=NO_AUTH)：用令牌文件里的当前内容
NO_BODY = object()      # request(body=NO_BODY)：不带请求体


class Response(object):
    """一次 HTTP 交换的结果；headers 的键统一小写。"""

    def __init__(self, status, headers, body):
        self.status = status
        self.headers = {str(k).lower(): v for k, v in dict(headers).items()}
        self.body = body
        self.text = body.decode("utf-8", "replace")

    @property
    def json(self):
        try:
            return json.loads(self.body)
        except ValueError:
            return None


class Service(object):
    """一条用例专属的适配服务进程，以及访问它的最小客户端。"""

    def __init__(self, port, token_path, config_dir, proc, log_path, log_file):
        self.port = port
        self.token_path = token_path
        self.config_dir = config_dir
        self.proc = proc
        self.log_path = log_path
        self._log_file = log_file

    def read_token(self):
        """令牌文件每次请求现读（接口约定：轮换即时生效）。"""
        return self.token_path.read_text(encoding="utf-8").strip()

    def log_text(self):
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def request(self, method, path, body=NO_BODY, raw=None, auth=NO_AUTH,
                headers=None, timeout=15):
        hdrs = {}
        if auth is NO_AUTH:
            hdrs["Authorization"] = "Bearer " + self.read_token()
        elif auth is not None:
            hdrs["Authorization"] = auth
        data = None
        if raw is not None:
            data = raw.encode("utf-8") if isinstance(raw, str) else raw
            hdrs["Content-Type"] = "application/json"
        elif body is not NO_BODY:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        if headers:
            hdrs.update(headers)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            conn.request(method, path, body=data, headers=hdrs)
            resp = conn.getresponse()
            return Response(resp.status, resp.getheaders(), resp.read())
        finally:
            conn.close()

    def raw_exchange(self, method, path, body=b"", auth=NO_AUTH, headers=None,
                     declared_length=None, read_timeout=10):
        """不经 http.client 的原始交换：用于「头部声明很大/只发一部分」的场景。

        服务端可能没读完请求体就回包并断开，发送端拿到 BrokenPipe 属正常，
        只要响应本身读得到就算数。
        """
        hdrs = {
            "Host": "127.0.0.1:%d" % self.port,
            "Connection": "close",
            "Content-Length": str(len(body) if declared_length is None else declared_length),
            "Content-Type": "application/json",
        }
        if auth is NO_AUTH:
            hdrs["Authorization"] = "Bearer " + self.read_token()
        elif auth is not None:
            hdrs["Authorization"] = auth
        if headers:
            hdrs.update(headers)
        head = "%s %s HTTP/1.1\r\n" % (method, path)
        head += "".join("%s: %s\r\n" % (k, v) for k, v in hdrs.items())
        head += "\r\n"
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        sock.settimeout(read_timeout)
        try:
            try:
                sock.sendall(head.encode("latin-1") + body)
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                # 服务端可能没读请求体就回包并断开，这属于被测行为的一部分。
                pass
            chunks = []
            while True:
                try:
                    chunk = sock.recv(65536)
                except (socket.timeout, ConnectionResetError):
                    break
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            sock.close()
        raw = b"".join(chunks)
        head_bytes, _, resp_body = raw.partition(b"\r\n\r\n")
        lines = head_bytes.decode("latin-1").split("\r\n")
        if len(lines) < 2 or not lines[0].strip():
            raise AssertionError("服务没有回任何 HTTP 响应（原始字节 %r）" % raw[:200])
        status = int(lines[0].split(" ")[1])
        resp_headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                resp_headers[key.strip().lower()] = value.strip()
        return Response(status, resp_headers, resp_body)

    def stop(self):
        if self.proc.poll() is None:
            try:
                if os.name == "nt":
                    self.proc.terminate()
                else:
                    os.killpg(self.proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    if os.name == "nt":
                        self.proc.kill()
                    else:
                        os.killpg(self.proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                self.proc.wait(timeout=5)
        self._log_file.close()


def _free_port():
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _wait_until_serving(service):
    deadline = time.time() + 20
    last_error = None
    while time.time() < deadline:
        if service.proc.poll() is not None:
            raise AssertionError(
                "适配服务没起来就退出了（退出码 %r），日志：\n%s"
                % (service.proc.returncode, service.log_text()[-3000:]))
        try:
            service.request("GET", "/hub/params", timeout=1)
            return
        except OSError as exc:      # 还没监听
            last_error = exc
            time.sleep(0.1)
    raise AssertionError("适配服务 20 秒内没有开始监听：%r\n日志：\n%s"
                         % (last_error, service.log_text()[-3000:]))


@pytest.fixture
def service(tmp_path):
    """起一个真实适配服务；每个用例一套独立临时根，结束即杀进程。"""
    port = _free_port()
    home = tmp_path / "home"
    home.mkdir()
    config_dir = tmp_path / "dsh-configs"
    config_dir.mkdir()
    legacy_dir = tmp_path / "legacy-configs-does-not-exist"   # 故意不创建
    token_dir = tmp_path / "console-token"
    token_dir.mkdir(mode=0o700)
    token_path = token_dir / "adapter.token"
    token_path.write_text(TOKEN, encoding="utf-8")
    os.chmod(token_path, 0o600)
    log_path = tmp_path / "adapter-service.log"
    log_file = open(log_path, "wb")

    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "USERPROFILE": str(home),
        "DSH_BOT_HOME": str(home / ".dsh-bot"),
        "HUB_CONFIGS_DSH_DIR": str(config_dir),
        "HUB_CONFIGS_DIR": str(legacy_dir),
        "HUB_CHANNELS_DIR": str(tmp_path / "channels"),
        "HUB_LAUNCHAGENTS_DIR": str(tmp_path / "launchagents"),
        "DSH_CONSOLE_HOST": "127.0.0.1",
        "DSH_CONSOLE_PORT": str(port),
        "DSH_CONSOLE_TOKEN_FILE": str(token_path),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    })
    proc = subprocess.Popen(
        [sys.executable, "-m", "moments.adapter_service"],
        cwd=str(REPO_ROOT),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=(os.name != "nt"),
    )
    svc = Service(port, token_path, config_dir, proc, log_path, log_file)
    try:
        _wait_until_serving(svc)
    except Exception:
        svc.stop()
        raise
    try:
        yield svc
    finally:
        svc.stop()
