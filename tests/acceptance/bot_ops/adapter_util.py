# -*- coding: utf-8 -*-
"""适配服务（`moments.adapter_service`）验收的辅助件：假数据、假网关、发请求、通用断言。

依据只有三份文件：`.devflow/BRIEF-适配服务.md`、`INTERFACE-适配约定-v0.3.md`、
`INTERFACE-适配服务.md`。不读实现。

三份文件之外、从本目录现成支架（conftest.py 的 gw_bot / heartbeat / FakeLaunchctl）
借来的东西只有"怎么摆一只假 bot"：配置文件的形状、网关端口与口令放在哪、心跳怎么写。
假网关 `GET /v1/model` 的返回体照补充文件 5.1 那份"照真实网关核对过"的样例：顶层 current、config、
choices，providers 每项字段齐全（models 是个数、key 是 ok/missing/none/unknown 之一、带 keyEnv）。
另外三种请求只用 5.2 表格里写明的字段。
下面这些是接口文件没写、只能猜着放的假数据，放错位置只会让"搜不到"的断言空转，不会误红：
Telegram 假令牌与假聊天号（放在频道目录的 .env）、供应商假密钥（放在环境变量；真实网关的返回里没有密钥原文）。

被测模块的导入放在 `AdapterEnv.client` 里（第一次发请求时），不放文件顶部。
"""
import copy
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
CFG_PAIR = (CFG_PROVIDER, CFG_MODEL)

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


# ---------------------------------------------------------------- 假网关
#: 补充文件第 5 节：适配服务对网关只发这两条路径上的读与写
GW_MODEL = "/v1/model"
GW_EFFORT = "/v1/effort"

#: 假网关清单里的三家（补充文件 5.1 的形状：模型名只在顶层 choices 里，providers[].models 只是个数）。
#: 模型的先后故意不是字典序，choices 里各家的条目也故意交错：摘要要按供应商归组、保持 choices 里的先后。
VENDOR_A = CFG_PROVIDER                      # 配置文件里写的那一家；key 为 ok
VENDOR_A_MODELS = [CFG_MODEL, "deepseek-pro-fake", "deepseek-alpha-fake"]
VENDOR_B = "fake-vendor-b"                   # 另一家；key 为 missing；个数给 null
VENDOR_B_MODELS = ["vb-small", "vb-large"]
VENDOR_OFF = "fake-vendor-off"               # 网关那边标了停用；key 为 ok，choices 里也有它的模型
VENDOR_OFF_MODELS = ["off-model-zz7"]
#: 5.1 说这五个键都不往外带
GW_ONLY_KEYS = ("keyEnv", "api", "source", "note", "last_refresh")
#: 醒目的假值：keyEnv 里的假变量名、note 里夹带的假接口地址。要验的是它们不出现在适配服务的响应里。
FAKE_ENDPOINT_HOST = "fake-endpoint-do-not-leak.invalid"
FAKE_KEY_ENVS = ["FAKEVENDORB_DO_NOT_LEAK_API_KEY", "FAKEVENDOROFF_DO_NOT_LEAK_API_KEY"]

NO_RUNNING_BOT = "没有正在运行的 bot，暂时读不到供应商清单"     # 补充文件 4.3
INTERNAL_MESSAGE = "内部错误"                                  # 补充文件第 3 节

DEFAULT = object()    # model_reply 的参数：用下面的默认假数据
OMIT = object()       # model_reply 的参数：返回体里干脆不放这个键


def provider_entry(name, *, count, key, key_env=None, enabled=True, source="builtin",
                   api="openai-completions", note=None, last_refresh=None):
    """`GET /v1/model` 里 providers 的一项，字段照 5.1 的样例齐全。count 是模型个数（数字或 None）。"""
    return {"name": name, "source": source, "api": api, "models": count, "key": key,
            "keyEnv": key_env, "enabled": enabled, "note": note, "last_refresh": last_refresh}


