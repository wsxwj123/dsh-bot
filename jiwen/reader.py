"""Phase 4：薄封装，给 life-context.py 用。

只读 state 文件 + 调 engine 拿 triggers/description，不做 drift（tick 已经在做）。
读失败一律返回 None — 调用方应当做 jiwen 未启用处理。
"""
from __future__ import annotations
import os
import json
from typing import Optional

from . import engine


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
    jcfg = jcfg or {}
    try:
        import config_loader
        d = dsh_state_dir(config_loader.load_bot(bot_id))
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
    enabled, state_dir = resolve(bot_id, jcfg)
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

    # thresholds 走 _global.yml 配置；缺省走 engine 默认
    # float() 强转防御 YAML 偶尔被写成字符串
    th_cfg = jcfg.get("thresholds") or {}
    try:
        thresholds = engine.Thresholds(
            notice=float(th_cfg.get("notice", 0.20)),
            consider=float(th_cfg.get("consider", 0.35)),
            forced=float(th_cfg.get("forced", 0.50)),
            pride_block=float(th_cfg.get("pride_block", 0.5)),
            valence_activity=float(th_cfg.get("valence_activity", -0.6)),
            arousal_agitation=float(th_cfg.get("arousal_agitation", 0.7)),
            immersion_block=float(th_cfg.get("immersion_block", 0.3)),
        )
    except (TypeError, ValueError):
        thresholds = engine.Thresholds()  # fallback to defaults

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
