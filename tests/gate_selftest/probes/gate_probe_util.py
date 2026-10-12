# -*- coding: utf-8 -*-
"""探针共用的小工具（不是用例）。"""
import os
import subprocess


def call_seam(var, *args):
    """执行环境变量 var 指向的程序，返回 CompletedProcess。

    保险：只有它确实是总闸造的毒丸才执行。探针传的也只是只读子命令（print / --version），
    即便所有防线都失效、真到了系统命令那里，也改不了任何东西。
    """
    exe = os.environ[var]
    assert "dsh-test-gate-" in exe, "保险：%s=%s 不是总闸的毒丸，探针拒绝执行" % (var, exe)
    return subprocess.run([exe] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
