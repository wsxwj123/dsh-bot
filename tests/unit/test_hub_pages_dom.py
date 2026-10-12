# -*- coding: utf-8 -*-
"""三个管理台页面在一棵最小的假 DOM 上实跑（INTERFACE-新系统bot运维 §13a 第 7 条、§13b、§13c、§13d）。

验收测试只按页面源码的片段断言，量不到「点了之后页面到底变成什么样、发了哪些请求」。上一轮
就是这样漏掉了三处：供应商页签点开是空白、读取失败后保存把人设清空、前一只的响应填进后一只
的弹窗。这里把渲染好的整页交给 node：

- 页面用 jinja 渲染（include 展开、导航里的分支都走真的），再解析成一棵元素树交给 node；
- node 里按文档顺序、在同一个全局里逐段执行页面的全部 <script>（与浏览器一样，连导航的
  hubApi 也是真的），只有 fetch、confirm、setTimeout 是记账的替身；
- 用例给一串步骤：点哪个按钮、往哪格填什么、放行哪个挂着的请求、拍一张快照。请求默认挂着，
  由用例决定谁先回，所以「甲的响应晚到」这种先后能摆出来。

假 DOM 只做了页面脚本用得到的那几样，另有两处照浏览器的规矩：禁用的按钮点不动；输入框会
规整换行（多行框把 CRLF 变成 LF，单行框丢掉换行）。innerHTML 只许清空，拼接会直接抛错。

机器上没有 node 时整个文件跳过。
"""
import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import jinja2
import pytest

TEMPLATES = Path(__file__).resolve().parents[2] / "moments" / "templates"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="这台机器没有 node，页面脚本跑不起来")

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
         "track", "wbr"}


class _Tree(HTMLParser):
    """渲染后的页面 → {"t": 标签, "a": 属性, "c": [子节点或文本]}。script 的正文原样保留。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {"t": "#root", "a": {}, "c": []}
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = {"t": tag, "a": {k: ("" if v is None else v) for k, v in attrs}, "c": []}
        self.stack[-1]["c"].append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1]["c"].append(
            {"t": tag, "a": {k: ("" if v is None else v) for k, v in attrs}, "c": []})

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i]["t"] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1]["c"].append(data)


DRIVER = r"""
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const log = {requests: [], confirms: [], errors: []};
process.on('unhandledRejection', (e) => { log.errors.push(String((e && e.stack) || e)); });

