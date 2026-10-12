# -*- coding: utf-8 -*-
"""§10 bot 二次编辑（第 4 稿追加）：GET/POST /hub/api/bots/<bot_id>/edit。

只对新系统 bot 开放；四个写入点 yml → access.json → CLAUDE.md → 朋友圈库，
任一失败按逆序恢复。朋友圈库经 botlife_db 夹具钉在 tmp（编辑端点会写 bot_profiles）。
实现未落地时本文件整片红（404/405），那是红基线，不是用例坏了。
"""
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from . import botops_util as u

IS_WIN = sys.platform == "win32"


def _url(bot_id):
    return "/hub/api/bots/%s/edit" % bot_id


def _bot(sandbox, bot_id="xiaoyu", *, display_name="旧名", channel_path=None,
         no_display_name=False, **yml_extra):
    """造一只 dsh bot。channel_path=None 不写 bot_channel_path（走回落路径）。

    返回的 `channel` 恒为默认回落路径 <dsh_home>/bots/<id>/channel，与显式给出的
    channel_path **彻底分开**（后者只写进 yml）——默认路径是「实现若忽略显式配置
    会去读的地方」，判「显式给了就用显式的」要靠两个不同目录，不能是同一个。
    """
    fields = dict(yml_extra)
    if not no_display_name:
        fields["display_name"] = display_name
    if channel_path is not None:
        fields["bot_channel_path"] = str(channel_path)
    fields.setdefault("dispatcher_port", 17951)
    yml = u.write_bot_yml(sandbox.dsh_cfg, bot_id, **fields)
    ch = sandbox.dsh_home / "bots" / bot_id / "channel"
    ch.mkdir(parents=True, exist_ok=True)
    if channel_path is not None:
        Path(channel_path).mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(yml=yml, channel=ch)


def _persona(ch, text):
    ch.mkdir(parents=True, exist_ok=True)
    (ch / "CLAUDE.md").write_text(text, encoding="utf-8")
    return ch / "CLAUDE.md"


def _access(ch, text):
    ch.mkdir(parents=True, exist_ok=True)
    (ch / "access.json").write_text(text, encoding="utf-8")
    return ch / "access.json"


def _get(client, bot_id):
    return client.get(_url(bot_id))


def _post(client, bot_id, body):
    return client.post(_url(bot_id), json=body)


def _names(db_path):
    con = sqlite3.connect(str(db_path))
    try:
        return [r[0] for r in con.execute("SELECT display_name FROM bot_profiles")]
    finally:
        con.close()


# ============================================================ GET §10a

def test_GET_正常_恰好四键且三个来源各就各位(web_client, sandbox):
    b = _bot(sandbox, display_name="小雨")
    _persona(b.channel, "# 人设\n说话简短。")
    _access(b.channel, '{"allowFrom": [1], "voiceId": "v-1"}')
    r = _get(web_client, "xiaoyu")
    assert r.status_code == 200, r.get_json()
    assert r.get_json() == {"bot_id": "xiaoyu", "display_name": "小雨",
                            "persona": "# 人设\n说话简短。", "fish_voice_id": "v-1"}


@pytest.mark.parametrize("yml_line", [None, "display_name: 123", "display_name: [1, 2]"])
def test_GET_显示名缺失或非字符串_回空串(web_client, sandbox, yml_line):
    b = _bot(sandbox, no_display_name=(yml_line is None))
    if yml_line is not None:
        b.yml.write_text("id: xiaoyu\n%s\n" % yml_line, encoding="utf-8")
    r = _get(web_client, "xiaoyu")
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["display_name"] == ""


def test_GET_显示名是空串_回空串(web_client, sandbox):
    b = _bot(sandbox)
    b.yml.write_text('id: xiaoyu\ndisplay_name: ""\n', encoding="utf-8")
    r = _get(web_client, "xiaoyu")
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["display_name"] == ""


def test_GET_人设文件不存在_回空串(web_client, sandbox):
    _bot(sandbox)
    r = _get(web_client, "xiaoyu")
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["persona"] == ""


