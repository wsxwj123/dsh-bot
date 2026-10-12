# -*- coding: utf-8 -*-
"""§0 第 7 条（接口约定第 7 稿）：合格的 bot id = 形状合法，并且不是保留名。

依据 `INTERFACE-新系统bot运维.md` §0 第 2 条新加的那一句与第 7 条、§2「不算 bot 的配置」、
§3「第 7 稿两点」、§4b 那张表的最后一行：

- 新系统配置根下，文件名去掉 `.yml` 之后不是合格 bot id 的配置**不算 bot**：不进列表，
  不进重启全部的执行范围，过不了放行闸；
- 对它零 launchctl 调用，只读的 print 也不发；
- 保留名（`jiwen`、`director`、`voicecall`）哪怕在新系统配置根下有一份配置，对它调开关、
  编辑、单只重启得到的都是 404，那份文件字节不变。

保留名在这里是被测对象本身，所以本文件**每一条**用例都在假 launchctl 与假 bun 在位的
前提下跑（`_fakes_in_place`），并且每条都断言两个假件的调用记录里没有它们的单元名。
"""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from . import botops_util as u
from .conftest import DispatcherStub

GOOD = "goodbot"                    # 合格的假名
BAD_FILE = "Bad-Name"               # 文件名形状非法（大写、连字符）
GOOD_UNIT = "com.dsh-bot.%s" % GOOD
RESERVED = list(u.RESERVED_IDS)

#: 别的不合格文件名（去掉 .yml 之后的部分）。最后三个是「看着像合格名字、多了一点东西」的
ODD_STEMS = [
    pytest.param("UPPER", id="uppercase"),
    pytest.param("has-dash", id="hyphen"),
    pytest.param("a", id="one-char"),
    pytest.param("1abc", id="digit-first"),
    pytest.param("x" * 33, id="33-chars"),
    pytest.param("has space", id="space-inside"),
    pytest.param(GOOD + " ", id="good-name-trailing-space"),
    pytest.param("oddbot\n", id="trailing-newline"),
    pytest.param("jiwen\n", id="reserved-trailing-newline"),
]


@pytest.fixture(autouse=True)
def _fakes_in_place(launchctl, bun, as_darwin, monkeypatch):
    """每条用例：假 launchctl、假 bun 都在位，平台钉成 macOS，重启等待上限收到 3 秒。"""
    u.tighten_constant(monkeypatch, value=45, new=3.0)
    launchctl.set_default(installed=True, running=True, pid=5555)


@pytest.fixture
def probes():
    """两个本机假 dispatcher：把不合格那两份配置的端口指过来，谁对它们发了 HTTP 探活就留痕。"""
    a, b = DispatcherStub().start(), DispatcherStub().start()
    try:
        yield SimpleNamespace(reserved=a, bad=b)
    finally:
        a.stop()
        b.stop()


@pytest.fixture
def three(sandbox, probes):
    """新系统配置根下三份内容都合法的配置：合格的、保留名的、文件名形状非法的。都没有心跳。

    `others_enabled` 给了就把不合格那两份的 `enabled` 写成它（停用时重启全部会想发 bootout）。
    """
    def _make(reserved="jiwen", others_enabled=None):
        extra = {} if others_enabled is None else {"enabled": others_enabled}
        good = u.write_bot_yml(sandbox.dsh_cfg, GOOD, display_name="合格的",
                               dispatcher_port=u.free_port())
        res = u.write_bot_yml(sandbox.dsh_cfg, reserved, display_name="保留名的",
                              dispatcher_port=probes.reserved.port, **extra)
        bad = u.write_bot_yml(sandbox.dsh_cfg, BAD_FILE, display_name="坏名字的",
                              dispatcher_port=probes.bad.port, **extra)
        return SimpleNamespace(good=good, reserved=res, bad=bad, reserved_id=reserved)
    return _make


def _odd_cfg(sandbox, stem, **fields):
    """新系统配置根下写一份内容合法、文件名是 `<stem>.yml` 的配置（stem 可以带空格、换行）。"""
    fields.setdefault("display_name", "怪名字")
    fields.setdefault("dispatcher_port", u.free_port())
    lines = ["id: %s" % stem.strip()]
    for k in sorted(fields):
        v = fields[k]
        lines.append("%s: %s" % (k, ("true" if v else "false") if isinstance(v, bool) else v))
    p = sandbox.dsh_cfg / (stem + ".yml")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert (stem + ".yml") in [e.name for e in sandbox.dsh_cfg.iterdir()], \
        "夹具自检：这个怪名字的配置文件没建出来（文件系统不收这种名字？）"
    return p


