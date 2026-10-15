# -*- coding: utf-8 -*-
"""适配服务 v0.9 验收：技能 GET / PUT /adapter/v1/bots/{bot_id}/skills/{name}（补充文件 11.2，v0.9 dsh 侧草案 A）。

依据：INTERFACE-适配服务.md 11.2（及它引用的 10.2、10.6）；INTERFACE-适配约定-v0.9-dsh侧.md A.1、A.2。不读实现。
通用名 → 这只 bot 频道目录下的 skills/skill-<通用名>/SKILL.md；备份目录是 <DSH_BOT_HOME>/backups/skills/<bot_id>/，
文件名带通用名与时间。版本号一律在用例里按约定的算法（原始字节 SHA-256 前 16 位小写十六进制）自己算，不靠 GET。
"""
import os

import pytest

from . import adapter_util as au
from .adapter_util import adapter, console_guard  # noqa: F401  夹具

IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
OLD = u"---\nname: skill-image\n---\n# 发图\n先想好画面再调工具。\n"
NEW = u"---\nname: skill-image\n---\n# 发图 🎨\n先想好画面，再调工具；一次最多两张。\n"
OTHER = u"---\nname: skill-image\n---\n# 发图\n管理台编辑页刚改过的一版。\n"
OLD_SYSTEM = u"# 另一套系统那只 bot 的技能 OLDSYS-SKILL-4417\n"
V_OLD = au.persona_version(OLD)
LIMIT_TEXT = u"中" * 87381 + u"a"               # 87381×3+1 = 262144 字节，正好 256 KiB
OVER_TEXT = u"中" * 87381 + u"ab"               # 262145 字节，但只有 87383 个字符：按字符数数会放过
assert V_OLD.upper() != V_OLD, "V_OLD 恰好全是数字，下面「version 是大写」那一项测不到东西，换一下 OLD"


def skill_bot(adapter, skills=None, enabled=True, mode=0o640):
    """botalpha（没有网关）；skills 是 {通用名: 内容}，不给就只有 image = OLD（权限 mode，修改时间在过去）。"""
    bot = adapter.bot("botalpha", enabled=enabled)
    for name, content in ({"image": OLD} if skills is None else skills).items():
        au.put_file(au.skill_path(bot, name), content, mode=mode)
    return bot


def call_skill(adapter, method, name="image", payload=au.NO_BODY, raw=None, bot_id="botalpha", auth=au.GOOD):
    return adapter.call(method, au.skill_url(bot_id, name), payload, raw=raw, auth=auth)


def no_backup(adapter):
    got = au.skill_backups(adapter.sandbox, "botalpha")
    assert got == [], "不该产生备份，实际有 %r" % got


def old_system_dir(adapter):
    """家目录外另一套系统的频道目录（归属边界外，只许看不许碰）。"""
    return adapter.sandbox.home / ".claude" / "channels" / "oldbot"


# ============================================================ GET

READS = dict(
    [(n, (n, u"# skill-%s\n%s 这个技能的写法：说话慢。\n" % (n, n), True)) for n in au.SKILL_NAMES[:-1]]
    + [("gif_停用的bot也能读_CRLF原样保留", ("gif", u"# skill-gif\r\n发动图前先想好。\r\n", False))])


@pytest.mark.parametrize("name,content,enabled", list(READS.values()), ids=list(READS))
def test_GET技能_七个通用名按约定路径读_回原文全文与版本号_不动文件(adapter, name, content, enabled):
    others = {n: u"# 别的技能 %s：不该被读到\n" % n for n in au.SKILL_NAMES if n != name}
    bot = skill_bot(adapter, dict(others, **{name: content}), enabled=enabled)
    skills = au.channel_of(bot) / "skills"
    before = au.tree_mark(skills)
    body = adapter.ok(call_skill(adapter, "GET", name))
    assert body.get("text") == content, "text 应是 skills/skill-%s/SKILL.md 按 UTF-8 原样读出的全文，实际 %r" % (
        name, body.get("text"))
    assert body.get("version") == au.persona_version(content), \
        "version 应是原始字节 SHA-256 前 16 位小写十六进制，实际 %r" % (body.get("version"),)
    assert au.tree_mark(skills) == before, "读技能却动了 skills 目录下的文件"
    no_backup(adapter)


NOT_NAMES = {
    "大小写不对_Image": "Image",
    "表外的名字_memory_即使文件真在": "memory",
    "带了目录前缀_skill-image": "skill-image",
    "带了扩展名_image.md": "image.md",
}


