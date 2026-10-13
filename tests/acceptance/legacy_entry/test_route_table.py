# -*- coding: utf-8 -*-
"""§6 第 1 条：路由清单与蓝图清单里不再有旧供应商那一套。

这一组不发请求，只看管理台应用注册了什么。
"""
import pytest

from . import le_util as u


@pytest.mark.parametrize("method,path", u.RETIRED_PARAMS)
def test_路由清单里没有这条旧接口(app, method, path):
    assert u.route_state(app, path) == "gone", \
        "路由清单里还有能匹配 %s 的规则（原来是 %s）" % (path, method)


def test_旧页面地址恰好一条规则(app):
    rules = u.rules_of(app, "/provider")
    assert len(rules) == 1, "/provider 应恰好一条规则，实得 %d 条" % len(rules)


def test_旧页面地址只收GET_连带HEAD与OPTIONS(app):
    methods = sorted(set().union(*[r.methods for r in u.rules_of(app, "/provider")]))
    assert methods == ["GET", "HEAD", "OPTIONS"], methods


def test_蓝图清单里没有名叫provider的蓝图(app):
    assert u.RETIRED_BLUEPRINT not in app.blueprints, \
        "蓝图清单里还有 %r：%s" % (u.RETIRED_BLUEPRINT, sorted(app.blueprints))


# ============================================================ 反向：别的没被顺手摘掉

@pytest.mark.parametrize("path", [
    "/hub/api/bots/%s/restart" % u.FAKE_BOT,     # 新系统的单只重启，名字像旧接口
    "/hub/api/bots/restart",                     # 新系统的重启全部
    "/hub/api/provider",                         # 模型来源的接口
    "/hub/provider",                             # 模型来源页
    "/api/moments",                              # 朋友圈
    "/api/styles",                               # 画风
    "/api/profile/%s" % u.FAKE_BOT,              # 朋友圈资料
    "/hub/api/dsh-model",                        # 供应商与模型
])
def test_不在下线名单里的路由仍在(app, path):
    assert u.route_state(app, path) == "alive", "%s 被顺手摘掉了" % path


@pytest.mark.parametrize("name", ["hub", "styles"])
def test_别的蓝图仍在(app, name):
    assert name in app.blueprints, "蓝图 %r 不见了：%s" % (name, sorted(app.blueprints))
