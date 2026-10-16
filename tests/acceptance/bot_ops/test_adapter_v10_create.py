# -*- coding: utf-8 -*-
"""适配服务 v1.0 验收：能力清单二十个键、新建 POST /adapter/v1/bots（补充文件 12.1、12.2，草案 A），
以及第 12 节七个新接口的鉴权。

依据：INTERFACE-适配服务.md 第 2、3 节与第 12 节；INTERFACE-适配约定-v1.0-dsh侧.md A。不读实现。
「与新建页效果一致」借自本目录现成用例（test_create_bot.py）：配置进新系统配置根，频道树进
<DSH_BOT_HOME>/bots/<编号>/channel（.env 放令牌、access.json、CLAUDE.md），装启动项是假 bun 跑
autostart.ts 的 install 与 install-jobs。getMe 用 conftest 的桩（回 username my_bot、id 123456789）。
接口没写死、这里自定的两处：装启动项失败回 503 unavailable（同 9.7「停启动项报错」）；
令牌已被本系统另一只 bot 用着回 422 invalid（新建页的同款校验，只查本系统）。
"""
import json
import os

import pytest
import yaml

from . import adapter_util as au
from .adapter_util import adapter, console_guard  # noqa: F401  夹具

NEW = "botnew"
CREATE_KEYS = {"ok", "bot_id", "person", "username", "enabled", "running",   # 约定 v1.0 5.18.1 加了 enabled
               "features", "skills", "features_skipped"}                     # v1.1 5.18.1 多带的三个键


def good_body(**over):
    body = {"bot_id": NEW, "telegram_token": au.NEW_TG_TOKEN, "chat_id": au.NEW_CHAT}
    body.update(over)
    return body


def nothing_written(adapter):
    sb = adapter.sandbox
    assert not os.path.lexists(str(sb.dsh_cfg / ("%s.yml" % NEW))), "不该留下配置"
    assert not os.path.lexists(str(sb.dsh_home / "bots" / NEW)), "不该留下 bot 目录"
    left = [p.name for p in sb.agents.iterdir() if NEW in p.name]
    assert left == [], "不该留下启动项文件：%r" % left


def no_secret(resp):
    text = au.response_text(resp)
    assert au.NEW_TG_TOKEN.split(":")[1] not in text, "响应里出现了 Telegram 令牌"
    assert au.NEW_FISH_KEY not in text, "响应里出现了 Fish key"


def test_meta_能力清单恰好二十个键_新增四项都为真(adapter):
    caps = adapter.ok(adapter.call("GET", au.META)).get("capabilities")
    assert caps == au.CAPABILITIES_V11, "能力清单应恰好二十三个键（补充文件 13.8，v1.1 加 bot.groups_discovered、bot.features）：%r" % (caps,)


@pytest.mark.parametrize("port", [None, "free"], ids=["不给端口_自动挑", "指定端口_照写"])
def test_新建_成功_回六个键_文件落在本系统_装了启动项_密钥不出现(adapter, telegram, bun, caplog, port):
    sb = adapter.sandbox
    want_port = au.free_port() if port == "free" else None
    persona = "# 新人设\n你是测试用的 bot。\n"
    body = good_body(display_name="  新来的  ", persona_text=persona, fish_voice_id="voice_abc-1",
                     fish_api_key=au.NEW_FISH_KEY)
    if want_port:
        body["port"] = want_port
    resp = adapter.call("POST", au.BOTS, body)
    got = adapter.ok(resp)
    assert set(got) == CREATE_KEYS, "成功应恰好回 ok、bot_id、person、username、enabled、running 与 v1.1 的三个键：%r" % (got,)
    assert (got["bot_id"], got["person"], got["username"]) == (NEW, NEW, "my_bot"), got
    assert got["enabled"] is True, "不给 enabled 时缺省为 true：%r" % (got,)
    assert got["running"] is True, "假 launchctl 说在跑，running 应为 true：%r" % (got,)
    no_secret(resp)
    assert au.NEW_FISH_KEY not in caplog.text and au.NEW_TG_TOKEN.split(":")[1] not in caplog.text, "日志里出现了密钥"

    yml = sb.dsh_cfg / ("%s.yml" % NEW)
    yml_text = yml.read_text(encoding="utf-8")
    assert au.NEW_TG_TOKEN.split(":")[1] not in yml_text and au.NEW_FISH_KEY not in yml_text, "配置里不该有密钥"
    cfg = yaml.safe_load(yml_text)
    assert cfg["display_name"] == "新来的" and cfg["chat_id"] == au.NEW_CHAT, cfg
    if want_port:
        assert cfg["dispatcher_port"] == want_port, cfg
    else:
        assert isinstance(cfg["dispatcher_port"], int) and 1024 <= cfg["dispatcher_port"] <= 65535, cfg
    channel = sb.dsh_home / "bots" / NEW / "channel"
    env = (channel / ".env").read_text(encoding="utf-8")
    assert au.NEW_TG_TOKEN in env and au.NEW_FISH_KEY in env, "令牌与 Fish key 应落在频道目录的 .env"
    access = json.loads((channel / "access.json").read_text(encoding="utf-8"))
    assert access.get("allowFrom") == [au.NEW_CHAT] and access.get("voiceId") == "voice_abc-1", access
    assert (channel / "CLAUDE.md").read_text(encoding="utf-8") == persona
    calls = bun.calls()
    assert [c["argv"][-2:] for c in calls] == [["install", str(yml)], ["install-jobs", str(yml)]], calls
    assert list(sb.legacy_cfg.iterdir()) == [] and list(sb.channels.iterdir()) == [], "另一套系统的目录一个字节都不该有"
    rows = adapter.ok(adapter.call("GET", au.BOTS))["bots"]
    assert [r["bot_id"] for r in rows] == [NEW], rows


