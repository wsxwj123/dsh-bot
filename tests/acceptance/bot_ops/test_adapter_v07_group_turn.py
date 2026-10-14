# -*- coding: utf-8 -*-
"""适配服务 v0.7 验收：notify 的第四种来源 group_turn（补充文件 9.6，约定 5.12.3）。

依据：INTERFACE-适配服务.md 6.2、第 8 节、9.6；INTERFACE-适配约定-v0.7摘录.md 5.7、5.12.3、5.12.7。不读实现。
human_ts 按补充文件 9.6 写成单个时间字符串；约定原文说它是"时间列表"，两份不一致，列表形状这里不测。
群成员身份看这只 bot 频道目录下的 access.json（9.5）。
"""
from types import SimpleNamespace

import pytest

from . import adapter_util as au
from .adapter_util import adapter, console_guard  # noqa: F401  夹具

GROUP = "-1001234567890"
OTHER_GROUP = "-1009998887776"
GT_TEXT = ("群里最近的对话：\n小B：今天好热\n（上面是刚才的聊天，你可以按自己的来。）\n"
           "（按你平时的语气说话；不要旁白和动作描写，不要频繁发emoji。）")
GT_KEY = "group-turn:%s:42" % GROUP
HUMAN_TS = "2026-10-14T08:00:00.000Z"


def gt(**over):
    d = {"source": "group_turn", "text": GT_TEXT, "key": GT_KEY, "chat_id": GROUP}
    d.update(over)
    return d


def notify(adapter, payload, bot_id="botbravo"):
    return adapter.call("POST", au.notify_url(bot_id), payload)


def injects(gw):
    return [r for r in gw.requests if (r["method"], r["path"]) == ("POST", au.GW_INJECT)]


def inject_gw(adapter):
    gw, inbox = adapter.gateway(), au.InjectInbox()
    gw.on_fn("POST", au.GW_INJECT, inbox)
    return gw, inbox


def group_bot(adapter, bot_id, *, groups, enabled=True, gw=None, port=None):
    """配置里有主人 chat_id 的 bot；access.json 里的群由 groups 定（None 表示没有 groups 键）。"""
    bot = adapter.bot(bot_id, enabled=enabled, gw_port=gw.port if gw else port)
    au.set_cfg(bot, chat_id=int(au.CHAT_ID))
    access = {"allowFrom": [au.CHAT_ID]}
    if groups is not None:
        access["groups"] = {g: {"requireMention": False, "intro": "群里的一员"} for g in groups}
    au.write_access(bot, access)
    if enabled and gw is not None:
        adapter.heartbeat.tick(bot_id, pid=4242)
    return bot


@pytest.fixture
def target(adapter):
    """botalpha（只在别的群，有自己的网关）与 botbravo（目标，在 GROUP 里）。"""
    gw_a, _ = inject_gw(adapter)
    gw_b, inbox_b = inject_gw(adapter)
    group_bot(adapter, "botalpha", gw=gw_a, groups=[OTHER_GROUP])
    group_bot(adapter, "botbravo", gw=gw_b, groups=[GROUP])
    return SimpleNamespace(gw=gw_b, inbox=inbox_b, other_gw=gw_a)


def test_group_turn_正常投递_以来源director注入这个群_key原样(adapter, target):
    body = adapter.ok(notify(adapter, gt()))
    assert body.get("status") == "delivered", body
    sent = injects(target.gw)
    assert len(sent) == 1, "应恰好向 botbravo 的网关注入一次：%r" % target.gw.requests
    got = sent[0]["json"]
    assert got.get("source") == "director", "group_turn 应以来源 director 注入：%r" % got.get("source")
    assert str(got.get("chat_id")) == GROUP, "chat_id 应填群号，不是主人的私聊号：%r" % got.get("chat_id")
    assert got.get("key") == GT_KEY, "不超过 120 个字符的 key 应原样转交：%r" % got.get("key")
    assert isinstance(got.get("text"), str) and GT_TEXT in got["text"], "导演的原文没交给网关"
    assert target.other_gw.requests == [], "投给 botbravo，却找了 botalpha 的网关"


