# -*- coding: utf-8 -*-
"""dsh 这一半的适配服务：把本系统的 bot 用本机 HTTP 接口交给另一套系统的管理台（壳）。

契约：``.devflow/INTERFACE-适配约定-v0.4.md``（两边定稿）与 ``INTERFACE-适配服务.md``（本系统补充）；
生活设定的做法在 ``INTERFACE-生活设定.md``，逻辑在 ``moments/life_settings.py``。
v0.6、v0.7 的启停、切进导入、同跑提示、群设置、``group_turn`` 照 ``INTERFACE-适配约定-v0.7摘录.md``
与补充文件第 9 节；切进导入的水位线与写账本在 ``scripts/import_history.py``。
v0.8 的人设、语音照 ``INTERFACE-适配约定-v0.8-dsh侧.md`` 与补充文件第 10 节；人设的备份、原子写用 ``life_settings`` 那一套。
v0.9 的技能读写、语音的两个语言键照 v0.9 两份草案（``INTERFACE-适配约定-v0.9-*.md``）与补充文件第 11 节；技能存盘同人设。
v1.0 的新建、归档与恢复、删除、显示名照 ``INTERFACE-适配约定-v1.0-dsh侧.md`` 与补充文件第 12 节；新建与显示名调管理台
新建页、编辑页那几个函数（``hub_addbot``），停 bot 用 9.2 的「停」。
v1.0 的补充（列表四个键、迁出标记、新建新字段、归档 shell_meta、删除先抹 .env、令牌转交、停着换供应商）照补充文件第 13 节。
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
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import socket
import stat
import sys
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import unquote

import yaml
from flask import Flask, Response, current_app, jsonify, request
from werkzeug.exceptions import HTTPException, InternalServerError, RequestEntityTooLarge

import gateway_client
from moments import bot_launchd, bots_client, config_sources, hub_addbot, hub_routes, life_settings, redact
from moments import features as feature_switch
from moments import handoff, skill_templates
from moments.provider_model import HubError

PREFIX = "/adapter/v1"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 17990
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
DEFAULT_TOKEN_FILE = "~/.config/claude-dsh-console/adapter.token"

EFFORT_LEVELS = ["off", "low", "high", "max"]
CAPABILITIES = {
    "bot.restart.graceful": True,
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
    "bot.skills": True,
    "bot.create": True,
    "bot.archive": True,
    "bot.delete": True,
    "bot.profile": True,
    "bot.token_handoff": True,
    "bot.groups_discovered": True,
    "bot.features": True,
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
#: 等空闲重启：后台查健康接口的间隔（秒），以及一条排队最多等多久（秒），超时放弃。
GRACEFUL_POLL = 1.0
GRACEFUL_QUEUE_TTL = 1800.0
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
#: 日志看板重建：launchd 定时单元（每 5 分钟给启用中的 bot 补建 tmux 会话 ``tg-<bot>-dsh``）。
#: 启用后立刻踢一次，用户不必等下一轮。只碰这一个 label，不碰其它单元。
TMUX_LOGS_LABEL = "com.dsh-bot.tmux-logs"
#: 切进导入（9.3）：旧频道目录根、Claude Code 会话目录根的缺省（测试接缝见 _switch_in）
DEFAULT_CHANNELS_DIR = "~/.claude/channels"
DEFAULT_PROJECTS_DIR = "~/.claude/projects"
#: 切换底层时人设与技能的中立交接目录（约定 5.10），两边的适配器都读写这里
DEFAULT_HANDOFF_DIR = "~/.config/claude-dsh-console/handoff"
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
#: 接口里的键 → access.json 里的键（v0.8 草案 B 两个，11.3 加两个语言键）；先后就是回应里的先后
VOICE_KEYS = (("voice_id", "voiceId"), ("tts_model", "voiceModel"),
              ("text_language", "textLanguage"), ("voice_language", "voiceLanguage"))
#: 11.3：语言名去掉首尾空白后的字符数上限，与整个值里不许出现的字符（换行与 ``str.splitlines`` 同一个口径）
LANGUAGE_MAX = 20
LANGUAGE_BAD_RE = re.compile(r"[\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029\[\]<>{}`|]")
#: 11.2：通用技能名表；文件是 <频道目录>/skills/skill-<通用名>/SKILL.md
SKILL_NAMES = ("image", "voice", "group", "moments", "proactive", "nsfw", "gif")
SKILL_STALE = "技能刚被别处改过，请刷新后再改"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
#: v1.0（补充文件第 12 节）：新建整条请求的时限与请求体的键；显示名的字数上限；归档与废纸篓的目录名、清单
CREATE_BUDGET = 55.0
#: 新建装启动项失败时撤回要的时间：两条 bootout，每条最多一个 launchctl 超时。装启动项要在整条时限减去它之前做完。
UNDO_RESERVE = 2 * bot_launchd.LAUNCHCTL_TIMEOUT
CREATE_KEYS = ("bot_id", "telegram_token", "chat_id", "display_name", "persona_text", "persona_template",
               "fish_voice_id", "fish_api_key", "port", "enabled", "person", "telegram_username",
               "telegram_user_id", "purpose", "features", "appearance")
#: 约定 v1.1 5.18.1：外貌去首尾空白后最多这么多个码点；不许出现的几样（注释标记与模板占位括号）
APPEARANCE_MAX = 500
APPEARANCE_BANNED = ("<!--", "-->", "{{", "}}")
PROFILE_NAME_MAX = 32
UTC_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
ARCHIVE_ID_RE = re.compile(r"^([a-z][a-z0-9_]{1,31})-([0-9]{8}T[0-9]{6}Z)\Z")
MANIFEST = "manifest.json"
DEFAULT_TRASH_DIR = "~/.Trash"
DELETED = "已删除本地数据。Telegram 上的 bot 照样能用，以后可以用新的人设和对应的 bot 令牌再加回来"   # 草案 C 原文
#: v1.0 补充（补充文件第 13 节）
#: 13.6：op_id 的写法（第一组是种类）与五种种类；令牌转交的限频窗口（秒）与记录条数上限
OP_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-([a-z]{4,7})-[0-9a-f]{8}\Z")
OP_KINDS = ("create", "switch", "archive", "restore", "delete")
OP_ID_BAD = "op_id 的写法不对：要像 20261015T051700Z-switch-1a2b3c4d，种类是 create、switch、archive、restore、delete 之一"
HANDOFF_WINDOW = 600
HANDOFF_KEEP = 200
HANDOFF_FILE = "token-handoff.json"
#: .env 里令牌那一行（整串取出来，只交给令牌转交的成功响应）
ENV_TOKEN_LINE_RE = re.compile(r"^[ \t]*TELEGRAM_BOT_TOKEN[ \t]*=[ \t]*[\"']?([^\"'\s]+)[\"']?[ \t]*$", re.M)
#: 13.2：迁出标记的文件名、认得的去向、一行的写法
MOVED_MARKER = ".moved"
MOVED_TARGETS = ("pending", "claude")
MOVED_LINE_RE = re.compile(r"^to=([a-z]+) op=(\S+) at=(\S+)\n?\Z")
MOVED_REFUSED = "这只 bot 已标记迁到另一种底层，不能在这边启用；要改回来，先发一次带 moved_to: null 的停用清掉迁出标记"
#: 13.3：新建的编号规则（person 同）、purpose 的两种取值、自动挑端口的范围
CREATE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}\Z")
CREATE_PURPOSES = ("new", "switch_in")
CREATE_PORTS = range(17850, 17900)
#: 13.4：shell_meta 序列化后的上限；归档请求体按 JSON 转义最坏 6 倍放宽；manifest 带上 shell_meta 后读的上限
SHELL_META_MAX = 64 * 1024
ARCHIVE_MAX_BODY = 6 * SHELL_META_MAX + 1024
MANIFEST_MAX = 1024 * 1024
QUEUED = "已保存，下次启用时生效"      # 13.7
#: 令牌转交的记录读、判、写在同一把锁里（进程内）
_handoff_lock = threading.Lock()

#: 配置文件没写 brain 时网关自己用的默认值（gateway/src/config.ts 的 parseBrain）。
CONFIG_DEFAULT_PROVIDER = "deepseek-official"
CONFIG_DEFAULT_MODEL = "deepseek-flash"

#: 网关 ``POST /v1/model`` 把 spec 截到 200 个字符（管理台模型页同一个上限）：超了就别发。
MAX_SPEC = 200
#: 网关 ``key`` 的四种取值里算「有密钥」的两种（补充文件 5.1：ok 已配，none 这家不需要）。
KEY_READY = ("ok", "none")
#: 供应商清单存档（``<DSH_BOT_HOME>/state/`` 下）：bot 全停时拿它当清单。清单没变时距上次存超过这么多秒才重写；
#: ``saved_at`` 的写法（UTC 到秒，带 Z）
CATALOG_FILE = "provider-catalog.json"
CATALOG_REFRESH = 600
CATALOG_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

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

#: 等空闲重启的排队（只在内存，进程重启就丢）。键是 bot id，值是 {"deadline": monotonic 时刻}。
#: 同一只 bot 只留一条；后台线程 _graceful_worker 每秒查一次，空了就退出。
_graceful_pending = {}
_graceful_guard = threading.Lock()
_graceful_worker_running = [False]


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
    methods = _TOP_ROUTES.get(path)
    if methods is not None:
        _require_method(method, ", ".join(methods))
        return methods[method]()
    head = PREFIX + "/bots/"
    if path.startswith(head):
        parts = path[len(head):].split("/")
        if len(parts) == 2:
            methods = _BOT_ROUTES.get(parts[1])
        elif len(parts) == 3 and parts[1:] == ["groups", "discovered"]:     # 刚发现的群（约定 v1.1 5.20.2）
            methods = _DISCOVERED_ROUTES
            parts = parts[:2]
        else:                               # 只有技能多一段：/bots/<bot_id>/skills/<通用名>（11.2）
            methods = _SKILL_ROUTES if len(parts) == 3 and parts[1] == "skills" else None
        if methods is not None:
            _require_method(method, ", ".join(methods))
            if not _valid_bot_id(parts[0]):
                refuse = _invalid("bot 编号不合规：只能用字母、数字、点、下划线、横线，1 到 64 个字符")
                if parts[1] == "life":
                    refuse.fields = {}      # 生活设定的 422 一律带 fields（INTERFACE-生活设定 第 1 节）
                raise refuse
            return methods[method](parts[0], *parts[2:])
    head = PREFIX + "/archives/"
    if path.startswith(head):               # 12.3：/archives/<archive_id>/restore
        parts = path[len(head):].split("/")
        if len(parts) == 2 and parts[1] == "restore":
            _require_method(method, "POST")
            return _restore(parts[0])
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
        result = _dispatch()
        return result if isinstance(result, Response) else _json(result)      # 要带响应头的（13.6）自己组好
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


def _bot_row(bot, seams, moved_to=None):
    """列表里的一行（约定 5.2 的八个键，v0.6 起加 ``person``，13.1 加 ``enabled``、``polling``、``username``、
    ``moved_to``）。在线程池里跑：只用传进来的值，不读环境与配置（迁出标记由 ``_bots`` 先读好传进来）。"""
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
    polling = _polling(bot_id, seams)
    # 9.4、13.1：只问在跑（state 为 running）的 bot 的健康接口，同跑提示与用户名都从这一次里取；
    # 停用的不问（9.4），用户名取配置里记下的
    health = _gateway_health(bot["channel"]) if state == "running" else {}
    detail = {}
    if state == "running" and health.get("conflict") is True:
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
            "provider_source": source, "pending": None, "detail": detail,
            "enabled": bot["enabled"], "polling": polling, "username": _username(health, bot["cfg"]),
            "moved_to": moved_to}


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
    moved = {bot["id"]: _moved_to(bot["id"]) for bot in bots}
    with ThreadPoolExecutor(max_workers=min(MAX_ROW_WORKERS, len(bots))) as pool:
        futures = [pool.submit(_bot_row, bot, seams, moved[bot["id"]]) for bot in bots]
        return {"ok": True, "bots": [f.result() for f in futures]}


def _provider_catalog():
    """4.3 的供应商列表 ``[{"id", "name", "has_key", "models"}]`` 与它存下的时间：回 ``(清单, cached_at)``。

    补充文件 4.3、5.1：所有 bot 共用一份清单，按 bot 编号的字典序问第一只连得上的，读的是
    ``GET /v1/model``（映射见 ``_catalog``），不读任何本地的凭据文件。停用的 bot、没有网关
    文件的 bot 跳过，不发请求。读到了先存进存档（``_save_catalog``）再回，``cached_at`` 为 None。

    - 没连上、发出后断了或超时：这是只读请求，换下一只问不会多写什么；
    - 连上了但回的东西认不出（不是 JSON、状态码不对、形状不对）：不再问别的，也不拿存档顶替，抛，外层回 500；
    - 一只都问不到（都停用了、都没在跑、一只 bot 都没有）：改用存档（``_load_catalog``），``cached_at``
      是存档的 ``saved_at``；存档没有、读不出、形状不对，503。

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
        catalog = _catalog(info)
        _save_catalog(catalog)
        return catalog, None
    saved = _load_catalog()
    if saved is None:
        raise Refuse(503, "unavailable", NO_RUNNING_BOT)
    return saved


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


def _catalog_path():
    return os.path.join(str(config_sources.dsh_home()), "state", CATALOG_FILE)


def _catalog_time(stamp):
    """存档的 ``saved_at``（形如 ``2026-10-17T02:15:00Z``）→ 带 UTC 时区的时刻。

    不是这个写法（补零、到秒、带 Z，strptime 放过的不补零写法也算不对）或不是真实存在的时刻回 None。
    """
    try:
        moment = datetime.strptime(stamp, CATALOG_TIME_FORMAT)
    except (TypeError, ValueError):
        return None
    return moment.replace(tzinfo=timezone.utc) if moment.strftime(CATALOG_TIME_FORMAT) == stamp else None


