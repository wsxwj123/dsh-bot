# -*- coding: utf-8 -*-
"""要写进文件的文字的规矩的白盒单测（INTERFACE-新系统bot运维 §0 第 9 条、§6、§7，第 8 稿）。

- 显示名：新建与编辑共用 `hub_addbot._display_name` 这一条。先去掉首尾空白，再量 1 到 64
  个字符，再按码位清单拒绝；落盘的是去掉首尾空白之后的值。
- 写配置之前先读回来核对：`_readback`（给它要写的全文与这次写进去的字符串键值）。
- 建文件的那一步自己收尾：`_create_file` 内容没写完时把自己刚建的文件删掉。

验收那层量的是接口形状；这里量规则本身的边界、清单与契约是不是一张、以及验收造不出来的
「写到一半失败」。落盘只在 tmp_path 下；bot 名全是假名。
"""
import os
import stat

import pytest
import yaml

from moments import hub_addbot as ab
from moments import hub_config
from moments.provider_model import HubError

BAD_NAME = "display_name 必须是 1-64 字的字符串，且不含控制字符"
READBACK = "这份配置写出来之后读不回原样，多半是名字里有特殊字符"
BOT = "textbot"


@pytest.fixture(autouse=True)
def _seams(tmp_path, monkeypatch):
    """两个配置根、新系统家目录、旧频道根、HOME 全钉在 tmp。"""
    for name in ("configs", "channels", "dsh-configs", "dsh-home", "home"):
        (tmp_path / name).mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(tmp_path / "configs"))
    monkeypatch.setenv("HUB_CHANNELS_DIR", str(tmp_path / "channels"))
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(tmp_path / "dsh-configs"))
    monkeypatch.setenv("DSH_BOT_HOME", str(tmp_path / "dsh-home"))
    monkeypatch.delenv("HUB_BOTS_FILE", raising=False)
    return tmp_path


def _rejected(fn, *a):
    with pytest.raises(HubError) as e:
        fn(*a)
    return e.value.status, e.value.error, e.value.detail


# ============================================================ 显示名：码位清单

#: 契约点名的禁区，逐段照抄（§0 第 9 条）
CONTRACT_BANNED = (set(range(0x0000, 0x0020)) | set(range(0x007F, 0x00A0)) | {0x2028, 0x2029}
                   | set(range(0xD800, 0xE000)) | {0xFFFE, 0xFFFF})


def test_码位清单与契约是同一张_全部码位逐个比过():
    got = {cp for cp in range(0x110000) if ab._name_char_banned(chr(cp))}
    assert got == CONTRACT_BANNED, (sorted(got - CONTRACT_BANNED)[:8], sorted(CONTRACT_BANNED - got)[:8])


@pytest.mark.parametrize("cp", [0x00, 0x09, 0x0A, 0x1F, 0x7F, 0x80, 0x85, 0x9F, 0x2028, 0x2029,
                                0xD800, 0xDBFF, 0xDC00, 0xDFFF, 0xFFFE, 0xFFFF])
def test_显示名中间有禁用的码位_400与固定文案(cp):
    assert _rejected(ab._display_name, "小" + chr(cp) + "雨") == (400, "bad_field", BAD_NAME)


@pytest.mark.parametrize("cp", [0x20, 0x7E, 0xA0, 0xAD, 0x200B, 0x200D, 0x2027, 0x202A, 0xD7FF,
                                0xE000, 0xFE0F, 0xFEFF, 0xFFFD, 0x10000, 0x1F600, 0x10FFFF])
def test_紧挨着禁区的码位与表情用的码位_放行(cp):
    name = "小" + chr(cp) + "雨"
    assert ab._display_name(name) == name


def test_零宽连接符拼成的家庭表情_放行且一个码位不少():
    name = "小雨\U0001F468\u200d\U0001F469\u200d\U0001F467\u200d\U0001F466"
    assert ab._display_name(name) == name


# ============================================================ 显示名：先去首尾空白，再量长度

@pytest.mark.parametrize("raw,want", [("  小雨  ", "小雨"), ("\t小雨\n", "小雨"),
                                      ("\u2028小雨\u2029", "小雨"), ("\u0085小雨\u3000", "小雨"),
                                      ("小 雨", "小 雨")])
