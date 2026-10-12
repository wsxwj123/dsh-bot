# -*- coding: utf-8 -*-
"""§4b 开关的放行闸（接口约定第 6 稿）：只对新系统的 bot 开放。

依据 `INTERFACE-新系统bot运维.md` §0 第 2 条、§4b 第 3 格「逐类写清」那张表、§9。
用户的硬规则（BRIEF 2026-10-12 追加第 1 条）：旧系统的 bot 一律不许碰 ——
不往它的配置文件里写任何东西，不产生任何进程动作，不产生后台任务。

三组：
- 被拒的每一类各一条，同时断言四件事（404 与错误体、两边配置文件逐字节没变、
  假 launchctl 零调用、没有后台任务）；
- 过闸的每一类各一条；
- 判定顺序：形状错先于放行闸，放行闸先于文件错。

同名时同步往哪个方向走，在 test_enabled_direction.py。
"""
import json
import os

import pytest

from . import botops_util as u

OFF_VALUES = ["off", "-", "none", "0", "false", "no"]       # §1：off 一族


def _post(client, bot_id, enabled):
    return client.post("/hub/api/bots/%s/enabled" % bot_id, json={"enabled": enabled})


def _legacy(sandbox, bot_id, enabled=True):
    return u.write_bot_yml(sandbox.legacy_cfg, bot_id, display_name="旧" + bot_id,
                           dispatcher_port=17899, enabled=enabled)


def _dsh(sandbox, bot_id, enabled=True):
    return u.write_bot_yml(sandbox.dsh_cfg, bot_id, display_name="新" + bot_id,
                           dispatcher_port=17951, enabled=enabled)


def _broken(dirpath, bot_id):
    p = dirpath / ("%s.yml" % bot_id)
    p.write_text("id: [oops\n  x: : :\n", encoding="utf-8")
    return p


def _inject(sandbox, monkeypatch, entries):
    """名单注入（§1 的 HUB_BOTS_FILE）。"""
    p = sandbox.tmp / "bots.json"
    p.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("HUB_BOTS_FILE", str(p))
    return p


@pytest.fixture(autouse=True)
def _short_wait(monkeypatch):
    """重启等待上限（§1：45 秒，模块常量、测试可改）收到 3 秒。

    本文件大半是「应当被拒」的用例。功能还没按第 6 稿实现时，被测代码可能把它们放进去、
    起一个等心跳的同步任务；不收紧的话，每条红用例收尾都要陪它等满 45 秒。
    """
    u.tighten_constant(monkeypatch, value=45, new=3.0)


# ============================================================ 被拒：逐类（§4b 第 3 格那张表）

@pytest.mark.parametrize("enabled", [False, True], ids=["disable", "enable"])   # 请求停用 / 请求启用
def test_闸_只在旧系统配置根下有配置的bot_被拒且四件事成立(
        client, sandbox, launchctl, as_darwin, monkeypatch, enabled):
    """用户原话那四只（都固定在旧系统）的情形。第 5 稿会把停用标记写进它的配置文件。"""
    _legacy(sandbox, "oldonly", enabled=not enabled)
    frozen = u.freeze_roots(sandbox.legacy_cfg, sandbox.dsh_cfg)
    r = _post(client, "oldonly", enabled)
    u.assert_gate_rejected(r, frozen, launchctl, client, monkeypatch, sandbox.tmp)


@pytest.mark.parametrize("enabled", [False, True], ids=["disable", "enable"])   # 请求停用 / 请求启用
def test_闸_新旧同名_新系统那份停着旧系统那份启用_被拒且四件事成立(
        client, sandbox, launchctl, as_darwin, monkeypatch, enabled):
    """生效的是旧系统那份 → 按旧系统的 bot 算，新系统那份也不许写。"""
    _dsh(sandbox, "twin", enabled=False)
    _legacy(sandbox, "twin", enabled=True)
    frozen = u.freeze_roots(sandbox.legacy_cfg, sandbox.dsh_cfg)
    r = _post(client, "twin", enabled)
    u.assert_gate_rejected(r, frozen, launchctl, client, monkeypatch, sandbox.tmp)


