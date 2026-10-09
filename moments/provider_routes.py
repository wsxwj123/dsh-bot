"""provider 管理面板：蓝图 + R2–R10 接口（INTERFACE §2、§5、§6）。

HTTP 出口一律用脱敏形状；明文 api_key 只在 provider_config 内部与 set_bot_provider 入参出现。

**old-provider 面板**：与新版 ``/hub/provider``（hub_routes）并存，URL 各自独立；
本文件从旧仓 moments/provider_routes.py 原样搬入，行为与 URL 不变。
"""
import http.client
import json
import os
import socket
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import provider_config as pc  # noqa: E402
from flask import Blueprint, jsonify, render_template, request  # noqa: E402
from scripts.restart_bot_worker import restart_bot_worker, settle  # noqa: E402

provider_bp = Blueprint("provider", __name__)


@provider_bp.before_request
def _reset_proxy_generation():
    import provider_proxy
    provider_proxy.take_request_generation()   # 线程复用：清掉上一个请求的残留
    pc.reset_request_warn_state()              # 同上：清残留 warning / 未验证标记（E-15）


@provider_bp.after_request
def _proxy_generation_header(resp):
    """9.2 表：本请求分配了代号（非纯 no-op）且不是 4xx 才带 X-Proxy-Generation。
    安全闸 500 发生在写入段之前，天然没有代号；自检失败的 500 带头。"""
    import provider_proxy
    g = provider_proxy.take_request_generation()
    if g is not None and not 400 <= resp.status_code < 500:
        resp.headers["X-Proxy-Generation"] = str(g)
    return resp


@provider_bp.after_request
def _security_headers(resp):
    """安全响应头（旧仓是 app 级同名头，本仓收窄为本蓝图，不动 /hub 的既有响应头）。

    本页经隧道公网可达，是 iframe 点击劫持的目标 —— SameSite=Lax 只拦跨站**请求**，
    iframe 里发出的算同站请求、cookie 照发，于是「重置 / 重启 / 删 provider / 同步全部」
    这几个 POST 能被借受害者的手点出来。只加 frame 相关两条指令：模板是 Jinja + 内联
    <script>，default-src 之类会把内联脚本一起挡掉、页面直接坏掉。
    """
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    return resp

_MODELS_TIMEOUT = 8
_MAX_BODY = 1024 * 1024


def _bot_ids() -> set:
    import legacy_config_loader as config_loader
    return {b["bot_id"] for b in config_loader.list_discovered_bots()}


def _stopped(bot_id: str) -> bool:
    import bot_stop
    return bot_stop.is_stopped(bot_id)


def _json_body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def _err(msg, code=400, **extra):
    body = {"error": msg}
    body.update(extra)
    return jsonify(body), code


def _with_warning(payload: dict) -> dict:
    """E-15（审计条目修法二）：保存响应若有「地址未验证」warning 就带上（不拦截、不改 ok 语义）。

    条件性带键：正常路径响应形状与既有一字不差。
    """
    w = pc.take_request_warning()
    if w:
        payload["warning"] = w
        payload.setdefault("ok", True)
    return payload


def _bind_warning(entry: dict) -> str | None:
    """绑定时把 provider 条目上的「openai 地址未验证」标记转成面板可显示的 warning。

    标记由保存/开代理时的地址探针写入（探针 404）；显式覆盖地址或探针非 404 时被清除。
    """
    if entry.get("proxy") is True and entry.get("openai_addr_unverified") is True:
        return pc.WARN_OPENAI_UNVERIFIED
    return None


def _local_conflict_response(bot_id: str):
    """local 层命中 provider 键 → 结构化 409；未命中 → **None**（作前置守卫用）。

    ``keys`` 直接取自 ``local_provider_keys()``（数据驱动），**不解析** provider_config
    返回的文案——文案一改就会静默降级成 400、连 UI 要用的键名列表一起丢。
    """
    keys = pc.local_provider_keys(bot_id)
    if not keys:
        return None
    msg = ("%s 的 settings.local.json 里存在 provider 键（local 层优先于项目层），"
           "会覆盖面板写入，请先手工清理" % bot_id)
    return jsonify({"error": msg, "code": "local_conflict", "keys": keys}), 409


