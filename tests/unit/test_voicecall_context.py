# -*- coding: utf-8 -*-
"""moments/voicecall_context.py 的白盒单测（voicecall-dsh 方案 ② 那一行）。

账本用网关同一份表结构（gateway/src/ledger-schema.sql）在 tmp 里现建，不读任何真实账本。
"""
import os
import re
import sqlite3
import time
from pathlib import Path

import pytest

import chat_history
from moments import voicecall_context as vc

REPO = Path(__file__).resolve().parents[2]
SCHEMA = (REPO / "gateway" / "src" / "ledger-schema.sql").read_text(encoding="utf-8")
OWNER = "1009876543"


def _db(path, wal=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path))
    if wal:
        db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


def _inb(db, text, ts, kind="user", state="done", chat=OWNER, n=[0]):
    n[0] += 1
    db.execute("INSERT INTO inbound (ukey, chat_id, kind, text, ts, received_at, state) VALUES (?,?,?,?,?,?,?)",
               ("k%d" % n[0], chat, kind, text, ts, ts, state))
    db.commit()


def _out(db, text, sent_at, turn=1, chat=OWNER):
    db.execute("INSERT INTO outbound (chat_id, turn_id, part, kind, text, state, created_at, sent_at)"
               " VALUES (?,?,0,'text',?,'sent',?,?)", (chat, turn, text, sent_at, sent_at))
    db.commit()


# ---------------------------------------------------------------- 清洗

def test_标记表就是chat_history那一份_同一个对象():
    assert chat_history.INTERNAL_MARKERS is chat_history._INTERNAL_MARKERS
    assert vc.INTERNAL_MARKERS is chat_history.INTERNAL_MARKERS


@pytest.mark.parametrize("raw,want", [
    ("[self-initiate] 主动找你", None),
    ("⟦关系状态⟧ 亲密\n今天好累", "今天好累"),
    ("a⟦x⟧b⟦y⟧c", "abc"),
    ("[语音] 明天见", "明天见"),
    ("[语音消息，没转成文字：你没听清这条语音，照实告诉对方]", ""),
    ("[贴纸 😀]", ""),
    ("[文件 报告.pdf] 看一下", "看一下"),
    ("[视频消息] 哈哈", "哈哈"),
    ("[图片] 我发了[图片]给你", "我发了[图片]给你"),
    ("看[图片]这个", "看[图片]这个"),
])
def test_主人侧清洗(raw, want):
    assert vc.clean_owner_text(raw) == want


def test_占位表契约_网关源码里的每种占位清洗都认():
    inbound = (REPO / "gateway" / "src" / "telegram" / "inbound.ts").read_text(encoding="utf-8")
    body = inbound[inbound.index("function placeholder("):]
    body = body[:body.index("\n}\n")]
    fixed = re.findall(r"return '(\[[^']*\])'", body)
    templ = re.findall(r"return `(\[[^$`]*)\$\{", body)           # [贴纸、[文件 后面跟可选的名字
    assert len(fixed) >= 7 and sorted(templ) == ["[文件", "[贴纸"], (fixed, templ)
    media = (REPO / "gateway" / "src" / "media" / "media.ts").read_text(encoding="utf-8")
    unheard = re.search(r"export const VOICE_UNHEARD = '([^']*)'", media).group(1)
    assert "`[语音] ${m.transcript}`" in media                    # 转写后的前缀
    for ph in fixed + [t + "]" for t in templ] + [t + " 名字]" for t in templ] + [unheard]:
        assert vc.clean_owner_text(ph + " 配文字") == "配文字", ph
        assert vc.clean_owner_text(ph) == "", ph


# ---------------------------------------------------------------- 账本：只读打开与 immutable 回退

def test_WAL库伴随文件都删掉之后_回退immutable照样读得出_且不建伴随文件(tmp_path):
    path = tmp_path / "bots" / "b" / "state" / "ledger.sqlite"
    db = _db(path, wal=True)
    now = int(time.time() * 1000)
    _inb(db, "电话前说的话", now - 1000)
    db.close()
    # 网关（bun）正常关机后 -wal、-shm 都不在（实验 E1c）；这里的 Python 可能用的是保留 WAL 文件的 macOS 版 SQLite，手动摆成那个状态
    for tail in ("-wal", "-shm"):
        if os.path.exists(str(path) + tail):
            os.remove(str(path) + tail)
    with pytest.raises(sqlite3.OperationalError):          # 实验 E1c：只读连接建不出 -shm
        vc._query(str(path), False, OWNER, 0)
    got = vc.read_dm_tail(vc.ledger_path(str(tmp_path), "b"), OWNER, 10, 72, 200, now)
    assert [m["text"] for m in got] == ["电话前说的话"]
    assert sorted(os.listdir(str(path.parent))) == ["ledger.sqlite"]


