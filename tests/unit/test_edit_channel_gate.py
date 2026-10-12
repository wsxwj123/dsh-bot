# -*- coding: utf-8 -*-
"""编辑端点频道目录的取法与落点校验的白盒单测（INTERFACE-新系统bot运维 §10，第 7 稿）。

- 目录只取新系统配置根下这只的 yml **自己**顶层写的 `bot_channel_path`；没写、为空、
  或这份 yml 读不出结构，就是 `<DSH_BOT_HOME>/bots/<bot>/channel`。`life_config` 那份写的
  不参与。
- 把 `<DSH_BOT_HOME>` 解析成真实路径，这只 bot 的家 = 它下面的 `bots/<bot_id>`。频道目录
  解析成真实路径之后必须就是这个家或在它里面，否则 409 `bad_channel_path`，零读取零写入。
- 判定顺序：404 → `bad_channel_path` → `bad_yaml` / `config_unreadable`。

读与写共用一处：`hub_addbot._edit_open` 做前半段，里面调 `_channel_dir` 取目录并校验。
这里直接调它们与 `edit_read` / `edit_save`。「外面」一律是用例 tmp 下的另一个目录；
bot 名全是假名。改名字那几条把写朋友圈库的那一步换成记账的替身，不碰真库。
"""
import os

import pytest

from moments import hub_addbot, hub_config
from moments.provider_model import HubError

pytestmark = pytest.mark.skipif(os.name == "nt", reason="软链与 POSIX 权限位在 Windows 上语义不同")

BOT = "xbot"
INSIDE, OUTSIDE = "家里的人设（甲）", "外面的人设（乙）"
BROKEN = "id: [oops\n  x: : :\n"


@pytest.fixture
def w(tmp_path, monkeypatch):
    """新旧两个配置根、新系统家目录、一个「外面」，全在 tmp；HOME 钉在 tmp。"""
    from types import SimpleNamespace
    d = {n: tmp_path / n for n in ("legacy", "dsh", "home", "dsh_home", "outside", "life")}
    for p in d.values():
        p.mkdir()
    monkeypatch.setenv("HOME", str(d["home"]))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(d["legacy"]))
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(d["dsh"]))
    monkeypatch.setenv("DSH_BOT_HOME", str(d["dsh_home"]))
    monkeypatch.delenv("HUB_BOTS_FILE", raising=False)
    real_home = os.path.join(os.path.realpath(str(d["dsh_home"])), "bots", BOT)
    return SimpleNamespace(tmp=tmp_path, bot_home=d["dsh_home"] / "bots" / BOT,
                           real_bot_home=real_home, **d)


def _fill(ch, persona, voice="v1"):
    ch.mkdir(parents=True, exist_ok=True)
    (ch / "CLAUDE.md").write_text(persona, encoding="utf-8")
    (ch / "access.json").write_text('{"allowFrom": [1], "voiceId": "%s"}\n' % voice, encoding="utf-8")
    return ch


def _yml(w, text="", bot=BOT):
    p = w.dsh / ("%s.yml" % bot)
    p.write_text("id: %s\ndisplay_name: 旧名\n%s" % (bot, text), encoding="utf-8")
    return p


def _dir(text=None):
    """直接量 `_channel_dir`：`text` 是 yml 里 `bot_channel_path` 那一行（None = 不写）。"""
    src = "id: %s\n" % BOT + ("" if text is None else "bot_channel_path: %s\n" % text)
    return hub_addbot._channel_dir(BOT, hub_config.compose(src))


def _refused(fn, *args):
    with pytest.raises(HubError) as e:
        fn(*args)
    assert (e.value.status, e.value.error, e.value.detail) == (
        409, "bad_channel_path", hub_addbot.BAD_CHANNEL_PATH_DETAIL), (e.value.status, e.value.error)


def test_被拒时的那句话逐字():
    assert hub_addbot.BAD_CHANNEL_PATH_DETAIL == (
        "频道目录不在这只 bot 的新系统家目录里（bot_channel_path 或软链指到了外面），这一页不读也不改它")


# ------------------------------------------------------------ 取法：只认这份 yml 自己写的

def test_没写_用家目录下的默认位置(w):
    assert _dir() == os.path.join(w.real_bot_home, "channel")


def test_yml读不出结构_也按默认位置算(w):
    assert hub_addbot._channel_dir(BOT, None) == os.path.join(w.real_bot_home, "channel")


@pytest.mark.parametrize("text", ['""', '"   "', "", "123", "true", "[a, b]", "{a: 1}", "~"],
                         ids=["empty", "blank", "null", "int", "bool", "list", "map", "yaml-null"])
def test_为空或不是字符串_当没写_用默认位置(w, text):
    assert _dir(text) == os.path.join(w.real_bot_home, "channel")


def test_自己写了家里的另一处_用它(w):
    assert _dir(str(w.bot_home / "custom")) == os.path.join(w.real_bot_home, "custom")


def test_写的就是家目录本身_也算在里面(w):
    assert _dir(str(w.bot_home)) == w.real_bot_home