@pytest.mark.parametrize("name", list(NOT_NAMES.values()), ids=list(NOT_NAMES))
def test_技能名不在通用名表里_GET与PUT都回422invalid_即使有对应文件也不读不写(adapter, name):
    bot = skill_bot(adapter, {})
    # 照这个名字真摆一份文件：按「文件在不在」判断而不是按名字表判断的实现会读到它
    au.put_file(au.skill_path(bot, name), OLD_SYSTEM)
    skills = au.channel_of(bot) / "skills"
    before = au.tree_mark(skills)
    resp = call_skill(adapter, "GET", name)
    adapter.expect_error(resp, 422, "invalid")
    assert "OLDSYS-SKILL-4417" not in au.response_text(resp), "表外名字对应的文件被读出来回给了调用方"
    adapter.expect_error(call_skill(adapter, "PUT", name,
                                    {"text": NEW, "version": au.persona_version(OLD_SYSTEM)}), 422, "invalid")
    assert au.tree_mark(skills) == before, "表外的技能名却动了 skills 目录下的文件"
    no_backup(adapter)


MISSING = {
    "skills目录都没有": "no_dir",
    "没有这个技能的目录": "no_skill_dir",
    "技能目录在但没有SKILL_md": "no_file",
}


@pytest.mark.parametrize("how", list(MISSING.values()), ids=list(MISSING))
def test_这只bot没有这个技能_GET与PUT都回404no_such_skill_不新建(adapter, how):
    bot = skill_bot(adapter, {} if how == "no_dir" else {"image": OLD})
    if how == "no_file":
        au.skill_path(bot, "gif").parent.mkdir(parents=True)
    channel = au.channel_of(bot)
    before = au.tree_mark(channel)
    adapter.expect_error(call_skill(adapter, "GET", "gif"), 404, "no_such_skill")
    # 版本号给空内容的：把「文件不存在」当成空内容的实现会以为对得上而新建
    adapter.expect_error(call_skill(adapter, "PUT", "gif", {"text": NEW, "version": au.persona_version(b"")}),
                         404, "no_such_skill")
    assert au.tree_mark(channel) == before, "没有这个技能却新建了文件或目录"
    no_backup(adapter)


LINKS = {
    "SKILL_md是软链": "file",
    "技能目录是软链": "skill_dir",
    "skills目录是软链": "skills_dir",
}


@pytest.mark.parametrize("how", list(LINKS.values()), ids=list(LINKS))
def test_技能文件或所在目录是软链_GET与PUT都回500internal_指向的文件一个字节没变(adapter, how):
    bot = skill_bot(adapter, {})
    outside = old_system_dir(adapter)
    target = au.put_file(outside / "skills" / "skill-voice" / "SKILL.md", OLD_SYSTEM)
    channel = au.channel_of(bot)
    link, to = {"file": (au.skill_path(bot, "voice"), target),
                "skill_dir": (channel / "skills" / "skill-voice", target.parent),
                "skills_dir": (channel / "skills", target.parent.parent)}[how]
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(str(to), str(link))
    before = au.tree_mark(outside)
    # PUT 的版本号按软链指向的内容算：顺着软链读的实现会以为版本对得上而写进去
    for method, payload in (("GET", au.NO_BODY), ("PUT", {"text": NEW, "version": au.persona_version(OLD_SYSTEM)})):
        resp = call_skill(adapter, method, "voice", payload)
        adapter.expect_error(resp, 500, "internal")
        assert "OLDSYS-SKILL-4417" not in au.response_text(resp), "软链指向的内容被读出来回给了调用方"
    assert au.tree_mark(outside) == before, "软链指向的那边的文件被动了"
    assert os.path.islink(str(link)) and os.readlink(str(link)) == str(to), "软链本身被换掉了"
    no_backup(adapter)


@pytest.mark.parametrize("kind", ["bytes", "perm"], ids=["内容不是UTF8", "没有读权限"])
def test_GET技能_读不出_回500internal(adapter, kind):
    if kind == "perm" and IS_ROOT:
        pytest.skip("root 不受文件权限限制")
    if kind == "bytes":
        skill_bot(adapter, {"image": b"# \xff\xfe\xfa not utf-8 \xc3\x28\n"}, mode=0o600)
    else:
        skill_bot(adapter, {"image": OLD}, mode=0o000)
    adapter.expect_error(call_skill(adapter, "GET", "image"), 500, "internal")


ENTRY = {
    "没带令牌_401": ("no_auth", 401, "unauthorized"),
    "令牌不对_401": ("wrong_auth", 401, "unauthorized"),
    "bot编号65个字符不合规_422": ("bad_id", 422, "invalid"),
    "没有这只bot_404unknown_bot": ("unknown", 404, "unknown_bot"),
    "方法不对_POST_405": ("post", 405, "method_not_allowed"),
}


