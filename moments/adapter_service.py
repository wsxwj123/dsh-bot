# -*- coding: utf-8 -*-
"""dsh 这一半的适配服务：把本系统的 bot 用本机 HTTP 接口交给另一套系统的管理台（壳）。

契约：``.devflow/INTERFACE-适配约定-v0.4.md``（两边定稿）与 ``INTERFACE-适配服务.md``（本系统补充）；
生活设定的做法在 ``INTERFACE-生活设定.md``，逻辑在 ``moments/life_settings.py``。
v0.6、v0.7 的启停、切进导入、同跑提示、群设置、``group_turn`` 照 ``INTERFACE-适配约定-v0.7摘录.md``
与补充文件第 9 节；切进导入的水位线与写账本在 ``scripts/import_history.py``。
v0.8 的人设、语音照 ``INTERFACE-适配约定-v0.8-dsh侧.md`` 与补充文件第 10 节；人设的备份、原子写用 ``life_settings`` 那一套。
起服务：仓库根执行 ``python3 -m moments.adapter_service``；测试用 ``create_app()``。
只支持这一种启动方式：监听地址只许回环、令牌文件不在就拒绝启动，这两道检查都在 ``main()`` 里。
``flask run`` 等别的方式拉起 ``create_app()`` 会绕过它们（例如对外监听），不要这样起。

本模块只做三件事：换一种鉴权（令牌文件）、按约定的形状组响应、把错误翻成约定的错误码。
判状态、读生效的模型、经网关改思考强度、重启并等心跳，全部调管理台已有的函数
（``bots_client`` / ``bot_launchd`` / ``config_sources`` / ``gateway_client``），这里不重写。
只有「读 bot 配置」不走管理台那条共用读法，见 ``_own_bots``。

同一个进程里还挂着管理台的三页与它们的接口（``INTERFACE-页面服务.md``，见「管理台三页」一节）：
处理函数就是 8765 上那几个（``hub_routes``），这里只换鉴权（同一个令牌）、按原样路径对名单、
渲染时把导航条换成一行占位注释，别的照 8765 收尾。

几条贯穿全模块的规矩：

- **鉴权先于一切**：没过鉴权时不看路径、不读请求体、不读任何 bot 的数据。
- **路径不交给框架匹配**：框架拿到的是解码、规范化之后的路径，``%2F`` 已经变成斜杠。这里
  直接取请求行里的原样路径逐字节比对，``bot_id`` 不合规就 422，绝不解码后再拼。
- **环境变量每次请求现读**，不缓存。
- 令牌、密钥、聊天号不进任何响应与日志；没预料到的错只记异常类名，对外固定回「内部错误」。
"""
import hashlib
import hmac
import json
import logging
import os
import re
import stat
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import unquote

import yaml
from flask import Flask, Response, current_app, jsonify, request
from werkzeug.exceptions import HTTPException, InternalServerError, RequestEntityTooLarge

import gateway_client
from moments import bot_launchd, bots_client, config_sources, hub_addbot, hub_routes, life_settings, redact
from moments.provider_model import HubError

PREFIX = "/adapter/v1"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 17990
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
DEFAULT_TOKEN_FILE = "~/.config/claude-dsh-console/adapter.token"

EFFORT_LEVELS = ["off", "low", "high", "max"]
CAPABILITIES = {
    "bot.restart.graceful": False,
    "bot.restart.force": True,
    "bot.set_provider": True,
    "bot.reset_provider": False,
    "bot.set_effort": True,
    "provider.subscription": False,
    "provider.per_session": False,
    "provider.switch_new_segment": True,
    "bot.notify": True,
    "bot.life_settings": True,
    "bot.enable": True,
    "bot.switch_in": True,
    "bot.group_chat": True,
    "bot.persona": True,
    "bot.voice": True,
}
DEFAULT_LABEL = "各 bot 配置文件里写的那一组"

#: 约定 5.2 的 bot_id 字符规则（整串匹配）；另外不许是纯点，见 ``_valid_bot_id``。
BOT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}\Z")
#: 请求体上限。五个接口的请求体都是几十个字节，超过这个数的不读、直接按不合规拒绝。
MAX_BODY = 64 * 1024
#: 列表接口向每只 bot 的网关读一次的超时。壳的读操作只等 5 秒，一只 bot 最多读两次。
#: 供应商摘要问每一只也用它。
GATEWAY_READ_TIMEOUT = 2.0
#: 重启从发命令算起的总时限（补充文件 4.5：50 秒内没等到就回 failed；壳等 60 秒）。
RESTART_BUDGET = 50.0
MAX_ROW_WORKERS = 8
#: 换供应商整条请求的时限（补充文件 6.1：55 秒内返回）。测试经 DSH_CONSOLE_PROVIDER_BUDGET 缩短（第 8 节）。
PROVIDER_BUDGET = 55.0
#: 读清单、读原强度最多用掉时限的这一份，写请求至少留下其余的时间（PLAN-v04 第 1 节）：
#: 读拖到最后才回的话，写请求刚发出就到点，只能回「还没确认」。
READ_SHARE = 0.4

#: 投递通知（补充文件 6.2、9.6）：source 的四种取值、text 按 UTF-8 的字节上限、key 的字符数上限
NOTIFY_SOURCES = ("user_moment", "moment_reply", "moment_image", "group_turn")
NOTIFY_MAX_TEXT = 16 * 1024
NOTIFY_MAX_KEY = 200
#: 网关把 key 截到前 120 个字符再去重（第 8 节），超过的改交摘要
GATEWAY_KEY_MAX = 120
#: 等网关的硬上限：壳只等 10 秒，留出余量
NOTIFY_TIMEOUT = 8.0
#: 生活设定 PUT 的请求体上限（约定 5.6：256 KiB），比别的接口的 MAX_BODY 大
LIFE_MAX_BODY = 256 * 1024

#: 启停（补充文件 9.2）：整条请求的时限；「停」之后等启动项停下的上限（DSH_CONSOLE_STOP_WAIT 只许缩短）与查的间隔
ENABLE_BUDGET = 55.0
STOP_WAIT = 50.0
STOP_POLL = 1.0
#: 「启」这一步最坏要的时间：launchctl 最多四条（kickstart、回落 bootstrap、定时单元的 bootout 与 bootstrap），
#: 每条最多一个超时。切进导入要在整条时限减去它之前做完，整条请求才不超过 55 秒。
START_RESERVE = 4 * bot_launchd.LAUNCHCTL_TIMEOUT
#: 切进导入（9.3）：旧频道目录根、Claude Code 会话目录根的缺省（测试接缝见 _switch_in）
DEFAULT_CHANNELS_DIR = "~/.claude/channels"
DEFAULT_PROJECTS_DIR = "~/.claude/projects"
#: 群设置（约定 5.12.1、5.12.2）：六个键、群号、bot 用户名、列表与介绍的上限
GROUP_KEYS = ("requireMention", "allowFrom", "selfAliases", "otherBotUsernames", "otherBotAliases", "intro")
GROUP_ID_RE = re.compile(r"^-[0-9]{1,20}\Z")
BOT_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}\Z")
GROUP_LIST_MAX = 50
GROUP_ITEM_MAX = 64
GROUP_INTRO_MAX = 4000
#: group_turn 的 human_ts（9.6）：毫秒位恒为 000 的 UTC 时间
HUMAN_TS_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.000Z\Z")
_ABSENT = object()

#: 人设与语音（v0.8 草案 A、B，补充文件第 10 节）
PERSONA_MAX = 256 * 1024
#: 人设 PUT 的请求体上限：JSON 转义最坏把 1 个字节写成 6 个（如 \u0001），按这个放宽；人设本身另按 UTF-8 字节判
PERSONA_MAX_BODY = 6 * PERSONA_MAX + 1024
PERSONA_VERSION_RE = re.compile(r"^[0-9a-f]{16}\Z")
#: 人设备份的文件名：时间，同一秒重名加 -01 等（life_settings._backup 的命名，不带后缀）
PERSONA_BACKUP_RE = re.compile(r"^[0-9]{8}-[0-9]{6}(-[0-9]{2})?\Z")
PERSONA_SAVED = "已保存，几秒内生效"
PERSONA_SAME = "没有改动"
PERSONA_STALE = "人设刚被别处改过，请刷新后再改"
VOICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\Z")
#: 白名单与语音桥那边一致，有增减时两边一起改（v0.8 草案 B.2）
TTS_MODELS = ("s1", "s2-pro", "s2.1-pro", "s2.1-pro-free", "drama-3")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

#: 配置文件没写 brain 时网关自己用的默认值（gateway/src/config.ts 的 parseBrain）。
CONFIG_DEFAULT_PROVIDER = "deepseek-official"
CONFIG_DEFAULT_MODEL = "deepseek-flash"

#: 网关 ``POST /v1/model`` 把 spec 截到 200 个字符（管理台模型页同一个上限）：超了就别发。
MAX_SPEC = 200
#: 网关 ``key`` 的四种取值里算「有密钥」的两种（补充文件 5.1：ok 已配，none 这家不需要）。
KEY_READY = ("ok", "none")

NOT_SAVED = "这只 bot 的进程现在连不上，这次没有保存"
NOT_SAVED_SLOW = "这只 bot 的进程没有及时回应，这次没有保存"
UNCONFIRMED = "已发给这只 bot，还没确认生效，请稍后刷新确认"     # 补充文件 6.1 原文
NO_RUNNING_BOT = "没有正在运行的 bot，暂时读不到供应商清单"

UNAUTHORIZED = {"ok": False, "code": "unauthorized", "message": "令牌缺失或不对"}
INTERNAL = {"ok": False, "code": "internal", "message": "内部错误"}

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_log = logging.getLogger("dsh.adapter")

#: 同一只 bot 同一时刻只许一个重启在跑（进程内）。键是已经过名单精确匹配的 bot id。
_restart_locks = {}
_restart_locks_guard = threading.Lock()


class Refuse(Exception):
    """按约定的失败形状回给调用方的拒绝：HTTP 状态码、``code``、给人看的一句话。

    ``fields`` 不为 None 时一并放进响应体（生活设定的 422 一律带它，INTERFACE-生活设定 第 1 节）。
    """

    def __init__(self, status, code, message, headers=None, fields=None):
        super().__init__(code)
        self.status, self.code, self.message, self.headers = status, code, message, headers
        self.fields = fields


