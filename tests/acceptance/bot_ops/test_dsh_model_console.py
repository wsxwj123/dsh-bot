# -*- coding: utf-8 -*-
"""§11c 与 §12b：管理台到网关的新端点（第 4、5 稿追加）。

- `POST /hub/api/dsh-model/provider/<name>/preview`（新增，只读）
- `POST /hub/api/dsh-model/provider/<name>/refresh` 的 only 扩展（校验在管理台侧、
  转发之前；零转发是硬断言）
- `POST /hub/api/dsh-model/provider/<name>/test`（新增，只读）

网关用本机假件（StubGateway）扮演；管理台的路由、校验、转发、错误映射都是真的。
"""
from urllib.parse import quote

import pytest

PREVIEW = "/hub/api/dsh-model/provider/%s/preview"
REFRESH = "/hub/api/dsh-model/provider/%s/refresh"
TEST = "/hub/api/dsh-model/provider/%s/test"

GW_PREVIEW = "/v1/provider/preview"
GW_REFRESH = "/v1/provider/refresh"
GW_TEST = "/v1/provider/test"

PREVIEW_OK = {
    "ok": True, "count": 2,
    "models": [
        {"id": "m1", "contextWindow": 131072, "in_list": True, "in_fetched": True,
         "manual": False, "default_checked": True},
        {"id": "m2", "contextWindow": None, "in_list": False, "in_fetched": True,
         "manual": False, "default_checked": True},
    ],
}
TEST_OK = {"ok": True, "model": "m1", "latency_ms": 37, "status": 200,
           "text": "连接正常：用 m1 测通，37 毫秒"}


def _url(tpl, name):
    return tpl % quote(name, safe="")


def _one_gw(web_client, gw_bot, gw_stub):
    gw_bot(port=gw_stub.port)
    return web_client


# ============================================================ §11c preview

def test_preview_没有网关_503_no_gateway精确(web_client):
    r = web_client.post(_url(PREVIEW, "myproxy"), json={})
    assert r.status_code == 503, r.get_json()
    assert r.get_json() == {"ok": False, "error": "no_gateway", "text": "没有在运行的新系统 bot"}


def test_preview_有bot但没api_port_等于没有网关(web_client, gw_bot):
    gw_bot(port=None)
    r = web_client.post(_url(PREVIEW, "myproxy"), json={})
    assert r.status_code == 503, r.get_json()
    assert r.get_json()["error"] == "no_gateway"


@pytest.mark.parametrize("name", ["bad.name", "-abc", "_abc", "a" * 33, "中文名", "a b", ""])
def test_preview_名字不合法_400_bad_provider且零转发(web_client, gw_stub, gw_bot, name):
    _one_gw(web_client, gw_bot, gw_stub)
    r = web_client.post(_url(PREVIEW, name), json={})
    assert r.status_code == 400, (name, r.status_code, r.get_json())
    body = r.get_json()
    assert body["ok"] is False and body["error"] == "bad_provider", body
    assert body["text"] == "供应商名字不合法", body
    assert gw_stub.requests == [], "参数都没过就不该打扰网关"


@pytest.mark.parametrize("raw", ["[1, 2]", '"text"', "not json"])
def test_preview_体不是JSON对象_400_bad_body且零转发(web_client, gw_stub, gw_bot, raw):
    _one_gw(web_client, gw_bot, gw_stub)
    r = web_client.post(_url(PREVIEW, "myproxy"), data=raw, content_type="application/json")
    assert r.status_code == 400, (raw, r.status_code, r.get_json())
    body = r.get_json()
    assert body["ok"] is False and body["error"] == "bad_body", body
    assert isinstance(body.get("text"), str) and body["text"], body
    assert gw_stub.requests == []


def test_preview_空对象体即可_200原样透传(web_client, gw_stub, gw_bot):
    _one_gw(web_client, gw_bot, gw_stub)
    gw_stub.on(GW_PREVIEW, 200, PREVIEW_OK)
    r = web_client.post(_url(PREVIEW, "myproxy"), json={})
    assert r.status_code == 200, r.get_json()
    assert r.get_json() == PREVIEW_OK, "200 应原样透传网关（{ok,count,models}）"
    hits = gw_stub.hits(GW_PREVIEW)
    assert len(hits) == 1, gw_stub.requests
    assert hits[0]["method"] == "POST" and hits[0]["auth_ok"], hits[0]
    assert hits[0]["body"] == {"name": "myproxy"}, hits[0]["body"]


