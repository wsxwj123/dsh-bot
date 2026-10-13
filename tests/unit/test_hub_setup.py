# -*- coding: utf-8 -*-
"""「密钥待填」页已下线（INTERFACE-dsh修补 第四节）。

跑：python3 -m pytest tests/unit/test_hub_setup.py -q

这个文件原来测的是那一页和它的两条接口（/hub/setup、/hub/api/setup/status、
/hub/api/setup/keys）。三条路由下线之后，这里只盯「确实下线了，而且没带坏别的」：

  1. 三条路径都回 404，蓝图的规则表里也找不到它们；
  2. 鉴权仍然先于 404：没登录时看不出这些地址还在不在；
  3. 路由段里那条对命令行脚本的导入没了，但「把仓库根放进搜索路径」的兜底还在
     （后面的段还要在模块顶层导入仓库根下的模块）。

不起 moments.web（会拖进 db / 朋友圈路由），只把蓝图挂到裸 Flask 上。接口路径在裸应用上
的 404 是框架默认页；真应用里它会被统一处理成 `{"error":"not_found"}`，那一层由验收测试
（tests/acceptance/legacy_entry/test_setup_page_retired.py）盯。

HOME 指到 tmp 沙箱，提交的值也故意不合格：就算哪天路由被误加回来，这些用例也写不到
机主真实的 configs/_global.yml 与 ~/.claude/channels/。
"""
import os
import sys
from pathlib import Path

import pytest
from flask import Flask

from moments import hub_auth, hub_routes
from moments.hub_routes import hub_bp

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "moments" / "templates"

#: 下线的三条路由：方法、路径、请求体
RETIRED = [
    ("get", "/hub/setup", None),
    ("get", "/hub/api/setup/status", None),
    ("post", "/hub/api/setup/keys", {"field": "userid", "value": "not-a-user-id"}),
]


def _app(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    app = Flask(__name__, template_folder=str(TEMPLATES))
    app.register_blueprint(hub_bp)
    app.config["TESTING"] = True
    return app


def _call(client, method, path, body, **kw):
    return getattr(client, method)(path, **({"json": body} if body is not None else {}), **kw)


@pytest.mark.parametrize("method,path,body", RETIRED)
def test_已下线的三条路由都回404(monkeypatch, tmp_path, method, path, body):
    client = _app(monkeypatch, tmp_path).test_client()
    assert _call(client, method, path, body).status_code == 404


def test_蓝图的规则表里没有密钥页的任何路径(monkeypatch, tmp_path):
    rules = [r.rule for r in _app(monkeypatch, tmp_path).url_map.iter_rules()]
    assert [r for r in rules if "setup" in r] == []
    # 没把别的页面一起带走：相邻的几条路由还在
    for still in ("/hub", "/hub/addbot", "/hub/params", "/hub/dsh-model", "/hub/bots"):
        assert still in rules, "%s 不见了" % still


@pytest.mark.parametrize("method,path,body", RETIRED)
def test_没登录时鉴权先于404_看不出路由在不在(monkeypatch, tmp_path, method, path, body):
    monkeypatch.setenv("HUB_ACCESS_PASSWORD", "hub-access-pw-12345")
    monkeypatch.delenv("HUB_ADMIN_PASSWORD", raising=False)
    app = _app(monkeypatch, tmp_path)
    hub_auth.install(app)
    client = app.test_client()
    if path == "/hub/setup":
        r = _call(client, method, path, body, headers={"Accept": "text/html"})
        assert r.status_code == 302 and "/login" in r.headers["Location"]
    else:
        r = _call(client, method, path, body, headers={"Accept": "*/*"})
        assert r.status_code == 401 and r.headers.get("Hub-Gate") == "1"


def test_路由段不再导入命令行脚本_也不留那一段的处理函数与模板():
    for gone in ("setup_keys", "_sk_paths", "_sk_deepseek_provider", "hub_setup_page",
                 "setup_status", "setup_write_key"):
        assert not hasattr(hub_routes, gone), "hub_routes 里还留着 %s" % gone
    assert not (TEMPLATES / "hub_setup.html").exists()
    # 预置常量别处还在用，不能跟着删
    assert hub_routes.DEEPSEEK_PRESET["base_url"] == "https://api.deepseek.com/anthropic"


def test_仓库根仍在搜索路径里_后面的段要靠它导入仓库根下的模块():
    # 用 samefile 比：仓库路径上有符号链接时，两边取到的字符串写法可以不同
    assert os.path.samefile(hub_routes._SK_REPO_ROOT, ROOT)
    assert any(p and os.path.isdir(p) and os.path.samefile(p, ROOT) for p in sys.path)
    # 第 9 段在模块顶层导入的两个仓库根下的模块
    assert os.path.isfile(os.path.join(hub_routes._SK_REPO_ROOT, "gateway_client.py"))
    assert os.path.isfile(os.path.join(hub_routes._SK_REPO_ROOT, "config_loader.py"))
    assert hub_routes.gateway_client is sys.modules["gateway_client"]
    assert hub_routes.config_loader is sys.modules["config_loader"]


def test_命令行脚本的用法原样保留():
    """下线的只是网页；`scripts/setup_keys.py` 的四个子命令与 .sh 入口都还在。"""
    from scripts import setup_keys

    src = (ROOT / "scripts" / "setup_keys.py").read_text(encoding="utf-8")
    for sub in ("status", "set-deepseek", "set-token", "set-userid"):
        assert '"%s"' % sub in src or "'%s'" % sub in src, "子命令 %s 不见了" % sub
    assert callable(setup_keys.main) and callable(setup_keys.status)
    assert (ROOT / "scripts" / "setup_keys.sh").is_file()
