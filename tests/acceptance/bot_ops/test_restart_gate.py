# -*- coding: utf-8 -*-
"""§3 重启全部、§4 单只重启：没过放行闸的 bot 一律不碰（接口约定第 6 稿）。

依据 `INTERFACE-新系统bot运维.md` §0 第 2 条、§3 首段（执行范围 = 过放行闸的那些；
来源读不出含名单注入时范围为空，一条 launchctl 都不发）、§4 的 404 一格（旧系统的 bot、
来源读不出的、名单注入的、新系统根不在场时的、不存在的都走这一格；后三类此前会漏到
下面几格）。
"""
import json

import pytest

from . import botops_util as u


def _restart_all(client):
    return client.post("/hub/api/bots/restart")


def _restart_one(client, bot_id):
    return client.post("/hub/api/bots/%s/restart" % bot_id)


def _legacy(sandbox, bot_id, enabled=True):
    return u.write_bot_yml(sandbox.legacy_cfg, bot_id, display_name="旧" + bot_id,
                           dispatcher_port=17899, enabled=enabled)


def _dsh(sandbox, bot_id, enabled=True):
    return u.write_bot_yml(sandbox.dsh_cfg, bot_id, display_name="新" + bot_id,
                           dispatcher_port=17951, enabled=enabled)


def _inject(sandbox, monkeypatch, ids):
    p = sandbox.tmp / "bots.json"
    p.write_text(json.dumps([{"id": i, "display_name": i} for i in ids]), encoding="utf-8")
    monkeypatch.setenv("HUB_BOTS_FILE", str(p))


def _assert_not_found(r, launchctl, client, monkeypatch, tmp):
    """§4 的 404 一格：错误码、detail 点明只管新系统、零 launchctl、没有后台任务。"""
    assert r.status_code == 404, (r.status_code, r.get_data(as_text=True)[:200])
    body = r.get_json()
    assert body["error"] == "bot_not_found", body
    assert "新系统" in (body.get("detail") or ""), body
    assert "job_id" not in body, body
    assert launchctl.calls() == [], ("404 了还调了 launchctl", launchctl.calls())
    u.assert_no_job_left(client, monkeypatch, tmp)
    assert launchctl.calls() == [], launchctl.calls()


@pytest.fixture(autouse=True)
def _short_wait(monkeypatch):
    """重启等待上限（§1：45 秒，测试可改）收到 3 秒：被测代码把不该碰的 bot 放进去等心跳时，
    红用例不用陪着等满。"""
    u.tighten_constant(monkeypatch, value=45, new=3.0)


# ============================================================ §3 重启全部：执行范围

def test_重启全部_名单注入_范围为空_直接done且一条launchctl都不发(
        client, sandbox, launchctl, as_darwin, monkeypatch):
    """名单注入时来源判定为空 → 没有 bot 过闸 → 范围为空（第 6 稿）。

    新系统配置根下明明有这两只的启用配置，注入的名单里也有它们，照样一条不发。
    """
    _dsh(sandbox, "alpha")
    _dsh(sandbox, "beta", enabled=False)
    _inject(sandbox, monkeypatch, ["alpha", "beta"])
    r = _restart_all(client)
    assert r.status_code == 202, (r.status_code, r.get_json())
    st = u.wait_job(client, r.get_json()["job_id"])
    assert launchctl.calls() == [], ("名单注入时不该发任何 launchctl", launchctl.calls())
    assert st["tail"].strip() == "没有可重启的 bot", st["tail"]
    assert st["state"] == "done", st
    assert st["exit_code"] is None, st


def test_重启全部_名单注入_设了覆盖命令_照旧执行那条命令(client, sandbox, monkeypatch):
    """§3：设了 HUB_RESTART_CMD 时整段换成执行该命令，不看执行范围。"""
    _dsh(sandbox, "alpha")
    _inject(sandbox, monkeypatch, ["alpha"])
    marker = sandbox.tmp / "ran.txt"
    monkeypatch.setenv("BOTOPS_MARKER", str(marker))
    monkeypatch.setenv("HUB_RESTART_CMD", u.python_cmd(
        "import os;open(os.environ['BOTOPS_MARKER'],'w').write('ran')"))
    r = _restart_all(client)
    assert r.status_code == 202, (r.status_code, r.get_json())
    st = u.wait_job(client, r.get_json()["job_id"])
    assert st["state"] == "done", st
    assert marker.read_text(encoding="utf-8") == "ran"


