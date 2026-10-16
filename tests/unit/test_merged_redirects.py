# -*- coding: utf-8 -*-
"""8765 上已并进合并管理台（壳）的页面一律跳到壳（2026-10-16 用户定：管理台、朋友圈、画风都只留壳里那一份）。

壳与旧系统供应商入口是同一个站点：本机入口还是公网入口，照 /provider 那一套判据挑，只换路径，
查询串原样带上。画风页还在 8765（壳导航上的"画风"指着这里），所以 /styles、/login、/image 不跳。
朋友圈的写接口回 410，不再走 8765 的旧通知逻辑；读接口照旧。
"""
import pytest

LOCAL = "HUB_LEGACY_PROVIDER_URL_LOCAL"
PUBLIC = "HUB_LEGACY_PROVIDER_URL_PUBLIC"
LOCAL_HOST = "127.0.0.1:8765"
PUBLIC_HOST = "8765.example.com"
SHELL_PUBLIC = "https://shell.example.test/provider"

PAGES = [("/", "/moments"), ("/moments", "/moments")]


@pytest.fixture
def web(monkeypatch):
    for key in (LOCAL, PUBLIC, "HUB_ACCESS_PASSWORD", "HUB_ADMIN_PASSWORD", "HUB_TRUST_PROXY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(PUBLIC, SHELL_PUBLIC)
    from moments import web as web_mod
    return web_mod


def _go(web, path, host=LOCAL_HOST, method="GET"):
    return web.app.test_client().open(path, method=method, environ_overrides={"HTTP_HOST": host},
                                      follow_redirects=False)


@pytest.mark.parametrize("path,dest", PAGES)
def test_本机打开_302到本机的壳_同一路径对照_不让浏览器记死(web, path, dest):
    r = _go(web, path)
    assert r.status_code == 302, (path, r.status_code)
    assert r.headers["Location"] == "http://127.0.0.1:8767" + dest, r.headers["Location"]
    assert r.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("path,dest", PAGES)
def test_经公网打开_302到配置里壳的公网站点(web, path, dest):
    r = _go(web, path, host=PUBLIC_HOST)
    assert r.status_code == 302
    assert r.headers["Location"] == "https://shell.example.test" + dest, r.headers["Location"]


def test_查询串原样带上(web):
    r = _go(web, "/?bot=bot4&x=%E4%BD%A0")
    assert r.headers["Location"] == "http://127.0.0.1:8767/moments?bot=bot4&x=%E4%BD%A0", r.headers["Location"]


@pytest.mark.parametrize("path", ["/moments/", "/momentsx", "/index"])
def test_只认这几条确切路径(web, path):
    r = _go(web, path)
    assert not (r.status_code == 302 and "8767" in r.headers.get("Location", "")), (path, r.status_code)


def test_HEAD也跳_POST不跳(web):
    assert _go(web, "/moments", method="HEAD").status_code == 302
    assert _go(web, "/moments", method="POST").status_code != 302


@pytest.mark.parametrize("path", ["/hub", "/hub/bots", "/hub/addbot", "/hub/dsh-model"])
def test_管理台各页这一步不跳_等壳接管8765端口时一起换(web, path):
    assert "8767" not in _go(web, path).headers.get("Location", "")


@pytest.mark.parametrize("path", ["/login", "/styles"])
def test_画风页还在8765_它和登录页都不跳到壳(web, path):
    r = _go(web, path)
    assert "8767" not in r.headers.get("Location", ""), (path, r.status_code, r.headers.get("Location"))
    assert r.status_code != 410


def test_旧系统供应商入口照旧(web):
    r = _go(web, "/provider")
    assert r.status_code == 302 and r.headers["Location"] == "http://127.0.0.1:8767/provider"


WRITES = [("POST", "/api/like"), ("POST", "/api/comment"), ("POST", "/api/moment"),
          ("DELETE", "/api/comment/12"), ("POST", "/api/profile/bot4")]


@pytest.mark.parametrize("method,path", WRITES)
def test_朋友圈写接口回410_说去合并管理台_什么都不写(web, monkeypatch, method, path):
    touched = []
    for name in ("toggle_like", "add_comment", "insert_moment", "delete_comment", "set_display_name"):
        if hasattr(web.db, name):
            monkeypatch.setattr(web.db, name, lambda *a, _n=name, **k: touched.append(_n))
    r = web.app.test_client().open(path, method=method, json={"moment_id": 1, "text": "x"},
                                   environ_overrides={"HTTP_HOST": LOCAL_HOST})
    assert r.status_code == 410, (method, path, r.status_code)
    body = r.get_json()
    assert body["error"] == "moved" and "合并管理台" in body["detail"], body
    assert touched == [], "410 时不该写朋友圈库：%r" % touched


@pytest.mark.parametrize("path", ["/api/moments", "/api/moment/1", "/api/profile/bot4"])
def test_朋友圈读接口照旧_不回410(web, path):
    assert _go(web, path).status_code != 410
