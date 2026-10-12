# -*- coding: utf-8 -*-
"""探针。场景：目录夹具已经把 HUB_LAUNCHCTL 换成假件，总闸的毒丸却还是被调用了。

真实世界里这是前面某条用例留下的后台线程干的：它在夹具撤掉假件之后才去读环境变量。
这里直接调总闸毒丸的路径来模拟（只读子命令 print，执行前确认它确实是毒丸）。
"""
import subprocess

import pytest


@pytest.fixture
def fake_launchctl_in_place(tmp_path, monkeypatch):
    exe = tmp_path / "fake-launchctl"
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("HUB_LAUNCHCTL", str(exe))


def test_假件已就位_总闸毒丸却被调用(fake_launchctl_in_place, total_gate):
    exe = total_gate.poison_launchctl
    assert "dsh-test-gate-" in exe, "保险：不是总闸的毒丸，探针拒绝执行"
    subprocess.run([exe, "print", "gui/501/com.dsh-bot.probe-stale"],
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