def test_重启全部_同名且生效的是旧系统那份_这只一条launchctl都不沾(
        client, sandbox, heartbeat, launchctl, as_darwin):
    """新系统那份停着、旧系统那份启用 → 生效的是旧系统那份，不过闸，不在执行范围里。

    第 5 稿会照新系统那份的「停用」去对它发 bootout。旁边一只正常的新系统 bot 照常重启。
    """
    _dsh(sandbox, "twin", enabled=False)
    _legacy(sandbox, "twin", enabled=True)
    _dsh(sandbox, "alpha")
    launchctl.set_default(installed=True, running=False, pid=4242)
    heartbeat.tick("alpha", pid=4242)
    frozen = u.freeze_roots(sandbox.legacy_cfg, sandbox.dsh_cfg)
    r = _restart_all(client)
    assert r.status_code == 202, (r.status_code, r.get_json())
    st = u.wait_job(client, r.get_json()["job_id"])
    assert set(launchctl.labels()) == {"com.dsh-bot.alpha"}, launchctl.calls()
    assert st["tail"].strip().splitlines()[0] == "重启 1 只：成功 1", st["tail"]
    assert st["state"] == "done", st
    u.assert_roots_untouched(frozen)


def test_重启全部_同名且两份都停着_生效的是新系统那份_对它bootout(
        client, sandbox, launchctl, as_darwin):
    _dsh(sandbox, "twin", enabled=False)
    _legacy(sandbox, "twin", enabled=False)
    launchctl.set_default(installed=True, running=True, pid=4242)
    r = _restart_all(client)
    assert r.status_code == 202, (r.status_code, r.get_json())
    st = u.wait_job(client, r.get_json()["job_id"])
    assert st["state"] == "done", st
    assert launchctl.subcommands() == ["bootout"], launchctl.calls()
    assert launchctl.labels() == ["com.dsh-bot.twin"], launchctl.calls()


# ============================================================ §4 单只重启：404 一格

@pytest.mark.parametrize("has_file", [True, False], ids=["has-file", "no-file"])
def test_单只重启_名单注入_404且零调用零任务(
        client, sandbox, launchctl, as_darwin, monkeypatch, has_file):
    """名单里有它、（has-file 时）新系统配置根下也有它的启用配置，照样 404（第 6 稿）。"""
    if has_file:
        _dsh(sandbox, "listed")
    _inject(sandbox, monkeypatch, ["listed"])
    r = _restart_one(client, "listed")
    _assert_not_found(r, launchctl, client, monkeypatch, sandbox.tmp)


def test_单只重启_新系统根被关掉_旧系统的bot_404不是restart_unsupported(
        client, sandbox, launchctl, as_darwin, monkeypatch):
    """404 这一格排在 409 restart_unsupported 之前：根关掉时不该漏到下面那格。"""
    _legacy(sandbox, "oldonly")
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", "off")
    r = _restart_one(client, "oldonly")
    _assert_not_found(r, launchctl, client, monkeypatch, sandbox.tmp)


def test_单只重启_新系统根被关掉_原先放在那里的bot_404(
        client, sandbox, launchctl, as_darwin, monkeypatch):
    _dsh(sandbox, "parked")
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", "off")
    r = _restart_one(client, "parked")
    _assert_not_found(r, launchctl, client, monkeypatch, sandbox.tmp)


def test_单只重启_新系统根目录不在场_404不是restart_unsupported(
        client, sandbox, launchctl, as_darwin, monkeypatch):
    _legacy(sandbox, "oldonly")
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(sandbox.tmp / "no-such-dsh-configs"))
    r = _restart_one(client, "oldonly")
    _assert_not_found(r, launchctl, client, monkeypatch, sandbox.tmp)


