# -*- coding: utf-8 -*-
"""适配服务 v0.7 验收：群设置 GET / POST /adapter/v1/bots/{bot_id}/groups（补充文件 9.5，约定 5.12.1、5.12.2）。

依据：INTERFACE-适配服务.md 9.5；INTERFACE-适配约定-v0.7摘录.md 5.12.1、5.12.2。不读实现。
access.json 放在这只 bot 的频道目录（配置里的 bot_channel_path）下。顶层的 dmPolicy、allowFrom、voice
代表"私聊名单、语音音色"这类别的键，形状是猜的，只用来断言"原样保留、一律不回"。
鉴权与 bot_id 校验在 test_adapter_v06_enable.py 的入口校验里。
"""
import os
from types import SimpleNamespace

import pytest

from . import adapter_util as au
from . import botops_util as u
from .adapter_util import adapter, console_guard  # noqa: F401  夹具

GROUP_A = "-1001234567890"
GROUP_B = "-1009998887776"
GROUP_NEW = "-1005550001112"
VOICE_ID = "FAKE-VOICE-ID-do-not-return-77"
FULL_A = {"requireMention": True, "allowFrom": ["111222333"], "selfAliases": ["小A"],
          "otherBotUsernames": ["other_bravo_bot"], "otherBotAliases": ["小B"], "intro": "小A：爱猫，说话慢。"}
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def sample_access():
    return {"dmPolicy": "allowlist", "allowFrom": [au.CHAT_ID], "voice": {"voice_id": VOICE_ID},
            "groups": {GROUP_A: dict(FULL_A), GROUP_B: {"requireMention": False}}}


def shaped(**given):
    """接口回的群设置形状：六个键都在，文件里没写的为 null。"""
    out = dict.fromkeys(au.GROUP_KEYS)
    out.update(given)
    return out


def post_groups(adapter, payload, raw=None):
    return adapter.call("POST", au.groups_url("botalpha"), payload, raw=raw)


@pytest.fixture
def grp(adapter):
    """botalpha 在跑，网关 health 报用户名 alpha_fake_bot；access.json 是 sample_access（权限 0640）。"""
    gw = au.serve_health(adapter.model_gateway(), username="alpha_fake_bot")
    alpha = adapter.running_bot("botalpha", gw)
    au.write_access(alpha, sample_access(), mode=0o640)
    return SimpleNamespace(gw=gw, alpha=alpha)


# ============================================================ 读

def test_GET_群设置六键照原样_没写的为null_其他键一律不回_username取自网关health(adapter, grp):
    before = au.access_path(grp.alpha).read_bytes()
    resp = adapter.call("GET", au.groups_url("botalpha"))
    body = adapter.ok(resp)
    assert body.get("username") == "alpha_fake_bot", body
    assert body.get("groups") == {GROUP_A: shaped(**FULL_A), GROUP_B: shaped(requireMention=False)}, body
    text = au.response_text(resp)
    for leak in (VOICE_ID, au.CHAT_ID, "dmPolicy"):
        assert leak not in text, "access.json 里群以外的键不该回：响应里出现了 %r" % leak
    assert au.access_path(grp.alpha).read_bytes() == before, "读群设置却改了 access.json"
    assert grp.gw.writes() == [], "读群设置却往网关写了东西：%r" % grp.gw.writes()


NULL_NAME = {"网关端口没人听": "down", "health里username为null": "null", "网关没有health接口": "404"}


@pytest.mark.parametrize("mode", list(NULL_NAME.values()), ids=list(NULL_NAME))
def test_GET_网关不在跑或问不到用户名_username为null_群设置照回(adapter, mode):
    if mode == "down":
        alpha = adapter.bot("botalpha", gw_port=au.free_port())
    else:
        gw = adapter.model_gateway()
        if mode == "null":
            au.serve_health(gw, username=None)
        alpha = adapter.running_bot("botalpha", gw)
    au.write_access(alpha, sample_access())
    body = adapter.ok(adapter.call("GET", au.groups_url("botalpha")))
    assert "username" in body and body["username"] is None, body
    assert (body.get("groups") or {}).get(GROUP_A) == shaped(**FULL_A), body