def gateway_providers():
    """假网关 `GET /v1/model` 里的 providers。每次新造一份，用例之间不共用。"""
    return [
        provider_entry(VENDOR_A, count=len(VENDOR_A_MODELS), key="ok"),
        provider_entry(VENDOR_B, count=None, key="missing", key_env=FAKE_KEY_ENVS[0],
                       source="custom", api="anthropic-messages",
                       note="走镜像 https://%s/b/v1" % FAKE_ENDPOINT_HOST,
                       last_refresh={"at": 0, "ok": True, "count": len(VENDOR_B_MODELS), "reason": None}),
        provider_entry(VENDOR_OFF, count=len(VENDOR_OFF_MODELS), key="ok", key_env=FAKE_KEY_ENVS[1],
                       enabled=False, source="custom"),
    ]


def gateway_choices():
    """假网关 `GET /v1/model` 里的 choices：各家条目交错排列，停用那家的模型也在里面。"""
    a, b, off = VENDOR_A_MODELS, VENDOR_B_MODELS, VENDOR_OFF_MODELS
    order = [(VENDOR_A, a[0]), (VENDOR_B, b[0]), (VENDOR_A, a[1]), (VENDOR_OFF, off[0]),
             (VENDOR_B, b[1]), (VENDOR_A, a[2])]
    return [{"provider": p, "model": m} for p, m in order]


def _pair(pair):
    return {"provider": pair[0], "model": pair[1]}


def model_reply(current=CFG_PAIR, config=CFG_PAIR, choices=DEFAULT, providers=DEFAULT):
    """拼一份 `GET /v1/model` 的返回体（补充文件 5.1 的形状）。

    current / config 给 (供应商, 模型)；config、choices、providers 给 OMIT 就不放这个键；
    choices、providers 给别的值（含 None）就原样放进去。
    """
    reply = {"ok": True, "current": _pair(current)}
    if config is not OMIT:
        reply["config"] = _pair(config)
    for key, value, make_default in (("choices", choices, gateway_choices),
                                     ("providers", providers, gateway_providers)):
        if value is DEFAULT:
            value = make_default()
        if value is not OMIT:
            reply[key] = value
    return reply


class RecordingGateway:
    """只绑 127.0.0.1 的假网关：记下收到的每个请求（任何方法）；没配应答的一律回 404。

    `on` / `on_fn` 给某个"方法 + 路径"配应答；`serve_model` 一次配齐补充文件第 5 节那四种请求。
    每条记录：method、path、body（原文）、json（解析出来的对象，解析不了为 None）。
    """

    def __init__(self):
        self.requests = []
        self.routes = {}
        self.state = {}
        self._srv = None

    def on(self, method, path, status, payload):
        """payload 是 str / bytes 就原样发（text/plain），别的按 JSON 发。"""
        self.routes[(method, path)] = lambda rec: (status, payload)
        return self

    def on_fn(self, method, path, fn):
        self.routes[(method, path)] = fn
        return self

    def serve_model(self, reply, effort=None):
        """照第 5 节应答四种请求。`GET /v1/model` 回 reply（见 model_reply）。

        换供应商只改 reply 里的 current（config 是配置文件里写的，不跟着变）；改强度记在
        self.state 里。下一次读就能读到。
        """
        st = self.state
        st["reply"] = copy.deepcopy(reply)
        st["effort"] = effort

        def get_model(rec):
            return 200, copy.deepcopy(st["reply"])

        def post_model(rec):
            spec = rec["json"].get("spec") if isinstance(rec["json"], dict) else None
            if not isinstance(spec, str) or "/" not in spec:
                return 400, {"ok": False}
            provider, model = spec.split("/", 1)
            st["reply"]["current"] = {"provider": provider, "model": model}
            return 200, {"ok": True, "current": dict(st["reply"]["current"])}

        def get_effort(rec):
            return 200, {"ok": True, "current": st["effort"]}

        def post_effort(rec):
            st["effort"] = rec["json"].get("effort") if isinstance(rec["json"], dict) else None
            return 200, {"ok": True}

        self.on_fn("GET", GW_MODEL, get_model).on_fn("POST", GW_MODEL, post_model)
        self.on_fn("GET", GW_EFFORT, get_effort).on_fn("POST", GW_EFFORT, post_effort)
        return self

    def start(self):
        gw = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _any(self):
                n = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(n) if n else b""
                text = raw.decode("utf-8", "replace")
                try:
                    parsed = json.loads(text) if text else None
                except ValueError:
                    parsed = None
                rec = {"method": self.command, "path": self.path.split("?")[0],
                       "body": text, "json": parsed}
                gw.requests.append(rec)
                handler = gw.routes.get((rec["method"], rec["path"]))
                status, payload = handler(rec) if handler else (404, {"error": "not found"})
                if isinstance(payload, (bytes, str)):
                    out = payload if isinstance(payload, bytes) else payload.encode("utf-8")
                    ctype = "text/plain; charset=utf-8"
                else:
                    out = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    ctype = "application/json"
                self.send_response(status)
                self.send_header("Content-Type", ctype)
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
        """只记不答的假网关（没配应答的请求一律回 404）。"""
        gw = RecordingGateway().start()
        self._gateways.append(gw)
        return gw

    def model_gateway(self, effort=None, **reply):
        """会应答的假网关：照补充文件第 5 节答四种请求。

        reply 原样交给 model_reply：current、config 不给就是配置文件里写的那一组，choices、providers
        不给就是上面那三家。effort 是单独指定的思考强度，不给就是没有单独指定（读到 null）。
        """
        return self.gateway().serve_model(model_reply(**reply), effort)

    def running_bot(self, bot_id, gw, **kw):
        """一只正在运行的假 bot：网关端口指向 gw，心跳一直在刷新。"""
        bot = self.bot(bot_id, gw_port=gw.port, **kw)
        self.heartbeat.tick(bot_id, pid=4242)
        return bot

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


