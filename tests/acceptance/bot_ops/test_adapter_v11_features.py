# -*- coding: utf-8 -*-
"""适配服务 v1.1 验收：功能开关 GET / PUT /adapter/v1/bots/{bot_id}/features（约定 v1.1 5.19）。

dsh 侧把开关存在这只 bot 自己的 yml 的 features 块里；存储里没有的键按上线缺省取（前八个开，nsfw 有技能才开）。
必需项：voice 要音色编号（access.json 的 voiceId），image 要 NovelAI 令牌（凭据文件或 novelai 技能目录的
.env.local），life 要生活设定（yml 的 life_config 指向的文件）。
"""
import json

import pytest
import yaml

from . import adapter_util as au
from .adapter_util import adapter, console_guard  # noqa: F401  夹具

KEYS = ["voice", "image", "stickers", "moments", "proactive", "life", "relationship", "groups", "nsfw"]
LABELS = ["发语音", "生图", "表情包", "朋友圈", "主动找你聊", "生活作息", "关系进展", "群聊", "成人内容"]


def features_url(bot_id):
    return "%s/%s/features" % (au.BOTS, bot_id)


def put(adapter, bot_id, features):
    return adapter.call("PUT", features_url(bot_id), {"features": features})


def cfg_of(bot):
    return yaml.safe_load(bot.yml.read_text(encoding="utf-8"))


@pytest.fixture
def ready(adapter, tmp_path):
    """一只在跑的 bot，三项必需的设置都齐：音色编号、NovelAI 令牌（技能目录的 .env.local）、生活设定文件。"""
    gw = au.serve_health(adapter.model_gateway(), username="alpha_fake_bot")
    bot = adapter.running_bot("botalpha", gw)
    au.write_access(bot, {"allowFrom": [au.CHAT_ID], "voiceId": "fake-voice-1"})
    skill = tmp_path / "novelai-skill"
    skill.mkdir()
    (skill / ".env.local").write_text("NOVELAI_BEARER_TOKEN=pst-fake-token-1234567890\n", encoding="utf-8")
    life = tmp_path / "life.yml"
    life.write_text("events: []\n", encoding="utf-8")
    au.set_cfg(bot, chat_id=au.CHAT_ID, life_config=str(life), gateway={"image_skill_dir": str(skill)})
    return bot


def test_GET_九个键按页面顺序带中文名_老bot没写的前八个开_nsfw没有技能就关(adapter, ready):
    body = adapter.ok(adapter.call("GET", features_url("botalpha")))
    assert body["keys"] == [{"key": k, "label": l} for k, l in zip(KEYS, LABELS)], body["keys"]
    assert body["features"] == dict(dict.fromkeys(KEYS[:8], True), nsfw=False), body["features"]
    assert set(body["requirements"]) == set(KEYS), body["requirements"]
    for k in KEYS:
        r = body["requirements"][k]
        assert r == {"ready": True, "missing": [], "notes": []}, (k, r)
    assert body.get("warnings", []) == [], body


def test_GET_已经有nsfw技能的老bot_nsfw取开(adapter, ready):
    au.put_file(au.skill_path(ready, "nsfw"), "---\nname: skill-nsfw\ndescription: x\n---\n正文\n")
    assert adapter.ok(adapter.call("GET", features_url("botalpha")))["features"]["nsfw"] is True


def test_GET_必需项缺什么就报什么_停用的bot也能读(adapter, tmp_path):
    bot = adapter.bot("botalpha", enabled=False)
    au.write_access(bot, {"allowFrom": [au.CHAT_ID]})
    empty_skill = tmp_path / "empty-skill"
    empty_skill.mkdir()
    au.set_cfg(bot, gateway={"image_skill_dir": str(empty_skill)})
    body = adapter.ok(adapter.call("GET", features_url("botalpha")))
    req = body["requirements"]
    for k in ("voice", "image", "life"):
        assert req[k]["ready"] is False and len(req[k]["missing"]) == 1, (k, req[k])
        m = req[k]["missing"][0]
        assert set(m) == {"code", "message"} and m["code"] and "去" in m["message"], (k, m)
    for k in ("stickers", "moments", "proactive", "relationship", "groups", "nsfw"):
        assert req[k]["ready"] is True, (k, req[k])
    assert "pst-fake" not in au.response_text(adapter.call("GET", features_url("botalpha")))


