# -*- coding: utf-8 -*-
"""第 8 稿的页面体验：§13（被取代的锚点）、§13a 第 5、7 条、§13b、§13c、§13d。

断言手段照接口约定写明的：经测试客户端取页面源码，按函数体、固定片段、元素写法断言。
「函数体」的切法照 §13a：从该函数的 function 那一行起，到它后面第一个顶层函数定义
（行首是 `function ` 或 `async function `）之前。

§13b 第 4 条写明可以「把页面脚本交给 node 跑」：这里只把 `switchTab` 这一个函数交给 node，
页面元素的初始状态取自初始 HTML（哪些带 `hidden` 属性、哪些带 `hidden` 类）。机器上没有
node 时这几条跳过。

测不到的（没有浏览器）：弹窗真的居中没有、读取失败后保存按钮真的点不动没有、甲的响应晚到
时弹窗里填的是谁的内容、保存时到底发了哪几个键、「再停一次」按钮出现在哪些行。这些只能
断言「该写的东西写在了该写的函数里」。
"""
import html as _html
import json
import re
import shutil
import subprocess
from html.parser import HTMLParser

import pytest

BOTS = "/hub/bots"
ADDBOT = "/hub/addbot"
DSHMODEL = "/hub/dsh-model"

_TOP_FN = re.compile(r"^(?:async[ \t]+)?function[ \t]+([A-Za-z_$][\w$]*)[ \t]*\(", re.M)


def _page(client, path):
    r = client.get(path)
    assert r.status_code == 200, "%s 期望 200，实得 %s" % (path, r.status_code)
    return r.get_data(as_text=True)


def _has(html, piece):
    """文案在不在页面里：原样找一遍，再按 HTML 反转义后的文本找一遍。"""
    return piece in html or piece in _html.unescape(html)


def _fn_body(html, name):
    """§13a 的切法。找不到这个顶层函数就直接红，并报出页面里有哪些顶层函数。"""
    tops = [(m.start(), m.group(1)) for m in _TOP_FN.finditer(html)]
    for i, (pos, fn) in enumerate(tops):
        if fn == name:
            end = tops[i + 1][0] if i + 1 < len(tops) else len(html)
            return html[pos:end]
    pytest.fail("页面源码里没有顶层函数 %s（行首 function / async function 的定义）；"
                "现有的顶层函数：%s" % (name, "、".join(fn for _, fn in tops) or "一个都没有"))


# ============================================================ §13 第 8 稿取代的锚点：不得再出现

@pytest.mark.parametrize("gone", ["重启结束", "点「重启全部 bot」补上", "还没同步"],
                         ids=["restart-finished", "use-restart-all", "not-synced"])
def test_bot状态页_被取代的文案不再出现(web_client, gone):
    assert not _has(_page(web_client, BOTS), gone), "/hub/bots 的源码里还留着「%s」" % gone


@pytest.mark.parametrize("gone", [
    pytest.param("com.claudebotlife.moments-web", id="old-unit-name"),
    pytest.param("重启它新 bot 才可见", id="restart-to-see"),
    pytest.param("configs/&lt;id&gt;.yml", id="old-config-path"),
    pytest.param("channels/&lt;id&gt;/.env", id="old-env-path"),
    pytest.param('placeholder="17802"', id="old-port-placeholder"),
    pytest.param("dispatcher 端口", id="dispatcher-port"),
    pytest.param("要等重启后探活", id="wait-probe"),
    pytest.param("最多 64 KiB", id="64KiB"),
])
def test_添加bot页_被取代的文案不再出现(web_client, gone):
    assert gone not in _page(web_client, ADDBOT), "/hub/addbot 的源码里还留着「%s」" % gone


def test_模型页_供应商面板不再用hidden属性起头(web_client):
    assert '<div id="pane-providers" hidden>' not in _page(web_client, DSHMODEL), \
        "供应商面板还是用 hidden 属性藏的：页签切的是 hidden 类，点开会是空白"


