# -*- coding: utf-8 -*-
"""§4 GET /provider：旧页面的地址不再出页面，只把人带到旧系统那边。

量的是下线之后的行为，实现落地前这里大半是红的。
"""
import pytest

from . import le_util as u


# ============================================================ 状态码与目的地

def test_本机打开旧地址_回302(hub):
    r = u.go(hub, host=u.LOCAL_HOST)
    assert r.status_code == 302, "从本机地址打开 /provider 应 302，实得 %s" % r.status_code


def test_本机打开旧地址_目的地逐字是本机入口的默认值(hub):
    r = u.go(hub, host=u.LOCAL_HOST)
    assert r.headers.get("Location") == u.DEFAULT_LOCAL


def test_公网打开旧地址_回302(hub):
    r = u.go(hub, host=u.PUBLIC_HOST)
    assert r.status_code == 302, "从公网地址打开 /provider 应 302，实得 %s" % r.status_code


def test_公网打开旧地址_目的地逐字是公网入口的默认值(hub):
    r = u.go(hub, host=u.PUBLIC_HOST)
    assert r.headers.get("Location") == u.DEFAULT_PUBLIC


@pytest.mark.parametrize("host", [u.LOCAL_HOST, u.PUBLIC_HOST], ids=["local", "public"])
def test_跳转不许被浏览器记住_带no_store(hub, host):
    """目的地随打开方式与配置而变，浏览器记死了就改不回来。"""
    r = u.go(hub, host=host)
    assert "no-store" in u.cache_directives(r), \
        "Cache-Control 里没有 no-store，实得 %r" % r.headers.get("Cache-Control")


@pytest.mark.parametrize("host", [u.LOCAL_HOST, u.PUBLIC_HOST], ids=["local", "public"])
def test_跳转只给一个目的地(hub, host):
    r = u.go(hub, host=host)
    assert len(r.headers.getlist("Location")) == 1, r.headers.getlist("Location")


# ============================================================ HEAD

def test_HEAD本机打开_回302(hub):
    r = u.go(hub, method="HEAD", host=u.LOCAL_HOST)
    assert r.status_code == 302, "HEAD /provider 应 302，实得 %s" % r.status_code


def test_HEAD本机打开_目的地与GET相同(hub):
    r = u.go(hub, method="HEAD", host=u.LOCAL_HOST)
    assert r.headers.get("Location") == u.DEFAULT_LOCAL


def test_HEAD公网打开_目的地是公网入口(hub):
    r = u.go(hub, method="HEAD", host=u.PUBLIC_HOST)
    assert r.headers.get("Location") == u.DEFAULT_PUBLIC


def test_HEAD_同样带no_store(hub):
    r = u.go(hub, method="HEAD", host=u.LOCAL_HOST)
    assert "no-store" in u.cache_directives(r), r.headers.get("Cache-Control")


# ============================================================ 其它方法

@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_其它方法_回405(hub, method):
    r = u.go(hub, method=method, host=u.LOCAL_HOST)
    assert r.status_code == 405, "%s /provider 应 405，实得 %s" % (method, r.status_code)


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_其它方法_不给跳转目的地(hub, method):
    """405 就是不受理：不能一边回 405 一边把人带走。"""
    r = u.go(hub, method=method, host=u.LOCAL_HOST)
    assert r.headers.get("Location") is None, r.headers.get("Location")


# ============================================================ 别的路径

@pytest.mark.parametrize("path", ["/provider/", "/provider/x", "/provider/index.html"])
def test_旧地址后面再接东西_仍是404(hub, path):
    r = u.go(hub, path=path, host=u.LOCAL_HOST)
    assert r.status_code == 404, "%s 应 404，实得 %s" % (path, r.status_code)


@pytest.mark.parametrize("path", ["/provider/", "/provider/x"])
def test_旧地址后面再接东西_不给跳转目的地(hub, path):
    r = u.go(hub, path=path, host=u.LOCAL_HOST)
    assert r.headers.get("Location") is None, r.headers.get("Location")


# ============================================================ 不再是本仓的页面

@pytest.mark.parametrize("marker", ["<script", "/api/providers", "/api/proxy", "/api/aux-llm"])
def test_响应体里没有旧页面的东西(hub, marker):
    """旧页面是那十五条接口的前端，页面源码里有脚本、有那些接口的地址。跳转的响应体里都不该有。"""
    body = u.go(hub, host=u.LOCAL_HOST).get_data(as_text=True)
    assert marker not in body, "/provider 的响应体里还有 %r，旧页面还在被端出来" % marker


def test_响应体只是一小段跳转提示_不是几万字节的页面(hub):
    body = u.go(hub, host=u.LOCAL_HOST).get_data()
    assert len(body) < 1024, "/provider 的响应体有 %d 字节，像是还在出页面" % len(body)


@pytest.mark.parametrize("name", u.LEGACY_ONLY_HEADERS)
def test_跳转响应不再带旧页面那三个头(hub, name):
    """§5：这三个头以前只加在旧页面与旧接口上，这组路径下线后不再带。"""
    r = u.go(hub, host=u.LOCAL_HOST)
    assert r.headers.get(name) is None, "%s 还在：%r" % (name, r.headers.get(name))
