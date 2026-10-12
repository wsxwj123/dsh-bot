# -*- coding: utf-8 -*-
"""加 bot 三段流程（INTERFACE-hub2 §4 / PLAN D3）。

A 只读校验（失败零副作用）→ B 落盘（可回滚）→ C 注册常驻 + 拉起（失败不回滚 B，回 207）。

改本文件前先读这三条红线：

1. **token 的唯一落点是 `<DSH_BOT_HOME>/bots/<bot>/channel/.env`**（F5）。
   `<dsh 配置根>/<bot>.yml` 不含它；响应、stdout/stderr、异常文本一律不含它 ——
   所以对外只抛 ``type(e).__name__``，绝不抛 ``str(e)``：requests 的异常消息里
   带完整请求 URL，而 URL 里就有 token。
2. **回滚按精确清单 unlink**（R8-3），绝不 ``rm -rf`` 任何已存在目录 ——
   用户的 bot 家可能早就在那儿，一次手滑就是别人的聊天记录没了。
3. **重启成功判定看 §5 判活，不看退出码**（F4）。autostart.ts 返回 0 只说明
   launchd 受理了，进程可能秒退；等不到心跳/pid 对不上就不算活（实现见
   `moments/bot_launchd.py`，本模块只等它判）。
"""
import contextlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from urllib.parse import urlsplit

import requests
import yaml
from flask import current_app

from moments import bots_client, config_sources, hub_auth, redact
from moments.provider_model import HubError

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_API_BASE = "https://api.telegram.org"
#: §4.1 R2-1：主机 + 可选端口，**不带路径、不带 query**。
BASE_RE = re.compile(r"^https?://[A-Za-z0-9.\-]+(:\d+)?/?$")
#: 小写起头；禁 `_` 开头（`list_enabled_bots()` 会跳过它，建了也看不见）
BOT_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
TOKEN_RE = re.compile(r"^\d{8,10}:[A-Za-z0-9_-]{30,}$")
#: A7：既有 bot 的 telegram id = `.env` 里 token 的 `:` 前数字段，不新增字段、不落第二份 token
ENV_TOKEN_RE = re.compile(r"^\s*TELEGRAM_BOT_TOKEN\s*=\s*[\"']?(\d{8,10}):", re.M)
#: 上游 description 里的请求 URL 片段 `/bot<token>/getMe`：抹掉 token 后仍留着 `/bot…`，
#: 那还是把「原始请求 URL」回显给了页面（S6 明令不许），所以整段一并删掉。
URL_PATH_RE = re.compile(r"/bot\S*")

GETME_TIMEOUT = 10               # §4.1
MAX_BODY = 64 * 1024             # §4.1 R2-2：读满即停
MAX_PERSONA = 64 * 1024          # §4.2 A 段
MAX_DISPLAY_NAME = 64            # R8-1
# §6b 两个可选字段的形状（全是本地判据，不做在线校验 —— 不因它们调任何语音服务）
MAX_FISH_VOICE = 64
MIN_FISH_KEY = 8
MAX_FISH_KEY = 256
FISH_VOICE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
FISH_KEY_RE = re.compile(r"^[\x21-\x7e]{8,256}$")
#: 把 Telegram token / 供应商 key 填错格子是最常见的两种手滑，各给一句人话
FISH_KEY_TELEGRAM_RE = re.compile(r"^\d{8,10}:[A-Za-z0-9_-]{30,}$")
REQUIRED_FIELDS = ("bot_id", "telegram_token", "dispatcher_port", "chat_id")
BLANK_TEMPLATE = "_persona_template"
MEMORY_SEED = "# 记忆\n\n（还没聊过，记忆会自己长出来。）\n"
#: S4 分档：207（C 段有一步没 done）待补的动作都属「让 bot 跑起来」面；
#: 201（register 已把网关拉起并等活通过）没有待补动作。`moments_web` 两档都 false ——
#: 新 bot 对朋友圈页现读可见（config_sources 每次调用现读），不需要重启任何 web 进程。
RESTART_FLAGS = {"bot": True, "moments_web": False}
RESTART_FLAGS_DONE = {"bot": False, "moments_web": False}
#: B 段产物清单 = 回滚的精确 unlink 清单（R8-3），两处共用这一份，不许各写各的。
#: 树根 = `<bot 家>/channel`（§7），文件的相对路径以它为准。
TREE_FILES = (".env", "access.json", "CLAUDE.md", os.path.join("memory", "MEMORY.md"))
TREE_DIRS = ("memory",)
#: I5：`com.dsh-bot.` 前缀下形状可达的非 bot 单元名。建一只叫这些名字的 bot 会在
#: register 时 bootout 掉真单元并覆盖其 plist —— 直接拒绝（依据见 PLAN §2.3）。
RESERVED_UNIT_NAMES = ("jiwen", "director", "voicecall")
#: register 两条子进程的超时（bun 装 launchd 单元是秒级的事）
INSTALL_TIMEOUT = 60
AUTOSTART_TS = os.path.join(REPO_ROOT, "gateway", "scripts", "autostart.ts")

HINT_MANUAL = "文件都已就位，手动注册并拉起：bun %s install <配置文件>" % AUTOSTART_TS


# ---------------------------------------------------------------- 测试接缝（§7）

def _registry():
    """延迟 import 仓库根模块 —— 与 `bots_client` 同款做法，别在 moments 包导入期就拉进来。"""
    import bots_registry
    return bots_registry


def channels_dir():
    """§7 `HUB_CHANNELS_DIR`（A2）：B 段产物树、模板扫描、A7 的 `.env` 比对全经它。"""
    return os.environ.get("HUB_CHANNELS_DIR") or os.path.expanduser("~/.claude/channels")


def configs_dir():
    """配置根与注册表同源（`HUB_CONFIGS_DIR` 在 bots_registry 里已解析），不另起一张表。"""
    return str(_registry().configs_dir())


def _config_dirs():
    """新旧两套配置根（顺序 = 覆盖优先级）。注册表按"同名以新系统为准"合并。"""
    return [str(d) for _source, d in config_sources.roots()]


def api_base():
    """校验过形状的 getMe base（已 rstrip `/`）。非法 → 抛，蓝图注册期就炸（§7 R2-1）。

    它是环境变量不是请求参数，配错要在启动期暴露；等到用户点「测试 token」才 4xx，
    中间那段时间每一次调用都在往一个陌生主机送 token。
    """
    raw = (os.environ.get("HUB_TELEGRAM_API_BASE") or DEFAULT_API_BASE).strip()
    if not BASE_RE.match(raw):
        # 不回显原文：它可能被写成 https://user:pass@host 这种自带凭据的形状
        raise RuntimeError("HUB_TELEGRAM_API_BASE 形状非法："
                           "只接受 http(s)://主机[:端口]，不带路径与 query")
    return raw.rstrip("/")


@contextlib.contextmanager
def hold_addbot_lock(app=None):
    """整个创建流程持一把进程内非阻塞锁（R4），抢不到 → `409 config_busy`。

    与参数页的 `hub_config.hold_config_lock` **有意分开**：创建里有 10 秒 getMe
    加 30 秒探活，共用一把锁会让「改个参数」也被顶 409 半分钟。
    ponytail: 单进程前提（同 hub_config）；真上多 worker 就换 flock，接口不变。
    """
    ext = (app or current_app).extensions
    lock = ext.setdefault("hub_addbot", threading.Lock())     # setdefault 在 GIL 下原子
    if not lock.acquire(blocking=False):
        raise HubError(409, "config_busy", "另一次创建正在进行，请稍候重试")
    try:
        yield
    finally:
        lock.release()


