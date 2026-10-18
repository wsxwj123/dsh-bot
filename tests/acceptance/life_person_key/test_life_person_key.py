# -*- coding: utf-8 -*-
"""life-person-key 验收测试（黑盒）：同一只 bot 有两个名字，写共用库、读共用库一律按 life 名。

依据：接口约定的行为清单和错误契约。只通过命令行入口（子进程）调用，不 import 产品内部模块，
不 mock 内部层，不断言内部状态；只用 sqlite3 直接读写临时共用库来预置数据、查结果。

名字约定（全是占位）：
  X=botx   配置编号（命令行传进去、用来找配置和账本）
  Y=luna   life 名（共用库里 bot_id 存的名字）
  zed      对照 Z1：编号与 life 名相同
  plain    对照 Z2：没写 life
  P        life 名占位 / 另一只对照 bot

安全约束（每个子进程都满足，World._check_isolation 逐次断言）：
  - 断网：PYTHONPATH 带进 stub/sitecustomize.py，拒绝一切解析与 AF_INET/AF_INET6 连接（含 127.0.0.1）；
    环境里剥掉所有名字含 proxy 的变量（不分大小写）；标记文件里不许有被放行的连接。
  - BOTLIFE_STATE_DB 一律指到 pytest 临时目录，并断言在临时目录下；审计钩子记下的、路径含 state.db
    的打开记录也必须都在临时目录里（漏设会写进仓库根的 state.db，那是真实的共用库）。
  - 假 LLM（fake-claude，sh 脚本）代替真模型；Windows 上整个文件跳过。
  - 局限：桩只管 Python 子进程；产品若再起非 Python 的联网程序，这里拦不住（本批用例里没有这种路径）。

运行：在仓库根目录  python3 -m pytest tests/acceptance/life_person_key -q
"""
import datetime
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="假 LLM 是 sh 脚本，Windows 上整个文件跳过")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]                      # tests/acceptance/life_person_key -> 仓库根
STUB_SRC = HERE / "stub"
FAKE_CLAUDE = HERE / "fake-claude"

X, Y = "botx", "luna"                       # 配置编号 / life 名
Z1, Z2 = "zed", "plain"                     # 对照：编号与 life 名相同 / 没写 life
HOBBY = "测试爱好"
SPEAK = {"CLAUDEBOTLIFE_BYPASS_SILENCE": "1"}   # 发言路径：放行预筛（假 LLM 默认判 dm）
BOT_TABLES = ("heartbeat_log", "event_dice_log", "ongoing_events", "hobby_log",
              "current_obsessions", "daily_wildcards", "bot_profiles")

_SELFCHECK = r'''
import json, os, socket, sqlite3, sys, urllib.request
res = {"stub": "sitecustomize" in sys.modules,
       "proxy_vars": sorted(k for k in os.environ if "proxy" in k.lower()),
       "state_db_env": os.environ.get("BOTLIFE_STATE_DB")}
import rng
res["rng_threshold"] = (rng.chance(0.99) is False and rng.chance(1.0) is True
                        and rng.uniform(3, 9) == 3 and rng.choice(["a", "b"]) == "a")
def t(name, fn):
    try:
        fn()
        res[name] = "NOT BLOCKED"
    except Exception as e:
        res[name] = "blocked" if "test-offline" in str(e) else "other:%s:%s" % (type(e).__name__, str(e)[:80])
t("dns_localhost", lambda: socket.getaddrinfo("localhost", 80))
t("dns_domain", lambda: socket.getaddrinfo("example.com", 80))
t("dns_gethostbyname", lambda: socket.gethostbyname("localhost"))
t("tcp_127_create_connection", lambda: socket.create_connection(("127.0.0.1", 9), timeout=2))
t("tcp_127_connect_ex", lambda: socket.socket().connect_ex(("127.0.0.1", 9)))
t("udp_127_sendto", lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("127.0.0.1", 9)))
t("urllib_127_noproxy", lambda: urllib.request.build_opener(urllib.request.ProxyHandler({})).open("http://127.0.0.1:9/", timeout=2))
try:
    import requests
    def rq():
        s = requests.Session(); s.trust_env = False; s.get("http://127.0.0.1:9/", timeout=2)
    t("requests_127_noenv", rq)
except ImportError:
    res["requests_127_noenv"] = "n/a"
try:
    s6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM) if socket.has_ipv6 else None
except OSError:
    s6 = None
if s6 is None:
    res["tcp6_loopback"] = "n/a"
else:
    t("tcp6_loopback", lambda: s6.connect(("::1", 9)))
sqlite3.connect(os.path.join(os.path.dirname(os.environ["BOTLIFE_STATE_DB"]), "selfcheck-state.db")).close()
print(json.dumps(res, ensure_ascii=False))
'''

# 绕过 Python 层的替换、直接调底层 C 方法：审计钩子必须兜底拦下，并在标记文件里记一条 allowed
_BYPASS = r'''
import _socket, json
s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
try:
    _socket.socket.connect(s, ("127.0.0.1", 9))
    out = "NOT BLOCKED"
except OSError as e:
    out = "blocked" if "test-offline" in str(e) else "other:%s" % e
print(json.dumps({"bypass": out}))
'''


