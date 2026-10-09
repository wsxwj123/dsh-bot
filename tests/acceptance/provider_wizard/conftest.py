# -*- coding: utf-8 -*-
"""供应商引导需求的 Python 侧验收夹具（黑盒，依据 .devflow/INTERFACE.md v2 的 3.9、3.10.2）。

不读实现代码。实现未落地时本目录会红 —— 那是红基线，不是夹具坏了。

硬隔离：
1. HOME、DSH_BOT_HOME、CLAUDE_TGBOT_HOME、HUB_* 全部指向 tmp，绝不碰 ~/.dsh-bot 与真实配置；
2. 需要"聊过一轮"的账本时，用 make_ledger.ts 拉起真网关（假 Telegram、假 dsh，只连 127.0.0.1）在 tmp 根目录下生成；
3. 管理台用 Flask test_client，不监听端口。
"""
import importlib
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
GATEWAY_DIR = REPO_ROOT / "gateway"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(HERE))
from _pw_util import OWNER  # noqa: E402

BUN = shutil.which("bun")
REAL_DSH_HOME = Path(os.path.expanduser("~")) / ".dsh-bot"   # 在任何 monkeypatch 之前定住

# Windows 上只给 PATH / HOME 会让子进程起不来（bun 直接 abort，python 报哈希随机化初始化失败），
# 把系统必需的那几个变量照原样带过去。HOME 仍指向每个用例的隔离家目录。
_WIN_ESSENTIALS = ("SystemRoot", "SystemDrive", "windir", "TEMP", "TMP", "USERPROFILE", "APPDATA",
                   "LOCALAPPDATA", "ComSpec", "PATHEXT", "NUMBER_OF_PROCESSORS")


def _child_env(home, extra=None):
    env = {k: os.environ[k] for k in _WIN_ESSENTIALS if k in os.environ}
    env["PATH"] = os.environ.get("PATH", "")
    env["HOME"] = str(home)
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """隔离的根目录与家目录；返回 dict(root=, home=, tmp=)。"""
    root = tmp_path / "dsh-root"
    home = tmp_path / "fakehome"
    (home / ".claude" / "channels").mkdir(parents=True)
    root.mkdir()
    for k in ("HUB_ACCESS_PASSWORD", "HUB_ADMIN_PASSWORD", "MOMENTS_WEB_PORT", "ACCESS_PASSWORD",
              "CLIPROXY_MGMT_KEY", "CLIPROXY_PORT", "CLIPROXY_API_KEY", "CLIPROXY_ANTHROPIC_COMPAT"):
        monkeypatch.delenv(k, raising=False)
    env = {
        "HOME": str(home),
        "DSH_BOT_HOME": str(root),
        "CLAUDE_TGBOT_HOME": str(tmp_path / "tgbot-home"),
        "CLAUDE_SETTINGS_PATH": str(home / ".claude" / "settings.json"),
        "HUB_ENV_FILE": str(tmp_path / "hub.env"),
        "MOMENTS_WEB_HOST": "127.0.0.1",
        "HUB_CONFIGS_DIR": str(tmp_path / "configs"),
        "HUB_LAUNCHAGENTS_DIR": str(tmp_path / "LaunchAgents"),
        "HUB_CHANNELS_DIR": str(tmp_path / "channels"),
        "HUB_RESTART_CMD": "/usr/bin/true",
        "HUB_RESTART_SCRIPT": str(tmp_path / "no-such-restart.sh"),
        "HUB_TELEGRAM_API_BASE": f"http://127.0.0.1:{_free_port()}",
    }
    for d in ("tgbot-home", "configs", "LaunchAgents", "channels"):
        (tmp_path / d).mkdir(exist_ok=True)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert str(root).startswith(str(tmp_path)), "DSH_BOT_HOME 必须在 tmp 里"
    assert root.resolve() != REAL_DSH_HOME.resolve(), "DSH_BOT_HOME 指到了真实的 ~/.dsh-bot"
    return {"root": root, "home": home, "tmp": tmp_path, "env": env}


@pytest.fixture
def make_ledger(sandbox):
    """(bot, who, text) -> 账本路径。who 为 OWNER/FRIEND 的 id，或 None（只启动再停止网关，账本里没有会话段）。"""
    if not BUN:
        pytest.skip("需要 bun 才能用真网关生成账本")

    def _make(bot="newbot", who=OWNER, text="你好"):
        r = subprocess.run([BUN, str(HERE / "make_ledger.ts"), str(sandbox["root"]), bot, "none" if who is None else str(who), text],
                           cwd=str(GATEWAY_DIR), capture_output=True, text=True, timeout=120,
                           env=_child_env(sandbox["home"]))
        assert r.returncode == 0, f"生成账本失败：{r.stderr[-2000:]}"
        p = sandbox["root"] / "bots" / bot / "state" / "ledger.sqlite"
        assert p.exists(), "网关没有生成账本"
        return p
    return _make


@pytest.fixture
def old_channel(sandbox):
    """(allow_from) -> 旧频道目录（只有 access.json；allowFrom[0] 是主人私聊 id）。"""
    def _make(allow_from=(OWNER,)):
        d = sandbox["tmp"] / "old-channel"
        d.mkdir(exist_ok=True)
        (d / "access.json").write_text(json.dumps({"dmPolicy": "allowlist", "allowFrom": [str(x) for x in allow_from], "groups": {}}), encoding="utf-8")
        return d
    return _make


@pytest.fixture
def run_import(sandbox):
    """(bot, from_dir, *extra) -> CompletedProcess。在隔离环境里运行 scripts/import_history.py。"""
    def _run(bot, from_dir, *extra):
        env = _child_env(sandbox["home"], {"DSH_BOT_HOME": str(sandbox["root"]), "PYTHONIOENCODING": "utf-8"})
        return subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "import_history.py"), bot, "--from", str(from_dir), *extra],
                              cwd=str(sandbox["tmp"]), capture_output=True, text=True, timeout=120, env=env)
    return _run


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
def hub(sandbox):
    """管理台测试客户端（门关闭：未设 HUB_ACCESS_PASSWORD）。"""
    app = _load_app()
    app.config["TESTING"] = True
    return app.test_client()
