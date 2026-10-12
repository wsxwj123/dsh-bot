# -*- coding: utf-8 -*-
"""形状校验是整串匹配的白盒单测（INTERFACE-新系统bot运维 §0 第 6 条、§5b 第 5 条，第 7 稿）。

Python 的 `$` 在字符串末尾那个换行之前也算匹配，所以 `^...$` 配 `.match()` 会放过
「合法名字加一个换行」。第 7 稿把这几条正则的结尾都换成 `\\Z`（只认字符串真正的末尾），
调用处的 `.match` 不动。这里量三件事：每条正则自己不再放过首尾多出来的东西；bot id 的
形状全仓库只有一条；各入口拿到带换行的名字时在形状这一步就拒了，没往下走。

不起子进程、不发网络请求；bot 名与供应商名全是假名。
"""
import re
from pathlib import Path

import pytest

from moments import bot_launchd, config_sources, hub_addbot, hub_routes
from moments.provider_model import HubError

TOKEN = "123456789:test-AAaaBBccDDddEEeeFFggHHhh11"
MOMENTS = Path(hub_addbot.__file__).resolve().parent

#: (说明, 正则, 一个合法的值)
SHAPES = [
    ("bot id", config_sources.BOT_ID_RE, "xbot"),
    ("telegram token", hub_addbot.TOKEN_RE, TOKEN),
    ("getMe base", hub_addbot.BASE_RE, "https://api.example.com"),
    ("音色 id", hub_addbot.FISH_VOICE_RE, "voice_1"),
    ("Fish key", hub_addbot.FISH_KEY_RE, "abcdefgh"),
    ("像 token 的 Fish key", hub_addbot.FISH_KEY_TELEGRAM_RE, TOKEN),
    ("换模型端点的 bot 名", hub_routes._DM_BOT_RE, "Bot-4"),
    ("供应商名", hub_routes._DM_PROVIDER_RE, "My-Proxy_2"),
]
#: 合法值尾巴上多出来的东西；`\n` 是 `$` 会放过的那一种
TAILS = ["\n", "\r\n", "\r", " ", "\t", "\x00", "\n\n"]
HEADS = ["\n", " ", "\t"]


@pytest.mark.parametrize("what,regex,good", SHAPES, ids=[s[0] for s in SHAPES])
def test_合法的值照常匹配(what, regex, good):
    assert regex.match(good), what


@pytest.mark.parametrize("what,regex,good", SHAPES, ids=[s[0] for s in SHAPES])
@pytest.mark.parametrize("tail", TAILS, ids=[repr(t) for t in TAILS])
def test_末尾多出一点东西就不匹配(what, regex, good, tail):
    assert regex.match(good + tail) is None, (what, good + tail)


@pytest.mark.parametrize("what,regex,good", SHAPES, ids=[s[0] for s in SHAPES])
@pytest.mark.parametrize("head", HEADS, ids=[repr(h) for h in HEADS])
def test_开头多出一点东西就不匹配(what, regex, good, head):
    assert regex.match(head + good) is None, (what, head + good)


def test_bot_id的形状全仓库只有一条_别处都是引用():
    assert hub_addbot.BOT_ID_RE is config_sources.BOT_ID_RE


def test_供应商名用的是它自己的字符集_大写与连字符合法():
    """整串匹配只管首尾，不许顺手把 bot id 的字符集套到供应商名上。"""
    for name in ("My-Proxy_2", "A", "a1", "OpenRouter", "x" * 32):
        assert hub_routes._DM_PROVIDER_RE.match(name), name
    for name in ("", "-lead", "_lead", "has space", "x" * 33, "名字"):
        assert hub_routes._DM_PROVIDER_RE.match(name) is None, name


@pytest.mark.parametrize("fn", ["config_sources.py", "hub_addbot.py", "hub_routes.py"])
def test_这三个文件里不再有以美元符结尾的单行形状正则(fn):
    """守住写法：以后在这里新加形状正则，结尾也得是 `\\Z`。带 re.M 的逐行模式不在此列。"""
    src = (MOMENTS / fn).read_text(encoding="utf-8")
    left = [m.group(0) for m in re.finditer(r"compile\(r?\"[^\"\n]*\$\"\)", src)]
    assert left == [], left


# ------------------------------------------------------------ 各入口：形状这一步就拒

BAD_IDS = ["xbot\n", " xbot", "xbot ", "xbot\r\n", "\nxbot", "xbot\t", "Xbot", "x-bot"]


@pytest.mark.parametrize("bad", BAD_IDS, ids=[repr(b) for b in BAD_IDS])
def test_拼路径的三处_名字不是整串合格就不给路径(bad, tmp_path):
    seams = bot_launchd.Seams("/nonexistent/launchctl", str(tmp_path / "agents"), tmp_path)
    assert config_sources.dsh_bot_dir(bad) is None
    assert config_sources.find_path(bad) is None
    assert bot_launchd.bot_home(bad, seams) is None


