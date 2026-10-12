# -*- coding: utf-8 -*-
"""§6/§7 新建 bot（接口约定第 8 稿）：人设长度按字符数、模板超限与模板编码、频道目录写绝对路径。

- 人设正文上限 65536 个**字符**，新建与编辑同一把尺（此前新建的正文按 UTF-8 字节数算）；
- 选了模板而模板超过 65536 个字符：413，不再悄悄截断；模板不是合法 UTF-8：400，不再 500；
- 配置里的 `bot_channel_path` 写绝对路径，不再收成 `~/…`（§7、§14 第 4 条）。

显示名的字符规则与「失败不留空配置」在 test_display_name_rules.py。
"""
import json

import pytest

from . import botops_util as u

BOT = "xiaoyu"
TPL = "tplbot"

TOO_LONG = "人设正文超过 65536 个字符"
BROKEN_PERSONA = "人设正文里有残缺的字符（多半是半个表情符号），删掉它再保存"
TPL_TOO_LONG = "这个人设模板超过 65536 个字符，换一个或直接贴正文"
TPL_NOT_UTF8 = "这个人设模板不是合法的 UTF-8 文本，换一个或直接贴正文"


def _payload(**over):
    body = {"bot_id": BOT, "telegram_token": u.FAKE_TOKEN, "dispatcher_port": u.free_port(),
            "chat_id": 123456789, "display_name": "小雨", "persona_text": "我是小雨的人设正文。"}
    body.update(over)
    return body


def _create(client, **over):
    return client.post("/hub/api/bots", data=json.dumps(_payload(**over)),
                       content_type="application/json")


def _create_from_template(client, tpl=TPL):
    body = _payload(persona_template=tpl)
    del body["persona_text"]
    return client.post("/hub/api/bots", data=json.dumps(body), content_type="application/json")


def _persona_file(sandbox, dsh_home=None):
    return (dsh_home or sandbox.dsh_home) / "bots" / BOT / "channel" / "CLAUDE.md"


def _dsh_template(sandbox, raw):
    """新系统来源的模板：`<DSH_BOT_HOME>/bots/<模板 id>/channel/CLAUDE.md`。"""
    d = sandbox.dsh_home / "bots" / TPL / "channel"
    d.mkdir(parents=True)
    (d / "CLAUDE.md").write_bytes(raw)


def _legacy_template(sandbox, raw):
    """旧根来源的模板：`<HUB_CHANNELS_DIR>/<模板 id>/CLAUDE.md`。"""
    d = sandbox.channels / TPL
    d.mkdir(parents=True)
    (d / "CLAUDE.md").write_bytes(raw)


def _roots(sandbox):
    return (sandbox.dsh_cfg, sandbox.dsh_home, sandbox.legacy_cfg, sandbox.channels, sandbox.agents)


@pytest.fixture(autouse=True)
def fakes(request, sandbox, bun, telegram, launchctl, as_darwin, monkeypatch):
    """新建流程要的假件全部在位：假 bun、假 launchctl、假 Telegram。

    探活上限（§1：30 秒，测试可改）只对「应当被拒」的用例收到 1 秒：实现没按约定拒掉、真的
    走到等它启动那一步时，红用例不用陪着等满。要它建成的用例（取用了 heartbeat、让心跳一直
    新鲜的那几条）不收，在约定的原值下跑：这几条实测每条本身就要 1 秒上下（假件头一次被执行
    就要零点几秒，见 botops_util.tightened 的说明），1 秒的上限正顶在它头上，机器一忙就可能
    偶发 207。
    """
    if "heartbeat" not in request.fixturenames:
        u.tighten_constant(monkeypatch, value=30, new=1.0)
    telegram.mode = "ok"
    launchctl.set_default(installed=True, running=True, pid=4242)
    return type("Fakes", (), {"bun": bun, "telegram": telegram, "launchctl": launchctl})


def _assert_nothing_happened(fakes, frozen):
    u.assert_roots_untouched(frozen)
    assert fakes.bun.calls() == [], ("被拒了还起了 bun", fakes.bun.calls())
    assert fakes.launchctl.calls() == [], ("被拒了还调了 launchctl", fakes.launchctl.calls())
    assert fakes.telegram.hits == [], ("被拒了还发了 getMe", fakes.telegram.hits)


