# -*- coding: utf-8 -*-
"""旧系统供应商入口的白盒单测（INTERFACE-旧系统供应商入口 §1、§2、§4，§7 末条）。

量 moments/web.py 里为这个入口写的几个小函数：地址合不合法、从 Host 头取主机名、读配置项
并回落默认值、按判据二选一；再经测试客户端打 /provider 这一条。最后量全站 JSON 键顺序那个
设置本身。

只碰 /provider 这一条路由：它只读两个配置项（进程环境与 hub.env，后者已被本目录的公共夹具
钉到临时目录），不读任何配置目录、不起子进程、不发网络请求、不跟随跳转。
"""
import os
from pathlib import Path

import pytest
from werkzeug.test import EnvironBuilder

LOCAL = "HUB_LEGACY_PROVIDER_URL_LOCAL"
PUBLIC = "HUB_LEGACY_PROVIDER_URL_PUBLIC"
DEFAULT_LOCAL = "http://127.0.0.1:8767/provider"
DEFAULT_PUBLIC = "https://legacy-provider.invalid/provider"
LOCAL_HOST = "127.0.0.1:8765"
PUBLIC_HOST = "8765.example.com"
FORWARD_HEADERS = ["X-Forwarded-For", "X-Forwarded-Proto", "X-Forwarded-Host", "Forwarded",
                   "CF-Connecting-IP"]


@pytest.fixture
def web(monkeypatch):
    """管理台应用所在的模块。两个配置项、口令、信转发头的开关都清掉，免得继承外面的环境。"""
    for key in (LOCAL, PUBLIC, "HUB_ACCESS_PASSWORD", "HUB_ADMIN_PASSWORD", "HUB_TRUST_PROXY"):
        monkeypatch.delenv(key, raising=False)
    from moments import web as web_mod
    return web_mod


def _go(web, path="/provider", host=LOCAL_HOST, headers=None, method="GET"):
    """打一次，不跟随跳转。host 原样写进 Host 头。"""
    return web.app.test_client().open(path, method=method, headers=headers or {},
                                      environ_overrides={"HTTP_HOST": host}, follow_redirects=False)


def _target(web, host=LOCAL_HOST, headers=None, drop_host=False):
    """在一个造出来的请求里直接调判据函数。drop_host：连 Host 头都没有。"""
    environ = EnvironBuilder(path="/provider", headers=headers or {}).get_environ()
    environ["HTTP_HOST"] = host
    if drop_host:
        del environ["HTTP_HOST"]
    with web.app.request_context(environ):
        return web.legacy_provider_target()


# ============================================================ §1 一个值合不合法

@pytest.mark.parametrize("value", [
    "http://127.0.0.1:9999/p",                           # 约定点名的那个
    "https://legacy.example.test/a/b?c=1&d=2#e",         # 带路径、查询与片段
    "http://[::1]:8767/provider",                        # IPv6 主机
    "https://legacy.example.test",                       # 只有协议与主机
    "https://a.example/" + "x" * (2048 - len("https://a.example/")),     # 恰好 2048 个字符
    DEFAULT_LOCAL, DEFAULT_PUBLIC,
])
def test_合法的地址(web, value):
    assert web._legacy_url_ok(value) is True


@pytest.mark.parametrize("value", [
    # 约定点名的六个
    "javascript:alert(1)", "//evil.example/x", "ftp://a.example/x", "https://a b.example/x",
    "https://u:p@a.example/x", "https:///x",
    # 不是 http:// 或 https:// 开头
    "", "legacy.example.test/provider", "http:/a.example/x", "file:///etc/hosts", "data:text/html,x",
    "HTTP://a.example/x",                                # 约定写的是小写，大写的不认
    # 解析不出非空的主机名
    "http://", "https://:8443/x", "http://?x=1", "http://[::1/x",
    # 反斜杠、@
    "https://a.example\\x", "https://a.example/x\\y", "https://a.example/x@y",
    "http://127.0.0.1:8317\\@evil.example/..",
    # 空白字符与控制字符
    "https://a.example/x y", "https://a.example/x\ty", "https://a.example/x\nX-Injected: 1",
    "https://a.example/x\rX-Injected: 1", "https://a.example/x　y", "https://a.example/x\x00y",
    "https://a.example/x\x01y", "https://a.example/x\x1fy", "https://a.example/x\x7fy",
    # 超长一个字符
    "https://a.example/" + "x" * (2049 - len("https://a.example/")),
])
def test_不合法的地址(web, value):
    assert web._legacy_url_ok(value) is False


