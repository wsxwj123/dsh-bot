# -*- coding: utf-8 -*-
"""适配服务 v0.6 验收：切进 dsh 时增量导入（补充文件 9.3，约定 5.9）。

请求：POST /adapter/v1/bots/{bot_id}/enabled，{"enabled": true, "import_from": "<旧频道目录名>"}。
依据：INTERFACE-适配服务.md 9.2、9.3；INTERFACE-适配约定-v0.7摘录.md 5.8、5.9。不读实现。

接口文件没写 .promises.json 与会话 *.jsonl 每行的形状，下面的假记录是照 Claude Code 频道会话的样子猜的：
所以只断言 imported 的形状与"第二次为 0"，不断言第一次导了几条。
会话目录名照 Claude Code 的规则换算：频道目录绝对路径里的非字母数字字符一律换成 '-'。
旧目录放在 tmp_path_factory 建的目录下（路径里没有中文），经两个测试接缝指过去。
"""
import json
import logging
import os
import re
from types import SimpleNamespace

import pytest

from . import adapter_util as au
from . import botops_util as u
from .adapter_util import adapter, console_guard  # noqa: F401  夹具

IMPORT_FROM = "telegram"
USER_SAYS = "私聊原文-主人说今天好累-ZQX81"
BOT_SAYS = "私聊原文-bot回早点睡-ZQX82"
PROMISE = "承诺原文-明早八点叫醒-ZQX83"
BAIT = "诱饵-不许读到-ZQX99"
#: 约定 5.9 点名不读的，加两份"频道目录里的其余文件"
BAIT_FILES = ("access.json", ".api-token", ".env", "restart-bots.sh", "notes.md")

IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
needs_posix = pytest.mark.skipif(os.name == "nt" or IS_ROOT,
                                 reason="要靠文件权限与软链造场景：Windows 与 root 下造不出来")


def project_dir_name(path):
    """Claude Code 由目录绝对路径换算会话目录名：非字母数字一律换成 '-'。"""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def session_lines():
    tag = ('<channel source="plugin:telegram:telegram" chat_id="%s" message_id="11" user="owner" '
           'user_id="%s" ts="2026-10-13T08:00:00.000Z">\n%s\n</channel>' % (au.CHAT_ID, au.CHAT_ID, USER_SAYS))
    return [
        {"type": "user", "uuid": "u1", "sessionId": "s1", "timestamp": "2026-10-13T08:00:00.000Z",
         "message": {"role": "user", "content": tag}},
        {"type": "assistant", "uuid": "u2", "parentUuid": "u1", "sessionId": "s1",
         "timestamp": "2026-10-13T08:00:05.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "toolu_1", "name": "mcp__plugin_telegram_telegram__reply",
              "input": {"chat_id": au.CHAT_ID, "text": BOT_SAYS}}]}},
    ]


