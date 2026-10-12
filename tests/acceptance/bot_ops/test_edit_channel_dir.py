# -*- coding: utf-8 -*-
"""§10「`channelDir` 定义」与「落点校验」（接口约定第 7 稿）：编辑端点的频道目录。

依据 `INTERFACE-新系统bot运维.md` §10：

- 频道目录只取新系统配置根下这只的 yml **自己**顶层写的 `bot_channel_path`；没写、为空、
  或这份 yml 读不出结构，就是 `<DSH_BOT_HOME>/bots/<bot>/channel`。`life_config` 指向的
  那份文件里写的不参与。
- 把 `<DSH_BOT_HOME>` 解析成真实路径，这只 bot 的家 = 它下面的 `bots/<bot_id>`。频道目录解析
  成真实路径（软链跟到底）之后必须就是这个家目录或在它里面，否则 409 `bad_channel_path`，
  零读取、零写入。
- 这一格在读与写两张表里位置相同：紧跟 404 之后，在 `bad_yaml` 之前。

接口约定给的四个场景各有一节；后面是它们的边界与判定顺序。

「外面」一律是本用例 `tmp_path` 下另一个临时目录。bot 用假名。
"""
import json
import os
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from . import botops_util as u

IS_WIN = sys.platform == "win32"
pytestmark = pytest.mark.skipif(IS_WIN, reason="软链与 POSIX 权限位在 Windows 上语义不同")

BOT = "xinbot"
INSIDE_PERSONA = "家目录里的人设（甲）INSIDE-PERSONA-2b7f"
OUTSIDE_PERSONA = "外面那份人设（乙）OUTSIDE-PERSONA-9f3a"
INSIDE_VOICE = "inside-voice-11"
OUTSIDE_VOICE = "outside-voice-77"
REJECTED = {"error": "bad_channel_path", "detail": u.BAD_CHANNEL_PATH_DETAIL}


def _url(bot_id=BOT):
    return "/hub/api/bots/%s/edit" % bot_id


def _body(r):
    j = r.get_json(silent=True)
    return j if isinstance(j, dict) else {"_不是JSON对象": r.get_data(as_text=True)[:200]}


def _fill(ch, persona, voice):
    """在一个目录里放好人设与白名单（带音色）。"""
    ch.mkdir(parents=True, exist_ok=True)
    (ch / "CLAUDE.md").write_text(persona, encoding="utf-8")
    (ch / "access.json").write_text(
        json.dumps({"allowFrom": [1], "voiceId": voice}, ensure_ascii=False) + "\n", encoding="utf-8")
    return ch


def _outside(sandbox, name="outside-channel"):
    """家目录之外的一个临时目录，里面放着「乙」。"""
    return _fill(sandbox.tmp / name, OUTSIDE_PERSONA, OUTSIDE_VOICE)


def _home(sandbox, bot=BOT):
    return sandbox.dsh_home / "bots" / bot


def _yml(sandbox, bot=BOT, **fields):
    fields.setdefault("display_name", "旧名")
    fields.setdefault("dispatcher_port", 17951)
    return u.write_bot_yml(sandbox.dsh_cfg, bot, **fields)


def _profiles(db):
    """朋友圈库 `bot_profiles` 的全部行（§10 落盘表第二处）。"""
    con = sqlite3.connect(str(db.DB_PATH))
    try:
        return sorted(con.execute("SELECT * FROM bot_profiles").fetchall(), key=repr)
    finally:
        con.close()


# ------------------------------------------------------------ 三种「落到外面」的摆法

def _explicit_outside(sandbox):
    """场景 1：新系统那份自己写的 `bot_channel_path` 指到外面。家里默认位置另放一份「甲」。"""
    outside = _outside(sandbox)
    _fill(_home(sandbox) / "channel", INSIDE_PERSONA, INSIDE_VOICE)
    yml = _yml(sandbox, bot_channel_path=str(outside))
    return SimpleNamespace(yml=yml, outside=outside)