# ── R1 页面 ────────────────────────────────────────────────────────────────

@provider_bp.route("/provider")
def provider_page():
    return render_template("provider.html")


# ── R2 概览 ────────────────────────────────────────────────────────────────

@provider_bp.route("/api/providers", methods=["GET"])
def api_providers():
    try:
        data = pc.load_providers()
    except pc.ProvidersCorrupt:
        return _err("providers.json 解析失败", 500)

    providers = [pc._public_entry(entry) for entry in data.get("providers", [])]

    bindings = {}
    for bot_id in (data.get("bindings") or {}):
        bindings[bot_id] = pc.binding_status(bot_id)["binding"]

    glob_env = pc._read_env(pc.GLOBAL_SETTINGS_PATH)
    return jsonify({
        "providers": providers,
        "bindings": bindings,
        "bots": pc.list_bots_status(),
        "global": {
            "base_url": glob_env.get("ANTHROPIC_BASE_URL"),
            "model": glob_env.get("ANTHROPIC_MODEL"),
            "has_key": bool(glob_env.get("ANTHROPIC_AUTH_TOKEN") or glob_env.get("ANTHROPIC_API_KEY")),
        },
        "pending": [{"bot_id": e["bot_id"], "pending_since": e["pending_since"],
                     "fail_count": e["fail_count"], "next_retry_at": e["next_retry_at"],
                     "halted": e["halted"], "last_error": e["last_error"]}
                    for e in pc.list_pending()],
    })


# ── 朋友圈模型（全部 bot 共用）：aux_llm ───────────────────────────────────

def _aux_err(msg, code):
    """aux 接口的错误体（INTERFACE-aux-llm-provider §9）：``{ok:false,error}`` + 4xx。"""
    return jsonify({"ok": False, "error": msg}), code


def _aux_models_of(data: dict, provider_id: str) -> list:
    entry = next((p for p in (data.get("providers") or [])
                  if isinstance(p, dict) and p.get("id") == provider_id), None)
    return pc._model_ids(entry) if entry else []


def _read_aux_config() -> tuple:
    """现读**旧仓** ``_global.yml`` 的 aux_llm，返回 ``(provider_id, model)``；缺块/类型非法归一为空串。

    旧仓这个读取口在 ``claude_cli.read_aux_config()``；本仓 ``claude_cli.py`` 是新系统那份、
    没有 aux 口，而这里必须读旧仓那份全局配置（legacy_config_loader 的默认路径），
    故就地实现同款语义（缺块/类型非法一律空串，见旧仓 claude_cli.py:134）。
    """
    try:
        import legacy_config_loader as config_loader
        g = config_loader.load_global()
    except Exception:
        return "", ""
    blk = g.get("aux_llm") if isinstance(g, dict) else None
    if not isinstance(blk, dict):
        return "", ""
    pid = blk.get("provider_id")
    model = blk.get("model")
    return (pid if isinstance(pid, str) else ""), (model if isinstance(model, str) else "")


@provider_bp.route("/api/aux-llm", methods=["GET"])
def api_aux_llm_get():
    """回显当前 aux_llm 设置 + 供应商清单（脱敏，无密钥）+ 当前供应商的模型列表。"""
    try:
        data = pc.load_providers()
    except pc.ProvidersCorrupt:
        return _aux_err("providers.json 解析失败", 500)
    pid, model = _read_aux_config()
    providers = [{"id": p.get("id"), "name": p.get("name")}
                 for p in (data.get("providers") or []) if isinstance(p, dict)]
    return jsonify({
        "provider_id": pid,
        "model": model,
        "providers": providers,
        "models": _aux_models_of(data, pid),
    })


