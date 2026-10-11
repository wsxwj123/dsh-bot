# -*- coding: utf-8 -*-
"""§6 尾段：模板列表纳入新系统 bot；人设正文按 id 归属选读取源。"""
import pytest

from . import botops_util as u


def _templates(client):
    r = client.get("/hub/api/bots/templates")
    assert r.status_code == 200, (r.status_code, r.get_data(as_text=True)[:300])
    body = r.get_json()
    items = body["templates"] if isinstance(body, dict) else body
    assert isinstance(items, list), body
    return items


def _ids(items):
    return [it["id"] if isinstance(it, dict) else it for it in items]


def _item(items, tid):
    for it in items:
        if (it["id"] if isinstance(it, dict) else it) == tid:
            return it
    raise AssertionError("模板列表里没有 %r：%r" % (tid, items))


def _arm_ok(bun, telegram, launchctl, heartbeat, bot="xiaoyu", pid=4242):
    telegram.mode = "ok"
    bun.expect(exit=0).expect(exit=0)
    launchctl.set_default(installed=True, running=True, pid=pid)
    heartbeat.tick(bot, pid=pid)


def _payload(port, **over):
    body = {"bot_id": "xiaoyu", "telegram_token": u.FAKE_TOKEN, "dispatcher_port": port,
            "chat_id": 123456789, "display_name": "小雨",
            "persona_text": "我是小雨的人设正文。"}
    body.update(over)
    return body


# ------------------------------------------------------------ 模板列表

def test_模板列表_含新系统bot的channel目录(client, sandbox):
    (sandbox.dsh_home / "bots" / "bot4" / "channel").mkdir(parents=True)
    assert "bot4" in _ids(_templates(client))


def test_模板列表_没有channel子目录的bot家不算模板(client, sandbox):
    (sandbox.dsh_home / "bots" / "nakedbot").mkdir(parents=True)
    assert "nakedbot" not in _ids(_templates(client))


def test_模板列表_旧根与新系统同名_只出现一次(client, sandbox):
    (sandbox.channels / "bot4").mkdir(parents=True, exist_ok=True)
    (sandbox.dsh_home / "bots" / "bot4" / "channel").mkdir(parents=True)
    ids = _ids(_templates(client))
    assert ids.count("bot4") == 1, ids


def test_模板列表_旧根目录照旧在表里(client, sandbox):
    (sandbox.channels / "chenlulu").mkdir(parents=True, exist_ok=True)
    assert "chenlulu" in _ids(_templates(client))


def test_模板列表_给不到注册表名字时display_name用原名(client, sandbox):
    (sandbox.dsh_home / "bots" / "bot4" / "channel").mkdir(parents=True)
    item = _item(_templates(client), "bot4")
    assert item["display_name"] == "bot4"


def test_模板列表_注册表有名字时display_name取注册表(client, sandbox):
    (sandbox.dsh_home / "bots" / "bot4" / "channel").mkdir(parents=True)
    u.write_bot_yml(sandbox.dsh_cfg, "bot4", display_name="陈露露", dispatcher_port=17951)
    assert _item(_templates(client), "bot4")["display_name"] == "陈露露"


# ------------------------------------------------------------ 人设来源

def test_人设模板_新系统优先于旧根(client, sandbox, bun, telegram, launchctl, heartbeat,
                              as_darwin):
    new_dir = sandbox.dsh_home / "bots" / "chenlulu" / "channel"
    new_dir.mkdir(parents=True)
    (new_dir / "CLAUDE.md").write_text("我是新系统的正文。\n", encoding="utf-8")
    old_dir = sandbox.channels / "chenlulu"
    old_dir.mkdir(parents=True, exist_ok=True)
    (old_dir / "CLAUDE.md").write_text("我是旧根的正文。\n", encoding="utf-8")
    _arm_ok(bun, telegram, launchctl, heartbeat)
    body = _payload(u.free_port(), persona_template="chenlulu")
    del body["persona_text"]
    r = client.post("/hub/api/bots", json=body)
    assert r.status_code == 201, r.get_json()
    text = (sandbox.dsh_home / "bots" / "xiaoyu" / "channel" /
            "CLAUDE.md").read_text(encoding="utf-8")
    assert "我是新系统的正文。" in text, text
    assert "我是旧根的正文。" not in text, text


def test_人设模板_只旧根有_读旧根(client, sandbox, bun, telegram, launchctl, heartbeat,
                            as_darwin):
    old_dir = sandbox.channels / "chenlulu"
    old_dir.mkdir(parents=True, exist_ok=True)
    (old_dir / "CLAUDE.md").write_text("我是旧根的正文。\n", encoding="utf-8")
    _arm_ok(bun, telegram, launchctl, heartbeat)
    body = _payload(u.free_port(), persona_template="chenlulu")
    del body["persona_text"]
    r = client.post("/hub/api/bots", json=body)
    assert r.status_code == 201, r.get_json()
    text = (sandbox.dsh_home / "bots" / "xiaoyu" / "channel" /
            "CLAUDE.md").read_text(encoding="utf-8")
    assert "我是旧根的正文。" in text, text


def test_人设模板_两处目录在但文件都不在_空人设(client, sandbox, bun, telegram, launchctl,
                                      heartbeat, as_darwin):
    (sandbox.dsh_home / "bots" / "chenlulu" / "channel").mkdir(parents=True)
    (sandbox.channels / "chenlulu").mkdir(parents=True, exist_ok=True)
    _arm_ok(bun, telegram, launchctl, heartbeat)
    body = _payload(u.free_port(), persona_template="chenlulu")
    del body["persona_text"]
    r = client.post("/hub/api/bots", json=body)
    assert r.status_code == 201, r.get_json()
    text = (sandbox.dsh_home / "bots" / "xiaoyu" / "channel" /
            "CLAUDE.md").read_text(encoding="utf-8")
    assert text.strip() == "", repr(text)