def _never_touched(launchctl, bun, *more):
    """保留名与坏名字的单元，两个假件里一次都没被提到；bun 整个没被调过。"""
    assert bun.calls() == [], ("本文件没有新建流程，bun 不该被调", bun.calls())
    u.assert_units_never_named(launchctl, *u.RESERVED_IDS, BAD_FILE, *more)
    u.assert_units_never_named(bun, *u.RESERVED_IDS, BAD_FILE, *more)


def _get_bots(client):
    r = client.get("/hub/api/bots")
    assert r.status_code == 200, (r.status_code, r.get_json(silent=True))
    return r.get_json()


def _restart_all_done(client):
    r = client.post("/hub/api/bots/restart")
    assert r.status_code == 202, (r.status_code, r.get_json(silent=True))
    return u.wait_job(client, r.get_json()["job_id"])


def _body(r):
    j = r.get_json(silent=True)
    return j if isinstance(j, dict) else {"_不是JSON对象": r.get_data(as_text=True)[:200]}


# ============================================================ §2 列表

@pytest.mark.parametrize("reserved", RESERVED)
def test_列表_合格的保留名的坏名字的各一份_bots里只有合格那只(
        client, sandbox, three, launchctl, bun, reserved):
    three(reserved)
    body = _get_bots(client)
    assert [b["id"] for b in body["bots"]] == [GOOD], [b["id"] for b in body["bots"]]
    _never_touched(launchctl, bun)


def test_列表_不合格的两份写着停用_disabled里也没有它们(client, sandbox, three, launchctl, bun):
    three(others_enabled=False)
    body = _get_bots(client)
    assert body["disabled"] == [], ("不算 bot 的配置进了停用名单", body["disabled"])
    _never_touched(launchctl, bun)


def test_列表_三份配置_sources里只有合格那只(client, sandbox, three, launchctl, bun):
    three()
    sources = _get_bots(client)["sources"]
    assert sorted(sources) == [GOOD], sorted(sources)
    assert sources[GOOD].get("source") == "dsh", sources
    _never_touched(launchctl, bun)


def test_列表_三份配置_overrides里没点到不合格的两份(client, sandbox, three, launchctl, bun):
    three()
    text = json.dumps(_get_bots(client)["overrides"], ensure_ascii=False)
    assert "jiwen" not in text and BAD_FILE not in text, text
    _never_touched(launchctl, bun)


@pytest.mark.parametrize("reserved", RESERVED)
def test_列表_三份配置_只对合格那只查launchd_另两只连print都不发(
        client, sandbox, three, launchctl, bun, reserved):
    """三只都没有新鲜心跳：合格那只要落到「问 launchd」那一档，另两只不许问。"""
    three(reserved)
    body = _get_bots(client)
    assert set(launchctl.labels()) == {GOOD_UNIT}, ("launchctl 的调用对象不对", launchctl.calls())
    assert set(launchctl.subcommands()) == {"print"}, launchctl.calls()
    assert [b["state"] for b in body["bots"] if b["id"] == GOOD] == ["starting"], body["bots"]
    _never_touched(launchctl, bun)


def test_列表_三份配置_不对不合格的两份发本机探活(client, sandbox, three, probes, launchctl, bun):
    three()
    _get_bots(client)
    assert probes.reserved.hits == [], ("对保留名那份发了本机探活", probes.reserved.hits)
    assert probes.bad.hits == [], ("对坏名字那份发了本机探活", probes.bad.hits)
    _never_touched(launchctl, bun)


@pytest.mark.parametrize("stem", ODD_STEMS)
def test_列表_别的不合格文件名_不进列表也不问launchd(client, sandbox, launchctl, bun, stem):
    u.write_bot_yml(sandbox.dsh_cfg, GOOD, display_name="合格的", dispatcher_port=u.free_port())
    _odd_cfg(sandbox, stem)
    body = _get_bots(client)
    assert [b["id"] for b in body["bots"]] == [GOOD], [b["id"] for b in body["bots"]]
    assert sorted(body["sources"]) == [GOOD], sorted(body["sources"])
    assert set(launchctl.labels()) == {GOOD_UNIT}, launchctl.calls()
    _never_touched(launchctl, bun)


# ============================================================ §3 重启全部

