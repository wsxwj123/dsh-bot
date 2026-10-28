# -*- coding: utf-8 -*-
"""复用 bot_ops 验收目录的沙箱与适配服务夹具。

那套夹具已经把 HOME、DSH_BOT_HOME、配置根、频道目录、LaunchAgents 全部钉在 tmp，
并把 launchctl / bun 换成毒丸；这里只做导入，不复制、不改动。
"""
from bot_ops.conftest import (  # noqa: F401
    sandbox, client, launchctl, heartbeat, as_darwin, bun, telegram, botlife_db,
    _no_touch_real, _jobs_reach_terminal,
)
from bot_ops.adapter_util import adapter, console_guard  # noqa: F401
