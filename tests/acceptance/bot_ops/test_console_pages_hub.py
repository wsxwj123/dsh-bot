# -*- coding: utf-8 -*-
"""页面服务验收：适配服务进程（`moments.adapter_service`）里新挂的三页与它们的接口。

依据只有三份：`.devflow/BRIEF-页面服务.md`、`INTERFACE-页面服务.md`、
`INTERFACE-适配服务.md` 第 1、2 节。不读实现。

两边怎么发请求：
- 新的一边：adapter_util.py 的 `adapter` 夹具（令牌文件经 DSH_CONSOLE_TOKEN_FILE 指到临时目录，
  假 launchctl、假家目录都已换好），用 `_hub()` 发，默认带对的令牌；
- 8765 一边：conftest.py 的 `web_client`（整站应用；测试环境里没设任何口令，即"已登录"）。

红基线：/hub/... 还没挂上时，带对令牌的用例红在 404；没过鉴权的那几条现在就绿——
适配服务已有的鉴权对任何路径都先回 401，挂上之后它们守的是"新挂的路由没有绕过鉴权"。
"""
import json
import logging
import time
from pathlib import Path
from urllib.parse import quote

import pytest

from . import botops_util as u
from .adapter_util import (  # noqa: F401  adapter、console_guard 是夹具，导入进来才能用
    ADAPTER_TOKEN,
    OTHER_TOKEN,
    PROVIDER_KEY,
    TG_TOKEN,
    WRONG_TOKEN,
    adapter,
    assert_unauthorized,
    body_of,
    console_guard,
    response_text,
)

# ---------------------------------------------------------------- 常量（假值全是编的）
TOKEN_HEADER = "Bearer " + ADAPTER_TOKEN
WRONG_HEADER = "Bearer " + WRONG_TOKEN
NO_AUTH = [None, WRONG_HEADER]
NO_AUTH_IDS = ["没带令牌", "令牌不对"]
#: 浏览器打开页面时的 Accept：用来确认页面请求没过鉴权时也回 JSON、不跳登录页
BROWSER = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

NAV_MARK = "<!-- console-nav -->"
#: 8765 导航条里的高亮项，三页各恰好一处（见 test_pages_structure.py）
NAV_HIGHLIGHT = 'class="chip on"'
#: 接口约定第 3 节举的 8765 导航链接文字
NAV_WORDS = ("旧系统供应商", "系统参数")

PAGES = ["/hub/bots", "/hub/addbot", "/hub/dsh-model"]
BOT = "botalpha"
VENDOR = "fake-vendor-b"
GHOST_JOB = "deadbeef"
JOB_URL = "/hub/api/bots/restart/%s"
ENABLED_URL = "/hub/api/bots/%s/enabled" % BOT


def _prov(op, name=VENDOR):
    """供应商那组接口的路径：名字按百分号编码放进路径。"""
    return "/hub/api/dsh-model/provider/%s/%s" % (quote(name, safe=""), op)


#: 名单内的每个入口各一个请求：(方法, 路径, 请求体)。请求体是猜的形状，没过鉴权时本来就不该被看。
LISTED = [
    ("GET", "/hub/bots", None),
    ("GET", "/hub/addbot", None),
    ("GET", "/hub/dsh-model", None),
    ("GET", "/hub/api/bots", None),
    ("POST", "/hub/api/bots", {"id": "botnew", "display_name": "新来的", "telegram_token": TG_TOKEN}),
    ("GET", "/hub/api/bots/templates", None),
    ("POST", "/hub/api/bots/test_token", {"telegram_token": TG_TOKEN}),
    ("GET", "/hub/api/bots/%s/edit" % BOT, None),
    ("POST", "/hub/api/bots/%s/edit" % BOT, {"display_name": "改过的名字"}),
    ("POST", ENABLED_URL, {"enabled": False}),
    ("POST", "/hub/api/bots/%s/restart" % BOT, {}),
    ("POST", "/hub/api/bots/restart", {}),
    ("GET", JOB_URL % GHOST_JOB, None),
    ("GET", "/hub/api/dsh-model", None),
    ("POST", "/hub/api/dsh-model/%s" % BOT, {"provider": VENDOR, "model": "vb-small"}),
    ("GET", "/hub/api/dsh-model/%s/effort" % BOT, None),
    ("POST", "/hub/api/dsh-model/%s/effort" % BOT, {"effort": "high"}),
    ("POST", "/hub/api/dsh-model/provider/save", {"name": "fake-vendor-new", "key": PROVIDER_KEY}),
] + [("POST", _prov(op), {}) for op in ("preview", "models", "refresh", "remove", "test")]

