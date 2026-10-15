# -*- coding: utf-8 -*-
"""适配服务换供应商（INTERFACE-适配服务 4.4）五步的先后。

前三步没过时对任何网关零请求；第 4 步向这只 bot 的网关读一次清单，没过时只有那一次读、
没有写请求；第 5 步都过了才写，强度被拒时把供应商和模型换回去。

bot 照真配置的形状摆在总闸给的临时目录里，名单、启停走真函数。网关不起桩：
``gateway_client._call`` 是适配服务到网关的唯一出口，换成记账的替身，谁走到都记下来。
"""
import json
import os
import urllib.error
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
    # 13.7：停着的不再 409。只改强度直接写进配置回 queued；带供应商的要向在跑的 bot 核对，这里一只都没有，503
    want = (200, None) if "provider_id" not in body else (503, "unavailable")
    assert (status, got.get("code")) == want, got
    assert gateway_calls == [], "不该找停着的 bot 自己的网关，也不该发写请求：%r" % gateway_calls


# ---------------------------------------------------------------- 第 4、5 步

GW_MODEL, GW_EFFORT = "/v1/model", "/v1/effort"
CFG_PAIR = {"provider": "deepseek-official", "model": "deepseek-flash"}     # 配置文件里写的那一组


def _model_reply(current):
    """``GET /v1/model`` 的返回体（补充文件 5.1 的形状）。停用那家的模型也在 choices 里。"""
    choices = [("deepseek-official", "deepseek-flash"), ("fake-vendor-b", "vb-small"),
               ("deepseek-official", "deepseek-pro"), ("fake-vendor-off", "off-1")]
    return {"ok": True, "current": dict(current), "config": dict(CFG_PAIR),
            "choices": [{"provider": p, "model": m} for p, m in choices],
            "providers": [
                {"name": "deepseek-official", "models": 2, "key": "ok", "keyEnv": None, "enabled": True},
                {"name": "fake-vendor-b", "models": 1, "key": "ok", "keyEnv": None, "enabled": True},
                {"name": "fake-vendor-off", "models": 1, "key": "ok", "keyEnv": None, "enabled": False}]}


class FakeGateway:
    """这只 bot 的网关：照补充文件第 5 节应答四种请求，记下每一次 ``(方法, 路径, 请求体)``。

    起初生效的是配置文件那一组。换模型时清掉单独指定的强度（适配服务正是为此先记下原来的
    强度）。``refuse_effort`` 里的档位回 409：这只 bot 现在用的模型不支持它。
    ``effort_answer``：改强度时换成这个应答，``(状态码, 返回体)`` 或要抛的异常；状态码是 200 的
    （以及抛异常的，模拟 200 但回体不是 JSON）算已经保存。``model_writes_ok`` 是整数时，换模型只成功
    这么多次，之后拒连（用来让「换回去」失败）。
    """

    def __init__(self):
        self.current, self.effort, self.refuse_effort = dict(CFG_PAIR), None, ()
        self.effort_answer, self.model_writes_ok = None, None
        self.calls = []

    def __call__(self, _channel_dir, method, path, body=None, timeout=10):
        self.calls.append((method, path, body))
        if (method, path) == ("GET", GW_MODEL):
            return 200, _model_reply(self.current)
        if (method, path) == ("POST", GW_MODEL):
            if self.model_writes_ok is not None:
                if self.model_writes_ok <= 0:
                    raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
                self.model_writes_ok -= 1
            provider, model = body["spec"].split("/", 1)
            self.current, self.effort = {"provider": provider, "model": model}, None
            return 200, {"ok": True, "text": "ok", "current": dict(self.current)}
        if (method, path) == ("GET", GW_EFFORT):
            return 200, {"ok": True, "current": self.effort}
        if (method, path) == ("POST", GW_EFFORT):
            if body["effort"] in self.refuse_effort:
                return 409, {"ok": False, "error": "unsupported"}
            answer, self.effort_answer = self.effort_answer, None     # 只顶替这一次
            if answer is None or isinstance(answer, BaseException) or answer[0] == 200:
                self.effort = body["effort"]
            if isinstance(answer, BaseException):
                raise answer
            return answer or (200, {"ok": True})
        return 404, {}


@pytest.fixture
def gateway(monkeypatch):
    gw = FakeGateway()
    monkeypatch.setattr(gateway_client, "_call", gw)
    return gw


READ_CATALOG = ("GET", GW_MODEL, None)

STEP4 = {
    "没有这家供应商": ({"provider_id": "no-such-vendor", "model": "vb-small"}, 404, "unknown_provider"),
    "网关标了停用的那家": ({"provider_id": "fake-vendor-off", "model": "off-1"}, 404, "unknown_provider"),
    "别家才有的模型": ({"provider_id": "fake-vendor-b", "model": "deepseek-flash"}, 422, "invalid"),
    "哪家都没有的模型": ({"provider_id": "fake-vendor-b", "model": "no-such-model"}, 422, "invalid"),
}


@pytest.mark.parametrize("effort", [None, "high"], ids=["不带强度", "带强度"])
@pytest.mark.parametrize("body,status,code", list(STEP4.values()), ids=list(STEP4))
def test_第4步_清单校验没过_只有那一次读清单_没有写请求(client, gateway, body, status, code, effort):
    _put_bot(enabled=True)
    if effort:
        body = {**body, "effort": effort}
    got_status, got = _post(client, body)
    assert (got_status, got["code"]) == (status, code), got
    assert gateway.calls == [READ_CATALOG], "第 4 步没过：只许那一次读，不许写：%r" % gateway.calls


