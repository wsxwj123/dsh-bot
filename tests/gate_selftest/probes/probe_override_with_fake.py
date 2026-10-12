# -*- coding: utf-8 -*-
"""探针。场景：目录夹具把两个接缝换成了自己的假件（bot_ops、hub2 的做法），调用假件不算碰闸。"""
import os
import subprocess
import sys

import pytest


def _fake(tmp_path, name):
    log = tmp_path / (name + ".log")
    if os.name == "nt":
        exe = tmp_path / (name + ".cmd")
        exe.write_text('@echo %%* >> "%s"\r\n@exit /b 0\r\n' % log, encoding="utf-8")
    else:
        exe = tmp_path / name
        exe.write_text("#!/bin/sh\necho \"$*\" >> '%s'\nexit 0\n" % log, encoding="utf-8")
        exe.chmod(0o755)
    return exe, log


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    launchctl, launchctl_log = _fake(tmp_path, "launchctl")
    bun, bun_log = _fake(tmp_path, "bun")
    monkeypatch.setenv("HUB_LAUNCHCTL", str(launchctl))
    monkeypatch.setenv("HUB_BUN", str(bun))
    return launchctl_log, bun_log


def test_换成假件后调用(fakes):
    launchctl_log, bun_log = fakes
    assert subprocess.run([os.environ["HUB_LAUNCHCTL"], "print", "x"]).returncode == 0
    assert subprocess.run([os.environ["HUB_BUN"], "--version"]).returncode == 0
    assert launchctl_log.read_text(encoding="utf-8").strip() == "print x"
    assert bun_log.read_text(encoding="utf-8").strip() == "--version"
    sys.stdout.write("PROBE_FAKE_OK\n")