def test_GET_存储里的值不是真假_按关报_带一句说明(adapter, ready):
    au.set_cfg(ready, features={"voice": "yes", "image": True})
    body = adapter.ok(adapter.call("GET", features_url("botalpha")))
    assert body["features"]["voice"] is False and body["features"]["image"] is True, body["features"]
    assert any("voice" in w for w in body["warnings"]), body["warnings"]


def test_PUT_只改给了的键_写进yml的features块_回生效说明与写后的开关(adapter, ready):
    resp = put(adapter, "botalpha", {"stickers": False})
    body = adapter.ok(resp)
    assert body["features"]["stickers"] is False and body["features"]["voice"] is True, body
    assert body["changed"] == ["stickers"] and body["applied"] is False and body["queued"] is True, body
    assert isinstance(body["message"], str) and body["message"], body
    assert cfg_of(ready)["features"] == {"stickers": False}, cfg_of(ready)
    body = adapter.ok(put(adapter, "botalpha", {"voice": False, "stickers": False}))
    assert body["changed"] == ["voice"], body
    assert cfg_of(ready)["features"] == {"stickers": False, "voice": False}, cfg_of(ready)
    again = adapter.ok(adapter.call("GET", features_url("botalpha")))["features"]
    assert again["stickers"] is False and again["voice"] is False and again["moments"] is True, again


def test_PUT_块状写法的yml_只动features那几行_别的字节原样(adapter, ready):
    cfg = cfg_of(ready)
    text = "# 手写的配置\n" + yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)
    ready.yml.write_text(text, encoding="utf-8")
    adapter.ok(put(adapter, "botalpha", {"moments": False}))
    after = ready.yml.read_text(encoding="utf-8")
    assert after.startswith(text), "原有内容应原样保留，新块加在后面"
    assert yaml.safe_load(after)["features"] == {"moments": False}
    adapter.ok(put(adapter, "botalpha", {"moments": True, "groups": False}))
    assert yaml.safe_load(ready.yml.read_text(encoding="utf-8"))["features"] == {"moments": True, "groups": False}


def test_PUT_停用的bot_写进去_queued为假(adapter, ready):
    au.set_cfg(ready, enabled=False)
    body = adapter.ok(put(adapter, "botalpha", {"proactive": False}))
    assert body["queued"] is False and body["changed"] == ["proactive"], body


def test_PUT_没改动_applied为真_不写盘(adapter, ready):
    before = ready.yml.read_bytes()
    body = adapter.ok(put(adapter, "botalpha", {"voice": True}))
    assert body["changed"] == [] and body["applied"] is True and body["queued"] is False, body
    assert ready.yml.read_bytes() == before


BAD = {
    "不认识的键": ({"features": {"teleport": True}}, ["teleport"]),
    "值不是真假": ({"features": {"voice": "off"}}, ["voice"]),
    "空的features": ({"features": {}}, None),
    "多了别的键": ({"features": {"voice": False}, "x": 1}, None),
    "features不是对象": ({"features": ["voice"]}, None),
}


@pytest.mark.parametrize("payload,fields", list(BAD.values()), ids=list(BAD))
def test_PUT_校验不过_422_invalid_一个字都不写(adapter, ready, payload, fields):
    before = ready.yml.read_bytes()
    resp = adapter.call("PUT", features_url("botalpha"), payload)
    adapter.expect_error(resp, 422, "invalid")
    if fields:
        assert sorted(au.body_of(resp).get("fields", {})) == fields, au.body_of(resp)
    assert ready.yml.read_bytes() == before


