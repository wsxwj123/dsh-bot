# -*- coding: utf-8 -*-
"""放行闸白盒单测（INTERFACE-新系统bot运维 §0 第 2 条、§5b 第 4 条，第 6 稿）。

闸只有一份实现 `hub_addbot._edit_target`：开关的写入口 `set_enabled` 与编辑的
`edit_read` / `edit_save` 调的都是它；`bot_launchd.enabled_of` 是同一判据落在
「新系统这边该不该跑」上的那一面。这里直接调函数，逐个分支量：谁过、谁不过，
不过时旧系统配置根下的文件是不是一个字节没动。

不起 Flask，不碰 launchctl（直接调写入函数不会走到同步那一段）；bot 名全是假名。
"""
import os

import pytest

from moments import bot_launchd, bots_client, hub_addbot
from moments.provider_model import HubError

BROKEN = "id: [oops\n  x: : :\n"


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """两套 tmp 配置根，HOME 钉在 tmp。返回 (旧系统根, 新系统根)。"""
    legacy, dsh, home = tmp_path / "legacy", tmp_path / "dsh", tmp_path / "home"
    for d in (legacy, dsh, home):
        d.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(legacy))
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(dsh))
    monkeypatch.delenv("HUB_BOTS_FILE", raising=False)
    return legacy, dsh


def _write(d, bot_id, enabled=None, **fields):
    lines = ["id: %s" % bot_id]
    if enabled is not None:
        lines.append("enabled: %s" % ("true" if enabled else "false"))
    lines += ["%s: %s" % kv for kv in sorted(fields.items())]
    p = d / ("%s.yml" % bot_id)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _rejected(fn, *args):
    """没过闸：404 bot_not_found，detail 就是那一句。"""
    with pytest.raises(HubError) as e:
        fn(*args)
    assert (e.value.status, e.value.error, e.value.detail) == (
        404, "bot_not_found", hub_addbot.GATE_404_DETAIL), (e.value.status, e.value.error)


def _tree(d):
    return {p.name: (p.read_bytes() if p.is_file() else None) for p in sorted(d.iterdir())}


# ------------------------------------------------------------ 闸本身：过

def test_只在新系统根下的bot_过闸_交出的配置来源是新系统(roots):
    _legacy, dsh = roots
    _write(dsh, "gatebot", display_name="新的")
    cfg = hub_addbot._edit_target("gatebot")
    assert (cfg["_bot_id"], cfg["_source"], cfg["display_name"]) == ("gatebot", "dsh", "新的")


@pytest.mark.parametrize("legacy_enabled", [True, False])
def test_同名且新系统那份启用_过闸_旧那份启用或停着都一样(roots, legacy_enabled):
    legacy, dsh = roots
    _write(legacy, "twinbot", enabled=legacy_enabled, display_name="旧的")
    _write(dsh, "twinbot", enabled=True, display_name="新的")
    assert hub_addbot._edit_target("twinbot")["display_name"] == "新的"


def test_同名且两份都停着_生效的是新系统那份_过闸(roots):
    legacy, dsh = roots
    _write(legacy, "twinbot", enabled=False, display_name="旧的")
    _write(dsh, "twinbot", enabled=False, display_name="新的")
    cfg = hub_addbot._edit_target("twinbot")
    assert (cfg["_source"], cfg["display_name"]) == ("dsh", "新的")


# ------------------------------------------------------------ 闸本身：不过

@pytest.mark.parametrize("bad", ["Gatebot", "a", "_x", "1x", "有中文", "../x", "a/b", "x" * 33, "", None])
def test_id形状非法_400且先于一切(roots, monkeypatch, bad):
    monkeypatch.setenv("HUB_BOTS_FILE", "/nonexistent/bots.json")     # 形状错排在名单注入之前
    with pytest.raises(HubError) as e:
        hub_addbot._edit_target(bad)
    assert (e.value.status, e.value.error, e.value.detail) == (400, "bad_bot_id", "bot id 形状非法")


def test_只在旧系统根下的bot_不过闸(roots):
    legacy, _dsh = roots
    _write(legacy, "oldbot")
    _rejected(hub_addbot._edit_target, "oldbot")


def test_同名且新系统那份停着旧那份启用_生效的是旧系统那份_不过闸(roots):
    legacy, dsh = roots
    _write(legacy, "twinbot", enabled=True)
    _write(dsh, "twinbot", enabled=False)
    _rejected(hub_addbot._edit_target, "twinbot")


def test_两套根下都没有这只_不过闸(roots):
    _rejected(hub_addbot._edit_target, "ghostbot")