def _load_catalog():
    """读供应商清单存档 → ``(清单, saved_at)``；没有、读不出、不是 JSON、任何一处形状不对回 None，整份不用。

    读法同生活设定（``life_settings._read``）：不跟软链、不阻塞地打开，只认普通文件，最多 1 MiB。所以路径是软链
    （哪怕指向合规的清单）、目录、FIFO，或文件超过 1 MiB，都算读不出，不会卡在 FIFO 上。

    形状：顶层只能是 ``saved_at``、``providers`` 两个键，``saved_at`` 是 ``_catalog_time`` 认的写法，
    ``providers`` 是数组（空数组也算合规）；每项只能是 ``id``、``name``、``has_key``、``models`` 四个键，
    id、name 是非空字符串，has_key 是真假值，models 是字符串数组。键一个不多：存档里的项原样回给调用方，
    多出来的键不能带出去。
    """
    raw, _st = life_settings._read(_catalog_path())
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or set(data) != {"saved_at", "providers"} \
            or _catalog_time(data["saved_at"]) is None or not isinstance(data["providers"], list):
        return None
    for p in data["providers"]:
        if not isinstance(p, dict) or set(p) != {"id", "name", "has_key", "models"} \
                or not _text(p["id"]) or not _text(p["name"]) or not isinstance(p["has_key"], bool) \
                or not isinstance(p["models"], list) or not all(isinstance(m, str) for m in p["models"]):
            return None
    return data["providers"], data["saved_at"]


def _save_catalog(catalog):
    """刚从在跑的网关读到的清单存进存档，供 bot 全停时用：只有 ``saved_at``（此刻的 UTC 时间）与这份清单，
    0600，原子写，``state/`` 不在就建。

    清单是空数组、或每一家都没有模型（网关拿不到 choices 时就是这样），不写：这样的清单拿来核对什么都过不了，
    不能顶掉更早那份有用的；已有的存档一个字节不动，``saved_at`` 也不刷新。只要有一家有模型就照常往下走。
    清单与存档里的一样、上次存的又没超过 10 分钟，就不重写；存档读不出（软链、不是普通文件、太大）当作没有，
    照常写，原子替换换掉的是路径上的软链本身，不跟过去写。写不成只记异常类名，这次读到的清单照常回；
    日志不写清单。JSON 按默认的 ASCII 转义写：网关报来的名字里就算有落单的代理码位也写得出、读回来一字不差。
    """
    try:
        if not any(p["models"] for p in catalog):
            return
        now = datetime.now(timezone.utc).replace(microsecond=0)
        saved = _load_catalog()
        if saved is not None and saved[0] == catalog \
                and 0 <= (now - _catalog_time(saved[1])).total_seconds() <= CATALOG_REFRESH:
            return
        path = _catalog_path()
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        text = json.dumps({"saved_at": now.strftime(CATALOG_TIME_FORMAT), "providers": catalog}) + "\n"
        life_settings._replace(path, text, 0o600)
    except Exception as e:                  # noqa: BLE001  存档只是备用：写不成不连累这次已读到的清单
        _log.error("provider catalog snapshot failed: %s", type(e).__name__)


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
    catalog, cached_at = _provider_catalog()
    body = {"ok": True, "providers": catalog, "default": {"label": DEFAULT_LABEL}}
    if cached_at is not None:               # bot 全停、用的是存档：带上存档存下的时间
        body["cached_at"] = cached_at
    return body


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
    if not bot["enabled"]:                  # 13.7（第 3 步修订）：停着的不再 409，核对过写进配置，下次启用时生效
        return _queue_provider(bot, body, has_provider, has_effort)
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


def _turns_running(channel):
    """这只 bot 的网关 ``GET /v1/health`` 里正在进行的回合数；问不到按 0（空闲）。

    读超时沿用 ``GATEWAY_READ_TIMEOUT``。网关没在跑、连不上、超时、回的不是对象、
    ``turns_running`` 不是整数，都算问不到。
    """
    if not gateway_client.available(channel):
        return 0
    try:
        _code, got = gateway_client._call(channel, "GET", "/v1/health", timeout=GATEWAY_READ_TIMEOUT)
    except Exception:                       # noqa: BLE001  问不到按空闲处理
        return 0
    turns = got.get("turns_running") if isinstance(got, dict) else None
    return turns if type(turns) is int and turns > 0 else 0


def _enabled_bot(bot_id):
    """后台线程用：名单里还有这只 bot 且仍启用就返回它，否则 None（不抛）。"""
    for bot in _own_bots():
        if bot["id"] == bot_id:
            return bot if bot["enabled"] else None
    return None


def _queue_graceful(bot_id):
    """登记一条等空闲重启；同一只 bot 只留一条，重复请求不排第二次。"""
    with _graceful_guard:
        if bot_id in _graceful_pending:
            return
        _graceful_pending[bot_id] = {"deadline": time.monotonic() + GRACEFUL_QUEUE_TTL}
        start = not _graceful_worker_running[0]
        if start:
            _graceful_worker_running[0] = True
    _log.info("graceful restart queued: bot=%s", bot_id)
    if start:
        threading.Thread(target=_graceful_worker, name="graceful-restart", daemon=True).start()


def _take_graceful(bot_id, entry):
    """把这一条排队摘下来；已被取消或换成新的一条时不动，成功才去重启。"""
    with _graceful_guard:
        if _graceful_pending.get(bot_id) is not entry:
            return False
        del _graceful_pending[bot_id]
        return True


def _cancel_graceful(bot_id):
    """取消这只 bot 已排队的重启（force 与发现已空闲的立即重启都走它）。"""
    with _graceful_guard:
        _graceful_pending.pop(bot_id, None)


def _graceful_tick(bot_id, entry):
    """看一眼这只 bot：还启用、还没超时、网关变空闲或问不到就执行重启。"""
    if time.monotonic() >= entry["deadline"]:
        if _take_graceful(bot_id, entry):
            _log.info("graceful restart abandoned: bot=%s reason=timeout", bot_id)
        return
    bot = _enabled_bot(bot_id)
    if bot is None:
        if _take_graceful(bot_id, entry):
            _log.info("graceful restart abandoned: bot=%s reason=disabled", bot_id)
        return
    if _turns_running(bot["channel"]) > 0:
        return
    if not _take_graceful(bot_id, entry):
        return                              # 等的期间被 force 或别的立即重启取消了
    try:
        result = _restart_now(bot_id)
    except Refuse:                          # 同一只 bot 正在重启（409 busy）：这条不再补
        _log.info("graceful restart failed: bot=%s reason=busy", bot_id)
        return
    if result["ok"]:
        _log.info("graceful restart done: bot=%s", bot_id)
    else:
        _log.info("graceful restart failed: bot=%s reason=%s", bot_id, result["message"])


def _graceful_worker():
    """后台守护线程：每秒扫一遍排队，全部处理完就退出（下次登记再起）。"""
    while True:
        with _graceful_guard:
            if not _graceful_pending:
                _graceful_worker_running[0] = False
                return
            entries = list(_graceful_pending.items())
        time.sleep(GRACEFUL_POLL)
        for bot_id, entry in entries:
            try:
                _graceful_tick(bot_id, entry)
            except Exception as e:          # noqa: BLE001  一只出事不拖累别的排队，也不许线程死掉
                _log.error("graceful restart failed: bot=%s error=%s", bot_id, type(e).__name__)
                _take_graceful(bot_id, entry)


def _restart_now(bot_id):
    """立刻重启（force 与等空闲后的后台执行共用），回包形状不变。"""
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


