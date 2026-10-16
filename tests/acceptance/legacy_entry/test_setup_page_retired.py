# -*- coding: utf-8 -*-
"""「密钥待填」页下线（INTERFACE-dsh修补 第四节）。

用户看得见的结果：管理台导航上不再挂着「⚠️ 密钥待填 N」，原来那一页和它的两条接口打不开了；
命令行的 `scripts/setup_keys.py` 照旧能用。

安全做法：保存密钥那条接口在下线之前是活的，会写文件。量「它已下线」的用例先看路由清单
（`le_util.require_retired`），清单里还有就当场判失败，不对它发请求。没登录的用例只发读请求。
请求体里的令牌、密钥都是测试自己编的假值。
"""
import html as html_mod
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from . import le_util as u

REPO_ROOT = Path(__file__).resolve().parents[3]
PW = "hub-access-pw-12345"
HTML = {"Accept": "text/html,application/xhtml+xml"}
FETCH = {"Accept": "*/*"}
#: 带导航条的页面（约定 4.2 点名的那几页）。朋友圈页 / 自 2026-10-16 跳到壳，已经不是带导航条的页面
PAGES = ["/hub", "/hub/bots", "/hub/addbot", "/hub/dsh-model", "/hub/params", "/hub/provider",
         "/styles"]
TRACES = ["nav-setup", "/hub/setup", "密钥待填", "/hub/api/setup/status"]


def test_密钥待填页_已下线_回404的HTML错误页(hub):
    r = u.go(hub, path="/hub/setup")
    assert (r.status_code, "text/html" in (r.headers.get("Content-Type") or "")) == (404, True), \
        "/hub/setup 实得 %s %s" % (r.status_code, r.headers.get("Content-Type"))


def test_保存密钥接口_已下线回404_不写文件不起进程不调launchctl(app, hub, sandbox):
    u.require_retired(app, "/hub/api/setup/keys")
    roots = (sandbox.home, sandbox.hub_legacy_cfg, sandbox.dsh_home)
    before = u.snapshot(*roots)
    body = {"token": "123456789:AAFakeTokenForTestsOnly_abcdefghijklmnop", "userid": "5550001",
            "deepseek": "sk-test-deepseek-000000", "bot": "chenlulu"}
    with u.watching(sandbox) as seen:
        r = u.go(hub, path="/hub/api/setup/keys", method="POST", json=body)
    assert (r.status_code, r.get_json(silent=True)) == (404, {"error": "not_found"})
    assert u.snapshot_diff(before, u.snapshot(*roots)) == [], "下线了的接口还在写文件"
    assert (seen.processes, seen.threads, seen.launchctl, seen.legacy_diff) == ([], [], [], [])


def test_没通过鉴权时_先被登录门挡住_不透露这些路由在不在(make_client):
    c = make_client(HUB_ACCESS_PASSWORD=PW)
    page = u.go(c, path="/hub/setup", headers=HTML)
    api = u.go(c, path="/hub/api/setup/status", headers=FETCH)
    assert (page.status_code, (page.headers.get("Location") or "").endswith("/login")) == (302, True)
    assert (api.status_code, api.headers.get("Hub-Gate")) == (401, "1")


def test_带导航条的页面_不再有密钥待填的任何痕迹_页面脚本也不查那条接口(hub, styles_env):
    """源码里没有 nav-setup、没有指向 /hub/setup 的链接、没有「密钥待填」，脚本里没有那条状态接口的地址。"""
    found = {}
    for path in PAGES:
        r = u.go(hub, path=path)
        assert r.status_code == 200, "%s 期望 200，实得 %s" % (path, r.status_code)
        text = html_mod.unescape(r.get_data(as_text=True))
        for src in re.findall(r"""<script\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", text):
            if src.startswith("/") and not src.startswith("//"):          # 只取同站的脚本
                got = u.go(hub, path=src)
                if got.status_code == 200:
                    text += "\n" + got.get_data(as_text=True)
        hit = [t for t in TRACES if t in text]
        if hit:
            found[path] = hit
    assert found == {}, "这些页面里还留着「密钥待填」的痕迹：%s" % found


def test_命令行查看三项密钥填了没_照旧能用(sandbox):
    """`python3 scripts/setup_keys.py status --repo … --home …`：输出三项各自填了没的 JSON。"""
    repo = sandbox.tmp / "fake-repo"
    (repo / "configs").mkdir(parents=True)
    env = dict(os.environ)          # 假家目录、HUB_LAUNCHCTL、HUB_BUN 原样带给子进程，一个都不删
    p = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "setup_keys.py"), "status",
                        "--repo", str(repo), "--home", str(sandbox.home)],
                       capture_output=True, text=True, timeout=60, env=env)
    assert p.returncode == 0, "退出码 %s，stderr：%s" % (p.returncode, p.stderr[-400:])
    data = json.loads(p.stdout)
    assert isinstance(data, dict) and len(data) == 3, data
    assert all(v is False for v in data.values()), "空环境下三项都该是没填：%s" % data


def test_安装脚本里漏填密钥的提示_改成指向命令行脚本():
    text = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")
    assert "漏填的随时再跑 bash scripts/setup_keys.sh 补上" in text
    assert "/hub/setup" not in text, "install.sh 里还在把人指向已下线的 /hub/setup"
