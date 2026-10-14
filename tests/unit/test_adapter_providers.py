# -*- coding: utf-8 -*-
"""适配服务的供应商清单（INTERFACE-适配服务 4.3、5.1）：网关返回体怎么映射、问哪一只网关。

映射直接调 ``_catalog``（纯函数）。问网关的几条走 HTTP：bot 照真配置的形状摆在总闸给的临时
目录里，名单、启停走真函数；``gateway_client._call`` 是适配服务到网关的唯一出口，换成按频道
目录认 bot 的替身，问到谁都记下来。
"""
import copy
import json
import os
import urllib.error
from pathlib import Path

import pytest

import gateway_client
from moments import adapter_service as svc

TOKEN = "unit-adapter-token"
INTERNAL = {"ok": False, "code": "internal", "message": "内部错误"}
NO_RUNNING_BOT = "没有正在运行的 bot，暂时读不到供应商清单"
OMIT = object()       # 参数化里表示「这个键干脆不放」


def _provider(name, key="ok", enabled=True, **extra):
    """``providers`` 的一项，字段照 5.1 的样例齐全（``models`` 是个数，不是模型名）。"""
    item = {"name": name, "source": "builtin", "api": "openai-completions", "models": 1,
            "key": key, "keyEnv": None, "enabled": enabled, "note": None, "last_refresh": None}
    item.update(extra)
    return item


def _reply(**over):
    """一份 ``GET /v1/model`` 的返回体（5.1 的形状）。choices 里各家交错，停用那家和清单外的名字也在。"""
    reply = {
        "ok": True,
        "current": {"provider": "va", "model": "a1"},
        "config": {"provider": "va", "model": "a1"},
        "choices": [{"provider": p, "model": m} for p, m in (
            ("va", "a1"), ("vb", "b1"), ("va", "a2"), ("voff", "off1"), ("vb", "b2"), ("ghost", "g1"))],
        "providers": [
            _provider("va", models=2),
            _provider("vb", key="missing", models=None, source="custom", api="anthropic-messages",
                      keyEnv="VB_DO_NOT_LEAK_KEY", note="https://vb.invalid/v1",
                      last_refresh={"at": 0, "ok": True, "count": 2, "reason": None}),
            _provider("voff", enabled=False, keyEnv="VOFF_DO_NOT_LEAK_KEY"),
            _provider("vc", key="none", models=0),
        ],
    }
    reply.update(over)
    return reply


def _without(key):
    reply = _reply()
    del reply[key]
    return reply


# ---------------------------------------------------------------- 映射（5.1）

def test_映射_模型名取choices按家归组保持先后_只带四个键_停用的和清单外的名字不列():
    assert svc._catalog(_reply()) == [
        {"id": "va", "name": "va", "has_key": True, "models": ["a1", "a2"]},
        {"id": "vb", "name": "vb", "has_key": False, "models": ["b1", "b2"]},
        {"id": "vc", "name": "vc", "has_key": True, "models": []},
    ]


KEYS = {"ok": ("ok", True), "none": ("none", True), "missing": ("missing", False),
        "unknown": ("unknown", False), "别的字符串": ("weird", False), "null": (None, False),
        "没有key字段": (OMIT, False)}


@pytest.mark.parametrize("key,has_key", list(KEYS.values()), ids=list(KEYS))
def test_映射_has_key只在key为ok或none时为真(key, has_key):
    item = _provider("va")
    if key is OMIT:
        del item["key"]
    else:
        item["key"] = key
    assert svc._catalog(_reply(providers=[item]))[0]["has_key"] is has_key


ENABLED = {"true_列": (True, True), "false_不列": (False, False), "没有这个字段_当作启用": (OMIT, True),
           "null_不列": (None, False), "字符串true_不列": ("true", False)}


@pytest.mark.parametrize("enabled,listed", list(ENABLED.values()), ids=list(ENABLED))
def test_映射_enabled不是true的不列_没有这个字段的当作启用(enabled, listed):
    item = _provider("va")
    if enabled is OMIT:
        del item["enabled"]
    else:
        item["enabled"] = enabled
    assert [p["id"] for p in svc._catalog(_reply(providers=[item]))] == (["va"] if listed else [])


@pytest.mark.parametrize("choices", [OMIT, None, "oops", {"provider": "va", "model": "a1"}],
                         ids=["没有choices键", "null", "字符串", "对象"])
