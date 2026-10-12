# -*- coding: utf-8 -*-
"""新系统 bot 运维（重启与新建）验收夹具（黑盒）。

只依据 `.devflow/BRIEF-新系统bot运维.md` 与 `INTERFACE-新系统bot运维.md` 编写，
不读实现代码。实现未落地时本目录整体红 —— 那是红基线，不是夹具坏了。

硬隔离：
1. HOME / DSH_BOT_HOME / HUB_CONFIGS_* / HUB_CHANNELS_DIR / HUB_LAUNCHAGENTS_DIR 全在 tmp；
2. `HUB_LAUNCHCTL` / `HUB_BUN` 默认指向"毒丸"（执行即退 97），用例要具体行为必须显式
   换成假件 —— 绝不许真调 launchctl / bun；
3. `HUB_TELEGRAM_API_BASE` 强制回环，任何用例都不可能打到 api.telegram.org；
4. autouse 反向断言：跑完真实 `~/Library/LaunchAgents`、`~/.dsh-bot`、
   `~/claudebotlife/configs`、`~/.claude/channels`、仓库 `configs/` 的顶层条目一字未变。

平台判定没有契约接缝（§1 无平台变量）：本目录用 `force_platform` 把
`sys.platform` 与 `platform.system()` 双桩钉住，让"macOS 分支"与"非 macOS 分支"
在任何平台上都得到同一结果。实现若用第三种判据（例如 import 期求值的常量），
平台敏感用例会误红 —— 这是契约含糊点，已在测试报告里点名。
"""
import importlib
import json
import os
import platform as _platform
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

from . import botops_util as u

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FAKES = HERE / "fakes"
REAL_HOME = Path(os.path.expanduser("~"))       # 在 monkeypatch HOME 之前定住
REPO_CONFIGS = REPO_ROOT / "configs"

FAKE_TOKEN = u.FAKE_TOKEN
FAKE_TOKEN_SAME_HEAD = u.FAKE_TOKEN_SAME_HEAD
FAKE_TOKEN_OTHER_HEAD = u.FAKE_TOKEN_OTHER_HEAD


# ------------------------------------------------------------ 通用小件

def make_stub(dirpath, name, script, args=()):
    """把 python 脚本包成当前平台可执行的假件，返回可执行文件路径。

    POSIX：`#!/bin/sh` shim（exec 解释器 + 脚本 + 烘焙参数 + "$@"）；
    Windows：`.cmd` shim（@"解释器" "脚本" 烘焙参数 %*）。
    假件的配置/日志路径烘焙在 shim 里，不依赖子进程 env —— 实现可能不给子进程
    传完整环境，经 env 传配置会假红。
    """
    d = Path(dirpath)
    d.mkdir(parents=True, exist_ok=True)
    quoted = " ".join('"%s"' % str(a) for a in args)
    if os.name == "nt":
        p = d / (name + ".cmd")
        p.write_text('@"%s" "%s" %s %%*\r\n' % (sys.executable, script, quoted),
                     encoding="utf-8")
    else:
        p = d / name
        p.write_text('#!/bin/sh\nexec "%s" "%s" %s "$@"\n' % (sys.executable, script, quoted),
                     encoding="utf-8")
        p.chmod(0o755)
    return p


def _write_poison(dirpath, name):
    """毒丸假件：任何调用都退出 97。用例忘了注入假件时行为立刻不对，而不是真去调系统。"""
    d = Path(dirpath)
    d.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        p = d / (name + ".cmd")
        p.write_text("@exit 97\r\n", encoding="utf-8")
    else:
        p = d / name
        p.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
        p.chmod(0o755)
    return p


