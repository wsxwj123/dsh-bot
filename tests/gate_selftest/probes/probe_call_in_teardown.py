# -*- coding: utf-8 -*-
"""探针。场景：毒丸是在夹具的收尾阶段被调用的。"""
import pytest

from gate_probe_util import call_seam


@pytest.fixture
def cleanup_that_touches_launchctl():
    yield
    call_seam("HUB_LAUNCHCTL", "print", "gui/501/com.dsh-bot.probe-teardown")


def test_夹具收尾阶段碰了接缝(cleanup_that_touches_launchctl):
    pass
