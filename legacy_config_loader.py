"""加载 bot 配置和全局配置（**旧系统**语义的搬运副本）。

本仓根已有 ``config_loader.py``（新系统那份：多根扫描、``life_config`` 合并、HUB_CONFIGS_DIR
接缝）。旧 provider 面板整条链（provider_config / restart_bot_worker）依赖的是**旧语义**：
``load_bot`` 直读 ``<config_dir>/<bot>.yml``、``list_discovered_bots`` 扫 channels 目录、
``set_global_aux_llm`` 文本级改写 ``_global.yml``，且默认路径必须落在旧仓那份数据上。
两者差异是行为差异，不能互相顶替，故本副本独立存在，消费方显式 ``import
legacy_config_loader as config_loader``。

以 ``_`` 开头的键是运行时注入的计算字段，不在 yml 文件里；
``load_bot`` 每次从磁盘重读后在内存态加工，绝不写回。
"""
import os
import re
import sys
import datetime
import yaml
import persona_clock

import provider_paths

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
# 默认指向**旧仓**的 configs/（yml 发现与 aux_llm 写回都落旧仓那份，8770 与旧 bot 才读得到）；
# CLAUDEBOT_CONFIG_DIR 覆盖接缝不变。CONFIGS_DIR 仍可被测试 monkeypatch。
CONFIGS_DIR = os.path.join(provider_paths.legacy_root(), "configs")
GLOBAL_CFG_PATH = os.path.join(CONFIGS_DIR, "_global.yml")

# INTERFACE §2.1：bot 标识只允许字母/数字开头，后续可含字母/数字/_/-，
# 从源头排除 _global.yml 与 ../ 等路径穿越。
BOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _is_safe_bot_id(bot_id: str) -> bool:
    """判断 bot_id 是否符合安全 bot 标识契约。"""
    return isinstance(bot_id, str) and BOT_ID_RE.fullmatch(bot_id) is not None


def _validate_bot_id(bot_id: str) -> None:
    """bot_id 不合法时抛 ValueError，错误信息不泄露路径/配置内容。"""
    if not _is_safe_bot_id(bot_id):
        raise ValueError(f"{bot_id} 非法 bot 标识")


def config_dir() -> str:
    """返回 bot 配置目录；支持公开测试/运维接缝 CLAUDEBOT_CONFIG_DIR。"""
    return os.environ.get("CLAUDEBOT_CONFIG_DIR") or CONFIGS_DIR


def global_cfg_path() -> str:
    """返回全局配置路径；支持公开测试/运维接缝 CLAUDEBOT_GLOBAL_CFG。"""
    global_cfg = os.environ.get("CLAUDEBOT_GLOBAL_CFG")
    if global_cfg:
        return global_cfg
    return os.path.join(config_dir(), "_global.yml")


def load_global() -> dict:
    path = global_cfg_path()
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _yaml_scalar(value: str) -> str:
    """把字符串渲染成一个安全的单行 YAML 标量。

    直接 ``yaml.safe_dump`` 会在裸标量后附文档结束标记 ``...``（多一个 key 就会破坏
    整份文件的 yaml 结构），故只取首行。
    """
    dumped = yaml.safe_dump(value, allow_unicode=True).strip()
    return dumped.splitlines()[0] if dumped else "''"


