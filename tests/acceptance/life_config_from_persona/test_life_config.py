# -*- coding: utf-8 -*-
"""life-config-from-persona 验收（黑盒）：新建 bot 时按人设生成生活设定骨架。

依据只有 .devflow/life-config-from-persona-接口.md 的行为清单与错误契约。
被测入口是接口文件指定的 ``moments.hub_addbot._life_text(fields)``（纯函数）；
另用 ``moments.life_settings.view`` 这个现成读取入口验证落盘文件能被正常解析。
不读 hub_addbot 实现，不 mock 内部层，只断言最终文本与解析结果。

运行：在仓库根目录  python3 -m pytest tests/acceptance/life_config_from_persona -q
"""
import pytest
import yaml

from moments import hub_addbot
from moments import life_settings

FIRST_LINE = "# 生活设定：新建 bot 时生成的骨架，可以直接改"
SLEEP_HOURS = ["00:00-07:30"]
UNWRITTEN_KEYS = ("bio", "city", "birthday", "speaking_threshold")
MISSING = object()


def make_fields(**over):
    fields = {"bot_id": "botabc", "display_name": "小雨"}
    fields.update(over)
    return fields


def render(fields):
    return hub_addbot._life_text(fields)


def load(text):
    doc = yaml.safe_load(text)
    assert isinstance(doc, dict), "生成的文件顶层必须是映射"
    return doc


def summary_of(doc):
    """块标量 ``|`` 解析后会带一个结尾换行，比较前统一去掉（正文本身已被去掉首尾空白）。"""
    assert "persona_summary" in doc
    return doc["persona_summary"].rstrip("\n")


# ---------------------------------------------------------------- 行为清单 1：固定内容

def test_首行是固定注释():
    text = render(make_fields())
    assert text.splitlines()[0] == FIRST_LINE


def test_id字段等于传入的bot_id():
    doc = load(render(make_fields(bot_id="botxyz")))
    assert doc["id"] == "botxyz"


def test_display_name字段等于传入的显示名():
    doc = load(render(make_fields(display_name="小雨")))
    assert doc["display_name"] == "小雨"


def test_sleep_hours固定为凌晨到七点半():
    doc = load(render(make_fields()))
    assert doc["sleep_hours"] == SLEEP_HOURS


def test_未填写字段不出现在文档中():
    doc = load(render(make_fields()))
    for key in UNWRITTEN_KEYS:
        assert key not in doc, "按契约 %r 只留注释，不写成字段" % key


# ---------------------------------------------------------------- 行为清单 2：persona_summary

def test_人设非空但未显式给摘要时不生成persona_summary():
    # 2026-10-28 用户决定：不再从人设正文自动提取摘要，改成新建时提示用户手填
    doc = load(render(make_fields(persona="喜欢下雨天，偶尔去公园散步。")))
    assert "persona_summary" not in doc


def test_显式给了persona_summary就写入():
    doc = load(render(make_fields(persona_summary="38岁成熟人妻，黏人但骄傲")))
    assert summary_of(doc) == "38岁成熟人妻，黏人但骄傲"


def test_显式摘要原样写入不再清洗():
    # 显式给的是用户手写的成品，不能再被 markdown 清洗或截断
    doc = load(render(make_fields(persona_summary="**重点**：喜欢下雨天")))
    assert summary_of(doc) == "**重点**：喜欢下雨天"


# ---------------------------------------------------------------- 行为清单 4：YAML 合法性

def test_id含特殊字符时yaml可解析且值不变():
    bot_id = 'bot: "A" #1\n第二行'
    doc = load(render(make_fields(bot_id=bot_id)))
    assert doc["id"] == bot_id


def test_display_name含特殊字符时yaml可解析且值不变():
    display_name = '名: "字" #注\n第二行'
    doc = load(render(make_fields(display_name=display_name)))
    assert doc["display_name"] == display_name


# ---------------------------------------------------------------- 集成：落盘后由 life_settings 读回

@pytest.fixture
def life_dir(tmp_path, monkeypatch):
    """生活设定目录指到测试临时目录（接口：目录不存在时新建 0700）。"""
    d = tmp_path / "life"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("DSH_CONSOLE_LIFE_DIR", str(d))
    return d


def write_and_view(life_dir, text, bot_id="botabc"):
    path = life_dir / (bot_id + ".yml")
    path.write_text(text, encoding="utf-8")
    bot = {"id": bot_id, "cfg": {"life_config": str(path)}}
    return life_settings.view(bot, [bot])


def test_生成文件能被life_settings读取(life_dir):
    persona = "喜欢下雨天。"
    text = render(make_fields(persona=persona))
    resp = write_and_view(life_dir, text)
    assert resp["values"]["sleep_hours"] == SLEEP_HOURS
    assert not (resp["values"].get("persona_summary") or "").strip()


def test_特殊字符显示名不影响life_settings读取(life_dir):
    fields = make_fields(bot_id='bot: "A" #1', display_name='名: "字" #注\n第二行',
                         persona="正文")
    resp = write_and_view(life_dir, render(fields))
    assert resp["values"]["sleep_hours"] == SLEEP_HOURS
    assert not (resp["values"].get("persona_summary") or "").strip()