def _restart(bot_id):
    body = _json_object(allow_empty=True)
    if "force" in body and type(body["force"]) is not bool:
        raise _invalid("force 只能是 true 或 false")
    bot = _require_bot(bot_id)
    _require_enabled(bot)
    if body.get("force") is True:
        _cancel_graceful(bot_id)            # 强制重启立刻做，已排队的那次不再做
        return _restart_now(bot_id)
    if _turns_running(bot["channel"]) > 0:
        _queue_graceful(bot_id)
        return {"ok": True, "status": "queued",
                "message": "这只 bot 正在回复，已记下，等这一轮结束再重启"}
    _cancel_graceful(bot_id)                # 已经空闲：这次立即重启，不留下排队的一条
    return _restart_now(bot_id)


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
    # 约定 v1.1 5.19.6：朋友圈、群聊功能关着的，这两类通知都回 409 feature_off，不投
    switch = feature_switch.read(bot["cfg"], bot["channel"])[0]
    if source == "group_turn" and not switch["groups"]:
        raise Refuse(409, "feature_off", "这只 bot 的群聊功能关着，没有投递")
    if source != "group_turn" and not switch["moments"]:
        raise Refuse(409, "feature_off", "这只 bot 的朋友圈功能关着，没有投递")
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
    """「停」（约定 5.8，补充文件 9.2）：先记成停用，再停启动项，每秒查一次，等到确认不在跑。

    在不在跑按 ``_polling``（约定 v1.0：与 5.2 的 polling 同一判定，宁可误报）：launchctl 答不上来算「可能还在跑」，
    照样等，到上限回 503，不报 ``running: false``——壳见到 false 就会去启另一边。"""
    bot_id = bot["id"]
    if not bot["enabled"] and _polling(bot_id, seams) is False and not any(
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
    while _polling(bot_id, seams) is not False:
        left = until - time.monotonic()
        if left <= 0:
            raise Refuse(503, "unavailable", "已记成停用，也发了停止命令，但等了 %g 秒它仍在跑（或查不到它停没停）" % wait)
        time.sleep(min(STOP_POLL, left))
    return {"ok": True, "enabled": False, "running": False}


def _kickstart_tmux_logs(seams):
    """启用成功后异步踢一次日志看板重建：``launchctl kickstart gui/<uid>/com.dsh-bot.tmux-logs``。

    看板任务每 5 分钟自己补建 ``tg-<bot>-dsh`` 的 tmux 会话；启用后马上踢一次，用户不必等下一轮。
    在后台线程里调用、不等结果，不增加启用接口的耗时；任务没装、launchctl 报错或平台没有
    launchctl 都只记一条日志，不影响回包与错误契约。``seams`` 是受理时定下的那一份（§1a），
    后台线程不读环境。
    """
    def run():
        try:
            code, _text, why = bot_launchd._run(       # 复用启停那套接缝与超时处理
                ["kickstart", "gui/%d/%s" % (bot_launchd._uid(), TMUX_LOGS_LABEL)], seams=seams)
        except Exception as e:              # noqa: BLE001  后台线程里的异常不许冒出去
            _log.warning("tmux log rebuild kickstart failed: %s", type(e).__name__)
            return
        if why is not None or code != 0:
            _log.warning("tmux log rebuild kickstart failed: %s", why or ("exit %s" % code))

    try:
        threading.Thread(target=run, daemon=True).start()
    except Exception as e:                  # noqa: BLE001  触发不了也只是少一次重建，不影响启用
        _log.warning("tmux log rebuild kickstart failed: %s", type(e).__name__)


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
    running = deadline - time.monotonic() > bot_launchd.LAUNCHCTL_TIMEOUT and _polling(bot_id, seams)   # 13.1 同一判定
    _kickstart_tmux_logs(seams)             # 真正启动后补建看板；幂等启用不走这里
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


def _handoff_person(bot):
    """中立目录里认这只 bot 用的 person（5.2）；拼路径前先校验，不合规回 None（不导出也不导入）。"""
    person = _person(bot)
    return person if isinstance(person, str) and BOT_ID_RE.match(person) and person not in (".", "..") else None


def _switch_sync(bot):
    """切进时导入人设与技能（约定 5.10，5.9 第 3 步）→ ``synced``。

    读对方（claude）刚导出的那份，只换自己文件的圈内，标记外原样；导入前原文照 5.13、5.17 备份。自己没有的技能
    按通用名新建：自己模板的圈外加对方的圈内，这只 bot 的功能开关关着就写成停用（v1.1）。没有导出、导出不是刚停下
    的对方的、自己文件没有标记的都不算错，如实少报：``skills`` 只在导出里的技能全导进来时报。写盘出错不启动。
    """
    person = _handoff_person(bot)
    got = handoff.read_export(_seam_dir("DSH_CONSOLE_HANDOFF_DIR", DEFAULT_HANDOFF_DIR), person, "claude") \
        if person else None
    if got is None:
        return []
    try:
        synced = ["persona"] if got["persona"] is not None and _import_persona(bot, got["persona"]) else []
        done = [_import_skill(bot, generic, text) for generic, text in sorted(got["skills"].items())]
    except Refuse as e:
        raise Refuse(e.status, e.code, "导入人设和技能这一步出错：%s，没有启动" % e.message) from None
    if done and all(done):
        synced.append("skills")
    _log.info("switch-in synced: persona=%s skills=%d/%d", "persona" in synced, sum(done), len(done))
    return synced


def _import_persona(bot, segment):
    """把人设的圈内换成 ``segment``；人设文件不在、不是 UTF-8、没有一对完整标记，回 False 不动。"""
    path = _persona_path(bot)
    old, mode = _read_persona(path, "人设")
    try:
        new = handoff.replace(old.decode("utf-8"), segment) if mode is not None else None
    except UnicodeDecodeError:
        new = None
    if new is None:
        return False
    _save_text(bot["id"], path, new, _persona_version(old), "人设", PERSONA_STALE, ("persona", "", PERSONA_BACKUP_RE))
    return True


def _import_skill(bot, generic, segment):
    """把技能 ``generic`` 的圈内换成 ``segment``；没有这个技能按模板新建。落点不对、没有标记，回 False 不动。"""
    try:
        path = _skill_path(bot, generic)
    except Refuse:
        return False
    old, mode = _read_persona(path, "技能")
    if mode is None:
        new = handoff.replace(skill_templates.render(generic), segment)
        key = next(k for k, g in feature_switch.SKILL_OF.items() if g == generic)
        if new is not None and not feature_switch.read(bot["cfg"], bot["channel"])[0][key]:
            new = feature_switch.set_skill_disabled(new, True)
        if new is not None:
            os.makedirs(os.path.dirname(path), exist_ok=True)
    else:
        try:
            new = handoff.replace(old.decode("utf-8"), segment)
        except UnicodeDecodeError:
            new = None
    if new is None:
        return False
    _save_text(bot["id"], path, new, _persona_version(old), "技能", SKILL_STALE, _skill_backup(generic))
    return True


def _export_handoff(bot):
    """切换任务里停稳之后，把人设与技能的圈内导出到中立目录（约定 5.10；5.8 v1.0：只在切换时导出）。
    出错只记日志：停用已经成功，壳会把没带过去的项记成没同步。"""
    def full_text(locate):
        try:
            raw, mode = _read_persona(locate(), "")
            return raw.decode("utf-8") if mode is not None else None
        except (Refuse, UnicodeDecodeError):
            return None

    person = _handoff_person(bot)
    if person is None:
        _log.error("handoff export skipped: person is not usable in a path")
        return
    try:
        done = handoff.write_export(
            _seam_dir("DSH_CONSOLE_HANDOFF_DIR", DEFAULT_HANDOFF_DIR), person, bot["id"],
            full_text(lambda: _persona_path(bot)),
            {g: full_text(lambda g=g: _skill_path(bot, g)) for g in skill_templates.GENERICS})
    except (OSError, ValueError) as e:
        _log.error("handoff export failed: %s", type(e).__name__)
        return
    _log.info("handoff export: bot=%s items=%d", bot["id"], len(done))


def _set_enabled(bot_id):
    """启停（约定 5.8，补充文件 9.2、13.2）；带 ``import_from`` 时先切进导入（5.9，9.3）。整条请求 55 秒封顶。

    13.2：``op_id`` 只用于记录；``moved_to`` 只随停用有效（在停之前写或删迁出标记，停不下来的 503 也已改过标记，
    宁可拦住）；有迁出标记时启用一律 ``409 moved``（含切进），什么都不做。
    """
    deadline = time.monotonic() + ENABLE_BUDGET - 1.0     # 留一秒收尾
    body = _json_object()
    enabled, import_from = body.get("enabled"), body.get("import_from")
    if type(enabled) is not bool or set(body) - {"enabled", "import_from", "op_id", "moved_to"}:
        raise _invalid("请求体只能有 enabled（true 或 false），和可选的 import_from、op_id、moved_to")
    if "op_id" in body and not _valid_op_id(body["op_id"]):
        raise _invalid(OP_ID_BAD)
    if "moved_to" in body:
        if enabled:
            raise _invalid("启用时不能带 moved_to；要清迁出标记，先发一次带 moved_to: null 的停用")
        if body["moved_to"] is not None and body["moved_to"] not in MOVED_TARGETS:
            raise _invalid("moved_to 只能是 pending、claude 或 null")
    if "import_from" in body:
        if not enabled:
            raise _invalid("停用时不能带 import_from")
        if not (isinstance(import_from, str) and BOT_ID_RE.match(import_from)
                and import_from not in (".", "..")):
            raise _invalid("import_from 只能用字母、数字、点、横线、下划线，1 到 64 个字符")
    bot = _require_bot(bot_id)
    seams = bot_launchd.resolve_seams()
    if "op_id" in body:
        _log.info("enable request: bot=%s op=%s enabled=%s", bot_id, body["op_id"], enabled)
    if not enabled:
        if "moved_to" in body:
            _write_moved(bot_id, body["moved_to"], body.get("op_id"))
        stopped = _stop_bot(bot, seams, deadline)
        if body.get("moved_to") in MOVED_TARGETS:       # 切换任务里的停用（5.8 v1.0）：停稳之后导出圈内（5.10）
            _export_handoff(bot)
        return stopped
    if _moved_to(bot_id) is not None:
        raise Refuse(409, "moved", MOVED_REFUSED)
    if "import_from" not in body:
        if bot["enabled"] and _unit_running(bot_id, seams):
            return {"ok": True, "enabled": True, "running": True}     # 已是目标状态，不重复操作
        return _start_bot(bot, seams, deadline)
    imported = _switch_in(bot, import_from, seams, deadline - START_RESERVE)
    synced = _switch_sync(bot)
    return dict(_start_bot(bot, seams, deadline), imported=imported, synced=synced)


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


# ---------------------------------------------------------------- v1.1 功能开关（约定 5.19）

#: 必需项缺了时的说法（5.19.2：只判"配没配"，不回任何密钥）
REQUIREMENT_TEXT = {
    "voice": ("voice_id_missing", "缺音色编号。去管理台这只 bot 的语音设置里填 Fish 音色编号"),
    "image": ("image_token_missing", "缺 NovelAI 令牌。去管理台的画风页填 NovelAI 令牌"),
    "life": ("life_config_missing", "缺生活设定。去管理台这只 bot 的生活设定里配好作息"),
}
NOVELAI_TOKEN_KEYS = ("NOVELAI_JWT", "NOVELAI_BEARER_TOKEN", "NOVELAI_TOKEN")


def _novelai_skill_dir(cfg):
    """NovelAI 技能目录：yml 里 gateway.image_skill_dir（同网关），没写就是仓库里的 skills/novelai-skill。"""
    gw = cfg.get("gateway") if isinstance(cfg.get("gateway"), dict) else {}
    raw = gw.get("image_skill_dir")
    return os.path.expanduser(raw.strip()) if _text(raw) else os.path.join(_REPO_ROOT, "skills", "novelai-skill")


def _has_novelai_token(cfg):
    """配没配 NovelAI 令牌，只判有没有、不往外带：新系统凭据文件 refs 里的 NOVELAI_BEARER_TOKEN（网关的读法），
    或 novelai 技能目录 .env.local 里三种写法之一（生图脚本与画风页的读法）。"""
    try:
        with open(os.path.join(str(config_sources.dsh_home()), "credentials.yaml"), encoding="utf-8") as f:
            refs = (yaml.safe_load(f) or {}).get("refs")
        value = refs.get("NOVELAI_BEARER_TOKEN") if isinstance(refs, dict) else None
        if isinstance(value, str) and len(value.strip()) >= 8:
            return True
    except Exception:                       # noqa: BLE001  读不出就看下一处
        pass
    try:
        with open(os.path.join(_novelai_skill_dir(cfg), ".env.local"), encoding="utf-8") as f:
            for line in f:
                head, sep, value = line.strip().partition("=")
                if sep and not head.startswith("#") and head.strip() in NOVELAI_TOKEN_KEYS and value.strip().strip("'\""):
                    return True
    except Exception:                       # noqa: BLE001  没有这个文件就是没配
        pass
    return False


def _feature_requirements(bot):
    """5.19.2 的 requirements：每个键都有；dsh 侧 voice 要音色编号、image 要 NovelAI 令牌、life 要生活设定。"""
    cfg = bot["cfg"]
    try:
        access = _load_access(bot)[1]
    except Refuse:
        access = {}
    life = cfg.get("life_config")
    ok = {"voice": _text(access.get("voiceId")),
          "image": _has_novelai_token(cfg),
          "life": _text(life) and os.path.isfile(os.path.expanduser(life.strip()))}
    out = {}
    for key, _label in feature_switch.KEYS:
        missing = [] if ok.get(key, True) else [{"code": REQUIREMENT_TEXT[key][0], "message": REQUIREMENT_TEXT[key][1]}]
        out[key] = {"ready": not missing, "missing": missing, "notes": []}
    return out


def _features_get(bot_id):
    """读功能开关（约定 5.19.2）。停用的 bot 也能读。"""
    bot = _require_bot(bot_id)
    values, warnings = feature_switch.read(bot["cfg"], bot["channel"])
    return {"ok": True, "keys": [{"key": k, "label": label} for k, label in feature_switch.KEYS],
            "features": values, "requirements": _feature_requirements(bot), "warnings": warnings}


def _features_text(text, changes):
    """把 ``changes`` 写进 yml 的 features 块：块里有键就逐键改（行级编辑器，别的字节不动）；块不在就新加一个；
    写成空的（``features:``、``{}``、``null``）当没有这一块，把那个空值换成新写的一组开关。别的写法拒绝，不猜。"""
    from moments import hub_config         # 延迟 import：同 _write_enabled
    lit = {k: "true" if v else "false" for k, v in changes.items()}
    flow = "{%s}" % ", ".join('"%s": %s' % kv for kv in lit.items())
    root = hub_config.compose(text)
    block = hub_config.find(root, ["features"])
    if isinstance(block, yaml.MappingNode) and block.value:
        for key, value in changes.items():
            text = hub_config.set_scalar(text, hub_config.compose(text), "features." + key, value, "bool")
        return text
    if isinstance(block, yaml.MappingNode) or (isinstance(block, yaml.ScalarNode) and block.tag == "tag:yaml.org,2002:null"):
        at, end = block.start_mark.index, block.end_mark.index
        return text[:at] + (" " if at == end else "") + flow + text[end:]
    if block is not None:
        raise Refuse(500, "internal", "这只 bot 配置里的 features 写法不对（应是一组开关），请先修好配置再改")
    if isinstance(root, yaml.MappingNode) and root.flow_style and root.value:
        at = root.value[0][0].start_mark.index      # 整份是 JSON 的配置：插在第一个键前面
        return text[:at] + '"features": %s, ' % flow + text[at:]
    lines = "".join("  %s: %s\n" % kv for kv in lit.items())
    return text + ("\n" if text and not text.endswith("\n") else "") + "features:\n" + lines


def _skill_change(bot, generic, on, place, warnings):
    """技能跟着开关走（约定 v1.1 5.17）：算出这个技能要怎么改 → ``(路径, 原文, 新全文)``，新放的原文为 None；不用改回 None。

    关掉在技能头部写上 disable-model-invocation: true，打开去掉；文件保留、正文一字不动。``place`` 为真（这次
    刚打开）且技能文件不在，按仓库模板放上（成人内容取这只 bot 人设里的例句）；yml 写了 skills_manual: true 的
    bot 技能由人手管，不自动放，但停用与恢复照做。落点不对、是软链（同 5.17 读写）、读不出、没有头部的不碰，
    在 ``warnings`` 里说一句，不悄悄跳过。模板读不出整次失败。"""
    name = "skill-" + generic
    try:
        path = _skill_path(bot, generic)
    except Refuse:
        warnings.append("%s 不在这只 bot 自己的目录里或者是软链，没有跟着开关改" % name)
        return None
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        if not (on and place and bot["cfg"].get("skills_manual") is not True):
            return None
        persona = _persona_text(bot)
        try:
            text = skill_templates.render(generic, persona=persona)
        except (OSError, ValueError):
            raise Refuse(500, "internal", "%s 的模板读不出来，开关没有改" % name) from None
        if generic == "nsfw" and skill_templates.nsfw_unmatched(persona):
            warnings.append("人设里提到 NSFW，但没认出例句段，skill-nsfw 里只写了“按人设自然反应”")
        return path, None, text
    except (OSError, ValueError):
        warnings.append("%s 读不出来，没有跟着开关改" % name)
        return None
    new_text = feature_switch.set_skill_disabled(text, disabled=not on)
    if new_text is None:
        warnings.append("%s 开头没有技能头部，没有跟着开关改" % name)
        return None
    return None if new_text == text else (path, text, new_text)


def _write_skills(plans):
    """按 ``_skill_change`` 算好的内容逐个写技能；中途出错把已经写了的还原再往上抛。返回写了的 ``(路径, 原文)``。"""
    from moments import hub_config         # 延迟 import：同 _write_enabled
    done = []
    try:
        for path, old, new in plans:
            if old is None:
                _place_skill(path, new)
            else:
                hub_config.atomic_write(new, path)
            done.append((path, old))
    except BaseException:
        _undo_skills(done)
        raise
    return done


def _undo_skills(done):
    """把 ``_write_skills`` 写了的还原：改过的写回原文，新放的连同它的目录拿掉。还原失败只记日志。"""
    from moments import hub_config         # 延迟 import：同 _write_enabled
    for path, old in reversed(done):
        try:
            if old is None:
                os.unlink(path)
                os.rmdir(os.path.dirname(path))
            else:
                hub_config.atomic_write(old, path)
        except (OSError, HubError) as e:
            _log.error("skill rollback failed: %s", type(e).__name__)


def _persona_text(bot):
    """这只 bot 的人设全文；读不到当空（放 nsfw 技能时就只写"按人设自然反应"）。"""
    try:
        with open(_persona_path(bot), encoding="utf-8") as f:
            return f.read()
    except (Refuse, OSError, ValueError):
        return ""


def _place_skill(path, text):
    """按模板放一个技能：建好 skill-<通用名> 目录，原子写，权限 0600（nsfw 技能里有从人设抄来的成人例句，同 _save_text）。"""
    from moments import hub_config         # 延迟 import：同 _write_enabled
    os.makedirs(os.path.dirname(path), exist_ok=True)
    hub_config.atomic_write(text, path)
    os.chmod(path, 0o600)


def _running_now(bot):
    """这只 bot 现在在不在跑（改开关的回话用）。查不出回 None、只记日志：开关已经存好，不能因为这一步回 500。"""
    if not bot["enabled"]:
        return False
    try:
        return bot_launchd.row_state(bot["id"], bot_launchd.resolve_seams())[0] in ("online", "starting")
    except Exception as e:                  # noqa: BLE001
        _log.error("row_state failed: %s", type(e).__name__)
        return None


def _features_put(bot_id):
    """改功能开关（约定 5.19.3）：只改给了的键；打开缺必需项回 422 requirement_missing；锁被占 409 busy。
    改了的开关等这只 bot 空闲时换新会话生效（5.19.5）：网关盯着自己的 yml，见 features 变了就排一次空闲重载。

    技能跟着开关走（5.17）：请求里给的每个键都把技能对齐一次（不只变了的），上次没跟上的这次补齐。先在锁里把
    技能和 yml 的新内容都算好，算不出整次失败、一个字不写；再先写技能、最后写 yml，yml 写不进去就把技能还原。"""
    from moments import hub_config         # 延迟 import：同 _write_enabled
    body = _json_object()
    want = body.get("features")
    if set(body) != {"features"} or not isinstance(want, dict) or not want:
        raise _invalid('请求体只能是 {"features": {功能键: true 或 false, …}}，至少给一个键')
    fields = {k: "不认识的功能键" if k not in feature_switch.KEY_SET else "值只能是 true 或 false"
              for k, v in want.items() if k not in feature_switch.KEY_SET or type(v) is not bool}
    if fields:
        refuse = _invalid("功能开关不合规")
        refuse.fields = fields
        raise refuse
    _require_bot(bot_id)
    path = os.path.join(str(config_sources.dsh_dir()), bot_id + ".yml")
    try:
        with hub_addbot.hold_addbot_lock():
            bot = _require_bot(bot_id)       # 锁里现读，别拿锁外读到的旧开关判
            current = feature_switch.read(bot["cfg"], bot["channel"])[0]
            reqs = _feature_requirements(bot)
            missing = {k: "；".join(m["message"] for m in reqs[k]["missing"])
                       for k, v in want.items() if v and not current[k] and reqs[k]["missing"]}
            if missing:
                raise Refuse(422, "requirement_missing", "打不开：缺少必需的设置", fields=missing)
            changed = [k for k, _label in feature_switch.KEYS if k in want and want[k] != current[k]]
            new_text = None
            if changed:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                new_text = _features_text(text, {k: want[k] for k in changed})
                got = (yaml.safe_load(new_text) or {}).get("features") or {}
                if any(got.get(k) is not want[k] for k in changed):
                    raise Refuse(500, "internal", "开关写进配置之后读不回来，配置没有改")
            warnings = []
            plans = [p for p in (_skill_change(bot, feature_switch.SKILL_OF[k], want[k], k in changed, warnings)
                                 for k, _label in feature_switch.KEYS if k in want and k in feature_switch.SKILL_OF) if p]
            written = _write_skills(plans)
            if new_text is not None:
                try:
                    hub_config.atomic_write(new_text, path)
                except BaseException:
                    _undo_skills(written)
                    raise
    except HubError as e:
        raise _config_failure(e, "这只 bot 的配置") from None
    except yaml.YAMLError:
        raise Refuse(500, "internal", "这只 bot 的配置读不出来，没有改") from None
    except OSError as e:
        _log.error("features put failed: %s", type(e).__name__)
        raise Refuse(500, "internal", "写技能文件或配置失败，开关没有改") from None
    after = dict(current, **{k: want[k] for k in changed})
    wrote = bool(changed or written)
    running = _running_now(bot) if wrote else False
    if not wrote:
        message = "开关本来就是这样，没有改动。"
    elif running:
        message = "已保存。这只 bot 空闲时换新会话后生效，不打断正在回的话。"
    elif running is None:
        message = "已保存。这只 bot 空闲时或下次启动时生效。"
    else:
        message = "已保存。这只 bot 现在没在跑，下次启动时生效。"
    return {"ok": True, "message": message, "features": after, "changed": changed,
            "applied": not wrote, "queued": wrote and running is True, "warnings": warnings}


# ---------------------------------------------------------------- v1.1 刚发现的群（约定 5.20）

#: 网关记"刚发现的群"的文件、管理台退群时间的文件，都在这只 bot 的 state/ 下（两个文件各只有一个写者）
SEEN_FILE = "groups-seen.json"
LEFT_FILE = "groups-console-left.json"
SEEN_STATUSES = ("member", "administrator", "restricted")       # left、kicked：已经不在群里，不列
DISCOVERED_MAX = 50
UTC_SECOND_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")


def _state_file(bot, name):
    """这只 bot 的 ``state/<name>``；频道目录不在它自己的家里、或者文件是软链，回 None（同 ``_channel_file``）。"""
    channel = _channel_file(bot, ".")
    if channel is None:
        return None
    path = os.path.join(os.path.dirname(os.path.normpath(channel)), "state", name)
    return None if os.path.islink(path) else path


def _read_state_json(bot, name):
    """读 ``state/<name>`` 里的 JSON 对象；没有、读不出、不是对象，都当空的（这两份只是记录，坏了不拦）。"""
    path = _state_file(bot, name)
    try:
        with open(path, "rb") as f:
            obj = json.loads(f.read().decode("utf-8"))
    except (TypeError, OSError, ValueError, RecursionError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _utc_second(value):
    return value if isinstance(value, str) and UTC_SECOND_RE.match(value) else None


def _note_left(bot, chat_id, left):
    """管理台退群（5.12.2 的 join: false）记下时间；再加入就抹掉。写在网关不碰的另一份文件里，原子写、0600。
    这只是一份记录：写不进去只记日志，退群已经生效，不能因为它回 500。"""
    path = _state_file(bot, LEFT_FILE)
    if path is None:
        return
    marks = _read_state_json(bot, LEFT_FILE)
    if left:
        marks[chat_id] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    elif marks.pop(chat_id, None) is None:
        return
    from moments import hub_config         # 延迟 import：同 _groups_post
    try:
        hub_config.atomic_write(json.dumps(marks, ensure_ascii=False, indent=1) + "\n", path)
    except (OSError, HubError) as e:
        _log.error("note left failed: %s", type(e).__name__)


def _groups_discovered(bot_id):
    """刚发现的群（约定 5.20.2）：网关记下的群里，去掉已经在 access.json 里的、bot 已经不在群里的。

    停用的 bot 也能列，只读记录文件、不去问网关；启用的网关在跑时 can_read_all、can_join_groups 从它的 health
    取，停用或网关不在跑为 null。
    在管理台退过的群带上退群时间。按最后看到从新到旧，最多 50 条。
    """
    bot = _require_bot(bot_id)
    joined = _read_access(bot)[2]
    seen = _read_state_json(bot, SEEN_FILE)
    left = _read_state_json(bot, LEFT_FILE)
    tg = _gateway_health(bot["channel"]) if bot["enabled"] else {}
    can_read = tg.get("can_read_all") if type(tg.get("can_read_all")) is bool else None
    can_join = tg.get("can_join_groups") if type(tg.get("can_join_groups")) is bool else None
    rows = []
    for chat_id, g in seen.items():
        if not _group_id(chat_id) or chat_id in joined or not isinstance(g, dict) or g.get("status") not in SEEN_STATUSES:
            continue
        status = g["status"]
        added = g.get("added_by_owner")
        rows.append({
            "chat_id": chat_id,
            "title": g.get("title") if isinstance(g.get("title"), str) else "",
            "type": "supergroup" if g.get("type") == "supergroup" else "group",
            "status": status,
            "added_by_owner": added if type(added) is bool else None,
            "sees_all": can_read is True or status == "administrator",
            "first_seen": _utc_second(g.get("first_seen")),
            "last_seen": _utc_second(g.get("last_seen")),
            "migrated_from": g.get("migrated_from") if _group_id(g.get("migrated_from")) else None,
            "left_in_console_at": _utc_second(left.get(chat_id)),
        })
    rows.sort(key=lambda r: r["last_seen"] or "", reverse=True)
    return {"ok": True, "can_read_all": can_read, "can_join_groups": can_join, "groups": rows[:DISCOVERED_MAX]}


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
                # 刚发现的群（约定 5.20.2）：退群记下时间，这个群会带着它重新出现在列表里；再加入就抹掉
                _note_left(bot, chat_id, left=not join)
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


def _read_persona(path, what="人设"):
    """人设或技能文件 → ``(原始字节, 权限)``，不存在是 ``(b"", None)``；读不出 500。

    ``O_NOFOLLOW``：查过之后才被换成软链的，也不跟过去读。
    """
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            return f.read(), stat.S_IMODE(os.fstat(f.fileno()).st_mode)
    except FileNotFoundError:
        return b"", None
    except OSError:
        raise Refuse(500, "internal", "这只 bot 的%s文件读不出来" % what) from None


def _text_view(raw, what):
    """读人设、读技能的回应：原文全文与版本号；不是 UTF-8 回 500。"""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise Refuse(500, "internal", "这只 bot 的%s文件不是合法的 UTF-8 文本" % what) from None
    return {"ok": True, "text": text, "version": _persona_version(raw)}


def _persona_get(bot_id):
    """读人设（v0.8 草案 A.1，补充文件 10.2）：原文全文与版本号。停用的 bot 照常能读；不是 UTF-8 回 500。"""
    return _text_view(_read_persona(_persona_path(_require_bot(bot_id)))[0], "人设")


def _text_body():
    """改人设、改技能的请求体（A.2；技能同它，11.2）→ ``(text, version)``，不合规 422。

    只许 ``text``、``version`` 两个键；``text`` 去掉首尾空白后不能为空、按 UTF-8 不超过 256 KiB；
    ``version`` 是 16 位小写十六进制。
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
    return text, version


def _save_text(bot_id, path, text, version, what, stale, backup, missing=None):
    """人设（10.2）与技能（11.2）共用的存盘，全程在管理台编辑页写人设那把锁里（同进程内有效，9.7）。

    文件不在且给了 ``missing``：抛它，不新建 → 比版本号（409，``stale`` 是那句提示）→ 全文没变就不写 →
    原文备份（``backup`` = 备份子目录、文件名后缀、轮换只认的名字）→ 原子写、保持原权限（原来没有就 0600）→
    写盘成功之后备份只留 2 份。写盘的就是 ``text`` 的 UTF-8 原样，去首尾空白只用来判断是不是空。
    """
    kind, suffix, name_re = backup
    data = text.encode("utf-8")
    try:
        with hub_addbot.hold_addbot_lock():
            old, mode = _read_persona(path, what)
            if mode is None and missing is not None:
                raise missing
            if _persona_version(old) != version:
                raise Refuse(409, "stale", stale)
            if data == old:
                return {"ok": True, "version": version, "message": PERSONA_SAME}
            try:
                folder = None if mode is None else life_settings._backup(bot_id, old, kind=kind, suffix=suffix)
                life_settings._replace(path, text, 0o600 if mode is None else mode)
            except OSError:
                raise Refuse(500, "internal", "写这只 bot 的%s失败" % what) from None
            if folder:
                life_settings._rotate(folder, name_re)      # 写盘成功之后才清理
    except HubError as e:
        raise _config_failure(e, "这只 bot 的" + what) from None
    return {"ok": True, "version": _persona_version(data), "message": PERSONA_SAVED}


def _persona_put(bot_id):
    """改人设（v0.8 草案 A.2，补充文件 10.2、10.6）。停用的 bot 也能改；不重启（网关自己盯着这个文件）。

    先后：请求体（422）→ 名单（404）→ 落点（500）→ 锁里比版本号（409）→ 全文没变就不写 → 原文备份到
    ``<DSH_BOT_HOME>/backups/persona/<bot_id>/`` → 原子写、保持原权限（原来没有就 0600）→ 备份只留 2 份。
    """
    text, version = _text_body()
    path = _persona_path(_require_bot(bot_id))
    return _save_text(bot_id, path, text, version, "人设", PERSONA_STALE, ("persona", "", PERSONA_BACKUP_RE))


def _no_such_skill():
    return Refuse(404, "no_such_skill", "这只 bot 没有这个技能")


def _skill_name(name):
    """11.2：只认通用名表里的名字，原样比对（大小写、前缀、扩展名都不认）；不在表里 422，不看文件。"""
    if name not in SKILL_NAMES:
        raise _invalid("技能名只能是 %s 之一" % "、".join(SKILL_NAMES))


def _skill_path(bot, name):
    """11.2：``<频道目录>/skills/skill-<通用名>/SKILL.md``。落点不对、路上任何一层是软链：500，不读不写。

    ``_channel_file`` 管频道目录的落点与 SKILL.md 本身；中间 ``skills``、``skill-<通用名>`` 两层是软链也会把
    读写领到频道目录外（另一套系统的技能目录在归属边界外），所以再比一次解析后的真实路径：一层软链都没有
    才相等。还不存在的层 ``realpath`` 原样保留，交给读文件那一步回 404。
    """
    path = _channel_file(bot, os.path.join("skills", "skill-" + name, "SKILL.md"))
    if path is None or os.path.realpath(path) != path:
        raise Refuse(500, "internal", "这只 bot 的技能文件不在它自己的目录里，或者是软链，不读也不改")
    return path


def _skill_get(bot_id, name):
    """读技能（v0.9 dsh 侧草案 A.1，补充文件 11.2）：原文全文与版本号。文件不在 404，停用的 bot 照常能读。"""
    _skill_name(name)
    raw, mode = _read_persona(_skill_path(_require_bot(bot_id), name), "技能")
    if mode is None:
        raise _no_such_skill()
    return _text_view(raw, "技能")


def _skill_backup(name):
    """技能备份的 ``(子目录, 文件名后缀, 轮换只认的名字)``：时间加通用名，每个技能各留最近 2 份（11.2）。"""
    suffix = ".%s.md" % name
    return "skills", suffix, re.compile(r"^[0-9]{8}-[0-9]{6}(-[0-9]{2})?" + re.escape(suffix) + r"\Z")


def _skill_put(bot_id, name):
    """改技能（草案 A.2，补充文件 11.2）：除了「文件不在 404、不新建」，全部同改人设（``_save_text``）。

    先后：技能名（422）→ 请求体（422）→ 名单（404）→ 落点与软链（500）→ 锁里看文件在不在（404）→ 版本号（409）。
    备份放 ``<DSH_BOT_HOME>/backups/skills/<bot_id>/``，名字是时间加通用名（如 ``20261015-084153.image.md``），
    轮换只数同一个通用名的，所以每个技能各留最近 2 份。
    """
    _skill_name(name)
    text, version = _text_body()
    path = _skill_path(_require_bot(bot_id), name)
    return _save_text(bot_id, path, text, version, "技能", SKILL_STALE, _skill_backup(name),
                      missing=_no_such_skill())


def _voice_view(obj):
    """B.1、B.2 与 11.3 的回应：四个键照 access.json 原样，不填缺省；没写的（或不是非空字符串）为 null，别的键一律不回。"""
    view = {"ok": True}
    for api_key, file_key in VOICE_KEYS:
        value = obj.get(file_key)
        view[api_key] = value if _text(value) else None
    return view


def _voice_get(bot_id):
    """读语音（v0.8 草案 B.1，补充文件 10.3、11.3）。停用的 bot 照常能读；access.json 读不出 500。"""
    return _voice_view(_load_access(_require_bot(bot_id))[1])


def _language(value, key):
    """11.3：语言名 → 要存的值（去掉首尾空白）；null 原样（删键）。不合规 422。

    去掉首尾空白后 1 到 20 个字符（按字符数，不按字节）；整个值里不许有换行、回车与 ``[ ] < > { } ` |``：
    语言名原样写进提示词，这些字符会弄乱提示词里的标签与格式。
    """
    if value is None:
        return None
    if (not isinstance(value, str) or LANGUAGE_BAD_RE.search(value)
            or not 1 <= len(value.strip()) <= LANGUAGE_MAX):
        raise _invalid("%s 要是 1 到 %d 个字符的语言名，不能有换行与 [ ] < > { } ` |；或者 null" % (key, LANGUAGE_MAX))
    return value.strip()


def _voice_put(bot_id):
    """改语音（v0.8 草案 B.2，补充文件 10.3、10.6、11.3）：给了的键整键替换、null 删键、没给的不动。

    只改这四个键，access.json 别的键原样，原子写、保持原权限，与群设置同一把锁；读不出 500，不新建、
    不覆盖。不重启：语音两键网关下次发语音时现读，语言两键网关自己盯着（11.4）。停用的 bot 也能改。
    """
    from moments import hub_config         # 延迟 import：同 _write_enabled
    body = _json_object()
    if not body or set(body) - {api_key for api_key, _ in VOICE_KEYS}:
        raise _invalid("请求体只能有 voice_id、tts_model、text_language、voice_language，至少给一个")
    voice_id, model = body.get("voice_id"), body.get("tts_model")
    if voice_id is not None and not (isinstance(voice_id, str) and VOICE_ID_RE.match(voice_id)):
        raise _invalid("voice_id 只能用字母、数字、下划线、横线，1 到 64 个字符；或者 null")
    if model is not None and model not in TTS_MODELS:
        raise _invalid("tts_model 只能是 %s 之一，或者 null" % "、".join(TTS_MODELS))
    values = dict(body)
    for key in ("text_language", "voice_language"):
        if key in body:
            values[key] = _language(body[key], key)
    bot = _require_bot(bot_id)
    try:
        with hub_addbot.hold_addbot_lock():
            path, obj = _load_access(bot)
            new = dict(obj)
            for api_key, file_key in VOICE_KEYS:
                if api_key in values and values[api_key] is None:
                    new.pop(file_key, None)
                elif api_key in values:
                    new[file_key] = values[api_key]
            if new != obj:                  # 本来就是这样就不写
                hub_config.atomic_write(json.dumps(new, ensure_ascii=False, indent=2) + "\n", path)
    except HubError as e:
        raise _config_failure(e, "这只 bot 的 access.json") from None
    return _voice_view(new)


# ---------------------------------------------------------------- v1.0 新建、归档、删除、显示名（补充文件第 12 节）

def _utc_stamp():
    """12.3、12.4：此刻的 UTC 时间，形如 ``20261015T080102Z``。"""
    return datetime.now(timezone.utc).strftime(UTC_STAMP_FORMAT)


def _utc_time(stamp):
    """``20261015T080102Z`` → ``2026-10-15T08:01:02Z``；不是真实存在的时刻回 None。"""
    try:
        return datetime.strptime(stamp, UTC_STAMP_FORMAT).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def _hub_invalid(err):
    """管理台校验函数抛的 400、413（字段形状不对、人设太长）→ 422；话照原样（那边的话从不带密钥原文）。"""
    return _invalid(err.detail or "请求体里有一项不合规")


def _create_persona(body, bots):
    """草案 A 的人设：``persona_text`` 或 ``persona_template`` 二选一，都不给是空白模板（空人设）。

    正文的检查同新建页（``hub_addbot._persona_text``）。模板只认本系统名单里的 bot（读它频道目录下的
    ``CLAUDE.md``，落点与软链同 10.2）和管理台的空白模板名；新建页还会去另一套系统的频道目录里找模板，
    那里在归属边界外，这里不读。
    """
    tmpl, text = body.get("persona_template"), body.get("persona_text")
    for key, value in (("persona_template", tmpl), ("persona_text", text)):
        if value is not None and not isinstance(value, str):
            raise _invalid("%s 要是字符串" % key)
    if _text(tmpl) and _text(text):
        raise _invalid("persona_text 与 persona_template 只能给一个")
    try:
        if _text(text):
            return hub_addbot._persona_text(text)
        if not _text(tmpl) or tmpl.strip() == hub_addbot.BLANK_TEMPLATE:
            return ""
        source = next((bot for bot in bots if bot["id"] == tmpl.strip()), None)
        path = _channel_file(source, "CLAUDE.md") if source else None
        if path is None:
            raise _invalid("没有这个人设模板")
        try:
            persona = _read_persona(path, "人设模板")[0].decode("utf-8")
        except UnicodeDecodeError:
            raise _invalid("这个人设模板不是合法的 UTF-8 文本") from None
        return hub_addbot._persona_text(persona, "这个人设模板超过 65536 个字符，换一个或直接给正文")
    except HubError as e:
        raise _hub_invalid(e) from None


def _create_fields(body, bots):
    """草案 A 的形状校验（422），字段与校验同管理台新建页；返回交给 ``hub_addbot.lay_down`` 的那份。

    编号除了约定 4.0 的字符规则，还要是本系统合格的 bot 编号（小写字母开头、2 到 32 位、不是保留名）：
    不合格的配置本服务列不出来，建了也看不见。显示名、Fish 两项不给或给空白都算没给，显示名缺省用编号。
    """
    if not set(body) <= set(CREATE_KEYS):
        raise _invalid("请求体只能有 %s" % "、".join(CREATE_KEYS))
    bot_id, token, chat_id, port = (body.get(k) for k in ("bot_id", "telegram_token", "chat_id", "port"))
    if not (isinstance(bot_id, str) and CREATE_ID_RE.match(bot_id) and config_sources.is_qualified_bot_id(bot_id)):
        raise _invalid("bot_id 只能是小写字母开头的 2 到 32 位小写字母、数字、下划线，也不能是保留名")
    if not isinstance(token, str) or not hub_addbot.TOKEN_RE.match(token):
        raise _invalid("telegram_token 的形状不对")      # 不带原值
    if not hub_addbot._is_int(chat_id):
        raise _invalid("chat_id 要是整数（主人的私聊编号）")
    if port is not None and not (hub_addbot._is_int(port) and 1024 <= port <= 65535):
        raise _invalid("port 要是 1024 到 65535 的整数")
    # 13.3 的几个可选字段
    enabled, person = body.get("enabled", True), body.get("person")
    username, user_id = body.get("telegram_username"), body.get("telegram_user_id")
    if type(enabled) is not bool:
        raise _invalid("enabled 只能是 true 或 false")
    if person is not None and not (isinstance(person, str) and CREATE_ID_RE.match(person)):
        raise _invalid("person 的规则同 bot_id：小写字母开头的 2 到 32 位小写字母、数字、下划线")
    if username is not None:
        username = username.strip().lstrip("@") if isinstance(username, str) else ""
        if not BOT_USERNAME_RE.match(username):
            raise _invalid("telegram_username 的形状不对：5 到 32 位字母、数字、下划线，开头可带 @")
    if user_id is not None and not (type(user_id) is int and user_id > 0):
        raise _invalid("telegram_user_id 要是正整数")
    if body.get("purpose", "new") not in CREATE_PURPOSES:     # dsh 侧 new 与 switch_in 流程相同
        raise _invalid("purpose 只能是 new 或 switch_in")
    # 约定 v1.1 5.18.1：功能键数组（省略等于九个全关）与外貌
    wanted = body.get("features", [])
    if not isinstance(wanted, list) or len(set(map(repr, wanted))) != len(wanted) \
            or not all(isinstance(k, str) and k in feature_switch.KEY_SET for k in wanted):
        raise _invalid("features 要是功能键的数组，不重复，只能用 voice、image、stickers、moments、proactive、"
                       "life、relationship、groups、nsfw")
    appearance = body.get("appearance", "")
    if appearance is None:
        appearance = ""
    if not isinstance(appearance, str):
        raise _invalid("appearance 要是字符串")
    appearance = appearance.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(appearance) > APPEARANCE_MAX or any(ch != "\n" and unicodedata.category(ch) == "Cc" for ch in appearance) \
            or any(bad in appearance for bad in APPEARANCE_BANNED):
        raise _invalid("appearance 去掉首尾空白后最多 %d 个字，可以换行，不能有别的控制字符，也不能含 <!--、-->、{{、}}"
                       % APPEARANCE_MAX)
    if appearance and "image" not in wanted:
        raise _invalid("appearance 只在勾了生图（features 里有 image）时才能填")
    extra = {"enabled": enabled, "person": person, "telegram_username": username, "telegram_user_id": user_id,
             "features": [k for k, _label in feature_switch.KEYS if k in wanted], "appearance": appearance}
    name = hub_addbot._optional(body, "display_name")
    try:
        name = bot_id if name is None else hub_addbot._display_name(name)
        voice = hub_addbot._fish_voice(hub_addbot._optional(body, "fish_voice_id"))
        fish_key = hub_addbot._fish_key(hub_addbot._optional(body, "fish_api_key"))
    except HubError as e:
        raise _hub_invalid(e) from None
    if len(name) > PROFILE_NAME_MAX:        # 13.3：新建的显示名去首尾空白后 1 到 32 个字
        raise _invalid("display_name 去掉首尾空白后要是 1 到 %d 个字" % PROFILE_NAME_MAX)
    return dict({"bot_id": bot_id, "token": token, "chat_id": chat_id, "port": port, "display_name": name,
                 "persona": _create_persona(body, bots), "fish_voice_id": voice, "fish_api_key": fish_key},
                **extra)


def _bot_slots(bot_id, seams, life=None):
    """12.3 要移的那几处 → ``[(原位置, 在归档里的相对位置)]``，不管在不在：bot 目录、配置、两个启动项的
    两种写法（装着、收起）、给了的话再加生活设定文件。归档目录里照原来的上级目录名摆。"""
    slots = [(str(bot_launchd.bot_home(bot_id, seams)), os.path.join("bots", bot_id)),
             (os.path.abspath(os.path.join(str(config_sources.dsh_dir()), bot_id + ".yml")),
              os.path.join("configs", bot_id + ".yml"))]
    for timer in (False, True):
        for path in bot_launchd.plist_paths(bot_id, timer, seams):
            slots.append((path, os.path.join("LaunchAgents", os.path.basename(path))))
    if life:
        slots.append((life, os.path.join("life", os.path.basename(life))))
    return slots


def _bot_items(bot_id, seams, life=None):
    """``_bot_slots`` 里此刻真在的那些（软链按它自己算，不跟过去）。"""
    return [(path, rel) for path, rel in _bot_slots(bot_id, seams, life) if os.path.lexists(path)]


def _own_life(bot, bots):
    """只被这只用的生活设定文件（在允许目录里、别的 bot 没指着它、不是软链、没有硬链接）；不是就 None。

    判定就是生活设定接口的 ``life_settings.locate``（同一个允许目录、同一个「共用」口径）。
    """
    try:
        return life_settings.locate(bot, bots)[0]
    except (life_settings.LifeError, OSError):
        return None


def _prune(folder):
    """归档目录用完（恢复了、或移走半路撤回了）：只删 manifest.json 与空目录，里面还有东西就留着。"""
    with contextlib.suppress(OSError):
        os.unlink(os.path.join(folder, MANIFEST))
    for dirpath, _dirs, _files in os.walk(folder, topdown=False):
        with contextlib.suppress(OSError):
            os.rmdir(dirpath)


def _stash(bot_id, items, folder, extra=None):
    """把 ``items`` 整份移进新建的 ``folder``（0700），先写 manifest.json 记下每样原来的位置（12.3、12.4）。

    只改名、不复制不删（同一块盘上）。中途出错：已移的按原路移回去再抛；全都移回去了才把 manifest 与空目录
    清掉，有没移回去的就留着 manifest，照着它能手工找回。
    """
    os.makedirs(os.path.dirname(folder), mode=0o700, exist_ok=True)
    os.mkdir(folder, 0o700)
    manifest = {"bot_id": bot_id, "at": _utc_time(_utc_stamp()),
                "items": [{"from": path, "to": rel} for path, rel in items]}
    manifest.update(extra or {})            # 13.4：归档另记显示名、person、shell_meta
    moved = []
    try:
        hub_addbot._create_file(os.path.join(folder, MANIFEST),
                                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        for path, rel in items:
            target = os.path.join(folder, rel)
            os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
            shutil.move(path, target)
            moved.append((path, target))
    except BaseException:
        back = True
        for path, target in reversed(moved):
            try:
                shutil.move(target, path)
            except OSError:
                back = False
        if back:
            _prune(folder)
        raise


def _discard(bot_id, items):
    """删除（12.4）与新建失败的撤回：移进废纸篓 ``<废纸篓>/dsh-bot-<编号>-<UTC 时间>/``（带 manifest.json）；
    废纸篓目录不存在（非 macOS）时直接删。废纸篓默认 ``~/.Trash``，测试接缝 ``DSH_CONSOLE_TRASH_DIR``。"""
    trash = _seam_dir("DSH_CONSOLE_TRASH_DIR", DEFAULT_TRASH_DIR)
    if os.path.isdir(trash):
        _stash(bot_id, items, os.path.join(trash, "dsh-bot-%s-%s" % (bot_id, _utc_stamp())))
        return
    for path, _rel in items:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)


def _move_out(bot, bots, seams, move, what):
    """已经停下的 bot：持管理台写配置那把锁，把 12.3 列的那些整份交给 ``move`` 移走（归档、删除共用）。"""
    try:
        with hub_addbot.hold_addbot_lock():
            move(_bot_items(bot["id"], seams, _own_life(bot, bots)))
    except HubError as e:
        raise _config_failure(e, "这只 bot 的文件") from None
    except OSError as e:
        _log.error("%s failed: %s", what, type(e).__name__)
        raise Refuse(500, "internal", "移走这只 bot 的文件时出错，已移走的尽量放回了原处") from None


def _port_in_use(port, bot_id, bots):
    return any(b["cfg"].get("dispatcher_port") == port for b in bots if b["id"] != bot_id)


def _create_port(port, bot_id, bots):
    """12.2 的 ``port``：给了就查本系统别的 bot 没占、本机没人在听；不给就在 17850–17899 里挑第一个没被本系统
    任何配置、任何归档占用且本机没人在听的（13.3）。"""
    if port is not None:
        if _port_in_use(port, bot_id, bots) or hub_addbot._listening(port):
            raise _invalid("端口 %d 已被占用" % port)
        return port
    taken = {b["cfg"].get("dispatcher_port") for b in bots if b["id"] != bot_id} | _archived_ports()
    for port in CREATE_PORTS:
        if port not in taken and not hub_addbot._listening(port):
            return port
    raise Refuse(503, "unavailable", "17850 到 17899 里没有空着的端口，没有新建")


def _recheck_port(port, bot_id):
    """落盘之后再查一次端口（新建页 R4 的同款复查，只看本系统）。抛出来 ``lay_down`` 会先回滚再往外抛。"""
    if _port_in_use(port, bot_id, _own_bots()):
        raise _invalid("端口 %d 刚被别的 bot 占了，没有新建" % port)


def _token_owner(telegram_id, bots):
    """新建页 R1 的同款校验，只查本系统：别的 bot 频道目录 ``.env`` 里令牌的数字段等于这个 Telegram bot 的 id。
    读到的整串只在这里过一遍正则，出去时只剩 bot 编号。"""
    if not hub_addbot._is_int(telegram_id):
        return None
    for bot in bots:
        path = _channel_file(bot, ".env")
        if path is None:
            continue
        try:
            with os.fdopen(os.open(path, os.O_RDONLY | _NOFOLLOW), "rb") as f:
                hit = hub_addbot.ENV_TOKEN_RE.search(f.read(MAX_BODY).decode("utf-8", "ignore"))
        except OSError:
            continue
        if hit and int(hit.group(1)) == telegram_id:
            return bot["id"]
    return None


def _create_yml(fields, bots):
    """新建页那份配置全文（``hub_addbot._render_yml``），读回来核对过（同 ``_checked_yml``）。

    ``namespace_uuid`` 只与本系统已有的去重（新建页还扫另一套系统的配置，归属边界外；uuid4 撞上的概率是 0）。
    """
    used = {bot["cfg"].get("namespace_uuid") for bot in bots}
    namespace = str(uuid.uuid4())
    while namespace in used:
        namespace = str(uuid.uuid4())
    target = os.path.abspath(os.path.join(str(config_sources.dsh_bot_dir(fields["bot_id"])), "channel"))
    life = hub_addbot.life_target(fields["bot_id"])     # 有就写 life_config，lay_down 照它生成生活设定文件
    text = hub_addbot._render_yml(fields, target, namespace, life and life[1]) + _create_extra(fields)
    expect = {"id": fields["bot_id"], "display_name": fields["display_name"],
              "bot_channel_path": target, "namespace_uuid": namespace}
    if life:
        expect["life_config"] = life[1]
    try:
        hub_addbot._readback(text, expect)
    except HubError as e:
        raise _hub_invalid(e) from None
    return text


def _undo_create(bot_id, seams, life=None):
    """新建在装启动项这一步没成：两个单元都 bootout，再把这次写的东西（配置、bot 目录、启动项文件，
    ``life`` 是 ``lay_down`` 这次生成的生活设定文件）整份移走，同删除（有废纸篓移进废纸篓，没有直接删）。
    撤的过程里再出错只记类名，不盖掉原来那个错。"""
    for timer in (False, True):
        with contextlib.suppress(Exception):
            bot_launchd.bootout(bot_id, seams, timer=timer)
    try:
        _discard(bot_id, _bot_items(bot_id, seams, life))
    except Exception as e:                  # noqa: BLE001
        _log.error("create rollback failed: %s", type(e).__name__)


def _create_features(fields, life):
    """新建时的九个开关（约定 v1.1 5.18.1、5.19.4）：勾了且必需项齐的开，其余关；勾了但缺必需项的
    不打开，列进 features_skipped。新 bot 的配置里还没有 image_skill_dir，按缺省判。

    ``life`` 是 ``hub_addbot.life_target(bot_id)`` 的结果（``_create_locked`` 已算好传进来）：不为 None
    时这次新建会写 life_config 并生成生活设定文件（与 ``_create_yml`` 同一判断；文件已占用的此前已 409），
    life 就算齐；为 None（允许目录不合格）不写 life_config，life 不打开、列进跳过。"""
    ready = {"voice": bool(fields.get("fish_voice_id")), "image": _has_novelai_token({}),
             "life": life is not None}
    final, skipped = {}, []
    for key, _label in feature_switch.KEYS:
        ticked = key in fields["features"]
        final[key] = ticked and ready.get(key, True)
        if ticked and not ready.get(key, True):
            code, message = REQUIREMENT_TEXT[key]
            skipped.append({"key": key, "missing": [{"code": code, "message": message}]})
    return final, skipped


def _create_skills(fields):
    """按勾选放技能（约定 v1.1 5.18.1）：勾了的功能都放（缺必需项没打开的照放、写成停用，补齐后在 5.19
    打开就能用）；外貌写进 image 的圈内，nsfw 的圈内取这份人设里的例句。返回放了的通用名。"""
    bot_id = fields["bot_id"]
    bot = {"id": bot_id, "cfg": {},
           "channel": os.path.abspath(os.path.join(str(config_sources.dsh_bot_dir(bot_id)), "channel"))}
    placed = []
    for key in fields["features"]:
        generic = feature_switch.SKILL_OF.get(key)
        if generic is None:
            continue
        text = skill_templates.render(generic, appearance=fields["appearance"], persona=fields["persona"])
        if generic == "nsfw" and skill_templates.nsfw_unmatched(fields["persona"]):
            _log.warning("create: persona mentions NSFW but no example section matched")
        if not fields["features_final"][key]:
            text = feature_switch.set_skill_disabled(text, True)
        _place_skill(_skill_path(bot, generic), text)
        placed.append(generic)
    return placed


def _create_locked(fields, bots, seams, deadline):
    """``_create_bot`` 持锁的那一段：409 → 端口 → 配置全文 → getMe → 令牌占用 → 落盘 → 装启动项。"""
    bot_id = fields["bot_id"]
    if any(os.path.lexists(path) for path, _rel in _bot_slots(bot_id, seams)):
        raise Refuse(409, "exists", "已经有一只叫 %s 的 bot 了（配置、bot 目录或启动项还在）" % bot_id)
    life = hub_addbot.life_target(bot_id)   # bot_id 已过 BOT_ID_RE（_create_fields）才拼路径
    if life is not None and os.path.lexists(life[1]):      # 软链也算占用，什么都不写、原文件不动
        raise Refuse(409, "exists", hub_addbot.LIFE_TAKEN % bot_id)
    fields["port"] = _create_port(fields["port"], bot_id, bots)
    fields["features_final"], fields["features_skipped"] = _create_features(fields, life)
    fields["yml_text"] = _create_yml(fields, bots)
    probe = hub_addbot.getme(fields["token"])
    if not probe.get("ok"):
        if probe.get("stage") in ("malformed", "invalid_token", "rejected"):
            raise Refuse(422, "token_rejected", "telegram_token 不对：Telegram 不认这个令牌")
        raise Refuse(503, "telegram_unreachable", "现在连不上 Telegram，没有新建，可以稍后重试")
    # 13.3：给了用户名或 user_id 就与 getMe 问到的核对（用户名不分大小写、不管开头的 @）
    got_name = probe.get("username") if isinstance(probe.get("username"), str) else ""
    if fields["telegram_username"] is not None \
            and fields["telegram_username"].lower() != got_name.lstrip("@").lower():
        raise Refuse(422, "token_rejected", "telegram_username 与这个令牌对应的 bot 对不上")
    if fields["telegram_user_id"] is not None and probe.get("bot_id") != fields["telegram_user_id"]:
        raise Refuse(422, "token_rejected", "telegram_user_id 与这个令牌对应的 bot 对不上")
    owner = _token_owner(probe.get("bot_id"), bots)
    if owner is not None:
        raise _invalid("这个 Telegram 令牌已经被 %s 用着了" % owner)
    try:
        hub_addbot.lay_down(fields, port_check=lambda: _recheck_port(fields["port"], bot_id))
    except Refuse:
        raise
    except Exception as e:                  # noqa: BLE001  lay_down 已自己回滚；异常消息可能带路径，只记类名
        _log.error("create lay_down failed: %s", type(e).__name__)
        raise Refuse(500, "internal", "写新 bot 的文件失败，已撤回") from None
    try:
        fields["skills_placed"] = _create_skills(fields)
    except Exception as e:                  # noqa: BLE001  技能没放成整只撤回；异常消息可能带路径，只记类名
        _log.error("create skills failed: %s", type(e).__name__)
        _undo_create(bot_id, seams, fields.get("life_file"))
        raise Refuse(500, "internal", "放技能文件失败，已撤回这次写的文件") from None
    try:
        # 13.3：enabled 为 false 时启动项文件按停用收起（只写成 .plist.disabled），不装载、不启动
        step = hub_addbot._register_dsh(fields, deadline=deadline - UNDO_RESERVE, stashed=not fields["enabled"]) \
            if sys.platform == "darwin" else {}
    except Exception:                       # noqa: BLE001
        step = {}
    if step.get("state") != "done":
        _undo_create(bot_id, seams, fields.get("life_file"))
        if step.get("detail") == "timeout" or time.monotonic() >= deadline - UNDO_RESERVE:
            raise Refuse(503, "unavailable", "装启动项没在时限内做完，已撤回这次写的文件与启动项")
        raise Refuse(503, "unavailable", "装启动项失败，已撤回这次写的文件与启动项")
    return probe.get("username")


def _create_bot():
    """新建（草案 A，补充文件 12.2）：形状（422）→ 编号已占用（409）→ getMe（422）→ 落盘 → 装启动项并拉起。

    校验、getMe、落盘、装启动项都调管理台新建页那几个函数，效果同新建页；查重、挑端口、查令牌占用只看本系统
    （新建页那几处会读另一套系统的配置与频道目录，归属边界外）。全程持新建页那把锁。整条 55 秒封顶：装启动项
    到点或失败回 503，已写的文件与已装的启动项全部撤掉。不等心跳，``running`` 按启动项此刻是否在跑（同 9.2「启」）。
    ``telegram_token``、``fish_api_key`` 不进响应、日志、报错。
    """
    deadline = time.monotonic() + CREATE_BUDGET - 1.0     # 留一秒收尾
    request.max_content_length = PERSONA_MAX_BODY + 1      # 人设正文可到 65536 个字符，按人设的上限读
    body = _json_object(limit=PERSONA_MAX_BODY, too_big="请求体太大")
    bots = _own_bots()
    fields = _create_fields(body, bots)
    if config_sources.dsh_dir() is None:
        raise Refuse(500, "internal", "本系统的配置目录被关掉了，建不了 bot")
    seams = bot_launchd.resolve_seams()
    try:
        with hub_addbot.hold_addbot_lock():
            username = _create_locked(fields, bots, seams, deadline)
    except HubError as e:                   # 只有锁会抛到这里：管理台正在建 bot 或改配置
        raise _config_failure(e, "新 bot 的配置") from None
    bot_id = fields["bot_id"]
    running = fields["enabled"] and deadline - time.monotonic() > bot_launchd.LAUNCHCTL_TIMEOUT \
        and _unit_running(bot_id, seams)    # enabled 为 false 时没有装载，不必再问
    person = _person({"id": bot_id, "cfg": {"life_id": fields["person"]} if fields["person"] else {}})
    return {"ok": True, "bot_id": bot_id, "person": person, "username": username,
            "enabled": fields["enabled"], "running": running,           # 约定 v1.0 5.18.1 的成功体
            # 约定 v1.1 5.18.1 多带的三个键
            "features": fields["features_final"], "skills": fields["skills_placed"],
            "features_skipped": fields["features_skipped"]}


def _archive_root():
    return os.path.join(str(config_sources.dsh_home()), "archive")


def _archive(bot_id):
    """归档（草案 B，补充文件 12.3、13.4）：请求体 ``{}`` 或只带 ``shell_meta``（422）→ 名单（404）→ 按 5.8 停掉
    （停不下来 503，什么都不移）→ 整份移进 ``<DSH_BOT_HOME>/archive/<编号>-<UTC 时间>/``。不删任何东西。
    manifest 另记显示名、person 与原样的 ``shell_meta``（列表要用）。"""
    deadline = time.monotonic() + ENABLE_BUDGET - 1.0
    request.max_content_length = ARCHIVE_MAX_BODY + 1
    body = _json_object(allow_empty=True, limit=ARCHIVE_MAX_BODY, too_big="请求体太大")
    meta = body.get("shell_meta")
    if set(body) - {"shell_meta"} or ("shell_meta" in body and not _shell_meta_ok(meta)):
        raise _invalid("请求体只能是 {}，或只带 shell_meta（JSON 对象，序列化后不超过 64 KiB）")
    bots = _own_bots()
    bot = _require_bot(bot_id, bots)
    seams = bot_launchd.resolve_seams()
    _stop_bot(bot, seams, deadline)
    archive_id = "%s-%s" % (bot_id, _utc_stamp())
    folder = os.path.join(_archive_root(), archive_id)
    extra = {"display_name": bot["display_name"], "person": _person(bot), "shell_meta": meta}
    _move_out(bot, bots, seams, lambda items: _stash(bot_id, items, folder, extra), "archive")
    return {"ok": True, "archive_id": archive_id}


def _archives():
    """归档列表（12.3、13.4）：``<DSH_BOT_HOME>/archive/`` 下名字合规、带 manifest.json 的目录，按时间倒序。
    每项另给显示名与 person（manifest 没记的用编号）、``status``（manifest 列的都还在归档里为 complete）、``shell_meta``。"""
    root = _archive_root()
    try:
        names = os.listdir(root)
    except FileNotFoundError:
        names = []
    rows = []
    for name in names:
        m = ARCHIVE_ID_RE.match(name)
        when = _utc_time(m.group(2)) if m else None
        folder = os.path.join(root, name)
        if when and not os.path.islink(folder) and os.path.isfile(os.path.join(folder, MANIFEST)):
            data = _manifest_data(folder)
            shown, person, meta = data.get("display_name"), data.get("person"), data.get("shell_meta")
            rows.append({"archive_id": name, "bot_id": m.group(1),
                         "display_name": shown.strip() if _text(shown) else m.group(1),
                         "person": person.strip() if _text(person) else m.group(1),
                         "archived_at": when, "status": _archive_status(folder, data),
                         "shell_meta": meta if isinstance(meta, dict) else None})
    rows.sort(key=lambda row: (row["archived_at"], row["archive_id"]), reverse=True)
    return {"ok": True, "archives": rows}


def _read_manifest(folder, bot_id, seams):
    """归档的 manifest.json → ``[(原位置, 归档里的位置)]``；认不出来回 None。

    原位置只认这只 bot 那几处（``_bot_slots``，生活设定文件只认允许目录下的 .yml），归档里的位置只认
    这个目录里面的相对路径：清单被人改过，也不会把东西移到别处去。
    """
    items = _manifest_data(folder).get("items")
    if not isinstance(items, list) or not items:
        return None
    slots = {path for path, _rel in _bot_slots(bot_id, seams)}
    life = life_settings.allowed_dir()
    out = []
    for item in items:
        src, rel = (item.get("from"), item.get("to")) if isinstance(item, dict) else (None, None)
        if not (isinstance(src, str) and isinstance(rel, str)) or os.path.isabs(rel) \
                or os.path.normpath(rel) != rel or rel.split(os.sep)[0] in ("", ".", ".."):
            return None
        own_life = life is not None and os.path.dirname(src) == life[1] and src.endswith(".yml")
        if src not in slots and not own_life:
            return None
        out.append((src, os.path.join(folder, rel)))
    return out


def _keep_disabled(bot_id, seams):
    """恢复之后保持停用（12.3）：启动项文件是装着的写法就收起（只改名，不调 launchctl）；配置没记停用就记上。"""
    for timer in (False, True):
        bot_launchd.stash_plist(bot_id, timer, seams)
    bot = next((b for b in _own_bots() if b["id"] == bot_id), None)
    if bot is not None and bot["enabled"]:
        _write_enabled(bot_id, False)


def _restore(archive_id):
    """恢复（12.3）：``archive_id`` 不合规 422、不存在 404 → 按 manifest 整份移回原处；原处任何一处已有东西
    409，什么都不移。恢复后是停用状态，不装载启动项。incomplete 的只移回归档里还在的（约定 v1.0 5.18.3）。"""
    if _json_object(allow_empty=True):
        raise _invalid("请求体只能是 {}")
    m = ARCHIVE_ID_RE.match(archive_id)
    if not m or not _utc_time(m.group(2)):
        raise _invalid("archive_id 要是 <bot 编号>-<UTC 时间，形如 20261015T080102Z>")
    bot_id, folder = m.group(1), os.path.join(_archive_root(), archive_id)
    if os.path.islink(folder) or not os.path.isdir(folder):
        raise Refuse(404, "unknown_archive", "没有这份归档")      # 13.4
    seams = bot_launchd.resolve_seams()
    try:
        with hub_addbot.hold_addbot_lock():
            items = _read_manifest(folder, bot_id, seams)
            if items is None:
                raise Refuse(500, "internal", "这份归档的清单认不出来，没有恢复")
            # incomplete（归档中途断了）：没移走的本来就还在原处，不算重名；只把已移走的放回去
            items = [(src, stored) for src, stored in items if os.path.lexists(stored)]
            if not items:
                raise Refuse(500, "internal", "这份归档里什么都没有，没有恢复")
            if any(os.path.lexists(src) for src, _stored in items):
                raise Refuse(409, "exists", "原处已经有同名的 bot 或文件，没有恢复")
            moved = []
            try:
                for src, stored in items:
                    os.makedirs(os.path.dirname(src), exist_ok=True)
                    shutil.move(stored, src)
                    moved.append((src, stored))
            except OSError as e:
                _log.error("restore failed: %s", type(e).__name__)
                for src, stored in reversed(moved):
                    with contextlib.suppress(OSError):
                        shutil.move(src, stored)
                raise Refuse(500, "internal", "移回原处时出错，已移回的尽量放回了归档") from None
            _prune(folder)
    except HubError as e:
        raise _config_failure(e, "这只 bot 的文件") from None
    _keep_disabled(bot_id, seams)
    return {"ok": True, "bot_id": bot_id, "enabled": False, "running": False}      # 13.4


def _secret_files(bot):
    """删除时直接抹掉、不进废纸篓的两份密钥：频道目录的 .env（13.5）与 state/api.key（v1.1 5.18.4）。

    这只 bot 自己家里的固定位置（``channel/.env``、``state/api.key``）一律算上，频道目录写到家里别处的再加上
    那两处；所在目录一路没有软链、又在家里的才算（文件本身是软链的只删链接，不碰它指向的文件）。家以外的
    （频道目录写到另一套系统那边的）不碰：那里的文件不归这边管，也不会跟着 bot 目录进废纸篓。"""
    home = os.path.join(os.path.realpath(str(config_sources.dsh_home())), "bots", bot["id"])
    paths = [os.path.join(home, "channel", ".env"), os.path.join(home, "state", "api.key")]
    channel = _channel_file(bot, ".")
    if channel is not None:
        channel = os.path.normpath(channel)
        paths += [os.path.join(channel, ".env"), os.path.join(os.path.dirname(channel), "state", "api.key")]
    out = []
    for path in paths:
        parent = os.path.dirname(path)
        if parent.startswith(home + os.sep) and os.path.realpath(parent) == parent and path not in out:
            out.append(path)
    return out


def _delete(bot_id):
    """删除（草案 C，补充文件 12.4）：请求体只许 ``{"confirm": "<编号>"}``（422）→ 名单（404）→ 按 5.8 停掉
    （停不下来 503，什么都不动）→ 12.3 列的那些移进废纸篓（没有废纸篓才直接删）。
    朋友圈库、共用的表情包与画风、语音桥、生成的图片都不在要移的那几处里，不动。"""
    deadline = time.monotonic() + ENABLE_BUDGET - 1.0
    body = _json_object()
    if set(body) != {"confirm"} or body["confirm"] != bot_id:
        raise _invalid('请求体只能是 {"confirm": "<这只 bot 的编号>"}，编号要与地址里的一字不差')
    bots = _own_bots()
    bot = _require_bot(bot_id, bots)
    seams = bot_launchd.resolve_seams()
    _stop_bot(bot, seams, deadline)
    def drop_env_then_discard(items):
        # 13.5：Telegram 令牌所在的 .env 先直接删掉，不进废纸篓；删不掉整次 500，别的一样都没移。
        # 约定 v1.1 5.18.4：网关本机接口口令 state/api.key 同样直接抹掉（归档时照旧带走，恢复要用）
        for secret in _secret_files(bot):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(secret)
        _discard(bot_id, items)

    _move_out(bot, bots, seams, drop_env_then_discard, "delete")
    return {"ok": True, "message": DELETED}


def _profile_get(bot_id):
    """读显示名（草案 D）：同列表里的 ``display_name``（配置没写就是编号）。停用的照常能读。"""
    return {"ok": True, "display_name": _require_bot(bot_id)["display_name"]}


def _profile_put(bot_id):
    """改显示名（草案 D，补充文件 12.5）：写法同管理台编辑页改名（``set_scalar`` 只换这一个值、写盘前读回来
    核对、原子写，再同步朋友圈库 ``bot_profiles.display_name``），同一把锁。朋友圈库没写成就把配置写回原样。
    停用的也能改；不重启（名字每次现读）。"""
    from moments import hub_config         # 延迟 import：同 _write_enabled
    import db as botlife_db                 # 延迟 import：仓库根模块，同 hub_addbot._save_db_name
    body = _json_object()
    name = body.get("display_name")
    if set(body) != {"display_name"} or not isinstance(name, str):
        raise _invalid("请求体只能有 display_name 一个键，值是字符串")
    name = name.strip()
    if not 1 <= len(name) <= PROFILE_NAME_MAX or any(hub_addbot._name_char_banned(ch) for ch in name):
        raise _invalid("display_name 去掉首尾空白后要是 1 到 %d 个字符，不能有控制字符与换行" % PROFILE_NAME_MAX)
    bot = _require_bot(bot_id)
    path = os.path.join(str(config_sources.dsh_dir()), bot_id + ".yml")
    try:
        with hub_addbot.hold_addbot_lock():
            with open(path, encoding="utf-8") as f:
                text = f.read()
            new_text = hub_config.set_scalar(text, hub_config.compose(text), "display_name", name, "str")
            try:
                hub_addbot._readback(new_text, {"display_name": name})
            except HubError:
                raise _invalid("这个名字写进配置之后读不回原样，换一个") from None
            hub_config.atomic_write(new_text, path)
            try:
                botlife_db.set_display_name(_person(bot), name)
            except Exception as e:          # noqa: BLE001  库的异常消息可能带路径，只记类名
                _log.error("profile db write failed: %s", type(e).__name__)
                hub_config.atomic_write(text, path)
                raise Refuse(500, "internal", "朋友圈库里的显示名没写成，配置已恢复原样") from None
    except HubError as e:
        raise _config_failure(e, "这只 bot 的配置") from None
    except (OSError, ValueError, yaml.YAMLError):          # 读不了、不是 UTF-8、解析不出
        raise Refuse(500, "internal", "写这只 bot 的配置失败") from None
    return {"ok": True, "display_name": name}


# ---------------------------------------------------------------- v1.0 补充（补充文件第 13 节）

def _polling(bot_id, seams):
    """13.1：这只的网关进程此刻在不在跑。``launchctl print`` 说 running 为 True；launchd 里没有它、或说的是别的
    状态为 False；launchctl 没跑成（超时、起不来）或输出里认不出状态为 None。5.8 响应里的 ``running`` 同这一判定。

    不用 ``bot_launchd.print_state``：它把「没跑成」与「没有这个单元」都并成没在运行，这里要分开报。
    """
    label = bot_launchd.label_of(bot_id)
    if label is None:
        return False
    code, text, why = bot_launchd._run(["print", "gui/%d/%s" % (bot_launchd._uid(), label)], seams=seams)
    if why:
        return None
    if code != 0:
        return False
    state = bot_launchd.parse_print(text)[0]
    return None if state is None else state == "running"


def _username(health, cfg):
    """13.1：网关健康接口里的 ``telegram.username``；问不到取配置里记下的 ``telegram_username``；都没有为 None。不带 @。"""
    for value in (health.get("username"), cfg.get("telegram_username")):
        if _text(value) and value.strip().lstrip("@"):
            return value.strip().lstrip("@")
    return None


def _moved_path(bot_id):
    """13.2：迁出标记 ``<DSH_BOT_HOME>/bots/<编号>/.moved``。``bot_id`` 已与名单精确匹配过。"""
    return os.path.join(str(config_sources.dsh_bot_dir(bot_id)), MOVED_MARKER)


def _moved_to(bot_id):
    """13.1、13.2：迁出标记里的去向（``pending`` 或 ``claude``）；没有标记为 None。

    标记在（软链、目录也算在）但读不出、格式不对、去向不认识，一律按 ``pending`` 报，启用照样拒绝（宁可拦住）。
    """
    path = _moved_path(bot_id)
    if not os.path.lexists(path):
        return None
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | _NOFOLLOW), "rb") as f:
            m = MOVED_LINE_RE.match(f.read(4096).decode("utf-8"))
    except (OSError, ValueError):
        return "pending"
    return m.group(1) if m and m.group(1) in MOVED_TARGETS else "pending"


