# -*- coding: utf-8 -*-
"""provider_wizard 验收用的小工具（名字唯一，避免和其它目录的 conftest 撞名）。"""
import hashlib
from pathlib import Path

OWNER = 5550001
FRIEND = 5550002


def digest(path):
    """文件内容的摘要；不存在为 None。"""
    p = Path(path)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
