# -*- coding: utf-8 -*-
"""M4：Python 周边接新网关——旧通知里的 Bash 命令换成工具用法、给网关投递系统消息、
管理台换新系统 bot 的模型、查别的 bot 的朋友圈不列私密圈。"""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from flask import Flask

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "TESTONLY-api-key"

import gateway_client  # noqa: E402


class _FakeGateway(BaseHTTPRequestHandler):
    calls = []
    model = {"provider": "deepseek-official", "model": "deepseek-flash"}

    def log_message(self, *a):
        pass

    def _reply(self, code, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        if self.headers.get("authorization") != f"Bearer {TOKEN}":
            self._reply(401, {"error": "unauthorized"})
            return False
        return True

    def do_GET(self):
        if not self._authed():
            return
        type(self).calls.append(("GET", self.path, None))
        if self.path == "/v1/providers":
            return self._reply(200, {"providers": [{"name": "myproxy", "displayName": "MyProxy", "api": "anthropic-messages", "models": 2, "fetchedAt": 1, "fetchError": None}]})
        self._reply(200, {"current": type(self).model, "config": {"provider": "deepseek-official", "model": "deepseek-flash"},
                          "choices": [{"provider": "deepseek-official", "model": "deepseek-flash"},
                                      {"provider": "deepseek-official", "model": "deepseek-v4-pro"}]})

    def do_POST(self):
        if not self._authed():
            return
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        type(self).calls.append(("POST", self.path, body))
        if self.path == "/v1/inject":
            return self._reply(200, {"ok": True, "id": 1, "duplicate": False})
        if self.path == "/v1/providers/refresh":
            ok = body.get("name") == "myproxy"
            return self._reply(200 if ok else 400, {"ok": ok, "text": "【系统】「MyProxy」拉到 2 个模型。" if ok else "【系统】没有用 /provider add 建过", "models": 2 if ok else 0})
        spec = body.get("spec")
        if spec == "deepseek-official/deepseek-v4-pro":
            type(self).model = {"provider": "deepseek-official", "model": "deepseek-v4-pro"}
            return self._reply(200, {"ok": True, "text": "已换成 deepseek-official / deepseek-v4-pro", "current": type(self).model})
        return self._reply(400, {"ok": False, "text": f"没有 {spec} 这个模型。", "current": type(self).model})


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    """新系统 bot5（state 里有口令和端口）+ 旧系统 bot1（没有），configs 指到 tmp"""
    _FakeGateway.calls = []
    _FakeGateway.model = {"provider": "deepseek-official", "model": "deepseek-flash"}
    srv = HTTPServer(("127.0.0.1", 0), _FakeGateway)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    bot = tmp_path / "bots" / "bot5"
    (bot / "channel").mkdir(parents=True)
    (bot / "state").mkdir()
    (bot / "state" / "api.key").write_text(TOKEN + "\n", encoding="utf-8")
    (bot / "state" / "api.port").write_text(str(srv.server_port), encoding="utf-8")
    old = tmp_path / "old" / "channel"
    old.mkdir(parents=True)
    cfgs = tmp_path / "configs"
    cfgs.mkdir()
    (cfgs / "bot5.yml").write_text(f"id: bot5\ndisplay_name: 五号\nbot_channel_path: {json.dumps(str(bot / 'channel'))}\n", encoding="utf-8")
    (cfgs / "bot1.yml").write_text(f"id: bot1\nbot_channel_path: {json.dumps(str(old))}\n", encoding="utf-8")
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(cfgs))
    yield str(bot / "channel"), str(old)
    srv.shutdown()


def test_旧通知里的Bash命令换成工具用法():
    old = ('想回就用 Bash 工具执行：\n'
           'python3 /x/scripts/moment_reply.py 12 0 "<你的回复>" [--image <图片路径>]\n'
           '想点赞：python3 /x/scripts/moment_like.py 12\n'
           '要配图就用 novelai-skill 生成')
    new = gateway_client.for_dsh(old)
    assert "Bash" not in new and "python3" not in new and "novelai-skill" not in new
    assert "moments 工具：action=reply，moment_id=12，parent_comment_id=0" in new
    assert "moments 工具：action=like，moment_id=12" in new
    assert "generate_image 工具" in new


def test_投递系统消息_带口令和电话里bot说过的话(gateway):
    ch, old = gateway
    assert gateway_client.available(ch) is True
    assert gateway_client.available(old) is False
    assert gateway_client.inject(ch, "42", "⟦系统·电话⟧ 回顾", "call", "k1", bot_lines=["三点提醒你"]) is True
    method, path, body = _FakeGateway.calls[-1]
    assert (method, path) == ("POST", "/v1/inject")
    assert body == {"chat_id": "42", "source": "call", "text": "⟦系统·电话⟧ 回顾", "key": "k1", "bot_lines": ["三点提醒你"]}