def _write_moved(bot_id, moved_to, op):
    """13.2：给了去向就写标记（一行 ``to=<去向> op=<op_id 或 -> at=<UTC 时间>``，0600，原子写），给 None 就删掉。
    写不成、删不掉回 500，这时还没去停它。"""
    path = _moved_path(bot_id)
    try:
        if moved_to is None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
            return
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        life_settings._replace(path, "to=%s op=%s at=%s\n" % (moved_to, op or "-", _utc_stamp()), 0o600)
    except OSError:
        raise Refuse(500, "internal", "改这只 bot 的迁出标记失败，没有停它") from None


def _valid_op_id(value, kinds=OP_KINDS):
    """13.6 的 op_id：整串合正则，且种类在 ``kinds`` 里（正则过了、种类不认识也算不合规）。"""
    m = OP_ID_RE.match(value) if isinstance(value, str) else None
    return m is not None and m.group(1) in kinds


def _queue_provider(bot, body, has_provider, has_effort):
    """13.7：停着的 bot 换供应商、模型、强度——核对过就写进它配置的 brain 段，不启动，回 queued。

    供应商与模型用在跑的 bot 的网关读来的清单核对（``_provider_catalog``），一只都问不到时按存档核对；存档也用不了、
    或回的东西认不出，都算核对不了，503，什么都不写；不在清单 404，这家没有这个模型 422。只改强度不用核对。
    不碰这只自己的网关。
    网关下次启动按配置这一组起：它账本里的单独指定（brain_override）记着当时配置那一组与 ``chosen_at``，
    两样有一样变了就清掉（gateway/src/engine/engine.ts 的 withOverride、overrideKey）。每次都写一个新的
    ``chosen_at``：选的正好就是配置里现在这一组时，配置那一组没变，靠它清掉聊天里临时切的。
    """
    changes = {}
    if has_provider:
        try:
            catalog = _provider_catalog()[0]
        except Refuse:
            raise
        except Exception as e:              # noqa: BLE001  认不出的回复同样是核对不了；只记类名
            _log.error("provider catalog failed: %s", type(e).__name__)
            raise Refuse(503, "unavailable", "读不到供应商清单，没法核对，这次没有保存") from None
        _require_offered(catalog, body["provider_id"], body["model"])
        changes.update(provider=body["provider_id"], model=body["model"])
    if has_effort:
        changes["reasoning_effort"] = body["effort"]
    changes["chosen_at"] = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    _write_brain(bot["id"], changes)
    return {"ok": True, "applied": False, "queued": True, "new_segment": False, "message": QUEUED}


