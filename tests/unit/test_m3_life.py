# -*- coding: utf-8 -*-
"""M3：新系统 bot 的生活周边——配置合并、主动消息按原因安排下次并投递到网关、情绪改读账本、作息计划。"""
import importlib.util
import json
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = (ROOT / "gateway" / "src" / "ledger-schema.sql").read_text(encoding="utf-8")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def dsh_bot(tmp_path, monkeypatch):
    """新系统布局：configs/bot5.yml（life_config 指向旧配置）+ bots/bot5/{channel,state/ledger.sqlite}"""
    monkeypatch.delenv("DSH_BOT_LEDGER", raising=False)
    root = tmp_path / "bots" / "bot5"
    (root / "channel").mkdir(parents=True)
    (root / "state").mkdir()
    db = sqlite3.connect(root / "state" / "ledger.sqlite")
    db.executescript(SCHEMA)
    db.commit()
    cfgs = tmp_path / "configs"
    cfgs.mkdir()
    (tmp_path / "old.yml").write_text("id: oldbot\nchat_id: '1'\nbot_channel_path: /old/channel\nsleep_hours: '00:00-07:30'\npersona_summary: 旧配置里的设定\n", encoding="utf-8")
    (cfgs / "bot5.yml").write_text(f"id: bot5\nbot_channel_path: {json.dumps(str(root / 'channel'))}\ndispatcher_port: 0\nlife_config: {json.dumps(str(tmp_path / 'old.yml'))}\n", encoding="utf-8")
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(cfgs))
    return root, db


def test_新配置用life_config指向旧配置_新配置里写了的优先(dsh_bot):
    import config_loader
    root, _ = dsh_bot
    cfg = config_loader.load_bot("bot5")
    assert cfg["bot_channel_path"] == str(root / "channel")
    assert cfg["sleep_hours"] == "00:00-07:30"
    assert cfg["persona_summary"].endswith("旧配置里的设定")


def test_主动消息_跳过的原因决定下次什么时候再试():
    si = _load("self_initiate", ROOT / "scripts" / "self_initiate.py")
    now = 1_000_000
    assert si.retry_after("sleep_hours(23:10)", now, wake=now + 8 * 3600) == 8 * 3600
    assert si.retry_after("sleep_hours(23:10)", now, wake=None) == 3600
    assert 5400 <= si.retry_after("用户大概率睡眠中", now) <= 9000
    assert si.retry_after("5h 配额耗尽", now) == 3600
    for r in ("非静默期(距上次5min<60min)", "刚说过相同话题", "专注时段(上课)无突发"):
        assert 1800 <= si.retry_after(r, now) <= 3600
    assert 3600 <= si.retry_after("毫无情境素材", now) <= 7200


