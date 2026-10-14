# -*- coding: utf-8 -*-
"""适配服务读 bot 配置：只读本系统配置根下那份 yml 自己的键（安审 F1、I1）。

本系统的 bot 配置可以写 ``life_config`` 指向另一套系统的旧配置。管理台共用的读法会打开那份
文件整份合并：那份坏了或被删，bot 从名单里整只消失；那份里写的显示名、供应商、频道目录会顶上来。
适配服务不许碰另一套系统的目录（BRIEF 归属边界）。这里在假家目录里摆一份「读了就出事」的旧配置，
旁边还摆好另一套系统那边的网关口令与端口，然后看三件事：结果只由本系统那份决定；另一套系统
目录下的文件一次都没被打开；网关只按本系统那份去找。

网关不起桩：``gateway_client._call`` 是适配服务到网关的唯一出口，换成记账的替身（一律拒连）。
"""
import builtins
import io
import json
import os
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

import gateway_client
from moments import adapter_service as svc

TOKEN = "unit-adapter-token"
BOT = "botalpha"
AUTH = {"Authorization": "Bearer " + TOKEN}
LIFE = "~/claudebotlife/configs/oldname.yml"        # 本系统那份 yml 里 life_config 的写法
CFG_PROVIDER = {"id": "deepseek-official", "name": "deepseek-official", "model": "deepseek-flash",
                "effort": None}                     # 本系统那份没写 brain：网关的默认值


class _Opens:
    """记下被测代码打开过的每个文件（解析成真实路径）。装上之后才开始记，摆数据时不算。"""

    def __init__(self, monkeypatch):
        self.paths = []
        real = builtins.open

        def spy(file, *a, **k):
            if isinstance(file, (str, bytes, os.PathLike)):
                self.paths.append(os.path.realpath(os.fsdecode(file)))
            return real(file, *a, **k)
        monkeypatch.setattr(builtins, "open", spy)
        monkeypatch.setattr(io, "open", spy)        # pathlib 走的是 io.open

    def under(self, roots):
        prefixes = [os.path.realpath(str(r)) + os.sep for r in roots]
        return [p for p in self.paths if any(p.startswith(x) for x in prefixes)]