@pytest.mark.parametrize("in_list", [True, False], ids=["listed", "unlisted"])        # 名单里有 / 没有它
@pytest.mark.parametrize("has_file", [True, False], ids=["has-file", "no-file"])      # 新系统根下有 / 没有它的文件
def test_闸_设了名单注入_被拒且四件事成立(
        client, sandbox, launchctl, as_darwin, monkeypatch, in_list, has_file):
    """§4b：设了 HUB_BOTS_FILE 就是 404，不管名单里有没有它、新系统配置根下有没有它的文件。"""
    if has_file:
        _dsh(sandbox, "listed", enabled=True)
    _dsh(sandbox, "bystander", enabled=True)
    entries = [{"id": "bystander", "display_name": "路人"}]
    if in_list:
        entries.append({"id": "listed", "display_name": "注入的"})
    _inject(sandbox, monkeypatch, entries)
    frozen = u.freeze_roots(sandbox.legacy_cfg, sandbox.dsh_cfg)
    r = _post(client, "listed", False)
    u.assert_gate_rejected(r, frozen, launchctl, client, monkeypatch, sandbox.tmp)


@pytest.mark.parametrize("off", OFF_VALUES)
def test_闸_新系统配置根被关掉_对原先放在那里的bot也被拒且四件事成立(
        client, sandbox, launchctl, as_darwin, monkeypatch, off):
    """§1：HUB_CONFIGS_DSH_DIR 设成 off 一族 = 关掉。关掉之后没有任何 bot 过得了闸。"""
    _dsh(sandbox, "parked", enabled=True)          # 文件还躺在原来那个目录里，但那里已不算配置根
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", off)
    frozen = u.freeze_roots(sandbox.legacy_cfg, sandbox.dsh_cfg)
    r = _post(client, "parked", False)
    u.assert_gate_rejected(r, frozen, launchctl, client, monkeypatch, sandbox.tmp)


def test_闸_新系统配置根被关掉_旧系统的bot同样被拒且四件事成立(
        client, sandbox, launchctl, as_darwin, monkeypatch):
    """只有一套配置根的环境：第 5 稿会照样把停用写进旧系统那份。"""
    _legacy(sandbox, "oldonly", enabled=True)
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", "off")
    frozen = u.freeze_roots(sandbox.legacy_cfg, sandbox.dsh_cfg)
    r = _post(client, "oldonly", False)
    u.assert_gate_rejected(r, frozen, launchctl, client, monkeypatch, sandbox.tmp)


def test_闸_新系统配置根目录不在场_被拒且四件事成立_目录也没被建出来(
        client, sandbox, launchctl, as_darwin, monkeypatch):
    missing = sandbox.tmp / "no-such-dsh-configs"
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(missing))
    _legacy(sandbox, "oldonly", enabled=True)
    frozen = u.freeze_roots(sandbox.legacy_cfg, sandbox.dsh_cfg, missing)
    r = _post(client, "oldonly", False)
    u.assert_gate_rejected(r, frozen, launchctl, client, monkeypatch, sandbox.tmp)
    assert not missing.exists(), "不在场的新系统配置根被建出来了"


def test_闸_来源读不出且新系统根下没有它的文件_被拒且四件事成立(
        client, sandbox, launchctl, as_darwin, monkeypatch):
    """旧系统配置根下那份是坏的（来源判不出），新系统那边没有它 → 404，不是 409。"""
    _broken(sandbox.legacy_cfg, "brokenold")
    frozen = u.freeze_roots(sandbox.legacy_cfg, sandbox.dsh_cfg)
    r = _post(client, "brokenold", False)
    u.assert_gate_rejected(r, frozen, launchctl, client, monkeypatch, sandbox.tmp)


