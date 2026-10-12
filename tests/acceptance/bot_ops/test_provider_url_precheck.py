# -*- coding: utf-8 -*-
"""§11e 保存供应商时的地址预检（接口约定第 8 稿）。

对象：模型页的保存端点 `POST /hub/api/dsh-model/provider/save`，请求体里的 `baseURL`。

起因：管理台与网关各用各的规则拆地址。`http://127.0.0.1:8317\\@example.com/..` 这种地址，
管理台认出的主机是 example.com，网关认出的是 127.0.0.1，管理台的内网判定就被绕过去了。
所以在做内网判定之前先看这串地址本身：含反斜杠、`@`、任何空白字符、任何控制字符，一律
400 `bad_url`，不做内网判定，零转发。

网关用本机假件（StubGateway）扮演：它在场，却一条请求都收不到，才说明真的没转发。
"""
import pytest

SAVE = "/hub/api/dsh-model/provider/save"
GW_SAVE = "/v1/provider/save"

BAD_URL = {"ok": False, "error": "bad_url", "text": "接口地址里不能有反斜杠、@ 或空白字符"}
SAVE_OK = {"ok": True, "kind": "created", "name": "myproxy", "count": 3, "reason": None,
           "text": "已添加供应商「myproxy」，拉到 3 个模型。", "epoch_changed": False}


def _body(base_url, **over):
    body = {"name": "myproxy", "api": "openai-completions", "baseURL": base_url,
            "key": "sk-test-000000000000", "mode": "create"}
    body.update(over)
    return body


@pytest.fixture
def gw(web_client, gw_bot, gw_stub):
    """有一只在跑的新系统 bot，它的网关是假件；保存请求要是被放行，就会落到这里。"""
    gw_bot(port=gw_stub.port)
    gw_stub.on(GW_SAVE, 200, SAVE_OK)
    return gw_stub


#: 约定点名的四个地址
NAMED = [
    pytest.param("http://127.0.0.1:8317\\@example.com/..", id="backslash-at-bypass"),
    pytest.param("https://user:pass@api.example.com", id="userinfo-at"),
    pytest.param("https://api.example.com/v1 ", id="trailing-space"),
    pytest.param("https://api.exa\tmple.com", id="tab-inside"),
]

#: 约定列的四类字符，每类再各取几例
BY_CLASS = [
    pytest.param("https://api.example.com\\v1", id="backslash-only"),
    pytest.param("https://api.example.com/v1@x", id="at-in-path"),
    pytest.param(" https://api.example.com/v1", id="leading-space"),
    pytest.param("https://api.example.com/v1\n", id="trailing-newline"),
    pytest.param("https://api.example.com/\r\nv1", id="crlf-inside"),
    pytest.param("https://api.example.com/v1　", id="ideographic-space"),
    pytest.param("https://api.example.com/v1 ", id="no-break-space"),
    pytest.param("https://api.example.com/\u0000v1", id="U+0000"),
    pytest.param("https://api.example.com/\u0001v1", id="U+0001"),
    pytest.param("https://api.example.com/\u001fv1", id="U+001F"),
    pytest.param("https://api.example.com/\u007fv1", id="U+007F"),
]


@pytest.mark.parametrize("url", NAMED + BY_CLASS)
def test_保存供应商_地址里有反斜杠at空白或控制字符_400_bad_url与固定文案(web_client, gw, url):
    r = web_client.post(SAVE, json=_body(url))
    assert r.status_code == 400, (r.status_code, r.get_json())
    assert r.get_json() == BAD_URL, r.get_json()


@pytest.mark.parametrize("url", NAMED + BY_CLASS)
def test_保存供应商_地址里有反斜杠at空白或控制字符_零转发(web_client, gw, url):
    web_client.post(SAVE, json=_body(url))
    assert gw.requests == [], ("被拒的地址还是转发给了网关", gw.requests)


def test_保存供应商_正常地址_照常转发且原样透传(web_client, gw):
    """对照：`https://api.example.com/v1` 这样的正常地址不受影响。"""
    body = _body("https://api.example.com/v1")
    r = web_client.post(SAVE, json=body)
    assert r.status_code == 200, (r.status_code, r.get_json())
    assert r.get_json() == SAVE_OK, r.get_json()
    hits = gw.hits(GW_SAVE)
    assert len(hits) == 1, gw.requests
    assert hits[0]["body"] == body, hits[0]["body"]


def test_保存供应商_没有网关在跑_坏地址仍是400不是503(web_client):
    """预检在找网关之前：没有任何新系统 bot 在跑时，答的也是地址的错。"""
    r = web_client.post(SAVE, json=_body("https://user:pass@api.example.com"))
    assert r.status_code == 400, (r.status_code, r.get_json())
    assert r.get_json() == BAD_URL, r.get_json()


def test_保存供应商_名字和地址都不对_先答名字的错(web_client, gw):
    """这一格排在名字校验（bad_provider）之后。"""
    r = web_client.post(SAVE, json=_body("https://user:pass@api.example.com", name="bad.name"))
    assert r.status_code == 400, (r.status_code, r.get_json())
    assert r.get_json()["error"] == "bad_provider", r.get_json()
    assert gw.requests == [], gw.requests


def test_保存供应商_坏地址的答复里不带回那串地址(web_client, gw):
    """带账号密码的地址被拒时，账号密码不该被原样回显。"""
    r = web_client.post(SAVE, json=_body("https://user:s3cret-pass@api.example.com"))
    assert "s3cret-pass" not in r.get_data(as_text=True), r.get_data(as_text=True)
