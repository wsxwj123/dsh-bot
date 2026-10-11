# -*- coding: utf-8 -*-
"""§13 三页界面结构与锚点（第 5 稿追加）+ §11d/§12c 的页面锚点。

判定一律"源码/HTML 里逐字包含即可"。§13 的既有锚点保全清单必须逐条仍在；
新增锚点是新一轮要落的东西。实现未落地时新锚点那几条红，那是红基线。
"""
import re

import pytest

BOTS = "/hub/bots"
ADDBOT = "/hub/addbot"
DSHMODEL = "/hub/dsh-model"


def _page(client, path):
    r = client.get(path)
    assert r.status_code == 200, "%s 期望 200，实得 %s" % (path, r.status_code)
    return r.get_data(as_text=True)


# ============================================================ §13 hub_bots

def test_hub_bots_四个子串逐字仍在(web_client):
    html = _page(web_client, BOTS)
    for word in ("只管新系统的 bot", "旧管理台", "桌面脚本", "重启"):
        assert word in html, "/hub/bots 缺既有子串「%s」" % word


def test_hub_bots_开关与重启的脚本锚点仍在(web_client):
    """§13：/enabled、confirm( 、停止、启用、r.body.disabled、startRestart() 函数定义。"""
    html = _page(web_client, BOTS)
    assert "/enabled" in html, "缺 /enabled"
    assert "confirm(" in html, "缺 confirm( 确认框"
    assert "停止" in html and "启用" in html, "缺开关按钮文案"
    assert "r.body.disabled" in html, "缺 r.body.disabled 的读法"
    assert re.search(r"startRestart\s*=", html) or re.search(r"function\s+startRestart", html), \
        "缺 startRestart() 函数定义"


def test_hub_bots_不含旧系统入口(web_client):
    html = _page(web_client, BOTS)
    assert "/hub/provider" not in html, "hub_bots 不该提到 /hub/provider（既有契约）"


# ============================================================ §13 hub_addbot

def test_hub_addbot_页200_html且导航四项全在(web_client):
    r = web_client.get(ADDBOT)
    assert r.status_code == 200, r.status_code
    assert r.headers["Content-Type"].startswith("text/html"), r.headers.get("Content-Type")
    html = r.get_data(as_text=True)
    assert 'href="/provider"' in html, "导航缺 href=\"/provider\""
    assert "旧系统" in html, "导航缺「旧系统」"
    assert 'href="/hub/dsh-model"' in html, "导航缺 href=\"/hub/dsh-model\""
    assert "供应商与模型" in html, "导航缺「供应商与模型」"


# ============================================================ §13 hub_dsh_model 既有锚点

def test_hub_dsh_model_既有文案与密码框逐字仍在(web_client):
    html = _page(web_client, DSHMODEL)
    for word in ("自建供应商", "添加供应商", "编辑", "删除", "管理", "OpenAI 格式", "Anthropic 格式"):
        assert word in html, "/hub/dsh-model 缺「%s」" % word
    assert 'type="password"' in html, "缺 type=\"password\" 的密钥输入位"


def test_hub_dsh_model_刷新模型列表仍是元素文案(web_client):
    html = _page(web_client, DSHMODEL)
    assert re.search(r">[^<>]*刷新模型列表[^<>]*<", html), \
        "「刷新模型列表」必须落在元素文案上（不是只在脚本字符串里）"


def test_hub_dsh_model_思考字样在_档位名不初始渲染(web_client):
    html = _page(web_client, DSHMODEL)
    assert "思考" in html, "缺「思考」"
    for label in (">最高<", ">关<", ">低<", ">高<"):
        assert label not in html, "初始 HTML 里渲染了档位名 %s（应懒加载）" % label


# ============================================================ §13 三页共同