@provider_bp.route("/api/aux-llm", methods=["POST"])
def api_aux_llm_set():
    """保存 aux_llm；``provider_id=""`` 恢复默认；供应商/模型不存在 → 4xx。"""
    body = _json_body()
    if body is None:
        return _aux_err("请求体必须是 JSON 对象", 400)
    pid = body.get("provider_id")
    pid = "" if pid is None else pid
    if not isinstance(pid, str):
        return _aux_err("provider_id 必须是字符串", 400)
    model = body.get("model")
    model = "" if model is None else model
    if not isinstance(model, str):
        return _aux_err("model 必须是字符串", 400)

    import legacy_config_loader as config_loader
    if pid == "":
        config_loader.set_global_aux_llm("", "")
        return jsonify({"ok": True})
    try:
        data = pc.load_providers()
    except pc.ProvidersCorrupt:
        return _aux_err("providers.json 解析失败", 500)
    entry = next((p for p in (data.get("providers") or [])
                  if isinstance(p, dict) and p.get("id") == pid), None)
    if entry is None:
        return _aux_err("供应商不存在", 404)
    if model not in pc._model_ids(entry):
        return _aux_err("模型不存在", 400)
    config_loader.set_global_aux_llm(pid, model)
    return jsonify({"ok": True})


# ── R3 / R4 / R5 provider 增删改 ───────────────────────────────────────────

@provider_bp.route("/api/providers", methods=["POST"])
def api_create_provider():
    body = _json_body()
    if body is None:
        return _err("请求体必须是 JSON 对象")
    entry, err = pc.upsert_provider(body)
    if err:
        return _err(err)
    return jsonify(_with_warning({"provider": entry})), 201


@provider_bp.route("/api/providers/<provider_id>", methods=["PUT"])
def api_update_provider(provider_id):
    body = _json_body()
    if body is None:
        return _err("请求体必须是 JSON 对象")
    try:
        entry, err = pc.upsert_provider(body, provider_id)
    except pc.ProviderConflict as e:   # 9.6：并发改动 / 统一绑定守卫，零写入
        out = {"error": str(e)}
        if e.bound_bots is not None:
            out["bound_bots"] = e.bound_bots
        return jsonify(out), 409
    if err:
        return _err(err, 404 if err == "provider not found" else 400)
    return jsonify(_with_warning({"provider": entry}))


@provider_bp.route("/api/providers/<provider_id>", methods=["DELETE"])
def api_delete_provider(provider_id):
    ok, err = pc.delete_provider(provider_id)   # delete_provider 内部已触发重建（§2.1）
    if not ok:
        if err == "provider not found":
            return _err(err, 404)
        return jsonify({"error": err, "bound_bots": pc.bound_bots(provider_id)}), 409
    return jsonify({"ok": True, "id": provider_id})


# ── R6 拉取模型 ────────────────────────────────────────────────────────────

def _bound_conflict(e):
    """统一绑定守卫 409 的响应形状（9.7.1，与 PUT 一致）：``{"error":…[,"bound_bots":[…]]}``。"""
    out = {"error": str(e)}
    if e.bound_bots is not None:
        out["bound_bots"] = e.bound_bots
    return jsonify(out), 409


def _r6_name_collision(entry: dict, models: list, endpoints) -> bool:
    """按拉到的模型算 N_p，与其它 proxy=true provider 的注册名集合求交；只读不写、不发探针（review #2）。"""
    mine = pc._registered_names(dict(entry, models=models))   # 不跑判定链：避免每个未判定模型被探针两次
    try:
        others = pc.load_providers().get("providers") or []
    except pc.ProvidersCorrupt:
        return False
    return any(isinstance(o, dict) and o.get("proxy") is True and o.get("id") != entry.get("id")
               and mine & pc._registered_names(o) for o in others)


