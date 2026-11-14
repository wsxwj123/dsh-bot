# -*- coding: utf-8 -*-
"""电话页要的两样上下文：主人私聊的尾巴（账本只读）与长期记忆主文件的前段。纯逻辑，不碰 Flask。

契约：``.devflow/INTERFACE-voicecall-dsh.md`` ③.1、③.2；做法与理由见 ``.devflow/PLAN-voicecall-dsh.md`` ①.3。

- 账本固定在 ``<DSH_BOT_HOME>/bots/<bot_id>/state/ledger.sqlite``（与网关 ``config.ts`` 的 ``stateDir`` 同一取法），
  不走 ``chat_history`` 的读法（它先认 ``DSH_BOT_LEDGER``、收 ambiguous、出错吞成空）。只读打开，整个模块只有 SELECT，
  绝不在 Python 侧建账本、跑建表。
- 出错一律抛 ``ContextError(状态码, code, message)``，由适配服务翻成约定的失败形状；异常原文不往外带。
"""
from __future__ import annotations

import codecs
import os
import re
import sqlite3
import stat
from urllib.parse import quote

from chat_history import INTERNAL_MARKERS

DM_UNREADABLE = "这只 bot 的聊天记录读不出来"
MEM_OUTSIDE = "这只 bot 的长期记忆文件不在它自己的目录里，或者是软链，不读"
MEM_UNREADABLE = "这只 bot 的长期记忆文件读不出来"

#: 每侧最多从账本取这么多行再清洗：不能先 LIMIT n，纯图片、贴纸行清洗后会被丢，把更早的真文字挤掉
SIDE_ROWS = 500
#: 等读锁的秒数（壳的读超时是 5 秒）
LOCK_TIMEOUT = 2.0
MEMORY_LIMIT = 1800
MIN_CHARS = 2

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


class ContextError(Exception):
    def __init__(self, status, code, message):
        super().__init__(code)
        self.status, self.code, self.message = status, code, message


def _unreadable():
    return ContextError(500, "internal", DM_UNREADABLE)


# ---------------------------------------------------------------- 主人侧清洗

#: 网关入账时放在正文开头的占位（gateway/src/telegram/inbound.ts 的 placeholder()，语音转写后由
#: media/media.ts 换成「[语音] <转写>」或 VOICE_UNHEARD）。只认开头一个；那边增减时这里跟着改
#: （tests/unit/test_voicecall_context.py 有契约测试从网关源码抽占位比对）。
_VOICE_PREFIX = "[语音] "
_PLACEHOLDER_RE = re.compile(
    r"\A(?:\[语音消息，没转成文字：[^\]\n]*\]|\[语音消息\]|\[音频\]|\[视频消息\]|\[视频\]|\[图片\]"
    r"|\[位置\]|\[联系人\]|\[贴纸[^\]\n]*\]|\[文件[^\]\n]*\]) ?")
_INLINE_NOTE_RE = re.compile(r"⟦.*?⟧")


def clean_owner_text(text: str) -> str | None:
    """INTERFACE ③.1 第 6 条：含程序标记的整条丢（None）；⟦ 开头的说明行整行删、行内成对 ⟦…⟧ 删；
    去掉开头的一个媒体占位（``[语音] `` 只去前缀留转写）；去首尾空白。"""
    if any(m in text for m in INTERNAL_MARKERS):
        return None
    lines = [_INLINE_NOTE_RE.sub("", ln) for ln in text.split("\n") if not ln.strip().startswith("⟦")]
    body = "\n".join(lines)
    if body.startswith(_VOICE_PREFIX):
        body = body[len(_VOICE_PREFIX):]
    else:
        body = _PLACEHOLDER_RE.sub("", body, count=1)
    return body.strip()


# ---------------------------------------------------------------- 账本

def ledger_path(home: str, bot_id: str) -> str | None:
    """账本的真实路径；文件不在返回 None。``home`` 先解析成真实路径再拼（macOS 的 /var → /private/var）。

    账本是软链、不是普通文件（目录、FIFO……）、真实路径不在 ``<家>/bots/<bot_id>/`` 里 → 500。
    ``bot_id`` 调用方已经与名单精确匹配过。
    """
    bot_home = os.path.join(os.path.realpath(home), "bots", bot_id)
    path = os.path.join(bot_home, "state", "ledger.sqlite")
    if not os.path.lexists(path):
        return None
    try:
        st = os.lstat(path)
    except OSError:
        raise _unreadable() from None
    if not stat.S_ISREG(st.st_mode):         # 软链、目录、FIFO 一律不读（FIFO 打开会卡住）
        raise _unreadable()
    real = os.path.realpath(path)
    if not real.startswith(bot_home + os.sep):   # state 目录经软链指到外面
        raise _unreadable()
    return real


def _cant_open(err: sqlite3.Error) -> bool:
    """只读连接打不开（SQLITE_CANTOPEN / SQLITE_READONLY 系）：WAL 库的 -wal、-shm 都已删掉时会这样（实验 E1c）。"""
    name = getattr(err, "sqlite_errorname", "") or ""
    if name.startswith(("SQLITE_CANTOPEN", "SQLITE_READONLY")):
        return True
    msg = str(err)
    return "unable to open database file" in msg or "readonly database" in msg


#: 主人侧候选：这条私聊线、kind='user'、不论处理状态；按时间倒序取最多 SIDE_ROWS 行（走 inbound_chat_state 索引）
OWNER_SQL = ("SELECT id, ts, text FROM inbound WHERE chat_id = ? AND kind = 'user' AND ts >= ?"
             " AND text IS NOT NULL ORDER BY ts DESC, id DESC LIMIT ?")
