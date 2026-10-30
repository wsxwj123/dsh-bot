# -*- coding: utf-8 -*-
"""outage-delivery 验收（黑盒）：Python 一侧的第 24、25 条与错误契约。

依据只有 .devflow/outage-delivery-接口.md：
  - 第 25 条：gateway_client.telegram_link(channel_dir, timeout=5.0) 读网关 /v1/health 的 telegram.link，
    通 → "up"、不通 → "down"，网关没开/连不上/超时/正文不是 JSON/旧网关没有 link 字段 → "unknown"，任何情况都不抛异常。
  - 第 24 条：scripts/self_initiate.py 机会已到、线路 "down" → 标准错误一行 skip、退出码 0、三个状态文件一个字节都不变、
    不调 life-context、不投递；恢复后下一次照常判断；"unknown" 照旧；--skip-judge 不查线路。

入口只用公开的：telegram_link() 函数调用；self_initiate.py 以子进程的命令行跑（bot chat [--skip-judge]）。
假件只在系统边界：本机假网关（/v1/health 与 /v1/inject，只绑 127.0.0.1）、life-context 子进程（在子进程里把
subprocess.run 对 life-context 的调用换成记账 + 固定回 TEXT，其余命令照常放行）。
HOME、DSH_BOT_HOME、配置目录都指到 tmp_path，不读写真实的 ~/.dsh-bot、~/.claude。

运行：cd <仓库根> && python3 -m pytest tests/acceptance/outage_delivery -q
"""


def _require_total_gate():
    """自检：tests/conftest.py 的总闸没加载，就拒绝运行本目录的用例（与其它验收目录同一做法）。"""
    import os
    import sys
    gate = getattr(sys, "_dsh_bot_test_gate", None)
    if isinstance(gate, dict) and gate.get("pid") == os.getpid():
        return
    raise RuntimeError("测试总闸（tests/conftest.py）没有加载，拒绝运行。请从仓库根启动："
                       "python3 -m pytest tests/acceptance/outage_delivery")


_require_total_gate()
del _require_total_gate

import json  # noqa: E402
import os  # noqa: E402
import socket  # noqa: E402
import sqlite3  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from contextlib import closing  # noqa: E402
from datetime import datetime  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import gateway_client  # noqa: E402

SCRIPT = REPO / "scripts" / "self_initiate.py"
SCHEMA = REPO / "gateway" / "src" / "ledger-schema.sql"   # 只用来建一份空账本（与其它验收夹具相同），不读实现
BOT, CHAT = "bot5", "42"
SKIP_LINE = "skip: Telegram 线路不通，这次不开口（下次机会时间不变）"
# 注入的「现在」：当天下午 3 点（白天，避开夜间不开口的时段）
NOW = int(datetime.now().replace(hour=15, minute=0, second=0, microsecond=0).timestamp())
PROXY_KEYS = ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")


def _health_body(link):
    t = {"last_ok_ms": 500, "last_reach_ms": 500, "problems": []}
    if link is not None:
        t["link"] = link
        t["link_down_ms"] = 60000 if link == "down" else None
    return {"ok": True, "problems": [], "telegram": t}


