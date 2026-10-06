# -*- coding: utf-8 -*-
"""chat_history 读新系统的送达账本：函数名和返回格式与读旧会话文件时完全一样。

账本用网关的同一份表结构（gateway/src/ledger-schema.sql）现建，不依赖 bun。
"""
import os
import sqlite3
import time
from pathlib import Path

import pytest

import chat_history

SCHEMA = (Path(__file__).resolve().parents[2] / "gateway" / "src" / "ledger-schema.sql").read_text(encoding="utf-8")


@pytest.fixture
def bot(tmp_path, monkeypatch):
    """新系统布局：bots/<bot>/channel + bots/<bot>/state/ledger.sqlite"""
    monkeypatch.delenv("DSH_BOT_LEDGER", raising=False)
    root = tmp_path / "bots" / "bot5"
    channel = root / "channel"
    channel.mkdir(parents=True)
    (root / "state").mkdir()
    db = sqlite3.connect(root / "state" / "ledger.sqlite")
    db.executescript(SCHEMA)
    db.commit()
    return channel, db


def add_user(db, chat, text, ts_s, kind="user", n=[0]):
    n[0] += 1
    db.execute("INSERT INTO inbound (ukey, chat_id, kind, text, ts, received_at, state) VALUES (?,?,?,?,?,?, 'done')",
               (f"k{n[0]}", chat, kind, text, int(ts_s * 1000), int(ts_s * 1000)))
    db.commit()


def add_bot(db, chat, text, ts_s, state="sent", kind="text", turn=1):
    db.execute("INSERT INTO outbound (chat_id, turn_id, part, kind, text, state, created_at, sent_at) VALUES (?,?,?,?,?,?,?,?)",
               (chat, turn, 1, kind, text, state, int(ts_s * 1000), int(ts_s * 1000)))
    db.commit()


def test_认得新系统的布局(bot):
    channel, _ = bot
    assert chat_history.is_dsh_bot(str(channel))
    assert not chat_history.is_dsh_bot(str(channel.parent.parent / "nothing" / "channel"))


def test_bot的话只算真正送达的reply(bot):
    channel, db = bot
    now = time.time()
    add_bot(db, "1", "早呀，今天也要加油", now - 60)
    add_bot(db, "1", "没发出去的这句不算", now - 50, state="failed")
    add_bot(db, "1", "【系统】已清空", now - 40, kind="system", turn=None)
    add_bot(db, "1", "网络断了但可能送达了", now - 30, state="ambiguous")
    add_bot(db, "1", "很久以前的话", now - 10 * 86400)
    assert chat_history.get_recent_assistant_messages(str(channel), days=3, limit=10) == ["早呀，今天也要加油", "网络断了但可能送达了"]
    assert chat_history.get_recent_assistant_messages(str(channel), days=3, limit=1) == ["网络断了但可能送达了"]


def test_对话片段格式不变_字数超限时保留最近的(bot):
    channel, db = bot
    now = time.time()
    add_user(db, "1", "我养了只橘猫", now - 300)
    add_bot(db, "1", "好可爱", now - 200)
    add_user(db, "1", "⟦系统：程序注入⟧", now - 150, kind="synthetic")
    add_user(db, "1", "它叫豆豆", now - 100)
    out = chat_history.get_recent_dialog(str(channel), days=7, max_chars=12000)
    assert out.split("\n") == ["[用户] 我养了只橘猫", "[我] 好可爱", "[用户] 它叫豆豆"]
    short = chat_history.get_recent_dialog(str(channel), days=7, max_chars=20)
    assert short.split("\n") == ["[我] 好可爱", "[用户] 它叫豆豆"]