@pytest.mark.parametrize("bad", BAD_IDS, ids=[repr(b) for b in BAD_IDS])
def test_放行闸_名字不是整串合格_400_bad_bot_id(bad):
    with pytest.raises(HubError) as e:
        hub_addbot._edit_target(bad)
    assert (e.value.status, e.value.error, e.value.detail) == (400, "bad_bot_id", "bot id 形状非法")


def _create_body(bot_id):
    return {"bot_id": bot_id, "telegram_token": TOKEN, "dispatcher_port": 17990,
            "chat_id": 1, "display_name": "新来的", "persona_text": "人设"}


@pytest.mark.parametrize("bad", BAD_IDS + ["jiwen\n", "director\n", "voicecall\n"],
                         ids=lambda b: repr(b))
def test_新建的校验_bot_id不是整串合格_在形状这一段就是400_bad_field(bad, monkeypatch):
    """保留名加换行也落在这一格：不许「形状过了、值却不等于保留名」，那样拿到的会是别的码。"""
    def boom(*_a, **_k):
        raise AssertionError("形状没过，不该走到查同名与保留名那一步")

    monkeypatch.setattr(hub_addbot, "_check_free", boom)
    with pytest.raises(HubError) as e:
        hub_addbot._validate(_create_body(bad))
    assert (e.value.status, e.value.error) == (400, "bad_field")


@pytest.mark.parametrize("tail", ["\n", "\r\n", " "], ids=repr)
def test_token末尾多出东西_本地预检就是malformed_不走到拼地址那一步(tail, monkeypatch):
    def boom():
        raise AssertionError("预检没过，不该去取 getMe 的地址")

    monkeypatch.setattr(hub_addbot, "api_base", boom)
    assert hub_addbot.getme(TOKEN + tail) == {"ok": False, "stage": "malformed"}
    assert hub_addbot.test_token(TOKEN + tail) == {"ok": False, "stage": "malformed"}


# ------------------------------------------------------------ 供应商名：路由层零转发

@pytest.fixture
def client(tmp_path, monkeypatch):
    from flask import Flask

    monkeypatch.setenv("HOME", str(tmp_path))
    app = Flask(__name__)
    app.register_blueprint(hub_routes.hub_bp)
    app.config["TESTING"] = True
    return app.test_client()


BAD_PROVIDER = {"ok": False, "error": "bad_provider", "text": "供应商名字不合法"}
PROVIDER_PATHS = ["preview", "refresh", "test", "remove", "models"]


@pytest.mark.parametrize("tail", ["%0A", "%0D%0A", "%20"])
@pytest.mark.parametrize("leaf", PROVIDER_PATHS)
def test_供应商名末尾多出东西_400_bad_provider_没走到找网关(client, monkeypatch, leaf, tail):
    def boom(*_a, **_k):
        raise AssertionError("名字不合法，不该走到转发那一步")

    monkeypatch.setattr(hub_routes, "_dm_forward", boom)
    r = client.post("/hub/api/dsh-model/provider/myproxy%s/%s" % (tail, leaf),
                    json={"action": "add", "id": "m1"})
    assert (r.status_code, r.get_json()) == (400, BAD_PROVIDER)


def test_保存供应商_名字末尾多一个换行_400_bad_provider_没走到找网关(client, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("名字不合法，不该走到转发那一步")

    monkeypatch.setattr(hub_routes, "_dm_forward", boom)
    r = client.post("/hub/api/dsh-model/provider/save",
                    json={"name": "myproxy\n", "api": "openai-completions",
                          "baseURL": "https://api.example.com/v1", "key": "sk-test-00000000"})
    assert (r.status_code, r.get_json()) == (400, BAD_PROVIDER)


@pytest.mark.parametrize("leaf", ["preview", "refresh", "test"])
def test_供应商名带大写下划线连字符_形状这一步放行_走到转发(client, monkeypatch, leaf):
    seen = []

    def forward(call, **_k):
        seen.append(1)
        return {"ok": True}, 200

    monkeypatch.setattr(hub_routes, "_dm_forward", forward)
    r = client.post("/hub/api/dsh-model/provider/My-Proxy_2/%s" % leaf, json={})
    assert r.status_code == 200 and seen == [1]


def test_换模型端点的bot名末尾多一个换行_400_bad_bot_id(client, monkeypatch):
    def boom():
        raise AssertionError("名字不合法，不该去找这只 bot 的网关")

    monkeypatch.setattr(hub_routes, "_dm_bots", boom)
    r = client.post("/hub/api/dsh-model/xbot%0A", json={"spec": "default"})
    assert r.status_code == 400 and r.get_json()["error"] == "bad_bot_id"