def test_返回的是去掉首尾空白之后的值_中间的空格留着(raw, want):
    assert ab._display_name(raw) == want


def test_长度按去掉首尾空白之后算_64个字放行_65个字被拒():
    assert ab._display_name("  " + "字" * 64 + "  ") == "字" * 64
    assert _rejected(ab._display_name, "字" * 65) == (400, "bad_field", BAD_NAME)


def test_长度按字符数_64个表情放行():
    assert ab._display_name("\U0001F600" * 64) == "\U0001F600" * 64


@pytest.mark.parametrize("bad", ["", "   ", "\t\n", None, 123, True, ["小雨"], {"a": 1}, b"xiaoyu"])
def test_空的全是空白的不是字符串的_被拒(bad):
    assert _rejected(ab._display_name, bad) == (400, "bad_field", BAD_NAME)


def test_新建与编辑调的是同一个函数_没有第二份校验():
    """旧的两份各写各的判据：`_validate` 里一段、`_edit_name` 一个函数。"""
    assert not hasattr(ab, "_edit_name")
    src = open(ab.__file__, encoding="utf-8").read()
    assert src.count("_display_name(body") == 2, "新建与编辑各调一次，不多不少"
    assert src.count(BAD_NAME) == 1, "那句文案只该出现在共用的那个函数里"


# ============================================================ 写之前先读回来核对

def test_读得回原样_不抛():
    ab._readback('id: "x"\ndisplay_name: "小雨"\nport: 1\n', {"id": "x", "display_name": "小雨"})


@pytest.mark.parametrize("text,expect", [
    pytest.param("display_name: [oops\n", {"display_name": "小雨"}, id="unparsable"),
    pytest.param('display_name: "小\x00雨"\n', {"display_name": "小\x00雨"}, id="reader-rejects"),
    pytest.param("- a\n- b\n", {"display_name": "小雨"}, id="top-not-mapping"),
    pytest.param("", {"display_name": "小雨"}, id="empty-document"),
    pytest.param('id: "x"\n', {"display_name": "小雨"}, id="key-missing"),
    pytest.param('display_name: "小雨 "\n', {"display_name": "小雨"}, id="value-differs"),
    pytest.param("display_name: 123\n", {"display_name": "123"}, id="reads-back-as-int"),
    pytest.param("display_name: yes\n", {"display_name": "yes"}, id="reads-back-as-bool"),
    pytest.param("display_name: ~\n", {"display_name": "~"}, id="reads-back-as-null"),
    pytest.param('display_name: "小\u0085雨"\n', {"display_name": "小\u0085雨"}, id="nel-folds-to-space"),
    pytest.param('a: "1"\nb: "2"\n', {"a": "1", "b": "x"}, id="one-of-two-differs"),
])
def test_读不回原样_400与固定文案(text, expect):
    assert _rejected(ab._readback, text, expect) == (400, "bad_field", READBACK)


