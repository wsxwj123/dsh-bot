# -*- coding: utf-8 -*-
"""③.1 GET /adapter/v1/bots/{bot_id}/dm_tail：私聊尾巴。"""
import os
import sqlite3

import pytest

from . import vc_util as vu

OMIT = vu.OMIT


def _texts(body):
    return [(m["role"], m["text"]) for m in body["messages"]]


def _messages(w, query=""):
    return vu.expect_ok(w.dm_tail(query=query))["messages"]


# ---------------------------------------------------------------- 正常路径
def test_正常_时间正序_角色_秒级ts_同毫秒主人在前_同侧按行号(w):
    w.bot()
    lg = w.ledger()
    t0 = lg.ago(60, 789)
    t3 = lg.ago(30, 123)
    t5 = lg.ago(10, 999)
    lg.out("我先说一句", sent_at=t0)
    lg.inb("嗯然后呢", ts=t0 + 1500)
    lg.out("同一毫秒的bot话", sent_at=t3)        # 先入账
    lg.inb("同一毫秒的主人话", ts=t3)            # 后入账，但同毫秒主人在前
    lg.inb("同毫秒第一句", ts=t5)
    lg.inb("同毫秒第二句", ts=t5)
    lg.close()
    msgs = _messages(w)
    assert msgs == [
        {"role": "assistant", "text": "我先说一句", "ts": t0 // 1000},
        {"role": "user", "text": "嗯然后呢", "ts": (t0 + 1500) // 1000},
        {"role": "user", "text": "同一毫秒的主人话", "ts": t3 // 1000},
        {"role": "assistant", "text": "同一毫秒的bot话", "ts": t3 // 1000},
        {"role": "user", "text": "同毫秒第一句", "ts": t5 // 1000},
        {"role": "user", "text": "同毫秒第二句", "ts": t5 // 1000},
    ]
    assert all(type(m["ts"]) is int for m in msgs), "ts 应是整数秒"


def test_只取这只bot与主人的私聊_群聊别的私聊别的bot都不出现(w):
    w.bot()
    w.bot(vu.OTHER_BOT)                          # 另一只 bot，主人聊天号相同
    lg = w.ledger()
    lg.inb("主人私聊里的话", minutes=20)
    lg.out("bot私聊回复", minutes=19)
    lg.inb("主人私聊里的话", minutes=18, chat=vu.GROUP_CHAT)       # 群里同文字
    lg.inb("群里的另一句话", minutes=17, chat=vu.GROUP_CHAT)
    lg.out("bot在群里说的话", minutes=16, chat=vu.GROUP_CHAT)
    lg.inb("别人私聊的话", minutes=15, chat=vu.FRIEND_CHAT)
    lg.out("bot回别人的话", minutes=14, chat=vu.FRIEND_CHAT)
    lg.close()
    other = w.ledger(vu.OTHER_BOT)
    other.inb("另一只bot的私聊内容", minutes=5)
    other.out("另一只bot的回复", minutes=4)
    other.close()
    assert _texts({"messages": _messages(w)}) == [("user", "主人私聊里的话"), ("assistant", "bot私聊回复")]


@pytest.mark.parametrize("chat_id,ok", [
    (vu.OWNER_CHAT, True),
    (str(vu.OWNER_CHAT), True),
    ("000" + str(vu.OWNER_CHAT), True),
    (OMIT, False),
    (-vu.OWNER_CHAT, False),
    (0, False),
    ("abc", False),
    (str(vu.OWNER_CHAT) + " ", False),
    ("１００９８７６５４３", False),             # 全角数字不是 ASCII 数字
    (float(vu.OWNER_CHAT), False),
    (True, False),
], ids=["正整数", "数字字符串", "带前导零", "没写", "负数", "零", "字母", "带空格", "全角数字", "小数", "布尔"])
def test_主人聊天号的各种写法(w, chat_id, ok):
    w.bot(chat_id=chat_id)
    lg = w.ledger()
    lg.inb("私聊里的一句话")
    lg.close()
    resp = w.dm_tail()
    if ok:
        assert _texts(vu.expect_ok(resp)) == [("user", "私聊里的一句话")]
    else:
        vu.expect_error(resp, 500, "internal", vu.DM_NO_CHAT)


# ---------------------------------------------------------------- 主人侧：候选与清洗
CLEAN_ROWS = [
    # (说明, kind, 原文, 期望文字；None = 整条不出现)
    ("程序合成消息", "synthetic", "朋友圈通知：她发了新动态", None),
    ("电话回写本身", "synthetic", vu.VOICE_TEXT, None),
    ("聊天命令", "command", "/model", None),
    ("含主动消息标记", "user", "[self-initiate] 主动找你聊聊", None),
    ("标记在正文中间", "user", "早安[moment-react]呀", None),
    ("含电话回顾标记", "user", "[voice-recap]哥哥在电话里说了想你", None),
    ("含系统自检标记", "user", "[系统自检] 一切正常", None),
    ("含记忆整理标记", "user", "[memory-compactor] 整理一下", None),
    ("⟦⟧开头的说明行整行删", "user", "⟦关系状态⟧ 亲密，最近很黏\n今天好累啊", "今天好累啊"),
    ("缩进的时刻行也删", "user", "  ⟦11月14日 周五 21:30⟧\n晚安啦", "晚安啦"),
    ("行内成对⟦⟧删掉", "user", "我想⟦插入说明⟧你了", "我想你了"),
    ("行内⟦⟧最短匹配", "user", "a⟦x⟧b⟦y⟧c好", "abc好"),
    ("语音转写去前缀", "user", "[语音] 明天见面吧", "明天见面吧"),
    ("图片带配文", "user", "[图片] 看这只猫", "看这只猫"),
    ("只有图片没配文", "user", "[图片]", None),
    ("只有位置", "user", "[位置]", None),
    ("只有语音消息占位", "user", "[语音消息]", None),
    ("只去开头那一个占位", "user", "[图片] 我发了[图片]给你", "我发了[图片]给你"),
    ("占位不在开头不动", "user", "看[图片]这个", "看[图片]这个"),
    ("转写只有一个字", "user", "[语音] 好", None),
    ("一个字丢掉", "user", "嗯", None),
    ("空白包着一个字也丢", "user", "  嗯  \n", None),
    ("两个字保留", "user", "嗯嗯", "嗯嗯"),
    ("一个emoji按一个码点丢", "user", "👍", None),
    ("两个emoji保留", "user", "👍👍", "👍👍"),
    ("首尾空白去掉", "user", "  早上好  \n", "早上好"),
]


@pytest.mark.parametrize("row", CLEAN_ROWS, ids=[r[0] for r in CLEAN_ROWS])
def test_主人侧候选与清洗(w, row):
    _what, kind, raw, expected = row
    w.bot()
    lg = w.ledger()
    lg.inb(raw, kind=kind)
    lg.close()
    want = [] if expected is None else [("user", expected)]
    assert _texts({"messages": _messages(w)}) == want


# ---------------------------------------------------------------- bot 侧：只收真送达的文字
OUT_ROWS = [
    # (说明, kind, state, turn_id, text, 是否出现)
    ("送达的文字", "text", "sent", OMIT, "好的呀", True),
    ("表情回应", "reaction", "sent", OMIT, "❤️❤️", False),
    ("系统通知", "system", "sent", OMIT, "【系统】已切换模型", False),
    ("接口通知", "api", "sent", OMIT, "接口发的通知", False),
    ("图片配文", "photo", "sent", OMIT, "图片配文字", False),
    ("动图", "animation", "sent", OMIT, "动图配文字", False),
    ("贴纸", "sticker", "sent", OMIT, "贴纸配文字", False),
    ("文件", "document", "sent", OMIT, "文件配文字", False),
    ("还没发", "text", "pending", OMIT, "还没发出去的话", False),
    ("发送失败", "text", "failed", OMIT, "发失败的话", False),
    ("没发", "text", "unsent", OMIT, "没发的话", False),
    ("送达不确定", "text", "ambiguous", OMIT, "不确定送没送到的话", False),
    ("不属于任何一轮", "text", "sent", None, "没有轮次的话", False),
    ("空文字", "text", "sent", OMIT, "", False),
    ("文字为NULL", "text", "sent", OMIT, None, False),
]


@pytest.mark.parametrize("row", OUT_ROWS, ids=[r[0] for r in OUT_ROWS])
def test_bot侧只收确实送达主人的文字段(w, row):
    _what, kind, state, turn_id, text, shown = row
    w.bot()
    lg = w.ledger()
    lg.out(text, kind=kind, state=state, turn_id=turn_id)
    lg.close()
    want = [("assistant", text)] if shown else []
    assert _texts({"messages": _messages(w)}) == want


def test_bot侧同一轮几段合成一条_按行号连_ts取最早送达(w):
    w.bot()
    lg = w.ledger()
    t = lg.ago(30)
    turn = lg.new_turn()
    lg.out("第一段", sent_at=t + 5000, turn_id=turn, part=0)
    lg.out("第二段", sent_at=t + 1000, turn_id=turn, part=1)      # 行号靠后、送达更早
    lg.out("❤️", kind="reaction", sent_at=t + 1500, turn_id=turn)
    lg.out("第三段没发出去", state="failed", sent_at=None, turn_id=turn, part=2)
    lg.inb("主人插了一句", ts=t + 3000)
    lg.close()
    assert _messages(w) == [
        {"role": "assistant", "text": "第一段\n第二段", "ts": (t + 1000) // 1000},
        {"role": "user", "text": "主人插了一句", "ts": (t + 3000) // 1000},
    ]


# ---------------------------------------------------------------- 时间窗
@pytest.mark.parametrize("query,rows,expected", [
    ("", [("in", 71 * 60, "七十一小时前"), ("in", 73 * 60, "七十三小时前")], ["七十一小时前"]),
    ("max_hours=1", [("in", 59, "五十九分钟前"), ("in", 61, "六十一分钟前")], ["五十九分钟前"]),
    ("max_hours=1", [("out_sent", 30, "早写好半小时前才送达"), ("out_old", 30, "刚写好却两小时前送达")],
     ["早写好半小时前才送达"]),
    ("max_hours=168", [("in", 167 * 60, "一百六十七小时前"), ("in", 169 * 60, "一百六十九小时前")],
     ["一百六十七小时前"]),
], ids=["默认72小时", "1小时", "bot侧按送达时间算", "168小时上限"])
def test_时间窗_只看窗口内_bot侧按送达时间(w, query, rows, expected):
    w.bot()
    lg = w.ledger()
    for side, minutes, text in rows:
        if side == "in":
            lg.inb(text, minutes=minutes)
        elif side == "out_sent":       # 两小时前写好，半小时前送达
            lg.out(text, sent_at=lg.ago(minutes), created_at=lg.ago(120))
        else:                          # 半小时前写好，两小时前送达（只为区分用哪一列）
            lg.out(text, sent_at=lg.ago(120), created_at=lg.ago(minutes))
    lg.close()
    assert [m["text"] for m in _messages(w, query)] == expected


# ---------------------------------------------------------------- 截断
@pytest.mark.parametrize("query,side,raw,expected", [
    ("max_chars=20", "in", "一二三四五六七八九十" * 2 + "多出来的五个", "一二三四五六七八九十" * 2),
    ("max_chars=20", "out", "😀" * 30, "😀" * 20),
    ("max_chars=20", "in", "正好二十个字" + "啊" * 14, "正好二十个字" + "啊" * 14),
    ("", "in", "a" * 250, "a" * 200),
    ("max_chars=1000", "out", "长" * 1200, "长" * 1000),
], ids=["中文截到20", "emoji按码点截", "正好20不动", "默认200", "上限1000"])
def test_单条按码点截断_不加省略号(w, query, side, raw, expected):
    w.bot()
    lg = w.ledger()
    lg.inb(raw) if side == "in" else lg.out(raw)
    lg.close()
    assert [m["text"] for m in _messages(w, query)] == [expected]


# ---------------------------------------------------------------- 条数
@pytest.mark.parametrize("query,total,keep", [("", 15, 10), ("n=1", 5, 1), ("n=3", 8, 3), ("n=50", 60, 50),
                                              ("n=50", 4, 4)],
                         ids=["默认10条", "只要1条", "3条", "上限50条", "不足n条全给"])
def test_条数n_主人与bot合计_取最后n条(w, query, total, keep):
    w.bot()
    lg = w.ledger()
    texts = []
    for i in range(total):
        t = "第%02d句" % i
        texts.append(t)
        (lg.inb if i % 2 == 0 else lg.out)(t, minutes=total - i)
    lg.close()
    assert [m["text"] for m in _messages(w, query)] == texts[-keep:]


# ---------------------------------------------------------------- 查询参数校验
PARAM_ROWS = [
    ("n=0", vu.N_BAD), ("n=51", vu.N_BAD), ("n=-1", vu.N_BAD), ("n=%2B5", vu.N_BAD),
    ("n=1.5", vu.N_BAD), ("n=", vu.N_BAD), ("n=%205", vu.N_BAD), ("n=5%20", vu.N_BAD),
    ("n=0000010", vu.N_BAD), ("n=abc", vu.N_BAD), ("n=%EF%BC%95", vu.N_BAD), ("n=5&n=5", vu.N_BAD),
    ("max_hours=0", vu.HOURS_BAD), ("max_hours=169", vu.HOURS_BAD), ("max_hours=", vu.HOURS_BAD),
    ("max_hours=1e2", vu.HOURS_BAD), ("max_hours=1&max_hours=2", vu.HOURS_BAD),
    ("max_chars=19", vu.CHARS_BAD), ("max_chars=1001", vu.CHARS_BAD), ("max_chars=0x20", vu.CHARS_BAD),
    ("max_chars=", vu.CHARS_BAD),
    ("n=0&max_hours=0", vu.N_BAD), ("max_hours=0&max_chars=0", vu.HOURS_BAD),
    ("max_chars=0&n=0", vu.N_BAD),
    # 合规的边界值
    ("n=010", None), ("n=1", None), ("n=50", None), ("n=000050", None), ("max_hours=1", None),
    ("max_hours=168", None), ("max_chars=20", None), ("max_chars=1000", None), ("foo=bar&x=", None),
]


@pytest.mark.parametrize("query,message", PARAM_ROWS, ids=[q for q, _ in PARAM_ROWS])
def test_查询参数_不合规422只报第一个_合规边界照常(w, query, message):
    w.bot()
    lg = w.ledger()
    lg.inb("一句私聊")
    lg.close()
    resp = w.dm_tail(query=query)
    if message is None:
        assert _texts(vu.expect_ok(resp)) == [("user", "一句私聊")]
    else:
        vu.expect_error(resp, 422, "invalid", message)


def test_n等于010按10条算(w):
    w.bot()
    lg = w.ledger()
    for i in range(12):
        lg.inb("第%02d句" % i, minutes=20 - i)
    lg.close()
    assert len(_messages(w, "n=010")) == 10


# ---------------------------------------------------------------- 空
@pytest.mark.parametrize("case", ["没有state目录", "state在但没有账本", "窗口里没有可用的话"])
def test_没有可给的话_返回空列表(w, case):
    bot = w.bot()
    if case == "没有state目录":
        os.rmdir(str(bot.botdir / "state"))
    elif case == "窗口里没有可用的话":
        lg = w.ledger()
        lg.inb("太久以前的话", minutes=80 * 60)
        lg.inb("嗯")
        lg.out("没发出去", state="failed", sent_at=None)
        lg.inb("群里的话", chat=vu.GROUP_CHAT)
        lg.close()
    assert vu.expect_ok(w.dm_tail()) == {"ok": True, "messages": []}


# ---------------------------------------------------------------- 账本读不出
def _good_ledger(path):
    lg = vu.Ledger(path)
    lg.inb("软链那边的私聊")
    lg.close()


def _make_bad(w, bot, case):
    led = w.ledger_path()
    state = bot.botdir / "state"
    if case == "软链指向自己目录里的好账本":
        _good_ledger(state / "real.sqlite")
        os.symlink(str(state / "real.sqlite"), str(led))
    elif case == "软链指向外面的好账本":
        _good_ledger(w.sb.tmp / "elsewhere.sqlite")
        os.symlink(str(w.sb.tmp / "elsewhere.sqlite"), str(led))
    elif case == "state目录是软链到外面":
        outside = w.sb.tmp / "outside-state"
        _good_ledger(outside / "ledger.sqlite")
        os.rmdir(str(state))
        os.symlink(str(outside), str(state))
    elif case == "是目录":
        led.mkdir()
    elif case == "是FIFO":
        os.mkfifo(str(led))
    elif case == "不是SQLite":
        vu.put_file(led, b"this is not a sqlite database at all\x00\xff" * 50)
    elif case == "空文件没有表":
        vu.put_file(led, b"")
    elif case == "缺outbound表":
        db = sqlite3.connect(str(led))
        db.execute("CREATE TABLE inbound (id INTEGER PRIMARY KEY, ukey TEXT, chat_id TEXT, kind TEXT,"
                   " text TEXT, ts INTEGER, received_at INTEGER, state TEXT)")
        db.commit()
        db.close()
    elif case == "inbound缺kind列":
        lg = vu.Ledger(led)
        lg.close()
        db = sqlite3.connect(str(led))
        db.execute("DROP TABLE inbound")
        db.execute("CREATE TABLE inbound (id INTEGER PRIMARY KEY, ukey TEXT, chat_id TEXT, text TEXT,"
                   " ts INTEGER, received_at INTEGER, state TEXT)")
        db.commit()
        db.close()
    else:
        raise AssertionError(case)


BAD_LEDGERS = ["软链指向自己目录里的好账本", "软链指向外面的好账本", "state目录是软链到外面", "是目录", "是FIFO",
               "不是SQLite", "空文件没有表", "缺outbound表", "inbound缺kind列"]


@pytest.mark.parametrize("case", BAD_LEDGERS)
def test_账本读不出_500_不带出任何内容(w, case):
    bot = w.bot()
    _make_bad(w, bot, case)
    resp = vu.call_with_deadline(lambda: w.dm_tail(), 10, "读 %s 的账本" % case)
    vu.expect_error(resp, 500, "internal", vu.DM_UNREADABLE)
    assert "软链那边的私聊" not in resp.get_data(as_text=True)


def test_账本被别人独占锁住_两秒左右放弃_回500(w):
    import time
    w.bot()
    lg = w.ledger()
    lg.inb("锁住时的私聊")
    lg.close()
    holder = sqlite3.connect(str(w.ledger_path()), timeout=0, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        t0 = time.monotonic()
        resp = vu.call_with_deadline(lambda: w.dm_tail(), 10, "读被锁住的账本")
        took = time.monotonic() - t0
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    vu.expect_error(resp, 500, "internal", vu.DM_UNREADABLE)
    assert 1.5 <= took <= 6, "应等约 2 秒拿读锁再放弃，实际 %.1f 秒" % took
