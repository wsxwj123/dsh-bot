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
    again = run(env, "--from", str(old))
    assert again.returncode != 0 and "已经聊过" in (again.stderr + again.stdout)


def test_只看承诺文件的结构_不打印内容(tmp_path):
    old, env, _ = make_old(tmp_path)
    r = run(env, "--from", str(old), "--inspect-promises")
    assert r.returncode == 0, r.stderr
    assert "识别出 4 条" in r.stdout and "due_at: int" in r.stdout and "会导入 2 条" in r.stdout
    assert "明早" not in r.stdout and "电影" not in r.stdout


def test_聊过的聊天_没有旧聊天记录只有新承诺_正式导入也整个跳过_和试算说的一致(tmp_path):
    old, env, ledger = make_old(tmp_path)
    assert run(env, "--from", str(old)).returncode == 0
    promises = json.loads((old / ".promises.json").read_text(encoding="utf-8"))
    promises.append({"text": "后来新加的承诺", "due_at": int((time.time() + 86400) * 1000)})
    (old / ".promises.json").write_text(json.dumps(promises, ensure_ascii=False), encoding="utf-8")
    def count():
        db = sqlite3.connect(ledger)
        try:
            return db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]
        finally:
            db.close()
    before = count()
    # --days 0：旧聊天记录一条都不取，只剩承诺
    dry = run(env, "--from", str(old), "--days", "0", "--dry-run")
    assert dry.returncode == 0 and "正式导入会跳过：聊天记录和承诺都不会导入" in dry.stdout
    r = run(env, "--from", str(old), "--days", "0")
    assert r.returncode != 0 and "已经聊过" in (r.stderr + r.stdout)
    assert count() == before


# ─── 增量模式（--since）：切回新系统时只补"切到旧系统那段时间" ───


def append_old(tmp_path, old, lines):
    """往 make_old 造好的旧会话文件里追加记录（时间戳自己给）"""
    import chat_history
    proj = tmp_path / "home" / ".claude" / "projects" / chat_history._project_slug_for(str(old))
    with open(proj / "s.jsonl", "a", encoding="utf-8") as f:
        for l in lines:
            f.write(json.dumps(l, ensure_ascii=False) + "\n")


def q(ledger, sql, args=()):
    db = sqlite3.connect(ledger)
    try:
        return db.execute(sql, args).fetchall()
    finally:
        db.close()


def back_to_dsh(tmp_path):
    """先把旧记录正式导入一次（模拟已经聊过），再插一个还活着的段（模拟切走前新系统还在用），
    然后往旧会话文件里追加"切到旧系统那几天"的记录。返回 (old, env, ledger, watermark_ms, seg_id)。"""
    old, env, ledger = make_old(tmp_path)
    assert run(env, "--from", str(old)).returncode == 0
    db = sqlite3.connect(ledger)
    try:
        db.execute("INSERT INTO segments (chat_id, mcp_token, state, created_at, needs_seed) VALUES ('42', 'tok', 'active', ?, 0)",
                   (int(time.time() * 1000),))
        db.commit()
        # 水位线的基准：脚本按"这个聊天最后一条记录（对方说的或她发的，取较晚的）"算
        wm = db.execute("SELECT MAX(t) FROM ("
                        "SELECT MAX(ts) AS t FROM inbound WHERE chat_id = '42' UNION ALL"
                        " SELECT MAX(COALESCE(sent_at, created_at)) AS t FROM outbound WHERE chat_id = '42')").fetchone()[0]
        seg = db.execute("SELECT id FROM segments WHERE state = 'active'").fetchone()[0]
    finally:
        db.close()
    append_old(tmp_path, old, [
        {"type": "user", "timestamp": iso(wm / 1000 + 60), "message": {"content": '<channel source="telegram" chat_id="42">\n补料的这句\n</channel>'}},
        {"type": "assistant", "timestamp": iso(wm / 1000 + 120), "message": {"content": [{"type": "tool_use", "name": "mcp__telegram-worker__reply", "input": {"text": "补料的回复"}}]}},
        {"type": "user", "timestamp": iso(wm / 1000 + 180), "message": {"content": '<channel source="telegram" chat_id="-100">\n群里的话不要\n</channel>'}},
    ])
    return old, env, ledger, wm, seg


