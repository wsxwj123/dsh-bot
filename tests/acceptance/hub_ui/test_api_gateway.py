# -*- coding: utf-8 -*-
"""契约 2 连上网关的分支：透传、504、多网关语义、两个键同源、密钥卫生。

网关用本机假件（stub_gateway）扮演，只在这一层用替身：管理台对网关的调用是 HTTP，
假件按契约 3 的形状回应，管理台的行为（路由、校验、转发、错误映射）都是真的。
真网关的端到端见 test_real_gateway.py。
"""
import pytest

from conftest import free_port
from stub_gateway import StubGateway

SAVE = "/hub/api/dsh-model/provider/save"
REMOVE = "/hub/api/dsh-model/provider/{}/remove"
MODELS = "/hub/api/dsh-model/provider/{}/models"
EFFORT = "/hub/api/dsh-model/{}/effort"

TWO_PROVIDERS = {
    "ok": True,
    "current": {"provider": "deepseek-official", "model": "deepseek-flash"},
    "providers": [
        {"name": "deepseek-official", "source": "builtin", "api": "openai-completions",
         "models": 2, "key": "ok", "enabled": True, "note": None, "last_refresh": None},
        {"name": "myproxy", "source": "custom", "api": "anthropic-messages",
         "models": 12, "key": "ok", "enabled": True, "note": None,
         "last_refresh": {"at": 0, "ok": True, "count": 12, "reason": None}},
    ],
}
PROVIDER_DETAIL = {
    "ok": True,
    "providers": [
        {"name": "myproxy", "api": "anthropic-messages", "baseURL": "https://api.example.com",
         "modelList": [{"id": "m1", "contextWindow": 131072, "guessed": False}],
         "manualModels": ["m2"], "key": "ok", "enabled": True, "note": None,
         "lastRefresh": {"at": 0, "ok": True, "count": 12, "reason": None}},
    ],
}
SAVE_OK = {"ok": True, "kind": "created", "name": "myproxy", "count": 3, "reason": None,
           "text": "已添加供应商「myproxy」，拉到 3 个模型。", "epoch_changed": False}


@pytest.fixture
def gw():
    s = StubGateway(key="test-api-key-0123456789")
    s.start()
    try:
        yield s
    finally:
        s.stop()


def save_body(key="sk-test-000000000000"):
    return {"name": "myproxy", "api": "anthropic-messages",
            "baseURL": "https://api.example.com", "key": key, "mode": "create"}


# ---------------------------------------------------------------- 列表（2.1）

def test_列表_providers透传加custom项合并_两个键来自同一次读(hub, gw, dsh_bot):
    dsh_bot(name="testbot", port=gw.port)
    gw.on("/v1/model", 200, TWO_PROVIDERS)
    gw.on("/v1/provider", 200, PROVIDER_DETAIL)
    r = hub.get("/hub/api/dsh-model")
    assert r.status_code == 200, "实得 %s" % r.status_code
    j = r.get_json()
    provs = {p.get("name"): p for p in (j.get("providers") or [])}
    assert set(provs) == {"deepseek-official", "myproxy"}, "providers 应含两项，实得 %s" % list(provs)
    assert provs["deepseek-official"].get("source") == "builtin", "内置项应原样带 source"
    assert provs["deepseek-official"].get("models") == 2, "models 应是数字"
    custom = provs["myproxy"]
    assert custom.get("base_url") == "https://api.example.com", "custom 项应合并 base_url，实得 %s" % custom.get("base_url")
    assert custom.get("model_list") == [{"id": "m1", "contextWindow": 131072, "guessed": False}], \
        "custom 项应合并 model_list，实得 %s" % custom.get("model_list")
    assert custom.get("models") == 12, "providers 里的 models 仍是数字（不是清单）"
    assert custom.get("last_refresh") == {"at": 0, "ok": True, "count": 12, "reason": None}
    proj = (j.get("custom_providers") or [])
    assert len(proj) == 1, "custom_providers 应只剩 custom 那一项，实得 %s" % proj
    for k in ("name", "api", "models", "key", "enabled", "note", "last_refresh"):
        assert proj[0].get(k) == custom.get(k), "custom_providers 的 %s 应与 providers 同值，实得 %s vs %s" % (k, proj[0].get(k), custom.get(k))
    # /v1/model 的次数不锁死（bots 行也要读它）；/v1/provider 只该对选中的网关拉一次
    assert len(gw.hits("/v1/model")) >= 1, "应读过 /v1/model"
    assert len(gw.hits("/v1/provider")) == 1, "只应对选中的网关拉一次 /v1/provider，实得 %s 次" % len(gw.hits("/v1/provider"))
    assert gw.hits("/v1/model")[0]["auth_ok"], "管理台调网关应带对 Bearer 口令"


