# -*- coding: utf-8 -*-
"""旧 provider 面板（管 8770 provider-proxy 的那套）搬入本仓的回归。

三块：① 路径解析的默认值与 env 覆盖（默认必须指向**旧仓**那份）；
② provider_config 的读写与校验（全部落 tmp_path，绝不碰旧仓/机主真实文件）；
③ 蓝图注册后新旧两套 provider 页 URL 共存不冲突。

不联网、不起 HTTP、不发探针；重启 worker 的调用点不在此文件覆盖（HTTP 层验证另行真跑）。
"""
import json
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import legacy_config_loader as lcl        # noqa: E402
import provider_config as pc              # noqa: E402
import provider_paths                     # noqa: E402


@pytest.fixture
def legacy_env(monkeypatch, tmp_path):
    """把旧 provider 链的落点全部钉到 tmp_path（清单 / yml 根 / channels 根 / 代理运行目录）。

    与 provider_config 的「接缝成组」约定一致地设置三个接缝；代理运行目录也隔离，
    让 provider_proxy 的安全闸没有机会碰到真实位置。
    """
    root = tmp_path / "legacy"
    configs = root / "configs"
    channels = tmp_path / "channels"
    proxy_dir = tmp_path / "proxy"
    bot_dir = channels / "chanbot"
    (bot_dir / ".claude").mkdir(parents=True)
    configs.mkdir(parents=True)
    proxy_dir.mkdir()
    (configs / "chanbot.yml").write_text(
        "bot_channel_path: %s\nchat_id: '1'\ndisplay_name: 测试\n" % bot_dir,
        encoding="utf-8")
    monkeypatch.setenv("CLAUDEBOT_CONFIG_DIR", str(configs))
    monkeypatch.setenv("CLAUDEBOT_CHANNELS_ROOT", str(channels))
    monkeypatch.setenv("CLAUDEBOT_PROVIDER_PATH", str(configs / "providers.json"))
    monkeypatch.setenv("CLAUDEBOT_PROXY_DIR", str(proxy_dir))
    monkeypatch.setenv("CLAUDEBOT_PROXY_LAUNCHCTL", "off")
    # 模块级常量是导入期快照（与生产一致），测试里显式对齐
    monkeypatch.setattr(pc, "PROVIDERS_PATH", str(configs / "providers.json"))
    monkeypatch.setattr(pc, "CHANNELS_ROOT", str(channels))
    return {"configs": configs, "channels": channels, "bot_dir": bot_dir,
            "providers": configs / "providers.json", "proxy_dir": proxy_dir}


def _entry(**over):
    e = {"id": "p1", "name": "P1", "base_url": "https://api.example.com/v1",
         "api_key": "sk-test-123", "models": ["m1"], "protocol": "anthropic"}
    e.update(over)
    return e


# ── ① 路径解析 ─────────────────────────────────────────────────────────────

def test_default_providers_path_points_into_legacy_repo(monkeypatch):
    monkeypatch.delenv("CLAUDEBOT_PROVIDER_PATH", raising=False)
    monkeypatch.delenv(provider_paths.LEGACY_ROOT_ENV, raising=False)
    assert provider_paths.default_providers_path() == os.path.join(
        os.path.expanduser("~/claudebotlife"), "configs", "providers.json")


def test_providers_path_env_overrides(monkeypatch, tmp_path):
    p = str(tmp_path / "x.json")
    monkeypatch.setenv("CLAUDEBOT_PROVIDER_PATH", p)
    assert provider_paths.default_providers_path() == p


def test_provider_config_constant_follows_default_rule():
    """清单落点（导入期常量）与运行期公式同源：外部设了接缝时两者读到同一值。"""
    assert pc.PROVIDERS_PATH == provider_paths.default_providers_path()


def test_legacy_config_loader_defaults(monkeypatch):
    monkeypatch.delenv("CLAUDEBOT_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDEBOT_CHANNELS_ROOT", raising=False)
    assert lcl.config_dir() == os.path.join(provider_paths.legacy_root(), "configs")
    assert lcl.channels_dir() == os.path.expanduser("~/.claude/channels")


# ── ② 读写与校验 ───────────────────────────────────────────────────────────

def test_load_providers_missing_file_is_empty(legacy_env):
    assert pc.load_providers() == {"providers": [], "bindings": {}}


def test_load_providers_corrupt_raises(legacy_env):
    legacy_env["providers"].write_text("{not json", encoding="utf-8")
    with pytest.raises(pc.ProvidersCorrupt):
        pc.load_providers()


def test_upsert_validation_and_roundtrip(legacy_env):
    entry, err = pc.upsert_provider(_entry())
    assert err is None and entry and entry["id"]
    assert "api_key" not in entry                    # HTTP 出口形状：脱敏
    assert pc.upsert_provider(_entry(name="x", base_url="ftp://bad"))[1]
    assert pc.upsert_provider({"base_url": "https://a.example"})[1] == "name required"
    raw = json.loads(legacy_env["providers"].read_text(encoding="utf-8"))
    assert raw["providers"][0]["api_key"] == "sk-test-123"   # 明文只落清单
    assert raw["proxy"]["instance_key"]                      # 汇口第 0 步
    assert not (legacy_env["proxy_dir"] / "config.yaml").exists()   # 无 proxy provider：零落盘


def test_set_bot_provider_writes_into_tmp_channels(legacy_env):
    result, err = pc.set_bot_provider("chanbot", _entry(), "m1")
    assert err is None and result
    sp = legacy_env["bot_dir"] / ".claude" / "settings.json"
    doc = json.loads(sp.read_text(encoding="utf-8"))
    assert doc["env"]["ANTHROPIC_BASE_URL"] == "https://api.example.com/v1"
    assert doc["env"]["ANTHROPIC_MODEL"] == "m1"
    raw = json.loads(legacy_env["providers"].read_text(encoding="utf-8"))
    assert raw["bindings"]["chanbot"]["provider_id"] == "p1"
    assert raw["bindings"]["chanbot"]["pending_restart"] is True
    assert pc.set_bot_provider("nope", _entry(), "m1")[1].startswith("unknown bot")


def test_set_bot_provider_rejects_bot_dir_outside_channels_root(legacy_env, tmp_path):
    outside = tmp_path / "outside"
    (outside / ".claude").mkdir(parents=True)
    (legacy_env["configs"] / "evil.yml").write_text(
        "bot_channel_path: %s\n" % outside, encoding="utf-8")
    _, err = pc.set_bot_provider("evil", _entry(), "m1")
    assert err                                       # 越界拒写
    assert not (outside / ".claude" / "settings.json").exists()


# ── ③ 两套 provider 页共存 ─────────────────────────────────────────────────

def test_url_map_has_both_provider_pages():
    from moments import web as web_mod
    rules = {}
    for r in web_mod.app.url_map.iter_rules():
        rules.setdefault(str(r), set()).add(r.endpoint)
    for path in ("/provider", "/api/providers", "/hub/provider"):
        assert path in rules, "缺路由: %s" % path
    assert rules["/provider"] == {"provider.provider_page"}
    # 旧页在自己前缀下，新版 hub 页在 /hub 下；互不覆盖（同一 path 不可能有两个端点，
    # 否则 Flask 注册期就抛异常）
    assert all(not p.startswith("/hub") for p in ("/provider", "/api/providers"))