def test_增量导入_只补水位线之后的记录_并把还活着的段标成重新取前情(tmp_path):
    old, env, ledger, _, seg = back_to_dsh(tmp_path)
    n_before = q(ledger, "SELECT COUNT(*) FROM inbound WHERE chat_id = '42'")[0][0]
    promises = json.loads((old / ".promises.json").read_text(encoding="utf-8"))
    promises.append({"text": "回程补的新承诺", "due_at": int((time.time() + 86400) * 1000)})
    (old / ".promises.json").write_text(json.dumps(promises, ensure_ascii=False), encoding="utf-8")

    r = run(env, "--from", str(old), "--since", "auto")
    assert r.returncode == 0, r.stderr
    assert "增量导入" in r.stdout and "还没到点的旧承诺：3 条" in r.stdout
    assert "补料的这句" not in r.stdout  # 只出数字，不打印内容

    texts = [t for (t,) in q(ledger, "SELECT text FROM inbound WHERE chat_id = '42'")]
    assert "补料的这句" in texts                                  # 水位线之后的补进来了
    assert not any("群里的话" in t for t in texts)               # 群聊记录不导
    assert q(ledger, "SELECT COUNT(*) FROM inbound WHERE chat_id = '42'")[0][0] == n_before + 1  # 老记录没重复
    assert [t for (t,) in q(ledger, "SELECT text FROM outbound WHERE chat_id = '42'")] == ["嗨，在呢", "补料的回复"]
    assert q(ledger, "SELECT needs_seed FROM segments WHERE id = ?", (seg,))[0][0] == 1  # 下一轮重新取前情
    assert q(ledger, "SELECT text FROM commitments WHERE state = 'pending' AND source = 'import' AND text = '回程补的新承诺'")

    # 再跑一次：水位线已经推到刚导入的最后一条，旧文件里没有更新的了
    again = run(env, "--from", str(old), "--since", "auto")
    assert again.returncode == 0 and "这段时间没有新记录" in again.stdout
    assert q(ledger, "SELECT COUNT(*) FROM inbound WHERE chat_id = '42'")[0][0] == n_before + 1


def test_增量导入_账本里没有这个聊天的行_明确报错并提示怎么手动指定(tmp_path):
    old, env, ledger = make_old(tmp_path)
    r = run(env, "--from", str(old), "--since", "auto")
    assert r.returncode != 0 and "拿不到水位线" in (r.stderr + r.stdout) and "--since" in (r.stderr + r.stdout)
    assert not ledger.exists()


def test_增量导入_dry_run_只数条数什么都不写(tmp_path):
    old, env, ledger, _, seg = back_to_dsh(tmp_path)
    n_before = q(ledger, "SELECT COUNT(*) FROM inbound")[0][0]
    r = run(env, "--from", str(old), "--since", "auto", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "增量导入" in r.stdout and "什么都没写" in r.stdout
    assert q(ledger, "SELECT COUNT(*) FROM inbound")[0][0] == n_before
    assert q(ledger, "SELECT needs_seed FROM segments WHERE id = ?", (seg,))[0][0] == 0


def test_增量导入_水位线不是数字或不是正数就拒绝(tmp_path):
    old, env, _ = make_old(tmp_path)
    for bad in ("昨天", "0", "-5"):
        r = run(env, "--from", str(old), "--since", bad)
        assert r.returncode != 0 and "--since" in (r.stderr + r.stdout), bad


def test_增量导入_网关还在跑就先停掉再导(tmp_path):
    old, env, ledger, _, seg = back_to_dsh(tmp_path)
    hb = tmp_path / "dsh" / "bots" / "bot5" / "state" / "heartbeat"
    hb.write_text("{}", encoding="utf-8")
    r = run(env, "--from", str(old), "--since", "auto")
    assert r.returncode != 0 and "网关还在运行" in (r.stderr + r.stdout)
    assert q(ledger, "SELECT COUNT(*) FROM inbound WHERE chat_id = '42'")[0][0]  # 账本没被写坏，行数照旧
    assert len(q(ledger, "SELECT id FROM segments WHERE state = 'active'")) == 1
