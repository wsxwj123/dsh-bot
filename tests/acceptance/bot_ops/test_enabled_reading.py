# -*- coding: utf-8 -*-
"""§0 第 8 条（接口约定第 7 稿）：「这只 bot 启用没有」只有一个读法。

依据 `INTERFACE-新系统bot运维.md` §0 第 8 条、§2「启用标注」两例、§3「第 7 稿两点」、
§4 的 `bot_disabled` 一格、§4b「单只同步 job 的执行方向」那一段：

    新系统配置根下 `<bot_id>.yml` **自己**顶层写的 `enabled`，没写就算启用。
    它的 `life_config` 指向的那份文件里写的 `enabled` 不参与。

三处用同一个读法：列表 `disabled` 的标注、重启全部与开关同步的启停方向、单只重启的
`bot_disabled` 判定。接口约定给了正反两例，这里对每一例把三处各量一遍。

    正例：自己不写 `enabled`，`life_config` 那份写 `enabled: false` → 算启用。
    反例：自己写 `enabled: false`，`life_config` 那份写 `enabled: true` → 算停用。

bot 用假名，全程假 launchctl 与假 bun。
"""
import pytest

from . import botops_util as u

BOT = "xinbot"
UNIT = "com.dsh-bot.%s" % BOT


def _with_life(sandbox, *, own, life):
    """造一只带 `life_config` 的新系统 bot。`own` / `life` 取 True、False 或 None（不写这个键）。

    `own` 是新系统那份自己写的 `enabled`，`life` 是 `life_config` 指向的那份写的。
    返回 (新系统那份, life 那份)。
    """
    life_fields = {"display_name": "life里的名字"}
    if life is not None:
        life_fields["enabled"] = life
    life_yml = u.write_bot_yml(sandbox.tmp / "life", BOT, **life_fields)
    fields = {"display_name": "新来的", "dispatcher_port": 17951, "life_config": str(life_yml)}
    if own is not None:
        fields["enabled"] = own
    yml = u.write_bot_yml(sandbox.dsh_cfg, BOT, **fields)
    return yml, life_yml


def _own_enabled_written(yml):
    """新系统那份自己顶层写没写 `enabled`（夹具自检用）。"""
    return any(ln.startswith("enabled:") for ln in yml.read_text(encoding="utf-8").splitlines())


@pytest.fixture(autouse=True)
def _fakes_in_place(launchctl, bun, as_darwin, monkeypatch):
    """假 launchctl、假 bun 都在位；重启等待上限（45 秒，测试可改）收到 3 秒。"""
    u.tighten_constant(monkeypatch, value=45, new=3.0)


@pytest.fixture
def startable(heartbeat, launchctl):
    """启用方向要等活：单元装着、kickstart 之后在跑，心跳一直新鲜且 pid 对得上。"""
    launchctl.set_default(installed=True, running=False, pid=4242)
    heartbeat.tick(BOT, pid=4242)
    return launchctl


def _get_bots(client):
    r = client.get("/hub/api/bots")
    assert r.status_code == 200, (r.status_code, r.get_json(silent=True))
    return r.get_json()


def _restart_all_done(client):
    r = client.post("/hub/api/bots/restart")
    assert r.status_code == 202, (r.status_code, r.get_json(silent=True))
    return u.wait_job(client, r.get_json()["job_id"])


# ============================================================ 正例：自己不写，life 那份写停用 → 算启用

def test_自己不写enabled_life那份写停用_列表里不标停用(client, sandbox, launchctl):
    yml, _ = _with_life(sandbox, own=None, life=False)
    assert not _own_enabled_written(yml), "夹具自检：新系统那份不该写 enabled"
    body = _get_bots(client)
    assert BOT in [b["id"] for b in body["bots"]], ("这只没被列出来，量不了标注", body["bots"])
    assert BOT not in body["disabled"], ("life_config 那份的 enabled 被当成了这只的", body["disabled"])


def test_自己不写enabled_life那份写停用_单只重启被受理并拉起(client, sandbox, startable):
    _with_life(sandbox, own=None, life=False)
    r = client.post("/hub/api/bots/%s/restart" % BOT)
    assert r.status_code == 202, ("不算停用，应当走到受理", r.status_code, r.get_json(silent=True))
    st = u.wait_job(client, r.get_json()["job_id"])
    assert st["state"] == "done", st
    cmds = startable.subcommands()
    assert "kickstart" in cmds and "bootout" not in cmds, startable.calls()