@pytest.mark.parametrize("reserved", RESERVED)
def test_重启全部_三份配置都启用_tail首行是重启1只_只有合格那只(
        client, sandbox, three, heartbeat, launchctl, bun, reserved):
    three(reserved)
    launchctl.set_default(installed=True, running=False, pid=4242)
    heartbeat.tick(GOOD, pid=4242)
    st = _restart_all_done(client)
    assert st["tail"].strip().splitlines()[0] == "重启 1 只：成功 1", st["tail"]
    assert st["state"] == "done", st
    _never_touched(launchctl, bun)


@pytest.mark.parametrize("reserved", RESERVED)
def test_重启全部_三份配置都启用_假launchctl只记到合格那只(
        client, sandbox, three, heartbeat, launchctl, bun, reserved):
    three(reserved)
    launchctl.set_default(installed=True, running=False, pid=4242)
    heartbeat.tick(GOOD, pid=4242)
    _restart_all_done(client)
    assert set(launchctl.labels()) == {GOOD_UNIT}, launchctl.calls()
    assert "kickstart" in launchctl.subcommands(), launchctl.calls()
    _never_touched(launchctl, bun)


@pytest.mark.parametrize("reserved", RESERVED)
def test_重启全部_不合格的两份写着停用_不对它们发bootout(
        client, sandbox, three, heartbeat, launchctl, bun, reserved):
    """最要紧的一条：`jiwen.yml` 里写着 `enabled: false` 时，重启全部不许把 `com.dsh-bot.jiwen`
    这个不属于 bot 的单元停掉。"""
    three(reserved, others_enabled=False)
    launchctl.set_default(installed=True, running=False, pid=4242)
    heartbeat.tick(GOOD, pid=4242)
    st = _restart_all_done(client)
    assert "bootout" not in launchctl.subcommands(), ("对不算 bot 的配置发了 bootout", launchctl.calls())
    assert set(launchctl.labels()) == {GOOD_UNIT}, launchctl.calls()
    assert st["tail"].strip().splitlines()[0] == "重启 1 只：成功 1", st["tail"]
    _never_touched(launchctl, bun)


def test_重启全部_配置根里只有不合格的两份_按名单为空处理_一条launchctl都不发(
        client, sandbox, probes, launchctl, bun):
    u.write_bot_yml(sandbox.dsh_cfg, "jiwen", display_name="保留名的",
                    dispatcher_port=probes.reserved.port)
    u.write_bot_yml(sandbox.dsh_cfg, BAD_FILE, display_name="坏名字的",
                    dispatcher_port=probes.bad.port, enabled=False)
    st = _restart_all_done(client)
    assert launchctl.calls() == [], launchctl.calls()
    assert st["tail"].strip() == "没有可重启的 bot", st["tail"]
    assert st["state"] == "done", st
    _never_touched(launchctl, bun)


def test_重启全部_配置根里混着各种不合格文件名_只重启合格那只(
        client, sandbox, heartbeat, launchctl, bun):
    u.write_bot_yml(sandbox.dsh_cfg, GOOD, display_name="合格的", dispatcher_port=u.free_port())
    for i, param in enumerate(ODD_STEMS):
        _odd_cfg(sandbox, param.values[0], enabled=bool(i % 2))    # 启用、停用交替着放
    launchctl.set_default(installed=True, running=False, pid=4242)
    heartbeat.tick(GOOD, pid=4242)
    st = _restart_all_done(client)
    assert set(launchctl.labels()) == {GOOD_UNIT}, launchctl.calls()
    assert "bootout" not in launchctl.subcommands(), launchctl.calls()
    assert st["tail"].strip().splitlines()[0] == "重启 1 只：成功 1", st["tail"]
    _never_touched(launchctl, bun)


# ============================================================ §4b 开关：保留名 404

@pytest.mark.parametrize("reserved", RESERVED)
def test_开关_保留名_请求停用_404且那份文件不动_零调用零任务(
        client, sandbox, three, launchctl, bun, monkeypatch, reserved):
    three(reserved)
    frozen = u.freeze_roots(sandbox.dsh_cfg, sandbox.legacy_cfg)
    r = client.post("/hub/api/bots/%s/enabled" % reserved, json={"enabled": False})
    u.assert_gate_rejected(r, frozen, launchctl, client, monkeypatch, sandbox.tmp)
    _never_touched(launchctl, bun)


