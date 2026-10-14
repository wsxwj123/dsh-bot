# -*- coding: utf-8 -*-
"""适配服务验收：两个写接口（换供应商、重启）的错误契约与反向用例。

依据：INTERFACE-适配约定-v0.3.md 5.2（bot_id 字符规则）、5.4、5.5，
INTERFACE-适配服务.md 第 3 节、4.4、4.5。不读实现。

换供应商的清单校验与"成功"几条用会应答的假网关（adapter_util 的 serve_model），
它只照 INTERFACE-适配服务.md 第 5 节写明的路径、请求体、返回字段应答。
"""
import json
import time
from types import SimpleNamespace

import pytest

from . import adapter_util as au
from .adapter_util import adapter, console_guard  # noqa: F401  夹具

#: 两个写接口各一个"本来合规"的请求：名字 -> (拼路径的函数, 请求体)
WRITE_CALLS = {
    "换供应商": (au.provider_url, {"effort": "high"}),
    "重启": (au.restart_url, {"force": True}),
}


@pytest.fixture
def live(adapter):
    """一只启用的 botalpha：网关端口指向记录型假网关，心跳一直在刷新。"""
    gw = adapter.gateway()
    bot = adapter.bot("botalpha", gw_port=gw.port)
    adapter.heartbeat.tick("botalpha", pid=4242)
    return SimpleNamespace(gw=gw, bot=bot)


@pytest.fixture
def switching(adapter):
    """一只正在运行的 botalpha：网关照补充文件第 5 节应答，当前生效的是配置文件里那一组。"""
    gw = adapter.model_gateway()
    return SimpleNamespace(gw=gw, bot=adapter.running_bot("botalpha", gw))


# ============================================================ bot_id 字符规则

BAD_IDS = {
    "两个点": "..",
    "一个点": ".",
    "三个点": "...",
    "编码的两个点": "%2e%2e",
    "编码的斜杠": "a%2Fb",
    "编码的斜杠_往上跳到别的接口": "..%2F..%2Fmeta",
    "编码的斜杠_绕回一只真bot": "x%2F..%2Fbotalpha",
    "编码的反斜杠": "a%5Cb",
    "编码的空格": "a%20b",
    "加号": "a+b",
    "at号": "a@b",
    "波浪号": "a~b",
    "冒号": "a:b",
    "中文": "机器人",
    "65个字符": "a" * 65,
}


@pytest.mark.parametrize("which", list(WRITE_CALLS))
@pytest.mark.parametrize("bad", list(BAD_IDS.values()), ids=list(BAD_IDS))
def test_bot_id不合规_写接口回422_invalid_不找网关不碰启动项(adapter, live, which, bad):
    make_url, body = WRITE_CALLS[which]
    adapter.expect_error(adapter.call("POST", make_url(bad), body), 422, "invalid")
    assert live.gw.requests == [], "bot_id 不合规却去找了网关：%r" % live.gw.requests
    assert adapter.launchctl.calls() == [], \
        "bot_id 不合规却调了启动项管理程序：%r" % adapter.launchctl.calls()


ABSENT_IDS = {
    "普通名字": "ghost",
    "带点横线下划线": "no-such.bot_1",
    "中间有两个点": "a..b",
    "点开头": ".hidden",
    "点结尾": "bot.",
    "单个横线": "-",
    "恰好64个字符": "A" * 64,
}


@pytest.mark.parametrize("which", list(WRITE_CALLS))
@pytest.mark.parametrize("absent", list(ABSENT_IDS.values()), ids=list(ABSENT_IDS))
def test_bot_id合规但没有这只bot_写接口回404_unknown_bot(adapter, live, which, absent):
    make_url, body = WRITE_CALLS[which]
    adapter.expect_error(adapter.call("POST", make_url(absent), body), 404, "unknown_bot")
    assert live.gw.requests == [], "没有这只 bot，却去找了别的 bot 的网关：%r" % live.gw.requests
    assert adapter.launchctl.actions() == [], \
        "没有这只 bot，却对启动项做了动作：%r" % adapter.launchctl.actions()


# ============================================================ 请求体不是 JSON 对象

