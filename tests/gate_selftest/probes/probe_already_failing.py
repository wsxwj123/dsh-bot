# -*- coding: utf-8 -*-
"""探针。场景：用例碰了接缝，而且它自己的断言本来也是红的。原失败要保留，总闸信息附在后面。"""
from gate_probe_util import call_seam


def test_本来就红还碰了接缝():
    call_seam("HUB_LAUNCHCTL", "print", "gui/501/com.dsh-bot.probe-ghost")
    assert 1 == 2, "PROBE_ORIGINAL_FAILURE"