def _brain_text(text, changes):
    """配置全文 → 把 brain 段的几项改成 ``changes`` 之后的全文，不 dump 整份。

    brain 是非空映射：逐项行级改值（``hub_config.set_scalar``，每改一项重新解析一次，下标才不会错位）；
    brain 是空的、null 或标量：整段换成一个流式映射；没有 brain 段：补一个（整份是 JSON 写法时插在第一个键前面）。
    """
    from moments import hub_config         # 延迟 import：同 _write_enabled
    root = hub_config.compose(text)
    if not isinstance(root, yaml.MappingNode) or not root.value:
        raise ValueError("config root is not a mapping")
    node = hub_config.find(root, ["brain"])
    if isinstance(node, yaml.MappingNode) and node.value:
        for key, value in changes.items():
            text = hub_config.set_scalar(text, hub_config.compose(text), "brain." + key, value, "str")
        return text
    literal = json.dumps(changes, ensure_ascii=False)        # 流式映射，也是合法的 YAML
    if node is not None:
        return hub_config.splice(text, [(node.start_mark.index, node.end_mark.index, " " + literal)])
    if root.flow_style:
        at = root.value[0][0].start_mark.index
        return text[:at] + "brain: %s, " % literal + text[at:]
    return text + ("" if text.endswith("\n") else "\n") + "brain: %s\n" % literal


