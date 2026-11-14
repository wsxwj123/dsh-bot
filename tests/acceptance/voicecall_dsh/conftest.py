# -*- coding: utf-8 -*-
"""voicecall-dsh 验收夹具（黑盒）。

只依据 `.devflow/BRIEF-voicecall-dsh.md` 与 `.devflow/INTERFACE-voicecall-dsh.md` 编写，不读实现。
隔离写法照 tests/acceptance/life_private_dir/conftest.py：HOME / DSH_BOT_HOME / 配置根 / 令牌全在 tmp，
launchctl / bun 指向毒丸，反向断言真实目录顶层条目前后一字未变。
"""


def _require_total_gate():
    """tests/conftest.py 的总闸没加载就拒绝运行（同 life_private_dir 的自检）。"""
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
        "请从仓库根启动，例如：cd %s && python3 -m pytest %s" % (root, os.path.relpath(here, root)))


_require_total_gate()
del _require_total_gate

import os
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
ACCEPTANCE_ROOT = HERE.parent
REPO_ROOT = HERE.parents[2]
for _p in (REPO_ROOT, ACCEPTANCE_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
REAL_HOME = Path(os.path.expanduser("~"))       # 在换掉 HOME 之前定住

from . import vc_util as vu  # noqa: E402


def _write_poison(dirpath, name):
    d = Path(dirpath)
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
    p.chmod(0o755)
    return p


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    home = tmp_path / "fakehome"
    home.mkdir()
    dsh_home = tmp_path / "dsh-home"
    dsh_home.mkdir()
    dsh_cfg = tmp_path / "configs-dsh"
    dsh_cfg.mkdir()
    legacy_cfg = tmp_path / "configs-legacy"
    legacy_cfg.mkdir()
    for k in ("DSH_CONSOLE_LIFE_DIR", "DSH_CONSOLE_HOST", "DSH_CONSOLE_PORT",
              "CLAUDEBOTLIFE_BYPASS_SILENCE", "SELF_INITIATE_EARLY",
              "http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY",
              "ALL_PROXY", "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("DSH_BOT_HOME", str(dsh_home))
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(dsh_cfg))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(legacy_cfg))
    monkeypatch.setenv("BOTLIFE_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("HUB_LAUNCHCTL", str(_write_poison(tmp_path / "poison", "launchctl")))
    monkeypatch.setenv("HUB_BUN", str(_write_poison(tmp_path / "poison", "bun")))
    monkeypatch.setenv("HUB_TELEGRAM_API_BASE", "http://127.0.0.1:%d" % _free_port())
    token_dir = tmp_path / "console-token"
    token_dir.mkdir()
    token_file = token_dir / "adapter.token"
    token_file.write_text(vu.ADAPTER_TOKEN, encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setenv("DSH_CONSOLE_TOKEN_FILE", str(token_file))
    return SimpleNamespace(tmp=tmp_path, home=home, dsh_home=dsh_home, dsh_cfg=dsh_cfg)


def _names(d):
    try:
        return sorted(p.name for p in Path(d).iterdir())
    except OSError:
        return None


@pytest.fixture(autouse=True)
def _no_touch_real():
    """反向断言：真实目录顶层条目前后一字未变。"""
    watched = [REAL_HOME / ".dsh-bot", REAL_HOME / ".dsh-bot" / "bots",
               REAL_HOME / "claudebotlife" / "configs", REPO_ROOT / "configs"]
    before = [_names(d) for d in watched]
    yield
    for d, b in zip(watched, before):
        assert _names(d) == b, "用例动到了真实目录 %s（接缝漏了）" % d


@pytest.fixture
def w(sandbox, monkeypatch):
    world = vu.World(sandbox, monkeypatch)
    yield world
    world.close()
