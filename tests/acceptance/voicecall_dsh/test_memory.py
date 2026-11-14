# -*- coding: utf-8 -*-
"""③.2 GET /adapter/v1/bots/{bot_id}/memory：长期记忆主文件前段。"""
import os

import pytest

from . import vc_util as vu

RAW = "  # Memory\r\n\n- 哥哥喜欢糖醋排骨🍖\n\t缩进的一行  \n"
TOPIC_SECRET = "主题记忆文件里的内容不该被读"
OTHER_SECRET = "别的bot的长期记忆不该被读"


@pytest.mark.parametrize("mode", ["配置里写了频道目录", "没写频道目录用默认", "频道目录写成波浪号开头", "带了查询参数"])
def test_正常_原样返回主文件_不读主题文件(w, sandbox, monkeypatch, mode):
    if mode == "频道目录写成波浪号开头":
        home = sandbox.home / ".dsh-bot"
        home.mkdir()
        monkeypatch.setenv("DSH_BOT_HOME", str(home))
        bot = w.bot(channel="~/.dsh-bot/bots/%s/channel" % vu.BOT)
    else:
        bot = w.bot(channel=None if mode == "没写频道目录用默认" else "default")
    vu.put_file(bot.channel / "memory" / "MEMORY.md", RAW)
    vu.put_file(bot.channel / "memory" / "topic-旅行.md", TOPIC_SECRET)
    path = vu.memory_url(vu.BOT) + ("?path=..%2F..%2Fetc&n=0" if mode == "带了查询参数" else "")
    resp = w.call("GET", path)
    body = vu.expect_ok(resp)
    assert body == {"ok": True, "exists": True, "text": RAW, "truncated": False}
    assert TOPIC_SECRET not in resp.get_data(as_text=True)


@pytest.mark.parametrize("data,text,truncated", [
    (("记" * 1799 + "🍖").encode("utf-8"), "记" * 1799 + "🍖", False),
    (("记" * 1801).encode("utf-8"), "记" * 1800, True),
    (b"a" * 5000, "a" * 1800, True),
    (("记" * 1800).encode("utf-8") + b"\xff\xfe\xfd", "记" * 1800, True),
], ids=["正好1800码点", "1801码点", "很长", "1800字之后才有坏字节"])
def test_前1800个码点_超出标truncated(w, data, text, truncated):
    bot = w.bot()
    vu.put_file(bot.channel / "memory" / "MEMORY.md", data)
    assert vu.expect_ok(w.memory()) == {"ok": True, "exists": True, "text": text, "truncated": truncated}


@pytest.mark.parametrize("case,exists", [("没有memory目录", False), ("有目录没有主文件只有主题文件", False),
                                         ("主文件是空的", True)])
def test_不存在或为空_明确说没有_不报错(w, case, exists):
    bot = w.bot()
    mem = bot.channel / "memory"
    if case == "有目录没有主文件只有主题文件":
        vu.put_file(mem / "topic-旅行.md", TOPIC_SECRET)
    elif case == "主文件是空的":
        vu.put_file(mem / "MEMORY.md", b"")
    resp = w.memory()
    assert vu.expect_ok(resp) == {"ok": True, "exists": exists, "text": "", "truncated": False}
    assert TOPIC_SECRET not in resp.get_data(as_text=True)


def _setup_bad(w, sandbox, case):
    """按情形摆一只读不了记忆的 bot。返回期望的 message。"""
    other = w.bot(vu.OTHER_BOT)
    vu.put_file(other.channel / "memory" / "MEMORY.md", OTHER_SECRET)
    outside = sandbox.tmp / "outside-channel"
    vu.put_file(outside / "memory" / "MEMORY.md", OTHER_SECRET)
    if case == "频道目录在自己家外面":
        w.bot(channel=outside)
        return vu.MEM_OUTSIDE
    if case == "频道目录指到别的bot":
        w.bot(channel=other.channel)
        return vu.MEM_OUTSIDE
    if case == "频道目录用点点跳到别的bot":
        w.bot(channel=str(w.botdir() / "channel" / ".." / ".." / vu.OTHER_BOT / "channel"))
        return vu.MEM_OUTSIDE
    bot = w.bot()
    mem = bot.channel / "memory"
    if case == "频道目录本身是软链到外面":
        os.rmdir(str(bot.channel))
        os.symlink(str(outside), str(bot.channel))
        return vu.MEM_OUTSIDE
    if case == "memory目录是软链":
        os.symlink(str(outside / "memory"), str(mem))
        return vu.MEM_OUTSIDE
    if case == "主文件是软链指到同目录真文件":
        vu.put_file(mem / "real.md", OTHER_SECRET)
        os.symlink(str(mem / "real.md"), str(mem / "MEMORY.md"))
        return vu.MEM_OUTSIDE
    if case == "主文件是软链指到别的bot":
        mem.mkdir(parents=True)
        os.symlink(str(other.channel / "memory" / "MEMORY.md"), str(mem / "MEMORY.md"))
        return vu.MEM_OUTSIDE
    if case == "主文件是目录":
        (mem / "MEMORY.md").mkdir(parents=True)
        return vu.MEM_UNREADABLE
    if case == "主文件是FIFO":
        mem.mkdir(parents=True)
        os.mkfifo(str(mem / "MEMORY.md"))
        return vu.MEM_UNREADABLE
    if case == "前1800字内有坏字节":
        vu.put_file(mem / "MEMORY.md", "开头正常".encode("utf-8") + b"\xff\xfe" + OTHER_SECRET.encode("utf-8"))
        return vu.MEM_UNREADABLE
    if case == "没有读权限":
        if os.geteuid() == 0:
            pytest.skip("root 不受文件权限限制")
        vu.put_file(mem / "MEMORY.md", OTHER_SECRET, mode=0o000)
        return vu.MEM_UNREADABLE
    raise AssertionError(case)


BAD_MEMORY = ["频道目录在自己家外面", "频道目录指到别的bot", "频道目录用点点跳到别的bot", "频道目录本身是软链到外面",
              "memory目录是软链", "主文件是软链指到同目录真文件", "主文件是软链指到别的bot",
              "主文件是目录", "主文件是FIFO", "前1800字内有坏字节", "没有读权限"]


@pytest.mark.parametrize("case", BAD_MEMORY)
def test_记忆文件不在自己目录或是软链或读不出_500_不带出内容(w, sandbox, case):
    message = _setup_bad(w, sandbox, case)
    resp = vu.call_with_deadline(lambda: w.memory(), 10, "读 %s 的记忆" % case)
    vu.expect_error(resp, 500, "internal", message)
    assert OTHER_SECRET not in resp.get_data(as_text=True)
