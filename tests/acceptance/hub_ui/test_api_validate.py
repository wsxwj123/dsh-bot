# -*- coding: utf-8 -*-
"""契约 2 的校验与错误：管理台新端点的参数校验、没有网关时的 503、鉴权（admin_unauthorized）。

这些用例都在"没有在跑的新系统网关"的隔离环境里跑（沙箱里没有任何 DSH bot），
连网关的分支见 test_api_gateway.py。
"""
from urllib.parse import quote

import pytest

from .conftest import free_port

SAVE = "/hub/api/dsh-model/provider/save"
REMOVE = "/hub/api/dsh-model/provider/{}/remove"
MODELS = "/hub/api/dsh-model/provider/{}/models"
EFFORT = "/hub/api/dsh-model/{}/effort"

PW = "hub-access-pw-12345"
ADMIN = "hub-admin-pw-67890"


def body(r):
    try:
        return r.get_json()
    except Exception:
        return None


# ---------------------------------------------------------------- 请求体通则

def test_保存_请求体不是JSON_400_bad_body(hub):
    r = hub.post(SAVE, data="not json", content_type="text/plain")
    assert r.status_code == 400, "期望 400，实得 %s" % r.status_code
    assert (body(r) or {}).get("error") == "bad_body", "实得 %s" % body(r)


def test_保存_请求体是JSON数组_400_bad_body(hub):
    r = hub.post(SAVE, data="[1, 2]", content_type="application/json")
    assert r.status_code == 400, "期望 400，实得 %s" % r.status_code
    assert (body(r) or {}).get("error") == "bad_body", "实得 %s" % body(r)


def test_删除_请求体不是JSON对象_400_bad_body(hub):
    r = hub.post(REMOVE.format("myproxy"), data='"myproxy"', content_type="application/json")
    assert r.status_code == 400, "期望 400，实得 %s" % r.status_code
    assert (body(r) or {}).get("error") == "bad_body", "实得 %s" % body(r)


def test_删除_请求体是空对象可以_走到找网关(hub):
    """契约 2.3：请求体可以是 {}（不是 JSON 对象才 400）。"""
    r = hub.post(REMOVE.format("myproxy"), json={})
    assert r.status_code == 503, "空对象应走到找网关（503），实得 %s" % r.status_code
    assert (body(r) or {}).get("error") == "no_gateway", "实得 %s" % body(r)


def test_模型_请求体不是JSON对象_400_bad_body(hub):
    r = hub.post(MODELS.format("myproxy"), data="xyz", content_type="text/plain")
    assert r.status_code == 400, "期望 400，实得 %s" % r.status_code
    assert (body(r) or {}).get("error") == "bad_body", "实得 %s" % body(r)


# ---------------------------------------------------------------- 参数校验

def test_保存_mode不是两个之一_400_bad_body带文案(hub):
    r = hub.post(SAVE, json={"name": "myproxy", "api": "openai-completions",
                             "baseURL": "https://api.example.com/v1", "key": "sk-test-00000000",
                             "mode": "delete"})
    assert r.status_code == 400, "期望 400，实得 %s" % r.status_code
    j = body(r) or {}
    assert j.get("error") == "bad_body", "实得 %s" % j
    assert "create" in str(j.get("text")) and "modify" in str(j.get("text")), \
        "text 应说明 mode 只能是 create 或 modify，实得 %s" % j.get("text")


@pytest.mark.parametrize("name", ["bad.name", "-abc", "_abc", "a" * 33, "中文名", "a b", "a/b"])
def test_保存_名字不合规则_400_bad_provider(hub, name):
    r = hub.post(SAVE, json={"name": name, "api": "openai-completions",
                             "baseURL": "https://api.example.com/v1", "key": "sk-test-00000000"})
    assert r.status_code == 400, "期望 400，实得 %s（名字 %r）" % (r.status_code, name)
    assert (body(r) or {}).get("error") == "bad_provider", "实得 %s" % body(r)


@pytest.mark.parametrize("name", ["bad.name", "-abc", "_abc", "a" * 33, "中文名", "a b"])
def test_删除_名字不合规则_400_bad_provider(hub, name):
    r = hub.post(REMOVE.format(quote(name, safe="")), json={})
    assert r.status_code == 400, "期望 400，实得 %s（名字 %r）" % (r.status_code, name)
    assert (body(r) or {}).get("error") == "bad_provider", "实得 %s" % body(r)


