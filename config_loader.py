"""加载 bot 配置和全局配置。

以 ``_`` 开头的键是运行时注入的计算字段（年龄/学制/身份），不在 yml 文件里；
``load_bot`` 每次从磁盘重读后在内存态加工，绝不写回。
"""
import datetime
import os
import yaml

import persona_clock
from bots_registry import is_enabled as _enabled   # 停用判定只有一处，别在这里再写一遍
from bots_registry import configs_dir as _registry_configs_dir

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIGS_DIR = os.path.join(PROJECT_ROOT, "configs")
_DEFAULT_CONFIGS_DIR = CONFIGS_DIR
GLOBAL_CFG_PATH = os.path.join(CONFIGS_DIR, "_global.yml")


def _configs_dir() -> str:
    """bot yml 目录，与 ``bots_registry.configs_dir()`` 同源：每次现读 ``HUB_CONFIGS_DIR``。

    导入期快照会让"注册表认停用、这里读另一份目录"两处不一致；
    ``CONFIGS_DIR`` 被显式改过（既有测试的 monkeypatch 写法）则以它为准。
    """
    if CONFIGS_DIR != _DEFAULT_CONFIGS_DIR:
        return CONFIGS_DIR
    return str(_registry_configs_dir())


def load_global() -> dict:
    if not os.path.exists(GLOBAL_CFG_PATH):
        return {}
    with open(GLOBAL_CFG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _life_alias(d: str, bot_id: str):
    """新系统（dsh-bot）的配置也能用旧名字找到：朋友圈、画风里记的是 bot 在旧系统里的名字。
    旧名字是配置里的 life_id，没写就取 life_config 指向的旧配置文件名。"""
    if not os.path.isdir(d):
        return None
    for fn in sorted(os.listdir(d)):
        if fn.startswith("_") or not fn.endswith(".yml"):
            continue
        try:
            with open(os.path.join(d, fn), encoding="utf-8") as f:
                y = yaml.safe_load(f) or {}
        except Exception:
            continue
        life = y.get("life_id")
        if not life and isinstance(y.get("life_config"), str) and y["life_config"].strip():
            life = os.path.splitext(os.path.basename(y["life_config"].strip()))[0]
        if life and str(life) == bot_id:
            return os.path.join(d, fn)
    return None


def load_bot(bot_id: str, configs_dir=None) -> dict:
    """读一个 bot 的配置（含 ``life_config`` 合并与计算字段注入）。

    ``configs_dir`` 指定从哪个配置根找（管理台的多根扫描用它）；缺省仍是
    ``_configs_dir()``（``HUB_CONFIGS_DIR`` 接缝，运行时消费方零变化）。
    """
    d = configs_dir or _configs_dir()
    p = os.path.join(d, f"{bot_id}.yml")
    if not os.path.exists(p):
        p = _life_alias(d, bot_id) or p
    if not os.path.exists(p):
        raise FileNotFoundError(f"配置不存在：{p}")
    with open(p, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    # 新系统（dsh-bot 网关）的 bot 配置可以用 life_config 指向旧配置：作息、生活、情绪等设置从那里读，
    # 新配置里写了的键优先（bot_channel_path、brain、gateway 等）。
    life = cfg.get("life_config")
    if isinstance(life, str) and life.strip():
        with open(os.path.expanduser(life.strip()), encoding="utf-8") as f:
            base = yaml.safe_load(f) or {}
        cfg = {**base, **cfg}
    # 朋友圈、画风里用的名字（旧系统里的 bot 名），见 _life_alias
    life_id = cfg.get("life_id") or (os.path.splitext(os.path.basename(life.strip()))[0] if isinstance(life, str) and life.strip() else None)
    if life_id:
        cfg["_life_id"] = str(life_id)

    # 运行时会注入的计算字段不应来自 YAML；先清掉残留，避免旧值绕过年龄闸门。
    cfg.pop("_age", None)
    cfg.pop("_schooling", None)
    cfg.pop("_identity", None)

    today = datetime.date.today()
    age = persona_clock.age_at(cfg, today)
    schooling = persona_clock.schooling_at(cfg, today)
    identity = persona_clock.identity_line(cfg, today)

    # 加工是内存态行为：计算字段只用下划线前缀标记，绝不写回 yml。
    if age is not None:
        cfg["_age"] = age
    if schooling is not None:
        cfg["_schooling"] = schooling
    if identity:
        cfg["_identity"] = identity

    # persona_summary：原字段非空且身份串非空才前置【当前身份】行。
    persona_summary = cfg.get("persona_summary")
    if isinstance(persona_summary, str) and persona_summary.strip() and identity:
        cfg["persona_summary"] = "【当前身份】" + identity + "\n" + persona_summary

    # face_traits：成年才前置无空格年龄；未成年拦截并 warn 一次（生图提示词永不带未成年年龄）。
    face_traits = cfg.get("face_traits")
    if isinstance(face_traits, str) and face_traits.strip():
        if age is not None and age >= persona_clock.MIN_AGE_IN_PROMPT:
            cfg["face_traits"] = f"{age}岁" + face_traits
        elif age is not None and age < persona_clock.MIN_AGE_IN_PROMPT:
            persona_clock._warn(
                persona_clock._bot_id(cfg), "face_traits", "age_below_min",
                "年龄未满 18 岁，已拒绝注入生图提示词", age)

    return cfg


def list_enabled_bots(include_disabled: bool = False, dirs=None) -> list[dict]:
    """枚举所有 configs/<bot>.yml（除 _ 开头的）。

    顶层 ``enabled: false`` 的 bot 默认跳过 —— 判定与 ``bots_registry._enabled`` 同一口径
    （没写字段 = 启用，只有显式假值才停），两处必须一致，否则"停了"在这条路径上不生效。
    ``include_disabled=True`` 只给管理台列表用：停用的 bot 得留在页面上才点得回来。

    ``dirs=None``（默认）：只扫 ``_configs_dir()`` 一个根 —— 行为与以前一字不改。
    ``dirs=[(source, 目录), ...]``：按顺序扫多个根，**同名以靠后的为准**（管理台传
    ``[("legacy", 旧根), ("dsh", 新根)]`` = 新系统那份赢）；每个 cfg 多带两个下划线标记：
    ``_source``（来自哪个根）与 ``_shadowed``（它压住了前面低优先根里的同名 bot）。
    不存在的根跳过；坏掉的 yml 只跳过它自己（同单根口径）。
    """
    multi = dirs is not None
    roots = list(dirs) if multi else [(None, _configs_dir())]
    out = {}
    for source, d in roots:
        d = str(d)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.startswith("_") or not fn.endswith(".yml"):
                continue
            bot_id = fn[:-4]
            try:
                cfg = load_bot(bot_id, configs_dir=d)
            except Exception:
                continue
            cfg["_bot_id"] = bot_id
            if multi:
                if bot_id in out:        # 靠后的根压住同名：旧的那条整个换掉
                    cfg["_shadowed"] = True
                cfg["_source"] = source
            out[bot_id] = cfg
    # 停用过滤放在合并**之后**：同名覆盖时停用标记以生效的那份（新系统）为准，
    # 扫到一半就按低优先那份的 enabled 过滤会放行/漏掉错的 bot。
    rows = [out[k] for k in sorted(out)] if multi else list(out.values())
    if include_disabled:
        return rows
    return [c for c in rows if _enabled(c)]


def in_sleep_hours(cfg: dict, now) -> bool:
    """now 在 sleep_hours 任一区间内 → True"""
    from generators.situation import _time_in_range
    for spec in cfg.get("sleep_hours", []):
        if _time_in_range(now.time(), spec):
            return True
    return False
