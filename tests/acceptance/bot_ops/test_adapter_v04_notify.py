# -*- coding: utf-8 -*-
"""适配服务 v0.4 验收：投递通知 POST /adapter/v1/bots/{bot_id}/notify，与 meta 的十项能力。

依据：约定 v0.4 第 4 节、5.7、第 6 节；补充文件 6.2、第 7 节、第 8 节。不读实现。
假网关的 POST /v1/inject 在本文件里配（adapter_util 不改）：照补充文件 6.2 应答
{"ok": true, "duplicate": 布尔}，并像真网关一样把 key 截到 120 个字符再去重（第 8 节）。
"""
import json
import logging
import threading
import time
from types import SimpleNamespace

import pytest

from . import adapter_util as au
from .adapter_util import adapter, console_guard  # noqa: F401  夹具

GW_INJECT = "/v1/inject"
OTHER_CHAT_ID = 1009876543210888          # 另一只 bot 的假聊天号
TEXT = "测试通知：用户刚发了一条动态，内容是今天下雨了。"
KEY = "user-moment:9001"


def notify_url(bot_id):
    return "%s/%s/notify" % (au.BOTS, bot_id)


def req(text=TEXT, key=KEY, source="user_moment"):
    return {"source": source, "text": text, "key": key}


class Inbox:
    """假网关 POST /v1/inject 的应答：按 key 的前 120 个字符去重，记下真正收下的。"""

    def __init__(self):
        self.seen = set()
        self.delivered = []

    def __call__(self, rec):
        body = rec["json"]
        if not isinstance(body, dict) or not isinstance(body.get("key"), str):
            return 400, {"ok": False}
        k = body["key"][:120]
        if k in self.seen:
            return 200, {"ok": True, "duplicate": True}
        self.seen.add(k)
        self.delivered.append(body)
        return 200, {"ok": True, "duplicate": False}


def chat_bot(adapter, bot_id, chat_id, *, enabled=True, gw=None):
    """摆一只 bot，配置顶层写上 chat_id；给了 gw 就让它的网关指向 gw 并刷心跳。"""
    bot = adapter.bot(bot_id, enabled=enabled, gw_port=gw.port if gw else None)
    cfg = json.loads(bot.yml.read_text(encoding="utf-8"))
    cfg["chat_id"] = chat_id
    bot.yml.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    if enabled and gw is not None:
        adapter.heartbeat.tick(bot_id, pid=4242)
    return bot


def inject_gateway(adapter):
    gw = adapter.gateway()
    inbox = Inbox()
    gw.on_fn("POST", GW_INJECT, inbox)
    return gw, inbox


@pytest.fixture
def target(adapter):
    """botalpha（另一个聊天号）与 botbravo（聊天号 au.CHAT_ID），各有自己的假网关；目标是 botbravo。"""
    gw_a, inbox_a = inject_gateway(adapter)
    gw_b, inbox_b = inject_gateway(adapter)
    chat_bot(adapter, "botalpha", OTHER_CHAT_ID, gw=gw_a)
    chat_bot(adapter, "botbravo", int(au.CHAT_ID), gw=gw_b)
    return SimpleNamespace(gw=gw_b, inbox=inbox_b, other_gw=gw_a)


def injects(gw):
    return [r for r in gw.requests if (r["method"], r["path"]) == ("POST", GW_INJECT)]


# ============================================================ 正常投递

def test_通知_正常投递_回delivered_只发给这只bot的网关_chat_id取它自己配置里的(adapter, target):
    body = adapter.ok(adapter.call("POST", notify_url("botbravo"), req()))
    assert body.get("status") == "delivered", body
    sent = injects(target.gw)
    assert len(sent) == 1, "应恰好向 botbravo 的网关发一次 POST /v1/inject：%r" % target.gw.requests
    got = sent[0]["json"]
    assert str(got.get("chat_id")) == au.CHAT_ID, "chat_id 应取 botbravo 配置里的那个"
    assert got.get("source") == "user_moment" and got.get("key") == KEY, "source 与 key 应原样转交"
    assert isinstance(got.get("text"), str) and TEXT in got["text"], "要投的那段话没交给网关"
    assert target.other_gw.requests == [], "给 botbravo 投通知，却找了 botalpha 的网关"


def test_通知_同一个key交两次_第二次回duplicate_bot只收到一次(adapter, target):
    first = adapter.ok(adapter.call("POST", notify_url("botbravo"), req()))
    second = adapter.ok(adapter.call("POST", notify_url("botbravo"), req()))
    assert first.get("status") == "delivered", first
    assert second.get("status") == "duplicate", "同一个 key 第二次应回 duplicate：%r" % second
    assert len(target.inbox.delivered) == 1, "bot 收到了不止一次：%r" % target.inbox.delivered


