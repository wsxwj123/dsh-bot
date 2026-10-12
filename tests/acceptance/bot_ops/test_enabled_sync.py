# -*- coding: utf-8 -*-
"""§4b POST /hub/api/bots/<id>/enabled：写盘后同步该只。

F1 的解法：开关触发的同步不受 `bot_disabled` 拒绝规则约束 ——
「停用」方向要走 bootout，若实现误用 §4 的拒绝规则，停用会永远停不下来。
"""
import json
import os
import re

import pytest

from . import botops_util as u


def _post(client, bot_id, enabled):
    return client.post("/hub/api/bots/%s/enabled" % bot_id, json={"enabled": enabled})


def _yml(path):
    return path.read_text(encoding="utf-8")


# ------------------------------------------------------------ 正常路径

def test_开关_停用_写盘生效且同步走停用方向bootout(
        client, sandbox, launchctl, as_darwin):
    """F1 核心：写盘后该 bot 已是停用，同步仍然执行 bootout（不受 bot_disabled 拒绝）。"""
    yml = u.dsh_bot(sandbox.dsh_cfg, "xiaoyu")
    launchctl.set_default(installed=True, running=True, pid=4242)
    r = _post(client, "xiaoyu", False)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["ok"] is True and body["enabled"] is False
    assert u.read_yaml(yml)["enabled"] is False, _yml(yml)
    sync = body["sync"]
    assert sync["state"] == "started", body
    assert re.fullmatch(r"[0-9a-f]{8}", sync["job_id"])
    assert isinstance(sync["started_at"], int)
    st = u.wait_job(client, sync["job_id"])
    assert st["state"] == "done", st
    assert launchctl.subcommands() == ["bootout"], launchctl.calls()
    assert launchctl.labels() == ["com.dsh-bot.xiaoyu"], launchctl.calls()


def test_开关_启用_写盘生效且同步kickstart并判活通过(
        client, sandbox, heartbeat, launchctl, as_darwin):
    yml = u.dsh_bot(sandbox.dsh_cfg, "xiaoyu", enabled=False)
    launchctl.set_default(installed=True, running=False, pid=4242)
    heartbeat.tick("xiaoyu", pid=4242)
    r = _post(client, "xiaoyu", True)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["enabled"] is True
    assert u.read_yaml(yml).get("enabled") is not False, _yml(yml)
    sync = r.get_json()["sync"]
    assert sync["state"] == "started", sync
    st = u.wait_job(client, sync["job_id"])
    assert st["state"] == "done", st
    cmds = launchctl.subcommands()
    assert "kickstart" in cmds and set(cmds) <= {"kickstart", "print"}, launchctl.calls()


def test_开关_已停用的再点停用_不409且同步照跑(
        client, sandbox, launchctl, as_darwin):
    """停用方向永不被自己的拒绝规则锁死（拍板 2 只服务行内手动重启）。"""
    yml = u.dsh_bot(sandbox.dsh_cfg, "xiaoyu", enabled=False)
    launchctl.set_default(installed=True, running=True, pid=4242)
    r = _post(client, "xiaoyu", False)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["sync"]["state"] == "started", r.get_json()
    assert u.read_yaml(yml)["enabled"] is False


def test_开关_响应形状_restart_hint与sync字段恒存在(client, sandbox, launchctl, as_darwin):
    u.dsh_bot(sandbox.dsh_cfg, "xiaoyu")
    body = _post(client, "xiaoyu", False).get_json()
    assert set(body) == {"ok", "enabled", "restart_hint", "sync"}, body
    assert body["restart_hint"] == {"bot": True, "moments_web": False}, body


def test_开关_重复同方向_幂等且都200(client, sandbox, launchctl, as_darwin):
    yml = u.dsh_bot(sandbox.dsh_cfg, "xiaoyu")
    launchctl.set_default(installed=True, running=True, pid=4242)
    assert _post(client, "xiaoyu", False).status_code == 200
    once = _yml(yml)
    r = _post(client, "xiaoyu", False)
    assert r.status_code == 200, r.get_json()
    assert _yml(yml) == once


# ------------------------------------------------------------ 同步的去向

def test_开关_legacy的bot_写盘但skipped_legacy且覆盖命令也不执行(
        client, sandbox, monkeypatch):
    """旧系统的东西一律不碰：即使设了 HUB_RESTART_CMD 也只写盘、不同步。"""
    yml = u.write_bot_yml(sandbox.legacy_cfg, "chenlulu", display_name="陈璐璐",
                          dispatcher_port=17899)
    marker = sandbox.tmp / "must-not-run"
    monkeypatch.setenv("HUB_RESTART_CMD",
                       u.python_cmd("import os;open(os.environ['BOTOPS_MARKER'],'w')"))
    monkeypatch.setenv("BOTOPS_MARKER", str(marker))
    r = _post(client, "chenlulu", False)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["sync"] == {"state": "skipped", "reason": "legacy"}, body
    assert u.read_yaml(yml)["enabled"] is False, "写盘没发生"
    assert not marker.exists(), "旧系统的 bot 被同步动作碰到了"