def _channel_is_link(sandbox):
    """场景 2：不写 `bot_channel_path`，家里的 `channel` 是一个指到外面的软链。"""
    outside = _outside(sandbox)
    _home(sandbox).mkdir(parents=True)
    os.symlink(str(outside), str(_home(sandbox) / "channel"), target_is_directory=True)
    yml = _yml(sandbox)
    return SimpleNamespace(yml=yml, outside=outside)


def _home_is_link(sandbox):
    """场景 2 的另一例：`bots/<bot>` 这一级本身是指到外面的软链，外面那个目录里有 `channel/`。"""
    outside_home = sandbox.tmp / "outside-home"
    outside = _fill(outside_home / "channel", OUTSIDE_PERSONA, OUTSIDE_VOICE)
    (sandbox.dsh_home / "bots").mkdir(parents=True)
    os.symlink(str(outside_home), str(_home(sandbox)), target_is_directory=True)
    yml = _yml(sandbox)
    return SimpleNamespace(yml=yml, outside=outside)


OUTSIDE_LAYOUTS = [
    pytest.param(_explicit_outside, id="explicit-path"),
    pytest.param(_channel_is_link, id="channel-symlink"),
    pytest.param(_home_is_link, id="home-symlink"),
]
#: 改名字、改人设、改音色：不论改哪个字段都先过落点这一格
EDITS = [
    pytest.param({"display_name": "想改成这个"}, id="display_name"),
    pytest.param({"persona": "想换成这份人设 NEW-PERSONA"}, id="persona"),
    pytest.param({"fish_voice_id": "new-voice-99"}, id="fish_voice_id"),
]


# ============================================================ 场景 1、2：落到外面 → 409，零读写

@pytest.mark.parametrize("layout", OUTSIDE_LAYOUTS)
def test_读_频道目录落在家目录之外_409_bad_channel_path_文案逐字(web_client, sandbox, layout):
    layout(sandbox)
    r = web_client.get(_url())
    assert r.status_code == 409, (r.status_code, _body(r))
    assert _body(r) == REJECTED, _body(r)


@pytest.mark.parametrize("layout", OUTSIDE_LAYOUTS)
def test_读_频道目录落在家目录之外_外面那份的内容没被读成响应(web_client, sandbox, layout):
    layout(sandbox)
    text = web_client.get(_url()).get_data(as_text=True)
    assert OUTSIDE_PERSONA not in text, "外面那份人设被读成了响应内容"
    assert OUTSIDE_VOICE not in text, "外面那份白名单里的音色被读成了响应内容"


@pytest.mark.parametrize("layout", OUTSIDE_LAYOUTS)
@pytest.mark.parametrize("edit", EDITS)
def test_写_频道目录落在家目录之外_不论改哪个字段都是409_文案逐字(
        web_client, sandbox, botlife_db, layout, edit):
    layout(sandbox)
    r = web_client.post(_url(), json=edit)
    assert r.status_code == 409, (edit, r.status_code, _body(r))
    assert _body(r) == REJECTED, _body(r)


@pytest.mark.parametrize("layout", OUTSIDE_LAYOUTS)
@pytest.mark.parametrize("edit", EDITS)
def test_写_频道目录落在家目录之外_外面的文件与配置逐字节没变_朋友圈库没写(
        web_client, sandbox, botlife_db, layout, edit):
    made = layout(sandbox)
    frozen = u.freeze_roots(made.outside, sandbox.dsh_cfg, sandbox.legacy_cfg)
    rows_before = _profiles(botlife_db)
    web_client.post(_url(), json=edit)
    u.assert_roots_untouched(frozen)
    assert _profiles(botlife_db) == rows_before, "被拒的编辑动了朋友圈库"