def test_设了名单注入_新系统根下有它的启用配置也不过闸(roots, monkeypatch, tmp_path):
    _legacy, dsh = roots
    _write(dsh, "gatebot")
    p = tmp_path / "bots.json"
    p.write_text('[{"id": "gatebot"}]', encoding="utf-8")
    monkeypatch.setenv("HUB_BOTS_FILE", str(p))
    _rejected(hub_addbot._edit_target, "gatebot")


@pytest.mark.parametrize("off", ["off", "OFF", "-", "none", "0", "false", "no"])
def test_新系统根被关掉_原先放在那里的bot也不过闸(roots, monkeypatch, off):
    _legacy, dsh = roots
    _write(dsh, "gatebot")
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", off)
    _rejected(hub_addbot._edit_target, "gatebot")


def test_新系统根目录不在场_不过闸_目录也不会被建出来(roots, monkeypatch, tmp_path):
    legacy, _dsh = roots
    _write(legacy, "oldbot")
    missing = tmp_path / "no-such-dsh"
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(missing))
    _rejected(hub_addbot._edit_target, "oldbot")
    assert not missing.exists()


# ------------------------------------------------------------ 两次读之间对不上

def test_判定说新系统_取配置时生效的已是旧系统那份_按不过闸算(roots, monkeypatch):
    """闸先问来源、再取配置。中间文件被改、第二次拿到旧系统那份时不许放行。"""
    legacy, dsh = roots
    _write(legacy, "twinbot", enabled=True)
    _write(dsh, "twinbot", enabled=False)           # 此刻生效的是旧系统那份
    monkeypatch.setattr(bots_client, "bot_sources",   # 第一次读还停在改动之前
                        lambda: {"twinbot": {"source": "dsh", "shadowed": True}})
    _rejected(hub_addbot._edit_target, "twinbot")


def test_判定说新系统_取配置时这只已经没了_按不过闸算(roots, monkeypatch):
    monkeypatch.setattr(bots_client, "bot_sources",
                        lambda: {"ghostbot": {"source": "dsh", "shadowed": False}})
    _rejected(hub_addbot._edit_target, "ghostbot")


# ------------------------------------------------------------ 「坏文件」补充口

def test_补充口_新系统那份解析不出且旧根没有同名_放行并返回None(roots):
    _legacy, dsh = roots
    (dsh / "badbot.yml").write_text(BROKEN, encoding="utf-8")
    assert hub_addbot._edit_target("badbot") is None


def test_补充口_旧根有同名文件_不走这个口(roots):
    legacy, dsh = roots
    (dsh / "badbot.yml").write_text(BROKEN, encoding="utf-8")
    (legacy / "badbot.yml").write_text(BROKEN, encoding="utf-8")     # 两份都坏，判不出归属
    _rejected(hub_addbot._edit_target, "badbot")


def test_补充口_旧根下同名的是个目录_也算有同名(roots):
    legacy, dsh = roots
    (dsh / "badbot.yml").write_text(BROKEN, encoding="utf-8")
    (legacy / "badbot.yml").mkdir()
    _rejected(hub_addbot._edit_target, "badbot")


@pytest.mark.skipif(os.name == "nt", reason="Windows 上建软链要特权")
def test_补充口_旧根下同名的是断掉的软链_也算有同名(roots, tmp_path):
    legacy, dsh = roots
    (dsh / "badbot.yml").write_text(BROKEN, encoding="utf-8")
    os.symlink(str(tmp_path / "nowhere.yml"), str(legacy / "badbot.yml"))
    _rejected(hub_addbot._edit_target, "badbot")


def test_补充口_新系统根下同名的是个目录_不算有它的文件(roots):
    _legacy, dsh = roots
    (dsh / "badbot.yml").mkdir()
    _rejected(hub_addbot._edit_target, "badbot")


# ------------------------------------------------------------ 要读写的 yml 恒在新系统根下

def test_yml路径由新系统根直接拼出(roots):
    _legacy, dsh = roots
    _write(dsh, "gatebot")
    assert hub_addbot._dsh_yml("gatebot") == str(dsh / "gatebot.yml")


def test_yml路径_同名且旧那份在跑时也不回落到旧系统那份(roots):
    """`config_sources.find_path` 这时回的是旧系统那份；闸后的定位不许跟着它走。"""
    from moments import config_sources
    legacy, dsh = roots
    _write(legacy, "twinbot", enabled=True)
    _write(dsh, "twinbot", enabled=False)
    assert config_sources.find_path("twinbot") == legacy / "twinbot.yml"     # 前提
    assert hub_addbot._dsh_yml("twinbot") == str(dsh / "twinbot.yml")


def test_yml路径_新系统根下没有这个文件_404(roots):
    legacy, _dsh = roots
    _write(legacy, "oldbot")
    _rejected(hub_addbot._dsh_yml, "oldbot")


