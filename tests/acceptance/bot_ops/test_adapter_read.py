# -*- coding: utf-8 -*-
"""适配服务验收：三个只读接口的返回形状，以及"响应里没有敏感内容"。

依据：INTERFACE-适配约定-v0.3.md 5.1 到 5.3，INTERFACE-适配服务.md 4.1 到 4.3、5.1、第 7 节。不读实现。
"""
import copy
import re
import time

import pytest

from . import adapter_util as au
from . import botops_util as u
from .adapter_util import adapter, console_guard  # noqa: F401  夹具

#: 补充文件第 7 节与 9.1：4.1 表里的八个，加 bot.notify、bot.life_settings，再加 bot.enable、
#: bot.switch_in、bot.group_chat（v0.6/v0.7），再加 bot.persona、bot.voice（v0.8，补充文件 10.1），再加 bot.skills（v0.9，补充文件 11.1），再加 bot.create、bot.archive、bot.delete、bot.profile（v1.0，补充文件 12.1），再加 bot.token_handoff（补充文件 13.8），再加 bot.groups_discovered（约定 v1.1 5.20），恰好二十二个键
CAPABILITIES_TEN = {
    "bot.restart.graceful": False,
    "bot.restart.force": True,
    "bot.set_provider": True,
    "bot.reset_provider": False,
    "bot.set_effort": True,
    "provider.subscription": False,
    "provider.per_session": False,
    "provider.switch_new_segment": True,
    "bot.notify": True,
    "bot.life_settings": True,
    "bot.enable": True,
    "bot.switch_in": True,
    "bot.group_chat": True,
    "bot.persona": True,
    "bot.voice": True,
    "bot.skills": True,
    "bot.create": True,
    "bot.archive": True,
    "bot.delete": True,
    "bot.profile": True,
    "bot.token_handoff": True,
    "bot.groups_discovered": True,
}


def test_meta_固定取值与十项能力如约(adapter):
    body = adapter.ok(adapter.call("GET", au.META))
    assert body["backend"] == "dsh"
    assert body["adapter"] == "v1"
    version = body["backend_version"]
    assert isinstance(version, str) and version.strip(), "backend_version 应是非空字符串：%r" % (version,)
    assert body["effort_levels"] == au.EFFORT_LEVELS
    caps = body["capabilities"]
    assert isinstance(caps, dict), "capabilities 应是对象：%r" % (caps,)
    missing = sorted(set(CAPABILITIES_TEN) - set(caps))
    extra = sorted(set(caps) - set(CAPABILITIES_TEN))
    assert not missing and not extra, "能力清单应恰好是约定的二十一个键：缺 %r，多 %r" % (missing, extra)
    for key, want in CAPABILITIES_TEN.items():
        assert caps[key] is want, "能力 %s 应为布尔 %r，实际 %r" % (key, want, caps[key])


def test_bots_一只bot都没有_200且是空数组(adapter):
    body = adapter.ok(adapter.call("GET", au.BOTS))
    assert body["bots"] == []


def test_bots_按编号字典序列出_另一套系统的bot不出现(adapter):
    for bot_id in ("bota", "bot2", "bot10"):
        adapter.bot(bot_id)
    u.write_bot_yml(adapter.sandbox.legacy_cfg, "oldbot", display_name="旧系统的", dispatcher_port=17899)
    body = adapter.ok(adapter.call("GET", au.BOTS))
    assert [b["bot_id"] for b in body["bots"]] == ["bot10", "bot2", "bota"]