# ---------------------------------------------------------------- getMe（§4.1）

def _sanitize(raw):
    """上游可控字符串的消毒：抹凭据 → 去请求 URL 片段 → 剔控制字符 → 截 200 字。

    页面那端一律 `textContent` 渲染，这端先消毒 —— 否则「配了个恶意 base」就是一条 XSS。
    """
    text = redact.scrub_text("" if raw is None else str(raw))
    text = URL_PATH_RE.sub("", text)
    text = "".join(ch for ch in text if 0x20 <= ord(ch) != 0x7f)
    return text.strip()[:200]


def _error_code(body, fallback=None):
    code = body.get("error_code") if isinstance(body, dict) else None
    return code if isinstance(code, int) and not isinstance(code, bool) else fallback


def _classify(status, body):
    """HTTP 状态 + body → §4.1 分型表的一行。"""
    if 300 <= status < 400 or not isinstance(body, dict):
        return {"ok": False, "stage": "bad_response"}
    if status in (401, 404):
        return {"ok": False, "stage": "invalid_token", "error_code": _error_code(body, status),
                "description": _sanitize(body.get("description"))}
    if status >= 500:
        return {"ok": False, "stage": "upstream", "error_code": _error_code(body, status)}
    # R9-1：HTTP 200 但 body `ok:false`（Telegram 限流的常见形态）。原稿缺这格，
    # 代码会落进"成功"分支取 result.username → KeyError → 500。
    if status >= 400 or body.get("ok") is not True:
        return {"ok": False, "stage": "rejected", "error_code": _error_code(body, status),
                "description": _sanitize(body.get("description"))}
    result = body.get("result") if isinstance(body.get("result"), dict) else {}
    username, tg_id = result.get("username"), result.get("id")
    if not isinstance(username, str) or not username:      # R9-2：base 指向任意 HTTP 服务时必然遇到
        return {"ok": False, "stage": "bad_response"}
    return {"ok": True, "username": username[:64],
            "bot_id": tg_id if isinstance(tg_id, int) and not isinstance(tg_id, bool) else None}


def getme(token):
    """§4.1：**只回业务结果，从不抛**（HTTP 恒 200 由路由层保证）。

    三条外发面硬约束都在这里：形状预检 / 明文 http 非回环拒发 / 64 KiB 截断；
    外加写死 `allow_redirects=False` —— 跟随重定向会把带 token 的 URL 跟到 Location 主机。
    """
    if not isinstance(token, str) or not TOKEN_RE.match(token):
        return {"ok": False, "stage": "malformed"}       # 预检不过，一个请求都不发
    base = api_base()
    parts = urlsplit(base)
    loopback = hub_auth.is_loopback(parts.hostname or "")
    if parts.scheme == "http" and not loopback:
        # A1：一次配置手滑（明文 http）就把 token 明文送给第三方。本地预检，不发请求、不回显 base
        return {"ok": False, "stage": "blocked_base"}
    sess = requests.Session()
    if loopback:
        sess.trust_env = False        # 回环流量绝不经系统代理（机主常年开着 http_proxy）
    try:
        with sess.get("%s/bot%s/getMe" % (base, token), timeout=GETME_TIMEOUT,
                      allow_redirects=False, stream=True) as resp:
            if 300 <= resp.status_code < 400:            # R9-3
                return {"ok": False, "stage": "bad_response"}
            buf = bytearray()
            for chunk in resp.iter_content(8192):
                buf += chunk
                if len(buf) > MAX_BODY:                  # R2-2：读满即停，不给大文件灌 OOM 的机会
                    return {"ok": False, "stage": "bad_response"}
            body = json.loads(bytes(buf).decode("utf-8", "replace"))
        return _classify(resp.status_code, body)
    except requests.exceptions.Timeout:
        return {"ok": False, "stage": "timeout"}
    except ValueError:                                   # 响应不是合法 JSON
        return {"ok": False, "stage": "bad_response"}
    except (requests.exceptions.RequestException, OSError) as e:
        # detail 只给异常类名 —— requests 的异常消息里带完整请求 URL（= 带 token）
        return {"ok": False, "stage": "network", "detail": type(e).__name__}
    finally:
        sess.close()


def test_token(token):
    """§4.1 端点的完整结果：成功时附打码 token（`****` + 末 4 位）。"""
    out = getme(token)
    return dict(out, token_masked=redact.mask(token)) if out.get("ok") else out


# ---------------------------------------------------------------- A 段 · 只读校验（§4.2）

def templates():
    """§4.4 + §6：可选的人设模板 = 旧根 `channels/` 下的一级目录 ∪
    `<DSH_BOT_HOME>/bots/` 下含 `channel/` 的一级目录（同名只出一条，按新系统 id 归属）。

    目录读不到是**正常态**，回空列表不是错误；两边读不到一律跳过、不报错。
    display_name 取注册表能给的名字，给不到用原名。
    """
    names = set()
    try:
        names |= {n for n in os.listdir(channels_dir())
                  if not n.startswith(".")
                  and os.path.isdir(os.path.join(channels_dir(), n))}
    except OSError:
        pass
    try:
        bots_root = os.path.join(str(config_sources.dsh_home()), "bots")
        for n in os.listdir(bots_root):
            if n.startswith("."):
                continue
            if os.path.isdir(os.path.join(bots_root, n, "channel")):
                names.add(n)
    except OSError:
        pass
    labels = {b["id"]: b["display_name"]
              for b in _registry().bots(include_disabled=True, dirs=_config_dirs())}
    return [{"id": n, "display_name": "空白模板" if n == BLANK_TEMPLATE else labels.get(n) or n}
            for n in sorted(names)]


def _need(body, name):
    """A4 裁决：键缺失、空串、纯空白一律按「缺必填」→ `bad_body`，`detail` 点名字段。
    非空但不合形状才是 `bad_field`（用户先看到真正该改的东西）。"""
    if name not in body:
        raise HubError(400, "bad_body", "缺少必填字段：%s" % name)
    value = body[name]
    if isinstance(value, str) and not value.strip():
        raise HubError(400, "bad_body", "必填字段不能为空：%s" % name)
    return value


