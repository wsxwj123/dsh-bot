# -*- coding: utf-8 -*-
"""M5：导演接新系统——名单从注册表读、点名投给网关（带口令）、看得到别的 bot 刚说的话、"闭嘴"锁、接话额度、调模型失败退避。"""
import importlib
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GROUP = "-1001"
TOKEN = "TESTONLY-api-key"


class _Gateway(BaseHTTPRequestHandler):
    calls: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        if self.headers.get("authorization") != f"Bearer {TOKEN}":
            self.send_response(401); self.end_headers(); return
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
        type(self).calls.append((self.server.bot, body))
        data = b'{"ok": true, "id": 1, "duplicate": false}'
        self.send_response(200); self.send_header("content-length", str(len(data))); self.end_headers(); self.wfile.write(data)


@pytest.fixture
def d(tmp_path, monkeypatch):
    """新系统布局：两个 bot（a、b）都在群 -1001 里，各有一个假网关"""
    _Gateway.calls = []
    servers = []
    cfgs = tmp_path / "configs"
    cfgs.mkdir()
    for bid, uname in (("a", "bot_a"), ("b", "bot_b")):
        bot = tmp_path / "bots" / bid
        (bot / "channel").mkdir(parents=True)
        (bot / "state").mkdir()
        (bot / "channel" / "access.json").write_text(json.dumps({"allowFrom": ["42"], "groups": {GROUP: {}}}), encoding="utf-8")
        (bot / "state" / "bot.json").write_text(json.dumps({"id": 1, "username": uname, "name": bid.upper()}), encoding="utf-8")
        srv = HTTPServer(("127.0.0.1", 0), _Gateway)
        srv.bot = bid
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        (bot / "state" / "api.key").write_text(TOKEN, encoding="utf-8")
        (bot / "state" / "api.port").write_text(str(srv.server_port), encoding="utf-8")
        (cfgs / f"{bid}.yml").write_text(f"id: {bid}\ndisplay_name: 角色{bid.upper()}\nbot_channel_path: {json.dumps(str(bot / 'channel'))}\n", encoding="utf-8")
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(cfgs))
    monkeypatch.setenv("DSH_BOT_HOME", str(tmp_path))
    for k in ("DIRECTOR_GT_DIR", "DIRECTOR_MARKER_DIR", "DIRECTOR_STATE_DIR", "DIRECTOR_MODE_DIR", "DIRECTOR_HUMAN_ID"):
        monkeypatch.delenv(k, raising=False)
    sys.path.insert(0, str(ROOT))
    import director
    director = importlib.reload(director)
    assert director.load_dsh_roster(GROUP)
    os.makedirs(os.path.join(tmp_path, "director", "mode"), exist_ok=True)
    open(os.path.join(tmp_path, "director", "mode", GROUP), "w").close()
    director.tmp = tmp_path
    yield director
    for s in servers:
        s.shutdown()


def say(d, mid, ts, text, who=None):
    """往共用的群聊记录写一行（格式同网关）：who=None 是真人，否则是那个 bot 的用户名"""
    os.makedirs(os.path.join(d.tmp, "groups"), exist_ok=True)
    line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts)), "chat_id": GROUP, "message_id": mid,
            "from_id": "42" if who is None else "1", "from_username": who, "from_name": "x", "is_bot": who is not None,
            "text": text, "observed_by": "bot_a"}
    with open(os.path.join(d.tmp, "groups", f"{GROUP}.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def test_名单来自注册表_主人取白名单第一个(d):
    assert d.BOTS == {"a": "角色A", "b": "角色B"}
    assert d.BOT_BY_USERNAME["bot_a"] == "a" and d.BOT_BY_USERNAME["@bot_b"] == "b"
    assert d.HUMAN_ID == "42"


def test_点名投给网关_被点名的bot看得到别的bot刚说的话(d):
    now = time.time()
    say(d, 1, now - 30, "大家好")
    say(d, 2, now - 20, "我是A，刚下班", who="bot_a")
    say(d, 3, now - 10, "B 你呢")
    d.call_claude_json = lambda prompt, **kw: {"speak": True, "who": "b"}
    r = d.tick(GROUP, now)
    assert r["action"] == "inject" and r["who"] == "b", r
    bot, body = _Gateway.calls[-1]
    assert bot == "b" and body["chat_id"] == GROUP and body["source"] == "director"
    assert body["text"].startswith("⟦群聊·导演点到你了⟧")
    assert "我是A，刚下班" in body["text"] and "角色A" in body["text"]


def test_闭嘴锁住_不再点名(d):
    now = time.time()
    say(d, 1, now - 10, "闭嘴")
    d.call_claude_json = lambda prompt, **kw: {"speak": True, "who": "a"}
    assert d.tick(GROUP, now)["action"] == "lock"
    say(d, 2, now + 5, "在吗")
    assert d.tick(GROUP, now + 20)["action"] == "locked"
    assert _Gateway.calls == []


def test_接话额度用完不调模型(d):
    now = time.time()
    say(d, 1, now - 100, "聊吧")
    for i in range(d.MAX_HEAT):
        say(d, 10 + i, now - 90 + i, f"bot 说 {i}", who="bot_a" if i % 2 else "bot_b")
    d.call_claude_json = lambda prompt, **kw: pytest.fail("额度用完不该调模型")
    r = d.decide(GROUP, history=d.read_group_history(GROUP, n=12))
    assert r["speak"] is False and r["heat"] <= 0


def test_调模型失败_退避_越来越久_有上限(d):
    d.call_claude_json = lambda prompt, **kw: None
    t0 = time.time()
    assert d._ask("x") == {}
    first = d._LLM_RETRY_AT - t0
    for _ in range(12):
        d._ask("x")
    assert 4 <= first <= 6
    assert d._LLM_RETRY_AT - time.time() <= 600 + 1
    d.call_claude_json = lambda prompt, **kw: {"speak": False}
    d._ask("x")
    assert d._LLM_FAILS == 0