def test_有wal文件时绝不用immutable_打不开就500(tmp_path, monkeypatch):
    path = tmp_path / "bots" / "b" / "state" / "ledger.sqlite"
    _db(path).close()
    (Path(str(path) + "-wal")).write_bytes(b"")
    calls = []

    def fake(p, immutable, chat, since):
        calls.append(immutable)
        raise sqlite3.OperationalError("unable to open database file")
    monkeypatch.setattr(vc, "_query", fake)
    with pytest.raises(vc.ContextError) as e:
        vc.read_dm_tail(str(path), OWNER, 10, 72, 200, 0)
    assert (e.value.status, e.value.message) == (500, vc.DM_UNREADABLE)
    assert calls == [False]


def test_锁住等不到_不回退immutable(tmp_path, monkeypatch):
    calls = []

    def fake(p, immutable, chat, since):
        calls.append(immutable)
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(vc, "_query", fake)
    with pytest.raises(vc.ContextError):
        vc.read_dm_tail(str(tmp_path / "x.sqlite"), OWNER, 10, 72, 200, 0)
    assert calls == [False]


def test_网关正在写时并发读_不报错(tmp_path):
    path = tmp_path / "bots" / "b" / "state" / "ledger.sqlite"
    writer = _db(path, wal=True)
    now = int(time.time() * 1000)
    for i in range(20):
        _inb(writer, "第%02d句" % i, now - 20_000 + i)
        got = vc.read_dm_tail(str(path), OWNER, 50, 72, 200, now)
        assert len(got) == i + 1
    writer.close()


def test_两条查询走按聊天的索引():
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA)
    plan_in = " ".join(r[-1] for r in db.execute("EXPLAIN QUERY PLAN " + vc.OWNER_SQL, (OWNER, 0, 1)))
    plan_out = " ".join(r[-1] for r in db.execute("EXPLAIN QUERY PLAN " + vc.BOT_SQL, (OWNER, 0, 1)))
    assert "inbound_chat_state" in plan_in, plan_in
    assert "outbound_chat" in plan_out, plan_out


def test_模块里只有SELECT():
    src = (REPO / "moments" / "voicecall_context.py").read_text(encoding="utf-8")
    for word in ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "PRAGMA", "executescript"):
        assert word not in src, word


# ---------------------------------------------------------------- 电话记录不改「主人最近说话」

def test_插一条held电话记录前后_chat_history的主人最近说话时间不变(tmp_path, monkeypatch):
    monkeypatch.delenv("DSH_BOT_LEDGER", raising=False)
    channel = tmp_path / "bots" / "b" / "channel"
    channel.mkdir(parents=True)
    db = _db(tmp_path / "bots" / "b" / "state" / "ledger.sqlite")
    t = int(time.time() * 1000) - 40 * 60_000
    _inb(db, "四十分钟前的真话", t)
    before = (chat_history.last_user_msg_ts(str(channel), OWNER), chat_history.mins_since_last_user_msg(str(channel), OWNER))
    _inb(db, "[voice-recap] 电话记录", int(time.time() * 1000), kind="synthetic", state="held")
    after = (chat_history.last_user_msg_ts(str(channel), OWNER), chat_history.mins_since_last_user_msg(str(channel), OWNER))
    assert before == after and before[1] >= 39
    got = vc.read_dm_tail(str(tmp_path / "bots" / "b" / "state" / "ledger.sqlite"), OWNER, 10, 72, 200, int(time.time() * 1000))
    assert [m["text"] for m in got] == ["四十分钟前的真话"]          # dm_tail 也不收合成消息
    db.close()


# ---------------------------------------------------------------- 记忆

def test_记忆_尾部不完整的多字节序列在1800之后不算错(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    data = ("记" * 2500).encode("utf-8")
    (mem / "MEMORY.md").write_bytes(data[:7205])           # 读的上限 7204 字节正好切在一个字中间
    assert vc.read_memory_head(str(mem)) == {"exists": True, "text": "记" * 1800, "truncated": True}


def test_记忆目录不是目录_读不出(tmp_path):
    (tmp_path / "memory").write_text("x", encoding="utf-8")
    with pytest.raises(vc.ContextError) as e:
        vc.read_memory_head(str(tmp_path / "memory"))
    assert e.value.message == vc.MEM_UNREADABLE
