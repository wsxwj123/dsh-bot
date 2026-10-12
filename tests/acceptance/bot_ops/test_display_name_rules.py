# -*- coding: utf-8 -*-
"""§0 第 9 条：显示名的字符规则，新建（§6）与编辑（§10）用同一套（接口约定第 8 稿）。

起因：此前只挡 U+0020 以下与 U+007F，别的控制字符能写进配置，把整份配置写坏；显示名里有
半个表情符号时，新建把配置文件建成空文件又不回滚，留下一只空配置的 bot，同名再也建不了。

规则：先去掉首尾空白，然后必须是 1 到 64 个字符，并且不含 U+0000–U+001F、U+007F–U+009F、
U+2028、U+2029、U+D800–U+DFFF（落单的代理）、U+FFFE、U+FFFF。其余一律放行。
不合规回 400 `bad_field`，磁盘上什么都不变。落盘的是去掉首尾空白之后的值。

本文件每类码位各一条，两个入口都测；违规的码位一律放在名字**中间**（放在首尾的空白类码位
会先被去掉，量不出规则）。
"""
import json

import pytest

from . import botops_util as u

BOT = "xiaoyu"
BAD_NAME = "display_name 必须是 1-64 字的字符串，且不含控制字符"
READBACK = "这份配置写出来之后读不回原样，多半是名字里有特殊字符"

#: 约定点名不许出现的码位，每类取边界上的
BAD = [
    pytest.param("\u0000", id="U+0000"),
    pytest.param("\u001f", id="U+001F"),
    pytest.param("\u007f", id="U+007F"),
    pytest.param("\u0080", id="U+0080"),
    pytest.param("\u0085", id="U+0085"),
    pytest.param("\u009f", id="U+009F"),
    pytest.param(" ", id="U+2028"),
    pytest.param(" ", id="U+2029"),
    pytest.param("\ud83d", id="lone-high-surrogate"),
    pytest.param("\udfff", id="lone-low-surrogate"),
    pytest.param("￾", id="U+FFFE"),
    pytest.param("￿", id="U+FFFF"),
]

FAMILY = "\U0001F468‍\U0001F469‍\U0001F467‍\U0001F466"      # 零宽连接符拼成的家庭表情

#: 该放行的：普通中文、表情、全角标点，以及紧挨着禁区的几个码位
GOOD = [
    pytest.param("小雨", id="plain-chinese"),
    pytest.param("小雨" + FAMILY, id="family-emoji-zwj"),
    pytest.param("小雨🌧️", id="emoji-with-variation-selector"),
    pytest.param("小雨（测试）！？，。", id="fullwidth-punct"),
    pytest.param("小 雨", id="inner-space"),
    pytest.param("小 雨", id="U+00A0-just-after-C1"),
    pytest.param("小‧雨", id="U+2027-just-before-LS"),
    pytest.param("小‪雨", id="U+202A-just-after-PS"),
    pytest.param("小雨", id="U+E000-just-after-surrogates"),
    pytest.param("小�雨", id="U+FFFD-just-before-FFFE"),
    pytest.param("ちゃん한글", id="other-scripts"),
]

#: 码位规则管不着、但写进配置时容易出岔子的名字。约定给的保证是二选一：
#: 要么落盘后读回来逐字相等，要么 400 且什么都不写（§0 第 9 条「写之前先读回来核对」）。
TRICKY = [
    pytest.param('小"雨"', id="double-quote"),
    pytest.param("小\\雨\\n", id="backslash"),
    pytest.param("小: 雨 #x", id="colon-hash"),
    pytest.param("'小雨'", id="single-quote"),
    pytest.param("{小雨}", id="braces"),
    pytest.param("- 小雨", id="dash-lead"),
    pytest.param("yes", id="yaml-bool-word"),
    pytest.param("null", id="yaml-null-word"),
    pytest.param("~", id="tilde"),
    pytest.param("0123", id="digits"),
    pytest.param("小﻿雨", id="U+FEFF-bom"),
    pytest.param("小­雨", id="U+00AD-soft-hyphen"),
    pytest.param("é", id="combining-mark"),
]