def test_通知_超过120个字符的key_前120个字符相同也能分开_同一个长key仍去重(adapter, target):
    head = "moment-reply:" + "9" * 120
    k1, k2 = head + ":A1", head + ":B2"
    r1 = adapter.ok(adapter.call("POST", notify_url("botbravo"), req(key=k1, source="moment_reply")))
    r2 = adapter.ok(adapter.call("POST", notify_url("botbravo"), req(key=k2, source="moment_reply")))
    r3 = adapter.ok(adapter.call("POST", notify_url("botbravo"), req(key=k1, source="moment_reply")))
    assert (r1.get("status"), r2.get("status")) == ("delivered", "delivered"), \
        "两个不同的长 key 只因前 120 个字符相同就被当成重复：%r %r" % (r1, r2)
    assert r3.get("status") == "duplicate", "同一个长 key 第二次应回 duplicate：%r" % r3
    keys = [d["key"] for d in target.inbox.delivered]
    assert len(keys) == 2 and keys[0] != keys[1], keys
    assert all(len(k) <= 120 for k in keys), "交给网关的 key 超过 120 个字符会被网关截断：%r" % keys


BOUNDARY = {
    "key一个字符": req(key="k"),
    "key恰好120个字符_原样交给网关": req(key="k" * 120),
    "key恰好200个字符": req(key="k" * 200),
    "key是200个中文字符_按字符数算": req(key="键" * 200),
    "text恰好16KiB字节": req(text="字" * 5461 + "a"),
    "source是moment_reply": req(source="moment_reply", key="moment-reply:7:8"),
}


@pytest.mark.parametrize("payload", list(BOUNDARY.values()), ids=list(BOUNDARY))
def test_通知_边界上的合规入参_照常投递(adapter, target, payload):
    body = adapter.ok(adapter.call("POST", notify_url("botbravo"), payload))
    assert body.get("status") == "delivered", body
    sent = injects(target.gw)
    assert len(sent) == 1, target.gw.requests
    got = sent[0]["json"]["key"]
    if len(payload["key"]) <= 120:
        assert got == payload["key"], "不超过 120 个字符的 key 应原样转交：%r" % got
    else:
        assert len(got) <= 120, "超过 120 个字符的 key 应换成摘要再交：%r" % got


# ============================================================ 错误契约

BAD = {
    "source缺失": ({"text": TEXT, "key": KEY}, None),
    "source不在两种里": (req(source="user_post"), None),
    "source不是字符串": (req(source=1), None),
    "text缺失": ({"source": "user_moment", "key": KEY}, None),
    "text是空串": (req(text=""), None),
    "text全是空白": (req(text=" \n\t \r\n"), None),
    "text不是字符串": (req(text=["一段话"]), None),
    "text超过16KiB字节_字符数没超": (req(text="字" * 5462), None),
    "key缺失": ({"source": "user_moment", "text": TEXT}, None),
    "key是空串": (req(key=""), None),
    "key超过200个字符": (req(key="k" * 201), None),
    "key不是字符串": (req(key=123), None),
    "请求体是数组": (au.NO_BODY, "[]"),
    "请求体是字符串": (au.NO_BODY, '"text"'),
    "坏JSON": (au.NO_BODY, "{bad"),
    "空体": (au.NO_BODY, ""),
}


@pytest.mark.parametrize("payload,raw", list(BAD.values()), ids=list(BAD))
def test_通知_入参不合规_回422_invalid_不发任何请求(adapter, target, payload, raw):
    adapter.expect_error(adapter.call("POST", notify_url("botbravo"), payload, raw=raw), 422, "invalid")
    assert target.gw.requests == [] and target.other_gw.requests == [], "校验没过却找了网关"


def test_通知_bot停用_回409_stopped_网关零请求(adapter):
    gw, inbox = inject_gateway(adapter)
    chat_bot(adapter, "botalpha", int(au.CHAT_ID), enabled=False, gw=gw)
    adapter.expect_error(adapter.call("POST", notify_url("botalpha"), req()), 409, "stopped")
    assert gw.requests == [], "bot 停用着，却去找了它的网关：%r" % gw.requests


def test_通知_网关连不上_回503_unavailable(adapter):
    adapter.bot("botalpha", gw_port=au.free_port())          # 端口没人听
    chat = adapter.sandbox.dsh_cfg / "botalpha.yml"
    cfg = json.loads(chat.read_text(encoding="utf-8"))
    cfg["chat_id"] = int(au.CHAT_ID)
    chat.write_text(json.dumps(cfg), encoding="utf-8")
    adapter.expect_error(adapter.call("POST", notify_url("botalpha"), req()), 503, "unavailable")


