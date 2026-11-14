# -*- coding: utf-8 -*-
"""voicecall-dsh 验收的纯工具：契约里的固定文案、摆假 bot、造账本与记忆、假网关、发请求、断言。

依据只有 `.devflow/BRIEF-voicecall-dsh.md` 与 `.devflow/INTERFACE-voicecall-dsh.md`（下称 INTERFACE）。
摆假 bot 的办法借自 tests/acceptance 现成支架（配置根里一份 `<bot_id>.yml`，令牌文件经
DSH_CONSOLE_TOKEN_FILE 指到临时目录，网关口令与端口在 `state/api.key`、`state/api.port`，
`moments.adapter_service.create_app()` 起测试客户端）。
账本表结构只取自 gateway/src/ledger-schema.sql。所有编号、令牌、聊天文字都是编的假值。
"""
import json
import os
import socket
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_SQL = REPO_ROOT / "gateway" / "src" / "ledger-schema.sql"

# ---------------------------------------------------------------- 路径
PREFIX = "/adapter/v1"
META = PREFIX + "/meta"


def dm_tail_url(bot_id):
    return "%s/bots/%s/dm_tail" % (PREFIX, bot_id)


def memory_url(bot_id):
    return "%s/bots/%s/memory" % (PREFIX, bot_id)


def notify_url(bot_id):
    return "%s/bots/%s/notify" % (PREFIX, bot_id)


# ---------------------------------------------------------------- 假值
ADAPTER_TOKEN = "FAKE-ADAPTER-TOKEN-voicecall-do-not-leak-6d2f8a1c"
WRONG_TOKEN = "FAKE-WRONG-TOKEN-voicecall-never-valid-9e0b7c3a"
GW_KEY = "FAKE-GATEWAY-KEY-voicecall-do-not-leak-44aa55bb"
BOT = "testlulu"
OTHER_BOT = "testyao"
OWNER_CHAT = 1009876543            # 主人私聊编号（假）
FRIEND_CHAT = 1009876999           # 别的私聊
GROUP_CHAT = -1001234567890        # 群
PAST = 1600000000

# ---------------------------------------------------------------- INTERFACE 逐字文案
UNAUTHORIZED = {"ok": False, "code": "unauthorized", "message": "令牌缺失或不对"}
NOT_FOUND = "没有这个接口"
METHOD_NOT_ALLOWED = "这个接口不支持这种请求方式"
BAD_BOT_ID = "bot 编号不合规：只能用字母、数字、点、下划线、横线，1 到 64 个字符"
UNKNOWN_BOT = "没有这只 bot"
MOVED = "这只 bot 已迁到另一种底层，这边的记录不是最新的，去另一边读"
STOPPED = "这只 bot 已停用，先启用它再操作"
INTERNAL = "内部错误"

N_BAD = "n 要是 1 到 50 的整数"
HOURS_BAD = "max_hours 要是 1 到 168 的整数"
CHARS_BAD = "max_chars 要是 20 到 1000 的整数"
DM_NO_CHAT = "这只 bot 的配置里没有主人的聊天号，读不了私聊记录"
DM_UNREADABLE = "这只 bot 的聊天记录读不出来"

MEM_OUTSIDE = "这只 bot 的长期记忆文件不在它自己的目录里，或者是软链，不读"
MEM_UNREADABLE = "这只 bot 的长期记忆文件读不出来"
MEM_LIMIT = 1800

BODY_BAD = "请求体必须是一个 JSON 对象"
SOURCE_BAD = "source 只能是 user_moment、moment_reply、moment_image、group_turn、voice_call 之一"
TEXT_BAD = "text 要是一段不全是空白的文字，不超过 16 KiB"
KEY_BAD = "key 要是 1 到 200 个字符"
CHAT_FIELDS_BAD = "只有 group_turn 才能带 chat_id 和 human_ts"
NOTIFY_NO_CHAT = "这只 bot 的配置里没有主人的聊天号，投不了"
GW_DOWN = "这只 bot 的进程现在连不上，这条通知没有投出去"
GW_SLOW = "这只 bot 的进程没有及时回应，这条通知不一定投到了"
GW_WEIRD = "这只 bot 的进程回了认不出的结果，这条通知不一定投到了"