@pytest.mark.parametrize("how", ["不是合法JSON", "读不出"])
def test_GET_access_json坏了_回500_internal_不当成空(adapter, grp, how):
    p = au.access_path(grp.alpha)
    if how == "不是合法JSON":
        p.write_text('{"groups": {', encoding="utf-8")
    else:
        if os.name == "nt" or IS_ROOT:
            pytest.skip("要靠文件权限造「读不出」：Windows 与 root 下造不出来")
        p.chmod(0)
    adapter.expect_error(adapter.call("GET", au.groups_url("botalpha")), 500, "internal")


# ============================================================ 写

@pytest.mark.parametrize("enabled", [True, False], ids=["在跑的bot", "停用的bot也能改"])
def test_POST_进新群_只写给了的键_其他原样_权限不变_原子替换_不重启(adapter, enabled):
    gw = adapter.model_gateway()
    if enabled:
        alpha = adapter.running_bot("botalpha", gw)
    else:
        alpha = adapter.bot("botalpha", enabled=False, gw_port=gw.port)
    p = au.write_access(alpha, sample_access(), mode=0o640)
    bp = au.write_access(adapter.bot("botbravo"), {"groups": {GROUP_A: {"requireMention": True}}})
    bravo_bytes, ino_before = bp.read_bytes(), p.stat().st_ino
    names_before = sorted(x.name for x in p.parent.iterdir())
    policy = {"requireMention": True, "intro": "新来的小A。", "otherBotUsernames": ["other_bravo_bot"]}
    body = adapter.ok(post_groups(adapter, {"chat_id": GROUP_NEW, "join": True, "policy": policy}))
    assert body.get("in_group") is True and body.get("policy") == shaped(**policy), body
    want = sample_access()
    want["groups"][GROUP_NEW] = policy
    assert au.read_access(alpha) == want, "只该多出这一个群号的条目（没给的键不写），其他键原样保留"
    assert u.mode_of(p) == 0o640, "应保持原文件权限 0640，实际 %o" % u.mode_of(p)
    assert p.stat().st_ino != ino_before, "应先写临时文件再改名（原子写入），文件却是原地改的"
    extra = sorted(set(x.name for x in p.parent.iterdir()) - set(names_before))
    assert all("lock" in n.lower() for n in extra), "频道目录里留下了临时文件：%r（锁文件除外）" % extra
    assert adapter.launchctl.calls() == [], "改群设置不用重启，却调了启动项管理程序"
    assert gw.writes() == [], "改群设置却往网关写了东西：%r" % gw.writes()
    assert bp.read_bytes() == bravo_bytes, "只动这只 bot 自己的 access.json，botbravo 的却变了"


def test_POST_改设置_给了的键整键替换_null删键_没给的保持原样(adapter, grp):
    policy = {"allowFrom": ["444555666"], "intro": None, "requireMention": False}
    body = adapter.ok(post_groups(adapter, {"chat_id": GROUP_A, "join": True, "policy": policy}))
    entry = {k: FULL_A[k] for k in ("selfAliases", "otherBotUsernames", "otherBotAliases")}
    entry.update(allowFrom=["444555666"], requireMention=False)
    assert body.get("in_group") is True and body.get("policy") == shaped(**entry), body
    want = sample_access()
    want["groups"][GROUP_A] = entry
    assert au.read_access(grp.alpha) == want, "intro 应删掉、allowFrom 整个换掉，没给的键与别的群原样"


@pytest.mark.parametrize("chat_id", [GROUP_A, GROUP_NEW], ids=["在群里", "本来就不在群里"])
def test_POST_退群_删掉这个群号连同intro_回in_group假与policy为null(adapter, grp, chat_id):
    body = adapter.ok(post_groups(adapter, {"chat_id": chat_id, "join": False}))
    assert body.get("in_group") is False and "policy" in body and body["policy"] is None, body
    want = sample_access()
    want["groups"].pop(chat_id, None)
    assert au.read_access(grp.alpha) == want, "只该删掉这一个群号，别的原样"