def test_bots_网关连不上的bot_取配置文件那一组并标default_整个接口仍200(adapter):
    adapter.bot("botalpha", display_name="阿尔法", gw_port=au.free_port())    # 端口没人听
    body = adapter.ok(adapter.call("GET", au.BOTS))
    assert len(body["bots"]) == 1, body
    bot = body["bots"][0]
    assert au.BOT_KEYS <= set(bot), "每只 bot 至少要有约定的八个键，缺 %r" % (au.BOT_KEYS - set(bot))
    assert bot["bot_id"] == "botalpha"
    assert bot["display_name"] == "阿尔法"
    assert bot["state"] in au.BOT_STATES, "state 只会是 running / stopped / broken：%r" % bot["state"]
    assert isinstance(bot["state_text"], str) and bot["state_text"].strip()
    provider = bot["provider"]
    assert isinstance(provider, dict), "provider 恒不为 null：%r" % (provider,)
    assert provider["id"] == au.CFG_PROVIDER and provider["name"] == au.CFG_PROVIDER, provider
    assert provider["model"] == au.CFG_MODEL, provider
    assert provider["effort"] is None, "配置文件里没设思考强度，effort 应为 null：%r" % (provider,)
    assert bot["provider_source"] == "default"
    assert bot["pending"] is None
    assert isinstance(bot["detail"], dict)


def _stopped(a):
    a.bot("botalpha", enabled=False)


def _fresh_heartbeat(a):
    a.bot("botalpha")
    a.launchctl.set_default(installed=True, running=True, pid=4242)
    a.heartbeat.write("botalpha", pid=4242)


def _unit_running_no_heartbeat(a):
    a.bot("botalpha")
    a.launchctl.set_default(installed=True, running=True, pid=4242)


def _nothing(a):
    a.bot("botalpha")
    a.launchctl.set_default(installed=True, running=False, pid=4242)


def _stale_heartbeat(a):
    a.bot("botalpha")
    a.launchctl.set_default(installed=True, running=False, pid=4242)
    a.heartbeat.write("botalpha", pid=4242, at_ms=int(time.time() * 1000) - 60000)


#: (怎么摆, 期望的 state, 期望的 state_text；None 表示只要求是非空的一句话)
STATE_CASES = {
    "配置里标了停用": (_stopped, "stopped", "已停用"),
    "启用_心跳新鲜": (_fresh_heartbeat, "running", "运行中"),
    "启用_启动项在跑_心跳还没出来": (_unit_running_no_heartbeat, "running", "启动中"),
    "启用_没有心跳_启动项也不在跑": (_nothing, "broken", None),
    "启用_心跳是一分钟前的_启动项不在跑": (_stale_heartbeat, "broken", None),
}


@pytest.mark.parametrize("setup,state,text", list(STATE_CASES.values()), ids=list(STATE_CASES))
def test_bots_状态与状态文字如约(adapter, setup, state, text):
    setup(adapter)
    bots = adapter.ok(adapter.call("GET", au.BOTS))["bots"]
    assert len(bots) == 1, bots
    got = bots[0]
    assert got["state"] == state, "state 应为 %s，实际 %r（%r）" % (state, got["state"], got["state_text"])
    if text is None:
        adapter.human_message(got["state_text"])     # broken 要说清原因，文案不限
    else:
        assert got["state_text"] == text


