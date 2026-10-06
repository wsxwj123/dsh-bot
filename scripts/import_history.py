#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""切换时把旧系统的私聊记录和还没到点的承诺导进新系统的账本（M6）。只读旧数据，不改。

用法（网关先停掉；只出数字，不打印聊天内容）：
    python3 scripts/import_history.py <新 bot 名> --from <旧频道目录> [--old-bot <旧名>] [--days 30] [--dry-run]
    python3 scripts/import_history.py <新 bot 名> --from <旧频道目录> --inspect-promises

- 私聊记录：从旧的 Claude Code 会话文件里取出对方说的话和 bot 真正发出去的话（调用了 reply 的），
  只取主人私聊的、最近 --days 天的。写进账本里一个"已结束"的段：新网关第一次处理这个聊天时，
  会照常用账本给它补写一份摘要，新会话带着摘要和最近的原话开始（和崩溃恢复同一条路）。
  这个聊天在新系统里已经聊过（账本里有段）就不导，免得新旧记录交错。
- 承诺：旧频道目录的 .promises.json，只导还没到点、也没完成的，登记成新系统的承诺（来源记为 import）。
  旧文件的格式没有文档，脚本按常见字段名宽松识别；先用 --inspect-promises 看结构（只打印字段名和类型）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sqlite3
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chat_history  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA = os.path.join(ROOT, "gateway", "src", "ledger-schema.sql")
CHAT_ID_RE = None


def _home() -> str:
    return os.path.expanduser(os.environ.get("DSH_BOT_HOME") or "~/.dsh-bot")


def _owner(channel_dir: str) -> str | None:
    try:
        with open(os.path.join(channel_dir, "access.json"), encoding="utf-8") as f:
            a = json.load(f).get("allowFrom") or []
        return str(a[0]) if a else None
    except Exception:
        return None


def _chat_of(raw: str) -> str | None:
    global CHAT_ID_RE
    if CHAT_ID_RE is None:
        import re
        CHAT_ID_RE = re.compile(r'<channel\b[^>]*\bchat_id="(-?\d+)"')
    m = CHAT_ID_RE.search(raw)
    return m.group(1) if m else None


def read_old_dialog(old_dir: str, old_bot: str | None, chat: str, days: int) -> list[tuple[str, str, int]]:
    """[(who, text, ts_ms)]，who = user / bot，时间正序。只取这个私聊的。"""
    proj = chat_history._project_dir(old_dir)
    if not os.path.isdir(proj):
        return []
    su = chat_history.unified_session_uuid(old_bot) if old_bot else None
    import glob
    files = [os.path.join(proj, f"{su}.jsonl")] if su and os.path.exists(os.path.join(proj, f"{su}.jsonl")) \
        else glob.glob(os.path.join(proj, "*.jsonl"))
    cutoff = time.time() - days * 86400
    out, seen = [], set()
    for path in files:
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                typ = o.get("type")
                if typ not in ("user", "assistant") or o.get("isCompactSummary"):
                    continue
                ts = chat_history._parse_iso_safe(o.get("timestamp", ""))
                if not ts or ts < cutoff:
                    continue
                content = (o.get("message") or {}).get("content", "")
                if typ == "assistant":
                    if not isinstance(content, list):
                        continue
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") in chat_history._REAL_REPLY_TOOL_NAMES:
                            inp = c.get("input") or {}
                            text = (inp.get("text") or inp.get("message") or "").strip()
                            to = str(inp.get("chat_id") or "") or None
                            if text and (to is None or to == chat):
                                out.append(("bot", text, int(ts * 1000)))
                    continue
                raw = chat_history._extract_text(content)
                if not raw or chat_history._is_internal_injection(raw) or _chat_of(raw) != chat:
                    continue
                text = chat_history._strip_crossmem(chat_history._strip_channel_wrapper(raw)).strip()
                if len(text) >= 1:
                    out.append(("user", text, int(ts * 1000)))
    # 同一条可能出现在多个会话文件里：按（谁、时间、内容）去重
    uniq = []
    for r in sorted(out, key=lambda r: r[2]):
        k = (r[0], r[2], r[1])
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    return uniq