BAD = [
    ("缺bot_id", {"telegram_token": au.NEW_TG_TOKEN, "chat_id": au.NEW_CHAT}),
    ("bot_id有大写", good_body(bot_id="BotNew")),
    ("bot_id是保留名", good_body(bot_id="director")),
    ("缺telegram_token", {"bot_id": NEW, "chat_id": au.NEW_CHAT}),
    ("令牌形状不对", good_body(telegram_token="not-a-telegram-token")),
    ("chat_id不是整数", good_body(chat_id="abc")),
    ("人设两样都给", good_body(persona_text="x", persona_template="botbravo")),
    ("端口超范围", good_body(port=80)),
    ("多了不认识的键", good_body(extra=1)),
    ("请求体是数组", None),
]


@pytest.mark.parametrize("payload", [b for _n, b in BAD], ids=[n for n, _b in BAD])
def test_新建_请求体不合规_422_不问Telegram_什么都不写(adapter, telegram, bun, payload):
    if payload is None:
        resp = adapter.call("POST", au.BOTS, raw=b"[]")
    else:
        resp = adapter.call("POST", au.BOTS, payload)
    adapter.expect_error(resp, 422, "invalid")
    no_secret(resp)
    assert telegram.hits == [], "形状不合规不该去问 getMe"
    assert bun.calls() == [] and adapter.launchctl.calls() == []
    nothing_written(adapter)


@pytest.mark.parametrize("taken", ["配置已在", "bot目录已在"])
def test_新建_编号已被占用_409_exists_不问Telegram_原样不动(adapter, telegram, bun, taken):
    sb = adapter.sandbox
    if taken == "配置已在":
        target = sb.dsh_cfg / ("%s.yml" % NEW)
        au.put_file(target, json.dumps({"id": NEW}))
    else:
        target = sb.dsh_home / "bots" / NEW
        target.mkdir(parents=True)
    before = au.tree_mark(sb.dsh_home), au.tree_mark(sb.dsh_cfg)
    adapter.expect_error(adapter.call("POST", au.BOTS, good_body()), 409, "exists")
    assert telegram.hits == [] and bun.calls() == []
    assert (au.tree_mark(sb.dsh_home), au.tree_mark(sb.dsh_cfg)) == before, "409 时什么都不该动"


def test_新建_getMe说令牌不对_422_什么都不写(adapter, telegram, bun):
    telegram.mode = "401"
    resp = adapter.call("POST", au.BOTS, good_body(fish_api_key=au.NEW_FISH_KEY))
    adapter.expect_error(resp, 422, "token_rejected")      # 13.3
    no_secret(resp)
    assert telegram.hits, "应向 Telegram 问过一次 getMe"
    assert bun.calls() == []
    nothing_written(adapter)


def test_新建_令牌已被本系统另一只bot用着_422_什么都不写(adapter, telegram, bun):
    adapter.bot("botbravo")                 # 它的 .env 里是 au.TG_TOKEN，数字段 700000001
    telegram.bot_id = 700000001             # getMe 说新令牌就是那个 Telegram bot
    adapter.expect_error(adapter.call("POST", au.BOTS, good_body()), 422, "invalid")
    assert bun.calls() == []
    nothing_written(adapter)


def test_新建_装启动项失败_回滚_不留半截(adapter, telegram, bun, monkeypatch):
    au.use_trash(adapter.sandbox, monkeypatch)
    bun.expect(exit=0).expect(exit=3)       # install 成了，install-jobs 失败
    resp = adapter.call("POST", au.BOTS, good_body(fish_api_key=au.NEW_FISH_KEY))
    adapter.expect_error(resp, 503, "unavailable")
    no_secret(resp)
    nothing_written(adapter)
    rows = adapter.ok(adapter.call("GET", au.BOTS))["bots"]
    assert NEW not in [r["bot_id"] for r in rows], rows


V10_CALLS = [
    ("新建", "POST", au.BOTS, good_body()),
    ("归档", "POST", au.archive_url("botalpha"), {}),
    ("删除", "POST", au.delete_url("botalpha"), {"confirm": "botalpha"}),
    ("读显示名", "GET", au.profile_url("botalpha"), au.NO_BODY),
    ("改显示名", "PUT", au.profile_url("botalpha"), {"display_name": "新名字"}),
    ("归档列表", "GET", au.ARCHIVES, au.NO_BODY),
    ("恢复", "POST", au.restore_url("botalpha-20261015T080102Z"), {}),
]


@pytest.mark.parametrize("method,path,body", [c[1:] for c in V10_CALLS], ids=[c[0] for c in V10_CALLS])
def test_v10_七个新接口_没带令牌_401_什么都不做(adapter, method, path, body):
    sb = adapter.sandbox
    au.full_bot(adapter, "botalpha")
    before = [au.tree_mark(d) for d in (sb.dsh_home, sb.dsh_cfg, sb.agents)]
    au.assert_unauthorized(adapter.call(method, path, body, auth=None))
    assert [au.tree_mark(d) for d in (sb.dsh_home, sb.dsh_cfg, sb.agents)] == before
    assert adapter.launchctl.calls() == []
