# -*- coding: utf-8 -*-
"""适配服务验收：鉴权，以及过了鉴权之后的未知路径、方法不对。

依据：INTERFACE-适配约定-v0.3.md 第 3、4 节，INTERFACE-适配服务.md 第 2、3 节。不读实现。
"""
import pytest

from . import adapter_util as au
from .adapter_util import adapter, console_guard  # noqa: F401  夹具

#: 各种"没过鉴权"的写法：(Authorization 头的值，None 表示不带；拼在路径后面的查询串)
BAD_AUTH = {
    "没带请求头": (None, ""),
    "请求头是空的": ("", ""),
    "写法是Basic": ("Basic dXNlcjpwYXNz", ""),
    "写法是Token": ("Token " + au.ADAPTER_TOKEN, ""),
    "只有令牌没有Bearer": (au.ADAPTER_TOKEN, ""),
    "只有Bearer没有令牌": ("Bearer", ""),
    "Bearer后面是空的": ("Bearer ", ""),
    "令牌不对": ("Bearer " + au.WRONG_TOKEN, ""),
    "对的令牌多一个字符": ("Bearer " + au.ADAPTER_TOKEN + "x", ""),
    "对的令牌少一个字符": ("Bearer " + au.ADAPTER_TOKEN[:-1], ""),
    "对的令牌大小写变了": ("Bearer " + au.ADAPTER_TOKEN.lower(), ""),
    "令牌放在查询参数里": (None, "?token=" + au.ADAPTER_TOKEN),
}


@pytest.mark.parametrize("auth,suffix", list(BAD_AUTH.values()), ids=list(BAD_AUTH))
def test_鉴权_没带或写法不对或令牌不对_五个接口都回同一个401(adapter, auth, suffix):
    for method, path, body in au.FIVE:
        au.assert_unauthorized(adapter.call(method, path + suffix, body, auth=auth))


TOKEN_FILE_CHANGES = {
    "被删掉": lambda p: p.unlink(),
    "被清空": lambda p: p.write_text("", encoding="utf-8"),
    "只剩空白和换行": lambda p: p.write_text(" \n\t\n", encoding="utf-8"),
    "换成了别的令牌": lambda p: p.write_text(au.OTHER_TOKEN, encoding="utf-8"),
}


@pytest.mark.parametrize("change", list(TOKEN_FILE_CHANGES.values()), ids=list(TOKEN_FILE_CHANGES))
def test_鉴权_运行中令牌文件变了_原来的令牌和空令牌都不认(adapter, change):
    assert adapter.call("GET", au.META).status_code == 200, "前提：文件没动之前，带对的令牌应当放行"
    change(adapter.token_file)
    for auth in (au.GOOD, "Bearer ", "Bearer", None):
        au.assert_unauthorized(adapter.call("GET", au.META, auth=auth))


def test_鉴权_令牌文件首尾有空白和换行_去掉后比对放行(adapter):
    adapter.token_file.write_text("\n  " + au.ADAPTER_TOKEN + " \t\n\n", encoding="utf-8")
    adapter.ok(adapter.call("GET", au.META))


#: 带了令牌本该回别的错的请求：(方法, 路径, JSON 请求体, 原样请求体)
WOULD_FAIL_OTHERWISE = {
    "bot_id是两个点_本该422": ("POST", au.restart_url(".."), {"force": True}, None),
    "bot_id带编码的斜杠_本该422": ("POST", au.provider_url("a%2Fb"), {"effort": "high"}, None),
    "bot_id超长_本该422": ("POST", au.restart_url("a" * 65), {"force": True}, None),
    "未知路径_本该404": ("GET", au.PREFIX + "/nope", au.NO_BODY, None),
    "前缀之外的路径_本该404": ("GET", "/nope", au.NO_BODY, None),
    "方法不对_本该405": ("POST", au.META, {}, None),
    "请求体是坏JSON_本该422": ("POST", au.provider_url("botalpha"), au.NO_BODY, "{not json"),
    "没有这只bot_本该404": ("POST", au.restart_url("ghost"), {"force": True}, None),
    "不支持的操作_本该501": ("POST", au.provider_url("botalpha"), {"reset": True}, None),
}


@pytest.mark.parametrize("method,path,body,raw", list(WOULD_FAIL_OTHERWISE.values()),
                         ids=list(WOULD_FAIL_OTHERWISE))
def test_鉴权_先于一切_本该回别的错的请求没带令牌一律401(adapter, method, path, body, raw):
    au.assert_unauthorized(adapter.call(method, path, body, raw=raw, auth=None))


@pytest.mark.parametrize("auth", [None, "Bearer " + au.WRONG_TOKEN], ids=["没带令牌", "令牌不对"])
def test_鉴权_没过时写操作什么都不做_网关零请求_启动项零调用(adapter, auth):
    gw = adapter.gateway()
    bot = adapter.bot("botalpha", gw_port=gw.port)
    adapter.heartbeat.tick("botalpha", pid=4242)
    before = bot.yml.read_bytes()
    for path, body in ((au.provider_url("botalpha"), {"effort": "high"}),
                       (au.restart_url("botalpha"), {"force": True})):
        au.assert_unauthorized(adapter.call("POST", path, body, auth=auth))
    assert gw.requests == [], "没过鉴权却去找了这只 bot 的网关：%r" % gw.requests
    assert adapter.launchctl.calls() == [], \
        "没过鉴权却调了启动项管理程序：%r" % adapter.launchctl.calls()
    assert bot.yml.read_bytes() == before, "没过鉴权却改了 bot 的配置文件"


@pytest.mark.parametrize("path", [
    au.PREFIX + "/nope", "/adapter/v2/meta", "/", au.PREFIX, "/meta",
    au.META + "/extra", au.BOTS + "/botalpha"])
def test_路由_带了令牌的未知路径_404_not_found(adapter, path):
    adapter.expect_error(adapter.call("GET", path), 404, "not_found")


WRONG_METHOD = [
    ("POST", au.META), ("POST", au.BOTS), ("POST", au.PROVIDERS),
    ("DELETE", au.META), ("PUT", au.BOTS),
    ("GET", au.provider_url("botalpha")), ("GET", au.restart_url("botalpha")),
    ("PUT", au.provider_url("botalpha")), ("DELETE", au.restart_url("botalpha")),
]


@pytest.mark.parametrize("method,path", WRONG_METHOD,
                         ids=["%s_%s" % (m, p.replace(au.PREFIX, "")) for m, p in WRONG_METHOD])
def test_路由_带了令牌_路径对但方法不对_405_method_not_allowed(adapter, method, path):
    adapter.expect_error(adapter.call(method, path), 405, "method_not_allowed")
