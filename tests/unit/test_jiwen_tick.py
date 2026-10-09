# -*- coding: utf-8 -*-
"""积温任务的 bot 选择（搬自上游 1e48e78 的那一项）：有 dsh_bots 时只跑它们，忽略 _global.yml 里的旧名字。"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _tick():
    return _load("jiwen_tick", ROOT / "jiwen" / "tick.py")


def test_explicit_bot_wins():
    assert _tick().choose_bots("only", ["old"], ["new"]) == ["only"]


def test_dsh_bots_ignore_old_names():
    # 有 dsh_bots（新系统 bot）时只跑它们，不看 jiwen.bots 里的旧名字
    assert _tick().choose_bots(None, ["old1", "old2"], ["new1", "new2"]) == ["new1", "new2"]


def test_falls_back_to_jiwen_bots_when_no_dsh_bots():
    assert _tick().choose_bots(None, ["old1"], []) == ["old1"]
    assert _tick().choose_bots(None, ["old1"], None) == ["old1"]


def test_scans_channels_when_nothing_configured(tmp_path):
    ch = tmp_path / "channels"
    (ch / "b1").mkdir(parents=True)
    (ch / "b1" / "access.json").write_text("{}", encoding="utf-8")
    (ch / "b2").mkdir()
    (ch / "b2" / "access.json").write_text("{}", encoding="utf-8")
    (ch / "not-a-bot").mkdir()  # 没有 access.json，不算
    assert _tick().choose_bots(None, None, [], channels_dir=str(ch)) == ["b1", "b2"]


def test_missing_channels_dir_is_empty(tmp_path):
    assert _tick().choose_bots(None, None, [], channels_dir=str(tmp_path / "nope")) == []
