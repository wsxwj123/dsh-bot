# -*- coding: utf-8 -*-
"""③.0 共用规则 + ③.4 能力键 + ③.5 里与三个接口都相关的不变量。"""
import logging
import os

import pytest

from . import vc_util as vu

SECRET_DM = "绝密私聊句子Q7只许出现在响应里"
SECRET_MEM = "绝密记忆句子M3只许出现在响应里"
SECRET_CALL = "绝密电话句子V9只许转给网关"


def _full_bot(w, bot_id=vu.BOT, **kw):
    """一只有账本、有记忆、有假网关的 bot。"""
    gw = w.gateway()
    bot = w.bot(bot_id, gw=gw, **kw)
    lg = w.ledger(bot_id)
    lg.inb("今天好累啊" + SECRET_DM)
    lg.out("抱抱，先去洗个热水澡？")
    lg.close()
    vu.put_file(w.memory_path(bot_id), "# Memory\n" + SECRET_MEM + "\n")
    return bot, gw


# ---------------------------------------------------------------- 鉴权
AUTH_PATHS = [
    vu.dm_tail_url(vu.BOT),
    vu.memory_url(vu.BOT),
    vu.notify_url(vu.BOT),
    vu.PREFIX + "/bots/bot4/dm_tail/x",     # 不存在的路径
    vu.PREFIX + "/bots/ghost/memory",       # 不存在的 bot
]


@pytest.mark.parametrize("auth", [None, "Bearer " + vu.WRONG_TOKEN, vu.ADAPTER_TOKEN],
                         ids=["没带令牌", "令牌不对", "少了Bearer前缀"])
@pytest.mark.parametrize("method", ["GET", "POST", "HEAD", "OPTIONS"])
@pytest.mark.parametrize("path", AUTH_PATHS, ids=["dm_tail", "memory", "notify", "不存在的路径", "不存在的bot"])
def test_没带或带错令牌_任何路径任何方法都回401_且不碰网关(w, path, method, auth):
    _bot, gw = _full_bot(w)
    body = vu.voice_body() if method == "POST" else None
    resp = w.call(method, path, body, auth=auth)
    assert resp.status_code == 401, "应为 401，实际 %s" % resp.status_code
    if method != "HEAD":
        assert vu.body_of(resp) == vu.UNAUTHORIZED
    text = resp.get_data(as_text=True)
    assert SECRET_DM not in text and SECRET_MEM not in text, "401 响应里带出了私聊或记忆内容"
    assert gw.requests == [], "没过鉴权却去找了网关：%r" % gw.requests


# ---------------------------------------------------------------- 能力键
def test_能力键_电话两项报true_原有各项不变(w):
    caps = vu.expect_ok(w.call("GET", vu.META)).get("capabilities")
    assert isinstance(caps, dict), "meta 里应有 capabilities 对象"
    assert caps.get("bot.voicecall.read") is True, "bot.voicecall.read 应为 true，实际 %r" % caps.get("bot.voicecall.read")
    assert caps.get("bot.notify.voice_call") is True, "bot.notify.voice_call 应为 true，实际 %r" % caps.get("bot.notify.voice_call")
    changed = {k: (v, caps.get(k, "<没了>")) for k, v in vu.CAPABILITIES_BEFORE.items() if caps.get(k, "<没了>") != v}
    assert not changed, "原有能力键被改了（原值, 现值）：%r" % changed


# ---------------------------------------------------------------- 路径、方法、编号、名单、判定顺序
D, M, N = vu.dm_tail_url, vu.memory_url, vu.notify_url
BAD_IDS = ["..", ".", "a" * 65, "%E4%B8%AD%E6%96%87", "a%2Fb", "a%20b", "a%00b"]

ERROR_ROWS = [
    # (说明, 方法, 路径, 请求体, 状态, code, message, Allow)
    ("dm_tail 多一段路径", "GET", D(vu.BOT) + "/x", None, 404, "not_found", vu.NOT_FOUND, None),
    ("memory 多一段路径", "GET", M(vu.BOT) + "/x", None, 404, "not_found", vu.NOT_FOUND, None),
    ("notify 多一段路径", "POST", N(vu.BOT) + "/x", "voice", 404, "not_found", vu.NOT_FOUND, None),
    ("dm_tail 用 POST", "POST", D(vu.BOT), None, 405, "method_not_allowed", vu.METHOD_NOT_ALLOWED, "GET"),
    ("memory 用 DELETE", "DELETE", M(vu.BOT), None, 405, "method_not_allowed", vu.METHOD_NOT_ALLOWED, "GET"),
    ("memory 用 PUT", "PUT", M(vu.BOT), None, 405, "method_not_allowed", vu.METHOD_NOT_ALLOWED, "GET"),
    ("notify 用 GET", "GET", N(vu.BOT), None, 405, "method_not_allowed", vu.METHOD_NOT_ALLOWED, "POST"),
    ("方法先于编号：坏编号 + 错方法", "POST", D("a%2Fb"), None, 405, "method_not_allowed", vu.METHOD_NOT_ALLOWED, "GET"),
    ("名单没有：dm_tail", "GET", D("ghost"), None, 404, "unknown_bot", vu.UNKNOWN_BOT, None),
    ("名单没有：memory", "GET", M("ghost"), None, 404, "unknown_bot", vu.UNKNOWN_BOT, None),
    ("名单没有：notify", "POST", N("ghost"), "voice", 404, "unknown_bot", vu.UNKNOWN_BOT, None),
    ("入参先于名单：不存在的 bot + 坏参数", "GET", D("ghost") + "?n=0", None, 422, "invalid", vu.N_BAD, None),
    ("编号先于入参：坏编号 + 坏参数", "GET", D("a%2Fb") + "?n=0", None, 422, "invalid", vu.BAD_BOT_ID, None),
] + [
    ("坏编号 %r：%s" % (bid, ep), meth, fn(bid), body, 422, "invalid", vu.BAD_BOT_ID, None)
    for bid in BAD_IDS
    for ep, meth, fn, body in (("dm_tail", "GET", D, None), ("memory", "GET", M, None), ("notify", "POST", N, "voice"))
]


