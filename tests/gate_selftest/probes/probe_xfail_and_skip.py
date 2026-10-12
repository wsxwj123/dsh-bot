# -*- coding: utf-8 -*-
"""探针。场景：碰了接缝的用例不能靠「预期失败」或「跳过」蒙混过去。"""
import pytest

from gate_probe_util import call_seam


@pytest.mark.xfail(reason="PROBE_XFAIL_REASON")
def test_预期失败的用例碰了接缝():
    call_seam("HUB_LAUNCHCTL", "print", "gui/501/com.dsh-bot.probe-xfail")
    assert 1 == 2


def test_碰了接缝之后跳过():
    call_seam("HUB_LAUNCHCTL", "print", "gui/501/com.dsh-bot.probe-skip")
    pytest.skip("PROBE_SKIP_REASON")