@pytest.mark.parametrize("status,payload", [
    (502, {"ok": False, "error": "fetch_failed", "reason": "server_error",
           "text": "「myproxy」没拉到模型：对方接口出错（HTTP 500）。"}),
    (503, {"ok": False, "error": "providers_unreadable", "text": "共用供应商文件读不了（格式坏了），先修好"}),
    (409, {"ok": False, "error": "key_missing", "text": "「myproxy」缺密钥"}),
])
def test_preview_网关的4xx5xx原样透传(web_client, gw_stub, gw_bot, status, payload):
    _one_gw(web_client, gw_bot, gw_stub)
    gw_stub.on(GW_PREVIEW, status, payload)
    r = web_client.post(_url(PREVIEW, "myproxy"), json={})
    assert r.status_code == status, r.get_json()
    assert r.get_json() == payload, "错误应原样透传"


def test_preview_发出后中断_504只读文案(web_client, gw_stub, gw_bot):
    _one_gw(web_client, gw_bot, gw_stub)
    gw_stub.mode = "drop"
    r = web_client.post(_url(PREVIEW, "myproxy"), json={})
    assert r.status_code == 504, r.get_json()
    assert r.get_json() == {"ok": False, "error": "gateway_timeout",
                            "text": "网关没有及时响应，请稍后重试"}, r.get_json()


def test_preview_名字含大写下划线_转发原样(web_client, gw_stub, gw_bot):
    _one_gw(web_client, gw_bot, gw_stub)
    gw_stub.on(GW_PREVIEW, 200, {"ok": True, "count": 0, "models": []})
    r = web_client.post(_url(PREVIEW, "My_Proxy"), json={})
    assert r.status_code == 200, r.get_json()
    assert gw_stub.hits(GW_PREVIEW)[0]["body"] == {"name": "My_Proxy"}


def test_preview_多网关_第一个拒连换下一个(web_client, gw_stub, gw_bot):
    from .botops_util import free_port
    gw_bot(name="bota", port=free_port())        # 没人听
    gw_bot(name="botb", port=gw_stub.port)
    gw_stub.on(GW_PREVIEW, 200, PREVIEW_OK)
    r = web_client.post(_url(PREVIEW, "myproxy"), json={})
    assert r.status_code == 200, ("第一个连不上应换第二个", r.get_json())
    assert len(gw_stub.hits(GW_PREVIEW)) == 1


def test_preview_多网关_第一个回了业务错误不再试(web_client, gw_stub, gw_bot):
    from .conftest import StubGateway
    gw_bot(name="bota", port=gw_stub.port)
    other = StubGateway().start()
    try:
        gw_bot(name="botb", port=other.port)
        gw_stub.on(GW_PREVIEW, 400, {"ok": False, "error": "bad_name", "text": "供应商名字不合法"})
        other.on(GW_PREVIEW, 200, PREVIEW_OK)
        r = web_client.post(_url(PREVIEW, "myproxy"), json={})
        assert r.status_code == 400, r.get_json()
        assert r.get_json()["error"] == "bad_name"
        assert other.hits(GW_PREVIEW) == [], "回了业务错误就不该再试下一个网关"
    finally:
        other.stop()


def test_preview_都连不上_503(web_client, gw_bot):
    from .botops_util import free_port
    gw_bot(name="bota", port=free_port())
    gw_bot(name="botb", port=free_port())
    r = web_client.post(_url(PREVIEW, "myproxy"), json={})
    assert r.status_code == 503, r.get_json()
    assert r.get_json()["error"] == "no_gateway"


# ============================================================ §11c refresh 的 only 校验

@pytest.mark.parametrize("only", ["m1", 7, {"m": 1}, True])
def test_refresh_only不是数组_400_bad_only且零转发(web_client, gw_stub, gw_bot, only):
    _one_gw(web_client, gw_bot, gw_stub)
    r = web_client.post(_url(REFRESH, "myproxy"), json={"only": only})
    assert r.status_code == 400, (only, r.status_code, r.get_json())
    body = r.get_json()
    assert body == {"ok": False, "error": "bad_only", "text": "only 必须是模型名数组"}, body
    assert gw_stub.requests == [], "校验没过就零转发"


@pytest.mark.parametrize("only", [[1], ["a", 2], ["a", None], ["a", ["b"]]])
def test_refresh_only含非字符串项_400零转发(web_client, gw_stub, gw_bot, only):
    _one_gw(web_client, gw_bot, gw_stub)
    r = web_client.post(_url(REFRESH, "myproxy"), json={"only": only})
    assert r.status_code == 400, (only, r.status_code, r.get_json())
    assert r.get_json()["error"] == "bad_only"
    assert gw_stub.requests == []


