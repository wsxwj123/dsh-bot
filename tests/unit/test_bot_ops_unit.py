# -*- coding: utf-8 -*-
"""§5b 内部契约（S3）单测：`bots_client.restart_plan()` / `RestartJobs.start(runner)` /
`config_sources` 新增的 `dsh_home()` / `dsh_bot_dir()` / `dsh_dir()` 默认值。

只调函数，不起 Flask、不碰真实 ~/.dsh-bot（HOME 钉 tmp）。
"""
import json
import re
import shlex
import sys
import time
from pathlib import Path

import pytest

from moments import config_sources
from moments import bots_client as bc

TOKEN = "123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    for k in ("DSH_BOT_HOME", "HUB_CONFIGS_DSH_DIR", "HUB_CONFIGS_DIR", "HUB_RESTART_CMD",
              "HUB_RESTART_SCRIPT", "HUB_BOTS_FILE"):
        monkeypatch.delenv(k, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    dsh_cfg = tmp_path / "dsh-cfg"
    dsh_cfg.mkdir()
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(dsh_cfg))
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(legacy))
    return tmp_path


def _force_platform(monkeypatch, name):
    import platform as _p
    if name == "darwin":
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(_p, "system", lambda: "Darwin")
    else:
        monkeypatch.setattr(sys, "platform", name)
        monkeypatch.setattr(_p, "system", lambda: name.capitalize())


def _wait(jobs, job_id, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        st = jobs.get(job_id)
        assert st is not None
        if st["state"] != "running":
            return st
        time.sleep(0.02)
    raise AssertionError("job 没进终态：%r" % jobs.get(job_id))


# ------------------------------------------------------------ config_sources

def test_dsh_home_优先读DSH_BOT_HOME(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_BOT_HOME", str(tmp_path / "x"))
    assert str(config_sources.dsh_home()) == str(tmp_path / "x")


def test_dsh_home_未设时回落家目录下的点dsh_bot(tmp_path):
    assert str(config_sources.dsh_home()) == str(tmp_path / "home" / ".dsh-bot")


def test_dsh_bot_dir_拼出bot家(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_BOT_HOME", str(tmp_path / "x"))
    assert str(config_sources.dsh_bot_dir("xiaoyu")) == str(tmp_path / "x" / "bots" / "xiaoyu")


@pytest.mark.parametrize("bad", ["Xiaoyu", "a", "a" * 33, "_x", "1x", "有中文", "../x",
                                 "x-1", "a/b"])
def test_dsh_bot_dir_名字形状不过关回None(tmp_path, monkeypatch, bad):
    monkeypatch.setenv("DSH_BOT_HOME", str(tmp_path / "x"))
    assert config_sources.dsh_bot_dir(bad) is None, bad


def test_dsh_dir_默认值是dsh_home下的configs(tmp_path, monkeypatch):
    monkeypatch.delenv("HUB_CONFIGS_DSH_DIR", raising=False)
    monkeypatch.setenv("DSH_BOT_HOME", str(tmp_path / "x"))
    assert str(config_sources.dsh_dir()) == str(tmp_path / "x" / "configs")


@pytest.mark.parametrize("raw", ["off", "-", "none", "0", "false", "no"])
def test_dsh_dir_被关掉一族时回None(monkeypatch, raw):
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", raw)
    assert config_sources.dsh_dir() is None, raw


# ------------------------------------------------------------ restart_plan

def test_restart_plan_返回三段namedtuple(monkeypatch):
    monkeypatch.setenv("HUB_RESTART_CMD", "true")
    plan = bc.restart_plan()
    assert set(plan._fields) == {"available", "reason", "override"}


def test_restart_plan_设了覆盖命令_override是shlex切分结果(monkeypatch):
    cmd = '"%s" -c "x y"' % sys.executable
    monkeypatch.setenv("HUB_RESTART_CMD", cmd)
    plan = bc.restart_plan()
    assert plan.override == shlex.split(cmd)
    assert plan.available is True
    assert plan.reason is None


def test_restart_plan_未设覆盖命令_override为None(monkeypatch, tmp_path):
    monkeypatch.delenv("HUB_RESTART_CMD", raising=False)
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(tmp_path / "dsh-cfg"))
    _force_platform(monkeypatch, "darwin")
    plan = bc.restart_plan()
    assert plan.override is None


def test_restart_plan_非macOS且无命令_false且reason含macOS(monkeypatch, tmp_path):
    monkeypatch.delenv("HUB_RESTART_CMD", raising=False)
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(tmp_path / "dsh-cfg"))
    _force_platform(monkeypatch, "linux")
    plan = bc.restart_plan()
    assert plan.available is False
    assert "macOS" in (plan.reason or "")


def test_restart_plan_macOS且根在场_true(monkeypatch, tmp_path):
    monkeypatch.delenv("HUB_RESTART_CMD", raising=False)
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(tmp_path / "dsh-cfg"))
    _force_platform(monkeypatch, "darwin")
    plan = bc.restart_plan()
    assert plan.available is True and plan.reason is None


