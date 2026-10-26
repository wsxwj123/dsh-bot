# -*- coding: utf-8 -*-
"""编辑接口第 8 稿的白盒单测（INTERFACE-新系统bot运维 §10）：空人设被拒、人设文件编码坏了
怎么答、没给的键不动、人设改写前留备份。

起因：编辑弹窗读取失败后仍能保存，而它每次把三个键全交上来 —— 填一个名字点保存，人设被
写成空文件。后端这一侧的保险：空的人设正文不接受；人设被改写之前先留一份备份，最多 2 份。

验收那层靠真的隔一秒去量「同一秒」「连改 7 次」；这里把时钟钉死，直接量备份的命名、同秒
冲突、清理挑谁删、以及失败恢复时备份跟不跟着走。落盘只在 tmp_path 下；bot 名全是假名；
写朋友圈库的那一步换成记账的替身，不碰真库。
"""
import os
import stat
import types

import pytest

from moments import hub_addbot as ab
from moments.provider_model import HubError

pytestmark = pytest.mark.skipif(os.name == "nt", reason="靠 POSIX 权限位造失败")

BOT = "bakbot"
OLD = "# 旧人设\n说话简短。\n"
EMPTY = "人设正文不能为空；不想改人设就不要带 persona 这个键"
BAD_FILE = "人设文件不是合法的 UTF-8 文本，这一页读不了它；先把文件修好再来编辑"
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
needs_perm = pytest.mark.skipif(IS_ROOT, reason="root 下目录权限拦不住写")


class _Clock:
    """把 `hub_addbot` 看到的「现在」钉在一个时刻；`tick()` 往后拨一秒。"""

    def __init__(self):
        import datetime as real
        self._real = real
        self.now_value = real.datetime(2026, 10, 13, 14, 25, 30)

    def now(self):
        return self.now_value

    def tick(self, seconds=1):
        self.now_value += self._real.timedelta(seconds=seconds)


@pytest.fixture
def w(tmp_path, monkeypatch):
    for name in ("configs", "dsh-configs", "dsh-home", "home"):
        (tmp_path / name).mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))   # Windows 上 expanduser 读它
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(tmp_path / "configs"))
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(tmp_path / "dsh-configs"))
    monkeypatch.setenv("DSH_BOT_HOME", str(tmp_path / "dsh-home"))
    monkeypatch.delenv("HUB_BOTS_FILE", raising=False)
    yml = tmp_path / "dsh-configs" / (BOT + ".yml")
    yml.write_text('id: %s\ndisplay_name: "旧名"\ndispatcher_port: 17981\n' % BOT, encoding="utf-8")
    ch = tmp_path / "dsh-home" / "bots" / BOT / "channel"
    ch.mkdir(parents=True)
    md, acc = ch / "CLAUDE.md", ch / "access.json"
    md.write_text(OLD, encoding="utf-8")
    acc.write_text('{"allowFrom": [1], "voiceId": "v-old"}', encoding="utf-8")
    clock = _Clock()
    monkeypatch.setattr(ab, "datetime", types.SimpleNamespace(datetime=clock))
    saved = []
    monkeypatch.setattr(ab, "_save_db_name", lambda cfg, bot, value, undo: saved.append(value))
    return types.SimpleNamespace(tmp=tmp_path, yml=yml, ch=ch, md=md, acc=acc, clock=clock, saved=saved)


def _baks(ch):
    return sorted(p.name for p in ch.iterdir() if p.name.startswith("CLAUDE.md.bak"))


def _err(fn, *a):
    with pytest.raises(HubError) as e:
        fn(*a)
    return e.value.status, e.value.error, e.value.detail


# ============================================================ 建备份的那一个入口

def test_备份_名字是本地时间的年月日时分秒_放在人设旁边(w):
    path = ab._backup_persona(str(w.md), b"old")
    assert path == str(w.ch / "CLAUDE.md.bak.20261013-142530")
    assert _baks(w.ch) == ["CLAUDE.md.bak.20261013-142530"]


