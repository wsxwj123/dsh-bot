# -*- coding: utf-8 -*-
"""§1 配置项：两个入口地址各自能配，配错了回落默认值，绝不报错。

读法：先看进程环境变量，再看 hub.env 文件（`HUB_ENV_FILE` 指到临时文件）；每次请求现读。
合法的四个条件（先去掉首尾空白）：以 http:// 或 https:// 开头；能解析出非空的主机名；
不含反斜杠、@、空白字符、控制字符；不超过 2048 个字符。
"""
import pytest

from . import le_util as u

CUSTOM_LOCAL = "http://127.0.0.1:9999/p"                  # 约定点名的合法值
CUSTOM_PUBLIC = "https://legacy.example.test/prov"

#: 哪个配置项对应从哪个 Host 打开
HOST_OF = {u.LOCAL_ENV: u.LOCAL_HOST, u.PUBLIC_ENV: u.PUBLIC_HOST}
BOTH = [pytest.param(u.LOCAL_ENV, id="local"), pytest.param(u.PUBLIC_ENV, id="public")]


def _loc(client, env_name):
    return u.location(u.go(client, host=HOST_OF[env_name]))


# ============================================================ 默认值

@pytest.mark.parametrize("env_name", BOTH)
def test_没设配置_用默认值(hub, env_name):
    assert _loc(hub, env_name) == u.DEFAULT_OF[env_name]


# ============================================================ 各自覆盖默认值

def test_设了本机入口_本机打开就去它(make_client):
    c = make_client(**{u.LOCAL_ENV: CUSTOM_LOCAL})
    assert _loc(c, u.LOCAL_ENV) == CUSTOM_LOCAL


def test_设了本机入口_公网打开仍去公网默认值(make_client):
    c = make_client(**{u.LOCAL_ENV: CUSTOM_LOCAL})
    assert _loc(c, u.PUBLIC_ENV) == u.DEFAULT_PUBLIC


def test_设了公网入口_公网打开就去它(make_client):
    c = make_client(**{u.PUBLIC_ENV: CUSTOM_PUBLIC})
    assert _loc(c, u.PUBLIC_ENV) == CUSTOM_PUBLIC


def test_设了公网入口_本机打开仍去本机默认值(make_client):
    c = make_client(**{u.PUBLIC_ENV: CUSTOM_PUBLIC})
    assert _loc(c, u.LOCAL_ENV) == u.DEFAULT_LOCAL


def test_两项都设_本机打开去本机那个(make_client):
    c = make_client(**{u.LOCAL_ENV: CUSTOM_LOCAL, u.PUBLIC_ENV: CUSTOM_PUBLIC})
    assert _loc(c, u.LOCAL_ENV) == CUSTOM_LOCAL


def test_两项都设_公网打开去公网那个(make_client):
    c = make_client(**{u.LOCAL_ENV: CUSTOM_LOCAL, u.PUBLIC_ENV: CUSTOM_PUBLIC})
    assert _loc(c, u.PUBLIC_ENV) == CUSTOM_PUBLIC


# ============================================================ 合法值原样用

@pytest.mark.parametrize("value", [
    pytest.param("http://127.0.0.1:9999/p", id="from-spec"),                       # 约定点名的那个
    pytest.param("https://legacy.example.test/a/b?c=1&d=2#e", id="path-query-fragment"),  # 带路径、查询与片段
    pytest.param("http://[::1]:8767/provider", id="ipv6-host"),                    # IPv6 主机
    pytest.param("https://legacy.example.test", id="scheme-and-host-only"),        # 只有协议与主机
])
@pytest.mark.parametrize("env_name", BOTH)
def test_合法的值_目的地逐字就是它(make_client, env_name, value):
    c = make_client(**{env_name: value})
    assert _loc(c, env_name) == value


@pytest.mark.parametrize("env_name", BOTH)
def test_首尾有空白的合法值_去掉空白后照用(make_client, env_name):
    c = make_client(**{env_name: "  \t" + CUSTOM_LOCAL + " \t "})
    assert _loc(c, env_name) == CUSTOM_LOCAL


@pytest.mark.parametrize("env_name", BOTH)
def test_恰好2048个字符_合法(make_client, env_name):
    value = "https://a.example/" + "x" * (2048 - len("https://a.example/"))
    assert len(value) == 2048
    c = make_client(**{env_name: value})
    assert _loc(c, env_name) == value


# ============================================================ 不合法的每一类都回落默认值

@pytest.mark.parametrize("value", u.BAD_VALUES_FROM_SPEC)
@pytest.mark.parametrize("env_name", BOTH)
def test_约定点名的六个不合法值_回落默认值(make_client, env_name, value):
    c = make_client(**{env_name: value})
    assert _loc(c, env_name) == u.DEFAULT_OF[env_name]


