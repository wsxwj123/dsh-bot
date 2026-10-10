# -*- coding: utf-8 -*-
"""`moments/config_sources.py` 与管理台"两套配置根"的白盒单测。

盯的是混跑期的合并规则：两边目录都在 / 只有一边 / 都不在 / 同名以新系统为准 /
env 关掉一边。全部在 tmp 目录里跑，不读真实 `~/.dsh-bot`（默认路径那条用例
把 HOME 钉在 tmp）。HTTP 层顺带验 /hub/api/bots 的来源标注与覆盖点名。
"""
import pytest
from flask import Flask

import config_loader
from moments import bots_client, config_sources as cs
from moments.hub_routes import hub_bp


def _write(d, bot_id, **fields):
    d.mkdir(parents=True, exist_ok=True)
    text = "id: %s\n" % bot_id
    for k, v in fields.items():
        text += "%s: %s\n" % (k, v)
    (d / ("%s.yml" % bot_id)).write_text(text, encoding="utf-8")


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """两个 tmp 配置根 + 已设好的 env；返回 (legacy, dsh) 两个 Path。"""
    legacy = tmp_path / "legacy"
    dsh = tmp_path / "dsh"
    legacy.mkdir()
    dsh.mkdir()
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(legacy))
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(dsh))
    return legacy, dsh


# ------------------------------------------------------------ 根解析

def test_默认读两边_新系统在后(roots):
    got = cs.roots()
    assert [s for s, _d in got] == [cs.SOURCE_LEGACY, cs.SOURCE_DSH]
    assert str(got[1][1]) == str(roots[1]), "靠后的根（新系统）优先级更高"


def test_默认新系统目录跟HOME走(tmp_path, monkeypatch):
    monkeypatch.delenv("HUB_CONFIGS_DSH_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cs.dsh_dir() == tmp_path / ".dsh-bot" / "configs"


def test_env关掉新系统那一边(monkeypatch, tmp_path):
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(tmp_path))
    for off in ("off", "OFF", "-", "none", "0"):
        monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", off)
        assert cs.dsh_dir() is None, off
        assert [s for s, _d in cs.roots()] == [cs.SOURCE_LEGACY]
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", "")      # 空串 = 没设，走默认
    assert cs.dsh_dir() is not None


# ------------------------------------------------------------ 合并名单

def test_两边目录都在_名单合并且带来源(roots):
    _write(roots[0], "bot2", display_name="二号")
    _write(roots[1], "bot5", display_name="五号")
    rows = config_loader.list_enabled_bots(include_disabled=True, dirs=cs.roots())
    assert [(c["_bot_id"], c["_source"]) for c in rows] == [
        ("bot2", cs.SOURCE_LEGACY), ("bot5", cs.SOURCE_DSH)]


def test_只有一边_另一边跳过不报错(roots):
    _write(roots[1], "bot5")
    rows = config_loader.list_enabled_bots(include_disabled=True, dirs=cs.roots())
    assert [c["_bot_id"] for c in rows] == ["bot5"]


def test_都不在_空名单不是错误(roots):
    import shutil
    shutil.rmtree(roots[1])
    assert config_loader.list_enabled_bots(include_disabled=True, dirs=cs.roots()) == []


def test_同名以新系统为准_并标出压住了旧那份(roots):
    _write(roots[0], "same", display_name="旧的")
    _write(roots[1], "same", display_name="新的")
    rows = config_loader.list_enabled_bots(include_disabled=True, dirs=cs.roots())
    assert len(rows) == 1
    cfg = rows[0]
    assert (cfg["_source"], cfg["display_name"], cfg["_shadowed"]) == (
        cs.SOURCE_DSH, "新的", True)


def test_停用判定跟着赢的那份走_两侧都验(roots):
    # 旧停用、新启用 → 默认列表里在；旧启用、新停用 → 不在（过滤发生在合并之后）
    _write(roots[0], "a", enabled="false")
    _write(roots[1], "a")
    _write(roots[0], "b")
    _write(roots[1], "b", enabled="false")
    assert [c["_bot_id"] for c in config_loader.list_enabled_bots(dirs=cs.roots())] == ["a"]


def test_不传dirs时形状与以前一字不变(roots):
    """默认单根：不加 _source/_shadowed，别的消费方（运行时）零感知。"""
    _write(roots[0], "bot2")
    rows = config_loader.list_enabled_bots(include_disabled=True)
    assert [c["_bot_id"] for c in rows] == ["bot2"]
    assert "_source" not in rows[0] and "_shadowed" not in rows[0]


# ------------------------------------------------------------ 面板名单 / 投递名单

def test_面板名单含停用的bot_两套根都在(roots):
    """停用的 bot 是"跑在另一套系统里"，展示面照样要列出来（与 /hub 页同口径）。"""
    _write(roots[0], "bot2")                       # 旧系统在跑
    _write(roots[1], "bot2", enabled="false")      # 新系统那份停用
    _write(roots[1], "bot4")
    assert [(c["_bot_id"], c["_source"]) for c in cs.panel_bot_configs()] == [
        ("bot2", cs.SOURCE_DSH), ("bot4", cs.SOURCE_DSH)]


def test_投递名单取在跑那份_新系统停用则回落旧系统(roots):
    """投递拿错那份会把通知写进没人读的目录，所以要按"哪份在跑"取。"""
    _write(roots[0], "bot2", bot_channel_path="/old/bot2")
    _write(roots[1], "bot2", enabled="false", bot_channel_path="/new/bot2")
    _write(roots[1], "bot4", bot_channel_path="/new/bot4")
    _write(roots[0], "dead", enabled="false")
    assert [(c["_bot_id"], c["bot_channel_path"]) for c in cs.active_bot_configs()] == [
        ("bot2", "/old/bot2"), ("bot4", "/new/bot4")]