#: 名单外的 /hub/ 路径：约定第 3 节举的例子，加上"不规范化"推出来的几个（末尾斜杠、双斜杠）
UNLISTED = [
    ("GET", "/hub"),
    ("GET", "/hub/"),
    ("GET", "/hub/params"),
    ("GET", "/hub/provider"),
    ("GET", "/hub/setup"),
    ("GET", "/hub/api/params"),
    ("POST", "/hub/api/params"),
    ("GET", "/hub/api/provider"),
    ("POST", "/hub/api/provider"),
    ("POST", "/hub/api/oauth/callback"),
    ("GET", "/hub/bots/"),
    ("GET", "/hub//bots"),
    ("GET", "/hub/api/bots/%s" % BOT),
    ("POST", "/hub/api/dsh-model/provider/%s/rename" % VENDOR),
]

#: 名单内的路径，用了名单外的方法
WRONG_METHOD = [
    ("POST", "/hub/bots"),
    ("DELETE", "/hub/addbot"),
    ("PUT", "/hub/dsh-model"),
    ("PUT", "/hub/api/bots"),
    ("DELETE", "/hub/api/bots"),
    ("POST", "/hub/api/bots/templates"),
    ("GET", "/hub/api/bots/test_token"),
    ("DELETE", "/hub/api/bots/%s/edit" % BOT),
    ("GET", ENABLED_URL),
    ("GET", "/hub/api/bots/%s/restart" % BOT),
    ("GET", "/hub/api/bots/restart"),
    ("POST", JOB_URL % GHOST_JOB),
    ("POST", "/hub/api/dsh-model"),
    ("GET", "/hub/api/dsh-model/%s" % BOT),
    ("PUT", "/hub/api/dsh-model/%s/effort" % BOT),
    ("GET", "/hub/api/dsh-model/provider/save"),
    ("GET", _prov("preview")),
    ("GET", _prov("remove")),
]


def _ids(rows):
    return ["%s %s" % (r[0], r[1]) for r in rows]


# ---------------------------------------------------------------- 发请求
def _hub(adapter, method, path, body=None, *, auth=TOKEN_HEADER, raw=None, headers=None):
    """向适配服务发一个请求。auth 是 Authorization 头的值，None 就不带这个头。"""
    h = dict(headers or {})
    if auth is not None:
        h["Authorization"] = auth
    kw = {"method": method, "headers": h}
    if raw is not None:
        kw["data"] = raw
        kw["content_type"] = "application/json"
    elif body is not None:
        kw["json"] = body
    return adapter.client.open(path, **kw)


def _old(web_client, method, path, body=None):
    """8765（已登录）上的同一请求。"""
    kw = {"method": method}
    if body is not None:
        kw["json"] = body
    return web_client.open(path, **kw)


def _view(resp):
    """状态码、类型、响应体（JSON 按解析后的对象，别的按原文）。两边比对就比这个。"""
    text = resp.get_data(as_text=True)
    body = text
    if resp.mimetype == "application/json":
        try:
            body = json.loads(text)
        except ValueError:
            pass
    return {"status": resp.status_code, "type": resp.mimetype, "body": body}


def _short(obj, n=600):
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
    return text if len(text) <= n else text[:n] + "…（截断）"


def _differ(want, got):
    return "\n8765：%s\n这边：%s" % (_short(want), _short(got))


