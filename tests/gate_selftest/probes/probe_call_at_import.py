# -*- coding: utf-8 -*-
"""探针。场景：毒丸在收集/导入期就被调用（不在任何用例的窗口里），用例本身是干净的。"""
from gate_probe_util import call_seam

call_seam("HUB_LAUNCHCTL", "print", "gui/501/com.dsh-bot.probe-import")


def test_用例本身是干净的():
    pass