def test_备份_内容逐字节相同_不是合法UTF8的旧人设也照留(w):
    raw = b"\xff\xfe\x00bad \x80 bytes\r\n"
    path = ab._backup_persona(str(w.md), raw)
    assert open(path, "rb").read() == raw


@pytest.mark.parametrize("mode", [0o600, 0o644, 0o666])
def test_备份_权限恒为0600_不跟着原文件放宽(w, mode):
    os.chmod(w.md, mode)
    path = ab._backup_persona(str(w.md), b"old")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_备份_同一秒里依次取01到99_已有的一份都不盖(w):
    names = [os.path.basename(ab._backup_persona(str(w.md), b"v%d" % i)) for i in range(4)]
    assert names == ["CLAUDE.md.bak.20261013-142530", "CLAUDE.md.bak.20261013-142530-01",
                     "CLAUDE.md.bak.20261013-142530-02", "CLAUDE.md.bak.20261013-142530-03"]
    assert [(w.ch / n).read_bytes() for n in names] == [b"v0", b"v1", b"v2", b"v3"]


def test_备份_同一秒的一百个名字都占满了_报错且不盖掉任何一份(w):
    for i in range(100):
        ab._backup_persona(str(w.md), b"v%d" % i)
    before = {n: (w.ch / n).read_bytes() for n in _baks(w.ch)}
    assert len(before) == 100
    with pytest.raises(FileExistsError):
        ab._backup_persona(str(w.md), b"one-too-many")
    assert {n: (w.ch / n).read_bytes() for n in _baks(w.ch)} == before


def test_备份_过了一秒就用新的时间戳_不带后缀(w):
    ab._backup_persona(str(w.md), b"a")
    w.clock.tick()
    assert os.path.basename(ab._backup_persona(str(w.md), b"b")) == "CLAUDE.md.bak.20261013-142531"


@needs_perm
def test_备份_目录不可写_照抛且什么都没留下(w):
    os.chmod(w.ch, 0o555)
    try:
        with pytest.raises(OSError):
            ab._backup_persona(str(w.md), b"old")
    finally:
        os.chmod(w.ch, 0o755)
    assert _baks(w.ch) == []


def test_备份_写到一半失败_不留半截备份(w, monkeypatch):
    real = os.fdopen

    class _Broken:
        def __init__(self, f):
            self._f = f

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._f.close()
            return False

        def write(self, data):
            self._f.write(data[:1])
            raise OSError(28, "No space left on device")
    monkeypatch.setattr(ab.os, "fdopen", lambda fd, mode: _Broken(real(fd, mode)))
    with pytest.raises(OSError):
        ab._backup_persona(str(w.md), b"old-content")
    assert _baks(w.ch) == []


# ============================================================ 清理：只认名字合规的，删最老的

@pytest.mark.parametrize("tail,ok", [
    ("20261013-142530", True), ("20261013-142530-01", True), ("20261013-142530-99", True),
    ("20261013-142530\n", False), ("20261013-142530-1", False), ("20261013-142530-001", False),
    ("20261013", False), ("manual", False), ("", False), ("20261013-142530x", False),
    ("x20261013-142530", False), ("2026101-142530", False),
])
def test_备份名里的时间戳_整串匹配(tail, ok):
    assert (ab.PERSONA_BAK_ID_RE.match(tail) is not None) is ok


def test_清理_超过2份_删最老的_留最近2份(w):
    names = ["CLAUDE.md.bak.2020010%d-000000" % d for d in range(1, 9)]
    for n in names:
        (w.ch / n).write_text(n, encoding="utf-8")
    ab._prune_persona_backups(str(w.ch))
    assert _baks(w.ch) == names[6:]