# ============================================================ §1 读配置项

def test_没设_用默认值(web):
    assert (web._legacy_url(LOCAL), web._legacy_url(PUBLIC)) == (DEFAULT_LOCAL, DEFAULT_PUBLIC)


def test_设了合法的值_原样用_首尾空白去掉(web, monkeypatch):
    monkeypatch.setenv(LOCAL, "  \thttp://127.0.0.1:9999/p \t ")
    assert web._legacy_url(LOCAL) == "http://127.0.0.1:9999/p"
    assert web._legacy_url(PUBLIC) == DEFAULT_PUBLIC, "一项设了不连带另一项"


@pytest.mark.parametrize("value", ["", "   \t ", "javascript:alert(1)", "ftp://a.example/x",
                                   "https://u:p@a.example/x", "https://a.example/x\r\nX-Injected: 1"])
def test_为空或不合法_回落默认值_不抛错(web, monkeypatch, value):
    monkeypatch.setenv(PUBLIC, value)
    assert web._legacy_url(PUBLIC) == DEFAULT_PUBLIC


def test_不合法时往标准错误写一行提示_说是哪一项_不带原值(web, monkeypatch, capsys):
    monkeypatch.setenv(LOCAL, "https://secret-user:secret-pass@a.example/x")
    assert web._legacy_url(LOCAL) == DEFAULT_LOCAL
    err = capsys.readouterr().err
    assert LOCAL in err and err.count("\n") == 1
    assert "secret" not in err and "a.example" not in err, "提示里带出了配置的原值：%r" % err


def test_合法或没设时不写提示(web, monkeypatch, capsys):
    monkeypatch.setenv(LOCAL, "http://127.0.0.1:9999/p")
    web._legacy_url(LOCAL)
    web._legacy_url(PUBLIC)
    assert capsys.readouterr().err == ""


def test_环境变量没有_读hub_env文件_环境变量有就它赢(web, monkeypatch):
    env_file = Path(os.environ["HUB_ENV_FILE"])           # 公共夹具钉在临时目录里的那个
    env_file.write_text("%s=https://from-file.example.test/x\n" % LOCAL, encoding="utf-8")
    assert web._legacy_url(LOCAL) == "https://from-file.example.test/x"
    monkeypatch.setenv(LOCAL, "https://from-env.example.test/x")
    assert web._legacy_url(LOCAL) == "https://from-env.example.test/x"


def test_环境变量赢了但不合法_直接回落默认值_不再去看文件(web, monkeypatch):
    """读法与别的配置项相同：环境变量非空就是它。它不合法时这一项就是配错了，用默认值。"""
    Path(os.environ["HUB_ENV_FILE"]).write_text("%s=https://from-file.example.test/x\n" % LOCAL,
                                                encoding="utf-8")
    monkeypatch.setenv(LOCAL, "ftp://a.example/x")
    assert web._legacy_url(LOCAL) == DEFAULT_LOCAL


def test_每次现读_改了环境变量下一次就是新值(web, monkeypatch):
    assert web._legacy_url(LOCAL) == DEFAULT_LOCAL
    monkeypatch.setenv(LOCAL, "http://127.0.0.1:9999/p")
    assert web._legacy_url(LOCAL) == "http://127.0.0.1:9999/p"
    monkeypatch.delenv(LOCAL)
    assert web._legacy_url(LOCAL) == DEFAULT_LOCAL


# ============================================================ §2 从 Host 头取主机名