#: bot 侧候选：确实送达主人的回复文字（text、sent、有轮次、非空），按送达时间（走 outbound_chat 索引）
BOT_SQL = ("SELECT id, turn_id, sent_at, text FROM outbound WHERE chat_id = ? AND kind = 'text'"
           " AND state = 'sent' AND turn_id IS NOT NULL AND text IS NOT NULL AND text <> ''"
           " AND sent_at >= ? ORDER BY sent_at DESC, id DESC LIMIT ?")


def _query(path: str, immutable: bool, chat: str, since_ms: int):
    uri = "file:%s?mode=ro%s" % (quote(path), "&immutable=1" if immutable else "")
    con = sqlite3.connect(uri, uri=True, timeout=LOCK_TIMEOUT, isolation_level=None)
    try:
        con.execute("BEGIN")                # 两条 SELECT 拿同一个快照
        try:
            owner = con.execute(OWNER_SQL, (chat, since_ms, SIDE_ROWS)).fetchall()
            bot = con.execute(BOT_SQL, (chat, since_ms, SIDE_ROWS)).fetchall()
        finally:
            con.execute("ROLLBACK")
    finally:
        con.close()
    return owner, bot


def _read_rows(path: str, chat: str, since_ms: int):
    """只读打开。``mode=ro`` 打不开、且同目录没有 ``-wal`` 时（写者已正常关机，没有未合并的页）改用
    ``immutable=1`` 再开一次；有 ``-wal`` 绝不用 immutable（那时可能有写者，immutable 会读到不一致的页）。
    锁等不到、不是 SQLite、缺表缺列 → 500。"""
    try:
        try:
            return _query(path, False, chat, since_ms)
        except sqlite3.OperationalError as e:
            if not _cant_open(e) or os.path.lexists(path + "-wal"):
                raise
            return _query(path, True, chat, since_ms)
    except sqlite3.Error:
        raise _unreadable() from None


def _row_value(text) -> str:
    return text if isinstance(text, str) else ""


def read_dm_tail(path: str | None, chat_id: str, n: int, max_hours: int, max_chars: int, now_ms: int) -> list[dict]:
    """INTERFACE ③.1 取数口径第 3 到 8 步。``path`` 为 None（账本不在）回空列表。"""
    if path is None:
        return []
    since_ms = now_ms - max_hours * 3600 * 1000
    owner_rows, bot_rows = _read_rows(path, chat_id, since_ms)
    items = []                              # (毫秒, 侧 0=主人 1=bot, 行号, role, text)
    for row_id, ts, text in owner_rows:
        cleaned = clean_owner_text(_row_value(text))
        if cleaned is not None:
            items.append((int(ts), 0, int(row_id), "user", cleaned))
    turns: dict = {}
    for row_id, turn_id, sent_at, text in bot_rows:
        turns.setdefault(turn_id, []).append((int(row_id), int(sent_at), _row_value(text)))
    for parts in turns.values():
        parts.sort()                        # 同一轮的几段按行号升序连起来，时刻取最早送达的那段
        items.append((min(p[1] for p in parts), 1, parts[0][0], "assistant", "\n".join(p[2] for p in parts)))
    out = []
    for ts, _side, _row, role, text in sorted(items):
        text = text.strip()
        if len(text) < MIN_CHARS:           # 先丢再截，按码点算
            continue
        out.append({"role": role, "text": text[:max_chars], "ts": ts // 1000})
    return out[-n:]


# ---------------------------------------------------------------- 长期记忆

def read_memory_head(memory_dir: str, limit: int = MEMORY_LIMIT) -> dict:
    """``<频道目录>/memory/MEMORY.md`` 的前 ``limit`` 个码点。``memory_dir`` 已过人设接口那道校验（不是软链、在自己家里）。

    不存在回 ``exists: false``；主文件是软链 → 500「不在自己目录里或是软链」；不是普通文件、没权限、
    前 ``limit`` 个码点内不是合法 UTF-8 → 500「读不出来」。以不跟软链、不阻塞的方式打开（FIFO 不会卡住）。
    """
    if not os.path.lexists(memory_dir):
        return {"exists": False, "text": "", "truncated": False}
    if not os.path.isdir(memory_dir) or os.path.islink(memory_dir):
        raise ContextError(500, "internal", MEM_OUTSIDE if os.path.islink(memory_dir) else MEM_UNREADABLE)
    path = os.path.join(memory_dir, "MEMORY.md")
    if not os.path.lexists(path):
        return {"exists": False, "text": "", "truncated": False}
    if os.path.islink(path):
        raise ContextError(500, "internal", MEM_OUTSIDE)
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _NONBLOCK)
    except OSError:
        raise ContextError(500, "internal", MEM_UNREADABLE) from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ContextError(500, "internal", MEM_UNREADABLE)
        budget = (limit + 1) * 4            # 够判断有没有超过 limit 个码点
        chunks, got = [], 0
        while got < budget:
            chunk = os.read(fd, budget - got)
            if not chunk:
                break
            chunks.append(chunk)
            got += len(chunk)
        data = b"".join(chunks)
        more = got >= budget and bool(os.read(fd, 1))
    except OSError:
        raise ContextError(500, "internal", MEM_UNREADABLE) from None
    finally:
        os.close(fd)
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        text = decoder.decode(data, final=not more)   # 读到上限时尾部不完整的多字节序列不算错
    except UnicodeDecodeError as e:
        text = data[:e.start].decode("utf-8")
        if len(text) < limit:                # 坏字节落在前 limit 个码点里
            raise ContextError(500, "internal", MEM_UNREADABLE) from None
        return {"exists": True, "text": text[:limit], "truncated": True}
    return {"exists": True, "text": text[:limit], "truncated": more or len(text) > limit}