@pytest.mark.parametrize("case,status,code", list(ENTRY.values()), ids=list(ENTRY))
def test_技能入口_鉴权_bot编号_未知bot_方法不对_按通用规则拒绝_不读不写(adapter, case, status, code):
    bot = skill_bot(adapter)
    channel = au.channel_of(bot)
    before = au.tree_mark(channel)
    good_put = {"text": NEW, "version": V_OLD}
    if case == "post":
        calls = [("POST", "botalpha", good_put, au.GOOD)]
    else:
        auth = {"no_auth": None, "wrong_auth": "Bearer " + au.WRONG_TOKEN}.get(case, au.GOOD)
        bot_id = {"bad_id": "a" * 65, "unknown": "botzeta"}.get(case, "botalpha")
        calls = [("GET", bot_id, au.NO_BODY, auth), ("PUT", bot_id, good_put, auth)]
    for method, bot_id, payload, auth in calls:
        resp = call_skill(adapter, method, "image", payload, bot_id=bot_id, auth=auth)
        adapter.expect_error(resp, status, code)
        assert "发图" not in au.response_text(resp), "拒绝的请求却把技能内容回给了调用方"
    assert au.tree_mark(channel) == before, "被拒的请求动了频道目录"
    no_backup(adapter)


@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_技能_频道目录落在本系统家目录外_回500internal_那边的文件不读不写(adapter, method):
    bot = skill_bot(adapter, {})
    old = old_system_dir(adapter)
    au.put_file(old / "skills" / "skill-image" / "SKILL.md", OLD_SYSTEM)
    au.set_cfg(bot, bot_channel_path=str(old))
    before = au.tree_mark(old)
    payload = {"text": NEW, "version": au.persona_version(OLD_SYSTEM)} if method == "PUT" else au.NO_BODY
    resp = call_skill(adapter, method, "image", payload)
    adapter.expect_error(resp, 500, "internal")
    assert "OLDSYS-SKILL-4417" not in au.response_text(resp), "家目录外的内容被读出来回给了调用方"
    assert au.tree_mark(old) == before, "家目录外（另一套系统频道目录）的文件被动了"
    no_backup(adapter)


# ============================================================ PUT：写成功

WRITES = {
    "启用的bot": (NEW, True),
    "停用的bot也能写": (NEW, False),
    "正好256KiB按UTF8字节算": (LIMIT_TEXT, True),
}


@pytest.mark.parametrize("text,enabled", list(WRITES.values()), ids=list(WRITES))
def test_PUT技能_成功_原样落盘_原子写_保持原权限_旧文备份一份0600且文件名带通用名_回新版本号_不动启动项(
        adapter, text, enabled):
    bot = skill_bot(adapter, {"image": OLD, "voice": u"# skill-voice\n别动我。\n"}, enabled=enabled)   # 权限 0640
    path, channel = au.skill_path(bot, "image"), au.channel_of(bot)
    in_channel, in_skill = sorted(os.listdir(str(channel))), sorted(os.listdir(str(path.parent)))
    voice_before = au.file_mark(au.skill_path(bot, "voice"))
    ino_before = os.stat(str(path)).st_ino
    body = adapter.ok(call_skill(adapter, "PUT", "image", {"text": text, "version": V_OLD}))
    assert body.get("version") == au.persona_version(text), "应回新内容的版本号，实际 %r" % (body.get("version"),)
    adapter.human_message(body.get("message"))          # A.2：message 是一句生效说明
    assert path.read_bytes() == text.encode("utf-8"), "SKILL.md 应原样是请求里的新全文"
    assert au.mode_of(path) == 0o640, "应保持原文件权限 0640，实际 %o" % au.mode_of(path)
    assert os.stat(str(path)).st_ino != ino_before, "应是临时文件写完改名的原子写，实际是原地改写（inode 没变）"
    assert sorted(os.listdir(str(path.parent))) == in_skill, "技能目录里多了或少了文件（临时文件没清？）"
    assert sorted(os.listdir(str(channel))) == in_channel, "频道目录里多了或少了文件（备份放错地方？）"
    assert au.file_mark(au.skill_path(bot, "voice")) == voice_before, "改 image 却动了别的技能"
    backups = au.skill_backups(adapter.sandbox, "botalpha")
    assert len(backups) == 1, "应恰好备份一份旧文，实际 %r" % backups
    assert "image" in backups[0].name, "备份文件名应带通用名，实际 %r" % backups[0].name
    assert backups[0].read_bytes() == OLD.encode("utf-8"), "备份的内容应是改之前的原文"
    assert au.mode_of(backups[0]) == 0o600, "备份权限应为 0600，实际 %o" % au.mode_of(backups[0])
    assert adapter.launchctl.actions() == [], "保存技能不该动启动项：%r" % adapter.launchctl.actions()