@provider_bp.route("/api/providers/models", methods=["POST"])
def api_list_models():
    body = _json_body()
    if body is None:
        return _err("请求体必须是 JSON 对象")

    provider_id = body.get("provider_id")
    has_pair = ("base_url" in body) or ("api_key" in body)
    if provider_id and has_pair:
        return _err("provider_id 与 base_url/api_key 只能给一组")

    if provider_id:
        try:
            data = pc.load_providers()
        except pc.ProvidersCorrupt:
            return _err("providers.json 解析失败", 500)
        entry = next((p for p in data.get("providers", []) if p.get("id") == provider_id), None)
        if entry is None:
            return _err("provider not found", 404)
        api_key = entry.get("api_key")
        if not api_key:
            return _err("该 provider 未存 api_key")
        base_url = entry.get("base_url")
        # 显式 OpenAI 侧地址同样要过 http/https 校验，不合格视为未设（回退会把同一把 key 发过去）
        openai_base_url = entry.get("openai_base_url")
        if not pc._valid_base_url(openai_base_url):
            openai_base_url = ""
    else:
        openai_base_url = ""
        base_url = body.get("base_url")
        api_key = body.get("api_key")
        # base_url 判据与 R3/R4 统一到 provider_config._valid_base_url（单一来源）
        if not pc._valid_base_url(base_url):
            return _err("base_url 必须是 http/https 开头的地址")
        if not api_key:
            return _err("api_key required")

    models, error = _fetch_models(base_url, api_key, openai_base_url)
    if error:
        # 失败时响应只含三个既有键（INTERFACE-provider-protocol-level §2.4：删除顶层 protocols 空壳）。
        return jsonify({"ok": False, "models": [], "error": error})

    # E-25.3「看一眼不改配置」：带 provider_id 的**只读查询**（query_only=true，面板点开
    # 模型框走的就是它）只拉取并回 candidates —— 不覆盖 provider 的模型清单、不重建代理
    # 配置、也不跑写前守卫（守卫是写给"即将发生的写"的）。缺省（无此键）= 显式拉取，
    # 行为与此前一字不差：守卫 → 写回 → 重建（F4）。
    if provider_id and models and body.get("query_only") is not True:
        # 9.7.1 统一绑定守卫：写回 models **之前**判定（proxy=true 时），命中即 409 零写入。
        # 复用 provider_config._binding_guard。判定链已随模型级协议一并移除，此处不再跑探针。
        try:
            pc._binding_guard(provider_id, dict(entry), dict(entry, models=models), data)
        except pc.ProviderConflict as e:
            return _bound_conflict(e)
        if entry.get("proxy") is True and _r6_name_collision(entry, models, None):
            # 9.7（F 组）：拉到的模型使 N_p 与另一 proxy provider 相交 → 零写入、不重建
            return jsonify({"ok": False, "models": [],
                            "error": "客户端名或裸名在多个代理 provider 间重复，请先关闭其中一个"})
        pc.store_models(provider_id, models)   # 读改写在 provider_config 锁内完成
        # R6 成功即触发一次配置重建（F4）：拉到的新模型立即在实例里可路由。
        snap = _proxy_snapshot()
        if snap is not None:
            pc._sync_after_write(snap)
    return jsonify({"ok": True, "models": models, "error": None})


def _models_url(base_url: str) -> str:
    """``<base_url>`` + ``/v1/models``（已以 ``/v1`` 结尾则只加 ``/models``）。

    **先剥离 base_url 的 fragment 与 query**：原样拼接时 ``?``/``#`` 会把后缀吞掉——
    ``http://127.0.0.1:6379/x#`` 实际发出的是 ``GET /x``，请求路径完全由调用方控制
    （绕过"只访问用户填的 provider"这一层的路径约束）。
    """
    b = base_url.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    return b + "/models" if b.endswith("/v1") else b + "/v1/models"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None  # 3xx 视为失败（不跟随重定向）