def test_yml路径_新系统根被关掉_404(roots, monkeypatch):
    _legacy, dsh = roots
    _write(dsh, "gatebot")
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", "off")
    _rejected(hub_addbot._dsh_yml, "gatebot")


# ------------------------------------------------------------ 开关的写入口

@pytest.mark.parametrize("enabled", [False, True])
def test_开关_旧系统的bot_抛404且两边一个字节不动(roots, enabled):
    legacy, dsh = roots
    _write(legacy, "oldbot", enabled=not enabled)
    before = (_tree(legacy), _tree(dsh))
    _rejected(hub_addbot.set_enabled, "oldbot", enabled)
    assert (_tree(legacy), _tree(dsh)) == before


def test_开关_旧系统那份是坏的_回404不是bad_yaml(roots):
    """归属先于任何文件读写判完：旧系统的 bot 哪怕配置是坏的，也轮不到报它的文件错。"""
    legacy, _dsh = roots
    (legacy / "oldbot.yml").write_text(BROKEN, encoding="utf-8")
    _rejected(hub_addbot.set_enabled, "oldbot", False)
    assert (legacy / "oldbot.yml").read_text(encoding="utf-8") == BROKEN


def test_开关_同名都启用_停用只写新系统那份(roots):
    legacy, dsh = roots
    old = _write(legacy, "twinbot", enabled=True)
    new = _write(dsh, "twinbot", enabled=True)
    before = old.read_bytes()
    assert hub_addbot.set_enabled("twinbot", False) is False
    assert "enabled: false" in new.read_text(encoding="utf-8")
    assert old.read_bytes() == before


def test_开关_停掉之后这只归旧系统_再开关就是404_两边都不再变(roots):
    legacy, dsh = roots
    _write(legacy, "twinbot", enabled=True)
    _write(dsh, "twinbot", enabled=True)
    hub_addbot.set_enabled("twinbot", False)
    before = (_tree(legacy), _tree(dsh))
    _rejected(hub_addbot.set_enabled, "twinbot", True)
    _rejected(hub_addbot.set_enabled, "twinbot", False)
    assert (_tree(legacy), _tree(dsh)) == before


def test_开关_新系统那份解析不出且旧根没有同名_409且文件不动(roots):
    _legacy, dsh = roots
    (dsh / "badbot.yml").write_text(BROKEN, encoding="utf-8")
    with pytest.raises(HubError) as e:
        hub_addbot.set_enabled("badbot", False)
    assert (e.value.status, e.value.error) == (409, "bad_yaml")
    assert (dsh / "badbot.yml").read_text(encoding="utf-8") == BROKEN


def test_开关_设了名单注入_新系统的bot也不写(roots, monkeypatch, tmp_path):
    _legacy, dsh = roots
    yml = _write(dsh, "gatebot")
    before = yml.read_bytes()
    p = tmp_path / "bots.json"
    p.write_text('[{"id": "gatebot"}]', encoding="utf-8")
    monkeypatch.setenv("HUB_BOTS_FILE", str(p))
    _rejected(hub_addbot.set_enabled, "gatebot", False)
    assert yml.read_bytes() == before


# ------------------------------------------------------------ 编辑走的是同一道闸、同一份 yml

def test_编辑读_同名且过闸_读的是新系统那份的名字(roots):
    legacy, dsh = roots
    _write(legacy, "twinbot", enabled=True, display_name="旧系统里的名字")
    _write(dsh, "twinbot", enabled=True, display_name="新系统里的名字")
    assert hub_addbot.edit_read("twinbot")["display_name"] == "新系统里的名字"


def test_来源临时读不出_同名且旧那份在跑_开关与编辑都404_旧那份不动(roots, monkeypatch):
    """核查表 §10 那一行的缺口：来源判定临时失败时，以前会经补充口放行、再被
    `find_path` 带到旧系统那份 yml 上。现在旧根下有同名文件，补充口不放。"""
    legacy, dsh = roots
    _write(legacy, "twinbot", enabled=True, display_name="旧名")
    _write(dsh, "twinbot", enabled=False, display_name="新名")
    monkeypatch.setattr(bots_client, "bot_sources", lambda: {})
    before = (_tree(legacy), _tree(dsh))
    _rejected(hub_addbot.edit_read, "twinbot")
    _rejected(hub_addbot.edit_save, "twinbot", {"display_name": "想改成这个"})
    _rejected(hub_addbot.set_enabled, "twinbot", False)
    assert (_tree(legacy), _tree(dsh)) == before


def test_编辑存_旧系统的bot_404且文件不动(roots):
    legacy, dsh = roots
    _write(legacy, "oldbot", display_name="旧名")
    before = (_tree(legacy), _tree(dsh))
    _rejected(hub_addbot.edit_save, "oldbot", {"display_name": "想改成这个"})
    assert (_tree(legacy), _tree(dsh)) == before


