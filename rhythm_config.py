# -*- coding: utf-8 -*-
"""每只 bot「行为节奏」8 个键的表与读值口径（纯标准库，不依赖 Flask）。

心跳、发圈、主动开口都是短命 CLI 进程，不能为拿一张范围表把 Flask 带进来；
适配服务 GET 里给页面看的 `global` 也用这里的同一组函数算，保证页面写的就是
bot 真正回退到的值（PLAN dsh-rhythm ① 1.2、③ 3.5）。

读值分两层：
- **bot 层**（`bot_cfg` = `config_loader.load_bot` 的结果，键是扁平的 `bot_key`）：
  键在、能认成整数（照 claude `rhythm_config._as_int`：`"60"`、`60.0` 也认）、
  并落在可写范围内，才算设了；否则当没设往全局落。0 在范围内就是有效值，
  所以不能用 `or` 判断。
- **全局层**（`_global.yml`，键可能是嵌套路径）：沿用改动前的口径，没覆盖的 bot
  行为逐字不变：
  - `non_negative`：非布尔的 Python int 且 >= 0（发圈每天上限，原 `post.py`）
  - `positive`：非布尔的 Python int 且 > 0（两个静默阈值，原
    `prefilter.positive_int_or_default`；冷却两键，原 `self_initiate._valid_positive_int`）
  - `in_range`：非布尔的 Python int 且在可写范围内（改动前不读全局的三个键）
  不合规一律回默认。全局层不认数字串：锁定用例 `silence_threshold_config` 要求
  全局 `"10"` 按 30。

任何坏值（文字、布尔、NaN、越界、上层不是映射）都只往下一层落，绝不抛。
本模块不 import `prefilter` 等消费方：它们要反过来 import 本模块，互相导入会成环；
默认值与它们的一致性由 tests/unit/test_rhythm_config.py 钉住。
"""
import math

_MISSING = object()

# 全局层口径的三种写法（见模块头）
GLOBAL_NON_NEGATIVE = "non_negative"
GLOBAL_POSITIVE = "positive"
GLOBAL_IN_RANGE = "in_range"


def _entry(bot_key, global_key, default, lo, hi, global_rule, label, unit):
    return {"bot_key": bot_key, "global_key": global_key, "default": default,
            "min": lo, "max": hi, "global_rule": global_rule,
            "label": label, "unit": unit, "type": "int"}


# 逻辑键 → 定义。顺序即 GET schema 里行为 8 行的顺序（INTERFACE 3.1 行为表从上到下），
# label / unit 逐字照 INTERFACE 3.1。min/max 是 bot 层可写范围（含两端）。
BEHAVIOR_KEYS = {
    "moments.daily_post_limit_per_bot": _entry(
        "moments_daily_limit", "moments.daily_post_limit_per_bot", 3, 0, 50,
        GLOBAL_NON_NEGATIVE, "每个 bot 每天发帖上限", "条"),
    "moments.silence_threshold_minutes": _entry(
        "moments_silence_threshold_minutes", "moments.silence_threshold_minutes", 30, 0, 1440,
        GLOBAL_POSITIVE, "刚聊完天后多久内不主动发朋友圈（0 = 不设静默期）", "分钟"),
    "silence_threshold_minutes": _entry(
        "proactive_silence_threshold_minutes", "silence_threshold_minutes", 30, 0, 1440,
        GLOBAL_POSITIVE, "刚聊完天后多久内不主动找你（0 = 不设静默期）", "分钟"),
    "moments.min_interval_minutes": _entry(
        "moments_min_interval_minutes", "moments.min_interval_minutes", 60, 0, 1440,
        GLOBAL_IN_RANGE, "发圈两条之间至少隔多久（0 = 不设间隔）", "分钟"),
    "moments.same_kind_hours": _entry(
        "moments_same_kind_hours", "moments.same_kind_hours", 4, 0, 72,
        GLOBAL_IN_RANGE, "同类朋友圈之间至少隔多久（0 = 不设限制）", "小时"),
    "proactive_max_calls_per_5h": _entry(
        "proactive_max_calls_per_5h", "proactive_max_calls_per_5h", 0, 0, 1000,
        GLOBAL_IN_RANGE, "主动找你每 5 小时最多几次（0 = 不限）", "次"),
    "proactive_cooldown_min_minutes": _entry(
        "proactive_cooldown_min_minutes", "proactive_cooldown_min_minutes", 30, 1, 960,
        GLOBAL_POSITIVE, "主动消息最短冷却", "分钟"),
    "proactive_cooldown_max_minutes": _entry(
        "proactive_cooldown_max_minutes", "proactive_cooldown_max_minutes", 1440, 1, 1440,
        GLOBAL_POSITIVE, "主动消息最长冷却", "分钟"),
}


def _dig(cfg, dotted):
    """按点分路径取值；任一层不是 dict 或缺键 → ``_MISSING``。"""
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def _as_int(value):
    """bot 层的整数认法（照 claude `rhythm_config._as_int`）。

    int（排除 bool）、整数值的有限 float、能解析成整数的字符串（`"30"`、`" 30 "`、
    `"30.0"`）→ int；`"30.5"`、`"abc"`、`""`、None、NaN、±Inf、bool → ``_MISSING``。
    """
    if isinstance(value, bool):
        return _MISSING
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value.is_integer() else _MISSING
    if isinstance(value, str):
        try:
            num = float(value.strip())
        except ValueError:
            return _MISSING
        if math.isfinite(num) and num.is_integer():
            return int(num)
    return _MISSING


def _bot_int(spec, raw):
    """bot 层：认成整数且在 [min, max] 内 → int，否则 ``_MISSING``。"""
    value = _as_int(raw)
    if value is _MISSING or not (spec["min"] <= value <= spec["max"]):
        return _MISSING
    return value


def bot_value_valid(key, raw) -> bool:
    """`raw`（生活设定文件里 `bot_key` 的原值）在 bot 层算不算「设了」。

    False 表示消费方会当没设、回退全局（GET 据此记 `invalid_value:<key>`）。
    `key` 不是 8 个行为逻辑键之一 → KeyError（调用方的编程错误，不吞）。
    """
    return _bot_int(BEHAVIOR_KEYS[key], raw) is not _MISSING


def global_value(key, global_cfg):
    """没有 bot 覆盖时消费方实际会用的值 → ``(值, 是否默认)``。

    全局配置里这个键没有、或值不合这个键的全局口径时回 ``(default, True)``。
    `global_cfg` 不是 dict（读不出、顶层不是映射）也按默认，不抛。
    """
    spec = BEHAVIOR_KEYS[key]
    raw = _dig(global_cfg, spec["global_key"])
    # 全局层只认真正的 int：bool 是 int 子类，要先排掉；数字串、小数一律不认（改动前口径）
    if isinstance(raw, int) and not isinstance(raw, bool):
        rule = spec["global_rule"]
        if rule == GLOBAL_NON_NEGATIVE:
            ok = raw >= 0
        elif rule == GLOBAL_POSITIVE:
            ok = raw > 0
        else:
            ok = spec["min"] <= raw <= spec["max"]
        if ok:
            return raw, False
    return spec["default"], True


def resolve(key, bot_cfg, global_cfg) -> int:
    """消费方取值入口：bot 覆盖（合规才算）→ 全局（按全局口径）→ 默认。"""
    spec = BEHAVIOR_KEYS[key]
    raw = bot_cfg.get(spec["bot_key"], _MISSING) if isinstance(bot_cfg, dict) else _MISSING
    value = _bot_int(spec, raw)
    if value is not _MISSING:
        return value
    return global_value(key, global_cfg)[0]
