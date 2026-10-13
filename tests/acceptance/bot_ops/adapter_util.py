# -*- coding: utf-8 -*-
"""适配服务（`moments.adapter_service`）验收的辅助件：假数据、假网关、发请求、通用断言。

依据只有三份文件：`.devflow/BRIEF-适配服务.md`、`INTERFACE-适配约定-v0.3.md`、
`INTERFACE-适配服务.md`。不读实现。

三份文件之外、从本目录现成支架（conftest.py 的 gw_bot / heartbeat / FakeLaunchctl）
借来的东西只有"怎么摆一只假 bot"：配置文件的形状、网关端口与口令放在哪、心跳怎么写。
下面这些是接口文件没写、只能猜着放的假数据，放错位置只会让"搜不到"的断言空转，不会误红：
Telegram 假令牌与假聊天号（放在频道目录的 .env）、供应商假密钥（放在环境变量）。

被测模块的导入放在 `AdapterEnv.client` 里（第一次发请求时），不放文件顶部。
"""
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
#: 真实家目录下那个中立位置。只看它在不在、里面有哪些名字，绝不读写。导入期（HOME 还没被换掉）定住。
REAL_CONSOLE_DIR = Path(os.path.expanduser("~")) / ".config" / "claude-dsh-console"

# ---------------------------------------------------------------- 路径
PREFIX = "/adapter/v1"
META = PREFIX + "/meta"
BOTS = PREFIX + "/bots"
PROVIDERS = PREFIX + "/providers"


def provider_url(bot_id):
    return "%s/%s/provider" % (BOTS, bot_id)


def restart_url(bot_id):
    return "%s/%s/restart" % (BOTS, bot_id)


# ---------------------------------------------------------------- 假值（全是编的）
ADAPTER_TOKEN = "FAKE-ADAPTER-TOKEN-do-not-leak-7f3a9c1e5b2d4860"
OTHER_TOKEN = "FAKE-ADAPTER-TOKEN-rotated-0b1c2d3e4f5a6978"
WRONG_TOKEN = "FAKE-WRONG-TOKEN-never-valid-a1b2c3d4e5f60718"
GW_KEY = "FAKE-GATEWAY-KEY-do-not-leak-51c0ffee51c0ffee"
TG_TOKEN = "700000001:FAKE-TG-TOKEN-do-not-leak-AAbbCCddEEff"
PROVIDER_KEY = "sk-FAKE-PROVIDER-KEY-do-not-leak-9a8b7c6d5e4f"
CHAT_ID = "1009876543210777"

#: 任何响应里都不许出现的东西。前两个一定会被服务读到；后三个的摆放位置是猜的（见文件头）。
SECRETS = {
    "适配器令牌": ADAPTER_TOKEN,
    "网关口令": GW_KEY,
    "Telegram 令牌": TG_TOKEN,
    "供应商密钥": PROVIDER_KEY,
    "聊天号": CHAT_ID,
}

#: 假 bot 配置文件里写的那一组（与 conftest.py 的 gw_bot 相同）
CFG_PROVIDER = "deepseek-official"
CFG_MODEL = "deepseek-flash"

# ---------------------------------------------------------------- 约定里的固定取值
UNAUTHORIZED = {"ok": False, "code": "unauthorized", "message": "令牌缺失或不对"}
EFFORT_LEVELS = ["off", "low", "high", "max"]
CAPABILITIES = {
    "bot.restart.graceful": False,
    "bot.restart.force": True,
    "bot.set_provider": True,
    "bot.reset_provider": False,
    "bot.set_effort": True,
    "provider.subscription": False,
    "provider.per_session": False,
    "provider.switch_new_segment": True,
}
BOT_KEYS = {"bot_id", "display_name", "state", "state_text", "provider", "provider_source",
            "pending", "detail"}
BOT_STATES = {"running", "stopped", "broken"}
DEFAULT_LABEL = "各 bot 配置文件里写的那一组"

GOOD = object()       # call(auth=GOOD)：带对的令牌
NO_BODY = object()    # call(body=NO_BODY)：不带请求体

#: 五个接口各一个"本来合规"的请求：(方法, 路径, 请求体)
FIVE = [
    ("GET", META, NO_BODY),
    ("GET", BOTS, NO_BODY),
    ("GET", PROVIDERS, NO_BODY),
    ("POST", provider_url("botalpha"), {"effort": "high"}),
    ("POST", restart_url("botalpha"), {"force": True}),
]