def test_自己不写enabled_life那份写停用_重启全部对它发kickstart不发bootout(
        client, sandbox, startable):
    _with_life(sandbox, own=None, life=False)
    st = _restart_all_done(client)
    cmds = startable.subcommands()
    assert "kickstart" in cmds and "bootout" not in cmds, startable.calls()
    assert set(startable.labels()) == {UNIT}, startable.calls()
    assert "%s 已重启" % BOT in st["tail"], st["tail"]


def test_开关写成启用_life那份写着停用_同步走kickstart不发bootout(client, sandbox, startable):
    """§4b：`life_config` 指向的文件里写的 `enabled` 不参与同步方向。"""
    yml, life_yml = _with_life(sandbox, own=False, life=False)
    life_before = life_yml.read_bytes()
    r = client.post("/hub/api/bots/%s/enabled" % BOT, json={"enabled": True})
    assert r.status_code == 200, (r.status_code, r.get_json(silent=True))
    st = u.finish_sync(client, r.get_json())
    assert st is not None and st["state"] == "done", (r.get_json(), st)
    cmds = startable.subcommands()
    assert "kickstart" in cmds and "bootout" not in cmds, startable.calls()
    assert life_yml.read_bytes() == life_before, "开关不该去写 life_config 那份"


# ============================================================ 反例：自己写停用，life 那份写启用 → 算停用

def test_自己写停用_life那份写启用_列表里标停用(client, sandbox, launchctl):
    yml, _ = _with_life(sandbox, own=False, life=True)
    assert _own_enabled_written(yml), "夹具自检：新系统那份该写着 enabled: false"
    body = _get_bots(client)
    assert BOT in body["disabled"], ("自己写的 enabled: false 没算数", body["disabled"])


def test_自己写停用_life那份写启用_单只重启409_bot_disabled_零调用(client, sandbox, launchctl):
    _with_life(sandbox, own=False, life=True)
    launchctl.set_default(installed=True, running=True, pid=4242)
    r = client.post("/hub/api/bots/%s/restart" % BOT)
    assert r.status_code == 409, (r.status_code, r.get_json(silent=True))
    body = r.get_json()
    assert body["error"] == "bot_disabled", body
    assert "启用" in body["detail"], body
    assert launchctl.calls() == [], ("被拒了还动进程", launchctl.calls())


def test_自己写停用_life那份写启用_重启全部对它发bootout不发kickstart(
        client, sandbox, launchctl):
    _with_life(sandbox, own=False, life=True)
    launchctl.set_default(installed=True, running=True, pid=4242)
    st = _restart_all_done(client)
    assert launchctl.subcommands() == ["bootout"], launchctl.calls()
    assert launchctl.labels() == [UNIT], launchctl.calls()
    assert "%s 已停止" % BOT in st["tail"], st["tail"]


def test_开关写成停用_life那份写着启用_同步走bootout不发kickstart(client, sandbox, launchctl):
    yml, life_yml = _with_life(sandbox, own=True, life=True)
    life_before = life_yml.read_bytes()
    launchctl.set_default(installed=True, running=True, pid=4242)
    r = client.post("/hub/api/bots/%s/enabled" % BOT, json={"enabled": False})
    assert r.status_code == 200, (r.status_code, r.get_json(silent=True))
    st = u.finish_sync(client, r.get_json())
    assert st is not None and st["state"] == "done", (r.get_json(), st)
    assert launchctl.subcommands() == ["bootout"], launchctl.calls()
    assert launchctl.labels() == [UNIT], launchctl.calls()
    assert life_yml.read_bytes() == life_before, "开关不该去写 life_config 那份"


# ============================================================ 边上两例

def test_自己写启用_life那份写停用_列表里不标停用(client, sandbox, launchctl):
    """自己明明白白写了 `enabled: true`，life 那份的 false 更不该盖过它。"""
    _with_life(sandbox, own=True, life=False)
    body = _get_bots(client)
    assert BOT in [b["id"] for b in body["bots"]], body["bots"]
    assert BOT not in body["disabled"], body["disabled"]


def test_两份都不写enabled_算启用_列表里不标停用(client, sandbox, launchctl):
    _with_life(sandbox, own=None, life=None)
    body = _get_bots(client)
    assert BOT in [b["id"] for b in body["bots"]], body["bots"]
    assert BOT not in body["disabled"], body["disabled"]