@pytest.mark.parametrize("raw", [None, "{}", '{"voiceId": 123}', '{"voiceId": ""}'])
def test_GET_音色键缺失或非字符串_回空串(web_client, sandbox, raw):
    b = _bot(sandbox)
    if raw is not None:
        _access(b.channel, raw)
    r = _get(web_client, "xiaoyu")
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["fish_voice_id"] == ""


@pytest.mark.parametrize("raw", ["{ 坏 json", ""])
def test_GET_access_json坏掉_只回空串不报错(web_client, sandbox, raw):
    b = _bot(sandbox)
    _access(b.channel, raw)
    r = _get(web_client, "xiaoyu")
    assert r.status_code == 200, ("读路径不该被坏 access.json 拦下", r.get_json())
    assert r.get_json()["fish_voice_id"] == ""


def test_GET_坏yml_409_bad_yaml(web_client, sandbox):
    (sandbox.dsh_cfg / "xiaoyu.yml").write_text("id: [oops\n  x: : :\n", encoding="utf-8")
    r = _get(web_client, "xiaoyu")
    assert r.status_code == 409, r.get_json()
    assert r.get_json()["error"] == "bad_yaml"


@pytest.mark.parametrize("bad", ["Chenlulu", "a", "_x", "1abc", "有中文", "a" * 33, "x-1"])
def test_GET_id形状非法_400_bad_bot_id(web_client, sandbox, bad):
    r = _get(web_client, bad)
    assert r.status_code == 400, (bad, r.status_code, r.get_json())
    assert r.get_json()["error"] == "bad_bot_id"


def test_GET_不存在的bot_404(web_client, sandbox):
    r = _get(web_client, "ghostbot")
    assert r.status_code == 404, r.get_json()
    assert r.get_json()["error"] == "bot_not_found"


def test_GET_legacy的bot_404且detail点明只管新系统(web_client, sandbox):
    u.write_bot_yml(sandbox.legacy_cfg, "chenlulu", display_name="陈璐璐", dispatcher_port=17899)
    r = _get(web_client, "chenlulu")
    assert r.status_code == 404, r.get_json()
    body = r.get_json()
    assert body["error"] == "bot_not_found"
    assert "新系统" in (body.get("detail") or ""), body


def test_GET_注入名单时来源读不出_404(web_client, sandbox, monkeypatch):
    _bot(sandbox)
    p = sandbox.tmp / "bots.json"
    p.write_text('[{"id": "xiaoyu", "display_name": "小雨"}]', encoding="utf-8")
    monkeypatch.setenv("HUB_BOTS_FILE", str(p))
    r = _get(web_client, "xiaoyu")
    assert r.status_code == 404, r.get_json()
    assert r.get_json()["error"] == "bot_not_found"


def test_GET_channelDir缺失时回落新系统家目录(web_client, sandbox):
    b = _bot(sandbox)          # 不写 bot_channel_path
    assert b.yml.read_text(encoding="utf-8").find("bot_channel_path") == -1
    fallback = sandbox.dsh_home / "bots" / "xiaoyu" / "channel"
    _persona(fallback, "回落路径里的人设")
    r = _get(web_client, "xiaoyu")
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["persona"] == "回落路径里的人设"


def test_GET_channelDir空串时也回落新系统家目录(web_client, sandbox):
    b = _bot(sandbox)
    with b.yml.open("a", encoding="utf-8") as f:
        f.write('bot_channel_path: ""\n')
    assert u.read_yaml(b.yml)["bot_channel_path"] == ""
    fallback = sandbox.dsh_home / "bots" / "xiaoyu" / "channel"
    _persona(fallback, "空串也走回落")
    r = _get(web_client, "xiaoyu")
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["persona"] == "空串也走回落"


def test_GET_channelDir显式给出时用它(web_client, sandbox):
    ch = sandbox.tmp / "custom-channel"
    b = _bot(sandbox, channel_path=ch)
    _persona(ch, "显式路径里的人设")
    _persona(b.channel, "不该读这里")
    r = _get(web_client, "xiaoyu")
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["persona"] == "显式路径里的人设"