def _dumps(body):
    """标准库序列化：落单的代理写成 `\\ud83d` 这样的转义，照样发得出去。"""
    return json.dumps(body)


# ============================================================ 编辑入口

def _edit(client, name):
    return client.post("/hub/api/bots/%s/edit" % BOT, data=_dumps({"display_name": name}),
                       content_type="application/json")


@pytest.fixture
def editable(sandbox):
    """一只可编辑的新系统 bot，配置、人设、白名单俱全。返回 (配置文件, 频道目录)。"""
    yml = u.write_bot_yml(sandbox.dsh_cfg, BOT, display_name="旧名", dispatcher_port=17951)
    ch = sandbox.dsh_home / "bots" / BOT / "channel"
    ch.mkdir(parents=True)
    (ch / "CLAUDE.md").write_text("旧人设\n", encoding="utf-8")
    (ch / "access.json").write_text('{"allowFrom": [1]}', encoding="utf-8")
    return yml, ch


@pytest.mark.parametrize("ch", BAD)
def test_编辑_显示名里有禁用的码位_400_bad_field与固定文案(web_client, sandbox, editable, ch):
    r = _edit(web_client, "小" + ch + "雨")
    assert r.status_code == 400, (r.status_code, r.get_json())
    assert r.get_json() == {"error": "bad_field", "detail": BAD_NAME}, r.get_json()


@pytest.mark.parametrize("ch", BAD)
def test_编辑_显示名里有禁用的码位_磁盘上什么都不变(web_client, sandbox, editable, ch):
    yml, channel = editable
    frozen = u.freeze_roots(sandbox.dsh_cfg, channel)
    _edit(web_client, "小" + ch + "雨")
    u.assert_roots_untouched(frozen)
    assert u.read_yaml(yml)["display_name"] == "旧名"


@pytest.mark.parametrize("name", GOOD)
def test_编辑_合规的显示名_200且落盘后读回来逐字相等(web_client, sandbox, editable, name):
    yml, _ = editable
    r = _edit(web_client, name)
    assert r.status_code == 200, (r.status_code, r.get_json())
    assert u.read_yaml(yml)["display_name"] == name


def test_编辑_显示名首尾是制表符与换行_先去掉再判_照常保存(web_client, sandbox, editable):
    """规则的先后：先去掉首尾空白，再看有没有禁用的码位。"""
    yml, _ = editable
    r = _edit(web_client, "\t新名\n")
    assert r.status_code == 200, (r.status_code, r.get_json())
    assert u.read_yaml(yml)["display_name"] == "新名"


def test_编辑_家庭表情的显示名_读接口读回来逐字相等(web_client, sandbox, editable):
    name = "小雨" + FAMILY
    assert _edit(web_client, name).status_code == 200
    r = web_client.get("/hub/api/bots/%s/edit" % BOT)
    assert r.status_code == 200, (r.status_code, r.get_json())
    assert r.get_json()["display_name"] == name


@pytest.mark.parametrize("name", TRICKY)
def test_编辑_刁钻的显示名_要么读回逐字相等_要么400且配置不变(web_client, sandbox, editable, name):
    yml, _ = editable
    before = yml.read_bytes()
    r = _edit(web_client, name)
    if r.status_code == 200:
        assert u.read_yaml(yml)["display_name"] == name, yml.read_text(encoding="utf-8")
    else:
        assert r.status_code == 400, (r.status_code, r.get_json())
        assert r.get_json() == {"error": "bad_field", "detail": READBACK}, r.get_json()
        assert yml.read_bytes() == before, "被拒了，配置却变了"


