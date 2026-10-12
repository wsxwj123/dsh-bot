'use strict';
/*
 * 假浏览器（测试侧假件，不是测试用例）：把管理台某一页的脚本整页跑一遍，交回跑完之后的页面结构。
 *
 * 用法：node page_dom_runner.js <输入.json> <输出.json>
 *
 * 输入：
 *   html      经测试客户端取到的页面源码（原样）
 *   external  {src: 脚本内容}，页面用 <script src> 引的同站脚本（测试侧事先取好）
 *   routes    [{method, path, status, body}]，页面发请求时照这张表作答；表里没有的一律答 200 与 {}
 *   call      页面载入之后再依次调用的全局函数名（接口约定写明的入口，例如 loadBots）
 * 输出：
 *   ok        跑没跑完
 *   tree      跑完之后 <body> 的结构：元素是 {tag, attrs, children}，文字是 {text}；脚本与样式的内容不带
 *   requests  页面发过的请求 [{method, path}]，按先后
 *   errors    页面脚本抛出来的错（含没人接的 Promise 拒绝）
 *   patched   页面用到、这里没有的浏览器全局名（自动补成了什么都不做的替身）
 *
 * 只认页面对外的东西：页面源码、它发的请求、它最后画出来的结构。不依赖页面内部的函数签名。
 * 不发任何网络请求、不起子进程、不写输出文件之外的任何东西。
 */
const fs = require('fs');
const vm = require('vm');

const input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const OUT = process.argv[3];

const VOID = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta',
  'source', 'track', 'wbr']);
const RAW = new Set(['script', 'style', 'textarea', 'title']);
const IMPLIED_END = {
  tr: ['tr', 'td', 'th'], td: ['td', 'th'], th: ['td', 'th'], li: ['li'], option: ['option'],
  tbody: ['tbody', 'thead', 'tfoot', 'tr', 'td', 'th'], thead: ['tbody', 'thead', 'tfoot', 'tr', 'td', 'th'],
};
const ENT = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' };