#: 改动前已有的能力键（tests/acceptance/bot_ops/adapter_util.py 的 CAPABILITIES_V11 原样）
CAPABILITIES_BEFORE = {
    "bot.restart.graceful": True, "bot.restart.force": True, "bot.set_provider": True,
    "bot.reset_provider": True, "bot.set_effort": True, "provider.subscription": False,
    "provider.per_session": False, "provider.switch_new_segment": True, "bot.notify": True,
    "bot.life_settings": True, "bot.enable": True, "bot.switch_in": True, "bot.group_chat": True,
    "bot.persona": True, "bot.voice": True, "bot.skills": True, "bot.create": True,
    "bot.archive": True, "bot.delete": True, "bot.profile": True, "bot.token_handoff": True,
    "bot.groups_discovered": True, "bot.features": True, "bot.rhythm": True,
    "bot.create.relationship": True,
}

#: 电话页拼好的一条通话回写（含 ⟦⟧、emoji、换行，验"原样收下"）
VOICE_TEXT = ("[voice-recap]（这是刚才哥哥和你打电话说的内容，记住即可，不用回复他）\n"
              "哥哥在电话里说了：\n· 明天要去⟦医院⟧复查\n· 想吃糖醋排骨🍖")
VOICE_KEY = "voice-call:call-20261114-0001"

OMIT = object()


# ---------------------------------------------------------------- 文件
def put_file(path, data, mode=0o600):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    p.chmod(mode)
    os.utime(str(p), (PAST, PAST))
    return p


def file_mark(path):
    """是否软链、字节、修改时间、inode。不在为 None。"""
    p = Path(path)
    if not os.path.lexists(str(p)):
        return None
    st = os.lstat(str(p))
    data = None if p.is_symlink() or p.is_dir() else p.read_bytes()
    return (p.is_symlink(), data, st.st_mtime_ns, st.st_ino)


def tree_mark(root):
    """一棵目录下全部条目的指纹（相对路径 → file_mark）。根不在为 None。"""
    root = Path(root)
    if not os.path.lexists(str(root)):
        return None
    out = {}
    for dirpath, dirnames, filenames in os.walk(str(root)):
        for n in dirnames + filenames:
            p = Path(dirpath) / n
            out[str(p.relative_to(root))] = "dir" if p.is_dir() and not p.is_symlink() else file_mark(p)
    return out


# ---------------------------------------------------------------- 账本（只按 ledger-schema.sql 造）
class Ledger:
    """在 <bot 目录>/state/ledger.sqlite 建库、插假行。时间一律毫秒，按 now_ms 往前倒推。"""

    def __init__(self, path, wal=True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.now_ms = int(time.time() * 1000)
        self.db = sqlite3.connect(str(self.path))
        if wal:
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
        self._seq = 0
        self._turn = 100

    def ago(self, minutes, extra_ms=0):
        return self.now_ms - int(minutes * 60_000) + extra_ms

    def inb(self, text, *, minutes=10, ts=None, chat=OWNER_CHAT, kind="user", state="pending"):
        """主人侧一行。返回行号。"""
        self._seq += 1
        ts = self.ago(minutes) if ts is None else ts
        cur = self.db.execute(
            "INSERT INTO inbound (ukey, chat_id, kind, tg_message_id, sender_id, sender_name, text, ts,"
            " received_at, state) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("tg:%s:%d" % (chat, self._seq), str(chat), kind, self._seq, str(chat), "哥哥", text,
             ts, ts + 5, state))
        return cur.lastrowid

    def new_turn(self):
        self._turn += 1
        return self._turn

    def out(self, text, *, minutes=9, sent_at=OMIT, created_at=None, chat=OWNER_CHAT, kind="text",
            state="sent", turn_id=OMIT, part=0):
        """bot 侧一行。turn_id 不给就新开一轮；给 None 就是空。返回行号。"""
        self._seq += 1
        if turn_id is OMIT:
            turn_id = self.new_turn()
        if sent_at is OMIT:
            sent_at = self.ago(minutes)
        if created_at is None:
            created_at = (sent_at if sent_at is not None else self.ago(minutes)) - 300
        cur = self.db.execute(
            "INSERT INTO outbound (okey, chat_id, turn_id, part, kind, text, state, tg_message_id,"
            " created_at, sent_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("o:%d" % self._seq, str(chat), turn_id, part, kind, text, state, 5000 + self._seq,
             created_at, sent_at))
        return cur.lastrowid

    def close(self):
        self.db.commit()
        try:
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.DatabaseError:
            pass
        self.db.close()
        # 合上之后再把 WAL 关掉，库文件里就是全部数据（不留 -wal / -shm）
        db = sqlite3.connect(str(self.path))
        db.execute("PRAGMA journal_mode=DELETE")
        db.close()