def test_清单之外的码位放在名字中间_渲染出来都读得回():
    """码位清单是按「写得进去、读得回来」定的：清单之外不该有要靠回读兜底才拦得住的码位。

    一个个渲染太慢：先整块拼进一个值里读回，读不回的那块再对半找。
    """
    allowed = [cp for cp in range(0x110000) if cp not in CONTRACT_BANNED]
    broken = []

    def scan(cps):
        value = "小" + "".join(map(chr, cps)) + "雨"
        try:
            got = yaml.safe_load("display_name: %s\n" % ab._yaml_str(value))["display_name"]
        except yaml.YAMLError:
            got = None
        if got == value:
            return
        if len(cps) == 1:
            broken.append(hex(cps[0]))
        else:
            scan(cps[:len(cps) // 2])
            scan(cps[len(cps) // 2:])
    for i in range(0, len(allowed), 4096):
        scan(allowed[i:i + 4096])
    assert broken == [], "这些码位过得了清单却读不回：%s" % broken[:20]


def _fields(**over):
    f = {"bot_id": BOT, "token": "123456789:test-AAaaBBbbCCccDDddEEeeFFggHHhh11", "port": 17971,
         "chat_id": 1, "display_name": "小雨", "persona": "正文"}
    f.update(over)
    return f


@pytest.mark.parametrize("name", ['小"雨"', "小\\雨\\n", "小: 雨 #x", "'小雨'", "{小雨}", "- 小雨",
                                  "yes", "null", "~", "0123", "小\ufeff雨", "é"])
def test_刁钻的显示名_核对过的全文读回来四个字符串键逐字相等(name, _seams):
    text = ab._checked_yml(_fields(display_name=name))
    data = yaml.safe_load(text)
    assert data["id"] == BOT and data["display_name"] == name
    assert data["bot_channel_path"] == str(_seams / "dsh-home" / "bots" / BOT / "channel")
    assert isinstance(data["namespace_uuid"], str) and len(data["namespace_uuid"]) == 36


def test_渲染出来读不回的_核对那一步就抛_不交出文本(monkeypatch):
    monkeypatch.setattr(ab, "_yaml_str", lambda v: v)        # 裸写：yes 读回来是布尔
    assert _rejected(ab._checked_yml, _fields(display_name="yes")) == (400, "bad_field", READBACK)


def test_新建_回读核对排在配置根被关掉之前(monkeypatch):
    """§6：它排在 fish_* 两个字段之后、409 那几格之前。"""
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", "off")
    monkeypatch.setattr(ab, "_yaml_str", lambda v: v)
    body = {"bot_id": BOT, "telegram_token": "123456789:test-AAaaBBbbCCccDDddEEeeFFggHHhh11",
            "dispatcher_port": 17971, "chat_id": 1, "display_name": "yes", "persona_text": "正文"}
    assert _rejected(ab._validate, body) == (400, "bad_field", READBACK)


def test_新建_音色的错先于回读核对(monkeypatch):
    monkeypatch.setattr(ab, "_yaml_str", lambda v: v)
    body = {"bot_id": BOT, "telegram_token": "123456789:test-AAaaBBbbCCccDDddEEeeFFggHHhh11",
            "dispatcher_port": 17971, "chat_id": 1, "display_name": "yes", "persona_text": "正文",
            "fish_voice_id": "bad.dot"}
    assert _rejected(ab._validate, body)[2] == "音色 id 只能是字母、数字、下划线或连字符"


def test_新建_校验交出的显示名与配置全文都是去掉首尾空白之后的(_seams):
    body = {"bot_id": BOT, "telegram_token": "123456789:test-AAaaBBbbCCccDDddEEeeFFggHHhh11",
            "dispatcher_port": 17971, "chat_id": 1, "display_name": "  小雨\t", "persona_text": "正文"}
    out = ab._validate(body)
    assert out["display_name"] == "小雨"
    assert yaml.safe_load(out["yml_text"])["display_name"] == "小雨"


def test_落盘的就是核对过的那一份_不渲染第二遍(_seams, monkeypatch):
    fields = _fields()
    fields["yml_text"] = ab._checked_yml(fields)
    monkeypatch.setattr(ab, "_render_yml", lambda *a, **k: pytest.fail("落盘时又渲染了一遍"))
    ab.lay_down(fields)
    assert (_seams / "dsh-configs" / (BOT + ".yml")).read_text(encoding="utf-8") == fields["yml_text"]


def test_没经校验直接落盘_也先核对_读不回就一个文件都不建(_seams, monkeypatch):
    monkeypatch.setattr(ab, "_yaml_str", lambda v: v)
    assert _rejected(ab.lay_down, _fields(display_name="yes")) == (400, "bad_field", READBACK)
    assert list((_seams / "dsh-configs").iterdir()) == []
    assert list((_seams / "dsh-home").iterdir()) == []


# ============================================================ 编辑：改好的配置写盘前读回来核对

@pytest.fixture
def editable(_seams, monkeypatch):
    yml = _seams / "dsh-configs" / (BOT + ".yml")
    yml.write_text('id: %s\ndisplay_name: "旧名"\ndispatcher_port: 17971\n' % BOT, encoding="utf-8")
    ch = _seams / "dsh-home" / "bots" / BOT / "channel"
    ch.mkdir(parents=True)
    (ch / "access.json").write_text('{"allowFrom": [1]}', encoding="utf-8")
    saved = []
    monkeypatch.setattr(ab, "_save_db_name", lambda cfg, bot, value, undo: saved.append(value))
    return yml, ch, saved


def test_编辑_改好的配置读不回原样_400且配置与朋友圈库都不动(editable, monkeypatch):
    yml, ch, saved = editable
    before = yml.read_bytes()
    monkeypatch.setattr(hub_config, "set_scalar",
                        lambda text, root, key, value, kind: text.replace('"旧名"', "yes"))
    assert _rejected(ab.edit_save, BOT, {"display_name": "yes"}) == (400, "bad_field", READBACK)
    assert yml.read_bytes() == before and saved == []


def test_编辑_回读核对排在白名单坏了之前(editable, monkeypatch):
    """§10b：`bad_yaml` → 配置读不回原样 → `bad_access_json`。"""
    yml, ch, _saved = editable
    (ch / "access.json").write_text("{oops", encoding="utf-8")
    monkeypatch.setattr(hub_config, "set_scalar",
                        lambda text, root, key, value, kind: text.replace('"旧名"', "yes"))
    got = _rejected(ab.edit_save, BOT, {"display_name": "yes", "fish_voice_id": "v1"})
    assert got == (400, "bad_field", READBACK)


def test_编辑_名字带首尾空白_落盘与写进朋友圈库的都是去掉之后的值(editable):
    yml, _ch, saved = editable
    body, code = ab.edit_save(BOT, {"display_name": "\t新名  "})
    assert code == 200 and body["saved"]["display_name"] is True
    assert yaml.safe_load(yml.read_text(encoding="utf-8"))["display_name"] == "新名"
    assert saved == ["新名"]


# ============================================================ 建文件：自己收尾

def _mode(p):
    return stat.S_IMODE(os.stat(str(p)).st_mode)


def test_建文件_正文编不成UTF8_文件根本不建(tmp_path):
    target = tmp_path / "x.yml"
    with pytest.raises(UnicodeEncodeError):
        ab._create_file(str(target), "半个\ud83d表情")
    assert not target.exists(), "编码失败却留下了一个空文件"


def test_建文件_写到一半失败_把自己刚建的文件删掉再抛(tmp_path, monkeypatch):
    target = tmp_path / "x.yml"
    real_fdopen = os.fdopen

    class _Broken:
        def __init__(self, f):
            self._f = f

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._f.close()
            return False

        def write(self, data):
            self._f.write(data[:3])
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(ab.os, "fdopen", lambda fd, mode: _Broken(real_fdopen(fd, mode)))
    with pytest.raises(OSError):
        ab._create_file(str(target), "正文很长很长")
    assert not target.exists(), "内容没写完，那个半截文件却留下了"


def test_建文件_fdopen自己失败_文件删掉且描述符不漏(tmp_path, monkeypatch):
    target = tmp_path / "x.yml"
    closed = []
    real_close = os.close
    monkeypatch.setattr(ab.os, "fdopen", lambda fd, mode: (_ for _ in ()).throw(OSError("boom")))
    monkeypatch.setattr(ab.os, "close", lambda fd: (closed.append(fd), real_close(fd)))
    with pytest.raises(OSError):
        ab._create_file(str(target), "正文")
    assert not target.exists() and len(closed) == 1


def test_建文件_同名的本来就在_报错且那个文件一个字节不动(tmp_path):
    target = tmp_path / "x.yml"
    target.write_text("别人的内容", encoding="utf-8")
    with pytest.raises(FileExistsError):
        ab._create_file(str(target), "新内容")
    assert target.read_text(encoding="utf-8") == "别人的内容", "不是这一步建的文件被删了或被改了"


@pytest.mark.skipif(os.name == "nt", reason="Windows 无 POSIX 权限位")
def test_建文件_字符串按UTF8写_字节原样写_权限都是0600(tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.bin"
    ab._create_file(str(a), "小雨\n")
    ab._create_file(str(b), b"\xff\xfe\x00raw")
    assert a.read_bytes() == "小雨\n".encode("utf-8") and b.read_bytes() == b"\xff\xfe\x00raw"
    assert (_mode(a), _mode(b)) == (0o600, 0o600)


def test_落盘_配置文件写到一半失败_不留空配置_同名可以再建(_seams, monkeypatch):
    """此前配置被建成空文件又不在回滚清单里，留下一只空配置的 bot，同名再也建不了。"""
    real = ab._create_file

    def half(path, text, mode=0o600):
        if path.endswith(".yml"):
            return real(path, "半个\ud83d表情", mode)       # 建文件那一步自己遇到写不成的内容
        return real(path, text, mode)
    monkeypatch.setattr(ab, "_create_file", half)
    with pytest.raises(UnicodeEncodeError):
        ab.lay_down(_fields())
    assert list((_seams / "dsh-configs").iterdir()) == [], "留下了配置文件"
    assert not (_seams / "dsh-home" / "bots" / BOT).exists(), "留下了 bot 家目录"
    monkeypatch.setattr(ab, "_create_file", real)
    ab.lay_down(_fields())
    assert yaml.safe_load((_seams / "dsh-configs" / (BOT + ".yml")).read_text(encoding="utf-8"))["id"] == BOT


# ============================================================ 人设正文：两项检查，新建与编辑同一把尺

TOO_LONG = "人设正文超过 65536 个字符"
BROKEN = "人设正文里有残缺的字符（多半是半个表情符号），删掉它再保存"
TPL_TOO_LONG = "这个人设模板超过 65536 个字符，换一个或直接贴正文"
TPL_NOT_UTF8 = "这个人设模板不是合法的 UTF-8 文本，换一个或直接贴正文"


def test_人设_上限按字符数_65536个四字节的表情放行_再多一个就413():
    text = "\U0001F600" * 65536                      # 262144 字节，按字节数算早就超了
    assert ab._persona_text(text) == text
    assert _rejected(ab._persona_text, text + "a") == (413, "payload_too_large", TOO_LONG)


@pytest.mark.parametrize("lone", ["\ud800", "\udbff", "\udc00", "\udfff"])
def test_人设_有落单的代理_400与固定文案(lone):
    assert _rejected(ab._persona_text, "前半句" + lone + "后半句") == (400, "bad_field", BROKEN)


def test_人设_又超长又有落单的代理_先答残缺的字符():
    """§10b 的判定顺序：字段形状的 400 先于 413。"""
    assert _rejected(ab._persona_text, "\ud83d" + "字" * 65537)[:2] == (400, "bad_field")


def test_人设_两项检查与两句文案各只有一份():
    src = open(ab.__file__, encoding="utf-8").read()
    assert src.count(TOO_LONG) == 1 and src.count(BROKEN) == 1
    assert src.count("> MAX_PERSONA") == 1, "量长度的地方只该有一处"
    assert ".encode(\"utf-8\")) > " not in src, "还有按字节数量长度的写法"
    assert "64 KiB" not in src.replace("64 KiB 截断", ""), "按字节数算的旧上限文案还留着"


def _body(**over):
    body = {"bot_id": BOT, "telegram_token": "123456789:test-AAaaBBbbCCccDDddEEeeFFggHHhh11",
            "dispatcher_port": 17971, "chat_id": 1, "display_name": "小雨", "persona_text": "正文"}
    body.update(over)
    return body


def _template(root, raw, name="tplbot"):
    d = root / name
    d.mkdir(parents=True)
    (d / "CLAUDE.md").write_bytes(raw)
    return name


def _from_template(name):
    body = _body(persona_template=name)
    del body["persona_text"]
    return body


def test_新建_直接给的正文经同一把尺(_seams):
    assert _rejected(ab._validate, _body(persona_text="字" * 65537)) == (413, "payload_too_large", TOO_LONG)
    assert _rejected(ab._validate, _body(persona_text="半个\ud83d表情")) == (400, "bad_field", BROKEN)
    assert ab._validate(_body(persona_text="字" * 21846))["persona"] == "字" * 21846   # 65538 字节


def test_新建_模板恰好65536个字符_一个字不少(_seams):
    text = "字" * 65535 + "尾"
    name = _template(_seams / "channels", text.encode("utf-8"))
    assert ab._validate(_from_template(name))["persona"] == text


def test_新建_模板多一个字符_413_不再悄悄截断(_seams):
    name = _template(_seams / "channels", ("字" * 65536 + "尾").encode("utf-8"))
    assert _rejected(ab._validate, _from_template(name)) == (413, "payload_too_large", TPL_TOO_LONG)


def test_新建_新系统来源的模板同一把尺(_seams):
    home = _seams / "dsh-home" / "bots" / "tplbot" / "channel"
    home.mkdir(parents=True)
    (home / "CLAUDE.md").write_bytes(("字" * 65537).encode("utf-8"))
    assert _rejected(ab._validate, _from_template("tplbot")) == (413, "payload_too_large", TPL_TOO_LONG)


@pytest.mark.parametrize("raw", [b"\xff\xfe\x00not utf8 \x80", "好".encode("utf-8")[:2], b"ok\x80"])
def test_新建_模板不是合法UTF8_400_不再冒成500(_seams, raw):
    name = _template(_seams / "channels", raw)
    assert _rejected(ab._validate, _from_template(name)) == (400, "bad_field", TPL_NOT_UTF8)


def test_新建_模板很大_只读到够判超限为止(_seams, monkeypatch):
    """不把一个巨大的文件整个读进内存：最多读上限加一个字符。"""
    name = _template(_seams / "channels", b"a" * (4 * 1024 * 1024))
    asked = []
    real_open = open

    class _Spy:
        def __init__(self, f):
            self._f = f

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._f.close()
            return False

        def read(self, n=-1):
            asked.append(n)
            return self._f.read(n)

    def spy_open(path, *a, **k):
        f = real_open(path, *a, **k)
        return _Spy(f) if str(path).endswith(os.path.join(name, "CLAUDE.md")) else f
    monkeypatch.setattr("builtins.open", spy_open)
    assert _rejected(ab._validate, _from_template(name))[0] == 413
    assert asked == [65537], asked


def test_新建_模板的换行照旧按文本读_与以前相同(_seams):
    """对照：没超限的模板读出来的内容与改之前一样（CRLF 读成 LF）。"""
    name = _template(_seams / "channels", "第一行\r\n第二行\n".encode("utf-8"))
    assert ab._validate(_from_template(name))["persona"] == "第一行\n第二行\n"


def test_新建_模板目录在文件不在_仍是空人设(_seams):
    (_seams / "channels" / "emptytpl").mkdir()
    assert ab._validate(_from_template("emptytpl"))["persona"] == ""


def test_编辑_人设按字符数_21846个汉字放行_65537个字符413(editable):
    _yml, ch, _saved = editable
    text = "中" * 21846                                  # 65538 字节
    body, code = ab.edit_save(BOT, {"persona": text})
    assert code == 200 and (ch / "CLAUDE.md").read_bytes() == text.encode("utf-8")
    assert _rejected(ab.edit_save, BOT, {"persona": "a" * 65537}) == (413, "payload_too_large", TOO_LONG)


def test_编辑_人设有落单的代理_400_人设文件不动(editable):
    _yml, ch, _saved = editable
    (ch / "CLAUDE.md").write_text("旧人设", encoding="utf-8")
    assert _rejected(ab.edit_save, BOT, {"persona": "半个\ud83d表情"}) == (400, "bad_field", BROKEN)
    assert (ch / "CLAUDE.md").read_text(encoding="utf-8") == "旧人设"


def test_编辑_判序_残缺的字符先于音色的错_音色的错先于超长(editable):
    """§10b：三个字段的形状错（400）全部先于 413。"""
    assert _rejected(ab.edit_save, BOT, {"persona": "半个\ud83d", "fish_voice_id": "bad.dot"})[2] == BROKEN
    assert _rejected(ab.edit_save, BOT, {"persona": "a" * 65537, "fish_voice_id": "bad.dot"})[2] == \
        "音色 id 只能是字母、数字、下划线或连字符"


def test_编辑_判序_超长先于没有这只bot(editable):
    assert _rejected(ab.edit_save, "ghostbot", {"persona": "a" * 65537})[0] == 413
