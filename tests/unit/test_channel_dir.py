# -*- coding: utf-8 -*-
"""频道目录的统一取法的白盒单测（INTERFACE-新系统bot运维 §14 / §7，第 8 稿）。

- `config_sources.channel_dir(cfg)` 是管理台里取 `bot_channel_path` 的唯一入口：把开头的
  `~` 展开成家目录，本来就是绝对路径的原样返回，没写就返回空串。只展开，不做别的规整。
- 三处拿它拼路径、建目录的地方（评论回复的通知、用户发圈的通知、发圈后的配图通知）不再
  在工作目录下建出一棵名字叫 `~` 的目录；没写频道目录时一个目录都不建。
- 新建 bot 写进配置的 `bot_channel_path` 恒为绝对路径，不再收成 `~/…`。

验收那边没覆盖「评论回复的通知」与「配图通知」这两条路径，这里直接调函数补上。
全部落盘只在 tmp_path 下：HOME、两个配置根、朋友圈库、工作目录都钉在 tmp；出站连接拦死。
bot 名全是假名。
"""
import importlib
import json
import os
import socket
import types
from pathlib import Path

import pytest
import yaml

from moments import config_sources, hub_addbot

CHAT = "77"


# ============================================================ 取值函数本身

def test_波浪号开头_展开成家目录(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))   # Windows 上 expanduser 读它
    assert config_sources.channel_dir({"bot_channel_path": "~/chan/x"}) == str(tmp_path / "chan" / "x")


def test_只有一个波浪号_就是家目录(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))   # Windows 上 expanduser 读它
    assert config_sources.channel_dir({"bot_channel_path": "~"}) == str(tmp_path)


@pytest.mark.parametrize("raw", ["/abs/chan/x", "/abs/with space/x/", "/abs/~/x", "rel/chan",
                                 "/abs/chan/../x", "  /abs/padded  "])
def test_不以波浪号开头的_原样返回_一个字节都不规整(raw, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))   # Windows 上 expanduser 读它
    assert config_sources.channel_dir({"bot_channel_path": raw}) == raw


@pytest.mark.parametrize("cfg", [{}, {"bot_channel_path": None}, {"bot_channel_path": ""},
                                 {"bot_channel_path": "   "}, {"bot_channel_path": 123},
                                 {"bot_channel_path": ["~/x"]}, {"bot_channel_path": True},
                                 None, "~/x", []])
def test_没写或不是字符串或不是配置_返回空串(cfg):
    assert config_sources.channel_dir(cfg) == ""


def test_每次调用现读HOME_不缓存(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    monkeypatch.setenv("HOME", str(a))
    monkeypatch.setenv("USERPROFILE", str(a))   # Windows 上 expanduser 读它
    assert config_sources.channel_dir({"bot_channel_path": "~/c"}) == str(a / "c")
    monkeypatch.setenv("HOME", str(b))
    monkeypatch.setenv("USERPROFILE", str(b))   # Windows 上 expanduser 读它
    assert config_sources.channel_dir({"bot_channel_path": "~/c"}) == str(b / "c")


# ============================================================ 三处通知：落在家目录底下

class _Recorder:
    """`_urlopen` 的替身：只记地址，不回网络。"""

    def __init__(self):
        self.urls = []

    def __call__(self, req, *a, **k):
        self.urls.append(req if isinstance(req, str) else req.full_url)
        return types.SimpleNamespace(read=lambda: b"{}")


@pytest.fixture
def panel(tmp_path, monkeypatch):
    """隔离的朋友圈进程：HOME、配置根、库全在 tmp；工作目录换到 tmp 里的一个空目录。"""
    home, legacy, dsh, cwd = (tmp_path / n for n in ("home", "configs-legacy", "configs-dsh", "cwd"))
    for d in (home, legacy, dsh, cwd):
        d.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))   # Windows 上 expanduser 读它
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(legacy))
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(dsh))
    monkeypatch.setenv("BOTLIFE_STATE_DB", str(tmp_path / "state.db"))
    for k in list(os.environ):
        if k.startswith("DISPATCHER_PORT_"):
            monkeypatch.delenv(k, raising=False)

    def _no_net(sock, addr):
        raise AssertionError("用例不允许真连网络：%r" % (addr,))
    monkeypatch.setattr(socket.socket, "connect", _no_net)

    import db
    importlib.reload(db)
    assert Path(db.DB_PATH).resolve() == (tmp_path / "state.db").resolve(), "朋友圈库没落到 tmp，停手"
    db.init()
    import moments.web as web
    monkeypatch.setattr(web, "_urlopen", _Recorder())
    monkeypatch.chdir(cwd)

    def bot(name, channel):
        """旧系统配置根下一只在跑的 bot（没有网关，通知只能写进它的频道目录）。"""
        (legacy / ("%s.yml" % name)).write_text(
            'id: %s\nbot_channel_path: "%s"\nchat_id: "%s"\ndispatcher_port: 17961\n'
            % (name, channel, CHAT), encoding="utf-8")
        return {"_bot_id": name, "id": name, "bot_channel_path": channel, "chat_id": CHAT,
                "dispatcher_port": 17961}

    return types.SimpleNamespace(home=home, cwd=cwd, web=web, bot=bot, tmp=tmp_path)


def _inbox(channel, pattern):
    return sorted((Path(channel) / "chats" / CHAT / "inbox").glob(pattern))


