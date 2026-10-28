# -*- coding: utf-8 -*-
"""addbot-botlife-db 验收（黑盒）：新建 bot 写出的 yml 多一个 gateway 块。

依据只有 .devflow/addbot-botlife-db-接口.md 的行为清单 5 条。
被测入口是接口文件指定的 ``moments.hub_addbot._render_yml(fields, target, ...)``（纯函数），
只调这一个对外入口、断言 yaml.safe_load 解析后的结构，不读实现、不 mock 内部层。

字段约定（占位值）：bot_id=botalpha，display_name=小雨，chat_id=12345，port=17801；
target 用临时目录下的绝对路径，避免路径规范化干扰断言。

运行：在仓库根目录  python3 -m pytest tests/acceptance/addbot_botlife_db -q
"""
import yaml

from moments import hub_addbot

EXPECTED_BOTLIFE_DB = "~/claudebotlife/state.db"
NAMESPACE = "11111111-1111-4111-8111-111111111111"
LIFE_CONFIG = "~/claudebotlife/configs/botalpha.yml"
FEATURES = ["voice", "image"]


def make_fields(**over):
    fields = {
        "bot_id": "botalpha",
        "display_name": "小雨",
        "chat_id": 12345,
        "port": 17801,
    }
    fields.update(over)
    return fields


def render(fields, target, namespace=NAMESPACE, life_config=None, features=None):
    return hub_addbot._render_yml(fields, target, namespace, life_config, features)


def parsed_of(fields, target, **kwargs):
    return yaml.safe_load(render(fields, target, **kwargs))


def test_渲染_默认参数_写出gateway_botlife_db共享库路径(tmp_path):
    """行为 1：任意正常字段集，输出里 gateway.botlife_db 等于 ~/claudebotlife/state.db。"""
    parsed = parsed_of(make_fields(), str(tmp_path / "channel"))

    gateway = parsed.get("gateway")
    assert isinstance(gateway, dict), f"gateway 块缺失或不是 dict：{gateway!r}"
    assert gateway["botlife_db"] == EXPECTED_BOTLIFE_DB


def test_渲染_默认参数_原有六个字段一个不少且值不变(tmp_path):
    """行为 2：原有字段 id/display_name/bot_channel_path/chat_id/dispatcher_port/namespace_uuid 全保留。"""
    target = str(tmp_path / "channel")
    parsed = parsed_of(make_fields(), target)

    assert parsed["id"] == "botalpha"
    assert parsed["display_name"] == "小雨"
    assert parsed["bot_channel_path"] == target
    assert parsed["chat_id"] == 12345
    assert parsed["dispatcher_port"] == 17801
    assert parsed["namespace_uuid"] == NAMESPACE


def test_渲染_传life_config_该项写出且与gateway并存(tmp_path):
    """行为 3：传 life_config，该项仍写出，与 gateway 块并存。"""
    parsed = parsed_of(make_fields(), str(tmp_path / "channel"), life_config=LIFE_CONFIG)

    assert parsed["life_config"] == LIFE_CONFIG
    gateway = parsed.get("gateway")
    assert isinstance(gateway, dict), f"gateway 块缺失或不是 dict：{gateway!r}"
    assert gateway["botlife_db"] == EXPECTED_BOTLIFE_DB


def test_渲染_传features_该项写出且与gateway并存(tmp_path):
    """行为 4：传 features，该项仍写出，与 gateway 块并存。"""
    parsed = parsed_of(make_fields(), str(tmp_path / "channel"), features=FEATURES)

    assert parsed["features"] == FEATURES
    gateway = parsed.get("gateway")
    assert isinstance(gateway, dict), f"gateway 块缺失或不是 dict：{gateway!r}"
    assert gateway["botlife_db"] == EXPECTED_BOTLIFE_DB


def test_渲染_同时传可选参数_整份可safe_load且gateway是dict不是字符串(tmp_path):
    """行为 5：任何情况下整份输出能被 yaml.safe_load 解析，gateway 是 dict 不是字符串。"""
    text = render(
        make_fields(),
        str(tmp_path / "channel"),
        life_config=LIFE_CONFIG,
        features=FEATURES,
    )

    # safe_load 不抛异常 = 整份输出可解析；返回 dict = 顶层是映射
    parsed = yaml.safe_load(text)
    assert isinstance(parsed, dict)
    gateway = parsed.get("gateway")
    assert isinstance(gateway, dict), f"gateway 不是 dict：{gateway!r}"
    assert not isinstance(gateway, str), "gateway 被写成了字符串"
    assert gateway["botlife_db"] == EXPECTED_BOTLIFE_DB
    assert parsed["life_config"] == LIFE_CONFIG
    assert parsed["features"] == FEATURES


def test_渲染_最小字段集且namespace缺省_仍生成gateway(tmp_path):
    """边界：fields 只含接口要求的四个键、namespace 不传，gateway 仍要写出且整份可解析。"""
    fields = {
        "bot_id": "botalpha",
        "display_name": "小雨",
        "chat_id": 12345,
        "port": 17801,
    }

    parsed = yaml.safe_load(
        hub_addbot._render_yml(fields, str(tmp_path / "channel"))
    )

    assert isinstance(parsed.get("namespace_uuid"), str)
    assert parsed["namespace_uuid"] != ""
    gateway = parsed.get("gateway")
    assert isinstance(gateway, dict), f"gateway 块缺失或不是 dict：{gateway!r}"
    assert gateway["botlife_db"] == EXPECTED_BOTLIFE_DB


def test_渲染_字段含引号冒号井号_原样保留且gateway照常(tmp_path):
    """边界：display_name 含引号/冒号/井号，值原样保留，整份仍可解析，gateway 不受影响。"""
    special_name = '小雨 "#1: 测试"'
    parsed = parsed_of(
        make_fields(display_name=special_name, chat_id=987654321),
        str(tmp_path / "channel"),
    )

    assert parsed["display_name"] == special_name
    assert parsed["chat_id"] == 987654321
    gateway = parsed.get("gateway")
    assert isinstance(gateway, dict), f"gateway 块缺失或不是 dict：{gateway!r}"
    assert gateway["botlife_db"] == EXPECTED_BOTLIFE_DB
