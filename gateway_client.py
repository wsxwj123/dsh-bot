# -*- coding: utf-8 -*-
"""Python 周边给新网关（dsh-bot）发系统消息：主动消息、朋友圈通知、电话回顾。

新系统的 bot：频道目录旁边的 state/ 里有网关写的 api.key（口令）和 api.port（端口）。
旧系统的 bot 没有这两个文件，调用方照旧写 inbox。

旧系统发给 bot 的通知里写的是 Bash 命令（让 worker 跑脚本），新系统的模型没有 Bash，
for_dsh() 把这些命令换成对应工具的用法。
"""
from __future__ import annotations

import http.client
import json
import os
import re
import socket
import urllib.error
import urllib.request


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """跳转一律不跟：3xx 原样当 HTTPError 交给调用方（``_call`` 里就是带状态码的返回）。

    默认的跳转处理会把请求头连同网关口令原样带到 Location 指的地方。端口被别的本机进程占了、
    api.port 是残留文件时，对方回一个跳转就能把口令引到任意主机。真网关从不回跳转。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# 管理台到网关一律走本机回环（127.0.0.1）。这台机器常驻 http_proxy（Clash TUN），
# urllib 默认会把本机请求也交给代理，代理接走回 502 空正文，页面一路显示「网关没响应」。
# 自己造一个不带代理的 opener：传了 ProxyHandler({}) 覆盖掉默认那个读环境变量的
# （管理台侧一条 U43 验收钉的就是这件事）。也不跟跳转（安审 S1，见 _NoRedirect）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _state_dir(channel_dir: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(os.path.expanduser(channel_dir))), "state")


def available(channel_dir: str | None) -> bool:
    """这个频道目录的 bot 由新网关在跑（能投递）"""
    if not channel_dir:
        return False
    st = _state_dir(channel_dir)
    return os.path.isfile(os.path.join(st, "api.key")) and os.path.isfile(os.path.join(st, "api.port"))


def _call(channel_dir: str, method: str, path: str, body: dict | None = None, timeout: float = 10) -> tuple[int, dict]:
    st = _state_dir(channel_dir)
    token = open(os.path.join(st, "api.key"), encoding="utf-8").read().strip()
    port = int(open(os.path.join(st, "api.port"), encoding="utf-8").read().strip())
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                 headers={"content-type": "application/json", "authorization": f"Bearer {token}"})
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except ValueError:
            return e.code, {}


# 请求"发出去之后"才可能出现的异常。这些都可能已经写进去了，调用方一律不自动重试。
_SENT_ERRORS = (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError)


def net_kind(err) -> str | None:
    """调网关时抛出的异常归类（管理台与适配服务共用这一份判据）。

    - ``unreachable``：请求根本没发出去（拒连、DNS 解析不了）。只有这种才算"这个网关不可用"，
      也才允许换下一个网关；
    - ``broken``：发出后中断或超时。可能已经写进去了，换网关重试就是第二次写；
    - ``None``：不是网络上的事（本地错误），由调用方自己处理。
    """
    if isinstance(err, urllib.error.URLError) and isinstance(
            err.reason, (ConnectionRefusedError, socket.gaierror)):
        return "unreachable"
    if isinstance(err, _SENT_ERRORS):
        return "broken"
    return None


def model_info(channel_dir: str, timeout: float = 70) -> dict:
    """现在用的模型、配置文件里的模型、能换哪些（管理台用）。

    timeout 缺省 70 秒（网关冷启动要问一次模型目录）；适配服务的列表接口等不了那么久，自己传短的。
    """
    code, r = _call(channel_dir, "GET", "/v1/model", timeout=timeout)
    if code != 200:
        raise RuntimeError(r.get("error") or f"HTTP {code}")
    return r


def model_set(channel_dir: str, spec: str) -> tuple[bool, dict]:
    """换模型：spec 是模型名（同 /model 命令），default 换回配置文件里的"""
    code, r = _call(channel_dir, "POST", "/v1/model", {"spec": spec}, timeout=70)
    return code == 200 and bool(r.get("ok")), r


# 刷新的超时：INTERFACE 3.9 = gateway.model_fetch_timeout_ms×3 + 10 秒，默认 55 秒。
# 门户读不到各 bot 网关的配置，用默认值这台机器上就是对的。
PROVIDER_REFRESH_TIMEOUT = 55.0


def provider_refresh(channel_dir: str, name: str, only: list | None = None,
                     rev: str | None = None) -> tuple[int, dict]:
    """刷新一个自建供应商的模型列表（管理台用）。

    原样把网关的状态码与正文带回去（网关按 INTERFACE 3.8 返回 200/400/404/409/502/503）；
    连不上网关（URLError）或超时（TimeoutError）由调用方接住。

    `only` 给了就转发（勾选落盘，INTERFACE §11b）；`rev` 给了就转发（预览拿到的清单指纹，
    网关在保存那一刻核对，对不上回 409）。都缺省时请求体只有 `{"name"}`。
    这里没有 `full`：把清单整份重铺只能直接调网关的本机接口，管理台不提供这个口子。
    """
    body = {"name": name}
    if only is not None:
        body["only"] = only
    if rev is not None:
        body["rev"] = rev
    return _call(channel_dir, "POST", "/v1/provider/refresh", body,
                 timeout=PROVIDER_REFRESH_TIMEOUT)


def provider_preview(channel_dir: str, name: str) -> tuple[int, dict]:
    """只拉不写：预览一个自建供应商这次能拉到哪些模型（管理台用，INTERFACE §11a）。

    网关侧是只读端点（不写盘、不更新 lastRefresh、不占 refreshing 去重），超时口径
    与刷新一致 —— 它同样要真的去拉一遍上游目录。
    """
    return _call(channel_dir, "POST", "/v1/provider/preview", {"name": name},
                 timeout=PROVIDER_REFRESH_TIMEOUT)


# 测试连接的超时：契约 §13 = 网关单次请求 15 秒（modelFetchTimeoutMs）+ 10 秒余量。
PROVIDER_TEST_TIMEOUT = 25.0


def provider_test(channel_dir: str, name: str) -> tuple[int, dict]:
    """测一个自建供应商通不通（管理台用，INTERFACE §12a）：网关发一次最小对话请求。

    只读端点，超时 25 秒；连不上 / 超时由调用方按只读文案接住。
    """
    return _call(channel_dir, "POST", "/v1/provider/test", {"name": name},
                 timeout=PROVIDER_TEST_TIMEOUT)


# 写端点的超时：INTERFACE-管理台UI 2.2 = 55 秒（贴着网关最坏情况，45 秒拉模型列表加 10 秒等锁）。
# 超时与「发出后中断」在管理台侧一律判 504，不自动换网关重试（重试会变成第二次写）。
PROVIDER_WRITE_TIMEOUT = 55.0


def provider_detail(channel_dir: str) -> tuple[int, dict]:
    """自建供应商详情（GET /v1/provider，含地址与模型清单、不含密钥，INTERFACE 3.1）。

    读接口，超时用默认 10 秒；降级（详情缺省）由调用方决定。
    """
    return _call(channel_dir, "GET", "/v1/provider")


def provider_save(channel_dir: str, body: dict) -> tuple[int, dict]:
    """新建 / 更新一个自建供应商（POST /v1/provider/save）。body 原样转发，不改字段。"""
    return _call(channel_dir, "POST", "/v1/provider/save", body, timeout=PROVIDER_WRITE_TIMEOUT)


def provider_remove(channel_dir: str, name: str) -> tuple[int, dict]:
    """删除一个自建供应商（POST /v1/provider/remove）。"""
    return _call(channel_dir, "POST", "/v1/provider/remove", {"name": name},
                 timeout=PROVIDER_WRITE_TIMEOUT)


def provider_model(channel_dir: str, body: dict) -> tuple[int, dict]:
    """模型增删改（POST /v1/provider/model，body 含 name/action/id/contextWindow）。"""
    return _call(channel_dir, "POST", "/v1/provider/model", body, timeout=PROVIDER_WRITE_TIMEOUT)


# 思考强度读的超时：与写端点同值。网关侧要建 dsh 会话才问得出档位与可选档位，
# 冷启动最坏情况可能等 60 秒，用 _call 的默认 10 秒会在冷启动时把 504 报给页面。
EFFORT_TIMEOUT = 55.0


def effort_get(channel_dir: str, timeout: float = EFFORT_TIMEOUT) -> tuple[int, dict]:
    """问一个 bot 当前的思考强度档位与可选档位（GET /v1/effort）。timeout 缺省同上面那条说明。"""
    return _call(channel_dir, "GET", "/v1/effort", timeout=timeout)


def effort_set(channel_dir: str, effort: str) -> tuple[int, dict]:
    """改一个 bot 的思考强度档位（POST /v1/effort）。"""
    return _call(channel_dir, "POST", "/v1/effort", {"effort": effort},
                 timeout=PROVIDER_WRITE_TIMEOUT)


def inject(channel_dir: str, chat_id: str, text: str, source: str, key: str,
           port: int | None = None, bot_lines: list[str] | None = None, timeout: float = 10) -> bool:
    """写一条系统消息进网关的账本，模型在下一轮处理。key 相同的只算一次。
    bot_lines：bot 自己说过的话（比如电话里），网关从里面找许诺登记成承诺。"""
    st = _state_dir(channel_dir)
    token = open(os.path.join(st, "api.key"), encoding="utf-8").read().strip()
    if port is None:
        port = int(open(os.path.join(st, "api.port"), encoding="utf-8").read().strip())
    body = {"chat_id": str(chat_id), "source": source, "text": text, "key": key}
    if bot_lines:
        body["bot_lines"] = [str(x) for x in bot_lines][:200]
    req = urllib.request.Request(f"http://127.0.0.1:{int(port)}/v1/inject", data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"content-type": "application/json", "authorization": f"Bearer {token}"})
    with _OPENER.open(req, timeout=timeout) as r:      # 同上：本机回环不走代理
        return bool(json.loads(r.read().decode("utf-8")).get("ok"))


# ─── 旧通知里的 Bash 命令 → 新工具的用法 ───

_PY = r'\S*python[\w.]*'
_RULES = [
    (re.compile(_PY + r'\s+\S*moment_reply\.py\s+(\S+)\s+(\S+)\s+"<([^>"]*)>"(?:\s*\[--image <[^>]*>\])?'),
     lambda m: f'moments 工具：action=reply，moment_id={m.group(1)}，parent_comment_id={m.group(2)}，text=（{m.group(3)}），要配图就加 image=图片路径'),
    (re.compile(_PY + r'\s+\S*moment_like\.py\s+(\S+)'),
     lambda m: f'moments 工具：action=like，moment_id={m.group(1)}'),
    (re.compile(_PY + r'\s+\S*moment_delete_comment\.py\s+<?(\w+)>?'),
     lambda m: 'moments 工具：action=delete_comment，comment_id=（要删的评论编号）'),
    (re.compile(_PY + r'\s+\S*moment_set_image\.py\s+(\S+)\s+<[^\n]*'),
     lambda m: f'moments 工具：action=set_image，moment_id={m.group(1)}，images=[图片路径, …]'),
    (re.compile(r'用 Bash \*\*一次性\*\*'), lambda m: '用 moments 工具**一次性**'),
    (re.compile(r'用 Bash 工具'), lambda m: '用 moments 工具'),
    (re.compile(r'novelai-skill|comfyui-skill'), lambda m: 'generate_image 工具'),
]


def for_dsh(text: str) -> str:
    """把旧通知里让 worker 跑的脚本命令，换成新系统的工具用法。"""
    for rx, fn in _RULES:
        text = rx.sub(fn, text)
    return text