def test_投递名单新系统启用时新系统赢(roots):
    _write(roots[0], "same", bot_channel_path="/old")
    _write(roots[1], "same", bot_channel_path="/new")
    assert [c["bot_channel_path"] for c in cs.active_bot_configs()] == ["/new"]


def test_两边都停用不进投递名单(roots):
    _write(roots[0], "x", enabled="false")
    _write(roots[1], "x", enabled="false")
    assert cs.active_bot_configs() == []


# ------------------------------------------------------------ 写路径落点

def test_find_path新系统优先_其次旧系统_都没有None(roots):
    _write(roots[1], "bot5")
    _write(roots[0], "bot2")
    _write(roots[0], "both")
    _write(roots[1], "both")
    assert cs.find_path("bot5") == roots[1] / "bot5.yml"
    assert cs.find_path("bot2") == roots[0] / "bot2.yml"
    assert cs.find_path("both") == roots[1] / "both.yml"
    assert cs.find_path("ghost") is None


def test_find_path拒绝路径形状的名字(roots):
    for bad in ("../x", "a/b", "a\\b", "", "a\x00b"):
        assert cs.find_path(bad) is None, bad


# ------------------------------------------------------------ 端口 / 停用（注册表侧）

def test_注册表端口合并两套_同名新系统赢(roots):
    import bots_registry
    _write(roots[0], "bot2", dispatcher_port="18002")
    _write(roots[0], "both", dispatcher_port="18003")
    _write(roots[1], "bot5", dispatcher_port="18005")
    _write(roots[1], "both", dispatcher_port="18006")
    ports = bots_registry.ports(dirs=[str(d) for _s, d in cs.roots()])
    assert ports["bot2"] == 18002 and ports["bot5"] == 18005
    assert ports["both"] == 18006


def test_bots_client端口表也走两套(roots):
    _write(roots[0], "bot2", dispatcher_port="18002")
    _write(roots[1], "bot5", dispatcher_port="18005")
    assert bots_client.bot_ports() == {"bot2": 18002, "bot5": 18005}
    assert bots_client.bot_port("bot5") == 18005


# ------------------------------------------------------------ HTTP 层：/hub/api/bots

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(bots_client, "_probe_one", lambda port: None)   # 不真探活
    app = Flask(__name__)
    app.register_blueprint(hub_bp)
    app.config["TESTING"] = True
    return app.test_client()


def test_接口只回新系统的bot并标出来源与覆盖(roots, client):
    """这一页只管新系统的 bot：只在旧根里的 bot2 四个出口都不出现。

    同名两份时 same 走新系统那份，标 shadowed，覆盖关系在 overrides 点名。
    """
    _write(roots[0], "bot2", dispatcher_port="18002")
    _write(roots[0], "same", dispatcher_port="18003")
    _write(roots[1], "same", dispatcher_port="18004")
    body = client.get("/hub/api/bots").get_json()
    assert [b["id"] for b in body["bots"]] == ["same"]
    assert "bot2" not in body["sources"] and "bot2" not in body["disabled"]
    assert body["sources"]["same"] == {"source": "dsh", "shadowed": True}
    assert body["overrides"] == ["same"]
    # §6.1 的行字段集不许因为这个功能变形（来源走顶层 sources）
    assert set(body["bots"][0]) == {"id", "display_name", "port", "state", "phase",
                                    "pid", "queue_depth", "in_flight"}


def test_接口在注入名单模式下不猜来源(tmp_path, monkeypatch, client):
    import json
    p = tmp_path / "bots.json"
    p.write_text(json.dumps([{"id": "a", "display_name": "甲"}]), encoding="utf-8")
    monkeypatch.setenv("HUB_BOTS_FILE", str(p))
    body = client.get("/hub/api/bots").get_json()
    assert body["sources"] == {} and body["overrides"] == []


# ------------------------------------------------------------ 停用开关落点 / dsh-model 页

def test_停用开关写它自己所在的那份配置(roots):
    from moments import hub_addbot
    from moments.provider_model import HubError
    _write(roots[0], "bot2")
    _write(roots[1], "bot5")
    hub_addbot.set_enabled("bot5", False)      # 新系统 bot：写新系统那份
    assert "enabled: false" in (roots[1] / "bot5.yml").read_text(encoding="utf-8")
    assert (roots[0] / "bot2.yml").read_text(encoding="utf-8") == "id: bot2\n"
    hub_addbot.set_enabled("bot2", False)      # 旧系统 bot：还是写旧那份
    assert "enabled: false" in (roots[0] / "bot2.yml").read_text(encoding="utf-8")
    with pytest.raises(HubError) as e:         # 都没有 → 404，行为同改动前
        hub_addbot.set_enabled("ghost", False)
    assert e.value.error == "bot_not_found"


def test_同名时开关只动生效的新系统那份(roots):
    from moments import hub_addbot
    _write(roots[0], "same")
    _write(roots[1], "same")
    hub_addbot.set_enabled("same", False)
    assert "enabled: false" in (roots[1] / "same.yml").read_text(encoding="utf-8")
    assert (roots[0] / "same.yml").read_text(encoding="utf-8") == "id: same\n"


def test_dsh_model页只列新系统的bot(roots, tmp_path):
    """网关可用的旧系统 bot 不进这个页面：它按配置来源（新系统那份）筛。"""
    from moments import hub_routes
    ch = tmp_path / "chan"
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "api.key").write_text("k", encoding="utf-8")
    (tmp_path / "state" / "api.port").write_text("1", encoding="utf-8")
    _write(roots[0], "yasuna", bot_channel_path=ch)
    _write(roots[1], "bot5", bot_channel_path=ch)
    assert [b[0] for b in hub_routes._dm_bots()] == ["bot5"]
