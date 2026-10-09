"""每只 bot 各用各的 provider —— 配置内核（无 Flask 依赖）。

单一事实源 ``configs/providers.json``；生效落点 = ``<bot_dir>/.claude/settings.json`` 的
``env``（7 键白名单）。本模块是**唯一**的 settings.json 写入点，负责备份/回滚/路径校验/
按 bot 串行（INTERFACE §1.1、§3.2）。
"""
import contextlib
import copy
import hashlib
import itertools
import json
import os
import secrets
import shutil
import sys
import time
from urllib.parse import urlparse

import yaml

import legacy_config_loader as config_loader   # 旧系统的配置加载语义（yml 发现/load_bot）
import provider_paths
import retry_backoff

# ── 测试接缝（INTERFACE §1.1 集中表）────────────────────────────────────────


class SeamsNotConfigured(RuntimeError):
    """接缝类错误的专用类型（rev8）：导入期的成组抛错与只读态的写入口抛错都用它。

    是 ``RuntimeError`` 的子类（既有用法与文案不变）；HTTP 层**只捕这一类**转 503，
    绝不 catch 宽的 RuntimeError，以免把别的 bug 吞成 503。
    """


# 成组判据的**触发集只取本轮新增的两个接缝**（rev7）：真正决定"写到哪个目录"的是
# CLAUDEBOT_CHANNELS_ROOT（写盘前缀校验基准）与 CLAUDEBOT_PROVIDER_PATH（清单落点），
# 所以设了它们之一却没设全 → 导入期 fail-closed。
# 既有的 CLAUDEBOT_CONFIG_DIR（config_loader.py:33-34，本轮之前就有）单独设是合法用法
# （本仓既有 4 处：desync/bot_stop/birthday_grade/config_loader_security）→ 导入放行，
# 但 provider_config 进**只读模式**：读入口正常、一切写入口抛同一条 SeamsNotConfigured。
# 理由：那种环境下 yml 可被重定向到真 yml 副本、bot_channel_path 仍指真目录，
# CHANNELS_ROOT 前缀校验会放过它 → "读到真 yml 却写进真目录"必须在写口挡住。
_SEAM_KEYS = ("CLAUDEBOT_CONFIG_DIR", "CLAUDEBOT_CHANNELS_ROOT", "CLAUDEBOT_PROVIDER_PATH")
_NEW_SEAM_KEYS = ("CLAUDEBOT_CHANNELS_ROOT", "CLAUDEBOT_PROVIDER_PATH")
_SEAM_ERROR = "测试接缝必须成组设置"
_SEAMS_SET = frozenset(k for k in _SEAM_KEYS if os.environ.get(k))
if (_SEAMS_SET & set(_NEW_SEAM_KEYS)) and len(_SEAMS_SET) != len(_SEAM_KEYS):
    raise SeamsNotConfigured(_SEAM_ERROR)

# 搬入本仓后的落点改变（唯一）：清单默认指向**旧仓**那份 —— 面板以后从本仓启动时，
# 写的仍是 8770 provider-proxy 与旧 bot 消费的文件。默认值定义在 provider_paths
# （与 provider_proxy 安全闸同源）；CLAUDEBOT_PROVIDER_PATH 覆盖不变。
PROVIDERS_PATH: str = provider_paths.default_providers_path()
CHANNELS_ROOT: str = os.environ.get("CLAUDEBOT_CHANNELS_ROOT") or os.path.expanduser(
    "~/.claude/channels")
GLOBAL_SETTINGS_PATH: str = os.path.expanduser("~/.claude/settings.json")

# 与 moments/web.py、self-initiate.sh、director.py 一致；唯一定义处（J2）
BOT_PORTS: dict[str, str] = {"yasuna": "17801", "bot2": "17802", "bot3": "17803", "bot4": "17804"}

