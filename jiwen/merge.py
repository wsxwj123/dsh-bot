"""积温（jiwen）全局配置 + 每 bot 覆盖的逐项合并（纯标准库，不依赖 Flask）。

优先级：全局 ``jiwen.<节>`` ← bot ``jiwen.<节>_override`` ← bot 旧写法 ``jiwen.<节>``
（旧写法后写，两种都在时旧写法生效，与 claude 侧同）。

两层口径不同（PLAN dsh-rhythm ③ 3.5）：
- **全局层**：能转成有限数字就用，**不卡范围**（同改动前：改动前全局值是 ``float()``
  直接用的；这里只多拦了 NaN/±Inf 和转不成数的，原来那两种会让整次心跳抛错或用 NaN）。
- **bot 层**：有限数字（数字串也认，布尔不认）且落在范围表内（含两端）才算设了；
  否则这一项跳过、保留上一层的值。0 在范围内就是有效值。

任何坏值都不抛：心跳不能被一只 bot 的手改配置带崩（改动前文字、``null``、
``rates_override:`` 留空都会让这只 bot 整次心跳抛错）。

返回「字段名 → 数值」的 dict，由调用方用自己的 ``engine`` 构造 ``Rates`` / ``Thresholds``：
``tick.py`` 是 ``import engine``（把 ``jiwen/`` 塞进 sys.path），``reader.py`` 是
``from . import engine``，同一文件会被加载成两个模块对象，这里若返回 dataclass 会出现
「两个 Rates 类」。本模块只拿 ``engine`` 的 dataclass 默认值（纯数字，哪个模块对象都一样）。

范围表与 ``moments.hub_config._RATE_META / _THRESHOLD_META`` 同一口径，一致性由
tests/unit/test_jiwen_merge.py 钉住（不做导入期断言：那会让适配服务整个起不来）。
"""
from __future__ import annotations

import dataclasses
import math

from . import engine

# dsh engine.Rates 的 21 个标量字段（不含 immersion_map）→ bot 层可写范围（含两端）
RATE_BOUNDS = {
    "connection_per_min": (0.0, 1.0),
    "pride_decay_per_min": (0.0, 1.0),
    "valence_decay_per_min": (0.0, 1.0),
    "arousal_decay_per_min": (0.0, 1.0),
    "valence_lock_factor": (0.0, 1.0),
    "arousal_connection_rise_rate": (0.0, 1.0),
    "pride_defend_rate": (0.0, 1.0),
    "pride_arousal_conflict_rate": (0.0, 1.0),
    "pride_erosion_rate": (0.0, 1.0),
    "immersion_decay_per_min": (0.0, 1.0),
    "activity_connection_relief": (0.0, 1.0),
    "connection_accel": (0.0, 5.0),
    "valence_severe_multiplier": (0.0, 5.0),
    "valence_mild_multiplier": (0.0, 5.0),
    "valence_severe_low": (-1.0, 1.0),
    "valence_mild_low": (-1.0, 1.0),
    "valence_lock_threshold": (-1.0, 1.0),
    "arousal_connection_rise_threshold": (-1.0, 1.0),
    "pride_defend_threshold": (-1.0, 1.0),
    "pride_defend_target": (-1.0, 1.0),
    "accel_threshold_min": (0.0, 1440.0),
}
# engine.Thresholds 的 7 个字段。前三项与 hub_config._THRESHOLD_META 一致（页面可编辑）；
# 后四项页面不编辑、手写 yml 仍认，范围照 claude 侧 jiwen/merge.py 按引擎语义取。
THRESHOLD_BOUNDS = {
    "notice": (0.0, 1.0),
    "consider": (0.0, 1.0),
    "forced": (0.0, 1.0),
    "pride_block": (0.0, 1.0),
    "valence_activity": (-1.0, 1.0),
    "arousal_agitation": (0.0, 1.0),
    "immersion_block": (0.0, 1.0),
}


def _defaults(cls) -> dict:
    return {f.name: f.default for f in dataclasses.fields(cls)
            if f.default is not dataclasses.MISSING}


def _mapping(value) -> dict:
    """YAML 里留空的节解析成 None；不是映射的一律当空表。"""
    return value if isinstance(value, dict) else {}


def _global_number(raw):
    """全局层：``float()`` 转得动且有限 → float，否则 None（同改动前，只多拦非有限）。"""
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _bot_number(raw, bounds):
    """bot 层：有限数字（数字串也认，布尔不认）且在范围内 → float，否则 None。"""
    if isinstance(raw, bool):
        return None
    value = _global_number(raw)
    if value is None:
        return None
    lo, hi = bounds
    return value if lo <= value <= hi else None


def _bot_layers(bot_jiwen, section):
    bot = _mapping(bot_jiwen)
    # 旧写法放后面：后写的覆盖前面的，两种都在时旧写法生效
    return [_mapping(bot.get(section + "_override")), _mapping(bot.get(section))]


def _merge(global_jiwen, bot_jiwen, section, bounds_table, defaults):
    global_layer = _mapping(_mapping(global_jiwen).get(section))
    layers = _bot_layers(bot_jiwen, section)
    out = {}
    for name, bounds in bounds_table.items():
        value = float(defaults[name])
        if name in global_layer:
            got = _global_number(global_layer[name])
            if got is not None:
                value = got
        for layer in layers:
            if name in layer:
                got = _bot_number(layer[name], bounds)
                if got is not None:
                    value = got
        out[name] = value
    return out


def rate_values(global_jiwen, bot_jiwen) -> dict:
    """21 个速率字段名 → float。参数是全局 ``jiwen`` 节与 bot 的 ``jiwen`` 节（都可为 None）。"""
    return _merge(global_jiwen, bot_jiwen, "rates", RATE_BOUNDS, _defaults(engine.Rates))


def threshold_values(global_jiwen, bot_jiwen) -> dict:
    """7 个阈值字段名 → float。参数同 ``rate_values``。"""
    return _merge(global_jiwen, bot_jiwen, "thresholds", THRESHOLD_BOUNDS,
                  _defaults(engine.Thresholds))


def immersion_map(global_jiwen, bot_jiwen) -> dict:
    """``rates.immersion_map``：维持改动前的整表替换口径（不在节奏页编辑范围）。

    全局 ``rates`` ← ``rates_override`` ← 旧写法 ``rates``，最后一个写了这个键的层整表生效；
    值为空或不是映射 → engine 默认表（改动前空值同样回默认，非映射会抛，这里不抛）。
    条目值不校验，同改动前。
    """
    chosen = None
    for layer in [_mapping(_mapping(global_jiwen).get("rates"))] + _bot_layers(bot_jiwen, "rates"):
        if "immersion_map" in layer:
            chosen = layer["immersion_map"]
    if isinstance(chosen, dict) and chosen:
        return dict(chosen)
    return dict(engine.Rates().immersion_map)
