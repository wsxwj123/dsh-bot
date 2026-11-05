# -*- coding: utf-8 -*-
"""rel-init 验收夹具：直接复用 bot_ops 的隔离夹具与假件（只读引用，不改它们）。

sandbox / _no_touch_real 是 autouse：HOME、DSH_BOT_HOME、各配置根全钉在 tmp，
launchctl / bun 默认是毒丸，getMe 只打本机桩。这里只把夹具名引进本目录，
不引 bot_ops 的 pytest 钩子（那几个钩子只服务它自己的后台 job 账本）。
"""


def _require_total_gate():
    """tests/conftest.py 的总闸没加载就拒绝运行（同 bot_ops 的自检；须在导入 bot_ops 之前，
    否则会先被 bot_ops.conftest 拒绝，提示的启动命令落在 bot_ops 而不是本目录）。"""
    import os
    import sys
    gate = getattr(sys, "_dsh_bot_test_gate", None)
    if isinstance(gate, dict) and gate.get("pid") == os.getpid():
        return
    here = os.path.dirname(os.path.abspath(__file__))
    tests = here
    while os.path.basename(tests) != "tests" and os.path.dirname(tests) != tests:
        tests = os.path.dirname(tests)
    root = os.path.dirname(tests)
    raise RuntimeError(
        "测试总闸（tests/conftest.py）没有加载，拒绝运行本目录的用例。\n"
        "请从仓库根或 tests/ 目录启动，例如：cd %s && python3 -m pytest %s" % (root, os.path.relpath(here, root)))


_require_total_gate()
del _require_total_gate

from bot_ops.conftest import (  # noqa: F401  夹具
    _no_touch_real, as_darwin, as_linux, bun, client, heartbeat, launchctl, sandbox, telegram)
from bot_ops.adapter_util import adapter, console_guard  # noqa: F401  夹具