ALLOWED_ENV_KEYS: tuple[str, ...] = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
)
# local 层出现这些键 = 会盖掉面板写入（local > project，M0 实测）
PROVIDER_KEYS: tuple[str, ...] = (
    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL")

_BACKUPS_KEEP = 5


def seams_redirected() -> bool:
    """三个接缝是否全被重定向（完整测试态）。生产（一个都不设）→ False。

    **不参与任何闸**（rev8 改名，原名 `seams_complete` 的 `complete` 在两种语境下含义相反）：
    它只表达"测试态是否配齐"，与"该不该起收敛器"的判据（`seams_read_only()`）语义相反。
    """
    return len(_SEAMS_SET) == len(_SEAM_KEYS)


def seams_read_only() -> bool:
    """只设了既有 ``CLAUDEBOT_CONFIG_DIR`` → True：读放行、写入口抛 SeamsNotConfigured、收敛器不启动。

    也即"接缝不全"这一危险态——注意**生产态（一个都不设）不是**接缝不全，
    它的路径就是生产路径，读写与后台收敛都应当正常工作。**闸只认这个函数。**
    """
    return _SEAMS_SET == {"CLAUDEBOT_CONFIG_DIR"}


class SafetyGateTripped(RuntimeError):
    """测试态安全闸（9.4/R7）：写端点回 500 ``测试接缝必须成组设置``，零写入。"""


def _real_default_paths() -> tuple[str, str]:
    """「真实默认位置」（唯一定义在 provider_proxy.real_default_paths）。单测替换本函数来测请求侧闸。"""
    import provider_proxy
    return provider_proxy.real_default_paths()


def _has_proxy(data) -> bool:
    return isinstance(data, dict) and any(
        isinstance(p, dict) and p.get("proxy") is True for p in data.get("providers") or [])


def safety_gate_tripped(data: dict | None = None) -> bool:
    """请求侧安全闸，只对清单支返回 True（500 零写入）。触及判定：现清单或待写清单有 proxy=true，或运行目录已有 config.yaml；
    清单读不出来时按「会触及」保守处理。"""
    import provider_proxy

    def touches() -> bool:
        if _has_proxy(data) or os.path.exists(provider_proxy.config_path()):
            return True
        try:
            return _has_proxy(load_providers())
        except ProvidersCorrupt:
            return True
    # 勘误 8：只有清单支 500 零写入；运行目录支由 provider_proxy.sync 跳过代理重建并记 last_error
    return provider_proxy.gate_branch(provider_proxy.gate_env_active(), PROVIDERS_PATH, provider_proxy.proxy_dir(),
                                      touches, _real_default_paths()) == provider_proxy.GATE_MANIFEST


def _require_writable(data: dict | None = None) -> None:
    """写闸：只读模式（接缝不成组）→ 503；测试态安全闸 → 500。都排在汇口确保 key 之前。"""
    if seams_read_only():
        raise SeamsNotConfigured(_SEAM_ERROR)
    if safety_gate_tripped(data):
        raise SafetyGateTripped(_SEAM_ERROR)


class ProvidersCorrupt(Exception):
    """providers.json 解析失败（写操作必须拒写且不改文件）。"""


def local_conflict_message(bot_id: str, keys: list) -> str:
    """local 冲突的**函数层**文案（INTERFACE §1.1 末条）——唯一构造处。"""
    return ("%s 的 settings.local.json 里存在 provider 键（local 层优先于项目层），"
            "会覆盖面板写入，请先手工清理: %s" % (bot_id, ", ".join(keys)))


class LocalConflict(RuntimeError):
    """settings.local.json 命中 provider 键（local > project）→ 写入口拒绝（R16）。

    带 ``bot_id`` / ``keys`` 两个字段，供 HTTP 层**按类型**取键名列表映射结构化 409，
    不必去匹配错误文案——文案一改就会静默降级成 400、连 UI 要用的 keys 一起丢。
    函数层（``set_bot_provider`` / ``clear_bot_provider``）仍按 INTERFACE 契约把它转成
    ``(值, 文案)`` 返回，故对这个异常不改变公开返回值形状。
    """

    def __init__(self, bot_id: str, keys: list):
        self.bot_id = bot_id
        self.keys = list(keys)
        super().__init__(local_conflict_message(bot_id, self.keys))


# ── 锁（Y5）：providers.json 一切读改写一把；同一 bot 的「写+kill+spawn」一把 ──
import threading  # noqa: E402  （放在常量之后，保持文件头可读）

_PROVIDERS_LOCK = threading.Lock()
_BOT_LOCKS: dict[str, threading.Lock] = {}
_BOT_LOCKS_GUARD = threading.Lock()
_id_seq = itertools.count()
_LAST_SNAPSHOT: dict[str, dict] = {}


def bot_lock(bot_id: str) -> threading.Lock:
    with _BOT_LOCKS_GUARD:
        lock = _BOT_LOCKS.get(bot_id)
        if lock is None:
            lock = _BOT_LOCKS[bot_id] = threading.Lock()
        return lock


# ── providers.json 读写 ────────────────────────────────────────────────────

def load_providers() -> dict:
    """读 providers.json。缺失/空 → 空结构；解析失败 → ProvidersCorrupt。

    **返回值含明文 api_key**：仅供本模块内部与 set_bot_provider 的 provider 入参使用，
    任何 Flask 视图不得直接序列化它（HTTP 出口一律用 upsert_provider 的脱敏形状）。
    """
    try:
        with open(PROVIDERS_PATH, encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        return {"providers": [], "bindings": {}}
    if not raw.strip():
        return {"providers": [], "bindings": {}}
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise ProvidersCorrupt("providers.json 解析失败") from e
    if not isinstance(data, dict):
        raise ProvidersCorrupt("providers.json 解析失败")
    data.setdefault("providers", [])
    data.setdefault("bindings", {})
    if not isinstance(data["providers"], list):
        data["providers"] = []
    if not isinstance(data["bindings"], dict):
        data["bindings"] = {}
    return data


def _load_providers_safe() -> dict:
    """读侧容错：坏文件当空（effective/list_bots_status 不抛异常，§9.3）。"""
    try:
        return load_providers()
    except ProvidersCorrupt:
        return {"providers": [], "bindings": {}}


def save_providers(data: dict) -> None:
    """原子落盘 0600：同目录 tmp + fsync + os.replace。"""
    _require_writable()
    with _manifest_lock():
        _commit_locked(data, ensure_key=False)   # 通用落盘 API 不是请求汇口，不代生成 key


_LOCK_STATE = threading.local()


@contextlib.contextmanager
def _manifest_lock():
    """取清单锁并在本线程记账。锁序（R6/§9.2）：先清单锁，**释放后**才进配置锁；
    持有配置锁时只读清单、不写清单（provider_proxy 从不 import 本模块，结构上保证）。"""
    with _PROVIDERS_LOCK:
        _LOCK_STATE.held = True
        try:
            yield
        finally:
            _LOCK_STATE.held = False


def _commit_locked(data: dict, ensure_key: bool = True) -> None:
    """清单唯一落盘汇口（调用方须已持清单锁）。第 0 步：确保顶层 ``proxy.instance_key`` 非空——
    已有则原样保留（I2），缺则新生成，不论有无 proxy provider。
    失败态记账（note_error/record_failure/rollback 等）传 ``ensure_key=False``：4xx/500 路径不得生成 key。"""
    _require_writable(data)   # 兜底：任何落盘都先过只读闸与安全闸（只读优先，保持 503 语义）
    if ensure_key:
        meta = data.get("proxy")
        if not isinstance(meta, dict):
            meta = data["proxy"] = {}
        key = meta.get("instance_key")
        if not (isinstance(key, str) and key):
            meta["instance_key"] = secrets.token_urlsafe(32)
        meta["port"] = _current_proxy_port()   # 与 config.yaml 端口对齐（原 ensure_proxy_meta 语义）
    _write_providers_locked(data)


def _current_proxy_port() -> int:
    import provider_proxy   # 调用期导入：单向依赖
    return provider_proxy.proxy_port()


def ensure_instance_key() -> str:
    """写路径（显式 sync / sync_all 等本身不改清单的 2xx 请求）用：缺 key 才落盘，返回 key。"""
    _require_writable()
    with _manifest_lock():
        data = load_providers()
        key = (data.get("proxy") or {}).get("instance_key") if isinstance(data.get("proxy"), dict) else None
        if not (isinstance(key, str) and key) or data["proxy"].get("port") != _current_proxy_port():
            _commit_locked(data)
            key = data["proxy"]["instance_key"]
        return key


def _write_providers_locked(data: dict) -> None:
    _require_writable()  # 所有 providers.json 写盘的唯一出口，只读模式在此挡住
    d = os.path.dirname(PROVIDERS_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d or ".", ".%s.tmp-%d-%d" % (
        os.path.basename(PROVIDERS_PATH), os.getpid(), int(time.time() * 1000)))
    fd = _open_private(tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, PROVIDERS_PATH)
    except OSError:
        _cleanup(tmp)
        raise


# 旧判定键（provider 协议改 provider 级前的历史键）：任意一次 PUT 会从条目删除（INTERFACE §1/§2.5）
_LEGACY_KEYS = ("protocols", "unavailable_models")


def _write_private_copy(src_path: str, dst_path: str) -> None:
    """把 ``src_path`` 逐字节复制到 ``dst_path``，**目标从创建那一刻起就是 0600**。

    SECURITY-REVIEW-provider-protocol-level-r2 重要-1：``shutil.copy2`` 先用默认 umask(022)
    建出 0644 的目标 inode 并往里写明文 key，之后才 ``copystat`` 回源模式；那个窗口里同机
    其他用户即可读到全部 api_key，且若拷贝中途抛错，0644 的半截文件会永久留下。改走
    ``_open_private``（``O_CREAT|O_EXCL|0600``）后"任何字节落盘之前模式已是 0600"，与 umask
    无关；失败时清掉半截目标。
    """
    with open(src_path, "rb") as src:
        fd = _open_private(dst_path)
        try:
            with os.fdopen(fd, "wb") as dst:
                shutil.copyfileobj(src, dst)
                dst.flush()
                os.fsync(dst.fileno())
        except OSError:
            _cleanup(dst_path)
            raise


def _pre_migration_snapshot_path() -> str:
    """一次性迁移快照的固定路径。**故意不带 ``.bak-<纯数字>`` 形**：``_prune_backups`` 只裁
    ``<base>.bak-<纯数字>`` 的备份，故该固定名天然不参与裁剪（而非靠字典序侥幸留在最后）。"""
    return PROVIDERS_PATH + ".pre-protocol-migration.bak"


def _snapshot_pre_migration() -> None:
    """首次触发迁移备份时落一份固定名快照（0600），保留"旧键齐全"的完整回退点。

    SECURITY-REVIEW-provider-protocol-level-r2 建议-2：带时间戳的 ``.bak-<ts>`` 会被
    ``_prune_backups`` 裁到 ``_BACKUPS_KEEP`` 份；第 6 个含旧键的 provider 被 PUT 时，最早
    那份"旧键齐全"的快照已被裁掉。此快照只在此刻（尚无任何迁移发生）生成一次，之后永不
    触碰，故任何时刻都能用它整份回退。已存在则跳过（幂等）。
    """
    path = _pre_migration_snapshot_path()
    if os.path.exists(path):
        return
    _write_private_copy(PROVIDERS_PATH, path)


def _backup_providers_before_migration() -> None:
    """迁移（删旧键 ``protocols``/``unavailable_models``）前对 providers.json 落一份 0600 备份。

    SECURITY-REPORT-provider-protocol-level 重要-1：旧键一旦被 PUT 删掉，磁盘上就没有文件级
    回退点。备份命名 ``providers.json.bak-<ts>``、权限 **创建即 0600**（不是事后 chmod，见
    ``_write_private_copy``）、与 providers.json 同目录，并复用 ``_prune_backups`` 保留最近
    ``_BACKUPS_KEEP`` 份。另在首次迁移前额外落一份固定名一次性快照（``_snapshot_pre_migration``，
    不参与裁剪）。

    **机会性**：与 ``_write_settings`` 同口径，备份失败不得阻断正常写流程，仅留 stderr 线索。
    内容为原文件**逐字节副本**，不截断、不改写任何字段。
    """
    if not os.path.exists(PROVIDERS_PATH):
        return
    try:
        _snapshot_pre_migration()
        backup_path = "%s.bak-%d" % (PROVIDERS_PATH, int(time.time()))
        _write_private_copy(PROVIDERS_PATH, backup_path)
        _prune_backups(PROVIDERS_PATH)
    except OSError as e:
        print("providers.json 备份失败，迁移继续: %s" % e, file=sys.stderr)


# ── provider 清单 ──────────────────────────────────────────────────────────

def _key_fp(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def _mask(value: str) -> str:
    if not value:
        return ""
    return ("****" + value[-4:]) if len(value) >= 4 else "****"


def _public_entry(entry: dict) -> dict:
    """脱敏形状：绝不含 api_key，只有 key_masked / has_key。

    ``default_model`` 是本字段**唯一的对外出口**，而契约要求它「类型恒为 str、从不 null」
    （旧 INTERFACE §1.2）。故这里按**类型**归一化，不用 ``or ""`` —— 后者只兜假值，
    清单被手工改成 ``123``／``["m1"]``／``{"id":"m1"}`` 时会把原值直接透给前端。

    本轮键集（INTERFACE-provider-protocol-level §1，恰 13 键、顺序即此）：**移除**
    ``protocols`` / ``unavailable_models``，**新增** provider 级 ``protocol``（可能是
    读取时派生的值，见 ``entry_protocol``）。``proxy`` 只认 ``is True``（缺省 false）。
    """
    key = entry.get("api_key") or ""
    default_model = entry.get("default_model")
    return {
        "id": entry.get("id"),
        "name": entry.get("name"),
        "base_url": entry.get("base_url"),
        "models": entry.get("models") or [],
        "default_model": default_model if isinstance(default_model, str) else "",
        "key_masked": _mask(key) if key else "",
        "has_key": bool(key),
        "created_at": entry.get("created_at"),
        "updated_at": entry.get("updated_at"),
        "proxy": entry.get("proxy") is True,
        "protocol": entry_protocol(entry),
        "anthropic_base_url": (entry.get("anthropic_base_url")
                               if isinstance(entry.get("anthropic_base_url"), str) else ""),
        "openai_base_url": (entry.get("openai_base_url")
                            if isinstance(entry.get("openai_base_url"), str) else ""),
    }


_PROTOCOLS = ("anthropic", "openai")


def entry_protocol(entry: dict) -> str:
    """provider 级**有效协议**（INTERFACE-provider-protocol-level §1 / §2.5）。

    优先级：条目显式 ``protocol`` 键 ∈ {anthropic, openai} → 原样返回；否则**只读派生**：
    仅当该 provider 的**每个**模型都已在旧 ``protocols`` 中判定为 anthropic/openai
    且取值**全部一致**时返回该唯一值，否则返回 ``""``（未选）。

    **绝不**把 ``unknown``／未判定模型按某个协议一并发出——那会造成静默误路由。
    派生值只读：磁盘上不写该键，直到用户下一次 PUT（见 ``upsert_provider``）才固化。
    """
    raw = entry.get("protocol")
    if raw in _PROTOCOLS:
        return raw
    protocols = entry.get("protocols")
    if not isinstance(protocols, dict):
        return ""
    seen = set()
    for mid in _model_ids(entry):
        value = protocols.get(mid)
        if value not in _PROTOCOLS:
            return ""
        seen.add(value)
    if len(seen) != 1:
        return ""
    return seen.pop()


def _find(data: dict, provider_id: str) -> dict | None:
    return next((p for p in data.get("providers", []) if p.get("id") == provider_id), None)


# upsert_provider 用它区分「body 里没有这个键」与「提供了什么值」——同一件事只留一个判据，
# 免得「是否提供」既写成 `x is None` 又写成 `"x" in body`，任一处改动就静默漂移
# （而 default_model 的漂移方向恰好会撞上 §1.4「"" ⇒ 显式清空」）。
_UNSET: object = object()


def new_provider_id(data: dict) -> str:
    """生成不重复的 provider id（不依赖 data 是否已落盘）。"""
    taken = {p.get("id") for p in (data.get("providers") or [])}
    while True:
        cand = "p_%d_%d" % (int(time.time()), next(_id_seq))
        if cand not in taken:
            return cand


def _valid_base_url(v) -> bool:
    """http(s) 且含主机名、无 userinfo —— **唯一判据**（provider_routes 的拉模型复用同一个）。

    只写 ``^https?://`` 会放过 ``http://`` 这种无主机名地址：provider 能存能绑，把
    ``ANTHROPIC_BASE_URL="http://"`` 写进 bot 的 settings.json，worker 重启后每次调用都失败，
    而面板仍报 applied/in_sync。与 ``urlparse().hostname`` 的接受度必须一致。
    """
    if not isinstance(v, str) or not (0 < len(v) <= 500) or "@" in v:
        return False
    u = urlparse(v)
    return u.scheme in ("http", "https") and bool(u.hostname)


def _valid_name(v) -> bool:
    return isinstance(v, str) and len(v) <= 200


def _norm_default_model(raw) -> str | None:
    """``default_model`` 的**唯一**判据：合法 → strip 后的值；非法 → None。

    合法 = str 且（空串，或 strip 后非空且长度 ≤200）。故 ``""`` 合法（= 显式清空），
    而只含空白的 ``"   "`` **非法**（含糊输入不猜）；长度按 **strip 后**的值判定，
    所以 ``" " + "x"*200`` 合法。上限与 R7 的 ``model``（1..200）同为 200。
    """
    if not isinstance(raw, str):
        return None
    if raw and not raw.strip():
        return None
    value = raw.strip()
    return value if len(value) <= 200 else None


def _norm_models(raw):
    """输入 list[str] 或 list[{"id","label"}] → list[{"id","label"}]；非法 → None。"""
    if not isinstance(raw, list) or len(raw) > 500:
        return None
    out = []
    for m in raw:
        if isinstance(m, str):
            if len(m) > 500:
                return None
            out.append({"id": m, "label": m})
        elif isinstance(m, dict) and isinstance(m.get("id"), str):
            if len(m["id"]) > 500:
                return None
            out.append({"id": m["id"], "label": m.get("label") or m["id"]})
        else:
            return None
    return out


# 准入判定的三条 400 文案（INTERFACE-provider-protocol-level §2；唯一构造处，测试逐字比较）
_ADM_PROTOCOL = "请先为该 provider 选择协议（anthropic 或 openai）"
_ADM_NOMODEL = "该 provider 还没有模型，请先拉取模型"
_ADM_ADDR = "代理上游地址无法自动推导，请填写 anthropic_base_url"
_ADM_NAME_DUP = "客户端名或裸名在多个代理 provider 间重复，请先关闭其中一个"


def _model_ids(entry: dict) -> list:
    """条目的模型 id 列表（对本地可手改的 JSON 容错：元素可能是 str 或 {"id":...}）。"""
    out = []
    for m in entry.get("models") or []:
        mid = m.get("id") if isinstance(m, dict) else (m if isinstance(m, str) else None)
        if isinstance(mid, str) and mid:
            out.append(mid)
    return out


def _registered_names(entry: dict) -> set:
    """该 provider 在实例里会注册的名字集合 N_p（INTERFACE §1.2 重名门）。

    ``N_p = { <pid>/<m> } ∪ { m }``——**两项都取全部模型**（INTERFACE §1.2 / §1.5；
    V0-11 实测：``claude-api-key`` 与 ``openai-compatibility`` 两个块**都**注册裸名、
    与模型属哪个桶无关）。原 R7「anthropic 块只注册前缀名」系误判（当时两块用同一模型名，
    路由到先注册的块，掩盖真实行为）。

    **冲突已解决（2026-09-27，用户授权）**：本轮锁定套件里 `test_客户端名撞客户端名_永不冲突`
    等用例原以「anthropic 桶只注册前缀名」为前提（= R7 旧结论，已被 V0-11 推翻），与本并集判据
    抵触。用户授权按 V0-11 修正那些用例的期望（**保留本并集判据**——裸名确实会撞且实例
    **静默路由到先注册的那块**，见 V0-5）。授权与审计记录见
    `.devflow/evidence-provider-openai-compat/LOCKED-TEST-AMENDMENT.md`。
    """
    pid = entry.get("id")
    names = set()
    for mid in _model_ids(entry):
        if isinstance(pid, str) and pid:
            names.add(pid + "/" + mid)
        names.add(mid)
    return names


def _proxy_admission_error(entry: dict, data: dict) -> str | None:
    """``proxy=true`` 的准入判定（INTERFACE-provider-protocol-level §2）：通过 → ``None``，否则错误文案。

    门顺序**固定**：未选协议 → 无模型 → 地址问题 → 重名。更具体的诊断优先——
    未选协议时谈地址无意义；地址问题由 ``_gate0_error``（网络，须在锁外）判定。
    """
    return _admission_local_error(entry) or _api_admission_remote_error(entry, data)


def _api_admission_remote_error(entry: dict, data: dict) -> str | None:
    """序 2（地址，网络）+ 序 3（重名，本地）。锁内调用，须先过 ``_admission_local_error``。"""
    return _gate0_error(entry) or _name_dup_error(entry, data)


def _admission_local_error(entry: dict) -> str | None:
    """序 0/1：未选协议 / 无模型（本地、无网络、可在锁外预检）。"""
    if entry_protocol(entry) not in _PROTOCOLS:
        return _ADM_PROTOCOL
    if not _model_ids(entry):
        return _ADM_NOMODEL
    return None


def _name_dup_error(entry: dict, data: dict) -> str | None:
    """序 3：重名 —— 与**另一个** proxy=true provider 的名字集合有交集即拒。

    依据 V0-5 实测：同名模型会被实例**静默路由到先注册的那个、不报错**，用户得到
    「配好了、却调到了别的 provider」这种无提示的错误结果。
    """
    mine = _registered_names(entry)
    for other in data.get("providers") or []:
        if other is entry or not isinstance(other, dict) or other.get("proxy") is not True:
            continue
        if mine & _registered_names(other):
            return _ADM_NAME_DUP
    return None


_ERR_PROXY_BASE_URL = "代理开启时不能修改 base_url，请先关闭代理"
_ERR_CONCURRENT = "provider 正在被并发修改，请重试"
_ERR_BOUND = "provider 仍被绑定，请先解绑再修改代理相关设置: %s"
_CONC_KEYS = ("base_url", "api_key", "anthropic_base_url", "openai_base_url", "models", "proxy")
_TRIPLE = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL")


class ProviderConflict(RuntimeError):
    """PUT 的 409（9.6）：并发改动 / 统一绑定守卫。``bound_bots`` 非 None 时路由原样带出。"""
    def __init__(self, msg: str, bound_bots: list | None = None):
        super().__init__(msg)
        self.bound_bots = bound_bots


# ── E-15（审计条目修法二）：地址探针 404 → 可见 warning（探针 404 判据不变）────────
# 探针收到 404 = host 可达但 <送入值>/chat/completions 没有这个端点：按既有设计不当
# 「地址错」拒保存，但要给用户一个**不再静默**的提示——保存/绑定响应带 warning、面板显示。
WARN_OPENAI_UNVERIFIED = ("openai 上游地址未验证（探测 404）：若调用失败，"
                          "请在面板手填 openai_base_url")
_REQ_WARN = threading.local()   # 请求级旁路；线程复用，须在请求边界清（reset_request_warn_state）


def take_request_warning() -> str | None:
    """取出并清空本线程的 warning 文案（保存类响应出口用）。"""
    w = getattr(_REQ_WARN, "value", None)
    _REQ_WARN.value = None
    return w


def reset_request_warn_state() -> None:
    """请求边界清理（线程复用）：丢弃上一请求残留的 warning 与未验证标记。"""
    _REQ_WARN.value = None
    _REQ_WARN.unverified = None


def _take_unverified_mark():
    """本次请求对条目「未验证」标记的动作：True=置位 / False=清除 / None=不动。"""
    mark = getattr(_REQ_WARN, "unverified", None)
    _REQ_WARN.unverified = None
    return mark


def _admission_with_probe(cand: dict) -> str | None:
    """锁外准入（E-12：序 0/1 本地 → 序 2 探针）；探针跑过时把结论记进请求旁路（E-15 修法二）。

    - 探针 404 → warning 文案 + 条目记「未验证」（**不**算地址错、**不**拒保存，判据不变）；
    - 探针跑过且非 404 → 清除旧标记（地址已验证）。
    （显式覆盖地址的清除不在这里：那种情形锁外条件根本不进来，由 upsert 锁内兜底。）
    ``cand`` 无覆盖地址是调用方条件保证的（否则本函数不会被调）。
    """
    err = _admission_local_error(cand)
    if err:
        return err
    from scripts import proxy_upstream
    proxy_upstream.reset_openai_probe_status()
    err = _gate0_error(cand)
    if err is not None:
        return err
    status = proxy_upstream.last_openai_probe_status()
    if status == 404:
        _REQ_WARN.value = WARN_OPENAI_UNVERIFIED
        _REQ_WARN.unverified = True
    elif status is not None:
        _REQ_WARN.unverified = False
    return None


def _gate0_error(entry: dict) -> str | None:
    """门 0（地址探针，有网络）：调用方保证不持清单锁（9.6）。"""
    from scripts import proxy_upstream   # 调用期导入：面板导入期不应加载网络模块
    # 序 0：地址问题 —— 仅当用户**两侧都没**显式声明地址时才探针（显式声明优先，§5.2）。
    if not (entry.get("anthropic_base_url") or entry.get("openai_base_url")):
        from scripts.derive_endpoints import effective_endpoints
        base = entry.get("base_url") if isinstance(entry.get("base_url"), str) else ""
        _, openai_url = effective_endpoints(base, "", "")
        if not proxy_upstream.probe_base_openai(openai_url, entry.get("api_key") or ""):
            return _ADM_ADDR

    return None


def _gates234_error(entry: dict, data: dict) -> str | None:
    """「序 0/1（本地）+ 序 2/3（含网络探针）」的**锁外**语义（E-12：upsert 主路径已不再用它，
    仅保留给外部/测试；持清单锁时一律改用 ``_gates_local_error``）。"""
    return _admission_local_error(entry) or _api_admission_remote_error(entry, data)


def _gates_local_error(entry: dict, data: dict) -> str | None:
    """锁内版准入门（E-12）：**只做本地判定**——序 0/1（未选协议/无模型）+ 序 3（重名），
    绝不发网络探针；地址门（序 2）已由 ``_gate0_outside_lock`` 在锁外判过。
    持清单锁期间发慢探针会拖住整把清单锁（其它 bot 的读写全被卡）。"""
    return _admission_local_error(entry) or _name_dup_error(entry, data)


def _binding_guard(pid: str, prev: dict, entry: dict, data: dict) -> None:
    """统一绑定守卫（9.6，R8/D1=A）：前后任一 proxy=true，且有已绑定 bot 的应写三键将变（含翻转 proxy）
    或其 model 将不在 E 中 → 409。前后都 proxy=false 不触发。

    本轮输入差异（INTERFACE-provider-protocol-level §4）：usable 由「协议已判 + 不在 unavailable」
    简化为「**provider 的 ``protocol`` 已选**」，即已选协议时 usable = 该 provider 的全部模型。
    """
    if prev.get("proxy") is not True and entry.get("proxy") is not True:
        return
    usable = None
    if entry.get("proxy") is True:
        usable = set(_model_ids(entry)) if entry_protocol(entry) in _PROTOCOLS else set()
    hit = []
    for bot in bound_bots(pid, data):
        model = ((data.get("bindings") or {}).get(bot) or {}).get("model")
        a, b = resolve_env(bot, prev, model), resolve_env(bot, entry, model)
        if any(a[k] != b[k] for k in _TRIPLE) or (usable is not None and model not in usable):
            hit.append(bot)
    if hit:
        hit.sort()
        raise ProviderConflict(_ERR_BOUND % ",".join(hit), hit)


def _norm_override_base_url(raw) -> tuple[bool, str | None]:
    """``anthropic_base_url`` / ``openai_base_url`` 的**唯一**判据（INTERFACE §1.2）。

    返回 ``(ok, value)``：ok=True 时 value 是归一化后的值（``""`` = 显式清空）；
    ok=False 表示非法。合法判据与 ``base_url`` **同一个函数**（http(s) + hostname +
    无 userinfo + 长度 ≤500），只是 ``""`` 额外合法——它表示「显式清空、回到自动推导」。
    """
    if not isinstance(raw, str):
        return False, None
    if raw == "":
        return True, ""
    if not _valid_base_url(raw):
        return False, None
    return True, raw


def _gate0_outside_lock(pid, body, proxy_opt, anthropic_opt, openai_opt, protocol_opt, models):
    """锁外预检（9.6 顺序：…→ 开启中改地址 400 → 门 0）。返回错误文案；门 0 跑过时返回探针前现值
    （供提交锁内比对并发改动）；否则 None。404 / 缺 api_key 留给锁内既有判定。

    门顺序（INTERFACE-provider-protocol-level §2）**序 0/1 先于序 2**：未选协议 / 无模型是本地
    判定，必须在地址探针（网络）**之前**给出——故在跑 ``_gate0_error`` 前先 ``_admission_local_error``。
    """
    if pid is None:   # 新建：false→true 且两覆盖地址均空才跑门 0（锁外）；缺 api_key 留给锁内既有 400
        key = body.get("api_key")
        if proxy_opt is not True or anthropic_opt not in (_UNSET, "") or openai_opt not in (_UNSET, "") \
                or not (isinstance(key, str) and key):
            return None
        cand = {"base_url": body.get("base_url"), "api_key": key, "anthropic_base_url": "",
                "openai_base_url": "", "models": models if models is not None else [],
                "protocol": protocol_opt if protocol_opt is not _UNSET else ""}
        return _admission_with_probe(cand)
    try:
        old = _find(load_providers(), pid)
    except ProvidersCorrupt:
        return None
    new_key = body.get("api_key") if isinstance(body.get("api_key"), str) else None
    if old is None or ("base_url" in body and body.get("base_url") != old.get("base_url") and new_key is None):
        return None
    base_changed = "base_url" in body and body.get("base_url") != old.get("base_url")
    cand = dict(old)
    if "base_url" in body:
        cand["base_url"] = body["base_url"]
    if models is not None:
        cand["models"] = models
    if new_key is not None:
        cand["api_key"] = new_key
    cand["protocol"] = _next_protocol(old, protocol_opt, base_changed)
    for k, v in (("proxy", proxy_opt), ("anthropic_base_url", anthropic_opt), ("openai_base_url", openai_opt)):
        if v is not _UNSET:
            cand[k] = v
    before, after = old.get("proxy") is True, cand.get("proxy") is True
    if before and after and cand.get("base_url") != old.get("base_url"):
        return _ERR_PROXY_BASE_URL
    # 何时必须跑门 0（网络探针，只能在此处锁外跑）：开启中、用户两侧都没显式声明地址，
    # 且本次提交**真的会改变**该 provider 的可用性——首开 / 换 key / 改覆盖地址 / 改 models。
    # 触发条件与锁内本地判定的触发条件一致：锁内不再补跑探针（E-12）。
    models_changed = models is not None and cand.get("models") != old.get("models")
    if after and not (cand.get("anthropic_base_url") or cand.get("openai_base_url")) and (
            not before or models_changed
            or any(cand.get(k) != old.get(k) for k in ("api_key", "anthropic_base_url", "openai_base_url"))):
        err = _admission_with_probe(cand)
        return err if err else copy.deepcopy(old)
    return None


def _next_protocol(entry: dict, protocol_opt, base_changed: bool) -> str:
    """写入后的**有效协议**：显式提交优先；否则「地址一换、结论作废」清空；否则保留（含派生值）。"""
    if protocol_opt is not _UNSET:
        return protocol_opt
    if base_changed:
        return ""
    return entry_protocol(entry)


def upsert_provider(body: dict, provider_id: str | None = None) -> tuple[dict | None, str | None]:
    """新建 / 更新 provider。返回 (脱敏条目, None) 或 (None, 错误文案)。"""
    if not isinstance(body, dict):
        return None, "请求体必须是 JSON 对象"

    updating = provider_id is not None

    # name：新建必填；更新时省略/空串 = 保留旧值（R4「全字段可选」）
    new_name = None
    raw_name = body.get("name")
    if raw_name not in (None, ""):
        if not _valid_name(raw_name):
            return None, "name 必须是长度 ≤200 的字符串"
        new_name = raw_name
    elif not updating:
        return None, "name required"

    if not updating and not _valid_base_url(body.get("base_url")):
        return None, "base_url 必须是 http/https 开头的地址"
    if updating and "base_url" in body and not _valid_base_url(body.get("base_url")):
        return None, "base_url 必须是 http/https 开头的地址"

    if "models" in body:
        models = _norm_models(body.get("models"))
        if models is None:
            return None, "models 必须是字符串数组（≤500 项）"
    else:
        models = None

    # default_model（可选）：缺省 = 保留旧值；"" = 显式清空；其余见 _norm_default_model。
    # 校验插在锁**之前**：与 §1.7 的优先级一致（"不存在的 id + 非法 default_model" 必须报
    # default_model 那条，不能先撞上锁内的 provider not found），且保证零写入。
    default_model: str | object = _UNSET
    if "default_model" in body:
        default_model = _norm_default_model(body.get("default_model"))
        if default_model is None:
            return None, "default_model 必须是长度 ≤200 的字符串"

    # proxy（可选，INTERFACE §1.2）：缺省/不传 = 保留旧值（新建落 false）；非 bool
    # （含 null / "true" / 1）→ 400。判据是「键存在且非 bool」，故 ``"proxy": null`` 也拒。
    proxy_opt: bool | object = _UNSET
    if "proxy" in body:
        if not isinstance(body.get("proxy"), bool):
            return None, "proxy 必须是布尔值"
        proxy_opt = body.get("proxy")

    # protocol（可选，INTERFACE-provider-protocol-level §1）：取值必须是 "anthropic" / "openai"，
    # 否则（含 ""、null、数字、其它字符串）→ 400。**新建时键必须存在**（"" 不是合法新建值）。
    # 校验先于锁内 provider not found（§2.2：「不存在 id + 非法 protocol」报 protocol 那条）。
    protocol_opt: str | object = _UNSET
    if "protocol" in body:
        value = body.get("protocol")
        if not isinstance(value, str) or value not in _PROTOCOLS:
            return None, "protocol 必须是 anthropic 或 openai"
        protocol_opt = value
    elif not updating:
        return None, "protocol 必须是 anthropic 或 openai"

    # 两个 base_url 覆盖（可选，INTERFACE §1.2）：缺省/不传 = 保留旧值（新建落 ""）；
    # "" = 显式清空（回到自动推导）；非 str / 非法地址 → 400。两条文案同构、逐字。
    anthropic_opt: str | object = _UNSET
    if "anthropic_base_url" in body:
        ok, value = _norm_override_base_url(body.get("anthropic_base_url"))
        if not ok:
            return None, "anthropic_base_url 必须是 http/https 开头的地址"
        anthropic_opt = value
    openai_opt: str | object = _UNSET
    if "openai_base_url" in body:
        ok, value = _norm_override_base_url(body.get("openai_base_url"))
        if not ok:
            return None, "openai_base_url 必须是 http/https 开头的地址"
        openai_opt = value

    pre_old = _gate0_outside_lock(provider_id, body, proxy_opt, anthropic_opt, openai_opt, protocol_opt, models)
    if isinstance(pre_old, str):
        return None, pre_old
    with _manifest_lock():
        data = load_providers()

        if updating:
            entry = _find(data, provider_id)
            if entry is None:
                return None, "provider not found"
        else:
            entry = None

        # api_key：更新时省略/空串 = 保留旧值；新建时与 has_key_field 同一判据
        # （None/空串 = 未提供）。若放空串过去，会造出一个永远绑不上的 provider
        # （set_bot_provider 要求 api_key 非空）却返回 201。
        has_key_field = "api_key" in body and body.get("api_key") not in (None, "")
        # 凭据与地址必须同源（rev10）：更新时若 base_url 的**值**真的变了，必须在同一次请求里
        # 提交非空 api_key，否则存储中的真 key 会被改道发往新地址（POST /api/providers/models
        # 的 ① 模式把清单里的明文 key 发向清单里的 base_url；改地址不必重填 key = 外泄出口）。
        # 只比“值”不比“字段”：同值回传不带 key 是真实流程，不得 400。判据与 has_key_field 同一套
        # （api_key 省略/None/"" 三者一律视为未提交）。零写入：任何 entry[...] = 之前返回。
        if (updating and "base_url" in body
                and body.get("base_url") != entry.get("base_url")
                and not has_key_field):
            return None, "base_url 变更时必须同时提交 api_key"
        if has_key_field:
            api_key = body.get("api_key")
            if not isinstance(api_key, str) or len(api_key) > 500:
                return None, "api_key 必须是长度 ≤500 的字符串"
        elif updating:
            api_key = entry.get("api_key")
        else:
            return None, "api_key required"

        now = int(time.time())
        if entry is None:
            new_entry = {
                "id": new_provider_id(data),
                "name": new_name,
                "base_url": body["base_url"],
                "api_key": api_key,
                "models": models if models is not None else [],
                "default_model": "" if default_model is _UNSET else default_model,
                "created_at": now,
                "updated_at": now,
                "proxy": False,
                "protocol": protocol_opt,   # 新建必填且已校验为 anthropic/openai
                "anthropic_base_url": "",
                "openai_base_url": "",
            }
            data["providers"].append(new_entry)
            entry = new_entry
        else:
            prev = copy.deepcopy(entry)
            if pre_old is not None and any(prev.get(k) != pre_old.get(k) for k in _CONC_KEYS):
                raise ProviderConflict(_ERR_CONCURRENT)   # 门 0 探针期间被别的请求改动（锁内最新清单）
            old_base_url = entry.get("base_url")
            base_changed = "base_url" in body and body.get("base_url") != old_base_url
            next_protocol = _next_protocol(entry, protocol_opt, base_changed)
            if new_name is not None:
                entry["name"] = new_name
            if "base_url" in body:
                entry["base_url"] = body["base_url"]
            if api_key is not None:
                entry["api_key"] = api_key
            if models is not None:
                entry["models"] = models
            if default_model is not _UNSET:
                entry["default_model"] = default_model
            entry["updated_at"] = now
            # 协议固化 + 旧键删除（INTERFACE-provider-protocol-level §1/§2.5）：显式提交优先；
            # 否则 base_url 真的换了就清空（地址一换、结论作废），否则保留有效协议（含派生值）。
            # 任意一次 PUT 都把旧 protocols / unavailable_models 从条目删除，不留空壳。
            entry["protocol"] = next_protocol
            entry.pop("protocols", None)
            entry.pop("unavailable_models", None)

        # 本轮可写字段（§1.2）。protocol 已在上面按语义写入。
        if proxy_opt is not _UNSET:
            entry["proxy"] = proxy_opt
        if anthropic_opt is not _UNSET:
            entry["anthropic_base_url"] = anthropic_opt
        if openai_opt is not _UNSET:
            entry["openai_base_url"] = openai_opt

        # E-15（修法二）：锁外探针的结论落到条目「未验证」标记（绑定响应据此继续提示）。
        # 内部键，不进 _public_entry 的 14 键；本请求没跑探针（mark=None）时保留旧值。
        _mark = _take_unverified_mark()
        if _mark is True:
            entry["openai_addr_unverified"] = True
        elif _mark is False:
            entry.pop("openai_addr_unverified", None)
        elif entry.get("anthropic_base_url") or entry.get("openai_base_url"):
            # 显式覆盖地址（任一）→ 锁外探针本就不跑（上面条件排除了这种情形），
            # 用户已自己声明地址，旧「未验证」标记不再适用。
            entry.pop("openai_addr_unverified", None)

        # 开启 proxy=true 的准入判定（INTERFACE-provider-protocol-level §2）。门顺序固定为
        # **未选协议 → 无模型 → 地址问题 → 重名**：更具体的诊断优先；地址门已在锁外判过。
        if provider_id is None:
            err = _gates_local_error(entry, data) if entry.get("proxy") is True else None   # 门 0 已在锁外
        else:   # 9.6：开启中改地址 → 统一守卫 → 准入门（仅 false→true 或开启中 models 变化才跑）
            before, after = prev.get("proxy") is True, entry.get("proxy") is True
            if before and after and entry.get("base_url") != prev.get("base_url"):
                return None, _ERR_PROXY_BASE_URL
            _binding_guard(provider_id, prev, entry, data)
            err = (_gates_local_error(entry, data)
                   if after and (not before or entry.get("models") != prev.get("models")) else None)
        if err:
            return None, err

        # 迁移前备份（SECURITY-REPORT-provider-protocol-level 重要-1）：**仅当本次写确实会从
        # 该条目删掉旧键**时才落备份（更新分支必 pop 旧键，见上）。不含旧键的普通 PUT 不产生
        # 备份，避免每次改名都堆备份。备份机会性：失败不阻断本次写（helper 内部自吞并记 stderr）。
        if provider_id is not None and any(k in prev for k in _LEGACY_KEYS):
            _backup_providers_before_migration()
        _commit_locked(data)
        public = _public_entry(entry)
        snapshot = copy.deepcopy(data)     # 快照在**锁内**取（I3）
    _sync_after_write(snapshot)
    return public, None


def delete_provider(provider_id: str) -> tuple[bool, str | None]:
    with _manifest_lock():
        data = load_providers()
        entry = _find(data, provider_id)
        if entry is None:
            return False, "provider not found"
        bound = bound_bots(provider_id, data)
        if bound:
            return False, "provider 仍被绑定: " + ",".join(bound)
        if any(k in entry for k in _LEGACY_KEYS):
            # 建议-3：删掉仍带旧键的条目会让旧的逐模型判定整条消失，同样在落盘前留回退点。
            _backup_providers_before_migration()
        data["providers"] = [p for p in data["providers"] if p.get("id") != provider_id]
        _commit_locked(data)
        snapshot = copy.deepcopy(data)     # 快照在**锁内**取（I3）
    _sync_after_write(snapshot)
    return True, None


def _sync_after_write(snapshot: dict) -> dict | None:
    """写后重建代理配置（R4/R5/R6/R7/R8 的共同副作用）；返回 ``provider_proxy.sync`` 的结果。

    **no-op 由 ``provider_proxy.sync`` 自己保证**（无 proxy provider 时不写盘、不探测、
    不 kickstart、不创建运行目录）——故这里无条件调用是安全的，锁定套件不受影响。
    重建**异常不得整体吞掉**（E-13）：写 stderr 一行并把代理状态 ``last_error`` 置位，
    也不得影响调用者自身的状态码/文案（§2.1 R6 副作用），故不重抛，返回失败体供
    E-17 的绑定路径据实报失败；其它调用方忽略返回值即可。
    """
    if getattr(_LOCK_STATE, "held", False):   # 在 try 之外：锁序违例必须抛出，不能被吞
        raise RuntimeError("锁序违例：持有清单锁时不得进入配置锁")
    try:
        import provider_proxy
        return provider_proxy.sync(snapshot)
    except Exception as e:
        msg = "写后重建代理配置失败: %s" % type(e).__name__
        print("[provider_config] %s" % msg, file=sys.stderr)
        try:
            import provider_proxy
            provider_proxy._LAST_ERROR = msg
        except Exception:
            pass
        return {"ok": False, "error": msg}


def store_models(provider_id: str, models: list) -> None:
    """把拉取到的模型快照写回 provider（读改写**在 _PROVIDERS_LOCK 内一次完成**）。

    原实现（调用方 load_providers → 改 → save_providers）在锁外跨两次加锁读改写，中间
    空档里别人写的 binding 会被手里那份陈旧快照整份覆盖（丢更新）；下沉到这里与其它
    providers.json 读改写共用同一把锁。provider 已不存在 / 文件损坏 → 静默跳过。
    """
    try:
        with _manifest_lock():
            data = load_providers()
            entry = _find(data, provider_id)
            if entry is None:
                return
            entry["models"] = models
            _commit_locked(data)
    except ProvidersCorrupt:
        return


def bound_bots(provider_id: str, data: dict | None = None) -> list[str]:
    if data is None:
        data = _load_providers_safe()
    return sorted(b for b, bd in (data.get("bindings") or {}).items()
                  if isinstance(bd, dict) and bd.get("provider_id") == provider_id)


# ── bot 目录 / local 层 ─────────────────────────────────────────────────────

def _bot_cfg(bot_id: str) -> dict | None:
    """已知 bot 的配置；yml 缺失/非法 → None（归一化，不外抛）。

    归一化的异常集（docstring 承诺"非法→None"，逃逸会让面板 500）：
    yml 不存在/不可读（OSError）、bot_id 非法（ValueError）、yml 语法错误（yaml.YAMLError）、
    yml 根不是映射（标量 → AttributeError；列表 → ``cfg.pop(key, default)`` 走 list.pop
    的签名而抛 TypeError）。后两类原先都会穿出函数。
    """
    try:
        return config_loader.load_bot(bot_id)
    except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError):
        return None


def _within_channels_root(real_dir: str) -> bool:
    """bot 目录（调用方已 ``os.path.realpath``）是否落在 ``CHANNELS_ROOT`` 内。

    settings.json 写口与侧车哨兵写口共用同一判据：``bot_channel_path`` 来自（可被测试
    接缝重定向的）yml，只要它指向 ``CHANNELS_ROOT`` 之外，两处都必须拒写——否则
    yml 一改就能把带明文 key 的文件写到任意目录。
    """
    real_root = os.path.realpath(CHANNELS_ROOT)
    return real_dir == real_root or real_dir.startswith(real_root + os.sep)


def _discovered_bot_ids() -> set:
    """发现的 bot 名单（yml ∪ 目录）id 集合；面板写口的合法集合。"""
    return {d["bot_id"] for d in config_loader.list_discovered_bots()}


def _bot_dir(bot_id: str) -> str | None:
    """bot 的 channels 工作目录（写盘基准），两分支：

    - yml 存在且有 ``bot_channel_path`` 字段 → 合法绝对路径返回之；字段非法
      （非 str/空/非绝对）→ None，保持既有报错——**即使同名目录存在**，配置损坏
      不被"同名目录"静默掩盖（I-6）。
    - 仅"无 yml 或 yml 无该字段"时兜底：bot ∈ 发现集合且 ``<CHANNELS_ROOT>/<id>``
      是目录 → 返回之；否则 None。yml 存在却读不出来（损坏）同样不兜底。
    """
    cfg = _bot_cfg(bot_id)
    if cfg is None:
        yml_path = os.path.join(config_loader.config_dir(), "%s.yml" % bot_id)
        if os.path.exists(yml_path):
            return None
    elif "bot_channel_path" in cfg:
        p = cfg.get("bot_channel_path")
        if not isinstance(p, str) or not p or not os.path.isabs(p):
            return None
        return p
    if bot_id not in _discovered_bot_ids():
        return None
    candidate = os.path.join(config_loader.channels_dir(), bot_id)
    return candidate if os.path.isdir(candidate) else None


def bot_settings_path(bot_id: str) -> str | None:
    d = _bot_dir(bot_id)
    return os.path.join(d, ".claude", "settings.json") if d else None


def bot_local_path(bot_id: str) -> str | None:
    d = _bot_dir(bot_id)
    return os.path.join(d, ".claude", "settings.local.json") if d else None


def _read_settings_doc(path: str | None) -> dict | None:
    """读一份 settings.json 的顶层对象；缺失/不可读/非 JSON 对象 → None（只读，绝不写）。"""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _env_of(doc: dict | None) -> dict:
    env = doc.get("env") if isinstance(doc, dict) else None
    return env if isinstance(env, dict) else {}


def _read_env(path: str | None) -> dict:
    """读一份 settings 的 env；文件缺失/坏 JSON → {}（读侧容错）。"""
    return _env_of(_read_settings_doc(path))


def local_provider_keys(bot_id: str) -> list[str]:
    """<bot_dir>/.claude/settings.local.json 里出现的 provider 键名（按 ALLOWED 顺序）。"""
    env = _read_env(bot_local_path(bot_id))
    return [k for k in PROVIDER_KEYS if k in env]


# ── 唯一落盘点 ─────────────────────────────────────────────────────────────

def _proxy_meta_readonly() -> dict:
    """只读地取顶层 ``proxy`` 段（GET /api/proxy 用；**绝不生成 key**，读侧无副作用）。"""
    try:
        data = load_providers()
    except ProvidersCorrupt:
        return {}
    meta = data.get("proxy")
    return meta if isinstance(meta, dict) else {}


def resolve_env(bot_id: str, provider: dict, model: str) -> dict:
    """把「某 bot 应该拿到的 7 个 env 键」算成纯数据（微信接入位：直接把返回值当子进程 env）。

    **键集恒为 7 键**（INTERFACE §0）。``proxy=true`` 时分叉（§3）：base_url 指向本机代理
    ``http://127.0.0.1:<port>``（<port> = 当前配置端口）、token 换成**实例 key**、
    model 换成**客户端名** ``<pid>/<model>``（**字面拼接**，模型名自带 ``/`` 时不折叠，N2）。
    ``proxy=false``（含全部既有 provider）走原分支，**字节级等价**（R2）。
    """
    if provider.get("proxy") is True:
        import provider_proxy
        client_model = "%s/%s" % (provider.get("id"), model)
        return {
            "ANTHROPIC_BASE_URL": provider_proxy.base_url(),
            "ANTHROPIC_AUTH_TOKEN": _proxy_meta_readonly().get("instance_key") or "",  # 只读，不生成 key
            "ANTHROPIC_MODEL": client_model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": client_model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": client_model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": client_model,
            "ANTHROPIC_DEFAULT_FABLE_MODEL": client_model,
        }
    return {
        "ANTHROPIC_BASE_URL": provider["base_url"],
        "ANTHROPIC_AUTH_TOKEN": provider["api_key"],
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
        "ANTHROPIC_DEFAULT_FABLE_MODEL": model,
    }


def _prune_backups(settings_path: str) -> None:
    """裁掉 ``<base>.bak-<纯数字>`` 备份，只留最近 ``_BACKUPS_KEEP`` 份。

    只认**纯数字后缀**：一次性迁移快照 ``<base>.pre-protocol-migration.bak`` 与 desync 安装器
    的 ``.bak-desync-<ts>`` 因此都不参与裁剪（前者是必须永存的全量回退点，后者归各自机制管）。
    """
    d = os.path.dirname(settings_path)
    base = os.path.basename(settings_path) + ".bak-"
    try:
        baks = sorted(n for n in os.listdir(d)
                      if n.startswith(base) and n[len(base):].isdigit())
    except OSError:
        return
    for name in baks[:-_BACKUPS_KEEP] if len(baks) > _BACKUPS_KEEP else []:
        try:
            os.remove(os.path.join(d, name))
        except OSError:
            pass


def _write_settings(bot_id: str, mutate) -> tuple[dict | None, str | None]:
    """settings.json 写入公共骨架：路径校验 → local 冲突 → 备份 → 写 → 校验 → 回滚。

    mutate(existing_doc_env) 就地改 env，返回被写入的 7 键 dict（None 表示只需删除）
    """
    _require_writable()
    d = _bot_dir(bot_id)
    if d is None:
        # 既有两条错误文案保持：彻底未知的 bot / yml 存在但路径不可用
        if _bot_cfg(bot_id) is None:
            return None, "unknown bot %s" % bot_id
        return None, "bot 配置缺少可用的 bot_channel_path"

    real_dir = os.path.realpath(d)
    if not _within_channels_root(real_dir):
        return None, "bot 目录越界，拒绝写入"

    settings_path = os.path.join(real_dir, ".claude", "settings.json")
    local_path = os.path.join(real_dir, ".claude", "settings.local.json")
    conflict = [k for k in PROVIDER_KEYS if k in _read_env(local_path)]
    if conflict:
        # 类型化抛出：路由按类型映射 409，不匹配文案（fn 层由 set/clear 转回字符串）
        raise LocalConflict(bot_id, conflict)

    try:
        with open(settings_path, encoding="utf-8") as f:
            existing = json.load(f)
        if not isinstance(existing, dict):
            raise ValueError
    except FileNotFoundError:
        existing = {}
    except ValueError:
        return None, "bot settings.json 解析失败，已中止"

    snapshot = {"enabledPlugins": existing.get("enabledPlugins"),
                "hooks": existing.get("hooks")}
    original_env = dict(existing.get("env") or {})

    backup_path = None
    if os.path.exists(settings_path):
        backup_path = "%s.bak-%d" % (settings_path, int(time.time()))
        try:
            _write_private_copy(settings_path, backup_path)   # 创建即 0600，不留 0644 明文 key 窗口
            _prune_backups(settings_path)
        except OSError:
            return None, "写入校验失败，已回滚"

    out = copy.deepcopy(existing)
    env = dict(existing.get("env") or {})
    written = mutate(env)
    if env:
        out["env"] = env
    else:
        out.pop("env", None)

    tmp = settings_path + ".tmp-%d" % os.getpid()
    try:
        os.makedirs(os.path.dirname(settings_path), exist_ok=True)
        fd = _open_private(tmp)   # 先建成 0600 再写：明文 key 不得先落在 0644 inode 上
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, settings_path)
    except OSError:
        _cleanup(tmp)
        return None, "写入校验失败，已回滚"

    if not _chmod_600(settings_path):
        _restore(settings_path, backup_path, existing)
        return None, "写入校验失败，已回滚"

    if not _verify(settings_path, written, snapshot):
        _restore(settings_path, backup_path, existing)
        return None, "写入校验失败，已回滚"

    env_removed = sorted(k for k in original_env if k not in env)
    _LAST_SNAPSHOT[bot_id] = snapshot
    return {
        "bot_id": bot_id,
        "bot_dir": real_dir,
        "settings_path": settings_path,
        "env_written": {k: _mask(v) for k, v in (written or {}).items()},
        "backup_path": backup_path,
        "env_removed": env_removed,
    }, None


def _open_private(path: str) -> int:
    """以 0600 独占创建临时文件并返回 fd（两份落盘骨架共用）。

    **必须在写入任何字节之前就把模式钉成 0600**：默认 umask 下 ``open()`` 会先建成
    0644，明文 api_key／settings 内容会在 ``chmod`` 之前就落在一个组/其他用户可读的
    inode 上——那道窗口每次保存都开着。``O_EXCL`` 顺带挡住残留 tmp（同 pid 的崩溃现场）：
    先清掉旧文件再重建，保证新 inode 的权限来自本参数而非继承。
    """
    for _ in range(2):
        try:
            return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            _cleanup(path)
    raise OSError("无法创建私有临时文件: %s" % path)


def _cleanup(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _chmod_600(path: str) -> bool:
    for _ in range(2):  # chmod 失败重试一次
        try:
            os.chmod(path, 0o600)
            return True
        except OSError:
            continue
    return False


def _verify(settings_path: str, written: dict, snapshot: dict) -> bool:
    try:
        with open(settings_path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return False
    env = doc.get("env") or {}
    for k, v in (written or {}).items():
        if env.get(k) != v:
            return False
    return (doc.get("enabledPlugins") == snapshot["enabledPlugins"]
            and doc.get("hooks") == snapshot["hooks"])


def _restore(settings_path: str, backup_path: str | None, fallback: dict) -> None:
    if backup_path and os.path.exists(backup_path):
        try:
            os.replace(backup_path, settings_path)
            return
        except OSError:
            pass
    # 兜底内容 = 写入前的文档，**可能含 ANTHROPIC_AUTH_TOKEN**（bot 早已绑过 provider 时）。
    # 与主写入路径同权：先建成 0600 的 tmp 再原子替换，绝不让明文 key 以默认 umask(0644)
    # 落在一个新建 inode 上（与 I1 同类）。裸 open(...,"w") 在文件不存在时会以 0644 建出含
    # key 的文件；改走 _open_private 后无论目标在否都只可能是 0600。
    tmp = settings_path + ".restore-%d" % os.getpid()
    try:
        fd = _open_private(tmp)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(fallback, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, settings_path)
    except OSError:
        _cleanup(tmp)


def _undo_settings_write(result: dict | None) -> None:
    """绑定登记失败时把 settings.json 退回写入前状态（E-11 第三条路径）。

    ``_write_settings`` 成功时返回的 result 里带本次写入前的备份路径：
    有备份 → 用 ``os.replace`` 还原；无备份（写入前文件本不存在）→ 删掉本次新建的文件。
    两条路都失败只留 stderr 线索——**绝不**在还原失败时再假装成功。
    """
    path = (result or {}).get("settings_path")
    if not path:
        return
    backup = (result or {}).get("backup_path")
    if backup:
        if not os.path.exists(backup):
            print("[provider_config] 绑定登记失败但 settings 备份缺失，未回滚: %s" % path, file=sys.stderr)
            return
        try:
            os.replace(backup, path)
        except OSError as e:
            print("[provider_config] 绑定登记失败后 settings 回滚失败: %s" % e, file=sys.stderr)
        return
    # 写入前文件不存在（无备份）：本次写入是新建 → 删除即回到原状。
    try:
        os.remove(path)
    except OSError as e:
        print("[provider_config] 绑定登记失败后 settings 删除失败: %s" % e, file=sys.stderr)


def set_bot_provider(bot_id: str, provider: dict, model: str) -> tuple[dict | None, str | None]:
    """把 provider 的 7 键写进该 bot 的项目级 settings.json，并登记 binding。"""
    _require_writable()
    if bot_id not in _discovered_bot_ids():
        return None, "unknown bot %s" % bot_id
    if not isinstance(model, str) or not (1 <= len(model) <= 200):
        return None, "model 必须是长度 1..200 的字符串"
    if (not isinstance(provider, dict) or not isinstance(provider.get("base_url"), str)
            or not provider.get("base_url")
            or not isinstance(provider.get("api_key"), str) or not provider.get("api_key")):
        return None, "provider 数据不完整"

    resolved = resolve_env(bot_id, provider, model)
    if provider.get("proxy") is True:
        # 汇口第 0 步：在清单锁内确保 key 并取回**最终落盘值**（key 落盘后不可变，I2），
        # 再用它写 env——并发首绑时各 bot 的 token 都等于清单里那一个 key（勘误 5）。
        resolved["ANTHROPIC_AUTH_TOKEN"] = ensure_instance_key()
    with bot_lock(bot_id):
        try:
            result, err = _write_settings(bot_id, lambda env: (env.update(resolved) or resolved))
        except LocalConflict as e:
            return None, str(e)   # 契约：函数层仍返回文案（HTTP 层按类型映射 409）
        if err:
            return None, err
        try:
            _set_binding(bot_id, provider, model, pending=True)
        except Exception:
            # 登记失败 → settings.json 恢复成备份（E-11）：否则用户看到“保存失败”，
            # 但那只 bot 的 settings.json 已被写入新 env，重启后半生效。
            _undo_settings_write(result)
            raise
        return result, None


def clear_bot_provider(bot_id: str) -> tuple[bool, str | None]:
    """从 env 删 7 键（其余键不动）；删后 env 为空 → 整个删掉 env 键（J7）。"""
    _require_writable()
    if bot_id not in _discovered_bot_ids():
        return False, "unknown bot %s" % bot_id

    def _mutate(env):
        for k in ALLOWED_ENV_KEYS:
            env.pop(k, None)
        return None

    with bot_lock(bot_id):
        try:
            result, err = _write_settings(bot_id, _mutate)
        except LocalConflict as e:
            return False, str(e)   # 契约：函数层仍返回文案（HTTP 层按类型映射 409）
        if err:
            return False, err
        _clear_binding(bot_id)
        return True, None


def _set_binding(bot_id: str, provider: dict, model: str, pending: bool) -> None:
    with _manifest_lock():
        data = load_providers()
        old = (data.get("bindings") or {}).get(bot_id) or {}
        changed = (old.get("base_url") != provider.get("base_url")
                   or old.get("model") != model
                   or old.get("key_fp") != _key_fp(provider.get("api_key", "")) )
        data.setdefault("bindings", {})[bot_id] = {
            "provider_id": provider.get("id"),
            "model": model,
            "base_url": provider.get("base_url"),
            "key_fp": _key_fp(provider.get("api_key", "")),
            "applied_at": old.get("applied_at"),
            "pending_restart": bool(pending),
            "pending_since": int(time.time()) if pending else None,
            "pending_desired": {"provider_id": provider.get("id"), "model": model},
            "forced_at": None,
            "last_error": old.get("last_error"),
        }
        _commit_locked(data)
    if changed:
        mark_thinking_stale(bot_id)


def _clear_binding(bot_id: str) -> None:
    with _manifest_lock():
        data = load_providers()
        bd = (data.get("bindings") or {}).get(bot_id)
        if bd:
            mark_thinking_stale(bot_id)
        data.setdefault("bindings", {}).pop(bot_id, None)
        _commit_locked(data)


# ── 读取 / 比对 ────────────────────────────────────────────────────────────

def effective_provider(bot_id: str) -> dict:
    """按 local → project → global 解析生效层（不抛异常）。"""
    data = _load_providers_safe()
    binding = (data.get("bindings") or {}).get(bot_id) or None
    local_env = _read_env(bot_local_path(bot_id))
    proj_env = _read_env(bot_settings_path(bot_id))
    glob_doc = _read_settings_doc(GLOBAL_SETTINGS_PATH)
    glob_env = _env_of(glob_doc)

    source = "unknown"
    chosen = {}
    for name, env in (("local", local_env), ("bot", proj_env), ("global", glob_env)):
        if env.get("ANTHROPIC_BASE_URL"):
            source, chosen = name, env
            break
    # rev6 P11：三层都没有 ANTHROPIC_BASE_URL 且全局 settings.json 可读 ⇒ 官方订阅（钥匙串登录）。
    # 模型只取全局的 ANTHROPIC_MODEL；全局文件缺失/坏 JSON 仍走原有 unknown/unmanaged，不新增分支。
    # 三层任一有地址的判定一字不变（chosen 为空 ⇒ base_url / key_masked 自然为 None）。
    subscription = source == "unknown" and glob_doc is not None
    if subscription:
        source = "subscription"

    extra = sorted({k for k in list(local_env) + list(proj_env)
                    if k.startswith("ANTHROPIC_") and k not in ALLOWED_ENV_KEYS})
    conflict = local_provider_keys(bot_id)

    token = chosen.get("ANTHROPIC_AUTH_TOKEN") or ""
    # in_sync 与 _set_binding 的「什么算变化」同口径：同一组 (base_url, model, key_fp) 三元组。
    # 只比 key 指纹时，PUT 改过 provider 的 base_url/key 也不标 drifted —— 面板仍报
    # applied/in_sync，而 bot 的 settings.json 与运行中的 worker 还是旧地址（假成功）。
    # provider 被删（entry 为 None）同样判不同步 → state 落 drifted。
    entry = _find(data, binding.get("provider_id")) if binding else None
    # 9.9：应写三键与写 env 同一来源（resolve_env，含 proxy=true 分叉）；只算不写盘。
    # proxy=true：端口 ≤0 恒 false；实例 key 缺失（want token 为空）为 false。
    in_sync = False
    if binding and entry and token:
        import provider_proxy
        want = resolve_env(bot_id, entry, binding.get("model"))
        in_sync = bool(
            want["ANTHROPIC_AUTH_TOKEN"]
            and _key_fp(token) == _key_fp(want["ANTHROPIC_AUTH_TOKEN"])
            and chosen.get("ANTHROPIC_BASE_URL") == want["ANTHROPIC_BASE_URL"]
            and chosen.get("ANTHROPIC_MODEL") == want["ANTHROPIC_MODEL"]
            and not (entry.get("proxy") is True and provider_proxy.proxy_port() <= 0))

    try:
        import bot_stop
        stopped = bot_stop.is_stopped(bot_id)
    except Exception:
        stopped = False
    alive = worker_alive(bot_id)
    pending = bool(binding and binding.get("pending_restart"))

    if stopped:
        state = "stopped"
    elif pending and binding.get("halted") is True:
        state = "restart_halted"  # 自动重启已停止（试满 5 次或根本无法重启），原因看 last_error
    elif pending:
        state = "pending"
    elif conflict:
        state = "local_override"
    elif binding and not in_sync:
        state = "drifted"
    elif binding and not alive:
        # E-25.2：worker 会话不在 ≠ 故障 —— 绑定在、配置一致（in_sync）、只是没有活会话，
        # 是"空闲"健康态（面板按中性提示渲染）。真拉起失败的走 pending / restart_halted。
        state = "worker_idle"
    elif not binding:
        state = "subscription" if subscription else "unmanaged"
    else:
        state = "applied"

    return {
        "bot_id": bot_id,
        "desired": {
            "source": source,
            "base_url": chosen.get("ANTHROPIC_BASE_URL"),
            "model": ((glob_env.get("ANTHROPIC_MODEL") or None) if subscription
                      else chosen.get("ANTHROPIC_MODEL")),
            "key_masked": _mask(chosen.get("ANTHROPIC_AUTH_TOKEN") or "") or None,
            "extra_keys": extra,
        },
        "local_conflict": conflict,
        "pending_restart": pending,
        "pending_since": binding.get("pending_since") if binding else None,
        "applied_at": binding.get("applied_at") if binding else None,
        "in_sync": in_sync,
        "worker_alive": alive,
        "state": state,
    }


def binding_status(bot_id: str) -> dict:
    data = _load_providers_safe()
    binding = (data.get("bindings") or {}).get(bot_id) or None
    known = bot_id in _discovered_bot_ids()
    if not known and not binding:
        return {"bot_id": bot_id, "bound": False, "binding": None,
                "provider_exists": False, "effective": None}
    if not binding:
        return {"bot_id": bot_id, "bound": False, "binding": None,
                "provider_exists": False, "effective": effective_provider(bot_id)}
    entry = _find(data, binding.get("provider_id"))
    return {
        "bot_id": bot_id,
        "bound": True,
        "binding": {
            "provider_id": binding.get("provider_id"),
            "provider_name": (entry or {}).get("name"),
            "model": binding.get("model"),
            "base_url": binding.get("base_url"),
            "key_fp": binding.get("key_fp"),
            "applied_at": binding.get("applied_at"),
            "pending_restart": bool(binding.get("pending_restart")),
            "pending_since": binding.get("pending_since"),
            "forced_at": binding.get("forced_at"),
            **_backoff_view(binding),
        },
        "provider_exists": entry is not None,
        "effective": effective_provider(bot_id),
    }


def list_bots_status() -> list[dict]:
    out = []
    for item in config_loader.list_discovered_bots():
        bot_id = item["bot_id"]
        try:
            import bot_stop
            stopped = bot_stop.is_stopped(bot_id)
        except Exception:
            stopped = False
        out.append({
            "bot_id": bot_id,
            "display_name": item["display_name"],
            "has_config": item["has_config"],
            "bot_dir": item["bot_dir"],
            "worker_session": "tg-%s-worker" % bot_id,
            "worker_alive": worker_alive(bot_id),
            "stopped": stopped,
            "effective": effective_provider(bot_id),
        })
    return out


def worker_alive(bot_id: str) -> bool:
    """tmux 里是否存在 unified worker 会话（只读探测，不 spawn）。"""
    import subprocess
    try:
        r = subprocess.run(
            ["tmux", "has-session", "-t", "tg-%s-worker" % bot_id],
            env={**os.environ, "TMUX_TMPDIR": "/tmp"},
            capture_output=True, timeout=5)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# ── 待重启标记（Z1，持久在 providers.json）─────────────────────────────────

FORCE_AFTER_SEC = 1800
# 拉起失败的退避：60 秒起翻倍、封顶 480 秒（5 次上限下能达到的最大延迟），满 5 次停止自动重试
RESTART_BACKOFF_BASE_SEC = 60
RESTART_BACKOFF_CAP_SEC = 480
RESTART_MAX_FAILURES = 5
_LAST_ERROR_MAX = 200  # last_error 会进清单、经 API 返回并显示在横幅上
_HALT_DEFAULT_REASON = "自动重启已停止"


def _backoff_view(bd: dict) -> dict:
    """读入口对退避五键的统一归一（脏值一律取缺省，不抛）。"""
    fc = bd.get("fail_count")
    nra = bd.get("queued_since"), bd.get("next_retry_at")
    le = bd.get("last_error")
    return {
        "fail_count": fc if isinstance(fc, int) and not isinstance(fc, bool) and fc >= 0 else 0,
        "next_retry_at": nra[1] if isinstance(nra[1], (int, float)) and not isinstance(nra[1], bool) else None,
        "halted": bd.get("halted") is True,
        "last_error": le if isinstance(le, str) else None,
        "queued_since": nra[0] if isinstance(nra[0], (int, float)) and not isinstance(nra[0], bool) else None,
    }


def _reset_backoff(bd: dict, *, fail_count: bool = True) -> None:
    if fail_count:
        bd["fail_count"] = 0
    bd["halted"] = False
    bd["next_retry_at"] = None
    bd["queued_since"] = None


def _pending_binding(data: dict, bot_id) -> dict | None:
    """待重启记账入口共用：bot_id 合法、清单里有 binding 且 pending_restart 为真才返回它。"""
    if not isinstance(bot_id, str) or not bot_id:
        return None
    bd = (data.get("bindings") or {}).get(bot_id)
    if not isinstance(bd, dict) or not bd.get("pending_restart"):
        return None
    return bd


def _check_now_arg(now):
    """now 非 None 时的合法性同 retry_backoff（TypeError / ValueError），None → 当前时间。"""
    if now is None:
        return time.time()
    retry_backoff.is_due(None, now)  # 只借它校验 now
    return now


def mark_pending(bot_id: str, desired: dict) -> None:
    _require_writable()
    with _manifest_lock():
        data = load_providers()
        bd = data.setdefault("bindings", {}).setdefault(bot_id, {})
        bd["pending_restart"] = True
        bd["pending_since"] = int(time.time())  # 已在排队的项也刷新（现有行为，写死）
        bd["pending_desired"] = desired
        _reset_backoff(bd)
        _commit_locked(data, ensure_key=False)


def clear_pending(bot_id: str) -> None:
    # 六个记账入口统一顺序：先校验 bot_id（非法 → 无操作、不抛），再过只读闸，再进锁
    if not isinstance(bot_id, str) or not bot_id:
        return
    _require_writable()
    with _manifest_lock():
        data = load_providers()
        bd = (data.get("bindings") or {}).get(bot_id)
        if not isinstance(bd, dict):
            return
        bd["pending_restart"] = False
        bd["pending_since"] = None
        _reset_backoff(bd, fail_count=False)
        _commit_locked(data, ensure_key=False)


def list_pending() -> list[dict]:
    data = _load_providers_safe()
    out = []
    for bot_id, bd in (data.get("bindings") or {}).items():
        if not isinstance(bd, dict) or not bd.get("pending_restart"):
            continue
        desired = bd.get("pending_desired") or {
            "provider_id": bd.get("provider_id"), "model": bd.get("model")}
        out.append({
            "bot_id": bot_id,
            "pending_since": bd.get("pending_since"),
            "desired": desired,
            "force_after_sec": FORCE_AFTER_SEC,
            "forced_at": bd.get("forced_at"),
            **_backoff_view(bd),
        })
    out.sort(key=lambda e: (e["pending_since"] or 0))
    return out


def note_forced(bot_id: str) -> None:
    _require_writable()
    with _manifest_lock():
        data = load_providers()
        bd = (data.get("bindings") or {}).get(bot_id)
        if bd is None:
            return
        bd["forced_at"] = int(time.time())
        _commit_locked(data, ensure_key=False)


def set_applied(bot_id: str) -> None:
    """重启成功后：清 pending + 记 applied_at。"""
    if not isinstance(bot_id, str) or not bot_id:
        return
    _require_writable()
    with _manifest_lock():
        data = load_providers()
        bd = (data.get("bindings") or {}).get(bot_id)
        if not isinstance(bd, dict):
            return
        bd["pending_restart"] = False
        bd["pending_since"] = None
        bd["forced_at"] = None
        bd["last_error"] = None
        _reset_backoff(bd)
        bd["applied_at"] = int(time.time())
        _commit_locked(data, ensure_key=False)


def note_error(bot_id: str, text: str) -> None:
    with _manifest_lock():
        data = load_providers()
        bd = (data.get("bindings") or {}).get(bot_id)
        if bd is None:
            return
        bd["last_error"] = text
        _commit_locked(data, ensure_key=False)


def record_failure(bot_id: str, text: str, *, now: float | None = None) -> None:
    """拉起失败：计数 + 退避（60 秒起翻倍、封顶 480 秒），满 5 次停止自动重试；≥3 次留红字文案。"""
    now = _check_now_arg(now)
    text = text if isinstance(text, str) else str(text)
    if not isinstance(bot_id, str) or not bot_id:
        return
    _require_writable()
    with _manifest_lock():
        data = load_providers()
        bd = _pending_binding(data, bot_id)
        if bd is None:
            return
        piece = retry_backoff.on_failure(_backoff_view(bd), int(now),
                                         base=RESTART_BACKOFF_BASE_SEC, cap=RESTART_BACKOFF_CAP_SEC,
                                         max_failures=RESTART_MAX_FAILURES)
        n = piece["fail_count"]
        bd["fail_count"] = n
        bd["next_retry_at"] = None if piece["next_retry_at"] is None else int(piece["next_retry_at"])
        bd["halted"] = piece["halted"]
        bd["queued_since"] = None
        if n >= 3:
            bd["last_error"] = ("连续 %d 次拉起失败：%s" % (n, text))[:_LAST_ERROR_MAX]
        _commit_locked(data, ensure_key=False)


def halt_restart(bot_id: str, reason: str) -> None:
    """无法重启：停止自动重试，标记保留等人工。reason 非 str 或空白 → 固定文案（不做 str()）。"""
    if not isinstance(reason, str) or not reason.strip():
        reason = _HALT_DEFAULT_REASON
    if not isinstance(bot_id, str) or not bot_id:
        return
    _require_writable()
    with _manifest_lock():
        data = load_providers()
        bd = _pending_binding(data, bot_id)
        if bd is None:
            return
        bd["halted"] = True
        bd["next_retry_at"] = None
        bd["queued_since"] = None
        bd["last_error"] = reason[:_LAST_ERROR_MAX]
        _commit_locked(data, ensure_key=False)


def note_queued(bot_id: str, *, now: float | None = None) -> None:
    """首次因忙排队：记 queued_since（强杀从这一刻起算）；已有值不动。"""
    now = _check_now_arg(now)
    if not isinstance(bot_id, str) or not bot_id:
        return
    _require_writable()
    with _manifest_lock():
        data = load_providers()
        bd = _pending_binding(data, bot_id)
        if bd is None or bd.get("queued_since") is not None:
            return
        bd["queued_since"] = int(now)
        _commit_locked(data, ensure_key=False)


def resume_restart(bot_id: str) -> None:
    """解除停止（手动重启前调用）：清零计数与退避；last_error、pending_* 不动。"""
    if not isinstance(bot_id, str) or not bot_id:
        return
    _require_writable()
    with _manifest_lock():
        data = load_providers()
        bd = _pending_binding(data, bot_id)
        if bd is None:
            return
        _reset_backoff(bd)
        _commit_locked(data, ensure_key=False)


def rollback_binding(bot_id: str, previous: dict | None) -> None:
    """重建失败的保存：把 binding 还原成 previous（None = 删掉）。"""
    with _manifest_lock():
        data = load_providers()
        if previous is None:
            data.setdefault("bindings", {}).pop(bot_id, None)
        else:
            data.setdefault("bindings", {})[bot_id] = previous
        _commit_locked(data, ensure_key=False)


def get_binding_raw(bot_id: str) -> dict | None:
    data = _load_providers_safe()
    bd = (data.get("bindings") or {}).get(bot_id)
    return copy.deepcopy(bd) if isinstance(bd, dict) else None


# ── thinking 剥离哨兵 ──────────────────────────────────────────────────────

def unified_jsonl_path(bot_id: str) -> str | None:
    d = _bot_dir(bot_id)
    if not d:
        return None
    import chat_history
    uuid = chat_history.unified_session_uuid(bot_id)
    if not uuid:
        return None
    return os.path.join(chat_history._project_dir(d), uuid + ".jsonl")


def mark_thinking_stale(bot_id: str) -> bool:
    """写侧车哨兵 forced-by-panel，触发 spawn-worker.sh 一次性全量剥离 thinking。

    jsonl 不存在 → False（走 --session-id 全新会话，本就不需要剥离）。
    """
    _require_writable()
    d = _bot_dir(bot_id)
    # 与 settings.json 写口同款越界校验：侧车写面同样是"每只 bot 只写自己那份"的边界，
    # 不能因为 bot_channel_path 被指向别处就跟着写到 CHANNELS_ROOT 之外。
    if not d or not _within_channels_root(os.path.realpath(d)):
        return False
    path = unified_jsonl_path(bot_id)
    if not path or not os.path.exists(path):
        return False
    try:
        with open(path + ".providerfp", "w", encoding="utf-8") as f:
            f.write("forced-by-panel")
        return True
    except OSError:
        return False
