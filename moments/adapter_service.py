# -*- coding: utf-8 -*-
"""dsh 这一半的适配服务：把本系统的 bot 用五个本机 HTTP 接口交给另一套系统的管理台（壳）。

契约：``.devflow/INTERFACE-适配约定-v0.3.md``（两边定稿）与 ``INTERFACE-适配服务.md``（本系统补充）。
起服务：仓库根执行 ``python3 -m moments.adapter_service``；测试用 ``create_app()``。
只支持这一种启动方式：监听地址只许回环、令牌文件不在就拒绝启动，这两道检查都在 ``main()`` 里。
``flask run`` 等别的方式拉起 ``create_app()`` 会绕过它们（例如对外监听），不要这样起。

本模块只做三件事：换一种鉴权（令牌文件）、按约定的形状组响应、把错误翻成约定的错误码。
判状态、读生效的模型、经网关改思考强度、重启并等心跳，全部调管理台已有的函数
（``bots_client`` / ``bot_launchd`` / ``config_sources`` / ``gateway_client``），这里不重写。
只有「读 bot 配置」不走管理台那条共用读法，见 ``_own_bots``。

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
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import yaml
from flask import Flask, Response, request
from werkzeug.exceptions import RequestEntityTooLarge

import gateway_client
from moments import bot_launchd, bots_client, config_sources

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

#: 配置文件没写 brain 时网关自己用的默认值（gateway/src/config.ts 的 parseBrain）。
CONFIG_DEFAULT_PROVIDER = "deepseek-official"
CONFIG_DEFAULT_MODEL = "deepseek-flash"

#: 网关 ``POST /v1/model`` 把 spec 截到 200 个字符（管理台模型页同一个上限）：超了就别发。
MAX_SPEC = 200
#: 网关 ``key`` 的四种取值里算「有密钥」的两种（补充文件 5.1：ok 已配，none 这家不需要）。
KEY_READY = ("ok", "none")

NOT_SAVED = "这只 bot 的进程现在连不上，这次没有保存"
NOT_SAVED_SLOW = "这只 bot 的进程没有及时回应，这次没有保存"
MAYBE_SAVED = "这只 bot 的进程没有及时回应，这次改动可能已经保存，请刷新后确认"
NO_RUNNING_BOT = "没有正在运行的 bot，暂时读不到供应商清单"

UNAUTHORIZED = {"ok": False, "code": "unauthorized", "message": "令牌缺失或不对"}
INTERNAL = {"ok": False, "code": "internal", "message": "内部错误"}

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_log = logging.getLogger("dsh.adapter")

#: 同一只 bot 同一时刻只许一个重启在跑（进程内）。键是已经过名单精确匹配的 bot id。
_restart_locks = {}
_restart_locks_guard = threading.Lock()


class Refuse(Exception):
    """按约定的失败形状回给调用方的拒绝：HTTP 状态码、``code``、给人看的一句话。"""

    def __init__(self, status, code, message, headers=None):
        super().__init__(code)
        self.status, self.code, self.message, self.headers = status, code, message, headers


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
        if len(parts) == 2 and parts[1] in _WRITE_ROUTES:
            _require_method(method, "POST")
            if not _valid_bot_id(parts[0]):
                raise _invalid("bot 编号不合规：只能用字母、数字、点、下划线、横线，1 到 64 个字符")
            return _WRITE_ROUTES[parts[1]](parts[0])
    raise Refuse(404, "not_found", "没有这个接口")


def _require_method(method, allowed):
    if method != allowed:
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
        return _json(_dispatch())
    except Refuse as r:
        return _json({"ok": False, "code": r.code, "message": r.message}, r.status, r.headers)
    except Exception as e:                  # noqa: BLE001  异常消息可能带路径，只记类名
        _log.error("adapter internal error: %s", type(e).__name__)
        return _json(INTERNAL, 500)


def _json_object(allow_empty=False):
    """请求体必须是一个 JSON 对象；``allow_empty`` 时空体等同 ``{}``。只在过了鉴权之后才读。"""
    bad = _invalid("请求体必须是一个 JSON 对象")
    length = request.content_length
    if length is not None and length > MAX_BODY:
        raise bad
    # 分块传输没有 Content-Length：靠应用的 MAX_CONTENT_LENGTH（MAX_BODY + 1，见 create_app）
    # 让框架最多读这么多就停，读满了就是超了。框架某些情况下改为抛 413，同样按不合规拒绝。
    try:
        raw = request.get_data(cache=False)
    except RequestEntityTooLarge:
        raise bad from None
    if len(raw) > MAX_BODY:
        raise bad
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


def _require_bot(bot_id):
    """名单里有这只 bot 才往下走（精确匹配，不拿 bot_id 去拼任何路径），返回它那一条。"""
    for bot in _own_bots():
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


def _bot_row(bot, seams):
    """列表里的一行（约定 5.2 的八个键）。在线程池里跑：只用传进来的值，不读环境与配置。"""
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
    return {"bot_id": bot_id, "display_name": bot["display_name"],
            "state": state, "state_text": state_text,
            "provider": {"id": provider, "name": provider, "model": model, "effort": effort},
            "provider_source": source, "pending": None, "detail": {}}


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


def _set_provider(bot_id):
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
        info = _gateway(lambda: gateway_client.model_info(channel), NOT_SAVED_SLOW)
        _require_offered(_catalog(info), body["provider_id"], body["model"])
        before = _pair(info.get("current"))  # 同一次读里现在生效的那一组，用来判断是不是换了一家
        # 第 5 步：以上都过了才写。
        if has_effort:
            # 强度被拒时要把供应商和模型换回去，而网关换模型会清掉单独指定的强度：先记下原来的。
            prior_effort = _gateway(lambda: _effort_override(channel), NOT_SAVED_SLOW)
        after = _switch(channel, before, body["provider_id"], body["model"])
    if has_effort:
        try:
            _apply_effort(channel, body["effort"])
        except Exception as e:              # noqa: BLE001  不只是拒绝：网关回 400、500、认不出的也在这里
            if after != before:             # 供应商和模型这一步已经写进去了（约定 5.4：不许写了一半）
                if isinstance(e, Refuse) and e.status != 422:
                    # 网关断了或没回应：换不回去，强度那一步也可能其实已经保存，只能把话说实
                    raise Refuse(e.status, e.code, "供应商和模型已经换了，但思考强度"
                                 "没有确认改成，请刷新后确认") from None
                # 网关明说不支持（422），或回了 400、500、认不出的东西：先换回原来那一组。
                # 换回去了再照原样往外报（认不出的仍是 500 内部错误，但设置已经是原样）；
                # 换不回去由 _undo_switch 把现状说实。
                _undo_switch(channel, before, prior_effort)
            raise
    new_segment = has_provider and after[0] != before[0]
    message = "已保存，下一条消息起生效"
    if new_segment:
        message += "；换到了另一家供应商，当前对话会先总结再另开一段"
    return {"ok": True, "applied": True, "queued": False, "new_segment": new_segment,
            "message": message}


def _gateway(call, when_sent):
    """向这只 bot 的网关发一次请求，把网络上的事翻成 503；别的异常原样往外抛（外层回 500）。

    ``when_sent``：请求发出去之后才断或超时的那句话。写请求可能已经保存、读请求没有，
    两种话不一样，由调用方给。约定里没有单独的码，都归到 503。
    """
    try:
        return call()
    except Exception as e:                  # noqa: BLE001  下面按类型分流
        kind = gateway_client.net_kind(e)
        if kind == "unreachable":
            raise Refuse(503, "unavailable", NOT_SAVED) from None
        if kind == "broken":
            raise Refuse(503, "unavailable", when_sent) from None
        raise


def _pair(choice):
    """网关回的一组 ``{"provider", "model"}`` → 元组；认不出就抛。"""
    if not isinstance(choice, dict) or not _text(choice.get("provider")) \
            or not _text(choice.get("model")):
        raise RuntimeError("unexpected gateway reply")
    return choice["provider"], choice["model"]


def _switch(channel, before, provider_id, model):
    """经这只 bot 的网关换供应商和模型，返回换之后生效的那一组 ``(供应商, 模型)``。

    ``before`` 是第 4 步那次读里现在生效的一组。发的是管理台模型页在用的写请求
    ``POST /v1/model``，请求体 ``{"spec": "供应商/模型"}``，网关回 ``{"ok", "text", "current"}``。
    已经是这一组时网关也回 ok，重复提交不叠加。
    网关明说没换（这只 bot 缺这家的密钥，或它的模型表里此刻没有这个模型）→ 422，什么都没写；
    网关的原话可能带密钥的变量名，不往外带。
    """
    ok, got = _gateway(lambda: gateway_client.model_set(channel, "%s/%s" % (provider_id, model)),
                       MAYBE_SAVED)
    after = _pair(got.get("current") if isinstance(got, dict) else None)
    if not ok:
        if got.get("ok") is False and after == before:
            raise _invalid("这只 bot 现在换不到这个模型：这家供应商在它那里可能还没配密钥，"
                           "或者它的模型表里暂时没有这个模型。设置没有变")
        raise RuntimeError("unexpected gateway reply")
    return after


def _apply_effort(channel, effort):
    """经这只 bot 的网关改思考强度：``POST /v1/effort``，请求体 ``{"effort": 档位}``。"""
    code, got = _gateway(lambda: gateway_client.effort_set(channel, effort), MAYBE_SAVED)
    if code == 409:
        # 网关的 409 unsupported：档位在四档之内，但这只 bot 现在用的模型不支持它
        raise _invalid("这只 bot 现在用的模型不支持这个思考强度")
    if code != 200 or not isinstance(got, dict) or got.get("ok") is not True:
        raise RuntimeError("unexpected gateway reply")


def _undo_switch(channel, before, prior_effort):
    """强度被拒之后把供应商和模型换回 ``before``，原来单独指定过强度的再设回去。

    换回配置文件里那一组时网关自己会清掉单独指定，不留痕迹。换不回去就抛 500，把现状说实。
    """
    try:
        ok, _got = gateway_client.model_set(channel, "%s/%s" % before)
        if ok and prior_effort is not None:
            code, got = gateway_client.effort_set(channel, prior_effort)
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


_READ_ROUTES = {PREFIX + "/meta": _meta, PREFIX + "/bots": _bots, PREFIX + "/providers": _providers}
_WRITE_ROUTES = {"provider": _set_provider, "restart": _restart}


# ---------------------------------------------------------------- 应用与命令行

def create_app():
    """适配服务的 Flask 应用。不注册任何 URL 规则：全部请求在 ``before_request`` 里由
    ``_respond`` 接走（鉴权先于路由；路由按原样路径自己比对，见模块说明）。"""
    app = Flask(__name__)
    # 请求体上限（安审 S4）。带 Content-Length 的在 _json_object 里先挡；分块传输的没有这个头，
    # 框架读这种流时最多读到这个数就停（不抛错、不读进更多）。多给 1 个字节，_json_object 才分得清
    # 「正好 MAX_BODY」与「超了」。
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY + 1

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
