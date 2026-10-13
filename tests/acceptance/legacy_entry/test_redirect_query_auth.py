# -*- coding: utf-8 -*-
"""§0 第 2 条与 §4：目的地只来自配置；鉴权照旧。

- 查询串、请求体不参与拼地址；请求头只用来在两个配置值里挑一个，换不出第三个地址。
- 这个地址在全站登录门之内，不在管理锁之内。
"""
import pytest

from . import le_util as u

EVIL = "https://evil.example"
PW = "hub-access-pw-12345"
ADMIN_PW = "hub-admin-pw-67890"
HTML = {"Accept": "text/html,application/xhtml+xml"}
FETCH = {"Accept": "*/*"}


# ============================================================ 查询参数

def test_带一个像地址的参数_目的地与不带时逐字相同(hub):
    """约定原话的那一条：GET /provider?next=https://evil.example&x=1。"""
    plain = u.location(u.go(hub))
    with_query = u.location(u.go(hub, path="/provider?next=%s&x=1" % EVIL))
    assert (plain, with_query) == (u.DEFAULT_LOCAL, u.DEFAULT_LOCAL)


def test_带一个像地址的参数_那个地址没被采用(hub):
    loc = u.location(u.go(hub, path="/provider?next=%s&x=1" % EVIL))
    assert "evil.example" not in loc, "查询参数里的地址进了目的地：%s" % loc


def test_查询串不带到目的地去(hub):
    loc = u.location(u.go(hub, path="/provider?next=%s&x=1" % EVIL))
    assert "?" not in loc and "x=1" not in loc, "查询串被带过去了：%s" % loc


@pytest.mark.parametrize("name", ["next", "url", "redirect", "redirect_uri", "to", "target",
                                  "return", "returnTo", "dest", "continue", "goto", "u"])
def test_各种像跳转目标的参数名_都不改变目的地(hub, name):
    loc = u.location(u.go(hub, path="/provider?%s=%s/landing" % (name, EVIL)))
    assert loc == u.DEFAULT_LOCAL


@pytest.mark.parametrize("value", [
    "//evil.example/x",             # 协议相对地址
    "/hub",                         # 站内路径
    "http://127.0.0.1:9999/p",      # 一个本来合法的配置值
    "javascript:alert(1)",          # javascript 伪协议
])
def test_参数值换各种写法_都不改变目的地(hub, value):
    loc = u.location(u.go(hub, query_string={"next": value}))
    assert loc == u.DEFAULT_LOCAL


@pytest.mark.parametrize("name", [u.LOCAL_ENV, u.PUBLIC_ENV])
def test_参数名冒充配置项的名字_也不改变目的地(hub, name):
    loc = u.location(u.go(hub, path="/provider?%s=%s" % (name, EVIL)))
    assert loc == u.DEFAULT_LOCAL


def test_同名参数重复出现_不改变目的地(hub):
    loc = u.location(u.go(hub, path="/provider?next=%s&next=%s/2" % (EVIL, EVIL)))
    assert loc == u.DEFAULT_LOCAL


def test_很长的查询串_照样302到默认值(hub):
    loc = u.location(u.go(hub, path="/provider?next=%s/%s" % (EVIL, "a" * 6000)))
    assert loc == u.DEFAULT_LOCAL


def test_公网打开带像地址的参数_仍是公网入口的默认值(hub):
    loc = u.location(u.go(hub, path="/provider?next=%s&x=1" % EVIL, host=u.PUBLIC_HOST))
    assert loc == u.DEFAULT_PUBLIC


def test_HEAD带像地址的参数_不改变目的地(hub):
    loc = u.location(u.go(hub, path="/provider?next=%s" % EVIL, method="HEAD"))
    assert loc == u.DEFAULT_LOCAL


def test_配了自定义入口_带像地址的参数_仍是配置的那个值(make_client):
    c = make_client(**{u.LOCAL_ENV: "http://127.0.0.1:9999/p"})
    loc = u.location(u.go(c, path="/provider?next=%s" % EVIL))
    assert loc == "http://127.0.0.1:9999/p"


# ============================================================ 请求体

def test_GET带表单体_里面有像地址的字段_不改变目的地(hub):
    r = u.go(hub, data={"next": EVIL, "url": EVIL},
             content_type="application/x-www-form-urlencoded")
    assert u.location(r) == u.DEFAULT_LOCAL


def test_GET带JSON体_里面有像地址的字段_不改变目的地(hub):
    r = u.go(hub, json={"next": EVIL, "url": EVIL, "location": EVIL})
    assert u.location(r) == u.DEFAULT_LOCAL


# ============================================================ 请求头只能二选一