RAW_BODIES = {"数组": "[1, 2]", "字符串": '"text"', "坏JSON": "{not json", "JSON的null": "null"}
BAD_BODY_CASES = ([("换供应商", k, v) for k, v in RAW_BODIES.items()]
                  + [("换供应商", "空体", "")]
                  + [("重启", k, v) for k, v in RAW_BODIES.items()])     # 重启允许空体，不在此列


@pytest.mark.parametrize("which,raw", [(w, v) for w, _, v in BAD_BODY_CASES],
                         ids=["%s_%s" % (w, k) for w, k, _ in BAD_BODY_CASES])
def test_请求体不是JSON对象_写接口回422_invalid_什么都不做(adapter, live, which, raw):
    make_url, _ = WRITE_CALLS[which]
    adapter.expect_error(adapter.call("POST", make_url("botalpha"), raw=raw), 422, "invalid")
    assert live.gw.requests == [], "请求体不合规却去找了网关：%r" % live.gw.requests
    assert adapter.launchctl.calls() == [], \
        "请求体不合规却调了启动项管理程序：%r" % adapter.launchctl.calls()


# ============================================================ 换供应商

@pytest.mark.parametrize("payload", [{"reset": True}, {"reset": False},
                                     {"reset": True, "effort": "high"}],
                         ids=["reset为真", "reset为假", "reset加思考强度"])
def test_换供应商_请求里出现reset_回501_unsupported_网关零请求(adapter, live, payload):
    adapter.expect_error(adapter.call("POST", au.provider_url("botalpha"), payload),
                         501, "unsupported")
    assert live.gw.requests == [], "不支持的操作却去找了网关：%r" % live.gw.requests


INVALID_PAYLOADS = {
    "三样都没有": {},
    "只有不认识的键": {"unknown_key": 1},
    "只有provider_id": {"provider_id": au.CFG_PROVIDER},
    "只有model": {"model": au.CFG_MODEL},
    "provider_id是空串": {"provider_id": "", "model": au.CFG_MODEL},
    "model是空串": {"provider_id": au.CFG_PROVIDER, "model": ""},
    "provider_id不是字符串": {"provider_id": 123, "model": au.CFG_MODEL},
    "model不是字符串": {"provider_id": au.CFG_PROVIDER, "model": [au.CFG_MODEL]},
    "provider_id和model都是null": {"provider_id": None, "model": None},
    "effort不在四档里": {"effort": "medium"},
    "effort大小写不对": {"effort": "HIGH"},
    "effort不是字符串": {"effort": 3},
    "effort是null": {"effort": None},
}


@pytest.mark.parametrize("payload", list(INVALID_PAYLOADS.values()), ids=list(INVALID_PAYLOADS))
def test_换供应商_入参不合规_回422_invalid_网关零请求(adapter, live, payload):
    adapter.expect_error(adapter.call("POST", au.provider_url("botalpha"), payload),
                         422, "invalid")
    assert live.gw.requests == [], "校验没过却去找了网关：%r" % live.gw.requests


def test_换供应商_没有这家供应商_回404_unknown_provider_网关零写请求(adapter, switching):
    """4.4 第 4 步：允许向这只 bot 的网关读一次清单，但不许有任何写请求。"""
    payload = {"provider_id": "no-such-provider-zz9", "model": "whatever"}
    adapter.expect_error(adapter.call("POST", au.provider_url("botalpha"), payload),
                         404, "unknown_provider")
    assert switching.gw.writes() == [], "清单校验没过却往网关发了写请求：%r" % switching.gw.writes()


WRONG_MODELS = {"哪一家都没有的模型": "no-such-model-zz9", "别家才有的模型": au.VENDOR_A_MODELS[1]}


@pytest.mark.parametrize("model", list(WRONG_MODELS.values()), ids=list(WRONG_MODELS))
def test_换供应商_模型不在这家里_回422_invalid_网关零写请求(adapter, switching, model):
    """4.4 第 4 步：供应商在清单里，模型不在这一家的模型里。"""
    payload = {"provider_id": au.VENDOR_B, "model": model}
    adapter.expect_error(adapter.call("POST", au.provider_url("botalpha"), payload),
                         422, "invalid")
    assert switching.gw.writes() == [], "清单校验没过却往网关发了写请求：%r" % switching.gw.writes()


