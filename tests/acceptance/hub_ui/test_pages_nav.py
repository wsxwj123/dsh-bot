# -*- coding: utf-8 -*-
"""契约 1：页面契约（门户 /hub、导航 _nav.html、/hub/dsh-model 重写，其余页面不动）。

判定一律"包含即可"，不要求逐字相同（契约开头就写明）。
实现未落地时本文件红，那是红基线。
"""
import re

import pytest

# 使用管理台那条导航的页面（契约 1 第 18 行说的"所有 hub 页面"）
NAV_PAGES = ["/hub", "/hub/dsh-model", "/hub/bots", "/hub/addbot", "/hub/params", "/hub/provider"]


def page(hub, path):
    r = hub.get(path)
    assert r.status_code == 200, "%s 期望 200，实得 %s" % (path, r.status_code)
    return r.get_data(as_text=True)


def test_门户三组卡片的入口一个不少(hub):
    """/hub 门户要留全部既有入口，再补上供应商与模型那张。"""
    html = page(hub, "/hub")
    for href in ('/hub/dsh-model', '/hub/bots', '/hub/addbot', '/hub/setup', '/hub/params',
                 '/hub/provider', '/provider', '/styles', 'href="/"'):
        assert href in html, "/hub 门户缺入口 %s" % href


def test_门户provider卡片class在href之前(hub):
    """形状约束（契约 1 第 20 行）：门户的卡片链接里 class 属性必须在 href 之前。"""
    html = page(hub, "/hub")
    tags = re.findall(r"<a\b[^>]*>", html)
    card = [t for t in tags if '/hub/provider' in t]
    assert card, "/hub 门户里没有指向 /hub/provider 的卡片，实得链接：%s" % [t[:80] for t in tags]
    for t in card:
        cls = t.find('class=')
        href = t.find('href=')
        assert cls != -1, "provider 卡片没有 class 属性：%s" % t
        assert cls < href, "provider 卡片的 class 必须在 href 之前，实得：%s" % t


@pytest.mark.parametrize("path", ["/hub", "/hub/dsh-model", "/hub/bots", "/hub/addbot", "/hub/params"])
def test_每页恰好高亮一个导航项(hub, path):
    """形状约束（契约 1 第 20 行）：任意一个页面里 class="chip on" 恰好出现一次。"""
    html = page(hub, path)
    n = html.count('class="chip on"')
    assert n == 1, "%s 里 class=\"chip on\" 应恰好一次，实得 %s 次" % (path, n)


@pytest.mark.parametrize("path", NAV_PAGES)
def test_每页导航都有旧系统与供应商与模型(hub, path):
    """契约 1 第 18 行：所有 hub 页面都含 /provider 与「旧系统」、/hub/dsh-model 与「供应商与模型」。"""
    html = page(hub, path)
    assert 'href="/provider"' in html, "%s 的导航缺 href=\"/provider\"" % path
    assert "旧系统" in html, "%s 的导航缺「旧系统」" % path
    assert 'href="/hub/dsh-model"' in html, "%s 的导航缺 href=\"/hub/dsh-model\"" % path
    assert "供应商与模型" in html, "%s 的导航缺「供应商与模型」" % path


def test_cliproxy的hub_provider撤出了导航(hub):
    """契约 1 第 18 行：/hub/provider 不再出现在 _nav.html，只在门户里留卡片。

    /hub/bots 是纯导航加状态页，它不该再提到 /hub/provider。
    """
    html = page(hub, "/hub/bots")
    assert "/hub/provider" not in html, "导航里还留着 /hub/provider（/hub/bots 页里出现了它）"


def test_供应商与模型页的关键元素齐全(hub):
    """契约 1 第 15 行：重写为「供应商与模型」闭环页的关键词与输入位。"""
    html = page(hub, "/hub/dsh-model")
    for word in ("自建供应商", "添加供应商", "编辑", "删除", "管理", "OpenAI 格式", "Anthropic 格式"):
        assert word in html, "/hub/dsh-model 缺「%s」" % word
    assert 'type="password"' in html, "/hub/dsh-model 缺 type=\"password\" 的密钥输入位"


def test_刷新模型列表出现在可见元素文案里(hub):
    """契约 1 第 15 行：必须保留；且落在供应商行内那颗按钮的文案上，不许只藏在脚本字符串里。

    判定用"某个元素的文字里出现它"（>文字< 的形状），脚本里的 "刷新模型列表" 不满足。
    """
    html = page(hub, "/hub/dsh-model")
    assert "刷新模型列表" in html, "/hub/dsh-model 缺「刷新模型列表」"
    assert re.search(r">[^<>]*刷新模型列表[^<>]*<", html), \
        "「刷新模型列表」只出现在脚本字符串里，没有落在元素文案上"


def test_思考按钮在页面上_档位名不是初始渲染(hub):
    """契约 2.6 的页面断言：HTML 含「思考」；档位懒加载，初始 HTML 不含档位名。

    档位名用按钮文字的形状（>最高< 之类）判定：JS 里的映射表（'最高' 带引号）不算初始渲染。
    """
    html = page(hub, "/hub/dsh-model")
    assert "思考" in html, "/hub/dsh-model 缺「思考」按钮的字样"
    for label in (">最高<", ">关<", ">低<", ">高<"):
        assert label not in html, "初始 HTML 里就渲染了档位按钮（%s），档位应懒加载" % label


def test_旧页provider不共用新导航(hub):
    """契约 1 第 19 行：/provider 自带顶栏，与旧仓逐字一致，不共用这条导航。"""
    html = page(hub, "/provider")
    assert 'href="/hub/dsh-model"' not in html, "/provider 旧页混进了新导航（出现了 /hub/dsh-model）"


def test_既有页面还能开(hub):
    """契约 1 恒 200 铁律：这些页面重写后都必须仍能打开。"""
    for path in ("/hub", "/hub/dsh-model", "/hub/bots", "/hub/addbot", "/hub/params", "/hub/provider",
                 "/provider", "/styles", "/"):
        r = hub.get(path)
        assert r.status_code == 200, "%s 期望 200，实得 %s" % (path, r.status_code)