def set_global_aux_llm(provider_id: str, model: str) -> None:
    """把 ``aux_llm`` 块写回全局配置（INTERFACE-aux-llm-provider §9）。

    采用**文本级 upsert**而非整份 yaml 重写：只替换/追加 aux_llm 这一段，其余键
    与注释逐字保留（真实 _global.yml 满是注释，整份 dump 会把它们全丢掉）。
    同目录 tmp + fsync + os.replace 原子落盘。缺文件则创建。
    """
    path = global_cfg_path()
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        text = ""
    block = "aux_llm:\n  provider_id: %s\n  model: %s\n" % (
        _yaml_scalar(provider_id or ""), _yaml_scalar(model or ""))
    lines = text.splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if ln.startswith("aux_llm:")), None)
    if start is None:
        new_text = text
        if new_text and not new_text.endswith("\n"):
            new_text += "\n"
        new_text += block
    else:
        # 块结束 = 下一个无缩进的非空行（顶层键/注释）或文件尾
        end = len(lines)
        for j in range(start + 1, len(lines)):
            ln = lines[j]
            if ln.strip() and not ln[0].isspace():
                end = j
                break
        new_text = "".join(lines[:start]) + block + "".join(lines[end:])
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = "%s.tmp-%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new_text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_bot(bot_id: str) -> dict:
    _validate_bot_id(bot_id)
    p = os.path.join(config_dir(), f"{bot_id}.yml")
    if not os.path.exists(p):
        raise FileNotFoundError(f"配置不存在：{p}")
    with open(p) as f:
        cfg = yaml.safe_load(f) or {}

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

    # face_traits：成年才前置无空格年龄；未成年拦截并 warn 一次。
    face_traits = cfg.get("face_traits")
    if isinstance(face_traits, str) and face_traits.strip():
        if age is not None and age >= persona_clock.MIN_AGE_IN_PROMPT:
            cfg["face_traits"] = f"{age}岁" + face_traits
        elif age is not None and age < persona_clock.MIN_AGE_IN_PROMPT:
            persona_clock._warn(
                persona_clock._bot_id(cfg), "face_traits", "age_below_min",
                "年龄未满 18 岁，已拒绝注入生图提示词", age)

    return cfg


def list_enabled_bots() -> list[dict]:
    """枚举配置目录下所有符合安全命名的 <bot>.yml。"""
    bots = []
    configs = config_dir()
    if not os.path.isdir(configs):
        return bots
    for fn in sorted(os.listdir(configs)):
        if not fn.endswith(".yml"):
            continue
        bot_id = fn[:-4]
        # 只枚举满足安全 bot 标识规则的文件名，_global.yml 等一律跳过。
        if not _is_safe_bot_id(bot_id):
            continue
        try:
            cfg = load_bot(bot_id)
            cfg["_bot_id"] = bot_id
            bots.append(cfg)
        except Exception:
            continue
    return bots


# ── bot 自动发现（channels 目录扫描）────────────────────────────────────────
# 目录 bot 判据：名称过安全正则、不在 NON_BOT_DIRS、非符号链接、含 CLAUDE.md 标志物。
# 选 CLAUDE.md 而非 .claude/：前者是每个 bot 工作区的固定锚（新建目录即可被发现），
# 后者是"面板写过 provider 之后"的产物，拿它当判据会把刚建好的新 bot 挡在门外。
NON_BOT_DIRS = frozenset({"media", "group_transcripts"})
BOT_MARKER_FILE = "CLAUDE.md"
# 目录名 ≠ bot_id 的已知唯一实例（该目录不出现在任何名单、不可被写入）。
# DIR_ALIASES 与 LEGACY_BOT_KEYS（styles_routes）均为**封闭集合**：仅当出现"目录-bot
# 历史失配、且 yml 路径无法自动认领"时才登记映射对；常态新增 bot（目录名=bot 名，
# 或有 yml 可被认领）一律不进表。
DIR_ALIASES = {"telegram": "yasuna"}


def channels_dir() -> str:
    """channels 根目录；支持公开测试/运维接缝 CLAUDEBOT_CHANNELS_ROOT（与 provider_config 同名接缝）。"""
    return os.environ.get("CLAUDEBOT_CHANNELS_ROOT") or os.path.expanduser("~/.claude/channels")


def _is_bot_dir(name: str, path: str) -> bool:
    """目录是否像一个 bot 工作区：安全名 + 非黑名单 + 非符号链接 + 含标志物（只做存在性检查）。"""
    if not _is_safe_bot_id(name) or name in NON_BOT_DIRS:
        return False
    if os.path.islink(path):
        return False
    return os.path.isfile(os.path.join(path, BOT_MARKER_FILE))