def test_单只重启_非macOS上旧系统的bot_404不是restart_unsupported(
        client, sandbox, launchctl, as_linux, monkeypatch):
    _legacy(sandbox, "oldonly")
    r = _restart_one(client, "oldonly")
    _assert_not_found(r, launchctl, client, monkeypatch, sandbox.tmp)


def test_单只重启_同名且生效的是旧系统那份_404不是bot_disabled(
        client, sandbox, launchctl, as_darwin, monkeypatch):
    """新系统那份停着、旧系统那份启用：它是旧系统的 bot，回的不是「先启用再重启」。"""
    _dsh(sandbox, "twin", enabled=False)
    _legacy(sandbox, "twin", enabled=True)
    r = _restart_one(client, "twin")
    _assert_not_found(r, launchctl, client, monkeypatch, sandbox.tmp)


def test_单只重启_新系统那份解析不出_来源读不出_404(
        client, sandbox, launchctl, as_darwin, monkeypatch):
    """§4 没有「坏文件」补充口（那个口只为 §4b 与 §10）：来源读不出就是 404。"""
    (sandbox.dsh_cfg / "brokennew.yml").write_text("id: [oops\n  x: : :\n", encoding="utf-8")
    r = _restart_one(client, "brokennew")
    _assert_not_found(r, launchctl, client, monkeypatch, sandbox.tmp)


def test_单只重启_旧系统的bot_设了覆盖命令也不执行(
        client, sandbox, launchctl, as_darwin, monkeypatch):
    """§4 的 404：不产生任何 launchctl 或其它子进程调用。"""
    _legacy(sandbox, "oldonly")
    marker = sandbox.tmp / "override-ran.marker"
    monkeypatch.setenv("BOTOPS_MARKER", str(marker))
    monkeypatch.setenv("HUB_RESTART_CMD", u.python_cmd(
        "import os,time;open(os.environ['BOTOPS_MARKER'],'w').write('ran');time.sleep(1.5)"))
    r = _restart_one(client, "oldonly")
    assert r.status_code == 404, (r.status_code, r.get_json())
    assert r.get_json()["error"] == "bot_not_found", r.get_json()
    u.assert_no_job_left(client, monkeypatch, sandbox.tmp)
    assert not marker.exists(), "旧系统的 bot 被拒了，覆盖命令却还是跑了"
    assert launchctl.calls() == [], launchctl.calls()


# ============================================================ §4 单只重启：过闸的同名情形

def test_单只重启_同名且两份都停着_过闸_409_bot_disabled(
        client, sandbox, launchctl, as_darwin):
    """两份都停着 → 生效的是新系统那份 → 过闸，往下走到「停用中」那一格。"""
    _dsh(sandbox, "twin", enabled=False)
    _legacy(sandbox, "twin", enabled=False)
    r = _restart_one(client, "twin")
    assert r.status_code == 409, (r.status_code, r.get_json())
    assert r.get_json()["error"] == "bot_disabled", r.get_json()
    assert launchctl.calls() == [], launchctl.calls()


def test_单只重启_同名且新系统那份启用_过闸_只动新系统的单元_旧系统那份字节不变(
        client, sandbox, heartbeat, launchctl, as_darwin):
    _dsh(sandbox, "twin", enabled=True)
    _legacy(sandbox, "twin", enabled=True)
    launchctl.set_default(installed=True, running=False, pid=4242)
    heartbeat.tick("twin", pid=4242)
    frozen = u.freeze_roots(sandbox.legacy_cfg, sandbox.dsh_cfg)
    r = _restart_one(client, "twin")
    assert r.status_code == 202, (r.status_code, r.get_json())
    st = u.wait_job(client, r.get_json()["job_id"])
    assert st["state"] == "done", st
    assert set(launchctl.labels()) == {"com.dsh-bot.twin"}, launchctl.calls()
    assert "bootout" not in launchctl.subcommands(), launchctl.calls()
    u.assert_roots_untouched(frozen)
