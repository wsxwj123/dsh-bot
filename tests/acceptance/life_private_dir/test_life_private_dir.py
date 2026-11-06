# -*- coding: utf-8 -*-
"""life-private-dir 验收：`DSH_BOT_HOME` 下 locks / backups 两个私有目录的行为。

只经 `PUT /adapter/v1/bots/<bot_id>/life`、`PUT /adapter/v1/bots/<bot_id>/rhythm`
与 `GET /adapter/v1/bots/<bot_id>/rhythm` 观察；用例编号对应接口「行为清单」的 10 条。
功能未实现时第 1–4、9、10 条红，第 5–8 条应为绿。
"""
import os
import stat
from pathlib import Path

import pytest

from . import life_util as lu
from .life_util import ru

BOT = ru.BOT
LIFE_NAME = "%s.yml" % BOT


def _names(d):
    p = Path(d)
    return sorted(x.name for x in p.iterdir()) if p.is_dir() else []


def _mode(p):
    return stat.S_IMODE(os.stat(str(p)).st_mode)


def _link_backups(w):
    outside = w.outside()
    w.make_link("backups", outside)
    return outside


def _link_locks(w):
    outside = w.outside()
    w.make_link("locks", outside)
    return outside


def _link_backups_life(w):
    outside = w.outside()
    (w.sb.dsh_home / "backups").mkdir()
    os.symlink(str(outside), str(w.sb.dsh_home / "backups" / "life"))
    return outside


# ============================================================ 拒绝：backups / locks 是软链

def test_01_backups是指向外部的软链_life改字段回500_internal且文案逐字_文件不变_外部为空(w):
    """接口行为 1。"""
    w.bot(life_text=lu.LIFE_TEXT)
    outside = _link_backups(w)
    life = w.life_path()
    before = life.read_bytes()

    w.expect_private_dir_refused(w.life_put({"city": "苏州"}))

    assert life.read_bytes() == before, "被拒了生活设定文件却变了"
    assert _names(outside) == [], "外部目录里多出了东西：%r" % (_names(outside),)


def test_02_backups是软链_rhythm改字段回500_同一句文案_文件不变_外部为空(w):
    """接口行为 2。"""
    w.bot(life_text=lu.LIFE_TEXT)
    v0 = w.version()
    outside = _link_backups(w)
    life = w.life_path()
    before = life.read_bytes()

    w.expect_private_dir_refused(w.put({"set": {lu.DAILY: 2}, "remove": [], "version": v0}))

    assert life.read_bytes() == before, "被拒了生活设定文件却变了"
    assert _names(outside) == [], "外部目录里多出了东西：%r" % (_names(outside),)


def test_03_locks是指向外部的软链_life与rhythm都回500_两文件不变_外部没有life_lock(w):
    """接口行为 3。"""
    w.bot(life_text=lu.LIFE_TEXT)
    v0 = w.version()
    outside = _link_locks(w)
    life = w.life_path()
    before = life.read_bytes()

    w.expect_private_dir_refused(w.life_put({"city": "苏州"}))
    assert life.read_bytes() == before, "life 被拒后文件变了"

    w.expect_private_dir_refused(w.put({"set": {lu.DAILY: 2}, "remove": [], "version": v0}))
    assert life.read_bytes() == before, "rhythm 被拒后文件变了"

    assert _names(outside) == [], "外部目录里出现了 %r（不该有 life.lock 或别的文件）" % (_names(outside),)


def test_04_backups_life是指向外部的软链_life保存回500_文件不变_外部为空(w):
    """接口行为 4。"""
    w.bot(life_text=lu.LIFE_TEXT)
    outside = _link_backups_life(w)
    life = w.life_path()
    before = life.read_bytes()

    w.expect_private_dir_refused(w.life_put({"city": "苏州"}))

    assert life.read_bytes() == before, "被拒了生活设定文件却变了"
    assert _names(outside) == [], "外部目录里多出了东西：%r" % (_names(outside),)


# ============================================================ 不误伤：DSH_BOT_HOME 本身是软链

def test_05_DSH_BOT_HOME本身是软链_life与rhythm照常成功_备份落在真实目录(w, monkeypatch):
    """接口行为 5：home-link -> realhome，业务文件都在 realhome 下。"""
    tmp = w.sb.tmp
    realhome = tmp / "realhome"
    realhome.mkdir()
    link = tmp / "home-link"
    os.symlink(str(realhome), str(link))
    assert link.is_dir() and realhome.is_dir(), "前提：软链与真目录都在"

    monkeypatch.setenv("DSH_BOT_HOME", str(link))
    original_home = w.sb.dsh_home
    w.sb.dsh_home = link                       # bot / 生活设定 / 备份都经软链落到 realhome
    w.bot(life_text=lu.LIFE_TEXT)

    body = ru.expect_ok(w.life_put({"city": "苏州"}))
    assert body.get("ok") is True
    assert ru.load_yaml(realhome / "life" / LIFE_NAME)["city"] == "苏州", \
        "生活设定应照常写进 realhome/life"
    assert len(w.life_backups()) == 1, "生活备份应落在 realhome/backups/life 下"

    v0 = w.version()
    ru.expect_ok(w.put({"set": {lu.DAILY: 2}, "remove": [], "version": v0}))
    assert ru.load_yaml(realhome / "life" / LIFE_NAME)["moments_daily_limit"] == 2, \
        "节奏应照常写进 realhome/life"
    assert len(w.rhythm_backups()) == 1, "节奏备份应落在 realhome/backups/rhythm 下"
    assert not (original_home / "backups").exists(), "不该写到换掉之前的临时家目录"