def test_末尾带斜杠与多余的点_规范之后还在家里就行(w):
    assert _dir(str(w.bot_home) + "/./channel/") == os.path.join(w.real_bot_home, "channel")


def test_波浪号按HOME展开_展开到家里的也认(w, monkeypatch):
    monkeypatch.setenv("HOME", str(w.bot_home))
    assert _dir("~/channel") == os.path.join(w.real_bot_home, "channel")


# ------------------------------------------------------------ 落点：不在这只 bot 自己家里的都拒

def test_外面的绝对路径_拒(w):
    _refused(_dir, str(w.outside))


def test_名字以这只打头的另一个目录_拒(w):
    _refused(_dir, str(w.dsh_home / "bots" / (BOT + "_evil") / "channel"))
    _refused(_dir, str(w.dsh_home / "bots" / (BOT + "2")))


def test_别的bot的家_拒(w):
    _refused(_dir, str(w.dsh_home / "bots" / "otherbot" / "channel"))


@pytest.mark.parametrize("up", ["", "bots"], ids=["dsh-home", "bots-dir"])
def test_家目录的上级_拒(w, up):
    _refused(_dir, str(w.dsh_home / up))


def test_用点点从自己家里绕出去_拒(w):
    _refused(_dir, str(w.bot_home / "channel" / ".." / ".." / "otherbot" / "channel"))


def test_波浪号展开到新系统家目录之外_拒(w):
    _refused(_dir, "~/outside-by-tilde")


def test_相对路径_拒(w, monkeypatch):
    monkeypatch.chdir(w.tmp)
    _refused(_dir, "bots/xbot/channel")


# ------------------------------------------------------------ 软链：跟到底再判

def test_默认位置是指到外面的软链_拒(w):
    w.bot_home.mkdir(parents=True)
    os.symlink(str(w.outside), str(w.bot_home / "channel"), target_is_directory=True)
    _refused(_dir)


def test_默认位置是指到家里另一处的软链_放行_交出的是解析后的真实路径(w):
    real = w.bot_home / "real-channel"
    real.mkdir(parents=True)
    os.symlink(str(real), str(w.bot_home / "channel"), target_is_directory=True)
    assert _dir() == os.path.join(w.real_bot_home, "real-channel")


def test_bot家这一级本身是指到外面的软链_拒(w):
    (w.dsh_home / "bots").mkdir()
    (w.outside / "channel").mkdir()
    os.symlink(str(w.outside), str(w.bot_home), target_is_directory=True)
    _refused(_dir)
    _refused(_dir, str(w.bot_home))


def test_bots这一级是指到外面的软链_拒(w):
    (w.outside / BOT / "channel").mkdir(parents=True)
    os.symlink(str(w.outside), str(w.dsh_home / "bots"), target_is_directory=True)
    _refused(_dir)


def test_新系统家目录本身是软链_放行(w, monkeypatch):
    link = w.tmp / "dsh-home-link"
    os.symlink(str(w.dsh_home), str(link), target_is_directory=True)
    monkeypatch.setenv("DSH_BOT_HOME", str(link))
    assert _dir() == os.path.join(w.real_bot_home, "channel")
    assert _dir(str(link / "bots" / BOT / "channel")) == os.path.join(w.real_bot_home, "channel")


# ------------------------------------------------------------ 读与写共用的前半段：顺序与零读写

@pytest.fixture
def spy(monkeypatch):
    """记下读过哪些文件、写过哪些文件、动没动朋友圈库（都只记账，读写本身照常）。"""
    from types import SimpleNamespace
    seen = SimpleNamespace(read=[], wrote=[], db=[])
    real_read, real_write = hub_addbot._read_bytes, hub_addbot._atomic_bytes

    def read(path, missing_ok=False):
        seen.read.append(str(path))
        return real_read(path, missing_ok=missing_ok)

    def write(path, data):
        seen.wrote.append(str(path))
        return real_write(path, data)

    monkeypatch.setattr(hub_addbot, "_read_bytes", read)
    monkeypatch.setattr(hub_addbot, "_atomic_bytes", write)
    monkeypatch.setattr(hub_addbot, "_save_db_name",
                        lambda cfg, bot_id, value, undo: seen.db.append((bot_id, value)))
    return seen


def test_读_目录在外面_409_只读过那份yml_外面的文件没碰(w, spy):
    _fill(w.outside, OUTSIDE)
    yml = _yml(w, "bot_channel_path: %s\n" % w.outside)
    _refused(hub_addbot.edit_read, BOT)
    assert spy.read == [str(yml)]


@pytest.mark.parametrize("body", [{"display_name": "想改成这个"}, {"persona": "想换的人设"},
                                  {"fish_voice_id": "v9"}],
                         ids=["display_name", "persona", "fish_voice_id"])
def test_写_目录在外面_不论改哪个字段都是409_零写入_朋友圈库不动(w, spy, body):
    _fill(w.outside, OUTSIDE)
    _fill(w.bot_home / "channel", INSIDE)           # 家里默认位置也有一份：被拒了不许转头写它
    yml = _yml(w, "bot_channel_path: %s\n" % w.outside)
    before = yml.read_bytes()
    _refused(hub_addbot.edit_save, BOT, body)
    assert spy.read == [str(yml)] and spy.wrote == [] and spy.db == []
    assert yml.read_bytes() == before
    assert (w.outside / "CLAUDE.md").read_text(encoding="utf-8") == OUTSIDE
    assert (w.bot_home / "channel" / "CLAUDE.md").read_text(encoding="utf-8") == INSIDE


