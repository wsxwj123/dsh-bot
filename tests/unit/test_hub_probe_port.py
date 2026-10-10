# -*- coding: utf-8 -*-
"""管理台探活按"生效那份配置"取端口，不再照多根合并的注册表。

真机形态与 tests/unit/test_moments_delivery_port.py 同一套。bot2 在两套配置根里
各有一份，旧根那份在跑（真 HTTP 服务，端口 A），新根那份 enabled: false、写着
另一个端口 B（没人听）。注册表按"新系统优先"合并，照它探活会打 B 永远离线，
页面上"来源 legacy、启用中、状态离线"三个标注互相打脸。

这里在 A 上起真服务、真的发 /status，用服务端收到的请求量出探活打到哪个端口，
不看实现内部。/hub/api/bots 那一页只管新系统的 bot：旧栈在跑的那只不进名单、
不探活（它由旧面板与桌面脚本管）；进名单的行，来源、停用徽标、在线状态三个
标注必须一致。
"""
import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from moments import bots_client


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _StatusHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.server.hits.append(self.path)
        body = json.dumps({"phase": "ready", "pid": 4321, "queue_depth": 0,
                           "in_flight": 0}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.wfile.write(body)

    def log_message(self, *a):     # 别把探活打到测试输出里
        pass


@pytest.fixture
def live_status():
    """真 HTTP 服务，扮演"旧栈在跑的那个 dispatcher"。返回服务对象，端口取 server_address。"""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _StatusHandler)
    srv.hits = []
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """两套 tmp 配置根 + 钉住的 HOME（默认的 ~/.dsh-bot 绝不落到真家目录）。"""
    for k in list(os.environ):
        if k.startswith("DISPATCHER_PORT_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("HUB_BOTS_FILE", raising=False)
    home = tmp_path / "home"
    legacy = tmp_path / "legacy"
    dsh = tmp_path / "dsh"
    for d in (home, legacy, dsh):
        d.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(legacy))
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(dsh))
    return legacy, dsh


def _write_bot(root, name, **fields):
    lines = ["id: %s" % name]
    for k, v in sorted(fields.items()):
        if isinstance(v, bool):
            v = "true" if v else "false"
        lines.append("%s: %s" % (k, v))
    (Path(root) / ("%s.yml" % name)).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _mixed_bot2(roots, live_status, dsh_port):
    """bot2 旧根在跑（端口 = live_status 真服务），新根停用写着 dsh_port。"""
    legacy, dsh = roots
    _write_bot(legacy, "bot2", display_name="李彤彤",
               dispatcher_port=live_status.server_address[1])
    _write_bot(dsh, "bot2", enabled=False, display_name="李彤彤", dispatcher_port=dsh_port)


def test_旧栈在跑新栈停用_探活打到旧栈端口(roots, live_status):
    """先确认注册表口径确实是"新系统优先"（旧实现会打 dsh_port），再量探活实际打哪。"""
    dsh_port = free_port()
    _mixed_bot2(roots, live_status, dsh_port)
    assert bots_client.bot_ports()["bot2"] == dsh_port, "注册表合并口径变了，本用例前提失效"

    rows = bots_client.probe_bots([{"id": "bot2", "display_name": "李彤彤"}])
    row = rows[0]
    assert row["port"] == live_status.server_address[1], (
        "探活打到了 %s，应该是旧栈在跑那份写的端口 %s" % (row["port"],
                                                        live_status.server_address[1]))
    assert row["state"] == "online", "探到了在跑的那份，状态必须是在线。实得 %s" % row["state"]
    assert live_status.hits == ["/status"], "真服务收到的请求 %s" % live_status.hits


def _hub_client():
    from flask import Flask

    from moments.hub_routes import hub_bp
    app = Flask(__name__)
    app.register_blueprint(hub_bp)
    app.config["TESTING"] = True
    return app.test_client()


def test_旧栈在跑的bot不进这一页(roots, live_status):
    """bot2 旧根在跑、新根停用：它由旧系统管，四个出口都不出现，也不为它探活。

    这一页的开关写的是新系统那份配置，旧系统的运行时看的却是它自己那份标记，
    摆一行点不动的开关就是用户报的那个静默说谎。探活打过去也只是白打旧系统
    的 dispatcher。
    """
    dsh_port = free_port()
    _mixed_bot2(roots, live_status, dsh_port)
    body = _hub_client().get("/hub/api/bots").get_json()
    assert body["bots"] == []
    assert body["sources"] == {} and body["disabled"] == []
    assert live_status.hits == [], "为旧系统跑着的 bot 发了探活请求：%s" % live_status.hits


def test_新系统在跑的bot_来源停用在线三个标注指向同一份(roots, live_status):
    """新根那份在跑（live_status 端口）、旧根那份停用：来源说 dsh、不在 disabled、
    状态 online，三者不许互相打脸。"""
    legacy, dsh = roots
    _write_bot(dsh, "bot2", display_name="李彤彤",
               dispatcher_port=live_status.server_address[1])
    _write_bot(legacy, "bot2", enabled=False, display_name="李彤彤",
               dispatcher_port=free_port())
    body = _hub_client().get("/hub/api/bots").get_json()
    row = [b for b in body["bots"] if b["id"] == "bot2"][0]
    assert body["sources"]["bot2"]["source"] == "dsh"
    assert "bot2" not in body["disabled"]
    assert (row["port"], row["state"]) == (live_status.server_address[1], "online")


def test_接缝名单态_端口仍按注册表口径(roots, live_status, tmp_path, monkeypatch):
    """HUB_BOTS_FILE 注入名单时名单与配置目录无关，端口不许去猜配置目录里的那份。"""
    p = tmp_path / "bots.json"
    p.write_text(json.dumps([{"id": "bot2"}]), encoding="utf-8")
    monkeypatch.setenv("HUB_BOTS_FILE", str(p))
    _mixed_bot2(roots, live_status, free_port())
    assert bots_client.probe_bots(bots_client.list_bots())[0]["port"] == \
        bots_client.bot_ports()["bot2"]