def test_映射_choices缺失或不是数组_当作空数组_不算认不出(choices):
    reply = _without("choices") if choices is OMIT else _reply(choices=choices)
    got = svc._catalog(reply)
    assert [p["id"] for p in got] == ["va", "vb", "vc"]
    assert all(p["models"] == [] for p in got), got


UNRECOGNIZED = {
    "返回体是数组": [1, 2],
    "返回体是字符串": "<html>gateway is confused</html>",
    "返回体是null": None,
    "没有providers": _without("providers"),
    "providers是null": _reply(providers=None),
    "providers是字符串": _reply(providers="oops"),
    "providers是对象": _reply(providers={"name": "va"}),
}


@pytest.mark.parametrize("reply", list(UNRECOGNIZED.values()), ids=list(UNRECOGNIZED))
def test_映射_认不出就抛(reply):
    with pytest.raises(RuntimeError):
        svc._catalog(copy.deepcopy(reply))


def test_映射_形状不对的单项跳过_认不出的值不往外带():
    providers = [_provider("va"), "vb", None, {"key": "ok", "enabled": True}, _provider(""),
                 _provider("  "), _provider(["va"]), _provider({"keyEnv": "X_DO_NOT_LEAK_KEY"})]
    choices = [{"provider": "va", "model": "a1"}, "va/a2", None, {"provider": "va"},
               {"provider": "va", "model": ""}, {"provider": "va", "model": 7},
               {"provider": ["va"], "model": "a9"}, {"provider": "va", "model": "a3"}]
    assert svc._catalog(_reply(providers=providers, choices=choices)) == [
        {"id": "va", "name": "va", "has_key": True, "models": ["a1", "a3"]}]


# ---------------------------------------------------------------- 问哪一只网关（4.3）

class Gateways:
    """``gateway_client._call`` 的替身：按频道目录认出是哪只 bot 的网关，记下 ``(bot, 方法, 路径)``。

    ``answers[bot]`` 是这只网关的应答：``(状态码, 返回体)``，或一个要抛的异常（网络上的事、
    返回体不是 JSON）。没配应答的 bot 被问到就是用例要抓的错。
    """

    def __init__(self):
        self.answers, self.calls = {}, []

    def __call__(self, channel_dir, method, path, body=None, timeout=10):
        bot = Path(channel_dir).parent.name
        self.calls.append((bot, method, path))
        answer = self.answers.get(bot)
        if answer is None:
            raise AssertionError("不该问 %s 的网关" % bot)
        if isinstance(answer, BaseException):
            raise answer
        return copy.deepcopy(answer)


@pytest.fixture
def gateways(monkeypatch):
    gw = Gateways()
    monkeypatch.setattr(gateway_client, "_call", gw)
    return gw


@pytest.fixture
def client(monkeypatch, tmp_path):
    token_file = tmp_path / "adapter.token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    monkeypatch.setenv("DSH_CONSOLE_TOKEN_FILE", str(token_file))
    return svc.create_app().test_client()


def _put_bot(bot_id, enabled=True, port=True):
    """摆一只 bot（形状照真配置）。``port`` 为假时没有网关端口文件，算没有可连的网关。"""
    botdir = Path(os.environ["DSH_BOT_HOME"]) / "bots" / bot_id
    state, channel = botdir / "state", botdir / "channel"
    state.mkdir(parents=True, exist_ok=True)
    channel.mkdir(exist_ok=True)
    (state / "api.key").write_text("unit-gateway-key\n", encoding="utf-8")
    if port:
        (state / "api.port").write_text("9\n", encoding="utf-8")
    (channel / ".env").write_text("TELEGRAM_BOT_TOKEN=unit-fake\nTELEGRAM_CHAT_ID=-1\n",
                                  encoding="utf-8")
    cfg = {"id": bot_id, "display_name": bot_id, "bot_channel_path": str(channel),
           "dispatcher_port": 0, "brain": {"provider": "va", "model": "a1"}}
    if not enabled:
        cfg["enabled"] = False
    cfg_dir = Path(os.environ["HUB_CONFIGS_DSH_DIR"])
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / ("%s.yml" % bot_id)).write_text(json.dumps(cfg), encoding="utf-8")