def test_换供应商_换到另一家_成功_返回形状如约_new_segment为真(adapter, switching):
    payload = {"provider_id": au.VENDOR_B, "model": au.VENDOR_B_MODELS[0]}
    body = adapter.ok(adapter.call("POST", au.provider_url("botalpha"), payload))
    assert body.get("applied") is True and body.get("queued") is False, body
    assert body.get("new_segment") is True, "从 %s 换到 %s，new_segment 应为 true：%r" % (
        au.CFG_PROVIDER, au.VENDOR_B, body)
    adapter.human_message(body.get("message"))


def test_换供应商_只在同一家里换模型_new_segment为假(adapter, switching):
    payload = {"provider_id": au.CFG_PROVIDER, "model": au.VENDOR_A_MODELS[1]}
    body = adapter.ok(adapter.call("POST", au.provider_url("botalpha"), payload))
    assert body.get("new_segment") is False, "还在 %s 这一家，new_segment 应为 false：%r" % (
        au.CFG_PROVIDER, body)


def test_换供应商_同样的请求提交两次_第二次仍是200_new_segment为假(adapter, switching):
    payload = {"provider_id": au.VENDOR_B, "model": au.VENDOR_B_MODELS[0]}
    adapter.ok(adapter.call("POST", au.provider_url("botalpha"), payload))
    second = adapter.ok(adapter.call("POST", au.provider_url("botalpha"), payload))
    assert second.get("applied") is True and second.get("queued") is False, second
    assert second.get("new_segment") is False, "第二次时已经在那一家了，new_segment 应为 false：%r" % (second,)


def test_换供应商_写请求只发给这只bot自己的网关_请求体是spec_别的bot的网关零请求(adapter):
    """目标故意选编号靠后的那只：照"按字典序问第一只"去找网关的做法在这里会露馅。"""
    gw_other, gw_mine = adapter.model_gateway(), adapter.model_gateway()
    adapter.running_bot("botalpha", gw_other)
    adapter.running_bot("botbravo", gw_mine)
    model = au.VENDOR_B_MODELS[0]
    adapter.call("POST", au.provider_url("botbravo"), {"provider_id": au.VENDOR_B, "model": model})
    writes = gw_mine.writes()
    assert [(w["method"], w["path"]) for w in writes] == [("POST", au.GW_MODEL)], \
        "应恰好向这只 bot 的网关发一次 POST /v1/model：%r" % gw_mine.requests
    assert writes[0]["json"] == {"spec": "%s/%s" % (au.VENDOR_B, model)}, writes[0]["body"]
    assert gw_other.requests == [], "给 botbravo 换供应商，却找了 botalpha 的网关：%r" % gw_other.requests


def test_换供应商_bot停用_回409_stopped_对任何网关零请求(adapter):
    """4.4 第 3 步没过：连第 4 步那一次读清单都不该发生。"""
    gw_stopped, gw_other = adapter.model_gateway(), adapter.model_gateway()
    adapter.running_bot("botalpha", gw_other)
    adapter.bot("botbravo", enabled=False, gw_port=gw_stopped.port)
    payload = {"provider_id": au.VENDOR_B, "model": au.VENDOR_B_MODELS[0]}
    adapter.expect_error(adapter.call("POST", au.provider_url("botbravo"), payload), 409, "stopped")
    assert gw_stopped.requests == [], "bot 停用着，却去找了它的网关：%r" % gw_stopped.requests
    assert gw_other.requests == [], "bot 停用着，却去找了别的 bot 的网关：%r" % gw_other.requests


def test_换供应商_bot启用但网关连不上_回503_unavailable(adapter):
    adapter.bot("botalpha", gw_port=au.free_port())      # 端口没人听
    adapter.expect_error(adapter.call("POST", au.provider_url("botalpha"), {"effort": "high"}),
                         503, "unavailable")


def test_换供应商_校验通过后只找这只bot自己的网关_别的bot的网关零请求(adapter):
    """假网关一律回 404，适配服务这次回什么不在断言范围里；只看请求打到了谁那儿。"""
    gw_mine, gw_other = adapter.gateway(), adapter.gateway()
    adapter.bot("botalpha", gw_port=gw_mine.port)
    adapter.bot("botbravo", gw_port=gw_other.port)
    resp = adapter.call("POST", au.provider_url("botalpha"), {"effort": "high"})
    assert isinstance(au.body_of(resp).get("ok"), bool)
    assert gw_mine.requests, "入参都合规，却没有去找这只 bot 的网关"
    assert gw_other.requests == [], "给 botalpha 换设置，却找了 botbravo 的网关：%r" % gw_other.requests