def enter(policy=None, **over):
    d = {"chat_id": GROUP_NEW, "join": True, "policy": {"requireMention": True} if policy is None else policy}
    d.update(over)
    return d


def without(d, key):
    d = dict(d)
    d.pop(key)
    return d


BAD = {
    "chat_id缺失": (without(enter(), "chat_id"), None),
    "chat_id是数字": (enter(chat_id=-1005550001112), None),
    "chat_id没有横线": (enter(chat_id="1005550001112"), None),
    "chat_id横线后21位": (enter(chat_id="-" + "1" * 21), None),
    "chat_id带字母": (enter(chat_id="-100abc"), None),
    "join缺失": (without(enter(), "join"), None),
    "join是字符串": (enter(join="true"), None),
    "policy是列表": (enter(policy=["requireMention"]), None),
    "policy有六个以外的键": (enter(policy={"requireMention": True, "voice": "x"}), None),
    "requireMention是字符串": (enter(policy={"requireMention": "yes"}), None),
    "allowFrom不是列表": (enter(policy={"allowFrom": "111222333"}), None),
    "allowFrom有空串": (enter(policy={"allowFrom": [""]}), None),
    "allowFrom有一项65个字符": (enter(policy={"allowFrom": ["1" * 65]}), None),
    "allowFrom有51项": (enter(policy={"allowFrom": [str(i) for i in range(51)]}), None),
    "selfAliases有一项不是字符串": (enter(policy={"selfAliases": [1]}), None),
    "otherBotAliases有一项是null": (enter(policy={"otherBotAliases": [None]}), None),
    "otherBotUsernames有一项4个字符": (enter(policy={"otherBotUsernames": ["abcd"]}), None),
    "otherBotUsernames有一项33个字符": (enter(policy={"otherBotUsernames": ["a" * 33]}), None),
    "otherBotUsernames带横线": (enter(policy={"otherBotUsernames": ["bad-name_bot"]}), None),
    "otherBotUsernames带at号": (enter(policy={"otherBotUsernames": ["@alpha_bot"]}), None),
    "intro是数字": (enter(policy={"intro": 123}), None),
    "intro有4001个字符": (enter(policy={"intro": "字" * 4001}), None),
    "请求体是数组": (au.NO_BODY, "[]"),
    "坏JSON": (au.NO_BODY, "{bad"),
}


@pytest.mark.parametrize("payload,raw", list(BAD.values()), ids=list(BAD))
def test_POST_不合规_回422_invalid_什么都不写(adapter, grp, payload, raw):
    frozen = u.freeze_roots(grp.alpha.botdir / "channel")
    adapter.expect_error(post_groups(adapter, payload, raw=raw), 422, "invalid")
    u.assert_roots_untouched(frozen)
    assert adapter.launchctl.calls() == [], "校验没过却调了启动项管理程序"


EDGE = {
    "chat_id横线后1位": ("-1", {"requireMention": True}),
    "chat_id横线后20位": ("-" + "9" * 20, {"requireMention": True}),
    "列表恰好50项每项64个字符": (GROUP_NEW, {"allowFrom": ["%064d" % i for i in range(50)]}),
    "otherBotUsernames恰好5个字符": (GROUP_NEW, {"otherBotUsernames": ["ab_12"]}),
    "otherBotUsernames恰好32个字符": (GROUP_NEW, {"otherBotUsernames": ["B" * 30 + "_9"]}),
    "intro恰好4000个中文字符": (GROUP_NEW, {"intro": "介" * 4000}),
    "intro是空串": (GROUP_NEW, {"intro": ""}),
}


@pytest.mark.parametrize("chat_id,policy", list(EDGE.values()), ids=list(EDGE))
def test_POST_边界上的合规入参_照常写入(adapter, grp, chat_id, policy):
    body = adapter.ok(post_groups(adapter, {"chat_id": chat_id, "join": True, "policy": policy}))
    assert body.get("in_group") is True and body.get("policy") == shaped(**policy), body
    assert au.read_access(grp.alpha)["groups"].get(chat_id) == policy, "给的值应原样写进 access.json"