# ============================================================ 人设正文：按字符数

def test_新建_人设65537个汉字_413与固定文案(client, sandbox):
    r = _create(client, persona_text="字" * 65537)
    assert r.status_code == 413, (r.status_code, r.get_json())
    assert r.get_json() == {"error": "payload_too_large", "detail": TOO_LONG}, r.get_json()


def test_新建_人设65537个汉字_零产物零子进程零getMe(client, sandbox, fakes):
    frozen = u.freeze_roots(*_roots(sandbox))
    _create(client, persona_text="字" * 65537)
    _assert_nothing_happened(fakes, frozen)


def test_新建_人设恰好65536个汉字_建成且正文完整落盘(client, sandbox, heartbeat):
    """65536 个汉字是 196608 字节：上限按字符数，不按字节数。"""
    heartbeat.tick(BOT, pid=4242)
    text = "字" * 65536
    r = _create(client, persona_text=text)
    assert r.status_code == 201, (r.status_code, r.get_json())
    assert text in _persona_file(sandbox).read_text(encoding="utf-8")


def test_新建_人设21846个汉字_建成(client, sandbox, heartbeat):
    """21846 个汉字是 65538 字节，此前按字节数算会被拒。"""
    heartbeat.tick(BOT, pid=4242)
    r = _create(client, persona_text="字" * 21846)
    assert r.status_code == 201, (r.status_code, r.get_json())


@pytest.mark.parametrize("lone", ["\ud83d", "\ude00"], ids=["high-surrogate", "low-surrogate"])
def test_新建_人设里有落单的代理_400_bad_field与固定文案(client, sandbox, lone):
    r = _create(client, persona_text="前半句" + lone + "后半句")
    assert r.status_code == 400, (r.status_code, r.get_json())
    assert r.get_json() == {"error": "bad_field", "detail": BROKEN_PERSONA}, r.get_json()


def test_新建_人设里有落单的代理_零产物零子进程零getMe(client, sandbox, fakes):
    frozen = u.freeze_roots(*_roots(sandbox))
    _create(client, persona_text="前半句\ud83d后半句")
    _assert_nothing_happened(fakes, frozen)


def test_新建_人设超长且同名配置已经在了_先答413(client, sandbox):
    """§6 判定顺序：长度的规则落在第一段，先于「已经有这只 bot」这类冲突。"""
    u.dsh_bot(sandbox.dsh_cfg, BOT)
    r = _create(client, persona_text="字" * 65537)
    assert r.status_code == 413, (r.status_code, r.get_json())
    assert r.get_json()["detail"] == TOO_LONG, r.get_json()


# ============================================================ 模板：超限不再悄悄截断

def test_新建_模板超过65536个字符_413与固定文案(client, sandbox):
    _dsh_template(sandbox, ("字" * 65537).encode("utf-8"))
    r = _create_from_template(client)
    assert r.status_code == 413, (r.status_code, r.get_json())
    assert r.get_json() == {"error": "payload_too_large", "detail": TPL_TOO_LONG}, r.get_json()


def test_新建_模板超过65536个字符_零产物且模板文件不动(client, sandbox, fakes):
    _dsh_template(sandbox, ("字" * 65537).encode("utf-8"))
    frozen = u.freeze_roots(*_roots(sandbox))
    _create_from_template(client)
    _assert_nothing_happened(fakes, frozen)


def test_新建_旧根的模板超过65536个字符_同样413(client, sandbox):
    _legacy_template(sandbox, ("字" * 65537).encode("utf-8"))
    r = _create_from_template(client)
    assert r.status_code == 413, (r.status_code, r.get_json())
    assert r.get_json()["detail"] == TPL_TOO_LONG, r.get_json()


def test_新建_模板恰好65536个字符_建成且正文一个字没少(client, sandbox, heartbeat):
    text = "字" * 65535 + "尾"
    _dsh_template(sandbox, text.encode("utf-8"))
    heartbeat.tick(BOT, pid=4242)
    r = _create_from_template(client)
    assert r.status_code == 201, (r.status_code, r.get_json())
    assert text in _persona_file(sandbox).read_text(encoding="utf-8")


