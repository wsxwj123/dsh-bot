# -*- coding: utf-8 -*-
"""新系统的技能模板（约定 v1.1 5.10、5.17、5.18.1、5.19.7）：仓库 ``templates/skills/skill-<通用名>/SKILL.md``。

圈外是新系统的用法（工具名、参数），圈内是通用的缺省写法，切换底层时原样带走。放技能时两处现填：
- image 的 ``{{外貌}}``：新建页"外貌"一栏的内容；没填写固定的一句（两边同一句）；
- nsfw 的 ``{{成人例句段}}``：这只 bot 人设里 NSFW 例句段的原句；人设里没有就只写"按人设自然反应"。
仓库里不放任何成人例句；外貌校验不许出现 ``{{``、``}}``，所以填进去的内容不会再被当成占位。
"""
import os
import re

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_DIR = os.path.join(_REPO_ROOT, "templates", "skills")
#: 七个有技能的通用名（5.10）；功能键 → 通用名见 features.SKILL_OF
GENERICS = ("image", "voice", "gif", "moments", "proactive", "group", "nsfw")
APPEARANCE_FALLBACK = "按人设里的外貌描写来画。"
NSFW_FALLBACK = "按人设自然反应"
NSFW_HEAD = "# 情欲场景语气参考\n\n以下只作语气参考，不是必用清单。按当下情境自然反应，不照搬原句。\n\n"
NSFW_TAIL = "\n\n说话特点和回复规则照常驻人设，这份语料不压过人设。"

#: 例句段标题：整行就是"NSFW"，前面可带 #、加粗、编号（2.、2、），后面可带括号注释、"例句/语料/示例/参考"和冒号。
#: 后面跟别的说明文字的不算（"### NSFW 节奏铁律"这类是规则段，不是例句）
_NSFW_TITLE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:\*\*)?\s*(?:\d+\s*[.、．)）]\s*)?NSFW\s*(?:[（(][^（()）]*[）)])?\s*"
    r"(?:例句|语料|示例|参考)?\s*(?:\*\*)?\s*[:：]?\s*(?:\*\*)?\s*$", re.IGNORECASE)
_ITEMS = (("dash", re.compile(r"^\s*[-*•]\s+(.*\S)\s*$")), ("num", re.compile(r"^\s*\d+\s*[.、．)）]\s*(.*\S)\s*$")))
_COMMENT_MARKS = ("<!--", "-->")


def nsfw_lines(persona):
    """人设里 NSFW 例句段的原句：认"NSFW"那一行标题（比如"2.NSFW"），收它下面连着的列表项（"-"、"*"、"•" 或
    数字编号，和第一项同一种写法），遇到别的内容就停。带注释标记的句子不收：抄进技能会把两边共用那一段的边界切错。"""
    out, taking, style = [], False, None
    for line in (persona or "").splitlines():
        if not taking:
            taking = bool(_NSFW_TITLE.match(line))
            continue
        item = None
        for name, pattern in _ITEMS:
            m = pattern.match(line)
            if m and style in (None, name):
                item, style = m.group(1), name
                break
        if item is not None:
            if not any(mark in item for mark in _COMMENT_MARKS):
                out.append(item)
        elif line.strip() or style:
            break
    return out


def nsfw_unmatched(persona):
    """人设里提到 NSFW、却没认出例句段（放 nsfw 技能时提醒一句，免得以为原句抄进去了）。"""
    return "nsfw" in (persona or "").lower() and not nsfw_lines(persona)


def render(generic, appearance="", persona=""):
    """按模板填好一个技能的全文。"""
    with open(os.path.join(TEMPLATE_DIR, "skill-" + generic, "SKILL.md"), encoding="utf-8") as f:
        text = f.read()
    if generic == "image":
        text = text.replace("{{外貌}}", (appearance or "").strip() or APPEARANCE_FALLBACK)
    if generic == "nsfw":
        found = nsfw_lines(persona)
        block = NSFW_HEAD + "\n".join("- " + line for line in found) + NSFW_TAIL if found else NSFW_FALLBACK
        text = text.replace("{{成人例句段}}", block)
    return text