def test_开关_非macOS无覆盖命令_skipped_no_launchd(
        client, sandbox, launchctl, as_linux):
    yml = u.dsh_bot(sandbox.dsh_cfg, "xiaoyu")
    r = _post(client, "xiaoyu", False)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["sync"] == {"state": "skipped", "reason": "no_launchd"}, body
    assert u.read_yaml(yml)["enabled"] is False, "写盘没发生"
    assert launchctl.calls() == []


def test_开关_非macOS有覆盖命令_started并真的执行(
        client, sandbox, monkeypatch):
    u.dsh_bot(sandbox.dsh_cfg, "xiaoyu")
    marker = sandbox.tmp / "ran.txt"
    monkeypatch.setenv("BOTOPS_MARKER", str(marker))
    monkeypatch.setenv("HUB_RESTART_CMD",
                       u.python_cmd("import os;open(os.environ['BOTOPS_MARKER'],'w')"
                                    ".write('ran')"))
    r = _post(client, "xiaoyu", False)
    assert r.status_code == 200, r.get_json()
    sync = r.get_json()["sync"]
    assert sync["state"] == "started", sync
    st = u.wait_job(client, sync["job_id"])
    assert st["state"] == "done", st
    assert marker.read_text(encoding="utf-8") == "ran"


def test_开关_忙时_写盘仍生效且sync_busy带在跑的job_id(
        client, sandbox, monkeypatch):
    """写盘必须发生（用户的诉求是改配置），不 409；忙的事实如实写进 sync。"""
    yml = u.dsh_bot(sandbox.dsh_cfg, "xiaoyu")
    monkeypatch.setenv("HUB_RESTART_CMD", u.python_cmd("import time;time.sleep(2)"))
    first = client.post("/hub/api/bots/restart")
    assert first.status_code == 202, first.get_json()
    running_id = first.get_json()["job_id"]
    r = _post(client, "xiaoyu", False)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["sync"] == {"state": "busy", "job_id": running_id}, body
    assert u.read_yaml(yml)["enabled"] is False, "忙时写盘也必须发生"
    assert u.wait_job(client, running_id)["state"] == "done"


# ------------------------------------------------------------ 错误面

@pytest.mark.parametrize("body", [
    {},
    {"enabled": "false"},
    {"enabled": 0},
    {"enabled": None},
    {"enabled": "true"},
])
def test_开关_enabled不是布尔_400_bad_body且不写盘(client, sandbox, body):
    yml = u.dsh_bot(sandbox.dsh_cfg, "xiaoyu")
    before = _yml(yml)
    r = client.post("/hub/api/bots/xiaoyu/enabled", json=body)
    assert r.status_code == 400, (body, r.status_code, r.get_json())
    assert r.get_json()["error"] == "bad_body"
    assert _yml(yml) == before


def test_开关_请求体不是对象_400_bad_body且不写盘(client, sandbox):
    yml = u.dsh_bot(sandbox.dsh_cfg, "xiaoyu")
    before = _yml(yml)
    r = client.post("/hub/api/bots/xiaoyu/enabled", data="[1,2,3]",
                    content_type="application/json")
    assert r.status_code == 400, r.get_json()
    assert r.get_json()["error"] == "bad_body"
    assert _yml(yml) == before
    assert "sync" not in r.get_json()


@pytest.mark.parametrize("bad", ["_global", "Chenlulu", "a", "1abc", "有中文"])
def test_开关_id形状非法_400_bad_bot_id且不落盘(client, sandbox, bad):
    r = client.post("/hub/api/bots/%s/enabled" % bad, json={"enabled": False})
    assert r.status_code == 400, (bad, r.status_code, r.get_json())
    assert r.get_json()["error"] == "bad_bot_id"
    assert list(sandbox.dsh_cfg.iterdir()) == []


def test_开关_找不到配置_404_bot_not_found(client, sandbox):
    r = _post(client, "ghostbot", False)
    assert r.status_code == 404, r.get_json()
    assert r.get_json()["error"] == "bot_not_found"


def test_开关_坏yml_409_bad_yaml且文件一个字节没动(client, sandbox):
    yml = sandbox.dsh_cfg / "xiaoyu.yml"
    yml.write_text("id: [oops\n  x: : :\n", encoding="utf-8")
    before = _yml(yml)
    r = _post(client, "xiaoyu", False)
    assert r.status_code == 409, r.get_json()
    assert r.get_json()["error"] == "bad_yaml"
    assert _yml(yml) == before


@pytest.mark.skipif(os.name == "nt", reason="Windows 上 chmod 0 不阻止读取")
def test_开关_文件存在但读不出来_500_config_unreadable且detail只有类名(client, sandbox):
    """文件在、读它抛 OSError（权限 0）→ 按契约 §9 是 500 config_unreadable。"""
    yml = u.dsh_bot(sandbox.dsh_cfg, "xiaoyu")
    before = yml.read_text(encoding="utf-8")
    os.chmod(yml, 0)
    try:
        r = _post(client, "xiaoyu", False)
    finally:
        os.chmod(yml, 0o600)
    assert r.status_code == 500, r.get_json()
    body = r.get_json()
    assert body["error"] == "config_unreadable", body
    detail = body.get("detail") or ""
    assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", detail), detail
    assert "/" not in detail and "\\" not in detail, detail
    assert yml.read_text(encoding="utf-8") == before, "读失败不该动文件"