# ---------------------------------------------------------------- v0.6、v0.7（补充文件第 9 节）
# 依据：INTERFACE-适配服务.md 第 9 节、INTERFACE-适配约定-v0.7摘录.md。下面是第 9 节几份用例共用的小件。
GW_HEALTH = "/v1/health"      # 9.4、9.5：网关健康接口，返回里有 telegram.conflict、telegram.username
GW_INJECT = "/v1/inject"      # 6.2：投递通知

#: 9.1：v0.4 的十个键，加 bot.enable、bot.switch_in、bot.group_chat，都报 true，共十三个键
CAPABILITIES_V07 = dict(CAPABILITIES, **{
    "bot.notify": True, "bot.life_settings": True,
    "bot.enable": True, "bot.switch_in": True, "bot.group_chat": True})

#: 约定 5.12.1：群设置的六个键
GROUP_KEYS = ("requireMention", "allowFrom", "selfAliases", "otherBotUsernames",
              "otherBotAliases", "intro")


def enabled_url(bot_id):
    return "%s/%s/enabled" % (BOTS, bot_id)


def groups_url(bot_id):
    return "%s/%s/groups" % (BOTS, bot_id)


def notify_url(bot_id):
    return "%s/%s/notify" % (BOTS, bot_id)


def serve_health(gw, conflict=False, username=None):
    """让假网关应答 GET /v1/health。接口文件只写了 telegram.conflict 与 telegram.username。"""
    return gw.on("GET", GW_HEALTH, 200,
                 {"ok": True, "telegram": {"conflict": conflict, "username": username}})


def set_cfg(bot, **fields):
    """改假 bot 配置文件的顶层键；值给 None 就删掉这个键（服务改写过的 YAML 也能读）。"""
    cfg = read_cfg(bot)
    for k, v in fields.items():
        if v is None:
            cfg.pop(k, None)
        else:
            cfg[k] = v
    bot.yml.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")


def read_cfg(bot):
    """读回配置（服务可能写成 YAML；JSON 是 YAML 的子集，两种都能读）。"""
    import yaml
    return yaml.safe_load(bot.yml.read_text(encoding="utf-8")) or {}


# -- 启动项文件（照管理台 §5c 的说法：装着 = <名>.plist，收起 = <名>.plist.disabled）
def unit_labels(bot_id):
    """主单元与主动消息定时单元。"""
    return ("com.dsh-bot.%s" % bot_id, "com.dsh-bot.self-initiate.%s" % bot_id)


def put_plists(sandbox, bot_id, stashed=False):
    for label in unit_labels(bot_id):
        name = label + (".plist.disabled" if stashed else ".plist")
        (sandbox.agents / name).write_text(
            '<?xml version="1.0"?>\n<plist><!-- %s --></plist>\n' % label, encoding="utf-8")


def plist_state(sandbox, label):
    on = (sandbox.agents / (label + ".plist")).exists()
    off = (sandbox.agents / (label + ".plist.disabled")).exists()
    return {(True, False): "装着", (False, True): "收起", (False, False): "没装"}.get((on, off), "两份都在")