def _fetch_models(base_url: str, api_key: str, openai_base_url: str = ""):
    """拉上游 ``/v1/models``。返回 ``(models, error)``。

    首次得到 404 且 OpenAI 侧地址（``effective_endpoints``）与 base_url 不同时，再用它回退请求一次
    （如 ``…/anthropic`` 下没有模型列表、根路径下才有）；其他错误不回退。两次都失败则拼接两次文案。
    """
    models, error = _fetch_models_once(base_url, api_key)
    if error != _status_error(404):
        return models, error
    from scripts.derive_endpoints import effective_endpoints
    openai_url = effective_endpoints(base_url, "", openai_base_url)[1]
    if openai_url == base_url.rstrip("/"):
        return models, error
    models2, error2 = _fetch_models_once(openai_url, api_key)
    if error2:
        return None, "%s；改用 OpenAI 端点也失败：%s" % (error, error2)
    return models2, None


def _fetch_models_once(base_url: str, api_key: str):
    """向 ``base_url`` 发一次 ``/v1/models`` 请求并解析。返回 ``(models, error)``。

    ``models`` 条目**对外恒为 ``{id, label}`` 两键**（INTERFACE §5-3 全等断言）。
    """
    # SSRF 面收敛：本路由是整个面板唯一的新增外联面，唯一访问控制是登录门（/api/* 未登录 401）。
    # 手段：只允许 http/https、拒 userinfo、不跟随重定向（3xx 判失败）、8 秒超时、响应 ≤1MiB、
    # 错误只回状态码类别（不回上游 body）。**不做内网 IP 黑名单** —— 本机/LAN 上的 provider
    # 是合法用法（NO_PROXY=127.0.0.1,localhost 是既有事实），黑名单会把正常用法一起砍掉。
    # 同时禁用环境代理（ProxyHandler({})），避免请求被 http_proxy 静默劫持到第三方。
    url = _models_url(base_url)
    from scripts import proxy_upstream   # 与探针同一浏览器式请求头基底（9.7，I 组）
    req = urllib.request.Request(url, method="GET", headers=proxy_upstream.browser_headers({
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Authorization": "Bearer %s" % api_key,
    }))
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=_MODELS_TIMEOUT) as resp:
            raw = resp.read(_MAX_BODY + 1)
    except urllib.error.HTTPError as e:
        return (None, _status_error(e.code))
    except (socket.timeout, TimeoutError):
        return (None, "超时（8 秒）")
    except urllib.error.URLError as e:
        msg = ("超时（8 秒）"
               if isinstance(getattr(e, "reason", None), (socket.timeout, TimeoutError))
               else "连接失败: %s" % type(getattr(e, "reason", e)).__name__)
        return (None, msg)
    except (http.client.HTTPException, OSError) as e:
        msg = "连接失败: %s" % type(e).__name__
        return (None, msg)
    if len(raw) > _MAX_BODY:
        return (None, "响应过大")
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return (None, "上游响应不是 JSON")
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return (None, "上游响应不是 JSON")
    out, seen = [], set()
    for it in items:
        if not isinstance(it, dict):
            continue
        mid = it.get("id")
        if not isinstance(mid, str) or not mid or mid in seen:
            continue
        seen.add(mid)
        label = it.get("display_name") or mid
        out.append({"id": mid, "label": label})
        if len(out) >= 500:
            break
    return out, None



def _status_error(code: int) -> str:
    if code == 401:
        return "上游返回 401（key 无效或无权限）"
    if code == 403:
        return "上游返回 403（无权限，或上游 CDN 拦截）"
    if code == 404:
        return "上游返回 404（该地址没有 /v1/models 端点）"
    return "上游返回 %s" % code



