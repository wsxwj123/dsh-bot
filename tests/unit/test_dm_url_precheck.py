# -*- coding: utf-8 -*-
"""模型页保存供应商时的地址预检的白盒单测（INTERFACE-新系统bot运维 §11e，第 8 稿）。

管理台与网关各用各的规则拆地址：`http://127.0.0.1:8317\\@example.com/..` 管理台认出的
主机是 example.com，网关认出的是 127.0.0.1。所以在内网判定之前先看这串地址本身：含
反斜杠、`@`、任何空白字符、控制字符（U+0000 到 U+001F、U+007F）就回 400 `bad_url` 加
固定文案，不往下判。

直接调 `hub_routes._dm_check_base_url`。内网判定那个函数换成记账的替身：被拦的地址一次
都不许走到它；放行的地址原样交给它（不发一次真 DNS）。
"""
import pytest
from flask import Flask

from moments import hub_routes, provider_model

TEXT = "接口地址里不能有反斜杠、@ 或空白字符"


@pytest.fixture
def check(monkeypatch):
    """返回 (调预检的函数, 走到内网判定的地址清单)。"""
    reached = []

    def _recorder(raw, allow_private=False, resolve=None):
        reached.append(raw)
        return raw
    monkeypatch.setattr(provider_model, "_check_base_url", _recorder)
    app = Flask(__name__)

    def run(raw):
        with app.app_context():
            out = hub_routes._dm_check_base_url(raw)
            return None if out is None else (out[1], out[0].get_json())
    return run, reached


BAD = [
    pytest.param("http://127.0.0.1:8317\\@example.com/..", id="backslash-at-bypass"),
    pytest.param("https://user:pass@api.example.com", id="userinfo"),
    pytest.param("https://api.example.com\\v1", id="backslash"),
    pytest.param("https://api.example.com/v1@x", id="at-in-path"),
    pytest.param("https://api.example.com/v1?to=a@b", id="at-in-query"),
    pytest.param(" https://api.example.com/v1", id="leading-space"),
    pytest.param("https://api.example.com/v1 ", id="trailing-space"),
    pytest.param("https://api.exa\tmple.com", id="tab"),
    pytest.param("https://api.example.com/v1\n", id="trailing-newline"),
    pytest.param("https://api.example.com/\r\nv1", id="crlf"),
    pytest.param("https://api.example.com/v1\u3000", id="ideographic-space"),
    pytest.param("https://api.example.com/v1\u00a0", id="no-break-space"),
    pytest.param("https://api.example.com/v1\u2028", id="line-separator"),
    pytest.param("https://api.example.com/v1\u0085", id="next-line"),
    pytest.param("https://api.example.com/\u0000v1", id="U+0000"),
    pytest.param("https://api.example.com/\u0001v1", id="U+0001"),
    pytest.param("https://api.example.com/\u001fv1", id="U+001F"),
    pytest.param("https://api.example.com/\u007fv1", id="U+007F"),
    pytest.param("@", id="only-at"),
    pytest.param(" ", id="only-space"),
]


@pytest.mark.parametrize("url", BAD)
def test_带禁用字符的地址_400_bad_url与固定文案(check, url):
    run, _ = check
    assert run(url) == (400, {"ok": False, "error": "bad_url", "text": TEXT})


@pytest.mark.parametrize("url", BAD)
def test_带禁用字符的地址_不走到内网判定(check, url):
    run, reached = check
    run(url)
    assert reached == [], "被拦的地址还是进了内网判定：%r" % reached


@pytest.mark.parametrize("url", ["https://api.example.com/v1", "http://[2606:4700::1]:8080/v1",
                                 "https://api.example.com/v1?x=1&y=%40", "https://例子.example.com",
                                 "https://api.example.com/a%20b"])
def test_正常地址_放行并原样交给内网判定(check, url):
    run, reached = check
    assert run(url) is None
    assert reached == [url]


@pytest.mark.parametrize("raw", [None, 123, ["https://a.example.com"], ""])
def test_不是字符串或是空串_预检不拦_交给内网判定去报(check, raw):
    """预检只看「这串地址里有没有那几类字符」；缺省、类型不对的照旧由下一步判。"""
    run, reached = check
    assert run(raw) is None
    assert reached == [raw]


def test_答复里不带回那串地址(check):
    run, _ = check
    code, body = run("https://user:s3cret-pass@api.example.com")
    assert code == 400 and "s3cret-pass" not in repr(body)


def test_旧系统供应商页共用的那个判定函数没有跟着变():
    """只加在模型页这条路径上：带账号的地址在那个函数里照旧按主机判（不发 DNS）。"""
    url = "https://user:pass@93.184.216.34/v1"
    assert provider_model._check_base_url(url, resolve=lambda _h: []) == url
