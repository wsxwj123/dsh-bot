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
import datetime
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
from collections import namedtuple
from urllib.parse import urlsplit

import requests
import yaml
from flask import current_app

from moments import bots_client, config_sources, hub_auth, life_settings, redact, skill_templates
from moments import features as feature_switch
from moments.provider_model import HubError

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_API_BASE = "https://api.telegram.org"
#: §4.1 R2-1：主机 + 可选端口，**不带路径、不带 query**。
BASE_RE = re.compile(r"^https?://[A-Za-z0-9.\-]+(:\d+)?/?\Z")
#: 小写起头；禁 `_` 开头（`list_enabled_bots()` 会跳过它，建了也看不见）。
#: 形状那条正则全仓库只有一份，定义在 `config_sources`（整串匹配），这里是引用。
BOT_ID_RE = config_sources.BOT_ID_RE
#: 本文件的形状正则一律以 `\Z` 结尾（整串匹配，§0 第 6 条）：`$` 会放过末尾多出来的一个换行，
#: 带换行的 token 就会被拼进请求 URL 发出去。新加正则照这个写法，调用处用 `.match` 即可。
TOKEN_RE = re.compile(r"^\d{8,10}:[A-Za-z0-9_-]{30,}\Z")
#: A7：既有 bot 的 telegram id = `.env` 里 token 的 `:` 前数字段，不新增字段、不落第二份 token
ENV_TOKEN_RE = re.compile(r"^\s*TELEGRAM_BOT_TOKEN\s*=\s*[\"']?(\d{8,10}):", re.M)
#: 上游 description 里的请求 URL 片段 `/bot<token>/getMe`：抹掉 token 后仍留着 `/bot…`，
#: 那还是把「原始请求 URL」回显给了页面（S6 明令不许），所以整段一并删掉。
URL_PATH_RE = re.compile(r"/bot\S*")

GETME_TIMEOUT = 10               # §4.1
MAX_BODY = 64 * 1024             # §4.1 R2-2：读满即停
#: 人设正文的上限，按**字符数**算（§0 第 9 条），新建与编辑同一把尺。此前新建直接给的正文
#: 与编辑按 UTF-8 字节数、从模板取的正文按字符数，三处两种算法。
MAX_PERSONA = 65536
_PERSONA_TOO_LONG = "人设正文超过 65536 个字符"
MAX_DISPLAY_NAME = 64            # R8-1
# §6b 两个可选字段的形状（全是本地判据，不做在线校验 —— 不因它们调任何语音服务）
MAX_FISH_VOICE = 64
MIN_FISH_KEY = 8
MAX_FISH_KEY = 256
FISH_VOICE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\Z")
FISH_KEY_RE = re.compile(r"^[\x21-\x7e]{8,256}\Z")
#: 把 Telegram token / 供应商 key 填错格子是最常见的两种手滑，各给一句人话
FISH_KEY_TELEGRAM_RE = re.compile(r"^\d{8,10}:[A-Za-z0-9_-]{30,}\Z")
REQUIRED_FIELDS = ("bot_id", "telegram_token", "dispatcher_port", "chat_id")
BLANK_TEMPLATE = "_persona_template"
#: 新建 bot 的 access.json 默认按空行分段、段间隔 1200ms（与 bot4/wenya 一致）
DEFAULT_PARAGRAPH_DELAY = 1200
#: 内置的通用人设模板（不需要先有别的 bot）。与空白模板一样用下划线开头，不会和真实编号撞
PERSONA_TEMPLATE = "_persona_basic"
PERSONA_TEMPLATE_LABEL = "基础人设（通用模板）"


