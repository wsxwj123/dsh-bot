# -*- coding: utf-8 -*-
"""管理台 UI 重规划的验收夹具（黑盒，依据 .devflow/INTERFACE-管理台UI.md）。

不读实现代码。实现未落地时本目录会红，那是红基线，不是夹具坏了。

硬隔离：
1. HOME、DSH_BOT_HOME、CLAUDE_TGBOT_HOME、HUB_* 全部指向 tmp，绝不碰 ~/.dsh-bot 与真实配置；
2. 管理台看到的"在跑的新系统网关"用假网关（本机 HTTP stub）扮演，绝不连真实 bot；
3. 管理台用 Flask test_client，不监听端口；
4. 真网关的用例要用 bun 起子进程（可选），没有 bun 时那批自动 skip。
"""
import importlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
# 不再把 HERE 插进 sys.path。本目录是包,目录内的模块一律相对导入。
# 把 hub_ui 目录挂在 sys.path 上,会让这里的 conftest.py 有机会被别家
# 的 `from conftest import ...` 捡走,正是这次要根除的坑。

REAL_DSH_HOME = Path(os.path.expanduser("~")) / ".dsh-bot"   # 在 monkeypatch 之前定住
GATEWAY_DIR = REPO_ROOT / "gateway"


def find_bun():
    """PATH 里找；找不到再试官方安装位置 ~/.bun/bin/bun（本机 bun 没配 PATH）。"""
    p = shutil.which("bun")
    if p:
        return p
    cand = Path(os.path.expanduser("~")) / ".bun" / "bin" / "bun"   # 这个 expanduser 在 monkeypatch HOME 之前求值
    return str(cand) if cand.is_file() else None


BUN = find_bun()

# Windows 上只给 PATH / HOME 会让子进程起不来，照 provider_wizard 带过去
_WIN_ESSENTIALS = ("SystemRoot", "SystemDrive", "windir", "TEMP", "TMP", "USERPROFILE", "APPDATA",
                   "LOCALAPPDATA", "ComSpec", "PATHEXT", "NUMBER_OF_PROCESSORS")


def child_env(home, extra=None):
    """给子进程（真网关）用的隔离环境。"""
    env = {k: os.environ[k] for k in _WIN_ESSENTIALS if k in os.environ}
    env["PATH"] = os.environ.get("PATH", "")
    env["HOME"] = str(home)
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


