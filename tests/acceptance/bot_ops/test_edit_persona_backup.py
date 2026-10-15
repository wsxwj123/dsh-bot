# -*- coding: utf-8 -*-
"""§10 编辑接口（接口约定第 8 稿）：空人设被拒、长度按字符数、人设文件编码坏了怎么答、
没给的键不动、人设改写前留备份。

起因（§13c）：编辑弹窗读取失败后仍能保存，而保存每次把三个键全交上来，于是填一个名字点
保存，人设被写成空文件、音色被删掉。后端这一侧的三道保险在这里量：
1. 空的人设正文不再接受；
2. 请求体里没出现的键，对应的那一口一个字节都不动；
3. 人设被改写之前先留一份备份，最多 2 份。

显示名的字符规则在 test_display_name_rules.py。
"""
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from . import botops_util as u

IS_WIN = sys.platform == "win32"
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
needs_perm = pytest.mark.skipif(IS_WIN or IS_ROOT, reason="要靠权限造失败：Windows 与 root 下造不出来")

BOT = "xiaoyu"
OLD = "# 旧人设\n说话简短。\n"

#: §10「人设备份」的名字：`CLAUDE.md.bak.<年月日-时分秒>`，同一秒里重名的依次加 `-01` 到 `-99`
BAK_RE = re.compile(r"CLAUDE\.md\.bak\.(\d{8}-\d{6})(?:-(\d{2}))?")

EMPTY_PERSONA = "人设正文不能为空；不想改人设就不要带 persona 这个键"
BROKEN_PERSONA = "人设正文里有残缺的字符（多半是半个表情符号），删掉它再保存"
TOO_LONG = "人设正文超过 65536 个字符"
BAD_NAME = "display_name 必须是 1-64 字的字符串，且不含控制字符"
BAD_PERSONA_FILE = "人设文件不是合法的 UTF-8 文本，这一页读不了它；先把文件修好再来编辑"


def _url(bot_id=BOT):
    return "/hub/api/bots/%s/edit" % bot_id


def _post(client, body, bot_id=BOT):
    """请求体用标准库序列化：落单的代理写成 `\\ud83d` 这样的转义，照样发得出去。"""
    return client.post(_url(bot_id), data=json.dumps(body), content_type="application/json")


def _bot(sandbox, *, persona=OLD, access='{"allowFrom": [1], "voiceId": "v-old"}'):
    """一只新系统的 bot：配置、人设、白名单三样俱全（给 None 就不放那个文件）。"""
    yml = u.write_bot_yml(sandbox.dsh_cfg, BOT, display_name="旧名", dispatcher_port=17951)
    ch = sandbox.dsh_home / "bots" / BOT / "channel"
    ch.mkdir(parents=True)
    md, acc = ch / "CLAUDE.md", ch / "access.json"
    if persona is not None:
        md.write_bytes(persona.encode("utf-8") if isinstance(persona, str) else persona)
        os.chmod(md, 0o600)
    if access is not None:
        acc.write_text(access, encoding="utf-8")
        os.chmod(acc, 0o600)
    return SimpleNamespace(yml=yml, channel=ch, md=md, acc=acc)


def _baks(ch):
    """名字符合 §10 形状的备份，按名字（也就是时间戳）从老到新。"""
    return sorted((p for p in ch.iterdir() if BAK_RE.fullmatch(p.name)), key=lambda p: p.name)


def _bak_like(ch):
    """任何看起来像备份的东西（形状不对的也算），给「没有产生备份」用。"""
    return sorted(p.name for p in ch.iterdir() if p.name != "CLAUDE.md" and "CLAUDE.md" in p.name)


def _stat(path):
    st = path.stat()
    return (path.read_bytes(), st.st_mtime_ns)


def _profiles(sandbox):
    con = sqlite3.connect(str(sandbox.tmp / "state.db"))
    try:
        return sorted(con.execute("SELECT * FROM bot_profiles").fetchall(), key=repr)
    finally:
        con.close()


# ============================================================ 空的人设正文被拒

@pytest.mark.parametrize("blank", ["", " ", "\n", " \n\t \r\n "],
                         ids=["empty", "space", "newline", "mixed-blank"])
