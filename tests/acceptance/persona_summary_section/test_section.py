# -*- coding: utf-8 -*-
"""persona-summary-section 验收（黑盒）。

依据只有 .devflow/persona-summary-section-接口.md 的行为清单与错误契约。
被测入口是接口指定的两个公开函数：
``moments.hub_addbot._persona_summary_ex(text) -> (摘要, method)`` 与
``moments.hub_addbot._persona_summary(text) -> 摘要``。
不读实现、不 mock 内部层，只断言返回值本身。

运行：在仓库根目录  python3 -m pytest tests/acceptance/persona_summary_section -q
"""
import pytest

from moments import hub_addbot

SECTION = "section"
TRUNCATE = "truncate"


def summary_ex(text):
    return hub_addbot._persona_summary_ex(text)


# ------------------------------------------------------------ 行为清单 2/3：停止小节切分

def test_摘要有说话方式小节_截到该小节结束且method为section():
    speech = "温" * 320
    persona = (
        "# 角色设定\n\n"
        "## 基本信息\n\n"
        "名字：陈露露\n\n"
        "## 说话方式\n\n"
        + speech
        + "\n\n## 语料库\n\n不应出现的语料"
    )
    summary, method = summary_ex(persona)
    expected = "角色设定\n\n基本信息\n\n名字：陈露露\n\n说话方式\n\n" + speech
    assert method == SECTION
    assert summary == expected
    assert len(summary) > 300


def test_摘要_说话特点标题_同样命中section():
    persona = (
        "# 陈露露\n\n"
        "## 基本信息\n\n"
        "职业：主播\n\n"
        "## 说话特点\n\n"
        "句子短，语气软。\n\n"
        "## 语料库\n\n不要"
    )
    summary, method = summary_ex(persona)
    assert method == SECTION
    assert summary == "陈露露\n\n基本信息\n\n职业：主播\n\n说话特点\n\n句子短，语气软。"


def test_摘要_三级标题说话方式_也能命中section():
    persona = (
        "# 人设\n\n"
        "## 基本信息\n\n"
        "年龄：20\n\n"
        "### 说话方式\n\n"
        "短句。\n\n"
        "## 语料库\n\n不要"
    )
    summary, method = summary_ex(persona)
    assert method == SECTION
    assert summary == "人设\n\n基本信息\n\n年龄：20\n\n说话方式\n\n短句。"


def test_摘要_标题含停止词组合_按包含匹配命中section():
    persona = "# 人设\n\n## 说话方式与语气\n\n轻松。\n\n## 语料库\n\n不要"
    summary, method = summary_ex(persona)
    assert method == SECTION
    assert summary == "人设\n\n说话方式与语气\n\n轻松。"


@pytest.mark.parametrize("title", ["语言风格", "对话风格", "说话风格", "语气"])
def test_摘要_其他停止词标题_命中section(title):
    persona = "# 人设\n\n## %s\n\n正文。\n\n## 语料库\n\n不要" % title
    summary, method = summary_ex(persona)
    assert method == SECTION
    assert summary == "人设\n\n%s\n\n正文。" % title


# ------------------------------------------------------------ 行为清单 1/3/4：注释与清洗

def test_摘要_HTML注释内的停止词不触发section():
    persona = "# 人设\n\n<!--\n## 说话方式\n-->\n\n正文。"
    summary, method = summary_ex(persona)
    assert method == TRUNCATE
    assert summary == "人设\n\n正文。"


# ------------------------------------------------------------ 行为清单 7：长度上限

def test_摘要_没有停止小节_截到300且method为truncate():
    persona = "测" * 401
    summary, method = summary_ex(persona)
    assert method == TRUNCATE
    assert len(summary) == 300
    assert summary == "测" * 300


def test_摘要_section路径超过4000字_截到4000():
    persona = "## 说话方式\n\n" + "长" * 4500 + "\n\n## 语料库\n\n尾"
    summary, method = summary_ex(persona)
    assert method == SECTION
    assert len(summary) == 4000
    assert summary == "说话方式\n\n" + "长" * 3994


# ------------------------------------------------------------ 行为清单 6：占位符模板

def test_摘要_全占位符模板_摘要为空():
    persona = "<姓名>\n<性别>\n<年龄>\n<职业>\n<爱好>"
    assert summary_ex(persona) == ("", TRUNCATE)


# ------------------------------------------------------------ 行为清单 2：停止小节之后的内容

def test_摘要_停止小节之后的正文不出现在摘要里():
    persona = "## 说话方式\n\n说话温柔。\n\n## 语料库\n\n秘密语料ABC，不可外泄"
    summary, _method = summary_ex(persona)
    assert summary == "说话方式\n\n说话温柔。"
    assert "秘密语料ABC" not in summary
    assert "语料库" not in summary


# ------------------------------------------------------------ 返回契约

@pytest.mark.parametrize("persona", [
    "## 语气\n\n温和。\n\n## 语料库\n\n不要",
    "正" * 350,
    "# 模板\n\n<姓名>\n<年龄>\n<职业>",
], ids=["section", "truncate", "占位符为空"])
def test_persona_summary返回值与ex摘要部分一致(persona):
    summary, _method = summary_ex(persona)
    assert hub_addbot._persona_summary(persona) == summary


@pytest.mark.parametrize("text", ["", None], ids=["空串", "None"])
def test_空文本或None_返回空摘要且method为truncate(text):
    assert summary_ex(text) == ("", TRUNCATE)