@pytest.mark.skipif(IS_WIN, reason="Windows 上 chmod 0 不阻止读取")
def test_GET_yml读不出来_500_config_unreadable且detail只有类名(web_client, sandbox):
    b = _bot(sandbox)
    os.chmod(b.yml, 0)
    try:
        r = _get(web_client, "xiaoyu")
    finally:
        os.chmod(b.yml, 0o600)
    assert r.status_code == 500, r.get_json()
    body = r.get_json()
    assert body["error"] == "config_unreadable", body
    detail = body.get("detail") or ""
    assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", detail), detail


# ============================================================ POST §10b：正常路径

def test_编辑_改名字_yml行级替换且其余行逐字节不变(web_client, sandbox):
    b = _bot(sandbox, display_name="旧名")
    lines_before = b.yml.read_text(encoding="utf-8").splitlines()
    idx = [i for i, l in enumerate(lines_before) if l.startswith("display_name:")]
    assert len(idx) == 1, lines_before
    r = _post(web_client, "xiaoyu", {"display_name": "新名"})
    assert r.status_code == 200, r.get_json()
    lines_after = b.yml.read_text(encoding="utf-8").splitlines()
    assert len(lines_after) == len(lines_before), lines_after
    i = idx[0]
    assert lines_after[i] == 'display_name: "新名"', lines_after
    assert lines_after[:i] + lines_after[i + 1:] == lines_before[:i] + lines_before[i + 1:], \
        "除 display_name 行外必须逐字节不变，实得：%r" % lines_after


@pytest.mark.parametrize("name", ["雨🌧️ちゃん", "名字里有 空格 也好"])
def test_编辑_改名字_unicode与emoji原样写yml(web_client, sandbox, name):
    b = _bot(sandbox)
    r = _post(web_client, "xiaoyu", {"display_name": name})
    assert r.status_code == 200, r.get_json()
    assert u.read_yaml(b.yml)["display_name"] == name


def test_编辑_改名字_yml原本没这个键_插进去且其余键保留(web_client, sandbox):
    b = _bot(sandbox, no_display_name=True)
    before = b.yml.read_text(encoding="utf-8")
    assert "display_name" not in before
    r = _post(web_client, "xiaoyu", {"display_name": "新名"})
    assert r.status_code == 200, r.get_json()
    after = b.yml.read_text(encoding="utf-8")
    assert after.count("display_name") == 1, after
    y = u.read_yaml(b.yml)
    assert y["display_name"] == "新名"
    assert y["id"] == "xiaoyu" and y["dispatcher_port"] == 17951, y


def test_编辑_改名字_朋友圈库也写新值(web_client, sandbox, botlife_db):
    _bot(sandbox, display_name="旧名")
    assert _names(sandbox.tmp / "state.db") in ([], [None]), "前置：库里还没有这只 bot"
    r = _post(web_client, "xiaoyu", {"display_name": "新名"})
    assert r.status_code == 200, r.get_json()
    names = _names(sandbox.tmp / "state.db")
    assert names.count("新名") == 1, ("第二处显示名没写进库", names)


def test_编辑_改名字_响应形状与text固定(web_client, sandbox):
    _bot(sandbox)
    r = _post(web_client, "xiaoyu", {"display_name": "新名"})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert set(body) == {"ok", "saved", "text"}, body
    assert body["ok"] is True
    assert body["saved"] == {"display_name": True, "persona": False, "fish_voice_id": False}, body
    assert body["text"] == "已保存。名字与音色立即生效；人设会被在跑的 bot 在几秒内检测到，空闲时自动换上。", body["text"]