@pytest.mark.parametrize("name", TRICKY)
def test_编辑_刁钻的显示名_配置里别的键不被带坏(web_client, sandbox, editable, name):
    """不管这次收没收下，配置文件都得还解析得出，原有的键一个不少。"""
    yml, _ = editable
    _edit(web_client, name)
    data = u.read_yaml(yml)
    assert data["id"] == BOT and data["dispatcher_port"] == 17951, yml.read_text(encoding="utf-8")


# ============================================================ 新建入口

def _payload(name, **over):
    body = {"bot_id": BOT, "telegram_token": u.FAKE_TOKEN, "dispatcher_port": u.free_port(),
            "chat_id": 123456789, "display_name": name, "persona_text": "我是小雨的人设正文。"}
    body.update(over)
    return body


def _create(client, name, **over):
    return client.post("/hub/api/bots", data=_dumps(_payload(name, **over)),
                       content_type="application/json")


def _roots(sandbox):
    return (sandbox.dsh_cfg, sandbox.dsh_home, sandbox.legacy_cfg, sandbox.channels, sandbox.agents)


@pytest.fixture
def creating(sandbox, bun, telegram, launchctl, as_darwin, monkeypatch):
    """新建流程要的假件全部在位：假 bun、假 launchctl、假 Telegram。探活上限收到 1 秒。"""
    u.tighten_constant(monkeypatch, value=30, new=1.0)
    telegram.mode = "ok"
    launchctl.set_default(installed=True, running=True, pid=4242)
    return type("Fakes", (), {"bun": bun, "telegram": telegram, "launchctl": launchctl})


@pytest.mark.parametrize("ch", BAD)
def test_新建_显示名里有禁用的码位_400_bad_field与固定文案(client, sandbox, creating, ch):
    r = _create(client, "小" + ch + "雨")
    assert r.status_code == 400, (r.status_code, r.get_json())
    assert r.get_json() == {"error": "bad_field", "detail": BAD_NAME}, r.get_json()


@pytest.mark.parametrize("ch", BAD)
def test_新建_显示名里有禁用的码位_磁盘零产物(client, sandbox, creating, ch):
    frozen = u.freeze_roots(*_roots(sandbox))
    _create(client, "小" + ch + "雨")
    u.assert_roots_untouched(frozen)


@pytest.mark.parametrize("ch", BAD)
def test_新建_显示名里有禁用的码位_零子进程零getMe(client, sandbox, creating, ch):
    _create(client, "小" + ch + "雨")
    assert creating.bun.calls() == [], ("被拒了还起了 bun", creating.bun.calls())
    assert creating.launchctl.calls() == [], ("被拒了还调了 launchctl", creating.launchctl.calls())
    assert creating.telegram.hits == [], ("被拒了还发了 getMe", creating.telegram.hits)


def test_新建_显示名不合规且同名配置已经在了_先答显示名的错(client, sandbox, creating):
    """§6 判定顺序：显示名的规则落在第一段，先于「已经有这只 bot」这类冲突。"""
    cfg = u.dsh_bot(sandbox.dsh_cfg, BOT)
    before = cfg.read_bytes()
    r = _create(client, "小\u2028雨")
    assert r.status_code == 400, (r.status_code, r.get_json())
    assert r.get_json() == {"error": "bad_field", "detail": BAD_NAME}, r.get_json()
    assert cfg.read_bytes() == before


@pytest.mark.parametrize("name", GOOD)
def test_新建_合规的显示名_201且落盘后读回来逐字相等(client, sandbox, creating, heartbeat, name):
    heartbeat.tick(BOT, pid=4242)
    r = _create(client, name)
    assert r.status_code == 201, (r.status_code, r.get_json())
    assert u.read_yaml(sandbox.dsh_cfg / ("%s.yml" % BOT))["display_name"] == name


def test_新建_显示名带首尾空格_落盘的是去掉之后的值(client, sandbox, creating, heartbeat):
    heartbeat.tick(BOT, pid=4242)
    r = _create(client, "  小雨  ")
    assert r.status_code == 201, (r.status_code, r.get_json())
    assert u.read_yaml(sandbox.dsh_cfg / ("%s.yml" % BOT))["display_name"] == "小雨"