# ------------------------------------------------------------ enabled_of：新系统这边该不该跑

def test_enabled_of_新系统的bot跟着它那份的enabled走(roots):
    _legacy, dsh = roots
    _write(dsh, "onbot")                    # 没写 enabled = 启用
    _write(dsh, "offbot", enabled=False)
    assert bot_launchd.enabled_of("onbot") is True
    assert bot_launchd.enabled_of("offbot") is False


def test_enabled_of_旧系统的bot一律回否(roots):
    legacy, _dsh = roots
    _write(legacy, "oldbot", enabled=True)
    assert bot_launchd.enabled_of("oldbot") is False


def test_enabled_of_没有这只回否(roots):
    assert bot_launchd.enabled_of("ghostbot") is False


def test_enabled_of_同名且生效的是旧系统那份_回否(roots):
    """开关把新系统那份停掉之后正是这个局面：同步必须走停用方向，不能把它反向拉起。"""
    legacy, dsh = roots
    _write(legacy, "twinbot", enabled=True)
    _write(dsh, "twinbot", enabled=False)
    assert bot_launchd.enabled_of("twinbot") is False


def test_enabled_of_同名且新系统那份启用_回是(roots):
    legacy, dsh = roots
    _write(legacy, "twinbot", enabled=True)
    _write(dsh, "twinbot", enabled=True)
    assert bot_launchd.enabled_of("twinbot") is True


def test_enabled_of_新系统根被关掉_原先放在那里的bot回否(roots, monkeypatch):
    _legacy, dsh = roots
    _write(dsh, "gatebot")
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", "off")
    assert bot_launchd.enabled_of("gatebot") is False


# ------------------------------------------------------------ 重启的两处：同一句判据（严格口径）

def _entries(*ids):
    return [{"id": i, "display_name": i} for i in ids]


def test_严格口径_只留来源明确是新系统的(roots):
    legacy, dsh = roots
    _write(legacy, "oldbot")
    _write(dsh, "gatebot")
    _write(legacy, "twinbot", enabled=True)
    _write(dsh, "twinbot", enabled=False)           # 生效的是旧系统那份
    got = bots_client.hub_visible(bots_client.list_bots(), bots_client.bot_sources(), strict=True)
    assert [e["id"] for e in got] == ["gatebot"]


def test_严格口径_来源读不出时为空_显示用的兜底不变(roots):
    es = _entries("gatebot", "oldbot")
    assert bots_client.hub_visible(es, {}, strict=True) == []
    assert bots_client.hub_visible(es, {}) == es


def test_严格口径_新系统根不在场时为空_显示用的兜底不变(roots, monkeypatch):
    legacy, _dsh = roots
    _write(legacy, "oldbot")
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", "off")
    es, src = bots_client.list_bots(), bots_client.bot_sources()
    assert [e["id"] for e in bots_client.hub_visible(es, src)] == ["oldbot"]
    assert bots_client.hub_visible(es, src, strict=True) == []


def test_严格口径_名单注入时为空_显示用的兜底不变(roots, monkeypatch, tmp_path):
    _legacy, dsh = roots
    _write(dsh, "gatebot")                          # 新系统根下明明有它的启用配置
    p = tmp_path / "bots.json"
    p.write_text('[{"id": "gatebot"}]', encoding="utf-8")
    monkeypatch.setenv("HUB_BOTS_FILE", str(p))
    es = bots_client.list_bots()
    assert [e["id"] for e in es] == ["gatebot"]
    assert bots_client.hub_visible(es, strict=True) == []       # 来源现取：注入时为空
    assert bots_client.hub_visible(es) == es


@pytest.fixture
def client(roots):
    from flask import Flask

    from moments.hub_routes import hub_bp
    app = Flask(__name__)
    app.register_blueprint(hub_bp)
    app.config["TESTING"] = True
    return app.test_client()


@pytest.mark.parametrize("bot_id", ["oldbot", "twinbot", "ghostbot"])
def test_单只重启_没过闸的404_与开关编辑逐字同一句(roots, client, bot_id):
    """旧系统的、同名且生效的是旧系统那份的、不存在的：同一格，同一句话。"""
    legacy, dsh = roots
    _write(legacy, "oldbot")
    _write(legacy, "twinbot", enabled=True)
    _write(dsh, "twinbot", enabled=False)
    r = client.post("/hub/api/bots/%s/restart" % bot_id)
    assert r.status_code == 404
    assert r.get_json() == {"error": "bot_not_found", "detail": hub_addbot.GATE_404_DETAIL}