def _write_brain(bot_id, changes):
    """13.7：把 ``changes``（provider、model、reasoning_effort 里的几项，加 chosen_at）写进这只 bot 配置的 brain 段。

    持管理台写配置那把锁；读回来核对只变了这几项（别的键、brain 里别的项一个不差）；原文先备份到
    ``<DSH_BOT_HOME>/backups/config/<bot_id>/``（0600，只留 2 份，同生活设定的备份）；原子写、保持原权限。
    """
    from moments import hub_config         # 延迟 import：同 _write_enabled
    path = os.path.join(str(config_sources.dsh_dir()), bot_id + ".yml")
    try:
        with hub_addbot.hold_addbot_lock():
            with open(path, "rb") as f:
                raw = f.read()
            text = raw.decode("utf-8")
            new_text = _brain_text(text, changes)
            old, new = yaml.safe_load(text), yaml.safe_load(new_text)
            old_brain = old.get("brain") if isinstance(old.get("brain"), dict) else {}
            if not isinstance(new, dict) or new.get("brain") != dict(old_brain, **changes) or \
                    {k: v for k, v in new.items() if k != "brain"} != {k: v for k, v in old.items() if k != "brain"}:
                raise Refuse(500, "internal", "改这只 bot 的配置时读回来对不上，没有保存")
            folder = life_settings._backup(bot_id, raw, kind="config", suffix=".yml")
            hub_config.atomic_write(new_text, path)
            life_settings._rotate(folder)   # 写盘成功之后才清理旧备份
    except HubError as e:
        raise _config_failure(e, "这只 bot 的配置") from None
    except (OSError, ValueError, AttributeError, yaml.YAMLError):    # 读不了、不是 UTF-8、解析不出、顶层不是映射
        raise Refuse(500, "internal", "写这只 bot 的配置失败") from None