@pytest.mark.parametrize("headers", [
    pytest.param({"Referer": EVIL + "/from"}, id="Referer"),
    pytest.param({"X-Original-URL": EVIL + "/x"}, id="X-Original-URL"),
    pytest.param({"X-Rewrite-URL": EVIL + "/x"}, id="X-Rewrite-URL"),
    pytest.param({"Location": EVIL + "/x"}, id="Location-in-request"),
    pytest.param({"X-Forwarded-Host": "evil.example"}, id="X-Forwarded-Host"),
    pytest.param({"Forwarded": "host=evil.example;proto=https"}, id="Forwarded"),
])
def test_请求头里写别的地址_目的地仍只在两个配置值里(hub, headers):
    loc = u.location(u.go(hub, host=u.LOCAL_HOST, headers=headers))
    assert loc in (u.DEFAULT_LOCAL, u.DEFAULT_PUBLIC), "请求头换出了第三个地址：%s" % loc


def test_Host写成别人的域名_目的地是公网入口_不是那个域名(hub):
    loc = u.location(u.go(hub, host="evil.example"))
    assert loc == u.DEFAULT_PUBLIC


def test_转发头里的主机名是别人的域名_目的地是公网入口_不是那个域名(hub):
    loc = u.location(u.go(hub, host=u.LOCAL_HOST, headers={"X-Forwarded-Host": "evil.example"}))
    assert loc == u.DEFAULT_PUBLIC


# ============================================================ 登录门（全站口令）

def test_设了访问口令没登录_浏览器打开_先被带去登录页(make_client):
    c = make_client(HUB_ACCESS_PASSWORD=PW)
    r = u.go(c, headers=HTML)
    assert (r.status_code, (r.headers.get("Location") or "").endswith("/login")) == (302, True), \
        "没登录应先去 /login，实得 %s %s" % (r.status_code, r.headers.get("Location"))


def test_设了访问口令没登录_响应里不透露旧系统那边的地址(make_client):
    c = make_client(HUB_ACCESS_PASSWORD=PW)
    r = u.go(c, headers=HTML)
    assert "8767" not in (r.headers.get("Location") or "") + r.get_data(as_text=True)


def test_设了访问口令没登录_带像地址的参数_仍是去登录页(make_client):
    c = make_client(HUB_ACCESS_PASSWORD=PW)
    r = u.go(c, path="/provider?next=%s" % EVIL, headers=HTML)
    assert "evil.example" not in (r.headers.get("Location") or ""), r.headers.get("Location")


def test_设了访问口令没登录_不是浏览器导航_回401(make_client):
    """既有的门：不是页面导航的请求一律 401，不给跳转。"""
    c = make_client(HUB_ACCESS_PASSWORD=PW)
    r = u.go(c, headers=FETCH)
    assert (r.status_code, r.get_json(silent=True)) == (401, {"error": "unauthorized"})


def test_设了访问口令没登录_POST_门先挡下_是401不是405(make_client):
    c = make_client(HUB_ACCESS_PASSWORD=PW)
    r = u.go(c, method="POST", headers=FETCH)
    assert r.status_code == 401, "没登录时轮不到 405，实得 %s" % r.status_code


def test_登录之后_本机打开_才跳到旧系统那边(make_client, login):
    c = make_client(HUB_ACCESS_PASSWORD=PW)
    assert login(c, PW).status_code == 303, "前提：登录成功"
    assert u.location(u.go(c, headers=HTML)) == u.DEFAULT_LOCAL


def test_登录之后_公网打开_跳到公网入口(make_client, login):
    c = make_client(HUB_ACCESS_PASSWORD=PW)
    assert login(c, PW, host=u.PUBLIC_HOST).status_code == 303, "前提：登录成功"
    assert u.location(u.go(c, host=u.PUBLIC_HOST, headers=HTML)) == u.DEFAULT_PUBLIC


# ============================================================ 管理锁（它不管这个地址）

def test_设了管理口令_只过了访问口令_旧地址照样跳转(make_client, login):
    """管理锁只管 /hub 开头的那些；这个地址以前就不在里面，现在也不在。"""
    c = make_client(HUB_ACCESS_PASSWORD=PW, HUB_ADMIN_PASSWORD=ADMIN_PW)
    assert login(c, PW).status_code == 303, "前提：只用访问口令登录成功"
    assert u.location(u.go(c, headers=HTML)) == u.DEFAULT_LOCAL


def test_设了管理口令_两把都过了_旧地址照样跳转(make_client, login):
    c = make_client(HUB_ACCESS_PASSWORD=PW, HUB_ADMIN_PASSWORD=ADMIN_PW)
    assert login(c, PW, admin_password=ADMIN_PW).status_code == 303, "前提：两把口令登录成功"
    assert u.location(u.go(c, headers=HTML)) == u.DEFAULT_LOCAL


def test_设了管理口令_两把都没过_浏览器打开_先被带去登录页(make_client):
    c = make_client(HUB_ACCESS_PASSWORD=PW, HUB_ADMIN_PASSWORD=ADMIN_PW)
    r = u.go(c, headers=HTML)
    assert (r.status_code, (r.headers.get("Location") or "").endswith("/login")) == (302, True)
