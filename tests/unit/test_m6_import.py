# -*- coding: utf-8 -*-
"""M6：切换时导入旧私聊记录和没到点的旧承诺（只读旧数据，只出数字）。"""
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "import_history.py"


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def make_old(tmp_path):
    """旧系统布局：频道目录 + ~/.claude/projects/<slug>/<会话>.jsonl"""
    home = tmp_path / "home"
    old = tmp_path / "old" / "chan"
    old.mkdir(parents=True)
    (old / "access.json").write_text(json.dumps({"allowFrom": ["42"]}), encoding="utf-8")
    sys.path.insert(0, str(ROOT))
    import chat_history
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "DSH_BOT_HOME": str(tmp_path / "dsh"), "PYTHONIOENCODING": "utf-8"}
    slug = chat_history._project_slug_for(str(old))
    proj = home / ".claude" / "projects" / slug
    proj.mkdir(parents=True)
    now = time.time()
    lines = [
        {"type": "user", "timestamp": iso(now - 3600), "message": {"content": '⟦时间⟧\n<channel source="telegram" chat_id="42" user="u">\n你好呀\n</channel>'}},
        {"type": "assistant", "timestamp": iso(now - 3500), "message": {"content": [{"type": "tool_use", "name": "mcp__telegram-worker__reply", "input": {"text": "嗨，在呢"}}]}},
        {"type": "assistant", "timestamp": iso(now - 3400), "message": {"content": [{"type": "text", "text": "（只是想了想，没发）"}]}},
        {"type": "user", "timestamp": iso(now - 3300), "message": {"content": '<channel source="telegram" chat_id="-100">\n群里的话\n</channel>'}},
        {"type": "assistant", "timestamp": iso(now - 3200), "message": {"content": [{"type": "tool_use", "name": "mcp__telegram-worker__reply", "input": {"text": "群里的回复", "chat_id": "-100"}}]}},
        {"type": "user", "timestamp": iso(now - 40 * 86400), "message": {"content": '<channel source="telegram" chat_id="42">\n很久以前\n</channel>'}},
        {"type": "user", "timestamp": iso(now - 3000), "message": {"content": "[memory-compactor] 内部注入"}},
    ]
    (proj / "s.jsonl").write_text("\n".join(json.dumps(l, ensure_ascii=False) for l in lines) + "\n", encoding="utf-8")
    ms = lambda s: int(s * 1000)
    (old / ".promises.json").write_text(json.dumps([
        {"text": "明早叫醒对方", "due_at": ms(now + 86400), "done": False},
        {"text": "已经过点的", "due_at": ms(now - 60)},
        {"content": "做完了的", "due": iso(now + 7200), "status": "done"},
        {"what": "周五提醒看电影", "when": iso(now + 3 * 86400)},
    ], ensure_ascii=False), encoding="utf-8")
    return old, env, tmp_path / "dsh" / "bots" / "bot5" / "state" / "ledger.sqlite"


def run(env, *args):
    return subprocess.run([sys.executable, str(SCRIPT), "bot5", *args], env=env, capture_output=True, text=True, encoding="utf-8")


def test_导入私聊记录和没到点的承诺_只出数字(tmp_path):
    old, env, ledger = make_old(tmp_path)
    dry = run(env, "--from", str(old), "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert "对方 1 条，bot 1 条" in dry.stdout and "还没到点的旧承诺：2 条" in dry.stdout
    assert not ledger.exists()
    r = run(env, "--from", str(old))
    assert r.returncode == 0, r.stderr
    for secret in ("你好呀", "嗨，在呢", "明早叫醒对方"):
        assert secret not in r.stdout
    db = sqlite3.connect(ledger)
    seg = db.execute("SELECT id, state, chat_id FROM segments").fetchall()
    assert len(seg) == 1 and seg[0][1] == "abandoned" and seg[0][2] == "42"
    turn = db.execute("SELECT id, root_id, segment_id, inbound_ids FROM turns").fetchone()
    assert turn[1] == turn[0] and turn[2] == seg[0][0]
    inb = db.execute("SELECT id, text, state, turn_id FROM inbound").fetchall()
    assert [(t, s) for _, t, s, _ in inb] == [("你好呀", "done")] and json.loads(turn[3]) == [inb[0][0]]
    assert db.execute("SELECT text, state, turn_id FROM outbound").fetchall() == [("嗨，在呢", "sent", turn[0])]
    assert sorted(t for (t,) in db.execute("SELECT text FROM commitments WHERE source = 'import' AND state = 'pending'")) == ["周五提醒看电影", "明早叫醒对方"]
    db.close()
    # 已经聊过：试算就提示正式导入会跳过；正式导入跳过聊天记录，承诺不重复导
    dry2 = run(env, "--from", str(old), "--dry-run")
    assert dry2.returncode == 0 and "已经聊过" in dry2.stdout and "会跳过" in dry2.stdout
    again = run(env, "--from", str(old))
    assert again.returncode == 0 and "聊天记录跳过" in again.stdout and "承诺 0 条" in again.stdout
    db = sqlite3.connect(ledger)
    assert db.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 1 and db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 2
    db.close()


def test_只看承诺文件的结构_不打印内容(tmp_path):
    old, env, _ = make_old(tmp_path)
    r = run(env, "--from", str(old), "--inspect-promises")
    assert r.returncode == 0, r.stderr
    assert "识别出 4 条" in r.stdout and "due_at: int" in r.stdout and "会导入 2 条" in r.stdout
    assert "明早" not in r.stdout and "电影" not in r.stdout