@pytest.mark.parametrize("only", [[""], ["a" * 201], ["a\nb"], ["a\tb"], ["带\x7f控制符"]])
def test_refresh_only项形状非法_400零转发(web_client, gw_stub, gw_bot, only):
    _one_gw(web_client, gw_bot, gw_stub)
    r = web_client.post(_url(REFRESH, "myproxy"), json={"only": only})
    assert r.status_code == 400, (only, r.status_code, r.get_json())
    assert r.get_json()["error"] == "bad_only"
    assert gw_stub.requests == []


def test_refresh_only超过1000项_400零转发(web_client, gw_stub, gw_bot):
    _one_gw(web_client, gw_bot, gw_stub)
    r = web_client.post(_url(REFRESH, "myproxy"), json={"only": ["m%d" % i for i in range(1001)]})
    assert r.status_code == 400, r.get_json()
    assert r.get_json()["error"] == "bad_only"
    assert gw_stub.requests == []


def test_refresh_only恰好1000项_放行转发(web_client, gw_stub, gw_bot):
    _one_gw(web_client, gw_bot, gw_stub)
    ids = ["m%d" % i for i in range(1000)]
    gw_stub.on(GW_REFRESH, 200, {"ok": True, "count": 0, "added": 0, "removed": [],
                                 "kept_manual": 0, "selected": 0, "text": "t"})
    r = web_client.post(_url(REFRESH, "myproxy"), json={"only": ids})
    assert r.status_code == 200, r.get_json()
    assert gw_stub.hits(GW_REFRESH)[0]["body"] == {"name": "myproxy", "only": ids}


def test_refresh_only合法_转发带only_200透传(web_client, gw_stub, gw_bot):
    _one_gw(web_client, gw_bot, gw_stub)
    reply = {"ok": True, "count": 3, "added": 1, "removed": ["b"],
             "kept_manual": 1, "selected": 3,
             "text": "「myproxy」拉到 3 个模型（新增 1 个，去掉 1 个；手动加的 1 个保留）。（按勾选保留 3 个）"}
    gw_stub.on(GW_REFRESH, 200, reply)
    r = web_client.post(_url(REFRESH, "myproxy"), json={"only": ["a", "d", "c"]})
    assert r.status_code == 200, r.get_json()
    assert r.get_json() == reply, "应原样透传"
    assert gw_stub.hits(GW_REFRESH)[0]["body"] == {"name": "myproxy", "only": ["a", "d", "c"]}


def test_refresh_only缺省_只发name_一字不变(web_client, gw_stub, gw_bot):
    _one_gw(web_client, gw_bot, gw_stub)
    gw_stub.on(GW_REFRESH, 200, {"ok": True, "count": 3, "added": 2, "removed": ["x"],
                                 "kept_manual": 1, "selected": 3,
                                 "text": "「myproxy」拉到 3 个模型（新增 2 个，去掉 1 个；手动加的 1 个保留）。"})
    r = web_client.post(_url(REFRESH, "myproxy"), json={})
    assert r.status_code == 200, r.get_json()
    assert gw_stub.hits(GW_REFRESH)[0]["body"] == {"name": "myproxy"}, \
        "缺省 only 时仍只发 {name}（既有行为一字不变）"


def test_refresh_only合法_网关的400原样透传(web_client, gw_stub, gw_bot):
    _one_gw(web_client, gw_bot, gw_stub)
    gw_stub.on(GW_REFRESH, 400, {"ok": False, "error": "bad_only", "text": "only 必须是模型名数组"})
    r = web_client.post(_url(REFRESH, "myproxy"), json={"only": ["ok-model"]})
    assert r.status_code == 400, r.get_json()
    assert r.get_json()["error"] == "bad_only"


# ============================================================ §12b test（测试连接）

def test_test_没有网关_503_no_gateway精确(web_client):
    r = web_client.post(_url(TEST, "myproxy"), json={})
    assert r.status_code == 503, r.get_json()
    assert r.get_json() == {"ok": False, "error": "no_gateway", "text": "没有在运行的新系统 bot"}