def test_life那份写了外面_不参与_读写的都是家里那份(w, spy):
    _fill(w.outside, OUTSIDE, "outside-voice")
    inside = _fill(w.bot_home / "channel", INSIDE, "inside-voice")
    life = w.life / ("%s.yml" % BOT)
    life.write_text("bot_channel_path: %s\n" % w.outside, encoding="utf-8")
    _yml(w, "life_config: %s\n" % life)
    got = hub_addbot.edit_read(BOT)
    assert (got["persona"], got["fish_voice_id"]) == (INSIDE, "inside-voice")
    body, code = hub_addbot.edit_save(BOT, {"persona": "改后的人设"})
    assert code == 200
    assert (inside / "CLAUDE.md").read_text(encoding="utf-8") == "改后的人设"
    assert (w.outside / "CLAUDE.md").read_text(encoding="utf-8") == OUTSIDE
    assert not any(str(w.outside) in p for p in spy.read + spy.wrote), (spy.read, spy.wrote)


def test_写_经软链落在家里另一处_写进的是解析后的真实目录(w, spy):
    real = _fill(w.bot_home / "real-channel", INSIDE)
    os.symlink(str(real), str(w.bot_home / "channel"), target_is_directory=True)
    _yml(w)
    _body, code = hub_addbot.edit_save(BOT, {"persona": "改后的人设"})
    assert code == 200
    assert spy.wrote == [os.path.join(w.real_bot_home, "real-channel", "CLAUDE.md")]
    assert (real / "CLAUDE.md").read_text(encoding="utf-8") == "改后的人设"


def test_判序_配置解析不出且默认位置指到外面_先报落点(w, spy):
    w.bot_home.mkdir(parents=True)
    os.symlink(str(_fill(w.outside, OUTSIDE)), str(w.bot_home / "channel"), target_is_directory=True)
    (w.dsh / ("%s.yml" % BOT)).write_text(BROKEN, encoding="utf-8")
    _refused(hub_addbot.edit_read, BOT)
    _refused(hub_addbot.edit_save, BOT, {"display_name": "想改成这个"})
    assert spy.wrote == [] and spy.db == []


def test_判序_配置解析不出但目录在家里_读与改名字照旧报bad_yaml(w, spy):
    _fill(w.bot_home / "channel", INSIDE)
    (w.dsh / ("%s.yml" % BOT)).write_text(BROKEN, encoding="utf-8")
    for call in (lambda: hub_addbot.edit_read(BOT),
                 lambda: hub_addbot.edit_save(BOT, {"display_name": "想改成这个"})):
        with pytest.raises(HubError) as e:
            call()
        assert (e.value.status, e.value.error) == (409, "bad_yaml")
    assert spy.wrote == [] and spy.db == []


def test_配置解析不出但目录在家里_只改人设仍能保存(w, spy):
    """既有行为保住：yml 坏了不妨碍换人设（目录按默认位置算，落点校验照过）。"""
    inside = _fill(w.bot_home / "channel", INSIDE)
    (w.dsh / ("%s.yml" % BOT)).write_text(BROKEN, encoding="utf-8")
    _body, code = hub_addbot.edit_save(BOT, {"persona": "改后的人设"})
    assert code == 200
    assert (inside / "CLAUDE.md").read_text(encoding="utf-8") == "改后的人设"
    assert (w.dsh / ("%s.yml" % BOT)).read_text(encoding="utf-8") == BROKEN


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root 读得了权限 0 的文件")
def test_判序_配置读不出来_默认位置指到外面先报落点_在家里才报读不出(w, spy):
    yml = _yml(w)
    w.bot_home.mkdir(parents=True)
    link = w.bot_home / "channel"
    os.symlink(str(_fill(w.outside, OUTSIDE)), str(link), target_is_directory=True)
    os.chmod(yml, 0)
    try:
        _refused(hub_addbot.edit_read, BOT)
        os.unlink(str(link))
        _fill(link, INSIDE)                         # 换成家里一个真目录
        with pytest.raises(HubError) as e:
            hub_addbot.edit_read(BOT)
        assert (e.value.status, e.value.error) == (500, "config_unreadable")
    finally:
        os.chmod(yml, 0o600)


def test_判序_旧系统的bot哪怕目录写在外面_是404不是落点那一格(w, spy):
    (w.legacy / ("%s.yml" % BOT)).write_text(
        "id: %s\nbot_channel_path: %s\n" % (BOT, w.outside), encoding="utf-8")
    for call in (lambda: hub_addbot.edit_read(BOT),
                 lambda: hub_addbot.edit_save(BOT, {"persona": "x"})):
        with pytest.raises(HubError) as e:
            call()
        assert (e.value.status, e.value.error) == (404, "bot_not_found")
    assert spy.read == [] and spy.wrote == []