def test_写_显式写到外面_家目录里默认位置那份也没被动(web_client, sandbox, botlife_db):
    """被拒就是零写入：不许「外面的不碰，转头去写默认位置」。"""
    _explicit_outside(sandbox)
    frozen = u.freeze_roots(sandbox.dsh_home)
    r = web_client.post(_url(), json={"persona": "想换成这份人设", "fish_voice_id": "new-voice-99"})
    assert r.status_code == 409, (r.status_code, _body(r))
    u.assert_roots_untouched(frozen)


# ============================================================ 场景 3：life_config 那份写了外面 → 不参与

@pytest.fixture
def life_points_outside(sandbox):
    """新系统那份不写 `bot_channel_path`，带 `life_config`；那份文件写的目录在外面（乙）。
    家目录里默认位置放着甲。"""
    outside = _outside(sandbox)
    inside = _fill(_home(sandbox) / "channel", INSIDE_PERSONA, INSIDE_VOICE)
    life = u.write_bot_yml(sandbox.tmp / "life", BOT, bot_channel_path=str(outside))
    yml = _yml(sandbox, life_config=str(life))
    assert "bot_channel_path" not in yml.read_text(encoding="utf-8"), "夹具自检：新系统那份不该写目录"
    return SimpleNamespace(yml=yml, life=life, outside=outside, inside=inside)


def test_读_life那份写了外面_照常200_读到的是家目录里那份人设(web_client, life_points_outside):
    r = web_client.get(_url())
    assert r.status_code == 200, (r.status_code, _body(r))
    assert r.get_json()["persona"] == INSIDE_PERSONA, r.get_json()


def test_读_life那份写了外面_读到的是家目录里那份音色(web_client, life_points_outside):
    r = web_client.get(_url())
    assert r.status_code == 200, (r.status_code, _body(r))
    assert r.get_json()["fish_voice_id"] == INSIDE_VOICE, r.get_json()


def test_写人设_life那份写了外面_变的是家目录里那份_外面那份仍是乙(
        web_client, sandbox, life_points_outside):
    made = life_points_outside
    frozen = u.freeze_roots(made.outside, sandbox.tmp / "life")
    r = web_client.post(_url(), json={"persona": "改后的人设 NEW-PERSONA"})
    assert r.status_code == 200, (r.status_code, _body(r))
    assert (made.inside / "CLAUDE.md").read_text(encoding="utf-8") == "改后的人设 NEW-PERSONA"
    u.assert_roots_untouched(frozen)
    assert (made.outside / "CLAUDE.md").read_text(encoding="utf-8") == OUTSIDE_PERSONA


def test_写音色_life那份写了外面_变的是家目录里那份白名单_外面那份没碰(
        web_client, sandbox, life_points_outside):
    made = life_points_outside
    frozen = u.freeze_roots(made.outside, sandbox.tmp / "life")
    r = web_client.post(_url(), json={"fish_voice_id": "new-voice-99"})
    assert r.status_code == 200, (r.status_code, _body(r))
    assert (made.inside / "access.json").read_bytes() == \
        b'{"allowFrom": [1], "voiceId": "new-voice-99"}\n'
    u.assert_roots_untouched(frozen)


# ============================================================ 场景 4：不该被拦的

def test_读_显式写的就是家目录里的默认位置_200(web_client, sandbox):
    inside = _fill(_home(sandbox) / "channel", INSIDE_PERSONA, INSIDE_VOICE)
    _yml(sandbox, bot_channel_path=str(inside))
    r = web_client.get(_url())
    assert r.status_code == 200, (r.status_code, _body(r))
    assert r.get_json()["persona"] == INSIDE_PERSONA, r.get_json()


def test_写_显式写的就是家目录里的默认位置_200且写在那里(web_client, sandbox):
    inside = _fill(_home(sandbox) / "channel", INSIDE_PERSONA, INSIDE_VOICE)
    _yml(sandbox, bot_channel_path=str(inside))
    r = web_client.post(_url(), json={"persona": "改后的人设"})
    assert r.status_code == 200, (r.status_code, _body(r))
    assert (inside / "CLAUDE.md").read_text(encoding="utf-8") == "改后的人设"


