# -*- coding: utf-8 -*-
"""频道目录写成 `~/…` 时，通知要落到家目录底下（接口约定 `INTERFACE-新系统bot运维.md` §14，第 8 稿）。

起因：配置里的 `bot_channel_path` 可以写成 `~/…`（仓库里的示例配置、此前添加 bot 页建出来的
配置都是这种写法）。管理台拿到这个值之后直接拼路径、建目录，没有把 `~` 展开，结果在管理台
进程的工作目录下建出一棵名字就叫 `~` 的目录，通知文件写进了那里，bot 收不到。

守用户感觉得到的结果：用户发一条朋友圈，没有网关在跑的 bot 的通知文件出现在
`<家目录>/…/chats/<chat_id>/inbox/` 下；工作目录下不冒出 `~`。

隔离：`HOME` 是沙箱里的假家目录；每条用例先把进程的工作目录换到 tmp 里的一个空目录
（这样还没改好的代码建出来的那棵 `~` 也只会落在 tmp 里，不会弄脏仓库）。

没覆盖：§14 场景 1「评论回复的通知」（`POST /api/comment`）。那个接口的请求形状与
「bot 的一条朋友圈」怎么造，这份接口约定与更早的接口约定里都没有写。
"""
import json
from pathlib import Path

import pytest

from .conftest import free_port, write_bot_cfg

CHAT_ID = "77"


def _inbox_files(channel):
    inbox = Path(channel) / "chats" / CHAT_ID / "inbox"
    if not inbox.is_dir():
        return []
    return sorted(inbox.glob("user-moment-*.json"))


def _legacy_bot(sandbox, name, channel, port):
    """旧系统的 bot：没有网关，通知只能写进它的频道目录。`channel` 原样写进配置。"""
    write_bot_cfg(sandbox["env"]["HUB_CONFIGS_DIR"], name, display_name=name,
                  bot_channel_path=channel, chat_id=CHAT_ID, dispatcher_port=port)


def _dsh_bot_without_gateway(sandbox, name, channel, port):
    """新系统的 bot，但网关没在跑（state 里没有 api.port）：通知落回它自己的频道目录。"""
    write_bot_cfg(sandbox["env"]["HUB_CONFIGS_DSH_DIR"], name, display_name=name,
                  bot_channel_path=channel, chat_id=CHAT_ID, dispatcher_port=port)


def _post_moment(hub, text):
    r = hub.post("/api/moment", json={"text": text})
    assert r.status_code == 200, "发朋友圈期望 200，实得 %s，%s" % (r.status_code, r.get_data(as_text=True))


@pytest.fixture
def cwd(sandbox, hub, monkeypatch):
    """管理台进程的工作目录：tmp 里的一个空目录（门户已经加载完再换）。"""
    d = sandbox["tmp"] / "panel-cwd"
    d.mkdir()
    monkeypatch.chdir(d)
    return d


MAKERS = pytest.mark.parametrize("make", [_legacy_bot, _dsh_bot_without_gateway],
                                 ids=["legacy-bot", "dsh-bot-no-gateway"])


@MAKERS
def test_频道目录写成波浪号_发圈的通知落在家目录底下(sandbox, hub, dispatcher, cwd, make):
    make(sandbox, "tildebot", "~/chan/x", dispatcher.port)
    _post_moment(hub, "今天下雨")
    files = _inbox_files(sandbox["home"] / "chan" / "x")
    assert len(files) == 1, "通知应恰好一份落在 <HOME>/chan/x/chats/%s/inbox/ 下，实得 %s" % (CHAT_ID, files)


@MAKERS
def test_频道目录写成波浪号_工作目录下没有冒出名叫波浪号的目录(sandbox, hub, dispatcher, cwd, make):
    make(sandbox, "tildebot", "~/chan/x", dispatcher.port)
    _post_moment(hub, "今天下雨")
    assert not (cwd / "~").exists(), "管理台在工作目录下建出了一棵名字叫 ~ 的目录"
    assert list(cwd.iterdir()) == [], "工作目录下多出了东西：%s" % [p.name for p in cwd.iterdir()]


@MAKERS
def test_频道目录写成波浪号_通知里带着用户发的正文(sandbox, hub, dispatcher, cwd, make):
    make(sandbox, "tildebot", "~/chan/x", dispatcher.port)
    _post_moment(hub, "今天下雨")
    files = _inbox_files(sandbox["home"] / "chan" / "x")
    assert files, "家目录底下没有通知文件"
    assert "今天下雨" in json.loads(files[0].read_text(encoding="utf-8"))["text"]


@MAKERS
def test_频道目录写绝对路径_通知的位置文件名与内容照旧(sandbox, hub, dispatcher, cwd, make):
    """对照：本来就是绝对路径的，行为与以前相同。"""
    channel = sandbox["tmp"] / "run" / "abs-channel"
    make(sandbox, "absbot", channel, dispatcher.port)
    _post_moment(hub, "晚安")
    files = _inbox_files(channel)
    assert len(files) == 1, "绝对路径的频道目录应恰好收到一份通知，实得 %s" % files
    assert "晚安" in json.loads(files[0].read_text(encoding="utf-8"))["text"]
    assert list(cwd.iterdir()) == [], "工作目录下多出了东西：%s" % [p.name for p in cwd.iterdir()]


def test_两只bot一只波浪号一只绝对路径_各收各的一份(sandbox, hub, dispatcher, cwd):
    absolute = sandbox["tmp"] / "run" / "abs-channel"
    _legacy_bot(sandbox, "tildebot", "~/chan/x", dispatcher.port)
    _legacy_bot(sandbox, "absbot", absolute, free_port())      # 两只的端口各不相同
    _post_moment(hub, "在吗")
    assert len(_inbox_files(sandbox["home"] / "chan" / "x")) == 1
    assert len(_inbox_files(absolute)) == 1
