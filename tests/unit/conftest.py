# -*- coding: utf-8 -*-
"""M4 纯函数层单测夹具。

与验收测试的区别：这里直接调函数，不起 Flask、不起桩、不发 HTTP、不解析域名
（SSRF 用例一律注入假解析器）。落盘只在 tmp_path 下，绝不碰真实 ~/.claude/settings.json。
"""
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture(autouse=True)
def _no_real_settings(monkeypatch, tmp_path):
    """任何用例都不许继承外部 env 的真实 settings 路径与真实 cliproxy 凭据。

    第二段是 M3 加的：`hub_routes` 在蓝图注册时跑一次启动 reconcile（§3.6 F4），
    它会读 cliproxy 三键；env 里读不到就**回落直读 `~/.claude-tgbot/hub.env`**（§0.3）。
    不钉死 `HUB_ENV_FILE`，任何挂了蓝图的用例都可能连上机主真实的 8317 并改它的配置。
    """
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(tmp_path / "settings.json"))
    # MOMENTS_WEB_HOST：抽查 13 起 hub_auth.install() 会据它跑拒绝闸，
    # 继承外部环境的话用例会莫名整站 503。
    for k in ("CLIPROXY_PORT", "CLIPROXY_MGMT_KEY", "CLIPROXY_API_KEY",
              "CLIPROXY_ANTHROPIC_COMPAT", "HUB_ALLOW_PRIVATE_BASE_URL",
              "MOMENTS_WEB_HOST"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HUB_ENV_FILE", str(tmp_path / "hub.env"))       # 有意指向不存在的文件
    # 安审 S3 起，写路径会去 chmod `<CLAUDE_TGBOT_HOME>/cliproxy/config.yaml`。
    # 不钉死的话用例会去动机主真实的那份配置。
    monkeypatch.setenv("CLAUDE_TGBOT_HOME", str(tmp_path / "tgbot-home"))


def _write_exec(path, posix_text, windows_text):
    """写一个当前平台能直接执行的小脚本，返回路径字符串。"""
    if os.name == "nt":
        path = path.with_suffix(".cmd")
        path.write_text(windows_text, encoding="utf-8")
    else:
        path.write_text(posix_text, encoding="utf-8")
        path.chmod(0o755)
    return str(path)


@pytest.fixture(scope="session")
def _launchctl_nothing_installed(tmp_path_factory, request):
    """假 launchctl（INTERFACE-新系统bot运维 §1 的 `HUB_LAUNCHCTL`）：launchd 里一个单元都没有。

    只回答只读的 `print`（照真 launchctl 的样子报找不到服务、退 113）。别的子命令
    （bootstrap / bootout / kickstart …）一律原样交给毒丸：谁要这些行为，自己在用例里
    换假件，这里不替它放行。整个会话只造一次。
    """
    d = tmp_path_factory.mktemp("unit-fake-launchctl")
    try:
        poison = request.getfixturevalue("total_gate").poison_launchctl   # tests/conftest.py 的总闸
    except pytest.FixtureLookupError:
        # 总闸没加载（在 tests/unit 里面直接起 pytest 时会这样）：自备一颗，照样默认拒绝
        poison = _write_exec(
            d / "poison-launchctl",
            "#!/bin/sh\necho 'unit: poison launchctl was called and refused' >&2\nexit 97\n",
            "@echo unit: poison launchctl was called and refused 1>&2\r\n@exit /b 97\r\n")
    return _write_exec(
        d / "launchctl",
        "#!/bin/sh\n"
        'if [ "$1" = "print" ]; then\n'
        '  echo "Could not find service \\"${2##*/}\\" in domain for user gui: 501" >&2\n'
        "  exit 113\n"
        "fi\n"
        "exec '%s' \"$@\"\n" % poison,
        "@echo off\r\n"
        'if "%%~1"=="print" (\r\n'
        "  echo Could not find service in domain for user gui 1>&2\r\n"
        "  exit /b 113\r\n"
        ")\r\n"
        '"%s" %%*\r\n'
        "exit /b %%ERRORLEVEL%%\r\n" % poison)


@pytest.fixture(autouse=True)
def _no_real_launchctl(monkeypatch, _launchctl_nothing_installed):
    """任何用例都不许去问真机的 launchd。

    管理台列 bot 时会对新系统的 bot 做 `launchctl print`（判活，§5）。不换假件的话，
    问到的是这台机器上真在跑的那些 bot，结果跟着真机状态变。要别的 launchd 状态的用例
    自己再 `monkeypatch.setenv("HUB_LAUNCHCTL", ...)` 覆盖。`HUB_BUN` 这里不动，
    保持总闸的毒丸。
    """
    monkeypatch.setenv("HUB_LAUNCHCTL", _launchctl_nothing_installed)


def payload(**over):
    """§3.3 的合法请求体（与验收 conftest.provider_payload 同形）。"""
    body = {"label": "DeepSeek", "kind": "openai",
            "base_url": "https://api.deepseek.com/v1", "api_key": "sk-test-0000000000",
            "upstream_model": "deepseek-v4-flash",
            "model_alias": "claude-sonnet-4-5-20250929"}
    body.update(over)
    return body


def no_dns(_host):
    """假解析器：域名一律解析不出 → 按"挡不掉就放行"处理，且保证单测不联网。"""
    return []


def dns_to(*addrs):
    return lambda _host: list(addrs)