@pytest.fixture
def world(tmp_path, monkeypatch):
    """假家目录 + 一只本系统的 bot + 另一套系统那边的旧配置、旧频道目录和那边的网关口令。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    token_file = tmp_path / "adapter.token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    monkeypatch.setenv("DSH_CONSOLE_TOKEN_FILE", str(token_file))

    other = [home / "claudebotlife", home / ".claude" / "channels"]   # 另一套系统的两处目录
    life = home / "claudebotlife" / "configs" / "oldname.yml"
    life.parent.mkdir(parents=True)
    legacy_channel = home / ".claude" / "channels" / "oldname"
    legacy_channel.mkdir(parents=True)
    legacy_state = legacy_channel.parent / "state"      # 按旧频道目录去找网关，就会找到这里
    legacy_state.mkdir()
    (legacy_state / "api.key").write_text("legacy-gateway-key\n", encoding="utf-8")
    (legacy_state / "api.port").write_text("9\n", encoding="utf-8")

    botdir = Path(os.environ["DSH_BOT_HOME"]) / "bots" / BOT
    (botdir / "state").mkdir(parents=True)
    (botdir / "channel").mkdir()
    (botdir / "state" / "api.key").write_text("unit-gateway-key\n", encoding="utf-8")
    (botdir / "state" / "api.port").write_text("9\n", encoding="utf-8")

    calls = []

    def _call(channel_dir, method, path, body=None, timeout=10):
        calls.append(os.path.realpath(channel_dir))
        raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
    monkeypatch.setattr(gateway_client, "_call", _call)

    def put_yml(**own):
        """本系统配置根下这只 bot 的 yml：只写 id、life_config、端口，外加 own。"""
        cfg = {"id": BOT, "life_config": LIFE, "dispatcher_port": 0, **own}
        cfg_dir = Path(os.environ["HUB_CONFIGS_DSH_DIR"])
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / ("%s.yml" % BOT)).write_text(json.dumps(cfg), encoding="utf-8")

    return SimpleNamespace(home=home, other=other, life=life, botdir=botdir, calls=calls,
                           put_yml=put_yml, legacy_channel=legacy_channel,
                           legacy_state=legacy_state, client=svc.create_app().test_client(),
                           opens=lambda: _Opens(monkeypatch))


def _get(w, path):
    r = w.client.get("/adapter/v1" + path, headers=AUTH)
    return r.status_code, r.get_json()


def _post_effort(w):
    r = w.client.post("/adapter/v1/bots/%s/provider" % BOT, json={"effort": "high"}, headers=AUTH)
    return r.status_code, r.get_json()


def _touched_other(w, opens):
    """另一套系统那边被碰过的：打开过的文件、拿来找网关的频道目录。"""
    gateway_dirs = [c for c in w.calls
                    if any(c.startswith(os.path.realpath(str(r)) + os.sep) for r in w.other)]
    return opens.under(w.other) + gateway_dirs


def _make_missing(life):
    """什么都不摆：life_config 指向的文件不存在。"""


def _make_dir(life):
    life.mkdir()


def _make_bad_yaml(life):
    life.write_text("display_name: [没有收尾\n", encoding="utf-8")


def _make_unreadable(life):
    life.write_text("display_name: 旧系统的名字\n", encoding="utf-8")
    life.chmod(0)


BROKEN = {"不存在": _make_missing, "是个目录": _make_dir, "不是合法YAML": _make_bad_yaml,
          "读不了": _make_unreadable}


@pytest.mark.parametrize("make", list(BROKEN.values()), ids=list(BROKEN))
def test_旧配置读不出_这只bot照样在名单里_结果只由本系统那份决定(world, make):
    if make is _make_unreadable and (sys.platform == "win32" or os.geteuid() == 0):
        pytest.skip("这个平台或身份下权限位拦不住读")
    world.put_yml()
    make(world.life)
    opens = world.opens()
    status, got = _get(world, "/bots")
    assert status == 200, got
    assert [(b["bot_id"], b["display_name"], b["provider"]) for b in got["bots"]] == [
        (BOT, BOT, CFG_PROVIDER)], "旧配置读不出不该让这只 bot 消失或变样：%r" % got
    status, got = _post_effort(world)
    assert (status, got["code"]) == (503, "unavailable"), "不该是没有这只 bot：%r" % (got,)
    assert _touched_other(world, opens) == [], "碰了另一套系统那边：%r" % _touched_other(world, opens)


def test_旧配置里的显示名_供应商_停用_频道目录_一个都不顶上来_那份文件一次都没打开(world):
    world.put_yml()
    world.life.write_text(json.dumps({
        "display_name": "旧系统的名字", "enabled": False,
        "bot_channel_path": "~/.claude/channels/oldname",
        "brain": {"provider": "old-vendor", "model": "old-model", "reasoning_effort": "max"},
    }), encoding="utf-8")
    opens = world.opens()
    status, got = _get(world, "/bots")
    assert status == 200 and len(got["bots"]) == 1, got
    row = got["bots"][0]
    assert row["display_name"] == BOT, "显示名取了旧配置里的：%r" % row
    assert row["provider"] == CFG_PROVIDER, "供应商取了旧配置里的：%r" % row
    assert row["state"] != "stopped", "停用取了旧配置里的：%r" % row
    assert _get(world, "/providers")[0] == 503
    assert _post_effort(world)[0] == 503
    assert _touched_other(world, opens) == [], "碰了另一套系统那边：%r" % _touched_other(world, opens)


# ---------------------------------------------------------------- 找网关（安审 I1）

def test_找网关_本系统那份没写频道目录_用bot家下的channel_不用旧配置里写的(world):
    world.put_yml()
    world.life.write_text(json.dumps({"bot_channel_path": "~/.claude/channels/oldname"}),
                          encoding="utf-8")
    opens = world.opens()
    assert _post_effort(world)[0] == 503                     # 替身一律拒连
    assert world.calls == [os.path.realpath(str(world.botdir / "channel"))], world.calls
    assert _touched_other(world, opens) == []


def _written_to_other(w):
    """本系统那份自己把频道目录写到另一套系统的频道目录。"""
    w.put_yml(bot_channel_path="~/.claude/channels/oldname")


def _written_as_bot_home(w):
    """频道目录写成 bot 家本身：旁边的 state 是 bots/state，不是这只 bot 的。"""
    shared = w.botdir.parent / "state"
    shared.mkdir()
    (shared / "api.key").write_text("not-this-bot\n", encoding="utf-8")
    (shared / "api.port").write_text("9\n", encoding="utf-8")
    w.put_yml(bot_channel_path=str(w.botdir))


def _state_linked_out(w):
    """频道目录写得没毛病，但 bot 家里的 state 是指到另一套系统那边的软链。"""
    for f in (w.botdir / "state").iterdir():
        f.unlink()
    (w.botdir / "state").rmdir()
    (w.botdir / "state").symlink_to(w.legacy_state, target_is_directory=True)
    w.put_yml(bot_channel_path=str(w.botdir / "channel"))


OUTSIDE = {"写到另一套系统的频道目录": _written_to_other, "写成bot家本身": _written_as_bot_home,
           "state经软链出去": _state_linked_out}


@pytest.mark.parametrize("make", list(OUTSIDE.values()), ids=list(OUTSIDE))
def test_找网关_口令所在的state落在bot家外面_当网关不可用_不去那里读(world, make):
    if make is _state_linked_out and sys.platform == "win32":
        pytest.skip("Windows 上建软链要特权")
    make(world)
    opens = world.opens()
    status, got = _post_effort(world)
    assert (status, got["code"]) == (503, "unavailable"), got
    assert _get(world, "/providers")[0] == 503
    assert _touched_other(world, opens) == []
    # 列表另外还读 bot 家里的心跳（state 下，管理台共用的读法，不归这里管），所以只看找没找网关
    assert _get(world, "/bots")[0] == 200
    assert world.calls == [], "不该按这个频道目录去找网关：%r" % world.calls
