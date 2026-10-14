# -*- coding: utf-8 -*-
"""适配服务 v0.8 验收：语音 GET / PUT /adapter/v1/bots/{bot_id}/voice（补充文件 10.3，v0.8 草案 B.1、B.2）。

依据：INTERFACE-适配服务.md 10.3；INTERFACE-适配约定-v0.8-dsh侧.md B.1、B.2。不读实现。
voice_id、tts_model 存在这只 bot 频道目录下 access.json 的 voiceId、voiceModel。
access.json 里语音以外的键（dmPolicy、allowFrom、groups）形状照 test_adapter_v07_groups 的样子，
只用来断言"原样保留、一律不回"。
鉴权、bot_id 校验、方法不对、频道目录落在家目录外：放在 test_adapter_v08_entry.py。
"""
import copy
import json
import os

import pytest

from . import adapter_util as au
from .adapter_util import adapter, console_guard  # noqa: F401  夹具

ABSENT = object()       # 期望 access.json 里没有这个键
OTHERS = {"dmPolicy": "allowlist", "allowFrom": [au.CHAT_ID],
          "groups": {"-1001234567890": {"requireMention": True, "intro": u"小A：爱猫。"}}}
BASE_VOICE = {"voiceId": "alpha_voice_01", "voiceModel": "s1"}


def voice_bot(adapter, voice, enabled=True, mode=0o640):
    """botalpha（没有网关）；access.json = OTHERS 加上 voice 里的键（权限 mode，修改时间在过去）。"""
    bot = adapter.bot("botalpha", enabled=enabled)
    data = copy.deepcopy(OTHERS)
    data.update(voice)
    au.put_file(au.access_path(bot), json.dumps(data, ensure_ascii=False, indent=2), mode=mode)
    return bot


def put_voice(adapter, payload, raw=None):
    return adapter.call("PUT", au.voice_url("botalpha"), payload, raw=raw)


def as_reply(want):
    return None if want is ABSENT else want


# ============================================================ GET

READS = {
    "两个键都写了": ({"voiceId": "alpha_voice_01", "voiceModel": "s2-pro"}, True, "alpha_voice_01", "s2-pro"),
    "只写了voiceId_tts_model为null": ({"voiceId": "alpha_voice_01"}, True, "alpha_voice_01", None),
    "两个键都没写_都为null": ({}, True, None, None),
    "停用的bot也能读": (BASE_VOICE, False, "alpha_voice_01", "s1"),
}


@pytest.mark.parametrize("voice,enabled,want_id,want_model", list(READS.values()), ids=list(READS))
def test_GET语音_回voice_id与tts_model_没写的为null_别的键不回_不动文件(adapter, voice, enabled, want_id, want_model):
    bot = voice_bot(adapter, voice, enabled=enabled)
    before = au.file_mark(au.access_path(bot))
    resp = adapter.call("GET", au.voice_url("botalpha"))
    body = adapter.ok(resp)
    assert "voice_id" in body and body["voice_id"] == want_id, "voice_id 应为 %r，实际 %r" % (want_id, body)
    assert "tts_model" in body and body["tts_model"] == want_model, "tts_model 应为 %r，实际 %r" % (want_model, body)
    text = au.response_text(resp)
    for leak in (au.CHAT_ID, "dmPolicy", "requireMention"):
        assert leak not in text, "access.json 里语音以外的东西不该回：响应里出现了 %r" % leak
    assert au.file_mark(au.access_path(bot)) == before, "读语音却动了 access.json"


@pytest.mark.parametrize("broken", [False, True], ids=["access_json不存在", "access_json不是合法JSON"])
def test_GET语音_access_json不存在或读不出_回500internal_也不凭空建文件(adapter, broken):
    bot = adapter.bot("botalpha")
    path = au.access_path(bot)
    if broken:
        au.put_file(path, '{"voiceId": "alpha_voice_01", ', mode=0o640)
    before = au.file_mark(path)
    adapter.expect_error(adapter.call("GET", au.voice_url("botalpha")), 500, "internal")
    assert au.file_mark(path) == before, "读不出时 access.json 被动了（或凭空建了一份）"


# ============================================================ PUT：写成功

WRITES = {
    "两个都给_都整键替换": ({"voice_id": "new_Voice-02", "tts_model": "s2.1-pro"}, True, "new_Voice-02", "s2.1-pro"),
    "只给voice_id_voiceModel保持原样": ({"voice_id": "new_Voice-02"}, True, "new_Voice-02", "s1"),
    "只给tts_model_voiceId保持原样": ({"tts_model": "drama-3-preview"}, True, "alpha_voice_01", "drama-3-preview"),
    "voice_id给null_删掉voiceId这个键": ({"voice_id": None}, True, ABSENT, "s1"),
    "tts_model给null_删掉voiceModel这个键": ({"tts_model": None}, True, "alpha_voice_01", ABSENT),
    "停用的bot也能写": ({"voice_id": "new_Voice-02"}, False, "new_Voice-02", "s1"),
}


