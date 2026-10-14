# -*- coding: utf-8 -*-
"""适配服务换供应商（INTERFACE-适配服务 4.4）前三步的先后：没过时对任何网关零请求。

bot 照真配置的形状摆在总闸给的临时目录里，名单、启停走真函数。网关不起桩：
``gateway_client._call`` 是适配服务到网关的唯一出口，换成记账的替身，谁走到都记下来。

第 4、5 步（读清单、校验、写请求）的单测还没有：清单的字段形状未定，见交回报告。
"""
import json
import os
from pathlib import Path

import pytest

import gateway_client
from moments import adapter_service as svc

TOKEN = "unit-adapter-token"
BOT = "botalpha"
SWITCH = {"provider_id": "fake-vendor-b", "model": "vb-small"}


@pytest.fixture
def gateway_calls(monkeypatch):
    """走到网关出口的每一次调用 ``(方法, 路径)``。替身不应答：走到了就是用例要抓的错。"""
    calls = []

    def _call(_channel_dir, method, path, *_a, **_kw):
        calls.append((method, path))
        raise AssertionError("不该走到网关：%s %s" % (method, path))

    monkeypatch.setattr(gateway_client, "_call", _call)
    return calls


@pytest.fixture
def client(monkeypatch, tmp_path):
    token_file = tmp_path / "adapter.token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    monkeypatch.setenv("DSH_CONSOLE_TOKEN_FILE", str(token_file))
    return svc.create_app().test_client()


def _put_bot(enabled):
    """摆一只网关文件齐全的 bot：停用的那只也"连得上"，先后错了才会真的走到网关出口。"""
    botdir = Path(os.environ["DSH_BOT_HOME"]) / "bots" / BOT
    state, channel = botdir / "state", botdir / "channel"
    state.mkdir(parents=True, exist_ok=True)
    channel.mkdir(exist_ok=True)
    (state / "api.key").write_text("unit-gateway-key\n", encoding="utf-8")
    (state / "api.port").write_text("9\n", encoding="utf-8")
    (channel / ".env").write_text("TELEGRAM_BOT_TOKEN=unit-fake\nTELEGRAM_CHAT_ID=-1\n",
                                  encoding="utf-8")
    cfg = {"id": BOT, "display_name": BOT, "bot_channel_path": str(channel), "dispatcher_port": 0,
           "brain": {"provider": "deepseek-official", "model": "deepseek-flash"}}
    if not enabled:
        cfg["enabled"] = False
    cfg_dir = Path(os.environ["HUB_CONFIGS_DSH_DIR"])
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / ("%s.yml" % BOT)).write_text(json.dumps(cfg), encoding="utf-8")


def _post(client, body):
    r = client.post("/adapter/v1/bots/%s/provider" % BOT, json=body,
                    headers={"Authorization": "Bearer " + TOKEN})
    return r.status_code, r.get_json()


def test_前提_这只bot在名单里_启用时带provider_id走得过前三步(client, gateway_calls):
    """下面几条的 409、422、501 要有意义，先得确认这只 bot 摆得合格：不是 404，也不是 409。"""
    _put_bot(enabled=True)
    status, body = _post(client, SWITCH)
    assert body["code"] not in ("unknown_bot", "stopped", "invalid", "unsupported"), (status, body)


EARLY = {
    "第1步_带reset为假": ({"reset": False, **SWITCH}, 501, "unsupported"),
    "第1步_带reset为真": ({"reset": True}, 501, "unsupported"),
    "第2步_只给provider_id": ({"provider_id": "fake-vendor-b"}, 422, "invalid"),
    "第2步_model是空串": ({"provider_id": "fake-vendor-b", "model": " "}, 422, "invalid"),
    "第2步_强度不在四档": ({**SWITCH, "effort": "medium"}, 422, "invalid"),
    "第2步_三样都没有": ({}, 422, "invalid"),
}


@pytest.mark.parametrize("enabled", [True, False], ids=["启用", "停用"])
@pytest.mark.parametrize("body,status,code", list(EARLY.values()), ids=list(EARLY))
def test_第1第2步没过_先于停用判断_对任何网关零请求(client, gateway_calls, body, status, code, enabled):
    _put_bot(enabled=enabled)
    got_status, got = _post(client, body)
    assert (got_status, got["code"]) == (status, code), got
    assert gateway_calls == [], "前两步没过就不该找网关：%r" % gateway_calls


STOPPED = {
    "带provider_id和model": dict(SWITCH),
    "只带effort": {"effort": "high"},
    "三样都带": {**SWITCH, "effort": "high"},
}


@pytest.mark.parametrize("body", list(STOPPED.values()), ids=list(STOPPED))
def test_第3步_bot停用_回409_stopped_先于读清单_对任何网关零请求(client, gateway_calls, body):
    _put_bot(enabled=False)
    status, got = _post(client, body)
    assert (status, got["code"]) == (409, "stopped"), got
    assert gateway_calls == [], "停用的 bot 不该为它读清单，也不该发写请求：%r" % gateway_calls
