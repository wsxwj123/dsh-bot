# -*- coding: utf-8 -*-
"""§10 bot 二次编辑：与开关共用同一道放行闸（接口约定第 6 稿收紧的那一格）。

依据 `INTERFACE-新系统bot运维.md` §10 首段与落盘表第一格：
- 「坏文件」补充口同样适用 —— `bad_yaml` 只在旧系统配置根下没有同名文件时才报得出来，
  有同名文件时回 404；
- display_name 写的 yml 恒为新系统配置根下那份，直接由新系统配置根拼出，
  不再经会回落到旧系统那份的定位。

既有的 404（旧系统的 bot、名单注入、不存在）与 409（坏 yml、旧根下没有同名文件）在
test_edit_bot.py，这里只补第 6 稿新说清的同名情形。
"""
import sqlite3

import pytest

from . import botops_util as u


def _url(bot_id):
    return "/hub/api/bots/%s/edit" % bot_id


def _legacy(sandbox, bot_id, enabled=True, display_name="旧名"):
    return u.write_bot_yml(sandbox.legacy_cfg, bot_id, display_name=display_name,
                           dispatcher_port=17899, enabled=enabled)


def _dsh(sandbox, bot_id, enabled=True, display_name="新名"):
    (sandbox.dsh_home / "bots" / bot_id / "channel").mkdir(parents=True, exist_ok=True)
    return u.write_bot_yml(sandbox.dsh_cfg, bot_id, display_name=display_name,
                           dispatcher_port=17951, enabled=enabled)


def _broken(dirpath, bot_id):
    p = dirpath / ("%s.yml" % bot_id)
    p.write_text("id: [oops\n  x: : :\n", encoding="utf-8")
    return p


def _profile_names(db):
    """朋友圈库里现有的显示名（§10 落盘表第二处：`bot_profiles.display_name`）。"""
    con = sqlite3.connect(str(db.DB_PATH))
    try:
        return [r[0] for r in con.execute("SELECT display_name FROM bot_profiles")]
    finally:
        con.close()


def _assert_404(r):
    assert r.status_code == 404, (r.status_code, r.get_json())
    assert r.get_json() == {"error": "bot_not_found", "detail": u.GATE_404_DETAIL}, r.get_json()


# ============================================================ 收紧的那一格：坏文件 + 同名旧文件

def test_GET_新系统那份解析不出且旧系统根下有同名文件_404不是bad_yaml(web_client, sandbox):
    _broken(sandbox.dsh_cfg, "twin")
    _legacy(sandbox, "twin")
    _assert_404(web_client.get(_url("twin")))


def test_POST_新系统那份解析不出且旧系统根下有同名文件_404且两边字节不变库不写(
        web_client, sandbox, botlife_db):
    _broken(sandbox.dsh_cfg, "twin")
    _legacy(sandbox, "twin")
    frozen = u.freeze_roots(sandbox.legacy_cfg, sandbox.dsh_cfg)
    r = web_client.post(_url("twin"), json={"display_name": "想改成这个"})
    _assert_404(r)
    u.assert_roots_untouched(frozen)
    assert "想改成这个" not in _profile_names(botlife_db), "被拒的编辑写进了朋友圈库"


def test_GET_新旧两份都解析不出_404不是bad_yaml(web_client, sandbox):
    _broken(sandbox.dsh_cfg, "twin")
    _broken(sandbox.legacy_cfg, "twin")
    _assert_404(web_client.get(_url("twin")))


# ============================================================ 同名：生效的是旧系统那份 → 不过闸

def test_GET_同名且新系统那份停着旧系统那份启用_404(web_client, sandbox):
    _dsh(sandbox, "twin", enabled=False)
    _legacy(sandbox, "twin", enabled=True)
    _assert_404(web_client.get(_url("twin")))


def test_POST_同名且新系统那份停着旧系统那份启用_404且两边字节不变库不写(
        web_client, sandbox, botlife_db):
    _dsh(sandbox, "twin", enabled=False)
    _legacy(sandbox, "twin", enabled=True)
    frozen = u.freeze_roots(sandbox.legacy_cfg, sandbox.dsh_cfg)
    r = web_client.post(_url("twin"), json={"display_name": "想改成这个"})
    _assert_404(r)
    u.assert_roots_untouched(frozen)
    assert "想改成这个" not in _profile_names(botlife_db), "被拒的编辑写进了朋友圈库"


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_新系统配置根被关掉_编辑一律404且原目录里的文件不动(
        web_client, sandbox, monkeypatch, method):
    _dsh(sandbox, "parked")
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", "off")
    frozen = u.freeze_roots(sandbox.legacy_cfg, sandbox.dsh_cfg)
    if method == "GET":
        r = web_client.get(_url("parked"))
    else:
        r = web_client.post(_url("parked"), json={"display_name": "想改成这个"})
    _assert_404(r)
    u.assert_roots_untouched(frozen)


# ============================================================ 同名：过闸时读写的都是新系统那份

@pytest.mark.parametrize("dsh_enabled,legacy_enabled", [(True, True), (True, False), (False, False)],
                         ids=["both-on", "new-on-old-off", "both-off"])
def test_POST_同名且过闸_名字写进新系统那份_旧系统那份字节不变(
        web_client, sandbox, botlife_db, dsh_enabled, legacy_enabled):
    new = _dsh(sandbox, "twin", enabled=dsh_enabled, display_name="新系统里的旧名")
    _legacy(sandbox, "twin", enabled=legacy_enabled, display_name="旧系统里的名字")
    frozen_legacy = u.freeze_roots(sandbox.legacy_cfg)
    r = web_client.post(_url("twin"), json={"display_name": "改后的名字"})
    assert r.status_code == 200, (r.status_code, r.get_json())
    assert u.read_yaml(new)["display_name"] == "改后的名字", new.read_text(encoding="utf-8")
    u.assert_roots_untouched(frozen_legacy)


@pytest.mark.parametrize("dsh_enabled,legacy_enabled", [(True, True), (True, False), (False, False)],
                         ids=["both-on", "new-on-old-off", "both-off"])
def test_GET_同名且过闸_读的是新系统那份的名字(
        web_client, sandbox, dsh_enabled, legacy_enabled):
    _dsh(sandbox, "twin", enabled=dsh_enabled, display_name="新系统里的名字")
    _legacy(sandbox, "twin", enabled=legacy_enabled, display_name="旧系统里的名字")
    r = web_client.get(_url("twin"))
    assert r.status_code == 200, (r.status_code, r.get_json())
    assert r.get_json()["display_name"] == "新系统里的名字", r.get_json()
