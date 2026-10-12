# -*- coding: utf-8 -*-
"""探针。场景：被拦下的 argv 里带着像密钥的串，失败信息里必须打码。"""
from gate_probe_util import call_seam

TG_TOKEN = "123456789:test-AAaaBBccDDddEEeeFFggHHhh11"
SK_KEY = "sk-test-0000000000000000000000"


def test_argv里带密钥():
    call_seam("HUB_LAUNCHCTL", "print", "TELEGRAM_BOT_TOKEN=" + TG_TOKEN, SK_KEY,
              "gui/501/com.dsh-bot.probe-ghost")