def test_编辑_人设为空或纯空白_400_bad_field与固定文案(web_client, sandbox, blank):
    _bot(sandbox)
    r = _post(web_client, {"persona": blank})
    assert r.status_code == 400, (r.status_code, r.get_json())
    assert r.get_json() == {"error": "bad_field", "detail": EMPTY_PERSONA}, r.get_json()


def test_编辑_人设为空_原来的人设文件逐字节不变(web_client, sandbox):
    """BRIEF 之外的那次事故：读取失败后点保存，人设被写成空文件。后端不再接受。"""
    b = _bot(sandbox)
    _post(web_client, {"persona": ""})
    assert b.md.read_bytes() == OLD.encode("utf-8")


def test_编辑_三个键全交且人设是空的_整次请求被拒_名字与音色也不改(web_client, sandbox):
    """旧页面每次把三个键全交上来；人设那格是空的时，另外两格也不许落盘。"""
    b = _bot(sandbox)
    frozen = u.freeze_roots(sandbox.dsh_cfg, b.channel)
    r = _post(web_client, {"display_name": "新名", "persona": "", "fish_voice_id": ""})
    assert r.status_code == 400, (r.status_code, r.get_json())
    assert r.get_json()["detail"] == EMPTY_PERSONA, r.get_json()
    u.assert_roots_untouched(frozen)


def test_编辑_人设为空_排在404之前(web_client, sandbox):
    """§10b 判定顺序：字段形状错先于归属。"""
    r = _post(web_client, {"persona": ""}, bot_id="ghostbot")
    assert r.status_code == 400, (r.status_code, r.get_json())
    assert r.get_json()["detail"] == EMPTY_PERSONA, r.get_json()


def test_编辑_判序_名字的错先于空人设(web_client, sandbox):
    _bot(sandbox)
    r = _post(web_client, {"display_name": "   ", "persona": ""})
    assert r.status_code == 400, (r.status_code, r.get_json())
    assert r.get_json()["detail"] == BAD_NAME, r.get_json()


def test_编辑_判序_空人设先于音色的错(web_client, sandbox):
    _bot(sandbox)
    r = _post(web_client, {"persona": "", "fish_voice_id": "bad.dot"})
    assert r.status_code == 400, (r.status_code, r.get_json())
    assert r.get_json()["detail"] == EMPTY_PERSONA, r.get_json()


# ============================================================ 残缺的字符与长度上限

@pytest.mark.parametrize("lone", ["\ud83d", "\ude00"], ids=["high-surrogate", "low-surrogate"])
def test_编辑_人设里有落单的代理_400_bad_field与固定文案(web_client, sandbox, lone):
    _bot(sandbox)
    r = _post(web_client, {"persona": "前半句" + lone + "后半句"})
    assert r.status_code == 400, (r.status_code, r.get_json())
    assert r.get_json() == {"error": "bad_field", "detail": BROKEN_PERSONA}, r.get_json()


def test_编辑_人设65537个汉字_413与固定文案(web_client, sandbox):
    _bot(sandbox)
    r = _post(web_client, {"persona": "字" * 65537})
    assert r.status_code == 413, (r.status_code, r.get_json())
    assert r.get_json() == {"error": "payload_too_large", "detail": TOO_LONG}, r.get_json()


def test_编辑_人设恰好65536个汉字_放行且原样落盘(web_client, sandbox):
    """上限按字符数：65536 个汉字是 196608 字节，此前按字节数算会被拒。"""
    b = _bot(sandbox)
    text = "字" * 65536
    r = _post(web_client, {"persona": text})
    assert r.status_code == 200, (r.status_code, r.get_json())
    assert b.md.read_bytes() == text.encode("utf-8")


def test_编辑_人设里有完整的表情符号_放行且原样落盘(web_client, sandbox):
    """对照：成对的代理（完整的表情）不是残缺字符。"""
    b = _bot(sandbox)
    text = "爱笑 😀，一家人 👨‍👩‍👧‍👦。\n"
    r = _post(web_client, {"persona": text})
    assert r.status_code == 200, (r.status_code, r.get_json())
    assert b.md.read_bytes() == text.encode("utf-8")


# ============================================================ 被拒时什么都不变，也不留备份

