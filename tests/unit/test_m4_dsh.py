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
        spec = body.get("spec")
        if spec == "deepseek-official/deepseek-v4-pro":
            type(self).model = {"provider": "deepseek-official", "model": "deepseek-v4-pro"}
            return self._reply(200, {"ok": True, "text": "已换成 deepseek-official / deepseek-v4-pro", "current": type(self).model})
        return self._reply(400, {"ok": False, "text": f"没有 {spec} 这个模型。", "current": type(self).model})


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    """新系统 bot5（state 里有口令和端口）+ 旧系统 bot1（没有）；两套配置目录都指到 tmp"""
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
    # bot5 在新系统那份配置目录、bot1 在旧系统那份：管理台两套都读（moments/config_sources），
    # 但 /hub/dsh-model 只认新系统那份里的 bot。
    cfgs = tmp_path / "configs"
    cfgs.mkdir()
    dsh_cfgs = tmp_path / "dsh-configs"
    dsh_cfgs.mkdir()
    (dsh_cfgs / "bot5.yml").write_text(f"id: bot5\ndisplay_name: 五号\nbot_channel_path: {json.dumps(str(bot / 'channel'))}\n", encoding="utf-8")
    (cfgs / "bot1.yml").write_text(f"id: bot1\nbot_channel_path: {json.dumps(str(old))}\n", encoding="utf-8")
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(cfgs))
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(dsh_cfgs))
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


def _hub_client():
    from moments.hub_routes import hub_bp
    app = Flask(__name__, template_folder=str(ROOT / "moments" / "templates"))
    app.register_blueprint(hub_bp)
    return app.test_client()


_SAVE_PROVIDER = "/hub/api/dsh-model/provider/save"


def _save_body(base_url, key="sk-unit-000000000001"):
    return {"name": "myproxy", "api": "openai-completions", "baseURL": base_url,
            "key": key, "mode": "create"}


def _forwarded():
    """假网关收到的 /v1/provider/save 调用（管理台转发没转发，看这里）。"""
    return [call for call in _FakeGateway.calls if call[1] == "/v1/provider/save"]


def test_保存供应商_内网地址在管理台侧就被拒且不转发给网关(gateway, monkeypatch):
    """新页面这条保存路径必须与老路径（POST /hub/api/provider）同一把 SSRF 尺子。

    老路径走 provider_model._check_base_url，内网、回环、链路本地、保留网段一律拒。
    这里钉住审计实测的两条（127.0.0.1 的任意端口、169.254.169.254）加几个同类。
    被拒就不能转发：转发就等于让持密钥的网关去打内网。
    """
    from moments import provider_model
    monkeypatch.setattr(provider_model, "resolve_host", lambda host: [])   # 单测不联网
    monkeypatch.delenv("HUB_ALLOW_PRIVATE_BASE_URL", raising=False)
    c = _hub_client()
    for url in ("http://127.0.0.1:8317/v1", "https://169.254.169.254/latest/meta-data",
                "http://10.1.2.3/v1", "http://192.168.0.1/v1", "http://[::1]/v1",
                "http://198.18.0.1/v1"):
        r = c.post(_SAVE_PROVIDER, json=_save_body(url))
        assert r.status_code == 400, "%s 应被拒，实得 %s" % (url, r.status_code)
        j = r.get_json()
        assert j["ok"] is False and j["error"] == "bad_url", "%s 实得 %s" % (url, j)
        assert j["text"], "%s 的正文要有原因，实得 %s" % (url, j)
    assert _forwarded() == [], "被拒的地址一个都不该转发给网关"


def test_保存供应商_公网地址放行_转发给网关(gateway, monkeypatch):
    """另一侧：公网地址照旧放行（解析不出按放行处理，这是老尺子的既有口径）。"""
    from moments import provider_model
    monkeypatch.setattr(provider_model, "resolve_host", lambda host: [])
    monkeypatch.delenv("HUB_ALLOW_PRIVATE_BASE_URL", raising=False)
    c = _hub_client()
    c.post(_SAVE_PROVIDER, json=_save_body("https://api.example.com/v1"))
    assert _forwarded(), "公网地址该转发给网关，实得 %s" % _FakeGateway.calls


def test_保存供应商_逃生门开着时内网地址放行_转发给网关(gateway, monkeypatch):
    """HUB_ALLOW_PRIVATE_BASE_URL=1 是老路径的逃生门（真在内网自建网关的用户），新路径同一扇门。"""
    monkeypatch.setenv("HUB_ALLOW_PRIVATE_BASE_URL", "1")
    c = _hub_client()
    c.post(_SAVE_PROVIDER, json=_save_body("http://127.0.0.1:8088/v1"))
    assert _forwarded(), "开了逃生门就该转发，实得 %s" % _FakeGateway.calls


def test_保存供应商_网关客户端的本地错误翻成写端点JSON形状(gateway, monkeypatch):
    """非网络异常（响应体不是 JSON、读不到 state 文件之类）也要是 {"ok","error","text"}。

    冒泡出去会变成框架级 500，没有 text 可显示，页面只能报"失败"。
    """
    def boom(ch, body):
        raise ValueError("boom")               # 本地错误：不是 URLError，也不是超时或中断
    monkeypatch.setattr(gateway_client, "provider_save", boom)
    c = _hub_client()
    r = c.post(_SAVE_PROVIDER, json=_save_body("https://api.example.com/v1"))
    assert r.status_code == 500, "实得 %s" % r.status_code
    j = r.get_json() or {}
    assert j.get("ok") is False and j.get("error") == "internal" and j.get("text"), "实得 %s" % j


def test_管理台换新系统bot的模型(gateway):
    c = _hub_client()
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