// ---------------------------------------------------------------- 假 DOM
function detach(n) {
  const p = n.parentNode;
  if (!p) return;
  p.childNodes.splice(p.childNodes.indexOf(n), 1);
  n.parentNode = null;
}
class TextNode {
  constructor(data) { this.nodeType = 3; this.data = data; this.parentNode = null; }
  get textContent() { return this.data; }
  cloneNode() { return new TextNode(this.data); }
  remove() { detach(this); }
}
function parseSel(sel) {
  if (sel[0] === '#') return (e) => e.attrs.id === sel.slice(1);
  const m = /^([a-zA-Z]+)?((?:\.[\w-]+)*)((?:\[[\w-]+(?:="[^"]*")?\])*)$/.exec(sel);
  if (!m) throw new Error('假 DOM 不认识这个选择器：' + sel);
  const tag = m[1] ? m[1].toUpperCase() : null;
  const classes = m[2] ? m[2].split('.').filter(Boolean) : [];
  const attrs = [...(m[3] || '').matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g)].map((a) => [a[1], a[2]]);
  return (e) => (!tag || e.tagName === tag) && classes.every((c) => e.classList.contains(c))
    && attrs.every(([k, v]) => (k in e.attrs) && (v === undefined || e.attrs[k] === v));
}
class El {
  constructor(tag) {
    this.nodeType = 1; this.tagName = String(tag).toUpperCase(); this.childNodes = [];
    this.parentNode = null; this.attrs = {}; this.style = {}; this.listeners = {};
    this.disabled = false; this.checked = false; this.open = false;
    this._value = this.tagName === 'SELECT' ? undefined : '';
  }
  get id() { return this.attrs.id || ''; }
  set id(v) { this.attrs.id = String(v); }
  get className() { return this.attrs.class || ''; }
  set className(v) { this.attrs.class = String(v); }
  get classList() {
    const self = this;
    const get = () => (self.attrs.class || '').split(/\s+/).filter(Boolean);
    const put = (list) => { self.attrs.class = list.join(' '); };
    return {
      contains: (n) => get().includes(n),
      add: (...ns) => put([...new Set([...get(), ...ns])]),
      remove: (...ns) => put(get().filter((x) => !ns.includes(x))),
      toggle: (n, force) => {
        const want = force === undefined ? !get().includes(n) : !!force;
        put(want ? [...new Set([...get(), n])] : get().filter((x) => x !== n));
        return want;
      },
    };
  }
  get hidden() { return 'hidden' in this.attrs; }
  set hidden(v) { if (v) this.attrs.hidden = ''; else delete this.attrs.hidden; }
  get value() {
    if (this.tagName !== 'SELECT' || this._value !== undefined) return this._value;
    const opts = this.querySelectorAll('option');
    const hit = opts.find((o) => o.selected) || opts[0];
    return hit ? hit.value : '';
  }
  set value(v) {
    v = String(v);
    if (this.tagName === 'TEXTAREA') v = v.replace(/\r\n?/g, '\n');   // 多行框：换行一律规整成 LF
    else if (this.tagName === 'INPUT') v = v.replace(/[\r\n]/g, '');   // 单行框：换行丢掉
    this._value = v;
  }
  get textContent() { return this.childNodes.map((c) => c.textContent).join(''); }
  set textContent(v) {
    this._clear();
    if (v !== null && v !== undefined && String(v) !== '') this.appendChild(new TextNode(String(v)));
  }
  set innerHTML(v) {
    if (v !== '') throw new Error('页面不许用 innerHTML 拼内容（假 DOM 只认 innerHTML = 空串）');
    this._clear();
  }
  _clear() { for (const c of this.childNodes) c.parentNode = null; this.childNodes = []; }
  get children() { return this.childNodes.filter((c) => c.nodeType === 1); }
  get firstChild() { return this.childNodes[0] || null; }
  get nextElementSibling() {
    const sib = this.parentNode ? this.parentNode.children : [];
    return sib[sib.indexOf(this) + 1] || null;
  }
  appendChild(n) {
    if (n.isFragment) { for (const c of [...n.childNodes]) this.appendChild(c); return n; }
    if (n.parentNode) n.remove();
    n.parentNode = this; this.childNodes.push(n);
    return n;
  }
  remove() { detach(this); }
  after(n) {
    const p = this.parentNode;
    if (n.parentNode) n.remove();
    n.parentNode = p; p.childNodes.splice(p.childNodes.indexOf(this) + 1, 0, n);
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  hasAttribute(k) { return k in this.attrs; }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  fire(type, init) {
    const ev = Object.assign({target: this, defaultPrevented: false}, init || {});
    ev.preventDefault = () => { ev.defaultPrevented = true; };
    const handler = this['on' + type];
    if (typeof handler === 'function') handler.call(this, ev);
    for (const fn of (this.listeners[type] || [])) fn.call(this, ev);
    return ev;
  }
  click() { if (this.disabled) return false; this.fire('click'); return true; }   // 禁用的按钮点不动
  _walk(out, test, intoTemplates) {
    for (const c of this.children) {
      if (test(c)) out.push(c);
      if (intoTemplates || c.tagName !== 'TEMPLATE') c._walk(out, test, intoTemplates);
    }
    return out;
  }
  querySelectorAll(sel) { return this._walk([], parseSel(sel), this.isFragment); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  cloneNode(deep) {
    const c = new El(this.tagName);
    c.attrs = {...this.attrs}; c.style = {...this.style}; c._value = this._value;
    c.disabled = this.disabled; c.checked = this.checked; c.selected = this.selected;
    if (deep) for (const k of this.childNodes) c.appendChild(k.cloneNode(true));
    return c;
  }
  get content() {       // <template>：正文是一份惰性的片段，克隆出来才进文档
    const self = this;
    return {cloneNode() {
      const frag = new El('#fragment');
      frag.isFragment = true;
      for (const k of self.childNodes) frag.appendChild(k.cloneNode(true));
      return frag;
    }};
  }
  showModal() {
    if (this.open) throw new Error('InvalidStateError：弹窗已经开着，又调了一次 showModal');
    this.open = true;
  }
  close() { this.open = false; }
  focus() {}
  scrollIntoView() {}
}
function build(node) {
  if (typeof node === 'string') return new TextNode(node);
  const e = new El(node.t);
  e.attrs = {...node.a};
  if ('value' in node.a) e._value = node.a.value;
  if ('disabled' in node.a) e.disabled = true;
  if ('selected' in node.a) e.selected = true;
  if ('checked' in node.a) e.checked = true;
  const display = /(?:^|;)\s*display\s*:\s*([\w-]+)/.exec(node.a.style || '');
  if (display) e.style.display = display[1];
  for (const c of node.c) e.appendChild(build(c));
  return e;
}
const root = build(input.tree);
const byId = (id) => root.querySelector('#' + id);
const need = (id) => { const e = byId(id); if (!e) throw new Error('页面里没有 #' + id); return e; };
const document = {
  getElementById: byId, createElement: (tag) => new El(tag),
  querySelectorAll: (sel) => root.querySelectorAll(sel), querySelector: (sel) => root.querySelector(sel),
};

// ---------------------------------------------------------------- 替身：网络、确认框、定时器
const pending = [];
const answer = (status, body) => ({
  status, headers: {get: () => null},
  json: async () => { if (body === undefined || body === null) throw new Error('不是 JSON'); return body; },
});
function fetch(url, opts) {
  const key = ((opts && opts.method) || 'GET') + ' ' + url;
  const auto = (input.auto || []).find((a) => a.req === key);
  if (!(auto && auto.quiet)) {
    log.requests.push({req: key, body: opts && opts.body !== undefined ? JSON.parse(opts.body) : null});
  }
  if (auto) return Promise.resolve(answer(auto.status, auto.body));
  return new Promise((resolve) => pending.push({key, resolve}));
}
let confirmAnswer = true;
const confirm = (text) => { log.confirms.push(text); return confirmAnswer; };
const timers = [];
const ctx = vm.createContext({document, fetch, confirm, console, URL, location: {href: ''},
                              setTimeout: (fn) => { timers.push(fn); return timers.length; }});

// ---------------------------------------------------------------- 观测
const visible = (e) => !e.hidden && !e.classList.contains('hidden');
function shown(e) {             // 自己与每一层祖先都没被藏起来
  for (let x = e; x && x.nodeType === 1; x = x.parentNode) {
    if (!visible(x) || x.style.display === 'none') return false;
  }
  return true;
}
function snapshot(st) {
  const snap = {requests: log.requests.map((r) => ({...r})), confirms: [...log.confirms],
                pending: pending.map((p) => p.key), els: {}, rows: {}};
  for (const id of st.ids || []) {
    const e = byId(id);
    snap.els[id] = e && {text: e.textContent, value: e.value === undefined ? null : e.value,
                         disabled: !!e.disabled, open: !!e.open, visible: visible(e), shown: shown(e),
                         cls: e.className, placeholder: e.attrs.placeholder || ''};
  }
  for (const spec of st.rows || []) {            // 「id」或「id 子选择器」（如 steps tbody）
    const [id, sub] = spec.split(' ');
    const box = sub ? need(id).querySelector(sub) : need(id);
    snap.rows[spec] = box.children.map((tr) => ({
      cells: tr.children.map((td) => td.textContent),
      buttons: tr.querySelectorAll('button').map((b) => ({text: b.textContent, disabled: !!b.disabled})),
      pills: tr.querySelectorAll('span').map((p) => ({text: p.textContent, cls: p.className})),
    }));
  }
  return snap;
}
function pick(st) {             // 点谁：按 id；或某个容器第 row 个子元素里文案是 text 的那枚按钮
  if (st.id) return need(st.id);
  const row = need(st.in).children[st.row];
  if (!row) throw new Error('#' + st.in + ' 里没有第 ' + st.row + ' 行');
  const btn = row.querySelectorAll('button').find((b) => b.textContent === st.text);
  if (!btn) throw new Error('那一行里没有文案为「' + st.text + '」的按钮');
  return btn;
}

const tick = () => new Promise((resolve) => setImmediate(resolve));
const settle = async () => { for (let i = 0; i < 10; i++) await tick(); };
(async () => {
  const out = {};
  try {
    for (const s of root._walk([], (e) => e.tagName === 'SCRIPT', true)) vm.runInContext(s.textContent, ctx);
    await settle();
    for (const st of input.steps) {
      if (st.op === 'click') {
        const hit = pick(st).click();
        if (st.as) out[st.as] = hit;
      } else if (st.op === 'set') {
        need(st.id).value = st.value;
      } else if (st.op === 'fire') {
        const e = need(st.id);
        const ev = e.fire(st.type, {target: e});
        if (st.type === 'cancel' && !ev.defaultPrevented) e.close();     // Esc 的默认动作
      } else if (st.op === 'call') {
        vm.runInContext(st.fn, ctx)(...(st.args || []));
      } else if (st.op === 'respond') {
        const i = pending.findIndex((p) => p.key === st.req);
        if (i < 0) throw new Error('没有挂着的请求 ' + st.req + '；挂着的有：' + pending.map((p) => p.key).join('、'));
        pending.splice(i, 1)[0].resolve(answer(st.status, st.body));
      } else if (st.op === 'timers') {
        for (const fn of timers.splice(0)) fn();
      } else if (st.op === 'confirm') {
        confirmAnswer = st.answer;
      } else if (st.op === 'clear') {
        log.requests.length = 0; log.confirms.length = 0;
      } else if (st.op === 'snap') {
        out[st.as] = snapshot(st);
      } else {
        throw new Error('不认识的步骤：' + JSON.stringify(st));
      }
      await settle();
    }
    console.log(JSON.stringify({ok: true, out, errors: log.errors}));
  } catch (e) {
    console.log(JSON.stringify({ok: false, error: String((e && e.stack) || e), errors: log.errors}));
  }
})();
"""

_ENV = jinja2.Environment(loader=jinja2.FileSystemLoader(str(TEMPLATES)), autoescape=True)
_NAV = {"hub_bots.html": "bots", "hub_dsh_model.html": "dsh-model", "hub_addbot.html": "addbot"}
#: 导航条自己拉的那一下，与被测的事无关：自动应答，不记账
_QUIET = {"req": "GET /hub/api/setup/status", "status": 200, "quiet": True,
          "body": {"token": True, "userid": True, "deepseek": True}}


def _run(tmp_path, page, steps, auto=()):
    """渲染 page，在 node 里把整页脚本跑起来，按 steps 驱动，返回各个快照。"""
    tree = _Tree()
    tree.feed(_ENV.get_template(page).render(nav_active=_NAV[page]))
    driver, spec = tmp_path / "driver.js", tmp_path / "spec.json"
    driver.write_text(DRIVER, encoding="utf-8")
    spec.write_text(json.dumps({"tree": tree.root, "steps": steps, "auto": [_QUIET, *auto]},
                               ensure_ascii=False), encoding="utf-8")
    r = subprocess.run([NODE, str(driver), str(spec)], stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    got = json.loads(r.stdout.decode("utf-8").strip().splitlines()[-1])
    assert got["ok"], got.get("error")
    assert got["errors"] == [], got["errors"]
    return got["out"]


def _snap(name, *ids, rows=()):
    return {"op": "snap", "as": name, "ids": list(ids), "rows": list(rows)}


def _respond(req, status, body=None):
    return {"op": "respond", "req": req, "status": status, "body": body}


# ============================================================ 三页各自的假后端与入口

BOTS, MODEL, ADDBOT = "hub_bots.html", "hub_dsh_model.html", "hub_addbot.html"


def _bot_row(bot_id, state):
    return {"id": bot_id, "display_name": bot_id, "port": 18001, "state": state, "phase": None,
            "pid": None, "queue_depth": None, "in_flight": None}


def _bots_list(rows, disabled=()):
    """GET /hub/api/bots 的 200。rows 是 [(id, state), …]，都算新系统来源。"""
    return {"req": "GET /hub/api/bots", "status": 200, "body": {
        "bots": [_bot_row(i, s) for i, s in rows], "disabled": list(disabled),
        "sources": {i: {"source": "dsh", "shadowed": False} for i, _ in rows},
        "overrides": [], "restart_available": True, "restart_reason": None}}


def _model_list(*bot_ids):
    """GET /hub/api/dsh-model 的 200：每只 bot 一行，外加一个自建供应商。"""
    cur = {"provider": "builtin", "model": "m1"}
    return {"req": "GET /hub/api/dsh-model", "status": 200, "body": {
        "bots": [{"id": i, "name": i, "current": cur, "config": cur, "choices": []} for i in bot_ids],
        "providers": [{"name": "myproxy", "source": "custom", "api": "openai-completions",
                       "models": 1, "key": "ok", "base_url": "https://p.example.com/v1",
                       "model_list": [{"id": "m1", "contextWindow": 1000}]}]}}


#: 编辑弹窗在两个页面上各跑一遍：打开它的那枚「编辑」在哪个表、保存成功的话显示在哪、重拉哪个列表
EDIT_PAGES = {
    "bots": {"page": BOTS, "rows": "bot-rows", "say": "restart-msg", "reload": "GET /hub/api/bots",
             "auto": [_bots_list([("bota", "online"), ("botb", "online")])]},
    "dsh-model": {"page": MODEL, "rows": "rows", "say": "msg", "reload": "GET /hub/api/dsh-model",
                  "auto": [_model_list("bota", "botb")]},
}
EDIT_IDS = ("editDialog", "edit-who", "edit-name", "edit-persona", "edit-voice", "edit-msg",
            "edit-save", "edit-cancel")
READ_A, READ_B = "GET /hub/api/bots/bota/edit", "GET /hub/api/bots/botb/edit"
SAVE_A, SAVE_B = "POST /hub/api/bots/bota/edit", "POST /hub/api/bots/botb/edit"
GOT_A = {"bot_id": "bota", "display_name": "甲", "persona": "甲的人设", "fish_voice_id": "voicea"}
GOT_B = {"bot_id": "botb", "display_name": "乙", "persona": "乙的人设", "fish_voice_id": "voiceb"}
SAVED = {"ok": True, "saved": {"display_name": True, "persona": False, "fish_voice_id": False},
         "text": "已保存。名字与音色立即生效；人设会被在跑的 bot 在几秒内检测到，空闲时自动换上。"}


@pytest.fixture(params=sorted(EDIT_PAGES))
def edit(request, tmp_path):
    """返回 (这一页的参数, 跑步骤的函数)。步骤里用 open_(行号) 点那一行的「编辑」。"""
    cfg = EDIT_PAGES[request.param]

    def run(steps):
        return _run(tmp_path, cfg["page"], steps, auto=cfg["auto"])

    cfg = dict(cfg, open=lambda row: {"op": "click", "in": cfg["rows"], "row": row, "text": "编辑"},
               snap=lambda name: _snap(name, *EDIT_IDS, cfg["say"], rows=[cfg["rows"]]))
    return cfg, run


def _fields(snap):
    return tuple(snap["els"][i]["value"] for i in ("edit-name", "edit-persona", "edit-voice"))


def _posts(snap):
    return [r for r in snap["requests"] if r["req"].startswith("POST ")]


def _row_edit_disabled(snap, cfg, row=0):
    """那一行的「编辑」（打开弹窗的那枚行内按钮）现在是不是禁用的。"""
    return {b["text"]: b["disabled"] for b in snap["rows"][cfg["rows"]][row]["buttons"]}["编辑"]


# ============================================================ §13b 模型页：两个面板恰好一个看得见

def test_模型页_点供应商页签后恰好供应商面板可见_面板里的按钮真的露出来(tmp_path):
    panes = ("pane-model", "pane-providers", "tab-model", "tab-providers", "add-open", "refresh")
    out = _run(tmp_path, MODEL, [
        _snap("load", *panes),
        {"op": "click", "id": "tab-providers"}, _snap("providers", *panes),
        {"op": "click", "id": "tab-model"}, _snap("model", *panes),
        {"op": "click", "id": "tab-providers"}, {"op": "click", "id": "tab-providers"},
        _snap("twice", *panes),
    ], auto=[_model_list("bota")])
    seen = {k: (v["els"]["pane-model"]["visible"], v["els"]["pane-providers"]["visible"])
            for k, v in out.items()}
    assert seen == {"load": (True, False), "providers": (False, True), "model": (True, False),
                    "twice": (False, True)}
    # 面板里的东西跟着面板走：「添加供应商」在供应商面板里，bot 表的「刷新」在模型面板里
    assert [out[k]["els"]["add-open"]["shown"] for k in ("load", "providers", "model")] == [False, True, False]
    assert [out[k]["els"]["refresh"]["shown"] for k in ("load", "providers", "model")] == [True, False, True]
    # 亮着的页签跟着换
    assert out["providers"]["els"]["tab-providers"]["cls"] == "btn mini"
    assert out["providers"]["els"]["tab-model"]["cls"] == "btn mini ghost"


def test_模型页_供应商面板里的行与操作按钮渲染得出来(tmp_path):
    """面板看得见之后里面得有东西：自建供应商那一行带着刷新、测试连接、管理、编辑、删除。"""
    out = _run(tmp_path, MODEL, [{"op": "click", "id": "tab-providers"}, _snap("s", "prows", rows=["prows"])],
               auto=[_model_list("bota")])
    assert out["s"]["els"]["prows"]["shown"] is True
    assert [b["text"] for b in out["s"]["rows"]["prows"][0]["buttons"]] == [
        "刷新模型列表", "测试连接", "管理", "编辑", "删除"]


# ============================================================ §13c 编辑弹窗：读

def test_编辑弹窗_读取没返回时保存是禁用的_点了也不发请求(edit):
    cfg, run = edit
    out = run([cfg["open"](0), cfg["snap"]("reading"),
               {"op": "click", "id": "edit-save", "as": "clicked"}, cfg["snap"]("after")])
    els = out["reading"]["els"]
    assert els["editDialog"]["open"] is True and els["edit-who"]["text"] == "bota"
    assert els["edit-save"]["disabled"] is True
    assert els["edit-msg"]["text"] == "读取中…"
    assert _fields(out["reading"]) == ("", "", "")
    assert out["clicked"] is False and _posts(out["after"]) == []
    assert out["reading"]["pending"] == [READ_A]


@pytest.mark.parametrize("status,body,reason", [
    (404, {"error": "bot_not_found", "detail": "没有这个新系统的 bot（这一页只管新系统的 bot）"},
     "没有这个新系统的 bot（这一页只管新系统的 bot）"),
    (409, {"error": "bad_yaml"}, "bad_yaml"),
    (500, None, "读取失败（500）"),
], ids=["detail", "error-only", "status-only"])
def test_编辑弹窗_读取失败_保存保持禁用_提示说明读不出(edit, status, body, reason):
    cfg, run = edit
    out = run([cfg["open"](0), _respond(READ_A, status, body), cfg["snap"]("failed"),
               # 以前出事的那条路：读失败后填个名字点保存，空着的人设框把人设写成空文件
               {"op": "set", "id": "edit-name", "value": "新名字"},
               {"op": "click", "id": "edit-save", "as": "clicked"},
               {"op": "call", "fn": "saveEdit"},          # 绕过按钮直接调也一样不发
               cfg["snap"]("after")])
    els = out["failed"]["els"]
    assert els["edit-msg"]["text"] == "读不出这只 bot 现在的内容，所以不能在这里改：" + reason
    assert els["edit-save"]["disabled"] is True and els["editDialog"]["open"] is True
    assert _fields(out["failed"]) == ("", "", "")
    assert out["clicked"] is False
    assert _posts(out["after"]) == [], "读取失败之后不许有任何保存请求发出去"
    assert out["after"]["els"]["edit-save"]["disabled"] is True


def test_编辑弹窗_读取成功_回填三格_保存可点_消息清空(edit):
    cfg, run = edit
    out = run([cfg["open"](0), _respond(READ_A, 200, GOT_A), cfg["snap"]("ok")])
    els = out["ok"]["els"]
    assert _fields(out["ok"]) == ("甲", "甲的人设", "voicea")
    assert els["edit-save"]["disabled"] is False and els["edit-msg"]["text"] == ""
    assert _row_edit_disabled(out["ok"], cfg) is True, "弹窗开着的时候，打开它的那枚行内按钮是禁用的"


# ============================================================ §13c 编辑弹窗：响应要对得上号

def test_编辑弹窗_关掉甲立刻开乙_甲的响应晚到_弹窗里是乙的内容(edit):
    cfg, run = edit
    out = run([cfg["open"](0), {"op": "click", "id": "edit-cancel"}, cfg["open"](1),
               _respond(READ_B, 200, GOT_B), _respond(READ_A, 200, GOT_A), cfg["snap"]("s"),
               {"op": "set", "id": "edit-name", "value": "乙改"},
               {"op": "click", "id": "edit-save"}, cfg["snap"]("saving")])
    els = out["s"]["els"]
    assert els["edit-who"]["text"] == "botb" and _fields(out["s"]) == ("乙", "乙的人设", "voiceb")
    assert els["edit-save"]["disabled"] is False and els["edit-msg"]["text"] == ""
    # 存的也是乙，而且原值是乙的：只改了名字，就只交名字
    assert _posts(out["saving"]) == [{"req": SAVE_B, "body": {"display_name": "乙改"}}]


def test_编辑弹窗_甲的响应先到也不许填_保存仍禁用_等乙自己的响应(edit):
    cfg, run = edit
    out = run([cfg["open"](0), {"op": "click", "id": "edit-cancel"}, cfg["open"](1),
               _respond(READ_A, 200, GOT_A), cfg["snap"]("stale"),
               _respond(READ_B, 200, GOT_B), cfg["snap"]("fresh")])
    stale = out["stale"]["els"]
    assert _fields(out["stale"]) == ("", "", ""), "甲的内容被填进了乙的弹窗"
    assert stale["edit-save"]["disabled"] is True and stale["edit-msg"]["text"] == "读取中…"
    assert _fields(out["fresh"]) == ("乙", "乙的人设", "voiceb")
    assert out["fresh"]["els"]["edit-save"]["disabled"] is False


def test_编辑弹窗_乙读取失败_晚到的甲成功了也不许放开保存(edit):
    cfg, run = edit
    out = run([cfg["open"](0), {"op": "click", "id": "edit-cancel"}, cfg["open"](1),
               _respond(READ_B, 500), _respond(READ_A, 200, GOT_A), cfg["snap"]("s")])
    els = out["s"]["els"]
    assert _fields(out["s"]) == ("", "", "") and els["edit-save"]["disabled"] is True
    assert els["edit-msg"]["text"] == "读不出这只 bot 现在的内容，所以不能在这里改：读取失败（500）"


def test_编辑弹窗_关掉之后才到的响应整个丢掉(edit):
    cfg, run = edit
    out = run([cfg["open"](0), {"op": "click", "id": "edit-cancel"},
               _respond(READ_A, 200, GOT_A), cfg["snap"]("s")])
    els = out["s"]["els"]
    assert els["editDialog"]["open"] is False and _fields(out["s"]) == ("", "", "")
    assert els["edit-save"]["disabled"] is True
    assert _row_edit_disabled(out["s"], cfg) is False


# ============================================================ §13c 编辑弹窗：只提交改过的键

@pytest.mark.parametrize("edits,want", [
    ({"edit-name": "新名字"}, {"display_name": "新名字"}),
    ({"edit-persona": "新人设"}, {"persona": "新人设"}),
    ({"edit-voice": "voice2"}, {"fish_voice_id": "voice2"}),
    ({"edit-voice": ""}, {"fish_voice_id": ""}),                      # 清空音色 = 删掉它
    ({"edit-name": "新名字", "edit-voice": "voice2"}, {"display_name": "新名字", "fish_voice_id": "voice2"}),
], ids=["name", "persona", "voice", "clear-voice", "name+voice"])
def test_编辑弹窗_只提交改过的键(edit, edits, want):
    cfg, run = edit
    steps = [cfg["open"](0), _respond(READ_A, 200, GOT_A)]
    steps += [{"op": "set", "id": k, "value": v} for k, v in edits.items()]
    out = run(steps + [{"op": "click", "id": "edit-save"}, cfg["snap"]("s")])
    assert _posts(out["s"]) == [{"req": SAVE_A, "body": want}]
    assert out["s"]["els"]["edit-msg"]["text"] == "保存中…"
    assert out["s"]["els"]["edit-save"]["disabled"] is True, "保存还没回来，不许连点"


def test_编辑弹窗_一个都没改_不发请求_显示没有改动(edit):
    cfg, run = edit
    out = run([cfg["open"](0), _respond(READ_A, 200, GOT_A), {"op": "click", "id": "edit-save"},
               cfg["snap"]("untouched"),
               {"op": "set", "id": "edit-name", "value": "改了"}, {"op": "set", "id": "edit-name", "value": "甲"},
               {"op": "click", "id": "edit-save"}, cfg["snap"]("reverted")])
    for name in ("untouched", "reverted"):
        assert _posts(out[name]) == [], name
        assert out[name]["els"]["edit-msg"]["text"] == "没有改动"
        assert out[name]["els"]["editDialog"]["open"] is True
        assert out[name]["els"]["edit-save"]["disabled"] is False


def test_编辑弹窗_输入框规整过的人设不算改过(edit):
    """人设文件是 CRLF 换行时，多行框读回来是 LF。原值若记的是接口给的原串，没动过的人设
    每次都会被当成改过交上去。原值与现值都从输入框里读，就比得平。"""
    cfg, run = edit
    got = dict(GOT_A, persona="第一行\r\n第二行\r\n")
    out = run([cfg["open"](0), _respond(READ_A, 200, got),
               {"op": "set", "id": "edit-name", "value": "新名字"},
               {"op": "click", "id": "edit-save"}, cfg["snap"]("s")])
    assert out["s"]["els"]["edit-persona"]["value"] == "第一行\n第二行\n"
    assert _posts(out["s"]) == [{"req": SAVE_A, "body": {"display_name": "新名字"}}]


def test_编辑弹窗_配置里没写名字时不动名字就不交名字(edit):
    """读到的名字是空串：以前照交，后端按「名字不能为空」拒掉，连人设都存不了。"""
    cfg, run = edit
    out = run([cfg["open"](0), _respond(READ_A, 200, dict(GOT_A, display_name="")),
               {"op": "set", "id": "edit-persona", "value": "新人设"},
               {"op": "click", "id": "edit-save"}, cfg["snap"]("s")])
    assert _posts(out["s"]) == [{"req": SAVE_A, "body": {"persona": "新人设"}}]


# ============================================================ §13c 编辑弹窗：存

@pytest.mark.parametrize("reply,said", [
    (SAVED, "bota：" + SAVED["text"]),
    ({"ok": True}, "bota：已保存"),
], ids=["with-text", "no-text"])
def test_编辑弹窗_保存成功_弹窗关闭_页面消息位给提示_列表重拉(edit, reply, said):
    cfg, run = edit
    out = run([cfg["open"](0), _respond(READ_A, 200, GOT_A),
               {"op": "set", "id": "edit-name", "value": "新名字"}, {"op": "clear"},
               {"op": "click", "id": "edit-save"}, _respond(SAVE_A, 200, reply), cfg["snap"]("s")])
    els = out["s"]["els"]
    assert els["editDialog"]["open"] is False
    assert els[cfg["say"]]["text"] == said
    assert [r["req"] for r in out["s"]["requests"]] == [SAVE_A, cfg["reload"]]


@pytest.mark.parametrize("status,body,shown", [
    (400, {"error": "bad_field", "detail": "人设正文不能为空；不想改人设就不要带 persona 这个键"},
     "人设正文不能为空；不想改人设就不要带 persona 这个键"),
    (409, {"error": "bad_access_json"}, "bad_access_json"),
    (500, None, "保存失败（500）"),
], ids=["detail", "error-only", "status-only"])
def test_编辑弹窗_保存失败_弹窗不关_显示原因_保存恢复可点(edit, status, body, shown):
    cfg, run = edit
    out = run([cfg["open"](0), _respond(READ_A, 200, GOT_A),
               {"op": "set", "id": "edit-persona", "value": "  "},
               {"op": "click", "id": "edit-save"}, _respond(SAVE_A, status, body), cfg["snap"]("failed"),
               {"op": "set", "id": "edit-persona", "value": "改好了"},
               {"op": "click", "id": "edit-save"}, cfg["snap"]("retry")])
    els = out["failed"]["els"]
    assert els["editDialog"]["open"] is True and els["edit-msg"]["text"] == shown
    assert els["edit-save"]["disabled"] is False
    assert els["edit-persona"]["value"] == "  ", "失败之后输入不许丢"
    assert els[cfg["say"]]["text"] == "", "没存成，页面消息位不该出现成功提示"
    assert _posts(out["retry"])[-1] == {"req": SAVE_A, "body": {"persona": "改好了"}}


def test_编辑弹窗_保存途中被关掉又开了另一只_晚到的保存结果不动现在的弹窗(edit):
    cfg, run = edit
    out = run([cfg["open"](0), _respond(READ_A, 200, GOT_A),
               {"op": "set", "id": "edit-name", "value": "新名字"}, {"op": "click", "id": "edit-save"},
               {"op": "fire", "id": "editDialog", "type": "cancel"},        # 保存还没回来就按了 Esc
               cfg["open"](1), _respond(READ_B, 200, GOT_B),
               _respond(SAVE_A, 200, SAVED), cfg["snap"]("s")])
    els = out["s"]["els"]
    assert els["editDialog"]["open"] is True, "甲的保存结果把乙的弹窗关掉了"
    assert els["edit-who"]["text"] == "botb" and _fields(out["s"]) == ("乙", "乙的人设", "voiceb")
    assert els["edit-save"]["disabled"] is False
    assert els[cfg["say"]]["text"] == "bota：" + SAVED["text"], "甲确实存成了，页面要知道"


# ============================================================ §13c 编辑弹窗：关

def test_编辑弹窗_点弹窗外面不关_输入还在(edit):
    cfg, run = edit
    out = run([cfg["open"](0), _respond(READ_A, 200, GOT_A),
               {"op": "set", "id": "edit-persona", "value": "写了很长的一篇"},
               {"op": "fire", "id": "editDialog", "type": "click"},         # 点到遮罩：目标是弹窗自己
               cfg["snap"]("s")])
    assert out["s"]["els"]["editDialog"]["open"] is True
    assert out["s"]["els"]["edit-persona"]["value"] == "写了很长的一篇"


@pytest.mark.parametrize("close", [{"op": "click", "id": "edit-cancel"},
                                   {"op": "fire", "id": "editDialog", "type": "cancel"}],
                         ids=["cancel-button", "esc"])
def test_编辑弹窗_取消与Esc能关_零保存请求_行内按钮还回去(edit, close):
    cfg, run = edit
    out = run([cfg["open"](0), _respond(READ_A, 200, GOT_A),
               {"op": "set", "id": "edit-name", "value": "不想存"}, close, cfg["snap"]("s")])
    assert out["s"]["els"]["editDialog"]["open"] is False
    assert _posts(out["s"]) == []
    assert _row_edit_disabled(out["s"], cfg) is False


def test_编辑弹窗_关了再开同一只_重新读取_不沿用上一次的内容(edit):
    cfg, run = edit
    out = run([cfg["open"](0), _respond(READ_A, 200, GOT_A),
               {"op": "set", "id": "edit-name", "value": "没存的草稿"},
               {"op": "click", "id": "edit-cancel"}, cfg["open"](0), cfg["snap"]("reopened")])
    assert _fields(out["reopened"]) == ("", "", "")
    assert out["reopened"]["els"]["edit-save"]["disabled"] is True
    assert out["reopened"]["pending"] == [READ_A]


# ============================================================ §13a 第 7 条：「再停一次」

ROWS = [("on_up", "online"), ("off_up", "online"), ("off_starting", "starting"),
        ("off_down", "offline"), ("off_unknown", "unknown")]
OFF = ["off_up", "off_starting", "off_down", "off_unknown"]
ENABLED_URL = "POST /hub/api/bots/off_up/enabled"


def test_bot状态页_再停一次只出现在停用而进程还在的行(tmp_path):
    out = _run(tmp_path, BOTS, [_snap("s", rows=["bot-rows"])], auto=[_bots_list(ROWS, OFF)])
    buttons = {row["cells"][0]: [b["text"] for b in row["buttons"]] for row in out["s"]["rows"]["bot-rows"]}
    assert buttons == {
        "on_up": ["编辑", "重启", "停止"],                    # 启用的：没有
        "off_up": ["编辑", "再停一次", "启用"],               # 停用、在线：有
        "off_starting": ["编辑", "再停一次", "启用"],         # 停用、启动中：有
        "off_down": ["编辑", "启用"],                         # 停用、离线：没有
        "off_unknown": ["编辑", "启用"],
    }


def test_bot状态页_点再停一次_确认框与停止相同_只对这一只再交一次停用(tmp_path):
    started = {"ok": True, "enabled": False, "sync": {"state": "started", "job_id": "abcd1234", "started_at": 1}}
    out = _run(tmp_path, BOTS, [
        {"op": "clear"},
        {"op": "click", "in": "bot-rows", "row": 1, "text": "再停一次"},
        _respond(ENABLED_URL, 200, started), _snap("s", "restart-msg"),
    ], auto=[_bots_list(ROWS, OFF)])
    assert out["s"]["confirms"] == ["停止 off_up？只停这一只，别的 bot 不受影响；它正在处理的对话会被打断。"]
    assert out["s"]["requests"][0] == {"req": ENABLED_URL, "body": {"enabled": False}}
    assert [r["req"] for r in out["s"]["requests"]] == [
        ENABLED_URL, "GET /hub/api/bots", "GET /hub/api/bots/restart/abcd1234"]
    assert "POST /hub/api/bots/restart" not in [r["req"] for r in out["s"]["requests"]]
    assert out["s"]["els"]["restart-msg"]["text"] == "off_up 的配置已改成停用，正在同步这一只…"


def test_bot状态页_再停一次的确认框点了取消_一个请求都不发(tmp_path):
    out = _run(tmp_path, BOTS, [
        {"op": "clear"}, {"op": "confirm", "answer": False},
        {"op": "click", "in": "bot-rows", "row": 1, "text": "再停一次"}, _snap("s", "restart-msg"),
    ], auto=[_bots_list(ROWS, OFF)])
    assert len(out["s"]["confirms"]) == 1 and out["s"]["requests"] == []


def test_bot状态页_再停一次又被挡住_提示仍指回这一行的再停一次(tmp_path):
    busy = {"ok": True, "enabled": False, "sync": {"state": "busy", "job_id": "abcd1234"}}
    out = _run(tmp_path, BOTS, [
        {"op": "click", "in": "bot-rows", "row": 1, "text": "再停一次"},
        _respond(ENABLED_URL, 200, busy), _snap("s", "restart-msg", rows=["bot-rows"]),
    ], auto=[_bots_list(ROWS, OFF)])
    assert out["s"]["els"]["restart-msg"]["text"] == (
        "off_up 的配置已改成停用，但这一只还没停下来：有别的重启任务正在跑。"
        "等它结束后刷新，在这一行点「再停一次」。")
    assert "再停一次" in [b["text"] for b in out["s"]["rows"]["bot-rows"][1]["buttons"]], \
        "提示让用户点的那个按钮，重拉之后得还在那一行"


# ============================================================ §13d 添加 bot 页

CREATE = "POST /hub/api/bots"
TEMPLATES_OK = {"req": "GET /hub/api/bots/templates", "status": 200, "body": {"templates": []}}
STEPS = [{"name": "validate", "state": "done", "detail": ""},
         {"name": "getme", "state": "done", "detail": "@xiaoyu_bot"},
         {"name": "files", "state": "done", "detail": ""},
         {"name": "register", "state": "failed", "detail": "bun 没装"},
         {"name": "restart", "state": "skipped", "detail": ""},
         {"name": "future_step", "state": "weird", "detail": "后端以后新加的"},
         {"name": "constructor", "state": "toString", "detail": ""}]


def test_添加bot页_端口一栏的占位符不再是旧系统某只bot的端口(tmp_path):
    out = _run(tmp_path, ADDBOT, [_snap("s", "dispatcher_port")], auto=[TEMPLATES_OK])
    assert out["s"]["els"]["dispatcher_port"]["placeholder"] == "1024-65535，每只 bot 各不相同"


def test_添加bot页_创建中的提示_成功后的提示_步骤表显示中文(tmp_path):
    out = _run(tmp_path, ADDBOT, [
        {"op": "click", "id": "create"}, _snap("creating", "create-msg", "create", "hint"),
        _respond(CREATE, 201, {"ok": True, "bot_id": "xiaoyu", "username": "xiaoyu_bot", "steps": STEPS}),
        _snap("done", "create-msg", "create", "hint", "steps"),
    ], auto=[TEMPLATES_OK])
    assert out["creating"]["els"]["create-msg"]["text"] == "创建中…（要等它启动完成，最多半分钟）"
    assert out["creating"]["els"]["create"]["disabled"] is True
    done = out["done"]["els"]
    assert done["create-msg"]["text"] == "创建成功：xiaoyu（@xiaoyu_bot）"
    assert done["hint"]["text"] == "它已经在跑了，去「bot 状态」页就能看到。"
    assert done["create"]["disabled"] is False and done["steps"]["shown"] is True


def test_添加bot页_步骤表_内部名换成中文_不认识的原样显示(tmp_path):
    out = _run(tmp_path, ADDBOT, [
        {"op": "click", "id": "create"},
        _respond(CREATE, 207, {"ok": True, "bot_id": "xiaoyu", "steps": STEPS,
                               "manual_hint": "launchctl bootstrap …"}),
        _snap("s", "hint", "create-msg"),
        _snap("table", rows=["steps tbody"]),
    ], auto=[TEMPLATES_OK])
    rows = out["table"]["rows"]["steps tbody"]
    assert [r["cells"] for r in rows] == [
        ["校验填写内容", "完成", ""],
        ["向 Telegram 核对 token", "完成", "@xiaoyu_bot"],
        ["写配置与频道目录", "完成", ""],
        ["装开机自启并启动", "失败", "bun 没装"],
        ["等它启动完成", "跳过", ""],
        ["future_step", "weird", "后端以后新加的"],
        ["constructor", "toString", ""],            # 对照表只认自己的键，不会取到原型上的东西
    ]
    # 状态那一格的颜色仍按后端的内部状态定：只有 done 是绿的
    assert [r["pills"][0]["cls"] for r in rows] == ["pill on", "pill on", "pill on", "pill off",
                                                    "pill off", "pill off", "pill off"]
    assert out["s"]["els"]["hint"]["text"] == "launchctl bootstrap …"
