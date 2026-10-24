# -*- coding: utf-8 -*-
"""forced-early-initiate 验收测试夹具（黑盒：只走 run_dsh 公开函数和 life-context.py 命令行）。

两套夹具：
  - early（用例 1-12）：直接调 scripts/self_initiate.py 的 run_dsh(bot, chat, cfg, now)。
    life-context 的子进程调用换成假的（记下每次的 env 关键字参数），网关换成本机假服务。
    随机闸的间隔用 random.randint 固定（默认 24 小时），时间全靠 now 参数推进。
  - lc（用例 13-15）：真跑 life-context.py 的命令行；LLM 换成 fake-claude，断网桩拦一切联网。
"""
def _require_total_gate():
    """自检：tests/conftest.py 的总闸没加载，就拒绝运行本目录的用例。

    pytest 只加载「启动目录及其下」的 conftest。cd 到本目录（或它的某个上级子目录）里面
    再起 pytest，总闸不会被加载：HUB_LAUNCHCTL / HUB_BUN 不是毒丸，用例可能碰到真实的
    launchd。总闸导入时在 sys 上留了带本进程 pid 的标记（tests/conftest.py 的
    _STATE_ATTR），这里只认它。自测在 tests/gate_selftest/。
    """
    import os
    import sys
    gate = getattr(sys, "_dsh_bot_test_gate", None)
    if isinstance(gate, dict) and gate.get("pid") == os.getpid():
        return
    here = os.path.dirname(os.path.abspath(__file__))
    tests = here
    while os.path.basename(tests) != "tests" and os.path.dirname(tests) != tests:
        tests = os.path.dirname(tests)
    root = os.path.dirname(tests)
    raise RuntimeError(
        "测试总闸（tests/conftest.py）没有加载，拒绝运行本目录的用例。\n"
        "在子目录里面启动 pytest 时总闸不生效，用例可能碰到真实的 launchd。\n"
        "请从仓库根或 tests/ 目录启动，例如：\n"
        "    cd %s && python3 -m pytest %s" % (root, os.path.relpath(here, root)))


_require_total_gate()
del _require_total_gate

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from fei_support import (BOT, CHAT, DAY, HERE, HOUR, MIN, ROOT, T0, _load,  # noqa: F401
                         _make_bot_layout, set_connection)


# ============================================================ 用例 1-12：run_dsh
class Result:
    def __init__(self, rc, out, calls, injects):
        self.rc = rc
        self.out = out
        self.calls = calls      # 本次新增的 life-context 调用：每项是 env 字典（没传 env 记为 {}）
        self.injects = injects  # 本次新增的网关投递：每项是 (path, body)


class Harness:
    def __init__(self, si, root, srv_got, capsys, monkeypatch):
        self.si = si
        self.root = root
        self.got = srv_got
        self.capsys = capsys
        self.monkeypatch = monkeypatch
        self.cfg = {"bot_channel_path": str(root / "channel"), "dispatcher_port": 0}
        self.interval = DAY          # 下一次"正常投递成功后"随机闸重抽出的间隔
        self.calls = []

    def run(self, now, action):
        """跑一次 run_dsh；life-context 假件回 action（字典）。返回本次新增的调用与投递。"""
        calls_before, got_before = len(self.calls), len(self.got)
        out = json.dumps(action, ensure_ascii=False)

        class R:
            returncode = 0
            stdout = out

        def fake_run(*a, **k):
            env = k.get("env")
            self.calls.append(dict(env) if env is not None else {})
            return R()

        self.monkeypatch.setattr(self.si.subprocess, "run", fake_run)
        self.capsys.readouterr()
        rc = self.si.run_dsh(BOT, CHAT, self.cfg, now)
        cap = self.capsys.readouterr()
        return Result(rc, cap.out + cap.err, self.calls[calls_before:], self.got[got_before:])

    # 常用动作
    def normal_delivery(self, now, interval=DAY):
        """闸已到点（或首次）的一次正常投递；成功后随机闸重抽成 interval 秒。"""
        self.interval = interval
        r = self.run(now, {"action": "TEXT", "text": "想你了"})
        assert r.rc == 0
        assert len(r.calls) == 1 and "SELF_INITIATE_EARLY" not in r.calls[0], "搭场景失败：正常投递不该带提前标记"
        assert len(r.injects) == 1, "搭场景失败：正常投递没投出去"
        return r

    def pending_gate_without_delivery(self, now):
        """没有任何成功投递的前提下，让随机闸停在 now+1 小时（5h 配额耗尽 → 固定 3600 秒后再试）。"""
        r = self.run(now, {"action": "SKIP", "reason": "5h 配额耗尽"})
        assert r.rc == 0 and r.injects == [], "搭场景失败"
        return r