def test_供应商摘要_每项形状如约_不带接口地址和密钥变量名(adapter):
    """清单是向正在运行的 bot 的网关读来的（补充文件 4.3、5.1）。

    假网关照 5.1 的形状给三家：A 的 key 是 ok；B 的 key 是 missing、keyEnv 是醒目的假变量名、
    note 里夹着假接口地址；第三家标了停用。模型名只在 choices 里，各家条目交错排列；
    providers[].models 只是个数（B 的是 null）。
    """
    gw = adapter.model_gateway()
    adapter.running_bot("botalpha", gw)
    resp = adapter.call("GET", au.PROVIDERS)
    body = adapter.ok(resp)
    providers = body["providers"]
    assert isinstance(providers, list), "providers 应是数组：%r" % (providers,)
    for p in providers:
        assert {"id", "name", "has_key", "models"} <= set(p), p
        assert isinstance(p["id"], str) and p["id"], p
        assert p["name"] == p["id"], "id 应等于 name：%r" % (p,)
        assert type(p["has_key"]) is bool, "has_key 必须是布尔：%r" % (p,)
        assert isinstance(p["models"], list), "models 应是模型名数组，不是网关给的个数：%r" % (p,)
        assert all(isinstance(m, str) and m for m in p["models"]), "models 应是模型名的字符串数组：%r" % (p,)
        leaked = sorted(set(p) & set(au.GW_ONLY_KEYS))
        assert not leaked, "网关的 %s 不该往外带：%r" % (leaked, p)
    ids = [p["id"] for p in providers]
    assert sorted(ids) == sorted([au.VENDOR_A, au.VENDOR_B]), \
        "应恰好列出网关清单里没停用的两家（停用的那家不列、不重复）：%r" % ids
    by_id = {p["id"]: p for p in providers}
    assert by_id[au.VENDOR_A]["has_key"] is True, "key 为 ok 应映射成 has_key 为真：%r" % by_id[au.VENDOR_A]
    assert by_id[au.VENDOR_B]["has_key"] is False, "key 为 missing 应映射成 has_key 为假：%r" % by_id[au.VENDOR_B]
    assert by_id[au.VENDOR_A]["models"] == au.VENDOR_A_MODELS, "应取 choices 里这一家的模型，保持 choices 里的先后"
    assert by_id[au.VENDOR_B]["models"] == au.VENDOR_B_MODELS, "应取 choices 里这一家的模型，保持 choices 里的先后"
    assert body["default"] == {"label": au.DEFAULT_LABEL}
    text = resp.get_data(as_text=True)
    for env_name in au.FAKE_KEY_ENVS:
        assert env_name not in text, "供应商摘要里带了网关给的 keyEnv（密钥的环境变量名）%s" % env_name
    assert au.FAKE_ENDPOINT_HOST not in text, "供应商摘要里带了接口地址（网关 note 里的）"
    assert "://" not in text, "供应商摘要里带了接口地址"
    assert au.PROVIDER_KEY not in text, "供应商摘要里带了密钥原文"
    hit = re.search(r"[A-Z][A-Z0-9_]*_KEY\b", text)
    assert hit is None, "供应商摘要里带了密钥的环境变量名：%r" % (hit and hit.group(0))


HAS_KEY_CASES = {"ok_密钥已配": ("ok", True), "none_这家不需要密钥": ("none", True),
                 "missing_缺密钥": ("missing", False), "unknown_情况不明": ("unknown", False)}


@pytest.mark.parametrize("key,has_key", list(HAS_KEY_CASES.values()), ids=list(HAS_KEY_CASES))
def test_供应商摘要_网关key的四种取值_has_key按约映射(adapter, key, has_key):
    """补充文件 5.1：key 为 ok 或 none 时 has_key 为真，为 missing 或 unknown 时为假。"""
    providers = au.gateway_providers()
    next(p for p in providers if p["name"] == au.VENDOR_A)["key"] = key       # 只改 A 这一家
    gw = adapter.model_gateway(providers=providers)
    adapter.running_bot("botalpha", gw)
    by_id = {p["id"]: p for p in adapter.ok(adapter.call("GET", au.PROVIDERS))["providers"]}
    assert au.VENDOR_A in by_id, "摘要里没有 %s：%r" % (au.VENDOR_A, by_id)
    assert by_id[au.VENDOR_A]["has_key"] is has_key, \
        "网关说 key 为 %r，has_key 应为 %r：%r" % (key, has_key, by_id[au.VENDOR_A])


def test_供应商摘要_网关标了停用的供应商_不出现在摘要里(adapter):
    """补充文件 4.3、5.1：enabled 为假的不列。停用那家 key 是 ok、choices 里也有它的模型，照样不列。"""
    gw = adapter.model_gateway()
    adapter.running_bot("botalpha", gw)
    resp = adapter.call("GET", au.PROVIDERS)
    ids = [p["id"] for p in adapter.ok(resp)["providers"]]
    assert ids, "摘要一家都没列，\"停用的不出现\"就说明不了什么"
    assert au.VENDOR_OFF not in ids, "网关标了停用的 %s 出现在摘要里：%r" % (au.VENDOR_OFF, ids)
    text = resp.get_data(as_text=True)
    for word in [au.VENDOR_OFF] + au.VENDOR_OFF_MODELS:
        assert word not in text, "摘要里还能搜到停用那家的 %r（名字或模型被归到了别家）" % word


