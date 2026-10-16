# -*- coding: utf-8 -*-
"""§7 不变的面：各挑代表性的一两条做对照，证明这次下线没有波及它们。

这一组在下线之前就是绿的，下线之后也必须是绿的。预期取自更早批次的约定与既有用例已经
钉住的行为，外加在空沙箱（没有任何 bot、没配 cliproxy、朋友圈库是空的）里量到的现状。
"""
import pytest

from . import le_util as u

PW = "hub-access-pw-12345"
ADMIN_PW = "hub-admin-pw-67890"
HTML = {"Accept": "text/html,application/xhtml+xml"}
FETCH = {"Accept": "*/*"}


def got(client, path, **kw):
    r = u.go(client, path=path, **kw)
    return r.status_code, r.get_json(silent=True)


# ============================================================ 页面都还打得开

# 「密钥待填」页 /hub/setup 已下线（INTERFACE-dsh修补 4.4），不在这张名单里；它的去向见 test_setup_page_retired.py
# 朋友圈页 / 自 2026-10-16 跳到壳（朋友圈只留壳里那一份），不在这张名单里，见下一条与 test_merged_redirects.py
@pytest.mark.parametrize("path", ["/hub", "/hub/provider", "/hub/params", "/hub/bots",
                                  "/hub/addbot", "/hub/dsh-model", "/styles"])
def test_页面仍是200的HTML(hub, styles_env, path):
    r = u.go(hub, path=path)
    assert (r.status_code, "text/html" in (r.headers.get("Content-Type") or "")) == (200, True), \
        "%s 实得 %s %s" % (path, r.status_code, r.headers.get("Content-Type"))


def test_朋友圈页跳到壳的朋友圈_不让浏览器记死(hub):
    r = u.go(hub, path="/")
    assert (r.status_code, r.headers.get("Location"), r.headers.get("Cache-Control")) == \
        (302, "http://127.0.0.1:8767/moments", "no-store")


def test_首页别的卡片入口一个不少(hub):
    html = u.go(hub, path="/hub").get_data(as_text=True)
    missing = [h for h in ('href="/hub/dsh-model"', 'href="/hub/bots"', 'href="/hub/addbot"',
                           'href="/hub/params"', 'href="/hub/provider"',
                           'href="/styles"', 'href="/"') if h not in html]
    assert missing == [], "/hub 少了这些入口：%s" % missing


# ============================================================ 模型来源那一块

def test_模型来源接口_没配cliproxy时照旧200并提示未配置(hub):
    code, body = got(hub, "/hub/api/provider")
    assert (code, (body or {}).get("warnings")) == (200, ["cliproxy_unconfigured"])


def test_授权账号接口_没配cliproxy时照旧200空列表(hub):
    assert got(hub, "/hub/api/oauth/accounts") == \
        (200, {"accounts": [], "warnings": ["cliproxy_unconfigured"]})


def test_切回官方订阅的接口还在_用GET请求是405(hub):
    assert got(hub, "/hub/api/claude-native/activate") == (405, {"error": "method_not_allowed"})


# ============================================================ 密钥向导（已下线）、系统参数

def test_密钥向导状态接口_已下线_回404的not_found(hub):
    """原来：空环境下回 200 与三项都是否。INTERFACE-dsh修补 4.1、4.4 把这条接口整条下线了。"""
    assert got(hub, "/hub/api/setup/status") == (404, {"error": "not_found"})


def test_系统参数接口_照旧200并给出参数值(hub):
    code, body = got(hub, "/hub/api/params")
    assert (code, isinstance((body or {}).get("values"), dict)) == (200, True)


# ============================================================ 朋友圈、画风

def test_朋友圈列表接口_空库回空列表(hub):
    assert got(hub, "/api/moments") == (200, [])


def test_发朋友圈回410_说去合并管理台_列表里没有多出这一条(hub):
    """2026-10-16 起 8765 不再收发帖：发帖后的通知只留壳里那一份。"""
    posted = hub.post("/api/moment", json={"text": "对照用的一条"})
    assert (posted.status_code, (posted.get_json() or {}).get("error")) == (410, "moved")
    assert got(hub, "/api/moments") == (200, [])


def test_朋友圈资料接口_没有资料的名字照旧200(hub):
    code, body = got(hub, "/api/profile/%s" % u.FAKE_BOT)
    assert (code, (body or {}).get("display_name", "缺键")) == (200, None)


