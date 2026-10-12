# -*- coding: utf-8 -*-
"""探针。场景：上一条用例绕过 monkeypatch 直接改了环境变量，紧接着一个模块级夹具才第一次启动。

模块级夹具在下一条用例的函数级夹具之前执行，它看到的值取决于「上一条用例收尾时环境有没有
放回会话默认值」。两条用例按文件内顺序执行，合起来是一个场景。
"""
import os

import pytest


def test_1_泄漏():
    os.environ["HUB_LAUNCHCTL"] = "/nonexistent/leaked-by-previous-test/launchctl"
    os.environ["HUB_BUN"] = "/nonexistent/leaked-by-previous-test/bun"


@pytest.fixture(scope="module")
def 晚启动的模块级夹具():
    return os.environ.get("HUB_LAUNCHCTL"), os.environ.get("HUB_BUN")


def test_2_模块级夹具启动时看到的已是毒丸(晚启动的模块级夹具):
    launchctl, bun = 晚启动的模块级夹具
    assert "dsh-test-gate-" in launchctl, launchctl
    assert "dsh-test-gate-" in bun, bun
