# -*- coding: utf-8 -*-
"""§2 GET /hub/api/bots：restart_available 判据与 dsh 行的在线状态判定。

判定输入：心跳文件（新鲜与否/坏法）、假 launchctl 的 print 输出、平台桩。
"""
import time

import pytest

from .botops_util import write_bot_yml


def _ago_ms(seconds):
    return int((time.time() - seconds) * 1000)


def _row(body, bot_id):
    rows = [b for b in body["bots"] if b["id"] == bot_id]
    assert len(rows) == 1, body["bots"]
    return rows[0]


def _get_bots(client):
    r = client.get("/hub/api/bots")
    assert r.status_code == 200, (r.status_code, r.get_json())
    return r.get_json()


# ------------------------------------------------------------ 行形状与判活

def test_列表_行恒8键(client, sandbox):
    write_bot_yml(sandbox.dsh_cfg, "xiaoyu", display_name="小雨", dispatcher_port=17951)
    body = _get_bots(client)
    assert set(_row(body, "xiaoyu")) == {"id", "display_name", "port", "state", "phase",
                                         "pid", "queue_depth", "in_flight"}


def test_列表_心跳新鲜_是online且pid取心跳(client, sandbox, heartbeat):
    write_bot_yml(sandbox.dsh_cfg, "xiaoyu", display_name="小雨", dispatcher_port=17951)
    heartbeat.write("xiaoyu", pid=4242)
    row = _row(_get_bots(client), "xiaoyu")
    assert row["state"] == "online"
    assert row["pid"] == 4242
    assert row["port"] == 17951


def test_列表_心跳新鲜时四列恒null_且dsh行不发本机探活(client, sandbox, heartbeat, dispatcher):
    """契约 §2：dsh 行不发本机 HTTP 探活，phase/queue_depth/in_flight 恒为 null。

    假 dispatcher 会记录任何被访问的请求；如果实现错误地对 dsh 行发探活，
    hits 会非空（而且 stub 回的 ready 会泄进 phase）。
    """
    write_bot_yml(sandbox.dsh_cfg, "xiaoyu", display_name="小雨",
                  dispatcher_port=dispatcher.port)
    heartbeat.write("xiaoyu", pid=4242)
    row = _row(_get_bots(client), "xiaoyu")
    assert (row["phase"], row["queue_depth"], row["in_flight"]) == (None, None, None)
    assert dispatcher.hits == [], "dsh 行发了本机探活（契约说不发）"


def test_列表_心跳超15秒且launchd没在跑_是offline(client, sandbox, heartbeat, launchctl):
    write_bot_yml(sandbox.dsh_cfg, "xiaoyu", display_name="小雨", dispatcher_port=17951)
    launchctl.set_default(installed=False)
    heartbeat.write("xiaoyu", pid=4242, at_ms=_ago_ms(20))
    row = _row(_get_bots(client), "xiaoyu")
    assert row["state"] == "offline"
    assert row["pid"] is None


def test_列表_心跳超15秒但print说running_是starting且pid取launchd(
        client, sandbox, heartbeat, launchctl):
    write_bot_yml(sandbox.dsh_cfg, "xiaoyu", display_name="小雨", dispatcher_port=17951)
    launchctl.set_default(installed=True, running=True, pid=5555)
    heartbeat.write("xiaoyu", pid=4242, at_ms=_ago_ms(20))
    row = _row(_get_bots(client), "xiaoyu")
    assert row["state"] == "starting"
    assert row["pid"] == 5555, "starting 的 pid 该取 launchd 的"


@pytest.mark.parametrize("raw", [
    "{ 坏掉的 json",
    "",
    "[]",
    "{}",                                  # 缺 at 与 pid
    '{"at": 0}',                           # 缺 pid
    '{"pid": 1}',                          # 缺 at
    '{"at": "x", "pid": 1}',               # at 类型不对
    '{"at": 1, "pid": "1"}',               # pid 类型不对
])
def test_列表_心跳读不出来一律按不新鲜处理_不报错(client, sandbox, heartbeat, launchctl, raw):
    write_bot_yml(sandbox.dsh_cfg, "xiaoyu", display_name="小雨", dispatcher_port=17951)
    launchctl.set_default(installed=False)
    heartbeat.raw("xiaoyu", raw)
    body = _get_bots(client)                 # 不 500、不抛错
    row = _row(body, "xiaoyu")
    assert row["state"] == "offline"
    assert row["pid"] is None


# ------------------------------------------------------------ restart_available

def test_restart_available_覆盖命令非空时恒真且reason为null(client, monkeypatch):
    monkeypatch.setenv("HUB_RESTART_CMD", "definitely-not-empty")
    body = _get_bots(client)
    assert body["restart_available"] is True
    assert body["restart_reason"] is None


def test_restart_available_非macOS且无命令_false且reason含macOS(client, as_linux):
    body = _get_bots(client)
    assert body["restart_available"] is False
    assert "macOS" in (body["restart_reason"] or "")


def test_restart_available_macOS且新系统根在场_true且reason为null(client, as_darwin, sandbox):
    assert sandbox.dsh_cfg.is_dir()
    body = _get_bots(client)
    assert body["restart_available"] is True
    assert body["restart_reason"] is None


def test_restart_available_macOS但新系统根被关掉_false且reason说人话(
        client, as_darwin, monkeypatch):
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", "off")
    body = _get_bots(client)
    assert body["restart_available"] is False
    reason = body["restart_reason"] or ""
    assert ("新系统" in reason) or ("旧系统" in reason), reason


def test_restart_available_macOS但新系统根目录不存在_false(
        client, as_darwin, sandbox, monkeypatch):
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(sandbox.tmp / "not-there"))
    assert (sandbox.tmp / "not-there").exists() is False
    body = _get_bots(client)
    assert body["restart_available"] is False


def test_restart_reason字段恒存在(client, as_linux):
    body = _get_bots(client)
    assert "restart_reason" in body and "restart_available" in body
