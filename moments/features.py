# -*- coding: utf-8 -*-
"""功能开关（约定 v1.1 5.19）：每只新系统 bot 九个开关，存在它自己 yml 的 ``features`` 块里。

文字聊天（含看图、表情回应、长期记忆、聊天命令）、听语音、约定提醒常开，不进开关。
存储里没有的键按上线缺省取（5.19.4）：前八个开；``nsfw`` 这只 bot 已经有 nsfw 技能才开。
网关（gateway/src/features.ts）、适配服务、定时任务都按这里的规则读，缺省两边要一致。
"""
import os
import re

#: 九个键与中文名，顺序就是管理台页面上块的顺序
KEYS = (("voice", "发语音"), ("image", "生图"), ("stickers", "表情包"), ("moments", "朋友圈"),
        ("proactive", "主动找你聊"), ("life", "生活作息"), ("relationship", "关系进展"),
        ("groups", "群聊"), ("nsfw", "成人内容"))
KEY_SET = frozenset(k for k, _ in KEYS)
#: 功能键 → 技能通用名（5.19.1）；life、relationship 没有技能
SKILL_OF = {"voice": "voice", "image": "image", "stickers": "gif", "moments": "moments",
            "proactive": "proactive", "groups": "group", "nsfw": "nsfw"}


def skill_file(channel_dir, generic):
    """dsh 侧技能文件的位置（5.17）：<频道目录>/skills/skill-<通用名>/SKILL.md。"""
    return os.path.join(channel_dir, "skills", "skill-" + generic, "SKILL.md")


def launch_default(key, channel_dir):
    """v1.1 之前就有的 bot，存储里没有这个键时取什么：前八个开；nsfw 已经有技能的开。"""
    if key == "nsfw":
        return bool(channel_dir) and os.path.isfile(skill_file(channel_dir, "nsfw"))
    return True


#: 写成字符串的真假（带引号的 yes/no/on/off/true/false）：PyYAML 把不带引号的这几个词认成真假，网关的 Bun.YAML
#: 只认 true/false、别的读成字符串；两边都按这张表认，管理台、定时任务和网关看到的开关才一致
WORD_BOOL = dict([(w, True) for w in ("yes", "Yes", "YES", "on", "On", "ON", "true", "True", "TRUE")]
                 + [(w, False) for w in ("no", "No", "NO", "off", "Off", "OFF", "false", "False", "FALSE")])


def read(cfg, channel_dir):
    """→ ``(九个键的布尔值, 警告列表)``。存储里不是真假的值按关，并在警告里说一句。"""
    block = cfg.get("features") if isinstance(cfg, dict) else None
    warnings = []
    if block is not None and not isinstance(block, dict):
        warnings.append("配置里的 features 不是一组开关，全部按缺省处理")
        block = None
    out = {}
    for key, _ in KEYS:
        if block is not None and key in block:
            value = block[key]
            value = value if type(value) is bool else WORD_BOOL.get(value) if isinstance(value, str) else None
            out[key] = value is True
            if value is None:
                warnings.append("features.%s 的值不是真或假，按关处理" % key)
        else:
            out[key] = launch_default(key, channel_dir)
    return out, warnings


def enabled(cfg, channel_dir, key):
    """这只 bot 的某项功能开着没有（定时任务跳过关掉的 bot 时用）。"""
    return read(cfg, channel_dir)[0][key]


_DISABLE_RE = re.compile(r"^disable-model-invocation[ \t]*:")
_FENCE_RE = re.compile(r"^---[ \t]*\r?$")


def set_skill_disabled(text, disabled):
    """技能跟着开关走（5.17）：在技能头部（第一行 ``---`` 与下一个整行 ``---`` 之间）写上或去掉
    ``disable-model-invocation: true``，头部别的行（含空行）和正文一个字不动，开头的 BOM、Windows 换行照原样。
    dsh 不把写了这一行的技能列给模型。返回新文本；没有头部的返回 None（动不了，这样的文件 dsh 本来也不列）。"""
    bom = "﻿" if text.startswith("﻿") else ""
    lines = text[len(bom):].split("\n")
    if not _FENCE_RE.match(lines[0]):
        return None
    end = next((i for i in range(1, len(lines)) if _FENCE_RE.match(lines[i])), None)
    if end is None:
        return None
    head = [line for line in lines[1:end] if not _DISABLE_RE.match(line)]
    if disabled:
        head.append("disable-model-invocation: true" + ("\r" if lines[0].endswith("\r") else ""))
    return bom + "\n".join(lines[:1] + head + lines[end:])
