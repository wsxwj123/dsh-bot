# -*- coding: utf-8 -*-
"""§3 导航与首页门户里的入口：仅有的两处，都指向站内的 /provider，都在新标签页打开。

判定按约定给的源码锚点，逐字包含。页面源码里不出现旧系统那边的地址：判据与配置只在
/provider 那一处生效，导航、卡片、旧书签走同一条路。
"""
import re

import pytest

from . import le_util as u

OTHER_NAV_PAGES = [p for p in u.NAV_PAGES if p != "/hub"]
#: 一个不可能碰巧出现在页面里的自定义入口地址
ODD_LOCAL = "http://127.0.0.1:18767/zz-legacy-entry-local"
ODD_PUBLIC = "https://zz-legacy-entry.example.test/zz-legacy-entry-public"


def page(client, path, **kw):
    r = u.go(client, path=path, **kw)
    assert r.status_code == 200, "%s 期望 200，实得 %s" % (path, r.status_code)
    return r.get_data(as_text=True)


def entry_tags(html):
    """页面里所有指向 /provider 的链接的开标签。"""
    return [t for t in re.findall(r"<a\b[^>]*>", html) if 'href="/provider"' in t]


def card_html(html):
    """首页门户里那张指向 /provider 的卡片，从开标签到它的 </a>。找不到就失败。"""
    for m in re.finditer(r"<a\b[^>]*>", html):
        tag = m.group(0)
        if 'href="/provider"' in tag and "card" in tag:
            end = html.find("</a>", m.end())
            assert end != -1, "卡片没有闭合"
            return html[m.start(): end + len("</a>")]
    raise AssertionError("/hub 上没有指向 /provider 的卡片")


# ============================================================ 共用导航（七个页面）

@pytest.mark.parametrize("path", u.NAV_PAGES)
def test_导航里那颗旧系统供应商_链接属性与文字逐字符合约定(hub, path):
    assert u.NAV_ANCHOR in page(hub, path), "%s 的源码里没有逐字出现 %s" % (path, u.NAV_ANCHOR)


@pytest.mark.parametrize("path", u.NAV_PAGES)
def test_导航里旧系统这个分组字样还在(hub, path):
    """分组字样是单独一个元素的文字，不是「旧系统供应商」这几个字的前半截。"""
    assert re.search(r">\s*旧系统\s*<", page(hub, path)), "%s 的导航里没有「旧系统」分组" % path


@pytest.mark.parametrize("path", [p for p in u.NAV_PAGES if p != "/hub/provider"])
def test_每页仍恰好高亮一个导航项(hub, path):
    n = page(hub, path).count('class="chip on"')
    assert n == 1, "%s 里 class=\"chip on\" 应恰好一次，实得 %d 次" % (path, n)


@pytest.mark.parametrize("path", u.NAV_PAGES)
def test_页面源码里不出现8767(hub, path):
    assert "8767" not in page(hub, path), "%s 的源码里出现了旧系统那边的端口号" % path


@pytest.mark.parametrize("path", u.NAV_PAGES)
def test_指向旧地址的链接_每一处都在新标签页打开且不带回管理台的窗口与地址(hub, path):
    """不管属性怎么排：凡是指向 /provider 的链接，都得有 target=_blank 与 rel 的两个词。"""
    tags = entry_tags(page(hub, path))
    bad = [t for t in tags if 'target="_blank"' not in t or 'rel="noopener noreferrer"' not in t]
    assert tags and bad == [], "%s 上这些入口不合约定：%s（共找到 %d 处）" % (path, bad, len(tags))


@pytest.mark.parametrize("path", OTHER_NAV_PAGES)
def test_首页之外的页面_指向旧地址的链接只有导航里那一处(hub, path):
    assert len(entry_tags(page(hub, path))) == 1, entry_tags(page(hub, path))


def test_首页_指向旧地址的链接恰好两处_导航一处卡片一处(hub):
    assert len(entry_tags(page(hub, "/hub"))) == 2, entry_tags(page(hub, "/hub"))


# ============================================================ 首页门户的卡片

def test_首页卡片_开标签逐字符合约定(hub):
    assert u.CARD_OPEN_TAG in page(hub, "/hub")


def test_首页卡片_标题仍是旧系统供应商(hub):
    assert "旧系统供应商" in card_html(page(hub, "/hub"))


def test_首页卡片_说明文字逐字符合约定(hub):
    card = card_html(page(hub, "/hub"))
    assert u.CARD_TEXT in card, "卡片里的说明文字不对：%s" % card


def test_首页_不再出现原来那句说明(hub):
    assert u.CARD_OLD_TEXT not in page(hub, "/hub")


# ============================================================ 链接不随打开方式、配置变化

@pytest.mark.parametrize("path", ["/hub", "/hub/bots"])
def test_从公网地址打开页面_入口的写法与本机打开时一样(hub, path):
    local = entry_tags(page(hub, path, host=u.LOCAL_HOST))
    public = entry_tags(page(hub, path, host=u.PUBLIC_HOST, headers=dict(u.FORWARD_HEADERS)))
    assert local and public == local, "打开方式改变了入口的写法：本机 %s，公网 %s" % (local, public)


@pytest.mark.parametrize("path", ["/hub", "/hub/bots"])
def test_从公网地址打开页面_导航锚点照样逐字出现(hub, path):
    html = page(hub, path, host=u.PUBLIC_HOST, headers=dict(u.FORWARD_HEADERS))
    assert u.NAV_ANCHOR in html


@pytest.mark.parametrize("path", ["/hub", "/hub/bots"])
def test_从公网地址打开页面_源码里也不出现8767(hub, path):
    html = page(hub, path, host=u.PUBLIC_HOST, headers=dict(u.FORWARD_HEADERS))
    assert "8767" not in html


@pytest.mark.parametrize("path", ["/hub", "/hub/dsh-model"])
def test_配了自定义入口地址_页面源码里也不出现它(make_client, path):
    c = make_client(**{u.LOCAL_ENV: ODD_LOCAL, u.PUBLIC_ENV: ODD_PUBLIC})
    assert "zz-legacy-entry" not in page(c, path), "配置里的入口地址被写进了 %s 的源码" % path


def test_配了自定义入口地址_导航锚点仍是站内地址(make_client):
    c = make_client(**{u.LOCAL_ENV: ODD_LOCAL, u.PUBLIC_ENV: ODD_PUBLIC})
    assert u.NAV_ANCHOR in page(c, "/hub")