@pytest.fixture
def linked_dsh_home(sandbox, monkeypatch):
    """`<DSH_BOT_HOME>` 本身是一个软链，指向真实的家目录（都在 tmp 里）。

    排在 `web_client` 之前取用：环境要在整站 app 载入之前就位。
    """
    real = sandbox.tmp / "real-dsh-home"
    real.mkdir()
    link = sandbox.tmp / "dsh-home-link"
    os.symlink(str(real), str(link), target_is_directory=True)
    monkeypatch.setenv("DSH_BOT_HOME", str(link))
    inside = _fill(real / "bots" / BOT / "channel", INSIDE_PERSONA, INSIDE_VOICE)
    yml = _yml(sandbox)
    return SimpleNamespace(real=real, link=link, inside=inside, yml=yml)


def test_读_新系统家目录本身是软链_200(linked_dsh_home, web_client):
    r = web_client.get(_url())
    assert r.status_code == 200, (r.status_code, _body(r))
    assert r.get_json()["persona"] == INSIDE_PERSONA, r.get_json()


def test_写_新系统家目录本身是软链_200且写进真实家目录(linked_dsh_home, web_client):
    r = web_client.post(_url(), json={"persona": "改后的人设"})
    assert r.status_code == 200, (r.status_code, _body(r))
    assert (linked_dsh_home.inside / "CLAUDE.md").read_text(encoding="utf-8") == "改后的人设"


def test_读_显式写的是家目录本身_200(web_client, sandbox):
    """「必须就是这个家目录或在它里面」：就是家目录本身也算数。"""
    home = _fill(_home(sandbox), INSIDE_PERSONA, INSIDE_VOICE)
    _yml(sandbox, bot_channel_path=str(home))
    r = web_client.get(_url())
    assert r.status_code == 200, (r.status_code, _body(r))
    assert r.get_json()["persona"] == INSIDE_PERSONA, r.get_json()


def test_读_默认位置是指向家目录里面另一处的软链_200(web_client, sandbox):
    """软链跟到底之后还在家目录里，就不该被拦。"""
    real = _fill(_home(sandbox) / "real-channel", INSIDE_PERSONA, INSIDE_VOICE)
    os.symlink(str(real), str(_home(sandbox) / "channel"), target_is_directory=True)
    _yml(sandbox)
    r = web_client.get(_url())
    assert r.status_code == 200, (r.status_code, _body(r))
    assert r.get_json()["persona"] == INSIDE_PERSONA, r.get_json()


# ============================================================ 边界：在新系统家目录树里，但不在这只 bot 的家里

def _elsewhere(sandbox, kind):
    """几个「看着在里面、其实不在这只 bot 家里」的位置，都放好乙。

    返回 (要写进 yml 的路径文字, 那个位置的真实目录)。
    """
    bots = sandbox.dsh_home / "bots"
    _fill(_home(sandbox) / "channel", INSIDE_PERSONA, INSIDE_VOICE)
    if kind == "sibling-prefix":            # 名字以这只的名字打头的另一个目录
        d = _fill(bots / (BOT + "_evil") / "channel", OUTSIDE_PERSONA, OUTSIDE_VOICE)
        return str(d), d
    if kind == "other-bot-home":            # 别的 bot 的家
        d = _fill(bots / "otherbot" / "channel", OUTSIDE_PERSONA, OUTSIDE_VOICE)
        return str(d), d
    if kind == "dsh-home-itself":           # 新系统家目录本身
        d = _fill(sandbox.dsh_home, OUTSIDE_PERSONA, OUTSIDE_VOICE)
        return str(d), d
    if kind == "dotdot":                    # 从自己家里用 .. 绕出去
        d = _fill(bots / "otherbot" / "channel", OUTSIDE_PERSONA, OUTSIDE_VOICE)
        return str(_home(sandbox) / "channel" / ".." / ".." / "otherbot" / "channel"), d
    if kind == "tilde":                     # ~ 展开到假 HOME 下面（在新系统家目录之外）
        d = _fill(sandbox.home / "outside-by-tilde", OUTSIDE_PERSONA, OUTSIDE_VOICE)
        return "~/outside-by-tilde", d
    raise ValueError(kind)


