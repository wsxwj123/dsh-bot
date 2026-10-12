# -*- coding: utf-8 -*-
"""覆盖命令（`HUB_RESTART_CMD`）子进程的环境在受理时定下（INTERFACE-新系统bot运维 §1a，第 6 稿追加）。

`bots_client.command_runner` 是三种 job 走覆盖命令时的共同入口：命令串与子进程环境都在
工厂体内（处理请求的线程里）算好，返回的闭包交给后台线程时不再读环境。这里直接调它与
`_run_command`，命令是一条只回显环境变量的 sh，输出从返回值里读。
"""
import os

import pytest

from moments import bots_client as bc

pytestmark = pytest.mark.skipif(os.name == "nt", reason="回显命令用的是 /bin/sh")


def _show(*names):
    """一条覆盖命令：把这几个环境变量按顺序回显出来，没设的显示 none。"""
    return ["/bin/sh", "-c", 'echo "%s"' % "|".join("${%s:-none}" % n for n in names)]


def _out(result):
    state, code, text = result
    assert (state, code) == ("done", 0), result
    return text.strip().split("|")


@pytest.fixture
def roots(tmp_path, monkeypatch):
    legacy, dsh, other = tmp_path / "legacy", tmp_path / "dsh", tmp_path / "other"
    for d in (legacy, dsh, other):
        d.mkdir()
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(legacy))
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(dsh))
    monkeypatch.delenv("HUB_RESTART_CMD", raising=False)
    monkeypatch.delenv("UNIT_EARLY", raising=False)
    monkeypatch.delenv("UNIT_LATE", raising=False)
    return legacy, dsh, other


def test_受理后改环境_子进程看到的是受理那一刻的(roots, monkeypatch):
    _legacy, dsh, other = roots
    monkeypatch.setenv("UNIT_EARLY", "early")
    runner = bc.command_runner(_show("HUB_CONFIGS_DIR", "UNIT_EARLY", "UNIT_LATE"))
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(other))      # 受理之后：新系统根换掉
    monkeypatch.setenv("UNIT_EARLY", "changed")                 # 已有的变量改值
    monkeypatch.setenv("UNIT_LATE", "late")                     # 再多出一个
    assert _out(runner()) == [str(dsh), "early", "none"]


def test_新系统根不在场_取受理时进程环境的拷贝_之后再改不跟着变(roots, monkeypatch):
    legacy, _dsh, other = roots
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", "off")
    monkeypatch.setenv("UNIT_EARLY", "early")
    runner = bc.command_runner(_show("HUB_CONFIGS_DIR", "UNIT_EARLY", "UNIT_LATE"))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(other))
    monkeypatch.setenv("UNIT_LATE", "late")
    assert _out(runner()) == [str(legacy), "early", "none"]     # 不注入，照受理时的原样继承


def test_命令串缺省时也是受理时读的那条(roots, monkeypatch):
    monkeypatch.setenv("HUB_RESTART_CMD", "/bin/sh -c 'echo first'")
    runner = bc.command_runner()
    monkeypatch.setenv("HUB_RESTART_CMD", "/bin/sh -c 'echo second'")
    assert runner() == ("done", 0, "first\n")


def test_两次受理各拿各的环境_互不串(roots, monkeypatch):
    monkeypatch.setenv("UNIT_EARLY", "one")
    first = bc.command_runner(_show("UNIT_EARLY"))
    monkeypatch.setenv("UNIT_EARLY", "two")
    second = bc.command_runner(_show("UNIT_EARLY"))
    assert (_out(first()), _out(second())) == (["one"], ["two"])


# ------------------------------------------------------------ _run_command 的环境参数

def test_环境给了就用给的_空表就是空环境(roots, monkeypatch):
    """「给了但是空的」不许被当成「没给」：子进程里连新系统根的注入都没有。"""
    monkeypatch.setenv("UNIT_EARLY", "early")
    got = _out(bc._run_command(_show("HUB_CONFIGS_DIR", "UNIT_EARLY"), 10, {}))
    assert got == ["none", "none"]


def test_环境没给才现算_新系统根在场时注入它(roots, monkeypatch):
    _legacy, dsh, _other = roots
    monkeypatch.setenv("UNIT_EARLY", "early")
    got = _out(bc._run_command(_show("HUB_CONFIGS_DIR", "UNIT_EARLY"), 10))
    assert got == [str(dsh), "early"]


def test_环境显式给None_是原样继承_与没给不是一回事(roots):
    legacy, _dsh, _other = roots
    got = _out(bc._run_command(_show("HUB_CONFIGS_DIR"), 10, None))
    assert got == [str(legacy)]                                 # 没有注入新系统根