class _Gw(BaseHTTPRequestHandler):
    got: list = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["content-length"])).decode("utf-8"))
        _Gw.got.append((self.path, self.headers.get("authorization"), body))
        out = json.dumps({"ok": True, "id": 1}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def test_主动消息_投递到网关的本机接口_真正投出去才重抽间隔(dsh_bot, monkeypatch):
    root, _ = dsh_bot
    (root / "state" / "api.key").write_text("TESTONLY-api-token\n", encoding="utf-8")
    srv = HTTPServer(("127.0.0.1", 0), _Gw)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    si = _load("self_initiate", ROOT / "scripts" / "self_initiate.py")
    cfg = {"bot_channel_path": str(root / "channel"), "dispatcher_port": srv.server_address[1]}

    class R:
        returncode = 0
        stdout = ""

    def life(action):
        r = R()
        r.stdout = json.dumps(action, ensure_ascii=False)
        return lambda *a, **k: r

    nf = root / "state" / "self-initiate" / "42.next"
    now = int(time.time())
    # 1. 跳过：没投递，下次时间按原因定
    monkeypatch.setattr(si.subprocess, "run", life({"action": "SKIP", "reason": "刚说过相同话题"}))
    assert si.run_dsh("bot5", "42", cfg, now) == 0
    assert 1800 <= int(nf.read_text()) - now <= 3600
    assert _Gw.got == []
    # 2. 还没到下次机会：直接跳过，不调 life-context
    monkeypatch.setattr(si.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该调")))
    assert si.run_dsh("bot5", "42", cfg, now + 60) == 0
    # 3. 有话说：投到网关（带口令），然后才重抽随机间隔
    monkeypatch.setattr(si.subprocess, "run", life({"action": "TEXT", "text": "想她了"}))
    assert si.run_dsh("bot5", "42", cfg, now + 4000) == 0
    path, auth, body = _Gw.got[-1]
    assert path == "/v1/inject" and auth == "Bearer TESTONLY-api-token"
    assert body["chat_id"] == "42" and body["source"] == "self_initiate"
    assert body["text"].startswith("⟦系统·主动开口⟧") and "想她了" in body["text"]
    assert si.COOLDOWN_MIN <= int(nf.read_text()) - (now + 4000) <= si.COOLDOWN_MAX
    # 4. 手动测试用的 --skip-judge：不问 life-context，直接投默认文本，也不改下次机会的时间
    before = nf.read_text()
    monkeypatch.setattr(si.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该调")))
    assert si.run_dsh("bot5", "42", cfg, now + 5000, skip_judge=True) == 0
    assert _Gw.got[-1][2]["text"].startswith("⟦系统·主动开口⟧") and nf.read_text() == before
    srv.shutdown()


def test_情绪_新系统的bot从账本读对话(dsh_bot):
    root, db = dsh_bot
    tick = _load("jiwen_tick", ROOT / "jiwen" / "tick.py")
    t = int(time.time()) - 600
    db.execute("INSERT INTO inbound (ukey, chat_id, kind, text, ts, received_at, state) VALUES ('a', '1', 'user', '今天好累', ?, ?, 'done')", (t * 1000, t * 1000))
    db.execute("INSERT INTO outbound (chat_id, turn_id, part, kind, text, state, created_at, sent_at) VALUES ('1', 1, 1, 'text', '抱抱你', 'sent', ?, ?)", ((t + 5) * 1000, (t + 5) * 1000))
    db.execute("INSERT INTO inbound (ukey, chat_id, kind, text, ts, received_at, state) VALUES ('b', '1', 'synthetic', '⟦系统·主动开口⟧', ?, ?, 'done')", ((t + 9) * 1000, (t + 9) * 1000))
    db.commit()
    msgs, max_ts = tick.find_recent_messages(str(root / "channel"), 0)
    assert msgs == [{"role": "user", "content": "今天好累"}, {"role": "assistant", "content": "抱抱你"}]
    assert max_ts == t
    assert tick.has_new_user_msg(msgs, 0, max_ts)
    msgs2, max2 = tick.find_recent_messages(str(root / "channel"), t)
    assert not tick.has_new_user_msg(msgs2, t, max2)


def test_作息计划_接下来几次起床的时刻(monkeypatch):
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(ROOT / "configs"))
    hs = _load("hang_situation", ROOT / "hang_situation.py")
    p = hs.plan("_example")
    assert len(p["wakes"]) >= 2
    assert p["wakes"] == sorted(p["wakes"]) and all(isinstance(w, int) for w in p["wakes"])
    assert 20 * 3600 <= p["wakes"][1] - p["wakes"][0] <= 28 * 3600


def test_情绪_新系统的bot不用全局配置也能跑_关系数值写在自己的频道目录_不碰旧系统目录(dsh_bot, tmp_path, monkeypatch):
    """真机发现：读不到 _global.yml 就悄悄跳过；补上之后关系数值又写进了 ~/.claude/channels/<bot>/。"""
    import sys
    root, db = dsh_bot
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("DSH_BOT_HOME", str(tmp_path))
    (tmp_path / "credentials.yaml").write_text("version: 1\nrefs:\n  DEEPSEEK_API_KEY: sk-test-only-0000000000\n", encoding="utf-8")
    (root / "channel" / "relationship.json").write_text(json.dumps({"affection": 50, "trust": 50, "desire": 0, "energy": 80}), encoding="utf-8")
    t = int(time.time()) - 60
    db.execute("INSERT INTO inbound (ukey, chat_id, kind, text, ts, received_at, state) VALUES ('j1', '1', 'user', '今天考试过了', ?, ?, 'done')", (t * 1000, t * 1000))
    db.commit()
    tick = _load("jiwen_tick_main", ROOT / "jiwen" / "tick.py")
    # 切换期间和旧系统的积温任务同时跑：_global.yml 里列的旧 bot 不碰（它们归旧系统管）
    monkeypatch.setattr(tick.config_loader, "load_global", lambda: {"jiwen": {"enabled": True, "bots": ["oldbot1", "oldbot2"]}})
    calls = []
    monkeypatch.setattr(tick.deepseek_delta, "compute_delta", lambda **k: calls.append(k) or {"connection": 0.1, "valence": 0.3, "arousal": 0.1})
    ticked = []
    real_tick = tick.tick_one_bot
    monkeypatch.setattr(tick, "tick_one_bot", lambda bot_id, *a, **k: ticked.append(bot_id) or real_tick(bot_id, *a, **k))
    monkeypatch.setattr(sys, "argv", ["tick.py"])
    tick.main()
    assert ticked == ["bot5"]
    assert len(calls) == 1
    rel = json.loads((root / "channel" / "relationship.json").read_text(encoding="utf-8"))
    assert "updated_ts" in rel and "prompt_snippet" in rel
    assert (root / "state" / "jiwen").is_dir()
    assert not (home / ".claude").exists()


def test_情绪_什么都没配置时退出码不是0(tmp_path, monkeypatch):
    import sys
    cfgs = tmp_path / "empty-configs"
    cfgs.mkdir()
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(cfgs))
    tick = _load("jiwen_tick_empty", ROOT / "jiwen" / "tick.py")
    monkeypatch.setattr(tick.config_loader, "load_global", lambda: {})
    monkeypatch.setattr(sys, "argv", ["tick.py"])
    with pytest.raises(SystemExit) as e:
        tick.main()
    assert e.value.code == 2
