# -*- coding: utf-8 -*-
"""探针。场景：一条规规矩矩、不碰任何系统接缝的用例，应当照常通过。"""
import os


def test_不碰接缝():
    assert "dsh-test-gate-" in os.environ["HUB_LAUNCHCTL"]
    assert "dsh-test-gate-" in os.environ["HUB_BUN"]
