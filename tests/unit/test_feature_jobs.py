# -*- coding: utf-8 -*-
"""功能开关（约定 v1.1 5.19）管到的两个定时任务：主动找你（proactive 关着就跳过）、积温（relationship 关着就跳过这只）。"""
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = (ROOT / "gateway" / "src" / "ledger-schema.sql").read_text(encoding="utf-8")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def dsh_bot(tmp_path, monkeypatch):
    """新系统布局：configs/bot5.yml + bots/bot5/{channel,state/ledger.sqlite}；features 由用例写进 yml"""
    monkeypatch.delenv("DSH_BOT_LEDGER", raising=False)
    root = tmp_path / "bots" / "bot5"
    (root / "channel").mkdir(parents=True)
    (root / "state").mkdir()
    db = sqlite3.connect(root / "state" / "ledger.sqlite")
    db.executescript(SCHEMA)
    db.commit()
    cfgs = tmp_path / "configs"
    cfgs.mkdir()
    yml = cfgs / "bot5.yml"

    def write(features=None):
        body = {"id": "bot5", "chat_id": "1", "bot_channel_path": str(root / "channel"), "dispatcher_port": 0}
        if features is not None:
            body["features"] = features
        yml.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")

    write()
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(cfgs))
    return root, db, write


def test_主动找你关着_不问life_context也不投递_开着照常问(dsh_bot, monkeypatch):
    root, _, _ = dsh_bot
    si = _load("self_initiate_feat", ROOT / "scripts" / "self_initiate.py")
    calls = []
    monkeypatch.setattr(si.subprocess, "run", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(RuntimeError("stop")))
    cfg = {"bot_channel_path": str(root / "channel"), "dispatcher_port": 0, "features": {"proactive": False}}
    assert si.run_dsh("bot5", "42", cfg, 1_000_000) == 0
    assert calls == []
    assert not (root / "state" / "self-initiate" / "42.next").exists()
    cfg["features"] = {"proactive": True}
    si.run_dsh("bot5", "42", cfg, 1_000_000)
    assert len(calls) == 1                                  # 开着：照常去问 life-context


def test_积温_关系进展关着的bot跳过_开着的照算(dsh_bot, tmp_path, monkeypatch):
    root, db, write = dsh_bot
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("DSH_BOT_HOME", str(tmp_path))
    (tmp_path / "credentials.yaml").write_text("version: 1\nrefs:\n  DEEPSEEK_API_KEY: sk-test-only-0000000000\n", encoding="utf-8")
    rel = root / "channel" / "relationship.json"
    rel.write_text(json.dumps({"affection": 50, "trust": 50, "desire": 0, "energy": 80}), encoding="utf-8")
    import time
    t = int(time.time()) - 60
    db.execute("INSERT INTO inbound (ukey, chat_id, kind, text, ts, received_at, state) VALUES ('j1', '1', 'user', '今天考试过了', ?, ?, 'done')", (t * 1000, t * 1000))
    db.commit()
    write(features={"relationship": False})
    tick = _load("jiwen_tick_feat", ROOT / "jiwen" / "tick.py")
    monkeypatch.setattr(tick.config_loader, "load_global", lambda: {})
    calls = []
    monkeypatch.setattr(tick.deepseek_delta, "compute_delta", lambda **k: calls.append(k) or {"connection": 0.1, "valence": 0.3, "arousal": 0.1})
    monkeypatch.setattr(sys, "argv", ["tick.py"])
    before = rel.read_text(encoding="utf-8")
    tick.main()
    assert calls == [] and rel.read_text(encoding="utf-8") == before
    write(features={"relationship": True})
    tick.main()
    assert len(calls) == 1
