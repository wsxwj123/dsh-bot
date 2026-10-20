# -*- coding: utf-8 -*-
"""白盒单测：daily_wildcards._normalize_cards 解析接口规定的标准卡片结构。

验收用例的假 LLM 只用 text/content/title 等别名字段，标准结构
``[{"card": "...", "emotion": "..."}]`` 没有覆盖，这里单独补上。
纯函数测试，不连外网、不写库。
"""
import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "daily_wildcards.py"


def _load(monkeypatch, tmp_path):
    # 脚本在导入时检查 BOTLIFE_STATE_DB，未设置会直接退出，因此先钉到临时路径。
    monkeypatch.setenv("BOTLIFE_STATE_DB", str(tmp_path / "state.db"))
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    spec = importlib.util.spec_from_file_location("daily_wildcards_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_normalize_cards_标准结构(monkeypatch, tmp_path):
    module = _load(monkeypatch, tmp_path)
    raw = [
        {"card": "今天想去书店坐一会儿。", "emotion": "平静"},
        {"card": "想给很久没联系的朋友打个电话。", "emotion": "怀念"},
    ]
    assert module._normalize_cards(raw) == raw