def test_闸_两套配置根下都没有这个bot_被拒且四件事成立(
        client, sandbox, launchctl, as_darwin, monkeypatch):
    _dsh(sandbox, "bystander", enabled=True)
    _legacy(sandbox, "otherold", enabled=True)
    frozen = u.freeze_roots(sandbox.legacy_cfg, sandbox.dsh_cfg)
    r = _post(client, "ghostbot", False)
    u.assert_gate_rejected(r, frozen, launchctl, client, monkeypatch, sandbox.tmp)


def test_闸_新系统那份解析不出且旧系统根下有同名文件_被拒且四件事成立(
        client, sandbox, launchctl, as_darwin, monkeypatch):
    """「坏文件」补充口不适用于有同名旧文件的情形：回 404，不是 409 bad_yaml。"""
    _broken(sandbox.dsh_cfg, "twin")
    _legacy(sandbox, "twin", enabled=True)
    frozen = u.freeze_roots(sandbox.legacy_cfg, sandbox.dsh_cfg)
    r = _post(client, "twin", False)
    u.assert_gate_rejected(r, frozen, launchctl, client, monkeypatch, sandbox.tmp)


def test_闸_新旧两份都解析不出_判不出归属_被拒且四件事成立(
        client, sandbox, launchctl, as_darwin, monkeypatch):
    """§0 第 2 条：两份都坏、判不出归属的也不过闸。"""
    _broken(sandbox.dsh_cfg, "twin")
    _broken(sandbox.legacy_cfg, "twin")
    frozen = u.freeze_roots(sandbox.legacy_cfg, sandbox.dsh_cfg)
    r = _post(client, "twin", False)
    u.assert_gate_rejected(r, frozen, launchctl, client, monkeypatch, sandbox.tmp)


def test_闸_旧系统的bot_设了覆盖命令也不执行_互斥没被占(
        client, sandbox, launchctl, as_darwin, monkeypatch):
    """§4b：旧系统的 bot 被拒时不登记 job，「设了 HUB_RESTART_CMD 也一样」。

    覆盖命令一启动就写标记、再睡 1.5 秒占着互斥：要是被拒的请求偷偷起了任务，
    要么互斥探测拿到 409，要么标记文件已经在了，两头堵。
    """
    _legacy(sandbox, "oldonly", enabled=True)
    marker = sandbox.tmp / "override-ran.marker"
    monkeypatch.setenv("BOTOPS_MARKER", str(marker))
    monkeypatch.setenv("HUB_RESTART_CMD", u.python_cmd(
        "import os,time;open(os.environ['BOTOPS_MARKER'],'w').write('ran');time.sleep(1.5)"))
    frozen = u.freeze_roots(sandbox.legacy_cfg, sandbox.dsh_cfg)
    r = _post(client, "oldonly", False)
    assert r.status_code == 404, (r.status_code, r.get_json())
    assert r.get_json() == {"error": "bot_not_found", "detail": u.GATE_404_DETAIL}, r.get_json()
    u.assert_no_job_left(client, monkeypatch, sandbox.tmp)
    assert not marker.exists(), "旧系统的 bot 被拒了，覆盖命令却还是跑了"
    u.assert_roots_untouched(frozen)
    assert launchctl.calls() == [], launchctl.calls()


# ============================================================ sync.reason 删掉的两档到不了（§9 表后一句）

def test_闸_非macOS上旧系统的bot_也是404_不再是skipped_legacy(
        client, sandbox, launchctl, as_linux, monkeypatch):
    _legacy(sandbox, "oldonly", enabled=True)
    frozen = u.freeze_roots(sandbox.legacy_cfg, sandbox.dsh_cfg)
    r = _post(client, "oldonly", False)
    u.assert_gate_rejected(r, frozen, launchctl, client, monkeypatch, sandbox.tmp)


