# -*- coding: utf-8 -*-
"""页面服务（17990 上的管理台三页）的白盒自测：导航开关、名单比对、日志里不出令牌与密钥。

8765 那一边用「同一个蓝图挂在裸应用上」代替，不起 moments.web（理由同 test_hub_routes_shell.py）。
与 8765 整站逐字比对、鉴权先于一切这些由 tests/acceptance/bot_ops/test_console_pages_hub.py 守着。
"""
import logging
import re
from pathlib import Path

import pytest
from flask import Flask, render_template

from moments import adapter_service as svc
from moments import hub_addbot, hub_routes

TOKEN = "unit-console-token"
AUTH = {"Authorization": "Bearer " + TOKEN}
MARK = "<!-- console-nav -->"
TEMPLATES = Path(__file__).resolve().parents[2] / "moments" / "templates"
PAGES = [("/hub/bots", "hub_bots.html", "bots"), ("/hub/addbot", "hub_addbot.html", "addbot"),
         ("/hub/dsh-model", "hub_dsh_model.html", "dsh-model")]
NAV_BLOCK = re.compile(r'<nav class="hubnav">.*?</nav>', re.S)


@pytest.fixture
def client(tmp_path, monkeypatch):
    """令牌文件与 bot 的几个根都指到临时目录：哪个处理函数去读，也读不到真实家目录。"""
    token_file = tmp_path / "adapter.token"
    token_file.write_text(TOKEN, encoding="utf-8")
    monkeypatch.setenv("DSH_CONSOLE_TOKEN_FILE", str(token_file))
    for name, sub in (("HOME", "home"), ("USERPROFILE", "home"), ("DSH_BOT_HOME", "dsh-home"),
                      ("HUB_CONFIGS_DSH_DIR", "dsh-configs"), ("HUB_CONFIGS_DIR", "legacy-configs")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.delenv("HUB_TELEGRAM_API_BASE", raising=False)
    urllib3_level = logging.getLogger("urllib3").level      # create_app 会调它，用完放回去
    yield svc.create_app().test_client()
    logging.getLogger("urllib3").setLevel(urllib3_level)


@pytest.fixture
def old_app():
    """8765 上这几页的路由与渲染：同一个蓝图，挂在没有鉴权门的裸应用上（即「已登录」）。"""
    app = Flask(__name__, template_folder=str(TEMPLATES))
    app.register_blueprint(hub_routes.hub_bp)
    return app


# ---------------------------------------------------------------- 导航开关
@pytest.mark.parametrize("path,template,active", PAGES)
def test_导航开关_关着同8765_开着只把nav换成单独一行占位注释(old_app, path, template, active):
    with old_app.test_request_context():
        off = render_template(template, nav_active=active)
        on = render_template(template, nav_active=active, console_nav=True)
    navs = NAV_BLOCK.findall(off)
    assert len(navs) == 1 and 'class="chip on"' in navs[0], "关着时应原样有一条带高亮项的导航"
    assert MARK not in off
    assert off.replace(navs[0], MARK) == on, "开着时除了导航条那一段，别的地方也变了"
    assert MARK in on.splitlines(), "占位注释要单独占一行"
    for gone in ("<nav", 'class="chip', "旧系统供应商", "系统参数"):
        assert gone not in on, "开着时页面里还有导航条的 %r" % gone
    for kept in (".hubnav{", ".chip.on{", "function hubApi"):
        assert kept in on, "导航用的样式与脚本不该跟着删：少了 %r" % kept


@pytest.mark.parametrize("path,template,active", PAGES)
def test_开关只在17990的应用上打开_8765那边照旧(client, old_app, path, template, active):
    new, old = client.get(path, headers=AUTH), old_app.test_client().get(path)
    assert new.status_code == old.status_code == 200
    assert new.headers["Content-Type"] == old.headers["Content-Type"]
    old_html, new_html = old.get_data(as_text=True), new.get_data(as_text=True)
    navs = NAV_BLOCK.findall(old_html)
    assert len(navs) == 1 and MARK not in old_html
    assert old_html.replace(navs[0], MARK) == new_html


# ---------------------------------------------------------------- 内嵌模式（?embed=1）
#: 合并管理台把模型页嵌在供应商页下半块：没有自己的标题、导航和外边距，只留模型设置
EMBED_GONE = ("<h1", 'class="sub"', "<nav")
EMBED_KEPT = ('id="pform"', 'id="pickDialog"', 'id="editDialog"', "function hubApi")


def test_内嵌_8765上embed1_没有标题与导航_清掉外边距与最小高度_模型设置都在(old_app):
    html = old_app.test_client().get("/hub/dsh-model?embed=1").get_data(as_text=True)
    for gone in EMBED_GONE:
        assert gone not in html, "内嵌时页面里还有 %r" % gone
    assert '<body class="embed">' in html
    assert "body.embed{min-height:0;}" in html, "内嵌时 body 不该再撑满一屏（iframe 量高度只增不减）"
    assert "body.embed .wrap{max-width:none;margin:0;padding:0;}" in html
    for kept in EMBED_KEPT:
        assert kept in html, "内嵌时少了 %r" % kept


@pytest.mark.parametrize("query", ["?embed=0", "?embed=true", "?embed=1x", "?embed="])
def test_内嵌_只认embed等于1_别的写法与不带时逐字节相同(old_app, query):
    c = old_app.test_client()
    assert c.get("/hub/dsh-model" + query).get_data() == c.get("/hub/dsh-model").get_data()


def test_内嵌_页面服务上embed1_占位注释照旧单独一行_标题没了(client):
    html = client.get("/hub/dsh-model?embed=1", headers=AUTH).get_data(as_text=True)
    assert MARK in html.splitlines(), "页面服务要留着占位注释，换成什么由壳决定"
    for gone in EMBED_GONE:
        assert gone not in html, "内嵌时页面里还有 %r" % gone
    assert '<body class="embed">' in html


@pytest.mark.parametrize("path", ["/hub/bots", "/hub/addbot"])
def test_内嵌_只有模型页认embed_别的页带了也逐字节照旧(old_app, path):
    c = old_app.test_client()
    assert c.get(path + "?embed=1").get_data() == c.get(path).get_data()


# ---------------------------------------------------------------- 名单
#: 名单外的路径：末尾斜杠、双斜杠、点段、编过码的写死段、大小写，还有 8765 上有而名单里没有的
UNLISTED = [
    "/hub", "/hub/", "/hub/bots/", "/hub/bots//", "/hub//bots", "//hub/bots", "/hub/./bots",
    "/hub/bots/.", "/hub/x/../bots", "/hub/%62ots", "/hub/BOTS", "/hub/api/bots/", "/hub/api/bots//",
    "/hub/api/bots//edit", "/hub/api/bots/botalpha/edit/", "/hub/api/bots/botalpha//edit",
    "/hub/api/bots/botalpha", "/hub/api/bots/restart/", "/hub/api/dsh-model/",
    "/hub/api/dsh-model//effort", "/hub/api/dsh-model/provider//models",
    "/hub/api/dsh-model/provider/x/preview/", "/hub/api/dsh-model/provider/x/rename",
    "/hub/params", "/hub/provider", "/hub/setup", "/hub/healthz", "/hub/api/params",
    "/hub/api/provider", "/hub/api/claude-native/activate",
]


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("path", UNLISTED)
def test_名单外的路径_一条都对不上_404(path, method):
    with pytest.raises(svc.Refuse) as e:
        svc._hub_match(path, method)
    assert (e.value.status, e.value.code) == (404, "not_found")


@pytest.mark.parametrize("path", [p for p in UNLISTED if not p.startswith("//")])
def test_名单外的路径_带对令牌_回404_JSON且不跳转(client, path):
    r = client.post(path, headers=AUTH, json={})
    assert r.status_code == 404 and "Location" not in r.headers
    assert r.get_json()["code"] == "not_found"


def test_框架的路由表里没有挂任何hub规则(client):
    assert [r.rule for r in client.application.url_map.iter_rules() if r.rule.startswith("/hub")] == []


def test_名单正好29项_每项都是8765同一路径同一方法的那个处理函数(old_app):
    on_8765 = {}
    for rule in old_app.url_map.iter_rules():
        for method in rule.methods - {"HEAD", "OPTIONS"}:
            on_8765[(rule.rule, method)] = old_app.view_functions[rule.endpoint]
    pairs = [(rule, m, fn) for rule, methods in svc._HUB_ROUTES for m, fn in methods.items()]
    assert len(pairs) == 29, "接口约定第 2 节是 3 页加 26 个接口（2026-10-20 新增 oauth 6 条）"
    for rule, method, fn in pairs:
        assert on_8765.get((rule, method)) is fn, "%s %s 不是 8765 上那个处理函数" % (method, rule)


#: 一个路径对得上两条的、空名字的：选哪个处理函数、参数是什么，要与 8765 的路由一致
AMBIGUOUS = [("GET", "/hub/api/bots/restart/edit"), ("POST", "/hub/api/bots/restart/edit"),
             ("POST", "/hub/api/bots/restart/restart"), ("GET", "/hub/api/bots/restart/enabled"),
             ("POST", "/hub/api/bots/restart/enabled"), ("POST", "/hub/api/bots/restart"),
             ("POST", "/hub/api/dsh-model/provider"), ("GET", "/hub/api/dsh-model/provider/effort"),
             ("POST", "/hub/api/dsh-model/provider/save"), ("POST", "/hub/api/dsh-model/provider//preview")]


@pytest.mark.parametrize("method,path", AMBIGUOUS)
def test_对得上两条或名字为空时_选的处理函数与参数同8765的路由(old_app, method, path):
    endpoint, want = old_app.url_map.bind("localhost").match(path, method)
    fn, got = svc._hub_match(path, method)
    assert fn is old_app.view_functions[endpoint] and got == want


@pytest.mark.parametrize("method,path,args", [
    ("POST", "/hub/api/bots/%62otalpha/edit", {"bot_id": "botalpha"}),
    ("POST", "/hub/api/dsh-model/provider/%E6%B5%8B%E8%AF%95%20x/test", {"name": "测试 x"}),
    ("POST", "/hub/api/dsh-model/provider/%2546ake/refresh", {"name": "%46ake"}),
    ("POST", "/hub/api/dsh-model/provider/a%2Fb/models", {"name": "a/b"}),
    ("GET", "/hub/api/bots/restart/%FF", {"job_id": "�"}),
])
def test_路径参数先切段_再解一次百分号编码(method, path, args):
    assert svc._hub_match(path, method)[1] == args


@pytest.mark.parametrize("method,path,allow", [
    ("HEAD", "/hub/bots", "GET"), ("OPTIONS", "/hub/addbot", "GET"), ("PUT", "/hub/api/bots", "GET, POST"),
    ("DELETE", "/hub/api/bots/x/edit", "GET, POST"), ("GET", "/hub/api/bots/x/enabled", "POST"),
])
def test_名单内路径用名单外的方法_405并写明能用哪些(method, path, allow):
    with pytest.raises(svc.Refuse) as e:
        svc._hub_match(path, method)
    assert (e.value.status, e.value.code, e.value.headers) == (405, "method_not_allowed", {"Allow": allow})


# ---------------------------------------------------------------- 请求体上限同 8765
def test_hub接口的请求体上限是8765的1MB_不是适配接口的64KB(client):
    url = "/hub/api/dsh-model/provider/save"
    mid = client.post(url, headers=AUTH, json={"name": "!", "pad": "a" * 100000})
    assert (mid.status_code, mid.get_json()["error"]) == (400, "bad_provider"), "100KB 的体应交到处理函数"
    big = b"{" + b" " * svc.HUB_MAX_BODY + b"}"
    r = client.post(url, headers=AUTH, data=big, content_type="application/json")
    assert (r.status_code, r.get_json()) == (413, {"error": "payload_too_large"})


# ---------------------------------------------------------------- 日志
SECRET_TG = "700000009:UNIT-BODY-TG-TOKEN-never-log-abcdefgh"
SECRET_KEY = "sk-UNIT-BODY-PROVIDER-KEY-never-log-12345678"


def test_请求体里的令牌与密钥_处理函数出意外时也不进日志与输出(client, monkeypatch, caplog, capfd):
    caplog.set_level(logging.DEBUG)

    def getme_boom(token):
        raise RuntimeError("getMe 出错：" + token)

    def render_boom(*_a, **_kw):
        raise RuntimeError("渲染出错：" + SECRET_KEY)

    monkeypatch.setattr(hub_addbot, "test_token", getme_boom)
    r = client.post("/hub/api/bots/test_token", headers=AUTH, json={"telegram_token": SECRET_TG})
    assert (r.status_code, r.get_json()) == (500, {"error": "internal"}), "接口出意外应同 8765 回 500 JSON"
    monkeypatch.setattr(hub_routes, "render_template", render_boom)
    r = client.get("/hub/bots", headers=AUTH)
    assert (r.status_code, r.mimetype) == (500, "text/html"), "页面出意外应同 8765 回框架的 500 页"
    r = client.post("/hub/api/dsh-model/provider/save", headers=AUTH,
                    json={"name": "unitvendor", "key": SECRET_KEY})
    assert r.status_code == 400, "前提：这条请求进了处理函数（没给地址，被它拦下）"
    # requests 发 getMe 时 urllib3 在调试级别记的就是这一句（地址里是令牌）：建应用之后它不该再落到日志里
    logging.getLogger("urllib3.connectionpool").debug('%s "GET /bot%s/getMe HTTP/1.1" 200', "x", SECRET_TG)
    out, err = capfd.readouterr()
    logs = caplog.text + out + err
    for secret in (TOKEN, SECRET_TG, SECRET_KEY):
        assert secret not in logs
    assert caplog.text.count("RuntimeError") == 2, "意外只记类名，两次都要记"