def _persona_template_text():
    """内置基础人设模板的正文（仓内 templates/persona.md）。读不到按空白模板处理，不能让新建卡在这里。"""
    try:
        with open(os.path.join(REPO_ROOT, "templates", "persona.md"), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""
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
#: 表只有一份，定义在 `config_sources`（§5b 第 5 条），这里是引用。
RESERVED_UNIT_NAMES = config_sources.RESERVED_UNIT_NAMES
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
    out = [{"id": n, "display_name": "空白模板" if n == BLANK_TEMPLATE else labels.get(n) or n}
           for n in sorted(names) if n != PERSONA_TEMPLATE]
    out.insert(0, {"id": PERSONA_TEMPLATE, "display_name": PERSONA_TEMPLATE_LABEL})
    return out


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


# 只用 ASCII 数字：Python 的 \d 默认匹配全角等 Unicode 数字，"１２３" 会被悄悄当成 123
_INT_STR_RE = re.compile(r"-?[0-9]{1,20}")


def _int_like(value):
    """整数，或「纯十进制数字的字符串」→ int；其余 → None。

    网页的输入框读出来天生是字符串，前端虽然会转数字，但浏览器缓存旧页面、复制粘贴
    带不可见字符等情况下会漏过来（2026-10-27 真机：dsh 新建页反复报「chat_id 要是整数」）。
    校验仍以 int 为准，只是把「字符串形态的数字」在服务端再收一次；解析后照常过范围检查。
    """
    if _is_int(value):
        return value
    if isinstance(value, str) and _INT_STR_RE.fullmatch(value.strip()):
        return int(value.strip())
    return None


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


def _requested_features(body):
    """可选 ``features`` 数组 → 按九个键的固定顺序排好的键元组；键缺失等于九个全不勾。

    不是数组、含未知键、有重复项一律 400 ``bad_body``（此刻一个文件都没建）。键名原样比对，
    不 trim、不认大小写 —— 与适配接口 5.18.1 同一把尺。
    """
    if "features" not in body:
        return ()
    raw = body["features"]
    if not isinstance(raw, list) or len(set(map(repr, raw))) != len(raw) \
            or not all(isinstance(k, str) and k in feature_switch.KEY_SET for k in raw):
        raise HubError(400, "bad_body",
                       "features 要是功能键的数组，不重复，只能用 voice、image、stickers、moments、"
                       "proactive、life、relationship、groups、nsfw")
    return tuple(k for k, _label in feature_switch.KEYS if k in raw)


def _feature_ready(fields):
    """三个有就绪条件的功能此刻齐不齐（与适配接口 5.19.2 同口径）：voice 要音色编号、
    image 要 NovelAI 令牌、life 要 ``life_target`` 这次真能给出生活设定文件；其余恒可用。"""
    from moments import adapter_service      # 延迟 import：它反过来 import 本模块
    return {"voice": bool(fields.get("fish_voice_id")),
            "image": adapter_service._has_novelai_token({}),
            "life": life_target(fields["bot_id"]) is not None}


def _plan_features(fields):
    """→ ``(九个最终布尔, features_skipped)``：勾了且就绪的开；勾了但缺就绪项的不打开、
    列进跳过项；没勾的关。跳过项形状同适配接口（``[{key, missing:[{code,message}]}]``）。"""
    from moments import adapter_service      # 跳过项的码与文案跟适配接口同一份
    ready = _feature_ready(fields)
    final, skipped = {}, []
    for key, _label in feature_switch.KEYS:
        ticked = key in fields["features"]
        final[key] = ticked and ready.get(key, True)
        if ticked and not ready.get(key, True):
            code, message = adapter_service.REQUIREMENT_TEXT[key]
            skipped.append({"key": key, "missing": [{"code": code, "message": message}]})
    return final, skipped


def _name_char_banned(ch):
    """显示名里不许出现的码位（§0 第 9 条）：U+0000–U+001F、U+007F–U+009F、U+2028、U+2029、
    U+D800–U+DFFF（落单的代理，也就是半个表情符号）、U+FFFE、U+FFFF。

    按这张清单判，不按字符类别判：表情符号、零宽连接符、各种文字一律放行。清单里的码位
    写进 yml 要么被当成换行（U+0085、U+2028、U+2029 读回来变成空格）、要么让整份配置读
    不出来（其余控制字符），落单的代理根本写不成 UTF-8。
    """
    cp = ord(ch)
    return (cp <= 0x1F or 0x7F <= cp <= 0x9F or cp in (0x2028, 0x2029, 0xFFFE, 0xFFFF)
            or 0xD800 <= cp <= 0xDFFF)


def _display_name(value):
    """显示名的规矩，**新建与编辑共用这一条**（§0 第 9 条）。返回去掉首尾空白之后的值。

    先去掉首尾空白，然后必须是 1 到 64 个字符，并且不含 `_name_char_banned` 那张清单里的
    码位。它会进 yml、进页面渲染、可能进 plist 的 Label（R8-1）。落盘用的是返回值。
    """
    if isinstance(value, str):
        name = value.strip()
        if 1 <= len(name) <= MAX_DISPLAY_NAME and not any(_name_char_banned(ch) for ch in name):
            return name
    _bad("display_name 必须是 1-64 字的字符串，且不含控制字符")


def _persona_encodable(text):
    """人设正文得写得成 UTF-8（§0 第 9 条）：有落单的代理（半个表情符号）就回 400。
    此前这种正文一路走到写文件那一步才炸，冒成 500。"""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        _bad("人设正文里有残缺的字符（多半是半个表情符号），删掉它再保存")


def _persona_within_limit(text, detail=_PERSONA_TOO_LONG):
    """人设正文最多 `MAX_PERSONA` 个字符（§0 第 9 条），超过回 413。`detail` 只有从模板取
    正文时换一句（让用户知道该换模板，而不是去删自己没写过的字）。"""
    if len(text) > MAX_PERSONA:
        raise HubError(413, "payload_too_large", detail)


def _persona_text(text, too_long=_PERSONA_TOO_LONG):
    """人设正文的两项检查，**新建与编辑同一把尺**：写得成 UTF-8、不超过 65536 个字符。

    新建直接给的正文与从模板取的正文一步查完。编辑要把音色的形状错夹在两项之间
    （§10b 的判定顺序：字段形状的 400 全部先于 413），所以分两步调上面那两个。
    规则与文案各只有那一份。
    """
    _persona_encodable(text)
    _persona_within_limit(text, too_long)
    return text


def _readback(text, expect):
    """**写配置之前先读回来核对**（§0 第 9 条）：`text` 是准备写的那份配置全文，`expect` 是
    这次写进去的字符串键值。解析不出、顶层不是映射、或哪个键读回来不是同一个字符串，
    就抛 400，调用方据此不写盘。

    这是兜底：`_display_name` 的码位清单漏掉的由它拦住。只用管理台这一侧的解析器
    （PyYAML），网关用的是另一个，这里过了不等于那边一定读得一样。
    """
    try:
        data = yaml.safe_load(text)
    except (yaml.YAMLError, ValueError):
        data = None
    same = isinstance(data, dict) and all(
        isinstance(data.get(key), str) and data.get(key) == value for key, value in expect.items())
    if not same:
        _bad("这份配置写出来之后读不回原样，多半是名字里有特殊字符")


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
    life = life_target(bot_id)
    if life is not None and os.path.lexists(life[1]):
        raise HubError(409, "bot_exists", LIFE_TAKEN % bot_id)


#: 生活设定目录里已有同名文件时的那一句（新建页 409 bot_exists、适配服务 409 exists 共用）
LIFE_TAKEN = "生活设定目录里已有同名文件 %s.yml，没有新建；换个名字或先手动清理"


def life_target(bot_id):
    """新 bot 的生活设定文件该落在哪：``(允许目录写法, <允许目录>/<bot_id>.yml)``。允许目录的写法本身不合格
    （落在禁区里、不是绝对路径），或目录已在但真实路径不合格（软链绕进禁区），回 None：这只就不生成生活设定
    文件、配置不写 life_config（同改动前）。目录还不在的，由 ``lay_down`` 建出来后再复核一次。

    ``bot_id`` 必须已经过编号字符规则（新建页 ``BOT_ID_RE`` / 适配服务 ``BOT_ID_RE``，都不含路径分隔符与
    ``..``），这里只拼路径不再校验。查重（含软链：``lexists``）与落盘共用这一处，保证查的就是要写的那个位置。
    """
    folder = life_settings.planned_dir()
    if folder is None:
        return None
    if os.path.lexists(folder):
        allowed = life_settings.allowed_dir()
        if allowed is None or allowed[0] != folder:
            return None
    return folder, os.path.join(folder, bot_id + ".yml")


def _planned_life(bot_id, yml_text):
    """``lay_down`` 要生成的生活设定文件：核对过的配置全文里 ``life_config`` 写的那个 → ``(目录, 路径)``；
    没写回 None。写了却不是 ``life_target`` 此刻给的那个位置（渲染之后环境变了）就拒绝，不往别处写。"""
    written = (yaml.safe_load(yml_text) or {}).get("life_config")
    if written is None:
        return None
    life = life_target(bot_id)
    if life is None or written != life[1]:
        raise HubError(500, "internal", "生活设定目录在校验之后变了，没有新建")
    return life


#: 人设里尖括号占位符的形状（``<角色名>`` 这类，不含尖括号嵌套与换行）
_PLACEHOLDER_RE = re.compile(r"<[^<>\n]+>")


def _persona_summary(text):
    """人设正文 → 摘要，按接口约定的顺序：去 HTML 注释（跨行非贪婪）→ 去行首标题符与紧跟的
    空格 → 去 ``**`` 与反引号 → 每行去首尾空白 → 连续空行压成一个换行 → 整体去首尾空白 →
    取前 300 个 Unicode 字符（不是字节；Python 的 str 就是码点，截断不补省略号）→ 按这 300 字
    算尖括号占位符占比，超过一半视为没填，返回空串（模板里满是 ``<角色名>`` 的不当摘要用）。

    行首的 ``-`` / ``*`` / ``>`` 保留：能看出人设的分条结构。"""
    if not text:
        return ""
    cleaned = re.sub(r"<!--.*?-->", "", str(text), flags=re.S)
    lines = []
    for line in cleaned.splitlines():
        # C0/C1 控制字符（NUL、ESC 等）会让 PyYAML 解析块标量时直接抛错，
        # 文件读不出来的话这只 bot 的 jiwen tick 会整条停掉，所以先剔掉
        line = re.sub(r"[\x00-\x08\x0e-\x1f\x7f-\x84\x86-\x9f]", "", line)
        line = re.sub(r"^#+[ \t]*", "", line)       # 标题符与紧跟的空格（只去行首，不吃换行）
        line = line.replace("**", "").replace("`", "")
        lines.append(line.strip())
    # 连续多个空行压成一个空行（保留段落分隔）；单个空行不动
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    summary = cleaned[:300]
    placeholder_chars = sum(len(m) for m in _PLACEHOLDER_RE.findall(summary))
    if placeholder_chars * 2 > len(summary):        # 先截断再算占比：长模板后面的正文不能把比例摊薄
        return ""
    return summary


def _life_text(fields):
    """新建 bot 的生活设定骨架：``id``、``display_name``、按人设生成的摘要与默认睡觉时段，
    其余字段只留注释（读出来都是空/缺省），用户拿到后直接改。

    ``id`` 是这只的 person（适配服务新建给了 person 时），没有就是编号。显式给了摘要字段就
    用摘要，否则从人设正文提取；人设为空时整条 ``persona_summary`` 不写。"""
    summary = _persona_summary(fields.get("persona_summary") or fields.get("persona"))
    lines = [
        "# 生活设定：新建 bot 时按人设生成的骨架，可以直接改",
        "id: %s" % _yaml_str(fields.get("person") or fields["bot_id"]),
        "display_name: %s" % _yaml_str(fields["display_name"]),
    ]
    if summary:
        lines.append("persona_summary: |")
        lines.extend("  " + line for line in summary.split("\n"))
    lines.extend([
        "sleep_hours:",
        '  - "00:00-07:30"',
        '# bio: ""             # 一句话简介，填了会进生活上下文',
        '# city: ""            # 城市',
        '# birthday: ""        # 生日，形如 "1995-03-15"，满 18 岁才会进生图提示词',
        '# speaking_threshold: |   # 主动开口的分寸，不填就用缺省',
    ])
    return "\n".join(lines) + "\n"


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
        return _persona_text(text)
    if tmpl == PERSONA_TEMPLATE:
        return _persona_text(_persona_template_text())
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
                # 多读一个字符就够判超限，不把一个巨大的文件整个读进来。此前读到上限就停，
                # 等于悄悄把模板截断；现在超限是 413，一个字都不落盘。
                text = f.read(MAX_PERSONA + 1)
        except OSError:
            continue
        except UnicodeDecodeError:
            # 模板文件不是合法 UTF-8：此前冒成 500
            _bad("这个人设模板不是合法的 UTF-8 文本，换一个或直接贴正文")
        return _persona_text(text, "这个人设模板超过 65536 个字符，换一个或直接贴正文")
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
    if isinstance(raw, str):
        # 展开只有 config_sources 那一处（§14）；先去首尾空白是这里既有的口径，保持不变
        written = config_sources.channel_dir({"bot_channel_path": raw.strip()})
        if written:
            paths.append(os.path.join(written, ".env"))
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
    port_num = _int_like(port)
    if port_num is None or not 1024 <= port_num <= 65535:
        _bad("dispatcher_port 必须是 1024-65535 的整数")
    port = port_num
    name = _display_name(body.get("display_name"))      # 落盘用去掉首尾空白之后的值
    chat_id_num = _int_like(chat_id)
    if chat_id_num is None:
        _bad("chat_id 必须是整数")
    chat_id = chat_id_num
    persona = _persona(body)
    # §6b 两个可选字段：判序在人设二选一之后、环境与冲突错（409）之前；两个字段各判各的
    # （四个组合独立落盘，不做成对约束）。
    fish_voice_id = _fish_voice(_optional(body, "fish_voice_id"))
    fish_api_key = _fish_key(_optional(body, "fish_api_key"))
    requested = _requested_features(body)
    out = {"bot_id": bot_id, "token": fields["telegram_token"], "port": port,
           "chat_id": chat_id, "display_name": name, "persona": persona,
           "fish_voice_id": fish_voice_id, "fish_api_key": fish_api_key,
           "features": requested}
    # 九个开关的最终值与跳过项（§5.18.1）：就绪判定在写盘之前，末尾落盘的 yml 用的是这一份
    out["features_final"], out["features_skipped"] = _plan_features(out)
    # 写配置之前先读回来核对（§0 第 9 条）：排在 fish_* 之后、环境与冲突错（409）之前，
    # 此刻一个文件都还没建。核对过的这份全文就是 B 段要落盘的那份，不再渲染第二遍。
    out["yml_text"] = _checked_yml(out)
    # 环境错（用户自己改不了）：新系统配置根被关掉就落不了盘，这一页只建新系统的 bot
    if config_sources.dsh_dir() is None:
        raise HubError(409, "dsh_root_off",
                       "新系统配置根被关掉了（HUB_CONFIGS_DSH_DIR=off），这一页只建新系统的 bot")
    _check_free(bot_id)
    _check_port(port)
    return out


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
    也顺带挡住同名并发创建（R4）。`text` 是 str（按 UTF-8 写）或 bytes（原样写）。

    **这一步自己收尾**（§7）：文件建出来了但内容没写完的，自己把它删掉再把异常抛出去 ——
    落盘的每个文件都经这里，所以「建出来就算进回滚清单」不靠调用方记得登记。此前显示名里
    有半个表情符号时，配置被建成空文件又不在回滚清单里，留下一只空配置的 bot，同名再也建
    不了。文件本来就在（`O_EXCL` 报错）的不是这一步建的，不删。
    """
    data = text if isinstance(text, bytes) else text.encode("utf-8")   # 编不出来就不建文件
    # O_BINARY（只有 Windows 有）：不加的话 CRT 走文本模式，下面按 "wb" 写的 \n 会被悄悄
    # 换成 \r\n —— yml/.env 的内容就不再逐字节等于我们生成的那份。
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                 | getattr(os, "O_BINARY", 0), mode)
    try:
        f = os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)
        _unlink(path)
        raise
    try:
        with f:
            f.write(data)
    except BaseException:
        _unlink(path)
        raise


def _unlink(path):
    with contextlib.suppress(OSError):
        os.unlink(path)


def _rmdir(path):
    # 只删空目录：里面若有别人的东西就留着。回滚里绝不 rm -rf（R8-3）
    with contextlib.suppress(OSError):
        os.rmdir(path)


#: 回滚清单条目类型（R8-3 的精确 unlink 清单）
_MADE_FILE = 0      # yml / 生活设定文件：直接 unlink
_MADE_TREE = 1      # 频道树：删清单内的文件与目录，再 rmdir 树根
_MADE_HOME = 2      # bot 家：只 rmdir（非空一律保留）
_MADE_SKILL = 3     # 技能文件：unlink 后 rmdir 它的 skill-<通用名>/ 目录（空的才删）


def _rollback(made):
    """按创建清单逆序精确删除。回滚过程里再抛异常会把真正的错因盖掉，所以全程吞。

    条目类型见上面的常量：频道树删 `TREE_FILES`/`TREE_DIRS` 清单内的东西，
    bot 家与其它目录一律只 rmdir（空的才删）—— 用户的 bot 家可能早就在那儿，
    一次手滑就是别人的聊天记录没了（R8-3）。技能文件按每条 `_MADE_SKILL` 精确删，
    最后把空掉的 `skills/` 摘掉；不递归删任何目录。"""
    for kind, path in reversed(made):
        if kind == _MADE_FILE:
            _unlink(path)
        elif kind == _MADE_HOME:
            _rmdir(path)
        elif kind == _MADE_SKILL:
            _unlink(path)
            _rmdir(os.path.dirname(path))
        else:
            for rel in TREE_FILES:
                _unlink(os.path.join(path, rel))
            for rel in TREE_DIRS:
                _rmdir(os.path.join(path, rel))
            _rmdir(os.path.join(path, "skills"))
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


def _place_skills(channel, fields):
    """在频道树里放这次勾了的技能，返回 ``[(相对路径, 通用名)]``（相对路径以频道目录为准）。

    勾了的功能都放：缺就绪项没打开的也照放、头部写停用标记，补齐后在功能块打开就能用
    （约定 5.18.1）；relationship、life 没有技能。文件走 `_create_file`（0600、O_EXCL），
    目录 0700。模板读不出照抛，由调用方按整单撤回处理。
    """
    placed = []
    for key in fields["features"]:
        generic = feature_switch.SKILL_OF.get(key)
        if generic is None:
            continue
        text = skill_templates.render(generic, persona=fields["persona"])
        if not fields["features_final"][key]:
            text = feature_switch.set_skill_disabled(text, True)
        skills_dir = os.path.join(channel, "skills")
        if not os.path.isdir(skills_dir):
            os.mkdir(skills_dir, 0o700)
        rel = os.path.join("skills", "skill-" + generic, "SKILL.md")
        target = os.path.join(channel, rel)
        os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
        _create_file(target, text)
        placed.append((rel, generic))
    return placed


def _yaml_str(value):
    """写进 yml 的字符串字面量。`json.dumps` 出的双引号串是 YAML 的合法子集：
    emoji / 引号 / 换行全都不用自己转义，也就没有"手写转义漏一种"的洞。"""
    return json.dumps(value, ensure_ascii=False)


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


def _render_yml(fields, target, namespace=None, life_config=None, features=None):
    """`configs/<bot>.yml`：**不含 token**（F5），只有注册表要的那几个键。

    `id` 与 `display_name` 同一种写法，带双引号（§7）：`null`、`true`、`no`、`on` 这类名字
    形状合法，裸写的话读回来是空值或布尔值，安装脚本拿到的就不是这只的名字了。

    `bot_channel_path` 写**绝对路径**（§7、§14）。此前能收进 `~` 就收成 `~/…`，而管理台里
    有几处拿到值不展开就去拼路径，在工作目录下建出一棵名字叫 `~` 的目录。读的一侧已经
    统一展开（`config_sources.channel_dir`），写的一侧也不再产出那个形状。

    ``life_config``（``life_target`` 给的生活设定文件绝对路径）给了才写一行，``lay_down`` 照它生成那个文件。
    ``features``（九个键的布尔表）给了才写 ``features:`` 块，九个键显式写出（5.19.4）；适配接口自己
    拼这块，不传这个参数，产出逐字节不变。
    """
    text = ("id: %s\n"
            "display_name: %s\n"
            "bot_channel_path: %s\n"
            "chat_id: %d\n"
            "dispatcher_port: %d\n"
            "namespace_uuid: %s\n"
            % (_yaml_str(fields["bot_id"]), _yaml_str(fields["display_name"]),
               _yaml_str(os.path.abspath(target)),
               fields["chat_id"], fields["port"], namespace or _new_namespace()))
    if life_config:
        text += "life_config: %s\n" % _yaml_str(life_config)
    if features is not None:
        # json.dumps 出来的 ``{"voice": false, …}`` 是合法 YAML，键序就是九个键的固定顺序
        text += "features: %s\n" % json.dumps(features, ensure_ascii=False)
    return text


def _checked_yml(fields):
    """渲染这只 bot 的配置全文并**读回来核对**（§0 第 9 条），返回核对过的那份文本。

    核对的是这次写进去的四个字符串键。频道目录直接由新系统家目录拼出，不依赖配置根在
    不在场，所以这一步可以排在 409 `dsh_root_off` 之前。对不上抛 400，一个文件都没建。
    """
    target = os.path.join(str(config_sources.dsh_bot_dir(fields["bot_id"])), "channel")
    namespace = _new_namespace()
    life = life_target(fields["bot_id"])
    features = fields.get("features_final")
    text = _render_yml(fields, target, namespace, life and life[1], features)
    expect = {"id": fields["bot_id"], "display_name": fields["display_name"],
              "bot_channel_path": os.path.abspath(target), "namespace_uuid": namespace}
    if life:
        expect["life_config"] = life[1]
    _readback(text, expect)
    if features is not None and (yaml.safe_load(text) or {}).get("features") != features:
        raise HubError(500, "internal", "功能开关写进配置之后读不回原样")
    return text


def lay_down(fields, port_check=None, place_skills=False):
    """B 段：tmp 里建好整棵树 → 一次 `move` 到 `<bot 家>` → 写 yml → 复查端口。

    落点全在新系统那套位置（§7）：配置进 `<dsh 配置根>/<bot>.yml`，频道树进
    `<DSH_BOT_HOME>/bots/<bot>/channel/`；旧系统的两个根一个字节都不产生。

    任一步失败：按精确清单逆序删干净，磁盘回到操作前（可断言 yml 不在、bot 家不在、
    注册表查不到它），然后把异常原样抛给调用方定码。回滚只删清单内的东西，
    非空目录一律保留 —— 用户的 bot 家可能早就在那儿，一次手滑就是别人的聊天记录没了。

    ``port_check``：落盘之后复查端口用的函数（抛错即回滚）。不给是管理台这一页的 `_check_port`
    （查新旧两套配置）；适配服务只许读本系统，传它自己只查本系统的那一份（补充文件 12.2）。

    ``place_skills``：管理台新建页传 True，按 ``fields["features"]`` 在 tmp 树里先放好技能
    （随频道目录一次 move 就位，失败按 `_MADE_SKILL` 精确删）。适配服务自己放技能，不传，行为不变。
    """
    bot_id = fields["bot_id"]
    home = str(config_sources.dsh_bot_dir(bot_id))
    channel = os.path.join(home, "channel")
    cfg_root = config_sources.dsh_dir()
    if cfg_root is None:
        raise HubError(409, "dsh_root_off", "新系统配置根被关掉了，落不了盘")
    # 要落盘的配置全文：A 段核对过的那份（`_validate` 放进来的）。没经 A 段直接调到这里的，
    # 在建任何文件之前现渲染、现核对 —— 没读回来核对过的文本不许落盘。
    yml_text = fields.get("yml_text") or _checked_yml(fields)
    # 生活设定文件（两条新建路径都经这里）：核对过的配置里写了 life_config 就照它生成那个文件，没写就不生成。
    # 查重在调用方的 409 阶段已做过，这里再看一眼只防并发；真正挡同名的是下面 O_EXCL 建文件。
    life = _planned_life(bot_id, yml_text)
    if life is not None and os.path.lexists(life[1]):
        raise HubError(409, "bot_exists", LIFE_TAKEN % bot_id)
    # 这两级目录可能本来就是有的（新系统早就在跑）；只记下"哪些是这次才建出来的"，
    # 失败回滚时把它们摘掉 —— 否则一次失败的创建会在新系统家目录里留一个空的 bots/。
    seeded = [d for d in (str(cfg_root), os.path.dirname(home)) if not os.path.isdir(d)]
    if life is not None and not os.path.isdir(life[0]):
        seeded.append(life[0])
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
        # 按空行分段的两个默认值：老 bot（bot4/wenya）都是 true + 1200，新建的原来没写、
        # 多句话就挤在一个气泡里（2026-10-27 真机）。「新建 bot 继承所有功能」要求这里默认带上。
        access = {"allowFrom": [fields["chat_id"]], "splitOnParagraph": True,
                  "paragraphDelay": DEFAULT_PARAGRAPH_DELAY}
        if fields.get("fish_voice_id"):
            access["voiceId"] = fields["fish_voice_id"]      # 键序 allowFrom 在前、voiceId 在后
        _create_file(os.path.join(chan, "access.json"),
                     json.dumps(access, ensure_ascii=False) + "\n")
        _create_file(os.path.join(chan, "CLAUDE.md"), fields["persona"])
        _create_file(os.path.join(chan, "memory", "MEMORY.md"), MEMORY_SEED)
        # 技能也先在 tmp 树里放好（管理台新建页）：随下面那一次 move 一起就位，
        # move 失败时目标位置一个字节都没写过，tmp 整个删掉即可。
        skill_rels = _place_skills(chan, fields) if place_skills else []
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
        if place_skills:
            # 技能文件已随树就位：在清单里逐条登记，回滚时先删文件、再摘空的 skill-目录
            for rel, _generic in skill_rels:
                made.append((_MADE_SKILL, os.path.join(channel, rel)))
            fields["skills_placed"] = [generic for _rel, generic in skill_rels]
        _fix_modes(home)
        if life is not None:
            os.makedirs(life[0], mode=0o700, exist_ok=True)
            if life[0] in seeded:
                os.chmod(life[0], 0o700)          # 这次才建的目录：不受 umask 影响，就是 0700
            allowed = life_settings.allowed_dir()
            if allowed is None or allowed[0] != life[0]:
                # 刚建出来的目录真实路径不合格（上级是绕进禁区的软链）：配置已写了 life_config，整单撤回
                raise HubError(500, "internal", "生活设定目录的真实位置不在允许范围内，没有新建")
            _create_file(life[1], _life_text(fields))          # 0600，O_EXCL：同名不覆盖
            made.append((_MADE_FILE, life[1]))
            fields["life_file"] = life[1]         # 适配服务的整单撤回（_undo_create）靠它一并移走
        _create_file(yml, yml_text)               # 没写完它自己删，不会留下空配置
        made.append((_MADE_FILE, yml))
        if port_check is None:
            _check_port(fields["port"], skip=bot_id)   # R4：A 段那次 bind 试探到这里已经有窗口
        else:
            port_check()
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
    """把该 bot **新系统配置根下那份** yml 的顶层 `enabled` 改成布尔值，返回改完的值。

    停用的唯一标记就是这个字段（`bots_registry.is_enabled` 判它），所以这里也是**唯一**
    的写入口。四条硬约束：

    - **先过放行闸，只写新系统那份**（INTERFACE §0 第 2 条 / §4b）：第一步就是
      `_edit_target`，旧系统的 bot、来源读不出的、名单注入的、不存在的一律 404，
      走不到读写文件那一步。写的恒是新系统配置根下的 `<bot_id>.yml`（`_dsh_yml` 直接
      拼出）；旧系统配置根下的文件一个字节不动，同名时也不动 —— 那边的 bot 归另一套
      系统、由别人维护。
    - **绝不 dump YAML**：走 `hub_config.set_scalar` 的行级编辑，注释/锚点/键序/块标量
      逐字节保真（§2.4 那条可 grep 的红线，bot 的 yml 里全是人设正文，dump 一次就毁）。
    - **原子写**：复用 `hub_config.atomic_write`，半截文件不可能出现 —— bot 的 yml 写坏了
      注册表就整个跳过它，等于把 bot 弄丢。
    - **幂等**：已经是这个值就原样写回同一份文本（`set_scalar` 只换那一段字节，
      重复调用产出完全相同的文件）。

    `enabled` 键不存在时走 `set_scalar` 的插入分支（按父块缩进插在块首）。
    """
    from moments import hub_config          # 延迟 import：只有这一个函数要它
    _edit_target(bot_id)                    # 400 / 404 都由放行闸给；归属先于任何文件读写判完
    if enabled and os.path.lexists(os.path.join(str(config_sources.dsh_bot_dir(bot_id)), ".moved")):
        # 适配服务 13.2：已迁到另一种底层的 bot 不许从这里启用（标记在就拦，不管内容）；停用照常
        raise HubError(409, "moved", "这只 bot 已标记迁到另一种底层，不能在这边启用；要改回来，先发一次带 moved_to: null 的停用清掉迁出标记")
    path = _dsh_yml(bot_id)
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:               # 过闸之后文件被挪走：同一句 404，不换地方找
        raise HubError(404, "bot_not_found", GATE_404_DETAIL)
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


def _register_dsh(fields, deadline=None, stashed=False):
    """macOS：交给 `gateway/scripts/autostart.ts` 装网关单元与主动消息单元（§6 register）。

    两条命令**短路**：install 非 0 即止（install-jobs 不跑）。子进程原始输出（含 launchd
    报错原文）一个字不进 `detail`/`manual_hint`（S9）—— 只给机读码与固定命令文本。
    PATH 前置 `~/.pyenv/shims`（目录存在时），让 autostart.ts 的 `Bun.which('python3')`
    解析到 pyenv 那支，与既有 dsh bot 单元口径一致；shims 不在的机器自然回落。

    ``deadline``（``time.monotonic()`` 的时刻）：给了就让每条命令最多跑到那时，到点还没跑的那条
    不再起、按 ``timeout`` 失败（适配服务整条请求 55 秒封顶，补充文件 12.2）；不给照旧每条 60 秒。
    ``stashed``：两条命令都带 ``--stashed``，启动项文件只写成收起的 ``.plist.disabled``，不装载、不启动
    （适配服务 13.3 新建时 ``enabled`` 为 false）。
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
        timeout = INSTALL_TIMEOUT
        if deadline is not None:
            timeout = min(INSTALL_TIMEOUT, deadline - time.monotonic())
            if timeout <= 0:
                return _step("register", "failed", "timeout", manual_hint=hint)
        try:
            rc = subprocess.run([bun, AUTOSTART_TS, sub, yml] + (["--stashed"] if stashed else []),
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env=env, timeout=timeout).returncode
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
            "steps": steps, "restart_hint": dict(RESTART_FLAGS_DONE),
            # 约定 5.18.1：九个最终开关、跳过项、放了的技能通用名；原有键的形状不变
            "features": fields["features_final"],
            "features_skipped": fields["features_skipped"],
            "skills": fields.get("skills_placed") or []}
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
        lay_down(fields, place_skills=True)
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
#: 放行闸的 404 只有这一句（§4b 第 3 格 / §10）：开关、编辑、单只重启逐字相同，
#: 旧系统的 bot 与不存在的 bot 不区分。
GATE_404_DETAIL = "没有这个新系统的 bot（这一页只管新系统的 bot）"
_EDIT_BAD_ACCESS = "access.json 读不出内容（不存在或解析失败），先修好"
#: 落点校验命中时的那一句（§10，逐字）
BAD_CHANNEL_PATH_DETAIL = ("频道目录不在这只 bot 的新系统家目录里（bot_channel_path 或软链指到了外面），"
                           "这一页不读也不改它")
