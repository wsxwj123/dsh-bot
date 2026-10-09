# -*- coding: utf-8 -*-
"""旧系统（claudebotlife）provider 子系统的路径基准（唯一事实源）。

provider_config 与 provider_proxy 有单向依赖约定（provider_proxy 永不 import
provider_config），而 provider_proxy 的安全闸要拿「真实默认位置」与「实际解析出的
位置」比对（gate_branch 的 GATE_MANIFEST / GATE_RUN_DIR 两支），两处必须同源，
否则闸形同虚设。故集中在本模块。

搬进本仓后默认值指向旧仓那份：面板读写的是旧仓 ``configs/providers.json`` 与旧
bot 的 ``<bot_dir>/.claude/settings.json``，8770 那套 provider-proxy 才读得到。
（本仓 ``configs/`` 只放新系统自己的配置文件，与旧清单无关。）

两类默认要分清（安全闸在旧仓就是这么判的）：
- ``real_default_providers_path()``：**固定**的缺省位置，不读任何 env —— 安全闸的
  比对基准必须固定，读了 env 就等于"测试设了接缝 → 接缝值被当成真实默认 → 闸失效"；
- ``default_providers_path()``：实际解析值（env 覆盖优先），供 provider_config 取默认。
"""
import os

# 旧仓根目录的覆盖接缝（测试/多机部署用）；不设 → ~/claudebotlife
LEGACY_ROOT_ENV = "CLAUDEBOT_LEGACY_ROOT"


def _account_home() -> str:
    """真实主目录（固定基准不看 env）。POSIX 上按账户数据库取 —— 有意不看 HOME：
    测试会把 HOME 钉到 tmp，闸的比对基准不能跟着漂；Windows 没有 pwd 也没有 uid 语义，
    家目录就是 HOME/USERPROFILE（`expanduser("~")` 正是这个口径），故落回它。
    `pwd` 是 POSIX 专用模块，**延后到调用期 import**（照 scripts/install_compact_hook.py
    的先例），模块导入面在 Windows 上保持干净。"""
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, AttributeError, KeyError, OSError):
        return os.path.expanduser("~")


def legacy_root() -> str:
    """旧仓根：``CLAUDEBOT_LEGACY_ROOT`` 覆盖，缺省 ``~/claudebotlife``。"""
    return os.path.expanduser(os.environ.get(LEGACY_ROOT_ENV) or "~/claudebotlife")


def real_default_providers_path() -> str:
    """旧清单的**固定**缺省位置（不读 env）：安全闸 GATE_MANIFEST 支的比对基准。"""
    return os.path.join(_account_home(), "claudebotlife", "configs", "providers.json")


def default_providers_path() -> str:
    """清单落点（实际解析值）：``CLAUDEBOT_PROVIDER_PATH`` 覆盖，缺省 = 旧仓那份。"""
    return os.environ.get("CLAUDEBOT_PROVIDER_PATH") or os.path.join(
        legacy_root(), "configs", "providers.json")


def real_run_dir() -> str:
    """8770 实例运行目录的缺省位置（按账户数据库取真实主目录，不看 HOME）。"""
    return os.path.join(_account_home(), "Library", "Application Support",
                        "claudebotlife-provider-proxy")
