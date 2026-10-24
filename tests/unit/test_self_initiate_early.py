# -*- coding: utf-8 -*-
"""self_initiate 提前开口（想念值满不等随机闸）的白盒补充：坏状态文件、提前那次投递失败。"""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
T = 1_780_000_000


def _si():
    spec = importlib.util.spec_from_file_location("self_initiate_early_unit", ROOT / "scripts" / "self_initiate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_坏状态文件当空_计数坏值当零(tmp_path, monkeypatch):
    si = _si()
    bad = tmp_path / "x.json"
    bad.write_text("{坏", encoding="utf-8")
    assert si._read_json(str(bad)) == {}
    assert si._read_json(str(tmp_path / "没有.json")) == {}
    assert si._early_count({"count": "abc"}) == 0 and si._early_count({"count": -3}) == 0
    monkeypatch.setattr(si, "_jiwen_forced", lambda b, c: True)
    assert si._early_block("b", "1", T, {"last_sent_at": "坏"}, {"count": "abc", "last_skip_at": None}) == ""
    monkeypatch.setattr(si, "_jiwen_forced", lambda b, c: False)
    assert si._early_block("b", "1", T, {}, {}) == "想念值没满"


def test_提前那次投递失败_不动随机闸_30分钟内不再提前(tmp_path, monkeypatch):
    si = _si()
    ch = tmp_path / "bots" / "b" / "channel"
    ch.mkdir(parents=True)
    st = tmp_path / "bots" / "b" / "state" / "self-initiate"
    st.mkdir(parents=True)
    (st / "1.next").write_text(str(T + 86400), encoding="utf-8")
    cfg = {"bot_channel_path": str(ch)}
    calls = []

    class R:
        returncode = 0
        stdout = json.dumps({"action": "TEXT", "text": "想你了"})

    def fake_run(*a, **k):
        calls.append(k.get("env") or {})
        return R()

    def boom(*a, **k):
        raise ConnectionError("网关没在跑")

    monkeypatch.setattr(si, "_jiwen_forced", lambda b, c: True)
    monkeypatch.setattr(si.subprocess, "run", fake_run)
    monkeypatch.setattr(si, "_inject", boom)
    assert si.run_dsh("b", "1", cfg, T) == 0
    assert calls and calls[-1].get("SELF_INITIATE_EARLY") == "1"
    assert (st / "1.next").read_text(encoding="utf-8") == str(T + 86400)
    assert json.loads((st / "1.early.json").read_text(encoding="utf-8"))["last_skip_at"] == T
    assert not (st / "1.sent.json").exists()
    assert si.run_dsh("b", "1", cfg, T + 600) == 0
    assert len(calls) == 1, "失败后 30 分钟内不该再提前"