def test_新建_模板超限且同名配置已经在了_先答413(client, sandbox):
    _dsh_template(sandbox, ("字" * 65537).encode("utf-8"))
    u.dsh_bot(sandbox.dsh_cfg, BOT)
    r = _create_from_template(client)
    assert r.status_code == 413, (r.status_code, r.get_json())
    assert r.get_json()["detail"] == TPL_TOO_LONG, r.get_json()


# ============================================================ 模板：编码坏了

@pytest.mark.parametrize("put", [_dsh_template, _legacy_template], ids=["dsh-template", "legacy-template"])
def test_新建_模板不是合法UTF8_400_bad_field与固定文案(client, sandbox, put):
    put(sandbox, b"\xff\xfe\x00not utf8 \x80")
    r = _create_from_template(client)
    assert r.status_code == 400, (r.status_code, r.get_json())
    assert r.get_json() == {"error": "bad_field", "detail": TPL_NOT_UTF8}, r.get_json()


def test_新建_模板不是合法UTF8_零产物零子进程零getMe(client, sandbox, fakes):
    _dsh_template(sandbox, b"\xff\xfe\x00not utf8 \x80")
    frozen = u.freeze_roots(*_roots(sandbox))
    _create_from_template(client)
    _assert_nothing_happened(fakes, frozen)


# ============================================================ 频道目录写绝对路径（§7、§14 第 4 条）

@pytest.fixture
def home_under_home(sandbox, monkeypatch):
    """把新系统家目录放到（假的）HOME 底下，就像真机上的 `~/.dsh-bot`。仍然全在 tmp_path 里。

    只有这样才量得出「收成 `~/…`」：家目录不在 HOME 底下时，本来就收不成波浪号的样子。
    """
    dsh_home = sandbox.home / ".dsh-bot"
    dsh_home.mkdir()
    assert str(dsh_home).startswith(str(sandbox.tmp)), "新系统家目录没落在 tmp_path 下"
    monkeypatch.setenv("DSH_BOT_HOME", str(dsh_home))
    return dsh_home


def test_新建_家目录在HOME底下_配置里的频道目录不以波浪号开头(
        client, sandbox, heartbeat, home_under_home):
    heartbeat.tick(BOT, pid=4242, home=home_under_home)
    r = _create(client)
    assert r.status_code == 201, (r.status_code, r.get_json())
    got = u.read_yaml(sandbox.dsh_cfg / ("%s.yml" % BOT))["bot_channel_path"]
    assert not str(got).startswith("~"), "bot_channel_path 还是波浪号的写法：%r" % got


def test_新建_家目录在HOME底下_配置里的频道目录等于展开之后的绝对路径(
        client, sandbox, heartbeat, home_under_home):
    heartbeat.tick(BOT, pid=4242, home=home_under_home)
    r = _create(client)
    assert r.status_code == 201, (r.status_code, r.get_json())
    got = u.read_yaml(sandbox.dsh_cfg / ("%s.yml" % BOT))["bot_channel_path"]
    assert got == str(home_under_home / "bots" / BOT / "channel"), got


def test_新建_家目录在HOME底下_频道树确实建在那个绝对路径上(
        client, sandbox, heartbeat, home_under_home):
    heartbeat.tick(BOT, pid=4242, home=home_under_home)
    r = _create(client)
    assert r.status_code == 201, (r.status_code, r.get_json())
    assert (home_under_home / "bots" / BOT / "channel" / ".env").is_file()
    assert not (sandbox.home / "~").exists(), "HOME 底下冒出了一棵名字叫 ~ 的目录"


def test_新建_家目录不在HOME底下_配置里的频道目录就是那个绝对路径(client, sandbox, heartbeat):
    """对照：这种摆法此前写的就是绝对路径，行为不变。"""
    heartbeat.tick(BOT, pid=4242)
    r = _create(client)
    assert r.status_code == 201, (r.status_code, r.get_json())
    got = u.read_yaml(sandbox.dsh_cfg / ("%s.yml" % BOT))["bot_channel_path"]
    assert got == str(sandbox.dsh_home / "bots" / BOT / "channel"), got