def _yml_bot_ids() -> list[str]:
    """配置目录下所有符合安全命名的 ``<bot>.yml`` 的 bot_id（按名升序，含 load 失败的）。

    与 ``list_enabled_bots`` 同一命名过滤（安全 bot 标识），只是**不尝试加载**——
    供发现列表把「yml 在但加载失败」的 bot 补回名单（E-14）。
    """
    configs = config_dir()
    try:
        names = sorted(os.listdir(configs))
    except OSError:
        return []
    return [fn[:-4] for fn in names if fn.endswith(".yml") and _is_safe_bot_id(fn[:-4])]


def list_discovered_bots() -> list[dict]:
    """发现的 bot 名单 = yml 段 ∪ 目录段（yml 段与目录段各自按 bot_id 升序后拼接）。

    返回轻量记录 ``{"bot_id","display_name","has_config","bot_dir"}``（不做人设加工）。
    与 ``list_enabled_bots()`` 分工：本函数供面板名单类消费方（provider/styles/feed 胶囊）；
    需要完整 yml 人设的消费方（daily-wildcard、用户发帖触发评论等）继续用前者。
    去重三级：DIR_ALIASES → 目录名 == yml bot_id → yml 路径归一化兜底（仅绝对路径参与认领）。
    不抛异常：channels 根不存在/不可读 → 仅 yml 段。
    """
    yml_bots = []
    claimed_names = set()
    claimed_realpaths = set()
    for cfg in list_enabled_bots():
        bot_id = cfg["_bot_id"]
        claimed_names.add(bot_id)
        raw_dir = cfg.get("bot_channel_path")
        if isinstance(raw_dir, str) and raw_dir:
            bot_dir = raw_dir
            expanded = os.path.expanduser(raw_dir)
            if os.path.isabs(expanded):
                claimed_realpaths.add(os.path.realpath(expanded))
        else:
            bot_dir = os.path.join(channels_dir(), bot_id)
        yml_bots.append({"bot_id": bot_id,
                         "display_name": cfg.get("display_name") or bot_id,
                         "has_config": True,
                         "bot_dir": bot_dir})
    # 配置损坏的 bot：yml 文件在、但加载失败（list_enabled_bots 会静默跳过）→ 用 broken 标记
    # 补回名单并写 stderr 一行点名（E-14）。绝不静默消失：面板上再也看不到它，用户无从排查。
    for bot_id in _yml_bot_ids():
        if bot_id in claimed_names:
            continue
        sys.stderr.write("[config_loader] bot 配置损坏，无法加载: %s.yml\n" % bot_id)
        yml_bots.append({"bot_id": bot_id,
                         "display_name": bot_id,
                         "has_config": False,
                         "bot_dir": os.path.join(channels_dir(), bot_id),
                         "broken": True})
    yml_bots.sort(key=lambda item: item["bot_id"])

    dir_bots = []
    root = channels_dir()
    try:
        names = sorted(os.listdir(root))
    except OSError:
        names = []
    for name in names:
        if name in DIR_ALIASES or name in claimed_names:
            continue
        path = os.path.join(root, name)
        if not _is_bot_dir(name, path):
            continue
        try:
            if os.path.realpath(path) in claimed_realpaths:
                continue
        except OSError:
            continue
        dir_bots.append({"bot_id": name,
                         "display_name": name,
                         "has_config": False,
                         "bot_dir": path})
    dir_bots.sort(key=lambda item: item["bot_id"])

    return yml_bots + dir_bots


def in_sleep_hours(cfg: dict, now) -> bool:
    """now 在 sleep_hours 任一区间内 → True"""
    from generators.situation import _time_in_range
    for spec in cfg.get("sleep_hours", []):
        if _time_in_range(now.time(), spec):
            return True
    return False
