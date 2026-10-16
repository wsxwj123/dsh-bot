# -*- coding: utf-8 -*-
"""技能跟着功能开关走（约定 v1.1 5.17）：关掉在技能头部写上 disable-model-invocation: true，打开去掉；正文一个字不动。"""
from moments import features

SKILL = "---\nname: skill-gif\ndescription: 表情包\n---\n\n正文第一行\n---\n正文里的分隔线也不能动\n"


def test_关掉写上一行_打开去掉_正文不动_重复做结果一样():
    off = features.set_skill_disabled(SKILL, True)
    assert off == "---\nname: skill-gif\ndescription: 表情包\ndisable-model-invocation: true\n---\n\n正文第一行\n---\n正文里的分隔线也不能动\n"
    assert features.set_skill_disabled(off, True) == off
    assert features.set_skill_disabled(off, False) == SKILL
    assert features.set_skill_disabled(SKILL, False) == SKILL


def test_头部原来写了别的值_按开关改():
    text = "---\nname: x\ndisable-model-invocation: false\ndescription: d\n---\n正文\n"
    assert features.set_skill_disabled(text, True) == "---\nname: x\ndescription: d\ndisable-model-invocation: true\n---\n正文\n"
    assert features.set_skill_disabled(text, False) == "---\nname: x\ndescription: d\n---\n正文\n"


def test_没有头部的动不了():
    assert features.set_skill_disabled("正文\n", True) is None
    assert features.set_skill_disabled("---\nname: x\n没有收尾", True) is None
