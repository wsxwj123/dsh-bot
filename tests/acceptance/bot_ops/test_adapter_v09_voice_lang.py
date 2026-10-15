# -*- coding: utf-8 -*-
"""适配服务 v0.9 验收：语音接口加两个语言键 text_language、voice_language（补充文件 11.3，约定 5.14）。

依据：INTERFACE-适配服务.md 11.3；INTERFACE-适配约定-v0.9-claude侧.md 第一节。不读实现。
两个键存这只 bot 频道目录下 access.json 的 textLanguage、voiceLanguage。GET 回存的原值，没存为 null，不替调用方填缺省。
PUT 成功回写入后的值：v0.8 草案 B.2 写明 PUT 回写入后的 voice_id、tts_model，v0.9 第一节写明 GET、PUT 都加这两个键。
voice_id、tts_model 的旧规则由 test_adapter_v08_voice.py 管，这里只测两个新键，以及它们和旧键一起给时的行为。
"""
import copy
import json
import os

import pytest

from . import adapter_util as au
from .adapter_util import adapter, console_guard  # noqa: F401  夹具

OTHERS = {"dmPolicy": "allowlist", "allowFrom": [au.CHAT_ID],
          "groups": {"-1001234567890": {"requireMention": True, "intro": u"小A：爱猫。"}}}
OLD_ONLY = {"voiceId": "alpha_voice_01", "voiceModel": "s1"}
BASE = dict(OLD_ONLY, textLanguage=u"简体中文", voiceLanguage=u"日文")
#: 接口里的键 → access.json 里的键
FILE_KEYS = {"voice_id": "voiceId", "tts_model": "voiceModel",
             "text_language": "textLanguage", "voice_language": "voiceLanguage"}
TWENTY = u"粤" * 20       # 20 个字符、60 个字节：按字节数数会拒掉


def voice_bot(adapter, voice, enabled=True, mode=0o640):
    """botalpha（没有网关）；access.json = OTHERS 加上 voice 里的键（权限 mode，修改时间在过去）。"""
    bot = adapter.bot("botalpha", enabled=enabled)
    data = copy.deepcopy(OTHERS)
    data.update(voice)
    au.put_file(au.access_path(bot), json.dumps(data, ensure_ascii=False, indent=2), mode=mode)
    return bot


def put_voice(adapter, payload):
    return adapter.call("PUT", au.voice_url("botalpha"), payload)


# ============================================================ GET

READS = {
    "两个语言键都写了_原样回": (BASE, True),
    "只写了voiceLanguage_text_language为null不填简体中文": (dict(OLD_ONLY, voiceLanguage="English"), True),
    "只写了textLanguage_voice_language为null不跟文字语言": ({"textLanguage": u"粤语"}, True),
    "停用的bot也能读": (BASE, False),
}


@pytest.mark.parametrize("voice,enabled", list(READS.values()), ids=list(READS))
def test_GET语音_多回text_language与voice_language_回存的原值_没写为null不填缺省_不动文件(adapter, voice, enabled):
    bot = voice_bot(adapter, voice, enabled=enabled)
    before = au.file_mark(au.access_path(bot))
    body = adapter.ok(adapter.call("GET", au.voice_url("botalpha")))
    for api_key, file_key in FILE_KEYS.items():
        assert api_key in body and body[api_key] == voice.get(file_key), \
            "%s 应为 access.json 里 %s 的原值（没写为 null），实际 %r" % (api_key, file_key, body)
    assert au.file_mark(au.access_path(bot)) == before, "读语音却动了 access.json"


# ============================================================ PUT：写成功