def test_PUT_打开缺必需项_422_requirement_missing_带fields_一个字都不写_关掉不拦(adapter, ready):
    au.set_cfg(ready, features={"voice": False, "life": False})
    au.write_access(ready, {"allowFrom": [au.CHAT_ID]})                 # 没有音色编号
    cfg = cfg_of(ready)
    cfg.pop("life_config")
    ready.yml.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    before = ready.yml.read_bytes()
    resp = put(adapter, "botalpha", {"voice": True, "life": True, "stickers": False})
    adapter.expect_error(resp, 422, "requirement_missing")
    body = au.body_of(resp)
    assert body["message"] == "打不开：缺少必需的设置", body
    assert sorted(body["fields"]) == ["life", "voice"] and all(body["fields"].values()), body
    assert ready.yml.read_bytes() == before
    adapter.ok(put(adapter, "botalpha", {"stickers": False}))           # 关掉、改别的照常


def test_PUT_没有这只bot回404_只许GET和PUT_能力键报true(adapter, ready):
    adapter.expect_error(put(adapter, "nobody", {"voice": False}), 404, "unknown_bot")
    assert adapter.call("POST", features_url("botalpha"), {}).status_code == 405
    meta = adapter.ok(adapter.call("GET", au.META))
    assert meta["capabilities"].get("bot.features") is True, meta


# ---------------------------------------------------------------- 对投递通知的影响（5.19.6）

def notify(adapter, source, **extra):
    body = {"source": source, "text": "[moment-interaction] 一条通知", "key": "k-%s" % source}
    body.update(extra)
    return adapter.call("POST", au.notify_url("botalpha"), body)


@pytest.mark.parametrize("source", ["user_moment", "moment_reply", "moment_image"])
def test_朋友圈关着_三种朋友圈通知回409_feature_off_不找网关(adapter, ready, source):
    au.set_cfg(ready, features={"moments": False})
    gw_writes = len(adapter._gateways[-1].writes())
    resp = notify(adapter, source)
    adapter.expect_error(resp, 409, "feature_off")
    assert "朋友圈" in au.body_of(resp)["message"]
    assert len(adapter._gateways[-1].writes()) == gw_writes


def test_群聊关着_group_turn回409_feature_off(adapter, ready):
    group = "-1001234567890"
    au.write_access(ready, {"allowFrom": [au.CHAT_ID], "voiceId": "v", "groups": {group: {}}})
    au.set_cfg(ready, features={"groups": False})
    resp = notify(adapter, "group_turn", chat_id=group)
    adapter.expect_error(resp, 409, "feature_off")
    assert "群聊" in au.body_of(resp)["message"]


# ---------------------------------------------------------------- 技能跟着开关走（5.17）

GIF = "---\nname: skill-gif\ndescription: 表情包\n---\n\n正文第一行\n"


def test_PUT_技能跟着开关走_关掉在头部写上停用一行_打开去掉_正文不动_停用的照样读得到(adapter, ready):
    au.put_file(au.skill_path(ready, "gif"), GIF)
    adapter.ok(put(adapter, "botalpha", {"stickers": False}))
    off = au.skill_path(ready, "gif").read_text(encoding="utf-8")
    assert off == "---\nname: skill-gif\ndescription: 表情包\ndisable-model-invocation: true\n---\n\n正文第一行\n", off
    assert adapter.ok(adapter.call("GET", au.skill_url("botalpha", "gif")))["text"] == off
    adapter.ok(put(adapter, "botalpha", {"stickers": True}))
    assert au.skill_path(ready, "gif").read_text(encoding="utf-8") == GIF


def test_PUT_没有那个技能文件_照样改开关_不新建(adapter, ready):
    au.set_cfg(ready, skills_manual=True)
    adapter.ok(put(adapter, "botalpha", {"moments": False}))
    adapter.ok(put(adapter, "botalpha", {"moments": True}))
    assert not au.skill_path(ready, "moments").exists()


def test_PUT_成人内容只管技能(adapter, ready):
    au.put_file(au.skill_path(ready, "nsfw"), "---\nname: skill-nsfw\ndescription: x\n---\n正文\n")
    body = adapter.ok(put(adapter, "botalpha", {"nsfw": False}))
    assert body["changed"] == ["nsfw"], body
    assert "disable-model-invocation: true" in au.skill_path(ready, "nsfw").read_text(encoding="utf-8")