function decode(s) {
  return String(s).replace(/&(#[xX][0-9a-fA-F]+|#[0-9]+|amp|lt|gt|quot|apos|nbsp);/g, (m, e) => {
    if (e[0] !== '#') return ENT[e];
    const hex = e[1] === 'x' || e[1] === 'X';
    return String.fromCodePoint(parseInt(e.slice(hex ? 2 : 1), hex ? 16 : 10));
  });
}
const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const escAttr = s => esc(s).replace(/"/g, '&quot;');
const kebab = s => String(s).replace(/[A-Z]/g, c => '-' + c.toLowerCase());

// ---------------------------------------------------------------- 节点

class Text {
  constructor(data) {
    this.nodeType = 3;
    this.nodeName = '#text';
    this.data = String(data);
    this.parentNode = null;
  }
  get textContent() { return this.data; }
  set textContent(v) { this.data = String(v); }
  get nodeValue() { return this.data; }
  set nodeValue(v) { this.data = String(v); }
  get parentElement() { return this.parentNode && this.parentNode.nodeType === 1 ? this.parentNode : null; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  cloneNode() { return new Text(this.data); }
}

function toNode(x) {
  return (x instanceof El || x instanceof Text) ? x : new Text(x === null || x === undefined ? '' : x);
}

function parseStyle(text, into) {
  for (const part of String(text).split(';')) {
    const i = part.indexOf(':');
    if (i > 0) into[part.slice(0, i).trim().toLowerCase()] = part.slice(i + 1).trim();
  }
  return into;
}

class El {
  constructor(tag) {
    this.localName = String(tag).toLowerCase();
    this.nodeType = this.localName === '#fragment' ? 11 : 1;
    this.tagName = this.nodeName = this.localName.toUpperCase();
    this._attrs = new Map();
    this._css = {};
    this._ls = {};
    this.childNodes = [];
    this.parentNode = null;
  }

  // ---- 属性
  getAttribute(k) {
    k = String(k).toLowerCase();
    if (k === 'style') { const s = this._cssText(); return s ? s : null; }
    return this._attrs.has(k) ? this._attrs.get(k) : null;
  }
  setAttribute(k, v) {
    k = String(k).toLowerCase();
    if (k === 'style') { this._css = parseStyle(v, {}); return; }
    this._attrs.set(k, String(v));
  }
  hasAttribute(k) { return this.getAttribute(k) !== null; }
  removeAttribute(k) {
    k = String(k).toLowerCase();
    if (k === 'style') this._css = {}; else this._attrs.delete(k);
  }
  toggleAttribute(k, force) {
    const want = force === undefined ? !this.hasAttribute(k) : !!force;
    if (want) this.setAttribute(k, ''); else this.removeAttribute(k);
    return want;
  }
  getAttributeNames() { return this._pairs().map(p => p[0]); }
  get attributes() { return this._pairs().map(([name, value]) => ({ name, value })); }
  _cssText() { return Object.keys(this._css).map(k => k + ':' + this._css[k]).join(';'); }
  _pairs() {
    const out = [...this._attrs];
    const s = this._cssText();
    if (s) out.push(['style', s]);
    return out;
  }
  get id() { return this.getAttribute('id') || ''; }
  set id(v) { this.setAttribute('id', v); }
  get className() { return this.getAttribute('class') || ''; }
  set className(v) { this.setAttribute('class', v); }
  get classList() {
    const el = this;
    const get = () => el.className.split(/\s+/).filter(Boolean);
    const put = list => el.setAttribute('class', list.join(' '));
    return {
      add: (...n) => put([...new Set([...get(), ...n.map(String)])]),
      remove: (...n) => put(get().filter(c => !n.map(String).includes(c))),
      contains: n => get().includes(String(n)),
      toggle: (n, force) => {
        const has = get().includes(String(n));
        const want = force === undefined ? !has : !!force;
        if (want && !has) put([...get(), String(n)]);
        if (!want && has) put(get().filter(c => c !== String(n)));
        return want;
      },
      replace: (a, b) => put(get().map(c => (c === String(a) ? String(b) : c))),
      get length() { return get().length; },
      get value() { return el.className; },
      [Symbol.iterator]: () => get()[Symbol.iterator](),
    };
  }
  get dataset() {
    const el = this;
    return new Proxy({}, {
      get: (t, k) => (typeof k === 'string' ? (el.getAttribute('data-' + kebab(k)) === null
        ? undefined : el.getAttribute('data-' + kebab(k))) : undefined),
      set: (t, k, v) => { el.setAttribute('data-' + kebab(k), v); return true; },
      has: (t, k) => typeof k === 'string' && el.hasAttribute('data-' + kebab(k)),
      deleteProperty: (t, k) => { el.removeAttribute('data-' + kebab(k)); return true; },
      ownKeys: () => [...el._attrs.keys()].filter(k => k.startsWith('data-'))
        .map(k => k.slice(5).replace(/-([a-z])/g, (m, c) => c.toUpperCase())),
      getOwnPropertyDescriptor: () => ({ enumerable: true, configurable: true }),
    });
  }
  get style() {
    const el = this;
    return new Proxy({}, {
      get: (t, k) => {
        if (typeof k !== 'string') return undefined;
        if (k === 'cssText') return el._cssText();
        if (k === 'setProperty') return (n, v) => { el._css[String(n).toLowerCase()] = String(v); };
        if (k === 'removeProperty') return n => { delete el._css[String(n).toLowerCase()]; };
        if (k === 'getPropertyValue') return n => el._css[String(n).toLowerCase()] || '';
        return el._css[kebab(k)] || '';
      },
      set: (t, k, v) => {
        if (k === 'cssText') el._css = parseStyle(v, {});
        else if (v === '' || v === null || v === undefined) delete el._css[kebab(k)];
        else el._css[kebab(k)] = String(v);
        return true;
      },
    });
  }
  set style(v) { this._css = parseStyle(v, {}); }
  get value() {
    if (this._value !== undefined) return this._value;
    if (this.localName === 'textarea') return this.textContent;
    if (this.localName === 'select') {
      const opts = this.querySelectorAll('option');
      const hit = opts.find(o => o.hasAttribute('selected')) || opts[0];
      return hit ? hit.value : '';
    }
    if (this.localName === 'option' && !this.hasAttribute('value')) return this.textContent;
    return this.getAttribute('value') || '';
  }
  set value(v) { this._value = String(v); }

  // ---- 子节点
  _adopt(n) {
    n = toNode(n);
    if (n.nodeType === 11) return n.childNodes.splice(0).map(c => { c.parentNode = this; return c; });
    if (n.parentNode) n.parentNode.removeChild(n);
    n.parentNode = this;
    return [n];
  }
  appendChild(n) { const got = this._adopt(n); this.childNodes.push(...got); return n; }
  append(...items) { for (const it of items) this.appendChild(toNode(it)); }
  prepend(...items) {
    const got = [];
    for (const it of items) got.push(...this._adopt(toNode(it)));
    this.childNodes.unshift(...got);
  }
  insertBefore(n, ref) {
    const got = this._adopt(n);
    const i = ref ? this.childNodes.indexOf(ref) : -1;
    if (i < 0) this.childNodes.push(...got); else this.childNodes.splice(i, 0, ...got);
    return n;
  }
  removeChild(n) {
    const i = this.childNodes.indexOf(n);
    if (i >= 0) { this.childNodes.splice(i, 1); n.parentNode = null; }
    return n;
  }
  replaceChild(n, old) { this.insertBefore(n, old); this.removeChild(old); return old; }
  replaceChildren(...items) {
    for (const c of this.childNodes.splice(0)) c.parentNode = null;
    this.append(...items);
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  before(...items) { for (const it of items) this.parentNode && this.parentNode.insertBefore(toNode(it), this); }
  after(...items) {
    if (!this.parentNode) return;
    const ref = this.nextSibling;
    for (const it of items) this.parentNode.insertBefore(toNode(it), ref);
  }
  replaceWith(...items) { this.before(...items); this.remove(); }
  insertAdjacentElement(pos, el) {
    pos = String(pos).toLowerCase();
    if (pos === 'beforebegin') this.before(el);
    else if (pos === 'afterbegin') this.prepend(el);
    else if (pos === 'beforeend') this.appendChild(el);
    else this.after(el);
    return el;
  }
  insertAdjacentHTML(pos, html) {
    const frag = new El('#fragment');
    parseInto(frag, String(html));
    this.insertAdjacentElement(pos, frag);
  }
  insertAdjacentText(pos, text) { this.insertAdjacentElement(pos, new Text(text)); }
  cloneNode(deep) {
    const c = new El(this.localName);
    for (const [k, v] of this._attrs) c._attrs.set(k, v);
    c._css = Object.assign({}, this._css);
    if (deep) for (const n of this.childNodes) c.appendChild(n.cloneNode(true));
    return c;
  }
  get content() {
    if (this.localName !== 'template') return undefined;
    const frag = new El('#fragment');
    for (const n of this.childNodes) frag.appendChild(n.cloneNode(true));
    return frag;
  }

  // ---- 走树
  get children() { return this.childNodes.filter(n => n.nodeType === 1); }
  get childElementCount() { return this.children.length; }
  get firstChild() { return this.childNodes[0] || null; }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; }
  get firstElementChild() { return this.children[0] || null; }
  get lastElementChild() { const c = this.children; return c[c.length - 1] || null; }
  get parentElement() { return this.parentNode && this.parentNode.nodeType === 1 ? this.parentNode : null; }
  _sib(d, onlyEl) {
    if (!this.parentNode) return null;
    const list = onlyEl ? this.parentNode.children : this.parentNode.childNodes;
    return list[list.indexOf(this) + d] || null;
  }
  get nextSibling() { return this._sib(1, false); }
  get previousSibling() { return this._sib(-1, false); }
  get nextElementSibling() { return this._sib(1, true); }
  get previousElementSibling() { return this._sib(-1, true); }
  get isConnected() { let n = this; while (n.parentNode) n = n.parentNode; return n === ROOT.top; }
  hasChildNodes() { return this.childNodes.length > 0; }
  contains(n) { for (; n; n = n.parentNode) if (n === this) return true; return false; }
  _walk(out) {
    for (const c of this.childNodes) if (c.nodeType === 1) { out.push(c); c._walk(out); }
    return out;
  }

  // ---- 内容
  get textContent() {
    return this.childNodes.map(n => (n.nodeType === 3 ? n.data : n.textContent)).join('');
  }
  set textContent(v) {
    for (const c of this.childNodes.splice(0)) c.parentNode = null;
    if (v !== null && v !== undefined && String(v) !== '') this.appendChild(new Text(v));
  }
  get innerText() { return this.textContent; }
  set innerText(v) { this.textContent = v; }
  get innerHTML() { return this.childNodes.map(serialize).join(''); }
  set innerHTML(v) {
    for (const c of this.childNodes.splice(0)) c.parentNode = null;
    parseInto(this, v === null || v === undefined ? '' : String(v));
  }
  get outerHTML() { return serialize(this); }

  // ---- 查找
  querySelectorAll(sel) { return query(this, sel); }
  querySelector(sel) { return query(this, sel)[0] || null; }
  getElementsByTagName(t) { t = String(t).toLowerCase(); return this._walk([]).filter(e => t === '*' || e.localName === t); }
  getElementsByClassName(c) { return this._walk([]).filter(e => e.classList.contains(c)); }
  matches(sel) { return parseSelector(sel).some(chain => matchChain(this, chain, chain.length - 1)); }
  closest(sel) { for (let n = this; n && n.nodeType === 1; n = n.parentNode) if (n.matches(sel)) return n; return null; }

  // ---- 事件
  addEventListener(type, fn) { if (typeof fn === 'function') (this._ls[type] = this._ls[type] || []).push(fn); }
  removeEventListener(type, fn) { this._ls[type] = (this._ls[type] || []).filter(f => f !== fn); }
  dispatchEvent(ev) {
    ev = ev || {};
    if (!ev.target) ev.target = this;
    for (let n = this; n; n = n.parentNode) {
      ev.currentTarget = n;
      for (const fn of [...((n._ls || {})[ev.type] || [])]) fn.call(n, ev);
      if (typeof n['on' + ev.type] === 'function') n['on' + ev.type](ev);
      if (ev.bubbles === false || ev._stopped) break;
    }
    return true;
  }
  click() { this.dispatchEvent(makeEvent('click')); }

  // ---- 表格与下拉
  get rows() {
    if (this.localName === 'table') return this._walk([]).filter(e => e.localName === 'tr');
    return this.children.filter(e => e.localName === 'tr');
  }
  get cells() { return this.children.filter(e => e.localName === 'td' || e.localName === 'th'); }
  get tBodies() { return this.children.filter(e => e.localName === 'tbody'); }
  get options() { return this._walk([]).filter(e => e.localName === 'option'); }
  insertRow(i) {
    const host = this.localName === 'table' ? (this.tBodies[0] || this) : this;
    const tr = new El('tr');
    host.insertBefore(tr, i === undefined || i < 0 ? null : host.rows[i] || null);
    return tr;
  }
  deleteRow(i) { const r = this.rows; const hit = r[i < 0 ? r.length + i : i]; if (hit) hit.remove(); }
  insertCell(i) {
    const td = new El('td');
    this.insertBefore(td, i === undefined || i < 0 ? null : this.cells[i] || null);
    return td;
  }
  add(opt) { this.appendChild(opt); }
}

for (const name of ['focus', 'blur', 'select', 'scrollIntoView', 'scrollTo', 'showModal', 'show', 'close',
  'submit', 'reset', 'requestSubmit', 'setCustomValidity', 'setSelectionRange', 'normalize']) {
  El.prototype[name] = function () {};
}
El.prototype.checkValidity = El.prototype.reportValidity = () => true;
El.prototype.getBoundingClientRect = () => ({ x: 0, y: 0, top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 });
for (const name of ['hidden', 'disabled', 'checked', 'selected', 'readOnly', 'required', 'open', 'multiple']) {
  Object.defineProperty(El.prototype, name, {
    get() { return this.hasAttribute(name); },
    set(v) { if (v) this.setAttribute(name, ''); else this.removeAttribute(name); },
  });
}
for (const [prop, attr] of [['type', 'type'], ['name', 'name'], ['href', 'href'], ['src', 'src'],
  ['title', 'title'], ['placeholder', 'placeholder'], ['alt', 'alt'], ['rel', 'rel'], ['target', 'target'],
  ['role', 'role'], ['lang', 'lang'], ['htmlFor', 'for'], ['colSpan', 'colspan'], ['rowSpan', 'rowspan'],
  ['action', 'action'], ['method', 'method'], ['min', 'min'], ['max', 'max']]) {
  Object.defineProperty(El.prototype, prop, {
    get() { return this.getAttribute(attr) || ''; },
    set(v) { this.setAttribute(attr, v); },
  });
}

function makeEvent(type, init) {
  return Object.assign({ type, bubbles: true, defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() { this._stopped = true; },
    stopImmediatePropagation() { this._stopped = true; } }, init || {});
}

// ---------------------------------------------------------------- 源码 <-> 结构

function parseInto(parent, html) {
  const stack = [parent];
  const top = () => stack[stack.length - 1];
  const addText = t => { if (t) top().appendChild(new Text(decode(t))); };
  const open = /<(\/?)([a-zA-Z][^\s\/>]*)/y;
  const n = html.length;
  let i = 0;
  while (i < n) {
    const lt = html.indexOf('<', i);
    if (lt < 0) { addText(html.slice(i)); break; }
    addText(html.slice(i, lt));
    if (html.startsWith('<!--', lt)) { const e = html.indexOf('-->', lt + 4); i = e < 0 ? n : e + 3; continue; }
    if (html[lt + 1] === '!' || html[lt + 1] === '?') { const e = html.indexOf('>', lt); i = e < 0 ? n : e + 1; continue; }
    open.lastIndex = lt;
    const m = open.exec(html);
    if (!m) { addText('<'); i = lt + 1; continue; }
    let j = open.lastIndex;
    for (let q = null; j < n; j++) {
      const c = html[j];
      if (q) { if (c === q) q = null; } else if (c === '"' || c === "'") q = c; else if (c === '>') break;
    }
    const raw = html.slice(open.lastIndex, j);
    const tag = m[2].toLowerCase();
    i = j + 1;
    if (m[1]) {
      for (let k = stack.length - 1; k > 0; k--) if (stack[k].localName === tag) { stack.length = k; break; }
      continue;
    }
    while (stack.length > 1 && IMPLIED_END[tag] && IMPLIED_END[tag].includes(top().localName)) stack.pop();
    const el = new El(tag);
    const attr = /([^\s"'<>\/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'<>`]+)))?/g;
    for (let a; (a = attr.exec(raw));) {
      const v = a[2] !== undefined ? a[2] : a[3] !== undefined ? a[3] : a[4] !== undefined ? a[4] : '';
      el.setAttribute(a[1], decode(v));
    }
    top().appendChild(el);
    if (VOID.has(tag) || /\/\s*$/.test(raw)) continue;
    if (RAW.has(tag)) {
      const rest = html.slice(i);
      const cm = new RegExp('</' + tag + '\\s*>', 'i').exec(rest);
      const body = cm ? rest.slice(0, cm.index) : rest;
      if (body) el.appendChild(new Text(tag === 'script' || tag === 'style' ? body : decode(body)));
      i = cm ? i + cm.index + cm[0].length : n;
      continue;
    }
    stack.push(el);
  }
}

function serialize(node) {
  if (node.nodeType === 3) {
    const p = node.parentNode;
    return p && (p.localName === 'script' || p.localName === 'style') ? node.data : esc(node.data);
  }
  const inner = node.childNodes.map(serialize).join('');
  if (node.nodeType === 11) return inner;
  let s = '<' + node.localName;
  for (const [k, v] of node._pairs()) s += ' ' + k + '="' + escAttr(v) + '"';
  return VOID.has(node.localName) ? s + '>' : s + '>' + inner + '</' + node.localName + '>';
}

function dump(node) {
  if (node.nodeType === 3) return { text: node.data };
  const attrs = {};
  for (const [k, v] of node._pairs()) attrs[k] = v;
  const skip = node.localName === 'script' || node.localName === 'style';
  return { tag: node.localName, attrs, children: skip ? [] : node.childNodes.map(dump) };
}

// ---------------------------------------------------------------- 选择器（够页面脚本用的那一小部分）

const SEL_CACHE = new Map();

function parseSelector(sel) {
  sel = String(sel);
  if (SEL_CACHE.has(sel)) return SEL_CACHE.get(sel);
  const groups = [];
  const tok = /\s*([>+~,])\s*|(\s+)|(\*|[a-zA-Z][\w-]*)|#([\w-]+)|\.([\w-]+)|\[\s*([\w-]+)\s*(?:([~|^$*]?=)\s*(?:"([^"]*)"|'([^']*)'|([^\]\s]+)))?\s*\]|:([\w-]+)(?:\(([^)]*)\))?/y;
  let chain = [];
  let cur = null;
  let comb = ' ';
  const need = () => { if (!cur) { cur = { comb, tag: null, id: null, cls: [], attrs: [], pseudo: [] }; chain.push(cur); } return cur; };
  let i = 0;
  const s = sel.trim();
  while (i < s.length) {
    tok.lastIndex = i;
    const m = tok.exec(s);
    if (!m) { chain = [{ bad: true }]; break; }
    i = tok.lastIndex;
    if (m[1] === ',') { groups.push(chain); chain = []; cur = null; comb = ' '; }
    else if (m[1]) { comb = m[1]; cur = null; }
    else if (m[2]) { comb = ' '; cur = null; }
    else if (m[3]) need().tag = m[3].toLowerCase();
    else if (m[4]) need().id = m[4];
    else if (m[5]) need().cls.push(m[5]);
    else if (m[6]) need().attrs.push({ n: m[6].toLowerCase(), op: m[7] || null, v: m[8] !== undefined ? m[8] : m[9] !== undefined ? m[9] : m[10] });
    else if (m[11]) need().pseudo.push({ n: m[11].toLowerCase(), arg: m[12] });
  }
  groups.push(chain);
  SEL_CACHE.set(sel, groups);
  return groups;
}

function matchCompound(el, c) {
  if (c.bad || el.nodeType !== 1) return false;
  if (c.tag && c.tag !== '*' && el.localName !== c.tag) return false;
  if (c.id && el.id !== c.id) return false;
  for (const k of c.cls) if (!el.classList.contains(k)) return false;
  for (const a of c.attrs) {
    const got = el.getAttribute(a.n);
    if (got === null) return false;
    if (a.op === '=' && got !== a.v) return false;
    if (a.op === '^=' && !got.startsWith(a.v)) return false;
    if (a.op === '$=' && !got.endsWith(a.v)) return false;
    if (a.op === '*=' && !got.includes(a.v)) return false;
    if (a.op === '~=' && !got.split(/\s+/).includes(a.v)) return false;
    if (a.op === '|=' && got !== a.v && !got.startsWith(a.v + '-')) return false;
  }
  for (const p of c.pseudo) {
    if (p.n === 'not') { if (el.matches(p.arg || '*')) return false; }
    else if (p.n === 'checked') { if (!el.checked && !el.selected) return false; }
    else if (p.n === 'disabled') { if (!el.disabled) return false; }
    else if (p.n === 'enabled') { if (el.disabled) return false; }
    else if (p.n === 'first-child') { if (el.previousElementSibling) return false; }
    else if (p.n === 'last-child') { if (el.nextElementSibling) return false; }
    else return false;
  }
  return true;
}

function matchChain(el, chain, i) {
  if (!chain.length || !matchCompound(el, chain[i])) return false;
  if (i === 0) return true;
  const comb = chain[i].comb;
  if (comb === '>') return !!el.parentElement && matchChain(el.parentElement, chain, i - 1);
  if (comb === '+') return !!el.previousElementSibling && matchChain(el.previousElementSibling, chain, i - 1);
  if (comb === '~') {
    for (let p = el.previousElementSibling; p; p = p.previousElementSibling) if (matchChain(p, chain, i - 1)) return true;
    return false;
  }
  for (let p = el.parentElement; p; p = p.parentElement) if (matchChain(p, chain, i - 1)) return true;
  return false;
}

function query(root, sel) {
  const groups = parseSelector(sel);
  return root._walk([]).filter(el => groups.some(chain => matchChain(el, chain, chain.length - 1)));
}

// ---------------------------------------------------------------- 环境

const ROOT = { top: null };

function anything() {
  const f = function () { return anything(); };
  return new Proxy(f, {
    get: (t, k) => (k === 'then' || k === Symbol.iterator ? undefined
      : k === Symbol.toPrimitive ? () => '' : anything()),
    set: () => true,
    construct: () => anything(),
  });
}

function describe(e) {
  if (e && typeof e === 'object' && 'message' in e) return (e.name || 'Error') + ': ' + e.message;
  return String(e);
}

async function runOnce(patched) {
  const errors = [];
  const requests = [];
  const top = new El('#fragment');
  ROOT.top = top;
  parseInto(top, input.html);
  const all = top._walk([]);
  const html = all.find(e => e.localName === 'html') || top;
  const body = all.find(e => e.localName === 'body') || html;
  const head = all.find(e => e.localName === 'head') || html;

  const docLs = {};
  const winLs = {};
  const fire = (ls, type, self) => { for (const fn of [...(ls[type] || [])]) { try { fn.call(self, makeEvent(type)); } catch (e) { errors.push(describe(e)); } } };
  const location = { href: 'http://127.0.0.1/', origin: 'http://127.0.0.1', protocol: 'http:', host: '127.0.0.1',
    hostname: '127.0.0.1', port: '', pathname: input.path || '/', search: '', hash: '',
    reload() {}, assign() {}, replace() {}, toString() { return this.href; } };
  location.href = location.origin + location.pathname;

  const doc = {
    nodeType: 9, documentElement: html, body, head, readyState: 'loading', title: '', cookie: '',
    hidden: false, visibilityState: 'visible', activeElement: body, location,
    getElementById: id => top._walk([]).find(e => e.id === String(id)) || null,
    querySelector: s => top.querySelector(s),
    querySelectorAll: s => top.querySelectorAll(s),
    getElementsByTagName: t => top.getElementsByTagName(t),
    getElementsByClassName: c => top.getElementsByClassName(c),
    getElementsByName: nm => top._walk([]).filter(e => e.getAttribute('name') === String(nm)),
    createElement: t => new El(t),
    createElementNS: (ns, t) => new El(t),
    createTextNode: t => new Text(t),
    createComment: () => new Text(''),
    createDocumentFragment: () => new El('#fragment'),
    addEventListener: (type, fn) => { if (typeof fn === 'function') (docLs[type] = docLs[type] || []).push(fn); },
    removeEventListener: (type, fn) => { docLs[type] = (docLs[type] || []).filter(f => f !== fn); },
    dispatchEvent: () => true,
    execCommand: () => false,
    hasFocus: () => true,
  };

  let ctx = null;
  let tid = 0;
  let fired = 0;
  const pending = new Map();
  const later = (fn, ms, args) => {
    const id = ++tid;
    if (typeof fn !== 'function' || ms > 50 || fired >= 300) return id;   // 轮询与长等待不替页面跑
    fired++;
    pending.set(id, setTimeout(() => {
      pending.delete(id);
      try { fn(...args); } catch (e) { errors.push(describe(e)); }
    }, 0));
    return id;
  };
  const cancel = id => { if (pending.has(id)) { clearTimeout(pending.get(id)); pending.delete(id); } };

  const makeStore = () => {
    const m = new Map();
    return { getItem: k => (m.has(String(k)) ? m.get(String(k)) : null), setItem: (k, v) => { m.set(String(k), String(v)); },
      removeItem: k => { m.delete(String(k)); }, clear: () => m.clear(), key: i => [...m.keys()][i] || null,
      get length() { return m.size; } };
  };

  const respond = (status, payload, url) => {
    const text = JSON.stringify(payload === undefined ? {} : payload);
    const parse = () => vm.runInContext('(t) => JSON.parse(t)', ctx)(text);     // 交给页面的是它自己那边的对象
    return { ok: status >= 200 && status < 300, status, statusText: '', url, redirected: false, type: 'basic',
      headers: { get: k => (String(k).toLowerCase() === 'content-type' ? 'application/json' : null),
        has: k => String(k).toLowerCase() === 'content-type' },
      json: async () => parse(), text: async () => text, clone() { return respond(status, payload, url); } };
  };
  const fetch = async (target, opts) => {
    const url = typeof target === 'string' ? target : String((target && target.url) || target);
    const method = String((opts && opts.method) || (target && target.method) || 'GET').toUpperCase();
    const path = url.replace(/^[a-z]+:\/\/[^/]+/i, '').split(/[?#]/)[0];
    requests.push({ method, path });
    const hit = (input.routes || []).find(r => r.method === method && r.path === path);
    return hit ? respond(hit.status === undefined ? 200 : hit.status, hit.body, url) : respond(200, {}, url);
  };

  const noop = () => {};
  class Observer { observe() {} unobserve() {} disconnect() {} takeRecords() { return []; } }
  const sandbox = {
    document: doc, location, fetch,
    console: { log: noop, info: noop, debug: noop, trace: noop,
      warn: (...a) => errors.push('console.warn: ' + a.map(describe).join(' ')),
      error: (...a) => errors.push('console.error: ' + a.map(describe).join(' ')) },
    alert: noop, confirm: () => true, prompt: () => null,
    setTimeout: (fn, ms, ...a) => later(fn, ms || 0, a), clearTimeout: cancel,
    setInterval: () => ++tid, clearInterval: noop,
    requestAnimationFrame: fn => later(fn, 0, [0]), cancelAnimationFrame: cancel,
    queueMicrotask: fn => Promise.resolve().then(fn),
    localStorage: makeStore(), sessionStorage: makeStore(),
    navigator: { userAgent: 'node', language: 'zh-CN', languages: ['zh-CN'], onLine: true,
      clipboard: { writeText: async () => {}, readText: async () => '' } },
    history: { pushState: noop, replaceState: noop, back: noop, forward: noop, go: noop, state: null, length: 1 },
    matchMedia: () => ({ matches: false, media: '', addEventListener: noop, removeEventListener: noop, addListener: noop, removeListener: noop }),
    getComputedStyle: el => (el && el.style) || {},
    addEventListener: (type, fn) => { if (typeof fn === 'function') (winLs[type] = winLs[type] || []).push(fn); },
    removeEventListener: (type, fn) => { winLs[type] = (winLs[type] || []).filter(f => f !== fn); },
    dispatchEvent: () => true, scrollTo: noop, scrollBy: noop, focus: noop, blur: noop, open: () => null, close: noop, print: noop,
    innerWidth: 1280, innerHeight: 800, devicePixelRatio: 1, scrollX: 0, scrollY: 0,
    MutationObserver: Observer, IntersectionObserver: Observer, ResizeObserver: Observer,
    Event: function (type, init) { return makeEvent(type, init); },
    CustomEvent: function (type, init) { return makeEvent(type, init); },
    Node: El, Element: El, HTMLElement: El,
    URL, URLSearchParams, TextEncoder, TextDecoder, AbortController, AbortSignal,
    structuredClone: v => JSON.parse(JSON.stringify(v)),
    atob: s => Buffer.from(String(s), 'base64').toString('binary'),
    btoa: s => Buffer.from(String(s), 'binary').toString('base64'),
  };
  for (const name of patched) if (!(name in sandbox)) sandbox[name] = anything();
  sandbox.window = sandbox.self = sandbox.top = sandbox.parent = sandbox.frames = sandbox;
  doc.defaultView = sandbox;
  ctx = vm.createContext(sandbox);

  const settle = async () => {
    for (let round = 0; round < 40; round++) {
      await new Promise(r => setImmediate(r));
      if (round >= 6 && pending.size === 0) break;
    }
  };

  for (const s of all.filter(e => e.localName === 'script')) {
    const type = (s.getAttribute('type') || '').toLowerCase();
    if (type && !/javascript|ecmascript|^module$/.test(type)) continue;
    const src = s.getAttribute('src');
    const code = src === null ? s.textContent : (input.external || {})[src];
    if (typeof code !== 'string' || !code.trim()) continue;
    try { vm.runInContext(code, ctx, { timeout: 10000 }); } catch (e) { errors.push(describe(e)); }
  }
  doc.readyState = 'interactive';
  fire(docLs, 'DOMContentLoaded', doc);
  fire(winLs, 'DOMContentLoaded', sandbox);
  doc.readyState = 'complete';
  fire(docLs, 'readystatechange', doc);
  fire(winLs, 'load', sandbox);
  if (typeof sandbox.onload === 'function') { try { sandbox.onload(makeEvent('load')); } catch (e) { errors.push(describe(e)); } }
  await settle();

  const missing = [];
  for (const name of input.call || []) {
    if (!/^[A-Za-z_$][\w$]*$/.test(name)) { missing.push(name); continue; }
    let fn;
    try { fn = vm.runInContext('typeof ' + name + ' === "function" ? ' + name + ' : undefined', ctx); } catch (e) { fn = undefined; }
    if (typeof fn !== 'function') { missing.push(name); continue; }
    try { await fn(); } catch (e) { errors.push(describe(e)); }
    await settle();
  }
  for (const t of pending.values()) clearTimeout(t);
  return { errors, requests, missing, body };
}

async function main() {
  const rejections = [];
  process.on('unhandledRejection', e => { rejections.push(describe(e)); });
  const patched = [];
  let got = null;
  for (let attempt = 0; attempt < 40; attempt++) {
    rejections.length = 0;
    got = await runOnce(patched);
    await new Promise(r => setImmediate(r));
    got.errors.push(...rejections);
    const fresh = [];
    for (const msg of got.errors) {
      const m = /ReferenceError: ([A-Za-z_$][\w$]*) is not defined/.exec(msg);
      if (m && !patched.includes(m[1]) && !fresh.includes(m[1])) fresh.push(m[1]);
    }
    if (!fresh.length) break;
    patched.push(...fresh);         // 页面用到了这里没有的浏览器全局：补一个替身，整页重跑
  }
  fs.writeFileSync(OUT, JSON.stringify({ ok: true, tree: dump(got.body), requests: got.requests,
    errors: got.errors, missing: got.missing, patched }));
}

main().catch(e => {
  fs.writeFileSync(OUT, JSON.stringify({ ok: false, error: describe(e) + '\n' + ((e && e.stack) || '') }));
  process.exitCode = 1;
});
