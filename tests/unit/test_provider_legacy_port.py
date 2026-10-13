# -*- coding: utf-8 -*-
"""旧 provider 面板（管 8770 provider-proxy 的那套）那几个模块自身的回归，以及它已从管理台摘掉。

三块：① 路径解析的默认值与 env 覆盖（默认必须指向**旧仓**那份）；
② provider_config 的读写与校验（全部落 tmp_path，绝不碰旧仓/机主真实文件）；
③ 管理台已经不再提供这一套：十五条旧接口不在路由清单里，旧页面的地址只剩一条跳转，
   管理台应用不再导入这几个模块（INTERFACE-旧系统供应商入口 §4、§5、§6）。

①② 量的是模块自己的函数，模块文件都还留在原处，所以原样保留。③ 原先量的是「蓝图注册后
新旧两套 provider 页 URL 共存」，这一批把旧的那套下线了，预期随之反过来。

不联网、不起 HTTP、不发探针。
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


# ── ③ 管理台已经把这一套摘掉 ───────────────────────────────────────────────

#: 约定 §5 的十五条：(原来的方法, 一个具体路径)。bot 与供应商都用假名。
RETIRED = [
    ("GET", "/api/providers"), ("POST", "/api/providers"),
    ("PUT", "/api/providers/jia-gys-001"), ("DELETE", "/api/providers/jia-gys-001"),
    ("POST", "/api/providers/models"), ("POST", "/api/providers/sync_all"),
    ("GET", "/api/aux-llm"), ("POST", "/api/aux-llm"),
    ("POST", "/api/bots/jiabot/provider"), ("POST", "/api/bots/jiabot/provider/reset"),
    ("POST", "/api/bots/jiabot/restart"),
    ("GET", "/api/proxy"), ("GET", "/api/proxy/generation"),
    ("POST", "/api/proxy/restart"), ("POST", "/api/proxy/sync"),
]
#: 管理台应用不许再导入的六个模块（约定 §6 第 2 条）
RETIRED_MODULES = {"moments.provider_routes", "provider_config", "provider_proxy",
                   "provider_paths", "legacy_config_loader", "scripts.restart_bot_worker"}
#: 这一批只摘入口、不删文件：下面这些都得还在原处
KEPT_FILES = ["moments/provider_routes.py", "moments/templates/provider.html", "provider_config.py",
              "provider_proxy.py", "provider_paths.py", "legacy_config_loader.py",
              "scripts/restart_bot_worker.py"]


def _web():
    from moments import web as web_mod
    return web_mod


@pytest.mark.parametrize("method,path", RETIRED, ids=["%s_%s" % mp for mp in RETIRED])
def test_管理台的路由清单里没有这条旧接口(method, path):
    """原来这里断言的是「/api/providers 在路由清单里」。现在十五条一条都匹配不上：
    不是换了方法，是整条路径没有规则。"""
    from werkzeug.exceptions import NotFound
    adapter = _web().app.url_map.bind("127.0.0.1")
    for m in (method, "GET", "POST", "PUT", "DELETE"):
        with pytest.raises(NotFound):
            adapter.match(path, method=m)


def test_旧页面的地址只剩一条跳转规则_不再是旧蓝图的页面端点():
    """原来这里断言的是「/provider 的端点是 provider.provider_page」。"""
    rules = [r for r in _web().app.url_map.iter_rules() if str(r) == "/provider"]
    assert len(rules) == 1, rules
    assert rules[0].endpoint == "legacy_provider_entry"
    assert sorted(rules[0].methods) == ["GET", "HEAD", "OPTIONS"]


def test_蓝图清单里没有旧供应商蓝图_模型来源那一套还在():
    app = _web().app
    assert "provider" not in app.blueprints, sorted(app.blueprints)
    assert {"hub", "styles"} <= set(app.blueprints)
    rules = {str(r) for r in app.url_map.iter_rules()}
    for path in ("/hub/provider", "/hub/api/provider", "/hub/api/bots/restart",
                 "/hub/api/bots/<bot_id>/restart", "/api/moments"):
        assert path in rules, "不在下线名单里的路由被顺手摘掉了：%s" % path


def test_管理台应用的源码不再导入那六个模块():
    """管理台进程里有没有这些模块，取决于 moments/web.py 导不导入它们（验收测试另起一个
    干净的解释器量整条导入链）。这里量源码：任何位置的 import 语句都算，写在 try 里、
    函数里的也算。"""
    import ast
    tree = ast.parse((_ROOT / "moments" / "web.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update("%s.%s" % (node.module, a.name) for a in node.names)
    assert imported & RETIRED_MODULES == set(), imported & RETIRED_MODULES


@pytest.mark.parametrize("rel", KEPT_FILES)
def test_只摘入口不删文件_这几个文件都还在原处(rel):
    assert (_ROOT / rel).is_file(), "%s 不见了：这一批不删任何文件" % rel