def test_编辑_重复提交同值_幂等且库不重复(web_client, sandbox, botlife_db):
    b = _bot(sandbox)
    assert _post(web_client, "xiaoyu", {"display_name": "新名"}).status_code == 200
    first = b.yml.read_text(encoding="utf-8")
    r = _post(web_client, "xiaoyu", {"display_name": "新名"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["saved"]["display_name"] is True, "同值也写，saved 仍为 true"
    assert b.yml.read_text(encoding="utf-8") == first, "同值重复提交该幂等"
    assert _names(sandbox.tmp / "state.db").count("新名") == 1, "库里不该多出行"


@pytest.mark.parametrize("text", ["人设第一行\n第二行\n", "没有尾换行", "# 标题\n\n正文 中文🀄\n"])
def test_编辑_改人设_原样写入不追加换行(web_client, sandbox, text):
    b = _bot(sandbox)
    r = _post(web_client, "xiaoyu", {"persona": text})
    assert r.status_code == 200, r.get_json()
    got = (b.channel / "CLAUDE.md").read_bytes()
    assert got == text.encode("utf-8"), ("人设必须原样落盘", got)


def test_编辑_改人设_空串写0字节文件(web_client, sandbox):
    b = _bot(sandbox)
    _persona(b.channel, "旧人设")
    r = _post(web_client, "xiaoyu", {"persona": ""})
    assert r.status_code == 200, r.get_json()
    assert (b.channel / "CLAUDE.md").read_bytes() == b""


@pytest.mark.parametrize("raw,value,want", [
    ('{"allowFrom": [123]}', "abc", '{"allowFrom": [123], "voiceId": "abc"}\n'),
    ('{"allowFrom": [123], "voiceId": "abc"}', "", '{"allowFrom": [123]}\n'),
    ('{"allowFrom": [123], "other": 1}', "abc", '{"allowFrom": [123], "other": 1, "voiceId": "abc"}\n'),
])
def test_编辑_改音色_access_json逐字节(web_client, sandbox, raw, value, want):
    """契约 §10b 给的三条字节样例，照抄。"""
    b = _bot(sandbox)
    _access(b.channel, raw)
    r = _post(web_client, "xiaoyu", {"fish_voice_id": value})
    assert r.status_code == 200, r.get_json()
    got = (b.channel / "access.json").read_bytes()
    assert got == want.encode("utf-8"), got


def test_编辑_音色纯空白_按空处理删键(web_client, sandbox):
    b = _bot(sandbox)
    _access(b.channel, '{"allowFrom": [123], "voiceId": "abc"}')
    r = _post(web_client, "xiaoyu", {"fish_voice_id": "   "})
    assert r.status_code == 200, r.get_json()
    assert (b.channel / "access.json").read_bytes() == b'{"allowFrom": [123]}\n'


def test_编辑_只改人设_yml与access_json一个字节不动(web_client, sandbox):
    b = _bot(sandbox)
    _access(b.channel, '{"allowFrom": [1]}')
    yml_before = b.yml.read_bytes()
    acc_before = (b.channel / "access.json").read_bytes()
    assert _post(web_client, "xiaoyu", {"persona": "换人设"}).status_code == 200
    assert b.yml.read_bytes() == yml_before
    assert (b.channel / "access.json").read_bytes() == acc_before


def test_编辑_只改名字_CLAUDE_md与access_json一个字节不动(web_client, sandbox):
    b = _bot(sandbox)
    _persona(b.channel, "旧人设\n")
    _access(b.channel, '{"allowFrom": [1], "voiceId": "v"}')
    md_before = (b.channel / "CLAUDE.md").read_bytes()
    acc_before = (b.channel / "access.json").read_bytes()
    assert _post(web_client, "xiaoyu", {"display_name": "新名"}).status_code == 200
    assert (b.channel / "CLAUDE.md").read_bytes() == md_before
    assert (b.channel / "access.json").read_bytes() == acc_before


def test_编辑_三键一次提交_四个写入点全部落新值(web_client, sandbox, botlife_db):
    b = _bot(sandbox, display_name="旧名")
    _access(b.channel, '{"allowFrom": [1]}')
    r = _post(web_client, "xiaoyu",
              {"display_name": "新名", "persona": "新人设", "fish_voice_id": "v9"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["saved"] == {"display_name": True, "persona": True, "fish_voice_id": True}
    assert u.read_yaml(b.yml)["display_name"] == "新名"
    assert (b.channel / "CLAUDE.md").read_text(encoding="utf-8") == "新人设"
    assert json.loads((b.channel / "access.json").read_text(encoding="utf-8"))["voiceId"] == "v9"
    assert _names(sandbox.tmp / "state.db").count("新名") == 1


@pytest.mark.skipif(IS_WIN, reason="Windows 无 POSIX 权限位")
def test_编辑_新建CLAUDE_md与yml权限0600(web_client, sandbox):
    b = _bot(sandbox)
    assert _post(web_client, "xiaoyu", {"persona": "x"}).status_code == 200
    assert u.mode_of(b.channel / "CLAUDE.md") == 0o600
    assert _post(web_client, "xiaoyu", {"display_name": "新名"}).status_code == 200
    assert u.mode_of(b.yml) == 0o600


def test_编辑_往返_改完GET读回新值(web_client, sandbox):
    _bot(sandbox, display_name="旧名")
    assert _post(web_client, "xiaoyu", {"display_name": "新名", "persona": "新人设"}).status_code == 200
    r = _get(web_client, "xiaoyu")
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["display_name"] == "新名"
    assert r.get_json()["persona"] == "新人设"


# ============================================================ POST §10b：400 面

@pytest.mark.parametrize("bad", ["", "   ", "a" * 65, "带\n换行", "带\x7f删除符"])
def test_编辑_400_bad_field_名字各档(web_client, sandbox, bad):
    _bot(sandbox)
    r = _post(web_client, "xiaoyu", {"display_name": bad})
    assert r.status_code == 400, (bad, r.status_code, r.get_json())
    body = r.get_json()
    assert body["error"] == "bad_field", body
    assert body["detail"] == "display_name 必须是 1-64 字的字符串，且不含控制字符", body


def test_编辑_名字64字边界_放行(web_client, sandbox):
    b = _bot(sandbox)
    name = "a" * 64
    r = _post(web_client, "xiaoyu", {"display_name": name})
    assert r.status_code == 200, r.get_json()
    assert u.read_yaml(b.yml)["display_name"] == name


def test_编辑_名字带首尾空白_判据按去空白后_仍能通过(web_client, sandbox):
    b = _bot(sandbox)
    r = _post(web_client, "xiaoyu", {"display_name": "  新名  "})
    assert r.status_code == 200, r.get_json()
    got = u.read_yaml(b.yml)["display_name"]
    assert str(got).strip() == "新名", ("落盘值（契约含糊点：是否 trim 以 strip 后为准）", got)


@pytest.mark.parametrize("bad", [123, True, ["x"], {"a": 1}, None])
def test_编辑_400_bad_field_persona非字符串(web_client, sandbox, bad):
    _bot(sandbox)
    r = _post(web_client, "xiaoyu", {"persona": bad})
    assert r.status_code == 400, (bad, r.status_code, r.get_json())
    body = r.get_json()
    assert body["error"] == "bad_field", body
    assert body["detail"] == "persona 必须是字符串", body


@pytest.mark.parametrize("bad,want", [
    (7, "fish_voice_id 必须是字符串"),
    ("x" * 65, "音色 id 最长 64 个字符"),
    ("有中文", "音色 id 只能是字母、数字、下划线或连字符"),
    ("a.b", "音色 id 只能是字母、数字、下划线或连字符"),
    ("a b", "音色 id 只能是字母、数字、下划线或连字符"),
])
def test_编辑_400_bad_field_音色各档与文案(web_client, sandbox, bad, want):
    _bot(sandbox)
    r = _post(web_client, "xiaoyu", {"fish_voice_id": bad})
    assert r.status_code == 400, (bad, r.status_code, r.get_json())
    body = r.get_json()
    assert body["error"] == "bad_field", body
    assert body["detail"] == want, body


def test_编辑_判序_名字的错先于其余两键(web_client, sandbox):
    _bot(sandbox)
    r = _post(web_client, "xiaoyu", {"display_name": "", "persona": 1, "fish_voice_id": 2})
    assert r.status_code == 400, r.get_json()
    assert r.get_json()["detail"] == "display_name 必须是 1-64 字的字符串，且不含控制字符", r.get_json()


def test_编辑_判序_persona的错先于音色(web_client, sandbox):
    _bot(sandbox)
    r = _post(web_client, "xiaoyu", {"persona": None, "fish_voice_id": 7})
    assert r.status_code == 400, r.get_json()
    assert r.get_json()["detail"] == "persona 必须是字符串", r.get_json()


@pytest.mark.parametrize("raw", ["[1, 2, 3]", '"just a string"', "not json at all"])
def test_编辑_400_bad_body_体不是JSON对象(web_client, sandbox, raw):
    _bot(sandbox)
    r = web_client.post(_url("xiaoyu"), data=raw, content_type="application/json")
    assert r.status_code == 400, (raw, r.status_code, r.get_json())
    assert r.get_json()["error"] == "bad_body", r.get_json()


def test_编辑_400_bad_body_三个键一个都没给(web_client, sandbox):
    b = _bot(sandbox)
    before = b.yml.read_bytes()
    r = _post(web_client, "xiaoyu", {})
    assert r.status_code == 400, r.get_json()
    body = r.get_json()
    assert body["error"] == "bad_body", body
    assert isinstance(body.get("detail"), str) and body["detail"], body
    assert b.yml.read_bytes() == before


def test_编辑_413_人设超64KiB(web_client, sandbox):
    _bot(sandbox)
    r = _post(web_client, "xiaoyu", {"persona": "a" * (65536 + 1)})
    assert r.status_code == 413, r.get_json()
    body = r.get_json()
    assert body["error"] == "payload_too_large", body
    assert body["detail"] == "人设正文超过 64 KiB", body


def test_编辑_413_按UTF8字节数_中文算三字节(web_client, sandbox):
    """21846 个汉字 = 65538 字节 > 65536。"""
    _bot(sandbox)
    r = _post(web_client, "xiaoyu", {"persona": "中" * 21846})
    assert r.status_code == 413, r.get_json()
    assert r.get_json()["error"] == "payload_too_large"


def test_编辑_人设恰好65536字节_放行(web_client, sandbox):
    b = _bot(sandbox)
    text = "a" * 65536
    r = _post(web_client, "xiaoyu", {"persona": text})
    assert r.status_code == 200, r.get_json()
    assert (b.channel / "CLAUDE.md").stat().st_size == 65536


def test_编辑_判序_413先于404(web_client, sandbox):
    r = _post(web_client, "ghostbot", {"persona": "a" * 65537})
    assert r.status_code == 413, ("payload 检查排在 404 之前", r.status_code, r.get_json())


def test_编辑_判序_400先于404(web_client, sandbox):
    r = _post(web_client, "ghostbot", {"display_name": ""})
    assert r.status_code == 400, ("形状检查排在 404 之前", r.status_code, r.get_json())
    assert r.get_json()["error"] == "bad_field"


@pytest.mark.parametrize("bad", ["Chenlulu", "1abc", "有中文"])
def test_编辑_id形状非法_400_bad_bot_id且detail有(web_client, sandbox, bad):
    r = _post(web_client, bad, {"display_name": "x"})
    assert r.status_code == 400, (bad, r.status_code, r.get_json())
    body = r.get_json()
    assert body["error"] == "bad_bot_id", body
    assert isinstance(body.get("detail"), str) and body["detail"], body


def test_编辑_404_不存在的bot(web_client, sandbox):
    r = _post(web_client, "ghostbot", {"display_name": "x"})
    assert r.status_code == 404, r.get_json()
    assert r.get_json()["error"] == "bot_not_found"


def test_编辑_404_legacy的bot且detail点明只管新系统(web_client, sandbox):
    yml = u.write_bot_yml(sandbox.legacy_cfg, "chenlulu", display_name="陈璐璐")
    before = yml.read_bytes()
    r = _post(web_client, "chenlulu", {"display_name": "x"})
    assert r.status_code == 404, r.get_json()
    body = r.get_json()
    assert body["error"] == "bot_not_found", body
    assert "新系统" in (body.get("detail") or ""), body
    assert yml.read_bytes() == before, "旧系统的配置文件一个字节都不许动"


def test_编辑_404_注入名单(web_client, sandbox, monkeypatch):
    _bot(sandbox)
    p = sandbox.tmp / "bots.json"
    p.write_text('[{"id": "xiaoyu", "display_name": "小雨"}]', encoding="utf-8")
    monkeypatch.setenv("HUB_BOTS_FILE", str(p))
    r = _post(web_client, "xiaoyu", {"display_name": "x"})
    assert r.status_code == 404, r.get_json()
    assert r.get_json()["error"] == "bot_not_found", r.get_json()


# ============================================================ POST §10b：409/500 与回滚

def test_编辑_409_bad_yaml_写盘不发生(web_client, sandbox):
    yml = sandbox.dsh_cfg / "xiaoyu.yml"
    yml.write_text("id: [oops\n  x: : :\n", encoding="utf-8")
    before = yml.read_bytes()
    r = _post(web_client, "xiaoyu", {"display_name": "x"})
    assert r.status_code == 409, r.get_json()
    assert r.get_json()["error"] == "bad_yaml"
    assert yml.read_bytes() == before


def test_编辑_409_bad_access_json_不存在时(web_client, sandbox):
    b = _bot(sandbox)
    assert not (b.channel / "access.json").exists()
    r = _post(web_client, "xiaoyu", {"fish_voice_id": "abc"})
    assert r.status_code == 409, r.get_json()
    body = r.get_json()
    assert body["error"] == "bad_access_json", body
    assert body["detail"] == "access.json 读不出内容（不存在或解析失败），先修好", body
    assert not (b.channel / "access.json").exists(), "写盘不该发生"


def test_编辑_409_bad_access_json_坏JSON时(web_client, sandbox):
    b = _bot(sandbox)
    _access(b.channel, "{ 坏的")
    r = _post(web_client, "xiaoyu", {"fish_voice_id": "abc"})
    assert r.status_code == 409, r.get_json()
    assert r.get_json()["error"] == "bad_access_json"
    assert (b.channel / "access.json").read_bytes() == "{ 坏的".encode("utf-8")


def test_编辑_只改人设时_access_json坏不拦(web_client, sandbox):
    """409 bad_access_json 的条件是「要写 fish_voice_id」。"""
    b = _bot(sandbox)
    _access(b.channel, "{ 坏的")
    r = _post(web_client, "xiaoyu", {"persona": "新人设"})
    assert r.status_code == 200, r.get_json()
    assert (b.channel / "CLAUDE.md").read_text(encoding="utf-8") == "新人设"


@pytest.mark.skipif(IS_WIN, reason="Windows 上 chmod 0 不阻止读取")
def test_编辑_500_config_unreadable_读旧内容失败且写盘不发生(web_client, sandbox):
    b = _bot(sandbox)
    _persona(b.channel, "旧人设")
    os.chmod(b.yml, 0)
    try:
        r = _post(web_client, "xiaoyu", {"display_name": "新名"})
    finally:
        os.chmod(b.yml, 0o600)
    assert r.status_code == 500, r.get_json()
    body = r.get_json()
    assert body["error"] == "config_unreadable", body
    assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", body.get("detail") or ""), body


@pytest.mark.skipif(IS_WIN, reason="Windows 无 POSIX 目录权限")
def test_编辑_写失败_yml首写失败_零改动且detail说没改任何文件(web_client, sandbox):
    b = _bot(sandbox)
    before = b.yml.read_bytes()
    os.chmod(sandbox.dsh_cfg, 0o555)
    try:
        r = _post(web_client, "xiaoyu", {"display_name": "新名"})
    finally:
        os.chmod(sandbox.dsh_cfg, 0o755)
    assert r.status_code == 500, r.get_json()
    body = r.get_json()
    assert body["error"] == "config_write_failed", body
    assert re.fullmatch(r"写配置失败：[A-Za-z_][A-Za-z0-9_.]*；这次没有改动任何文件",
                        body.get("detail") or ""), body
    assert b.yml.read_bytes() == before


@pytest.mark.skipif(IS_WIN, reason="Windows 无 POSIX 目录权限")
def test_编辑_写失败_音色写入失败_yml也恢复(web_client, sandbox):
    """写入点二（access.json）失败：它之前的 yml 必须按逆序恢复。"""
    b = _bot(sandbox, display_name="旧名")
    _access(b.channel, '{"allowFrom": [1]}')
    before_yml = b.yml.read_bytes()
    before_acc = (b.channel / "access.json").read_bytes()
    os.chmod(b.channel, 0o555)
    try:
        r = _post(web_client, "xiaoyu", {"display_name": "新名", "fish_voice_id": "vv"})
    finally:
        os.chmod(b.channel, 0o755)
    assert r.status_code == 500, r.get_json()
    body = r.get_json()
    assert body["error"] == "config_write_failed", body
    assert re.fullmatch(r"写配置失败：[A-Za-z_][A-Za-z0-9_.]*；这次没有改动任何文件",
                        body.get("detail") or ""), body
    assert b.yml.read_bytes() == before_yml, "yml 没按逆序恢复"
    assert (b.channel / "access.json").read_bytes() == before_acc


@pytest.mark.skipif(IS_WIN, reason="Windows 无 POSIX 目录权限")
def test_编辑_写失败_逆序恢复_后写的失败前写的回滚(web_client, sandbox):
    """channel 目录不可写：yml 先写成功、CLAUDE.md 后写失败 → yml 必须恢复旧字节。"""
    b = _bot(sandbox, display_name="旧名")
    before_yml = b.yml.read_bytes()
    os.chmod(b.channel, 0o555)
    try:
        r = _post(web_client, "xiaoyu", {"display_name": "新名", "persona": "新人设"})
    finally:
        os.chmod(b.channel, 0o755)
    assert r.status_code == 500, r.get_json()
    body = r.get_json()
    assert body["error"] == "config_write_failed", body
    assert re.fullmatch(r"写配置失败：[A-Za-z_][A-Za-z0-9_.]*；这次没有改动任何文件",
                        body.get("detail") or ""), body
    assert b.yml.read_bytes() == before_yml, "yml 没按逆序恢复"
    assert not (b.channel / "CLAUDE.md").exists()


def test_编辑_写失败_库写失败_三个文件都恢复(web_client, sandbox, botlife_db):
    """库路径钉成写不了的文件：yml/access.json/CLAUDE.md 都已写成功，必须全部回滚。"""
    b = _bot(sandbox, display_name="旧名")
    _persona(b.channel, "旧人设")
    _access(b.channel, '{"allowFrom": [1]}')
    before = (b.yml.read_bytes(), (b.channel / "CLAUDE.md").read_bytes(),
              (b.channel / "access.json").read_bytes())
    db_file = sandbox.tmp / "state.db"
    os.chmod(db_file, 0)
    try:
        r = _post(web_client, "xiaoyu",
                  {"display_name": "新名", "persona": "新人设", "fish_voice_id": "vv"})
    finally:
        os.chmod(db_file, 0o600)
    assert r.status_code == 500, r.get_json()
    body = r.get_json()
    assert body["error"] == "config_write_failed", body
    assert (body.get("detail") or "").startswith("写配置失败："), body
    got = (b.yml.read_bytes(), (b.channel / "CLAUDE.md").read_bytes(),
           (b.channel / "access.json").read_bytes())
    assert got == before, ("库写失败后三个文件都必须恢复", got)
    assert _names(db_file).count("新名") == 0, "库里不该留下这次的值"


# ============================================================ 生效性与反向

def test_编辑_生效性_零launchctl调用零job字段(web_client, sandbox, launchctl):
    """人设由网关侧 mtime 轮询发现：本端点不产生 launchctl 调用、不发网络请求、不产生 job。"""
    b = _bot(sandbox)
    r = _post(web_client, "xiaoyu", {"display_name": "新名", "persona": "新人设"})
    assert r.status_code == 200, r.get_json()
    assert launchctl.calls() == [], ("编辑不许碰 launchd", launchctl.calls())
    body = r.get_json()
    assert "job_id" not in json.dumps(body) and "sync" not in body, body


def test_编辑_状态码与error码总表一致_失败时不动库(web_client, sandbox, botlife_db):
    """反向：被 400 拒的一次请求，四个写入点一个都不许动。"""
    b = _bot(sandbox, display_name="旧名")
    _persona(b.channel, "旧人设")
    _access(b.channel, '{"allowFrom": [1]}')
    before = (b.yml.read_bytes(), (b.channel / "CLAUDE.md").read_bytes(),
              (b.channel / "access.json").read_bytes())
    r = _post(web_client, "xiaoyu",
              {"display_name": "新名", "persona": "新人设", "fish_voice_id": "bad.dot"})
    assert r.status_code == 400, r.get_json()
    got = (b.yml.read_bytes(), (b.channel / "CLAUDE.md").read_bytes(),
           (b.channel / "access.json").read_bytes())
    assert got == before
    assert _names(sandbox.tmp / "state.db").count("新名") == 0