def _create_extra(fields):
    """13.3 新建另写进配置的几行：``enabled``（false 时）、``life_id``（person 与编号不同时）、
    ``telegram_username``、``telegram_user_id``（给了才写）。值都按 JSON 写（也是合法 YAML），读回来核对过。"""
    want = {}
    if not fields["enabled"]:
        want["enabled"] = False
    if "features_final" in fields:          # 约定 v1.1 5.19.4：新建时九个键都显式写进去
        want["features"] = fields["features_final"]
    if fields["person"] and fields["person"] != fields["bot_id"]:
        want["life_id"] = fields["person"]
    if fields["telegram_username"]:
        want["telegram_username"] = fields["telegram_username"]
    if fields["telegram_user_id"] is not None:
        want["telegram_user_id"] = fields["telegram_user_id"]
    text = "".join("%s: %s\n" % (key, json.dumps(value)) for key, value in want.items())
    if (yaml.safe_load(text) or {}) != want:
        raise _invalid("新字段写进配置之后读不回原样")
    return text


def _archived_ports():
    """13.3：各份归档里那份配置（``configs/<编号>.yml``）记下的 ``dispatcher_port``；读不出的跳过。"""
    ports = set()
    root = _archive_root()
    try:
        names = os.listdir(root)
    except OSError:
        return ports
    for name in names:
        m = ARCHIVE_ID_RE.match(name)
        if not m:
            continue
        path = os.path.join(root, name, "configs", m.group(1) + ".yml")
        try:
            with os.fdopen(os.open(path, os.O_RDONLY | _NOFOLLOW), "rb") as f:
                cfg = yaml.safe_load(f.read(MAX_BODY))
        except (OSError, ValueError, yaml.YAMLError):
            continue
        if isinstance(cfg, dict) and hub_addbot._is_int(cfg.get("dispatcher_port")):
            ports.add(cfg["dispatcher_port"])
    return ports


