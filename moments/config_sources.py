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

**"该不该投递、投给谁"只有这里一个出处**：``delivery_configs`` / ``delivery_config_for``
给投递面（每条都是"这个人在跑的那份"），``pick_per_person`` 给展示面挑同一个人的
哪一份（朋友圈页的 chip 与投递面必须挑同一份，不然显示与投递会指向两个 bot）。
"某个 bot 算不算在跑"的判定本身在 ``bots_registry.running_ids``，本模块只负责
把两套根递过去（``running_names``），不另立一份判法。

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

    这里仍然每个名字只出一条（同名以新系统为准），因为画风页的 id 契约就是
    ``<配置文件名>``。朋友圈名（life 名）撞车由 ``pick_per_person`` 处理。
    """
    import config_loader       # 延迟 import：本模块导入期不读 env、不碰磁盘
    return config_loader.list_enabled_bots(include_disabled=True, dirs=roots())


def _copies_by_name():
    """``{bot 名: [每套根里的一份配置, 按优先级从低到高]}``（同名在两套根里各有一份时两条都在）。"""
    import config_loader
    out = {}
    for source, d in roots():      # 顺序 = 优先级，靠后（新系统）赢
        for cfg in config_loader.list_enabled_bots(include_disabled=True, dirs=[(source, d)]):
            out.setdefault(cfg["_bot_id"], []).append(cfg)
    return out


def running_names():
    """现在在跑的 bot 名。判定只有一处（``bots_registry.running_ids``），这里只把两套根递给它。"""
    import bots_registry
    return bots_registry.running_ids(dirs=[str(d) for _s, d in roots()])


def is_running(bot_id):
    """这个 bot 名现在算不算在跑（只要有一份启用配置就算）。

    投递名单之外的入口（dispatcher 拉起、评论回复）用它再守一道。判"停用"只许问
    这一处：旧版这里问的是只扫旧根的单根口径，某个 bot 迁到新系统、旧系统留着
    一份停用配置时，通知写进了 inbox 却没人被拉起。
    """
    return bot_id in running_names()


def active_bot_configs():
    """每个 bot 取**在跑的那一份**配置，供"往 bot 投东西"的路径使用。

    混跑期新系统那份 ``enabled: false`` 的意思是"现在跑在旧系统"，通道目录与 inbox
    都在旧系统那边；投递若拿了新系统那份，东西会落进没人读的目录。规则是取优先级
    最高的**启用份**：新系统启用就用新系统，否则回落到旧系统启用那份，两边都没有
    启用份的 bot 不进名单（"谁在跑"由 ``bots_registry.running_ids`` 一处判定）。
    """
    from bots_registry import is_enabled
    running = running_names()
    picked = {}
    for bot_id, copies in _copies_by_name().items():
        if bot_id not in running:
            continue
        for cfg in copies:          # 靠后的根（新系统）覆盖前面的
            if is_enabled(cfg):
                picked[bot_id] = cfg
    return [picked[k] for k in sorted(picked)]


def _effective_of(copies):
    """一个名字的多份配置里哪份生效：在跑的那份优先（启停开关要对准它才停得掉），
    没有在跑的就按覆盖规则取优先级最高那份（启用一个停着的 bot，落点就是它）。"""
    from bots_registry import is_enabled
    for cfg in reversed(copies):    # 先看新系统那份
        if is_enabled(cfg):
            return cfg
    return copies[-1]


def effective_config(bot_id):
    """这个 bot 名**生效的那份**配置；两套根里都没有这个名字 → None。"""
    copies = _copies_by_name().get(bot_id)
    return _effective_of(copies) if copies else None


def effective_configs():
    """``{bot 名: 生效那份}``。一次扫描给全表（/hub 页逐行标来源用）。"""
    return {name: _effective_of(copies) for name, copies in _copies_by_name().items()}


def person_id(cfg):
    """这份配置在朋友圈里记的名字（新系统的 bot 用 life 别名，如 bot4 记 chenlulu）。"""
    return cfg.get("_life_id") or cfg["_bot_id"]


def config_rank(cfg):
    """同一个人撞名时两份配置谁赢：在跑的那份优先，其次新系统（dsh）优先。"""
    from bots_registry import is_enabled
    return (1 if is_enabled(cfg) else 0,
            1 if cfg.get("_source") == SOURCE_DSH else 0)


def config_tag(cfg):
    """日志里指认一份配置，如 bot5(dsh)。"""
    return "%s(%s)" % (cfg.get("_bot_id") or cfg.get("id"), cfg.get("_source") or "单根")


def pick_per_person(configs):
    """``(留下来的名单, 撞名记录)``：同一个人（朋友圈名相同）只留一份配置。

    两份配置记同一个 life 名就是同一个人，数据只有一份，投递也只能发一份
    （展示面的 chip 同理）。挑法见 ``config_rank``。本函数不写 stderr，
    留痕由调用方决定（撞名不许静默，但也不许每次渲染都写）。
    """
    picked, collisions = {}, []
    for cfg in configs:
        key = person_id(cfg)
        cur = picked.get(key)
        if cur is None:
            picked[key] = cfg
            continue
        win, drop = (cfg, cur) if config_rank(cfg) > config_rank(cur) else (cur, cfg)
        picked[key] = win
        collisions.append({"key": key, "kept": win, "dropped": drop})
    return [picked[k] for k in sorted(picked)], collisions


def delivery_plan():
    """``(投递名单, 撞名记录)``：**哪些 bot 该被投递、每个投给谁**的唯一来源。

    每个 bot 取在跑的那份配置（``active_bot_configs``），同一个人只留一份
    （``pick_per_person``）。投递路径拿到的每条配置就是"这个人现在跑着的那份"，
    调用方不需要、也不许再自己判一次停用（那正是两套口径打架的地方）。
    撞名记录给调用方留痕用（同一个人两份都在跑是异常态，压掉哪份要看得出来）。
    """
    return pick_per_person(active_bot_configs())


def delivery_configs():
    """投递名单（不含撞名记录，给只关心"投给谁"的调用方）。"""
    return delivery_plan()[0]


def delivery_config_for(name):
    """这个人在跑的那份配置；没在跑（停用 / 没有这个名字）→ None。"""
    for cfg in delivery_configs():
        if person_id(cfg) == name:
            return cfg
    return None


def find_path(bot_id):
    """该 bot 的配置文件路径：**在跑的那份优先**，没有在跑的按覆盖规则取优先级最高那份；两套都没有 → None。

    启停开关要写进"正在跑的那份"才停得掉那个进程。混跑期新系统那份写着
    ``enabled: false``（意思是跑在旧系统）时，写新系统那份既停不掉它、
    又在那边留下一处没人看的改动。读坏的 yml 当"没在跑"处理，不改变命中结果
    （坏文件本身照旧由写入端报 409）。

    只认 ``<bot_id>.yml`` 文件名（管理台名单里的 id 就是文件名 stem）。
    不跟 ``life_config`` 别名走：别名是"读配置"（config_loader）的事，
    写路径（启停开关）落到哪份文件，按文件名命中才不会写错人。
    """
    if not isinstance(bot_id, str) or not _BOT_ID_RE.match(bot_id):
        return None
    priority, running = None, None
    for _source, d in roots():            # 顺序 = 优先级，靠后（新系统）赢
        p = d / ("%s.yml" % bot_id)
        if not p.is_file():
            continue
        priority = p                      # 最后留下的是优先级最高的那份文件
        if _is_enabled_file(p):
            running = p                   # 在跑的那份压过优先级：停它才停得掉
    return running or priority


def _is_enabled_file(path):
    """一份 yml 顶层 ``enabled`` 是不是启用的；读不了 / 不是映射一律当没启用。"""
    import yaml
    from bots_registry import is_enabled
    try:
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    except Exception:
        return False
    return is_enabled(cfg if isinstance(cfg, dict) else {})
