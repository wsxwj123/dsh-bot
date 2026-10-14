# -*- coding: utf-8 -*-
"""适配服务 v0.8 验收：人设 GET / PUT /adapter/v1/bots/{bot_id}/persona（补充文件 10.2，v0.8 草案 A）。

依据：INTERFACE-适配服务.md 10.2；INTERFACE-适配约定-v0.8-dsh侧.md A.1、A.2。不读实现。
人设文件是这只 bot 频道目录（配置里的 bot_channel_path）下的 CLAUDE.md；备份目录是
<DSH_BOT_HOME>/backups/persona/<bot_id>/。版本号一律在用例里按约定的算法自己算，不靠 GET。
鉴权、bot_id 校验、方法不对、频道目录落在家目录外：四个新入口一起放在 test_adapter_v08_entry.py。
"""
import os

import pytest

from . import adapter_util as au
from .adapter_util import adapter, console_guard  # noqa: F401  夹具

IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
OLD = u"# 小A 的人设\n你是小A，爱猫，说话慢。\n"
NEW = u"# 小A 的人设\n你是小A，爱猫 🐱，说话慢，偶尔用「嗯……」开头。\n"
OTHER = u"# 小A 的人设\n管理台编辑页刚改过的一版。\n"
OLD_SYSTEM = u"# 另一套系统那只 bot 的人设 OLDSYS-PERSONA-7731\n"
EMPTY_VERSION = au.persona_version(b"")          # 文件不存在时按空内容算
V_OLD = au.persona_version(OLD)
LIMIT_TEXT = u"中" * 87381 + u"a"               # 87381×3+1 = 262144 字节，正好 256 KiB
OVER_TEXT = u"中" * 87381 + u"ab"               # 262145 字节，但只有 87383 个字符：按字符数数会放过


def persona_bot(adapter, content=OLD, enabled=True, mode=0o640):
    """botalpha（没有网关）；content 不是 None 就在频道目录写好 CLAUDE.md（权限 mode，修改时间在过去）。"""
    bot = adapter.bot("botalpha", enabled=enabled)
    if content is not None:
        au.put_file(au.persona_path(bot), content, mode=mode)
    return bot


def put_persona(adapter, payload, raw=None):
    return adapter.call("PUT", au.persona_url("botalpha"), payload, raw=raw)


def no_backup(adapter):
    assert au.persona_backups(adapter.sandbox, "botalpha") == [], \
        "不该产生备份，实际有 %r" % au.persona_backups(adapter.sandbox, "botalpha")


# ============================================================ GET

READS = {
    "中文与emoji原样回": (u"# 小A\n爱猫 🐱，说话慢。\n".encode("utf-8"), True),
    "CRLF换行原样保留不改成LF": (u"第一行\r\n第二行\r\n".encode("utf-8"), True),
    "文件不存在_text为空串_版本号按空内容算": (None, True),
    "停用的bot也能读": (OLD.encode("utf-8"), False),
}


@pytest.mark.parametrize("raw,enabled", list(READS.values()), ids=list(READS))
def test_GET人设_回原文全文与版本号_版本号是原始字节SHA256前16位_不动文件(adapter, raw, enabled):
    bot = persona_bot(adapter, content=raw, enabled=enabled)
    path = au.persona_path(bot)
    before = au.file_mark(path)
    body = adapter.ok(adapter.call("GET", au.persona_url("botalpha")))
    want_text = (raw or b"").decode("utf-8")
    assert body.get("text") == want_text, "text 应是 CLAUDE.md 按 UTF-8 原样读出的全文，实际 %r" % (body.get("text"),)
    assert body.get("version") == au.persona_version(raw or b""), \
        "version 应是原始字节 SHA-256 前 16 位十六进制，实际 %r" % (body.get("version"),)
    assert au.file_mark(path) == before, "读人设却动了 CLAUDE.md（或凭空建了它）"
    no_backup(adapter)


UNREADABLE = {"内容不是UTF8": "bytes", "没有读权限": "perm"}


