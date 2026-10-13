# -*- coding: utf-8 -*-
"""§7 全站 JSON 响应里键的顺序：各接口照自己给出的顺序输出，不按键名重排。

这一条以前是旧供应商那个蓝图注册时顺带给整个应用设的，下线它最容易把这条带坏，坏了
没有任何页面会报错，只是全站接口的字段悄悄换了位置。

断言办法照约定给的：看响应原文里两个键谁先出现。每条挑的都是「照接口自己的顺序」与
「按键名排序」会得出相反先后的一对键，所以应用一旦改成按键名重排，这里每条都会红。

除了 /hub/api/bots 那一对是约定点名的，其余键对是在下线之前的代码上量出来的现状
（约定的说法是「下线之后必须原样保持」）。
"""
from . import le_util as u


def raw(client, path, want=200):
    r = u.go(client, path=path)
    assert r.status_code == want, "%s 期望 %s，实得 %s" % (path, want, r.status_code)
    return r.get_data(as_text=True)


def assert_before(text, first, second):
    """响应原文里，键 first 第一次出现的位置在键 second 之前。"""
    i, j = text.find('"%s"' % first), text.find('"%s"' % second)
    assert i != -1 and j != -1, "响应里缺键：%s=%d %s=%d，原文 %s" % (first, i, second, j, text[:300])
    assert i < j, "键被重排了：%r 应在 %r 之前，原文 %s" % (first, second, text[:300])


# ============================================================ 新系统各页

def test_bot状态接口_sources在overrides之前(hub):
    """约定点名的那一条。"""
    assert_before(raw(hub, "/hub/api/bots"), "sources", "overrides")


def test_供应商与模型接口_providers在custom_providers之前(hub):
    assert_before(raw(hub, "/hub/api/dsh-model"), "providers", "custom_providers")


# ============================================================ 模型来源

def test_模型来源接口_cliproxy在active之前(hub):
    assert_before(raw(hub, "/hub/api/provider"), "cliproxy", "active")


def test_模型来源接口_嵌套对象里running在port之前(hub):
    assert_before(raw(hub, "/hub/api/provider"), "running", "port")


def test_错误体_error在detail之前(hub):
    """出错时的响应体也一样：缺 state 查授权状态，回 400 的那个体。"""
    assert_before(raw(hub, "/hub/api/oauth/status", want=400), "error", "detail")


# ============================================================ 画风

def test_画风接口_styles在models之前(hub, styles_env):
    assert_before(raw(hub, "/api/styles"), "styles", "models")


# ============================================================ 朋友圈

def test_朋友圈资料接口_signature在display_name之前(hub):
    assert_before(raw(hub, "/api/profile/%s" % u.FAKE_BOT), "signature", "display_name")


def test_发朋友圈的响应_visibility在text之前(hub):
    r = hub.post("/api/moment", json={"text": "量字段顺序"})
    assert r.status_code == 200, "发朋友圈期望 200，实得 %s" % r.status_code
    assert_before(r.get_data(as_text=True), "visibility", "text")


def test_朋友圈列表接口_id在bot_id之前(hub):
    posted = hub.post("/api/moment", json={"text": "量字段顺序"})
    assert posted.status_code == 200, "前提：先发出一条朋友圈，实得 %s" % posted.status_code
    assert_before(raw(hub, "/api/moments"), "id", "bot_id")
