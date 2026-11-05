# -*- coding: utf-8 -*-
"""rel-init 验收（黑盒）：新建 bot 时给关系初始值，tick 默认值，nsfw 尖峰与好感脱钩。

唯一依据：.devflow/rel-init-接口.md 第 7 节行为清单 25 条，用例名 test_NN_ 对应清单编号。
只走对外接口：GET /hub/addbot 的页面与页面脚本（node 假 DOM 实跑）、POST /hub/api/bots、
GET /adapter/v1/meta 与 POST /adapter/v1/bots、jiwen/tick.py 的 main()、relationship.drift_desire。
夹具写法照 bot_ops（getMe 桩、假 bun/launchctl、心跳）、tests/unit/test_hub_pages_dom.py（node 假 DOM）、
tests/unit/test_m3_life.py（tick 假环境）。
"""
import builtins
import datetime
import importlib.util
import io
import json
import os
import random
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from html.parser import HTMLParser
from pathlib import Path

import pytest

from bot_ops import adapter_util as au
from bot_ops import botops_util as u

ROOT = Path(__file__).resolve().parents[3]
NODE = shutil.which("node")

KEYS201 = {"ok", "bot_id", "username", "steps", "restart_hint", "features", "features_skipped", "skills"}
#: 适配接口成功体键集合（改动前，见 bot_ops/test_adapter_v10_create.py 的 CREATE_KEYS）
ADAPTER_KEYS = {"ok", "bot_id", "person", "username", "enabled", "running",
                "features", "skills", "features_skipped"}

MSG_NOT_OBJ = "relationship 要是对象，只能有 affection、trust、desire 三个键"
MSG_BAD_KEY = "relationship 只能有 affection、trust、desire 三个键（精力不能填，固定 85）"


def msg_bad_val(key):
    return "relationship.%s 必须是 0-100 的整数" % key


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ============================================================ 页面（1-4）