ELSEWHERE = ["sibling-prefix", "other-bot-home", "dsh-home-itself", "dotdot", "tilde"]


@pytest.mark.parametrize("kind", ELSEWHERE)
def test_读_目录不在这只bot自己的家里_409_bad_channel_path(web_client, sandbox, kind):
    written, _where = _elsewhere(sandbox, kind)
    _yml(sandbox, bot_channel_path=written)
    r = web_client.get(_url())
    assert r.status_code == 409, (kind, r.status_code, _body(r))
    assert _body(r) == REJECTED, _body(r)
    assert OUTSIDE_PERSONA not in r.get_data(as_text=True), "不在自己家里的那份人设被读了出来"


@pytest.mark.parametrize("kind", ELSEWHERE)
def test_写_目录不在这只bot自己的家里_409且那里的文件逐字节没变(
        web_client, sandbox, botlife_db, kind):
    written, where = _elsewhere(sandbox, kind)
    _yml(sandbox, bot_channel_path=written)
    frozen = u.freeze_roots(where, _home(sandbox), sandbox.dsh_cfg)
    r = web_client.post(_url(), json={"persona": "想换成这份人设", "fish_voice_id": "new-voice-99"})
    assert r.status_code == 409, (kind, r.status_code, _body(r))
    assert _body(r) == REJECTED, _body(r)
    u.assert_roots_untouched(frozen)


# ============================================================ 判定顺序：在 404 之后

@pytest.mark.parametrize("method", ["GET", "POST"])
def test_判序_旧系统的bot哪怕目录写在外面_是404不是bad_channel_path(
        web_client, sandbox, botlife_db, method):
    outside = _outside(sandbox)
    u.write_bot_yml(sandbox.legacy_cfg, BOT, display_name="旧系统的", dispatcher_port=17899,
                    bot_channel_path=str(outside))
    frozen = u.freeze_roots(outside, sandbox.legacy_cfg, sandbox.dsh_cfg)
    if method == "GET":
        r = web_client.get(_url())
    else:
        r = web_client.post(_url(), json={"persona": "想换成这份人设"})
    assert r.status_code == 404, (r.status_code, _body(r))
    assert _body(r) == {"error": "bot_not_found", "detail": u.GATE_404_DETAIL}, _body(r)
    u.assert_roots_untouched(frozen)


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_判序_同名且生效的是旧系统那份_新系统那份目录写在外面_是404(
        web_client, sandbox, botlife_db, method):
    outside = _outside(sandbox)
    _yml(sandbox, enabled=False, bot_channel_path=str(outside))
    u.write_bot_yml(sandbox.legacy_cfg, BOT, display_name="旧系统的", dispatcher_port=17899)
    if method == "GET":
        r = web_client.get(_url())
    else:
        r = web_client.post(_url(), json={"persona": "想换成这份人设"})
    assert r.status_code == 404, (r.status_code, _body(r))
    assert _body(r) == {"error": "bot_not_found", "detail": u.GATE_404_DETAIL}, _body(r)


# ============================================================ 判定顺序：在 bad_yaml 与读文件错之前

def _broken_yml_with_outside_link(sandbox):
    """配置解析不出（目录按默认位置算），而默认位置是一个指到外面的软链。"""
    outside = _outside(sandbox)
    _home(sandbox).mkdir(parents=True)
    os.symlink(str(outside), str(_home(sandbox) / "channel"), target_is_directory=True)
    yml = sandbox.dsh_cfg / ("%s.yml" % BOT)
    yml.write_text("id: [oops\n  x: : :\n", encoding="utf-8")
    return SimpleNamespace(yml=yml, outside=outside)