@pytest.mark.parametrize("choices", [au.OMIT, None], ids=["没有choices键", "choices是null"])
def test_供应商摘要_网关没给choices_仍200_各家models为空数组(adapter, choices):
    """补充文件 5.1：choices 缺失或不是数组时当作空数组，不算认不出（网关刚启动时会这样）。"""
    gw = adapter.model_gateway(choices=choices)
    adapter.running_bot("botalpha", gw)
    providers = adapter.ok(adapter.call("GET", au.PROVIDERS))["providers"]
    assert sorted(p["id"] for p in providers) == sorted([au.VENDOR_A, au.VENDOR_B]), providers
    for p in providers:
        assert p["models"] == [], "没有 choices 时 %s 的 models 应为空数组（不是网关给的个数）：%r" % (p["id"], p)


def _no_bots(a):
    pass


def _all_stopped(a):
    a.bot("botalpha", enabled=False, gw_port=au.free_port())     # 端口没人听
    a.bot("botbravo", enabled=False)


def _none_listening(a):
    a.launchctl.set_default(installed=True, running=False, pid=4242)
    a.bot("botalpha", gw_port=au.free_port())                    # 端口没人听
    a.bot("botbravo")                                            # 连网关端口文件都没有


NO_GATEWAY_CASES = {"一只bot都没有": _no_bots, "都停用了": _all_stopped, "都没在跑": _none_listening}


@pytest.mark.parametrize("setup", list(NO_GATEWAY_CASES.values()), ids=list(NO_GATEWAY_CASES))
def test_供应商摘要_没有任何网关连得上_回503_unavailable_文案如约(adapter, setup):
    setup(adapter)
    body = adapter.expect_error(adapter.call("GET", au.PROVIDERS), 503, "unavailable")
    assert body["message"] == au.NO_RUNNING_BOT


#: 除了说明里那一处不对，其余照 5.1 的形状齐全，确保"认不出"只因为那一处
UNRECOGNIZED_REPLIES = {
    "不是JSON": "<html><body>gateway is confused</body></html>",
    "是JSON但是个数组": [1, 2],
    "对象里没有providers": au.model_reply(providers=au.OMIT),
    "providers不是数组": au.model_reply(providers="oops"),
}


@pytest.mark.parametrize("reply", list(UNRECOGNIZED_REPLIES.values()), ids=list(UNRECOGNIZED_REPLIES))
def test_供应商摘要_网关回了认不出的内容_回500_internal_文案固定(adapter, reply):
    gw = adapter.gateway().on("GET", au.GW_MODEL, 200, copy.deepcopy(reply))
    adapter.running_bot("botalpha", gw)
    body = adapter.expect_error(adapter.call("GET", au.PROVIDERS), 500, "internal")
    assert body["message"] == au.INTERNAL_MESSAGE


_A = au.CFG_PAIR                                  # 配置文件里写的那一组
_B = (au.VENDOR_B, au.VENDOR_B_MODELS[0])
#: 网关返回里的 current / config（OMIT 表示返回里没有 config）-> 期望的 provider_source
SOURCE_CASES = {
    "current与config相同_default": (dict(current=_A, config=_A), "default"),
    "current与config不同_override": (dict(current=_B, config=_A), "override"),
    "网关的config与配置文件不同_按网关的config比_default": (dict(current=_B, config=_B), "default"),
    "网关没给config_拿配置文件那一组比_override": (dict(current=_B, config=au.OMIT), "override"),
}