def _open_ledger(bot: str) -> sqlite3.Connection:
    state = os.path.join(_home(), "bots", bot, "state")
    os.makedirs(state, mode=0o700, exist_ok=True)
    db = sqlite3.connect(os.path.join(state, "ledger.sqlite"))
    db.execute("PRAGMA busy_timeout = 5000")
    with open(SCHEMA, encoding="utf-8") as f:
        db.executescript(f.read())
    return db


def _gateway_running(bot: str) -> bool:
    hb = os.path.join(_home(), "bots", bot, "state", "heartbeat")
    try:
        return time.time() - os.path.getmtime(hb) < 15
    except OSError:
        return False


def import_dialog(db: sqlite3.Connection, chat: str, rows: list[tuple[str, str, int]]) -> int:
    if not rows:
        return 0
    if db.execute("SELECT COUNT(*) FROM segments WHERE chat_id = ?", (chat,)).fetchone()[0]:
        raise SystemExit("这个聊天在新系统里已经聊过（账本里有会话段），不导入旧记录，免得新旧交错。")
    now = int(time.time() * 1000)
    first, last = rows[0][2], rows[-1][2]
    cur = db.cursor()
    cur.execute("INSERT INTO segments (chat_id, mcp_token, state, created_at, closed_at, close_reason) VALUES (?, ?, 'abandoned', ?, ?, ?)",
                (chat, secrets.token_hex(16), first, last, "imported from old system"))
    seg = cur.lastrowid
    cur.execute("INSERT INTO turns (root_id, chat_id, segment_id, kind, state, inbound_ids, started_at, ended_at) VALUES (0, ?, ?, 'message', 'ok', '[]', ?, ?)",
                (chat, seg, first, last))
    turn = cur.lastrowid
    cur.execute("UPDATE turns SET root_id = ? WHERE id = ?", (turn, turn))
    ids = []
    for who, text, ts in rows:
        key = "import:" + hashlib.sha1(f"{who}|{ts}|{text}".encode("utf-8")).hexdigest()[:24]
        if who == "user":
            cur.execute("INSERT OR IGNORE INTO inbound (ukey, chat_id, kind, sender_id, text, ts, received_at, state, turn_id) VALUES (?, ?, 'user', ?, ?, ?, ?, 'done', ?)",
                        (key, chat, chat, text, ts, ts, turn))
            if cur.rowcount:
                ids.append(cur.lastrowid)
        else:
            cur.execute("INSERT OR IGNORE INTO outbound (okey, chat_id, turn_id, part, kind, text, state, created_at, sent_at) VALUES (?, ?, ?, 1, 'text', ?, 'sent', ?, ?)",
                        (key, chat, turn, text, ts, ts))
    cur.execute("UPDATE turns SET inbound_ids = ? WHERE id = ?", (json.dumps(ids), turn))
    cur.execute("INSERT INTO events (at, chat_id, kind, data) VALUES (?, ?, 'history_imported', ?)",
                (now, chat, json.dumps({"rows": len(rows), "segment": seg})))
    db.commit()
    return len(rows)


# ─── 旧承诺 ───

TEXT_KEYS = ("text", "content", "what", "task", "promise", "desc", "description", "note")
DUE_KEYS = ("due_at", "due", "fire_at", "remind_at", "at", "time", "when", "deadline", "ts")
DONE_WORDS = {"done", "fulfilled", "completed", "cancelled", "canceled", "expired", "failed", "kept"}


def _entries(data) -> list:
    if isinstance(data, dict):
        for k in ("promises", "items", "list", "pending"):
            if isinstance(data.get(k), list):
                return data[k]
        return [v for v in data.values() if isinstance(v, dict)]
    return data if isinstance(data, list) else []


def _due_ms(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v if v > 1e12 else v * 1000)
    if isinstance(v, str) and v.strip():
        s = v.strip()
        if s.isdigit():
            return _due_ms(int(s))
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return int(d.timestamp() * 1000)
        except ValueError:
            return None
    return None