def test_闸_非macOS上新系统根关掉_也是404_不再是skipped_no_dsh_root(
        client, sandbox, launchctl, as_linux, monkeypatch):
    _dsh(sandbox, "parked", enabled=True)
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", "off")
    frozen = u.freeze_roots(sandbox.legacy_cfg, sandbox.dsh_cfg)
    r = _post(client, "parked", False)
    u.assert_gate_rejected(r, frozen, launchctl, client, monkeypatch, sandbox.tmp)


# ============================================================ 过闸：逐类

def test_过闸_只在新系统配置根下的bot_200且旧系统配置根一个字节不动(
        client, sandbox, launchctl, as_darwin):
    yml = _dsh(sandbox, "newonly", enabled=True)
    _legacy(sandbox, "otherold", enabled=True)              # 旁边一只不相干的旧系统 bot
    frozen_legacy = u.freeze_roots(sandbox.legacy_cfg)
    r = _post(client, "newonly", False)
    assert r.status_code == 200, r.get_json()
    st = u.finish_sync(client, r.get_json())
    assert st is not None and st["state"] == "done", (r.get_json(), st)
    assert u.read_yaml(yml)["enabled"] is False
    u.assert_roots_untouched(frozen_legacy)


@pytest.mark.parametrize("legacy_enabled", [True, False], ids=["legacy-on", "legacy-off"])   # 旧系统那份启用 / 停着
def test_过闸_新旧同名_新系统那份启用_写的是新系统那份(
        client, sandbox, launchctl, as_darwin, legacy_enabled):
    """§4b 表：新系统那份启用 → 过闸，旧系统那份启用或停着都一样。"""
    new = _dsh(sandbox, "twin", enabled=True)
    _legacy(sandbox, "twin", enabled=legacy_enabled)
    frozen_legacy = u.freeze_roots(sandbox.legacy_cfg)
    r = _post(client, "twin", False)
    assert r.status_code == 200, r.get_json()
    u.finish_sync(client, r.get_json())
    assert u.read_yaml(new)["enabled"] is False, new.read_text(encoding="utf-8")
    u.assert_roots_untouched(frozen_legacy)


def test_过闸_新旧同名_两份都停着_生效的是新系统那份_写的是新系统那份(
        client, sandbox, heartbeat, launchctl, as_darwin):
    new = _dsh(sandbox, "twin", enabled=False)
    _legacy(sandbox, "twin", enabled=False)
    launchctl.set_default(installed=True, running=False, pid=4242)
    heartbeat.tick("twin", pid=4242)
    frozen_legacy = u.freeze_roots(sandbox.legacy_cfg)
    r = _post(client, "twin", True)
    assert r.status_code == 200, r.get_json()
    u.finish_sync(client, r.get_json())
    assert u.read_yaml(new)["enabled"] is True, new.read_text(encoding="utf-8")
    u.assert_roots_untouched(frozen_legacy)


def test_过闸_新系统那份解析不出且旧系统根下没有同名文件_409_bad_yaml_零调用(
        client, sandbox, launchctl, as_darwin, monkeypatch):
    """「坏文件」补充口：只在新系统配置根下有文件 → 放它走到读文件那一步，落第 4 格。"""
    _broken(sandbox.dsh_cfg, "brokennew")
    frozen = u.freeze_roots(sandbox.legacy_cfg, sandbox.dsh_cfg)
    r = _post(client, "brokennew", False)
    assert r.status_code == 409, (r.status_code, r.get_json())
    assert r.get_json()["error"] == "bad_yaml", r.get_json()
    u.assert_roots_untouched(frozen)
    assert launchctl.calls() == [], launchctl.calls()
    u.assert_no_job_left(client, monkeypatch, sandbox.tmp)


# ============================================================ 判定顺序（§4b 那张带「序」的表）

@pytest.mark.parametrize("body", [{"enabled": "no"}, {}, {"enabled": 1}],
                         ids=["str", "missing", "int"])