@pytest.fixture
def sw(adapter, tmp_path_factory, monkeypatch):
    """旧频道目录 telegram（两样要读的 + 一堆诱饵）、它的会话目录；botalpha 停用、配置有 chat_id、plist 收起。"""
    chan_root, proj_root = tmp_path_factory.mktemp("oldchan"), tmp_path_factory.mktemp("ccproj")
    monkeypatch.setenv("DSH_CONSOLE_CHANNELS_DIR", str(chan_root))
    monkeypatch.setenv("DSH_CONSOLE_CLAUDE_PROJECTS_DIR", str(proj_root))
    chan = chan_root / IMPORT_FROM
    chan.mkdir()
    promises = {"promises": [{"id": "p1", "chat_id": au.CHAT_ID, "text": PROMISE,
                              "due": "2026-10-15T00:00:00.000Z", "done": False}]}
    (chan / ".promises.json").write_text(json.dumps(promises, ensure_ascii=False), encoding="utf-8")
    for name in BAIT_FILES:
        (chan / name).write_text("%s %s\n" % (BAIT, name), encoding="utf-8")
    sess = proj_root / project_dir_name(chan)
    sess.mkdir()
    (sess / "s1.jsonl").write_text(
        "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in session_lines()), encoding="utf-8")
    (sess / "notes.txt").write_text(BAIT + "\n", encoding="utf-8")
    alpha = adapter.bot("botalpha", enabled=False)
    au.set_cfg(alpha, chat_id=int(au.CHAT_ID))
    au.put_plists(adapter.sandbox, "botalpha", stashed=True)
    bravo = adapter.bot("botbravo")
    adapter.launchctl.set_default(installed=False, running=False, pid=4242)
    return SimpleNamespace(chan_root=chan_root, proj_root=proj_root, chan=chan, sess=sess,
                           alpha=alpha, bravo=bravo)


def switch_in(adapter):
    return adapter.call("POST", au.enabled_url("botalpha"), {"enabled": True, "import_from": IMPORT_FROM})


def stash_again(adapter, bot_id):
    for label in au.unit_labels(bot_id):
        p = adapter.sandbox.agents / (label + ".plist")
        if p.exists():
            p.rename(adapter.sandbox.agents / (label + ".plist.disabled"))


def test_切进_成功_回约定形状_synced为空_启动了这一只_日志与响应不带私聊原文(adapter, sw, caplog, capfd):
    caplog.set_level(logging.DEBUG)
    bravo_tree = u.freeze_roots(sw.bravo.botdir)
    resp = switch_in(adapter)
    body = adapter.ok(resp)
    assert body.get("enabled") is True and isinstance(body.get("running"), bool), body
    imported = body.get("imported")
    assert isinstance(imported, dict) and set(imported) == {"messages", "promises"}, imported
    for k, v in imported.items():
        assert type(v) is int and v >= 0, "imported.%s 应是非负整数：%r" % (k, v)
    assert body.get("synced") == [], "本批不同步人设和技能，synced 应恒为 []：%r" % (body.get("synced"),)
    assert au.read_cfg(sw.alpha).get("enabled") is not False, "导入成功后应记成启用"
    assert au.plist_state(adapter.sandbox, au.unit_labels("botalpha")[0]) == "装着"
    subs = adapter.launchctl.subcommands()
    assert "bootstrap" in subs or "kickstart" in subs, "导入成功却没启动：%r" % adapter.launchctl.calls()
    u.assert_units_never_named(adapter.launchctl, "botbravo", "self-initiate.botbravo")
    u.assert_roots_untouched(bravo_tree)
    out, err = capfd.readouterr()
    texts = [au.response_text(resp), caplog.text, out, err]
    for secret in (USER_SAYS, BOT_SAYS, PROMISE, BAIT):
        for i, text in enumerate(texts):
            assert secret not in text, "第 %d 段（响应或日志）里出现了导入的原文 %r（日志只许记条数）" % (i + 1, secret)
    assert au.CHAT_ID not in texts[0], "响应里出现了聊天号"


def test_切进_同样的旧记录再切进一次_imported都为0_不重复导入(adapter, sw):
    first = adapter.ok(switch_in(adapter))
    au.set_cfg(sw.alpha, enabled=False)                  # 模拟又切回旧系统：记成停用、卸载、收起
    adapter.launchctl.set_unit("botalpha", installed=False, running=False, pid=None)
    stash_again(adapter, "botalpha")
    second = adapter.ok(switch_in(adapter))
    assert second.get("imported") == {"messages": 0, "promises": 0}, \
        "旧记录没有新增，第二次应一条都不导：%r（第一次 %r）" % (second.get("imported"), first.get("imported"))


@needs_posix
def test_切进_只读约定的两样_其他文件读不了也照常成功_旧目录一字未变(adapter, sw):
    for name in BAIT_FILES:
        (sw.chan / name).chmod(0)
    (sw.sess / "notes.txt").chmod(0)
    other = sw.proj_root / "-some-other-project"
    other.mkdir()
    (other / "x.jsonl").write_text(BAIT + "\n", encoding="utf-8")
    (other / "x.jsonl").chmod(0)
    frozen = u.freeze_roots(sw.chan_root, sw.proj_root)
    adapter.ok(switch_in(adapter))
    u.assert_roots_untouched(frozen)


@pytest.mark.parametrize("case", ["bot在跑", "配置缺chat_id"])
def test_切进_前置条件不满足_拒绝_什么都不做(adapter, sw, case):
    if case == "bot在跑":
        au.set_cfg(sw.alpha, enabled=True)
        for label in au.unit_labels("botalpha"):
            (adapter.sandbox.agents / (label + ".plist.disabled")).rename(adapter.sandbox.agents / (label + ".plist"))
        adapter.launchctl.set_unit("botalpha", installed=True, running=True, pid=4242)
        adapter.heartbeat.write("botalpha")
        want = (409, "running")
    else:
        au.set_cfg(sw.alpha, chat_id=None)
        want = (422, "invalid")
    frozen = u.freeze_roots(adapter.sandbox.dsh_cfg, adapter.sandbox.agents, sw.alpha.botdir,
                            sw.chan_root, sw.proj_root)
    body = adapter.expect_error(switch_in(adapter), *want)
    if case == "配置缺chat_id":
        assert "chat_id" in body["message"], "应说明是配置缺 chat_id：%r" % body["message"]
    assert adapter.launchctl.actions() == [], "被拒绝却动了启动项：%r" % adapter.launchctl.actions()
    u.assert_roots_untouched(frozen)


@needs_posix
@pytest.mark.parametrize("what", ["频道目录本身", "会话目录", "promises文件", "会话记录jsonl"])
def test_切进_任一处是软链_回422_一个文件都没读_什么都不做(adapter, sw, what):
    if what == "频道目录本身":
        real = sw.chan_root / "real-telegram"
        sw.chan.rename(real)
        sw.chan.symlink_to(real, target_is_directory=True)
    elif what == "会话目录":
        real = sw.proj_root / "real-session"
        sw.sess.rename(real)
        sw.sess.symlink_to(real, target_is_directory=True)
    elif what == "promises文件":
        real = sw.chan_root / "elsewhere-promises.json"
        (sw.chan / ".promises.json").rename(real)
        (sw.chan / ".promises.json").symlink_to(real)
    else:
        real = sw.proj_root / "elsewhere.jsonl"
        (sw.sess / "s1.jsonl").rename(real)
        (sw.sess / "s1.jsonl").symlink_to(real)
    # 要读的两样都设成读不了：先读后查软链的实现会在这里出错，而不是回 422
    (sw.chan / ".promises.json").chmod(0)
    (sw.sess / "s1.jsonl").chmod(0)
    frozen = u.freeze_roots(adapter.sandbox.dsh_cfg, adapter.sandbox.agents, sw.alpha.botdir,
                            sw.chan_root, sw.proj_root)
    adapter.expect_error(switch_in(adapter), 422, "invalid")
    assert adapter.launchctl.actions() == [], "有软链却动了启动项：%r" % adapter.launchctl.actions()
    u.assert_roots_untouched(frozen)


@needs_posix
@pytest.mark.parametrize("how", ["promises是坏JSON", "会话记录读不出"])
def test_切进_导入失败_回500_说明是导入这一步_不启动(adapter, sw, how):
    if how == "promises是坏JSON":
        (sw.chan / ".promises.json").write_text('{"promises": [', encoding="utf-8")
    else:
        (sw.sess / "s1.jsonl").chmod(0)
    body = adapter.expect_error(switch_in(adapter), 500, "internal")
    assert "导入" in body["message"], "500 应说明是导入这一步：%r" % body["message"]
    subs = adapter.launchctl.subcommands()
    assert "bootstrap" not in subs and "kickstart" not in subs, "导入失败却启动了：%r" % adapter.launchctl.calls()
    assert au.read_cfg(sw.alpha).get("enabled") is False, "导入失败不启动，配置也不该记成启用"
    assert {au.plist_state(adapter.sandbox, lb) for lb in au.unit_labels("botalpha")} == {"收起"}
