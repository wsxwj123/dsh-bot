# -*- coding: utf-8 -*-
"""拉起旧系统 bot 的 worker 时带不带口令。

旧系统的调度器自 2026-10-11 起给本机接口加了口令（``<频道目录>/.api-token``）。8765 这段
没带，四只 bot 的唤醒全部被 401 拒掉；当时没在跑的那只就收不到朋友圈、评论、点赞的通知。
这里只测 ``_ensure_worker_alive`` 发出去的请求：有口令文件就带 ``X-Api-Token``，没有就不带，
口令不进日志。出站一律走替身，不连网络。
"""
import importlib
import socket
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

TOKEN = "tok-FAKE-wake-0123456789abcdef"
#: 真的 connect：取在 web 夹具把它换成「拦网络」之前。只有要连本机回环假件的那一条用它。
_REAL_CONNECT = socket.socket.connect


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


# ---------------------------------------------------------------- 安审 S2

@pytest.mark.parametrize("bad", ["tok\nX-Other: 1", "tok\rmore", "tok\x00x", "tok\tx", "tok\x1bx",
                                 "tok\x7fx", "tok-\u00e9"],
                         ids=["换行", "回车", "空字符", "制表符", "ESC", "DEL", "非ASCII"])
def test_口令里有换行_回车或别的控制字符_当无效_不带请求头(web, monkeypatch, tmp_path, bad):
    ch = tmp_path / "ch"; ch.mkdir()
    (ch / ".api-token").write_text(bad, encoding="utf-8")
    req = _wake(web, monkeypatch, ch)
    assert req.get_header("X-api-token") is None


def test_异常消息里带着口令原文_日志里也没有(web, monkeypatch, tmp_path, capsys):
    ch = tmp_path / "ch"; ch.mkdir()
    (ch / ".api-token").write_text(TOKEN, encoding="utf-8")
    _wake(web, monkeypatch, ch, fail=ValueError("Invalid header value b'%s'" % TOKEN))
    out = capsys.readouterr()
    assert "ensure_worker" in out.err and "lili" in out.err
    assert TOKEN not in out.err and TOKEN not in out.out


class _Fake:
    """只绑 127.0.0.1 的假件：记下每个请求 ``(方法, 路径, 请求头)``，一律回同一个应答。"""

    def __init__(self, status, location=None):
        self.hits = []
        hits = self.hits

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                hits.append((self.command, self.path, dict(self.headers)))
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._srv.daemon_threads = True
        threading.Thread(target=self._srv.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()
        self.port = self._srv.server_address[1]

    def stop(self):
        self._srv.shutdown()
        self._srv.server_close()


def test_默认出口_不走代理_不跟跳转(web, monkeypatch, tmp_path):
    """环境里有 http_proxy 时拉起请求照样直连本机；对方回跳转也不跟，口令头不会被带去别处。"""
    monkeypatch.setattr(socket.socket, "connect", _REAL_CONNECT)   # 本条要连本机回环上的假件
    target, proxy = _Fake(200), _Fake(200)
    dispatcher = _Fake(302, location="http://127.0.0.1:%d/ensure_worker" % target.port)
    try:
        for k in ("http_proxy", "HTTP_PROXY"):
            monkeypatch.setenv(k, "http://127.0.0.1:%d" % proxy.port)
        for k in ("no_proxy", "NO_PROXY", "all_proxy", "ALL_PROXY"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setattr(urllib.request, "_opener", None)    # 默认 opener 按此刻的环境现建
        monkeypatch.setattr(web, "_bot_port", lambda bot_id, cfg=None: dispatcher.port)
        ch = tmp_path / "ch"; ch.mkdir()
        (ch / ".api-token").write_text(TOKEN, encoding="utf-8")
        web._ensure_worker_alive("lili", "77", str(ch))
    finally:
        for s in (target, proxy, dispatcher):
            s.stop()
    assert proxy.hits == [], "拉起请求经过了代理：%r" % proxy.hits
    assert target.hits == [], "跟着跳转把请求（连同口令）发到了别处：%r" % target.hits
    assert [h[1] for h in dispatcher.hits] == ["/ensure_worker"], dispatcher.hits
    assert dispatcher.hits[0][2].get("X-Api-Token") == TOKEN