# ============================================================ 正常环境回归

def test_06_正常环境_life成功_文件按请求更新_备份等于改动前字节_连改三次只留两份(w):
    """接口行为 6。"""
    w.bot(life_text="# 头注释\ncity: 杭州\n")
    life = w.life_path()
    originals = []
    for city in ("苏州", "常州", "上海"):
        originals.append(life.read_bytes())
        ru.expect_ok(w.life_put({"city": city}))
        assert ru.load_yaml(life)["city"] == city, "生活设定文件应随请求更新"

    files = w.life_backups()
    assert len(files) == 2, "连改三次后 backups/life/<bot>/ 应恰好 2 个文件，实际 %r" % (
        [p.name for p in files],)
    assert {p.read_bytes() for p in files} == {originals[1], originals[2]}, \
        "留下的备份应是最近两次改动前的原文"


def test_07_正常环境_rhythm成功_备份留两份_返回version与文件现状一致(w):
    """接口行为 7。"""
    w.bot(life_text=lu.LIFE_TEXT)
    life = w.life_path()
    originals = []
    last_version = None
    for n in (1, 2, 3):
        originals.append(life.read_bytes())
        body = ru.expect_ok(w.put({"set": {lu.DAILY: n}, "remove": [], "version": w.version()}))
        last_version = body["version"]
        assert ru.expect_ok(w.get())["version"] == last_version, \
            "PUT 返回的 version 应与随后 GET 到的一致"
        assert ru.load_yaml(life)["moments_daily_limit"] == n

    files = w.rhythm_backups()
    assert len(files) == 2, "连改三次后 backups/rhythm/<bot>/ 应恰好 2 个文件，实际 %r" % (
        [p.name for p in files],)
    assert {p.read_bytes() for p in files} == {originals[1], originals[2]}, \
        "留下的备份应是最近两次改动前的原文"


@pytest.mark.skipif(os.name == "nt", reason="Windows 没有 POSIX 目录/文件权限位")
def test_08_首次保存后_locks_backups_备份子目录都是0700_备份文件0600(w):
    """接口行为 8。"""
    w.bot(life_text=lu.LIFE_TEXT)
    ru.expect_ok(w.life_put({"city": "苏州"}))

    dsh = w.sb.dsh_home
    assert _mode(dsh / "locks") == 0o700, "locks 目录权限应为 0700，实际 %o" % _mode(dsh / "locks")
    assert _mode(dsh / "backups") == 0o700, "backups 目录权限应为 0700，实际 %o" % _mode(dsh / "backups")
    sub = dsh / "backups" / "life" / BOT
    assert _mode(sub) == 0o700, "backups/life/<bot> 权限应为 0700，实际 %o" % _mode(sub)

    files = w.life_backups()
    assert len(files) == 1, "首次保存应留一份备份：%r" % ([p.name for p in files],)
    assert _mode(files[0]) == 0o600, "备份文件权限应为 0600，实际 %o" % _mode(files[0])


# ============================================================ 拒绝后不留垃圾

@pytest.mark.parametrize("arrange", [_link_backups, _link_locks, _link_backups_life],
                         ids=["backups软链", "locks软链", "backups_life软链"])
def test_09_拒绝后不留垃圾_家目录没有tmp残留_生活目录只剩原文件_外部为空(w, arrange):
    """接口行为 9：第 1、3、4 条失败后。"""
    w.bot(life_text=lu.LIFE_TEXT)
    outside = arrange(w)
    w.expect_private_dir_refused(w.life_put({"city": "苏州"}))

    dsh = w.sb.dsh_home
    leftovers = []
    for root, _dirs, files in os.walk(str(dsh)):
        for name in files:
            if ".tmp" in name:
                leftovers.append(os.path.relpath(os.path.join(root, name), str(dsh)))
    assert leftovers == [], "拒绝后留下了临时文件：%r" % (leftovers,)
    assert _names(dsh / "life") == [LIFE_NAME], \
        "生活设定同目录下只剩原文件，实际 %r" % (_names(dsh / "life"),)
    assert _names(outside) == [], "外部目录里留下了半成品：%r" % (_names(outside),)


# ============================================================ 失败只影响那一次

def _repair(w, name):
    p = w.sb.dsh_home / name
    os.unlink(str(p))
    p.mkdir(mode=0o700)
    assert p.is_dir() and not p.is_symlink(), "前提：%s 已换回真目录" % name


@pytest.mark.parametrize("name, arrange", [("backups", _link_backups), ("locks", _link_locks)],
                         ids=["backups软链", "locks软链"])
def test_10_失败只影响那一次_换回真目录后保存成功_备份与文件都正常(w, name, arrange):
    """接口行为 10：第 1、3 条失败后。"""
    w.bot(life_text=lu.LIFE_TEXT)
    arrange(w)
    life = w.life_path()
    before = life.read_bytes()

    w.expect_private_dir_refused(w.life_put({"city": "苏州"}))
    assert life.read_bytes() == before, "失败的那次不该改文件"

    _repair(w, name)
    ru.expect_ok(w.life_put({"city": "苏州"}))

    assert ru.load_yaml(life)["city"] == "苏州", "换回真目录后保存应成功"
    files = w.life_backups()
    assert len(files) == 1, "成功后应有且只有一份备份：%r" % ([p.name for p in files],)
    assert files[0].read_bytes() == before, "备份应是换回真目录那次改动前的原文"