@pytest.mark.parametrize("name", ["bad.name", "-abc", "_abc", "a" * 33, "中文名"])
def test_模型_名字不合规则_400_bad_provider(hub, name):
    r = hub.post(MODELS.format(quote(name, safe="")), json={"action": "add", "id": "m1"})
    assert r.status_code == 400, "期望 400，实得 %s（名字 %r）" % (r.status_code, name)
    assert (body(r) or {}).get("error") == "bad_provider", "实得 %s" % body(r)


@pytest.mark.parametrize("action", ["add2", "", "ADD"])
def test_模型_action不是三个之一_400_bad_action(hub, action):
    r = hub.post(MODELS.format("myproxy"), json={"action": action, "id": "m1"})
    assert r.status_code == 400, "期望 400，实得 %s（action %r）" % (r.status_code, action)
    j = body(r) or {}
    assert j.get("error") == "bad_action", "实得 %s" % j
    assert "add" in str(j.get("text")) and "set_context" in str(j.get("text")), \
        "text 应列出三个 action，实得 %s" % j.get("text")


@pytest.mark.parametrize("mid", ["", 123, None])
def test_模型_id不是非空字符串_400_bad_id(hub, mid):
    r = hub.post(MODELS.format("myproxy"), json={"action": "add", "id": mid})
    assert r.status_code == 400, "期望 400，实得 %s（id %r）" % (r.status_code, mid)
    j = body(r) or {}
    assert j.get("error") == "bad_id", "实得 %s" % j
    assert j.get("text"), "text 应说明模型名不合法，实得 %s" % j.get("text")


# ---------------------------------------------------------------- 没有在跑的新系统网关

def test_保存_没有网关_503_no_gateway精确正文(hub):
    r = hub.post(SAVE, json={"name": "myproxy", "api": "openai-completions",
                             "baseURL": "https://api.example.com/v1", "key": "sk-test-00000000"})
    assert r.status_code == 503, "期望 503，实得 %s" % r.status_code
    assert body(r) == {"ok": False, "error": "no_gateway", "text": "没有在运行的新系统 bot"}, "实得 %s" % body(r)


def test_删除_没有网关_503_no_gateway(hub):
    r = hub.post(REMOVE.format("myproxy"), json={})
    assert r.status_code == 503, "期望 503，实得 %s" % r.status_code
    assert (body(r) or {}).get("error") == "no_gateway", "实得 %s" % body(r)


def test_模型_没有网关_503_no_gateway(hub):
    r = hub.post(MODELS.format("myproxy"), json={"action": "add", "id": "m1"})
    assert r.status_code == 503, "期望 503，实得 %s" % r.status_code
    assert (body(r) or {}).get("error") == "no_gateway", "实得 %s" % body(r)


def test_思考POST_没有网关_503_no_gateway(hub, dsh_bot):
    """bot 在列表里但网关连不上，仍走 503 no_gateway 这条路径。

    只带 hub 时 bot 名单为空，请求会先落成 404 bot_not_found（见下面的 GET 用例），
    所以这里要造一个带端口的 bot，端口选个没人监听的。
    """
    dsh_bot(name="testbot", port=free_port())
    r = hub.post(EFFORT.format("testbot"), json={"effort": "low"})
    assert r.status_code == 503, "期望 503，实得 %s" % r.status_code
    assert (body(r) or {}).get("error") == "no_gateway", "实得 %s" % body(r)


def test_思考GET_bot不存在_404_bot_not_found(hub):
    """契约 2.6：bot 不存在 404 {"error":"bot_not_found"}（没有网关时任何一个 bot_id 都不存在）。"""
    r = hub.get(EFFORT.format("nosuchbot"))
    assert r.status_code == 404, "期望 404，实得 %s" % r.status_code
    assert (body(r) or {}).get("error") == "bot_not_found", "实得 %s" % body(r)


def test_思考POST_bot不存在_404_bot_not_found(hub):
    r = hub.post(EFFORT.format("nosuchbot"), json={"effort": "low"})
    assert r.status_code == 404, "期望 404，实得 %s" % r.status_code
    assert (body(r) or {}).get("error") == "bot_not_found", "实得 %s" % body(r)