@pytest.mark.parametrize("header,name", [
    ("127.0.0.1:8765", "127.0.0.1"), ("127.0.0.1", "127.0.0.1"),
    ("localhost:8765", "localhost"), ("LOCALHOST", "LOCALHOST"),
    ("[::1]:8765", "::1"), ("[::1]", "::1"), ("[2001:db8::1]:443", "2001:db8::1"),
    ("8765.example.com:443", "8765.example.com"), ("  127.0.0.1:8765  ", "127.0.0.1"),
    ("", ""), (None, ""), (":8765", ""),
    ("[::1", ""),                    # 方括号没配上
    ("::1", ""),                     # 不带方括号的 IPv6 不合规矩：解析不出主机名，走公网入口
])
def test_从Host头取主机名(web, header, name):
    assert web._host_name(header) == name


# ============================================================ §2 判据

@pytest.mark.parametrize("host,headers,want", [
    # 约定那张表的七行
    ("127.0.0.1:8765", {}, DEFAULT_LOCAL),
    ("localhost:8765", {}, DEFAULT_LOCAL),
    ("[::1]:8765", {}, DEFAULT_LOCAL),
    ("127.0.0.1:8765", {"X-Forwarded-For": "203.0.113.9"}, DEFAULT_PUBLIC),
    ("127.0.0.1:8765", {"CF-Connecting-IP": "203.0.113.9"}, DEFAULT_PUBLIC),
    ("8765.example.com", {}, DEFAULT_PUBLIC),
    ("192.168.1.5:8765", {}, DEFAULT_PUBLIC),
    # 回环网段的两头与网段之外
    ("127.255.255.254:80", {}, DEFAULT_LOCAL),
    ("128.0.0.1:8765", {}, DEFAULT_PUBLIC),
    ("0.0.0.0:8765", {}, DEFAULT_PUBLIC),
    ("LocalHost:8765", {}, DEFAULT_LOCAL),
    # 长得像本机的别的名字
    ("localhost.evil.example", {}, DEFAULT_PUBLIC),
    ("127.0.0.1.evil.example", {}, DEFAULT_PUBLIC),
    ("[::]:8765", {}, DEFAULT_PUBLIC),
    # 解析不出主机名
    ("", {}, DEFAULT_PUBLIC), (":8765", {}, DEFAULT_PUBLIC),
])
def test_判据_主机名与转发头(web, host, headers, want):
    assert _target(web, host=host, headers=headers) == want


def test_判据_连Host头都没有_用公网入口(web):
    """读的是原始的 Host 头。request.host 在没有这个头时会回落到服务器自己绑的地址，
    测试环境里那正好是 localhost，用它就会判错。"""
    assert _target(web, drop_host=True) == DEFAULT_PUBLIC


@pytest.mark.parametrize("name", FORWARD_HEADERS)
def test_判据_五个转发头各自都算_值为空的不算(web, name):
    assert _target(web, headers={name: "203.0.113.9"}) == DEFAULT_PUBLIC
    assert _target(web, headers={name.lower(): "x"}) == DEFAULT_PUBLIC, "头的名字不分大小写"
    assert _target(web, headers={name: ""}) == DEFAULT_LOCAL
    assert _target(web, headers={name: "   "}) == DEFAULT_LOCAL


@pytest.mark.parametrize("name", ["X-Real-IP", "Via", "Referer", "Origin"])
def test_判据_五个头之外的头不参与(web, name):
    assert _target(web, headers={name: "https://8765.example.com"}) == DEFAULT_LOCAL


def test_判据_选的是哪个配置项就读哪个(web, monkeypatch):
    monkeypatch.setenv(LOCAL, "http://127.0.0.1:9999/p")
    monkeypatch.setenv(PUBLIC, "https://legacy.example.test/prov")
    assert _target(web, host=LOCAL_HOST) == "http://127.0.0.1:9999/p"
    assert _target(web, host=PUBLIC_HOST) == "https://legacy.example.test/prov"
    assert _target(web, host=LOCAL_HOST, headers={"Forwarded": "for=203.0.113.9"}) == \
        "https://legacy.example.test/prov"


