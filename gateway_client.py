# -*- coding: utf-8 -*-
"""Python 周边给新网关（dsh-bot）发系统消息：主动消息、朋友圈通知、电话回顾。

新系统的 bot：频道目录旁边的 state/ 里有网关写的 api.key（口令）和 api.port（端口）。
旧系统的 bot 没有这两个文件，调用方照旧写 inbox。

旧系统发给 bot 的通知里写的是 Bash 命令（让 worker 跑脚本），新系统的模型没有 Bash，
for_dsh() 把这些命令换成对应工具的用法。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request


# 管理台到网关一律走本机回环（127.0.0.1）。这台机器常驻 http_proxy（Clash TUN），
# urllib 默认会把本机请求也交给代理，代理接走回 502 空正文，页面一路显示「网关没响应」。
# 自己造一个不带代理的 opener：传了 ProxyHandler({}) 覆盖掉默认那个读环境变量的
# （管理台侧一条 U43 验收钉的就是这件事）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


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


def model_info(channel_dir: str) -> dict:
    """现在用的模型、配置文件里的模型、能换哪些（管理台用）"""
    code, r = _call(channel_dir, "GET", "/v1/model", timeout=70)
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


def provider_refresh(channel_dir: str, name: str) -> tuple[int, dict]:
    """刷新一个自建供应商的模型列表（管理台用）。

    原样把网关的状态码与正文带回去（网关按 INTERFACE 3.8 返回 200/400/404/409/502/503）；
    连不上网关（URLError）或超时（TimeoutError）由调用方接住。
    """
    return _call(channel_dir, "POST", "/v1/provider/refresh", {"name": name},
                 timeout=PROVIDER_REFRESH_TIMEOUT)


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


def effort_get(channel_dir: str) -> tuple[int, dict]:
    """问一个 bot 当前的思考强度档位与可选档位（GET /v1/effort）。"""
    return _call(channel_dir, "GET", "/v1/effort", timeout=EFFORT_TIMEOUT)


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