@pytest.mark.parametrize("reply,source", list(SOURCE_CASES.values()), ids=list(SOURCE_CASES))
def test_bots_网关连得上_provider_source按current与config是否同一组(adapter, reply, source):
    """补充文件 5.1：current 与 config 同一组为 default，不同为 override；返回里没有 config 时
    拿这只 bot 配置文件里那一组比。配置文件里写的是 CFG_PROVIDER / CFG_MODEL。"""
    gw = adapter.model_gateway(**reply)
    adapter.running_bot("botalpha", gw)
    bots = adapter.ok(adapter.call("GET", au.BOTS))["bots"]
    assert len(bots) == 1, bots
    reads = [r for r in gw.requests if (r["method"], r["path"]) == ("GET", au.GW_MODEL)]
    assert reads, "列 bot 时没有向这只 bot 的网关读当前生效的一组（第 5 节），下面的判断就是空转：%r" % gw.requests
    assert bots[0]["provider_source"] == source, \
        "provider_source 应为 %s，实际 %r：%r" % (source, bots[0]["provider_source"], bots[0])


def test_bots_只单独指定了思考强度_current与config相同_provider_source为default(adapter):
    """补充文件 5.1：只单独指定了思考强度、供应商和模型没变的，仍算 default。"""
    gw = adapter.model_gateway(effort="high")         # current 与 config 都是配置文件里那一组
    adapter.running_bot("botalpha", gw)
    bots = adapter.ok(adapter.call("GET", au.BOTS))["bots"]
    assert len(bots) == 1, bots
    effort = (bots[0]["provider"] or {}).get("effort")
    assert effort == "high", "网关说单独指定了思考强度 high，provider.effort 却是 %r，下面的判断就是空转" % (effort,)
    assert bots[0]["provider_source"] == "default", \
        "只改了思考强度，provider_source 应为 default，实际 %r：%r" % (bots[0]["provider_source"], bots[0])


def test_泄密_各种成功和失败的响应里都搜不到假密钥假令牌假聊天号(adapter):
    adapter.bot("botalpha", gw_port=au.free_port())
    adapter.bot("botbravo", enabled=False, gw_port=au.free_port())
    adapter.heartbeat.write("botalpha", pid=4242)
    wrong = "Bearer " + au.WRONG_TOKEN
    requests = {
        "meta": ("GET", au.META, au.NO_BODY, au.GOOD),
        "bot 列表": ("GET", au.BOTS, au.NO_BODY, au.GOOD),
        "供应商摘要": ("GET", au.PROVIDERS, au.NO_BODY, au.GOOD),
        "没带令牌的 401": ("GET", au.BOTS, au.NO_BODY, None),
        "令牌不对的 401": ("GET", au.BOTS, au.NO_BODY, wrong),
        "未知路径的 404": ("GET", au.PREFIX + "/nope", au.NO_BODY, au.GOOD),
        "bot_id 不合规的 422": ("POST", au.provider_url(".."), {"effort": "high"}, au.GOOD),
        "没有这只 bot 的 404": ("POST", au.restart_url("ghost"), {"force": True}, au.GOOD),
        "不支持回到默认的 501": ("POST", au.provider_url("botalpha"), {"reset": True}, au.GOOD),
        "不支持等空闲重启的 501": ("POST", au.restart_url("botalpha"), {"force": False}, au.GOOD),
        "没有这家供应商的 404": ("POST", au.provider_url("botalpha"),
                          {"provider_id": "no-such-provider-zz9", "model": "m"}, au.GOOD),
        "网关连不上的 503": ("POST", au.provider_url("botalpha"), {"effort": "high"}, au.GOOD),
        "停用时换供应商的 409": ("POST", au.provider_url("botbravo"), {"effort": "high"}, au.GOOD),
        "停用时重启的 409": ("POST", au.restart_url("botbravo"), {"force": True}, au.GOOD),
    }
    for label, (method, path, body, auth) in requests.items():
        text = au.response_text(adapter.call(method, path, body, auth=auth))
        for name, secret in au.SECRETS.items():
            assert secret not in text, "「%s」的响应里出现了%s" % (label, name)
        assert au.WRONG_TOKEN not in text, "「%s」的响应把发过来的错令牌原样带了回去" % label
