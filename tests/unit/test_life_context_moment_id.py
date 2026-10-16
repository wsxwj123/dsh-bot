# -*- coding: utf-8 -*-
"""主动找你（life-context.py）顺带发的朋友圈，用朋友圈里的身份发。

新系统的 bot 在朋友圈里叫 life_id（陈露露是 chenlulu），网关里 moments 工具发圈也用这个名字；
用配置编号（bot4）发，管理台的朋友圈页对不上人。作息、积温那些记录照旧用配置编号，不在这里改。
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _life_context():
    spec = importlib.util.spec_from_file_location("life_context_moment_id", ROOT / "life-context.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_moment_is_posted_under_life_id(monkeypatch):
    lc = _life_context()
    seen = []
    monkeypatch.setattr(lc, "maybe_post_moment", lambda bot_id, *a, **k: seen.append(bot_id) or 7)
    assert lc._try_post_moment("bot4", {"_life_id": "chenlulu"}, None, None, None, None, 0.5, [], None, {}) == 7
    assert lc._try_post_moment("yasuna", {}, None, None, None, None, 0.5, [], None, {}) == 7
    assert lc._try_post_moment("wenya", None, None, None, None, None, 0.5, [], None, {}) == 7
    assert seen == ["chenlulu", "yasuna", "wenya"]
