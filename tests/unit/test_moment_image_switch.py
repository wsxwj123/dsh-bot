# -*- coding: utf-8 -*-
"""发朋友圈的配图跟着管理台的共用生图开关走（约定 v1.1 5.21），ComfyUI 调共用的 comfyui-skill 脚本。

网关和"主动找你"任务把开关所在的全局配置（CLAUDEBOT_GLOBAL_CFG）与共用技能目录（COMFYUI_SKILL_DIR）
放进环境变量；没设时照旧按本仓库的配置与目录。
"""
import types
from pathlib import Path

import yaml

from moments import post

ROOT = Path(__file__).resolve().parents[2]


def _cfg(tmp_path, **img):
    p = tmp_path / "_global.yml"
    p.write_text(yaml.safe_dump({"moments": {"image_generation": img}}), encoding="utf-8")
    return p


def test_engine_follows_shared_switch(monkeypatch, tmp_path):
    local = {"provider_moment": "novelai"}
    monkeypatch.delenv("CLAUDEBOT_GLOBAL_CFG", raising=False)
    assert post._moment_engine(local) == "novelai"
    assert post._moment_engine({"provider": "comfyui"}) == "comfyui"
    assert post._moment_engine({"provider_moment": "dalle", "provider": "comfyui"}) == "comfyui"
    assert post._moment_engine({}) == "novelai"
    monkeypatch.setenv("CLAUDEBOT_GLOBAL_CFG", str(_cfg(tmp_path, provider="novelai", provider_moment="comfyui")))
    assert post._moment_engine(local) == "comfyui"            # 设了共用开关就按它，不看本仓库的
    monkeypatch.setenv("CLAUDEBOT_GLOBAL_CFG", str(tmp_path / "missing.yml"))
    assert post._moment_engine(local) == "novelai"            # 读不到就按本仓库的


class _SyncThread:
    def __init__(self, target, daemon=None):
        self.target = target

    def start(self):
        self.target()


def _wire(monkeypatch):
    import config_loader
    import subprocess
    import threading
    monkeypatch.setattr(post, "_translate_via_cli", lambda prompt, timeout=60: {"prompt": "1girl, cafe", "size": "wide"})
    monkeypatch.setattr(config_loader, "load_bot", lambda bot_id, *a, **k: {"id": bot_id})
    monkeypatch.setattr(threading, "Thread", _SyncThread)
    calls, fell_back = [], []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw.get("env") or {}))
        return types.SimpleNamespace(returncode=1, stdout="FAIL: 连不上 ComfyUI\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(post, "_gen_image_novelai", lambda *a, **k: fell_back.append(a[0]))
    return calls, fell_back


def test_comfyui_moment_calls_shared_script_and_does_not_switch_engine(monkeypatch, tmp_path):
    shared = _cfg(tmp_path, provider="comfyui")
    skill = tmp_path / "comfyui-skill"
    monkeypatch.setenv("CLAUDEBOT_GLOBAL_CFG", str(shared))
    monkeypatch.setenv("COMFYUI_SKILL_DIR", str(skill))
    monkeypatch.delenv("CLAUDEBOTLIFE_REPO", raising=False)
    calls, fell_back = _wire(monkeypatch)
    post._gen_image_comfyui("chenlulu", 5, "在咖啡馆", "public")
    cmd, env = calls[0]
    assert cmd[1] == str(skill / "scripts" / "comfyui_gen.py")
    assert cmd[2] == "chenlulu" and cmd[3].startswith("1girl, cafe")
    assert cmd[cmd.index("--global") + 1] == str(shared)
    assert cmd[cmd.index("--size") + 1] == "wide"
    assert env.get("CLAUDEBOTLIFE_REPO") == str(ROOT)        # 仓库 configs/ 下的 bot yml 里的锚点照旧找得到
    assert fell_back == []                                   # 出不了图不换引擎（用户 2026-10-16 定）：这条圈只发文字


def test_without_shared_settings_uses_repo_script_and_config(monkeypatch):
    import config_loader
    monkeypatch.delenv("CLAUDEBOT_GLOBAL_CFG", raising=False)
    monkeypatch.delenv("COMFYUI_SKILL_DIR", raising=False)
    calls, _ = _wire(monkeypatch)
    post._gen_image_comfyui("yasuna", 6, "在家", "public")
    cmd, _ = calls[0]
    assert cmd[1] == str(ROOT / "skills" / "comfyui-skill" / "scripts" / "comfyui_gen.py")
    assert cmd[cmd.index("--global") + 1] == config_loader.GLOBAL_CFG_PATH
