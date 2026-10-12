# -*- coding: utf-8 -*-
"""§13a bot 状态页：开关之后只动那一只（接口约定第 6 稿追加）+ §13 第 6 稿取代的四处锚点。

BRIEF 2026-10-12 追加第 2 条：页面上点了启用或停止之后，不许再去触发「重启全部」；
点之前那句确认的话要说成只影响这一只；同步结果做成了、失败了、被挡住没做，各是各的说法。

断言手段照 §13a 写明的：经测试客户端取 /hub/bots 的页面源码，按函数名、固定文案片段、
元素 id 断言，不需要真实浏览器。「函数体」的切法也照 §13a：从该函数的 function 那一行起，
到它后面第一个顶层函数定义（行首是 `function ` 或 `async function `）之前。

测不到的（没有 JS 运行时）：点了之后浏览器到底发没发 POST /hub/api/bots/restart、
三句话在什么时候显示、轮询的节奏。这里只能断言「源码里没有那个调用」「那几句话写在了
该写的函数里」。
"""
import html as _html
import re

import pytest

BOTS = "/hub/bots"
_TOP_FN = re.compile(r"^(?:async[ \t]+)?function[ \t]+([A-Za-z_$][\w$]*)[ \t]*\(", re.M)


def _page(client):
    r = client.get(BOTS)
    assert r.status_code == 200, "%s 期望 200，实得 %s" % (BOTS, r.status_code)
    return r.get_data(as_text=True)


def _has(html, piece):
    """文案在不在页面里：原样找一遍，再按 HTML 反转义后的文本找一遍。

    正文里的文案可能被模板转义（& 变 &amp; 之类），脚本里的不会；两种都认。
    """
    return piece in html or piece in _html.unescape(html)


def _top_functions(html):
    return [(m.start(), m.group(1)) for m in _TOP_FN.finditer(html)]


def _fn_body(html, name):
    """§13a 的切法。找不到这个顶层函数就直接红，并报出页面里有哪些顶层函数。"""
    tops = _top_functions(html)
    for i, (pos, fn) in enumerate(tops):
        if fn == name:
            end = tops[i + 1][0] if i + 1 < len(tops) else len(html)
            return html[pos:end]
    pytest.fail("页面源码里没有顶层函数 %s（行首 function / async function 的定义）；"
                "现有的顶层函数：%s" % (name, "、".join(fn for _, fn in tops) or "一个都没有"))


# ============================================================ 确认框只说这一只

@pytest.mark.parametrize("piece", ["只停这一只", "只启动这一只", "别的 bot 不受影响"],
                         ids=["stop-only-this", "start-only-this", "others-untouched"])
def test_页面_确认框的固定片段在(web_client, piece):
    assert _has(_page(web_client), piece), "确认框缺固定片段「%s」" % piece


# ============================================================ 开关成功后不发起重启全部

def test_页面_setEnabled函数体里把结果交给showSync(web_client):
    assert "showSync(" in _fn_body(_page(web_client), "setEnabled"), \
        "setEnabled 里没有 showSync( —— 开关成功后应当交给 showSync 分三路显示"


def test_页面_setEnabled函数体里不再调startRestart(web_client):
    """旧行为：开关成功后调 startRestart() 重启全部。第 6 稿取代。"""
    assert "startRestart(" not in _fn_body(_page(web_client), "setEnabled"), \
        "setEnabled 里还在调 startRestart( —— 点一只的开关会把别的 bot 一起重启"


def test_页面_showSync是顶层函数(web_client):
    names = [fn for _, fn in _top_functions(_page(web_client))]
    assert "showSync" in names, "缺 showSync（开关成功后的唯一显示入口）；现有：%s" % names


def test_页面_showSync函数体里用pollJob轮询(web_client):
    assert "pollJob(" in _fn_body(_page(web_client), "showSync"), \
        "showSync 里没有 pollJob( —— started 那一路要拿 sync.job_id 去轮询"


def test_页面_showSync函数体里不调startRestart(web_client):
    assert "startRestart(" not in _fn_body(_page(web_client), "showSync"), \
        "showSync 里出现了 startRestart( —— 同步结果的显示不许触发重启全部"


# ============================================================ 三种同步状态各自的文案

@pytest.mark.parametrize("piece,state", [
    pytest.param("正在同步这一只", "started", id="started"),
    # 第 8 稿 §13a 第 2 条：busy 由一句拆成两句，各指一个只动这一只的入口；「还没同步」被取代
    pytest.param("还没停下来", "busy（这次是停用）", id="busy-not-stopped"),
    pytest.param("还没拉起来", "busy（这次是启用）", id="busy-not-started"),
    pytest.param("有别的重启任务正在跑", "busy", id="busy-other-job"),
    pytest.param("没有自动同步", "skipped", id="skipped"),
])
def test_页面_showSync函数体里有这一路的固定片段(web_client, piece, state):
    assert _has(_fn_body(_page(web_client), "showSync"), piece), \
        "showSync 里缺 sync.state=%s 那一路的固定片段「%s」" % (state, piece)


@pytest.mark.parametrize("piece", ["同步完成", "同步失败", "同步超时"],
                         ids=["done", "failed", "timeout"])
def test_页面_轮询终态用同步这一套说法(web_client, piece):
    assert _has(_page(web_client), piece), "页面源码缺「%s」" % piece


# ============================================================ 被取代的锚点不得再出现

@pytest.mark.parametrize("gone,what", [
    pytest.param("改完会立即重启", "旧确认框", id="old-confirm"),
    pytest.param("正在重启使其生效", "旧成功提示", id="old-success"),
    pytest.param("那一步现在还没接上", "页头折叠说明里过时的一句", id="old-header-note"),
])
def test_页面_被取代的文案不再出现(web_client, gone, what):
    assert not _has(_page(web_client), gone), "页面源码里还留着%s：「%s」" % (what, gone)


def test_页面_页头折叠说明改成只动这一只的说法(web_client):
    assert _has(_page(web_client), "点「启用」会把配置改回来，并只把这一只拉起来；点「停止」只停这一只。"), \
        "页头折叠说明没有换成第 6 稿那一句"


# ============================================================ 没改的那部分照旧

@pytest.mark.parametrize("piece", ["重启中…", "任务状态读不到了"],
                         ids=["restarting", "job-unreadable"])
def test_页面_重启全部与行内重启的那几句照旧(web_client, piece):
    assert _has(_page(web_client), piece), "页面源码缺既有的「%s」" % piece


@pytest.mark.parametrize("piece", ["重启完成", "重启失败", "重启超时"],
                         ids=["restart-done", "restart-failed", "restart-timeout"])
def test_页面_重启的结束提示用重启这一套说法(web_client, piece):
    """第 8 稿 §13a 第 5 条：此前是「重启结束：done」这种内部词，改成人话。"""
    assert _has(_page(web_client), piece), "页面源码缺「%s」" % piece


def test_页面_三种显示写在既有的restart_msg里_元素还在(web_client):
    assert 'id="restart-msg"' in _page(web_client), "缺 #restart-msg"


def test_页面_startRestart的函数定义保留(web_client):
    names = [fn for _, fn in _top_functions(_page(web_client))]
    assert "startRestart" in names, "startRestart 的定义不见了（重启全部按钮还要用它）；现有：%s" % names
