# -*- coding: utf-8 -*-
"""self_initiate 提前开口（想念值满不等随机闸）的白盒补充：坏状态文件、提前那次投递失败、先记账后投递。"""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
T = 1_780_000_000
GAP = 3 * 3600


def _si():
    spec = importlib.util.spec_from_file_location("self_initiate_early_unit", ROOT / "scripts" / "self_initiate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _world(tmp_path, monkeypatch, si, next_at, inject=lambda *a, **k: True):
    """频道目录 + 一条 4 小时前的投递记录 + 想念值满 + life-context 假件回 TEXT。返回 (state 目录, cfg, 调用记录)。"""
    ch = tmp_path / "bots" / "b" / "channel"
    ch.mkdir(parents=True)
    st = tmp_path / "bots" / "b" / "state" / "self-initiate"
    st.mkdir(parents=True)
    (st / "1.next").write_text(str(next_at), encoding="utf-8")
    (st / "1.sent.json").write_text(json.dumps({"last_sent_at": T - 4 * 3600}), encoding="utf-8")
    calls = []

    class R:
        returncode = 0
        stdout = json.dumps({"action": "TEXT", "text": "想你了"})

    def fake_run(*a, **k):
        calls.append(k.get("env") or {})
        return R()

    monkeypatch.setattr(si, "_jiwen_forced", lambda b, c: True)
    monkeypatch.setattr(si, "_in_sleep", lambda b, c, n: False)
    monkeypatch.setattr(si.subprocess, "run", fake_run)
    monkeypatch.setattr(si, "_inject", inject)
    return st, {"bot_channel_path": str(ch)}, calls


def _fail_on(si, monkeypatch, suffix):
    real = si._write_json

    def flaky(path, data):
        if path.endswith(suffix):
            raise PermissionError("只读")
        return real(path, data)

    monkeypatch.setattr(si, "_write_json", flaky)


def test_坏状态文件当空_计数坏值当零_没有投递记录先记下(tmp_path, monkeypatch):
    si = _si()
    bad = tmp_path / "x.json"
    bad.write_text("{坏", encoding="utf-8")
    assert si._read_json(str(bad)) == {}
    assert si._read_json(str(tmp_path / "没有.json")) == {}
    assert si._early_count({"count": "abc"}) == 0 and si._early_count({"count": -3}) == 0
    monkeypatch.setattr(si, "_in_sleep", lambda b, c, n: False)
    monkeypatch.setattr(si, "_jiwen_forced", lambda b, c: True)
    sent_f = tmp_path / "s.json"
    assert si._early_block("b", "1", T, {"last_sent_at": "坏"}, {}, {}, str(sent_f)) == "没有上次投递记录，先记下"
    assert json.loads(sent_f.read_text(encoding="utf-8"))["last_sent_at"] == T
    assert si._early_block("b", "1", T, {"last_sent_at": T - GAP}, {"count": "abc", "last_skip_at": None}) == ""
    monkeypatch.setattr(si, "_jiwen_forced", lambda b, c: False)
    assert si._early_block("b", "1", T, {"last_sent_at": T - GAP}, {}) == "想念值没满"


def test_提前那次投递失败_不动随机闸_30分钟内不再提前(tmp_path, monkeypatch):
    si = _si()

    def boom(*a, **k):
        raise ConnectionError("网关没在跑")

    st, cfg, calls = _world(tmp_path, monkeypatch, si, T + 86400, inject=boom)
    assert si.run_dsh("b", "1", cfg, T) == 0
    assert len(calls) == 1 and calls[0].get("SELF_INITIATE_EARLY") == "1"
    assert (st / "1.next").read_text(encoding="utf-8") == str(T + 86400)
    assert json.loads((st / "1.early.json").read_text(encoding="utf-8"))["last_skip_at"] == T
    assert si.run_dsh("b", "1", cfg, T + 600) == 0
    assert len(calls) == 1, "失败后 30 分钟内不该再提前"


def _record_inject(sent):
    def inject(*a, **k):
        sent.append(a)
        return True
    return inject


def test_提前那次sent写失败_不投递_10分钟后也不再调(tmp_path, monkeypatch, capsys):
    si = _si()
    sent = []
    st, cfg, calls = _world(tmp_path, monkeypatch, si, T + 86400, inject=_record_inject(sent))
    _fail_on(si, monkeypatch, "1.sent.json")
    assert si.run_dsh("b", "1", cfg, T) == 0
    assert len(calls) == 1 and calls[0].get("SELF_INITIATE_EARLY") == "1"
    assert sent == [], "记账写不进去不该投递"
    assert "PermissionError" in capsys.readouterr().err
    assert si.run_dsh("b", "1", cfg, T + 600) == 0
    assert len(calls) == 1, "记账失败后 10 分钟不该再调 life-context"
    assert (st / "1.next").read_text(encoding="utf-8") == str(T + 86400)


def test_提前那次early写失败_不投递_10分钟后也不再调(tmp_path, monkeypatch, capsys):
    si = _si()
    sent = []
    st, cfg, calls = _world(tmp_path, monkeypatch, si, T + 86400, inject=_record_inject(sent))
    _fail_on(si, monkeypatch, "1.early.json")
    assert si.run_dsh("b", "1", cfg, T) == 0
    assert len(calls) == 1 and calls[0].get("SELF_INITIATE_EARLY") == "1"
    assert sent == [], "记账写不进去不该投递"
    assert "PermissionError" in capsys.readouterr().err
    assert si.run_dsh("b", "1", cfg, T + 600) == 0
    assert len(calls) == 1, "记账失败后 10 分钟不该再调 life-context"
    assert (st / "1.next").read_text(encoding="utf-8") == str(T + 86400)


def test_提前投递成功_下次机会取原定与3小时后的较晚者(tmp_path, monkeypatch):
    for next_at, want in ((T + 600, T + GAP), (T + 86400, T + 86400)):
        si = _si()
        sent = []
        base = tmp_path / str(next_at)
        st, cfg, calls = _world(base, monkeypatch, si, next_at, inject=_record_inject(sent))
        assert si.run_dsh("b", "1", cfg, T) == 0
        assert len(sent) == 1 and calls[0].get("SELF_INITIATE_EARLY") == "1"
        assert (st / "1.next").read_text(encoding="utf-8") == str(want)
        assert json.loads((st / "1.sent.json").read_text(encoding="utf-8"))["last_sent_at"] == T
        assert json.loads((st / "1.early.json").read_text(encoding="utf-8"))["count"] == 1


def test_early_skipped写失败不抛异常(tmp_path, monkeypatch, capsys):
    si = _si()

    class Bad:
        returncode = 1
        stdout = ""

    st, cfg, calls = _world(tmp_path, monkeypatch, si, T + 86400)
    monkeypatch.setattr(si.subprocess, "run", lambda *a, **k: calls.append(k.get("env") or {}) or Bad())
    _fail_on(si, monkeypatch, "1.early.json")
    assert si.run_dsh("b", "1", cfg, T) == 0
    assert calls[0].get("SELF_INITIATE_EARLY") == "1"
    assert (st / "1.next").read_text(encoding="utf-8") == str(T + 86400)
    assert "PermissionError" in capsys.readouterr().err