def _is_int(value):
    """bool 是 int 的子类，`True` 当端口用会一路走到 bind —— 这里必须把它挡掉。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _optional(body, name):
    """可选字段的「未填」四等价（§6b）：键缺失、`null`、空串、纯空白。

    四者都回 None；填了的回**去首尾空白后**的值（契约：判定与落盘一律用它）。
    非 str 且非 None（`123`/`True`/`[]`）原样返回，由各自的形状函数报它那一档 ——
    这里不替它们报，因为文案按字段不同。
    """
    value = body.get(name)
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    return value


def _fish_voice(value):
    """§6b 音色三档（非字符串 / 超长 / 字符非法），任一命中抛 400。未填回 None。"""
    if value is not None and not isinstance(value, str):
        _bad("fish_voice_id 必须是字符串")
    if value is None:
        return None
    if len(value) > MAX_FISH_VOICE:
        _bad("音色 id 最长 64 个字符")
    if not FISH_VOICE_RE.match(value):
        _bad("音色 id 只能是字母、数字、下划线或连字符")
    return value


def _fish_key(value):
    """§6b Fish key 各档，顺序：非字符串 → 太短 → 太长 → 形状 → 像谁家的 key。未填回 None。"""
    if value is not None and not isinstance(value, str):
        _bad("fish_api_key 必须是字符串")
    if value is None:
        return None
    if len(value) < MIN_FISH_KEY:
        _bad("Fish key 太短（至少 8 个字符）")
    if len(value) > MAX_FISH_KEY:
        _bad("Fish key 太长（最多 256 个字符）")
    if not FISH_KEY_RE.match(value):
        _bad("Fish key 只能是 8-256 位的可打印 ASCII，不含空格")
    if FISH_KEY_TELEGRAM_RE.match(value):
        _bad("这看起来是 Telegram bot token，不是 Fish Audio 的 key")
    if value.startswith("sk-"):
        _bad("这看起来是模型供应商的 key（sk- 开头），不是 Fish Audio 的 key")
    return value


def _bad(why):
    raise HubError(400, "bad_field", why)


def _check_free(bot_id):
    """同名判定看四处：保留单元名、两套配置根的 `configs/<id>.yml`、旧根 `channels/<id>/`、
    新系统 `<DSH_BOT_HOME>/bots/<id>`。

    只看 yml 会在目标目录已存在时把 `shutil.move` 变成"搬进人家目录里"
    （`<bot 家>/<id>/`），而那里可能是用户手工建的另一个 bot。只看旧系统那份还会放行一个
    建完就被新系统同名配置压住、根本不生效的 bot。保留名（I5）不拦的话，
    register 会 bootout 掉真单元并覆盖它的 plist。
    """
    if bot_id in RESERVED_UNIT_NAMES:
        raise HubError(409, "bot_exists",
                       "%s 这个单元名已被新系统占用（com.dsh-bot. 前缀下的非 bot 单元），"
                       "换个名字" % bot_id)
    for source, d in config_sources.roots():
        if os.path.exists(os.path.join(str(d), bot_id + ".yml")):
            raise HubError(409, "bot_exists",
                           "已经有一个叫 %s 的 bot 了（%s那份配置里）"
                           % (bot_id, config_sources.SOURCE_LABELS[source]))
    if os.path.exists(os.path.join(channels_dir(), bot_id)):
        raise HubError(409, "bot_exists", "频道目录 %s 已存在，换个名字或先手动清理" % bot_id)
    home = config_sources.dsh_bot_dir(bot_id)
    if home is not None and _bot_home_taken(home):
        raise HubError(409, "bot_exists",
                       "新系统里已经有一个叫 %s 的 bot 家了，换个名字或先手动清理" % bot_id)


def _bot_home_taken(home):
    """这个 bot 家已经被真的占了吗（§7 A 段拒绝面的判据）。

    判的是"已经有一棵频道树"（`channel/` 在），不是"目录/文件存在"：运行中的 bot 家
    （网关自建的 `state`、`logs` 与心跳）会先于本流程存在 —— 心跳必须落在
    `<bot 家>/state/heartbeat`，验收里"全流程成功"的场景天然带着它；把"目录存在"
    当占用会把"这只 bot 之前在跑"误判成冲突。真正的数据面是 `channel/`
    （`.env`、`CLAUDE.md`、记忆），它在那里才说明这个位置是别人的 bot。
    符号链接一律按占用处理：往一个软链后面写等于写到任意位置去。
    """
    if os.path.islink(str(home)):
        return True
    if os.path.exists(str(home)) and not os.path.isdir(str(home)):
        return True          # 位置被一个文件占了，不是目录
    return os.path.exists(os.path.join(str(home), "channel"))


def _listening(port):
    """本机是否已有进程在监听。R6-4：这与 bot 存在与否无关，所以它不是 `bot_exists` ——
    回 `bot_exists` 会让用户去找一个根本不存在的同名 bot。"""
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        sock.close()


def _check_port(port, skip=None):
    """端口占用两问：注册表里有没有别人占（含派生值）、本机是不是已经有人在听。

    ``include_disabled=True``：被停用的 bot 仍占着它的端口，放行一个撞号的新 bot
    会让那个 bot 一旦重新启用就起不来（或反过来抢走新 bot 的号）。
    注册表并**新旧两套配置**：新系统 bot 占的号（它也要在本机监听）同样不许撞。
    """
    for bot in _registry().bots(include_disabled=True,
                                dirs=[str(d) for _source, d in config_sources.roots()]):
        if bot["port"] == port and bot["id"] != skip:
            raise HubError(409, "bot_exists", "端口 %d 已被 %s 占用" % (port, bot["id"]))
    if skip is None and _listening(port):
        raise HubError(409, "port_in_use", "端口 %d 已被本机其它进程监听" % port)


def _persona(body):
    """人设来源二选一，返回要写进 `CLAUDE.md` 的正文。

    模板选定时**按 id 归属读正文**（I6）：`<DSH_BOT_HOME>/bots/<id>/channel/CLAUDE.md`
    存在就优先读它，否则读旧根 `<HUB_CHANNELS_DIR>/<id>/CLAUDE.md`；两处都读不到
    （目录在、文件都不在）→ 空人设（空白模板语义），不是错误。
    """
    tmpl, text = body.get("persona_template"), body.get("persona_text")
    has_tmpl = isinstance(tmpl, str) and bool(tmpl.strip())
    has_text = isinstance(text, str) and bool(text.strip())
    if has_tmpl == has_text:
        _bad("人设来源二选一：persona_template 或 persona_text，给一个")
    if has_text:
        if len(text.encode("utf-8")) > MAX_PERSONA:
            raise HubError(413, "payload_too_large", "人设正文超过 64 KiB")
        return text
    if tmpl not in {t["id"] for t in templates()}:
        # 只认扫出来的目录名，`../` 这类穿越串自然落不进集合里
        _bad("没有这个人设模板：%s" % tmpl[:64])
    home = config_sources.dsh_bot_dir(tmpl)
    candidates = []
    if home is not None:
        candidates.append(os.path.join(str(home), "channel", "CLAUDE.md"))
    candidates.append(os.path.join(channels_dir(), tmpl, "CLAUDE.md"))
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as f:
                return f.read(MAX_PERSONA)
        except OSError:
            continue
    return ""            # 目录在但两处都没有 CLAUDE.md → 空人设，不是错误


def _telegram_ids():
    """既有各 bot 的 telegram id（A7）：两套根下各 bot 的 `.env` 里 token 的 `:` 前数字段。

    新系统 bot 的 token 落在 `<bot 家>/channel/.env`（yml 的 `bot_channel_path` 优先，
    缺失回落默认家目录），不扫它就漏掉"新旧两只同 token"那类静默丢消息的坑（R1）。
    读到的整串只在内存里过一遍正则，函数出去时只剩数字段 ——
    绝不进响应/日志/异常文本。`.env` 缺失或形状不符 → 跳过该 bot 继续，不报错。
    """
    import config_loader
    out = {}
    for cfg in config_loader.list_enabled_bots(include_disabled=True,
                                               dirs=config_sources.roots()):
        bot_id = cfg.get("_bot_id")
        if not bot_id:
            continue
        for path in _env_paths(cfg):
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    hit = ENV_TOKEN_RE.search(f.read(MAX_BODY))
            except OSError:
                continue
            if hit:
                out[bot_id] = int(hit.group(1))
                break
    return out


def _env_paths(cfg):
    """一份配置可能对应的 `.env` 位置（旧根频道目录 + 新系统 bot 家的 channel）。"""
    bot_id = cfg["_bot_id"]
    paths = [os.path.join(channels_dir(), bot_id, ".env")]
    raw = cfg.get("bot_channel_path")
    if isinstance(raw, str) and raw.strip():
        paths.append(os.path.join(os.path.expanduser(raw.strip()), ".env"))
    home = config_sources.dsh_bot_dir(bot_id)
    if home is not None:
        paths.append(os.path.join(str(home), "channel", ".env"))
    seen, out = set(), []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _validate(body):
    """A 段的纯校验部分（不含 getMe 与 token 比对），顺序照 §4.2/§6 的表逐条来。

    **纯输入错（用户自己能改的）优先于环境与冲突错**：先把形状挑干净，再判
    `dsh_root_off`（环境）、再看跟别人撞没撞。冲突判定里配置与家目录在前、端口在后。
    """
    if not isinstance(body, dict):
        raise HubError(400, "bad_body", "请求体必须是一个 JSON 对象")
    fields = {k: _need(body, k) for k in REQUIRED_FIELDS}
    bot_id, port, chat_id = fields["bot_id"], fields["dispatcher_port"], fields["chat_id"]
    if not isinstance(bot_id, str) or not BOT_ID_RE.match(bot_id):
        _bad("bot_id 只能是小写字母开头的 2-32 位小写字母/数字/下划线")
    if not _is_int(port) or not 1024 <= port <= 65535:
        _bad("dispatcher_port 必须是 1024-65535 的整数")
    name = body.get("display_name")
    if (not isinstance(name, str) or not name.strip() or len(name) > MAX_DISPLAY_NAME
            or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in name)):
        # R8-1：它会进 yml、进页面渲染、可能进 plist 的 Label，控制字符一路带进去必炸
        _bad("display_name 必须是 1-64 字的字符串，且不含控制字符")
    if not _is_int(chat_id):
        _bad("chat_id 必须是整数")
    persona = _persona(body)
    # §6b 两个可选字段：判序在人设二选一之后、环境与冲突错（409）之前；两个字段各判各的
    # （四个组合独立落盘，不做成对约束）。
    fish_voice_id = _fish_voice(_optional(body, "fish_voice_id"))
    fish_api_key = _fish_key(_optional(body, "fish_api_key"))
    # 环境错（用户自己改不了）：新系统配置根被关掉就落不了盘，这一页只建新系统的 bot
    if config_sources.dsh_dir() is None:
        raise HubError(409, "dsh_root_off",
                       "新系统配置根被关掉了（HUB_CONFIGS_DSH_DIR=off），这一页只建新系统的 bot")
    _check_free(bot_id)
    _check_port(port)
    return {"bot_id": bot_id, "token": fields["telegram_token"], "port": port,
            "chat_id": chat_id, "display_name": name, "persona": persona,
            "fish_voice_id": fish_voice_id, "fish_api_key": fish_api_key}


def _check_token_free(bot_id, telegram_id):
    """R1：同一个 Telegram bot 被两个 dispatcher 同时 long-poll，Telegram 回 409，
    **用户消息静默丢一半**，日志里只有零星 409 —— 极难定位，必须在落盘前拦住。"""
    if telegram_id is None:
        return
    for other, known in _telegram_ids().items():
        if known == telegram_id and other != bot_id:
            raise HubError(409, "token_in_use", "这个 Telegram bot 已经被 %s 用着了" % other)


# ---------------------------------------------------------------- B 段 · 落盘（§4.2）

def _create_file(path, text, mode=0o600):
    """`O_CREAT|O_EXCL|O_WRONLY` 一次建成：没有"先建再 chmod"的 0644 窗口，
    也顺带挡住同名并发创建（R4）。"""
    # O_BINARY（只有 Windows 有）：不加的话 CRT 走文本模式，下面按 "wb" 写的 \n 会被悄悄
    # 换成 \r\n —— yml/.env 的内容就不再逐字节等于我们生成的那份。
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                 | getattr(os, "O_BINARY", 0), mode)
    with os.fdopen(fd, "wb") as f:
        f.write(text.encode("utf-8"))


def _unlink(path):
    with contextlib.suppress(OSError):
        os.unlink(path)


def _rmdir(path):
    # 只删空目录：里面若有别人的东西就留着。回滚里绝不 rm -rf（R8-3）
    with contextlib.suppress(OSError):
        os.rmdir(path)


#: 回滚清单条目类型（R8-3 的精确 unlink 清单）
_MADE_FILE = 0      # yml 文件：直接 unlink
_MADE_TREE = 1      # 频道树：删清单内的文件与目录，再 rmdir 树根
_MADE_HOME = 2      # bot 家：只 rmdir（非空一律保留）


def _rollback(made):
    """按创建清单逆序精确删除。回滚过程里再抛异常会把真正的错因盖掉，所以全程吞。

    三类条目见上面的常量：频道树删 `TREE_FILES`/`TREE_DIRS` 清单内的东西，
    bot 家与其它目录一律只 rmdir（空的才删）—— 用户的 bot 家可能早就在那儿，
    一次手滑就是别人的聊天记录没了（R8-3）。"""
    for kind, path in reversed(made):
        if kind == _MADE_FILE:
            _unlink(path)
        elif kind == _MADE_HOME:
            _rmdir(path)
        else:
            for rel in TREE_FILES:
                _unlink(os.path.join(path, rel))
            for rel in TREE_DIRS:
                _rmdir(os.path.join(path, rel))
            _rmdir(path)


def _fix_modes(home):
    """F5：`shutil.move` 跨文件系统会走 copy 路径、权限取决于实现，
    而 macOS 的 TMPDIR 在 `/var/folders/...`，与 `~/.dsh-bot` 大概率不同挂载点 —— 就位后逐个复核。"""
    channel = os.path.join(home, "channel")
    os.chmod(home, 0o700)
    os.chmod(channel, 0o700)
    for rel in TREE_DIRS:
        os.chmod(os.path.join(channel, rel), 0o700)
    for rel in TREE_FILES:
        os.chmod(os.path.join(channel, rel), 0o600)


def _yaml_str(value):
    """写进 yml 的字符串字面量。`json.dumps` 出的双引号串是 YAML 的合法子集：
    emoji / 引号 / 换行全都不用自己转义，也就没有"手写转义漏一种"的洞。"""
    return json.dumps(value, ensure_ascii=False)


def _channel_path_literal(target):
    """`bot_channel_path`：能收进 `~` 就收（与既有 `configs/*.yml` 同形），否则给绝对路径。"""
    home = os.path.expanduser("~")
    if target == home or target.startswith(home + os.sep):
        return "~" + target[len(home):]
    return target


def _new_namespace():
    """uuid4 + 全表去重。namespace 撞车 = 两个 bot 共用会话历史，所以去重是契约的一部分
    （撞的概率是 0，这个 while 只是把契约写进代码里）。扫描范围是**两套根**：
    新系统 bot 的 namespace 也要算进已用集合。"""
    used = {b["namespace_uuid"] for b in _registry().bots(include_disabled=True,
                                                          dirs=_config_dirs())}
    ns = str(uuid.uuid4())
    while ns in used:
        ns = str(uuid.uuid4())
    return ns


def _render_yml(fields, target):
    """`configs/<bot>.yml`：**不含 token**（F5），只有注册表要的那几个键。"""
    return ("id: %s\n"
            "display_name: %s\n"
            "bot_channel_path: %s\n"
            "chat_id: %d\n"
            "dispatcher_port: %d\n"
            "namespace_uuid: %s\n"
            % (fields["bot_id"], _yaml_str(fields["display_name"]),
               _yaml_str(_channel_path_literal(target)),
               fields["chat_id"], fields["port"], _new_namespace()))


def lay_down(fields):
    """B 段：tmp 里建好整棵树 → 一次 `move` 到 `<bot 家>` → 写 yml → 复查端口。

    落点全在新系统那套位置（§7）：配置进 `<dsh 配置根>/<bot>.yml`，频道树进
    `<DSH_BOT_HOME>/bots/<bot>/channel/`；旧系统的两个根一个字节都不产生。

    任一步失败：按精确清单逆序删干净，磁盘回到操作前（可断言 yml 不在、bot 家不在、
    注册表查不到它），然后把异常原样抛给调用方定码。回滚只删清单内的东西，
    非空目录一律保留 —— 用户的 bot 家可能早就在那儿，一次手滑就是别人的聊天记录没了。
    """
    bot_id = fields["bot_id"]
    home = str(config_sources.dsh_bot_dir(bot_id))
    channel = os.path.join(home, "channel")
    cfg_root = config_sources.dsh_dir()
    if cfg_root is None:
        raise HubError(409, "dsh_root_off", "新系统配置根被关掉了，落不了盘")
    # 这两级目录可能本来就是有的（新系统早就在跑）；只记下"哪些是这次才建出来的"，
    # 失败回滚时把它们摘掉 —— 否则一次失败的创建会在新系统家目录里留一个空的 bots/。
    seeded = [d for d in (str(cfg_root), os.path.dirname(home)) if not os.path.isdir(d)]
    os.makedirs(str(cfg_root), exist_ok=True)
    os.makedirs(os.path.dirname(home), exist_ok=True)   # <DSH_BOT_HOME>/bots
    yml = os.path.join(str(cfg_root), bot_id + ".yml")
    made = []                                  # 精确回滚清单：(类型, 路径)，按创建顺序
    tmp = tempfile.mkdtemp()                   # mkdtemp 本身就是 0700
    try:
        src = os.path.join(tmp, bot_id)
        os.mkdir(src, 0o700)
        chan = os.path.join(src, "channel")
        os.mkdir(chan, 0o700)
        for rel in TREE_DIRS:
            os.mkdir(os.path.join(chan, rel), 0o700)
        # token 的唯一落点（F5）。yml 里一个字都没有它。§6b 的 Fish key 是它的第二行
        # （独立一行、键序 token 在前），只有填了才写；两个字段都落在既有文件内，
        # 回滚清单不用动（整树删自然覆盖）。
        env_lines = "TELEGRAM_BOT_TOKEN=%s\n" % fields["token"]
        if fields.get("fish_api_key"):
            env_lines += "FISH_AUDIO_API_KEY=%s\n" % fields["fish_api_key"]
        _create_file(os.path.join(chan, ".env"), env_lines)
        access = {"allowFrom": [fields["chat_id"]]}
        if fields.get("fish_voice_id"):
            access["voiceId"] = fields["fish_voice_id"]      # 键序 allowFrom 在前、voiceId 在后
        _create_file(os.path.join(chan, "access.json"),
                     json.dumps(access, ensure_ascii=False) + "\n")
        _create_file(os.path.join(chan, "CLAUDE.md"), fields["persona"])
        _create_file(os.path.join(chan, "memory", "MEMORY.md"), MEMORY_SEED)
        if os.path.exists(home):
            # 已有的 bot 家（网关自建过 state/logs 之类）→ 只把频道树补进去，
            # 绝不用 move 整个 src（那会变成"搬进人家目录里"）
            if os.path.exists(channel):
                raise HubError(409, "bot_exists", "这个 bot 家已经有频道树了")
            shutil.move(chan, channel)
            made.append((_MADE_TREE, channel))
        else:
            # 一次 move 建出整个 bot 家（含 channel）：tmp 里全建好再就位，
            # 中途失败时目标位置一个字节都没写过，回滚清单为空
            shutil.move(src, home)
            made.append((_MADE_HOME, home))
            made.append((_MADE_TREE, channel))
        _fix_modes(home)
        _create_file(yml, _render_yml(fields, channel))
        made.append((_MADE_FILE, yml))
        _check_port(fields["port"], skip=bot_id)   # R4：A 段那次 bind 试探到这里已经有窗口
    except BaseException:
        _rollback(made)
        for d in reversed(seeded):                 # 逆序：先摘深的 bots/，再摘配置根
            _rmdir(d)                              # 只删空的，里面还有别人的东西就留着
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)     # 只删自己 mkdtemp 出来的那个
    return channel


# ---------------------------------------------------------------- C 段 · 注册 + 拉起（§4.2）

# ---------------------------------------------------------------- 停用开关

def set_enabled(bot_id, enabled):
    """把该 bot 配置文件里的顶层 `enabled` 改成布尔值，返回改完的值。

    停用的唯一标记就是这个字段（`bots_registry.is_enabled` 判它），所以这里也是**唯一**
    的写入口。四条硬约束：

    - **写"这个 bot 生效的那份"配置**：混跑期两套配置都读（新旧系统），在跑的那份优先，
      没有在跑的就写优先级最高那份（启用一个停着的 bot 等于让它在新系统里跑起来）。
      盯着文件名写会出两种错：旧栈正在跑的 bot 点停止落在新系统那份上，既停不掉它、
      又在那边留一处没人看的改动；而对一个"只有旧系统那份在跑"的 bot 点停止，
      写的必须正是旧系统那份。
    - **绝不 dump YAML**：走 `hub_config.set_scalar` 的行级编辑，注释/锚点/键序/块标量
      逐字节保真（§2.4 那条可 grep 的红线，bot 的 yml 里全是人设正文，dump 一次就毁）。
    - **原子写**：复用 `hub_config.atomic_write`，半截文件不可能出现 —— bot 的 yml 写坏了
      注册表就整个跳过它，等于把 bot 弄丢。
    - **幂等**：已经是这个值就原样写回同一份文本（`set_scalar` 只换那一段字节，
      重复调用产出完全相同的文件）。

    `enabled` 键不存在时走 `set_scalar` 的插入分支（按父块缩进插在块首）。
    """
    from moments import hub_config          # 延迟 import：只有这一个函数要它
    if not isinstance(bot_id, str) or not BOT_ID_RE.match(bot_id):
        raise HubError(400, "bad_bot_id", "bot id 形状非法")
    # 两套根里按覆盖规则找：命中新系统那份就写新系统那份；都没有 → 默认根下的路径
    # （读文件时 FileNotFoundError → 404，与改动前一致）。
    found = config_sources.find_path(bot_id)
    path = str(found) if found is not None else os.path.join(configs_dir(), bot_id + ".yml")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        raise HubError(404, "bot_not_found", "没有叫 %s 的 bot" % bot_id)
    except OSError as e:
        # 只报类型名：异常消息带路径（§0.2 同款口径）
        raise HubError(500, "config_unreadable", type(e).__name__)
    try:
        root = hub_config.compose(text)
    except yaml.YAMLError as e:
        raise HubError(409, "bad_yaml",
                       hub_config.yaml_error_detail(e, "%s.yml 解析失败" % bot_id))
    new_text = hub_config.set_scalar(text, root, "enabled", bool(enabled), "bool")
    hub_config.atomic_write(new_text, path)
    return bool(enabled)


def _step(name, state, detail=None, **extra):
    step = {"name": name, "state": state}
    if detail:
        step["detail"] = detail
    step.update(extra)
    return step


def _resolve_bun():
    """bun 可执行文件：``HUB_BUN`` → PATH → ``~/.bun/bin/bun``；都找不到回 None。

    面板 plist 的 PATH 实测没有 bun（所以不能只靠 ``shutil.which``）；
    测试经 ``HUB_BUN`` 注入假件，绝不实调真 bun。
    """
    env = os.environ.get("HUB_BUN")
    if env:
        return env if os.path.isfile(env) and os.access(env, os.X_OK) else None
    found = shutil.which("bun")
    if found:
        return found
    cand = os.path.expanduser("~/.bun/bin/bun")
    return cand if os.path.isfile(cand) and os.access(cand, os.X_OK) else None


def _register_dsh(fields):
    """macOS：交给 `gateway/scripts/autostart.ts` 装网关单元与主动消息单元（§6 register）。

    两条命令**短路**：install 非 0 即止（install-jobs 不跑）。子进程原始输出（含 launchd
    报错原文）一个字不进 `detail`/`manual_hint`（S9）—— 只给机读码与固定命令文本。
    PATH 前置 `~/.pyenv/shims`（目录存在时），让 autostart.ts 的 `Bun.which('python3')`
    解析到 pyenv 那支，与既有 dsh bot 单元口径一致；shims 不在的机器自然回落。
    """
    yml = os.path.join(str(config_sources.dsh_dir()), fields["bot_id"] + ".yml")
    bun = _resolve_bun()
    hint = ("手动注册：%s %s install %s ； %s %s install-jobs %s"
            % (bun or "bun", AUTOSTART_TS, yml, bun or "bun", AUTOSTART_TS, yml))
    if bun is None:
        return _step("register", "failed", "bun_missing", manual_hint=hint)
    if not os.path.isfile(AUTOSTART_TS):
        return _step("register", "failed", "autostart_missing", manual_hint=hint)
    env = dict(os.environ)
    shims = os.path.expanduser("~/.pyenv/shims")
    if os.path.isdir(shims):
        env["PATH"] = shims + os.pathsep + (env.get("PATH") or "")
    for sub, code in (("install", "install_failed"), ("install-jobs", "install_jobs_failed")):
        try:
            rc = subprocess.run([bun, AUTOSTART_TS, sub, yml], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env=env, timeout=INSTALL_TIMEOUT).returncode
        except (OSError, subprocess.SubprocessError):
            return _step("register", "failed", code, manual_hint=hint)
        if rc != 0:
            return _step("register", "failed", code, manual_hint=hint)
    return _step("register", "done")


def _register_task(fields):
    """Windows：`register-tasks.ps1` 已按 D1-⑥ 遍历注册表，跑一次就把新 bot 带上，
    不必写单 bot 注册逻辑（它先 Unregister 再注册，天然幂等）。"""
    script = os.path.join(REPO_ROOT, "windows", "register-tasks.ps1")
    hint = "powershell -NoProfile -ExecutionPolicy Bypass -File %s" % script
    if not os.path.isfile(script):
        return _step("register", "skipped", "register_tasks_missing", manual_hint=hint)
    try:
        code = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                               "-File", script], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=120).returncode
    except (OSError, subprocess.SubprocessError) as e:
        return _step("register", "failed", type(e).__name__, manual_hint=hint)
    if code != 0:
        return _step("register", "failed", "register_tasks_failed", manual_hint=hint)
    return _step("register", "done")


def register(fields):
    """C 段第 4 步：注册常驻单元并把网关拉起来（§6 register 的分平台口径）。

    darwin 交给 autostart.ts（它自己 bootout + bootstrap）；nt 保留既有 `_register_task`；
    其余平台 `skipped` + 确切的手工命令走 207。
    """
    if sys.platform == "darwin":
        return _register_dsh(fields)
    if os.name == "nt":
        return _register_task(fields)
    return _step("register", "skipped", "no_launchd",
                 manual_hint="本机没有 launchd/计划任务，手动拉起：bun %s install <配置文件>"
                             % AUTOSTART_TS)


def wait_alive(bot_id):
    """C 段第 7 步：等该 bot 满足 §5 的存活条件，上限 30 秒（§6；换芯见 PLAN §1.7）。

    判活实现只有一处（``bot_launchd``），这里不另判一遍；上限取它的模块常量
    （测试可改），不在这里再抄一个数。
    """
    from moments import bot_launchd
    ok, reason, _pid, _age = bot_launchd.wait_alive(
        bot_id, since_ms=time.time() * 1000, timeout=bot_launchd.CREATE_WAIT)
    return ok, reason


def restart(fields):
    """C 段第 5 步：等它活起来（§6 restart）。该步自己等，不产生 job。

    非 macOS 上没有机制会拉起新 bot（拍板 5）→ `skipped` + manual_hint，
    不假装等 30 秒再报 failed（S7）。等不到为 failed，manual_hint 给确切命令与日志路径。
    """
    bot_id = fields["bot_id"]
    home = str(config_sources.dsh_bot_dir(bot_id))
    hint = ("手动拉起：launchctl kickstart -k gui/$UID/com.dsh-bot.%s ；日志 %s"
            % (bot_id, os.path.join(home, "logs", "launchd.log")))
    if sys.platform != "darwin":
        return _step("restart", "skipped", "no_launchd", manual_hint=hint)
    ok, reason = wait_alive(bot_id)
    if ok:
        return _step("restart", "done")
    return _step("restart", "failed", reason or "not_alive", manual_hint=hint)


# ---------------------------------------------------------------- 三段总装（§4.3）

def _respond(fields, probe, steps):
    """全成功 201；C 段有任一步没 done → `207 bot_created_partial` + 能自己补的确切命令。

    207 不删 B 段产物：文件已经是对的、bot 手动跑得起来，把它删掉等于用户白填一遍表单。
    `restart_hint` 按 S4 分档：201 没有待补动作；207 的待补动作都属「让 bot 跑起来」面。
    """
    body = {"ok": True, "bot_id": fields["bot_id"], "username": probe.get("username"),
            "steps": steps, "restart_hint": dict(RESTART_FLAGS_DONE)}
    stuck = [s for s in steps if s["state"] != "done"]
    if not stuck:
        return body, 201
    hints = [s["manual_hint"] for s in stuck if s.get("manual_hint")]
    body.update(ok=False, error="bot_created_partial",
                manual_hint=" ；".join(hints) or HINT_MANUAL,
                restart_hint=dict(RESTART_FLAGS))
    return body, 207


def create(body, jobs):
    """三段全流程。返回 `(响应体, HTTP 码)`。

    A 段的失败一律以 `HubError` 抛出（零副作用）；getMe 的业务失败**不是错误码** ——
    回 `200 {"ok": false, "stage": ...}` 且不落盘（§4.1 同款风味）。
    """
    fields = _validate(body)
    probe = getme(fields["token"])
    if not probe.get("ok"):
        return probe, 200
    _check_token_free(fields["bot_id"], probe.get("bot_id"))
    steps = [_step("validate", "done"), _step("getme", "done")]
    try:
        lay_down(fields)
    except HubError:
        raise                              # 端口复查这类：已回滚干净，照原码抛
    except Exception as e:
        # detail 只给异常类名：路径与 token 都不出站
        raise HubError(500, "internal", type(e).__name__)
    steps.append(_step("files", "done"))
    steps.append(register(fields))
    steps.append(restart(fields))
    return _respond(fields, probe, steps)


# ======================================================================
# bot 二次编辑（INTERFACE §10）：改名 / 换人设 / 换音色
#
# 用户看得见的三样东西各写一处，显示名额外写第二处（同一个人的两份读源）：
#   yml 顶层 display_name → 画风页与状态页现读；`bot_profiles.display_name` → 朋友圈；
#   `<channelDir>/CLAUDE.md` → 人设正文；`<channelDir>/access.json` 的 voiceId → 音色。
# **只对新系统的 bot 开放**（放行 = `bot_sources()` 说它生效的那份来自 dsh），
# 旧系统一个字节都不碰。
#
# 四口写入顺序固定（yml → access.json → CLAUDE.md → 朋友圈库），全部校验通过才动
# 第一个字节；任一步失败按**逆序**把已写的恢复旧内容，然后 500 —— 三个字段可能同一
# 弹窗一起提交，"保存失败但名字已经变了"是最难排查的状态（PLAN §11.4）。
# 本端点**不产生任何 launchctl 调用、不发网络请求、不产生 job**：人设由网关侧
# mtime 轮询自己发现（默认 5 秒），名字与音色都是每次现读。
# ======================================================================

_EDIT_FIELDS = ("display_name", "persona", "fish_voice_id")
_EDIT_TEXT = ("已保存。名字与音色立即生效；人设会被在跑的 bot 在几秒内检测到，"
              "空闲时自动换上。")
_EDIT_404 = "没有这个新系统的 bot（这一页只管新系统的 bot）"
_EDIT_BAD_ACCESS = "access.json 读不出内容（不存在或解析失败），先修好"


def _atomic_bytes(path, data):
    """原子写字节：mkstemp → fsync → chmod 0600 → replace。

    权限**恒 0600**（契约 §10b：三个文件都按 0600 走）：写的是 .env / access.json /
    yml 这类配置，历史遗留的宽权限顺手收紧，绝不反向放松。

    不复用 `hub_config.atomic_write` 的两条理由：它只收 str，而人设与 access.json 的
    旧内容要按**字节**原样恢复（字符串往返会改变非法 UTF-8 的字节）；它把 OSError 包成
    HubError，类名会被壳盖掉，而契约要的 detail 正是「写配置失败：<类名>」。
    """
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".",
                                   prefix="." + os.path.basename(path) + "-", suffix=".tmp")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp:
            _unlink(tmp)


def _read_bytes(path, missing_ok=False):
    """读文件的字节。`missing_ok` 时"不存在"回 None；其余 OSError 照抛给调用方翻码。"""
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise


def _edit_target(bot_id):
    """编辑端点的放行闸（§10）：只对新系统的 bot 开放，返回它生效的那份配置。

    放行条件 = `bot_sources()` 说这只生效的那份配置来自 dsh；legacy、`HUB_BOTS_FILE`
    注入名单、压根没这只 —— 一律 404 `bot_not_found`，与单只重启同款口径。

    只有一种情况越过 `bot_sources()`：**新系统配置根下确实有这只的 yml、但那份文件
    解析不出来**（`bot_sources()` 会跳过坏配置，直接判死会让契约里的 409 `bad_yaml`
    永远不可达）。这时放它过去，让读文件那一步报 409/500，用户才知道要修什么。
    返回值可能为 None（坏配置算不出生效配置），channelDir 与朋友圈身份各自有回落。
    """
    if not isinstance(bot_id, str) or not BOT_ID_RE.match(bot_id):
        raise HubError(400, "bad_bot_id", "bot id 形状非法")
    if os.environ.get("HUB_BOTS_FILE"):
        raise HubError(404, "bot_not_found", _EDIT_404)
    info = bots_client.bot_sources().get(bot_id) or {}
    source = info.get("source")
    if source == config_sources.SOURCE_DSH:
        return config_sources.effective_config(bot_id)
    if source:
        raise HubError(404, "bot_not_found", _EDIT_404)      # 来源判得出来，但不是新系统
    d = config_sources.dsh_dir()
    if d is not None and os.path.isfile(os.path.join(str(d), bot_id + ".yml")):
        return config_sources.effective_config(bot_id)       # 坏配置：通常是 None
    raise HubError(404, "bot_not_found", _EDIT_404)


def _channel_dir(cfg, bot_id):
    """§10 的 channelDir：yml 的 `bot_channel_path`（expanduser）；缺失或空 → 新系统家目录下的 channel。

    与网关 `config.ts:286` 同款 —— 用户手改过 `bot_channel_path` 的机器上，编辑跟着写
    网关真正读的那一份，而不是硬编码路径。`cfg` 为 None（yml 读不出来）时按缺失算。
    """
    raw = (cfg or {}).get("bot_channel_path")
    if isinstance(raw, str) and raw.strip():
        return os.path.expanduser(raw.strip())
    return os.path.join(str(config_sources.dsh_bot_dir(bot_id)), "channel")


def _top_str(root, key):
    """yml 顶层某个键的字符串值；键缺失 / 非字符串 / 空 → 空串（§10a）。"""
    from moments import hub_config              # 延迟 import：理由同 set_enabled
    node = hub_config.find(root, [key])
    # compose 出来的是节点树：`display_name: 123` 也是 ScalarNode，靠 tag 才分得清字符串
    if isinstance(node, yaml.ScalarNode) and node.tag == "tag:yaml.org,2002:str":
        value = node.value
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _voice_of(raw):
    """access.json 里的 voiceId：文件不存在 / 坏 JSON / 非对象 / 键缺失或非字符串 → 空串（§10a）。"""
    if raw is None:
        return ""
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return ""
    if not isinstance(obj, dict):
        return ""
    value = obj.get("voiceId")
    return value if isinstance(value, str) else ""


def edit_read(bot_id):
    """GET /hub/api/bots/<bot_id>/edit（§10a）：读当前值，供弹窗回填。

    读路径只有一个地方会报错：yml 读不出来（500）或解析不出（409）。坏 access.json、
    缺人设文件都回空串 —— 保存时才拦（§10b 的 409 条件是"要写那个字段"）。
    """
    cfg = _edit_target(bot_id)
    yml_path = config_sources.find_path(bot_id)
    if yml_path is None:
        raise HubError(404, "bot_not_found", _EDIT_404)
    channel = _channel_dir(cfg, bot_id)
    try:
        text = _read_bytes(str(yml_path)).decode("utf-8")
        persona_raw = _read_bytes(os.path.join(channel, "CLAUDE.md"), missing_ok=True)
        access_raw = _read_bytes(os.path.join(channel, "access.json"), missing_ok=True)
    except OSError as e:
        raise HubError(500, "config_unreadable", type(e).__name__)
    except UnicodeDecodeError as e:
        raise HubError(500, "config_unreadable", type(e).__name__)
    from moments import hub_config              # 延迟 import：理由同 set_enabled
    try:
        root = hub_config.compose(text)
    except yaml.YAMLError as e:
        raise HubError(409, "bad_yaml", hub_config.yaml_error_detail(e, "%s.yml 解析失败" % bot_id))
    if persona_raw is None:
        persona = ""
    else:
        try:
            persona = persona_raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise HubError(500, "config_unreadable", type(e).__name__)
    return {"bot_id": bot_id, "display_name": _top_str(root, "display_name"),
            "persona": persona, "fish_voice_id": _voice_of(access_raw)}


def _edit_name(value):
    """§10b 显示名：给了这个键就必须是 1-64 字、不含控制字符的字符串（判据取 strip 后的值）。"""
    if isinstance(value, str):
        name = value.strip()
        if (1 <= len(name) <= MAX_DISPLAY_NAME
                and not any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in name)):
            return name
    _bad("display_name 必须是 1-64 字的字符串，且不含控制字符")


def _edit_voice(value):
    """§10b 音色：非字符串（含 null）400；strip 后为空 = 清除音色（回空串）；其余与 §6b 同三档。"""
    if not isinstance(value, str):
        _bad("fish_voice_id 必须是字符串")
    voice = value.strip()
    if not voice:
        return ""
    if len(voice) > MAX_FISH_VOICE:
        _bad("音色 id 最长 64 个字符")
    if not FISH_VOICE_RE.match(voice):
        _bad("音色 id 只能是字母、数字、下划线或连字符")
    return voice


def _restore_writes(undo):
    """逆序恢复已写的东西（§10b 第 3 条）；返回"有没有恢复也失败的"。

    恢复里再抛异常会把真正的错因盖掉，所以逐个吞掉、只记下有人没恢复回来 ——
    那一档要给用户「部分文件已改动，请手工核对」。
    """
    partial = False
    for restore in reversed(undo):
        try:
            restore()
        except Exception:                       # noqa: BLE001
            partial = True
    return partial


def _write_detail(exc, partial):
    """§10b 的两档 detail（类名不带消息：异常消息里可能有路径）。"""
    tail = "部分文件已改动，请手工核对" if partial else "这次没有改动任何文件"
    return "写配置失败：%s；%s" % (type(exc).__name__, tail)


def _save_db_name(cfg, bot_id, value, undo):
    """朋友圈库那一口（§10b 第 4 步）：读旧值 → upsert → 记下恢复动作。

    读旧值也在这步里：库文件不可写时 `get_profile` 一样会炸，与写一起走同一条
    「写配置失败」路径（契约那两档文案就在这条路上）。恢复 = 写回旧 display_name；
    原本没有这行时会留下一行空值，`get_profile` 读出来仍是空，等效无值。
    """
    import db as botlife_db                     # 延迟 import：与 bots_registry 同一理由
    pid = config_sources.person_id(cfg) if cfg else bot_id   # 朋友圈身份（life 名），不是 bot id
    old = (botlife_db.get_profile(pid) or {}).get("display_name")
    botlife_db.set_display_name(pid, value)
    undo.append(lambda p=pid, v=old: botlife_db.set_display_name(p, v))


def edit_save(bot_id, body):
    """POST /hub/api/bots/<bot_id>/edit（§10b）：校验 → 读旧 → 写四口 → 失败逆序恢复。

    返回 `(响应体, 状态码)`；一切失败以 HubError 抛出，判序照契约的表先命中先回。
    「不改某个字段」的表达是**不给这个键**：给了就必须是字符串，null 也是 400。
    """
    if not isinstance(bot_id, str) or not BOT_ID_RE.match(bot_id):
        raise HubError(400, "bad_bot_id", "bot id 形状非法")
    if not isinstance(body, dict):
        raise HubError(400, "bad_body", "请求体必须是一个 JSON 对象")
    if not any(k in body for k in _EDIT_FIELDS):
        raise HubError(400, "bad_body", "至少给一个要改的字段：display_name / persona / fish_voice_id")

    # 字段形状：display_name 各档 → persona → fish_voice_id 三档
    name = _edit_name(body["display_name"]) if "display_name" in body else None
    if "persona" in body and not isinstance(body["persona"], str):
        _bad("persona 必须是字符串")
    voice = _edit_voice(body["fish_voice_id"]) if "fish_voice_id" in body else None
    persona = body.get("persona") if "persona" in body else None
    if persona is not None and len(persona.encode("utf-8")) > MAX_PERSONA:
        raise HubError(413, "payload_too_large", "人设正文超过 64 KiB")

    cfg = _edit_target(bot_id)                  # 404
    yml_path = config_sources.find_path(bot_id)
    if yml_path is None:
        raise HubError(404, "bot_not_found", _EDIT_404)
    channel = _channel_dir(cfg, bot_id)

    # 读旧内容：409 / 500 全在这一步，全部过了才动第一个字节
    plan = []                                   # [(路径, 新字节, 旧字节或 None)]，按写入顺序
    if name is not None:
        try:
            text = _read_bytes(str(yml_path)).decode("utf-8")
        except OSError as e:
            raise HubError(500, "config_unreadable", type(e).__name__)
        except UnicodeDecodeError as e:
            raise HubError(500, "config_unreadable", type(e).__name__)
        from moments import hub_config          # 延迟 import：理由同 set_enabled
        try:
            root = hub_config.compose(text)
        except yaml.YAMLError as e:
            raise HubError(409, "bad_yaml",
                           hub_config.yaml_error_detail(e, "%s.yml 解析失败" % bot_id))
        new_text = hub_config.set_scalar(text, root, "display_name", name, "str")
        plan.append((str(yml_path), new_text.encode("utf-8"), text.encode("utf-8")))

    if voice is not None:
        acc_path = os.path.join(channel, "access.json")
        try:
            raw = _read_bytes(acc_path)
        except FileNotFoundError:
            raise HubError(409, "bad_access_json", _EDIT_BAD_ACCESS)
        except OSError as e:
            raise HubError(500, "config_unreadable", type(e).__name__)
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            obj = None
        if not isinstance(obj, dict):
            raise HubError(409, "bad_access_json", _EDIT_BAD_ACCESS)
        if voice:
            obj["voiceId"] = voice              # 已存在则原位替换、不存在则追加末尾
        else:
            obj.pop("voiceId", None)
        plan.append((acc_path, (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"), raw))

    if persona is not None:
        md_path = os.path.join(channel, "CLAUDE.md")
        try:
            old_md = _read_bytes(md_path, missing_ok=True)
        except OSError as e:
            raise HubError(500, "config_unreadable", type(e).__name__)
        plan.append((md_path, persona.encode("utf-8"), old_md))

    # 写入：顺序固定 yml → access.json → CLAUDE.md → 朋友圈库
    undo = []                                   # 逆序恢复清单（无参 callable）
    try:
        for path, new_bytes, old_bytes in plan:
            _atomic_bytes(path, new_bytes)
            undo.append(lambda p=path, b=old_bytes: (_unlink(p) if b is None
                                                     else _atomic_bytes(p, b)))
        if name is not None:
            _save_db_name(cfg, bot_id, name, undo)
    except Exception as e:                      # noqa: BLE001 下面按契约翻成 500
        partial = _restore_writes(undo)
        raise HubError(500, "config_write_failed", _write_detail(e, partial))
    return {"ok": True,
            "saved": {"display_name": name is not None, "persona": persona is not None,
                      "fish_voice_id": voice is not None},
            "text": _EDIT_TEXT}, 200
