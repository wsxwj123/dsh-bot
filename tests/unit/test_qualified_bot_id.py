# -*- coding: utf-8 -*-
"""「合格的 bot id」的白盒单测（INTERFACE-新系统bot运维 §0 第 7 条、§5b 第 5 条，第 7 稿）。

合格 = 形状合法（整串）并且不是保留名 `jiwen` / `director` / `voicecall`。判定与保留名表
各只有一份，在 `config_sources`；新建流程、放行闸、名单出口、单元名拼装四处引用同一份。
这里逐处量：

- 判定函数自己的各分支；
- `bot_launchd.label_of` 对不合格的 id 不给单元名，四个动作拿不到就不调 launchctl
  （只读的 print 也不发）；
- `bots_client.list_bots` / `bot_sources` 跳过新系统配置根下文件名不合格的条目，
  旧系统配置根与注入名单不受影响；
- 保留名过不了放行闸，「坏文件」补充口也不对它开。

保留名在这里是被测对象本身，所以**每一条**用例都在假 launchctl 与假 bun 在位的前提下跑
（`_fakes` 是 autouse），并且每条收尾时统一断言两个假件的记录里没有保留名与坏名字的
单元名。别的 bot 名全是假名。
"""
import os
from types import SimpleNamespace

import pytest

from moments import bot_launchd as bl
from moments import bots_client as bc
from moments import config_sources as cs
from moments import hub_addbot
from moments.provider_model import HubError

pytestmark = pytest.mark.skipif(os.name == "nt", reason="假件是 sh 脚本")

RESERVED = ["jiwen", "director", "voicecall"]
BAD_FILE = "Bad-Name"
BROKEN = "id: [oops\n  x: : :\n"
TOKEN = "123456789:test-AAaaBBccDDddEEeeFFggHHhh11"
#: 不合格的 id：三个保留名，加上各种形状非法的
UNQUALIFIED = RESERVED + [BAD_FILE, "UPPER", "has-dash", "a", "1abc", "x" * 33, "has space",
                          "goodbot ", "goodbot\n", "jiwen\n", "", None, 7]


def _recorder(dirpath, name, extra=""):
    """一支只记参数的假件：每次调用把参数记一行，退出 0。"""
    log = dirpath / (name + ".calls")
    path = dirpath / ("fake-" + name)
    path.write_text("#!/bin/sh\necho \"$@\" >> '%s'\n%sexit 0\n" % (log, extra), encoding="utf-8")
    path.chmod(0o755)
    return SimpleNamespace(
        path=str(path),
        calls=lambda: log.read_text(encoding="utf-8").splitlines() if log.exists() else [])


@pytest.fixture(autouse=True)
def _fakes(tmp_path, monkeypatch):
    """每条用例：假 launchctl（print 报 running / pid 4242）与假 bun 都在位。

    收尾时统一查一遍：两个假件的调用记录里，一次都没提到保留名与坏名字的单元名。
    """
    d = tmp_path / "fakes"
    d.mkdir()
    lc = _recorder(d, "launchctl",
                   "case \"$1\" in print) printf '\\tstate = running\\n\\tpid = 4242\\n';; esac\n")
    bun = _recorder(d, "bun")
    monkeypatch.setenv("HUB_LAUNCHCTL", lc.path)
    monkeypatch.setenv("HUB_BUN", bun.path)
    monkeypatch.setattr(bl, "RESTART_WAIT", 1.0)    # 哪天把关失效了，红用例别陪着等满 45 秒
    monkeypatch.setattr(bl, "POLL_INTERVAL", 0.02)
    yield SimpleNamespace(launchctl=lc, bun=bun)
    blob = "\n".join(lc.calls() + bun.calls())
    for name in RESERVED + [BAD_FILE]:
        assert ("com.dsh-bot.%s" % name) not in blob, ("假件的记录里出现了不该碰的单元", name, blob)
    assert bun.calls() == [], bun.calls()


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """两套 tmp 配置根 + 新系统家目录 + plist 目录，HOME 钉在 tmp。"""
    d = {n: tmp_path / n for n in ("legacy", "dsh", "home", "dsh_home", "agents")}
    for p in d.values():
        p.mkdir()
    monkeypatch.setenv("HOME", str(d["home"]))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(d["legacy"]))
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(d["dsh"]))
    monkeypatch.setenv("DSH_BOT_HOME", str(d["dsh_home"]))
    monkeypatch.setenv("HUB_LAUNCHAGENTS_DIR", str(d["agents"]))
    monkeypatch.delenv("HUB_BOTS_FILE", raising=False)
    monkeypatch.delenv("HUB_RESTART_CMD", raising=False)
    for k in list(os.environ):
        if k.startswith("DISPATCHER_PORT_"):
            monkeypatch.delenv(k, raising=False)
    return SimpleNamespace(**d)