def test_判序_enabled不是布尔先于放行闸_旧系统的bot也回400且文件不动(
        client, sandbox, launchctl, as_darwin, body):
    """序 1 先于序 3：形状错是用户自己能改的，先报它。"""
    _legacy(sandbox, "oldonly", enabled=True)
    frozen = u.freeze_roots(sandbox.legacy_cfg, sandbox.dsh_cfg)
    r = client.post("/hub/api/bots/oldonly/enabled", json=body)
    assert r.status_code == 400, (r.status_code, r.get_json())
    assert r.get_json() == {"error": "bad_body",
                            "detail": "enabled 必须是布尔值 true / false"}, r.get_json()
    u.assert_roots_untouched(frozen)
    assert launchctl.calls() == []


def test_判序_体不是对象先于放行闸_旧系统的bot也回400且文件不动(
        client, sandbox, launchctl, as_darwin):
    _legacy(sandbox, "oldonly", enabled=True)
    frozen = u.freeze_roots(sandbox.legacy_cfg, sandbox.dsh_cfg)
    r = client.post("/hub/api/bots/oldonly/enabled", data="[1,2,3]",
                    content_type="application/json")
    assert r.status_code == 400, (r.status_code, r.get_json())
    assert r.get_json() == {"error": "bad_body", "detail": "请求体必须是一个 JSON 对象"}, r.get_json()
    u.assert_roots_untouched(frozen)
    assert launchctl.calls() == []


def test_判序_体的错先于id形状的错(client, sandbox, launchctl, as_darwin):
    """序 1 先于序 2：id 非法、体也非法时，报的是 bad_body。"""
    r = client.post("/hub/api/bots/Bad_Id/enabled", json={"enabled": "no"})
    assert r.status_code == 400, (r.status_code, r.get_json())
    assert r.get_json()["error"] == "bad_body", r.get_json()


def test_判序_id形状非法_400且detail逐字(client, sandbox, launchctl, as_darwin):
    r = client.post("/hub/api/bots/Bad_Id/enabled", json={"enabled": False})
    assert r.status_code == 400, (r.status_code, r.get_json())
    assert r.get_json() == {"error": "bad_bot_id", "detail": "bot id 形状非法"}, r.get_json()
    assert launchctl.calls() == []


@pytest.mark.skipif(os.name == "nt", reason="Windows 上 chmod 0 不阻止读取")
def test_判序_放行闸先于文件错_旧系统那份读不出来_是404不是500(
        client, sandbox, launchctl, as_darwin, monkeypatch):
    """§4b：归属要在读写这只 bot 的任何配置文件之前判完。`config_unreadable` 只属于新系统那份。"""
    yml = _legacy(sandbox, "oldonly", enabled=True)
    os.chmod(yml, 0)
    try:
        frozen = u.freeze_roots(sandbox.legacy_cfg, sandbox.dsh_cfg)
        r = _post(client, "oldonly", False)
        u.assert_gate_rejected(r, frozen, launchctl, client, monkeypatch, sandbox.tmp)
    finally:
        os.chmod(yml, 0o600)


# ============================================================ detail 是同一句话

def test_闸_开关与编辑的404_detail逐字相同(web_client, sandbox, launchctl, as_darwin):
    """§4b：detail 文案与 §10 的 404 逐字相同（同一道闸、同一句话）。"""
    _legacy(sandbox, "oldonly", enabled=True)
    toggle = web_client.post("/hub/api/bots/oldonly/enabled", json={"enabled": False})
    edit_get = web_client.get("/hub/api/bots/oldonly/edit")
    edit_post = web_client.post("/hub/api/bots/oldonly/edit", json={"persona": "x"})
    got = [(r.status_code, (r.get_json() or {}).get("error"), (r.get_json() or {}).get("detail"))
           for r in (toggle, edit_get, edit_post)]
    assert got == [(404, "bot_not_found", u.GATE_404_DETAIL)] * 3, got