READ_FAILS = {"拒连": urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")),
              "超时": TimeoutError("timed out")}


@pytest.mark.parametrize("error", list(READ_FAILS.values()), ids=list(READ_FAILS))
def test_第4步_读清单时网关连不上或没回应_回503_什么都没写(client, monkeypatch, error):
    _put_bot(enabled=True)
    calls = []

    def _call(_channel_dir, method, path, body=None, timeout=10):
        calls.append((method, path, body))
        raise error

    monkeypatch.setattr(gateway_client, "_call", _call)
    status, got = _post(client, SWITCH)
    assert (status, got["code"]) == (503, "unavailable"), got
    assert calls == [READ_CATALOG], calls


def test_第4步_清单认不出_回500_内部错误_什么都没写(client, monkeypatch):
    _put_bot(enabled=True)
    calls = []

    def _call(_channel_dir, method, path, body=None, timeout=10):
        calls.append((method, path, body))
        return 200, {"ok": True, "current": dict(CFG_PAIR)}          # 没有 providers

    monkeypatch.setattr(gateway_client, "_call", _call)
    status, got = _post(client, SWITCH)
    assert (status, got) == (500, {"ok": False, "code": "internal", "message": "内部错误"})
    assert calls == [READ_CATALOG], calls


def test_第5步_校验过了才写_一次读一次写_换到另一家new_segment为真(client, gateway):
    _put_bot(enabled=True)
    status, got = _post(client, SWITCH)
    assert status == 200 and got["new_segment"] is True, got
    assert gateway.calls == [READ_CATALOG, ("POST", GW_MODEL, {"spec": "fake-vendor-b/vb-small"})]


@pytest.mark.parametrize("prior", ["low", None], ids=["原来单独指定过强度", "原来没有单独指定"])
def test_第5步_强度被拒_把供应商和模型换回去_原来的强度设回去(client, gateway, prior):
    _put_bot(enabled=True)
    gateway.effort, gateway.refuse_effort = prior, ("max",)
    status, got = _post(client, {**SWITCH, "effort": "max"})
    assert (status, got["code"]) == (422, "invalid"), got
    restore = [("POST", GW_EFFORT, {"effort": prior})] if prior else []
    assert gateway.calls == [
        READ_CATALOG,
        ("GET", GW_EFFORT, None),                                     # 先记下原来的强度
        ("POST", GW_MODEL, {"spec": "fake-vendor-b/vb-small"}),
        ("POST", GW_EFFORT, {"effort": "max"}),                       # 被拒
        ("POST", GW_MODEL, {"spec": "deepseek-official/deepseek-flash"}),   # 换回去
    ] + restore, gateway.calls
    assert (gateway.current, gateway.effort) == (CFG_PAIR, prior), "没有换回原样"


INTERNAL = {"ok": False, "code": "internal", "message": "内部错误"}
#: 改强度那一步网关回的「不是拒绝也不是断网」的东西：400、500、认不出的回体（安审 I2）
EFFORT_ODD = {
    "回400": (400, {"ok": False, "error": "bad effort"}),
    "回500": (500, {"ok": False, "error": "boom"}),
    "回200但ok不是true": (200, {"ok": "yes"}),
    "回200但不是对象": (200, ["ok"]),
    "回200但不是JSON": json.JSONDecodeError("Expecting value", "<html>", 0),
}


def _switch_with_effort(client, gateway, answer, prior):
    """换到另一家并同时改强度；改强度那一次网关回 answer。"""
    _put_bot(enabled=True)
    gateway.effort, gateway.effort_answer = prior, answer
    return _post(client, {**SWITCH, "effort": "max"})


@pytest.mark.parametrize("prior", ["low", None], ids=["原来单独指定过强度", "原来没有单独指定"])
@pytest.mark.parametrize("answer", list(EFFORT_ODD.values()), ids=list(EFFORT_ODD))
def test_第5步_强度那步网关回400_500或认不出_也把供应商和模型换回去_不留半截(client, gateway, answer, prior):
    status, got = _switch_with_effort(client, gateway, answer, prior)
    assert (status, got) == (500, INTERNAL), got
    restore = [("POST", GW_EFFORT, {"effort": prior})] if prior else []
    assert gateway.calls == [
        READ_CATALOG,
        ("GET", GW_EFFORT, None),
        ("POST", GW_MODEL, {"spec": "fake-vendor-b/vb-small"}),
        ("POST", GW_EFFORT, {"effort": "max"}),                       # 回的东西不对
        ("POST", GW_MODEL, {"spec": "deepseek-official/deepseek-flash"}),   # 换回去
    ] + restore, gateway.calls
    assert (gateway.current, gateway.effort) == (CFG_PAIR, prior), "没有换回原样"


@pytest.mark.parametrize("answer", list(EFFORT_ODD.values()), ids=list(EFFORT_ODD))
def test_第5步_强度那步回的东西不对_换回去也没成_如实说已经换了没换回去(client, gateway, answer):
    gateway.model_writes_ok = 1                     # 换过去那一次成功，换回去那一次拒连
    status, got = _switch_with_effort(client, gateway, answer, None)
    assert (status, got["code"]) == (500, "internal"), got
    assert "已经换了" in got["message"] and "没能换回去" in got["message"], got
    assert gateway.current == {"provider": "fake-vendor-b", "model": "vb-small"}