def free_port():
    """一个此刻没人监听的回环端口。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------- 通用断言
def body_of(resp):
    """补充文件第 3 节：所有响应都是 JSON 对象，Content-Type 是 application/json。"""
    raw = resp.get_data(as_text=True)
    assert resp.mimetype == "application/json", \
        "响应不是 application/json：状态 %s，Content-Type=%r，开头 %r" % (
            resp.status_code, resp.headers.get("Content-Type"), raw[:120])
    try:
        body = json.loads(raw)
    except ValueError:
        raise AssertionError("响应体不是 JSON：状态 %s，开头 %r" % (resp.status_code, raw[:120]))
    assert isinstance(body, dict), "响应体不是 JSON 对象：%r" % (body,)
    return body


def assert_unauthorized(resp):
    """没过鉴权：401，响应体与固定的那一个一字不差。"""
    body = body_of(resp)
    assert resp.status_code == 401, "应为 401，实际 %s：%r" % (resp.status_code, body)
    assert body == UNAUTHORIZED, "401 的响应体应固定不变，实际 %r" % (body,)


def response_text(resp):
    """响应体加响应头拼成一段文字，用来搜敏感内容。"""
    head = "\n".join("%s: %s" % (k, v) for k, v in resp.headers.items())
    return resp.get_data(as_text=True) + "\n" + head


# ---------------------------------------------------------------- 记录型假网关
class RecordingGateway:
    """只绑 127.0.0.1 的假网关：记下收到的每个请求（任何方法），一律回 404。

    适配服务与网关之间怎么说话不在三份接口文件里，所以这个假件不扮演"会成功的网关"，
    只用来回答两个问题：有没有请求打过来、打过来的是不是写请求。
    """

    def __init__(self):
        self.requests = []
        self._srv = None

    def start(self):
        gw = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _any(self):
                n = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(n) if n else b""
                gw.requests.append({"method": self.command, "path": self.path.split("?")[0],
                                    "body": raw.decode("utf-8", "replace")})
                out = b'{"error": "not found"}'
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(out)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _any

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._srv.daemon_threads = True
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        return self

    def stop(self):
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None

    @property
    def port(self):
        return self._srv.server_address[1]

    def writes(self):
        """会改东西的请求（读以外的方法）。"""
        return [r for r in self.requests if r["method"] not in ("GET", "HEAD", "OPTIONS")]


# ---------------------------------------------------------------- 测试环境句柄
class AdapterEnv:
    """一条用例的全部家当：发请求、摆假 bot、起假网关、看假的启动项管理程序。"""

    def __init__(self, sandbox, token_file, launchctl, heartbeat):
        self.sandbox = sandbox
        self.token_file = token_file
        self.launchctl = launchctl
        self.heartbeat = heartbeat
        self._client = None
        self._gateways = []

    @property
    def client(self):
        if self._client is None:
            import importlib
            mod = importlib.import_module("moments.adapter_service")   # 还没实现时在这里红
            self._client = mod.create_app().test_client()
        return self._client

    def call(self, method, path, body=NO_BODY, *, auth=GOOD, raw=None):
        """发一个请求。

        auth：GOOD 带对的令牌；None 不带 Authorization 头；别的字符串原样当头的值。
        body：按 JSON 发；raw 给了就原样当请求体发（Content-Type 仍是 application/json）。
        """
        headers = {}
        if auth is GOOD:
            headers["Authorization"] = "Bearer " + ADAPTER_TOKEN
        elif auth is not None:
            headers["Authorization"] = auth
        kw = {"method": method, "headers": headers}
        if raw is not None:
            kw["data"] = raw
            kw["content_type"] = "application/json"
        elif body is not NO_BODY:
            kw["json"] = body
        return self.client.open(path, **kw)

    # -- 假数据 ----------------------------------------------------------
    def bot(self, bot_id, *, enabled=True, gw_port=None, display_name=None):
        """在临时配置目录里摆一只假 bot（形状照 conftest.py 的 gw_bot）。

        gw_port 给了才有网关端口文件；不给就是"没有可连的网关"。
        """
        botdir = self.sandbox.dsh_home / "bots" / bot_id
        state = botdir / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "api.key").write_text(GW_KEY + "\n", encoding="utf-8")
        if gw_port is not None:
            (state / "api.port").write_text("%d\n" % gw_port, encoding="utf-8")
        channel = botdir / "channel"
        channel.mkdir(exist_ok=True)
        (channel / ".env").write_text(
            "TELEGRAM_BOT_TOKEN=%s\nTELEGRAM_CHAT_ID=-%s\n" % (TG_TOKEN, CHAT_ID), encoding="utf-8")
        cfg = {"id": bot_id, "display_name": display_name or bot_id,
               "bot_channel_path": str(channel), "dispatcher_port": 0,
               "brain": {"provider": CFG_PROVIDER, "model": CFG_MODEL}}
        if not enabled:
            cfg["enabled"] = False
        yml = self.sandbox.dsh_cfg / ("%s.yml" % bot_id)
        yml.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        return SimpleNamespace(id=bot_id, yml=yml, botdir=botdir)

    def gateway(self):
        gw = RecordingGateway().start()
        self._gateways.append(gw)
        return gw

    def close(self):
        for gw in self._gateways:
            gw.stop()

    # -- 断言 ------------------------------------------------------------
    def ok(self, resp):
        """成功：200，JSON，ok 为 true。返回响应体。"""
        body = body_of(resp)
        assert resp.status_code == 200, "应为 200，实际 %s：%r" % (resp.status_code, body)
        assert body.get("ok") is True, "成功响应必有 ok: true，实际 %r" % (body,)
        return body

    def expect_error(self, resp, status, code):
        """失败：状态码、code 对得上，message 是一句给人看的话。返回响应体。"""
        body = body_of(resp)
        assert resp.status_code == status and body.get("code") == code, \
            "应为 %d %s，实际 %d %r（%r）" % (status, code, resp.status_code, body.get("code"), body)
        assert body.get("ok") is False, "失败响应必有 ok: false，实际 %r" % (body,)
        self.human_message(body.get("message"))
        return body

    def human_message(self, msg):
        """message：非空、有中文、不带堆栈、不带文件路径、不带密钥。"""
        assert isinstance(msg, str) and msg.strip(), "message 应是非空的一句话，实际 %r" % (msg,)
        assert re.search(u"[一-鿿]", msg), "message 应是给人看的中文，实际 %r" % (msg,)
        assert "Traceback" not in msg, "message 里带了堆栈：%r" % (msg,)
        tmp = str(self.sandbox.tmp)
        for path in {tmp, os.path.realpath(tmp)}:
            assert path not in msg, "message 里带了文件路径：%r" % (msg,)
        for name, secret in SECRETS.items():
            assert secret not in msg, "message 里出现了%s" % name

    def first_provider(self):
        """从供应商摘要里拿第一家（调用方就是这么拿下拉选项的）。"""
        providers = self.ok(self.call("GET", PROVIDERS)).get("providers")
        if not isinstance(providers, list) or not providers:
            pytest.fail("用例前提不成立：供应商摘要是空的，拿不到一家现成的供应商（应至少有内置供应商）")
        return providers[0]


def _console_snapshot():
    try:
        return sorted(p.name for p in REAL_CONSOLE_DIR.iterdir())
    except OSError:
        return None


@pytest.fixture
def console_guard():
    """反向断言：真实家目录的 ~/.config/claude-dsh-console 前后一个名字都没多没少。"""
    before = _console_snapshot()
    yield
    assert _console_snapshot() == before, "用例动到了真实家目录下的 .config/claude-dsh-console"


@pytest.fixture
def adapter(sandbox, tmp_path, monkeypatch, launchctl, heartbeat, as_darwin, console_guard):
    """适配服务的测试环境。

    - 令牌文件经 DSH_CONSOLE_TOKEN_FILE 指到临时目录；
    - 假的启动项管理程序已装上（conftest.py 的 launchctl 夹具），任何调用都会被记下；
    - 假家目录、临时配置目录由 conftest.py 的 sandbox（自动生效）设好，HOME 与 USERPROFILE 都换了。
    """
    assert os.environ["HOME"] == str(sandbox.home) == os.environ["USERPROFILE"]
    token_dir = tmp_path / "console-token"
    token_dir.mkdir(mode=0o700)
    token_file = token_dir / "adapter.token"
    token_file.write_text(ADAPTER_TOKEN, encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setenv("DSH_CONSOLE_TOKEN_FILE", str(token_file))
    monkeypatch.delenv("DSH_CONSOLE_HOST", raising=False)
    monkeypatch.delenv("DSH_CONSOLE_PORT", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", PROVIDER_KEY)
    env = AdapterEnv(sandbox, token_file, launchctl, heartbeat)
    yield env
    env.close()


# ---------------------------------------------------------------- 命令行启动
def _group_alive(proc):
    """子进程所在的进程组里还有没有活着的（POSIX）。Windows 上只看它自己。"""
    if os.name == "nt":
        return proc.poll() is None
    try:
        os.killpg(proc.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill_group(proc):
    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def run_cli(env_over, timeout=20.0):
    """在仓库根跑 `python -m moments.adapter_service`，等它自己退出。

    环境是当前（已被 sandbox 换好的）环境的完整副本，再叠上 env_over：
    HUB_LAUNCHCTL、HUB_BUN 原样带过去，绝不摘掉。超时就整组杀掉。
    返回 code / output / timed_out / leftover（退出后进程组里是否还有活的）。
    """
    env = dict(os.environ)
    assert env.get("HUB_LAUNCHCTL") and env.get("HUB_BUN"), "两个执行接缝必须带给子进程"
    env.update(env_over)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    kw = {} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen([sys.executable, "-m", "moments.adapter_service"],
                            cwd=str(REPO_ROOT), env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        out, err = proc.communicate()
    leftover = _group_alive(proc)
    if leftover:
        _kill_group(proc)
    text = (out + b"\n" + err).decode("utf-8", "replace").strip()
    return SimpleNamespace(code=proc.returncode, output=text, timed_out=timed_out,
                           leftover=leftover)