MOMENT = {"id": 1, "text": "下雨了", "visibility": "public"}


def test_评论回复的通知_波浪号写法_落在家目录底下且工作目录干净(panel):
    cfg = panel.bot("tildebot", "~/chan/x")
    out = panel.web._trigger_bot_moment_reply(cfg, MOMENT, "你好", 5, "我")
    files = _inbox(panel.home / "chan" / "x", "moment-reply-*.json")
    assert len(files) == 1 and str(out) == str(files[0]), (out, files)
    assert "你好" in json.loads(files[0].read_text(encoding="utf-8"))["text"]
    assert list(panel.cwd.iterdir()) == [], "工作目录下多出了东西（多半是那棵叫 ~ 的目录）"


def test_评论回复的通知_绝对路径_位置与以前相同(panel):
    channel = panel.tmp / "run" / "abs-channel"
    cfg = panel.bot("absbot", str(channel))
    out = panel.web._trigger_bot_moment_reply(cfg, MOMENT, "你好", 5, "我")
    assert [str(p) for p in _inbox(channel, "moment-reply-*.json")] == [str(out)]
    assert list(panel.cwd.iterdir()) == []


def test_用户发圈的通知_波浪号写法_落在家目录底下且工作目录干净(panel):
    cfg = panel.bot("tildebot", "~/chan/x")
    panel.web._trigger_bot_see_user_moment(cfg, 3, "今天下雨", None, "public")
    assert len(_inbox(panel.home / "chan" / "x", "user-moment-*.json")) == 1
    assert list(panel.cwd.iterdir()) == []


def test_发圈后的配图通知_波浪号写法_落在家目录底下且工作目录干净(panel):
    from moments import post
    cfg = panel.bot("tildebot", "~/chan/x")
    out = post._write_moment_image("tildebot", cfg, 9, "配图的正文", "public")
    files = _inbox(panel.home / "chan" / "x", "moment-image-*.json")
    assert len(files) == 1 and str(out) == str(files[0]), (out, files)
    assert list(panel.cwd.iterdir()) == []


@pytest.mark.parametrize("missing", [None, "", "   "], ids=["no-key", "empty", "blank"])
def test_没写频道目录_三处都不建目录也不写文件(panel, missing):
    """拿空串去拼路径等于往工作目录里写：宁可这一条投不出去。"""
    from moments import post
    cfg = panel.bot("nodirbot", "/unused")
    if missing is None:
        del cfg["bot_channel_path"]
    else:
        cfg["bot_channel_path"] = missing
    with pytest.raises(KeyError):
        panel.web._trigger_bot_moment_reply(cfg, MOMENT, "你好", 5, "我")
    with pytest.raises(KeyError):
        panel.web._trigger_bot_see_user_moment(cfg, 3, "今天下雨", None, "public")
    with pytest.raises(KeyError):
        post._write_moment_image("nodirbot", cfg, 9, "配图的正文", "public")
    assert list(panel.cwd.iterdir()) == [], "没写频道目录，却在工作目录下建了东西"


# ============================================================ 模型页找网关

def test_模型页找网关_波浪号写法的频道目录照样认得出网关(tmp_path, monkeypatch):
    from moments import hub_routes
    home, dsh = tmp_path / "home", tmp_path / "configs-dsh"
    state = home / "gw" / "tildebot" / "state"
    state.mkdir(parents=True)
    dsh.mkdir()
    (state / "api.key").write_text("k", encoding="utf-8")
    (state / "api.port").write_text("1", encoding="utf-8")
    (dsh / "tildebot.yml").write_text(
        'id: tildebot\nbot_channel_path: "~/gw/tildebot/channel"\ndispatcher_port: 17962\n',
        encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))   # Windows 上 expanduser 读它
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(dsh))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(tmp_path / "configs-legacy-missing"))
    assert hub_routes._dm_bots() == [
        ("tildebot", "tildebot", str(home / "gw" / "tildebot" / "channel"))]


# ============================================================ 新建：写绝对路径

def _render(target):
    fields = {"bot_id": "newbot", "display_name": "小新", "chat_id": 1, "port": 17963}
    return yaml.safe_load(hub_addbot._render_yml(fields, target))["bot_channel_path"]


def test_新建_频道目录在HOME底下_写的是绝对路径不是波浪号(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))   # Windows 上 expanduser 读它
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(tmp_path / "configs-dsh"))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(tmp_path / "configs-legacy"))
    target = str(tmp_path / ".dsh-bot" / "bots" / "newbot" / "channel")
    assert _render(target) == target


def test_新建_给了相对的目录_写进去的也是绝对路径(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(tmp_path / "configs-dsh"))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(tmp_path / "configs-legacy"))
    monkeypatch.chdir(tmp_path)
    got = _render(os.path.join("rel-home", "bots", "newbot", "channel"))
    assert os.path.isabs(got) and got == os.path.join(os.getcwd(), "rel-home", "bots", "newbot", "channel")


def test_收成波浪号的那个函数已经删掉():
    assert not hasattr(hub_addbot, "_channel_path_literal")


def test_写出去的值经取值函数读回来_还是同一个目录(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))   # Windows 上 expanduser 读它
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(tmp_path / "configs-dsh"))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(tmp_path / "configs-legacy"))
    target = str(tmp_path / ".dsh-bot" / "bots" / "newbot" / "channel")
    assert config_sources.channel_dir({"bot_channel_path": _render(target)}) == target
