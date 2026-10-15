# -*- coding: utf-8 -*-
"""适配服务 v1.0 验收：显示名 GET/PUT /adapter/v1/bots/{bot_id}/profile（补充文件 12.5，草案 D）。

依据：INTERFACE-适配服务.md 12.5；INTERFACE-适配约定-v1.0-dsh侧.md D。不读实现。
「同步朋友圈库里的显示名」借自本目录现成用例（test_edit_bot.py）：写的是 bot_profiles.display_name，
朋友圈身份就是这只 bot 的编号（假 bot 没写 life_id / life_config）。朋友圈库经 botlife_db 夹具钉在临时目录。
"""
import pytest

from . import adapter_util as au
from .adapter_util import adapter, console_guard  # noqa: F401  夹具


def db_name(botlife_db, person):
    return (botlife_db.get_profile(person) or {}).get("display_name")


def test_显示名_读当前值_改了写配置并同步朋友圈库_停用的也能改(adapter, botlife_db):
    alpha = adapter.bot("botalpha", enabled=False, display_name="旧名字")
    bravo = adapter.bot("botbravo", display_name="别人")
    bravo_before = bravo.yml.read_bytes()
    cfg_before = au.read_cfg(alpha)
    assert adapter.ok(adapter.call("GET", au.profile_url("botalpha"))) == {"ok": True, "display_name": "旧名字"}

    body = adapter.ok(adapter.call("PUT", au.profile_url("botalpha"), {"display_name": "  新名字✨  "}))

    assert body.get("display_name") == "新名字✨", body
    cfg = au.read_cfg(alpha)
    assert cfg.pop("display_name") == "新名字✨", "配置里的 display_name 应是去掉首尾空白后的值"
    cfg_before.pop("display_name")
    assert cfg == cfg_before, "配置里别的键不该变"
    assert db_name(botlife_db, "botalpha") == "新名字✨", "朋友圈库里的显示名应同步"
    assert adapter.ok(adapter.call("GET", au.profile_url("botalpha")))["display_name"] == "新名字✨"
    assert bravo.yml.read_bytes() == bravo_before and db_name(botlife_db, "botbravo") is None
    assert adapter.launchctl.calls() == [], "改显示名不该动启动项"
    long_ok = "字" * 32
    assert adapter.ok(adapter.call("PUT", au.profile_url("botalpha"), {"display_name": long_ok}))["display_name"] == long_ok


@pytest.mark.parametrize("payload", [
    {"display_name": ""},
    {"display_name": "   "},
    {"display_name": "字" * 33},
    {"display_name": "两行\n名字"},
    {"display_name": "响铃\x07"},
    {"display_name": 123},
    {"display_name": "好名字", "extra": 1},
    {},
], ids=["空串", "纯空白", "33个字", "带换行", "带控制字符", "不是字符串", "多了别的键", "空对象"])
def test_显示名_不合规_422_什么都不写(adapter, botlife_db, payload):
    alpha = adapter.bot("botalpha", display_name="旧名字")
    before = au.file_mark(alpha.yml)
    adapter.expect_error(adapter.call("PUT", au.profile_url("botalpha"), payload), 422, "invalid")
    assert au.file_mark(alpha.yml) == before, "不合规时配置一个字节都不该动"
    assert db_name(botlife_db, "botalpha") is None, "不合规时朋友圈库不该写"
