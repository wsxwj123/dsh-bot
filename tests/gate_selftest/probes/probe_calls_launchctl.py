# -*- coding: utf-8 -*-
"""探针（不是常规用例；只由 ../test_total_gate.py 起子进程点名运行）。

场景：一条用例没换假件，直接去调 HUB_LAUNCHCTL。用例体自己的断言全过，
应当被总闸改判失败。
"""
from gate_probe_util import call_seam


def test_没换假件就调HUB_LAUNCHCTL():
    r = call_seam("HUB_LAUNCHCTL", "print", "gui/501/com.dsh-bot.probe-ghost")
    print("PROBE_RC=%d" % r.returncode)
    assert r.returncode == 97
