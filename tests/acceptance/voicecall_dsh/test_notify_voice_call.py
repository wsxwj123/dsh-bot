# -*- coding: utf-8 -*-
"""③.3 POST /adapter/v1/bots/{bot_id}/notify 的新来源 voice_call（适配服务这一层）。

INTERFACE 没写适配服务转给网关时用哪个路径、请求体长什么样，所以假网关接任何路径，
只断言「投了几次」「投出去的内容里有没有电话原文、key、主人私聊编号」。
"""
import os
import time

import pytest

from . import vc_util as vu

OMIT = vu.OMIT
DELIVERED = {"ok": True, "status": "delivered"}
DUPLICATE = {"ok": True, "status": "duplicate"}


def _bracket_swapped(s):
    return s.replace("⟦", "〚").replace("⟧", "〛")


@pytest.mark.parametrize("features", [None, {"moments": False, "voice": False, "proactive": False, "group": False}],
                         ids=["功能开关默认", "功能开关全关也照收"])
def test_正常投递_回delivered_电话原文key与主人私聊编号都交给网关(w, features):
    gw = w.gateway()
    w.bot(gw=gw, extra={"features": features} if features else None)
    resp = w.notify(vu.voice_body())
    assert vu.expect_ok(resp) == DELIVERED
    posts = gw.posts()
    assert len(posts) == 1, "应恰好投给网关一次，实际 %d 次" % len(posts)
    values = list(vu.scalars(posts[0]["json"]))
    assert vu.VOICE_TEXT in values or _bracket_swapped(vu.VOICE_TEXT) in values, \
        "投给网关的内容里应原样带着电话页拼好的 text，实际 %r" % posts[0]["body"][:300]
    assert vu.VOICE_KEY in values, "投给网关的内容里应带着 key"
    assert vu.OWNER_CHAT in values or str(vu.OWNER_CHAT) in values, "应投进主人私聊（带主人私聊编号）"


GW_REPLIES = [
    ("回duplicate为true", 200, {"ok": True, "duplicate": True}, 200, DUPLICATE),
    ("没有duplicate字段", 200, {"ok": True}, 200, DELIVERED),
    ("duplicate为false", 200, {"ok": True, "duplicate": False}, 200, DELIVERED),
    ("duplicate是字符串true", 200, {"ok": True, "duplicate": "true"}, 200, DELIVERED),
    ("duplicate是1", 200, {"ok": True, "duplicate": 1}, 200, DELIVERED),
    ("200但ok为false", 200, {"ok": False}, 500, vu.GW_WEIRD),
    ("200但ok是字符串", 200, {"ok": "true"}, 500, vu.GW_WEIRD),
    ("200但不是JSON", 200, "<html>oops</html>", 500, vu.GW_WEIRD),
    ("200但是JSON数组", 200, [True], 500, vu.GW_WEIRD),
    ("回400", 400, {"ok": False, "error": "bad"}, 500, vu.GW_WEIRD),
    ("回401", 401, {"ok": False, "error": "unauthorized"}, 500, vu.GW_WEIRD),
    ("回500", 500, {"ok": False}, 500, vu.GW_WEIRD),
    ("回502纯文本", 502, "bad gateway", 500, vu.GW_WEIRD),
]


@pytest.mark.parametrize("row", GW_REPLIES, ids=[r[0] for r in GW_REPLIES])
def test_网关回包到适配服务结果的对应(w, row):
    _what, gw_status, gw_payload, status, expected = row
    gw = w.gateway(lambda rec: (gw_status, gw_payload))
    w.bot(gw=gw)
    resp = w.notify(vu.voice_body())
    if status == 200:
        assert vu.expect_ok(resp) == expected
    else:
        vu.expect_error(resp, 500, "internal", expected)
    assert len(gw.posts()) == 1, "适配服务自己不该重试（重试归壳），实际投了 %d 次" % len(gw.posts())


BIG = "a" * (70 * 1024)
BODY_ROWS = [
    # (说明, 请求体 dict 或 ("raw", 字节), 期望 message；None = 合规应 delivered)
    ("不是JSON", ("raw", b"abc"), vu.BODY_BAD),
    ("空请求体", ("raw", b""), vu.BODY_BAD),
    ("JSON数组", ("raw", b"[]"), vu.BODY_BAD),
    ("JSON字符串", ("raw", b'"voice_call"'), vu.BODY_BAD),
    ("JSON null", ("raw", b"null"), vu.BODY_BAD),
    ("坏UTF-8", ("raw", b'{"source":"voice_call","text":"\xff\xfe","key":"k"}'), vu.BODY_BAD),
    ("超过64KiB", vu.voice_body(text=BIG), vu.BODY_BAD),
    ("source缺", vu.voice_body(source=OMIT), vu.SOURCE_BAD),
    ("source拼错", vu.voice_body(source="voice"), vu.SOURCE_BAD),
    ("source大写", vu.voice_body(source="VOICE_CALL"), vu.SOURCE_BAD),
    ("source是数字", vu.voice_body(source=1), vu.SOURCE_BAD),
    ("text缺", vu.voice_body(text=OMIT), vu.TEXT_BAD),
    ("text空串", vu.voice_body(text=""), vu.TEXT_BAD),
    ("text全是空白", vu.voice_body(text="  \n\t　 "), vu.TEXT_BAD),
    ("text是数字", vu.voice_body(text=123), vu.TEXT_BAD),
    ("text是列表", vu.voice_body(text=["哥哥说了"]), vu.TEXT_BAD),
    ("text超16KiB一个字节", vu.voice_body(text="a" * (16 * 1024 + 1)), vu.TEXT_BAD),
    ("text中文按字节超16KiB", vu.voice_body(text="啊" * 5462), vu.TEXT_BAD),
    ("key缺", vu.voice_body(key=OMIT), vu.KEY_BAD),
    ("key空串", vu.voice_body(key=""), vu.KEY_BAD),
    ("key 201个字符", vu.voice_body(key="k" * 201), vu.KEY_BAD),
    ("key 201个中文", vu.voice_body(key="键" * 201), vu.KEY_BAD),
    ("key是数字", vu.voice_body(key=12345), vu.KEY_BAD),
    ("key是null", vu.voice_body(key=None), vu.KEY_BAD),
    ("带了chat_id", vu.voice_body(chat_id=vu.OWNER_CHAT), vu.CHAT_FIELDS_BAD),
    ("带了human_ts", vu.voice_body(human_ts=1794200000), vu.CHAT_FIELDS_BAD),
    ("chat_id为null也算带了", vu.voice_body(chat_id=None), vu.CHAT_FIELDS_BAD),
    # 合规的边界
    ("text正好16KiB", vu.voice_body(text="啊" * 5461 + "a"), None),
    ("text只有一个字", vu.voice_body(text="嗯"), None),
    ("key 1个字符", vu.voice_body(key="k"), None),
    ("key 200个中文", vu.voice_body(key="键" * 200), None),
    ("key带emoji", vu.voice_body(key="voice-call:📞-1"), None),
    ("不认识的字段忽略", vu.voice_body(extra_field="x"), None),
]