@pytest.mark.parametrize("reserved", RESERVED)
def test_开关_保留名那份写着停用_请求启用_404且那份文件不动_零调用零任务(
        client, sandbox, three, launchctl, bun, monkeypatch, reserved):
    three(reserved, others_enabled=False)
    frozen = u.freeze_roots(sandbox.dsh_cfg, sandbox.legacy_cfg)
    r = client.post("/hub/api/bots/%s/enabled" % reserved, json={"enabled": True})
    u.assert_gate_rejected(r, frozen, launchctl, client, monkeypatch, sandbox.tmp)
    _never_touched(launchctl, bun)


@pytest.mark.parametrize("reserved", RESERVED)
def test_开关_保留名那份配置是坏的_也是404不是bad_yaml(
        client, sandbox, launchctl, bun, monkeypatch, reserved):
    """§0 第 2 条：「坏文件」补充口不对保留名开。"""
    (sandbox.dsh_cfg / ("%s.yml" % reserved)).write_text("id: [oops\n  x: : :\n", encoding="utf-8")
    frozen = u.freeze_roots(sandbox.dsh_cfg, sandbox.legacy_cfg)
    r = client.post("/hub/api/bots/%s/enabled" % reserved, json={"enabled": False})
    u.assert_gate_rejected(r, frozen, launchctl, client, monkeypatch, sandbox.tmp)
    _never_touched(launchctl, bun)


def test_开关_保留名_设了覆盖命令也不执行(client, sandbox, three, launchctl, bun, monkeypatch):
    three("jiwen")
    marker = sandbox.tmp / "override-ran.marker"
    monkeypatch.setenv("BOTOPS_MARKER", str(marker))
    monkeypatch.setenv("HUB_RESTART_CMD", u.python_cmd(
        "import os,time;open(os.environ['BOTOPS_MARKER'],'w').write('ran');time.sleep(1.5)"))
    frozen = u.freeze_roots(sandbox.dsh_cfg, sandbox.legacy_cfg)
    r = client.post("/hub/api/bots/jiwen/enabled", json={"enabled": False})
    assert r.status_code == 404, (r.status_code, _body(r))
    assert _body(r) == {"error": "bot_not_found", "detail": u.GATE_404_DETAIL}, _body(r)
    u.assert_no_job_left(client, monkeypatch, sandbox.tmp)
    assert not marker.exists(), "保留名被拒了，覆盖命令却还是跑了"
    u.assert_roots_untouched(frozen)
    assert launchctl.calls() == [], launchctl.calls()
    _never_touched(launchctl, bun)


# ============================================================ §4 单只重启：保留名 404

def _assert_restart_404(r, launchctl, client, monkeypatch, tmp):
    assert r.status_code == 404, (r.status_code, _body(r))
    body = _body(r)
    assert body.get("error") == "bot_not_found", body
    assert "新系统" in (body.get("detail") or ""), body
    assert "job_id" not in body, body
    assert launchctl.calls() == [], ("404 了还调了 launchctl", launchctl.calls())
    u.assert_no_job_left(client, monkeypatch, tmp)
    assert launchctl.calls() == [], launchctl.calls()


@pytest.mark.parametrize("reserved", RESERVED)
def test_单只重启_保留名_404且那份文件不动_零调用零任务(
        client, sandbox, three, launchctl, bun, monkeypatch, reserved):
    three(reserved)
    frozen = u.freeze_roots(sandbox.dsh_cfg, sandbox.legacy_cfg)
    r = client.post("/hub/api/bots/%s/restart" % reserved)
    _assert_restart_404(r, launchctl, client, monkeypatch, sandbox.tmp)
    u.assert_roots_untouched(frozen)
    _never_touched(launchctl, bun)


def test_单只重启_保留名那份写着停用_是404不是bot_disabled(
        client, sandbox, three, launchctl, bun, monkeypatch):
    """404 这一格排在「停用中」之前：保留名不是这一页的 bot，不该被劝「先启用再重启」。"""
    three("jiwen", others_enabled=False)
    r = client.post("/hub/api/bots/jiwen/restart")
    _assert_restart_404(r, launchctl, client, monkeypatch, sandbox.tmp)
    _never_touched(launchctl, bun)


# ============================================================ §10 编辑：保留名 404

RESERVED_PERSONA = "保留名家目录里的人设 RESERVED-PERSONA-7d1e"


def _reserved_channel(sandbox, reserved):
    ch = sandbox.dsh_home / "bots" / reserved / "channel"
    ch.mkdir(parents=True)
    (ch / "CLAUDE.md").write_text(RESERVED_PERSONA, encoding="utf-8")
    (ch / "access.json").write_text('{"allowFrom": [1], "voiceId": "reserved-voice"}\n',
                                    encoding="utf-8")
    return ch