def _get(client):
    r = client.get("/adapter/v1/providers", headers={"Authorization": "Bearer " + TOKEN})
    return r.status_code, r.get_json()


READ = "GET", "/v1/model"
UNREACHABLE = urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))


def test_按编号字典序问第一只连得上的_后面的不问(client, gateways):
    for bot in ("botbravo", "botalpha"):            # 摆的先后故意与字典序相反
        _put_bot(bot)
    gateways.answers["botalpha"] = (200, _reply(providers=[_provider("from-alpha")]))
    gateways.answers["botbravo"] = (200, _reply(providers=[_provider("from-bravo")]))
    status, got = _get(client)
    assert status == 200, got
    assert [p["id"] for p in got["providers"]] == ["from-alpha"], got
    assert got["default"] == {"label": "各 bot 配置文件里写的那一组"}
    assert gateways.calls == [("botalpha",) + READ]


NEXT_ONE = {"拒连": UNREACHABLE, "超时": TimeoutError("timed out"),
            "发出后断开": ConnectionResetError(54, "Connection reset by peer"),
            "URLError包着超时": urllib.error.URLError(TimeoutError("timed out"))}


@pytest.mark.parametrize("error", list(NEXT_ONE.values()), ids=list(NEXT_ONE))
def test_第一只连不上或没回应_换下一只问(client, gateways, error):
    _put_bot("botalpha")
    _put_bot("botbravo")
    gateways.answers["botalpha"] = error
    gateways.answers["botbravo"] = (200, _reply())
    status, got = _get(client)
    assert status == 200, got
    assert [p["id"] for p in got["providers"]] == ["va", "vb", "vc"], got
    assert gateways.calls == [("botalpha",) + READ, ("botbravo",) + READ]


SKIPPED = {"停用的": dict(enabled=False), "没有网关端口文件": dict(port=False)}


@pytest.mark.parametrize("kw", list(SKIPPED.values()), ids=list(SKIPPED))
def test_停用的和没有网关文件的bot_跳过_不发请求(client, gateways, kw):
    _put_bot("botalpha", **kw)
    _put_bot("botbravo")
    gateways.answers["botalpha"] = (200, _reply(providers=[_provider("from-alpha")]))
    gateways.answers["botbravo"] = (200, _reply())
    status, got = _get(client)
    assert status == 200, got
    assert "from-alpha" not in [p["id"] for p in got["providers"]], got
    assert gateways.calls == [("botbravo",) + READ]


UNRECOGNIZED_ANSWERS = {
    "不是JSON": json.JSONDecodeError("Expecting value", "<html>", 0),
    "JSON数组": (200, [1, 2]),
    "没有providers": (200, _without("providers")),
    "providers不是数组": (200, _reply(providers="oops")),
    "状态码不是200": (500, {"ok": False, "error": "boom"}),
}


@pytest.mark.parametrize("answer", list(UNRECOGNIZED_ANSWERS.values()), ids=list(UNRECOGNIZED_ANSWERS))
def test_第一只连得上但回的认不出_回500_文案固定为内部错误_不再问下一只(client, gateways, answer):
    _put_bot("botalpha")
    _put_bot("botbravo")
    gateways.answers["botalpha"] = answer
    gateways.answers["botbravo"] = (200, _reply())
    status, got = _get(client)
    assert (status, got) == (500, INTERNAL)
    assert gateways.calls == [("botalpha",) + READ]


def test_一只都问不到_回503_unavailable_文案如约(client, gateways):
    _put_bot("botalpha")                            # 拒连
    _put_bot("botbravo", enabled=False)             # 停用：问都不问
    _put_bot("botcharlie", port=False)              # 没有网关端口文件
    _put_bot("botdelta")                            # 超时
    gateways.answers["botalpha"] = UNREACHABLE
    gateways.answers["botbravo"] = (200, _reply())
    gateways.answers["botdelta"] = TimeoutError("timed out")
    status, got = _get(client)
    assert (status, got) == (503, {"ok": False, "code": "unavailable", "message": NO_RUNNING_BOT})
    assert gateways.calls == [("botalpha",) + READ, ("botdelta",) + READ]


def test_一只bot都没有_回503_不发请求(client, gateways):
    status, got = _get(client)
    assert (status, got) == (503, {"ok": False, "code": "unavailable", "message": NO_RUNNING_BOT})
    assert gateways.calls == []