@pytest.mark.parametrize("body,code", [
    pytest.param({"persona": ""}, 400, id="empty-persona"),
    pytest.param({"persona": "  \n"}, 400, id="blank-persona"),
    pytest.param({"persona": "半个\ud83d表情"}, 400, id="lone-surrogate"),
    pytest.param({"persona": None}, 400, id="null-persona"),
    pytest.param({"persona": "字" * 65537}, 413, id="too-long"),
    pytest.param({"display_name": "小 雨", "persona": "新人设"}, 400, id="bad-name"),
    pytest.param({"persona": "新人设", "fish_voice_id": "bad.dot"}, 400, id="bad-voice"),
    pytest.param({}, 400, id="no-keys"),
])
def test_编辑_被拒_配置人设白名单逐字节不变且没有产生备份(web_client, sandbox, body, code):
    b = _bot(sandbox)
    frozen = u.freeze_roots(sandbox.dsh_cfg, b.channel)
    r = _post(web_client, body)
    assert r.status_code == code, (r.status_code, r.get_json())
    u.assert_roots_untouched(frozen)
    assert _bak_like(b.channel) == [], _bak_like(b.channel)


# ============================================================ 读接口：人设文件编码坏了

def test_GET_人设文件不是合法UTF8_409_bad_persona与固定文案(web_client, sandbox):
    _bot(sandbox, persona=b"\xff\xfe\x00bad \x80 bytes")
    r = web_client.get(_url())
    assert r.status_code == 409, (r.status_code, r.get_json())
    assert r.get_json() == {"error": "bad_persona", "detail": BAD_PERSONA_FILE}, r.get_json()


def test_GET_人设文件不是合法UTF8_那个文件逐字节不变(web_client, sandbox):
    raw = b"\xff\xfe\x00bad \x80 bytes"
    b = _bot(sandbox, persona=raw)
    frozen = u.freeze_roots(b.channel)
    web_client.get(_url())
    u.assert_roots_untouched(frozen)
    assert b.md.read_bytes() == raw


def test_GET_判序_配置解析不出先于人设编码坏(web_client, sandbox):
    b = _bot(sandbox, persona=b"\xff\xfe")
    b.yml.write_text("id: [oops\n  x: : :\n", encoding="utf-8")
    r = web_client.get(_url())
    assert r.status_code == 409, (r.status_code, r.get_json())
    assert r.get_json()["error"] == "bad_yaml", r.get_json()


# ============================================================ 没给的键不动

def test_编辑_只带名字_人设与白名单的字节和修改时间都不变(web_client, sandbox):
    b = _bot(sandbox)
    before = (_stat(b.md), _stat(b.acc))
    r = _post(web_client, {"display_name": "新名"})
    assert r.status_code == 200, (r.status_code, r.get_json())
    assert (_stat(b.md), _stat(b.acc)) == before, "只改名字，人设或白名单却被重写过"


def test_编辑_只带名字_频道目录没被写过也没有备份(web_client, sandbox):
    b = _bot(sandbox)
    frozen = u.freeze_roots(b.channel)
    r = _post(web_client, {"display_name": "新名"})
    assert r.status_code == 200, (r.status_code, r.get_json())
    u.assert_roots_untouched(frozen)
    assert _bak_like(b.channel) == [], _bak_like(b.channel)


def test_编辑_只带人设_配置文件与白名单的字节和修改时间都不变(web_client, sandbox):
    b = _bot(sandbox)
    frozen_cfg = u.freeze_roots(sandbox.dsh_cfg)
    acc_before = _stat(b.acc)
    r = _post(web_client, {"persona": "换一份人设\n"})
    assert r.status_code == 200, (r.status_code, r.get_json())
    u.assert_roots_untouched(frozen_cfg)
    assert _stat(b.acc) == acc_before, "只改人设，白名单却被重写过"


def test_编辑_只带人设_朋友圈库里的名字不变(web_client, sandbox):
    _bot(sandbox)
    assert _post(web_client, {"display_name": "库里的名字"}).status_code == 200    # 先让库里有这一行
    before = _profiles(sandbox)
    assert before, "夹具自检：库里应当已经有这只的名字"
    r = _post(web_client, {"persona": "换一份人设\n"})
    assert r.status_code == 200, (r.status_code, r.get_json())
    assert _profiles(sandbox) == before