def _restart_and_finalize(bot_id: str, ok_payload: dict, queued_payload: dict,
                          previous_binding, settings_backup: str | None):
    res = restart_bot_worker(bot_id, idle_wait_sec=20, ready_wait_sec=20)
    status = res["status"]
    if status in ("ok", "queued", "spawn_failed", "unrestartable"):
        settle(bot_id, res)  # 唯一记账入口（I-3）；R8 已删 binding 时全是无操作
    if status == "ok":
        ok_payload["effective"] = pc.effective_provider(bot_id)
        ok_payload["restart"] = res
        return jsonify(ok_payload), 200
    if status in ("queued", "spawn_failed", "unrestartable"):
        queued_payload["restart"] = res
        queued_payload["effective"] = pc.effective_provider(bot_id)
        if status == "unrestartable":  # 写盘已成功但自动重启已停止：不再谎称已排队，标明原因
            queued_payload["queued"] = False
            queued_payload["halted"] = True
            queued_payload["halt_reason"] = res.get("detail")
        return jsonify(queued_payload), 202
    if status == "stopped":
        _rollback(bot_id, previous_binding, settings_backup)
        return jsonify({"error": "bot 已停用", "code": "stopped"}), 409
    return _err("unknown bot %s" % bot_id, 400)


def _rollback(bot_id: str, previous_binding, settings_backup: str | None) -> None:
    pc.rollback_binding(bot_id, previous_binding)
    path = pc.bot_settings_path(bot_id)
    if settings_backup and path and os.path.exists(settings_backup):
        try:
            os.replace(settings_backup, path)
        except OSError:
            pass


def _sync_proxy_after_binding():
    """绑定/重置成功后的配置重建副作用（§2.1）；返回 ``provider_proxy.sync`` 的结果（读不出清单 → None）。

    **no-op 由 ``provider_proxy.sync`` 自己保证**（无 proxy provider 时不写盘、不探测、
    不 kickstart、不创建运行目录），故无条件调用安全。E-17：把结果带回响应，
    由调用方（绑定走代理的 provider）据实报失败；读不出清单时无条件调用不安全，返回 None。
    """
    try:
        snap = pc.load_providers()
    except pc.ProvidersCorrupt:
        return None
    return pc._sync_after_write(snap)


@provider_bp.route("/api/bots/<bot_id>/provider", methods=["POST"])
def api_bind_bot(bot_id):
    # 多错误命中顺序（Y2-6）：unknown bot → stopped → provider not found → 字段校验
    if bot_id not in _bot_ids():
        return _err("unknown bot %s" % bot_id, 404)
    if _stopped(bot_id):
        return jsonify({"error": "bot 已停用", "code": "stopped"}), 409

    body = _json_body()
    if body is None:
        return _err("请求体必须是 JSON 对象")

    provider_id = body.get("provider_id")
    model = body.get("model")
    try:
        data = pc.load_providers()
    except pc.ProvidersCorrupt:
        return _err("providers.json 解析失败", 500)
    entry = next((p for p in data.get("providers", []) if p.get("id") == provider_id), None)
    if entry is None:
        return _err("provider not found", 404)
    if not isinstance(model, str) or not (1 <= len(model) <= 200):
        return _err("model 必须是长度 1..200 的字符串")
    # §2.1 末：proxy=true 时客户端名为 <pid>/<model>，绑定前校验拼接后长度（不截断、不改名）。
    # proxy=false 时行为与今天完全一致（只校验 model 本身 1..200）。
    if entry.get("proxy") is True and len(provider_id) + 1 + len(model) > 200:
        return _err("model 拼接 provider 前缀后超过 200 字符")

    resp = _local_conflict_response(bot_id)
    if resp:
        return resp

    previous_binding = pc.get_binding_raw(bot_id)
    result, err = pc.set_bot_provider(bot_id, entry, model)
    if err:
        # 纵深防御：set 内部的 local 复核在前置检查之后才命中（竞态）时也走 409 分支
        resp = _local_conflict_response(bot_id)
        if resp:
            return resp
        return _err(err, 400)

    sync_result = _sync_proxy_after_binding()   # §2.1 绑定成功副作用：触发配置重建
    if (entry.get("proxy") is True and isinstance(sync_result, dict)
            and sync_result.get("ok") is False):
        # E-17：绑定走代理的 provider，代理同步失败 → 如实报失败（不重启、不报 applied）。
        # 写入本身已成功、pending_restart 已登记（收敛器后续重试）；但实例里还没有该 provider，
        # 报成功会让用户以为切换完成。
        return jsonify({"ok": False, "applied": False, "queued": False, "bot_id": bot_id,
                        "error": sync_result.get("error") or "代理配置同步失败",
                        "restart": {}}), 200
    warning = _bind_warning(entry)   # E-15：该 provider 的 openai 地址未验证 → 绑定响应带提示
    ok_payload = {"ok": True, "applied": True, "queued": False, "bot_id": bot_id,
                  "env_written": result["env_written"]}
    queued_payload = {"ok": True, "applied": False, "queued": True, "bot_id": bot_id}
    if warning:
        ok_payload["warning"] = warning
        queued_payload["warning"] = warning
    return _restart_and_finalize(bot_id, ok_payload, queued_payload,
                                 previous_binding, result.get("backup_path"))