@pytest.mark.parametrize("path", [BOTS, ADDBOT, DSHMODEL])
def test_三页_高亮导航项恰好一次(web_client, path):
    html = _page(web_client, path)
    n = html.count('class="chip on"')
    assert n == 1, "%s 里 class=\"chip on\" 应恰好一次，实得 %s 次" % (path, n)


@pytest.mark.parametrize("path", [BOTS, ADDBOT, DSHMODEL])
def test_三页_都能打开(web_client, path):
    r = web_client.get(path)
    assert r.status_code == 200, "%s 实得 %s" % (path, r.status_code)


# ============================================================ §13 新增锚点（hub_dsh_model）

def test_新增锚点_编辑弹窗与音色盒(web_client):
    html = _page(web_client, DSHMODEL)
    assert 'id="editDialog"' in html, "缺 #editDialog（二次编辑弹窗）"
    assert 'id="voice-box"' in html, "缺 #voice-box（音色盒）"


def test_新增锚点_两个页签与两个面板(web_client):
    html = _page(web_client, DSHMODEL)
    for anchor in ('id="tab-model"', 'id="tab-providers"', 'id="pane-model"', 'id="pane-providers"'):
        assert anchor in html, "缺 %s" % anchor
    assert re.search(r"switchTab\s*\(", html), "缺 switchTab(name) 函数"


def test_页签切换用hidden类(web_client):
    """§13：页签只切 hidden。源码里页签的显隐机制必须走 hidden 类。"""
    html = _page(web_client, DSHMODEL)
    assert re.search(r"classList\.(toggle|add|remove)\(\s*['\"]hidden['\"]", html), \
        "页签显隐没用 hidden 类（classList.toggle/add/remove('hidden')）"


# ============================================================ §11d 弹窗锚点

def test_弹窗_九个元素锚点全在(web_client):
    html = _page(web_client, DSHMODEL)
    for anchor in ("pickDialog", "pickSearch", "pickAll", "pickNone", "pickCount",
                   "pickList", "pickRowTpl", "pickConfirm", "pickCancel"):
        assert ("id=\"%s\"" % anchor) in html, "缺 #%s" % anchor


def test_弹窗_两个函数定义在(web_client):
    html = _page(web_client, DSHMODEL)
    assert re.search(r"function\s+refreshProvider|refreshProvider\s*=", html), \
        "缺 refreshProvider（刷新按钮处理器）"
    assert re.search(r"function\s+openPick|openPick\s*=", html), \
        "缺 openPick（唯一的弹窗打开入口）"


def test_弹窗_openPick的调用点恰好一处(web_client):
    """§11d：失败分支不得出现对 openPick 的调用 —— 调用只该有成功分支那一处。"""
    html = _page(web_client, DSHMODEL)
    calls = re.findall(r"openPick\s*\(", html)
    defines = re.findall(r"function\s+openPick|openPick\s*=", html)
    assert len(defines) >= 1, "openPick 没有定义"
    assert len(calls) == 1, \
        "openPick 的调用应恰好一处（成功分支），实得 %d 处" % len(calls)


def test_弹窗_行标记文案在(web_client):
    html = _page(web_client, DSHMODEL)
    for word in ("手动", "上游没有", "上下文"):
        assert word in html, "弹窗缺标记文案「%s」" % word


# ============================================================ §12c 测试连接按钮

def test_测试连接_按钮锚点与文案(web_client):
    html = _page(web_client, DSHMODEL)
    assert 'data-act="test"' in html, "缺 data-act=\"test\" 按钮"
    assert "测试连接" in html, "缺「测试连接」文案"
    assert html.find('data-act="test"') > html.find("刷新模型列表"), \
        "「测试连接」按钮应排在「刷新模型列表」之后"


def test_测试连接_处理器与进行中文案(web_client):
    html = _page(web_client, DSHMODEL)
    assert re.search(r"testProvider\s*\(", html), "缺 testProvider(p, btn) 处理器"
    assert "测试中…" in html, "缺「测试中…」进行中文案"
