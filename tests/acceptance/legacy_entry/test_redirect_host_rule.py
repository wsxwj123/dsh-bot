# -*- coding: utf-8 -*-
"""§2 判据：从哪儿打开，就跳哪一个入口。

本机入口要两条同时成立：Host 里的主机名是 localhost 或回环地址；请求里没有那五个转发头。
其余一律公网入口。每条用例只换一个条件。
"""
import pytest

from . import le_util as u


# ============================================================ 第 1 条：Host 是本机的各种写法

@pytest.mark.parametrize("host", [
    "127.0.0.1:8765",                  # 回环地址带端口
    "127.0.0.1",                       # 回环地址不带端口
    "localhost:8765",                  # localhost带端口
    "localhost",                       # localhost不带端口
    "LOCALHOST:8765",                  # localhost全大写
    "LocalHost:8765",                  # localhost大小写混写
    "[::1]:8765",                      # IPv6回环带方括号与端口
    "[::1]",                           # IPv6回环不带端口
    "127.0.0.2:8765",                  # 回环网段里的别的地址
    "127.8.9.10:8765",                 # 回环网段中间的地址
    "127.255.255.254:8765",            # 回环网段末尾的地址
    "127.0.0.1:80",                    # 换一个端口
])
def test_主机名是本机_没有转发头_用本机入口(hub, host):
    assert u.location(u.go(hub, host=host)) == u.DEFAULT_LOCAL


# ============================================================ 第 1 条不成立：主机名不是本机

@pytest.mark.parametrize("host", [
    "8765.example.com",                # 隧道域名
    "8765.example.com:443",            # 域名带端口
    "192.168.1.5:8765",                # 局域网地址
    "10.0.0.8:8765",                   # 内网地址
    "0.0.0.0:8765",                    # 全零地址不是回环
    "128.0.0.1:8765",                  # 回环网段上边界之外
    "126.255.255.255:8765",            # 回环网段下边界之外
    "203.0.113.7",                     # 公网地址
    "localhost.evil.example",          # 以localhost开头的别的域名
    "evil-localhost:8765",             # 以localhost结尾的别的名字
    "127.0.0.1.evil.example",          # 以回环地址开头的域名
    "[::2]:8765",                      # 不是回环的IPv6地址
    "[::]:8765",                       # IPv6全零地址
    "[fe80::1]:8765",                  # IPv6链路本地地址
    "[2001:db8::1]:8765",              # IPv6公网地址
])
def test_主机名不是本机_没有转发头_用公网入口(hub, host):
    assert u.location(u.go(hub, host=host)) == u.DEFAULT_PUBLIC


# ============================================================ 没有 Host、解析不出主机名

def test_请求里没有Host头_用公网入口(app):
    """约定明写：没有 Host 一律公网入口。服务器自己绑在哪个地址上不算数。"""
    status, headers, _ = u.wsgi_get(app, drop_host=True)
    assert (status, headers.get("Location")) == (302, u.DEFAULT_PUBLIC)


def test_Host头是空的_用公网入口(app):
    status, headers, _ = u.wsgi_get(app, HTTP_HOST="")
    assert (status, headers.get("Location")) == (302, u.DEFAULT_PUBLIC)


def test_Host头只有端口没有主机名_用公网入口(app):
    status, headers, _ = u.wsgi_get(app, HTTP_HOST=":8765")
    assert (status, headers.get("Location")) == (302, u.DEFAULT_PUBLIC)


# ============================================================ 第 2 条：五个转发头各一条

@pytest.mark.parametrize("name,value", u.FORWARD_HEADERS, ids=[n for n, _ in u.FORWARD_HEADERS])
def test_主机名是本机_但带了转发头_用公网入口(hub, name, value):
    """隧道把请求转到本机时 Host 可能已经是回环地址，靠这些头认出它不是从本机来的。"""
    r = u.go(hub, host=u.LOCAL_HOST, headers={name: value})
    assert u.location(r) == u.DEFAULT_PUBLIC


@pytest.mark.parametrize("host", ["localhost:8765", "[::1]:8765"])
@pytest.mark.parametrize("name,value", u.FORWARD_HEADERS, ids=[n for n, _ in u.FORWARD_HEADERS])
def test_本机的另外两种写法_带了转发头_也用公网入口(hub, host, name, value):
    r = u.go(hub, host=host, headers={name: value})
    assert u.location(r) == u.DEFAULT_PUBLIC


@pytest.mark.parametrize("name,_value", u.FORWARD_HEADERS, ids=[n for n, _ in u.FORWARD_HEADERS])
def test_转发头的值是空的_不算有_仍用本机入口(hub, name, _value):
    r = u.go(hub, host=u.LOCAL_HOST, headers={name: ""})
    assert u.location(r) == u.DEFAULT_LOCAL


@pytest.mark.parametrize("name,value", [
    pytest.param("x-forwarded-for", "203.0.113.9", id="xff-lowercase"),
    pytest.param("X-FORWARDED-PROTO", "https", id="xfp-uppercase"),
    pytest.param("cf-connecting-ip", "203.0.113.9", id="cf-lowercase"),
])
def test_转发头的名字不分大小写(hub, name, value):
    r = u.go(hub, host=u.LOCAL_HOST, headers={name: value})
    assert u.location(r) == u.DEFAULT_PUBLIC


@pytest.mark.parametrize("name,value", [
    pytest.param("X-Forwarded-For", "127.0.0.1", id="xff-says-loopback"),
    pytest.param("CF-Connecting-IP", "::1", id="cf-says-loopback"),
    pytest.param("X-Forwarded-Host", "localhost:8765", id="xfh-says-localhost"),
])
def test_转发头里写的是本机地址_有这个头就算_用公网入口(hub, name, value):
    """判据只看有没有这个头，不看它的值。"""
    r = u.go(hub, host=u.LOCAL_HOST, headers={name: value})
    assert u.location(r) == u.DEFAULT_PUBLIC


def test_五个转发头一起带_用公网入口(hub):
    r = u.go(hub, host=u.LOCAL_HOST, headers=dict(u.FORWARD_HEADERS))
    assert u.location(r) == u.DEFAULT_PUBLIC


# ============================================================ 两条都不成立

@pytest.mark.parametrize("name,value", u.FORWARD_HEADERS, ids=[n for n, _ in u.FORWARD_HEADERS])
def test_主机名不是本机_又带了转发头_用公网入口(hub, name, value):
    r = u.go(hub, host=u.PUBLIC_HOST, headers={name: value})
    assert u.location(r) == u.DEFAULT_PUBLIC


def test_主机名不是本机_转发头的值是空的_仍用公网入口(hub):
    """空的转发头不算有，但主机名这一条本来就不成立。"""
    r = u.go(hub, host=u.PUBLIC_HOST, headers={"X-Forwarded-For": ""})
    assert u.location(r) == u.DEFAULT_PUBLIC


# ============================================================ 判据只认那五个头

@pytest.mark.parametrize("name,value", [
    pytest.param("X-Real-IP", "203.0.113.9", id="X-Real-IP"),
    pytest.param("Via", "1.1 proxy.example", id="Via"),
    pytest.param("Referer", "https://8765.example.com/hub", id="Referer"),
    pytest.param("Origin", "https://8765.example.com", id="Origin"),
])
def test_五个头之外的头不参与判据_仍用本机入口(hub, name, value):
    """约定写的是「当且仅当」，第 2 条只点了五个头的名。"""
    r = u.go(hub, host=u.LOCAL_HOST, headers={name: value})
    assert u.location(r) == u.DEFAULT_LOCAL