def test_group_turn_带合规human_ts_照常投递_human_ts只校验不交给网关(adapter, target):
    body = adapter.ok(notify(adapter, gt(human_ts=HUMAN_TS)))
    assert body.get("status") == "delivered", body
    sent = injects(target.gw)
    assert len(sent) == 1, target.gw.requests
    assert "human_ts" not in sent[0]["json"] and HUMAN_TS not in sent[0]["body"], \
        "human_ts 只校验不使用，却交给了网关：%r" % sent[0]["body"]


def test_group_turn_同一个key交两次_第二次回duplicate_bot只收到一次(adapter, target):
    first = adapter.ok(notify(adapter, gt()))
    second = adapter.ok(notify(adapter, gt()))
    assert (first.get("status"), second.get("status")) == ("delivered", "duplicate"), (first, second)
    assert len(target.inbox.delivered) == 1, "bot 收到了不止一次：%r" % target.inbox.delivered


@pytest.mark.parametrize("groups", [[OTHER_GROUP], None], ids=["access里没有这个群号", "access里没有groups"])
def test_group_turn_这只bot不在这个群里_回409_not_in_group_不投递(adapter, groups):
    gw, _ = inject_gw(adapter)
    group_bot(adapter, "botbravo", gw=gw, groups=groups)
    adapter.expect_error(notify(adapter, gt()), 409, "not_in_group")
    assert gw.requests == [], "不在群里却找了网关：%r" % gw.requests


@pytest.mark.parametrize("case", ["bot停用_回409_stopped", "网关连不上_回503_unavailable"])
def test_group_turn_失败路径同另三种来源(adapter, case):
    if case.startswith("bot停用"):
        gw, _ = inject_gw(adapter)
        group_bot(adapter, "botbravo", enabled=False, gw=gw, groups=[GROUP])
        adapter.expect_error(notify(adapter, gt()), 409, "stopped")
        assert gw.requests == [], "bot 停用着，却找了它的网关：%r" % gw.requests
    else:
        group_bot(adapter, "botbravo", port=au.free_port(), groups=[GROUP])     # 端口没人听
        adapter.expect_error(notify(adapter, gt()), 503, "unavailable")


def _no_chat_id():
    d = gt()
    del d["chat_id"]
    return d


BAD = {
    "group_turn缺chat_id": _no_chat_id(),
    "chat_id是数字": gt(chat_id=-1001234567890),
    "chat_id没有横线": gt(chat_id="1001234567890"),
    "chat_id只有横线": gt(chat_id="-"),
    "chat_id横线后21位": gt(chat_id="-" + "1" * 21),
    "chat_id带字母": gt(chat_id="-100abc"),
    "human_ts没有毫秒": gt(human_ts="2026-10-14T08:00:00Z"),
    "human_ts毫秒不是000": gt(human_ts="2026-10-14T08:00:00.123Z"),
    "human_ts带时区偏移不是Z": gt(human_ts="2026-10-14T16:00:00.000+08:00"),
    "human_ts是数字": gt(human_ts=1760428800),
    "user_moment带chat_id": {"source": "user_moment", "text": "用户发了动态", "key": "user-moment:1",
                             "chat_id": GROUP},
    "moment_reply带human_ts": {"source": "moment_reply", "text": "有人评论", "key": "moment-reply:1:2",
                              "human_ts": HUMAN_TS},
    "moment_image带chat_id": {"source": "moment_image", "text": "要配图", "key": "moment-image:1",
                              "chat_id": GROUP},
}


@pytest.mark.parametrize("payload", list(BAD.values()), ids=list(BAD))
def test_group_turn相关入参不合规_回422_invalid_不发任何请求(adapter, target, payload):
    adapter.expect_error(notify(adapter, payload), 422, "invalid")
    assert target.gw.requests == [] and target.other_gw.requests == [], "校验没过却找了网关"