@pytest.mark.parametrize("path", [BOTS, DSHMODEL], ids=["bots", "dsh-model"])
@pytest.mark.parametrize("gone", ["e.target === $('editDialog')", "最多 64 KiB",
                                  "persona: $('edit-persona').value"],
                         ids=["click-outside-closes", "64KiB", "always-send-persona"])
def test_编辑弹窗_被取代的写法不再出现(web_client, path, gone):
    assert gone not in _page(web_client, path), "%s 的源码里还留着「%s」" % (path, gone)


# ============================================================ §13a 第 5 条：重启的结束提示

@pytest.mark.parametrize("piece", ["重启完成", "重启失败", "重启超时", "等它结束再试", "重启没有受理"],
                         ids=["done", "failed", "timeout", "busy-wait", "not-accepted"])
def test_bot状态页_重启提示的固定片段在(web_client, piece):
    assert _has(_page(web_client, BOTS), piece), "/hub/bots 缺固定片段「%s」" % piece


# ============================================================ §13a 第 2、7 条：同步被挡时的指引

@pytest.mark.parametrize("piece", ["还没停下来", "还没拉起来", "有别的重启任务正在跑", "再停一次"],
                         ids=["not-stopped", "not-started", "other-job", "stop-again"])
def test_bot状态页_showSync函数体里有被挡时两个方向的固定片段(web_client, piece):
    assert _has(_fn_body(_page(web_client, BOTS), "showSync"), piece), \
        "showSync 里缺 busy 那一路的固定片段「%s」" % piece


def test_bot状态页_actsCell函数体里渲染再停一次(web_client):
    assert _has(_fn_body(_page(web_client, BOTS), "actsCell"), "再停一次"), \
        "actsCell 里没有「再停一次」：停用没生效时，这一行没有只动这一只的补救入口"


# ============================================================ §13b 模型页两个面板：显隐只用一种办法

def test_模型页_两个面板的起始标签逐字(web_client):
    html = _page(web_client, DSHMODEL)
    assert '<div id="pane-model">' in html, "模型面板的起始标签不是 <div id=\"pane-model\">"
    assert '<div id="pane-providers" class="hidden">' in html, \
        "供应商面板的起始标签不是 <div id=\"pane-providers\" class=\"hidden\">"


def test_模型页_样式里有hidden类的定义(web_client):
    assert ".hidden{display:none !important;}" in _page(web_client, DSHMODEL)


@pytest.mark.parametrize("piece", ["$('pane-model').classList.toggle('hidden'",
                                   "$('pane-providers').classList.toggle('hidden'"],
                         ids=["pane-model", "pane-providers"])
def test_模型页_switchTab对两个面板用同一种切法(web_client, piece):
    assert piece in _fn_body(_page(web_client, DSHMODEL), "switchTab"), "switchTab 里缺 %s" % piece


def test_模型页_switchTab函数体里不出现hidden属性的赋值(web_client):
    assert ".hidden =" not in _fn_body(_page(web_client, DSHMODEL), "switchTab"), \
        "switchTab 里还在写 .hidden =（属性），与 hidden 类混用"


# ---- §13b 第 4 条：把 switchTab 交给 node 跑