class _NoReply(Exception):
    """请求已经交给网关，在时限内没等到回复（超时、中途断开）。写请求时：可能已经生效，也可能没有。"""


def _invalid(message):
    return Refuse(422, "invalid", message)


def _json(payload, status=200, headers=None):
    return Response(json.dumps(payload, ensure_ascii=False), status=status,
                    mimetype="application/json", headers=headers)


def _text(value):
    return isinstance(value, str) and value.strip() != ""


# ---------------------------------------------------------------- 令牌

def token_file():
    """令牌文件的位置：``DSH_CONSOLE_TOKEN_FILE``，没设或为空走中立位置的默认值。"""
    raw = os.environ.get("DSH_CONSOLE_TOKEN_FILE")
    if raw is None or not raw.strip():
        raw = DEFAULT_TOKEN_FILE
    return os.path.expanduser(raw.strip())


def read_token():
    """令牌 = 令牌文件的全部内容去掉首尾空白（字节）。不存在、读不了、去掉空白后为空 → None。"""
    try:
        with open(token_file(), "rb") as f:
            token = f.read().strip()
    except OSError:
        return None
    return token or None


def _authorized():
    """请求头与 ``Bearer <令牌>`` 逐字节一致才放行。

    两边先各取一次摘要再用 ``hmac.compare_digest`` 比：耗时与「对了几个字符」无关，
    也与两边的长度无关。令牌文件此刻读不出或为空时一律不放行（空的 ``Bearer`` 也不行）。
    """
    token = read_token()
    if token is None:
        return False
    got = request.environ.get("HTTP_AUTHORIZATION")
    if not isinstance(got, str):
        return False
    try:
        got = got.encode("latin-1")         # WSGI 的请求头是按 latin-1 解出来的，原样还原成字节
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(hashlib.sha256(got).digest(),
                               hashlib.sha256(b"Bearer " + token).digest())


# ---------------------------------------------------------------- 路由（原样路径）

def _raw_path():
    """请求行里**没被解码、没被规范化**的路径（去掉查询串）。

    ``RAW_URI`` / ``REQUEST_URI`` 是服务器原样记下的请求目标（本模块自己起的 werkzeug 服务器
    与 Flask 测试客户端都给）。两个都没有时退回 ``PATH_INFO``：那是解码过的，带编码斜杠的
    请求会因为段数对不上落到 404 而不是 422，但仍然不会被规范化到别的接口上。
    """
    env = request.environ
    target = env.get("RAW_URI")
    if not isinstance(target, str):
        target = env.get("REQUEST_URI")
    if not isinstance(target, str):
        target = env.get("PATH_INFO") or ""
    return target.split("?", 1)[0]


def _valid_bot_id(bot_id):
    return BOT_ID_RE.match(bot_id) is not None and bot_id.strip(".") != ""


def _dispatch():
    path, method = _raw_path(), request.method
    handler = _READ_ROUTES.get(path)
    if handler is not None:
        _require_method(method, "GET")
        return handler()
    head = PREFIX + "/bots/"
    if path.startswith(head):
        parts = path[len(head):].split("/")
        if len(parts) == 2 and parts[1] in _BOT_ROUTES:
            methods = _BOT_ROUTES[parts[1]]
            _require_method(method, ", ".join(methods))
            if not _valid_bot_id(parts[0]):
                refuse = _invalid("bot 编号不合规：只能用字母、数字、点、下划线、横线，1 到 64 个字符")
                if parts[1] == "life":
                    refuse.fields = {}      # 生活设定的 422 一律带 fields（INTERFACE-生活设定 第 1 节）
                raise refuse
            return methods[method](parts[0])
    raise Refuse(404, "not_found", "没有这个接口")


def _require_method(method, allowed):
    """``allowed`` 是 ``Allow`` 头的写法：一个方法，或逗号加空格隔开的几个。"""
    if method not in allowed.split(", "):
        raise Refuse(405, "method_not_allowed", "这个接口不支持这种请求方式",
                     headers={"Allow": allowed})


def _respond():
    """全部请求的唯一入口：鉴权 → 路由 → 把拒绝与意外翻成约定的失败形状。"""
    try:
        allowed = _authorized()
    except Exception:                       # noqa: BLE001  判不出来就不放行，不把原因带出去
        allowed = False
    if not allowed:
        return _json(UNAUTHORIZED, 401)
    try:
        path = _raw_path()
        if path == "/hub" or path.startswith("/hub/"):
            return _hub_respond(path)       # 名单外的 404、错方法的 405 也从这里以 Refuse 抛出来
        return _json(_dispatch())
    except Refuse as r:
        payload = {"ok": False, "code": r.code, "message": r.message}
        if r.fields is not None:
            payload["fields"] = r.fields
        return _json(payload, r.status, r.headers)
    except Exception as e:                  # noqa: BLE001  异常消息可能带路径，只记类名
        _log.error("adapter internal error: %s", type(e).__name__)
        return _json(INTERNAL, 500)


def _json_object(allow_empty=False, limit=MAX_BODY, too_big=None):
    """请求体必须是一个 JSON 对象；``allow_empty`` 时空体等同 ``{}``。只在过了鉴权之后才读。

    ``limit`` 是字节上限，超了回 422，话是 ``too_big``（没给就同「不是 JSON 对象」）。
    上限不是 ``MAX_BODY`` 的，调用方先把 ``request.max_content_length`` 设成 ``limit + 1``。
    """
    bad = _invalid("请求体必须是一个 JSON 对象")
    big = _invalid(too_big) if too_big else bad
    length = request.content_length
    if length is not None and length > limit:
        raise big
    # 分块传输没有 Content-Length：靠 max_content_length（上限加 1，见 create_app）
    # 让框架最多读这么多就停，读满了就是超了。框架某些情况下改为抛 413，同样按超了拒绝。
    try:
        raw = request.get_data(cache=False)
    except RequestEntityTooLarge:
        raise big from None
    if len(raw) > limit:
        raise big
    if not raw.strip():
        if allow_empty:
            return {}
        raise bad
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):    # 坏 JSON、不是 UTF-8、嵌套深到解析不动
        raise bad from None
    if not isinstance(body, dict):
        raise bad
    return body


# ---------------------------------------------------------------- 本系统的 bot