def test_清理_同一秒里不带后缀的比带后缀的老(w):
    names = ["CLAUDE.md.bak.20200101-000000"] + ["CLAUDE.md.bak.20200101-000000-%02d" % i
                                                  for i in range(1, 6)]
    for n in names:
        (w.ch / n).write_text(n, encoding="utf-8")
    ab._prune_persona_backups(str(w.ch))
    assert _baks(w.ch) == names[4:], "没带后缀的那份是同一秒里最先建的，该在删掉的那几份里"


def test_清理_恰好2份_一份都不删(w):
    names = ["CLAUDE.md.bak.2020010%d-000000" % d for d in range(1, 3)]
    for n in names:
        (w.ch / n).write_text(n, encoding="utf-8")
    ab._prune_persona_backups(str(w.ch))
    assert _baks(w.ch) == names


def test_清理_名字不合规的不算备份_不占名额也不会被删(w):
    olds = ["CLAUDE.md.bak.2020010%d-000000" % d for d in range(1, 7)]
    bystanders = ["CLAUDE.md.bak", "CLAUDE.md.bak.manual", "CLAUDE.md.bak.20200101",
                  "CLAUDE.md.bak.20200101-000000\n", "CLAUDE.md.bak.20200101-000000.txt",
                  "CLAUDE.md.backup", "xCLAUDE.md.bak.20200101-000000", "notes.txt"]
    for n in olds + bystanders:
        (w.ch / n).write_text("别碰我：" + n, encoding="utf-8")
    ab._prune_persona_backups(str(w.ch))
    left = {p.name for p in w.ch.iterdir()}
    assert left == set(olds[4:]) | set(bystanders) | {"CLAUDE.md", "access.json"}
    for n in bystanders:
        assert (w.ch / n).read_text(encoding="utf-8") == "别碰我：" + n


def test_清理_目录读不了_不抛(w):
    ab._prune_persona_backups(str(w.tmp / "no-such-dir"))


# ============================================================ 写接口：什么时候留、留在哪一步

def test_保存_改人设_先留备份再改写_备份是旧人设(w):
    body, code = ab.edit_save(BOT, {"persona": "新人设\n"})
    assert code == 200 and body["saved"]["persona"] is True
    assert _baks(w.ch) == ["CLAUDE.md.bak.20261013-142530"]
    assert (w.ch / _baks(w.ch)[0]).read_bytes() == OLD.encode("utf-8")
    assert w.md.read_bytes() == "新人设\n".encode("utf-8")


def test_保存_备份建在第一个字节之前_配置还没被写过(w, monkeypatch):
    seen = {}
    real = ab._backup_persona

    def spy(md_path, old):
        seen["yml"] = w.yml.read_text(encoding="utf-8")
        seen["md"] = w.md.read_bytes()
        return real(md_path, old)
    monkeypatch.setattr(ab, "_backup_persona", spy)
    ab.edit_save(BOT, {"display_name": "新名", "persona": "新人设\n"})
    assert '"旧名"' in seen["yml"] and seen["md"] == OLD.encode("utf-8")


def test_保存_旧人设不是合法UTF8_照样能改_备份逐字节是那份坏的(w):
    raw = b"\xff\xfe\x00bad \x80 bytes"
    w.md.write_bytes(raw)
    body, code = ab.edit_save(BOT, {"persona": "修好了\n"})
    assert code == 200
    assert (w.ch / _baks(w.ch)[0]).read_bytes() == raw


@pytest.mark.parametrize("body", [{"display_name": "新名"}, {"fish_voice_id": "v-new"},
                                  {"display_name": "新名", "fish_voice_id": ""}])
def test_保存_没带人设这个键_不建备份_人设一个字节不动(w, monkeypatch, body):
    monkeypatch.setattr(ab, "_backup_persona", lambda *a: pytest.fail("没带 persona 却去建备份"))
    before = (w.md.read_bytes(), os.stat(w.md).st_mtime_ns)
    assert ab.edit_save(BOT, body)[1] == 200
    assert (w.md.read_bytes(), os.stat(w.md).st_mtime_ns) == before and _baks(w.ch) == []