def test_思考POST_effort不是字符串_400_bad_body(hub, dsh_bot):
    """契约 2.6：effort 非字符串 400 {"error":"bad_body"}。

    这里用"bot 在列表里但网关连不上"的形态，好让请求能走到 effort 校验那一步。
    """
    dsh_bot(name="testbot", port=free_port())
    for bad in (123, None, True):
        r = hub.post(EFFORT.format("testbot"), json={"effort": bad})
        assert r.status_code == 400, "effort=%r 期望 400，实得 %s" % (bad, r.status_code)
        assert (body(r) or {}).get("error") == "bad_body", "effort=%r 实得 %s" % (bad, body(r))


# ---------------------------------------------------------------- 列表接口的兼容

def test_列表_没有网关时两个键都是空数组(hub):
    r = hub.get("/hub/api/dsh-model")
    assert r.status_code == 200, "读接口恒 200，实得 %s" % r.status_code
    j = body(r) or {}
    assert isinstance(j.get("bots"), list), "bots 应仍是数组，实得 %s" % j.get("bots")
    assert j.get("providers") == [], "没有网关时 providers 应是 []，实得 %s" % j.get("providers")
    assert j.get("custom_providers") == [], "没有网关时 custom_providers 应是 []，实得 %s" % j.get("custom_providers")


def test_列表_恒200不抛网络错误(hub, dsh_bot):
    """有人在列表里但端口没人听：读接口仍 200（网络故障不外抛）。"""
    dsh_bot(name="testbot", port=free_port())
    r = hub.get("/hub/api/dsh-model")
    assert r.status_code == 200, "网关连不上也必须 200，实得 %s" % r.status_code


# ---------------------------------------------------------------- 鉴权（契约第 5 节末）

def test_双口令_只持access_cookie_四个写端点都401_admin_unauthorized(make_client, login):
    c = make_client(HUB_ACCESS_PASSWORD=PW, HUB_ADMIN_PASSWORD=ADMIN)
    assert login(c, PW).status_code == 303, "前置条件 登录应成功"
    calls = [
        ("POST", SAVE, {"name": "myproxy", "api": "openai-completions",
                        "baseURL": "https://api.example.com/v1", "key": "sk-test-00000000"}),
        ("POST", REMOVE.format("myproxy"), {}),
        ("POST", MODELS.format("myproxy"), {"action": "add", "id": "m1"}),
        ("POST", EFFORT.format("testbot"), {"effort": "low"}),
    ]
    for method, path, payload in calls:
        r = c.post(path, json=payload)
        assert r.status_code == 401, "%s 只持 access cookie 应 401，实得 %s" % (path, r.status_code)
        assert body(r) == {"error": "admin_unauthorized"}, "%s 实得 %s" % (path, body(r))


def test_双口令_只持access_cookie_读接口同样401(make_client, login):
    """is_admin_path 覆盖整个 /hub/*（契约第 5 节末点名要钉住）。"""
    c = make_client(HUB_ACCESS_PASSWORD=PW, HUB_ADMIN_PASSWORD=ADMIN)
    login(c, PW)
    r = c.get("/hub/api/dsh-model")
    assert r.status_code == 401, "实得 %s" % r.status_code
    assert (body(r) or {}).get("error") == "admin_unauthorized", "实得 %s" % body(r)


def test_双口令_两把cookie都有_写端点正常通过鉴权(make_client, login):
    c = make_client(HUB_ACCESS_PASSWORD=PW, HUB_ADMIN_PASSWORD=ADMIN)
    assert login(c, PW, ADMIN).status_code == 303, "两把口令都对时登录应成功"
    r = c.post(SAVE, json={"name": "myproxy", "api": "openai-completions",
                           "baseURL": "https://api.example.com/v1", "key": "sk-test-00000000"})
    assert r.status_code == 503, "过了鉴权、没有网关应是 503，实得 %s（%s）" % (r.status_code, body(r))
    assert (body(r) or {}).get("error") == "no_gateway", "实得 %s" % body(r)


def test_单口令模式_拿到全站口令就能改(make_client, login):
    """契约第 5 节末：没设 HUB_ADMIN_PASSWORD 时单口令就是能改（既有设计）。"""
    c = make_client(HUB_ACCESS_PASSWORD=PW)
    assert login(c, PW).status_code == 303
    r = c.post(SAVE, json={"name": "myproxy", "api": "openai-completions",
                           "baseURL": "https://api.example.com/v1", "key": "sk-test-00000000"})
    assert r.status_code == 503, "单口令下应走到找网关（503），实得 %s（%s）" % (r.status_code, body(r))