def test_管理台换新系统bot的模型(gateway):
    from moments.hub_routes import hub_bp
    app = Flask(__name__, template_folder=str(ROOT / "moments" / "templates"))
    app.register_blueprint(hub_bp)
    c = app.test_client()
    page = c.get("/hub/dsh-model").get_data(as_text=True)
    assert 'href="/hub/dsh-model"' in page and page.count('class="chip on"') == 1
    bots = c.get("/hub/api/dsh-model").get_json()["bots"]
    assert [b["id"] for b in bots] == ["bot5"]                # 旧系统的 bot 不出现
    assert bots[0]["name"] == "五号" and bots[0]["current"]["model"] == "deepseek-flash"
    r = c.post("/hub/api/dsh-model/bot5", json={"spec": "deepseek-official/deepseek-v4-pro"})
    assert r.status_code == 200 and r.get_json()["current"]["model"] == "deepseek-v4-pro"
    r = c.post("/hub/api/dsh-model/bot5", json={"spec": "nope"})
    assert r.status_code == 400 and "没有 nope" in r.get_json()["text"]
    assert c.post("/hub/api/dsh-model/bot1", json={"spec": "x"}).status_code == 404
    assert c.post("/hub/api/dsh-model/bot5", json=["x"]).status_code == 400
    assert c.post("/hub/api/dsh-model/..%2Fx", json={"spec": "x"}).status_code in (400, 404)


def test_查别的bot的朋友圈不列它的私密圈(tmp_path):
    env = {**os.environ, "BOTLIFE_STATE_DB": str(tmp_path / "state.db"), "TELEGRAM_WORKER_BOT": "bot3", "PYTHONIOENCODING": "utf-8"}
    seed = ("import db, time; db.init(); now=int(time.time());"
            "db.insert_moment('bot3', now-60, '公开的圈', '', {}, 'daily');"
            "db.insert_moment('bot3', now-30, '私密的圈', '', {}, 'daily', 'private')")
    subprocess.run([sys.executable, "-c", seed], cwd=ROOT, env=env, check=True)
    script = str(ROOT / "scripts" / "recent_moments.py")
    other = subprocess.run([sys.executable, script, "bot3"], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8").stdout
    assert "公开的圈" in other and "私密的圈" not in other
    own = subprocess.run([sys.executable, script, "self"], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8").stdout
    assert "私密的圈" in own
    assert not (ROOT / "state.db").exists() or "私密的圈" not in (ROOT / "state.db").read_bytes().decode("utf-8", "ignore")


def test_新系统的bot用旧名字也能找到配置(tmp_path, monkeypatch):
    """朋友圈、画风里记的是旧名字：网关跑朋友圈脚本时用旧名字，脚本要能找到新系统的配置"""
    import config_loader
    old = tmp_path / "oldcfg" / "chen.yml"
    old.parent.mkdir()
    old.write_text("id: chen\nchat_id: '1'\nbot_channel_path: /old/channel\n", encoding="utf-8")
    cfgs = tmp_path / "configs"
    cfgs.mkdir()
    (cfgs / "bot5.yml").write_text(f"id: bot5\nbot_channel_path: /new/channel\nlife_config: {json.dumps(str(old))}\n", encoding="utf-8")
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(cfgs))
    cfg = config_loader.load_bot("chen")
    assert cfg["bot_channel_path"] == "/new/channel" and cfg["chat_id"] == "1"
    assert cfg["_life_id"] == "chen"                          # 朋友圈网页按这个名字找头像、认"自己的评论"
    assert config_loader.load_bot("bot5")["bot_channel_path"] == "/new/channel"
    with pytest.raises(FileNotFoundError):
        config_loader.load_bot("nobody")


def test_管理台列出用provider_add建的供应商并能刷新模型列表(gateway):
    from moments.hub_routes import hub_bp
    app = Flask(__name__, template_folder=str(ROOT / "moments" / "templates"))
    app.register_blueprint(hub_bp)
    c = app.test_client()
    assert "用 /provider add 建的供应商" in c.get("/hub/dsh-model").get_data(as_text=True)
    ps = c.get("/hub/api/dsh-providers").get_json()["providers"]
    assert ps == [{"name": "myproxy", "displayName": "MyProxy", "api": "anthropic-messages", "models": 2, "fetchedAt": 1, "fetchError": None}]
    r = c.post("/hub/api/dsh-providers/myproxy/refresh")
    assert r.status_code == 200 and r.get_json()["text"] == "「MyProxy」拉到 2 个模型。"
    assert _FakeGateway.calls[-1] == ("POST", "/v1/providers/refresh", {"name": "myproxy"})
    assert c.post("/hub/api/dsh-providers/nope/refresh").status_code == 400
    assert c.post("/hub/api/dsh-providers/a_b/refresh").status_code == 400