# ------------------------------------------------------------ RestartJobs.start(runner)

def test_restart_jobs_runner回done就是done_四键形状不变():
    jobs = bc.RestartJobs()
    acc = jobs.start(lambda: ("done", None, "重启 1 只：成功 1"))
    assert re.fullmatch(r"[0-9a-f]{8}", acc.job_id)
    assert isinstance(acc.started_at, int)
    assert acc.busy is None
    st = _wait(jobs, acc.job_id)
    assert set(st) == {"state", "exit_code", "took_ms", "tail"}
    assert st["state"] == "done"
    assert st["exit_code"] is None
    assert st["tail"] == "重启 1 只：成功 1"
    assert isinstance(st["took_ms"], int) and st["took_ms"] >= 0


def test_restart_jobs_runner回failed与退出码照实带出():
    jobs = bc.RestartJobs()
    acc = jobs.start(lambda: ("failed", 3, "命令退 3"))
    st = _wait(jobs, acc.job_id)
    assert (st["state"], st["exit_code"]) == ("failed", 3)


def test_restart_jobs_runner抛异常也进终态且互斥释放():
    jobs = bc.RestartJobs()

    def boom():
        raise RuntimeError("runner 炸了")

    acc = jobs.start(boom)
    st = _wait(jobs, acc.job_id)
    assert st["state"] == "failed", st
    assert jobs.start(lambda: ("done", None, "ok")).job_id, "失败后互斥没释放"


def test_restart_jobs_忙时第二次回busy带在跑的job_id():
    jobs = bc.RestartJobs()

    def slow():
        time.sleep(1.0)
        return ("done", None, "ok")

    first = jobs.start(slow)
    second = jobs.start(lambda: ("done", None, "ok"))
    assert second.job_id is None and second.busy == first.job_id
    _wait(jobs, first.job_id)
    third = jobs.start(lambda: ("done", None, "ok"))
    assert third.job_id and third.job_id != first.job_id


def test_restart_jobs_tail先脱敏():
    jobs = bc.RestartJobs()
    acc = jobs.start(lambda: ("failed", None, "错误里带了 %s 这个串" % TOKEN))
    st = _wait(jobs, acc.job_id)
    assert TOKEN not in json.dumps(st, ensure_ascii=False)
    assert "***" in st["tail"], st["tail"]


def test_restart_jobs_tail截尾2000():
    jobs = bc.RestartJobs()
    acc = jobs.start(lambda: ("done", None, "x" * 5000))
    st = _wait(jobs, acc.job_id)
    assert len(st["tail"]) <= 2000, len(st["tail"])


def test_restart_jobs_只留最近5条():
    jobs = bc.RestartJobs()
    ids = []
    for _ in range(6):
        acc = jobs.start(lambda: ("done", None, ""))
        ids.append(acc.job_id)
        _wait(jobs, acc.job_id)
    assert jobs.get(ids[0]) is None, "第 1 条没被挤掉"
    assert all(jobs.get(j) is not None for j in ids[1:])


def test_未知job回None():
    assert bc.RestartJobs().get("deadbeef") is None