# ---------------------------------------------------------------- 文件快照
def _tree(root):
    """root 下每个文件的相对路径 → 字节。"""
    root = Path(root)
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _tree_changes(before, after):
    added = sorted(set(after) - set(before))
    gone = sorted(set(before) - set(after))
    changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
    return {"多出": added, "没了": gone, "变了": changed}


# ---------------------------------------------------------------- 后台任务
def _job_id(resp):
    body = resp.get_json(silent=True)
    if not isinstance(body, dict):
        return None
    for holder in (body, body.get("sync")):
        if isinstance(holder, dict) and isinstance(holder.get("job_id"), str):
            return holder["job_id"]
    return None


def _wait_job(get, resp, timeout=30.0):
    """响应里带了任务号就轮询到终态，返回终态名；没带任务号返回 None。"""
    jid = _job_id(resp)
    if jid is None:
        return None
    deadline = time.time() + timeout
    while True:
        r = get(JOB_URL % jid)
        body = r.get_json(silent=True) if r.status_code == 200 else None
        state = body.get("state") if isinstance(body, dict) else "gone"
        if state != "running":
            return state
        assert time.time() < deadline, "任务 %s 等了 %.0f 秒还没到终态" % (jid, timeout)
        time.sleep(0.05)


def _mask_volatile(obj):
    """任务号与时间戳（键名以 _at 结尾的数）每次都不同，比对前换成同一个占位。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "job_id":
                out[k] = "<job_id>"
            elif k.endswith("_at") and isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k] = "<时间戳>"
            else:
                out[k] = _mask_volatile(v)
        return out
    if isinstance(obj, list):
        return [_mask_volatile(v) for v in obj]
    return obj


def _actions(calls):
    """假 launchctl 的调用里去掉只读的 print，留下每次的完整参数。"""
    return [c.get("argv") for c in calls if c.get("cmd") != "print"]


# ---------------------------------------------------------------- 假网关：什么都答、原样回显
class _EchoRoutes(dict):
    """塞给 RecordingGateway 当应答表：任何方法、任何路径都回 200，并把收到的方法、路径、体写回去。"""

    def get(self, key, default=None):
        return lambda rec: (200, {"ok": True, "echo": {"method": rec["method"], "path": rec["path"],
                                                        "json": rec["json"]}})


def _echo_gateway(adapter):
    gw = adapter.gateway()
    gw.routes = _EchoRoutes()
    return gw


def _seen(gw):
    return [(r["method"], r["path"], r["json"]) for r in gw.requests]


# ================================================================ 鉴权
@pytest.mark.parametrize("auth", NO_AUTH, ids=NO_AUTH_IDS)
@pytest.mark.parametrize("method,path,body", LISTED, ids=_ids(LISTED))
def test_名单内_没带或带错令牌_401固定JSON_页面也不跳转(adapter, method, path, body, auth):
    adapter.bot(BOT)
    r = _hub(adapter, method, path, body, auth=auth, headers=BROWSER)
    assert "Location" not in r.headers, "没过鉴权不许跳转，实际跳到 %r" % r.headers.get("Location")
    assert_unauthorized(r)


#: (说明, Authorization 头, 查询串, 发请求前对令牌文件做的事)
AUTH_VARIANTS = [
    ("写法不是Bearer", "Token " + ADAPTER_TOKEN, "", None),
    ("只有令牌没写Bearer", ADAPTER_TOKEN, "", None),
    ("令牌少最后一位", "Bearer " + ADAPTER_TOKEN[:-1], "", None),
    ("令牌多一位", "Bearer " + ADAPTER_TOKEN + "0", "", None),
    ("Bearer后面是空的", "Bearer ", "", None),
    ("令牌放在查询参数里不带头", None, "?token=%s&access_token=%s" % (ADAPTER_TOKEN, ADAPTER_TOKEN), None),
    ("令牌文件运行中被清空", TOKEN_HEADER, "", "empty"),
    ("令牌文件运行中被删", TOKEN_HEADER, "", "delete"),
]
AUTH_SAMPLES = [("GET", "/hub/bots", None), ("GET", "/hub/api/bots", None),
                ("POST", ENABLED_URL, {"enabled": False})]


@pytest.mark.parametrize("sample", AUTH_SAMPLES, ids=_ids(AUTH_SAMPLES))
@pytest.mark.parametrize("variant", AUTH_VARIANTS, ids=[v[0] for v in AUTH_VARIANTS])
def test_鉴权_各种不对的带法_一律401固定JSON(adapter, launchctl, variant, sample):
    _, header, query, file_action = variant
    method, path, body = sample
    adapter.bot(BOT)
    assert adapter.client is not None          # 先把应用建好，令牌文件的变化发生在"运行中"
    if file_action == "empty":
        adapter.token_file.write_text("  \n", encoding="utf-8")
    elif file_action == "delete":
        adapter.token_file.unlink()
    r = _hub(adapter, method, path + query, body, auth=header)
    assert_unauthorized(r)
    assert launchctl.calls() == [], "没过鉴权却调了启动项管理程序：%r" % launchctl.calls()


@pytest.mark.parametrize("auth", NO_AUTH, ids=NO_AUTH_IDS)
@pytest.mark.parametrize("method,path,body", LISTED, ids=_ids(LISTED))
def test_名单内_没过鉴权_不调外部程序_不发请求_临时目录一字不变(
        adapter, launchctl, bun, telegram, method, path, body, auth):
    gw = adapter.model_gateway()
    adapter.bot(BOT, gw_port=gw.port)
    assert adapter.client is not None          # 先建好应用：建应用本身留下的东西不算这次请求的
    before = _tree(adapter.sandbox.tmp)
    r = _hub(adapter, method, path, body, auth=auth)
    assert r.status_code == 401, "应为 401，实际 %s" % r.status_code
    assert launchctl.calls() == [], "调了启动项管理程序：%r" % launchctl.calls()
    assert bun.calls() == [], "调了 bun：%r" % bun.calls()
    assert telegram.hits == [], "向 Telegram 发了请求：%r" % telegram.hits
    assert gw.requests == [], "向 bot 的网关发了请求：%r" % [(q["method"], q["path"]) for q in gw.requests]
    changes = _tree_changes(before, _tree(adapter.sandbox.tmp))
    assert not any(changes.values()), "没过鉴权却动了文件：%r" % changes


def test_令牌文件换了内容_旧令牌401_新令牌放行且结果同8765(adapter, web_client):
    adapter.bot(BOT)
    assert adapter.client is not None
    adapter.token_file.write_text("\n  %s  \n" % OTHER_TOKEN, encoding="utf-8")   # 首尾空白不算令牌
    assert_unauthorized(_hub(adapter, "GET", "/hub/api/bots"))
    got = _view(_hub(adapter, "GET", "/hub/api/bots", auth="Bearer " + OTHER_TOKEN))
    want = _view(_old(web_client, "GET", "/hub/api/bots"))
    assert got == want, "换成新令牌后应放行，结果应同 8765" + _differ(want, got)


# ================================================================ 名单外路径与错方法
@pytest.mark.parametrize("method,path", UNLISTED, ids=_ids(UNLISTED))
def test_名单外的hub路径_带对令牌_404_not_found且不跳转(adapter, method, path):
    adapter.bot(BOT)
    r = _hub(adapter, method, path)
    assert "Location" not in r.headers, "不许把路径规范化后跳转，实际跳到 %r" % r.headers.get("Location")
    body = body_of(r)
    assert r.status_code == 404, "应为 404，实际 %s：%r" % (r.status_code, body)
    assert body.get("ok") is False and body.get("code") == "not_found", body


@pytest.mark.parametrize("method,path", WRONG_METHOD, ids=_ids(WRONG_METHOD))
def test_名单内路径_用了名单外的方法_405且没触发启停(adapter, launchctl, method, path):
    adapter.bot(BOT)
    r = _hub(adapter, method, path)
    assert r.status_code == 405, "应为 405，实际 %s：%s" % (r.status_code, _short(r.get_data(as_text=True)))
    assert launchctl.calls() == [], "错方法却调了启动项管理程序：%r" % launchctl.calls()


#: 没过鉴权时连路径、方法、请求体都不该看：名单外、错方法、坏请求体一律先回 401
GATE_FIRST = ([(m, p, None) for m, p in UNLISTED] + [(m, p, None) for m, p in WRONG_METHOD]
              + [("POST", ENABLED_URL, "{坏的"), ("POST", "/hub/api/dsh-model/provider/save", "[1, 2")])
GATE_FIRST_IDS = ["%s %s%s" % (m, p, " 坏请求体" if raw else "") for m, p, raw in GATE_FIRST]


@pytest.mark.parametrize("auth", NO_AUTH, ids=NO_AUTH_IDS)
@pytest.mark.parametrize("method,path,raw", GATE_FIRST, ids=GATE_FIRST_IDS)
def test_名单外路径_错方法_坏请求体_没过鉴权都先回401(adapter, method, path, raw, auth):
    r = _hub(adapter, method, path, auth=auth, raw=raw.encode("utf-8") if raw else None)
    assert "Location" not in r.headers, "没过鉴权不许跳转，实际跳到 %r" % r.headers.get("Location")
    assert_unauthorized(r)


# ================================================================ 三页与导航
def _page(adapter, page):
    r = _hub(adapter, "GET", page)
    assert r.status_code == 200, "%s 带对令牌应为 200，实际 %s：%s" % (
        page, r.status_code, _short(r.get_data(as_text=True)))
    return r


def _first_diff(a, b):
    n = min(len(a), len(b))
    i = next((k for k in range(n) if a[k] != b[k]), n)
    return "第 %d 个字符起不同。8765：%r；这边：%r" % (i, a[max(0, i - 60):i + 120], b[max(0, i - 60):i + 120])


@pytest.mark.parametrize("page", PAGES)
def test_三页_带对令牌_回200的HTML且恰有一行占位注释(adapter, web_client, page):
    adapter.bot(BOT)
    r = _page(adapter, page)
    assert r.mimetype == "text/html", "页面应回 HTML，实际 %r" % r.headers.get("Content-Type")
    html = r.get_data(as_text=True)
    assert html.count(NAV_MARK) == 1, "%s 应恰有一处 %s，实际 %d 处" % (page, NAV_MARK, html.count(NAV_MARK))
    assert NAV_MARK in [ln.strip() for ln in html.splitlines()], "%s 要单独占一行" % NAV_MARK


@pytest.mark.parametrize("page", PAGES)
def test_三页_带对令牌_页面里没有本系统导航条的元素(adapter, web_client, page):
    adapter.bot(BOT)
    html = _page(adapter, page).get_data(as_text=True)
    assert NAV_HIGHLIGHT not in html, "%s 里还有导航条的高亮项 %s" % (page, NAV_HIGHLIGHT)
    for word in NAV_WORDS:
        assert word not in html, "%s 里还有 8765 导航的链接文字「%s」" % (page, word)


@pytest.mark.parametrize("page", PAGES)
def test_三页_除导航那一段换成占位注释外_与8765同一页逐字相同(adapter, web_client, page):
    adapter.bot(BOT)
    first, second = web_client.get(page), web_client.get(page)
    assert first.status_code == 200, "前提不成立：8765 的 %s 回了 %s" % (page, first.status_code)
    assert first.get_data() == second.get_data(), "前提不成立：8765 同一页两次内容不同，没法逐字比对"
    old = first.get_data(as_text=True)
    new = _page(adapter, page).get_data(as_text=True)
    assert new.count(NAV_MARK) == 1, "%s 应恰有一处 %s" % (page, NAV_MARK)
    head, tail = new.split(NAV_MARK)
    head, tail = head.rstrip(), tail.lstrip()
    assert old.startswith(head), "占位注释之前的部分与 8765 不同：" + _first_diff(old, head)
    assert old.endswith(tail), "占位注释之后的部分与 8765 不同（从末尾倒着比）：" + \
        _first_diff(old[::-1], tail[::-1])
    assert len(head) + len(tail) <= len(old), "两段拼起来比 8765 整页还长，说明有内容重复"
    cut = old[len(head):len(old) - len(tail)]
    assert NAV_HIGHLIGHT in cut, "8765 上被换掉的那一段里没有导航条的高亮项，换掉的不是导航：%r" % _short(cut)


@pytest.mark.parametrize("page", PAGES)
def test_8765三页_仍显示本系统导航且没有占位注释(web_client, page):
    r = web_client.get(page)
    assert r.status_code == 200, "8765 的 %s 应为 200，实际 %s" % (page, r.status_code)
    html = r.get_data(as_text=True)
    assert html.count(NAV_HIGHLIGHT) == 1, "8765 的 %s 导航高亮项应恰好一处" % page
    for word in NAV_WORDS:
        assert word in html, "8765 的 %s 导航里少了「%s」" % (page, word)
    assert NAV_MARK not in html, "8765 的 %s 不该出现占位注释" % page


# ================================================================ 与 8765 一致
#: 不改东西的请求（含两个出错的：不存在的 bot、不存在的任务号）
READS = [
    ("GET", "/hub/api/bots", None),
    ("GET", "/hub/api/bots/templates", None),
    ("GET", "/hub/api/bots/%s/edit" % BOT, None),
    ("GET", "/hub/api/bots/nosuchbot/edit", None),
    ("GET", JOB_URL % GHOST_JOB, None),
    ("GET", "/hub/api/dsh-model", None),
    ("GET", "/hub/api/dsh-model/%s/effort" % BOT, None),
    ("POST", "/hub/api/bots/test_token", {"telegram_token": TG_TOKEN}),
]


@pytest.mark.parametrize("method,path,body", READS, ids=_ids(READS))
def test_不改东西的接口_带对令牌_状态码与响应体同8765上的同一请求(
        adapter, web_client, telegram, method, path, body):
    gw = adapter.model_gateway(effort="high")
    adapter.bot(BOT, gw_port=gw.port)
    adapter.bot("botbeta", enabled=False, display_name="停着的那只")
    u.write_bot_yml(adapter.sandbox.legacy_cfg, "legacyone", display_name="旧系统那只",
                    dispatcher_port=17899, enabled=True)
    want = _view(_old(web_client, method, path, body))
    again = _view(_old(web_client, method, path, body))
    assert want == again, "前提不成立：8765 上同一请求两次结果不同，没法逐字比对" + _differ(want, again)
    got = _view(_hub(adapter, method, path, body))
    assert got == want, "%s %s 与 8765 不一致" % (method, path) + _differ(want, got)


#: 路径里那一段（已编码）。前两个在 8765 上是"名字不合法"，第三个合法、会真的转给网关
ENCODED_NAMES = [quote("测试 供应商", safe=""), quote("百分号%20名", safe=""), "%46ake_Vendor-B"]


@pytest.mark.parametrize("op", ["preview", "models", "refresh", "test", "remove"])
@pytest.mark.parametrize("segment", ENCODED_NAMES, ids=["中文和空格", "名字里本身带百分号", "合法名字里有字母被编码"])
def test_供应商名按百分号编码原样到达_回应与转给网关的请求都同8765(adapter, web_client, segment, op):
    path = "/hub/api/dsh-model/provider/%s/%s" % (segment, op)
    gw_old, gw_new = _echo_gateway(adapter), _echo_gateway(adapter)
    adapter.bot(BOT, gw_port=gw_old.port)
    want = _view(_old(web_client, "POST", path, {}))
    adapter.bot(BOT, gw_port=gw_new.port)      # 换一只假网关：两边各记各的
    got = _view(_hub(adapter, "POST", path, {}))
    assert got == want, "POST %s 的回应与 8765 不一致" % path + _differ(want, got)
    assert _seen(gw_new) == _seen(gw_old), "转给网关的请求与 8765 不一致" + _differ(_seen(gw_old), _seen(gw_new))


def test_带对令牌停用一只假bot_回应与配置文件变化和8765同一请求一致(adapter, web_client, launchctl):
    bot = adapter.bot(BOT)
    launchctl.set_default(installed=True, running=True, pid=4242)
    yml0, fake0 = bot.yml.read_bytes(), launchctl.cfg_path.read_bytes()

    n0 = len(launchctl.calls())
    r_old = _old(web_client, "POST", ENABLED_URL, {"enabled": False})
    st_old = _wait_job(web_client.get, r_old)
    yml_old, acts_old = bot.yml.read_bytes(), _actions(launchctl.calls()[n0:])
    assert yml_old != yml0, "前提不成立：8765 上这次停用没有改配置文件（%s）" % _short(_view(r_old))

    bot.yml.write_bytes(yml0)                  # 复原到同一个起点：配置文件与假 launchctl 的状态
    launchctl.cfg_path.write_bytes(fake0)
    n1 = len(launchctl.calls())
    r_new = _hub(adapter, "POST", ENABLED_URL, {"enabled": False})
    st_new = _wait_job(lambda p: _hub(adapter, "GET", p), r_new)

    want, got = _mask_volatile(_view(r_old)), _mask_volatile(_view(r_new))
    assert got == want, "停用的回应与 8765 不一致" + _differ(want, got)
    assert bot.yml.read_bytes() == yml_old, "配置文件的变化与 8765 不一致：\n8765 写成 %r\n这边写成 %r" % (
        yml_old.decode("utf-8", "replace"), bot.yml.read_bytes().decode("utf-8", "replace"))
    assert _actions(launchctl.calls()[n1:]) == acts_old, "对启动项管理程序的调用与 8765 不一致"
    assert st_new == st_old, "后台任务的终态与 8765 不一致：8765 %r，这边 %r" % (st_old, st_new)


# ================================================================ 令牌与密钥不外泄
#: (方法, 路径, 应得状态码)：先确认请求真的到了该到的地方，再看有没有带出令牌
LEAK_PROBES = [("GET", "/hub/bots", 200), ("GET", "/hub/addbot", 200), ("GET", "/hub/dsh-model", 200),
               ("GET", "/hub/api/bots", 200), ("GET", "/hub/params", 404), ("DELETE", "/hub/api/bots", 405)]


@pytest.mark.parametrize("method,path,status", LEAK_PROBES, ids=_ids(LEAK_PROBES))
def test_带对令牌_响应体和响应头里都不出现令牌原文(adapter, web_client, method, path, status):
    adapter.bot(BOT)
    r = _hub(adapter, method, path)
    assert r.status_code == status, "%s %s 应为 %d，实际 %d" % (method, path, status, r.status_code)
    assert ADAPTER_TOKEN not in response_text(r), "%s %s 的响应里出现了令牌原文" % (method, path)


def test_请求头里的令牌和请求体里的令牌密钥_都不进日志和输出(adapter, web_client, telegram, caplog, capfd):
    caplog.set_level(logging.DEBUG)
    gw = adapter.model_gateway()
    adapter.bot(BOT, gw_port=gw.port)
    body_tg = "700000002:FAKE-BODY-TG-TOKEN-never-log-QQrrSStt"
    body_key = "sk-FAKE-BODY-PROVIDER-KEY-never-log-1a2b3c4d"
    want = _old(web_client, "POST", "/hub/api/bots/test_token", {"telegram_token": body_tg}).status_code
    caplog.clear()
    capfd.readouterr()                         # 8765 那次留下的不算
    got = _hub(adapter, "POST", "/hub/api/bots/test_token", {"telegram_token": body_tg})
    assert got.status_code == want, "前提：这条请求要真的进了处理函数（状态码应同 8765 的 %d，实际 %d）" % (
        want, got.status_code)
    _hub(adapter, "POST", "/hub/api/dsh-model/provider/save", {"name": "fake-vendor-new", "key": body_key})
    _hub(adapter, "POST", "/hub/api/bots/test_token", {"telegram_token": body_tg}, auth=WRONG_HEADER)
    out, err = capfd.readouterr()
    logs = caplog.text + out + err
    for label, secret in (("适配服务令牌", ADAPTER_TOKEN), ("请求体里的 Telegram 令牌", body_tg),
                          ("请求体里的供应商密钥", body_key)):
        assert secret not in logs, "%s出现在日志或输出里" % label