def test_列表_两个键必须来自同一个网关(hub, gw, dsh_bot):
    """契约 2.1：providers 与 custom_providers 取自同一网关的同一次读，不许各选各的。

    两个网关给的数据明显不同，两个键要么都是 a 的、要么都是 b 的，不许一个来自 a 一个来自 b。
    """
    gwB = StubGateway(key="test-api-key-0123456789").start()
    try:
        dsh_bot(name="bota", port=gw.port)
        dsh_bot(name="botb", port=gwB.port)
        gw.on("/v1/model", 200, {"ok": True, "providers": [
            {"name": "from-a", "source": "custom", "api": "openai-completions", "models": 1,
             "key": "ok", "enabled": True, "note": None, "last_refresh": None}]})
        gw.on("/v1/provider", 200, {"ok": True, "providers": [
            {"name": "from-a", "api": "openai-completions", "baseURL": "https://a.example.com",
             "modelList": [], "manualModels": [], "key": "ok", "enabled": True, "note": None, "lastRefresh": None}]})
        gwB.on("/v1/model", 200, {"ok": True, "providers": [
            {"name": "from-b", "source": "custom", "api": "openai-completions", "models": 9,
             "key": "ok", "enabled": True, "note": None, "last_refresh": None}]})
        gwB.on("/v1/provider", 200, {"ok": True, "providers": [
            {"name": "from-b", "api": "openai-completions", "baseURL": "https://b.example.com",
             "modelList": [], "manualModels": [], "key": "ok", "enabled": True, "note": None, "lastRefresh": None}]})
        r = hub.get("/hub/api/dsh-model")
        assert r.status_code == 200, "实得 %s" % r.status_code
        j = r.get_json()
        names = sorted(p.get("name") for p in (j.get("providers") or []))
        proj = sorted(p.get("name") for p in (j.get("custom_providers") or []))
        assert names in (["from-a"], ["from-b"]), "providers 应来自某一个网关，实得 %s" % names
        assert proj == names, "custom_providers 应与 providers 同源，实得 providers=%s custom_providers=%s" % (names, proj)
        total = len(gw.hits("/v1/provider")) + len(gwB.hits("/v1/provider"))
        assert total == 1, \
            "只该对选中的网关拉一次 /v1/provider，实得 a=%s b=%s" % (len(gw.hits("/v1/provider")), len(gwB.hits("/v1/provider")))
    finally:
        gwB.stop()


def test_列表_详情接口503时降级为空且仍200(hub, gw, dsh_bot):
    dsh_bot(name="testbot", port=gw.port)
    gw.on("/v1/model", 200, TWO_PROVIDERS)
    gw.on("/v1/provider", 503, {"ok": False, "error": "providers_unreadable", "text": "先修好 providers.json"})
    r = hub.get("/hub/api/dsh-model")
    assert r.status_code == 200, "读接口恒 200，实得 %s" % r.status_code
    custom = [p for p in r.get_json().get("providers") or [] if p.get("source") == "custom"][0]
    assert custom.get("base_url") is None, "详情读不到时 base_url 应是 null，实得 %s" % custom.get("base_url")
    assert custom.get("model_list") == [], "详情读不到时 model_list 应是 []，实得 %s" % custom.get("model_list")