@pytest.fixture
def early(tmp_path, monkeypatch, capsys):
    import random
    monkeypatch.delenv("DSH_BOT_LEDGER", raising=False)
    monkeypatch.delenv("SELF_INITIATE_EARLY", raising=False)
    root, cfgs = _make_bot_layout(tmp_path)
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(cfgs))
    monkeypatch.setenv("DSH_BOT_HOME", str(tmp_path))
    (root / "state" / "api.key").write_text("TESTONLY-api-token\n", encoding="utf-8")

    got = []

    class Gw(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["content-length"])).decode("utf-8"))
            got.append((self.path, body))
            out = json.dumps({"ok": True, "id": len(got)}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Gw)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    holder = {}
    monkeypatch.setattr(random, "randint", lambda a, b: holder["h"].interval)
    si = _load("self_initiate_early_acc", ROOT / "scripts" / "self_initiate.py")
    h = Harness(si, root, got, capsys, monkeypatch)
    h.cfg["dispatcher_port"] = srv.server_address[1]
    holder["h"] = h
    yield h
    srv.shutdown()
    srv.server_close()


# ============================================================ 用例 13-15：真跑 life-context.py
class LifeWorld:
    def __init__(self, base: Path):
        self.base = base.resolve()
        self.root, self.configs = _make_bot_layout(self.base)
        self.work = self.base / "work"
        self.mark = self.base / "stub.mark"
        self.fake_log = self.base / "fake.log"
        for d in (self.work, self.base / "home", self.base / "tmp"):
            d.mkdir(parents=True, exist_ok=True)
        shutil.copytree(HERE / "stub", self.base / "stub", ignore=shutil.ignore_patterns("__pycache__"))
        self.db_path = self.base / "state.db"

    def env(self, extra=None):
        env = {k: v for k, v in os.environ.items()
               if "proxy" not in k.lower()
               and not k.startswith(("CLAUDE", "DSH_BOT", "HUB_", "BOTLIFE", "TEST_", "FAKE_", "SELF_INITIATE"))}
        env.update(
            HOME=str(self.base / "home"), TMPDIR=str(self.base / "tmp"),
            BOTLIFE_STATE_DB=str(self.db_path),
            HUB_CONFIGS_DIR=str(self.configs), DSH_BOT_HOME=str(self.base),
            CLAUDEBOTLIFE_FORCE_CLAUDE="1", CLAUDE_BIN=str(HERE / "fake-claude"),
            PYTHONPATH=str(self.base / "stub"),
            PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
            TEST_REPO=str(ROOT), TEST_RNG="threshold", TEST_STUB_MARK=str(self.mark),
            FAKE_LOG=str(self.fake_log),
        )
        env.update(extra or {})
        assert not [k for k in env if "proxy" in k.lower()]
        return env

    def _exec(self, argv, extra=None, cwd=None):
        proc = subprocess.run(argv, cwd=str(cwd or self.work), env=self.env(extra),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)
        # 断网自证：桩必须加载过，且没有被放行的连接
        marks = self.mark.read_text(encoding="utf-8").splitlines() if self.mark.exists() else []
        assert any(m.startswith("loaded ") for m in marks), "断网桩没加载"
        assert not [m for m in marks if not m.startswith(("loaded ", "blocked "))], "有被放行的网络连接"
        return proc

    def init_db(self):
        p = self._exec([sys.executable, "-c", "import db; db.init()"], cwd=ROOT)
        assert p.returncode == 0, p.stderr[-600:]

    def put_inbound(self, text, ago_s):
        ms = int((time.time() - ago_s) * 1000)
        with closing(sqlite3.connect(str(self.root / "state" / "ledger.sqlite"))) as c:
            c.execute("INSERT INTO inbound (ukey, chat_id, kind, text, ts, received_at, state) "
                      "VALUES (?, ?, 'user', ?, ?, ?, 'done')", (f"in-{ms}", CHAT, text, ms, ms))
            c.commit()

    def life(self, early_flag):
        """命令行跑 life-context.py bot5 42（不放行预筛）；判官假件一律判 skip，
        这样"判官被问过"会直接体现在结果里。返回 (进程, 最后一行 JSON, 判官调用次数)。"""
        extra = {"FAKE_ACTION": "skip"}
        if early_flag:
            extra["SELF_INITIATE_EARLY"] = "1"
        p = self._exec([sys.executable, str(ROOT / "life-context.py"), BOT, CHAT], extra=extra)
        assert p.returncode == 0, "life-context 退出码 %s，stderr：%s" % (p.returncode, p.stderr[-600:])
        lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
        assert lines, "stdout 为空；stderr：%r" % p.stderr[-600:]
        judge = 0
        if self.fake_log.exists():
            judge = self.fake_log.read_text(encoding="utf-8").split().count("judge")
        return p, json.loads(lines[-1]), judge


@pytest.fixture
def lc(tmp_path):
    if os.name == "nt":
        pytest.skip("假 LLM 是 sh 脚本")
    w = LifeWorld(tmp_path)
    w.init_db()
    set_connection(w.root, 0.9)
    return w