def test_判序_读_配置解析不出且默认位置指到外面_是bad_channel_path不是bad_yaml(
        web_client, sandbox):
    _broken_yml_with_outside_link(sandbox)
    r = web_client.get(_url())
    assert r.status_code == 409, (r.status_code, _body(r))
    assert _body(r) == REJECTED, _body(r)


def test_判序_写_配置解析不出且默认位置指到外面_是bad_channel_path不是bad_yaml(
        web_client, sandbox, botlife_db):
    made = _broken_yml_with_outside_link(sandbox)
    frozen = u.freeze_roots(made.outside, sandbox.dsh_cfg)
    r = web_client.post(_url(), json={"display_name": "想改成这个"})
    assert r.status_code == 409, (r.status_code, _body(r))
    assert _body(r) == REJECTED, _body(r)
    u.assert_roots_untouched(frozen)


def test_判序_读_配置解析不出但默认位置在家里_仍是bad_yaml(web_client, sandbox):
    """互不遮挡的另一半：目录没问题时，坏配置照旧报 `bad_yaml`。"""
    _fill(_home(sandbox) / "channel", INSIDE_PERSONA, INSIDE_VOICE)
    (sandbox.dsh_cfg / ("%s.yml" % BOT)).write_text("id: [oops\n  x: : :\n", encoding="utf-8")
    r = web_client.get(_url())
    assert r.status_code == 409, (r.status_code, _body(r))
    assert _body(r).get("error") == "bad_yaml", _body(r)


def test_判序_读_配置读不出来且默认位置指到外面_是bad_channel_path不是500(web_client, sandbox):
    """§10：这份 yml 读不出结构时目录按默认位置算；落点这一格排在 `config_unreadable` 之前。"""
    made = _channel_is_link(sandbox)
    os.chmod(made.yml, 0)
    try:
        r = web_client.get(_url())
    finally:
        os.chmod(made.yml, 0o600)
    assert r.status_code == 409, (r.status_code, _body(r))
    assert _body(r) == REJECTED, _body(r)


def test_判序_写音色_目录在外面且那里的白名单是坏的_是bad_channel_path不是bad_access_json(
        web_client, sandbox, botlife_db):
    """落点这一格在 `bad_access_json` 之前；零读取，所以根本不该知道外面那份白名单是坏的。"""
    made = _explicit_outside(sandbox)
    (made.outside / "access.json").write_text("{ 坏的", encoding="utf-8")
    frozen = u.freeze_roots(made.outside)
    r = web_client.post(_url(), json={"fish_voice_id": "new-voice-99"})
    assert r.status_code == 409, (r.status_code, _body(r))
    assert _body(r) == REJECTED, _body(r)
    u.assert_roots_untouched(frozen)


# ============================================================ 判定顺序：体的形状错与 413 仍在它前面

@pytest.mark.parametrize("body,code,error", [
    pytest.param({}, 400, "bad_body", id="bad_body"),
    pytest.param({"display_name": ""}, 400, "bad_field", id="bad_field"),
    pytest.param({"persona": "a" * 65537}, 413, "payload_too_large", id="payload_too_large"),
])
def test_判序_写_体的形状错与超长先于落点校验(web_client, sandbox, botlife_db, body, code, error):
    """§10b 的表：400、413 在 404 之前，自然也在 `bad_channel_path` 之前。"""
    made = _explicit_outside(sandbox)
    frozen = u.freeze_roots(made.outside, sandbox.dsh_cfg)
    r = web_client.post(_url(), json=body)
    assert r.status_code == code, (r.status_code, _body(r))
    assert _body(r).get("error") == error, _body(r)
    u.assert_roots_untouched(frozen)
