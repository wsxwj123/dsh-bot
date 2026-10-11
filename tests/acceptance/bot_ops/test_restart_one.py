# -*- coding: utf-8 -*-
"""§4 POST /hub/api/bots/<id>/restart：行内手动重启（单只）。

与 §4b 的开关同步是两种语义：本端点对停用中的 bot 拒绝是刻意的。
"""
import re

import pytest

from . import botops_util as u


def _post(client, bot_id):
    return client.post("/hub/api/bots/%s/restart" % bot_id)


def _json(r):
    """非 JSON 的响应（比如路由根本不存在时 Flask 的 404）在这里就炸出原文。"""
    body = r.get_json()
    assert isinstance(body, dict), "响应不是 JSON 对象：%r" % r.get_data(as_text=True)[:200]
    return body


def test_单只重启_正常_202且job_done且只动这一只(
        client, sandbox, heartbeat, launchctl, as_darwin):
    u.dsh_bot(sandbox.dsh_cfg, "xiaoyu")
    u.dsh_bot(sandbox.dsh_cfg, "otherbot")
    launchctl.set_default(installed=True, running=False, pid=4242)
    heartbeat.tick("xiaoyu", pid=4242)
    r = _post(client, "xiaoyu")
    assert r.status_code == 202, r.get_json()
    job_id = r.get_json()["job_id"]
    assert re.fullmatch(r"[0-9a-f]{8}", job_id)
    st = u.wait_job(client, job_id)
    assert st["state"] == "done", st
    assert st["tail"].strip().splitlines()[0] == "重启 1 只：成功 1", st["tail"]
    assert launchctl.labels() == ["com.dsh-bot.xiaoyu"], launchctl.calls()


def test_单只重启_停用中_409_bot_disabled且不碰进程(
        client, sandbox, launchctl, as_darwin):
    u.dsh_bot(sandbox.dsh_cfg, "xiaoyu", enabled=False)
    r = _post(client, "xiaoyu")
    assert r.status_code == 409, (r.status_code, r.get_data(as_text=True)[:200])
    body = _json(r)
    assert body["error"] == "bot_disabled"
    assert "启用" in body["detail"], body
    assert launchctl.calls() == [], "被拒了还动进程"


def test_单只重启_旧系统的bot_404且detail点明只管新系统(
        client, sandbox, launchctl, as_darwin):
    u.write_bot_yml(sandbox.legacy_cfg, "bot2", display_name="旧二", dispatcher_port=17899)
    r = _post(client, "bot2")
    assert r.status_code == 404, (r.status_code, r.get_data(as_text=True)[:200])
    body = _json(r)
    assert body["error"] == "bot_not_found"
    assert "新系统" in body["detail"], body
    assert launchctl.calls() == []


def test_单只重启_不存在的bot_404(client, sandbox, launchctl, as_darwin):
    u.dsh_bot(sandbox.dsh_cfg, "xiaoyu")
    r = _post(client, "ghostbot")
    assert r.status_code == 404, (r.status_code, r.get_data(as_text=True)[:200])
    assert _json(r)["error"] == "bot_not_found"
    assert launchctl.calls() == []


@pytest.mark.parametrize("bad", ["Xiaoyu", "_x", "1x", "a", "a" * 33, "x-1", "小雨"])
def test_单只重启_id形状非法_400_bad_bot_id(client, bad, launchctl, as_darwin):
    r = _post(client, bad)
    assert r.status_code == 400, (bad, r.status_code, r.get_data(as_text=True)[:200])
    assert _json(r)["error"] == "bad_bot_id"
    assert launchctl.calls() == []


def test_单只重启_路径穿越被拒且不动文件(client, sandbox, launchctl, as_darwin):
    victim_dir = sandbox.tmp / "outside"
    victim_dir.mkdir()
    victim = u.write_bot_yml(victim_dir, "victim")
    before = victim.read_text(encoding="utf-8")
    r = client.post("/hub/api/bots/..%2f..%2foutside%2fvictim/restart")
    assert r.status_code in (400, 404), r.status_code
    if r.status_code in (400, 404) and r.headers.get("Content-Type", "").startswith("application/json"):
        assert r.get_json()["error"] in ("bad_bot_id", "bot_not_found")
    assert victim.read_text(encoding="utf-8") == before
    assert launchctl.calls() == []


def test_单只重启_unsupported先于disabled判(client, sandbox, as_linux, launchctl):
    """判定顺序：先判这台机器能不能重启，再判这只 bot 停没停。"""
    u.dsh_bot(sandbox.dsh_cfg, "xiaoyu", enabled=False)
    r = _post(client, "xiaoyu")
    assert r.status_code == 409, (r.status_code, r.get_data(as_text=True)[:200])
    assert _json(r)["error"] == "restart_unsupported", r.get_json()


def test_单只重启_进行中_409_in_progress(client, sandbox, monkeypatch):
    u.dsh_bot(sandbox.dsh_cfg, "xiaoyu")
    monkeypatch.setenv("HUB_RESTART_CMD", u.python_cmd("import time;time.sleep(2)"))
    first = client.post("/hub/api/bots/restart")
    assert first.status_code == 202, first.get_json()
    running_id = first.get_json()["job_id"]
    r = _post(client, "xiaoyu")
    assert r.status_code == 409, (r.status_code, r.get_data(as_text=True)[:200])
    body = _json(r)
    assert body["error"] == "restart_in_progress"
    assert body["job_id"] == running_id
    assert u.wait_job(client, running_id)["state"] == "done"


def test_单只重启_命令退出码0但心跳一直不来_必须failed(
        client, sandbox, launchctl, as_darwin, monkeypatch):
    """判成功看进程真活着，不看退出码：kickstart 成功但等不到心跳就是失败。"""
    u.dsh_bot(sandbox.dsh_cfg, "xiaoyu")
    launchctl.set_default(installed=True, running=True, pid=9999)   # print 说在跑
    # 不写心跳 → §5 第 1 档永远不满足
    u.tighten_constant(monkeypatch, value=45, new=1.0)
    r = _post(client, "xiaoyu")
    assert r.status_code == 202, r.get_json()
    st = u.wait_job(client, r.get_json()["job_id"], timeout=15.0)
    assert st["state"] == "failed", st
    assert "心跳" in st["tail"], st["tail"]
    assert "xiaoyu 已重启" not in st["tail"]


def test_单只重启_心跳pid与launchd对不上_判活等不到_failed(
        client, sandbox, heartbeat, launchctl, as_darwin, monkeypatch):
    u.dsh_bot(sandbox.dsh_cfg, "xiaoyu")
    launchctl.set_default(installed=True, running=True, pid=7777)
    heartbeat.tick("xiaoyu", pid=4242)          # 与 print 的 pid 不一致
    u.tighten_constant(monkeypatch, value=45, new=1.0)
    r = _post(client, "xiaoyu")
    assert r.status_code == 202, r.get_json()
    st = u.wait_job(client, r.get_json()["job_id"], timeout=15.0)
    assert st["state"] == "failed", st
    assert "不一致" in st["tail"], st["tail"]


def test_单只重启_launchd路径不读请求体(client, sandbox, launchctl, as_darwin):
    """契约 §4：执行内容里没有请求体这回事；顶层非对象也不该冒 500。"""
    u.dsh_bot(sandbox.dsh_cfg, "xiaoyu")
    launchctl.set_default(installed=True, running=False, pid=4242)
    r = client.post("/hub/api/bots/xiaoyu/restart", json=[1, 2, 3])
    assert r.status_code == 202, (r.status_code, r.get_json())