@pytest.mark.parametrize("row", ERROR_ROWS, ids=[r[0] for r in ERROR_ROWS])
def test_共用错误契约_路径方法编号名单与判定顺序(w, row):
    _what, method, path, body, status, code, message, allow = row
    _full_bot(w)
    # 只有目录、没有配置的 "ghost"：不在名单里，它目录里的东西一个字都不能被读出来
    ghost = vu.Ledger(w.botdir("ghost") / "state" / "ledger.sqlite")
    ghost.inb("ghost 的私聊不该被读到")
    ghost.close()
    vu.put_file(w.memory_path("ghost"), "ghost 的记忆不该被读到")
    gw = w.gateway()
    resp = w.call(method, path, vu.voice_body() if body == "voice" else None)
    vu.expect_error(resp, status, code, message)
    if allow is not None:
        got = {m.strip().upper() for m in (resp.headers.get("Allow") or "").split(",") if m.strip()}
        assert got == {allow}, "Allow 头应为 %r，实际 %r" % (allow, resp.headers.get("Allow"))
    assert "ghost 的" not in resp.get_data(as_text=True)
    assert gw.requests == []


# ---------------------------------------------------------------- 只读接口：迁出、停用
@pytest.mark.parametrize("endpoint", ["dm_tail", "memory"])
@pytest.mark.parametrize("state,status,code,message", [
    ("迁出标记在、仍启用", 409, "moved", vu.MOVED),
    ("迁出标记在、已停用（迁出优先）", 409, "moved", vu.MOVED),
    ("没迁出、已停用", 409, "stopped", vu.STOPPED),
])
def test_只读接口_迁出或停用_409_不返回任何内容(w, endpoint, state, status, code, message):
    _full_bot(w, enabled=("已停用" not in state))
    if "迁出标记在" in state:
        w.moved()
    resp = w.dm_tail() if endpoint == "dm_tail" else w.memory()
    vu.expect_error(resp, status, code, message)
    text = resp.get_data(as_text=True)
    assert SECRET_DM not in text and SECRET_MEM not in text


# ---------------------------------------------------------------- 只读接口不改东西
def test_只读接口_读前读后文件一字不变_带no_store(w):
    bot, _gw = _full_bot(w)
    vu.put_file(bot.channel / "memory" / "topic-旅行.md", "主题记忆\n")
    ledger_before = vu.file_mark(w.ledger_path())
    mem_before = vu.tree_mark(bot.channel / "memory")
    bot_before = vu.tree_mark(bot.botdir)
    cfg_before = vu.tree_mark(w.sb.dsh_cfg)
    home_top = sorted(p.name for p in w.home.iterdir())
    for _ in range(2):
        for resp in (w.dm_tail(), w.memory()):
            vu.expect_ok(resp)
            assert "no-store" in (resp.headers.get("Cache-Control") or ""), \
                "只读接口成功时应带 Cache-Control: no-store，实际 %r" % resp.headers.get("Cache-Control")
    assert vu.file_mark(w.ledger_path()) == ledger_before, "读私聊尾巴改动了账本文件"
    assert vu.tree_mark(bot.channel / "memory") == mem_before, "读记忆改动了 memory 目录"
    assert vu.tree_mark(w.sb.dsh_cfg) == cfg_before, "读接口改动了配置根"
    assert sorted(p.name for p in w.home.iterdir()) == home_top, "读接口在家目录下新建了东西"
    after = vu.tree_mark(bot.botdir)
    extra = set(after) - set(bot_before)
    allowed = {os.path.join("state", "ledger.sqlite-wal"), os.path.join("state", "ledger.sqlite-shm")}
    assert extra <= allowed, "读接口新建了文件或目录：%r" % sorted(extra - allowed)
    gone = set(bot_before) - set(after)
    assert not gone, "读接口删掉了：%r" % sorted(gone)


# ---------------------------------------------------------------- 8a：家目录本身是软链路径
def test_家目录是软链路径_两个读接口照常能读(w, sandbox, monkeypatch):
    link = sandbox.tmp / "dsh-home-link"
    os.symlink(str(sandbox.dsh_home), str(link))
    monkeypatch.setenv("DSH_BOT_HOME", str(link))
    _full_bot(w)                       # 配置里的频道目录写的也是软链那条路径
    msgs = vu.expect_ok(w.dm_tail())["messages"]
    assert [m["text"] for m in msgs] == ["今天好累啊" + SECRET_DM, "抱抱，先去洗个热水澡？"]
    mem = vu.expect_ok(w.memory())
    assert mem["exists"] is True and SECRET_MEM in mem["text"]


# ---------------------------------------------------------------- 日志不带正文
def test_日志里不出现私聊记忆电话正文和主人聊天号(w, caplog, capfd):
    caplog.set_level(logging.DEBUG)
    bot, _gw = _full_bot(w)
    vu.expect_ok(w.dm_tail())
    vu.expect_ok(w.memory())
    vu.expect_ok(w.notify(vu.voice_body(text="哥哥在电话里说了：" + SECRET_CALL)))
    out, err = capfd.readouterr()
    logs = caplog.text + "\n" + out + "\n" + err
    for name, s in (("私聊文字", SECRET_DM), ("记忆文字", SECRET_MEM), ("电话 text", SECRET_CALL),
                    ("主人聊天号", str(vu.OWNER_CHAT))):
        assert s not in logs, "日志里出现了%s" % name