def _write(root, stem, enabled=None, **fields):
    """在配置根下写一份内容合法、文件名是 `<stem>.yml` 的配置（stem 可以是怪名字）。"""
    fields.setdefault("dispatcher_port", 17951)
    lines = ["id: %s" % (stem.strip() or "x")]
    if enabled is not None:
        lines.append("enabled: %s" % ("true" if enabled else "false"))
    lines += ["%s: %s" % kv for kv in sorted(fields.items())]
    p = root / (stem + ".yml")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _rejected(fn, *args):
    with pytest.raises(HubError) as e:
        fn(*args)
    assert (e.value.status, e.value.error, e.value.detail) == (
        404, "bot_not_found", hub_addbot.GATE_404_DETAIL), (e.value.status, e.value.error)


# ------------------------------------------------------------ 判定与保留名表：各只有一份

@pytest.mark.parametrize("bot_id", ["goodbot", "ab", "a_1", "x" * 32, "null", "true"])
def test_合格_形状合法且不是保留名(bot_id):
    assert cs.is_qualified_bot_id(bot_id) is True


@pytest.mark.parametrize("bot_id", UNQUALIFIED, ids=repr)
def test_不合格_保留名与形状非法的都不算(bot_id):
    assert cs.is_qualified_bot_id(bot_id) is False


def test_保留名表只有一份_新建流程引用的就是它():
    assert hub_addbot.RESERVED_UNIT_NAMES is cs.RESERVED_UNIT_NAMES
    assert sorted(cs.RESERVED_UNIT_NAMES) == sorted(RESERVED)


@pytest.mark.parametrize("reserved", RESERVED)
def test_新建的校验_保留名仍是409_bot_exists(roots, reserved):
    with pytest.raises(HubError) as e:
        hub_addbot._validate({"bot_id": reserved, "telegram_token": TOKEN, "dispatcher_port": 17990,
                              "chat_id": 1, "display_name": "新来的", "persona_text": "人设"})
    assert (e.value.status, e.value.error) == (409, "bot_exists")
    assert "已被新系统占用" in e.value.detail


# ------------------------------------------------------------ 单元名只有一个拼装入口

def test_单元名_合格的id照常拼():
    assert bl.label_of("goodbot") == "com.dsh-bot.goodbot"


@pytest.mark.parametrize("bot_id", UNQUALIFIED, ids=repr)
def test_单元名_不合格的id不给(bot_id):
    assert bl.label_of(bot_id) is None


@pytest.mark.parametrize("bot_id", RESERVED + [BAD_FILE, "goodbot\n", "jiwen\n"], ids=repr)
def test_四个动作_拿不到单元名就不调launchctl(roots, _fakes, bot_id):
    """kickstart / bootstrap / bootout 按这只失败，print 按「没在运行」。plist 就算在场也不动手。"""
    (roots.agents / ("com.dsh-bot.%s.plist" % bot_id.strip())).write_text("<plist/>", encoding="utf-8")
    assert bl.kickstart(bot_id) == ("failed", "")
    assert bl.bootstrap(bot_id) == ("failed", "")
    assert bl.bootout(bot_id) == ("failed", "")
    assert bl.print_state(bot_id) == (None, None)
    assert _fakes.launchctl.calls() == []


@pytest.mark.parametrize("bot_id", RESERVED + [BAD_FILE])
def test_状态列与判活_不合格的id一条print都不发(roots, _fakes, bot_id):
    assert bl.row_state(bot_id) == ("offline", None)
    assert bl.alive_now(bot_id)[0] is False
    assert _fakes.launchctl.calls() == []


@pytest.mark.parametrize("enabled", [True, False], ids=["start", "stop"])
def test_单只执行体_对保留名两个方向都是failed_零调用(roots, _fakes, enabled):
    state, code, tail = bl.sync_one("jiwen", enabled)
    assert (state, code) == ("failed", None)
    assert tail.splitlines()[0] == "重启 1 只：成功 0，失败 1"
    assert _fakes.launchctl.calls() == []


def test_多只编组_不合格的那只失败_合格的那只照常(roots, _fakes):
    state, _code, tail = bl.restart_group([("jiwen", False), ("goodbot", False)])
    assert state == "failed"
    lines = tail.splitlines()
    assert lines[0] == "重启 2 只：成功 1，失败 1"
    assert lines[1].startswith("jiwen 失败：launchctl bootout 失败") and lines[2] == "goodbot 已停止"
    assert _fakes.launchctl.calls() == ["bootout gui/%d/com.dsh-bot.goodbot" % os.getuid()]


# ------------------------------------------------------------ 名单的两个出口