class FakeGateway:
    """只绑 127.0.0.1 的假网关：GET /v1/health 按 health=(状态码, 正文) 回；POST /v1/inject 记下并回 ok。"""

    def __init__(self):
        self.hits = []
        self.health = (200, _health_body("up"))
        self.delay = 0.0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, status, body):
                data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except OSError:
                    pass

            def do_GET(self):  # noqa: N802
                outer.hits.append(("GET", self.path, None))
                if outer.delay:
                    time.sleep(outer.delay)
                if self.path.startswith("/v1/health"):
                    self._reply(*outer.health)
                else:
                    self._reply(404, {"ok": False})

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw.decode("utf-8"))
                except ValueError:
                    body = None
                outer.hits.append(("POST", self.path, body))
                self._reply(200, {"ok": True, "id": len(outer.hits)})

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._srv.daemon_threads = True
        threading.Thread(target=self._srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.port = self._srv.server_address[1]

    def stop(self):
        self._srv.shutdown()
        self._srv.server_close()

    def injects(self):
        return [h for h in self.hits if h[0] == "POST" and h[1].startswith("/v1/inject")]

    def health_queries(self):
        return [h for h in self.hits if h[0] == "GET" and h[1].startswith("/v1/health")]


def _dead_port():
    """一个刚释放、无人监听的本机端口"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def fake_gateway():
    gw = FakeGateway()
    yield gw
    gw.stop()


# ============================================================ 第 25 条：telegram_link()

def _client_layout(base, port, with_files=True):
    """<base>/bot/{channel, state/api.key, state/api.port}；返回频道目录"""
    channel, state = base / "bot" / "channel", base / "bot" / "state"
    channel.mkdir(parents=True)
    state.mkdir()
    if with_files:
        (state / "api.key").write_text("TESTONLY-outage-key\n", encoding="utf-8")
        (state / "api.port").write_text("%d\n" % port, encoding="utf-8")
    return channel


# (用例名, 健康接口状态码, 正文, 回应前延迟秒, 端口情形 live/dead/nofiles, 调用 timeout, 期望)
LINK_CASES = [
    ("网关通_up", 200, _health_body("up"), 0, "live", 5.0, "up"),
    ("网关不通_down", 200, _health_body("down"), 0, "live", 5.0, "down"),
    ("健康接口503_照读正文里的down", 503,
     {"ok": False, "problems": ["Telegram 已经 95 秒没有拉取成功"], "telegram": {"link": "down", "last_reach_ms": 95000, "link_down_ms": 3000}},
     0, "live", 5.0, "down"),
    ("旧网关没有link字段_unknown", 200, _health_body(None), 0, "live", 5.0, "unknown"),
    ("正文不是JSON_unknown", 200, b"<html>bad gateway</html>", 0, "live", 5.0, "unknown"),
    ("telegram字段不是对象_unknown", 200, {"ok": True, "telegram": "up"}, 0, "live", 5.0, "unknown"),
    ("超时_unknown", 200, _health_body("up"), 3.0, "live", 0.5, "unknown"),
    ("网关没开_端口无人监听_unknown", 200, _health_body("up"), 0, "dead", 2.0, "unknown"),
    ("没有端口与口令文件_unknown", 200, _health_body("up"), 0, "nofiles", 2.0, "unknown"),
]


@pytest.mark.parametrize("case", LINK_CASES, ids=[c[0] for c in LINK_CASES])
def test_25_telegram_link_按健康接口返回up_down_unknown_任何情况不抛异常(tmp_path, monkeypatch, fake_gateway, case):
    name, status, body, delay, port_mode, timeout, want = case
    for k in PROXY_KEYS:
        monkeypatch.delenv(k, raising=False)
    fn = getattr(gateway_client, "telegram_link", None)
    assert callable(fn), "gateway_client 里应有公开函数 telegram_link(channel_dir, timeout=5.0)"
    fake_gateway.health = (status, body)
    fake_gateway.delay = delay
    port = fake_gateway.port if port_mode == "live" else _dead_port()
    channel = _client_layout(tmp_path, port, with_files=(port_mode != "nofiles"))
    t0 = time.monotonic()
    try:
        got = fn(str(channel), timeout=timeout)
    except Exception as e:  # noqa: BLE001 —— 契约：任何情况都不抛异常
        pytest.fail("telegram_link 抛了异常：%r" % (e,))
    elapsed = time.monotonic() - t0
    assert got == want, "%s：期望 %r，实际 %r" % (name, want, got)
    if port_mode == "live":
        assert len(fake_gateway.health_queries()) >= 1, "结果应来自网关的 /v1/health"
    if delay:
        assert elapsed < timeout + 2.0, "超时 %.1f 秒却等了 %.1f 秒" % (timeout, elapsed)


# ============================================================ 第 24 条：self_initiate.py

# 子进程引导：把对 life-context 的 subprocess.run 换成「记一笔 + 回 TEXT」，其余命令照常；然后按命令行方式跑脚本
BOOT = r'''
import json, os, runpy, subprocess, sys
_rec = os.environ["T_LIFE_REC"]
_real_run = subprocess.run
class _Done:
    returncode = 0
    stdout = json.dumps({"action": "TEXT", "text": "想你了"}, ensure_ascii=False)
    stderr = ""
def _fake_run(*a, **k):
    argv = a[0] if a else k.get("args", [])
    words = [str(x) for x in argv] if isinstance(argv, (list, tuple)) else [str(argv)]
    if any("life-context" in w for w in words):
        with open(_rec, "a", encoding="utf-8") as f:
            f.write(json.dumps({"argv": words}, ensure_ascii=False) + "\n")
        return _Done()
    return _real_run(*a, **k)
subprocess.run = _fake_run
script = sys.argv[1]
sys.argv = sys.argv[1:]
sys.path.insert(0, os.path.dirname(os.path.abspath(script)))
runpy.run_path(script, run_name="__main__")
'''


def _si_world(base, gw):
    """新系统布局：<base>/configs/bot5.yml + <base>/bots/bot5/{channel, state/{ledger.sqlite, api.key, api.port, self-initiate/}}。
    三个状态文件都预先写好：下次机会时刻早已过去（机会已到）、上次投递很久以前、提前开口记录为空。返回状态目录。"""
    (base / "home").mkdir()
    bot = base / "bots" / BOT
    (bot / "channel").mkdir(parents=True)
    (bot / "state").mkdir()
    with closing(sqlite3.connect(str(bot / "state" / "ledger.sqlite"))) as db:
        db.executescript(SCHEMA.read_text(encoding="utf-8"))
        db.commit()
    (bot / "state" / "api.key").write_text("TESTONLY-outage-key\n", encoding="utf-8")
    (bot / "state" / "api.port").write_text("%d\n" % gw.port, encoding="utf-8")
    cfgs = base / "configs"
    cfgs.mkdir()
    (cfgs / ("%s.yml" % BOT)).write_text(
        "id: %s\nchat_id: '%s'\nbot_channel_path: %s\ndispatcher_port: %d\npersona_summary: 测试人设\n"
        % (BOT, CHAT, json.dumps(str(bot / "channel")), gw.port), encoding="utf-8")
    st = bot / "state" / "self-initiate"
    st.mkdir()
    (st / ("%s.next" % CHAT)).write_text(str(NOW - 600), encoding="utf-8")
    (st / ("%s.sent.json" % CHAT)).write_text(json.dumps({"last_sent_at": NOW - 5 * 3600}), encoding="utf-8")
    (st / ("%s.early.json" % CHAT)).write_text(json.dumps({"count": 0, "last_skip_at": None}), encoding="utf-8")
    return st


def _state_bytes(st):
    """三个状态文件（下次机会时刻、上次投递记录、提前开口记录）的原始字节"""
    names = ("%s.next" % CHAT, "%s.sent.json" % CHAT, "%s.early.json" % CHAT)
    return {n: ((st / n).read_bytes() if (st / n).exists() else None) for n in names}


def _run_si(base, *extra):
    """命令行跑一次 self_initiate.py bot5 42 [extra]；返回 (进程结果, 本次新增的 life-context 调用数)"""
    rec = base / "life-calls.jsonl"
    before = len(rec.read_text(encoding="utf-8").splitlines()) if rec.exists() else 0
    env = {k: v for k, v in os.environ.items()
           if "proxy" not in k.lower() and not k.startswith(("SELF_INITIATE", "DSH_BOT_LEDGER"))}
    env.update(HOME=str(base / "home"), USERPROFILE=str(base / "home"), DSH_BOT_HOME=str(base),
               HUB_CONFIGS_DIR=str(base / "configs"), HUB_CONFIGS_DSH_DIR=str(base / "configs"),
               SELF_INITIATE_NOW=str(NOW), PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
               T_LIFE_REC=str(rec))
    p = subprocess.run([sys.executable, "-c", BOOT, str(SCRIPT), BOT, CHAT, *extra], cwd=str(REPO), env=env,
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=60)
    after = len(rec.read_text(encoding="utf-8").splitlines()) if rec.exists() else 0
    return p, after - before


def test_24_机会已到_线路down_stderr一行skip_退出码0_三个状态文件不变_不调life_context_不投递(tmp_path, fake_gateway):
    fake_gateway.health = (200, _health_body("down"))
    st = _si_world(tmp_path, fake_gateway)
    before = _state_bytes(st)
    p, life_calls = _run_si(tmp_path)
    assert p.returncode == 0, "退出码应为 0，实际 %s；stderr：%s" % (p.returncode, p.stderr[-800:])
    assert SKIP_LINE in p.stderr.splitlines(), "标准错误应有一整行 %r；实际 stderr：%r" % (SKIP_LINE, p.stderr[-800:])
    assert life_calls == 0, "线路不通时不该调 life-context，实际调了 %d 次" % life_calls
    n_inject = len(fake_gateway.injects())
    assert n_inject == 0, "线路不通时不该投递，实际投了 %d 次" % n_inject
    assert _state_bytes(st) == before, "三个状态文件应一个字节都不变"


def test_24_down跳过之后线路恢复up_下一次运行照常调life_context并投递(tmp_path, fake_gateway):
    fake_gateway.health = (200, _health_body("down"))
    _si_world(tmp_path, fake_gateway)
    first, _ = _run_si(tmp_path)
    assert first.returncode == 0, "搭场景：断线那次应正常退出；stderr：%s" % first.stderr[-800:]
    fake_gateway.health = (200, _health_body("up"))
    injects_before = len(fake_gateway.injects())
    p, life_calls = _run_si(tmp_path)
    assert p.returncode == 0, p.stderr[-800:]
    assert SKIP_LINE not in p.stderr, "线路恢复后不该再打 skip 行"
    assert life_calls == 1, "恢复后下一次运行应照常调 life-context 1 次，实际 %d 次" % life_calls
    n_inject = len(fake_gateway.injects()) - injects_before
    assert n_inject == 1, "life-context 回 TEXT 应投递 1 次，实际 %d 次" % n_inject


def test_24_线路unknown_旧网关健康接口没有link字段_照旧调life_context并投递(tmp_path, fake_gateway):
    fake_gateway.health = (200, _health_body(None))
    _si_world(tmp_path, fake_gateway)
    p, life_calls = _run_si(tmp_path)
    assert p.returncode == 0, p.stderr[-800:]
    assert SKIP_LINE not in p.stderr, "线路 unknown 时不该跳过"
    assert life_calls == 1, "线路 unknown 时应照旧调 life-context 1 次，实际 %d 次" % life_calls
    n_inject = len(fake_gateway.injects())
    assert n_inject == 1, "应照旧投递 1 次，实际 %d 次" % n_inject


def test_24_带skip_judge_线路down也不查线路_不调life_context_照常投递(tmp_path, fake_gateway):
    fake_gateway.health = (200, _health_body("down"))
    _si_world(tmp_path, fake_gateway)
    p, life_calls = _run_si(tmp_path, "--skip-judge")
    assert p.returncode == 0, p.stderr[-800:]
    assert SKIP_LINE not in p.stderr, "--skip-judge 不查线路，不该打断线 skip 行"
    assert life_calls == 0, "--skip-judge 不问 life-context，实际调了 %d 次" % life_calls
    n_inject = len(fake_gateway.injects())
    assert n_inject == 1, "--skip-judge 应直接投递 1 次，实际 %d 次" % n_inject
    n_health = len(fake_gateway.health_queries())
    assert n_health == 0, "--skip-judge 不该查线路（/v1/health），实际查了 %d 次" % n_health