def _own_bots():
    """本系统的 bot，按编号字典序：``[{"id", "display_name", "enabled", "channel", "cfg"}]``。

    范围是补充文件第 1 节那句：新系统配置根（``HUB_CONFIGS_DSH_DIR``）下文件名是合格 bot id
    的 ``<id>.yml``。每份只取它**自己**顶层写的键（``cfg``），不走管理台共用的
    ``config_loader.load_bot``：那条读法会打开 ``life_config`` 指向的旧配置（另一套系统的目录）
    整份合并进来，那份文件坏了或被删，这只 bot 还会从名单里整只消失（BRIEF 归属边界）。
    旧系统配置根同理不读。读不了、解析不出、顶层不是映射的那份只跳过它自己（与管理台名单同口径）；
    配置根本身列不出来就抛，外层回 500。

    启停与网关位置都在这里（处理请求的线程里）读好：``enabled`` 按这份 yml 自己的 ``enabled``
    判（判定只有 ``bots_registry.is_enabled`` 一处），``channel`` 是找网关用的频道目录。
    """
    from bots_registry import is_enabled    # 延迟 import：仓库根模块，理由同 config_sources
    d = config_sources.dsh_dir()
    if d is None or not os.path.isdir(d):
        return []
    bots = []
    for name in sorted(os.listdir(d)):
        bot_id = name[:-4] if name.endswith(".yml") else None
        if not config_sources.is_qualified_bot_id(bot_id):
            continue
        try:
            with open(os.path.join(str(d), name), encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except Exception:                   # noqa: BLE001  坏掉的只跳过它自己
            continue
        if not isinstance(cfg, dict):
            continue
        shown = cfg.get("display_name")
        bots.append({"id": bot_id, "display_name": shown.strip() if _text(shown) else bot_id,
                     "enabled": is_enabled(cfg), "channel": _gateway_channel(bot_id, cfg),
                     "cfg": cfg})
    return bots


def _gateway_channel(bot_id, cfg):
    """找这只 bot 的网关用的频道目录（``gateway_client`` 到它旁边的 ``state/`` 里读口令与端口）。

    取法与管理台编辑 bot 那边（``hub_addbot._channel_dir``）、网关自己（``config.ts``）一致：只认
    这份 yml 自己顶层写的 ``bot_channel_path``（展开 ``~``），没写、为空、不是字符串就是
    ``<bot 家>/channel``；``life_config`` 那份里写的不参与（安审 I1）。

    落点：真正会被读的是旁边那个 ``state/``。它解析成真实路径（软链跟到底）之后必须在这只 bot 的家
    （``<DSH_BOT_HOME>/bots/<id>``）里面，否则回空串，按网关不可用处理：写到另一套系统频道目录
    （``~/.claude/channels/<x>``）的、``state`` 经软链出去的、频道目录就写成 bot 家本身的（那样
    ``state`` 落在 ``bots/state``，不是这只的），都不去那里找口令。
    """
    raw = cfg.get("bot_channel_path")
    home = os.path.join(os.path.realpath(str(config_sources.dsh_home())), "bots", bot_id)
    written = config_sources.channel_dir({"bot_channel_path": raw.strip()}) if _text(raw) else ""
    channel = os.path.abspath(written or os.path.join(home, "channel"))
    state = os.path.realpath(os.path.join(os.path.dirname(channel), "state"))
    return channel if state.startswith(home + os.sep) else ""


def _require_bot(bot_id, bots=None):
    """名单里有这只 bot 才往下走（精确匹配，不拿 bot_id 去拼任何路径），返回它那一条。

    ``bots`` 是已经读好的名单（生活设定还要用整份名单查共用），没给就现读。
    """
    for bot in _own_bots() if bots is None else bots:
        if bot["id"] == bot_id:
            return bot
    raise Refuse(404, "unknown_bot", "没有这只 bot")


def _require_enabled(bot):
    if not bot["enabled"]:
        raise Refuse(409, "stopped", "这只 bot 已停用，先启用它再操作")


def _config_brain(cfg):
    """配置文件里写的那一组 ``{"provider", "model", "effort"}``；没写的两项取网关的默认值。"""
    brain = cfg.get("brain") if isinstance(cfg, dict) else None
    brain = brain if isinstance(brain, dict) else {}
    provider, model, effort = brain.get("provider"), brain.get("model"), brain.get("reasoning_effort")
    return {"provider": provider if _text(provider) else CONFIG_DEFAULT_PROVIDER,
            "model": model if _text(model) else CONFIG_DEFAULT_MODEL,
            "effort": effort if isinstance(effort, str) and effort in EFFORT_LEVELS else None}


def _effort_override(channel, timeout=gateway_client.EFFORT_TIMEOUT):
    """``GET /v1/effort`` 的 current：这只 bot 单独指定的思考强度，没有单独指定是 None。

    网关回的东西认不出就抛；网络上的事原样抛给调用方（各处的容错不一样，由调用方定）。
    """
    code, got = gateway_client.effort_get(channel, timeout=timeout)
    if code != 200 or not isinstance(got, dict):
        raise RuntimeError("unexpected gateway reply")
    current = got.get("current")
    return current if _text(current) else None


def _live_brain(channel):
    """经这只 bot 自己的网关读当前生效的那一组；连不上、超时、回的东西认不出 → None。

    读的是管理台模型页在用的那两个接口：``GET /v1/model``（current 与 config）与
    ``GET /v1/effort``（current = 单独指定的思考强度，没有为 null）。返回
    ``{"provider", "model", "config_provider", "config_model", "effort"}``。
    """
    if not gateway_client.available(channel):
        return None
    try:
        info = gateway_client.model_info(channel, timeout=GATEWAY_READ_TIMEOUT)
    except Exception:                       # noqa: BLE001  补充文件 4.2：这一只降级，不拖累整个列表
        return None
    current = info.get("current") if isinstance(info, dict) else None
    if not isinstance(current, dict) or not _text(current.get("provider")) \
            or not _text(current.get("model")):
        return None
    config = info.get("config") if isinstance(info.get("config"), dict) else {}
    try:
        effort = _effort_override(channel, GATEWAY_READ_TIMEOUT)
    except Exception:                       # noqa: BLE001  思考强度读不到只少这一项
        effort = None
    if effort not in EFFORT_LEVELS:         # 聊天里设的别的档位不在约定的四档里，不往外报
        effort = None
    return {"provider": current["provider"], "model": current["model"],
            "config_provider": config.get("provider"), "config_model": config.get("model"),
            "effort": effort}


def _person(bot):
    """约定 5.2 的 ``person``（v0.6）：朋友圈里记这只 bot 的名字，与 ``config_sources.person_id`` 同一口径。

    只看 bot 自己 yml 里的键：先 ``life_id``，再 ``life_config`` 的文件名去掉扩展名，都没有就是 bot 编号。
    不打开 life 文件（归属边界），所以 life 文件里写的 ``life_id`` 不算；现有配置都没写这个键。
    """
    cfg = bot["cfg"]
    if _text(cfg.get("life_id")):
        return cfg["life_id"].strip()
    life = cfg.get("life_config")
    if _text(life):
        stem = os.path.splitext(os.path.basename(life.strip()))[0]
        if stem:
            return stem
    return bot["id"]


def _bot_row(bot, seams):
    """列表里的一行（约定 5.2 的八个键，v0.6 起加 ``person``）。在线程池里跑：只用传进来的值，不读环境与配置。"""
    bot_id = bot["id"]
    brain = _config_brain(bot["cfg"])
    live = None
    if not bot["enabled"]:
        state, state_text = "stopped", "已停用"
    else:
        row_state, _pid = bot_launchd.row_state(bot_id, seams)
        if row_state == "online":
            state, state_text = "running", "运行中"
        elif row_state == "starting":
            state, state_text = "running", "启动中"
        else:
            state, state_text = "broken", "没在运行：没有新鲜的心跳，启动项也不在跑"
        live = _live_brain(bot["channel"])
    detail = {}
    if state == "running" and _gateway_health(bot["channel"]).get("conflict") is True:
        detail = {"telegram_conflict": True}    # 9.4：同一个 Telegram 令牌有别的程序在收消息
    if live is None:
        provider, model, effort, source = brain["provider"], brain["model"], brain["effort"], "default"
    else:
        provider, model = live["provider"], live["model"]
        effort = live["effort"] or brain["effort"]
        # 补充文件 5.1：current 与 config 比，返回里没有 config 拿配置文件那一组比。
        # 只单独指定了思考强度、供应商和模型没变的仍算 default，所以这里不看强度。
        config_pair = (live["config_provider"] or brain["provider"],
                       live["config_model"] or brain["model"])
        source = "override" if (provider, model) != config_pair else "default"
    return {"bot_id": bot_id, "display_name": bot["display_name"], "person": _person(bot),
            "state": state, "state_text": state_text,
            "provider": {"id": provider, "name": provider, "model": model, "effort": effort},
            "provider_source": source, "pending": None, "detail": detail}


# ---------------------------------------------------------------- 五个接口

def _backend_version():
    """本仓当前提交的短哈希。只读 ``.git`` 里的文件，不起子进程；读不出回 ``unknown``。"""
    git = os.path.join(_REPO_ROOT, ".git")
    try:
        with open(os.path.join(git, "HEAD"), encoding="utf-8") as f:
            head = f.read().strip()
        if head.startswith("ref: "):
            ref = head[5:].strip()
            try:
                with open(os.path.join(git, *ref.split("/")), encoding="utf-8") as f:
                    head = f.read().strip()
            except OSError:                 # 这个分支的指针被收进了 packed-refs
                head = ""
                with open(os.path.join(git, "packed-refs"), encoding="utf-8") as f:
                    for line in f:
                        if line.rstrip().endswith(" " + ref):
                            head = line.split(" ", 1)[0]
                            break
    except (OSError, ValueError):
        return "unknown"
    return head[:12] if re.match(r"^[0-9a-f]{40,64}\Z", head) else "unknown"


def _meta():
    return {"ok": True, "backend": "dsh", "backend_version": _backend_version(), "adapter": "v1",
            "capabilities": dict(CAPABILITIES), "effort_levels": list(EFFORT_LEVELS)}


def _bots():
    bots = _own_bots()
    if not bots:
        return {"ok": True, "bots": []}
    # 接缝、每只的启停、每只的配置都在处理请求的线程里读好，线程池里不再读环境与配置。
    seams = bot_launchd.resolve_seams()
    with ThreadPoolExecutor(max_workers=min(MAX_ROW_WORKERS, len(bots))) as pool:
        futures = [pool.submit(_bot_row, bot, seams) for bot in bots]
        return {"ok": True, "bots": [f.result() for f in futures]}


def _provider_catalog():
    """4.3 的供应商列表 ``[{"id", "name", "has_key", "models"}]``，向正在运行的 bot 的网关读来。

    补充文件 4.3、5.1：所有 bot 共用一份清单，按 bot 编号的字典序问第一只连得上的，读的是
    ``GET /v1/model``（映射见 ``_catalog``），不读任何本地的凭据文件。停用的 bot、没有网关
    文件的 bot 跳过，不发请求。

    - 没连上、发出后断了或超时：这是只读请求，换下一只问不会多写什么；
    - 连上了但回的东西认不出（不是 JSON、状态码不对、形状不对）：不再问别的，抛，外层回 500；
    - 一只都问不到（都停用了、都没在跑、一只 bot 都没有）：503。

    换供应商的第 4 步不走这里：它只问那只 bot 自己的网关（见 ``_set_provider``）。
    """
    for bot in _own_bots():
        if not bot["enabled"]:
            continue
        channel = bot["channel"]
        if not gateway_client.available(channel):
            continue
        try:
            info = gateway_client.model_info(channel, timeout=GATEWAY_READ_TIMEOUT)
        except Exception as e:              # noqa: BLE001  按类型分流：网络上的事换下一只，别的往外抛
            if gateway_client.net_kind(e) is None:
                raise
            continue
        return _catalog(info)
    raise Refuse(503, "unavailable", NO_RUNNING_BOT)


def _catalog(info):
    """``GET /v1/model`` 的返回体 → 4.3 的供应商列表 ``[{"id", "name", "has_key", "models"}]``。

    照补充文件 5.1 映射：名字取 ``providers[].name``；模型名取 ``choices`` 里归这一家的那些，
    保持 ``choices`` 的先后（``providers[].models`` 只是个数）；``key`` 为 ok 或 none 才算有密钥；
    停用的不列。只往外带这四个键，keyEnv、api、note 这些一律不带。

    返回体不是对象、没有 ``providers`` 或它不是数组 → 认不出，抛（外层回 500）。``choices``
    缺失或不是数组当作空（网关刚启动时会这样）。约定没写的照最保守的办：形状不对的单项跳过，
    认不出的值不往外带；``choices`` 里 ``providers`` 没有的名字不单列；没有 ``enabled`` 字段的
    当作启用（那样的网关没有停用这回事），有这个字段但不是 true 的不列。
    """
    providers = info.get("providers") if isinstance(info, dict) else None
    if not isinstance(providers, list):
        raise RuntimeError("unexpected gateway reply")
    choices = info.get("choices")
    models = {}
    for choice in choices if isinstance(choices, list) else []:
        if isinstance(choice, dict) and _text(choice.get("provider")) \
                and _text(choice.get("model")):
            models.setdefault(choice["provider"], []).append(choice["model"])
    catalog = []
    for p in providers:
        if not isinstance(p, dict) or not _text(p.get("name")) \
                or p.get("enabled", True) is not True:
            continue
        name = p["name"]
        catalog.append({"id": name, "name": name, "has_key": p.get("key") in KEY_READY,
                        "models": list(models.get(name, []))})
    return catalog


def _require_offered(catalog, provider_id, model):
    """换供应商第 4 步的两项校验（补充文件 4.4）：只看这只 bot 的网关刚读来的清单。"""
    for provider in catalog:
        if provider["id"] != provider_id:
            continue
        if model not in provider["models"]:
            raise _invalid("这家供应商没有这个模型")
        if len(provider_id) + 1 + len(model) > MAX_SPEC:
            raise _invalid("模型名太长，这只 bot 的进程收不下")
        return
    raise Refuse(404, "unknown_provider", "没有这家供应商")


def _providers():
    return {"ok": True, "providers": _provider_catalog(), "default": {"label": DEFAULT_LABEL}}


def _provider_budget():
    """换供应商整条请求的时限（秒）：``DSH_CONSOLE_PROVIDER_BUDGET``，每次请求现读。

    只供测试缩短时限用：不是正数、比 55 秒还长的（含 nan、inf）一律按 55 秒。
    """
    try:
        budget = float((os.environ.get("DSH_CONSOLE_PROVIDER_BUDGET") or "").strip())
    except ValueError:
        return PROVIDER_BUDGET
    return budget if 0 < budget <= PROVIDER_BUDGET else PROVIDER_BUDGET


def _set_provider(bot_id):
    started = time.monotonic()
    budget = _provider_budget()
    # 补充文件 6.1：整条请求在 deadline 前返回；读阶段（读清单、读原强度）在 read_by 前回来
    deadline, read_by = started + budget, started + READ_SHARE * budget
    body = _json_object()
    if "reset" in body:
        raise Refuse(501, "unsupported", "这个底层还不支持一键回到默认供应商")
    has_provider, has_model, has_effort = "provider_id" in body, "model" in body, "effort" in body
    if has_provider != has_model or (
            has_provider and not (_text(body["provider_id"]) and _text(body["model"]))):
        raise _invalid("provider_id 和 model 必须一起给，而且都不能为空")
    if has_effort and not (isinstance(body["effort"], str) and body["effort"] in EFFORT_LEVELS):
        raise _invalid("思考强度只能是 off、low、high、max 之一")
    if not has_provider and not has_effort:
        raise _invalid("请求里没有要改的内容：给 provider_id 加 model，或者给 effort")
    bot = _require_bot(bot_id)
    _require_enabled(bot)                   # 补充文件 4.4 第 3 步：停用先于读清单，停用时什么都不读
    # 前三步都过了，下面才开始找这只 bot 自己的网关（不找别的 bot 的）。
    channel = bot["channel"]
    if not gateway_client.available(channel):
        raise Refuse(503, "unavailable", NOT_SAVED)
    before = after = prior_effort = None
    if has_provider:
        # 第 4 步：向这只 bot 的网关读一次清单（只读请求），没过就到此为止，什么都没写。
        info = _read_gateway(lambda t: gateway_client.model_info(channel, timeout=t), read_by)
        _require_offered(_catalog(info), body["provider_id"], body["model"])
        before = _pair(info.get("current"))  # 同一次读里现在生效的那一组，用来判断是不是换了一家
        # 第 5 步：以上都过了才写。
        if has_effort:
            # 强度被拒时要把供应商和模型换回去，而网关换模型会清掉单独指定的强度：先记下原来的。
            prior_effort = _read_gateway(lambda t: _effort_override(channel, t), read_by)
        try:
            after = _switch(channel, before, body["provider_id"], body["model"], deadline)
        except _NoReply:
            # 6.1：换模型已经发出、时限内没回，换没换成不知道；强度不再发
            return _unconfirmed(False)
    if has_effort:
        try:
            _apply_effort(channel, body["effort"], deadline)
        except _NoReply:
            # 6.1：改强度已经发出、时限内没回；供应商和模型这一步（若有）是确认过的
            return _unconfirmed(has_provider and after[0] != before[0])
        except Exception as e:              # noqa: BLE001  不只是拒绝：网关回 400、500、认不出的也在这里
            if after != before:             # 供应商和模型这一步已经写进去了（约定 5.4：不许写了一半）
                if isinstance(e, Refuse) and e.status != 422:
                    # 改强度没发出去（网关连不上了，或者没时间了）：供应商和模型换了，强度没动
                    raise Refuse(e.status, e.code, "供应商和模型已经换了，但思考强度"
                                 "没有确认改成，请刷新后确认") from None
                # 网关明说不支持（422），或回了 400、500、认不出的东西：在剩下的时间里换回原来那一组。
                # 换回去了再照原样往外报（认不出的仍是 500 内部错误，但设置已经是原样）；
                # 换不回去由 _undo_switch 把现状说实。
                _undo_switch(channel, before, prior_effort, deadline)
            raise
    new_segment = has_provider and after[0] != before[0]
    message = "已保存，下一条消息起生效"
    if new_segment:
        message += "；换到了另一家供应商，当前对话会先总结再另开一段"
    return {"ok": True, "applied": True, "queued": False, "new_segment": new_segment,
            "message": message}


def _unconfirmed(new_segment):
    return {"ok": True, "applied": True, "queued": False, "new_segment": new_segment,
            "message": UNCONFIRMED}


def _gateway(call, deadline):
    """向这只 bot 的网关发一次请求 ``call(超时秒数)``，最多等到 ``deadline``（单调时钟）。

    硬封顶靠线程：urllib 的超时管的是每一次阻塞操作，连接、发送、接收能各用掉接近一个超时，
    不是整次调用的上限。到点没回，那次请求留在守护线程里，socket 超时（同为当时剩下的时间）到了自己退出。

    - 还没发就到了时限 → 503 ``NOT_SAVED_SLOW``；连不上（拒连）→ 503 ``NOT_SAVED``。都是没发出去。
    - 发出去了，中途断开、超时、到点没回 → ``_NoReply``，由调用方按读还是写来定怎么回。
    - 别的异常原样往外抛（外层回 500）。
    """
    left = deadline - time.monotonic()
    if left <= 0:
        raise Refuse(503, "unavailable", NOT_SAVED_SLOW)
    box = {}

    def run():
        try:
            box["reply"] = call(left)
        except Exception as e:              # noqa: BLE001  交回主线程分流
            box["error"] = e

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(left)
    if worker.is_alive():
        raise _NoReply()
    error = box.get("error")
    if error is None:
        return box["reply"]
    kind = gateway_client.net_kind(error)
    if kind == "unreachable":
        raise Refuse(503, "unavailable", NOT_SAVED) from None
    if kind == "broken":
        raise _NoReply() from None
    raise error


def _read_gateway(call, deadline):
    """只读请求：没等到回复也什么都没写，回 503。"""
    try:
        return _gateway(call, deadline)
    except _NoReply:
        raise Refuse(503, "unavailable", NOT_SAVED_SLOW) from None


def _pair(choice):
    """网关回的一组 ``{"provider", "model"}`` → 元组；认不出就抛。"""
    if not isinstance(choice, dict) or not _text(choice.get("provider")) \
            or not _text(choice.get("model")):
        raise RuntimeError("unexpected gateway reply")
    return choice["provider"], choice["model"]


def _switch(channel, before, provider_id, model, deadline):
    """经这只 bot 的网关换供应商和模型，返回换之后生效的那一组 ``(供应商, 模型)``。

    ``before`` 是第 4 步那次读里现在生效的一组。发的是管理台模型页在用的写请求
    ``POST /v1/model``，请求体 ``{"spec": "供应商/模型"}``，网关回 ``{"ok", "text", "current"}``。
    已经是这一组时网关也回 ok，重复提交不叠加。
    网关明说没换（这只 bot 缺这家的密钥，或它的模型表里此刻没有这个模型）→ 422，什么都没写；
    网关的原话可能带密钥的变量名，不往外带。发出去了没等到回复抛 ``_NoReply``。
    """
    spec = "%s/%s" % (provider_id, model)
    ok, got = _gateway(lambda t: gateway_client.model_set(channel, spec, timeout=t), deadline)
    after = _pair(got.get("current") if isinstance(got, dict) else None)
    if not ok:
        if got.get("ok") is False and after == before:
            raise _invalid("这只 bot 现在换不到这个模型：这家供应商在它那里可能还没配密钥，"
                           "或者它的模型表里暂时没有这个模型。设置没有变")
        raise RuntimeError("unexpected gateway reply")
    return after


def _apply_effort(channel, effort, deadline):
    """经这只 bot 的网关改思考强度：``POST /v1/effort``，请求体 ``{"effort": 档位}``。"""
    code, got = _gateway(lambda t: gateway_client.effort_set(channel, effort, timeout=t), deadline)
    if code == 409:
        # 网关的 409 unsupported：档位在四档之内，但这只 bot 现在用的模型不支持它
        raise _invalid("这只 bot 现在用的模型不支持这个思考强度")
    if code != 200 or not isinstance(got, dict) or got.get("ok") is not True:
        raise RuntimeError("unexpected gateway reply")


def _undo_switch(channel, before, prior_effort, deadline):
    """强度被拒之后，在时限剩下的时间里把供应商和模型换回 ``before``，原来单独指定过强度的再设回去。

    换回配置文件里那一组时网关自己会清掉单独指定，不留痕迹。换不回去（含到点没换完）就抛 500，把现状说实。
    """
    spec = "%s/%s" % before
    try:
        ok, _got = _gateway(lambda t: gateway_client.model_set(channel, spec, timeout=t), deadline)
        if ok and prior_effort is not None:
            code, got = _gateway(lambda t: gateway_client.effort_set(channel, prior_effort, timeout=t),
                                 deadline)
            ok = code == 200 and isinstance(got, dict) and got.get("ok") is True
    except Exception:                       # noqa: BLE001  换不回去的原因不重要，结果要说实
        ok = False
    if not ok:
        raise Refuse(500, "internal", "思考强度没有改成，供应商和模型已经换了，"
                     "而且没能换回去，请刷新后确认") from None


def _restart_lock(bot_id):
    with _restart_locks_guard:
        return _restart_locks.setdefault(bot_id, threading.Lock())


def _restart(bot_id):
    body = _json_object(allow_empty=True)
    if "force" in body and type(body["force"]) is not bool:
        raise _invalid("force 只能是 true 或 false")
    if body.get("force") is not True:
        raise Refuse(501, "unsupported", "这个底层还不支持等空闲再重启，只能强制重启")
    _require_enabled(_require_bot(bot_id))
    # 覆盖命令（HUB_RESTART_CMD）是「整次换成跑一条全局命令」，做不到只动这一只，这里不走它。
    plan = bots_client.restart_plan()
    if not plan.available:
        return {"ok": False, "status": "failed", "message": plan.reason or "这台机器重启不了 bot"}
    lock = _restart_lock(bot_id)
    if not lock.acquire(blocking=False):
        raise Refuse(409, "busy", "这只 bot 正在重启，这次没有执行")
    try:
        ok, reason, _pid, _age = bot_launchd.start_and_wait(
            bot_id, bot_launchd.resolve_seams(), budget=RESTART_BUDGET)
    finally:
        lock.release()
    if ok:
        return {"ok": True, "status": "ok", "message": "已重启"}
    if not reason or "没有新心跳" in reason:
        reason = "%g 秒内没有等到新的心跳" % RESTART_BUDGET
    return {"ok": False, "status": "failed", "message": "重启命令发了，但没起来：%s" % reason}


def _utf8_size(text):
    """按 UTF-8 算的字节数。JSON 能解出落单的代理码（``\\ud800``），那种串编不成 UTF-8，回 None。"""
    try:
        return len(text.encode("utf-8"))
    except UnicodeEncodeError:
        return None


def _gateway_key(key):
    """交给网关的去重键。网关（JS）把 key 截到前 120 个 UTF-16 码元再去重，两个前 120 个字相同的长 key
    会被当成同一条漏投；超过的改交摘要，同一个 key 永远得到同一个摘要（补充文件第 8 节）。"""
    if len(key.encode("utf-16-le")) // 2 <= GATEWAY_KEY_MAX:
        return key
    return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()


def _notify(bot_id):
    """投递通知（约定 5.7，补充文件 6.2）：形状 → 名单 → 停用 → 这只 bot 的网关 ``POST /v1/inject``。

    ``group_turn``（约定 5.12.3，补充文件 9.6）多带群号：停用之后先看 access.json 里有没有这个群，
    再以来源 ``director`` 注入这个群的聊天；别的同另三种来源。
    """
    body = _json_object()
    source, text, key = body.get("source"), body.get("text"), body.get("key")
    if not isinstance(source, str) or source not in NOTIFY_SOURCES:
        raise _invalid("source 只能是 user_moment、moment_reply、moment_image、group_turn 之一")
    size = _utf8_size(text) if isinstance(text, str) else None
    if size is None or not text.strip() or size > NOTIFY_MAX_TEXT:
        raise _invalid("text 要是一段不全是空白的文字，不超过 16 KiB")
    if not isinstance(key, str) or not 1 <= len(key) <= NOTIFY_MAX_KEY or _utf8_size(key) is None:
        raise _invalid("key 要是 1 到 200 个字符")
    group = body.get("chat_id")
    if source == "group_turn":
        # 9.6：群号必填；human_ts 可选，只校验、不交给网关
        if not _group_id(group):
            raise _invalid("group_turn 要带 chat_id：群号，- 后面跟 1 到 20 位数字的字符串")
        if "human_ts" in body and not _human_ts(body["human_ts"]):
            raise _invalid("human_ts 要是 YYYY-MM-DDTHH:MM:SS.000Z 形状的 UTC 时间")
    elif "chat_id" in body or "human_ts" in body:
        raise _invalid("只有 group_turn 才能带 chat_id 和 human_ts")
    bot = _require_bot(bot_id)
    _require_enabled(bot)
    if source == "group_turn":
        if group not in _read_access(bot)[2]:
            raise Refuse(409, "not_in_group", "这只 bot 不在这个群里，没有投递")
        # 以来源 director 注入这个群的聊天：网关只在收到导演的注入时才在群里开口（约定 5.12.3）
        chat_id, inject_source = group, "director"
    else:
        # 投进这只 bot 与主人的私聊：聊天号取它自己 yml 顶层写的（不读 life_config 指过去的那份）
        chat_id, inject_source = bot["cfg"].get("chat_id"), source
        if not (_text(chat_id) or type(chat_id) is int):
            raise Refuse(500, "internal", "这只 bot 的配置里没有主人的聊天号，投不了")
    channel = bot["channel"]
    if not gateway_client.available(channel):
        raise Refuse(503, "unavailable", "这只 bot 的进程现在连不上，这条通知没有投出去")
    text = gateway_client.for_dsh(text)     # 旧系统的工具用法换成本系统的（同朋友圈投给网关的那份规则）
    deadline = time.monotonic() + NOTIFY_TIMEOUT
    try:
        code, got = _gateway(lambda t: gateway_client.inject_result(
            channel, chat_id, text, inject_source, _gateway_key(key), timeout=t), deadline)
    except _NoReply:
        # 网关可能已经收下了：壳用同一个 key 重试时网关去重，只会投一次
        raise Refuse(503, "unavailable", "这只 bot 的进程没有及时回应，这条通知不一定投到了") from None
    except Refuse as r:                     # 连不上：没投出去
        raise Refuse(r.status, r.code, "这只 bot 的进程现在连不上，这条通知没有投出去") from None
    if code == 200 and isinstance(got, dict) and got.get("ok") is True:
        return {"ok": True, "status": "duplicate" if got.get("duplicate") is True else "delivered"}
    if code == 403:                         # 不带 Origin 时，网关的 inject 只在聊天号不在允许名单里回 403
        raise Refuse(500, "internal", "这只 bot 的进程拒收了这条通知：%s不在它的允许名单里"
                     % ("这个群" if source == "group_turn" else "它配置里主人的聊天号"))
    raise RuntimeError("unexpected gateway reply")


def _life_refuse(err):
    return Refuse(err.status, err.code, err.message, fields=err.fields)


def _life_get(bot_id):
    """生活设定 GET（约定 5.6，INTERFACE-生活设定 第 4 节）：不看请求体与查询串；bot 停用时照常返回。"""
    bots = _own_bots()
    bot = _require_bot(bot_id, bots)
    try:
        return dict(ok=True, **life_settings.view(bot, bots))
    except life_settings.LifeError as e:
        raise _life_refuse(e) from None


def _life_put(bot_id):
    """生活设定 PUT（INTERFACE-生活设定 第 5 节）：请求体 → 名单 → 交给 life_settings；停用的 bot 照常能改。"""
    request.max_content_length = LIFE_MAX_BODY + 1      # 分块传输的读到上限多一个字节即停
    try:
        body = _json_object(limit=LIFE_MAX_BODY, too_big="请求体太大，上限 256 KiB")
    except Refuse as r:
        r.fields = {}
        raise
    values = body.get("values")
    if not isinstance(values, dict):
        raise Refuse(422, "invalid", "请求体里要有 values，而且是一个对象", fields={})
    bots = _own_bots()
    bot = _require_bot(bot_id, bots)
    try:
        message = life_settings.save(bot, bots, values)
    except life_settings.LifeError as e:
        raise _life_refuse(e) from None
    return {"ok": True, "applied": True, "queued": False, "message": message}


# ---------------------------------------------------------------- v0.6、v0.7（补充文件第 9 节）

def _gateway_health(channel):
    """这只 bot 的网关 ``GET /v1/health`` 返回里 ``telegram`` 那一段（9.4 同跑提示、9.5 用户名）。

    最多等 ``GATEWAY_READ_TIMEOUT``。问不到（没有网关文件、连不上、超时、回的不是 JSON）不算错，回 ``{}``。
    网关自己有毛病时（同跑冲突本身就算一条）这个接口回 503，返回体照样是完整的健康信息，所以 503 也认。
    返回体里别的东西（聊天号、模型）一概不往外带。
    """
    if not gateway_client.available(channel):
        return {}
    try:
        code, got = gateway_client._call(channel, "GET", "/v1/health", timeout=GATEWAY_READ_TIMEOUT)
    except Exception:                       # noqa: BLE001  问不到不算错（9.4）
        return {}
    telegram = got.get("telegram") if code in (200, 503) and isinstance(got, dict) else None
    return telegram if isinstance(telegram, dict) else {}


def _unit_running(bot_id, seams):
    """启动项此刻在不在跑：``launchctl print`` 说 running。9.2、9.3 都按它判，不看心跳。"""
    return bot_launchd.print_state(bot_id, seams)[0] == "running"


def _stop_wait():
    """「停」之后等启动项停下的秒数：``DSH_CONSOLE_STOP_WAIT``，每次请求现读（9.2）。

    只供测试缩短：不是正数、比 50 秒长、不是数（含 nan、inf）一律按 50 秒。
    """
    try:
        wait = float((os.environ.get("DSH_CONSOLE_STOP_WAIT") or "").strip())
    except ValueError:
        return STOP_WAIT
    return wait if 0 < wait <= STOP_WAIT else STOP_WAIT


def _config_failure(err, what):
    """管理台写配置的函数抛的 ``HubError`` → 约定的失败。锁被占着（管理台正在建 bot、改配置）是 409 busy。"""
    if err.status == 409 and err.error == "config_busy":
        return Refuse(409, "busy", "管理台正在改 bot 的配置，这次没有执行，请稍后重试")
    return Refuse(500, "internal", "写%s失败" % what)


def _write_enabled(bot_id, enabled):
    """把这只 bot 记成启用或停用，与管理台的停用开关同一种效果（9.2）。

    写法照 ``hub_addbot.set_enabled``：同一把锁，按行只改顶层 ``enabled`` 这一个值（注释、键序、人设原文
    一字不动），原子写。没有直接调它：它第一步过管理台的放行闸，闸里会读 ``life_config`` 指过去的旧系统
    配置（归属边界不许读，见 ``_own_bots``）。本服务的 bot 范围已由 ``_own_bots`` 定好，写的恒是新系统
    配置根下的 ``<id>.yml``。
    """
    from moments import hub_config         # 延迟 import：理由同 hub_addbot.set_enabled
    path = os.path.join(str(config_sources.dsh_dir()), bot_id + ".yml")
    try:
        with hub_addbot.hold_addbot_lock():
            with open(path, encoding="utf-8") as f:
                text = f.read()
            new_text = hub_config.set_scalar(text, hub_config.compose(text), "enabled", enabled, "bool")
            hub_config.atomic_write(new_text, path)
    except HubError as e:
        raise _config_failure(e, "这只 bot 的配置") from None
    except (OSError, ValueError, yaml.YAMLError):      # 读不了、不是 UTF-8、解析不出
        raise Refuse(500, "internal", "写这只 bot 的配置失败") from None


def _stop_bot(bot, seams, deadline):
    """「停」（约定 5.8，补充文件 9.2）：先记成停用，再停启动项，每秒查一次，等到确认不在跑。"""
    bot_id = bot["id"]
    if not bot["enabled"] and not _unit_running(bot_id, seams) and not any(
            bot_launchd.unit_state(bot_id, timer, seams) == "installed" for timer in (False, True)):
        return {"ok": True, "enabled": False, "running": False}       # 已是目标状态，不重复操作
    if bot["enabled"]:
        _write_enabled(bot_id, False)
    # 管理台停用开关同步时跑的就是这一段（sync_one 的停用方向）：两个单元 bootout，再把 plist 收起
    ok, reason = bot_launchd._stop_one(bot_id, seams)
    if not ok:
        raise Refuse(503, "unavailable", "已记成停用，但停止它的启动项失败：%s" % reason)
    wait = _stop_wait()
    until = min(time.monotonic() + wait, deadline)
    while _unit_running(bot_id, seams):
        left = until - time.monotonic()
        if left <= 0:
            raise Refuse(503, "unavailable", "已记成停用，也发了停止命令，但等了 %g 秒它仍在跑" % wait)
        time.sleep(min(STOP_POLL, left))
    return {"ok": True, "enabled": False, "running": False}


def _start_bot(bot, seams, deadline):
    """「启」（约定 5.8，补充文件 9.2）：先记成启用，再装载并启动启动项，不等心跳。"""
    bot_id = bot["id"]
    if not bot["enabled"]:
        _write_enabled(bot_id, True)
    # 管理台开关同步时启用方向的那一段，去掉等心跳：放回 plist、kickstart（回落 bootstrap）、装回定时单元
    ok, reason = bot_launchd._start_one(bot_id, seams)
    if not ok:
        raise Refuse(503, "unavailable", "已记成启用，但启动它的启动项失败：%s" % reason)
    # 再问一次 print 最多要一个 launchctl 超时：剩的时间不够就不问，按「还没在跑」报
    running = deadline - time.monotonic() > bot_launchd.LAUNCHCTL_TIMEOUT and _unit_running(bot_id, seams)
    return {"ok": True, "enabled": True, "running": running}


def _seam_dir(name, default):
    """测试接缝（9.3，生产不设）：环境变量给了用它，没给或为空用缺省；展开 ``~``、转成绝对路径。"""
    raw = (os.environ.get(name) or "").strip()
    return os.path.abspath(os.path.expanduser(raw or default))


def _switch_in(bot, import_from, seams, deadline):
    """切进导入（约定 5.9，补充文件 9.3、9.7），成功回 ``{"messages", "promises"}``。

    先后照 9.7：在跑（409）→ 配置缺 chat_id（422）→ 频道目录与会话目录的存在和软链（422）→ 读文件导入
    （500，到 ``deadline`` 还没读完 503）。只读约定列的两样，软链全部查完才读。哪一步没过都以 ``Refuse``
    抛出，此时还没启动、也没记成启用。水位线与写账本在 ``scripts/import_history.switch_in``。
    """
    import chat_history                     # 延迟 import：仓库根模块，只有切进要用
    from scripts import import_history
    bot_id = bot["id"]
    if _unit_running(bot_id, seams):
        raise Refuse(409, "running", "这只 bot 正在跑，先停下再切进")
    chat = bot["cfg"].get("chat_id")        # 主人的聊天编号取 bot 自己的配置，不读旧频道目录的 access.json
    if not (_text(chat) or type(chat) is int):
        raise _invalid("这只 bot 的配置缺 chat_id（主人的聊天编号），没法导入")
    channel = os.path.join(_seam_dir("DSH_CONSOLE_CHANNELS_DIR", DEFAULT_CHANNELS_DIR), import_from)
    # 会话目录名照 Claude Code 的规则由频道目录的绝对路径换算（9.7，与 chat_history 同一个函数）
    session = os.path.join(_seam_dir("DSH_CONSOLE_CLAUDE_PROJECTS_DIR", DEFAULT_PROJECTS_DIR),
                           chat_history._project_slug_for(channel))
    if os.path.islink(channel) or not os.path.isdir(channel):
        raise _invalid("旧频道目录不存在，或者是软链")
    promises = os.path.join(channel, ".promises.json")
    if os.path.islink(promises) or os.path.islink(session):
        raise _invalid("要读的旧记录里有软链，没有导入")
    try:
        names = sorted(os.listdir(session)) if os.path.isdir(session) else []   # 会话目录不在：没有聊天记录
    except OSError:
        raise Refuse(500, "internal", "导入旧记录这一步出错：会话目录读不了，没有启动") from None
    files = [os.path.join(session, name) for name in names if name.endswith(".jsonl")]
    if any(os.path.islink(path) for path in files):
        raise _invalid("要读的旧记录里有软链，没有导入")
    try:
        messages, promised = import_history.switch_in(
            bot_id, str(chat).strip(), promises if os.path.lexists(promises) else None,
            [path for path in files if os.path.isfile(path)], deadline=deadline)
    except import_history.ImportTimeout:
        raise Refuse(503, "unavailable", "导入旧记录没在时限内做完，没有启动，可以重试") from None
    except Exception as e:                  # noqa: BLE001  只记类名：异常消息里可能有路径或旧记录原文
        _log.error("switch-in import failed: %s", type(e).__name__)
        raise Refuse(500, "internal", "导入旧记录这一步出错：旧记录读不了或解析不出，没有启动") from None
    _log.info("switch-in imported %d messages, %d promises", messages, promised)    # 日志只记条数
    return {"messages": messages, "promises": promised}


def _set_enabled(bot_id):
    """启停（约定 5.8，补充文件 9.2）；带 ``import_from`` 时先切进导入（5.9，9.3）。整条请求 55 秒封顶。"""
    deadline = time.monotonic() + ENABLE_BUDGET - 1.0     # 留一秒收尾
    body = _json_object()
    enabled, import_from = body.get("enabled"), body.get("import_from")
    if type(enabled) is not bool or set(body) - {"enabled", "import_from"}:
        raise _invalid("请求体只能有 enabled（true 或 false）和可选的 import_from")
    if "import_from" in body:
        if not enabled:
            raise _invalid("停用时不能带 import_from")
        if not (isinstance(import_from, str) and BOT_ID_RE.match(import_from)
                and import_from not in (".", "..")):
            raise _invalid("import_from 只能用字母、数字、点、横线、下划线，1 到 64 个字符")
    bot = _require_bot(bot_id)
    seams = bot_launchd.resolve_seams()
    if not enabled:
        return _stop_bot(bot, seams, deadline)
    if "import_from" not in body:
        if bot["enabled"] and _unit_running(bot_id, seams):
            return {"ok": True, "enabled": True, "running": True}     # 已是目标状态，不重复操作
        return _start_bot(bot, seams, deadline)
    imported = _switch_in(bot, import_from, seams, deadline - START_RESERVE)
    # 5.10 的人设和技能同步不在本批（第 9 节），synced 恒为空
    return dict(_start_bot(bot, seams, deadline), imported=imported, synced=[])


def _group_id(value):
    return isinstance(value, str) and GROUP_ID_RE.match(value) is not None


def _human_ts(value):
    """9.6：``YYYY-MM-DDTHH:MM:SS.000Z``，毫秒位恒为 000，而且是真实存在的时刻。"""
    if not isinstance(value, str) or not HUMAN_TS_RE.match(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.000Z")
    except ValueError:
        return False
    return True


def _channel_file(bot, name):
    """这只 bot 频道目录下的 ``name``（``access.json``、``CLAUDE.md``；9.5、10.2、10.3）；不该碰的回 None。

    频道目录同找网关的那个（``_gateway_channel``）。它解析成真实路径之后必须在这只 bot 自己的家里
    （同管理台编辑页 ``hub_addbot._channel_dir`` 的落点校验），文件本身也不许是软链：
    不跟着软链去读写别处（另一套系统的频道目录在归属边界外）。
    """
    if not bot["channel"]:
        return None
    home = os.path.join(os.path.realpath(str(config_sources.dsh_home())), "bots", bot["id"])
    channel = os.path.realpath(bot["channel"])
    path = os.path.join(channel, name)
    if not channel.startswith(home + os.sep) or os.path.islink(path):
        return None
    return path


def _load_access(bot):
    """读 ``access.json`` → ``(路径, 整个对象)``。读不出、不是 JSON 对象：500（不当成空，也不凭空新建）。"""
    path = _channel_file(bot, "access.json")
    try:
        if path is None:
            raise ValueError("access.json is not ours to read")
        with open(path, "rb") as f:
            obj = json.loads(f.read().decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        raise Refuse(500, "internal", "这只 bot 的 access.json 读不出来，或者不是合法的 JSON") from None
    if not isinstance(obj, dict):
        raise Refuse(500, "internal", "这只 bot 的 access.json 内容认不出来")
    return path, obj


def _read_access(bot):
    """读 ``access.json`` → ``(路径, 整个对象, groups)``。没有 ``groups`` 键当作没进任何群。

    读不出、不是 JSON 对象、``groups`` 不是对象：500（约定 5.12.1：不当成空）。
    """
    path, obj = _load_access(bot)
    groups = obj.get("groups")
    groups = {} if groups is None else groups
    if not isinstance(groups, dict):
        raise Refuse(500, "internal", "这只 bot 的 access.json 内容认不出来")
    return path, obj, groups


def _group_view(entry):
    """约定 5.12.1 的群设置：六个键照原样，文件里没写的为 null，别的键一律不回。"""
    entry = entry if isinstance(entry, dict) else {}
    return {key: entry.get(key) for key in GROUP_KEYS}


def _groups_get(bot_id):
    """读群设置（约定 5.12.1，补充文件 9.5）。停用的 bot 照常能读；用户名问不到为 null。"""
    bot = _require_bot(bot_id)
    groups = _read_access(bot)[2]
    username = _gateway_health(bot["channel"]).get("username")
    return {"ok": True, "username": username if _text(username) else None,
            "groups": {chat_id: _group_view(entry) for chat_id, entry in groups.items()}}


def _check_policy(policy):
    """约定 5.12.2 的校验：只许六个键，值为 null 表示删掉这个键。"""
    if not isinstance(policy, dict) or set(policy) - set(GROUP_KEYS):
        raise _invalid("policy 要是一个对象，键只能是 requireMention、allowFrom、selfAliases、"
                       "otherBotUsernames、otherBotAliases、intro")
    for key, value in policy.items():
        if value is None:
            continue
        if key == "requireMention":
            ok = type(value) is bool
        elif key == "intro":
            ok = isinstance(value, str) and len(value) <= GROUP_INTRO_MAX and _utf8_size(value) is not None
        else:
            ok = isinstance(value, list) and len(value) <= GROUP_LIST_MAX and all(
                isinstance(item, str) and 1 <= len(item) <= GROUP_ITEM_MAX and _utf8_size(item) is not None
                and (key != "otherBotUsernames" or BOT_USERNAME_RE.match(item)) for item in value)
        if not ok:
            raise _invalid("policy 里的 %s 不合规" % key)


def _groups_post(bot_id):
    """进群、改设置、退群（约定 5.12.2，补充文件 9.5）。停用的 bot 也能改；不重启、不找网关。"""
    from moments import hub_config         # 延迟 import：同 _write_enabled
    body = _json_object()
    chat_id, join = body.get("chat_id"), body.get("join")
    if set(body) - {"chat_id", "join", "policy"}:
        raise _invalid("请求体只能有 chat_id、join、policy")
    if not _group_id(chat_id):
        raise _invalid("chat_id 要是 - 后面跟 1 到 20 位数字的字符串")
    if type(join) is not bool:
        raise _invalid("join 只能是 true 或 false")
    if not join and "policy" in body:
        raise _invalid("退群时不能带 policy")
    policy = body.get("policy", {})
    _check_policy(policy)
    bot = _require_bot(bot_id)
    try:
        # 与管理台新建页、编辑页写 access.json 同一把锁：读、改、写都在锁里，两边不会互相覆盖
        with hub_addbot.hold_addbot_lock():
            path, obj, groups = _read_access(bot)
            before = groups.get(chat_id, _ABSENT)
            if join:
                entry = dict(before) if isinstance(before, dict) else {}
                for key, value in policy.items():
                    if value is None:
                        entry.pop(key, None)
                    else:
                        entry[key] = value
                groups[chat_id] = entry
            else:
                groups.pop(chat_id, None)
            if groups.get(chat_id, _ABSENT) != before:      # 本来就是这样（比如本来就不在群里）就不写
                obj["groups"] = groups
                # 只改这一个群号的条目，别的键原样；原子写（临时文件改名），保持原文件权限
                hub_config.atomic_write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", path)
    except HubError as e:
        raise _config_failure(e, "这只 bot 的 access.json") from None
    return {"ok": True, "in_group": join, "policy": _group_view(groups[chat_id]) if join else None}


# ---------------------------------------------------------------- v0.8 人设、语音（补充文件第 10 节）

def _persona_version(raw):
    """A.1：人设文件原始字节 SHA-256 的前 16 位小写十六进制；文件不存在按空内容算。"""
    return hashlib.sha256(raw).hexdigest()[:16]


def _persona_path(bot):
    """频道目录下的 ``CLAUDE.md``；落在本系统家目录外、本身是软链：500，不读不写（10.2、10.6）。"""
    path = _channel_file(bot, "CLAUDE.md")
    if path is None:
        raise Refuse(500, "internal", "这只 bot 的人设文件不在它自己的目录里，或者是软链，不读也不改")
    return path


def _read_persona(path):
    """人设文件 → ``(原始字节, 权限)``，不存在是 ``(b"", None)``；读不出 500。

    ``O_NOFOLLOW``：查过之后才被换成软链的，也不跟过去读。
    """
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            return f.read(), stat.S_IMODE(os.fstat(f.fileno()).st_mode)
    except FileNotFoundError:
        return b"", None
    except OSError:
        raise Refuse(500, "internal", "这只 bot 的人设文件读不出来") from None


def _persona_get(bot_id):
    """读人设（v0.8 草案 A.1，补充文件 10.2）：原文全文与版本号。停用的 bot 照常能读；不是 UTF-8 回 500。"""
    raw = _read_persona(_persona_path(_require_bot(bot_id)))[0]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise Refuse(500, "internal", "这只 bot 的人设文件不是合法的 UTF-8 文本") from None
    return {"ok": True, "text": text, "version": _persona_version(raw)}


def _persona_put(bot_id):
    """改人设（v0.8 草案 A.2，补充文件 10.2、10.6）。停用的 bot 也能改；不重启（网关自己盯着这个文件）。

    先后：请求体（422）→ 名单（404）→ 落点（500）→ 锁里比版本号（409）→ 全文没变就不写 → 原文备份到
    ``<DSH_BOT_HOME>/backups/persona/<bot_id>/`` → 原子写、保持原权限（原来没有就 0600）→ 备份只留 5 份。
    写盘的就是 ``text`` 的 UTF-8 原样，去首尾空白只用来判断是不是空。
    """
    request.max_content_length = PERSONA_MAX_BODY + 1      # 分块传输的读到上限多一个字节即停
    body = _json_object(limit=PERSONA_MAX_BODY, too_big="请求体太大")
    text, version = body.get("text"), body.get("version")
    if set(body) != {"text", "version"}:
        raise _invalid("请求体只能有 text、version 两个键，而且都要给")
    if not isinstance(text, str) or not text.strip():
        raise _invalid("text 要是字符串，去掉首尾空白后不能为空")
    size = _utf8_size(text)
    if size is None or size > PERSONA_MAX:
        raise _invalid("text 按 UTF-8 不能超过 256 KiB")
    if not isinstance(version, str) or not PERSONA_VERSION_RE.match(version):
        raise _invalid("version 要是 16 位小写十六进制")
    bot = _require_bot(bot_id)
    path, data = _persona_path(bot), text.encode("utf-8")
    try:
        with hub_addbot.hold_addbot_lock():          # 与管理台编辑页写人设同一把锁（同进程内有效，9.7）
            old, mode = _read_persona(path)
            if _persona_version(old) != version:
                raise Refuse(409, "stale", PERSONA_STALE)
            if data == old:
                return {"ok": True, "version": version, "message": PERSONA_SAME}
            try:
                folder = None if mode is None else life_settings._backup(bot_id, old, kind="persona", suffix="")
                life_settings._replace(path, text, 0o600 if mode is None else mode)
            except OSError:
                raise Refuse(500, "internal", "写这只 bot 的人设失败") from None
            if folder:
                life_settings._rotate(folder, PERSONA_BACKUP_RE)      # 写盘成功之后才清理
    except HubError as e:
        raise _config_failure(e, "这只 bot 的人设") from None
    return {"ok": True, "version": _persona_version(data), "message": PERSONA_SAVED}


def _voice_view(obj):
    """B.1、B.2 的回应：``voiceId``、``voiceModel`` 照原样；没写的（或不是非空字符串）为 null，别的键一律不回。"""
    voice_id, model = obj.get("voiceId"), obj.get("voiceModel")
    return {"ok": True, "voice_id": voice_id if _text(voice_id) else None,
            "tts_model": model if _text(model) else None}


def _voice_get(bot_id):
    """读语音（v0.8 草案 B.1，补充文件 10.3）。停用的 bot 照常能读；access.json 读不出 500。"""
    return _voice_view(_load_access(_require_bot(bot_id))[1])


def _voice_put(bot_id):
    """改语音（v0.8 草案 B.2，补充文件 10.3、10.6）：给了的键整键替换、null 删键、没给的不动。

    只改这两个键，access.json 别的键原样，原子写、保持原权限，与群设置同一把锁；读不出 500，不新建、
    不覆盖。不重启：网关下次发语音时现读。停用的 bot 也能改。
    """
    from moments import hub_config         # 延迟 import：同 _write_enabled
    body = _json_object()
    if not body or set(body) - {"voice_id", "tts_model"}:
        raise _invalid("请求体只能有 voice_id、tts_model，至少给一个")
    voice_id, model = body.get("voice_id"), body.get("tts_model")
    if voice_id is not None and not (isinstance(voice_id, str) and VOICE_ID_RE.match(voice_id)):
        raise _invalid("voice_id 只能用字母、数字、下划线、横线，1 到 64 个字符；或者 null")
    if model is not None and model not in TTS_MODELS:
        raise _invalid("tts_model 只能是 %s 之一，或者 null" % "、".join(TTS_MODELS))
    bot = _require_bot(bot_id)
    try:
        with hub_addbot.hold_addbot_lock():
            path, obj = _load_access(bot)
            new = dict(obj)
            for api_key, file_key in (("voice_id", "voiceId"), ("tts_model", "voiceModel")):
                if api_key in body and body[api_key] is None:
                    new.pop(file_key, None)
                elif api_key in body:
                    new[file_key] = body[api_key]
            if new != obj:                  # 本来就是这样就不写
                hub_config.atomic_write(json.dumps(new, ensure_ascii=False, indent=2) + "\n", path)
    except HubError as e:
        raise _config_failure(e, "这只 bot 的 access.json") from None
    return _voice_view(new)


_READ_ROUTES = {PREFIX + "/meta": _meta, PREFIX + "/bots": _bots, PREFIX + "/providers": _providers}
#: ``/bots/<bot_id>/<这一段>`` 上的接口：方法 → 处理函数（键的先后就是 405 的 Allow 头）
_BOT_ROUTES = {
    "provider": {"POST": _set_provider},
    "restart": {"POST": _restart},
    "notify": {"POST": _notify},
    "life": {"GET": _life_get, "PUT": _life_put},
    "enabled": {"POST": _set_enabled},
    "groups": {"GET": _groups_get, "POST": _groups_post},
    "persona": {"GET": _persona_get, "PUT": _persona_put},
    "voice": {"GET": _voice_get, "PUT": _voice_put},
}


# ---------------------------------------------------------------- 管理台三页（页面服务）
#
# INTERFACE-页面服务.md。处理函数一个不重写：调的就是 8765 上注册的那几个函数对象（hub_routes）。
# 它们在 8765 上还依赖几样应用级的东西，这里照搬：JSON 键不重排、重启任务表、getMe 地址的形状检查
# （见 create_app）；/hub/api/ 的请求体上限、框架层出错回的 JSON、出站脱敏（见下面，原样出自 web.py）。
# 只有鉴权（上面的令牌，先于一切）、按原样路径对名单、导航条换成占位注释这三样与 8765 不同。

HUB_API_PREFIX = "/hub/api/"
#: 8765 对 /hub/api/ 请求体的上限（web.py 的 HUB_MAX_BODY）。适配接口自己的上限 MAX_BODY 不管这里。
HUB_MAX_BODY = 1024 * 1024
#: 8765 在 /hub/api/ 下对这几种出错回 ``{"error": 码名}``（web.py 的 _HUB_ERROR_CODES），别的照框架默认。
HUB_ERROR_CODES = {404: "not_found", 405: "method_not_allowed", 413: "payload_too_large", 500: "internal"}

#: 名单（INTERFACE-页面服务 §2，只有这些，方法也只有这些）：``(路径, {方法: 8765 上的处理函数})``。
#: 路径照 8765 的规则串写：``<x>`` 是一段、不许为空；``<dmname:x>`` 连空段也收（8765 预览接口的转换器，
#: 空名字由处理函数回 400）。一个路径对得上两条时先试排在前面的：``bots/restart/<job_id>`` 排在
#: ``bots/<bot_id>/…`` 之前，同框架「写死的段优先」；方法对不上接着往下试，也同框架。
_HUB_ROUTES = [
    ("/hub/bots", {"GET": hub_routes.hub_bots_page}),
    ("/hub/addbot", {"GET": hub_routes.hub_addbot_page}),
    ("/hub/dsh-model", {"GET": hub_routes.hub_dsh_model_page}),
    ("/hub/api/bots", {"GET": hub_routes.bots_list, "POST": hub_routes.bots_create}),
    ("/hub/api/bots/templates", {"GET": hub_routes.bots_templates}),
    ("/hub/api/bots/test_token", {"POST": hub_routes.bots_test_token}),
    ("/hub/api/bots/restart", {"POST": hub_routes.bots_restart}),
    ("/hub/api/bots/restart/<job_id>", {"GET": hub_routes.bots_restart_status}),
    ("/hub/api/bots/<bot_id>/edit", {"GET": hub_routes.bots_edit_read,
                                      "POST": hub_routes.bots_edit_save}),
    ("/hub/api/bots/<bot_id>/enabled", {"POST": hub_routes.bots_set_enabled}),
    ("/hub/api/bots/<bot_id>/restart", {"POST": hub_routes.bots_restart_one}),
    ("/hub/api/dsh-model", {"GET": hub_routes.dsh_model_list}),
    ("/hub/api/dsh-model/<bot_id>", {"POST": hub_routes.dsh_model_set}),
    ("/hub/api/dsh-model/<bot_id>/effort", {"GET": hub_routes.dsh_model_effort_get,
                                             "POST": hub_routes.dsh_model_effort_set}),
    ("/hub/api/dsh-model/provider/save", {"POST": hub_routes.dsh_model_provider_save}),
    ("/hub/api/dsh-model/provider/<dmname:name>/preview",
     {"POST": hub_routes.dsh_model_provider_preview}),
    ("/hub/api/dsh-model/provider/<name>/models", {"POST": hub_routes.dsh_model_provider_models}),
    ("/hub/api/dsh-model/provider/<name>/refresh", {"POST": hub_routes.dsh_model_provider_refresh}),
    ("/hub/api/dsh-model/provider/<name>/remove", {"POST": hub_routes.dsh_model_provider_remove}),
    ("/hub/api/dsh-model/provider/<name>/test", {"POST": hub_routes.dsh_model_provider_test}),
]
_HUB_TABLE = [(rule.split("/"), methods) for rule, methods in _HUB_ROUTES]


def _segment_value(raw):
    """原样路径里的一段 → 处理函数拿到的值，与 8765 上框架给的逐字相同。

    WSGI 环境里的串是「按 latin-1 装着的字节」，框架解路径时先还原成字节、再按 UTF-8 解百分号编码
    （解不出的字节换成替换字符）；这里对这一段做同样两步。先切段再解码：段里的 ``%2F`` 解出来
    仍在这一段里，交给处理函数自己的名字校验去拒，不会变成分隔符把请求带到别的路由上。
    """
    try:
        raw = raw.encode("latin-1").decode("utf-8", "replace")
    except UnicodeEncodeError:              # 不是服务器给的样子：保持原样，名字校验会拒
        pass
    return unquote(raw, encoding="utf-8", errors="replace")


def _hub_match(path, method):
    """原样路径与方法 → ``(处理函数, 路径参数)``。逐段比对，不规范化（末尾斜杠、双斜杠、
    ``%62ots`` 这种编过码的写死段都对不上）。路径不在名单 → 404，路径在而方法不在 → 405。"""
    parts = path.split("/")
    allowed = set()
    for rule, methods in _HUB_TABLE:
        if len(rule) != len(parts):
            continue
        args = {}
        for want, got in zip(rule, parts):
            if want.startswith("<") and want.endswith(">"):
                kind, _, name = want[1:-1].rpartition(":")
                if got == "" and kind != "dmname":
                    break
                args[name] = _segment_value(got)
            elif want != got:
                break
        else:
            if method in methods:
                return methods[method], args
            allowed.update(methods)
    if allowed:
        raise Refuse(405, "method_not_allowed", "这个接口不支持这种请求方式",
                     headers={"Allow": ", ".join(sorted(allowed))})
    raise Refuse(404, "not_found", "没有这个接口")


def _hub_failure(api, err):
    """8765 框架层对同一种出错的回应（web.py 的 _hub_error_handler）。"""
    if api and err.code in HUB_ERROR_CODES:
        resp = jsonify({"error": HUB_ERROR_CODES[err.code]})
        resp.status_code = err.code
        return resp
    return err.get_response(request.environ)


def _hub_scrub(resp):
    """8765 出站的第二层脱敏（web.py 的 _hub_api_scrub），同一个函数、同一个判据。"""
    if resp.direct_passthrough or not resp.is_json:
        return resp
    body = resp.get_json(silent=True)
    if body is None:
        return resp
    scrubbed, changed = redact.scrub_fields(body)
    if changed:
        resp.set_data(json.dumps(scrubbed, ensure_ascii=False))
    return resp


def _hub_respond(path):
    """名单内的 ``/hub/...``：已过鉴权，调 8765 上同一个处理函数，照 8765 的样子收尾。"""
    handler, args = _hub_match(path, request.method)
    api = path.startswith(HUB_API_PREFIX)
    try:
        if api:
            if (request.content_length or 0) > HUB_MAX_BODY:     # 8765 的体积闸（web.py 的 _hub_body_limit）
                raise RequestEntityTooLarge()
            # 应用级的 MAX_CONTENT_LENGTH 是给适配接口的；这几条接口按 8765 的上限读请求体，
            # 分块传输的读到上限就停（超了框架抛 413，下面回同一个 JSON）。
            request.max_content_length = HUB_MAX_BODY
        resp = current_app.make_response(handler(**args))
    except HTTPException as e:
        resp = _hub_failure(api, e)
    except Exception as e:                  # noqa: BLE001  异常消息可能带请求体里的令牌或密钥，只记类名
        _log.error("console page internal error: %s", type(e).__name__)
        resp = _hub_failure(api, InternalServerError())
    return _hub_scrub(resp) if api else resp


def _addbot_base_error():
    """``HUB_TELEGRAM_API_BASE`` 形状不对时的那句话，没问题是 None。

    与 8765 同一个判定、同一个时机（建应用时判一次）。8765 那边挂在蓝图注册上（hub_routes 的
    ``_ab_check_base``，被 record_once 装饰后模块里那个名字是 None，调不到），这里照它写。
    """
    try:
        hub_addbot.api_base()
    except RuntimeError as e:
        return str(e)
    return None


# ---------------------------------------------------------------- 应用与命令行

def create_app():
    """适配服务的 Flask 应用。不注册任何 URL 规则：全部请求在 ``before_request`` 里由
    ``_respond`` 接走（鉴权先于路由；路由按原样路径自己比对，见模块说明）。"""
    app = Flask(__name__)
    # 请求体上限（安审 S4）。带 Content-Length 的在 _json_object 里先挡；分块传输的没有这个头，
    # 框架读这种流时最多读到这个数就停（不抛错、不读进更多）。多给 1 个字节，_json_object 才分得清
    # 「正好 MAX_BODY」与「超了」。
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY + 1
    # 管理台三页的处理函数在 8765 上依赖的应用级设置（适配接口自己的响应不经 jsonify，不受影响）：
    app.json.sort_keys = False              # 同 web.py：JSON 的键照处理函数给的顺序，不重排
    app.extensions["hub_restart_jobs"] = bots_client.RestartJobs()      # 同 hub_routes 的 _init_restart_jobs
    app.extensions["hub_addbot_base_error"] = _addbot_base_error()
    # requests 底下的 urllib3 在调试级别会把整条地址记进日志，getMe 的地址里就是请求体里的 Telegram 令牌
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    @app.context_processor
    def _console_nav():
        # 渲染开关：_nav.html 见到它就把导航条换成一行占位注释（壳换成它的导航）；8765 不设，照旧
        return {"console_nav": True}

    @app.before_request
    def _entry():
        return _respond()

    @app.errorhandler(Exception)
    def _framework_error(_err):
        # 走不到 before_request 的框架级出错（极少见）：同样先看鉴权，再固定回 500。
        try:
            allowed = _authorized()
        except Exception:                   # noqa: BLE001
            allowed = False
        return _json(INTERNAL, 500) if allowed else _json(UNAUTHORIZED, 401)

    return app


def listen_address():
    """``(host, port, 原因)``：监听地址与端口；不合规时前两项为 None、第三项是一句原因。"""
    host = (os.environ.get("DSH_CONSOLE_HOST") or "").strip() or DEFAULT_HOST
    if host not in LOOPBACK_HOSTS:
        return None, None, "DSH_CONSOLE_HOST 只许是 127.0.0.1、::1 或 localhost，拒绝启动"
    raw = (os.environ.get("DSH_CONSOLE_PORT") or "").strip() or str(DEFAULT_PORT)
    if not re.match(r"^[0-9]{1,5}\Z", raw) or not 1 <= int(raw) <= 65535:
        return None, None, "DSH_CONSOLE_PORT 必须是 1 到 65535 的整数，拒绝启动"
    return host, int(raw), None


def main():
    host, port, why = listen_address()
    if why:
        print(why, file=sys.stderr)
        return 2
    if read_token() is None:
        print("令牌文件不存在、读不了或是空的，拒绝启动：%s" % token_file(), file=sys.stderr)
        return 3
    # 访问日志会把整条请求行（含查询串）记下来，有人把令牌拼进地址就落盘了：只留错误级别。
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    create_app().run(host=host, port=port, debug=False, load_dotenv=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