# ---------------------------------------------------------------- 假网关（任何路径的 POST 都记下）
class FakeGateway:
    """只绑 127.0.0.1。INTERFACE 没写适配服务转给网关的路径与请求体，所以任何请求都接、都记。

    reply(rec) -> (status, payload[, delay_s])；payload 为 dict/list 按 JSON 发，str 按纯文本发。
    默认：200 {"ok": true}。
    """

    def __init__(self, reply=None):
        self.requests = []
        self.reply = reply or (lambda rec: (200, {"ok": True}))
        gw = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _any(self):
                n = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(n) if n else b""
                text = raw.decode("utf-8", "replace")
                try:
                    parsed = json.loads(text) if text else None
                except ValueError:
                    parsed = None
                rec = {"method": self.command, "path": self.path, "body": text, "json": parsed,
                       "auth": self.headers.get("Authorization")}
                gw.requests.append(rec)
                res = gw.reply(rec)
                status, payload = res[0], res[1]
                if len(res) > 2 and res[2]:
                    time.sleep(res[2])
                if isinstance(payload, str):
                    out, ctype = payload.encode("utf-8"), "text/plain; charset=utf-8"
                else:
                    out, ctype = json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json"
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                except OSError:
                    pass

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _any

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._srv.daemon_threads = True
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    @property
    def port(self):
        return self._srv.server_address[1]

    def posts(self):
        return [r for r in self.requests if r["method"] not in ("GET", "HEAD", "OPTIONS")]

    def stop(self):
        self._srv.shutdown()
        self._srv.server_close()