def test_图片地址_不存在的文件照旧404(hub):
    assert u.go(hub, path="/image/zz-no-such-file.png").status_code == 404


def test_生图供应商接口_照旧200(hub):
    assert got(hub, "/api/image_provider") == (200, {"provider": "novelai", "scope": "legacy"})


def test_画风接口_空数据时照旧200(hub, styles_env):
    code, body = got(hub, "/api/styles")
    assert (code, (body or {}).get("styles"), (body or {}).get("active")) == (200, [], "default")


def test_画风密钥接口_没设时照旧回未设置(hub, styles_env):
    assert got(hub, "/api/novelai_key") == (200, {"set": False, "masked": ""})


# ============================================================ 新系统各页的接口

def test_bot状态接口_没有bot时回空列表(hub):
    code, body = got(hub, "/hub/api/bots")
    assert (code, (body or {}).get("bots"), (body or {}).get("disabled")) == (200, [], [])


def test_供应商与模型接口_没有网关时三项都是空的(hub):
    assert got(hub, "/hub/api/dsh-model") == \
        (200, {"bots": [], "providers": [], "custom_providers": []})


def test_添加bot页的模板接口_照旧200(hub):
    assert got(hub, "/hub/api/bots/templates") == (200, {"templates": []})


def test_量这些接口期间_没有调launchctl(hub, sandbox):
    for path in ("/hub/api/bots", "/hub/api/dsh-model", "/hub/api/provider", "/hub/api/params"):
        u.go(hub, path=path)
    assert sandbox.launchctl_calls() == [], sandbox.launchctl_calls()


# ============================================================ 全站登录门与管理锁

def test_设了访问口令没登录_打开管理台首页_照旧去登录页(make_client):
    c = make_client(HUB_ACCESS_PASSWORD=PW)
    r = u.go(c, path="/hub", headers=HTML)
    assert (r.status_code, (r.headers.get("Location") or "").endswith("/login")) == (302, True)


def test_设了访问口令没登录_调朋友圈接口_照旧401(make_client):
    c = make_client(HUB_ACCESS_PASSWORD=PW)
    assert got(c, "/api/moments", headers=FETCH) == (401, {"error": "unauthorized"})


def test_设了访问口令没登录_探活地址照旧免登录(make_client):
    c = make_client(HUB_ACCESS_PASSWORD=PW)
    assert got(c, "/hub/healthz", headers=FETCH) == (200, {"status": "ok"})


def test_设了管理口令只过访问口令_调管理台接口_照旧被管理锁挡(make_client, login):
    c = make_client(HUB_ACCESS_PASSWORD=PW, HUB_ADMIN_PASSWORD=ADMIN_PW)
    assert login(c, PW).status_code == 303, "前提：只用访问口令登录成功"
    assert got(c, "/hub/api/bots", headers=FETCH) == (401, {"error": "admin_unauthorized"})


def test_设了管理口令只过访问口令_朋友圈照旧能看(make_client, login):
    c = make_client(HUB_ACCESS_PASSWORD=PW, HUB_ADMIN_PASSWORD=ADMIN_PW)
    assert login(c, PW).status_code == 303, "前提：只用访问口令登录成功"
    r = u.go(c, path="/", headers=HTML)             # 朋友圈页跳到壳：过了访问口令就放行到跳转，不要管理口令
    assert (r.status_code, r.headers.get("Location")) == (302, "http://127.0.0.1:8767/moments")


# ============================================================ 框架级错误仍回 JSON

@pytest.mark.parametrize("path", ["/api/zz-no-such-endpoint", "/hub/api/zz-no-such-endpoint"])
def test_不存在的接口地址_照旧是not_found的JSON(hub, path):
    assert got(hub, path) == (404, {"error": "not_found"})


def test_只收POST的接口用GET请求_照旧是method_not_allowed的JSON(hub):
    assert got(hub, "/hub/api/bots/test_token") == (405, {"error": "method_not_allowed"})


# ============================================================ 别的页面本来就没有那三个头

@pytest.mark.parametrize("path", ["/hub", "/hub/provider", "/", "/styles", "/hub/api/bots",
                                  "/api/moments"])
def test_别的页面与接口照旧不带那三个头(hub, styles_env, path):
    r = u.go(hub, path=path)
    present = {h: r.headers.get(h) for h in u.LEGACY_ONLY_HEADERS if r.headers.get(h) is not None}
    assert present == {}, "%s 多出了这些头：%s" % (path, present)
