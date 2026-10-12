# -*- coding: utf-8 -*-
"""探针。场景：一条用例没换假件，直接去调 HUB_BUN。"""
from gate_probe_util import call_seam


def test_没换假件就调HUB_BUN():
    r = call_seam("HUB_BUN", "--version")
    print("PROBE_RC=%d" % r.returncode)
    assert r.returncode == 97
