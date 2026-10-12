# -*- coding: utf-8 -*-
"""探针。场景：上一条用例绕过 monkeypatch 直接改了环境变量（泄漏），下一条开始前必须已被重置。

两条用例按文件内顺序执行，合起来是一个场景。泄漏用的都是不存在的路径，只设不执行。
"""
import os

LEAKED = {
    "HUB_LAUNCHCTL": "/nonexistent/leaked-launchctl",
    "HUB_BUN": "/nonexistent/leaked-bun",
    "HUB_LAUNCHAGENTS_DIR": "/nonexistent/leaked-LaunchAgents",
    "DSH_BOT_HOME": "/nonexistent/leaked-dsh-home",
    "HUB_CONFIGS_DSH_DIR": "/nonexistent/leaked-configs",
    "HUB_CHANNELS_DIR": "/nonexistent/leaked-channels",
    "HUB_CONFIGS_DIR": "/nonexistent/leaked-configs-legacy",
    "HUB_TELEGRAM_API_BASE": "http://127.0.0.1:2/leaked",
}


def test_1_泄漏():
    os.environ.update(LEAKED)


def test_2_下一条已重置():
    for key, leaked in LEAKED.items():
        assert os.environ[key] != leaked, "%s 还是上一条用例漏过来的值" % key
    assert "dsh-test-gate-" in os.environ["HUB_LAUNCHCTL"]
    assert "dsh-test-gate-" in os.environ["HUB_BUN"]
    assert os.environ["HUB_TELEGRAM_API_BASE"] == "http://127.0.0.1:1"