def test_名单与来源_新系统根下文件名不合格的不算bot(roots):
    _write(roots.dsh, "goodbot")
    for stem in RESERVED + [BAD_FILE, "UPPER", "a", "has space", "goodbot ", "oddbot\n", "jiwen\n"]:
        _write(roots.dsh, stem)
    assert [e["id"] for e in bc.list_bots()] == ["goodbot"]
    assert bc.bot_sources() == {"goodbot": {"source": "dsh", "shadowed": False}}


def test_名单与来源_旧系统根下的文件不受这条影响(roots):
    _write(roots.legacy, "Old-Name")
    _write(roots.legacy, "jiwen")               # 旧系统那边叫什么名字不归这条管
    assert [e["id"] for e in bc.list_bots()] == ["Old-Name", "jiwen"]
    assert {k: v["source"] for k, v in bc.bot_sources().items()} == {
        "Old-Name": "legacy", "jiwen": "legacy"}


def test_名单_注入的名单不过滤(roots, monkeypatch, tmp_path):
    p = tmp_path / "bots.json"
    p.write_text('[{"id": "jiwen"}, {"id": "Bad-Name"}]', encoding="utf-8")
    monkeypatch.setenv("HUB_BOTS_FILE", str(p))
    assert [e["id"] for e in bc.list_bots()] == ["jiwen", "Bad-Name"]


def test_名单与来源_新系统那份不合格时_被它压住的同名旧系统那份也不在名单里(roots):
    """新系统那份压住了同名的旧系统那份，它自己又不算 bot：两处都不出现。
    页面在新系统根在场时本来也只列新系统的行，这里取的是保守的一边（见报告）。"""
    _write(roots.legacy, "jiwen")
    _write(roots.dsh, "jiwen")
    assert bc.list_bots() == [] and bc.bot_sources() == {}


def test_重启全部的范围_不含不合格的_哪怕它们写着停用(roots):
    _write(roots.dsh, "goodbot")
    _write(roots.dsh, "jiwen", enabled=False)
    _write(roots.dsh, BAD_FILE, enabled=False)
    assert bl.restart_pairs() == [("goodbot", True)]


# ------------------------------------------------------------ 放行闸：保留名不过

@pytest.mark.parametrize("reserved", RESERVED)
def test_放行闸_保留名不过_哪怕新系统根下有它的合法配置(roots, reserved):
    yml = _write(roots.dsh, reserved, display_name="保留名的")
    before = yml.read_bytes()
    _rejected(hub_addbot._edit_target, reserved)
    _rejected(hub_addbot.set_enabled, reserved, False)
    _rejected(hub_addbot.edit_read, reserved)
    _rejected(hub_addbot.edit_save, reserved, {"display_name": "想改成这个", "persona": "x"})
    assert yml.read_bytes() == before


@pytest.mark.parametrize("reserved", RESERVED)
def test_放行闸_保留名那份是坏的_补充口也不对它开(roots, reserved):
    (roots.dsh / ("%s.yml" % reserved)).write_text(BROKEN, encoding="utf-8")
    _rejected(hub_addbot._edit_target, reserved)
    _rejected(hub_addbot.set_enabled, reserved, False)
    assert (roots.dsh / ("%s.yml" % reserved)).read_text(encoding="utf-8") == BROKEN


def test_放行闸_保留名的判定排在形状之后(roots):
    """`jiwen` 加一个换行是形状错（400），不是「没有这个 bot」（404）。"""
    with pytest.raises(HubError) as e:
        hub_addbot._edit_target("jiwen\n")
    assert (e.value.status, e.value.error) == (400, "bad_bot_id")


# ------------------------------------------------------------ HTTP 层：列表与单只重启

@pytest.fixture
def client(roots):
    from flask import Flask

    from moments.hub_routes import hub_bp
    app = Flask(__name__)
    app.register_blueprint(hub_bp)
    app.config["TESTING"] = True
    return app.test_client()


def test_列表_三份配置_四个出口都只有合格那只_只对它问launchd(roots, client, _fakes):
    _write(roots.dsh, "goodbot")
    _write(roots.dsh, "jiwen", enabled=False)
    _write(roots.dsh, BAD_FILE, enabled=False)
    body = client.get("/hub/api/bots").get_json()
    assert [b["id"] for b in body["bots"]] == ["goodbot"]
    assert body["disabled"] == [] and sorted(body["sources"]) == ["goodbot"]
    assert body["overrides"] == []
    assert _fakes.launchctl.calls() == ["print gui/%d/com.dsh-bot.goodbot" % os.getuid()]


@pytest.mark.parametrize("reserved", RESERVED)
def test_单只重启_保留名_404_零调用(roots, client, _fakes, reserved):
    _write(roots.dsh, reserved)
    r = client.post("/hub/api/bots/%s/restart" % reserved)
    assert r.status_code == 404
    assert r.get_json() == {"error": "bot_not_found", "detail": hub_addbot.GATE_404_DETAIL}
    assert _fakes.launchctl.calls() == []