def test_编辑_只带音色_配置文件与人设的字节和修改时间都不变(web_client, sandbox):
    b = _bot(sandbox)
    frozen_cfg = u.freeze_roots(sandbox.dsh_cfg)
    md_before = _stat(b.md)
    r = _post(web_client, {"fish_voice_id": "v-new"})
    assert r.status_code == 200, (r.status_code, r.get_json())
    u.assert_roots_untouched(frozen_cfg)
    assert _stat(b.md) == md_before, "只改音色，人设却被重写过"


def test_编辑_只带音色_不产生人设备份(web_client, sandbox):
    b = _bot(sandbox)
    r = _post(web_client, {"fish_voice_id": "v-new"})
    assert r.status_code == 200, (r.status_code, r.get_json())
    assert _bak_like(b.channel) == [], _bak_like(b.channel)


# ============================================================ 人设备份

def test_备份_改一次人设_目录里多出恰好一份且名字合规(web_client, sandbox):
    b = _bot(sandbox)
    r = _post(web_client, {"persona": "新人设\n"})
    assert r.status_code == 200, (r.status_code, r.get_json())
    assert len(_baks(b.channel)) == 1, _bak_like(b.channel)
    assert _bak_like(b.channel) == [p.name for p in _baks(b.channel)], \
        ("多出了名字不合规的备份", _bak_like(b.channel))


def test_备份_改一次人设_备份的内容是改之前的原文(web_client, sandbox):
    b = _bot(sandbox)
    _post(web_client, {"persona": "新人设\n"})
    baks = _baks(b.channel)
    assert len(baks) == 1, _bak_like(b.channel)
    assert baks[0].read_bytes() == OLD.encode("utf-8")
    assert b.md.read_bytes() == "新人设\n".encode("utf-8")


@pytest.mark.skipif(IS_WIN, reason="Windows 无 POSIX 权限位")
@pytest.mark.parametrize("mode", [0o600, 0o644], ids=["persona-0600", "persona-0644"])
def test_备份_权限是0600_不跟着原文件走(web_client, sandbox, mode):
    b = _bot(sandbox)
    os.chmod(b.md, mode)
    _post(web_client, {"persona": "新人设\n"})
    baks = _baks(b.channel)
    assert len(baks) == 1, _bak_like(b.channel)
    assert u.mode_of(baks[0]) == 0o600, oct(u.mode_of(baks[0]))


def test_备份_名字里的时间戳是本地时间且就是刚才(web_client, sandbox):
    b = _bot(sandbox)
    t0 = datetime.now().replace(microsecond=0)
    _post(web_client, {"persona": "新人设\n"})
    t1 = datetime.now()
    baks = _baks(b.channel)
    assert len(baks) == 1, _bak_like(b.channel)
    stamp = datetime.strptime(BAK_RE.fullmatch(baks[0].name).group(1), "%Y%m%d-%H%M%S")
    assert t0 <= stamp <= t1, (baks[0].name, t0, t1)


def test_备份_原来没有人设文件_不留备份(web_client, sandbox):
    b = _bot(sandbox, persona=None)
    r = _post(web_client, {"persona": "第一份人设\n"})
    assert r.status_code == 200, (r.status_code, r.get_json())
    assert _bak_like(b.channel) == [], _bak_like(b.channel)


def test_备份_提交与现有内容相同的人设_没有新备份(web_client, sandbox):
    b = _bot(sandbox)
    r = _post(web_client, {"persona": OLD})
    assert r.status_code == 200, (r.status_code, r.get_json())
    assert r.get_json()["saved"]["persona"] is True, r.get_json()
    assert _bak_like(b.channel) == [], _bak_like(b.channel)


def test_备份_名字与人设一起改_备份照留(web_client, sandbox):
    b = _bot(sandbox)
    r = _post(web_client, {"display_name": "新名", "persona": "新人设\n"})
    assert r.status_code == 200, (r.status_code, r.get_json())
    assert [p.read_bytes() for p in _baks(b.channel)] == [OLD.encode("utf-8")]