def test_新建_显示名首尾是制表符与换行_先去掉再判_照常建成(client, sandbox, creating, heartbeat):
    """规则的先后：先去掉首尾空白，再看有没有禁用的码位。首尾的制表符、换行属于被去掉的那部分。"""
    heartbeat.tick(BOT, pid=4242)
    r = _create(client, "\t小雨\n")
    assert r.status_code == 201, (r.status_code, r.get_json())
    assert u.read_yaml(sandbox.dsh_cfg / ("%s.yml" % BOT))["display_name"] == "小雨"


@pytest.mark.parametrize("name", TRICKY)
def test_新建_刁钻的显示名_要么读回逐字相等_要么400且零产物(
        client, sandbox, creating, heartbeat, name):
    frozen = u.freeze_roots(sandbox.dsh_cfg, sandbox.legacy_cfg, sandbox.channels, sandbox.agents)
    heartbeat.tick(BOT, pid=4242)
    r = _create(client, name)
    cfg = sandbox.dsh_cfg / ("%s.yml" % BOT)
    if r.status_code == 201:
        data = u.read_yaml(cfg)
        assert data["display_name"] == name, cfg.read_text(encoding="utf-8")
        assert data["id"] == BOT, cfg.read_text(encoding="utf-8")
    else:
        assert r.status_code == 400, (r.status_code, r.get_json())
        assert r.get_json() == {"error": "bad_field", "detail": READBACK}, r.get_json()
        u.assert_roots_untouched(frozen)
        assert not (sandbox.dsh_home / "bots" / BOT).exists(), "被拒了还留下了 bot 家目录"
        assert creating.bun.calls() == [], creating.bun.calls()


# ============================================================ 第一段失败不留空配置，同名能再建

@pytest.mark.parametrize("over", [
    pytest.param({"display_name": "小\ud83d雨"}, id="name-lone-surrogate"),
    pytest.param({"display_name": "小 雨"}, id="name-U+2028"),
    pytest.param({"display_name": "小\u0085雨"}, id="name-U+0085"),
    pytest.param({"persona_text": "半个\ud83d表情"}, id="persona-lone-surrogate"),
    pytest.param({"persona_text": "字" * 65537}, id="persona-too-long"),
])
def test_新建_第一段失败之后_新系统配置根下没有这只的配置(client, sandbox, creating, over):
    """此前显示名里有半个表情符号时，配置被建成空文件又不在回滚清单里。"""
    r = client.post("/hub/api/bots", data=_dumps(_payload("小雨", **over)),
                    content_type="application/json")
    assert r.status_code in (400, 413), (r.status_code, r.get_json())
    assert not (sandbox.dsh_cfg / ("%s.yml" % BOT)).exists(), "失败之后留下了配置文件"
    assert not (sandbox.dsh_home / "bots" / BOT).exists(), "失败之后留下了 bot 家目录"


@pytest.mark.parametrize("over", [
    pytest.param({"display_name": "小\ud83d雨"}, id="name-lone-surrogate"),
    pytest.param({"display_name": "小 雨"}, id="name-U+2028"),
    pytest.param({"persona_text": "半个\ud83d表情"}, id="persona-lone-surrogate"),
])
def test_新建_第一段失败之后_换合规的内容用同一个id再建一次能成功(
        client, sandbox, creating, heartbeat, over):
    first = client.post("/hub/api/bots", data=_dumps(_payload("小雨", **over)),
                        content_type="application/json")
    assert first.status_code == 400, (first.status_code, first.get_json())
    heartbeat.tick(BOT, pid=4242)
    r = _create(client, "小雨")
    assert r.status_code == 201, ("同一个 bot_id 再也建不了了", r.status_code, r.get_json())
    assert u.read_yaml(sandbox.dsh_cfg / ("%s.yml" % BOT))["display_name"] == "小雨"