def _templates():
    """13.3：本系统的人设模板 ``[{"id", "label"}]``——频道目录（落点、软链同 10.2）下有人设文件的本系统 bot，
    新建时 ``persona_template`` 给它的编号就用它的人设。另一套系统的频道目录在归属边界外，不列。"""
    items = []
    for bot in _own_bots():
        path = _channel_file(bot, "CLAUDE.md")
        if path is not None and os.path.isfile(path):
            items.append({"id": bot["id"], "label": bot["display_name"]})
    return {"ok": True, "templates": items}


def _shell_meta_ok(meta):
    """13.4：``shell_meta`` 是 JSON 对象，按 UTF-8 序列化后不超过 64 KiB（存得进 manifest 的才收）。"""
    if not isinstance(meta, dict):
        return False
    try:
        return len(json.dumps(meta, ensure_ascii=False).encode("utf-8")) <= SHELL_META_MAX
    except (UnicodeEncodeError, ValueError, RecursionError):
        return False


def _manifest_data(folder):
    """归档的 manifest.json（JSON 对象）；不是软链才读，读不出、太大、不是对象都回 ``{}``。"""
    try:
        with os.fdopen(os.open(os.path.join(folder, MANIFEST), os.O_RDONLY | _NOFOLLOW), "rb") as f:
            raw = f.read(MANIFEST_MAX + 1)
        data = json.loads(raw.decode("utf-8")) if len(raw) <= MANIFEST_MAX else None
    except (OSError, ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def _archive_status(folder, data):
    """13.4：manifest 里列的每一样都还在归档里为 ``complete``；少了任何一样、或清单认不出为 ``incomplete``。"""
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return "incomplete"
    for item in items:
        rel = item.get("to") if isinstance(item, dict) else None
        if not isinstance(rel, str) or os.path.isabs(rel) or os.path.normpath(rel) != rel \
                or rel.split(os.sep)[0] in ("", ".", "..") or not os.path.lexists(os.path.join(folder, rel)):
            return "incomplete"
    return "complete"


def _read_bot_token(bot):
    """13.6：这只 bot 频道目录 ``.env`` 里的 ``TELEGRAM_BOT_TOKEN``；读不到 500（话里不带路径与文件内容）。"""
    path = _channel_file(bot, ".env")
    token = ""
    if path is not None:
        try:
            with os.fdopen(os.open(path, os.O_RDONLY | _NOFOLLOW), "rb") as f:
                hit = ENV_TOKEN_LINE_RE.search(f.read(MAX_BODY).decode("utf-8"))
            token = hit.group(1) if hit else ""
        except (OSError, ValueError):
            token = ""
    if not hub_addbot.TOKEN_RE.match(token):
        raise Refuse(500, "internal", "读不到这只 bot 的 Telegram 令牌")
    return token


def _handoff_path():
    return os.path.join(str(config_sources.dsh_home()), "state", HANDOFF_FILE)


def _handoff_records(path):
    """已交记录 ``[{"bot_id", "op_id", "at"}]``（at 是 Unix 秒）。文件不在是空；读不出、认不出回 500：
    判不了「交没交过」就不交。"""
    try:
        with open(path, "rb") as f:
            data = json.loads(f.read().decode("utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError, RecursionError):
        data = None
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, list):
        raise Refuse(500, "internal", "令牌转交的记录读不出来，这次没有交出令牌")
    return [r for r in records if isinstance(r, dict)]


def _token_handoff(bot_id):
    """令牌转交（补充文件 13.6）：形状（422）→ 名单（404）→ 同一个 op_id 交过（409）→ 这只 10 分钟内交过（429）→
    读令牌（500）→ 记下（只记 bot_id、op_id、时间，只留最近 200 条，0600，原子写）→ 回令牌。

    记下之后才回令牌：记不下就不交（500），「每个 op_id 只交一次」才算数。令牌只出现在这一个成功响应体里，
    日志只记 bot_id、op_id、结果。读、判、写在同一把锁里。不要求先停用。
    """
    body = _json_object()
    if set(body) != {"op_id", "to", "purpose"} or not _valid_op_id(body["op_id"], ("switch",)) \
            or body["to"] != "claude" or body["purpose"] != "switch":
        raise _invalid('请求体只能是 {"op_id": "<…-switch-…>", "to": "claude", "purpose": "switch"}')
    op = body["op_id"]
    bot = _require_bot(bot_id)
    with _handoff_lock:
        path = _handoff_path()
        records = _handoff_records(path)
        now = time.time()
        if any(r.get("op_id") == op for r in records):
            _log.info("token handoff: bot=%s op=%s result=already_handed", bot_id, op)
            raise Refuse(409, "already_handed", "这个 op_id 已经交过令牌了")
        if any(r.get("bot_id") == bot_id and type(r.get("at")) in (int, float) and now - r["at"] < HANDOFF_WINDOW
               for r in records):
            _log.info("token handoff: bot=%s op=%s result=too_many", bot_id, op)
            raise Refuse(429, "too_many", "这只 bot 10 分钟内已经交过一次令牌，请稍后再试")
        token = _read_bot_token(bot)
        records = (records + [{"bot_id": bot_id, "op_id": op, "at": int(now)}])[-HANDOFF_KEEP:]
        try:
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            life_settings._replace(path, json.dumps({"records": records}, ensure_ascii=False) + "\n", 0o600)
        except OSError:
            raise Refuse(500, "internal", "令牌转交的记录写不进去，这次没有交出令牌") from None
    _log.info("token handoff: bot=%s op=%s result=ok", bot_id, op)
    return _json({"ok": True, "telegram_token": token}, 200, {"Cache-Control": "no-store"})


#: 不带 bot 编号的接口：路径 → {方法: 处理函数}（键的先后就是 405 的 Allow 头）
_TOP_ROUTES = {
    PREFIX + "/meta": {"GET": _meta},
    PREFIX + "/bots": {"GET": _bots, "POST": _create_bot},
    PREFIX + "/bots/templates": {"GET": _templates},
    PREFIX + "/providers": {"GET": _providers},
    PREFIX + "/archives": {"GET": _archives},
}
#: ``/bots/<bot_id>/<这一段>`` 上的接口：方法 → 处理函数（键的先后就是 405 的 Allow 头）
_BOT_ROUTES = {
    "provider": {"POST": _set_provider},
    "restart": {"POST": _restart},
    "notify": {"POST": _notify},
    "life": {"GET": _life_get, "PUT": _life_put},
    "enabled": {"POST": _set_enabled},
    "groups": {"GET": _groups_get, "POST": _groups_post},
    "features": {"GET": _features_get, "PUT": _features_put},
    "persona": {"GET": _persona_get, "PUT": _persona_put},
    "voice": {"GET": _voice_get, "PUT": _voice_put},
    "archive": {"POST": _archive},
    "delete": {"POST": _delete},
    "profile": {"GET": _profile_get, "PUT": _profile_put},
    "token_handoff": {"POST": _token_handoff},
}
#: ``/bots/<bot_id>/skills/<通用名>`` 上的接口（11.2）：处理函数多收一个通用名
_SKILL_ROUTES = {"GET": _skill_get, "PUT": _skill_put}
#: ``/bots/<bot_id>/groups/discovered``（约定 v1.1 5.20.2）
_DISCOVERED_ROUTES = {"GET": _groups_discovered}


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
    # oauth 账号管理（与 8765 同一批处理函数）；activate 排在两段的删除路由之前，同「写死的段优先」
    ("/hub/api/oauth/accounts", {"GET": hub_routes.oauth_accounts}),
    ("/hub/api/oauth/start", {"POST": hub_routes.oauth_start}),
    ("/hub/api/oauth/submit", {"POST": hub_routes.oauth_submit}),
    ("/hub/api/oauth/status", {"GET": hub_routes.oauth_status}),
    ("/hub/api/oauth/accounts/<provider>/<email_hash>/activate",
     {"POST": hub_routes.oauth_account_activate}),
    ("/hub/api/oauth/accounts/<provider>/<email_hash>", {"DELETE": hub_routes.oauth_account_delete}),
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