#: 人设文件读得出字节、但不是合法 UTF-8 时读接口的那一句（§10a，逐字）
_EDIT_BAD_PERSONA = "人设文件不是合法的 UTF-8 文本，这一页读不了它；先把文件修好再来编辑"
#: 人设备份（§10「人设备份」）：与人设同一个目录，名字是 `CLAUDE.md.bak.<时间戳>`，最多留 2 份。
#: 时间戳的形状与 `hub_config.make_backup` 同一套（年月日-时分秒，同秒重名加 `-01` 到 `-99`）；
#: 正则在这里另写一条是因为本文件的形状正则一律以 `\Z` 结尾 —— 那边那条是 `$`，
#: 名字末尾多一个换行的文件也会被它认成备份，清理时就可能删错。
PERSONA_BAK_PREFIX = "CLAUDE.md.bak."
PERSONA_BAK_KEEP = 2
PERSONA_BAK_ID_RE = re.compile(r"^\d{8}-\d{6}(?:-\d{2})?\Z")
#: `_edit_open` 交回的东西：闸交回的配置、yml 路径、yml 原文、节点树、yml 自己的错（或 None）、
#: 频道目录的真实路径
_Edit = namedtuple("_Edit", "cfg yml_path text root yml_err channel")


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
    """**放行闸**（INTERFACE §0 第 2 条）：开关与编辑共用这一份，不许再写第二份判定。

    过闸 = `bots_client.bot_sources()` 给出这只、且它生效的那份配置来自新系统（dsh）。
    旧系统的 bot、来源读不出的、`HUB_BOTS_FILE` 注入名单的、压根没这只的、id 是保留名的
    —— 一律 404 `bot_not_found`、同一句 detail；调用方据此**不写盘、不起进程、不登记 job**。

    返回这只生效的那份配置，来源必为 dsh。判定与取配置是两次读：中间文件若被改、
    第二次拿到的不是新系统那份，按不过闸算 —— 否则编辑会把人设写进旧系统那只的频道目录。

    只有一个补充口越过 `bot_sources()`：新系统配置根下有这只的 yml、来源却判不出来
    （`bot_sources()` 会跳过坏配置，直接判死会让契约里的 409 `bad_yaml` / 500
    `config_unreadable` 永远不可达），**并且旧系统配置根下没有同名的东西**。这时放它走到
    读文件那一步、返回 None（channelDir 与朋友圈身份各自有回落）：这只 bot 只在新系统
    那边有文件，怎么走都碰不到旧系统。旧系统那边有同名的不走这个口 —— 归属判不出来，
    宁可 404。
    """
    if not isinstance(bot_id, str) or not BOT_ID_RE.match(bot_id):
        raise HubError(400, "bad_bot_id", "bot id 形状非法")
    # 保留名不是 bot（§0 第 7 条）：新系统配置根下就算有一份它的配置也不过闸，
    # 判在这里，下面的「坏文件」补充口对它同样到不了。
    if bot_id in RESERVED_UNIT_NAMES or os.environ.get("HUB_BOTS_FILE"):
        raise HubError(404, "bot_not_found", GATE_404_DETAIL)
    source = (bots_client.bot_sources().get(bot_id) or {}).get("source")
    if source == config_sources.SOURCE_DSH:
        cfg = config_sources.effective_config(bot_id)
        if cfg and cfg.get("_source") == config_sources.SOURCE_DSH:
            return cfg
    elif not source:
        name = bot_id + ".yml"
        d = config_sources.dsh_dir()
        # lexists 而不是 isfile：旧系统那边同名的哪怕是目录、断掉的软链，也算「有同名」
        if (d is not None and os.path.isfile(os.path.join(str(d), name))
                and not os.path.lexists(os.path.join(str(config_sources.legacy_dir()), name))):
            return None
    raise HubError(404, "bot_not_found", GATE_404_DETAIL)