@provider_bp.route("/api/bots/<bot_id>/provider/reset", methods=["POST"])
def api_reset_bot(bot_id):
    if bot_id not in _bot_ids():
        return _err("unknown bot %s" % bot_id, 404)
    if _stopped(bot_id):
        return jsonify({"error": "bot 已停用", "code": "stopped"}), 409
    resp = _local_conflict_response(bot_id)
    if resp:
        return resp

    previous_binding = pc.get_binding_raw(bot_id)
    ok, err = pc.clear_bot_provider(bot_id)
    if not ok:
        resp = _local_conflict_response(bot_id)
        if resp:
            return resp
        return _err(err, 400)

    _sync_proxy_after_binding()   # §2.1 副作用：重置成功后同样触发配置重建
    return _restart_and_finalize(
        bot_id,
        {"ok": True, "applied": True, "queued": False, "bot_id": bot_id, "env_removed": list(pc.ALLOWED_ENV_KEYS)},
        {"ok": True, "applied": False, "queued": True, "bot_id": bot_id},
        previous_binding, None)


# ── R9 手动重启 ────────────────────────────────────────────────────────────

@provider_bp.route("/api/bots/<bot_id>/restart", methods=["POST"])
def api_restart_bot(bot_id):
    if bot_id not in _bot_ids():
        return jsonify({"error": "unknown bot %s" % bot_id, "code": "unknown_bot"}), 404
    if _stopped(bot_id):
        return jsonify({"error": "bot 已停用", "code": "stopped"}), 409
    body = request.get_json(silent=True)
    force = bool(body.get("force")) if isinstance(body, dict) else False
    # 待重启且已停止的项：手动重启先解除停止（M4）；结果交 settle 记账（不在待重启的项全是无操作）
    binding = pc.get_binding_raw(bot_id) or {}
    if binding.get("pending_restart") and binding.get("halted") is True:
        pc.resume_restart(bot_id)
    res = restart_bot_worker(bot_id, force=force, idle_wait_sec=20, ready_wait_sec=20)
    settle(bot_id, res)
    # E-24（裁定：以旧锁定契约为准）：状态码恒 200，成败由前端读 body.ok 判定并显示失败原因。
    return jsonify({"ok": res["status"] == "ok", "restart": res})


# ── R10 一键重下发 ─────────────────────────────────────────────────────────