def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def scalars(obj):
    """一个 JSON 值里全部的字符串与数字（递归）。"""
    if isinstance(obj, dict):
        for v in obj.values():
            yield from scalars(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from scalars(v)
    elif isinstance(obj, (str, int, float)) and not isinstance(obj, bool):
        yield obj


# ---------------------------------------------------------------- 发请求与断言
def body_of(resp):
    raw = resp.get_data(as_text=True)
    try:
        body = json.loads(raw)
    except ValueError:
        raise AssertionError("响应体不是 JSON：状态 %s，开头 %r" % (resp.status_code, raw[:200]))
    assert isinstance(body, dict), "响应体不是 JSON 对象：%r" % (body,)
    return body


def expect_ok(resp):
    body = body_of(resp)
    assert resp.status_code == 200, "应为 200，实际 %s：%r" % (resp.status_code, body)
    assert body.get("ok") is True, "成功响应必有 ok: true：%r" % (body,)
    return body


def expect_error(resp, status, code, message):
    """失败体固定为 {ok:false, code, message}，message 逐字。"""
    body = body_of(resp)
    assert (resp.status_code, body.get("code")) == (status, code), \
        "应为 %d %s，实际 %d %r" % (status, code, resp.status_code, body)
    assert body == {"ok": False, "code": code, "message": message}, \
        "失败体应逐字为 %r，实际 %r" % ({"ok": False, "code": code, "message": message}, body)
    return body


def call_with_deadline(fn, seconds=10.0, what="请求"):
    """在子线程里发请求，超时就判红（防止实现打开 FIFO 时卡死整组测试）。"""
    box = {}

    def run():
        try:
            box["r"] = fn()
        except BaseException as e:  # noqa: B902  原样带回主线程
            box["e"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), "%s 超过 %.0f 秒没有返回（疑似以阻塞方式打开了非普通文件）" % (what, seconds)
    if "e" in box:
        raise box["e"]
    return box["r"]


class World:
    """一条用例的全部家当：配置根、bot 目录、账本、记忆、假网关、测试客户端。"""

    def __init__(self, sandbox, monkeypatch):
        self.sb = sandbox
        self.mp = monkeypatch
        self._client = None
        self._gateways = []

    @property
    def home(self):
        """当前生效的 DSH_BOT_HOME（用例可能把它换成软链路径）。"""
        return Path(os.environ["DSH_BOT_HOME"])

    # -- 客户端 ----------------------------------------------------------
    @property
    def client(self):
        if self._client is None:
            import importlib
            mod = importlib.import_module("moments.adapter_service")
            self._client = mod.create_app().test_client()
        return self._client

    def call(self, method, path, body=None, *, token=ADAPTER_TOKEN, raw=None, auth=OMIT):
        headers = {}
        if auth is not OMIT:
            if auth is not None:
                headers["Authorization"] = auth
        elif token is not None:
            headers["Authorization"] = "Bearer " + token
        kw = {"method": method, "headers": headers}
        if raw is not None:
            kw["data"] = raw
            kw["content_type"] = "application/json"
        elif body is not None:
            kw["json"] = body
        return self.client.open(path, **kw)

    def dm_tail(self, bot_id=BOT, query="", **kw):
        return self.call("GET", dm_tail_url(bot_id) + ("?" + query if query else ""), **kw)

    def memory(self, bot_id=BOT, **kw):
        return self.call("GET", memory_url(bot_id), **kw)

    def notify(self, body, bot_id=BOT, **kw):
        return self.call("POST", notify_url(bot_id), body, **kw)

    # -- 摆假 bot ----------------------------------------------------------
    def botdir(self, bot_id=BOT):
        return self.home / "bots" / bot_id

    def bot(self, bot_id=BOT, *, enabled=True, chat_id=OWNER_CHAT, channel="default", gw=None,
            gw_port=None, extra=None):
        """在配置根摆一只 dsh bot。

        chat_id=OMIT：yml 里不写 chat_id；channel="default" 写 <bot 目录>/channel，None 不写这个键，
        别的值原样写进 bot_channel_path；gw 给了假网关就写好 state/api.key 与 api.port。
        """
        botdir = self.botdir(bot_id)
        (botdir / "channel").mkdir(parents=True, exist_ok=True)
        (botdir / "state").mkdir(parents=True, exist_ok=True)
        cfg = {"id": bot_id, "display_name": "测试" + bot_id, "dispatcher_port": 0,
               "brain": {"provider": "deepseek-official", "model": "deepseek-flash"}}
        if channel == "default":
            cfg["bot_channel_path"] = str(botdir / "channel")
        elif channel is not None:
            cfg["bot_channel_path"] = str(channel)
        if chat_id is not OMIT:
            cfg["chat_id"] = chat_id
        if not enabled:
            cfg["enabled"] = False
        cfg.update(extra or {})
        port = gw.port if gw is not None else gw_port
        if port is not None:
            put_file(botdir / "state" / "api.key", GW_KEY + "\n")
            put_file(botdir / "state" / "api.port", "%d\n" % port)
        yml = put_file(self.sb.dsh_cfg / ("%s.yml" % bot_id), json.dumps(cfg, ensure_ascii=False))
        return SimpleNamespace(id=bot_id, yml=yml, botdir=botdir, channel=botdir / "channel", cfg=cfg)

    def set_cfg(self, bot, **fields):
        cfg = dict(bot.cfg)
        for k, v in fields.items():
            if v is None:
                cfg.pop(k, None)
            else:
                cfg[k] = v
        bot.cfg = cfg
        put_file(bot.yml, json.dumps(cfg, ensure_ascii=False))

    def moved(self, bot_id=BOT, content="this-is-not-a-valid-marker\n"):
        """迁出标记 <DSH_BOT_HOME>/bots/<id>/.moved。INTERFACE：读不出、格式不对也按有标记处理。"""
        return put_file(self.botdir(bot_id) / ".moved", content)

    def ledger(self, bot_id=BOT, **kw):
        return Ledger(self.botdir(bot_id) / "state" / "ledger.sqlite", **kw)

    def ledger_path(self, bot_id=BOT):
        return self.botdir(bot_id) / "state" / "ledger.sqlite"

    def memory_path(self, bot_id=BOT):
        return self.botdir(bot_id) / "channel" / "memory" / "MEMORY.md"

    def gateway(self, reply=None):
        gw = FakeGateway(reply)
        self._gateways.append(gw)
        return gw

    def close(self):
        for gw in self._gateways:
            gw.stop()


def voice_body(**over):
    body = {"source": "voice_call", "text": VOICE_TEXT, "key": VOICE_KEY}
    for k, v in over.items():
        if v is OMIT:
            body.pop(k, None)
        else:
            body[k] = v
    return body
