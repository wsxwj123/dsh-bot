# -*- coding: utf-8 -*-
"""dsh-group-speak 第二轮（导演读群记录）验收夹具（黑盒）。

只依据 .devflow/dsh-group-speak-接口.md 第 4 节编写，不读 director.py 实现。
硬隔离：HOME、DSH_BOT_HOME、HUB_CONFIGS_DIR 全指向 tmp；DIRECTOR_GT_DIR 默认清掉，
用例要用时由 load(gt_dir=...) 显式设置。
"""


def _require_total_gate():
    """自检：tests/conftest.py 的总闸没加载，就拒绝运行本目录的用例。"""
    import os
    import sys
    gate = getattr(sys, "_dsh_bot_test_gate", None)
    if isinstance(gate, dict) and gate.get("pid") == os.getpid():
        return
    here = os.path.dirname(os.path.abspath(__file__))
    tests = here
    while os.path.basename(tests) != "tests" and os.path.dirname(tests) != tests:
        tests = os.path.dirname(tests)
    root = os.path.dirname(tests)
    raise RuntimeError(
        "测试总闸（tests/conftest.py）没有加载，拒绝运行本目录的用例。\n"
        "请从仓库根或 tests/ 目录启动，例如：\n"
        "    cd %s && python3 -m pytest %s" % (root, os.path.relpath(here, root)))


_require_total_gate()
del _require_total_gate

import importlib
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
GROUP = "-1001"


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


def make_line(mid, ts, text, who=None):
    """一行群记录（格式同网关）：who=None 是真人，否则是该 bot 的用户名"""
    return {"ts": _iso(ts), "chat_id": GROUP, "message_id": mid,
            "from_id": "42" if who is None else "1", "from_username": who, "from_name": "x",
            "is_bot": who is not None, "text": text, "observed_by": "bot_a"}


def write_lines(path, lines):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(l, ensure_ascii=False) + "\n" for l in lines), encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """两只 bot 的新系统布局 + 临时 HOME。返回 env：load(gt_dir=None) 重载 director 并装名单；
    home_dir / legacy_dir / now 供用例往各处放群记录。"""
    cfgs = tmp_path / "configs"
    cfgs.mkdir()
    for bid, uname in (("a", "bot_a"), ("b", "bot_b")):
        bot = tmp_path / "bots" / bid
        (bot / "channel").mkdir(parents=True)
        (bot / "state").mkdir()
        (bot / "channel" / "access.json").write_text(
            json.dumps({"allowFrom": ["42"], "groups": {GROUP: {}}}), encoding="utf-8")
        (bot / "state" / "bot.json").write_text(
            json.dumps({"id": 1, "username": uname, "name": bid.upper()}), encoding="utf-8")
        (cfgs / f"{bid}.yml").write_text(
            f"id: {bid}\ndisplay_name: 角色{bid.upper()}\nbot_channel_path: {json.dumps(str(bot / 'channel'))}\n",
            encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(cfgs))
    monkeypatch.setenv("DSH_BOT_HOME", str(tmp_path))
    for k in ("DIRECTOR_GT_DIR", "DIRECTOR_MARKER_DIR", "DIRECTOR_STATE_DIR", "DIRECTOR_MODE_DIR", "DIRECTOR_HUMAN_ID"):
        monkeypatch.delenv(k, raising=False)

    def load(gt_dir=None):
        if gt_dir is not None:
            monkeypatch.setenv("DIRECTOR_GT_DIR", str(gt_dir))
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import director
        director = importlib.reload(director)
        assert director.load_dsh_roster(GROUP)
        return director

    return SimpleNamespace(
        load=load,
        home_gt=home / ".config" / "claude-dsh-console" / "group_transcripts",
        legacy_dir=tmp_path / "groups",
        now=time.time(),
    )