def test_列表_详情接口缺失404也降级(hub, gw, dsh_bot):
    dsh_bot(name="testbot", port=gw.port)
    gw.on("/v1/model", 200, TWO_PROVIDERS)
    r = hub.get("/hub/api/dsh-model")
    custom = [p for p in r.get_json().get("providers") or [] if p.get("source") == "custom"][0]
    assert custom.get("base_url") is None and custom.get("model_list") == [], \
        "实得 base_url=%s model_list=%s" % (custom.get("base_url"), custom.get("model_list"))


def test_列表_模型接口500时providers降级为空且仍200(hub, gw, dsh_bot):
    dsh_bot(name="testbot", port=gw.port)
    gw.on("/v1/model", 500, {"error": "boom"})
    r = hub.get("/hub/api/dsh-model")
    assert r.status_code == 200, "读接口恒 200，实得 %s" % r.status_code
    assert r.get_json().get("providers") == [], "读不到时应是 []，实得 %s" % r.get_json().get("providers")
    assert r.get_json().get("custom_providers") == []


def test_列表_网关返回里key字段只是状态词(hub, gw, dsh_bot):
    dsh_bot(name="testbot", port=gw.port)
    gw.on("/v1/model", 200, TWO_PROVIDERS)
    gw.on("/v1/provider", 200, PROVIDER_DETAIL)
    text = hub.get("/hub/api/dsh-model").get_data(as_text=True)
    for p in (hub.get("/hub/api/dsh-model").get_json().get("providers") or []):
        assert p.get("key") in ("ok", "missing", "unknown"), "key 只能是状态词，实得 %s" % p.get("key")
    assert "sk-" not in text, "列表响应里不该出现密钥形状的值"


# ---------------------------------------------------------------- 写端点透传（2.2、2.3、2.4、2.6）

def test_保存_成功透传网关正文与状态码(hub, gw, dsh_bot):
    dsh_bot(name="testbot", port=gw.port)
    gw.on("/v1/provider/save", 200, SAVE_OK)
    r = hub.post(SAVE, json=save_body())
    assert r.status_code == 200, "实得 %s" % r.status_code
    assert r.get_json() == SAVE_OK, "应原样透传网关正文，实得 %s" % r.get_json()
    got = gw.hits("/v1/provider/save")[0]["body"]
    assert got == save_body(), "转发给网关的请求体应原样，实得 %s" % got


def test_保存_网关业务错误原样透传(hub, gw, dsh_bot):
    dsh_bot(name="testbot", port=gw.port)
    gw.on("/v1/provider/save", 409, {"ok": False, "error": "busy", "text": "别的 bot 正在改供应商，请稍后再试"})
    r = hub.post(SAVE, json=save_body())
    assert r.status_code == 409, "应原样透传 409，实得 %s" % r.status_code
    assert r.get_json().get("error") == "busy", "实得 %s" % r.get_json()


def test_保存_网关发出的请求中断_504_gateway_timeout(hub, gw, dsh_bot):
    """契约 2.2：发出后中断一律 504，不自动换网关重试。"""
    dsh_bot(name="testbot", port=gw.port)
    gw.mode = "drop"
    r = hub.post(SAVE, json=save_body())
    assert r.status_code == 504, "应 504，实得 %s（%s）" % (r.status_code, r.get_json())
    j = r.get_json()
    assert j.get("error") == "gateway_timeout", "实得 %s" % j
    assert "可能已经保存成功" in str(j.get("text")), "text 应提醒用户核对结果，实得 %s" % j.get("text")


def test_删除_成功透传(hub, gw, dsh_bot):
    dsh_bot(name="testbot", port=gw.port)
    gw.on("/v1/provider/remove", 200, {"ok": True, "name": "myproxy", "key_grace_ms": 600000, "text": "已删除供应商「myproxy」。"})
    r = hub.post(REMOVE.format("myproxy"), json={})
    assert r.status_code == 200, "实得 %s" % r.status_code
    assert r.get_json().get("key_grace_ms") == 600000, "实得 %s" % r.get_json()


