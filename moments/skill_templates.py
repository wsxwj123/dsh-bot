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

_NSFW_TITLE = re.compile(r"^\s*(?:\d+\s*[.、．]\s*)?NSFW\s*[:：]?\s*$", re.IGNORECASE)
_BULLET = re.compile(r"^\s*[-*]\s+(.*\S)\s*$")


def nsfw_lines(persona):
    """人设里 NSFW 例句段的原句：认"NSFW"那一行标题（比如"2.NSFW"），收它下面连着的列表项，遇到别的内容就停。"""
    out, taking = [], False
    for line in (persona or "").splitlines():
        if not taking:
            taking = bool(_NSFW_TITLE.match(line))
            continue
        bullet = _BULLET.match(line)
        if bullet:
            out.append(bullet.group(1))
        elif line.strip() or out:
            break
    return out


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
