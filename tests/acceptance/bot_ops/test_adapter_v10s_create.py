# -*- coding: utf-8 -*-
"""适配服务 v1.0 补充验收：新建的新字段、新规则、自动挑端口、getMe 的两个新错误码、人设模板列表（13.3）。

依据：INTERFACE-适配服务.md 12.2、13.3。不读实现。getMe 用 conftest 的桩（回 username my_bot、id 123456789）；
不取用桩时，sandbox 已把 Telegram 地址指到一个没人听的本机端口，就是「连不上 Telegram」。
「启动项文件按停用收起」：假 bun 不真写启动项文件，这里只验没有拉起（launchctl 没有装载、启动类的调用）。
"""
import json
import socket

import pytest
import yaml

from . import adapter_util as au
from .adapter_util import adapter, console_guard  # noqa: F401  夹具

NEW = "botnew"
STARTS = ("bootstrap", "kickstart", "enable", "load")


def good_body(**over):
    body = {"bot_id": NEW, "telegram_token": au.NEW_TG_TOKEN, "chat_id": au.NEW_CHAT}
    body.update(over)
    return body


def new_cfg(adapter):
    return yaml.safe_load((adapter.sandbox.dsh_cfg / ("%s.yml" % NEW)).read_text(encoding="utf-8"))


def nothing_written(adapter):
    sb = adapter.sandbox
    assert list(sb.dsh_cfg.iterdir()) == [], "不该留下配置：%r" % list(sb.dsh_cfg.iterdir())
    bots = sb.dsh_home / "bots"
    assert not bots.exists() or list(bots.iterdir()) == [], "不该留下 bot 目录"
    assert list(sb.agents.iterdir()) == [], "不该留下启动项文件"


def listening(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def test_新建_enabled假_新字段写进配置_不启动_person与username对得上(adapter, telegram, bun):
    body = good_body(enabled=False, person="lucy", telegram_username="@MY_BOT",
                     telegram_user_id=123456789, purpose="switch_in")
    got = adapter.ok(adapter.call("POST", au.BOTS, body))
    assert (got["bot_id"], got["person"], got["running"]) == (NEW, "lucy", False), got
    cfg = new_cfg(adapter)
    assert cfg.get("enabled") is False and cfg.get("life_id") == "lucy", cfg
    assert str(cfg.get("telegram_username")).lower() == "my_bot", "用户名应去掉 @ 记进配置：%r" % cfg
    assert cfg.get("telegram_user_id") == 123456789, cfg
    starts = [c for c in adapter.launchctl.calls() if c.get("cmd") in STARTS]
    assert starts == [], "enabled 为 false 时不该拉起：%r" % starts
    live = [p.name for p in adapter.sandbox.agents.iterdir() if p.name.endswith(".plist")]
    assert live == [], "启动项文件应按停用收起：%r" % live
    row = au.rows_by_id(adapter)[NEW]
    assert (row["state"], row["enabled"], row["person"]) == ("stopped", False, "lucy"), row
    assert str(row["username"]).lower() == "my_bot", row


BAD = [
    ("enabled不是布尔", good_body(enabled="no")),
    ("person有大写", good_body(person="Lucy")),
    ("person只有一个字", good_body(person="l")),
    ("username太短", good_body(telegram_username="ab")),
    ("user_id是字符串", good_body(telegram_user_id="123456789")),
    ("user_id是布尔", good_body(telegram_user_id=True)),
    ("purpose不认识", good_body(purpose="other")),
    ("编号带横线", good_body(bot_id="bot-new")),
    ("编号只有一个字", good_body(bot_id="b")),
    ("显示名超过32个字", good_body(display_name="名" * 33)),
]


@pytest.mark.parametrize("payload", [b for _n, b in BAD], ids=[n for n, _b in BAD])
def test_新建_新字段与新规则不合规_422_不问Telegram_什么都不写(adapter, telegram, bun, payload):
    adapter.expect_error(adapter.call("POST", au.BOTS, payload), 422, "invalid")
    assert telegram.hits == [], "形状不合规不该去问 getMe"
    assert bun.calls() == [] and adapter.launchctl.calls() == []
    nothing_written(adapter)


@pytest.mark.parametrize("over", [{"telegram_username": "someone_else_bot"}, {"telegram_user_id": 987654321}],
                         ids=["用户名对不上", "user_id对不上"])
def test_新建_与getMe对不上_422_token_rejected_什么都不写(adapter, telegram, bun, over):
    resp = adapter.call("POST", au.BOTS, good_body(**over))
    adapter.expect_error(resp, 422, "token_rejected")
    assert au.NEW_TG_TOKEN.split(":")[1] not in au.response_text(resp)
    assert telegram.hits, "应向 Telegram 问过 getMe"
    assert bun.calls() == []
    nothing_written(adapter)


def test_新建_连不上Telegram_503_telegram_unreachable_什么都不写(adapter, bun):
    resp = adapter.call("POST", au.BOTS, good_body())
    adapter.expect_error(resp, 503, "telegram_unreachable")
    assert au.NEW_TG_TOKEN.split(":")[1] not in au.response_text(resp)
    assert bun.calls() == []
    nothing_written(adapter)


def test_新建_不给端口_在17850到17899挑第一个没被配置归档占用且没人听的(adapter, telegram, bun):
    sb = adapter.sandbox
    au.set_cfg(adapter.bot("botbusy"), dispatcher_port=17850)
    old = sb.dsh_home / "archive" / "botold-20261015T080102Z"
    au.put_file(old / "configs" / "botold.yml", json.dumps({"id": "botold", "dispatcher_port": 17851}))
    au.put_file(old / "manifest.json", json.dumps({"bot_id": "botold", "items": [
        {"from": str(sb.dsh_cfg / "botold.yml"), "to": "configs/botold.yml"}]}))
    holder = socket.socket()
    try:
        holder.bind(("127.0.0.1", 17852))
        holder.listen(1)
    except OSError:                         # 本机已有人占着 17852，同样算「有人在听」
        holder.close()
        holder = None
    try:
        want = next(p for p in range(17850, 17900) if p not in (17850, 17851) and not listening(p))
        adapter.ok(adapter.call("POST", au.BOTS, good_body()))
    finally:
        if holder is not None:
            holder.close()
    assert new_cfg(adapter)["dispatcher_port"] == want, "应挑 %d：%r" % (want, new_cfg(adapter))


def test_模板列表_列本系统有人设的bot_每项只有id与label_另一套系统的不列(adapter):
    sb = adapter.sandbox
    alpha = adapter.bot("botalpha", display_name="阿尔法")
    au.put_file(au.persona_path(alpha), "# 人设\n")
    legacy = sb.channels / "legacychan"
    au.put_file(legacy / "CLAUDE.md", "# 旧系统的人设\n")
    items = adapter.ok(adapter.call("GET", au.TEMPLATES))["templates"]
    assert isinstance(items, list) and all(set(t) == {"id", "label"} for t in items), items
    by_id = {t["id"]: t for t in items}
    assert by_id.get("botalpha", {}).get("label") == "阿尔法", items
    assert "legacychan" not in by_id, "另一套系统的频道目录不该列：%r" % items
