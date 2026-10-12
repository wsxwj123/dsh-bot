# -*- coding: utf-8 -*-
"""§7 落盘清单「配置」一格（接口约定第 7 稿）：新建 bot 时配置里的 `id` 带双引号。

依据 `INTERFACE-新系统bot运维.md` §7：

    `id` 的值写成带双引号的字符串，与 `display_name` 同一种写法，首行形如 `id: "<名字>"`；
    这样 `null`、`true`、`false`、`yes`、`no`、`on`、`off` 这些形状合法的名字读回来也还是
    字符串。

这七个词在 YAML 里不带引号时会被读成空值或布尔值。它们都是形状合法的 bot id（小写字母
开头、2 到 32 个字符），用户完全可能拿来当名字。

新建是全流程（落盘、register、等活），假 bun、假 launchctl、假 Telegram 都在位。
"""
import pytest

from . import botops_util as u

#: YAML 里有特殊含义、同时又是形状合法 bot id 的词（§7 点名的七个）
YAML_WORDS = ["null", "true", "false", "yes", "no", "on", "off"]


def _create(client, bot_id):
    return client.post("/hub/api/bots", json={
        "bot_id": bot_id, "telegram_token": u.FAKE_TOKEN, "dispatcher_port": u.free_port(),
        "chat_id": 123456789, "display_name": "新来的", "persona_text": "新来的人设。"})


@pytest.fixture
def arm(bun, telegram, launchctl, heartbeat, as_darwin, monkeypatch):
    """把全流程成功要的外部条件摆好：getMe 通、两条安装命令都成、单元在跑、心跳一直新鲜。"""
    u.tighten_constant(monkeypatch, value=30, new=3.0)      # 新建探活上限（§1，测试可改）

    def _arm(bot_id):
        telegram.mode = "ok"
        bun.expect(exit=0).expect(exit=0)
        launchctl.set_default(installed=True, running=True, pid=4242)
        heartbeat.tick(bot_id, pid=4242)
    return _arm


def _created_yml(sandbox, r, bot_id):
    """新建落盘成功（201，或 C 段没走完的 207）之后那份配置的路径。"""
    assert r.status_code in (201, 207), ("新建没落盘", r.status_code, r.get_json(silent=True))
    p = sandbox.dsh_cfg / ("%s.yml" % bot_id)
    assert p.is_file(), "新建回了 %s 但配置不在新系统配置根下" % r.status_code
    return p


def test_新建_配置首行是带双引号的id(client, sandbox, arm):
    arm("xinbot")
    yml = _created_yml(sandbox, _create(client, "xinbot"), "xinbot")
    first = yml.read_text(encoding="utf-8").splitlines()[0]
    assert first == 'id: "xinbot"', ("首行应当是带双引号的 id", first)


def test_新建_id与display_name是同一种带双引号的写法(client, sandbox, arm):
    arm("xinbot")
    yml = _created_yml(sandbox, _create(client, "xinbot"), "xinbot")
    lines = yml.read_text(encoding="utf-8").splitlines()
    assert 'id: "xinbot"' in lines, lines
    assert 'display_name: "新来的"' in lines, lines


@pytest.mark.parametrize("word", YAML_WORDS)
def test_新建_名字是YAML里有特殊含义的词_读回来仍是这个字符串(client, sandbox, arm, word):
    arm(word)
    yml = _created_yml(sandbox, _create(client, word), word)
    got = u.read_yaml(yml)["id"]
    assert isinstance(got, str) and got == word, ("id 读回来不是原来那个字符串", type(got).__name__, got)


@pytest.mark.parametrize("word", YAML_WORDS)
def test_新建_名字是YAML里有特殊含义的词_五步照常全done(client, sandbox, arm, word):
    """名字被读成空值或布尔值的话，后面装单元、等活拿到的就不是这只了。"""
    arm(word)
    r = _create(client, word)
    assert r.status_code == 201, (word, r.status_code, r.get_json(silent=True))
    body = r.get_json()
    assert body["bot_id"] == word, body
    assert [s["state"] for s in body["steps"]] == ["done"] * 5, body["steps"]


@pytest.mark.parametrize("word", ["null", "true", "no"])
def test_新建_名字是YAML里有特殊含义的词_建完在列表里就是这个名字(client, sandbox, arm, word):
    """BRIEF 的成功标准：建完能在 bot 状态页看到它。"""
    arm(word)
    _created_yml(sandbox, _create(client, word), word)
    r = client.get("/hub/api/bots")
    assert r.status_code == 200, (r.status_code, r.get_json(silent=True))
    ids = [b["id"] for b in r.get_json()["bots"]]
    assert ids == [word], ("列表里应当恰好有这一只，名字是原来那个字符串", ids)