class FakeLaunchctl:
    """假 launchctl：状态机 + 调用记录（见 fakes/fake_launchctl.py 的头部注释）。"""

    def __init__(self, tmp_path):
        self.dir = Path(tmp_path) / "fake-launchctl"
        self.cfg_path = self.dir / "cfg.json"
        self.log_path = self.dir / "calls.jsonl"
        self.cfg = {"default": {"installed": True, "running": True, "pid": 4242},
                    "units": {}, "delay": {}, "pid": 4242}
        self._dump()
        self.path = make_stub(self.dir, "launchctl", FAKES / "fake_launchctl.py",
                              ["--cfg", str(self.cfg_path), "--log", str(self.log_path)])

    # -- 安装/读写 -------------------------------------------------------
    def install(self, monkeypatch):
        monkeypatch.setenv("HUB_LAUNCHCTL", str(self.path))
        return self

    def _reload(self):
        try:
            self.cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

    def _dump(self):
        self.cfg_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg_path.write_text(json.dumps(self.cfg, ensure_ascii=False), encoding="utf-8")

    def _set(self, **kw):
        self._reload()
        self.cfg.update(kw)
        self._dump()
        return self

    # -- 场景设置 --------------------------------------------------------
    def set_default(self, **kw):
        """未见过的单元（label）的初始状态。"""
        self._reload()
        self.cfg["default"] = kw
        self._dump()
        return self

    def set_unit(self, bot, **kw):
        """按 bot id 设某一只的单元状态：installed / running / pid。"""
        self._reload()
        self.cfg.setdefault("units", {})["com.dsh-bot.%s" % bot] = kw
        self._dump()
        return self

    def set_pid(self, pid):
        return self._set(pid=pid)

    def set_delay(self, cmd, seconds):
        self._reload()
        self.cfg.setdefault("delay", {})[cmd] = seconds
        self._dump()
        return self

    def set_print_text(self, text):
        return self._set(print_text=text)

    def set_kickstart_missing(self, flag=True):
        return self._set(kickstart_missing=bool(flag))

    def set_kickstart_pid(self, pid):
        """kickstart 成功后单元改报这个 pid（缺省沿用 cfg 的 pid，行为不变）。"""
        return self._set(kickstart_pid=pid)

    def set_bootstrap_fail(self, flag=True):
        return self._set(bootstrap_fail=bool(flag))

    # -- 读记录 ----------------------------------------------------------
    def calls(self):
        if not self.log_path.exists():
            return []
        out = []
        for ln in self.log_path.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln:
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    pass
        return out

    def subcommands(self):
        return [c.get("cmd") for c in self.calls()]

    def labels(self):
        return [c.get("label") for c in self.calls()]

    def state_of(self, bot):
        self._reload()
        label = "com.dsh-bot.%s" % bot
        units = self.cfg.get("units") or {}
        return dict(units.get(label) or self.cfg.get("default") or {})


class FakeBun:
    """假 bun：按序消费响应 + 记录 argv/关键 env。"""

    def __init__(self, tmp_path):
        self.dir = Path(tmp_path) / "fake-bun"
        self.cfg_path = self.dir / "cfg.json"
        self.log_path = self.dir / "calls.jsonl"
        self.cfg = {"responses": [], "default": {"exit": 0, "out": "", "err": ""}}
        self._dump()
        self.path = make_stub(self.dir, "bun", FAKES / "fake_bun.py",
                              ["--cfg", str(self.cfg_path), "--log", str(self.log_path)])

    def install(self, monkeypatch):
        monkeypatch.setenv("HUB_BUN", str(self.path))
        return self

    def _dump(self):
        self.cfg_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg_path.write_text(json.dumps(self.cfg, ensure_ascii=False), encoding="utf-8")

    def _reload(self):
        try:
            self.cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

    def expect(self, exit=0, out="", err=""):
        """追加一条响应（按调用顺序消费）。"""
        self._reload()
        self.cfg.setdefault("responses", []).append({"exit": exit, "out": out, "err": err})
        self._dump()
        return self

    def calls(self):
        if not self.log_path.exists():
            return []
        out = []
        for ln in self.log_path.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln:
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    pass
        return out


class StubTelegram:
    """最小 getMe 桩：只认 `GET /bot<token>/getMe`。

    `bot_id` 是 getMe 返回值里的 id —— 真实世界里 token 的数字段就是这个 id，
    token 占用判定也按它比对；用例需要时显式改。
    """

    def __init__(self, mode="ok", bot_id=123456789):
        self.mode = mode          # ok | 401
        self.bot_id = bot_id
        self.hits = []
        self._srv = None
        self._thread = None

    def start(self):
        stub = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                raw = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                stub.hits.append(("POST", self.path))
                self._send(405, {"ok": False, "description": "stub: method not allowed"})

            def do_GET(self):
                stub.hits.append(("GET", self.path))
                path = self.path.split("?")[0]
                if stub.mode == "ok" and path.startswith("/bot") and path.endswith("/getMe"):
                    self._send(200, {"ok": True, "result": {"id": stub.bot_id, "is_bot": True,
                                                            "username": "my_bot",
                                                            "first_name": "my_bot"}})
                else:
                    self._send(401, {"ok": False, "error_code": 401,
                                     "description": "Unauthorized"})

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._srv.daemon_threads = True
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None

    @property
    def port(self):
        return self._srv.server_address[1]

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.port


