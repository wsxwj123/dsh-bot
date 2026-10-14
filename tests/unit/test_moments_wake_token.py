# -*- coding: utf-8 -*-
"""拉起旧系统 bot 的 worker 时带不带口令。

旧系统的调度器自 2026-10-11 起给本机接口加了口令（``<频道目录>/.api-token``）。8765 这段
没带，四只 bot 的唤醒全部被 401 拒掉；当时没在跑的那只就收不到朋友圈、评论、点赞的通知。
这里只测 ``_ensure_worker_alive`` 发出去的请求：有口令文件就带 ``X-Api-Token``，没有就不带，
口令不进日志。出站一律走替身，不连网络。
"""
import importlib
import socket
from pathlib import Path

import pytest

TOKEN = "tok-FAKE-wake-0123456789abcdef"


class _Recorder:
    def __init__(self, fail=None):
        self.reqs, self.fail = [], fail

    def __call__(self, req, *a, **k):
        self.reqs.append(req)
        if self.fail:
            raise self.fail

        class R:
            def read(s):
                return b"{}"

            def __enter__(s):
                return s

            def __exit__(s, *x):
                return False
        return R()


@pytest.fixture
def web(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude" / "channels").mkdir(parents=True)
    for d in ("configs-legacy", "configs-dsh"):
        (tmp_path / d).mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(tmp_path / "configs-legacy"))
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(tmp_path / "configs-dsh"))
    monkeypatch.setenv("BOTLIFE_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("HUB_ENV_FILE", str(tmp_path / "hub.env"))

    def _no_net(sock, addr):
        raise AssertionError("用例不允许真连网络：%r" % (addr,))
    monkeypatch.setattr(socket.socket, "connect", _no_net)

    import db
    importlib.reload(db)
    assert Path(db.DB_PATH).resolve() == (tmp_path / "state.db").resolve()
    db.init()
    import moments.web as w
    monkeypatch.setattr(w.config_sources, "is_running", lambda bot_id: True)
    monkeypatch.setattr(w, "_bot_port", lambda bot_id, cfg=None: 18999)
    return w


def _wake(web, monkeypatch, bot_dir, fail=None):
    rec = _Recorder(fail)
    monkeypatch.setattr(web, "_urlopen", rec)
    web._ensure_worker_alive("lili", "77", str(bot_dir))
    assert len(rec.reqs) == 1
    return rec.reqs[0]


def test_有口令文件_请求头带上口令_去掉首尾空白(web, monkeypatch, tmp_path):
    ch = tmp_path / "ch"; ch.mkdir()
    (ch / ".api-token").write_text("  %s\n" % TOKEN, encoding="utf-8")
    req = _wake(web, monkeypatch, ch)
    assert req.full_url == "http://127.0.0.1:18999/ensure_worker"
    assert req.get_method() == "POST"
    assert req.get_header("X-api-token") == TOKEN


def test_每次现读_文件换了内容下一次就用新的(web, monkeypatch, tmp_path):
    ch = tmp_path / "ch"; ch.mkdir()
    (ch / ".api-token").write_text("old-token", encoding="utf-8")
    assert _wake(web, monkeypatch, ch).get_header("X-api-token") == "old-token"
    (ch / ".api-token").write_text(TOKEN, encoding="utf-8")
    assert _wake(web, monkeypatch, ch).get_header("X-api-token") == TOKEN


@pytest.mark.parametrize("content", [None, "", "  \n"])
def test_没有口令文件或是空的_照旧不带请求头(web, monkeypatch, tmp_path, content):
    ch = tmp_path / "ch"; ch.mkdir()
    if content is not None:
        (ch / ".api-token").write_text(content, encoding="utf-8")
    req = _wake(web, monkeypatch, ch)
    assert req.get_header("X-api-token") is None


def test_被拒时日志里没有口令(web, monkeypatch, tmp_path, capsys):
    ch = tmp_path / "ch"; ch.mkdir()
    (ch / ".api-token").write_text(TOKEN, encoding="utf-8")
    _wake(web, monkeypatch, ch, fail=OSError("HTTP Error 401: Unauthorized"))
    out = capsys.readouterr()
    assert "ensure_worker" in out.err and "lili" in out.err
    assert TOKEN not in out.err and TOKEN not in out.out