@pytest.mark.parametrize("value", [
    pytest.param("", id="empty"),                                                  # 空串
    pytest.param("   \t ", id="blank"),                                             # 只有空白
    pytest.param("legacy.example.test/provider", id="no-scheme"),                  # 没写协议
    pytest.param("http:/a.example/x", id="single-slash"),                          # 协议后只有一条斜杠
    pytest.param("file:///etc/hosts", id="file-scheme"),                           # file 协议
    pytest.param("data:text/html,x", id="data-scheme"),                            # data 协议
    pytest.param("http://", id="scheme-only"),                                     # 协议后什么都没有
    pytest.param("https://:8443/x", id="port-without-host"),                       # 只有端口没有主机名
    pytest.param("http://?x=1", id="query-without-host"),                          # 主机名的位置是问号
    pytest.param("https://a.example\\x", id="backslash-after-host"),               # 主机后跟反斜杠
    pytest.param("https://a.example/x\\y", id="backslash-in-path"),                # 路径里有反斜杠
    pytest.param("https://a.example/x@y", id="at-in-path"),                        # 路径里有 @
    pytest.param("https://a.example/x y", id="space-in-path"),                     # 路径里有空格
    pytest.param("https://a.example/x\ty", id="tab-in-path"),                      # 路径里有制表符
    pytest.param("https://a.example/x\nX-Injected: 1", id="newline-inside"),       # 中间有换行
    pytest.param("https://a.example/x\rX-Injected: 1", id="carriage-return-inside"),  # 中间有回车
    pytest.param("https://a.example/x\x01y", id="control-char-01"),                # 控制字符 0x01
    pytest.param("https://a.example/x\x7fy", id="control-char-7f"),                # 控制字符 0x7f
    pytest.param("https://a.example/" + "x" * (2049 - len("https://a.example/")), id="2049-chars"),  # 超长一个字符
])
@pytest.mark.parametrize("env_name", BOTH)
def test_别的不合法写法_回落默认值(make_client, env_name, value):
    c = make_client(**{env_name: value})
    assert _loc(c, env_name) == u.DEFAULT_OF[env_name]


def test_一项配错了_不连累另一项(make_client):
    c = make_client(**{u.LOCAL_ENV: "javascript:alert(1)", u.PUBLIC_ENV: CUSTOM_PUBLIC})
    assert _loc(c, u.PUBLIC_ENV) == CUSTOM_PUBLIC


def test_值里夹着换行_响应头里没有被塞进别的头(make_client):
    """配错的值不许原样进响应头：回落默认值之外，也不能多出一个头。"""
    c = make_client(**{u.LOCAL_ENV: "https://a.example/x\r\nX-Injected: 1"})
    r = u.go(c, host=u.LOCAL_HOST)
    assert r.headers.get("X-Injected") is None, dict(r.headers)


# ============================================================ hub.env 文件

@pytest.mark.parametrize("env_name,value", [
    pytest.param(u.LOCAL_ENV, CUSTOM_LOCAL, id="local"),
    pytest.param(u.PUBLIC_ENV, CUSTOM_PUBLIC, id="public"),
])
def test_环境变量没有_只写在hub_env文件里_也生效(make_client, sandbox, env_name, value):
    u.write_env_file(sandbox.env_file, **{env_name: value})
    c = make_client()
    assert _loc(c, env_name) == value


@pytest.mark.parametrize("env_name", BOTH)
def test_环境变量与hub_env文件都有_环境变量赢(make_client, sandbox, env_name):
    u.write_env_file(sandbox.env_file, **{env_name: "https://from-file.example.test/x"})
    c = make_client(**{env_name: "https://from-env.example.test/x"})
    assert _loc(c, env_name) == "https://from-env.example.test/x"


@pytest.mark.parametrize("env_name", BOTH)
def test_hub_env文件里的值不合法_回落默认值(make_client, sandbox, env_name):
    u.write_env_file(sandbox.env_file, **{env_name: "ftp://a.example/x"})
    c = make_client()
    assert _loc(c, env_name) == u.DEFAULT_OF[env_name]


def test_hub_env文件不存在_用默认值(make_client, sandbox):
    assert not sandbox.env_file.exists(), "前提：这条用例里 hub.env 不存在"
    c = make_client()
    assert _loc(c, u.LOCAL_ENV) == u.DEFAULT_LOCAL


# ============================================================ 每次请求现读

def test_请求之间改了环境变量_下一次请求就用新值(hub, monkeypatch):
    assert _loc(hub, u.LOCAL_ENV) == u.DEFAULT_LOCAL, "前提：改之前是默认值"
    monkeypatch.setenv(u.LOCAL_ENV, CUSTOM_LOCAL)
    assert _loc(hub, u.LOCAL_ENV) == CUSTOM_LOCAL


def test_请求之间删了环境变量_下一次请求回到默认值(make_client, monkeypatch):
    c = make_client(**{u.PUBLIC_ENV: CUSTOM_PUBLIC})
    assert _loc(c, u.PUBLIC_ENV) == CUSTOM_PUBLIC, "前提：删之前是配置的值"
    monkeypatch.delenv(u.PUBLIC_ENV)
    assert _loc(c, u.PUBLIC_ENV) == u.DEFAULT_PUBLIC


def test_请求之间改了hub_env文件_下一次请求就用新值(make_client, sandbox):
    u.write_env_file(sandbox.env_file, **{u.LOCAL_ENV: "https://first.example.test/x"})
    c = make_client()
    assert _loc(c, u.LOCAL_ENV) == "https://first.example.test/x", "前提：先读到文件里的第一个值"
    u.write_env_file(sandbox.env_file, **{u.LOCAL_ENV: "https://second.example.test/x"})
    assert _loc(c, u.LOCAL_ENV) == "https://second.example.test/x"