@pytest.mark.parametrize("payload,enabled,want_id,want_model", list(WRITES.values()), ids=list(WRITES))
def test_PUT语音_给了的键整键替换_null删键_没给的不动_别的键原样_保持权限_不动启动项(
        adapter, payload, enabled, want_id, want_model):
    bot = voice_bot(adapter, BASE_VOICE, enabled=enabled)          # access.json 权限 0640
    channel = au.channel_of(bot)
    names = sorted(os.listdir(str(channel)))
    body = adapter.ok(put_voice(adapter, payload))
    assert body.get("voice_id", "缺这个键") == as_reply(want_id), "回的 voice_id 应是写入后的值：%r" % (body,)
    assert body.get("tts_model", "缺这个键") == as_reply(want_model), "回的 tts_model 应是写入后的值：%r" % (body,)
    data = au.read_access(bot)
    for file_key, want in (("voiceId", want_id), ("voiceModel", want_model)):
        if want is ABSENT:
            assert file_key not in data, "值给 null 应删掉 %s 这个键，实际 %r" % (file_key, data)
        else:
            assert data.get(file_key) == want, "%s 应为 %r，实际 %r" % (file_key, want, data)
    rest = {k: v for k, v in data.items() if k not in ("voiceId", "voiceModel")}
    assert rest == OTHERS, "access.json 里别的键应原样保留，实际 %r" % rest
    assert au.mode_of(au.access_path(bot)) == 0o640, "应保持原文件权限 0640，实际 %o" % au.mode_of(au.access_path(bot))
    assert sorted(os.listdir(str(channel))) == names, "频道目录里多了或少了文件（临时文件没清？）"
    assert adapter.launchctl.actions() == [], "改语音不用重启，却动了启动项：%r" % adapter.launchctl.actions()


ACCEPTED = dict(
    [("tts_model=" + m, ("tts_model", "voiceModel", m)) for m in au.TTS_MODELS]
    + [("voice_id只有1个字符", ("voice_id", "voiceId", "a")),
       ("voice_id正好64个字符_含大小写数字下划线横线", ("voice_id", "voiceId", ("Ab9_-" * 13)[:64]))])


@pytest.mark.parametrize("api_key,file_key,value", list(ACCEPTED.values()), ids=list(ACCEPTED))
def test_PUT语音_合规取值都照写_白名单五个模型与长度1和64的voice_id(adapter, api_key, file_key, value):
    bot = voice_bot(adapter, BASE_VOICE)
    body = adapter.ok(put_voice(adapter, {api_key: value}))
    assert body.get(api_key) == value, "回的 %s 应为 %r，实际 %r" % (api_key, value, body)
    assert au.read_access(bot).get(file_key) == value, "access.json 的 %s 应写成 %r" % (file_key, value)


# ============================================================ PUT：不该写的时候没写

BAD = {
    "空对象_一个键都没给": ({}, None),
    "多了别的键": ({"voice_id": "new_voice_02", "speed": 1.2}, None),
    "用了文件里的键名voiceId": ({"voiceId": "new_voice_02"}, None),
    "voice_id有65个字符": ({"voice_id": "a" * 65}, None),
    "voice_id是空串": ({"voice_id": ""}, None),
    "voice_id末尾带换行": ({"voice_id": "abc\n"}, None),
    "voice_id含斜杠和点": ({"voice_id": "../voice"}, None),
    "voice_id不是字符串": ({"voice_id": 12345}, None),
    "tts_model不在白名单": ({"tts_model": "s3"}, None),
    "tts_model大小写不对": ({"tts_model": "S1"}, None),
    "一个合规一个不合规_不许只写一半": ({"voice_id": "new_voice_02", "tts_model": "s9"}, None),
    "请求体是数组": (None, "[]"),
    "坏JSON": (None, '{"voice_id": '),
}


@pytest.mark.parametrize("payload,raw", list(BAD.values()), ids=list(BAD))
def test_PUT语音_请求体不合规_回422invalid_access_json一个字节没变(adapter, payload, raw):
    bot = voice_bot(adapter, BASE_VOICE)
    before = au.file_mark(au.access_path(bot))
    adapter.expect_error(put_voice(adapter, payload, raw=raw), 422, "invalid")
    assert au.file_mark(au.access_path(bot)) == before, "请求体不合规却动了 access.json"


def test_PUT语音_access_json不是合法JSON_不覆盖原文件_回失败(adapter):
    """B.2 没写这种情况回什么码；只断言最要紧的：失败，且坏文件原样留着（不被只含语音两键的新文件盖掉）。"""
    bot = adapter.bot("botalpha")
    path = au.put_file(au.access_path(bot), '{"dmPolicy": "allowlist", "allowFrom": [', mode=0o640)
    before = au.file_mark(path)
    resp = put_voice(adapter, {"voice_id": "new_voice_02"})
    body = au.body_of(resp)
    assert resp.status_code >= 400 and body.get("ok") is False, "读不出原文件时不该报成功：%s %r" % (resp.status_code, body)
    assert body.get("code") != "not_found", "这个入口还不存在（404 not_found），没测到拒绝逻辑"
    assert au.file_mark(path) == before, "读不出的 access.json 被覆盖了"