class Run:
    """一次子进程运行的结果。marks / opens 只含这一次运行新增的行。"""

    def __init__(self, proc, marks, opens):
        self.rc = proc.returncode
        self.out = proc.stdout
        self.err = proc.stderr
        self.marks = marks
        self.opens = opens

    @property
    def blocked(self):
        return [m for m in self.marks if m.startswith("blocked ")]

    def json(self):
        lines = [ln for ln in self.out.splitlines() if ln.strip()]
        assert lines, "stdout 为空；stderr 末尾：%r" % self.err[-600:]
        return json.loads(lines[-1])

    def err_lines(self):
        return [ln for ln in self.err.splitlines() if ln.strip()]


def _read_from(path, offset):
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read().decode("utf-8", "replace").splitlines()
    except FileNotFoundError:
        return []


def _size(path):
    try:
        return os.path.getsize(path)
    except FileNotFoundError:
        return 0


class World:
    """一个独立的临时世界：配置根、life 配置、账本、共用库、断网桩、假 LLM 都在 base 之下。"""

    def __init__(self, base):
        self.base = Path(base).resolve()
        self.configs = self.base / "configs"
        self.old = self.base / "old"
        self.work = self.base / "work"
        self.db_path = self.base / "state.db"
        self.mark = self.base / "stub.mark"
        self.open_log = self.base / "opens.log"
        self.fake_log = self.base / "fake.log"
        for d in (self.configs, self.old, self.work, self.base / "home", self.base / "tmp"):
            d.mkdir(parents=True, exist_ok=True)
        shutil.copytree(STUB_SRC, self.base / "stub", ignore=shutil.ignore_patterns("__pycache__"))
        self._offline_ok = False
        self._seq = 0

    # ------------------------------------------------------------ 子进程
    def env(self, extra=None):
        env = {k: v for k, v in os.environ.items()
               if "proxy" not in k.lower()
               and not k.startswith(("CLAUDE", "DSH_BOT", "HUB_", "BOTLIFE", "TEST_", "FAKE_"))}
        env.update(
            HOME=str(self.base / "home"), TMPDIR=str(self.base / "tmp"),
            BOTLIFE_STATE_DB=str(self.db_path),
            HUB_CONFIGS_DIR=str(self.configs), DSH_BOT_HOME=str(self.base),
            CLAUDEBOTLIFE_FORCE_CLAUDE="1", CLAUDE_BIN=str(FAKE_CLAUDE),
            PYTHONPATH=os.pathsep.join([str(self.base / "stub")] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])),
            PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
            TEST_REPO=str(ROOT), TEST_RNG="threshold", TEST_STUB_MARK=str(self.mark),
            TEST_OPEN_LOG=str(self.open_log), TEST_WATCH="state.db", FAKE_LOG=str(self.fake_log),
        )
        env.update(extra or {})
        # 硬约束：没有任何代理变量；共用库一定在临时目录下
        assert not [k for k in env if "proxy" in k.lower()], "子进程环境里还有代理变量"
        db = Path(env["BOTLIFE_STATE_DB"]).resolve()
        assert self.base in db.parents, "BOTLIFE_STATE_DB 不在临时目录下：%s" % db
        return env

    def _exec(self, argv, extra_env=None, cwd=None, timeout=120, check_isolation=True):
        m0, o0 = _size(self.mark), _size(self.open_log)
        proc = subprocess.run(argv, cwd=str(cwd or self.work), env=self.env(extra_env),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
        run = Run(proc, _read_from(self.mark, m0), _read_from(self.open_log, o0))
        if check_isolation:
            self._check_isolation(run)
        return run

    def _check_isolation(self, run):
        assert any(m.startswith("loaded ") for m in run.marks), \
            "断网桩没有加载（标记文件里没有 loaded 行），这次运行不能算断网"
        passed = [m for m in run.marks if not m.startswith(("loaded ", "blocked "))]
        assert not passed, "标记文件里有被放行的网络连接：%s" % passed
        outside = [ln for ln in run.opens if str(self.base) not in ln]
        assert not outside, "打开了临时目录之外的 state.db（真实共用库）：%s" % outside

    def selfcheck(self):
        """断网与随机数桩自检；返回 (run, 结果字典)，不合格直接断言失败。"""
        run = self._exec([sys.executable, "-c", _SELFCHECK])
        assert run.rc == 0, "自检脚本退出码 %s，stderr：%s" % (run.rc, run.err[-600:])
        res = run.json()
        assert res["stub"] is True, "sitecustomize 没有被加载"
        assert res["proxy_vars"] == [], "子进程里还有代理变量：%s" % res["proxy_vars"]
        assert res["rng_threshold"] is True, "rng 没有被替换成阈值模式"
        assert Path(res["state_db_env"]) == self.db_path
        channels = {k: v for k, v in res.items()
                    if k not in ("stub", "proxy_vars", "state_db_env", "rng_threshold")}
        leaked = {k: v for k, v in channels.items() if v not in ("blocked", "n/a")}
        assert not leaked, "这些通道没被拦住：%s" % leaked
        self._offline_ok = True
        return run, res

    def _ensure_offline(self):
        if not self._offline_ok:
            self.selfcheck()

    def run(self, script, *args, env=None, timeout=120):
        """跑仓库里的脚本（路径相对仓库根），cwd 是临时目录。每个世界第一次跑之前先确认断网生效。"""
        self._ensure_offline()
        return self._exec([sys.executable, str(ROOT / script), *args], extra_env=env, timeout=timeout)

    def life(self, code, *, speak=True, env=None):
        e = dict(SPEAK) if speak else {}
        e.update(env or {})
        return self.run("life-context.py", code, "42", env=e)

    def init_db(self):
        self._ensure_offline()
        run = self._exec([sys.executable, "-c", "import db; db.init()"], cwd=ROOT)
        assert run.rc == 0, "建库失败 rc=%s：%s" % (run.rc, run.err[-600:])
        assert self.db_path.exists(), "建库后临时共用库不存在"

    # ------------------------------------------------------------ 配置与账本
    def add_bot(self, code, life=None, *, life_id=None, persona="测试人设", hobby=True):
        chan = self.base / "bots" / code / "channel"
        state = self.base / "bots" / code / "state"
        chan.mkdir(parents=True, exist_ok=True)
        state.mkdir(parents=True, exist_ok=True)
        schema = (ROOT / "gateway" / "src" / "ledger-schema.sql").read_text(encoding="utf-8")
        with closing(sqlite3.connect(str(state / "ledger.sqlite"))) as c:
            c.executescript(schema)
            c.commit()
        lines = ["id: %s" % code, "chat_id: '42'", "bot_channel_path: %s" % json.dumps(str(chan)),
                 "dispatcher_port: 0", "moments_daily_limit: 1"]
        if hobby:
            lines += ["personal_hobbies:", "  - name: %s" % HOBBY, "    probability_per_roll: 1.0"]
        if life:
            life_file = self.old / ("%s.yml" % life)
            life_file.write_text("id: %s\npersona_summary: %s\n" % (life, persona), encoding="utf-8")
            lines.append("life_config: %s" % json.dumps(str(life_file)))
        else:
            lines.append("persona_summary: %s" % persona)
        if life_id is not None:
            lines.append("life_id: %s" % json.dumps(life_id))
        (self.configs / ("%s.yml" % code)).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _ledger(self, code, sql, params):
        with closing(sqlite3.connect(str(self.base / "bots" / code / "state" / "ledger.sqlite"))) as c:
            c.execute(sql, params)
            c.commit()

    def put_inbound(self, code, text, ago_s):
        """账本里放一条对方的消息，ago_s 秒前。"""
        self._seq += 1
        ms = int((time.time() - ago_s) * 1000)
        self._ledger(code, "INSERT INTO inbound (ukey, chat_id, kind, text, ts, received_at, state) "
                           "VALUES (?, '42', 'user', ?, ?, ?, 'done')", ("in-%d-%d" % (ms, self._seq), text, ms, ms))

    def put_outbound(self, code, text, ago_s):
        """账本里放一条 bot 的回复，ago_s 秒前。"""
        ms = int((time.time() - ago_s) * 1000)
        self._ledger(code, "INSERT INTO outbound (chat_id, turn_id, part, kind, text, state, created_at, sent_at) "
                           "VALUES ('42', 1, 1, 'text', ?, 'sent', ?, ?)", (text, ms, ms))

    # ------------------------------------------------------------ 共用库
    def q(self, sql, params=()):
        with closing(sqlite3.connect(str(self.db_path), timeout=30)) as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    def _write(self, sql, params=()):
        with closing(sqlite3.connect(str(self.db_path), timeout=30)) as c:
            c.execute(sql, params)
            c.commit()

    def insert(self, table, **vals):
        """往共用库预置一行；没给的"非空且无默认值"的列用占位值补上。列名对不上直接断言失败。"""
        cols = {r["name"]: r for r in self.q("PRAGMA table_info(%s)" % table)}
        assert cols, "共用库里没有表 %s" % table
        unknown = sorted(set(vals) - set(cols))
        assert not unknown, "%s 里没有列 %s（与接口约定对不上），实际列：%s" % (table, unknown, sorted(cols))
        row = dict(vals)
        for name, info in cols.items():
            if name in row:
                continue
            typ = (info["type"] or "").upper()
            if info["pk"] and "INT" in typ:
                continue
            if info["notnull"] and info["dflt_value"] is None:
                row[name] = 0 if "INT" in typ else (0.0 if any(k in typ for k in ("REAL", "FLOA", "DOUB")) else "")
        names = list(row)
        self._write("INSERT INTO %s (%s) VALUES (%s)" % (table, ", ".join(names), ", ".join("?" * len(names))),
                    tuple(row[n] for n in names))

    def rows(self, table, bot=None):
        if bot is None:
            return self.q("SELECT * FROM %s ORDER BY rowid" % table)
        return self.q("SELECT * FROM %s WHERE bot_id = ? ORDER BY rowid" % table, (bot,))

    def counts(self):
        names = [r["name"] for r in self.q(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {n: self.q("SELECT COUNT(*) AS n FROM %s" % n)[0]["n"] for n in names}

    def fake_log_lines(self):
        try:
            return self.fake_log.read_text(encoding="utf-8").split()
        except FileNotFoundError:
            return []


def assert_nothing_under_code(w, code, tables=BOT_TABLES):
    """编号名下一行都不该有（改动以后写库一律按 life 名）。"""
    found = {t: n for t in tables
             for n in [w.q("SELECT COUNT(*) AS n FROM %s WHERE bot_id = ?" % t, (code,))[0]["n"]] if n}
    assert not found, "共用库里有记在编号 %s 名下的行（应为 0）：%s" % (code, found)


def now_s():
    return int(time.time())


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


def test_断网自检_解析与连接全被拒_放行的尝试会被记下并拦住(w):
    """前提自检：不过的话下面所有用例的"断网"都不成立。"""
    run, res = w.selfcheck()
    assert len(run.blocked) >= 7, "标记文件里应记下被拦的尝试，实际：%s" % run.blocked
    assert any(ln.startswith("sqlite3.connect ") and ln.endswith("selfcheck-state.db") for ln in run.opens), \
        "审计钩子没记下对 state.db 类文件的打开：%s" % run.opens
    # 绕过 Python 层、直接调底层方法：审计钩子兜底拦下，并在标记文件里留下 allowed 记录
    bypass = w._exec([sys.executable, "-c", _BYPASS], check_isolation=False)
    assert bypass.rc == 0, bypass.err[-400:]
    assert bypass.json() == {"bypass": "blocked"}
    assert any(m.startswith("allowed socket.connect ") for m in bypass.marks), bypass.marks
    # 反向：标记文件里真有放行记录时，每次运行后的隔离断言必须报错（保证"无放行"这条断言有牙齿）
    with pytest.raises(AssertionError, match="被放行"):
        w._check_isolation(bypass)


EVENT = "测试事件"
OBSESSION = "测试痴迷"
CONT = "⚠️【对话延续模式】"
FOLLOWED = "上次你已经主动跟进过一次"
SKIP_REASON_QUIET = "非静默期(距上次1min<30min)"


def tick_twice_with_event(w, bot_arg, owner):
    """第 13 条的两次法：先 tick 一次（记下上次 tick 时间），再在 owner 名下插事件（started_at=当前秒+5），再 tick 一次。"""
    first = w.run("jiwen/tick.py", "--bot", bot_arg)
    assert first.rc == 0, first.err
    w.insert("ongoing_events", bot_id=owner, event_name=EVENT, mood_delta=-0.4,
             effect="测试效果", started_at=now_s() + 5, expires_at=now_s() + 3600)
    second = w.run("jiwen/tick.py", "--bot", bot_arg)
    assert second.rc == 0, second.err
    return second


def last_line(text):
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert lines, "输出为空"
    return lines[-1]


# ====================================================================== 行为清单 1-3：心跳

def test_01_情境脚本_发言路径_心跳记在life名下_编号名下没有行(w):
    w.add_bot(X, Y)
    w.init_db()
    r = w.life(X)
    assert r.rc == 0, r.err
    assert r.json()["action"] != "SKIP"
    beats = [(h["bot_id"], h["judge_speak"], h["skipped"]) for h in w.rows("heartbeat_log")]
    assert beats == [(Y, 1, 0)]
    assert_nothing_under_code(w, X)


def test_02_情境脚本_判skip_输出SKIP_心跳按life名记为跳过(w):
    w.add_bot(X, Y)
    w.init_db()
    r = w.life(X, env={"FAKE_ACTION": "skip"})
    assert r.rc == 0, r.err
    assert r.json() == {"action": "SKIP", "reason": "action=skip:测试"}
    beats = [(h["bot_id"], h["judge_speak"], h["skipped"], h["skip_reason"]) for h in w.rows("heartbeat_log")]
    assert beats == [(Y, 0, 1, "action=skip:测试")]
    assert_nothing_under_code(w, X)


def test_03_情境脚本_非静默期预筛跳过_心跳记在life名下_没问过判断模型(w):
    w.add_bot(X, Y)
    w.init_db()
    w.put_inbound(X, "在吗", 60)                 # 账本按编号找：X 的账本里有一条 1 分钟前的对方消息
    r = w.life(X, speak=False)                   # 不放行预筛
    assert r.rc == 0, r.err
    assert r.json() == {"action": "SKIP", "reason": SKIP_REASON_QUIET}
    beats = w.rows("heartbeat_log")
    assert [(h["bot_id"], h["skipped"], h["skip_reason"]) for h in beats] == [(Y, 1, SKIP_REASON_QUIET)]
    assert not beats[0]["judge_reason"], beats[0]["judge_reason"]
    assert "judge" not in w.fake_log_lines(), "预筛已经跳过，不该再问判断模型"
    assert_nothing_under_code(w, X)


# ====================================================================== 行为清单 4-5：读心跳

def test_04_情境脚本_赞过的圈按life名下上次心跳算_不含更早的赞和bot自赞(w):
    w.add_bot(X, Y)
    w.init_db()
    t = now_s() - 3600                                   # Y 名下上次心跳 T；X 名下没有
    w.insert("heartbeat_log", bot_id=Y, ts=t, judge_speak=1, skipped=0)
    old, new, own = "旧圈文案AAA", "新圈文案BBB", "自赞圈文案CCC"
    for text, made in ((old, t - 7200), (new, t + 300), (own, t + 300)):
        w.insert("moments", bot_id=Y, ts=made, text=text)
    mid = {text: w.q("SELECT id FROM moments WHERE text = ?", (text,))[0]["id"] for text in (old, new, own)}
    w.insert("moment_likes", moment_id=mid[old], liker="哥哥", ts=t - 1800)     # T 之前被赞
    w.insert("moment_likes", moment_id=mid[new], liker="哥哥", ts=t + 600)      # T 之后被赞
    w.insert("moment_likes", moment_id=mid[own], liker=Y, ts=t + 600)           # bot 自己赞自己，T 之后
    r = w.life(X)
    assert r.rc == 0, r.err
    text = r.json()["text"]
    assert "赞了：「%s」" % new in text
    assert old not in text, "T 之前被赞的圈不该出现"
    assert own not in text, "bot 自己赞的那条不该出现"


def test_05_情境脚本_对话延续与已跟进提示_只看life名下发言过的心跳(w):
    w.add_bot(X, Y)
    w.init_db()
    w.put_inbound(X, "在吗", 3 * 3600 + 60)
    w.put_outbound(X, "在的", 3 * 3600)
    w.insert("heartbeat_log", bot_id=X, ts=now_s() - 1800, judge_speak=1, skipped=0)   # 发言过的心跳只在编号名下
    a = w.life(X)
    assert a.rc == 0, a.err
    ta = a.json()["text"]
    assert CONT in ta, "(a) 应是对话延续模式"
    assert FOLLOWED not in ta, "(a) 编号名下的心跳不该被读到"
    b = w.life(X)                                                    # 此时 Y 名下已有发言过的心跳
    assert b.rc == 0, b.err
    tb = b.json()["text"]
    assert FOLLOWED in tb, "(b) 应提示已经主动跟进过一次"
    assert CONT not in tb, "(b) 不该再是对话延续模式"
    assert len(w.rows("heartbeat_log", X)) == 1, "编号名下不该新增心跳"
    assert_nothing_under_code(w, X, [t for t in BOT_TABLES if t != "heartbeat_log"])


# ====================================================================== 行为清单 6-7：签名、掷骰

def test_06_情境脚本_签名按life名读写_没有资料行时写入_一小时内不覆盖(tmp_path):
    a = World(tmp_path / "a")
    a.add_bot(X, Y)
    a.init_db()
    ra = a.life(X)
    assert ra.rc == 0, ra.err
    assert [(p["bot_id"], p["signature"]) for p in a.rows("bot_profiles")] == [(Y, "测试签名")], "(a) 没有资料行"
    assert_nothing_under_code(a, X)

    b = World(tmp_path / "b")
    b.add_bot(X, Y)
    b.init_db()
    stamp = now_s() - 3600
    b.insert("bot_profiles", bot_id=Y, signature="旧签名", sig_updated_at=stamp, updated_at=stamp)
    rb = b.life(X)
    assert rb.rc == 0, rb.err
    profiles = [(p["bot_id"], p["signature"], p["sig_updated_at"]) for p in b.rows("bot_profiles")]
    assert profiles == [(Y, "旧签名", stamp)], "(b) 一小时内不该改签名"
    assert_nothing_under_code(b, X)


def test_07_情境脚本_掷骰记录按life名读写_冷却内不重掷(tmp_path):
    a = World(tmp_path / "a")
    a.add_bot(X, Y)
    a.init_db()
    ra = a.life(X)
    assert ra.rc == 0, ra.err
    assert [d["bot_id"] for d in a.rows("event_dice_log")] == [Y], "(a) 应在 Y 名下出现一行"
    assert_nothing_under_code(a, X)

    b = World(tmp_path / "b")
    b.add_bot(X, Y)
    b.init_db()
    stamp = now_s() - 3600
    b.insert("event_dice_log", bot_id=Y, last_roll_at=stamp)
    rb = b.life(X)
    assert rb.rc == 0, rb.err
    assert [(d["bot_id"], d["last_roll_at"]) for d in b.rows("event_dice_log")] == [(Y, stamp)], "(b) Y 行应保持不变"
    assert b.rows("ongoing_events") == [], "(b) 冷却内不该新增进行中事件"
    assert_nothing_under_code(b, X)


# ====================================================================== 行为清单 8-12：事件、爱好、痴迷、每日卡

def test_08_情境脚本_进行中事件按life名读取_发言后记下提起时间(w):
    w.add_bot(X, Y)
    w.init_db()
    t = now_s()
    w.insert("ongoing_events", bot_id=Y, event_name=EVENT, mood_delta=-0.2, effect="测试效果",
             started_at=t - 600, expires_at=t + 3600)
    r = w.life(X)
    assert r.rc == 0, r.err
    assert r.json()["metadata"]["sporadic"] == EVENT
    beats = w.rows("heartbeat_log")
    assert [(h["bot_id"], h["sporadic"]) for h in beats] == [(Y, EVENT)]
    event = w.rows("ongoing_events", Y)
    assert [e["event_name"] for e in event] == [EVENT]
    assert event[0]["last_mentioned_at"] is not None, "提起过的事件应记下 last_mentioned_at"
    assert w.rows("event_dice_log") == [], "有进行中事件时不该掷骰"
    assert_nothing_under_code(w, X)


def test_09_情境脚本_爱好记录按life名读写_冷却内不重复记(tmp_path):
    a = World(tmp_path / "a")
    a.add_bot(X, Y)
    a.init_db()
    ra = a.life(X)
    assert ra.rc == 0, ra.err
    assert [(h["bot_id"], h["hobby_name"]) for h in a.rows("hobby_log")] == [(Y, HOBBY)], "(a) 应记在 Y 名下"
    assert ra.json()["metadata"]["hobby"] == HOBBY
    assert_nothing_under_code(a, X)

    b = World(tmp_path / "b")
    b.add_bot(X, Y)
    b.init_db()
    stamp = now_s() - 3600
    b.insert("hobby_log", bot_id=Y, hobby_name=HOBBY, ts=stamp)
    rb = b.life(X)
    assert rb.rc == 0, rb.err
    assert [(h["bot_id"], h["hobby_name"], h["ts"]) for h in b.rows("hobby_log")] == [(Y, HOBBY, stamp)], "(b) 不该新增"
    assert_nothing_under_code(b, X)


def test_10_情境脚本_骰子冷却中_有未过期痴迷时爱好记在life名下(w):
    w.add_bot(X, Y)
    w.init_db()
    t = now_s()
    w.insert("event_dice_log", bot_id=Y, last_roll_at=t - 3600)           # 骰子在冷却中
    w.insert("current_obsessions", bot_id=Y, name=OBSESSION, effect="测试效果", theme="剧",
             started_at=t - 3600, expires_at=t + 86400)
    r = w.life(X, env={"TEST_RNG": "always"})
    assert r.rc == 0, r.err
    assert [(h["bot_id"], h["hobby_name"]) for h in w.rows("hobby_log")] == [(Y, OBSESSION)]
    assert_nothing_under_code(w, X)


def test_11_情境脚本_每日卡按life名读取_用掉后标记used且心跳记下(w):
    if datetime.datetime.now().time() >= datetime.time(23, 58):
        pytest.skip("23:58 以后可能跨天，每日卡的\"今天\"不稳定")
    w.add_bot(X, Y)
    w.init_db()
    today = datetime.date.today().isoformat()
    w.insert("daily_wildcards", bot_id=Y, date=today, card_id=1, card="测试卡", emotion="平静", used=0)
    r = w.life(X, env={"TEST_RNG": "always"})
    assert r.rc == 0, r.err
    cards = w.q("SELECT bot_id, date, card_id, card, used FROM daily_wildcards WHERE bot_id = ? AND date = ? AND card_id = 1",
                (Y, today))
    assert [c["used"] for c in cards] == [1], "Y 名下今天那张卡应被标记为已用"
    beats = w.rows("heartbeat_log")
    assert [h["bot_id"] for h in beats] == [Y]
    assert beats[0]["wildcard_used"] == "测试卡"
    assert_nothing_under_code(w, X)


def test_12_先发圈再跑情境脚本_爱好冷却共用life名下的记录(w):
    w.add_bot(X, Y)
    w.init_db()
    p = w.run("scripts/bot_post_moment.py", Y)               # 网关的调法：传 life 名
    assert p.rc == 3, p.err                                  # 文案为空，没发出
    assert [(h["bot_id"], h["hobby_name"]) for h in w.rows("hobby_log")] == [(Y, HOBBY)]
    r = w.life(X)
    assert r.rc == 0, r.err
    assert [(h["bot_id"], h["hobby_name"]) for h in w.rows("hobby_log")] == [(Y, HOBBY)], "爱好冷却中，不该新增"
    assert_nothing_under_code(w, X)


# ====================================================================== 行为清单 13-15：积温、对照、空白 life_id

def test_13_积温_事件按life名读取_只插在编号名下的事件看不到(tmp_path):
    a = World(tmp_path / "a")
    a.add_bot(X, Y)
    a.init_db()
    second_a = tick_twice_with_event(a, X, Y)
    assert "事件[%s] mood_delta=-0.40" % EVENT in second_a.err, "(a) 插在 Y 名下的事件应被第二次 tick 读到"

    b = World(tmp_path / "b")
    b.add_bot(X, Y)
    b.init_db()
    second_b = tick_twice_with_event(b, X, X)
    assert "事件[" not in second_b.err, "(b) 只插在编号名下的事件不该被读到"


def test_14_对照_编号与life名相同或没写life_行为不变(w):
    w.add_bot(Z1, Z1)
    w.add_bot(Z2)
    w.init_db()
    for code in (Z1, Z2):
        r = w.life(code)
        assert r.rc == 0, (code, r.err)
    for table in ("heartbeat_log", "event_dice_log", "hobby_log", "bot_profiles"):
        assert sorted(row["bot_id"] for row in w.rows(table)) == sorted([Z1, Z2]), table
    for code in (Z1, Z2):
        second = tick_twice_with_event(w, code, code)
        assert "事件[%s] mood_delta=-0.40" % EVENT in second.err, code
        assert "ERROR" not in second.err, (code, second.err)


def test_15_life_id只有空白_退回用life配置的文件名当life名(w):
    w.add_bot(X, "P", life_id="  ")
    w.init_db()
    r = w.life(X)
    assert r.rc == 0, r.err
    for table in ("heartbeat_log", "event_dice_log", "hobby_log", "bot_profiles"):
        assert [row["bot_id"] for row in w.rows(table)] == ["P"], table
    for name in (X, "  ", ""):
        assert_nothing_under_code(w, name)


# ====================================================================== 行为清单 16-19：作息查询、账本、强制发圈、刷新痴迷

def test_16_作息查询_不读不写共用库_输出字段齐全(w):
    w.add_bot(X, Y)
    w.init_db()
    t = now_s() - 60
    w.insert("heartbeat_log", bot_id=Y, ts=t, judge_speak=1, skipped=0)
    w.insert("event_dice_log", bot_id=Y, last_roll_at=t)
    w.insert("hobby_log", bot_id=Y, hobby_name=HOBBY, ts=t)
    w.insert("bot_profiles", bot_id=Y, signature="旧签名", sig_updated_at=t)
    before = {name: w.rows(name) for name in w.counts()}
    r = w.run("hang_situation.py", X, "--plan")
    assert r.rc == 0, r.err
    lines = [ln for ln in r.out.splitlines() if ln.strip()]
    assert len(lines) == 1, "stdout 应只有一行 JSON：%r" % r.out
    out = json.loads(lines[0])
    assert {"name", "state", "interruptible", "term_label", "wakes", "free_at"} <= set(out), sorted(out)
    assert isinstance(out["wakes"], list)
    assert r.opens == [], "作息查询不该打开共用库：%s" % r.opens
    assert {name: w.rows(name) for name in before} == before, "共用库里各表内容不该有任何变化"


def test_17_情境脚本_账本仍按编号读_对方3小时前的消息算出约3小时(w):
    w.add_bot(X, Y)
    w.init_db()
    w.put_inbound(X, "在吗", 3 * 3600)
    r = w.life(X, speak=False)
    assert r.rc == 0, r.err
    json.loads(r.out)                                            # stdout 是合法 JSON
    hours = re.findall(r"hours_since_user=([0-9.]+)", r.err)
    assert hours, "stderr 里没有 hours_since_user=：%s" % r.err[-500:]
    assert 2.9 <= float(hours[0]) <= 3.1, hours


def test_18_手动强制发圈_文案为空_掷骰和爱好记在life名下(w):
    w.add_bot(X, Y)
    w.init_db()
    r = w.run("scripts/force_moment.py", X)
    assert r.rc == 0, r.err
    assert last_line(r.out).startswith("⚠️ 未生成"), last_line(r.out)
    assert [d["bot_id"] for d in w.rows("event_dice_log")] == [Y]
    assert [(h["bot_id"], h["hobby_name"]) for h in w.rows("hobby_log")] == [(Y, HOBBY)]
    assert_nothing_under_code(w, X)


def test_19_刷新痴迷_按life名写入_输出前缀仍是编号_失败那只零行(w):
    w.add_bot(X, Y)
    w.add_bot(Z1, Z1)
    w.add_bot(Z2)
    w.add_bot("P", persona="FAILME 测试人设")                     # 假 LLM 见到 FAILME 就报错
    w.init_db()
    r = w.run("scripts/refresh_obsessions.py")
    assert r.rc == 0, r.err
    rows = w.rows("current_obsessions")
    assert sorted(o["bot_id"] for o in rows) == sorted([Y, Z1, Z2]), "新增的痴迷应在 Y、Z1、Z2 各自名下"
    assert_nothing_under_code(w, X)
    assert_nothing_under_code(w, "P")                             # 失败的那只 0 行
    lines = [ln.strip() for ln in r.out.splitlines()]
    for code in (X, Z1, Z2):
        assert any(ln.startswith("[%s] +%s" % (code, OBSESSION)) for ln in lines), (code, r.out)
    assert not any(ln.startswith("[%s]" % Y) for ln in lines), "stdout 前缀应是编号，不是 life 名：%s" % r.out
    m = re.fullmatch(r"总计新增 (\d+) 条短期痴迷", last_line(r.out).strip())
    assert m, last_line(r.out)
    assert int(m.group(1)) == len(rows) == 3
    assert any(ln.startswith("[P] obsession 生成失败：") for ln in r.err_lines()), r.err


# ====================================================================== 行为清单 20-24：错误契约

def test_20_错误_情境脚本_配置不存在与参数不足(w):
    w.add_bot(X, Y)
    w.init_db()
    before = w.counts()
    nosuch = w.run("life-context.py", "nosuch", "42")
    assert nosuch.rc == 1
    assert nosuch.err.strip() == "配置加载失败：配置不存在：%s/nosuch.yml" % w.configs
    assert w.counts() == before, "配置不存在时共用库不该有新行"
    usage = "用法: life-context.py <bot_id> <chat_id> [--dry-run]"
    for args in ((), (X,)):
        r = w.run("life-context.py", *args)
        assert r.rc == 1, args
        assert r.err.strip() == usage, args
    assert w.counts() == before, "参数不足时共用库不该有新行"


def test_21_错误_作息查询_配置不存在_退1_不打开共用库(w):
    w.add_bot(X, Y)
    w.init_db()
    r = w.run("hang_situation.py", "nosuch", "--plan")
    assert r.rc == 1
    assert r.err.strip() == "hang_situation: nosuch failed (FileNotFoundError)"
    assert r.opens == [], "不该打开共用库：%s" % r.opens


def test_22_错误_积温_配置不存在只跳过_配置根里有坏yml时其余照跑(tmp_path):
    a = World(tmp_path / "a")
    a.add_bot(X, Y)
    a.init_db()
    before = a.counts()
    ra = a.run("jiwen/tick.py", "--bot", "nosuch")
    assert ra.rc == 0, ra.err
    assert "[jiwen.tick] nosuch: enabled=false，跳过（用 --dry-run 可强跑测试）" in ra.err_lines()
    assert a.counts() == before, "(a) 共用库不该有新行"

    b = World(tmp_path / "b")
    b.add_bot(X, Y)
    b.add_bot(Z1, Z1)
    b.add_bot(Z2)
    b.init_db()
    t = now_s()
    b.insert("ongoing_events", bot_id=Y, event_name=EVENT, mood_delta=-0.4, effect="测试效果",
                  started_at=t - 600, expires_at=t + 3600)
    (b.configs / "P.yml").write_text("bad: [unclosed\n  - : :\n", encoding="utf-8")      # 一份加载不了的 yml
    before_rows = {name: b.rows(name) for name in b.counts()}
    rb = b.run("jiwen/tick.py")
    assert rb.rc == 0, rb.err
    m = re.search(r"bots=\[([^\]]*)\]", rb.err_lines()[0])
    assert m, rb.err_lines()[0]
    assert sorted(re.findall(r"'([^']*)'", m.group(1))) == sorted([X, Z1, Z2]), "(b) 名单里不该有坏 yml：%s" % m.group(0)
    for code in (X, Z1, Z2):
        assert "[jiwen.tick] %s:" % code in rb.err, "(b) %s 应照跑" % code
    assert "ERROR" not in rb.err
    assert {name: b.rows(name) for name in before_rows} == before_rows, "(b) 共用库应只读"


def test_23_错误_手动强制发圈_无参数与配置不存在(w):
    w.add_bot(X, Y)
    w.init_db()
    before = w.counts()
    noarg = w.run("scripts/force_moment.py")
    assert noarg.rc == 1
    assert noarg.err.strip() == "usage: force_moment.py <bot_id> [--inject-hobby]"
    assert w.counts() == before, "无参数时共用库不该有新行"
    nosuch = w.run("scripts/force_moment.py", "nosuch")
    assert nosuch.rc == 1
    assert last_line(nosuch.err) == "FileNotFoundError: 配置不存在：%s/nosuch.yml" % w.configs
    assert w.counts() == before, "配置不存在时共用库不该有新行"


def test_24_手动强制发圈_文案那次LLM失败_退0_已写的掷骰爱好留在life名下(w):
    w.add_bot(X, Y)
    w.init_db()
    r = w.run("scripts/force_moment.py", X, env={"FAKE_FAIL_TEXT": "1"})
    assert r.rc == 0, r.err
    assert any(ln.startswith("[%s] moment 文案生成失败：" % Y) for ln in r.err_lines()), r.err
    assert last_line(r.out).startswith("⚠️ 未生成"), last_line(r.out)
    assert [d["bot_id"] for d in w.rows("event_dice_log")] == [Y]
    assert [(h["bot_id"], h["hobby_name"]) for h in w.rows("hobby_log")] == [(Y, HOBBY)]
    assert_nothing_under_code(w, X)