@pytest.mark.parametrize("kind", list(UNREADABLE.values()), ids=list(UNREADABLE))
def test_GET人设_读不出_回500internal(adapter, kind):
    if kind == "perm" and IS_ROOT:
        pytest.skip("root 不受文件权限限制")
    if kind == "bytes":
        persona_bot(adapter, content=b"# \xff\xfe\xfa not utf-8 \xc3\x28\n", mode=0o600)
    else:
        persona_bot(adapter, content=OLD, mode=0o000)
    adapter.expect_error(adapter.call("GET", au.persona_url("botalpha")), 500, "internal")


@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_人设CLAUDE_md是软链_GET与PUT都回500internal_指向的文件一个字节没变(adapter, method):
    bot = persona_bot(adapter, content=None)
    target = au.put_file(adapter.sandbox.home / ".claude" / "channels" / "oldbot" / "CLAUDE.md",
                         OLD_SYSTEM, mode=0o600)
    link = au.persona_path(bot)
    os.symlink(str(target), str(link))
    before = au.file_mark(target)
    # 版本号按软链指向的内容算：顺着软链读的实现会以为版本对得上而写进去
    body = {"text": NEW, "version": au.persona_version(OLD_SYSTEM)} if method == "PUT" else au.NO_BODY
    resp = adapter.call(method, au.persona_url("botalpha"), body)
    adapter.expect_error(resp, 500, "internal")
    assert "OLDSYS-PERSONA-7731" not in au.response_text(resp), "软链指向的内容被读出来回给了调用方"
    assert au.file_mark(target) == before, "软链指向的文件被改了"
    assert os.path.islink(str(link)) and os.readlink(str(link)) == str(target), "CLAUDE.md 这个软链本身被换掉了"
    no_backup(adapter)


# ============================================================ PUT：写成功

WRITES = {
    "启用的bot": (NEW, True),
    "停用的bot也能写": (NEW, False),
    "正好256KiB按UTF8字节算": (LIMIT_TEXT, True),
}


@pytest.mark.parametrize("text,enabled", list(WRITES.values()), ids=list(WRITES))
def test_PUT人设_成功_原样落盘_原子写_保持原权限_旧文备份一份0600_回新版本号_不动启动项(adapter, text, enabled):
    bot = persona_bot(adapter, enabled=enabled)          # CLAUDE.md = OLD，权限 0640
    path, channel = au.persona_path(bot), au.channel_of(bot)
    names = sorted(os.listdir(str(channel)))
    env_before = au.file_mark(channel / ".env")
    ino_before = os.stat(str(path)).st_ino
    body = adapter.ok(put_persona(adapter, {"text": text, "version": V_OLD}))
    assert body.get("version") == au.persona_version(text), "应回新内容的版本号，实际 %r" % (body.get("version"),)
    assert body.get("message") == au.PERSONA_SAVED, "成功的 message 应为 %r，实际 %r" % (au.PERSONA_SAVED, body.get("message"))
    assert path.read_bytes() == text.encode("utf-8"), "CLAUDE.md 应原样是请求里的新全文"
    assert au.mode_of(path) == 0o640, "应保持原文件权限 0640，实际 %o" % au.mode_of(path)
    assert os.stat(str(path)).st_ino != ino_before, "应是临时文件写完改名的原子写，实际是原地改写（inode 没变）"
    assert sorted(os.listdir(str(channel))) == names, "频道目录里多了或少了文件（临时文件没清？备份放错地方？）"
    assert au.file_mark(channel / ".env") == env_before, "频道目录里别的文件被动了"
    backups = au.persona_backups(adapter.sandbox, "botalpha")
    assert len(backups) == 1, "应恰好备份一份旧文，实际 %r" % backups
    assert backups[0].read_bytes() == OLD.encode("utf-8"), "备份的内容应是改之前的原文"
    assert au.mode_of(backups[0]) == 0o600, "备份权限应为 0600，实际 %o" % au.mode_of(backups[0])
    assert adapter.launchctl.actions() == [], "保存人设不该动启动项：%r" % adapter.launchctl.actions()


def test_PUT人设_原来没有CLAUDE_md_用空内容的版本号能写入_不产生备份(adapter):
    bot = persona_bot(adapter, content=None)
    body = adapter.ok(put_persona(adapter, {"text": NEW, "version": EMPTY_VERSION}))
    assert body.get("version") == au.persona_version(NEW), body
    assert au.persona_path(bot).read_bytes() == NEW.encode("utf-8")
    no_backup(adapter)