def test_通知_网关卡住不回_10秒内回503_unavailable(adapter):
    release = threading.Event()
    gw = adapter.gateway()
    gw.on_fn("POST", GW_INJECT, lambda rec: (release.wait(14.0), (200, {"ok": True, "duplicate": False}))[1])
    chat_bot(adapter, "botalpha", int(au.CHAT_ID), gw=gw)
    started = time.time()
    try:
        resp = adapter.call("POST", notify_url("botalpha"), req())
        took = time.time() - started
    finally:
        release.set()
    adapter.expect_error(resp, 503, "unavailable")
    assert took < 11.0, "网关卡住时应在 10 秒左右放弃，实际等了 %.1f 秒" % took


ODD_REPLIES = {
    "网关回500": (500, {"ok": False}),
    "网关回ok为假": (200, {"ok": False, "duplicate": False}),
    "网关回的不是JSON": (200, "oops"),
    "网关回JSON数组": (200, []),
}


@pytest.mark.parametrize("status,payload", list(ODD_REPLIES.values()), ids=list(ODD_REPLIES))
def test_通知_网关回了别的_回500_internal_文案固定(adapter, status, payload):
    gw = adapter.gateway().on("POST", GW_INJECT, status, payload)
    chat_bot(adapter, "botalpha", int(au.CHAT_ID), gw=gw)
    body = adapter.expect_error(adapter.call("POST", notify_url("botalpha"), req()), 500, "internal")
    assert body["message"] == au.INTERNAL_MESSAGE, body


@pytest.mark.parametrize("bot_id,status,code", [("..", 422, "invalid"), ("a%2Fb", 422, "invalid"),
                                                ("a" * 65, 422, "invalid"), ("ghost", 404, "unknown_bot")],
                         ids=["两个点", "编码的斜杠", "65个字符", "没有这只bot"])
def test_通知_bot_id不合规回422_没有这只回404_都不找网关(adapter, target, bot_id, status, code):
    adapter.expect_error(adapter.call("POST", notify_url(bot_id), req()), status, code)
    assert target.gw.requests == [] and target.other_gw.requests == [], "不该去找任何网关"


def test_meta_v04_能力清单恰好十个键_多了notify与生活设定(adapter):
    body = adapter.ok(adapter.call("GET", au.META))
    want = dict(au.CAPABILITIES, **{"bot.notify": True, "bot.life_settings": True})
    assert body.get("capabilities") == want, "能力清单应恰好是十个键：%r" % body.get("capabilities")


# ============================================================ 敏感信息

def test_泄密_通知与换供应商超时的各种响应和日志里_没有令牌密钥聊天号(adapter, target, monkeypatch,
                                                         caplog, capfd):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("DSH_CONSOLE_PROVIDER_BUDGET", "1")
    release = threading.Event()
    gw_slow = adapter.model_gateway()
    orig = gw_slow.routes[("POST", au.GW_MODEL)]
    gw_slow.on_fn("POST", au.GW_MODEL, lambda rec: (release.wait(6.0), orig(rec))[1])
    chat_bot(adapter, "botcharlie", OTHER_CHAT_ID, gw=gw_slow)    # 它的网关不认 /v1/inject
    chat_bot(adapter, "botdelta", int(au.CHAT_ID), enabled=False)
    calls = [
        ("POST", notify_url("botbravo"), req()),                       # delivered
        ("POST", notify_url("botbravo"), req()),                       # duplicate
        ("POST", notify_url("botbravo"), req(text="  ")),              # 422
        ("POST", notify_url("botdelta"), req()),                       # 409
        ("POST", notify_url("botcharlie"), req()),                     # 网关不认 inject
        ("POST", au.provider_url("botcharlie"),
         {"provider_id": au.CFG_PROVIDER, "model": au.VENDOR_A_MODELS[1]}),   # 写请求没等到回复
    ]
    resps = []
    try:
        for method, path, body in calls:
            resps.append(adapter.call(method, path, body))
    finally:
        release.set()
    assert [r.status_code for r in resps] == [200, 200, 422, 409, 500, 200], \
        "前提没满足（各条路径没真走到）：%r" % [r.status_code for r in resps]
    texts = [au.response_text(r) for r in resps]
    out, err = capfd.readouterr()
    texts += [caplog.text, out, err]
    for name, secret in au.SECRETS.items():
        for i, text in enumerate(texts):
            assert secret not in text, "第 %d 段（响应或日志）里出现了%s" % (i + 1, name)