@pytest.mark.parametrize("row", BODY_ROWS, ids=[r[0] for r in BODY_ROWS])
def test_请求体校验_不合规422不投_合规边界照投(w, row):
    _what, body, message = row
    gw = w.gateway()
    w.bot(gw=gw)
    if isinstance(body, tuple):
        resp = w.call("POST", vu.notify_url(vu.BOT), raw=body[1])
    else:
        resp = w.notify(body)
    if message is None:
        assert vu.expect_ok(resp) == DELIVERED
        assert len(gw.posts()) == 1
    else:
        vu.expect_error(resp, 422, "invalid", message)
        assert gw.requests == [], "请求体不合规却去找了网关"


@pytest.mark.parametrize("case,status,code,message", [
    ("不存在的bot带坏请求体", 422, "invalid", vu.BODY_BAD),
    ("停用", 409, "stopped", vu.STOPPED),
    ("停用的bot带坏请求体", 422, "invalid", vu.TEXT_BAD),
    ("已迁出且停用", 409, "stopped", vu.STOPPED),
    ("停用且没有主人聊天号", 409, "stopped", vu.STOPPED),
    ("没有主人聊天号", 500, "internal", vu.NOTIFY_NO_CHAT),
    ("主人聊天号是负数", 500, "internal", vu.NOTIFY_NO_CHAT),
    ("主人聊天号是字母", 500, "internal", vu.NOTIFY_NO_CHAT),
])
def test_投不了的配置状态_按判定顺序报错_网关一次都没被找(w, case, status, code, message):
    gw = w.gateway()
    chat = {"没有主人聊天号": OMIT, "停用且没有主人聊天号": OMIT, "主人聊天号是负数": -vu.OWNER_CHAT,
            "主人聊天号是字母": "abc"}.get(case, vu.OWNER_CHAT)
    w.bot(gw=gw, enabled="停用" not in case, chat_id=chat)
    if "迁出" in case:
        w.moved()
    if case == "不存在的bot带坏请求体":
        resp = w.call("POST", vu.notify_url("ghost"), raw=b"[]")
    elif "坏请求体" in case:
        resp = w.notify(vu.voice_body(text="   "))
    else:
        resp = w.notify(vu.voice_body())
    vu.expect_error(resp, status, code, message)
    assert gw.requests == [], "不该投的却去找了网关：%r" % gw.requests


def test_停用时被拒的那条_启用后不补投(w):
    gw = w.gateway()
    bot = w.bot(gw=gw, enabled=False)
    vu.expect_error(w.notify(vu.voice_body()), 409, "stopped", vu.STOPPED)
    w.set_cfg(bot, enabled=True)
    second = "voice-call:call-after-enable"
    assert vu.expect_ok(w.notify(vu.voice_body(key=second))) == DELIVERED
    time.sleep(0.5)
    posts = gw.posts()
    assert len(posts) == 1, "启用后只应投新的这一条，实际 %d 次" % len(posts)
    values = list(vu.scalars(posts[0]["json"]))
    assert second in values and vu.VOICE_KEY not in values, "停用时被拒的那条被补投了"


@pytest.mark.parametrize("case", ["没有端口文件", "端口上没人听", "没有口令文件"])
def test_网关连不上_503(w, case):
    gw = w.gateway()
    if case == "没有端口文件":
        w.bot()
    elif case == "端口上没人听":
        w.bot(gw_port=vu.closed_port())
    else:
        bot = w.bot(gw=gw)
        os.remove(str(bot.botdir / "state" / "api.key"))
    vu.expect_error(w.notify(vu.voice_body()), 503, "unavailable", vu.GW_DOWN)
    assert gw.requests == []


def test_网关8秒不回_503_且在壳的10秒超时之前回(w):
    gw = w.gateway(lambda rec: (200, {"ok": True}, 9.5))
    w.bot(gw=gw)
    t0 = time.monotonic()
    resp = vu.call_with_deadline(lambda: w.notify(vu.voice_body()), 15, "网关卡住时的 notify")
    took = time.monotonic() - t0
    vu.expect_error(resp, 503, "unavailable", vu.GW_SLOW)
    assert 7.0 <= took < 10.0, "应在约 8 秒时放弃、10 秒内回壳，实际 %.1f 秒" % took