def pending_promises(path: str, default_chat: str, now_ms: int) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    out = []
    for e in _entries(data):
        if not isinstance(e, dict):
            continue
        if e.get("done") is True or e.get("fulfilled") is True or str(e.get("state") or e.get("status") or "").lower() in DONE_WORDS:
            continue
        text = next((str(e[k]).strip() for k in TEXT_KEYS if isinstance(e.get(k), str) and e[k].strip()), "")
        due = next((d for d in (_due_ms(e.get(k)) for k in DUE_KEYS) if d), None)
        if not text or not due or due <= now_ms:
            continue
        out.append({"chat": str(e.get("chat_id") or default_chat), "text": text, "due": due})
    return out


def inspect_promises(path: str) -> None:
    """只打印结构：顶层类型、条数、每个字段名和值的类型、各字段出现几次。不打印值。"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    entries = _entries(data)
    print(f"顶层：{type(data).__name__}；识别出 {len(entries)} 条")
    keys: dict[str, set] = {}
    for e in entries:
        if isinstance(e, dict):
            for k, v in e.items():
                keys.setdefault(k, set()).add(type(v).__name__)
    for k in sorted(keys):
        n = sum(1 for e in entries if isinstance(e, dict) and k in e)
        print(f"  {k}: {'/'.join(sorted(keys[k]))}，出现 {n} 次")
    now = int(time.time() * 1000)
    print(f"按现在的规则会导入 {len(pending_promises(path, '0', now))} 条（还没到点、也没完成的）")


def import_promises(db: sqlite3.Connection, items: list[dict]) -> int:
    now = int(time.time() * 1000)
    n = 0
    for p in items:
        dup = db.execute("SELECT 1 FROM commitments WHERE chat_id = ? AND text = ? AND due_at = ?", (p["chat"], p["text"], p["due"])).fetchone()
        if dup:
            continue
        db.execute("INSERT INTO commitments (chat_id, text, quote, due_at, next_at, state, source, created_at) VALUES (?, ?, ?, ?, ?, 'pending', 'import', ?)",
                   (p["chat"], p["text"], p["text"], p["due"], p["due"], now))
        n += 1
    db.commit()
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description="把旧系统的私聊记录和没到点的承诺导进新系统的账本（只读旧数据）")
    ap.add_argument("bot", help="新系统里的 bot 名（~/.dsh-bot/bots/<名>）")
    ap.add_argument("--from", dest="old", required=True, help="旧频道目录")
    ap.add_argument("--old-bot", help="旧系统里的 bot 名（用来精确找到它的会话文件）")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true", help="只数条数，不写")
    ap.add_argument("--inspect-promises", action="store_true", help="只看 .promises.json 的结构")
    a = ap.parse_args()
    old = os.path.abspath(os.path.expanduser(a.old))
    promises = os.path.join(old, ".promises.json")
    if a.inspect_promises:
        if not os.path.exists(promises):
            print("没有 .promises.json")
            return
        inspect_promises(promises)
        return
    chat = _owner(old)
    if not chat:
        raise SystemExit("旧频道目录的 access.json 里没有 allowFrom，不知道主人的私聊是哪个")
    if not a.dry_run and _gateway_running(a.bot):
        raise SystemExit("这个 bot 的网关还在运行，先停掉再导入")
    rows = read_old_dialog(old, a.old_bot, chat, a.days)
    items = pending_promises(promises, chat, int(time.time() * 1000)) if os.path.exists(promises) else []
    print(f"旧私聊记录（最近 {a.days} 天）：对方 {sum(1 for r in rows if r[0] == 'user')} 条，bot {sum(1 for r in rows if r[0] == 'bot')} 条")
    print(f"还没到点的旧承诺：{len(items)} 条")
    if a.dry_run:
        return
    db = _open_ledger(a.bot)
    try:
        n = import_dialog(db, chat, rows)
        m = import_promises(db, items)
    finally:
        db.close()
    print(f"已导入：聊天记录 {n} 条，承诺 {m} 条。新网关第一次处理这个私聊时会用它们补写一份摘要。")


if __name__ == "__main__":
    main()