def test_模型_成功透传(hub, gw, dsh_bot):
    dsh_bot(name="testbot", port=gw.port)
    gw.on("/v1/provider/model", 200, {"ok": True, "text": "已给「myproxy」加上模型 m1（上下文 128000）。几秒后就能切过去。"})
    r = hub.post(MODELS.format("myproxy"), json={"action": "add", "id": "m1", "contextWindow": 128000})
    assert r.status_code == 200, "实得 %s" % r.status_code
    assert r.get_json().get("ok") is True, "实得 %s" % r.get_json()


def test_思考GET_透传current与choices(hub, gw, dsh_bot):
    dsh_bot(name="testbot", port=gw.port)
    gw.on("/v1/effort", 200, {"ok": True, "current": "low", "choices": ["off", "low", "high", "max"]})
    r = hub.get(EFFORT.format("testbot"))
    assert r.status_code == 200, "实得 %s" % r.status_code
    assert r.get_json() == {"ok": True, "current": "low", "choices": ["off", "low", "high", "max"]}, "实得 %s" % r.get_json()


def test_思考POST_成功与409都透传(hub, gw, dsh_bot):
    dsh_bot(name="testbot", port=gw.port)
    gw.on("/v1/effort", 200, {"ok": True, "text": "已把思考强度改成「低」，下一条消息起生效（不换新会话）。"})
    r = hub.post(EFFORT.format("testbot"), json={"effort": "low"})
    assert r.status_code == 200, "实得 %s" % r.status_code
    gw.on("/v1/effort", 409, {"ok": False, "error": "unsupported", "text": "这个模型现在不支持这个档位"})
    r2 = hub.post(EFFORT.format("testbot"), json={"effort": "max"})
    assert r2.status_code == 409, "档位不支持应原样透传 409，实得 %s" % r2.status_code
    assert r2.get_json().get("error") == "unsupported", "实得 %s" % r2.get_json()


# ---------------------------------------------------------------- 多网关语义（契约 2.2，本轮钉死）

def test_多网关_第一个拒连_换下一个(hub, gw, dsh_bot):
    dsh_bot(name="bota", port=free_port())     # 没人听
    dsh_bot(name="botb", port=gw.port)
    gw.on("/v1/provider/save", 200, SAVE_OK)
    r = hub.post(SAVE, json=save_body())
    assert r.status_code == 200, "第一个连不上应换第二个，实得 %s（%s）" % (r.status_code, r.get_json())
    assert len(gw.hits("/v1/provider/save")) == 1, "第二个网关应收到请求"


def test_多网关_第一个回了业务错误_不再试下一个(hub, gw, dsh_bot):
    dsh_bot(name="bota", port=gw.port)
    gwB = StubGateway(key="test-api-key-0123456789").start()
    try:
        dsh_bot(name="botb", port=gwB.port)
        gw.on("/v1/provider/save", 409, {"ok": False, "error": "busy", "text": "别的 bot 正在改供应商，请稍后再试"})
        gwB.on("/v1/provider/save", 200, SAVE_OK)
        r = hub.post(SAVE, json=save_body())
        assert r.status_code == 409, "应原样透传第一个网关的业务错误，实得 %s" % r.status_code
        assert gwB.hits("/v1/provider/save") == [], "回了业务错误就不该再试下一个网关"
    finally:
        gwB.stop()


def test_多网关_第一个中断_504且不换下一个(hub, gw, dsh_bot):
    dsh_bot(name="bota", port=gw.port)
    gwB = StubGateway(key="test-api-key-0123456789").start()
    try:
        dsh_bot(name="botb", port=gwB.port)
        gwB.on("/v1/provider/save", 200, SAVE_OK)
        gw.mode = "drop"
        r = hub.post(SAVE, json=save_body())
        assert r.status_code == 504, "中断应 504，实得 %s（%s）" % (r.status_code, r.get_json())
        assert gwB.hits("/v1/provider/save") == [], "发出后中断不许自动换网关重试（重试会变成第二次写）"
    finally:
        gwB.stop()


