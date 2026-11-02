# -*- coding: utf-8 -*-
"""bots-chat-id：bots() 每行的 chat_id 只取 bot 自己 yml 顶层的值，正整数或纯 ASCII 数字串，其余为 None。"""
import pytest

from moments import adapter_service as svc

BIG = 10 ** 30


@pytest.mark.parametrize("value, want", [
    (123456789, 123456789),
    ("987654321", 987654321),
    ("0123456789", 123456789),      # 前导零：去零后的整数
    ("0", None),                    # 转成整数后为 0
    ("000", None),
    (0, None),
    (-123456789, None),
    (True, None),                   # 布尔是 int 的子类，不能当 1
    (False, None),
    (123456789.0, None),
    (" 123456789", None),
    ("123456789\n", None),
    ("+123456789", None),
    ("-123456789", None),
    ("", None),
    ("１２３", None),                # 全角数字，str.isdigit() 会放行
    ("١٢٣", None),                  # 阿拉伯-印度数字
    ("²", None),                    # 上标
    ([123456789], None),
    ({"id": 123456789}, None),
    (None, None),
    (BIG, BIG),                     # 超大正整数原样给
    (str(BIG), BIG),
    ("9" * 5000, None),             # 超过解释器整数转换上限，不抛
])
def test_chat_id(value, want):
    got = svc._chat_id({"chat_id": value})
    assert got == want and type(got) is type(want)


def test_chat_id_缺键():
    assert svc._chat_id({}) is None