@pytest.mark.parametrize("reserved", RESERVED)
def test_编辑读_保留名_404且不把它家里的人设读出来(
        web_client, sandbox, three, launchctl, bun, reserved):
    three(reserved)
    _reserved_channel(sandbox, reserved)
    r = web_client.get("/hub/api/bots/%s/edit" % reserved)
    assert r.status_code == 404, (r.status_code, _body(r))
    assert _body(r) == {"error": "bot_not_found", "detail": u.GATE_404_DETAIL}, _body(r)
    assert RESERVED_PERSONA not in r.get_data(as_text=True), "保留名家里的人设被读成了响应内容"
    assert launchctl.calls() == [], launchctl.calls()
    _never_touched(launchctl, bun)


@pytest.mark.parametrize("reserved", RESERVED)
def test_编辑写_保留名_404且配置与家目录都不动_朋友圈库不写(
        web_client, sandbox, three, launchctl, bun, botlife_db, reserved):
    three(reserved)
    _reserved_channel(sandbox, reserved)
    frozen = u.freeze_roots(sandbox.dsh_cfg, sandbox.dsh_home, sandbox.legacy_cfg)
    r = web_client.post("/hub/api/bots/%s/edit" % reserved,
                        json={"display_name": "想改成这个", "persona": "想换的人设",
                              "fish_voice_id": "v9"})
    assert r.status_code == 404, (r.status_code, _body(r))
    assert _body(r) == {"error": "bot_not_found", "detail": u.GATE_404_DETAIL}, _body(r)
    u.assert_roots_untouched(frozen)
    con = sqlite3.connect(str(botlife_db.DB_PATH))
    try:
        names = [row[0] for row in con.execute("SELECT display_name FROM bot_profiles")]
    finally:
        con.close()
    assert "想改成这个" not in names, ("被拒的编辑写进了朋友圈库", names)
    assert launchctl.calls() == [], launchctl.calls()
    _never_touched(launchctl, bun)


def test_编辑读_保留名那份配置是坏的_也是404不是bad_yaml(web_client, sandbox, launchctl, bun):
    (sandbox.dsh_cfg / "jiwen.yml").write_text("id: [oops\n  x: : :\n", encoding="utf-8")
    r = web_client.get("/hub/api/bots/jiwen/edit")
    assert r.status_code == 404, (r.status_code, _body(r))
    assert _body(r) == {"error": "bot_not_found", "detail": u.GATE_404_DETAIL}, _body(r)
    _never_touched(launchctl, bun)


# ============================================================ 对照：合格那只不被连累

def test_三份配置并存_合格那只的单只重启照常受理_只动它(
        client, sandbox, three, heartbeat, launchctl, bun):
    three()
    launchctl.set_default(installed=True, running=False, pid=4242)
    heartbeat.tick(GOOD, pid=4242)
    r = client.post("/hub/api/bots/%s/restart" % GOOD)
    assert r.status_code == 202, (r.status_code, _body(r))
    st = u.wait_job(client, r.get_json()["job_id"])
    assert st["state"] == "done", st
    assert set(launchctl.labels()) == {GOOD_UNIT}, launchctl.calls()
    _never_touched(launchctl, bun)


def test_三份配置并存_合格那只的开关照常200_只动它(client, sandbox, three, launchctl, bun):
    cfgs = three()
    frozen = u.freeze_roots(sandbox.legacy_cfg)
    before = (cfgs.reserved.read_bytes(), cfgs.bad.read_bytes())
    r = client.post("/hub/api/bots/%s/enabled" % GOOD, json={"enabled": False})
    assert r.status_code == 200, (r.status_code, _body(r))
    st = u.finish_sync(client, r.get_json())
    assert st is not None and st["state"] == "done", (r.get_json(), st)
    assert u.read_yaml(cfgs.good)["enabled"] is False, cfgs.good.read_text(encoding="utf-8")
    assert (cfgs.reserved.read_bytes(), cfgs.bad.read_bytes()) == before, "旁边两份配置被动过"
    # 第 8 稿 §5c：停用方向恰好两条 bootout，先主单元、后主动消息的定时单元
    assert launchctl.labels() == [GOOD_UNIT, "com.dsh-bot.self-initiate.%s" % GOOD], launchctl.calls()
    u.assert_roots_untouched(frozen)
    _never_touched(launchctl, bun)
