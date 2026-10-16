# -*- coding: utf-8 -*-
"""适配服务 v1.1 验收：新建带功能与外貌（约定 v1.1 5.18.1）。

请求多两个可选字段：features（功能键数组，省略等于九个全关）、appearance（外貌，只在勾了 image 时可非空）。
新建时九个键都显式写进配置；勾了的功能放技能，缺必需项的不打开、技能照放但停用，并列进 features_skipped。
外貌写进 image 技能的圈内，没填写固定的一句；nsfw 的圈内取这只 bot 人设里的 NSFW 例句，没有写"按人设自然反应"。
getMe、装启动项都用 conftest 的桩（同 test_adapter_v10_create.py）。
"""
import pytest
import yaml

from . import adapter_util as au
from .adapter_util import adapter, console_guard  # noqa: F401  夹具
from .test_adapter_v10_create import NEW, good_body, nothing_written

PERSONA = "# 新人设\n你是测试用的 bot。\n\n## 语料库\n2.NSFW\n- 示例甲\n- 示例乙\n\n## 别的\n"
ALL = ["voice", "image", "stickers", "moments", "proactive", "life", "relationship", "groups", "nsfw"]


def channel(adapter):
    return adapter.sandbox.dsh_home / "bots" / NEW / "channel"


def skill(adapter, generic):
    return channel(adapter) / "skills" / ("skill-" + generic) / "SKILL.md"


def with_novelai_token(adapter):
    (adapter.sandbox.dsh_home / "credentials.yaml").write_text(
        "version: 1\nrefs:\n  NOVELAI_BEARER_TOKEN: pst-fake-token-0123456789\n", encoding="utf-8")


def test_新建_全勾_外貌进生图技能_成人技能取人设例句_缺生活设定的不打开但列出来(adapter, telegram, bun):
    with_novelai_token(adapter)
    body = good_body(persona_text=PERSONA, fish_voice_id="voice_abc-1", features=ALL, appearance="  长发\r\n圆脸  ")
    got = adapter.ok(adapter.call("POST", au.BOTS, body))
    want = dict(dict.fromkeys(ALL, True), life=False)
    assert got["features"] == want, got
    assert got["skills"] == ["voice", "image", "gif", "moments", "proactive", "group", "nsfw"], got
    assert got["features_skipped"] == [{"key": "life", "missing": [{"code": "life_config_missing",
                                        "message": got["features_skipped"][0]["missing"][0]["message"]}]}], got
    cfg = yaml.safe_load((adapter.sandbox.dsh_cfg / ("%s.yml" % NEW)).read_text(encoding="utf-8"))
    assert cfg["features"] == want, cfg
    image = skill(adapter, "image").read_text(encoding="utf-8")
    assert "照片里的你：\n长发\n圆脸\n" in image and "disable-model-invocation" not in image, image
    nsfw = skill(adapter, "nsfw").read_text(encoding="utf-8")
    assert "- 示例甲\n- 示例乙" in nsfw and "只适用于成年角色" in nsfw, nsfw
    assert oct(skill(adapter, "gif").stat().st_mode & 0o777) == "0o644"


def test_新建_缺NovelAI令牌_生图不打开_技能照放但停用(adapter, telegram, bun):
    got = adapter.ok(adapter.call("POST", au.BOTS, good_body(features=["image"])))
    assert got["features"]["image"] is False and got["skills"] == ["image"], got
    assert [s["key"] for s in got["features_skipped"]] == ["image"], got
    assert got["features_skipped"][0]["missing"][0]["code"] == "image_token_missing", got
    text = skill(adapter, "image").read_text(encoding="utf-8")
    assert "disable-model-invocation: true" in text and "照片里的你：\n按人设里的外貌描写来画。\n" in text, text


def test_新建_不带features_九个全关_不放技能_成功体照样带三个键(adapter, telegram, bun):
    got = adapter.ok(adapter.call("POST", au.BOTS, good_body()))
    assert got["features"] == dict.fromkeys(ALL, False) and got["skills"] == [] and got["features_skipped"] == [], got
    cfg = yaml.safe_load((adapter.sandbox.dsh_cfg / ("%s.yml" % NEW)).read_text(encoding="utf-8"))
    assert cfg["features"] == dict.fromkeys(ALL, False), cfg
    assert not (channel(adapter) / "skills").exists()


def test_新建_外貌只有空白_当作没填_不勾生图也行(adapter, telegram, bun):
    adapter.ok(adapter.call("POST", au.BOTS, good_body(appearance="  \n  ")))


BAD = [
    ("features不是数组", {"features": "voice"}),
    ("features有不认识的键", {"features": ["voice", "teleport"]}),
    ("features重复", {"features": ["voice", "voice"]}),
    ("features里有非字符串", {"features": [1]}),
    ("外貌不是字符串", {"features": ["image"], "appearance": 5}),
    ("外貌超过500个字", {"features": ["image"], "appearance": "长" * 501}),
    ("外貌有别的控制字符", {"features": ["image"], "appearance": "长发\t圆脸"}),
    ("外貌含注释标记", {"features": ["image"], "appearance": "长发<!--"}),
    ("外貌含占位括号", {"features": ["image"], "appearance": "{{外貌}}"}),
    ("没勾生图却填了外貌", {"features": ["voice"], "appearance": "长发"}),
]


@pytest.mark.parametrize("extra", [b for _n, b in BAD], ids=[n for n, _b in BAD])
def test_新建_功能或外貌不合规_422_什么都不写(adapter, telegram, bun, extra):
    adapter.expect_error(adapter.call("POST", au.BOTS, good_body(**extra)), 422, "invalid")
    nothing_written(adapter)
    assert telegram.hits == [], "校验不过不该问 Telegram"


def test_新建_外貌正好500个字_收下(adapter, telegram, bun):
    with_novelai_token(adapter)
    adapter.ok(adapter.call("POST", au.BOTS, good_body(features=["image"], appearance="长" * 500)))