# ============================================================ 停用的 bot

@pytest.mark.parametrize("which", list(WRITE_CALLS))
def test_停用的bot_写接口回409_stopped_不往网关写东西也不重启(adapter, which):
    gw = adapter.gateway()
    bot = adapter.bot("botalpha", enabled=False, gw_port=gw.port)
    before = bot.yml.read_bytes()
    make_url, body = WRITE_CALLS[which]
    adapter.expect_error(adapter.call("POST", make_url("botalpha"), body), 409, "stopped")
    assert gw.writes() == [], "bot 停用着，却往它的网关发了写请求：%r" % gw.writes()
    assert adapter.launchctl.actions() == [], \
        "bot 停用着，却对它的启动项做了动作：%r" % adapter.launchctl.actions()
    assert bot.yml.read_bytes() == before, "bot 停用着，配置文件却被改了"


# ============================================================ 重启

NO_FORCE = {
    "force是false": ({"force": False}, None),
    "没给force": ({}, None),
    "只有不认识的键": ({"other": 1}, None),
    "空体": (au.NO_BODY, None),
    "空体但声明了JSON": (au.NO_BODY, ""),
}


@pytest.mark.parametrize("body,raw", list(NO_FORCE.values()), ids=list(NO_FORCE))
def test_重启_没要求强制_回501_unsupported_不重启(adapter, live, body, raw):
    adapter.expect_error(adapter.call("POST", au.restart_url("botalpha"), body, raw=raw),
                         501, "unsupported")
    assert adapter.launchctl.actions() == [], \
        "不支持等空闲重启，却换成别的方式动了启动项：%r" % adapter.launchctl.actions()


@pytest.mark.parametrize("force", ["true", 1, 0, None, [], {"a": 1}],
                         ids=["字符串true", "数字1", "数字0", "null", "数组", "对象"])
def test_重启_force不是布尔值_回422_invalid_启动项零调用(adapter, live, force):
    adapter.expect_error(adapter.call("POST", au.restart_url("botalpha"), {"force": force}),
                         422, "invalid")
    assert adapter.launchctl.calls() == [], \
        "校验没过却调了启动项管理程序：%r" % adapter.launchctl.calls()


def test_重启_强制重启成功_回已重启_只动这一只bot的启动项(adapter):
    adapter.bot("botalpha")
    other = adapter.bot("botbravo")
    other_before = other.yml.read_bytes()
    adapter.launchctl.set_default(installed=True, running=False, pid=4242)
    adapter.heartbeat.tick("botalpha", pid=4242)         # 重启之后心跳一直是新鲜的
    body = adapter.ok(adapter.call("POST", au.restart_url("botalpha"), {"force": True}))
    assert body["status"] == "ok"
    assert body["message"] == "已重启"
    assert adapter.launchctl.actions(), "回了已重启，却没有对启动项管理程序发出任何动作"
    argv_text = json.dumps([c.get("argv") for c in adapter.launchctl.calls()], ensure_ascii=False)
    assert "botalpha" in argv_text, "对启动项管理程序的调用里没有这只 bot：%s" % argv_text
    assert "botbravo" not in argv_text, "重启 botalpha 却碰了 botbravo 的启动项：%s" % argv_text
    labels = [label for label in adapter.launchctl.labels() if label]
    assert all("botalpha" in label for label in labels), "出现了别的启动项标签：%r" % labels
    assert other.yml.read_bytes() == other_before, "重启 botalpha 却改了 botbravo 的配置文件"


def test_重启_命令发了但一直等不到心跳_仍回200_status是failed_这条约跑50秒(adapter):
    adapter.bot("botalpha")
    adapter.launchctl.set_default(installed=True, running=True, pid=9999)    # 启动项说在跑，心跳始终不来
    started = time.time()
    resp = adapter.call("POST", au.restart_url("botalpha"), {"force": True})
    took = time.time() - started
    body = au.body_of(resp)
    assert resp.status_code == 200, "没起来也应回 200，实际 %s：%r" % (resp.status_code, body)
    assert body.get("ok") is False and body.get("status") == "failed", body
    adapter.human_message(body.get("message"))
    assert took < 60, "等了 %.0f 秒才返回，超过了调用方 60 秒的重启超时" % took