def test_多网关_都连不上_503_no_gateway(hub, dsh_bot):
    dsh_bot(name="bota", port=free_port())
    dsh_bot(name="botb", port=free_port())
    r = hub.post(SAVE, json=save_body())
    assert r.status_code == 503, "实得 %s" % r.status_code
    assert r.get_json() == {"ok": False, "error": "no_gateway", "text": "没有在运行的新系统 bot"}, "实得 %s" % r.get_json()


# ---------------------------------------------------------------- 校验先于网关调用（反向用例）

def test_请求体不是JSON对象_网关一个请求都不该收到(hub, gw, dsh_bot):
    dsh_bot(name="testbot", port=gw.port)
    r = hub.post(SAVE, data="not json", content_type="text/plain")
    assert r.status_code == 400, "实得 %s" % r.status_code
    assert gw.requests == [], "参数都没校验过就不该打扰网关，实得 %s" % gw.requests


def test_名字不合法_网关一个请求都不该收到(hub, gw, dsh_bot):
    dsh_bot(name="testbot", port=gw.port)
    r = hub.post(SAVE, json=save_body() | {"name": "bad.name"})
    assert r.status_code == 400, "实得 %s" % r.status_code
    assert gw.requests == [], "实得 %s" % gw.requests


# ---------------------------------------------------------------- 密钥卫生（契约 5）

def test_密钥只出现在转发给网关的请求里(hub, gw, dsh_bot, capsys):
    dsh_bot(name="testbot", port=gw.port)
    gw.on("/v1/provider/save", 200, SAVE_OK)
    KEY = "test-key-7unique-manage-0001"
    capsys.readouterr()                       # 清掉夹具阶段的输出
    r = hub.post(SAVE, json=save_body(key=KEY))
    assert r.status_code == 200, "实得 %s" % r.status_code
    out = capsys.readouterr()
    assert KEY not in (out.out + out.err), "密钥出现在了管理台的输出里"
    assert KEY not in r.get_data(as_text=True), "密钥出现在了管理台的响应里"
    got = gw.hits("/v1/provider/save")[0]["body"]
    assert got.get("key") == KEY, "密钥应原样转发给网关，实得 %s" % got.get("key")


def test_管理台调本机网关不该被http_proxy劫持(hub, gw, dsh_bot, monkeypatch):
    """管理台到 127.0.0.1 的调用必须绕开 http_proxy（这台机器的代理是常驻的）。

    不绕开的后果：请求被代理接走，管理台的列表、保存、删除、模型、effort 全部失败
    （实测代理会回 502 空正文，管理台一路显示「网关没响应」）。
    契约要求管理台能调通网关，所以这里钉住"本机直连"。
    """
    dsh_bot(name="testbot", port=gw.port)
    gw.on("/v1/model", 200, TWO_PROVIDERS)
    gw.on("/v1/provider", 200, PROVIDER_DETAIL)
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:%d" % free_port())   # 一个没人听的"代理"
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:%d" % free_port())
    r = hub.get("/hub/api/dsh-model")
    assert r.status_code == 200, "实得 %s" % r.status_code
    j = r.get_json()
    assert [p.get("name") for p in (j.get("providers") or [])] == ["deepseek-official", "myproxy"], \
        "设了 http_proxy 后管理台就没连上网关了，实得 %s" % j.get("providers")
    assert gw.hits("/v1/model"), "网关一个请求都没收到，请求被代理接走了"


def test_504响应里也没有密钥(hub, gw, dsh_bot, capsys):
    dsh_bot(name="testbot", port=gw.port)
    gw.mode = "drop"
    KEY = "test-key-7unique-manage-0002"
    capsys.readouterr()
    r = hub.post(SAVE, json=save_body(key=KEY))
    assert r.status_code == 504, "实得 %s" % r.status_code
    body_text = r.get_data(as_text=True)
    out = capsys.readouterr()
    assert KEY not in body_text, "504 响应里不该有密钥"
    assert KEY not in (out.out + out.err), "504 路径输出里不该有密钥"