class DispatcherStub:
    """本机假 dispatcher：记录任何被访问的请求。

    dsh 行按契约不发本机探活（§2）；若实现发了，这里会留下 hits。
    """

    def __init__(self):
        self.hits = []
        self._srv = None

    def start(self):
        stub = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _any(self):
                stub.hits.append((self.command, self.path))
                raw = json.dumps({"phase": "ready", "pid": 999, "queue_depth": 0,
                                  "in_flight": 0}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_POST = _any
            do_GET = _any

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


class _HeartbeatTicker:
    """持续刷新心跳的线程（模拟真网关一直在写心跳）。"""

    def __init__(self, path, pid):
        self.path = Path(path)
        self.pid = pid
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            u.write_heartbeat(self.path, self.pid)
            self._stop.wait(0.05)

    def start(self):
        self._t.start()
        return self

    def stop(self):
        self._stop.set()


# ------------------------------------------------------------ 平台桩

def force_platform(monkeypatch, name):
    """把平台判定钉住（契约 §1 没有平台接缝，按最常见的两种判据双保险）。"""
    if name == "darwin":
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(_platform, "system", lambda: "Darwin")
        if not hasattr(os, "getuid"):       # Windows 上给实现补上 Unix 前提
            monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
    elif name == "linux":
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(_platform, "system", lambda: "Linux")
    else:
        raise ValueError("不支持的平台桩值：%r" % name)
    return name


@pytest.fixture
def as_darwin(monkeypatch):
    return force_platform(monkeypatch, "darwin")


@pytest.fixture
def as_linux(monkeypatch):
    return force_platform(monkeypatch, "linux")


# ------------------------------------------------------------ 隔离

@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """一切路径/接缝钉在 tmp；毒丸盯住 launchctl 与 bun。"""
    home = tmp_path / "fakehome"
    (home / ".claude" / "channels").mkdir(parents=True)
    dsh_home = tmp_path / "dsh-home"
    dsh_home.mkdir()
    dsh_cfg = tmp_path / "configs-dsh"
    legacy_cfg = tmp_path / "configs-legacy"
    channels = tmp_path / "channels-legacy"
    agents = tmp_path / "LaunchAgents"
    for d in (dsh_cfg, legacy_cfg, channels, agents):
        d.mkdir()
    for k in ("HUB_BOTS_FILE", "HUB_RESTART_CMD", "HUB_RESTART_SCRIPT",
              "HUB_ACCESS_PASSWORD", "HUB_ADMIN_PASSWORD", "MOMENTS_WEB_PORT",
              "ACCESS_PASSWORD", "CLIPROXY_MGMT_KEY", "CLIPROXY_PORT", "CLIPROXY_API_KEY",
              "CLIPROXY_ANTHROPIC_COMPAT", "HUB_ALLOW_PRIVATE_BASE_URL",
              "http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY",
              "ALL_PROXY", "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))     # Windows 上 expanduser 看它
    monkeypatch.setenv("DSH_BOT_HOME", str(dsh_home))
    # 朋友圈库（第 4 稿编辑端点会写 bot_profiles）。db.DB_PATH 是导入期读
    # BOTLIFE_STATE_DB 定死的：不在这里钉住，编辑用例会写到仓库根的 state.db。
    # 已导入过的模块要重载，见 botlife_db 夹具。
    monkeypatch.setenv("BOTLIFE_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(legacy_cfg))
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(dsh_cfg))
    monkeypatch.setenv("HUB_CHANNELS_DIR", str(channels))
    monkeypatch.setenv("HUB_LAUNCHAGENTS_DIR", str(agents))
    monkeypatch.setenv("HUB_LAUNCHCTL", str(_write_poison(tmp_path / "poison", "launchctl")))
    monkeypatch.setenv("HUB_BUN", str(_write_poison(tmp_path / "poison", "bun")))
    monkeypatch.setenv("HUB_TELEGRAM_API_BASE", "http://127.0.0.1:%d" % u.free_port())
    monkeypatch.setenv("MOMENTS_WEB_HOST", "127.0.0.1")
    assert str(dsh_home).startswith(str(tmp_path)), "DSH_BOT_HOME 没钉在 tmp"
    assert str(dsh_cfg).startswith(str(tmp_path)), "HUB_CONFIGS_DSH_DIR 没钉在 tmp"
    return SimpleNamespace(tmp=tmp_path, home=home, dsh_home=dsh_home, dsh_cfg=dsh_cfg,
                           legacy_cfg=legacy_cfg, channels=channels, agents=agents)


def _names(d):
    d = Path(d)
    if not d.exists():
        return None
    try:
        return {p.name: p.is_dir() for p in d.iterdir()}
    except OSError:
        return None


@pytest.fixture(autouse=True)
def _no_touch_real():
    """反向断言：跑完任何用例，真实目录的顶层条目必须一字未变。"""
    watched = [REAL_HOME / "Library" / "LaunchAgents",
               REAL_HOME / ".dsh-bot",
               REAL_HOME / "claudebotlife" / "configs",
               REAL_HOME / ".claude" / "channels",
               REPO_CONFIGS]
    before = [_names(d) for d in watched]
    yield
    after = [_names(d) for d in watched]
    for d, b, a in zip(watched, before, after):
        assert b == a, "用例动到了真实目录 %s 的条目（接缝漏了）" % d


# ------------------------------------------------------------ 假件/桩

@pytest.fixture
def launchctl(tmp_path, monkeypatch):
    return FakeLaunchctl(tmp_path).install(monkeypatch)


@pytest.fixture
def bun(tmp_path, monkeypatch):
    return FakeBun(tmp_path).install(monkeypatch)


@pytest.fixture
def telegram(monkeypatch):
    """getMe 桩。取用它即把 HUB_TELEGRAM_API_BASE 指到桩上（绝不发真请求）。"""
    t = StubTelegram().start()
    monkeypatch.setenv("HUB_TELEGRAM_API_BASE", t.base)
    try:
        yield t
    finally:
        t.stop()


@pytest.fixture
def dispatcher():
    d = DispatcherStub().start()
    try:
        yield d
    finally:
        d.stop()


@pytest.fixture
def heartbeat(sandbox):
    """心跳工具：write/raw 写一次，tick 起持续刷新的线程（teardown 自动停）。"""
    tickers = []

    class HB:
        def path(self, bot):
            return u.heartbeat_path(sandbox.dsh_home, bot)

        def write(self, bot, pid=4242, at_ms=None):
            u.write_heartbeat(self.path(bot), pid, at_ms)

        def raw(self, bot, text):
            p = self.path(bot)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")

        def tick(self, bot, pid=4242):
            t = _HeartbeatTicker(self.path(bot), pid).start()
            tickers.append(t)
            return t

    yield HB()
    for t in tickers:
        t.stop()


def _flask_app():
    from moments.hub_routes import hub_bp
    app = Flask(__name__, template_folder=str(REPO_ROOT / "moments" / "templates"))
    app.register_blueprint(hub_bp)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(sandbox):
    return _flask_app().test_client()


# ------------------------------------------------------------ 朋友圈库（第 4 稿接缝）

@pytest.fixture
def botlife_db(sandbox, monkeypatch):
    """朋友圈库钉在 tmp 里，返回 db 模块（编辑端点写 bot_profiles 的那张库）。

    `db.DB_PATH` 是导入期读 `BOTLIFE_STATE_DB` 定死的：sandbox 虽然设了变量，
    但如果本进程早就把 db 模块导入过（跑整个 tests/ 或先收 unit 目录时会发生），
    模块级常量已经落在仓库根的 state.db。这里重载一次让 DB_PATH 跟上，并当场
    断言它真在 tmp —— 编辑端点在契约里会写这张库，写错地方绝对不行。
    """
    import db as _db
    importlib.reload(_db)
    want = (sandbox.tmp / "state.db").resolve()
    got = Path(_db.DB_PATH).resolve()
    assert got == want, "朋友圈库没钉在 tmp：DB_PATH=%s（应 %s）—— db.DB_PATH 是导入期定死的，必须重载" % (got, want)
    _db.init()
    return _db


# ------------------------------------------------------------ 全站客户端（第 4/5 稿接缝）

@pytest.fixture
def web_client(sandbox, monkeypatch, botlife_db):
    """整站 Flask 客户端（moments.web）。

    §11c/§12b 的 dsh-model 转发端点与 §13 的三个页面都在整站 app 上；
    既有 client 只挂了 hub 蓝图，覆盖不到。模块级路径常量按导入期环境求值，
    按读图口径重载一次（与 hub_ui 既有做法同款）。
    """
    (sandbox.tmp / "tgbot-home").mkdir(exist_ok=True)
    monkeypatch.setenv("CLAUDE_TGBOT_HOME", str(sandbox.tmp / "tgbot-home"))
    monkeypatch.setenv("HUB_ENV_FILE", str(sandbox.tmp / "hub.env"))
    mod = sys.modules.get("moments.web")
    mod = importlib.reload(mod) if mod else importlib.import_module("moments.web")
    for attr in ("app", "application"):
        app = getattr(mod, attr, None)
        if app is not None and hasattr(app, "test_client"):
            app.config["TESTING"] = True
            return app.test_client()
    raise AssertionError("moments.web 未导出可用的 Flask app")


class StubGateway:
    """可编程的假网关（管理台的下游替身，只绑 127.0.0.1，绝不发外部请求）。

    模式：ok 正常回应；drop 读到请求即断连（管理台应判 504）；
    hang 挂着不回应（测试里少用，太重）。记录每个请求的方法、路径、体与口令是否正确。
    """

    def __init__(self, key="test-key-0123456789"):
        self.key = key
        self.requests = []
        self.routes = {}
        self.mode = "ok"
        self.hang_seconds = 3.0
        self._srv = None
        self._port = None

    def on(self, path, status, payload):
        self.routes[path] = lambda rec: (status, payload)
        return self

    def on_fn(self, path, fn):
        self.routes[path] = fn
        return self

    def start(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        stub = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _read_body(self):
                n = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    return json.loads(raw.decode("utf-8"))
                except Exception:
                    return None

            def _json(self, code, obj):
                raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _dispatch(self, method):
                auth = self.headers.get("authorization") or ""
                rec = {"method": method, "path": self.path.split("?")[0],
                       "body": self._read_body(), "auth_ok": auth == "Bearer %s" % stub.key}
                stub.requests.append(rec)
                if stub.mode == "drop":
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    try:
                        self.connection.close()
                    except OSError:
                        pass
                    return
                if stub.mode == "hang":
                    time.sleep(stub.hang_seconds)
                handler = stub.routes.get(rec["path"])
                if handler is None:
                    return self._json(404, {"error": "not found"})
                status, payload = handler(rec)
                return self._json(status, payload)

            def do_GET(self):
                self._dispatch("GET")

            def do_POST(self):
                self._dispatch("POST")

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._srv.daemon_threads = True
        self._port = self._srv.server_address[1]
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        return self

    def stop(self):
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None

    @property
    def port(self):
        return self._port

    def hits(self, path):
        return [r for r in self.requests if r["path"] == path]


@pytest.fixture
def gw_stub(sandbox):
    """管理台眼里的「在跑的新系统网关」：假件接管，绝不连真网关。"""
    s = StubGateway().start()
    try:
        yield s
    finally:
        s.stop()


@pytest.fixture
def gw_bot(sandbox):
    """造一只「管理台眼里在跑」的新系统 bot：DSH 配置 + state/api.key + state/api.port。

    port=None 表示 state 里没有 api.port（不算在跑，等于没有这个网关）。
    """
    def _make(name="testbot", port=None, key="test-key-0123456789", display_name=None):
        botdir = sandbox.dsh_home / "bots" / name
        (botdir / "state").mkdir(parents=True, exist_ok=True)
        (botdir / "state" / "api.key").write_text(key + "\n", encoding="utf-8")
        if port is not None:
            (botdir / "state" / "api.port").write_text(str(port) + "\n", encoding="utf-8")
        channel = botdir / "channel"
        channel.mkdir(exist_ok=True)
        yml = sandbox.dsh_cfg / ("%s.yml" % name)
        yml.write_text(json.dumps({
            "id": name,
            "display_name": display_name or name,
            "bot_channel_path": str(channel),
            "dispatcher_port": 0,
            "brain": {"provider": "deepseek-official", "model": "deepseek-flash"},
        }, ensure_ascii=False), encoding="utf-8")
        return SimpleNamespace(name=name, botdir=botdir, channel=channel, port=port,
                               key=key, yml=yml)
    return _make
