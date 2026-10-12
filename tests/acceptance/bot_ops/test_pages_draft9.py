# -*- coding: utf-8 -*-
"""第 9 稿 §13a 第 7 条：「再停一次」按钮，停用的行恒有、不看状态；启用的行没有。

用户看得见的结果：一只 bot 停用之后，不管它那一行显示的是在线、启动中、离线还是状态未知，
这一行都有一个「再停一次」，能把它停干净；启用着的 bot 那一行没有这个按钮。

第 8 稿只在「停用而且在线或启动中」的行给这个按钮。漏掉的情形是：配置写着停用，启动项
文件却还装着，进程此刻恰好没在跑，这一行显示「已停用、离线」，页面上没有再停一次的入口。

断言手段照 §13a 第 7 条写明的「把页面脚本交给 node 跑」：
1. 经测试客户端取 /hub/bots 的页面源码；
2. 交给假浏览器 `fakes/page_dom_runner.js` 整页跑一遍，页面去拉 `GET /hub/api/bots` 时，
   答给它一份按 §2 形状造的列表；
3. 看跑完之后页面画出来的结构：某只 bot 那一行里有哪些看得见的按钮。

「某只 bot 那一行」的认法不靠页面内部的写法：在画出来的结构里，找只提到这一只、没提到
列表里别的 bot 的最大那一块（表格里就是它那一行）。「看得见」= 元素自己和这一块里它的
上级都没有 `hidden` 属性、没有 `display:none`。机器上没有 node 时这几条跳过。

测不到的：真实浏览器里按钮的样子与位置；点了之后发什么请求、弹什么确认框（这里只量按钮
在不在）。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from . import botops_util as u

BOTS = "/hub/bots"
RUNNER = Path(__file__).resolve().parent / "fakes" / "page_dom_runner.js"
STATES = ["online", "starting", "offline", "unknown"]      # §2 的四种 state
AGAIN = "再停一次"


# ------------------------------------------------------------ 按 §2 的形状造列表

def _bot_row(bot_id, state, port):
    """§2 的一行，恒为 8 键。unknown 就是端口查不到；online、starting 才有 pid。"""
    return {"id": bot_id, "display_name": bot_id,
            "port": None if state == "unknown" else port,
            "state": state, "phase": None,
            "pid": 4242 if state in ("online", "starting") else None,
            "queue_depth": None, "in_flight": None}


def _listing(rows, disabled):
    return {"bots": rows, "disabled": list(disabled),
            "sources": {r["id"]: {"source": "dsh", "shadowed": False} for r in rows},
            "overrides": [], "restart_available": True, "restart_reason": None}


def _eight_rows():
    """八只：停用的四只（dis_*）与启用的四只（ena_*），四种状态各一，停用与启用交错着排。"""
    rows, disabled = [], []
    for i, state in enumerate(STATES):
        rows.append(_bot_row("dis_%s" % state, state, 17951 + 2 * i))
        rows.append(_bot_row("ena_%s" % state, state, 17952 + 2 * i))
        disabled.append("dis_%s" % state)
    return _listing(rows, disabled)


# ------------------------------------------------------------ 读假浏览器交回来的结构

def _blob(node):
    """这棵子树里所有的文字与属性值（判「这一块提到了哪几只 bot」用）。"""
    if "text" in node:
        return node["text"]
    return "\n".join(list(node["attrs"].values()) + [_blob(c) for c in node["children"]])


def _row_blocks(node, target, ids):
    """只提到 target 这一只的最大那几块。提到了别的 bot 的块往下拆，直到只剩它自己。"""
    if "text" in node:
        return []
    blob = _blob(node)
    seen = {i for i in ids if i in blob}
    if target not in seen:
        return []
    if seen == {target}:
        return [node]
    out = []
    for child in node["children"]:
        out += _row_blocks(child, target, ids)
    return out


def _text(node):
    return node["text"] if "text" in node else "".join(_text(c) for c in node["children"])


def _hidden(node, hidden_class):
    attrs = node["attrs"]
    if "hidden" in attrs or re.search(r"(^|;)\s*display\s*:\s*none", attrs.get("style", "")):
        return True
    return hidden_class and "hidden" in attrs.get("class", "").split()


def _buttons(node, hidden_class):
    """这一块里看得见的按钮的文案，按出现顺序。"""
    if "text" in node or _hidden(node, hidden_class):
        return []
    attrs = node["attrs"]
    if node["tag"] == "button" or attrs.get("role") == "button":
        return [" ".join(_text(node).split())]
    if node["tag"] == "input" and attrs.get("type", "").lower() in ("button", "submit"):
        return [" ".join(attrs.get("value", "").split())]
    out = []
    for child in node["children"]:
        out += _buttons(child, hidden_class)
    return out


class _Page:
    def __init__(self, got, html, ids):
        self.tree = got["tree"]
        self.requests = got["requests"]
        self.errors = got["errors"]
        self.missing = got["missing"]
        self.patched = got["patched"]
        self.ids = list(ids)
        # 页面自己把 hidden 类定义成「不显示」时，带这个类的也算看不见
        self.hidden_class = bool(re.search(r"\.hidden\s*\{[^}]*display\s*:\s*none", html))

    def buttons(self, bot_id):
        """这只 bot 那一行里看得见的按钮文案。找不到带按钮的那一行就直接红（不让用例假绿）。"""
        assert len(self.ids) >= 2, "列表里至少要两只 bot：只有一只时，分不出哪一块是它那一行"
        blocks = [b for b in _row_blocks(self.tree, bot_id, self.ids) if _buttons(b, self.hidden_class)]
        assert blocks, (
            "页面画出来的东西里没有 %s 带按钮的那一行。\n页面发过的请求：%s\n页面脚本报的错：%s\n"
            "没找到的入口函数：%s\n假浏览器补的全局：%s\n页面上的文字（前 400 字）：%s"
            % (bot_id, self.requests, self.errors, self.missing, self.patched,
               " ".join(_text(self.tree).split())[:400]))
        out = []
        for block in blocks:
            out += _buttons(block, self.hidden_class)
        return out


@pytest.fixture
def render(web_client, tmp_path):
    """把 /hub/bots 整页交给 node 跑，列表请求答 `listing`，返回跑完之后的页面。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("这台机器上没有 node，§13a 第 7 条「把页面脚本交给 node 跑」的断言跑不了")

    def run(listing):
        r = web_client.get(BOTS)
        assert r.status_code == 200, "%s 期望 200，实得 %s" % (BOTS, r.status_code)
        html = r.get_data(as_text=True)
        external = {}
        for src in re.findall(r"""<script\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", html):
            if src.startswith("/") and not src.startswith("//"):         # 只取同站的，不出本进程
                got = web_client.get(src)
                if got.status_code == 200:
                    external[src] = got.get_data(as_text=True)
        data, out = tmp_path / "page_in.json", tmp_path / "page_out.json"
        data.write_text(json.dumps({
            "html": html, "path": BOTS, "external": external, "call": ["loadBots"],
            "routes": [{"method": "GET", "path": "/hub/api/bots", "status": 200, "body": listing}],
        }), encoding="utf-8")
        p = subprocess.run([node, str(RUNNER), str(data), str(out)],
                           capture_output=True, text=True, timeout=60)
        assert out.is_file(), "node 没有给出结果（退出码 %s）：%s" % (p.returncode, p.stderr[-600:])
        got = json.loads(out.read_text(encoding="utf-8"))
        assert got.get("ok"), "页面在 node 里没跑完：%s" % got.get("error")
        return _Page(got, html, [b["id"] for b in listing["bots"]])

    return run


# ============================================================ §13a 第 7 条的可断言写法

@pytest.mark.parametrize("state", STATES)
def test_bot状态页_停用的行_不管什么状态都有再停一次(render, state):
    got = render(_eight_rows()).buttons("dis_%s" % state)
    assert AGAIN in got, (
        "停用、state 是 %s 的这一行没有「再停一次」：这一只没法在页面上停干净。这一行的按钮：%s"
        % (state, got))


@pytest.mark.parametrize("state", STATES)
def test_bot状态页_启用的行_不管什么状态都没有再停一次(render, state):
    got = render(_eight_rows()).buttons("ena_%s" % state)
    assert AGAIN not in got, (
        "启用、state 是 %s 的这一行出现了「再停一次」。这一行的按钮：%s" % (state, got))


# ============================================================ 第 9 稿要堵的那个口子，从后端一路量到页面

def test_bot状态页_配置停用而启动项还装着进程没在跑_列表说离线_这一行有再停一次(
        web_client, sandbox, launchctl, bun, as_darwin, render):
    """列表不是造的：配置、启动项文件、假 launchctl 摆成那个样子，拿真实的列表响应去画页面。

    另放一只启用着的 otherbot 作对照：它那一行没有这个按钮。
    """
    u.dsh_bot(sandbox.dsh_cfg, "xiaoyu", enabled=False)
    u.dsh_bot(sandbox.dsh_cfg, "otherbot", dispatcher_port=17952)
    for unit in ("com.dsh-bot.xiaoyu", "com.dsh-bot.self-initiate.xiaoyu"):
        (sandbox.agents / (unit + ".plist")).write_text("<plist/>\n", encoding="utf-8")
    launchctl.set_default(installed=True, running=False, pid=None)   # launchd 里还装载着，进程没在跑
    r = web_client.get("/hub/api/bots")
    assert r.status_code == 200, (r.status_code, r.get_json(silent=True))
    listing = r.get_json()
    rows = [b for b in listing["bots"] if b["id"] == "xiaoyu"]
    assert len(rows) == 1 and rows[0]["state"] == "offline" and "xiaoyu" in listing["disabled"], (
        "前提不成立：这一行应当是「已停用、离线」", listing)
    assert "otherbot" not in listing["disabled"], ("前提不成立：对照那只应当是启用的", listing)
    page = render(listing)
    got = page.buttons("xiaoyu")
    assert AGAIN in got, (
        "已停用、离线、启动项还装着的这一行没有「再停一次」：它一恢复或下次登录就会跑起来，"
        "页面上却没有入口把它停干净。这一行的按钮：%s" % got)
    assert AGAIN not in page.buttons("otherbot"), "启用着的对照那一行出现了「再停一次」"


# ============================================================ 反向：画列表不等于动手

def test_bot状态页_画列表的时候只读不写_没有发出任何会改状态的请求(render):
    page = render(_eight_rows())
    page.buttons("dis_offline")                 # 先确认行真的画出来了，下面的「没发」才有意义
    assert {"method": "GET", "path": "/hub/api/bots"} in page.requests, (
        "页面没有去拉列表", page.requests)
    writes = [q for q in page.requests if q["method"] != "GET"]
    assert writes == [], ("页面只是画个列表，却发出了会改状态的请求", writes)
