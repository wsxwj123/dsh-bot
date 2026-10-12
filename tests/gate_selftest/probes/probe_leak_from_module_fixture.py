# -*- coding: utf-8 -*-
"""探针。场景：比用例作用域更高的夹具（模块级）改了两个执行接缝。

模块级夹具先于任何函数级夹具执行，这里的泄漏只有「每条用例开始前重置」这一层拦得住。
泄漏用的是不存在的路径，只设不执行。
"""
import os

import pytest


@pytest.fixture(scope="module", autouse=True)
def 模块级夹具改了接缝():
    os.environ["HUB_LAUNCHCTL"] = "/nonexistent/leaked-by-module-fixture/launchctl"
    os.environ["HUB_BUN"] = "/nonexistent/leaked-by-module-fixture/bun"
    yield


def test_用例开始前两个执行接缝已回到毒丸():
    assert "dsh-test-gate-" in os.environ["HUB_LAUNCHCTL"], os.environ["HUB_LAUNCHCTL"]
    assert "dsh-test-gate-" in os.environ["HUB_BUN"], os.environ["HUB_BUN"]
