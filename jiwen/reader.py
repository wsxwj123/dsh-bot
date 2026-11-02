"""Phase 4：薄封装，给 life-context.py 用。

只读 state 文件 + 调 engine 拿 triggers/description，不做 drift（tick 已经在做）。
读失败一律返回 None — 调用方应当做 jiwen 未启用处理。
"""
from __future__ import annotations
import os
import json
from typing import Optional

from . import engine
from .merge import threshold_values


DEFAULT_STATE_DIR = os.path.expanduser("~/.claude/dispatcher/.jiwen-state")


def dsh_state_dir(bot_cfg: dict) -> Optional[str]:
    """新系统（dsh-bot 网关）的 bot：情绪状态放在 bot 自己的 state 目录，绝不碰旧系统的目录。不是新系统的 bot 返回 None。"""
    ch = (bot_cfg or {}).get("bot_channel_path")
    try:
        import chat_history
        if ch and chat_history.is_dsh_bot(ch):
            return os.path.join(os.path.dirname(os.path.abspath(os.path.expanduser(ch))), "state", "jiwen")
    except Exception:
        pass
    return None


def resolve(bot_id: str, jcfg: dict) -> tuple[bool, str]:
    """(启用没有, 状态目录)。新系统的 bot 默认启用（除非写了 enabled: false），状态放在自己的 state 目录；
    旧系统的 bot 照旧：看 _global.yml 的 jiwen.enabled 和 jiwen.state_dir。"""
    return _resolve_with(_load_bot_or_none(bot_id), jcfg)


def _load_bot_or_none(bot_id: str):
    """读这只 bot 的配置（load_bot 浅合并结果）；读不出返回 None，调用方按旧系统处理。"""
    try:
        import config_loader
        return config_loader.load_bot(bot_id)
    except Exception:
        return None


def _resolve_with(bot_cfg, jcfg: dict) -> tuple[bool, str]:
    """resolve 的本体：bot 配置已经读好时直接用，read() 借此只读一次 bot 配置。"""
    jcfg = jcfg or {}
    try:
        d = dsh_state_dir(bot_cfg)
    except Exception:
        d = None
    if d:
        return jcfg.get("enabled") is not False, d
    return bool(jcfg.get("enabled")), jcfg.get("state_dir") or DEFAULT_STATE_DIR


def _state_path(state_dir: str, bot_id: str, chat_id: str) -> str:
    return os.path.join(state_dir, f"{bot_id}-{chat_id}.json")


def read(bot_id: str, chat_id: str, global_cfg: dict) -> Optional[dict]:
    """读 jiwen state 并附带 triggers / description。

    返回：{
        "state":       engine.State,
        "triggers":    list[dict],
        "description": str,
        "forced":      bool,        # 是否有 forced trigger
        "pride_block": bool,        # 是否被 pride 阻断
    }
    若 jiwen 未启用 / 文件不存在 / 解析失败 → None。
    """
    jcfg = (global_cfg or {}).get("jiwen") or {}
    bot_cfg = _load_bot_or_none(bot_id)   # 读一次：开关判定与阈值覆盖两处用
    enabled, state_dir = _resolve_with(bot_cfg, jcfg)
    if not enabled:
        return None
    path = _state_path(state_dir, bot_id, chat_id)
    if not os.path.exists(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        state = engine.state_from_dict(data)
    except Exception:
        return None

    # thresholds：全局 jiwen.thresholds ← bot 的 thresholds_override（兼容旧写法 thresholds）。
    # 逐项校验、坏值回落、绝不抛（jiwen/merge.py）；改动前全局一项坏了整组回默认。
    bot_jiwen = bot_cfg.get("jiwen") if isinstance(bot_cfg, dict) else None
    thresholds = engine.Thresholds(**threshold_values(jcfg, bot_jiwen))

    triggers = engine.get_triggers(state, thresholds)
    description = engine.get_state_description(state)
    # 平稳态描述无信息增益，返空避免 token 浪费
    if description.startswith("状态平稳"):
        description = ""

    # 提取首个 find_activity trigger（life-context.py 据此调 set_activity）
    find_activity = next((t for t in triggers if t.get("action") == "find_activity"), None)

    return {
        "state": state,
        "triggers": triggers,
        "description": description,
        "style_guidance": engine.get_style_guidance(state),
        "find_activity": find_activity,
        "forced": any(t.get("action") == "forced" for t in triggers),
        "pride_block": any(t.get("action") in ("pride_block", "pride_too_high_immersed") for t in triggers),
    }