def free_port():
    """拿一个当前空闲端口并立刻释放（用于"没人监听"的前提）。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """隔离的根目录与家目录；返回 dict(root=, home=, tmp=, env=)。"""
    root = tmp_path / "dsh-root"
    home = tmp_path / "fakehome"
    (home / ".claude" / "channels").mkdir(parents=True)
    root.mkdir()
    for k in ("HUB_ACCESS_PASSWORD", "HUB_ADMIN_PASSWORD", "MOMENTS_WEB_PORT", "ACCESS_PASSWORD",
              "CLIPROXY_MGMT_KEY", "CLIPROXY_PORT", "CLIPROXY_API_KEY", "CLIPROXY_ANTHROPIC_COMPAT",
              # 本机常备代理（Clash TUN 的 shell 变量）会把 127.0.0.1 的请求也劫持走，
              # 而管理台到网关是 urllib 直连本机。不隔离这里，所有连网关的用例都会拿到代理的 502。
              "http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
              "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(k, raising=False)
    env = {
        "HOME": str(home),
        "DSH_BOT_HOME": str(root),
        "CLAUDE_TGBOT_HOME": str(tmp_path / "tgbot-home"),
        "CLAUDE_SETTINGS_PATH": str(home / ".claude" / "settings.json"),
        "HUB_ENV_FILE": str(tmp_path / "hub.env"),
        "MOMENTS_WEB_HOST": "127.0.0.1",
        "HUB_CONFIGS_DIR": str(tmp_path / "configs-legacy"),
        "HUB_CONFIGS_DSH_DIR": str(tmp_path / "configs-dsh"),
        "HUB_LAUNCHAGENTS_DIR": str(tmp_path / "LaunchAgents"),
        "HUB_CHANNELS_DIR": str(tmp_path / "channels"),
        "HUB_RESTART_CMD": "/usr/bin/true",
        "HUB_RESTART_SCRIPT": str(tmp_path / "no-such-restart.sh"),
        "HUB_TELEGRAM_API_BASE": f"http://127.0.0.1:{free_port()}",
    }
    for d in ("tgbot-home", "configs-legacy", "configs-dsh", "LaunchAgents", "channels"):
        (tmp_path / d).mkdir(exist_ok=True)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert str(root).startswith(str(tmp_path)), "DSH_BOT_HOME 必须在 tmp 里"
    assert root.resolve() != REAL_DSH_HOME.resolve(), "DSH_BOT_HOME 指到了真实的 ~/.dsh-bot"
    return {"root": root, "home": home, "tmp": tmp_path, "env": env}


def _load_app():
    mod = sys.modules.get("moments.web")
    mod = importlib.reload(mod) if mod else importlib.import_module("moments.web")
    for attr in ("app", "application"):
        obj = getattr(mod, attr, None)
        if obj is not None and hasattr(obj, "test_client"):
            return obj
    factory = getattr(mod, "create_app", None)
    if callable(factory):
        return factory()
    raise AssertionError("moments.web 未导出可用的 Flask app")


@pytest.fixture
def make_client(sandbox, monkeypatch):
    """(**env) -> client。每次调用按给定 env 重载门户（env 在导入期被读）。"""
    def _make(**env):
        for k, v in env.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, str(v))
        app = _load_app()
        app.config["TESTING"] = True
        return app.test_client()
    return _make


@pytest.fixture
def hub(make_client):
    """默认客户端：门关闭（未设 HUB_ACCESS_PASSWORD）。"""
    return make_client()


@pytest.fixture
def login():
    """走表单登录（application/x-www-form-urlencoded）。"""
    def _login(client, password=None, admin_password=None):
        form = {}
        if password is not None:
            form["password"] = password
        if admin_password is not None:
            form["admin_password"] = admin_password
        return client.post("/login", data=form, content_type="application/x-www-form-urlencoded")
    return _login


@pytest.fixture
def fake_tg():
    """最小假 Telegram（真网关进程启动用）。"""
    from .fake_telegram import FakeTelegram
    tg = FakeTelegram().start()
    try:
        yield tg
    finally:
        tg.stop()


@pytest.fixture
def real_gateway(sandbox, fake_tg):
    """起一个真网关进程（bun src/main.ts），返回 dict(root=, port=, key_path=, stdout_path=, proc=)。

    没有 bun 时整批 skip。网关自己写 state/api.key 与 api.port；管理台经 DSH 配置找到它。
    """
    if not BUN:
        pytest.skip("需要 bun 才能起真网关（PATH 里没有，也不在 ~/.bun/bin）")
    root = sandbox["root"]
    name = "testbot"
    botdir = root / "bots" / name
    channel = botdir / "channel"
    channel.mkdir(parents=True, exist_ok=True)
    (channel / ".env").write_text("TELEGRAM_BOT_TOKEN=%s\n" % fake_tg.token, encoding="utf-8")
    (channel / "access.json").write_text(json.dumps({"dmPolicy": "allowlist", "allowFrom": ["5550001"], "groups": {}}), encoding="utf-8")
    (channel / "CLAUDE.md").write_text("# 测试人设\n你是小测，说话简短。\n", encoding="utf-8")
    acp_state = root / ("acp-%s" % name)
    acp_state.mkdir(exist_ok=True)
    gwcfg = {
        "telegram_api": fake_tg.base,
        "dsh_command": [BUN, str(GATEWAY_DIR / "test" / "fakes" / "fake-acp.ts"), "--state", str(acp_state)],
        "burst_window_ms": 150, "burst_max_ms": 2000, "poll_timeout_s": 1,
        "retry_backoff_ms": [50, 100], "turn_stall_cancel_ms": 60000, "turn_stall_warn_ms": 30000,
        "max_send_wait_ms": 500, "config_poll_ms": 100, "commit_poll_ms": 200,
        "situation_ttl_ms": 300,
        "situation_cmd": [BUN, str(GATEWAY_DIR / "test" / "fakes" / "fake-situation.ts"), str(acp_state / "situation.json")],
        "heartbeat_ms": 500, "probe_ms": 60000, "log_level": "debug", "model_fetch_timeout_ms": 3000,
    }
    cfg = root / ("%s.yml" % name)
    cfg.write_text(json.dumps({
        "id": name, "display_name": name, "bot_channel_path": str(channel),
        "dispatcher_port": 0,
        "brain": {"provider": "deepseek-official", "model": "deepseek-flash", "reasoning_effort": "low"},
        "gateway": gwcfg,
    }, ensure_ascii=False), encoding="utf-8")
    # 管理台靠这份 DSH 配置找到这个 bot（state 文件由网关自己写）
    dsh_cfg = Path(sandbox["env"]["HUB_CONFIGS_DSH_DIR"]) / ("%s.yml" % name)
    dsh_cfg.write_text(json.dumps({
        "id": name, "display_name": name, "bot_channel_path": str(channel), "dispatcher_port": 0,
        "brain": {"provider": "deepseek-official", "model": "deepseek-flash"},
    }, ensure_ascii=False), encoding="utf-8")
    ready = root / ("%s.ready" % name)
    stdout_path = root / "gw-stdout.log"
    out_f = open(stdout_path, "wb")
    proc = subprocess.Popen(
        [BUN, str(GATEWAY_DIR / "src" / "main.ts"), "--config", str(cfg)],
        cwd=str(GATEWAY_DIR),
        env=child_env(sandbox["home"], {"DSH_BOT_HOME": str(root), "DSH_BOT_READY_FILE": str(ready)}),
        stdout=out_f, stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if ready.exists() or proc.poll() is not None:
                break
            time.sleep(0.2)
        assert ready.exists(), "真网关 30 秒内没起来（退出码 %s），输出见 %s" % (proc.poll(), stdout_path)
        info = json.loads(ready.read_text(encoding="utf-8"))
        yield {"root": root, "name": name, "port": int(info["api_port"]),
               "key_path": botdir / "state" / "api.key", "stdout_path": stdout_path, "proc": proc,
               "channel": channel}
    finally:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        finally:
            out_f.close()


@pytest.fixture
def dsh_bot(sandbox):
    """造一个管理台眼里的"新系统 bot"：DSH 配置 + state 里的 api.key/api.port。

    port=None 表示 state 里没有 api.port（不算在跑，等于没有网关）。
    返回 dict(name=, channel=, state=, port=, key=, config=)。
    """
    def _make(name="testbot", port=None, key="test-api-key-0123456789", display_name=None):
        botdir = sandbox["root"] / "bots" / name
        channel = botdir / "channel"
        state = botdir / "state"
        channel.mkdir(parents=True, exist_ok=True)
        state.mkdir(parents=True, exist_ok=True)
        (state / "api.key").write_text(key + "\n", encoding="utf-8")
        if port is not None:
            (state / "api.port").write_text(str(port) + "\n", encoding="utf-8")
        cfg = Path(sandbox["env"]["HUB_CONFIGS_DSH_DIR"]) / f"{name}.yml"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({
            "id": name,
            "display_name": display_name or name,
            "bot_channel_path": str(channel),
            "dispatcher_port": port or 0,
            "brain": {"provider": "deepseek-official", "model": "deepseek-flash"},
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"name": name, "channel": channel, "state": state, "port": port, "key": key, "config": cfg}
    return _make
