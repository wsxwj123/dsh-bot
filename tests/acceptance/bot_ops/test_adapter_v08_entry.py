# -*- coding: utf-8 -*-
"""适配服务 v0.8 验收：人设、语音四个新入口共有的规矩。

依据：INTERFACE-适配服务.md 第 2、3 节与 10.2、10.3；INTERFACE-适配约定-v0.8-dsh侧.md A、B；
BRIEF-适配服务.md「归属边界」（不读写 ~/.claude/channels/**）。不读实现。
"""
import json
import os

import pytest

from . import adapter_util as au
from .adapter_util import adapter, console_guard  # noqa: F401  夹具

PERSONA = u"# 小A 的人设\n爱猫。\n"
ACCESS = {"dmPolicy": "allowlist", "allowFrom": [au.CHAT_ID], "voiceId": "alpha_voice_01", "voiceModel": "s1"}
URL = {"persona": au.persona_url, "voice": au.voice_url}
GOOD_PUT = {
    "persona": {"text": u"# 新人设\n", "version": au.persona_version(PERSONA)},
    "voice": {"voice_id": "new_voice_02", "tts_model": "s2-pro"},
}
OLDSYS_PERSONA = u"# 另一套系统的人设 OLDSYS-PERSONA-8830\n"
OLDSYS_VOICE = "OLDSYS-VOICE-5521"


def alpha_with_files(adapter):
    """botalpha：频道目录下有人设和 access.json（修改时间在过去）。"""
    bot = adapter.bot("botalpha")
    au.put_file(au.persona_path(bot), PERSONA)
    au.put_file(au.access_path(bot), json.dumps(ACCESS))
    return bot


def marks(bot):
    return au.file_mark(au.persona_path(bot)), au.file_mark(au.access_path(bot))


def send(adapter, method, what, bot_id="botalpha", **kw):
    body = GOOD_PUT[what] if method == "PUT" else au.NO_BODY
    return adapter.call(method, URL[what](bot_id), body, **kw)


AUTH = {
    "读人设_没带令牌": ("GET", "persona", None),
    "存人设_令牌不对": ("PUT", "persona", "Bearer " + au.WRONG_TOKEN),
    "读语音_令牌不对": ("GET", "voice", "Bearer " + au.WRONG_TOKEN),
    "存语音_没带令牌": ("PUT", "voice", None),
}


@pytest.mark.parametrize("method,what,auth", list(AUTH.values()), ids=list(AUTH))
def test_新入口_没过鉴权_回固定401_不读不写(adapter, method, what, auth):
    bot = alpha_with_files(adapter)
    before = marks(bot)
    au.assert_unauthorized(send(adapter, method, what, auth=auth))
    assert marks(bot) == before, "没过鉴权却动了人设或 access.json"
    assert not (adapter.sandbox.dsh_home / "backups").exists(), "没过鉴权却产生了备份"


BAD_ID = {
    "存人设_编码的斜杠": ("PUT", "persona", "a%2Fb", 422, "invalid"),
    "读人设_没有这只bot": ("GET", "persona", "ghost", 404, "unknown_bot"),
    "存语音_两个点": ("PUT", "voice", "..", 422, "invalid"),
    "读语音_65个字符": ("GET", "voice", "a" * 65, 422, "invalid"),
    "存语音_没有这只bot": ("PUT", "voice", "ghost", 404, "unknown_bot"),
}


@pytest.mark.parametrize("method,what,bot_id,status,code", list(BAD_ID.values()), ids=list(BAD_ID))
def test_新入口_bot_id不合规回422_没有这只回404_什么都不写(adapter, method, what, bot_id, status, code):
    bot = alpha_with_files(adapter)
    before = marks(bot)
    adapter.expect_error(send(adapter, method, what, bot_id=bot_id), status, code)
    assert marks(bot) == before, "别的 bot 的文件被动了"
    assert not (adapter.sandbox.dsh_home / "backups").exists(), "bot_id 不对却产生了备份"


@pytest.mark.parametrize("what", ["persona", "voice"])
def test_新入口_方法不对_POST回405method_not_allowed_不写(adapter, what):
    bot = alpha_with_files(adapter)
    before = marks(bot)
    adapter.expect_error(adapter.call("POST", URL[what]("botalpha"), GOOD_PUT[what]), 405, "method_not_allowed")
    assert marks(bot) == before, "方法不对却动了文件"


OUTSIDE = {
    "读人设_配置指到旧系统频道目录": ("GET", "persona", "cfg"),
    "存人设_配置指到旧系统频道目录": ("PUT", "persona", "cfg"),
    "读人设_频道目录是软链落到家目录外": ("GET", "persona", "link"),
    "存人设_频道目录是软链落到家目录外": ("PUT", "persona", "link"),
    "读语音_配置指到旧系统频道目录": ("GET", "voice", "cfg"),
    "存语音_配置指到旧系统频道目录": ("PUT", "voice", "cfg"),
}


@pytest.mark.parametrize("method,what,how", list(OUTSIDE.values()), ids=list(OUTSIDE))
def test_新入口_频道目录落在本系统家目录外_那边的文件不读不写_回失败(adapter, method, what, how):
    """10.2「落点不在本系统家目录下的不碰」；BRIEF 不读写 ~/.claude/channels/**。接口没写回什么码，只断言失败。"""
    bot = adapter.bot("botalpha")
    old = adapter.sandbox.home / ".claude" / "channels" / "oldbot"
    au.put_file(old / "CLAUDE.md", OLDSYS_PERSONA)
    au.put_file(old / "access.json", json.dumps({"voiceId": OLDSYS_VOICE, "voiceModel": "s1"}))
    if how == "cfg":
        au.set_cfg(bot, bot_channel_path=str(old))
    else:
        link = bot.botdir / "channel-link"
        os.symlink(str(old), str(link))
        au.set_cfg(bot, bot_channel_path=str(link))
    snap = lambda: (au.file_mark(old / "CLAUDE.md"), au.file_mark(old / "access.json"), sorted(os.listdir(str(old))))
    before = snap()
    body = au.NO_BODY
    if method == "PUT":     # 人设的版本号按那边的内容算：不设防的实现会以为对得上而写进去
        body = GOOD_PUT[what] if what == "voice" else {"text": u"# 新人设\n", "version": au.persona_version(OLDSYS_PERSONA)}
    resp = adapter.call(method, URL[what]("botalpha"), body)
    text = au.response_text(resp)
    assert "OLDSYS-PERSONA-8830" not in text and OLDSYS_VOICE not in text, "家目录外的内容被读出来回给了调用方"
    assert resp.status_code >= 400 and au.body_of(resp).get("ok") is False, \
        "频道目录落在家目录外却报了成功：%s %r" % (resp.status_code, au.body_of(resp))
    assert au.body_of(resp).get("code") != "not_found", "这个入口还不存在（404 not_found），没测到拒绝逻辑"
    assert snap() == before, "家目录外（旧系统频道目录）的文件被动了"
    assert au.persona_backups(adapter.sandbox, "botalpha") == [], "不该产生备份"
