# -*- coding: utf-8 -*-
"""探针。场景：被测代码在「导入时」就读环境变量。

本模块顶层（导入期）把读到的值写进 GATE_PROBE_OUT 指的文件。配合 --collect-only 运行：
没有任何夹具执行，读到的值只可能来自 conftest 的模块顶层。
"""
import json
import os

KEYS = ("HUB_LAUNCHCTL", "HUB_BUN", "HUB_LAUNCHAGENTS_DIR", "DSH_BOT_HOME",
        "HUB_CONFIGS_DSH_DIR", "HUB_CHANNELS_DIR", "HUB_CONFIGS_DIR", "HUB_TELEGRAM_API_BASE",
        "HUB_RESTART_CMD")

_env = {k: os.environ.get(k) for k in KEYS}
with open(os.environ["GATE_PROBE_OUT"], "w", encoding="utf-8") as _f:
    json.dump({
        "env": _env,
        "executable": {k: bool(_env[k]) and os.access(_env[k], os.X_OK)
                       for k in ("HUB_LAUNCHCTL", "HUB_BUN")},
        "isdir": {k: bool(_env[k]) and os.path.isdir(_env[k])
                  for k in ("HUB_LAUNCHAGENTS_DIR", "DSH_BOT_HOME", "HUB_CONFIGS_DSH_DIR",
                            "HUB_CHANNELS_DIR", "HUB_CONFIGS_DIR")},
    }, _f)


def test_占位_仅收集时不该被执行():
    raise AssertionError("这条用例只该被收集，不该被执行")