def test_保存_内容与现有的相同_照写但不留备份(w):
    body, code = ab.edit_save(BOT, {"persona": OLD})
    assert code == 200 and body["saved"]["persona"] is True and _baks(w.ch) == []


def test_保存_原来没有人设文件_不留备份(w):
    w.md.unlink()
    assert ab.edit_save(BOT, {"persona": "第一份\n"})[1] == 200
    assert _baks(w.ch) == [] and w.md.read_bytes() == "第一份\n".encode("utf-8")


def test_保存_连改7次_留下的是最近2次改写之前的内容(w):
    w.md.write_text("v0\n", encoding="utf-8")
    for i in range(1, 8):
        w.clock.tick()
        assert ab.edit_save(BOT, {"persona": "v%d\n" % i})[1] == 200
    assert [(w.ch / n).read_text(encoding="utf-8") for n in _baks(w.ch)] == \
        ["v5\n", "v6\n"]


def test_保存_同一秒连改两次_两份都在_后一份带01(w):
    w.md.write_text("v0\n", encoding="utf-8")
    ab.edit_save(BOT, {"persona": "v1\n"})
    ab.edit_save(BOT, {"persona": "v2\n"})
    assert {n: (w.ch / n).read_text(encoding="utf-8") for n in _baks(w.ch)} == {
        "CLAUDE.md.bak.20261013-142530": "v0\n", "CLAUDE.md.bak.20261013-142530-01": "v1\n"}


# ============================================================ 写接口：失败时备份跟着恢复

@needs_perm
def test_保存_备份建不成_500且说没改动任何文件_人设与配置都不动(w):
    yml_before = w.yml.read_bytes()
    os.chmod(w.ch, 0o555)
    try:
        got = _err(ab.edit_save, BOT, {"display_name": "新名", "persona": "新人设\n"})
    finally:
        os.chmod(w.ch, 0o755)
    assert got == (500, "config_write_failed", "写配置失败：PermissionError；这次没有改动任何文件")
    assert w.md.read_bytes() == OLD.encode("utf-8") and w.yml.read_bytes() == yml_before
    assert _baks(w.ch) == [] and w.saved == []


def test_保存_后面写朋友圈库失败_三处恢复原样_这次建的备份也删掉(w, monkeypatch):
    def boom(cfg, bot, value, undo):
        raise RuntimeError("db is locked")
    monkeypatch.setattr(ab, "_save_db_name", boom)
    yml_before, acc_before = w.yml.read_bytes(), w.acc.read_bytes()
    got = _err(ab.edit_save, BOT, {"display_name": "新名", "persona": "新人设\n", "fish_voice_id": "v2"})
    assert got == (500, "config_write_failed", "写配置失败：RuntimeError；这次没有改动任何文件")
    assert (w.yml.read_bytes(), w.acc.read_bytes(), w.md.read_bytes()) == \
        (yml_before, acc_before, OLD.encode("utf-8"))
    assert _baks(w.ch) == [], "说没改动任何文件，却留下了这次建的备份"


def test_保存_失败恢复只删这次建的备份_以前的备份不动(w, monkeypatch):
    (w.ch / "CLAUDE.md.bak.20200101-000000").write_text("很久以前的", encoding="utf-8")
    monkeypatch.setattr(ab, "_save_db_name",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("db is locked")))
    _err(ab.edit_save, BOT, {"display_name": "新名", "persona": "新人设\n"})
    assert _baks(w.ch) == ["CLAUDE.md.bak.20200101-000000"]
    assert (w.ch / "CLAUDE.md.bak.20200101-000000").read_text(encoding="utf-8") == "很久以前的"