def test_判据_开着信转发头的开关时_照样看得到那些头(web):
    """走隧道的机器上会设 HUB_TRUST_PROXY=1，管理台外面套一层 ProxyFix（只认一跳的协议）。
    它读了转发头之后不把头拿走，判据照样认得出这不是从本机来的。参数与 web.py 里那一行相同。"""
    from werkzeug.middleware.proxy_fix import ProxyFix
    from werkzeug.test import Client
    client = Client(ProxyFix(web.app.wsgi_app, x_proto=1, x_for=0, x_host=0, x_port=0, x_prefix=0))

    def loc(headers):
        r = client.get("/provider", headers=headers, environ_overrides={"HTTP_HOST": LOCAL_HOST})
        return r.status_code, r.headers.get("Location")

    assert loc({}) == (302, DEFAULT_LOCAL)
    assert loc({"X-Forwarded-Proto": "https"}) == (302, DEFAULT_PUBLIC)
    assert loc({"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https"}) == (302, DEFAULT_PUBLIC)


# ============================================================ §4 GET /provider

@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("host,want", [(LOCAL_HOST, DEFAULT_LOCAL), (PUBLIC_HOST, DEFAULT_PUBLIC)])
def test_302到选出的那个地址_带no_store(web, method, host, want):
    r = _go(web, host=host, method=method)
    assert (r.status_code, r.headers.getlist("Location")) == (302, [want])
    assert r.headers.get("Cache-Control") == "no-store"


def test_配置值逐字进Location_查询与片段都不变形(web, monkeypatch):
    monkeypatch.setenv(PUBLIC, "https://legacy.example.test/a/b?c=1&d=2#e")
    monkeypatch.setenv(LOCAL, "http://[::1]:8767/provider")
    assert _go(web, host=PUBLIC_HOST).headers["Location"] == "https://legacy.example.test/a/b?c=1&d=2#e"
    assert _go(web, host=LOCAL_HOST).headers["Location"] == "http://[::1]:8767/provider"


@pytest.mark.parametrize("path", ["/provider?next=https://evil.example&x=1",
                                  "/provider?HUB_LEGACY_PROVIDER_URL_LOCAL=https://evil.example",
                                  "/provider?url=//evil.example/x"])
def test_查询串不参与_也不带过去(web, path):
    assert _go(web, path=path).headers["Location"] == DEFAULT_LOCAL


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_别的方法405_不给目的地(web, method):
    r = _go(web, method=method)
    assert (r.status_code, r.headers.get("Location")) == (405, None)


@pytest.mark.parametrize("path", ["/provider/", "/provider/x"])
def test_旧地址后面再接东西仍是404(web, path):
    r = _go(web, path=path)
    assert (r.status_code, r.headers.get("Location")) == (404, None)


def test_响应不是旧页面_也不带旧页面那三个头(web):
    r = _go(web)
    body = r.get_data(as_text=True)
    assert len(body) < 1024 and "<script" not in body and "/api/providers" not in body
    for name in ("X-Proxy-Generation", "X-Frame-Options", "Content-Security-Policy"):
        assert r.headers.get(name) is None, name


def test_配错的值里夹着换行_不会在响应里多出一个头(web, monkeypatch):
    monkeypatch.setenv(LOCAL, "https://a.example/x\r\nX-Injected: 1")
    r = _go(web)
    assert (r.headers["Location"], r.headers.get("X-Injected")) == (DEFAULT_LOCAL, None)


# ============================================================ §7 全站 JSON 键的顺序

def test_应用上关着按键名排序_而且旧供应商蓝图不在(web):
    """这个设置原先是旧供应商蓝图注册时顺带设的。蓝图已经摘掉，设置仍在，说明它不再靠那个蓝图。"""
    assert "provider" not in web.app.blueprints
    assert web.app.json.sort_keys is False


def test_响应照给出的顺序输出键_嵌套的也一样(web):
    with web.app.test_request_context("/"):
        from flask import jsonify
        text = jsonify({"zeta": 1, "alpha": {"yy": 1, "bb": 2}, "mid": [{"q": 1, "a": 2}]}).get_data(as_text=True)
    order = [text.index('"%s"' % k) for k in ("zeta", "alpha", "yy", "bb", "mid", "q", "a")]
    assert order == sorted(order), "键被重排了：%s" % text