def _dsh_yml(bot_id):
    """过闸之后要读写的那份 yml：恒为新系统配置根下的 `<bot_id>.yml`（§0 第 2 条）。

    直接由新系统配置根拼出，不经 `config_sources.find_path` —— 那个定位是「在跑的那份
    优先」，会回落到旧系统那份。配置根此刻被关掉、或那里没有这个文件，一律 404：
    宁可拒绝，也不换个地方找。
    """
    d = config_sources.dsh_dir()
    path = os.path.join(str(d), bot_id + ".yml") if d is not None else None
    if path is None or not os.path.isfile(path):
        raise HubError(404, "bot_not_found", GATE_404_DETAIL)
    return path


def _channel_dir(bot_id, root):
    """§10 的 channelDir 连同**落点校验**：读与写共用这一处，交出去的是可以放心读写的目录。

    取法。新系统配置根下这只的 yml **自己**顶层写的 `bot_channel_path`（expanduser）；
    没写、为空、不是字符串、或这份 yml 读不出结构（`root` 为 None）→ 家目录下的 `channel`。
    与网关 `config.ts:286` 同款，只认这份 yml 自己的键：`life_config` 指向的那份里写的
    不参与。此前取的是两份合并之后的值，那个目录可以在新系统家目录之外，而且与网关读的
    不是同一处。

    落点。把新系统家目录解析成真实路径，这只 bot 的家 = 它下面的 `bots/<bot_id>`。频道目录
    解析成真实路径（软链跟到底）之后必须就是这个家或在它里面，否则 409 `bad_channel_path`。
    显式写到外面的、经软链出去的、`bots/<bot_id>` 这一级本身是软链的，都落在这一格；
    名字以这只打头的另一个目录（`<bot_id>_x`）不算在里面，所以比的是带分隔符的前缀。

    返回**解析后的真实路径**：校验的是哪条路径，随后读写的就是哪条。
    """
    raw = _top_str(root, "bot_channel_path").strip()
    home = os.path.join(os.path.realpath(str(config_sources.dsh_home())), "bots", bot_id)
    written = config_sources.channel_dir({"bot_channel_path": raw})   # 展开只有那一处（§14）
    channel = os.path.realpath(written or os.path.join(home, "channel"))
    if channel != home and not channel.startswith(home + os.sep):
        raise HubError(409, "bad_channel_path", BAD_CHANNEL_PATH_DETAIL)
    return channel


