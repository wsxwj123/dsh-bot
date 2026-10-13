# -*- coding: utf-8 -*-
"""§6 第 2 条：新起一个干净的解释器导入管理台应用，旧供应商那六个模块不在已加载模块里。

为什么要新起解释器：同一个测试进程里别的用例可能早就加载过它们，在本进程里看 sys.modules
说明不了问题。

子进程的环境：照抄当前进程的环境（已被总闸与本目录的夹具钉到临时目录、launchctl 是记账
假件、bun 是毒丸），只加 PYTHONPATH 指到仓库根。起之前再核一遍几个要紧的变量确实在临时
目录里。子进程只做一件事：导入、打印一行结果、退出；不监听端口，不发请求。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from . import le_util as u

REPO_ROOT = Path(__file__).resolve().parents[3]

_MUST_BE_TEMP = ("HOME", "DSH_BOT_HOME", "HUB_CONFIGS_DIR", "HUB_CONFIGS_DSH_DIR",
                 "HUB_LAUNCHAGENTS_DIR", "HUB_CHANNELS_DIR", "HUB_ENV_FILE", "BOTLIFE_STATE_DB",
                 "CLAUDE_SETTINGS_PATH", "CLAUDE_TGBOT_HOME", "HUB_LAUNCHCTL", "HUB_BUN")

_CHILD = r"""
import json, sys
import moments.web as web
app = getattr(web, "app", None) or getattr(web, "application", None)
if app is None and callable(getattr(web, "create_app", None)):
    app = web.create_app()
names = json.loads(sys.argv[1])
print("RESULT=" + json.dumps({
    "has_app": app is not None and hasattr(app, "test_client"),
    "loaded": sorted(n for n in names if n in sys.modules),
}))
"""


@pytest.fixture
def fresh_import(sandbox, tmp_path_factory, total_gate):
    """() -> 新解释器里导入管理台应用之后的结果 {"has_app": 布尔, "loaded": [还在的模块名]}。"""
    def _run():
        temp_root = os.path.realpath(str(tmp_path_factory.getbasetemp()))
        gate_root = os.path.realpath(total_gate.dir)
        env = dict(os.environ)
        for key in _MUST_BE_TEMP:
            real = os.path.realpath(env.get(key) or "")
            assert env.get(key) and (real.startswith(temp_root + os.sep) or real.startswith(gate_root + os.sep)), \
                "子进程环境里的 %s 没钉在临时目录：%r" % (key, env.get(key))
        for key in ("HUB_ACCESS_PASSWORD", "HUB_ADMIN_PASSWORD"):
            assert key not in env, "前提：子进程里不设口令"
        env["PYTHONPATH"] = str(REPO_ROOT)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        proc = subprocess.run(
            [sys.executable, "-c", _CHILD, json.dumps(u.RETIRED_MODULES)],
            cwd=str(sandbox.tmp), env=env, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120)
        assert proc.returncode == 0, "新解释器里导入管理台应用失败：%s" % proc.stderr[-1500:]
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT=")]
        assert lines, "子进程没打印结果：%r" % proc.stdout[-500:]
        return json.loads(lines[-1][len("RESULT="):])
    return _run


def test_新解释器里能导入管理台应用(fresh_import):
    """对照：下面几条说的「没加载」不是因为导入本身就失败了。"""
    assert fresh_import()["has_app"] is True


@pytest.mark.parametrize("module", u.RETIRED_MODULES)
def test_新解释器里导入管理台应用之后_这个模块没被加载(fresh_import, module):
    loaded = fresh_import()["loaded"]
    assert module not in loaded, "导入管理台应用连带加载了 %s（一共还在的：%s）" % (module, loaded)


def test_新解释器里导入管理台应用_没有调launchctl(fresh_import, sandbox):
    """导入管理台应用这件事本身不碰 launchctl（假件的账本是空的）。"""
    fresh_import()
    assert sandbox.launchctl_calls() == [], sandbox.launchctl_calls()


def test_新解释器里导入管理台应用_旧系统那边的文件不变(fresh_import, sandbox):
    before = u.snapshot(*sandbox.legacy_roots)
    fresh_import()
    assert u.snapshot_diff(before, u.snapshot(*sandbox.legacy_roots)) == []