def test_最后一次说话的时间_按聊天区分(bot):
    channel, db = bot
    now = time.time()
    add_user(db, "1", "私聊里说的", now - 3600)
    add_user(db, "-100", "群里说的", now - 600)
    add_user(db, "1", "⟦系统：合成消息不算⟧", now - 60, kind="synthetic")
    assert chat_history.last_user_msg_ts(str(channel), chat_id="1") == int(now - 3600)
    assert chat_history.last_user_msg_ts(str(channel)) == int(now - 600)
    assert chat_history.mins_since_last_user_msg(str(channel), chat_id="1") == 60
    assert chat_history.last_user_msg_ts(str(channel), chat_id="999") is None


def test_对话尾巴_格式不变_只看这个聊天(bot):
    channel, db = bot
    now = time.time()
    add_user(db, "1", "在吗", now - 300)
    add_bot(db, "1", "在的", now - 200)
    add_user(db, "-100", "群里的话不该出现在私聊尾巴里", now - 150)
    add_user(db, "1", "嗯", now - 120)        # 太短，和旧实现一样过滤掉
    add_user(db, "1", "晚上吃什么", now - 100)
    tail = chat_history.get_thread_tail(str(channel), chat_id="1", n=12, max_hours=72)
    assert [(r, t) for r, t, _ in tail] == [("user", "在吗"), ("assistant", "在的"), ("user", "晚上吃什么")]
    assert all(isinstance(ts, int) for _, _, ts in tail)
    assert len(chat_history.get_thread_tail(str(channel), chat_id="1", n=2)) == 2


def test_没有账本时回到旧数据源(tmp_path, monkeypatch):
    monkeypatch.delenv("DSH_BOT_LEDGER", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    old = tmp_path / ".claude" / "channels" / "oldbot"
    old.mkdir(parents=True)
    assert not chat_history.is_dsh_bot(str(old))
    assert chat_history.get_recent_dialog(str(old)) == ""
    assert chat_history.get_thread_tail(str(old)) == []


def test_账本坏了不抛(bot):
    channel, db = bot
    db.close()  # Windows 上打开着的文件不能改写
    p = channel.parent / "state" / "ledger.sqlite"
    p.write_bytes(b"not a database")
    for f in ("-wal", "-shm"):
        try:
            os.remove(str(p) + f)
        except FileNotFoundError:
            pass
    assert chat_history.get_recent_dialog(str(channel)) == ""
    assert chat_history.last_user_msg_ts(str(channel)) is None


def test_新系统的长期记忆在频道目录里(bot):
    channel, _ = bot
    from memory.memory_inject import memory_path
    assert memory_path(str(channel)) == str(channel / "memory" / "MEMORY.md")


def test_整理期间新记下的条目不会被覆盖():
    from memory.memory_compactor import merge_concurrent_additions
    original = "# Memory\n\n## 关于对方\n- 养猫\n"
    latest = original + "\n## 随手记\n- 2026-10-06 下周三去成都出差\n"
    new = "# Memory\n\n## 关于对方\n- 养了一只叫豆豆的橘猫\n"
    out = merge_concurrent_additions(original, latest, new)
    assert "- 2026-10-06 下周三去成都出差" in out
    assert "## 随手记" in out
    assert merge_concurrent_additions(original, original, new) == new


def test_DeepSeek密钥可以从新系统的共用密钥文件读(tmp_path, monkeypatch):
    import deepseek_client
    import config_loader
    monkeypatch.setattr(config_loader, "load_global", lambda: {})
    monkeypatch.setenv("DSH_BOT_HOME", str(tmp_path))
    with pytest.raises(RuntimeError):
        deepseek_client._delta_cfg()
    (tmp_path / "credentials.yaml").write_text("version: 1\nrefs:\n  DEEPSEEK_API_KEY: 把这里换成你的密钥\n", encoding="utf-8")
    with pytest.raises(RuntimeError):
        deepseek_client._delta_cfg()
    (tmp_path / "credentials.yaml").write_text("version: 1\nrefs:\n  DEEPSEEK_API_KEY: sk-test-only-0000000000\n", encoding="utf-8")
    assert deepseek_client._delta_cfg()["api_key"] == "sk-test-only-0000000000"
