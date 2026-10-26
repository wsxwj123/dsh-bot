# -*- coding: utf-8 -*-
"""dsh-group-speak 第二轮：导演读群记录（接口文件第 4 节第 16–19 条）。
只通过公开函数 director.load_dsh_roster / director.read_group_history。"""
from .conftest import GROUP, make_line, write_lines


def _texts(hist):
    return [h["text"] for h in hist]


def _mids(hist):
    return [str(h["msg_id"]) for h in hist]  # 实际类型不在契约里，按字符串比


def test_16_不设GT_DIR_读HOME下缺省目录里的行(env):
    write_lines(env.home_gt / f"{GROUP}.jsonl", [make_line(1, env.now - 10, "缺省目录里的话")])
    d = env.load()
    hist = d.read_group_history(GROUP, 10)
    assert _texts(hist) == ["缺省目录里的话"]
    assert _mids(hist) == ["1"]


def test_16_不设GT_DIR_不读DSH_BOT_HOME下groups里的行(env):
    write_lines(env.home_gt / f"{GROUP}.jsonl", [make_line(1, env.now - 10, "缺省目录里的话")])
    write_lines(env.legacy_dir / f"{GROUP}.jsonl", [make_line(2, env.now - 5, "旧位置里的话")])
    d = env.load()
    hist = d.read_group_history(GROUP, 10)
    assert "旧位置里的话" not in _texts(hist)
    assert "2" not in _mids(hist)
    assert _texts(hist) == ["缺省目录里的话"]


def test_17_设了GT_DIR_读该目录_不读HOME缺省目录(env, tmp_path):
    custom = tmp_path / "custom_gt"
    write_lines(custom / f"{GROUP}.jsonl", [make_line(7, env.now - 10, "自定义目录里的话")])
    write_lines(env.home_gt / f"{GROUP}.jsonl", [make_line(8, env.now - 5, "缺省目录里的话")])
    d = env.load(gt_dir=custom)
    hist = d.read_group_history(GROUP, 10)
    assert _texts(hist) == ["自定义目录里的话"]
    assert _mids(hist) == ["7"]


def test_18_点1与当前文件各有不同的行_两边都读到_按时间正序(env):
    d0 = env.home_gt
    now = env.now
    write_lines(d0 / f"{GROUP}.jsonl.1", [make_line(1, now - 50, "旧一"), make_line(2, now - 40, "旧二")])
    write_lines(d0 / f"{GROUP}.jsonl", [make_line(3, now - 30, "新一"), make_line(4, now - 20, "新二")])
    d = env.load()
    hist = d.read_group_history(GROUP, 10)
    assert _texts(hist) == ["旧一", "旧二", "新一", "新二"]
    assert _mids(hist) == ["1", "2", "3", "4"]


def test_18_同一message_id两个文件都有_只出现一次(env):
    d0 = env.home_gt
    now = env.now
    dup = make_line(2, now - 40, "两边都有的话")
    write_lines(d0 / f"{GROUP}.jsonl.1", [make_line(1, now - 50, "旧一"), dup])
    write_lines(d0 / f"{GROUP}.jsonl", [dup, make_line(3, now - 30, "新一")])
    d = env.load()
    hist = d.read_group_history(GROUP, 10)
    assert _mids(hist).count("2") == 1
    assert _texts(hist) == ["旧一", "两边都有的话", "新一"]


def test_19_只有点1没有当前文件_仍读到点1的行(env):
    now = env.now
    write_lines(env.home_gt / f"{GROUP}.jsonl.1", [make_line(1, now - 50, "只在旧文件里"), make_line(2, now - 40, "也在旧文件里")])
    assert not (env.home_gt / f"{GROUP}.jsonl").exists()
    d = env.load()
    hist = d.read_group_history(GROUP, 10)
    assert _texts(hist) == ["只在旧文件里", "也在旧文件里"]
