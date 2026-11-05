# -*- coding: utf-8 -*-
"""rel-init 验收夹具：直接复用 bot_ops 的隔离夹具与假件（只读引用，不改它们）。

sandbox / _no_touch_real 是 autouse：HOME、DSH_BOT_HOME、各配置根全钉在 tmp，
launchctl / bun 默认是毒丸，getMe 只打本机桩。这里只把夹具名引进本目录，
不引 bot_ops 的 pytest 钩子（那几个钩子只服务它自己的后台 job 账本）。
"""
from bot_ops.conftest import (  # noqa: F401  夹具
    _no_touch_real, as_darwin, as_linux, bun, client, heartbeat, launchctl, sandbox, telegram)
from bot_ops.adapter_util import adapter, console_guard  # noqa: F401  夹具
