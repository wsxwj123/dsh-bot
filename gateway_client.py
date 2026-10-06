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
        with urllib.request.urlopen(req, timeout=timeout) as r:
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
    with urllib.request.urlopen(req, timeout=timeout) as r:
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