def test_保存_失败且备份删不掉_如实说部分文件已改动(w, monkeypatch):
    monkeypatch.setattr(ab, "_save_db_name",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("db is locked")))
    monkeypatch.setattr(ab, "_drop_backup", lambda p: (_ for _ in ()).throw(PermissionError(p)))
    got = _err(ab.edit_save, BOT, {"display_name": "新名", "persona": "新人设\n"})
    assert got[2] == "写配置失败：RuntimeError；部分文件已改动，请手工核对"


def test_删备份_已经不在了不算错(w):
    ab._drop_backup(str(w.ch / "CLAUDE.md.bak.20261013-142530"))


def test_保存_失败的那次不触发清理_旧备份一份不少(w, monkeypatch):
    olds = ["CLAUDE.md.bak.2020010%d-000000" % d for d in range(1, 6)]
    for n in olds:
        (w.ch / n).write_text(n, encoding="utf-8")
    monkeypatch.setattr(ab, "_save_db_name",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("db is locked")))
    _err(ab.edit_save, BOT, {"display_name": "新名", "persona": "新人设\n"})
    assert _baks(w.ch) == olds, "保存没成功，却把最老的备份清掉了"


# ============================================================ 写接口：空的人设正文不接受

@pytest.mark.parametrize("blank", ["", " ", "\n", " \n\t \r\n ", "\u3000", "\u2028"])
def test_保存_人设为空或全是空白_400_什么都不写(w, blank):
    before = (w.yml.read_bytes(), w.acc.read_bytes(), w.md.read_bytes())
    assert _err(ab.edit_save, BOT, {"display_name": "新名", "persona": blank, "fish_voice_id": ""}) == \
        (400, "bad_field", EMPTY)
    assert (w.yml.read_bytes(), w.acc.read_bytes(), w.md.read_bytes()) == before
    assert _baks(w.ch) == [] and w.saved == []


def test_保存_判序_不是字符串先于空_名字的错先于空人设_空人设先于音色与归属(w):
    assert _err(ab.edit_save, BOT, {"persona": None})[2] == "persona 必须是字符串"
    assert _err(ab.edit_save, BOT, {"display_name": "  ", "persona": ""})[2] != EMPTY
    assert _err(ab.edit_save, BOT, {"persona": "", "fish_voice_id": "bad.dot"})[2] == EMPTY
    assert _err(ab.edit_save, "ghostbot", {"persona": ""})[2] == EMPTY


def test_保存_只有空白包着的正文_不算空_原样落盘(w):
    assert ab.edit_save(BOT, {"persona": "\n  正文  \n"})[1] == 200
    assert w.md.read_bytes() == "\n  正文  \n".encode("utf-8")


# ============================================================ 读接口：人设文件编码坏了

def test_读_人设不是合法UTF8_409_bad_persona_文件不动(w):
    raw = b"\xff\xfe\x00bad \x80 bytes"
    w.md.write_bytes(raw)
    assert _err(ab.edit_read, BOT) == (409, "bad_persona", BAD_FILE)
    assert w.md.read_bytes() == raw


def test_读_判序_配置解析不出先于人设编码坏(w):
    w.md.write_bytes(b"\xff\xfe")
    w.yml.write_text("id: [oops\n  x: : :\n", encoding="utf-8")
    assert _err(ab.edit_read, BOT)[:2] == (409, "bad_yaml")


@needs_perm
def test_读_判序_文件读不出是500_先于编码坏(w):
    w.md.write_bytes(b"\xff\xfe")
    os.chmod(w.md, 0)
    try:
        got = _err(ab.edit_read, BOT)
    finally:
        os.chmod(w.md, 0o600)
    assert got == (500, "config_unreadable", "PermissionError")


def test_读_合法的人设与没有人设文件_照旧(w):
    assert ab.edit_read(BOT)["persona"] == OLD
    w.md.unlink()
    assert ab.edit_read(BOT)["persona"] == ""