def _edit_open(bot_id):
    """编辑的读与写共用的前半段，判定顺序照 §10：404 → `bad_channel_path` → yml 自己的错。

    过放行闸 → 定位新系统那份 yml → 读它、解析成节点树 → 定频道目录并校验落点。
    yml 读不出（500）或解析不出（409）时**先不抛**：这种时候目录按默认位置算，落点那一格
    照判，两种错互不遮挡。yml 的错放进返回值，由调用方在用得着这份 yml 时再抛 ——
    只改人设与音色用不着它，yml 坏了照样能保存。

    落点不过就在这里抛。此刻只读过这一份 yml：频道目录下的文件一个没碰，什么都没写，
    朋友圈库也没动。
    """
    from moments import hub_config              # 延迟 import：理由同 set_enabled
    cfg = _edit_target(bot_id)
    yml_path = _dsh_yml(bot_id)
    text = root = yml_err = None
    try:
        text = _read_bytes(yml_path).decode("utf-8")
        root = hub_config.compose(text)
    except (OSError, UnicodeDecodeError) as e:
        yml_err = HubError(500, "config_unreadable", type(e).__name__)
    except yaml.YAMLError as e:
        yml_err = HubError(409, "bad_yaml",
                           hub_config.yaml_error_detail(e, "%s.yml 解析失败" % bot_id))
    return _Edit(cfg, yml_path, text, root, yml_err, _channel_dir(bot_id, root))


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

    判定顺序：404 → 频道目录落点（409 `bad_channel_path`）→ yml 解析不出（409）或读不出
    （500）→ 读人设与白名单（读不出 500）→ 人设不是合法 UTF-8（409 `bad_persona`）。
    落点没过之前不碰频道目录下的任何文件。坏 access.json、缺人设文件都回空串 ——
    保存时才拦（§10b 的 409 条件是"要写那个字段"）。
    """
    ed = _edit_open(bot_id)                     # 404 → bad_channel_path
    if ed.yml_err:
        raise ed.yml_err
    try:
        persona_raw = _read_bytes(os.path.join(ed.channel, "CLAUDE.md"), missing_ok=True)
        access_raw = _read_bytes(os.path.join(ed.channel, "access.json"), missing_ok=True)
    except OSError as err:
        raise HubError(500, "config_unreadable", type(err).__name__)
    if persona_raw is None:
        persona = ""
    else:
        try:
            persona = persona_raw.decode("utf-8")
        except UnicodeDecodeError:
            # 读得出字节、只是不是文本：不是「读不了文件」的 500，给一句人话让用户去修文件
            raise HubError(409, "bad_persona", _EDIT_BAD_PERSONA)
    return {"bot_id": bot_id, "display_name": _top_str(ed.root, "display_name"),
            "persona": persona, "fish_voice_id": _voice_of(access_raw)}


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


def _backup_persona(md_path, old_bytes):
    """改写人设之前留一份（§10「人设备份」），返回备份文件的路径。**建备份只有这一个入口。**

    人设是长文本，被错的内容盖掉之后没处找回。备份放在人设旁边，名字是
    `CLAUDE.md.bak.<本地时间的年月日-时分秒>`，内容是改写前那份的字节，权限恒 0600
    （不跟着原文件放宽）。命名与同秒冲突的处理照 `hub_config.make_backup` 那一套：
    `O_EXCL` 建文件，同一秒里已经有同名的就依次取 `-01` 到 `-99`，绝不盖掉已有的备份。
    建不成（目录不可写、名字用尽）照抛，调用方据此不动人设。
    """
    base = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    for bid in [base] + ["%s-%02d" % (base, i) for i in range(1, 100)]:
        target = os.path.join(os.path.dirname(md_path), PERSONA_BAK_PREFIX + bid)
        try:
            _create_file(target, old_bytes)     # 没写完它自己删，不会留下半截备份
        except FileExistsError:
            continue
        return target
    raise FileExistsError("同一秒内的备份名已用尽")


def _drop_backup(path):
    """失败恢复时把这次请求建的备份删掉（§10b 第 5 条）：「这次没有改动任何文件」要名副其实。
    已经不在了不算错；别的删不掉照抛，由 `_restore_writes` 记成「部分文件已改动」。"""
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)


def _prune_persona_backups(channel):
    """人设备份最多留 `PERSONA_BAK_KEEP` 份：**写盘成功之后**按文件名里的时间戳删最老的。

    只认名字形状对得上的（前缀加合规的时间戳），目录里别的东西一概不碰 —— 手工留的
    `CLAUDE.md.bak`、`CLAUDE.md.bak.manual` 不算备份，不占名额也不会被删。清理失败不
    影响这次保存（人设已经写好了），所以全程吞掉。
    """
    try:
        names = sorted(n for n in os.listdir(channel)
                       if n.startswith(PERSONA_BAK_PREFIX)
                       and PERSONA_BAK_ID_RE.match(n[len(PERSONA_BAK_PREFIX):]))
    except OSError:
        return
    for name in names[:-PERSONA_BAK_KEEP] if len(names) > PERSONA_BAK_KEEP else []:
        _unlink(os.path.join(channel, name))


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
    「不改某个字段」的表达是**不给这个键**：给了就必须是字符串，null 也是 400；没给的键
    对应的那一口一个字节都不动。空的人设正文不接受（要清空人设得去改文件）。

    人设被改写之前先留一份备份（`_backup_persona`），写盘成功之后清理到 2 份；后面哪一步
    失败，恢复时把这次建的备份一并删掉。
    """
    if not isinstance(bot_id, str) or not BOT_ID_RE.match(bot_id):
        raise HubError(400, "bad_bot_id", "bot id 形状非法")
    if not isinstance(body, dict):
        raise HubError(400, "bad_body", "请求体必须是一个 JSON 对象")
    if not any(k in body for k in _EDIT_FIELDS):
        raise HubError(400, "bad_body", "至少给一个要改的字段：display_name / persona / fish_voice_id")

    # 字段形状：display_name 各档 → persona → fish_voice_id 三档
    name = _display_name(body["display_name"]) if "display_name" in body else None
    persona = None
    if "persona" in body:
        persona = body["persona"]
        if not isinstance(persona, str):
            _bad("persona 必须是字符串")
        if not persona.strip():
            # 此前「空串 = 写出 0 字节的人设」：页面读取失败后把空的人设框交上来，人设就没了
            _bad("人设正文不能为空；不想改人设就不要带 persona 这个键")
        _persona_encodable(persona)
    voice = _edit_voice(body["fish_voice_id"]) if "fish_voice_id" in body else None
    if persona is not None:
        _persona_within_limit(persona)          # 413 排在三个字段的形状错之后

    # 404 → 频道目录落点。不论改的是哪个字段都先过落点这一格（只改名字也一样）：
    # 落在这只 bot 的家目录之外就整个拒掉，零读取零写入。
    ed = _edit_open(bot_id)
    channel = ed.channel

    # 读旧内容：409 / 500 全在这一步，全部过了才动第一个字节
    plan = []                                   # [(路径, 新字节, 旧字节或 None)]，按写入顺序
    backup_of = None                            # (人设路径, 旧字节)：这次要不要先留人设备份
    if name is not None:
        if ed.yml_err:                          # 要写名字才用得着这份 yml：读不出 500、解析不出 409
            raise ed.yml_err
        from moments import hub_config          # 延迟 import：理由同 set_enabled
        new_text = hub_config.set_scalar(ed.text, ed.root, "display_name", name, "str")
        _readback(new_text, {"display_name": name})     # 写盘之前读回来核对（§0 第 9 条）
        plan.append((ed.yml_path, new_text.encode("utf-8"), ed.text.encode("utf-8")))

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
        new_md = persona.encode("utf-8")
        plan.append((md_path, new_md, old_md))
        # 人设备份：原来有这个文件、新内容与旧内容的字节不一样才留；没有文件或内容没变都不留
        if old_md is not None and old_md != new_md:
            backup_of = (md_path, old_md)

    # 写入：先留人设备份，再按固定顺序 yml → access.json → CLAUDE.md → 朋友圈库。
    # 备份排在第一个字节之前：它没建成时什么都还没动，直接按「没有改动任何文件」回 500。
    undo = []                                   # 逆序恢复清单（无参 callable）
    try:
        if backup_of:
            bak = _backup_persona(*backup_of)
            undo.append(lambda p=bak: _drop_backup(p))
        for path, new_bytes, old_bytes in plan:
            _atomic_bytes(path, new_bytes)
            undo.append(lambda p=path, b=old_bytes: (_unlink(p) if b is None
                                                     else _atomic_bytes(p, b)))
        if name is not None:
            _save_db_name(ed.cfg, bot_id, name, undo)
    except Exception as err:                    # noqa: BLE001 下面按契约翻成 500
        partial = _restore_writes(undo)
        raise HubError(500, "config_write_failed", _write_detail(err, partial))
    if backup_of:
        _prune_persona_backups(channel)         # 写盘成功之后才清理，留最近 2 份
    return {"ok": True,
            "saved": {"display_name": name is not None, "persona": persona is not None,
                      "fish_voice_id": voice is not None},
            "text": _EDIT_TEXT}, 200