def test_PUT人设_连存7次_备份只留最近5份_同一秒内存的也不互相覆盖(adapter):
    texts = [u"第%d版人设：小A。\n" % i for i in range(8)]       # texts[0] 是原文
    bot = persona_bot(adapter, content=texts[0])
    for prev, cur in zip(texts, texts[1:]):
        adapter.ok(put_persona(adapter, {"text": cur, "version": au.persona_version(prev)}))
    backups = au.persona_backups(adapter.sandbox, "botalpha")
    got = sorted(p.read_text(encoding="utf-8") for p in backups)
    assert got == sorted(texts[2:7]), "应只留最近 5 份（第 2 到第 6 版）且互不覆盖，实际 %r" % got
    assert [au.mode_of(p) for p in backups] == [0o600] * 5, "备份权限都应为 0600"
    assert au.persona_path(bot).read_bytes() == texts[7].encode("utf-8")


# ============================================================ PUT：不该写的时候没写

def test_PUT人设_和当前全文完全相同_回没有改动_不写盘不备份(adapter):
    bot = persona_bot(adapter)
    before = au.file_mark(au.persona_path(bot))
    body = adapter.ok(put_persona(adapter, {"text": OLD, "version": V_OLD}))
    assert body.get("message") == au.PERSONA_SAME, "message 应为 %r，实际 %r" % (au.PERSONA_SAME, body.get("message"))
    assert au.file_mark(au.persona_path(bot)) == before, "没有改动却写了盘（字节、修改时间、权限或 inode 变了）"
    no_backup(adapter)


STALE = {"拿到版本号之后别处改了内容": "edit", "拿到版本号之后别处把文件删了": "delete"}


@pytest.mark.parametrize("change", list(STALE.values()), ids=list(STALE))
def test_PUT人设_版本号已过时_回409stale_别处的改动原样留着_不备份(adapter, change):
    bot = persona_bot(adapter)
    path = au.persona_path(bot)
    if change == "edit":
        au.put_file(path, OTHER, mode=0o640)
    else:
        path.unlink()
    before = au.file_mark(path)
    body = adapter.expect_error(put_persona(adapter, {"text": NEW, "version": V_OLD}), 409, "stale")
    assert body["message"] == au.PERSONA_STALE, "409 的 message 应为 %r，实际 %r" % (au.PERSONA_STALE, body["message"])
    assert au.file_mark(path) == before, "409 时 CLAUDE.md 被动了"
    no_backup(adapter)


BAD_BODIES = {
    "多了第三个键": ({"text": NEW, "version": V_OLD, "restart": True}, None),
    "缺text": ({"version": V_OLD}, None),
    "缺version": ({"text": NEW}, None),
    "text不是字符串": ({"text": 123, "version": V_OLD}, None),
    "text是空串": ({"text": "", "version": V_OLD}, None),
    "text去掉首尾空白后为空": ({"text": " \n\t\r\n ", "version": V_OLD}, None),
    "text超256KiB一个字节_按UTF8字节算": ({"text": OVER_TEXT, "version": V_OLD}, None),
    "version只有15位": ({"text": NEW, "version": V_OLD[:15]}, None),
    "version有非十六进制字符": ({"text": NEW, "version": "g" * 16}, None),
    "version不是字符串": ({"text": NEW, "version": 1234567890123456}, None),
    "请求体是数组": (None, "[]"),
    "坏JSON": (None, '{"text": '),
    "空体": (None, ""),
}


@pytest.mark.parametrize("payload,raw", list(BAD_BODIES.values()), ids=list(BAD_BODIES))
def test_PUT人设_请求体不合规_回422invalid_文件一个字节没变_不备份(adapter, payload, raw):
    bot = persona_bot(adapter)
    before = au.file_mark(au.persona_path(bot))
    adapter.expect_error(put_persona(adapter, payload, raw=raw), 422, "invalid")
    assert au.file_mark(au.persona_path(bot)) == before, "请求体不合规却动了 CLAUDE.md"
    no_backup(adapter)
