# -*- coding: utf-8 -*-
"""探针。场景：毒丸是在夹具的准备阶段被调用的，用例体本身什么都没碰。"""
import pytest

from gate_probe_util import call_seam


@pytest.fixture
def app_that_touches_launchctl():
    return call_seam("HUB_LAUNCHCTL", "print", "gui/501/com.dsh-bot.probe-setup").returncode


def test_夹具准备阶段碰了接缝(app_that_touches_launchctl):
    assert app_that_touches_launchctl == 97
