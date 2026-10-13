# -*- coding: utf-8 -*-
"""适配服务验收：三个只读接口的返回形状，以及"响应里没有敏感内容"。

依据：INTERFACE-适配约定-v0.3.md 5.1 到 5.3，INTERFACE-适配服务.md 4.1 到 4.3。不读实现。
"""
import re
import time

import pytest

from . import adapter_util as au
from . import botops_util as u
from .adapter_util import adapter, console_guard  # noqa: F401  夹具


def test_meta_固定取值与八项能力如约(adapter):
    body = adapter.ok(adapter.call("GET", au.META))
    assert body["backend"] == "dsh"
    assert body["adapter"] == "v1"
    version = body["backend_version"]
    assert isinstance(version, str) and version.strip(), "backend_version 应是非空字符串：%r" % (version,)
    assert body["effort_levels"] == au.EFFORT_LEVELS
    caps = body["capabilities"]
    assert caps == au.CAPABILITIES, "能力清单应恰好是约定的八个键与取值"
    assert all(type(v) is bool for v in caps.values()), "能力清单的值必须是布尔：%r" % (caps,)


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
    resp = adapter.call("GET", au.PROVIDERS)
    body = adapter.ok(resp)
    providers = body["providers"]
    assert isinstance(providers, list) and providers, "供应商摘要应至少列出内置供应商：%r" % (providers,)
    for p in providers:
        assert {"id", "name", "has_key", "models"} <= set(p), p
        assert isinstance(p["id"], str) and p["id"], p
        assert p["name"] == p["id"], "id 应等于 name：%r" % (p,)
        assert type(p["has_key"]) is bool, "has_key 必须是布尔：%r" % (p,)
        assert isinstance(p["models"], list), p
        assert all(isinstance(m, str) and m for m in p["models"]), p
    ids = [p["id"] for p in providers]
    assert len(ids) == len(set(ids)), "供应商 id 有重复，下拉里分不清：%r" % ids
    assert body["default"] == {"label": au.DEFAULT_LABEL}
    text = resp.get_data(as_text=True)
    assert "://" not in text, "供应商摘要里带了接口地址"
    hit = re.search(r"[A-Z][A-Z0-9_]*_KEY\b", text)
    assert hit is None, "供应商摘要里带了密钥的环境变量名：%r" % (hit and hit.group(0))


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