def test_备份_连着改7次每次都不同_恰好留2份且是最近2次改写之前的内容(web_client, sandbox):
    b = _bot(sandbox, persona="v0\n")
    for i in range(1, 8):
        time.sleep(1.05)                    # 每次落在不同的一秒：时间戳各不相同，谁老谁新没有歧义
        r = _post(web_client, {"persona": "v%d\n" % i})
        assert r.status_code == 200, (i, r.status_code, r.get_json())
    got = [p.read_bytes().decode("utf-8") for p in _baks(b.channel)]
    assert got == ["v5\n", "v6\n"], got
    assert b.md.read_bytes() == b"v7\n"


def test_备份_同一秒里连改两次_两份都留下且不同名(web_client, sandbox):
    """同一秒里已经有同名的，后面加 `-01`：不许后一份把前一份盖掉。"""
    b = _bot(sandbox, persona="v0\n")
    assert _post(web_client, {"persona": "v1\n"}).status_code == 200
    assert _post(web_client, {"persona": "v2\n"}).status_code == 200
    baks = _baks(b.channel)
    assert sorted(p.read_bytes() for p in baks) == [b"v0\n", b"v1\n"], _bak_like(b.channel)
    first = [p for p in baks if p.read_bytes() == b"v0\n"][0]
    second = [p for p in baks if p.read_bytes() == b"v1\n"][0]
    m1, m2 = BAK_RE.fullmatch(first.name), BAK_RE.fullmatch(second.name)
    if m1.group(1) == m2.group(1):          # 真落在了同一秒：后一份必须是 -01
        assert (m1.group(2), m2.group(2)) == (None, "01"), (first.name, second.name)


def test_备份_清理只删名字合规里最老的_别的文件不碰(web_client, sandbox):
    b = _bot(sandbox)
    olds = ["CLAUDE.md.bak.2020010%d-000000" % d for d in range(1, 7)]      # 6 份合规的旧备份
    for name in olds:
        (b.channel / name).write_text(name, encoding="utf-8")
    bystanders = ["CLAUDE.md.bak", "CLAUDE.md.bak.manual", "CLAUDE.md.bak.20200101",
                  "CLAUDE.md.backup", "notes.txt"]
    for name in bystanders:
        (b.channel / name).write_text("别碰我：" + name, encoding="utf-8")
    r = _post(web_client, {"persona": "新人设\n"})
    assert r.status_code == 200, (r.status_code, r.get_json())
    names = [p.name for p in _baks(b.channel)]
    assert len(names) == 2, names
    assert names[:1] == olds[5:], ("该删的是最老的五份", names)
    assert (b.channel / names[1]).read_bytes() == OLD.encode("utf-8")
    for name in bystanders:
        assert (b.channel / name).read_text(encoding="utf-8") == "别碰我：" + name, name


def test_备份_后面某一步写入失败_500且detail说没改任何文件_目录里没有多出备份(
        web_client, sandbox):
    """朋友圈库写不了：名字、人设都已写过，要全部恢复，这次建的备份也要一并删掉。"""
    b = _bot(sandbox)
    db_file = sandbox.tmp / "state.db"
    os.chmod(db_file, 0)
    try:
        r = _post(web_client, {"display_name": "新名", "persona": "新人设\n"})
    finally:
        os.chmod(db_file, 0o600)
    assert r.status_code == 500, (r.status_code, r.get_json())
    body = r.get_json()
    assert body["error"] == "config_write_failed", body
    assert (body.get("detail") or "").endswith("这次没有改动任何文件"), body
    assert _bak_like(b.channel) == [], ("说没改动任何文件，却留下了备份", _bak_like(b.channel))
    assert b.md.read_bytes() == OLD.encode("utf-8")


@needs_perm
def test_备份_备份建不成_500_config_write_failed且人设不动(web_client, sandbox):
    b = _bot(sandbox)
    os.chmod(b.channel, 0o555)
    try:
        r = _post(web_client, {"persona": "新人设\n"})
    finally:
        os.chmod(b.channel, 0o755)
    assert r.status_code == 500, (r.status_code, r.get_json())
    assert r.get_json()["error"] == "config_write_failed", r.get_json()
    assert b.md.read_bytes() == OLD.encode("utf-8")
    assert _bak_like(b.channel) == [], _bak_like(b.channel)
