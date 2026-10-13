# -*- coding: utf-8 -*-
"""§5 与 §6 第 3 条：十五条旧接口已下线，请求它们什么都不会发生。

安全：这十五条在下线之前是活的（杀进程重拉、调 launchctl、写旧系统的清单）。本文件每条
用例都先看路由清单，清单里还有能匹配的规则就当场判失败，**不发请求**。所以实现落地之前
这里整片是红的，红的原因都是同一句「功能还没实现」。

bot 名与供应商编号一律用假名（见 le_util 的 FAKE_BOT / FAKE_PROVIDER）。
"""
import time

import pytest

from . import le_util as u

PW = "hub-access-pw-12345"
FETCH = {"Accept": "*/*"}

ANY_METHOD = [pytest.param(m, p, id="%s_%s" % (m, p)) for p in u.RETIRED_PATHS for m in u.ALL_METHODS]


@pytest.fixture
def observed(app, sandbox):
    """(方法, 路径) -> (响应, 旁观记录)。先过路由清单这一关，再在旁观之下发一次请求。"""
    def _do(method, path):
        u.require_retired(app, path)
        client = app.test_client()
        with u.watching(sandbox) as seen:
            resp = u.call(client, method, path)
        return resp, seen
    return _do


# ============================================================ 状态码与响应体

@pytest.mark.parametrize("method,path", u.RETIRED_PARAMS)
def test_用原来的方法请求_回404(observed, method, path):
    resp, _ = observed(method, path)
    assert resp.status_code == 404, "%s %s 应 404，实得 %s" % (method, path, resp.status_code)


@pytest.mark.parametrize("method,path", u.RETIRED_PARAMS)
def test_用原来的方法请求_响应体是not_found的JSON(observed, method, path):
    resp, _ = observed(method, path)
    assert ("application/json" in (resp.headers.get("Content-Type") or ""),
            resp.get_json(silent=True)) == (True, {"error": "not_found"}), \
        "实得 %s %r" % (resp.headers.get("Content-Type"), resp.get_data(as_text=True)[:200])


@pytest.mark.parametrize("method,path", u.RETIRED_PARAMS)
def test_用原来的方法请求_响应不带旧接口那三个头(observed, method, path):
    resp, _ = observed(method, path)
    still = {h: resp.headers.get(h) for h in u.LEGACY_ONLY_HEADERS if resp.headers.get(h) is not None}
    assert still == {}, "这些头应当随旧接口一起消失：%s" % still


@pytest.mark.parametrize("method,path", ANY_METHOD)
def test_换任何方法请求_都是404(app, method, path):
    """不是「方法不对」的 405：这条路径整个不存在了。"""
    u.require_retired(app, path)
    resp = u.call(app.test_client(), method, path)
    assert resp.status_code == 404, "%s %s 应 404，实得 %s" % (method, path, resp.status_code)


# ============================================================ 请求期间什么都没发生

@pytest.mark.parametrize("method,path", u.RETIRED_PARAMS)
def test_请求期间_假launchctl一次都没被调(observed, method, path):
    _, seen = observed(method, path)
    assert seen.launchctl == [], "launchctl 被调了：%s" % seen.launchctl


@pytest.mark.parametrize("method,path", u.RETIRED_PARAMS)
def test_请求期间_没有起子进程也没有发信号(observed, method, path):
    _, seen = observed(method, path)
    assert seen.processes == [], "管理台起了子进程或发了信号：%s" % seen.processes


@pytest.mark.parametrize("method,path", u.RETIRED_PARAMS)
def test_请求期间_没有起后台线程(observed, method, path):
    _, seen = observed(method, path)
    assert seen.threads == [], "请求期间起了线程（后台任务）：%s" % seen.threads


@pytest.mark.parametrize("method,path", u.RETIRED_PARAMS)
def test_请求之后_旧系统的供应商清单逐字节没变(observed, sandbox, method, path):
    observed(method, path)
    got = [p.read_bytes() if p.exists() else None for p in sandbox.seeded_providers]
    assert got == [u.SEED_PROVIDERS_JSON] * len(sandbox.seeded_providers), \
        "预置的供应商清单被改了或被删了：%s" % [str(p) for p in sandbox.seeded_providers]


@pytest.mark.parametrize("method,path", u.RETIRED_PARAMS)
def test_请求之后_旧系统的全局配置逐字节没变(observed, sandbox, method, path):
    observed(method, path)
    got = [p.read_bytes() if p.exists() else None for p in sandbox.seeded_globals]
    assert got == [u.SEED_GLOBAL_YML] * len(sandbox.seeded_globals), \
        "预置的全局配置被改了或被删了：%s" % [str(p) for p in sandbox.seeded_globals]


@pytest.mark.parametrize("method,path", u.RETIRED_PARAMS)
def test_请求之后_旧系统那边没有多出也没有少掉文件(observed, method, path):
    """备份、临时文件、待重启标记、代理配置，一样都不该冒出来。"""
    _, seen = observed(method, path)
    assert seen.legacy_diff == [], "旧系统那边的文件变了：%s" % seen.legacy_diff


def test_十五条连着各请求一次_全程零子进程零launchctl零后台线程_旧系统文件不变(app, sandbox):
    """§6 第 3 条的原话，在同一个应用实例上连着打完十五条。

    打完多等半秒再收：「先回响应、后台接着干」的写法，子进程与线程可能晚一步才冒出来。
    """
    for _method, _spec, path, _why in u.RETIRED:
        u.require_retired(app, path)
    client = app.test_client()
    with u.watching(sandbox) as seen:
        codes = [u.call(client, method, path).status_code for method, _spec, path, _why in u.RETIRED]
        time.sleep(0.5)
    assert (codes, seen.processes, seen.launchctl, seen.threads, seen.legacy_diff) == \
        ([404] * 15, [], [], [], [])


# ============================================================ 登录门照旧在前面

@pytest.mark.parametrize("method,path", u.RETIRED_PARAMS)
def test_设了访问口令没登录_先回401_轮不到404(make_app, method, path):
    app = make_app(HUB_ACCESS_PASSWORD=PW)
    u.require_retired(app, path)
    resp = u.call(app.test_client(), method, path, headers=FETCH)
    assert (resp.status_code, resp.get_json(silent=True)) == (401, {"error": "unauthorized"})


@pytest.mark.parametrize("method,path", u.RETIRED_PARAMS)
def test_设了访问口令并登录之后_是404(make_app, login, method, path):
    app = make_app(HUB_ACCESS_PASSWORD=PW)
    u.require_retired(app, path)
    client = app.test_client()
    assert login(client, PW).status_code == 303, "前提：登录成功"
    resp = u.call(client, method, path, headers=FETCH)
    assert (resp.status_code, resp.get_json(silent=True)) == (404, {"error": "not_found"})