def test_PUT技能_同一技能连存7次只留它最近2份_同一秒也不互相覆盖_别的技能的备份不受影响(adapter):
    texts = [u"第%d版发图技能。\n" % i for i in range(8)]       # texts[0] 是原文
    voice0, voice1 = u"# skill-voice 第0版\n", u"# skill-voice 第1版\n"
    bot = skill_bot(adapter, {"image": texts[0], "voice": voice0})
    adapter.ok(call_skill(adapter, "PUT", "voice", {"text": voice1, "version": au.persona_version(voice0)}))
    for prev, cur in zip(texts, texts[1:]):
        adapter.ok(call_skill(adapter, "PUT", "image", {"text": cur, "version": au.persona_version(prev)}))
    image = au.skill_backups(adapter.sandbox, "botalpha", "image")
    got = sorted(p.read_text(encoding="utf-8") for p in image)
    assert got == sorted(texts[5:7]), "image 应只留最近 2 份（第 5、第 6 版）且互不覆盖，实际 %r" % got
    assert [au.mode_of(p) for p in image] == [0o600] * 2, "备份权限都应为 0600"
    voice = au.skill_backups(adapter.sandbox, "botalpha", "voice")
    assert [p.read_text(encoding="utf-8") for p in voice] == [voice0], \
        "voice 那一份备份应原样留着（只留每个技能各自最近 2 份），实际 %r" % voice
    assert au.skill_path(bot, "image").read_bytes() == texts[7].encode("utf-8")


# ============================================================ PUT：不该写的时候没写

def test_PUT技能_和当前全文完全相同_回200带当前版本号_不写盘不备份(adapter):
    bot = skill_bot(adapter)
    before = au.file_mark(au.skill_path(bot, "image"))
    body = adapter.ok(call_skill(adapter, "PUT", "image", {"text": OLD, "version": V_OLD}))
    assert body.get("version") == V_OLD, "没有改动的 200 也应带当前版本号，实际 %r" % (body,)
    assert au.file_mark(au.skill_path(bot, "image")) == before, "没有改动却写了盘（字节、修改时间、权限或 inode 变了）"
    no_backup(adapter)


def test_PUT技能_版本号已过时_回409stale_别处的改动原样留着_不备份(adapter):
    bot = skill_bot(adapter)
    path = au.put_file(au.skill_path(bot, "image"), OTHER, mode=0o640)       # 拿到版本号之后别处改了
    before = au.file_mark(path)
    adapter.expect_error(call_skill(adapter, "PUT", "image", {"text": NEW, "version": V_OLD}), 409, "stale")
    assert au.file_mark(path) == before, "409 时 SKILL.md 被动了"
    no_backup(adapter)


BAD_BODIES = {
    "多了第三个键": ({"text": NEW, "version": V_OLD, "name": "image"}, None),
    "缺version": ({"text": NEW}, None),
    "text不是字符串": ({"text": 123, "version": V_OLD}, None),
    "text去掉首尾空白后为空": ({"text": " \n\t\r\n ", "version": V_OLD}, None),
    "text超256KiB一个字节_按UTF8字节算": ({"text": OVER_TEXT, "version": V_OLD}, None),
    "version是大写十六进制": ({"text": NEW, "version": V_OLD.upper()}, None),
    "version只有15位": ({"text": NEW, "version": V_OLD[:15]}, None),
    "请求体不合规且版本号过时_先判请求体": ({"text": "", "version": au.persona_version(OTHER)}, None),
    "请求体是数组": (None, "[]"),
    "坏JSON": (None, '{"text": '),
}


@pytest.mark.parametrize("payload,raw", list(BAD_BODIES.values()), ids=list(BAD_BODIES))
def test_PUT技能_请求体不合规_回422invalid_文件一个字节没变_不备份(adapter, payload, raw):
    bot = skill_bot(adapter)
    before = au.file_mark(au.skill_path(bot, "image"))
    adapter.expect_error(call_skill(adapter, "PUT", "image", payload, raw=raw), 422, "invalid")
    assert au.file_mark(au.skill_path(bot, "image")) == before, "请求体不合规却动了 SKILL.md"
    no_backup(adapter)