@pytest.mark.parametrize("raw", ["[1]", '"s"', "not json"])
def test_test_体不是JSON对象_400_bad_body零转发(web_client, gw_stub, gw_bot, raw):
    _one_gw(web_client, gw_bot, gw_stub)
    r = web_client.post(_url(TEST, "myproxy"), data=raw, content_type="application/json")
    assert r.status_code == 400, (raw, r.status_code, r.get_json())
    body = r.get_json()
    assert body["ok"] is False and body["error"] == "bad_body", body
    assert gw_stub.requests == []


@pytest.mark.parametrize("name", ["bad.name", "a" * 33, "中文名"])
def test_test_名字不合法_400_bad_provider零转发(web_client, gw_stub, gw_bot, name):
    _one_gw(web_client, gw_bot, gw_stub)
    r = web_client.post(_url(TEST, name), json={})
    assert r.status_code == 400, (name, r.status_code, r.get_json())
    body = r.get_json()
    assert body["error"] == "bad_provider" and body["text"] == "供应商名字不合法", body
    assert gw_stub.requests == []


def test_test_发出后中断_504只读文案(web_client, gw_stub, gw_bot):
    _one_gw(web_client, gw_bot, gw_stub)
    gw_stub.mode = "drop"
    r = web_client.post(_url(TEST, "myproxy"), json={})
    assert r.status_code == 504, r.get_json()
    assert r.get_json() == {"ok": False, "error": "gateway_timeout",
                            "text": "网关没有及时响应，请稍后重试"}, r.get_json()


def test_test_成功_200原样透传且转发体对(web_client, gw_stub, gw_bot):
    _one_gw(web_client, gw_bot, gw_stub)
    gw_stub.on(GW_TEST, 200, TEST_OK)
    r = web_client.post(_url(TEST, "myproxy"), json={})
    assert r.status_code == 200, r.get_json()
    assert r.get_json() == TEST_OK, "成功体应原样透传"
    hits = gw_stub.hits(GW_TEST)
    assert len(hits) == 1 and hits[0]["auth_ok"], hits
    assert hits[0]["body"] == {"name": "myproxy"}, hits[0]["body"]


@pytest.mark.parametrize("status,payload", [
    (409, {"ok": False, "error": "no_models",
           "text": "这个供应商还没有模型，先刷新模型列表拿到模型再测"}),
    (502, {"ok": False, "error": "connect_failed", "reason": "unauthorized",
           "text": "对方不认这个密钥（HTTP 401）；用 m1 测的"}),
    (404, {"ok": False, "error": "not_found", "text": "没有 nosuch 这个供应商"}),
])
def test_test_网关的4xx5xx原样透传(web_client, gw_stub, gw_bot, status, payload):
    _one_gw(web_client, gw_bot, gw_stub)
    gw_stub.on(GW_TEST, status, payload)
    r = web_client.post(_url(TEST, "myproxy"), json={})
    assert r.status_code == status, r.get_json()
    assert r.get_json() == payload


def test_test_多网关_第一个拒连换下一个(web_client, gw_stub, gw_bot):
    from .botops_util import free_port
    gw_bot(name="bota", port=free_port())
    gw_bot(name="botb", port=gw_stub.port)
    gw_stub.on(GW_TEST, 200, TEST_OK)
    r = web_client.post(_url(TEST, "myproxy"), json={})
    assert r.status_code == 200, ("第一个连不上应换第二个", r.get_json())
    assert len(gw_stub.hits(GW_TEST)) == 1


def test_test_多网关_第一个回了业务错误不再试(web_client, gw_stub, gw_bot):
    from .conftest import StubGateway
    gw_bot(name="bota", port=gw_stub.port)
    other = StubGateway().start()
    try:
        gw_bot(name="botb", port=other.port)
        gw_stub.on(GW_TEST, 409, {"ok": False, "error": "no_models",
                                  "text": "这个供应商还没有模型，先刷新模型列表拿到模型再测"})
        other.on(GW_TEST, 200, TEST_OK)
        r = web_client.post(_url(TEST, "myproxy"), json={})
        assert r.status_code == 409, r.get_json()
        assert r.get_json()["error"] == "no_models"
        assert other.hits(GW_TEST) == [], "回了业务错误就不该再试下一个网关"
    finally:
        other.stop()


def test_test_名字含大写下划线_转发原样(web_client, gw_stub, gw_bot):
    _one_gw(web_client, gw_bot, gw_stub)
    gw_stub.on(GW_TEST, 200, TEST_OK)
    r = web_client.post(_url(TEST, "My_Proxy"), json={})
    assert r.status_code == 200, r.get_json()
    assert gw_stub.hits(GW_TEST)[0]["body"] == {"name": "My_Proxy"}
