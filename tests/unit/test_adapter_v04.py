# -*- coding: utf-8 -*-
"""适配服务 v0.4 里验收测试碰不到的几处：换供应商时限的读法、写请求发出后没回或断开的回法、
投递通知的几条少见路径、长 key 的摘要。

网关不起桩：``gateway_client._call`` 是适配服务到网关的唯一出口，换成记账的替身。
"""
import json
import os
import threading
from pathlib import Path

import pytest

import gateway_client
from moments import adapter_service as svc

TOKEN = "unit-adapter-token"
BOT = "botalpha"
CFG_PAIR = {"provider": "deepseek-official", "model": "deepseek-flash"}
CATALOG = {"ok": True, "current": dict(CFG_PAIR), "config": dict(CFG_PAIR),
           "choices": [dict(CFG_PAIR), {"provider": "vendor-b", "model": "vb-small"}],
           "providers": [{"name": "deepseek-official", "key": "ok", "enabled": True},
                         {"name": "vendor-b", "key": "ok", "enabled": True}]}


@pytest.fixture
def client(monkeypatch, tmp_path):
    token_file = tmp_path / "adapter.token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    monkeypatch.setenv("DSH_CONSOLE_TOKEN_FILE", str(token_file))
    return svc.create_app().test_client()


def put_bot(**cfg):
    """摆一只网关文件齐全、启用着的 bot；cfg 叠到它自己的 yml 顶层。"""
    botdir = Path(os.environ["DSH_BOT_HOME"]) / "bots" / BOT
    (botdir / "state").mkdir(parents=True, exist_ok=True)
    (botdir / "channel").mkdir(exist_ok=True)
    (botdir / "state" / "api.key").write_text("unit-gateway-key\n", encoding="utf-8")
    (botdir / "state" / "api.port").write_text("9\n", encoding="utf-8")
    base = {"id": BOT, "bot_channel_path": str(botdir / "channel"), "dispatcher_port": 0,
            "brain": dict(CFG_PAIR)}
    cfg_dir = Path(os.environ["HUB_CONFIGS_DSH_DIR"])
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / ("%s.yml" % BOT)).write_text(json.dumps({**base, **cfg}), encoding="utf-8")


def post(client, action, body):
    r = client.post("/adapter/v1/bots/%s/%s" % (BOT, action), json=body,
                    headers={"Authorization": "Bearer " + TOKEN})
    return r.status_code, r.get_json()


def test_换供应商时限_只认0到55秒之间的数_别的一律按55秒(monkeypatch):
    for raw, want in (("2", 2.0), ("0.5", 0.5), ("55", 55.0), ("", 55.0), ("abc", 55.0), ("0", 55.0),
                      ("-3", 55.0), ("60", 55.0), ("nan", 55.0), ("inf", 55.0)):
        monkeypatch.setenv("DSH_CONSOLE_PROVIDER_BUDGET", raw)
        assert svc._provider_budget() == want, raw


def test_写请求发出后没回或中途断开_回200还没确认_new_segment按已知(client, monkeypatch):
    put_bot()
    monkeypatch.setenv("DSH_CONSOLE_PROVIDER_BUDGET", "1")
    release = threading.Event()
    calls = []

    def hang_on_effort(_channel_dir, method, path, body=None, timeout=10):
        calls.append((method, path))
        if (method, path) == ("POST", "/v1/model"):
            return 200, {"ok": True, "current": {"provider": "vendor-b", "model": "vb-small"}}
        if (method, path) == ("POST", "/v1/effort"):
            release.wait(5)                         # 换模型确认了，改强度卡住不回
            return 200, {"ok": True}
        return 200, CATALOG if path == "/v1/model" else {"ok": True, "current": None}

    monkeypatch.setattr(gateway_client, "_call", hang_on_effort)
    try:
        status, got = post(client, "provider", {"provider_id": "vendor-b", "model": "vb-small",
                                                "effort": "high"})
    finally:
        release.set()
    assert status == 200 and got["message"] == svc.UNCONFIRMED, got
    assert got["new_segment"] is True, "换模型确认过、换到了另一家：new_segment 应为真"
    assert calls[-1] == ("POST", "/v1/effort")

    calls.clear()

    def reset_on_switch(_channel_dir, method, path, body=None, timeout=10):
        calls.append((method, path))
        if (method, path) == ("POST", "/v1/model"):
            raise ConnectionResetError(54, "Connection reset by peer")     # 发出去之后断了
        return 200, CATALOG if path == "/v1/model" else {"ok": True, "current": None}

    monkeypatch.setattr(gateway_client, "_call", reset_on_switch)
    status, got = post(client, "provider", {"provider_id": "vendor-b", "model": "vb-small",
                                            "effort": "high"})
    assert (status, got["new_segment"], got["message"]) == (200, False, svc.UNCONFIRMED), got
    assert ("POST", "/v1/effort") not in calls, "换模型没确认时不该再发改强度"


def test_通知_没有聊天号回500零请求_网关拒收回500说明_落单代理码回422零请求(client, monkeypatch):
    calls = []

    def refuse(_channel_dir, method, path, body=None, timeout=10):
        calls.append((method, path, body))
        return 403, {"error": "chat not allowed"}

    monkeypatch.setattr(gateway_client, "_call", refuse)
    ok_body = {"source": "user_moment", "text": "通知", "key": "user-moment:1"}
    put_bot()                                                    # 顶层没写 chat_id
    status, got = post(client, "notify", ok_body)
    assert (status, got["code"]) == (500, "internal") and "聊天号" in got["message"], got
    put_bot(chat_id=42)
    status, got = post(client, "notify", {**ok_body, "text": "\ud800"})
    assert (status, got["code"]) == (422, "invalid"), got
    assert calls == []
    status, got = post(client, "notify", ok_body)
    assert (status, got["code"]) == (500, "internal") and "拒收" in got["message"], got
    assert [(m, p, b["chat_id"]) for m, p, b in calls] == [("POST", "/v1/inject", "42")]


def test_交给网关的key_按UTF16码元数超过120才换摘要_同一个key摘要不变():
    assert svc._gateway_key("k" * 120) == "k" * 120
    emoji = "😀" * 61                                            # 61 个字，122 个 UTF-16 码元
    digest = svc._gateway_key(emoji)
    assert digest.startswith("sha256:") and len(digest) <= 120
    assert svc._gateway_key(emoji) == digest and svc._gateway_key(emoji + "x") != digest
