# -*- coding: utf-8 -*-
"""dsh 这一半的适配服务：把本系统的 bot 用五个本机 HTTP 接口交给另一套系统的管理台（壳）。

契约：``.devflow/INTERFACE-适配约定-v0.3.md``（两边定稿）与 ``INTERFACE-适配服务.md``（本系统补充）。
起服务：仓库根执行 ``python3 -m moments.adapter_service``；测试用 ``create_app()``。

本模块只做三件事：换一种鉴权（令牌文件）、按约定的形状组响应、把错误翻成约定的错误码。
列 bot、判状态、读生效的模型、经网关改思考强度、重启并等心跳，全部调管理台已有的函数
（``bots_client`` / ``bot_launchd`` / ``config_sources`` / ``gateway_client``），这里不重写。

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

from flask import Flask, Response, request

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
GATEWAY_READ_TIMEOUT = 2.0
#: 重启从发命令算起的总时限（补充文件 4.5：50 秒内没等到就回 failed；壳等 60 秒）。
RESTART_BUDGET = 50.0
MAX_ROW_WORKERS = 8

#: 配置文件没写 brain 时网关自己用的默认值（gateway/src/config.ts 的 parseBrain）。
CONFIG_DEFAULT_PROVIDER = "deepseek-official"
CONFIG_DEFAULT_MODEL = "deepseek-flash"

#: 网关 ``POST /v1/model`` 把 spec 截到 200 个字符（管理台模型页同一个上限）：超了就别发。
MAX_SPEC = 200

NOT_SAVED = "这只 bot 的进程现在连不上，这次没有保存"
NOT_SAVED_SLOW = "这只 bot 的进程没有及时回应，这次没有保存"
MAYBE_SAVED = "这只 bot 的进程没有及时回应，这次改动可能已经保存，请刷新后确认"

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
    raw = request.get_data(cache=False)
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

def _bot_entries():
    """本系统的 bot ``[{"id", "display_name"}]``，按编号字典序。

    范围与管理台单只重启的名单是同一句判据（过放行闸的：生效的那份配置在新系统配置根下、
    文件名是合格的 bot id）。另一套系统的 bot 不在里面。名单读不出来就抛，外层回 500。
    """
    entries = bots_client.hub_visible(bots_client.list_bots(), bots_client.bot_sources(),
                                      strict=True)
    return sorted(entries, key=lambda e: e["id"])


def _require_bot(bot_id):
    """名单里有这只 bot 才往下走（精确匹配，不拿 bot_id 去拼任何路径）。"""
    if bot_id not in {e["id"] for e in _bot_entries()}:
        raise Refuse(404, "unknown_bot", "没有这只 bot")


def _require_enabled(bot_id):
    if not bot_launchd.enabled_of(bot_id):
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


def _bot_row(entry, cfg, enabled, seams):
    """列表里的一行（约定 5.2 的八个键）。在线程池里跑：只用传进来的值，不读环境与配置。"""
    bot_id = entry["id"]
    brain = _config_brain(cfg)
    live = None
    if not enabled:
        state, state_text = "stopped", "已停用"
    else:
        row_state, _pid = bot_launchd.row_state(bot_id, seams)
        if row_state == "online":
            state, state_text = "running", "运行中"
        elif row_state == "starting":
            state, state_text = "running", "启动中"
        else:
            state, state_text = "broken", "没在运行：没有新鲜的心跳，启动项也不在跑"
        live = _live_brain(config_sources.channel_dir(cfg))
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
    return {"bot_id": bot_id, "display_name": entry["display_name"],
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
    entries = _bot_entries()
    if not entries:
        return {"ok": True, "bots": []}
    # 接缝、每只的启停、每只的配置都在处理请求的线程里读好，线程池里不再读环境与配置。
    seams = bot_launchd.resolve_seams()
    configs = config_sources.effective_configs()
    enabled = {e["id"]: bot_launchd.enabled_of(e["id"]) for e in entries}
    with ThreadPoolExecutor(max_workers=min(MAX_ROW_WORKERS, len(entries))) as pool:
        futures = [pool.submit(_bot_row, e, configs.get(e["id"]) or {}, enabled[e["id"]], seams)
                   for e in entries]
        return {"ok": True, "bots": [f.result() for f in futures]}


def _provider_catalog():
    """4.3 的供应商列表 ``[{"id", "name", "has_key", "models"}]``，``models`` 是模型名的数组。

    **还没实现，来源要两边定**（见开发交回报告的接口问题）。补充文件 4.4 与验收用例要求
    「供应商在不在、模型在不在」两项校验对网关零请求、一只 bot 都没有时也要列得出内置供应商，
    所以这份列表不能经网关读；可离线能读到的只有共用清单 ``<DSH_BOT_HOME>/providers.json``
    里自建供应商的名字和模型。剩下两样没有离线出处：

    - 内置供应商有哪些模型：只有 dsh 进程在运行时才回报，网关拿到也只放在内存里；
    - ``has_key``：密钥有没有只写在凭据文件里。管理台这一侧至今不读凭据文件（都是问网关），
      让这个服务去读是新开一个读取面，需求说明的敏感面声明里没有这一条。

    定下来之前这里一律抛：``GET /providers`` 与带 ``provider_id`` 的换供应商都回 500，
    不向任何网关发请求，不写任何东西。换供应商在这之后的部分（``_require_offered`` 与
    ``_switch``）已经做完，只认上面那个返回形状，不管它从哪来。
    """
    raise NotImplementedError("provider catalog source undecided")


def _require_offered(provider_id, model):
    """换供应商的两项校验（补充文件 4.4）：只看 4.3 的列表，不找任何网关。"""
    for provider in _provider_catalog():
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
    _require_bot(bot_id)
    _require_enabled(bot_id)                # 补充文件 4.4 第 3 步：停用先于清单校验，停用时什么都不读
    if has_provider:
        _require_offered(body["provider_id"], body["model"])
    channel = config_sources.channel_dir(config_sources.effective_config(bot_id) or {})
    if not gateway_client.available(channel):
        raise Refuse(503, "unavailable", NOT_SAVED)
    # 到这里校验全部通过，下面才开始找这只 bot 自己的网关。
    before = after = prior_effort = None
    if has_provider:
        if has_effort:
            # 强度被拒时要把供应商和模型换回去，而网关换模型会清掉单独指定的强度：先记下原来的。
            prior_effort = _gateway(lambda: _effort_override(channel), NOT_SAVED_SLOW)
        before, after = _switch(channel, body["provider_id"], body["model"])
    if has_effort:
        try:
            _apply_effort(channel, body["effort"])
        except Refuse as r:
            if after != before:             # 供应商和模型这一步已经写进去了（约定 5.4：不许写了一半）
                if r.status != 422:
                    # 网关断了或没回应：换不回去，强度那一步也可能其实已经保存，只能把话说实
                    raise Refuse(r.status, r.code, "供应商和模型已经换了，但思考强度"
                                 "没有确认改成，请刷新后确认") from None
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


def _switch(channel, provider_id, model):
    """经这只 bot 的网关换供应商和模型，返回换之前、换之后生效的两组 ``(供应商, 模型)``。

    发的是管理台模型页在用的那两个请求：``GET /v1/model`` 读现在生效的那一组（判断是不是
    换到了另一家），再 ``POST /v1/model``，请求体 ``{"spec": "供应商/模型"}``，网关回
    ``{"ok", "text", "current"}``。已经是这一组时网关也回 ok，重复提交不叠加。
    网关明说没换（这只 bot 缺这家的密钥，或它的模型表里此刻没有这个模型）→ 422，什么都没写；
    网关的原话可能带密钥的变量名，不往外带。
    """
    info = _gateway(lambda: gateway_client.model_info(channel), NOT_SAVED_SLOW)
    before = _pair(info.get("current") if isinstance(info, dict) else None)
    ok, got = _gateway(lambda: gateway_client.model_set(channel, "%s/%s" % (provider_id, model)),
                       MAYBE_SAVED)
    after = _pair(got.get("current") if isinstance(got, dict) else None)
    if not ok:
        if got.get("ok") is False and after == before:
            raise _invalid("这只 bot 现在换不到这个模型：这家供应商在它那里可能还没配密钥，"
                           "或者它的模型表里暂时没有这个模型。设置没有变")
        raise RuntimeError("unexpected gateway reply")
    return before, after


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
    _require_bot(bot_id)
    _require_enabled(bot_id)
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