class _Inputs(HTMLParser):
    """收集页面里所有 input 的属性，以及 id=rel-box 那个元素里的文字。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.inputs = []
        self.box_depth = 0
        self.box_found = False
        self.box_text = []
        self._stack = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "input":
            self.inputs.append(a)
            return
        if tag in ("br", "img", "meta", "link", "hr"):
            return
        if a.get("id") == "rel-box":
            self.box_found = True
            self.box_depth = 1
        elif self.box_depth:
            self.box_depth += 1

    def handle_endtag(self, tag):
        if tag in ("input", "br", "img", "meta", "link", "hr"):
            return
        if self.box_depth:
            self.box_depth -= 1

    def handle_data(self, data):
        if self.box_depth:
            self.box_text.append(data)


def _page(client):
    r = client.get("/hub/addbot")
    assert r.status_code == 200, r.status_code
    p = _Inputs()
    p.feed(r.get_data(as_text=True))
    return p


def _dom_run(client, tmp_path, steps, auto=()):
    """把 GET /hub/addbot 拿到的整页交给 node 假 DOM（驱动脚本取自 tests/unit/test_hub_pages_dom.py）。"""
    if NODE is None:
        pytest.fail("这台机器没有 node，页面脚本跑不起来")
    ref = _load("rel_init_dom_ref", ROOT / "tests" / "unit" / "test_hub_pages_dom.py")
    html = client.get("/hub/addbot").get_data(as_text=True)
    tree = ref._Tree()
    tree.feed(html)
    driver, spec = tmp_path / "driver.js", tmp_path / "spec.json"
    driver.write_text(ref.DRIVER, encoding="utf-8")
    spec.write_text(json.dumps({"tree": tree.root, "steps": steps, "auto": list(auto)},
                               ensure_ascii=False), encoding="utf-8")
    r = subprocess.run([NODE, str(driver), str(spec)], stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    got = json.loads(r.stdout.decode("utf-8").strip().splitlines()[-1])
    assert got["ok"], got.get("error")
    assert got["errors"] == [], got["errors"]
    return got["out"]


TEMPLATES_OK = {"req": "GET /hub/api/bots/templates", "status": 200, "body": {"templates": []}}
CREATE_REQ = "POST /hub/api/bots"


def _fill(aff, trust, desire):
    base = {"bot_id": "xiaoyu", "display_name": "小雨", "dispatcher_port": "17955",
            "chat_id": "123456789", "telegram_token": u.FAKE_TOKEN, "persona_text": "人设正文"}
    steps = [{"op": "set", "id": k, "value": v} for k, v in base.items()]
    steps += [{"op": "set", "id": "rel_affection", "value": aff},
              {"op": "set", "id": "rel_trust", "value": trust},
              {"op": "set", "id": "rel_desire", "value": desire}]
    return steps


def test_01_页面有关系初始值卡片_三个数字框属性齐全且没有value(client):
    p = _page(client)
    assert p.box_found, "页面里没有 id=rel-box 的卡片"
    assert "关系初始值" in "".join(p.box_text), "rel-box 卡片标题不是「关系初始值」"
    for name in ("rel_affection", "rel_trust", "rel_desire"):
        hits = [a for a in p.inputs if a.get("id") == name]
        assert len(hits) == 1, "页面里应恰好有一个 id=%s 的输入框，实际 %d 个" % (name, len(hits))
        a = hits[0]
        assert a.get("name") == name, (name, a)
        assert (a.get("type"), a.get("min"), a.get("max"), a.get("step")) == ("number", "0", "100", "1"), (name, a)
        assert "required" in a, "%s 缺 required" % name
        assert "value" not in a, "%s 不该有 value 属性：%r" % (name, a.get("value"))


def test_02_页面没有精力输入框(client):
    p = _page(client)
    bad = [a for a in p.inputs if "energy" in (a.get("id") or "") or "energy" in (a.get("name") or "")]
    assert bad == [], bad


def test_03_填20_30_50点创建_请求体relationship是三个JSON整数(client, tmp_path):
    out = _dom_run(client, tmp_path, _fill("20", "30", "50") + [
        {"op": "click", "id": "create"}, {"op": "snap", "as": "s", "ids": ["create-msg"], "rows": []}],
        auto=[TEMPLATES_OK])
    posts = [r for r in out["s"]["requests"] if r["req"] == CREATE_REQ]
    assert len(posts) == 1, out["s"]["requests"]
    rel = posts[0]["body"].get("relationship")
    assert rel == {"affection": 20, "trust": 30, "desire": 50}, rel
    assert all(type(v) is int for v in rel.values()), rel


def test_04_留空或3点5或101_不发请求_显示提示_按钮没禁用(client, tmp_path):
    cases = [(("", "30", "50"), "好感"), (("20", "3.5", "50"), "信任"), (("20", "30", "101"), "nsfw")]
    for i, (vals, label) in enumerate(cases):
        d = tmp_path / ("c%d" % i)
        d.mkdir()
        out = _dom_run(client, d, _fill(*vals) + [
            {"op": "click", "id": "create"},
            {"op": "snap", "as": "s", "ids": ["create-msg", "create"], "rows": []}], auto=[TEMPLATES_OK])
        s = out["s"]
        assert [r for r in s["requests"] if r["req"] == CREATE_REQ] == [], (vals, s["requests"])
        assert s["els"]["create-msg"]["text"] == "关系初始值的「%s」要填 0-100 的整数" % label, \
            (vals, s["els"]["create-msg"]["text"])
        assert s["els"]["create"]["disabled"] is False, vals


# ============================================================ 页面接口 POST /hub/api/bots（5-16）

def _token(head):
    return "%d:test-AAaaBBccDDddEEeeFFggHHhh11" % head


def _body(bot_id="xiaoyu", port=None, head=123456789, **over):
    body = {"bot_id": bot_id, "telegram_token": _token(head),
            "dispatcher_port": port if port is not None else u.free_port(),
            "chat_id": 123456789, "display_name": "小雨", "persona_text": "我是小雨的人设正文。"}
    body.update(over)
    return body


@pytest.fixture
def hub(client, sandbox, bun, telegram, launchctl, heartbeat, as_darwin):
    """新建全流程成功要的外部条件（同 test_create_bot._arm_ok），每建一只再补一份。"""
    launchctl.set_default(installed=True, running=True, pid=4242)
    telegram.mode = "ok"

    class H:
        def create(self, bot_id="xiaoyu", head=123456789, **over):
            bun.expect(exit=0).expect(exit=0)
            heartbeat.tick(bot_id, pid=4242)
            telegram.bot_id = head
            return client.post("/hub/api/bots", json=_body(bot_id, head=head, **over))

        def rel(self, bot_id="xiaoyu"):
            return sandbox.dsh_home / "bots" / bot_id / "channel" / "relationship.json"

    return H()


def _read_rel(path):
    assert path.is_file(), "relationship.json 没写出来：%s" % path
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_rel(path, aff, trust, desire, stage=None):
    rel = _read_rel(path)
    for k, want in (("affection", aff), ("trust", trust), ("desire", desire), ("energy", 85)):
        assert isinstance(rel.get(k), float) and rel[k] == float(want), (k, rel.get(k))
    if stage is not None:
        assert rel.get("stage") == stage, rel.get("stage")
    assert isinstance(rel.get("prompt_snippet"), str) and rel["prompt_snippet"].strip(), rel.get("prompt_snippet")
    assert type(rel.get("updated_ts")) is int, rel.get("updated_ts")
    return rel


def _assert_nothing(sandbox, telegram, bot_id="xiaoyu"):
    assert not (sandbox.dsh_cfg / ("%s.yml" % bot_id)).exists(), "被拒了还落了配置"
    assert not (sandbox.dsh_home / "bots" / bot_id).exists(), "被拒了还落了 bot 家"
    assert telegram.hits == [], "校验不过不该问 getMe：%r" % telegram.hits


def test_05_带20_30_50_201_文件0600数值对_stage客气试探(hub, sandbox):
    r = hub.create(relationship={"affection": 20, "trust": 30, "desire": 50})
    assert r.status_code == 201, r.get_json()
    assert set(r.get_json()) == KEYS201, r.get_json()
    _assert_rel(hub.rel(), 20, 30, 50, "客气试探")
    if os.name != "nt":
        assert u.mode_of(hub.rel()) == 0o600, oct(u.mode_of(hub.rel()))


def test_06_不带_null_空对象_都201_文件5_5_0_85_陌生防备(hub):
    cases = [("omit", None), ("nullv", "null"), ("empty", {})]
    for i, (name, val) in enumerate(cases):
        bot_id = "xiaoyu" + name
        over = {} if val is None else {"relationship": None if val == "null" else val}
        r = hub.create(bot_id=bot_id, head=111111111 * (i + 1) + 1, **over)
        assert r.status_code == 201, (name, r.get_json())
        _assert_rel(hub.rel(bot_id), 5, 5, 0, "陌生防备")


def test_07_只带trust40_文件5_40_0_85(hub):
    r = hub.create(relationship={"trust": 40})
    assert r.status_code == 201, r.get_json()
    _assert_rel(hub.rel(), 5, 40, 0)


def test_08_0和100都收_字符串30收为30(hub):
    r = hub.create(relationship={"affection": 0, "trust": 100, "desire": "30"})
    assert r.status_code == 201, r.get_json()
    _assert_rel(hub.rel(), 0, 100, 30, "交心亲密")


def test_09_勾不勾relationship功能都写文件_数值与stage相同(hub):
    rel = {"affection": 20, "trust": 70, "desire": 50}
    r1 = hub.create(bot_id="xiaoyuon", head=211111111, relationship=rel, features=["relationship"])
    assert r1.status_code == 201, r1.get_json()
    r2 = hub.create(bot_id="xiaoyuoff", head=311111111, relationship=rel)
    assert r2.status_code == 201, r2.get_json()
    a = _assert_rel(hub.rel("xiaoyuon"), 20, 70, 50, "亲近暧昧")
    b = _assert_rel(hub.rel("xiaoyuoff"), 20, 70, 50, "亲近暧昧")
    pick = lambda d: {k: d[k] for k in ("affection", "trust", "desire", "energy", "stage")}  # noqa: E731
    assert pick(a) == pick(b)


def test_10_relationship不是对象_400_bad_body_零文件_没调getMe(client, sandbox, telegram):
    for val in ([], "x", 5, True):
        r = client.post("/hub/api/bots", json=_body(relationship=val))
        assert r.status_code == 400, (val, r.status_code, r.get_json())
        body = r.get_json()
        assert body.get("error") == "bad_body" and body.get("detail") == MSG_NOT_OBJ, (val, body)
        _assert_nothing(sandbox, telegram)


BAD_VALUES = [-1, 101, 3.5, 30.0, "abc", True, "0" * 30000, "+30", "", "3.5"]


def test_11_某键值不合规_400_bad_field_逐字提示_零文件_不回500(client, sandbox, telegram):
    for key in ("affection", "trust", "desire"):
        for val in BAD_VALUES:
            rel = {"affection": 20, "trust": 30, "desire": 50}
            rel[key] = val
            r = client.post("/hub/api/bots", json=_body(relationship=rel))
            shown = val if not (isinstance(val, str) and len(val) > 20) else "3万位0"
            assert r.status_code == 400, (key, shown, r.status_code)
            body = r.get_json()
            assert body.get("error") == "bad_field" and body.get("detail") == msg_bad_val(key), (key, shown, body)
            _assert_nothing(sandbox, telegram)
    # 多处不合规：按 affection → trust → desire 报第一个
    r = client.post("/hub/api/bots", json=_body(relationship={"affection": 5, "trust": -1, "desire": 101}))
    assert r.status_code == 400 and r.get_json().get("detail") == msg_bad_val("trust"), r.get_json()


def test_12_含energy或foo键_400_bad_field_逐字提示_零文件(client, sandbox, telegram):
    for rel in ({"affection": 20, "energy": 85}, {"foo": 1}):
        r = client.post("/hub/api/bots", json=_body(relationship=rel))
        assert r.status_code == 400, (rel, r.status_code, r.get_json())
        body = r.get_json()
        assert body.get("error") == "bad_field" and body.get("detail") == MSG_BAD_KEY, (rel, body)
        _assert_nothing(sandbox, telegram)


def test_13_relationship不合规同时端口被占_回400不是409(client, sandbox, telegram):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(5)
    try:
        r = client.post("/hub/api/bots", json=_body(port=s.getsockname()[1], relationship={"trust": 101}))
        assert r.status_code == 400, (r.status_code, r.get_json())
        assert r.get_json().get("detail") == msg_bad_val("trust"), r.get_json()
    finally:
        s.close()


class _PortGrabber:
    """relationship.json 一被写（打开、改名、搬移任一原语碰到这个文件名）之后，立刻在端口上起监听。

    只包标准库的文件原语（同 test_create_bot._boom_moves 的接缝），不碰被测实现。
    """

    def __init__(self, monkeypatch, port):
        self.port = port
        self.sock = None
        names = [(builtins, "open"), (io, "open"), (os, "open"), (os, "replace"),
                 (os, "rename"), (shutil, "move")]
        for mod, attr in names:
            monkeypatch.setattr(mod, attr, self._wrap(getattr(mod, attr)))

    def _wrap(self, real):
        grab = self

        def wrapper(*a, **kw):
            out = real(*a, **kw)
            if grab.sock is None and any("relationship.json" in str(x) for x in a[:2]):
                s = socket.socket()
                s.bind(("127.0.0.1", grab.port))
                s.listen(5)
                grab.sock = s
            return out
        return wrapper

    def close(self):
        if self.sock is not None:
            self.sock.close()


def _rel_files(root):
    return [str(p) for p in Path(root).rglob("relationship.json")]


def test_14_文件写完后端口被抢_409_没有bot家也没有relationship_json(hub, sandbox, monkeypatch):
    port = u.free_port()
    with monkeypatch.context() as mp:
        grab = _PortGrabber(mp, port)
        try:
            r = hub.create(port=port, relationship={"affection": 20, "trust": 30, "desire": 50})
        finally:
            grab.close()
    assert grab.sock is not None, "relationship.json 根本没被写过，端口没机会被抢"
    assert r.status_code == 409, (r.status_code, r.get_json())
    assert not (sandbox.dsh_home / "bots" / "xiaoyu").exists(), "失败后还留着 bot 家"
    assert _rel_files(sandbox.tmp) == [], "失败后还留着 relationship.json"


def test_15_bot家已有state_落盘中途失败_channel删干净_state原样(hub, sandbox, monkeypatch):
    home = sandbox.dsh_home / "bots" / "xiaoyu"
    au.put_file(home / "state" / "keep.txt", "别删我")
    before = u.tree_bytes(home / "state")
    port = u.free_port()
    with monkeypatch.context() as mp:
        grab = _PortGrabber(mp, port)
        try:
            r = hub.create(port=port, relationship={"affection": 20, "trust": 30, "desire": 50})
        finally:
            grab.close()
    assert grab.sock is not None, "relationship.json 根本没被写过，端口没机会被抢"
    assert r.status_code == 409, (r.status_code, r.get_json())
    assert not (home / "channel").exists(), "channel/ 没删干净"
    assert u.tree_bytes(home / "state") == before, "state/ 被动了"
    assert _rel_files(sandbox.tmp) == []


def test_16_201响应体键集合与改动前相同(hub):
    r = hub.create()
    assert r.status_code == 201, r.get_json()
    assert set(r.get_json()) == KEYS201, sorted(r.get_json())


# ============================================================ 适配接口（17-20）

def test_17_meta能力表多bot_create_relationship_其余不变(adapter):
    caps = dict(adapter.ok(adapter.call("GET", au.META)).get("capabilities") or {})
    assert caps.get("bot.create.relationship") is True, caps
    caps.pop("bot.create.relationship")
    before = {k: v for k, v in au.CAPABILITIES_V11.items() if k != "bot.create.relationship"}
    assert caps == before, caps


def _adapter_body(**over):
    body = {"bot_id": "botnew", "telegram_token": au.NEW_TG_TOKEN, "chat_id": au.NEW_CHAT}
    body.update(over)
    return body


def _adapter_rel(adapter):
    return adapter.sandbox.dsh_home / "bots" / "botnew" / "channel" / "relationship.json"


def test_18_适配新建带合法relationship_200_文件同第5条_成功体键不变(adapter, telegram, bun):
    got = adapter.ok(adapter.call("POST", au.BOTS, _adapter_body(
        relationship={"affection": 20, "trust": 30, "desire": 50})))
    assert set(got) == ADAPTER_KEYS, got
    _assert_rel(_adapter_rel(adapter), 20, 30, 50, "客气试探")
    if os.name != "nt":
        assert au.mode_of(_adapter_rel(adapter)) == 0o600


def test_19_适配switch_in不带relationship_200_文件5_5_0_85(adapter, telegram, bun):
    adapter.ok(adapter.call("POST", au.BOTS, _adapter_body(
        enabled=False, person="lucy", telegram_username="@MY_BOT",
        telegram_user_id=123456789, purpose="switch_in")))
    _assert_rel(_adapter_rel(adapter), 5, 5, 0, "陌生防备")


def test_20_适配不合规_422_invalid_message同页面_零文件_没调getMe(adapter, telegram, bun):
    sb = adapter.sandbox
    for rel, msg in (([], MSG_NOT_OBJ), ({"trust": 101}, msg_bad_val("trust")),
                     ({"energy": 85}, MSG_BAD_KEY)):
        body = adapter.expect_error(adapter.call("POST", au.BOTS, _adapter_body(relationship=rel)), 422, "invalid")
        assert body.get("message") == msg, (rel, body)
        assert not (sb.dsh_cfg / "botnew.yml").exists() and not (sb.dsh_home / "bots" / "botnew").exists(), rel
        assert telegram.hits == [], rel


# ============================================================ tick（21-22）

SCHEMA = (ROOT / "gateway" / "src" / "ledger-schema.sql").read_text(encoding="utf-8")
NINE = ("voice", "image", "stickers", "moments", "proactive", "life", "relationship", "groups", "nsfw")


def _tick_bot(tmp_path, monkeypatch, rel=None):
    """照 test_m3_life 的新系统布局：配置根 + bots/bot5/{channel,state/ledger.sqlite}；不插消息。"""
    base = tmp_path / "tick"
    root = base / "bots" / "bot5"
    (root / "channel").mkdir(parents=True)
    (root / "state").mkdir()
    db = sqlite3.connect(str(root / "state" / "ledger.sqlite"))
    db.executescript(SCHEMA)
    db.commit()
    db.close()
    cfgs = base / "configs"
    cfgs.mkdir()
    (base / "old.yml").write_text("id: oldbot\nchat_id: '1'\nbot_channel_path: /old/channel\n"
                                  "sleep_hours: '00:00-07:30'\npersona_summary: 旧配置里的设定\n", encoding="utf-8")
    feats = "".join("  %s: %s\n" % (k, "true" if k == "relationship" else "false") for k in NINE)
    (cfgs / "bot5.yml").write_text(
        "id: bot5\nbot_channel_path: %s\ndispatcher_port: 0\nlife_config: %s\nfeatures:\n%s"
        % (json.dumps(str(root / "channel")), json.dumps(str(base / "old.yml")), feats), encoding="utf-8")
    home = base / "home"
    home.mkdir()
    (base / "credentials.yaml").write_text(
        "version: 1\nrefs:\n  DEEPSEEK_API_KEY: sk-test-only-0000000000\n", encoding="utf-8")
    for k, v in (("HUB_CONFIGS_DIR", cfgs), ("HOME", home), ("USERPROFILE", home), ("DSH_BOT_HOME", base)):
        monkeypatch.setenv(k, str(v))
    monkeypatch.delenv("DSH_BOT_LEDGER", raising=False)
    if rel is not None:
        (root / "channel" / "relationship.json").write_text(json.dumps(rel), encoding="utf-8")
    assert not (root / "state" / "jiwen").exists()
    return root


def _run_tick(monkeypatch, name):
    tick = _load(name, ROOT / "jiwen" / "tick.py")
    monkeypatch.setattr(tick.config_loader, "load_global", lambda: {})
    monkeypatch.setattr(tick.deepseek_delta, "compute_delta",
                        lambda **k: {"connection": 0.0, "valence": 0.0, "arousal": 0.0})
    monkeypatch.setattr(sys, "argv", ["tick.py"])
    tick.main()


def test_21_tick_没有关系文件也没有积温状态_写出5_5_0_85_陌生防备(tmp_path, monkeypatch):
    root = _tick_bot(tmp_path, monkeypatch)
    _run_tick(monkeypatch, "rel_init_tick_fresh")
    rel = _read_rel(root / "channel" / "relationship.json")
    got = {k: rel.get(k) for k in ("affection", "trust", "desire", "energy")}
    assert got == {"affection": 5, "trust": 5, "desire": 0, "energy": 85}, got
    assert rel.get("stage") == "陌生防备", rel.get("stage")


def _spike_p(mins, night, rel_factor, affection):
    """接口 §6：p = min(0.30, 0.0015 * mins * 夜间系数 * rel * (0.6 + affection/100))。"""
    return min(0.30, 0.0015 * mins * night * rel_factor * (0.6 + affection / 100.0))


def test_22_tick_已有50_50_0_80_好感信任仍是50_尖峰概率用established系数(tmp_path, monkeypatch):
    """积温状态里的「上次 tick 是 5 分钟前」不手写文件（格式接口没给），而是先跑一次 tick 让它自己写，
    再把时钟（time.time）拨后 5 分钟跑第二次；"现在"固定在本地时间 21:00。"""
    import relationship
    root = _tick_bot(tmp_path, monkeypatch, rel={"affection": 50, "trust": 50, "desire": 0, "energy": 80})
    probs = []
    monkeypatch.setattr(relationship.rng, "chance", lambda p: probs.append(p) or False)
    t21 = datetime.datetime(2026, 11, 5, 21, 0).timestamp()
    clock = {"t": t21 - 300}
    monkeypatch.setattr(time, "time", lambda: clock["t"])
    _run_tick(monkeypatch, "rel_init_tick_kept_1")
    probs.clear()
    clock["t"] = t21
    _run_tick(monkeypatch, "rel_init_tick_kept_2")
    rel = _read_rel(root / "channel" / "relationship.json")
    assert (rel.get("affection"), rel.get("trust")) == (50, 50), rel
    assert "updated_ts" in rel, "tick 没真跑到这只 bot（文件没被重写）"
    want = _spike_p(5, 1.6, 1.2, 50)
    assert probs, "第二次 tick 没有调用尖峰判定"
    assert all(p == pytest.approx(want) for p in probs), (probs, want, "stranger 应为 %r" % _spike_p(5, 1.6, 0.25, 50))


# ============================================================ nsfw 尖峰回归（23-25，本次不改）

def _rel_mod():
    import relationship
    return relationship


def _seeded(monkeypatch, rel):
    """把 rng.chance / rng.uniform 换成 random.Random(42) 驱动的版本，记下每次传给 chance 的概率。"""
    r = random.Random(42)
    probs = []

    def chance(p):
        probs.append(p)
        return r.random() < p

    monkeypatch.setattr(rel.rng, "chance", chance)
    monkeypatch.setattr(rel.rng, "uniform", lambda lo, hi: r.uniform(lo, hi))
    return probs


def _never(monkeypatch, rel):
    probs = []
    monkeypatch.setattr(rel.rng, "chance", lambda p: probs.append(p) or False)
    return probs


AT21 = datetime.datetime(2026, 11, 5, 21, 0)


def test_23_好感度仍影响尖峰概率_比值1点6比0点6_stranger是established的0点25比1点2(monkeypatch):
    rel = _rel_mod()

    def run(n=2000, **kw):
        with monkeypatch.context() as mp:
            probs = _seeded(mp, rel)
            d = 0.0
            for _ in range(n):
                d = rel.drift_desire({"desire": d}, AT21, 5, **kw)["desire"]
            return list(probs)

    p100, p0 = run(affection=100), run(affection=0)
    assert p100 and p0, "chance 一次都没被调用"
    assert p100[0] / p0[0] == pytest.approx(1.6 / 0.6), (p100[0], p0[0])
    assert all(p == pytest.approx(p100[0]) for p in p100) and all(p == pytest.approx(p0[0]) for p in p0)
    stranger, established = run(n=1, profile="stranger"), run(n=1, profile="established")
    assert stranger and established
    assert stranger[0] / established[0] == pytest.approx(0.25 / 1.2), (stranger, established)


def test_24_夜间系数_21点_4点_10点传入的概率(monkeypatch):
    rel = _rel_mod()
    for hour, night in ((21, 1.6), (4, 0.4), (10, 1.0)):
        probs = _never(monkeypatch, rel)
        rel.drift_desire({"desire": 0.0}, datetime.datetime(2026, 11, 5, hour, 0), 5,
                         affection=50, profile="established")
        want = _spike_p(5, night, 1.2, 50)
        assert probs and all(p == pytest.approx(want) for p in probs), (hour, probs, want)


def test_25_压制与冷却回归_例假压到30_冷却59点4_低于15不降(monkeypatch):
    rel = _rel_mod()
    _never(monkeypatch, rel)
    got = rel.drift_desire({"desire": 80.0}, AT21, 5, event={"name": "例假第一天"})["desire"]
    assert got == pytest.approx(30.0), got
    assert rel.drift_desire({"desire": 60.0}, AT21, 5)["desire"] == pytest.approx(59.4)
    assert rel.drift_desire({"desire": 10.0}, AT21, 5)["desire"] == pytest.approx(10.0)