def print_text(label, running):
    """假 launchctl 的 print 原文（照 fakes/fake_launchctl.py 的两种写法）。"""
    if running:
        return ("%s = {\n\tactive count = 1\n\tstate = running\n\tpid = 4242\n"
                "\tpath = /tmp/fake.plist\n}\n" % label)
    return "%s = {\n\tstate = not running\n\tpath = /tmp/fake.plist\n}\n" % label


# -- access.json（9.5：这只 bot 频道目录下的那一份）
def access_path(bot):
    return bot.botdir / "channel" / "access.json"


def write_access(bot, data, mode=0o600):
    p = access_path(bot)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    p.chmod(mode)
    return p


def read_access(bot):
    return json.loads(access_path(bot).read_text(encoding="utf-8"))


class InjectInbox:
    """假网关 POST /v1/inject 的应答（同 test_adapter_v04_notify 的 Inbox）：按 key 前 120 个字符去重。"""

    def __init__(self):
        self.seen = set()
        self.delivered = []

    def __call__(self, rec):
        body = rec["json"]
        if not isinstance(body, dict) or not isinstance(body.get("key"), str):
            return 400, {"ok": False}
        k = body["key"][:120]
        if k in self.seen:
            return 200, {"ok": True, "duplicate": True}
        self.seen.add(k)
        self.delivered.append(body)
        return 200, {"ok": True, "duplicate": False}


# ---------------------------------------------------------------- v0.8（补充文件第 10 节）
# 依据：INTERFACE-适配服务.md 10.1 到 10.3；INTERFACE-适配约定-v0.8-dsh侧.md A、B。下面是第 10 节几份用例共用的小件。

#: 10.1：第 9 节的十三个键，加 bot.persona、bot.voice，都报 true，共十五个键
CAPABILITIES_V08 = dict(CAPABILITIES_V07, **{"bot.persona": True, "bot.voice": True})

#: v0.8 草案 B.2：tts_model 的白名单
TTS_MODELS = ("s1", "s2-pro", "s2.1-pro", "s2.1-pro-free", "drama-3-preview")
PERSONA_SAVED = "已保存，几秒内生效"                 # A.2 成功
PERSONA_SAME = "没有改动"                            # A.2 和当前全文完全相同
PERSONA_STALE = "人设刚被别处改过，请刷新后再改"     # A.2 409 stale
PERSONA_LIMIT = 256 * 1024                           # A.2：text 按 UTF-8 字节数的上限
PAST = 1600000000    # 2020-09：先把文件修改时间拨到过去，"没写过"才看得出来


def persona_url(bot_id):
    return "%s/%s/persona" % (BOTS, bot_id)


def voice_url(bot_id):
    return "%s/%s/voice" % (BOTS, bot_id)


def persona_version(data):
    """A.1：原始字节的 SHA-256 取前 16 位十六进制。str 按 UTF-8 编码；文件不存在就传 b""。"""
    import hashlib
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:16]


def channel_of(bot):
    """adapter.bot 摆出来的频道目录（配置里的 bot_channel_path）。"""
    return bot.botdir / "channel"


def persona_path(bot):
    """A.1：人设是这只 bot 频道目录下的 CLAUDE.md。"""
    return channel_of(bot) / "CLAUDE.md"


def persona_backups(sandbox, bot_id):
    """10.2：<DSH_BOT_HOME>/backups/persona/<bot_id>/ 下的文件，按名字排好；目录不在就是空列表。"""
    d = sandbox.dsh_home / "backups" / "persona" / bot_id
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.is_file())


def put_file(path, data, mode=0o600):
    """写一个文件（str 按 UTF-8），设好权限，修改时间拨到过去。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    p.chmod(mode)
    os.utime(str(p), (PAST, PAST))
    return p


def file_mark(path):
    """"一个字节都没变"的指纹：是否软链、字节、修改时间、权限、inode。文件不在就是 None。"""
    p = Path(path)
    if not os.path.lexists(str(p)):
        return None
    st = os.lstat(str(p))
    data = None if p.is_symlink() else p.read_bytes()
    return (p.is_symlink(), data, st.st_mtime_ns, st.st_mode & 0o7777, st.st_ino)


def mode_of(path):
    return os.stat(str(path)).st_mode & 0o7777