@provider_bp.route("/api/providers/sync_all", methods=["POST"])
def api_sync_all():
    try:
        data = pc.load_providers()
    except pc.ProvidersCorrupt:
        return _err("providers.json 解析失败", 500)
    pc.ensure_instance_key()   # R6 汇口第 0 步：2xx 写请求后清单必有 instance_key

    results = []
    for bot_id, binding in (data.get("bindings") or {}).items():
        entry = next((p for p in data.get("providers", [])
                      if p.get("id") == (binding or {}).get("provider_id")), None)
        if entry is None:
            results.append({"bot_id": bot_id, "ok": False, "key_masked": "",
                            "in_sync": False, "restart": {},
                            "error": "provider not found"})
            continue
        model = binding.get("model")
        if bot_id not in _bot_ids() or _stopped(bot_id):
            results.append({"bot_id": bot_id, "ok": False, "key_masked": "",
                            "in_sync": False, "restart": {}, "error": "bot 不可用"})
            continue
        result, err = pc.set_bot_provider(bot_id, entry, model)
        if err:
            results.append({"bot_id": bot_id, "ok": False, "key_masked": "",
                            "in_sync": False, "restart": {}, "error": err})
            continue
        results.append({
            "bot_id": bot_id, "ok": True,
            "key_masked": pc._mask(entry.get("api_key") or ""),
            # 9.9：写入后用与 effective 同一判据实算（本地层覆盖等情形为 false），不写死
            "in_sync": bool(pc.effective_provider(bot_id).get("in_sync")), "restart": {},
        })
    return jsonify({"ok": True, "queued": len(results), "results": results}), 202


# ── GET /api/proxy、POST /api/proxy/sync ──

@provider_bp.record_once
def _disable_json_sort(state):
    """关掉本 app 的 JSON 键排序（INTERFACE §1.1 要求 providers[] 条目**恰好 14 键且顺序即此**）。

    Flask 3.x 默认 ``app.json.sort_keys = True``，会把键按字典序重排（``id`` 跑到 ``name`` 后面），
    与契约顺序不符。本蓝图注册时统一关掉——比逐个响应手工拼串干净，也覆盖了所有既有路由。
    **不改 moments/web.py**（PLAN §3.2 明确不改），用蓝图的 ``record_once`` 在注册期拿到 app。
    """
    state.app.json.sort_keys = False


def _proxy_snapshot():
    """取一份 providers.json 快照给 sync（读失败 → None）。"""
    try:
        return pc.load_providers()
    except pc.ProvidersCorrupt:
        return None


@provider_bp.route("/api/proxy/restart", methods=["POST"])
def api_proxy_restart():
    import provider_proxy
    status, body = provider_proxy.restart(ensure_key=pc.ensure_instance_key)   # 忽略请求体（9.15）
    return jsonify(body), status


@provider_bp.route("/api/proxy/generation", methods=["GET"])
def api_proxy_generation():
    import provider_proxy
    return jsonify(provider_proxy.generation_info())   # 恒 200（9.15）


@provider_bp.route("/api/proxy", methods=["GET"])
def api_proxy_status():
    import provider_proxy
    return jsonify(provider_proxy.proxy_status(_proxy_snapshot()))


@provider_bp.route("/api/proxy/sync", methods=["POST"])
def api_proxy_sync():
    import provider_proxy
    body = request.get_json(silent=True)
    # 请求体必须是对象或省略（INTERFACE §2.5）。
    if body is not None and not isinstance(body, dict):
        return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
    pc.ensure_instance_key()   # R6 汇口第 0 步（先于取快照：sync 要用 key 生成 config）
    snap = _proxy_snapshot()
    result = provider_proxy.sync(snap, explicit=True)   # 显式 sync 等核验结果（截止 3T+11.2 s）
    if not result.get("ok"):
        return jsonify({"ok": False, "error": result.get("error")}), 500
    if result.get("reloaded"):
        return jsonify({"ok": True, "reloaded": True,
                        "providers_count": result.get("providers_count", 0),
                        "models_count": result.get("models_count", 0)}), 200
    if result.get("last_error"):
        # 写入成功但核验失败（已 kickstart 仍未生效）→ 202；空清单重写的核验失败同样 202（9.4）。
        return jsonify({"ok": True, "reloaded": False,
                        "last_error": result.get("last_error"),
                        "providers_count": result.get("providers_count", 0),
                        "models_count": result.get("models_count", 0)}), 202
    # no-op（无任何 proxy=true provider）：200 + 全零（E10）。
    return jsonify({"ok": True, "reloaded": False, "providers_count": 0,
                    "models_count": 0, "last_error": None}), 200