WRITES = {
    "只给text_language_另三个键不动": (BASE, {"text_language": "English"}, True, dict(BASE, textLanguage="English")),
    "原来没有语言键_只给voice_language_加上这个键": (
        OLD_ONLY, {"voice_language": u"日文"}, True, dict(OLD_ONLY, voiceLanguage=u"日文")),
    "两个语言键都给null_都删掉": (BASE, {"text_language": None, "voice_language": None}, True, OLD_ONLY),
    "四个键一起给_都整键替换": (
        OLD_ONLY, {"voice_id": "new_Voice-02", "tts_model": "drama-3", "text_language": u"日文",
                   "voice_language": "English"}, True,
        {"voiceId": "new_Voice-02", "voiceModel": "drama-3", "textLanguage": u"日文", "voiceLanguage": "English"}),
    "停用的bot也能写": (BASE, {"voice_language": u"简体中文"}, False, dict(BASE, voiceLanguage=u"简体中文")),
    "首尾空白去掉后存": (BASE, {"text_language": u"  日文 \t"}, True, dict(BASE, textLanguage=u"日文")),
    "去掉首尾空白后正好20个字符_按字符数不按字节数": (
        BASE, {"voice_language": u" " + TWENTY + u"  "}, True, dict(BASE, voiceLanguage=TWENTY)),
    "只有1个字符": (BASE, {"text_language": u"英"}, True, dict(BASE, textLanguage=u"英")),
}


@pytest.mark.parametrize("start,payload,enabled,want", list(WRITES.values()), ids=list(WRITES))
def test_PUT语音_语言键_给了的整键替换_null删键_没给的不动_存去首尾空白的值_别的键原样_保持权限_回写入后的值(
        adapter, start, payload, enabled, want):
    bot = voice_bot(adapter, start, enabled=enabled)          # access.json 权限 0640
    channel = au.channel_of(bot)
    names = sorted(os.listdir(str(channel)))
    body = adapter.ok(put_voice(adapter, payload))
    data = au.read_access(bot)
    got = {k: v for k, v in data.items() if k in FILE_KEYS.values()}
    assert got == want, "access.json 里语音的四个键应为 %r，实际 %r" % (want, got)
    rest = {k: v for k, v in data.items() if k not in FILE_KEYS.values()}
    assert rest == OTHERS, "access.json 里别的键应原样保留，实际 %r" % rest
    for api_key, file_key in FILE_KEYS.items():
        assert body.get(api_key, "缺这个键") == want.get(file_key), \
            "回的 %s 应是写入后的值 %r，实际 %r" % (api_key, want.get(file_key), body)
    assert au.mode_of(au.access_path(bot)) == 0o640, "应保持原文件权限 0640，实际 %o" % au.mode_of(au.access_path(bot))
    assert sorted(os.listdir(str(channel))) == names, "频道目录里多了或少了文件（临时文件没清？）"
    assert adapter.launchctl.actions() == [], "改语音设置不该动启动项：%r" % adapter.launchctl.actions()


# ============================================================ PUT：不该写的时候没写

FORBIDDEN = {"换行": "\n", "回车": "\r", "左方括号": "[", "右方括号": "]", "小于号": "<", "大于号": ">",
             "左花括号": "{", "右花括号": "}", "反引号": "`", "竖线": "|"}
BAD = dict(
    [("中间含" + label, {"text_language": u"日" + ch + u"文"}) for label, ch in FORBIDDEN.items()]
    + [("空串", {"voice_language": ""}),
       ("只有空白", {"voice_language": u" \t "}),
       ("去掉首尾空白后21个字符", {"text_language": u" " + u"粤" * 21 + u" "}),
       ("不是字符串_数字", {"text_language": 1}),
       ("不是字符串_列表", {"voice_language": [u"日文"]}),
       ("用了文件里的键名textLanguage", {"textLanguage": u"日文"}),
       ("一个语言键合规一个不合规_不许只写一半", {"voice_language": u"日文", "text_language": u"<日文>"}),
       ("语言键合规但tts_model不合规_不许只写一半", {"text_language": "English", "tts_model": "s9"})])


@pytest.mark.parametrize("payload", list(BAD.values()), ids=list(BAD))
def test_PUT语音_语言键不合规_回422invalid_access_json一个字节没变(adapter, payload):
    bot = voice_bot(adapter, BASE)
    before = au.file_mark(au.access_path(bot))
    adapter.expect_error(put_voice(adapter, payload), 422, "invalid")
    assert au.file_mark(au.access_path(bot)) == before, "请求体不合规却动了 access.json"
