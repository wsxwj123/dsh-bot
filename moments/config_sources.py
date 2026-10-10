# -*- coding: utf-8 -*-
"""管理台读的 bot 配置"两套根"（新旧系统混跑期的过渡件）。

- **旧系统**（legacy）：``HUB_CONFIGS_DIR``，默认仓库 ``configs/`` —— 与
  ``bots_registry.configs_dir()`` / ``config_loader`` 同源，不另立第二套寻址。
- **新系统**（dsh）：``HUB_CONFIGS_DSH_DIR``，默认 ``~/.dsh-bot/configs``。

默认**两边都读**；同名（同一个 ``<bot>.yml`` 文件名）以**新系统**为准 ——
``roots()`` 的顺序就是覆盖优先级（靠后的赢）。目录不存在一律跳过、不报错：
混跑期机器上只会有一边，另一边天然为空。

关掉一边：

- 新边：``HUB_CONFIGS_DSH_DIR`` 设成 ``off``（``-``/``none``/``0`` 同义，大小写不敏感）；
- 旧边：把 ``HUB_CONFIGS_DIR`` 指到一个不存在的目录（读取端对不存在的边直接跳过）。

导入期不读 env、不碰磁盘，每次调用现读（同 ``bots_registry`` 的接缝口径，
测试用 monkeypatch 换目录，不会串到真配置）。
"""
import os
import re
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SOURCE_LEGACY = "legacy"
SOURCE_DSH = "dsh"
#: 页面上给用户看的来源名
SOURCE_LABELS = {SOURCE_LEGACY: "旧系统", SOURCE_DSH: "新系统"}

_OFF_VALUES = {"off", "-", "none", "0", "false", "no"}
#: `find_path` 拼文件名的信任边界：bot 名只许是"一个文件名的形状"，
#: 带路径分隔符/`..`/NUL 的一律当"没这个 bot"（别拿它去拼路径）。
_BOT_ID_RE = re.compile(r"^[^/\\\x00]+$")


def legacy_dir() -> Path:
    """旧系统配置根：``HUB_CONFIGS_DIR``，默认仓库 ``configs/``。"""
    return Path(os.environ.get("HUB_CONFIGS_DIR") or os.path.join(_REPO_ROOT, "configs"))


def dsh_dir():
    """新系统配置根：``HUB_CONFIGS_DSH_DIR``，默认 ``~/.dsh-bot/configs``。

    设成 off/-/none/0（大小写不敏感）→ None，这一边不读；空串按"没设"处理（走默认）。
    """
    raw = os.environ.get("HUB_CONFIGS_DSH_DIR")
    if raw is None or not raw.strip():
        raw = "~/.dsh-bot/configs"
    raw = raw.strip()
    if raw.lower() in _OFF_VALUES:
        return None
    # expanduser 跟 HOME 走：验收环境把 HOME 钉在 tmp 时，这条默认路径自然落空，
    # 不会去读真实 ~/.dsh-bot（测试隔离靠它，不靠测试逐个个例记得关）。
    return Path(os.path.expanduser(raw))


def roots():
    """``[(source, Path)]``，顺序 = 覆盖优先级（靠后的赢）。

    不存在的目录也在这里、由读取端跳过 —— 这样"这台机器没有这一边"与
    "用 env 关掉这一边"走的是同一条代码路径，没有第二套特判。
    """
    out = [(SOURCE_LEGACY, legacy_dir())]
    d = dsh_dir()
    if d is not None:
        out.append((SOURCE_DSH, d))
    return out


def panel_bot_configs():
    """面板展示面（朋友圈页、画风页）要的 bot 名单：两套根都读，**含停用的 bot**。

    停用的 bot 不是"没有这个 bot"，是"这会儿跑在另一套系统里"，展示面照样要列出来
    才点得回去（与 /hub 页同口径）。``config_loader.list_enabled_bots`` 的默认口径
    （单根、只启用）是给运行时消费方（拉起、投递）用的，展示面别直接用它的默认值。
    形状见 ``list_enabled_bots`` 的多根分支：每条多带 ``_source`` / ``_shadowed``。
    """
    import config_loader       # 延迟 import：本模块导入期不读 env、不碰磁盘
    return config_loader.list_enabled_bots(include_disabled=True, dirs=roots())


def active_bot_configs():
    """每个 bot 取**在跑的那一份**配置，供"往 bot 投东西"的路径使用。

    混跑期新系统那份 ``enabled: false`` 的意思是"现在跑在旧系统"，通道目录与 inbox
    都在旧系统那边；投递若拿了新系统那份，东西会落进没人读的目录。规则是取优先级
    最高的**启用份**：新系统启用就用新系统，否则回落到旧系统启用那份，两边都没有
    启用份的 bot 不进名单。
    """
    import config_loader
    from bots_registry import is_enabled
    picked = {}
    for source, d in roots():      # 顺序 = 优先级，靠后（新系统）赢
        for cfg in config_loader.list_enabled_bots(include_disabled=True, dirs=[(source, d)]):
            cur = picked.get(cfg["_bot_id"])
            if cur is not None and is_enabled(cur) and not is_enabled(cfg):
                continue           # 已有启用份，靠后的停用份不覆盖它
            picked[cfg["_bot_id"]] = cfg
    return [c for _k, c in sorted(picked.items()) if is_enabled(c)]


def find_path(bot_id):
    """该 bot 的配置文件路径（按覆盖规则：新系统优先）；两套都没有 → None。

    只认 ``<bot_id>.yml`` 文件名（管理台名单里的 id 就是文件名 stem）。
    不跟 ``life_config`` 别名走：别名是"读配置"（config_loader）的事，
    写路径（启停开关）落到哪份文件，按文件名命中才不会写错人。
    """
    if not isinstance(bot_id, str) or not _BOT_ID_RE.match(bot_id):
        return None
    for _source, d in reversed(roots()):
        p = d / ("%s.yml" % bot_id)
        if p.is_file():
            return p
    return None