class _Ids(HTMLParser):
    """初始 HTML 里每个带 id 的元素：它的类名，以及带没带 hidden 属性。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.els = {}

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if d.get("id"):
            self.els.setdefault(d["id"], {"cls": (d.get("class") or "").split(),
                                          "hiddenAttr": "hidden" in d})

    handle_startendtag = handle_starttag


def _js_function(src, name):
    """从页面源码里把 `function <name>(…){…}` 这一个函数原样取出来（按花括号配对）。"""
    m = re.search(r"^(?:async[ \t]+)?function[ \t]+%s[ \t]*\(" % re.escape(name), src, re.M)
    if not m:
        pytest.fail("页面源码里没有顶层函数 %s" % name)
    i = src.index("{", m.end())
    depth, j, n = 0, i, len(src)
    while j < n:
        c = src[j]
        if c in "'\"`":                                 # 字符串：跳到配对的引号
            q, j = c, j + 1
            while j < n and src[j] != q:
                j += 2 if src[j] == "\\" else 1
        elif src.startswith("//", j):
            j = src.find("\n", j)
            if j < 0:
                break
        elif src.startswith("/*", j):
            j = src.find("*/", j) + 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
        j += 1
    pytest.fail("没能把函数 %s 的花括号配上对" % name)


_RUNNER = r"""
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
function anything() {
  const f = function () { return anything(); };
  return new Proxy(f, {
    get: (t, k) => (k === 'then' ? undefined : k === Symbol.toPrimitive ? () => '' : anything()),
    set: () => true,
  });
}
const els = {};
function makeEl(id, init) {
  const cls = new Set(init.cls), attrs = new Set(init.hiddenAttr ? ['hidden'] : []);
  const own = {
    id, __cls: cls, __attrs: attrs, style: {}, dataset: {},
    classList: {
      add: (...n) => n.forEach(x => cls.add(x)),
      remove: (...n) => n.forEach(x => cls.delete(x)),
      contains: n => cls.has(n),
      toggle: (n, force) => {
        const want = force === undefined ? !cls.has(n) : !!force;
        if (want) cls.add(n); else cls.delete(n);
        return want;
      },
    },
    setAttribute: k => { attrs.add(k); },
    removeAttribute: k => { attrs.delete(k); },
    hasAttribute: k => attrs.has(k),
    getAttribute: k => (attrs.has(k) ? '' : null),
    toggleAttribute: (k, force) => {
      const want = force === undefined ? !attrs.has(k) : !!force;
      if (want) attrs.add(k); else attrs.delete(k);
      return want;
    },
  };
  Object.defineProperty(own, 'hidden', {
    get: () => attrs.has('hidden'),
    set: v => { if (v) attrs.add('hidden'); else attrs.delete('hidden'); },
  });
  Object.defineProperty(own, 'className', {
    get: () => [...cls].join(' '),
    set: v => { cls.clear(); String(v).split(/\s+/).filter(Boolean).forEach(x => cls.add(x)); },
  });
  return new Proxy(own, { get: (t, k) => (k in t ? t[k] : anything()), set: (t, k, v) => { t[k] = v; return true; } });
}
const byId = id => (id in input.els ? (els[id] || (els[id] = makeEl(id, input.els[id]))) : null);
const storage = { getItem: () => null, setItem() {}, removeItem() {} };
const known = {
  $: byId,
  document: new Proxy({ getElementById: byId, querySelectorAll: () => [],
                        querySelector: s => (/^#[\w-]+$/.test(s) ? byId(s.slice(1)) : null) },
                      { get: (t, k) => (k in t ? t[k] : anything()) }),
  localStorage: storage, sessionStorage: storage, console,
};
const scope = new Proxy({}, {
  has: () => true,
  get: (t, k) => (k === Symbol.unscopables ? undefined
                  : k in known ? known[k] : k in globalThis ? globalThis[k] : anything()),
  set: (t, k, v) => { known[k] = v; return true; },
});
const visible = id => { const e = byId(id); return !!e && !e.__attrs.has('hidden') && !e.__cls.has('hidden'); };
const snap = after => ({ after, model: visible('pane-model'), providers: visible('pane-providers') });
try {
  const fn = new Function('scope', 'with (scope) { ' + input.fn + '\n; return switchTab; }')(scope);
  const out = [snap('load')];
  for (const name of input.calls) { fn(name); out.push(snap(name)); }
  console.log(JSON.stringify({ ok: true, steps: out }));
} catch (e) {
  console.log(JSON.stringify({ ok: false, error: String(e && e.message) }));
}
"""


@pytest.fixture
def tab_states(web_client, tmp_path):
    """按顺序调 `switchTab(...)`，返回每一步之后两个面板看不看得见。

    「看得见」的判据照 §13b 第 4 条：元素既没有 `hidden` 属性，也没有 `hidden` 类。
    返回 `[{"after": "load"|<页签名>, "model": bool, "providers": bool}, …]`，第一项是载入后。
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("这台机器上没有 node，§13b 第 4 条的行为断言跑不了")

    def run(*calls):
        html = _page(web_client, DSHMODEL)
        ids = _Ids()
        ids.feed(html)
        for need in ("pane-model", "pane-providers"):
            assert need in ids.els, "初始 HTML 里没有 #%s" % need
        runner, data = tmp_path / "run_switch_tab.js", tmp_path / "switch_tab_input.json"
        runner.write_text(_RUNNER, encoding="utf-8")
        data.write_text(json.dumps({"fn": _js_function(html, "switchTab"), "els": ids.els,
                                    "calls": list(calls)}), encoding="utf-8")
        p = subprocess.run([node, str(runner), str(data)], capture_output=True, text=True, timeout=30)
        lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
        assert lines, "node 没有给出结果（退出码 %s）" % p.returncode
        got = json.loads(lines[-1])
        assert got.get("ok"), "switchTab 在 node 里没跑完：%s" % got.get("error")
        return got["steps"]

    return run


def test_模型页_载入后模型面板看得见供应商面板看不见(tab_states):
    load = tab_states()[0]
    assert (load["model"], load["providers"]) == (True, False), load


def test_模型页_点供应商页签之后供应商面板看得见(tab_states):
    """必修第一条：「供应商」页签点开不能是空白。"""
    after = tab_states("providers")[-1]
    assert after["providers"] is True, ("点了「供应商」，供应商面板还是看不见", after)


def test_模型页_点供应商页签之后模型面板看不见(tab_states):
    after = tab_states("providers")[-1]
    assert after["model"] is False, after


def test_模型页_再点回模型页签_回到载入时的样子(tab_states):
    steps = tab_states("providers", "model")
    assert (steps[-1]["model"], steps[-1]["providers"]) == (steps[0]["model"], steps[0]["providers"]), steps


def test_模型页_来回切页签_任何时刻恰好一个面板看得见(tab_states):
    steps = tab_states("providers", "model", "providers", "providers", "model")
    bad = [s for s in steps if (s["model"], s["providers"]).count(True) != 1]
    assert bad == [], ("有的时刻两个面板都看得见或都看不见", bad)


# ============================================================ §13c 编辑弹窗：读与存

EDIT_PAGES = pytest.mark.parametrize("path", [BOTS, DSHMODEL], ids=["bots", "dsh-model"])


@EDIT_PAGES
def test_编辑弹窗_openEdit函数体里一打开就把保存按钮禁用(web_client, path):
    assert "$('edit-save').disabled = true" in _fn_body(_page(web_client, path), "openEdit"), \
        "%s：openEdit 里没有 $('edit-save').disabled = true —— 读取回来之前保存还能点" % path


@EDIT_PAGES
def test_编辑弹窗_打开时显示读取中(web_client, path):
    assert _has(_page(web_client, path), "读取中…"), "%s 缺「读取中…」" % path


@EDIT_PAGES
def test_编辑弹窗_有给每次打开记序号的计数器editSeq(web_client, path):
    assert "editSeq" in _page(web_client, path), \
        "%s 缺 editSeq —— 前一只的响应晚到会被填进后一只的弹窗" % path


@EDIT_PAGES
def test_编辑弹窗_saveEdit函数体里有没有改动这一路(web_client, path):
    assert _has(_fn_body(_page(web_client, path), "saveEdit"), "没有改动"), \
        "%s：saveEdit 里没有「没有改动」—— 一个都没改时不该发请求" % path


@EDIT_PAGES
def test_编辑弹窗_saveEdit函数体里不再无条件提交人设(web_client, path):
    assert "persona: $('edit-persona').value" not in _fn_body(_page(web_client, path), "saveEdit"), \
        "%s：saveEdit 还是每次把人设那格原样交上去" % path


@EDIT_PAGES
@pytest.mark.parametrize("piece", ["读不出这只 bot 现在的内容", "已保存"],
                         ids=["read-failed", "saved"])
def test_编辑弹窗_读取失败与保存成功的提示在(web_client, path, piece):
    assert _has(_page(web_client, path), piece), "%s 缺「%s」" % (path, piece)


@EDIT_PAGES
def test_编辑弹窗_页面自己定义了onBotEdited(web_client, path):
    assert "function onBotEdited(text)" in _page(web_client, path), \
        "%s 缺 function onBotEdited(text) —— 保存成功后的提示没有出口" % path


@EDIT_PAGES
@pytest.mark.parametrize("el", ["editDialog", "edit-who", "edit-name", "edit-persona", "edit-voice",
                                "edit-msg", "edit-save", "edit-cancel"])
def test_编辑弹窗_既有的元素id都保留(web_client, path, el):
    assert ('id="%s"' % el) in _page(web_client, path), "%s 缺 #%s" % (path, el)


# ============================================================ §13c 编辑弹窗：版式

@EDIT_PAGES
@pytest.mark.parametrize("piece", [
    pytest.param("dialog{margin:auto;", id="centered"),
    pytest.param("width:min(720px,92vw)", id="width"),
    pytest.param("max-height:88vh", id="max-height"),
    pytest.param("overflow:auto", id="inner-scroll"),
    pytest.param('rows="16"', id="persona-rows"),
    pytest.param("min-height:320px", id="persona-min-height"),
    pytest.param("resize:vertical", id="persona-resizable"),
    pytest.param("人设正文（最多 65536 个字）", id="persona-caption"),
])
def test_编辑弹窗_版式的写法逐字出现(web_client, path, piece):
    assert piece in _page(web_client, path), "%s 的源码里缺 %s" % (path, piece)


def test_添加bot页_也带着全站的弹窗居中样式(web_client):
    """居中写在全站公共样式里，别的页同样受益。"""
    assert "dialog{margin:auto;" in _page(web_client, ADDBOT)


# ============================================================ §13d 添加 bot 页的文案

@pytest.mark.parametrize("piece", [
    pytest.param("填完点创建：配置与频道目录建在新系统这边，装好开机自启并把它拉起来；建完就能在 bot 状态页看到。",
                 id="subtitle"),
    pytest.param("Telegram token（只写进这只 bot 自己频道目录里的 .env，配置文件里不留）", id="token-label"),
    pytest.param("本机端口（这只 bot 的网关用，只在本机监听）", id="port-label"),
    pytest.param("1024-65535，每只 bot 各不相同", id="port-placeholder"),
    pytest.param("管理台和定时任务靠这个端口找到这只 bot。挑一个没被别的 bot 或本机其它程序占用的，撞了创建时会提示。",
                 id="port-help"),
    pytest.param("人设正文（最多 65536 个字）", id="persona-label"),
    pytest.param("创建中…（要等它启动完成，最多半分钟）", id="creating"),
    pytest.param("它已经在跑了，去「bot 状态」页就能看到。", id="created-hint"),
])
def test_添加bot页_改过的文字逐字出现(web_client, piece):
    assert _has(_page(web_client, ADDBOT), piece), "/hub/addbot 缺「%s」" % piece


@pytest.mark.parametrize("piece", ["校验填写内容", "向 Telegram 核对 token", "写配置与频道目录",
                                   "装开机自启并启动", "等它启动完成"],
                         ids=["validate", "getme", "files", "register", "restart"])
def test_添加bot页_步骤名的中文逐字出现(web_client, piece):
    assert _has(_page(web_client, ADDBOT), piece), "/hub/addbot 缺步骤名「%s」" % piece


@pytest.mark.parametrize("piece", ["完成", "失败", "跳过"], ids=["done", "failed", "skipped"])
def test_添加bot页_步骤状态的中文逐字出现(web_client, piece):
    assert _has(_page(web_client, ADDBOT), piece), "/hub/addbot 缺步骤状态「%s」" % piece


@pytest.mark.parametrize("const", ["STEP_NAME", "STEP_STATE"])
def test_添加bot页_renderSteps函数体里用到对照表常量(web_client, const):
    assert const in _fn_body(_page(web_client, ADDBOT), "renderSteps"), \
        "renderSteps 里没有用到 %s —— 步骤表显示的还是英文内部名" % const
