# -*- coding: utf-8 -*-
"""新系统的技能模板（约定 v1.1 5.10、5.17、5.18.1、5.19.7）：仓库 templates/skills/ 下七个，新建或打开功能时按它放技能。"""
import re

import pytest

from moments import skill_templates as st

TOOL_NAMES = ("image_guide", "generate_image", "reply", "sticker", "moments", "stay_silent", "as_voice",
              "voice_text", "voice_instruct", "voice_emotion", "⟦系统")
BOT4_LIKE = "## 语料库\n1.日常对话\n- 中午吃啥了\n\n2.NSFW\n- 第一句示例\n- 第二句示例\n\n## USER.md：\n- Name: x\n"


def inside(text):
    return text.split("<!-- portable:start -->\n", 1)[1].split("<!-- portable:end -->", 1)[0]


@pytest.mark.parametrize("generic", st.GENERICS)
def test_七个模板都有文件头和圈_填完不留占位_圈内不出现任何工具名(generic):
    text = st.render(generic, appearance="", persona=BOT4_LIKE)
    assert text.startswith("---\nname: skill-%s\ndescription: " % generic), text[:60]
    assert text.count("<!-- portable:start -->\n") == 1 and text.count("<!-- portable:end -->") == 1
    assert "{{" not in text and "}}" not in text
    for name in TOOL_NAMES:
        assert name not in inside(text), (generic, name)


def test_生图模板_外貌填进圈内_没填写固定的一句():
    assert "照片里的你：\n长发，圆脸\n戴眼镜\n" in st.render("image", appearance="  长发，圆脸\n戴眼镜  ")
    assert "照片里的你：\n按人设里的外貌描写来画。\n" in st.render("image", appearance="")


def test_成人模板_圈外固定写成年限制_圈内是人设里NSFW段的原句_没有就写按人设自然反应():
    text = st.render("nsfw", persona=BOT4_LIKE)
    assert "只适用于成年角色，角色未成年时不进行任何性相关内容。" in text.split("<!-- portable:start -->")[0]
    body = inside(text)
    assert "- 第一句示例\n- 第二句示例" in body and "中午吃啥了" not in body and "Name" not in body
    assert body.startswith("# 情欲场景语气参考\n\n以下只作语气参考")
    assert inside(st.render("nsfw", persona="# 人设\n说话温柔\n")).strip() == "按人设自然反应"


@pytest.mark.parametrize("persona,want", [
    ("2.NSFW\n- 一\n- 二\n", ["一", "二"]),
    ("2、NSFW：\n* 一\n\n# 下一节\n- 不算\n", ["一"]),
    ("NSFW\n\n- 一\n", ["一"]),
    ("1.日常\n- 不算\n", []),
])
def test_认NSFW例句段(persona, want):
    assert st.nsfw_lines(persona) == want
