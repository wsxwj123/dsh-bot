# -*- coding: utf-8 -*-
"""§6/§7 POST /hub/api/bots：新建 bot。

落盘目标改成新系统那套位置（配置根 + DSH_BOT_HOME 下的 bot 家），
旧系统两个根一个字节都不许产生。
"""
import json
import os
import re
import shutil

import pytest

from . import botops_util as u


def _payload(port, **over):
    body = {"bot_id": "xiaoyu", "telegram_token": u.FAKE_TOKEN, "dispatcher_port": port,
            "chat_id": 123456789, "display_name": "小雨",
            "persona_text": "我是小雨的人设正文。"}
    body.update(over)
    return body


def _post(client, port, **over):
    return client.post("/hub/api/bots", json=_payload(port, **over))


def _arm_ok(bun, telegram, launchctl, heartbeat, bot="xiaoyu", pid=4242):
    """把全流程成功要的外部条件摆好。"""
    telegram.mode = "ok"
    bun.expect(exit=0).expect(exit=0)
    launchctl.set_default(installed=True, running=True, pid=pid)
    heartbeat.tick(bot, pid=pid)


def _steps(body):
    return {s.get("name"): s for s in (body.get("steps") or []) if isinstance(s, dict)}


def _boom_moves(monkeypatch):
    """让"把造物搬到位"这一步必失败（覆盖常见的三种搬移原语）。"""
    def boom(*a, **kw):
        raise OSError("mocked move failure")
    monkeypatch.setattr(shutil, "move", boom)
    monkeypatch.setattr(os, "replace", boom)
    monkeypatch.setattr(os, "rename", boom)


# ------------------------------------------------------------ 正常路径

def test_新建_全流程成功_201且五步done_restart_hint两个false(
        client, sandbox, bun, telegram, launchctl, heartbeat, as_darwin):
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port())
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["ok"] is True
    assert body["bot_id"] == "xiaoyu"
    assert [s["name"] for s in body["steps"]] == ["validate", "getme", "files",
                                                  "register", "restart"]
    assert all(s["state"] == "done" for s in body["steps"]), body["steps"]
    assert body["restart_hint"] == {"bot": False, "moments_web": False}, body["restart_hint"]


def test_新建_落盘清单齐全(client, sandbox, bun, telegram, launchctl, heartbeat, as_darwin):
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port())
    assert r.status_code == 201, r.get_json()
    cfg = sandbox.dsh_cfg / "xiaoyu.yml"
    home = sandbox.dsh_home / "bots" / "xiaoyu"
    channel = home / "channel"
    assert cfg.is_file(), "配置没落在新系统配置根"
    assert channel.is_dir(), "频道树没落在 DSH_BOT_HOME 下的 bot 家"
    assert home.is_dir()
    assert (channel / ".env").read_text(encoding="utf-8").strip() == \
        "TELEGRAM_BOT_TOKEN=%s" % u.FAKE_TOKEN
    assert json.loads((channel / "access.json").read_text(encoding="utf-8")) == \
        {"allowFrom": [123456789]}
    assert "我是小雨的人设正文。" in (channel / "CLAUDE.md").read_text(encoding="utf-8")
    assert (channel / "memory" / "MEMORY.md").is_file()


@pytest.mark.skipif(os.name == "nt", reason="Windows 无 POSIX 权限位")
def test_新建_落盘权限(client, sandbox, bun, telegram, launchctl, heartbeat, as_darwin):
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port())
    assert r.status_code == 201, r.get_json()
    home = sandbox.dsh_home / "bots" / "xiaoyu"
    channel = home / "channel"
    assert u.mode_of(sandbox.dsh_cfg / "xiaoyu.yml") == 0o600
    assert u.mode_of(home) == 0o700
    assert u.mode_of(channel) == 0o700
    assert u.mode_of(channel / ".env") == 0o600
    assert u.mode_of(channel / "access.json") == 0o600
    assert u.mode_of(channel / "CLAUDE.md") == 0o600
    assert u.mode_of(channel / "memory" / "MEMORY.md") == 0o600


def test_新建_yml字段齐全且不含token(client, sandbox, bun, telegram, launchctl, heartbeat,
                                 as_darwin):
    port = u.free_port()
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, port)
    assert r.status_code == 201, r.get_json()
    cfg = sandbox.dsh_cfg / "xiaoyu.yml"
    data = u.read_yaml(cfg)
    assert data["id"] == "xiaoyu"
    assert data["display_name"] == "小雨"
    assert data["chat_id"] == 123456789
    assert data["dispatcher_port"] == port
    assert data["bot_channel_path"]
    assert re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}",
                        str(data["namespace_uuid"]))
    assert u.FAKE_TOKEN not in cfg.read_text(encoding="utf-8"), "token 进了 yml"
    assert u.FAKE_TOKEN not in json.dumps(r.get_json(), ensure_ascii=False), "响应带出 token"


def test_新建_token唯一落点是channel下的env(client, sandbox, bun, telegram, launchctl,
                                       heartbeat, as_darwin):
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port())
    assert r.status_code == 201, r.get_json()
    hits = []
    for root in (sandbox.dsh_home, sandbox.dsh_cfg):
        for p in root.rglob("*"):
            if p.is_file() and u.FAKE_TOKEN in p.read_text(encoding="utf-8", errors="ignore"):
                hits.append(p)
    assert hits == [sandbox.dsh_home / "bots" / "xiaoyu" / "channel" / ".env"], hits


def test_新建_旧系统两个根零字节(client, sandbox, bun, telegram, launchctl, heartbeat,
                            as_darwin):
    """成功路径也一样：旧配置根与旧频道根一个字节都不许产生。"""
    u.write_bot_yml(sandbox.legacy_cfg, "oldbot", display_name="老家伙", dispatcher_port=17899)
    (sandbox.channels / "oldbot").mkdir()
    legacy_before = u.snapshot_tree(sandbox.legacy_cfg)
    channels_before = u.snapshot_tree(sandbox.channels)
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port())
    assert r.status_code == 201, r.get_json()
    assert u.snapshot_tree(sandbox.legacy_cfg) == legacy_before, "旧配置根被写了"
    assert u.snapshot_tree(sandbox.channels) == channels_before, "旧频道根被写了"
    assert not (sandbox.legacy_cfg / "xiaoyu.yml").exists()
    assert not (sandbox.channels / "xiaoyu").exists()


# ------------------------------------------------------------ A 段拒绝面

def test_新建_bot家已被占_409_bot_exists且不落盘(client, sandbox, telegram, bun):
    (sandbox.dsh_home / "bots" / "xiaoyu").mkdir(parents=True)
    r = _post(client, u.free_port())
    assert r.status_code == 409, r.get_json()
    assert r.get_json()["error"] == "bot_exists"
    assert not (sandbox.dsh_cfg / "xiaoyu.yml").exists(), "被拒了还落了盘"


def test_新建_同名配置已存在_409_bot_exists且不覆盖(client, sandbox, telegram, bun):
    cfg = u.write_bot_yml(sandbox.dsh_cfg, "xiaoyu", display_name="先来的")
    before = cfg.read_text(encoding="utf-8")
    r = _post(client, u.free_port())
    assert r.status_code == 409, r.get_json()
    assert r.get_json()["error"] == "bot_exists"
    assert cfg.read_text(encoding="utf-8") == before, "既有配置被改"


@pytest.mark.parametrize("reserved", ["jiwen", "director", "voicecall"])
def test_新建_保留单元名_409_bot_exists且detail点明被新系统占用(
        client, sandbox, telegram, reserved):
    r = _post(client, u.free_port(), bot_id=reserved)
    assert r.status_code == 409, r.get_json()
    body = r.get_json()
    assert body["error"] == "bot_exists"
    assert "新系统" in body["detail"] and "占用" in body["detail"], body


def test_新建_dsh根被关掉_409_dsh_root_off且先于冲突判(
        client, sandbox, telegram, monkeypatch, as_darwin):
    u.write_bot_yml(sandbox.dsh_cfg, "xiaoyu", display_name="先来的")
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", "off")
    r = _post(client, u.free_port())
    assert r.status_code == 409, r.get_json()
    assert r.get_json()["error"] == "dsh_root_off", r.get_json()


def test_新建_端口被本机进程占_409_port_in_use(client, sandbox, telegram):
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(5)
    held = s.getsockname()[1]
    try:
        r = _post(client, held)
        assert r.status_code == 409, r.get_json()
        assert r.get_json()["error"] == "port_in_use", r.get_json()
    finally:
        s.close()


def test_新建_端口撞已配置的bot_409_bot_exists且点名占用者(client, sandbox, telegram):
    u.dsh_bot(sandbox.dsh_cfg, "alpha", dispatcher_port=17811)
    r = _post(client, 17811)
    assert r.status_code == 409, r.get_json()
    body = r.get_json()
    assert body["error"] == "bot_exists", body
    assert "alpha" in json.dumps(body, ensure_ascii=False), body


def test_新建_这个telegram_id已被别的bot用_409_token_in_use(client, sandbox, telegram):
    """token 占用按 getMe 的 bot id（= token 数字段）比对，经旧频道根已有 bot 的 .env。

    alpha 的 .env 数字段与桩回的 id 都是 123456789 —— 同一个 Telegram bot，
    必须拦（报出来的 detail 点 alpha 的名）。
    """
    u.write_bot_yml(sandbox.legacy_cfg, "alpha", display_name="旧一", dispatcher_port=17811)
    envdir = sandbox.channels / "alpha"
    envdir.mkdir(parents=True, exist_ok=True)
    (envdir / ".env").write_text("TELEGRAM_BOT_TOKEN=%s\n" % u.FAKE_TOKEN, encoding="utf-8")
    telegram.bot_id = 123456789                      # = FAKE_TOKEN 的数字段 = alpha 的段
    r = _post(client, u.free_port(), telegram_token=u.FAKE_TOKEN_SAME_HEAD)
    assert r.status_code == 409, r.get_json()
    body = r.get_json()
    assert body["error"] == "token_in_use", body
    assert "alpha" in json.dumps(body, ensure_ascii=False), body


def test_新建_token已被新系统bot用_409_token_in_use(client, sandbox, telegram):
    """替用户想到的：token 的唯一落点已经改成新系统那套位置，那新系统里
    已有 bot 占着的 Telegram id 也必须拦住，不然两只 bot 会撞同一个 token。"""
    u.dsh_bot(sandbox.dsh_cfg, "alpha", dispatcher_port=17811)
    envdir = sandbox.dsh_home / "bots" / "alpha" / "channel"
    envdir.mkdir(parents=True, exist_ok=True)
    (envdir / ".env").write_text("TELEGRAM_BOT_TOKEN=%s\n" % u.FAKE_TOKEN, encoding="utf-8")
    telegram.bot_id = 123456789
    r = _post(client, u.free_port(), telegram_token=u.FAKE_TOKEN_SAME_HEAD)
    assert r.status_code == 409, r.get_json()
    assert r.get_json()["error"] == "token_in_use", r.get_json()


def test_新建_telegram_id没被占就不拦(client, sandbox, bun, telegram, launchctl,
                                heartbeat, as_darwin):
    """反向：alpha 占着 123456789，这次要建的 bot 是 987654321 —— 绝不能被判冲突。"""
    u.write_bot_yml(sandbox.legacy_cfg, "alpha", display_name="旧一", dispatcher_port=17811)
    envdir = sandbox.channels / "alpha"
    envdir.mkdir(parents=True, exist_ok=True)
    (envdir / ".env").write_text("TELEGRAM_BOT_TOKEN=%s\n" % u.FAKE_TOKEN, encoding="utf-8")
    telegram.bot_id = 987654321                      # = FAKE_TOKEN_OTHER_HEAD 的数字段
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port(), telegram_token=u.FAKE_TOKEN_OTHER_HEAD)
    assert r.status_code != 409, "没被占的 telegram id 被判成了冲突：%r" % (r.get_json(),)
    assert (r.get_json().get("error") or "") != "token_in_use", r.get_json()


def test_新建_getMe失败_200_ok_false且未落盘(client, sandbox, telegram, bun, as_darwin):
    telegram.mode = "401"
    r = _post(client, u.free_port())
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["ok"] is False
    assert body["stage"] == "invalid_token", body   # 401 分型（既有口径）
    assert not (sandbox.dsh_cfg / "xiaoyu.yml").exists(), "getMe 失败却落了盘"
    assert not (sandbox.dsh_home / "bots" / "xiaoyu").exists()


def test_新建_形状错先于环境错(client, sandbox, telegram, monkeypatch, as_darwin):
    """bot_id 形状错是用户自己能改的，必须先于环境错（dsh 根关掉）回 400。"""
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", "off")
    r = _post(client, u.free_port(), bot_id="Xiaoyu")
    assert r.status_code == 400, r.get_json()
    assert r.get_json()["error"] != "dsh_root_off"


@pytest.mark.parametrize("field", ["bot_id", "dispatcher_port", "chat_id"])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_新建_必填缺失或空白_400(client, sandbox, telegram, field, value):
    r = _post(client, u.free_port(), **{field: value})
    assert r.status_code == 400, (field, value, r.status_code, r.get_json())


@pytest.mark.parametrize("value", ["", "   "])
def test_新建_token空串或空白_400(client, sandbox, telegram, value):
    r = _post(client, u.free_port(), telegram_token=value)
    assert r.status_code == 400, (value, r.status_code, r.get_json())


def test_新建_token为None_不落盘且不冒500(client, sandbox, telegram):
    """token 形状预检是既有口径：给不出 token 就 200 + ok=false，绝不落盘、不冒 500。"""
    r = _post(client, u.free_port(), telegram_token=None)
    assert r.status_code in (200, 400), (r.status_code, r.get_json())
    if r.status_code == 200:
        body = r.get_json()
        assert body["ok"] is False and body["stage"] == "malformed", body
    assert not (sandbox.dsh_cfg / "xiaoyu.yml").exists()
    assert not (sandbox.dsh_home / "bots" / "xiaoyu").exists()


def test_新建_必填键完全不发_400(client, sandbox, telegram):
    body = _payload(u.free_port())
    del body["bot_id"]
    r = client.post("/hub/api/bots", json=body)
    assert r.status_code == 400, r.get_json()


def test_新建_id超长33字_400(client, sandbox, telegram):
    r = _post(client, u.free_port(), bot_id="a" * 33)
    assert r.status_code == 400, r.get_json()
    assert r.get_json()["error"] == "bad_field"


def test_新建_显示名65字_400(client, sandbox, telegram):
    r = _post(client, u.free_port(), display_name="名" * 65)
    assert r.status_code == 400, r.get_json()
    assert r.get_json()["error"] == "bad_field"


# ------------------------------------------------------------ files 步骤的回滚

def test_新建_files步骤失败_整体回滚到操作前且不碰别人的东西(
        client, sandbox, telegram, bun, monkeypatch, as_darwin):
    telegram.mode = "ok"
    victim = u.write_bot_yml(sandbox.dsh_cfg, "someone", display_name="别人家的")
    keep = sandbox.dsh_home / "bots" / "someone" / "channel"
    keep.mkdir(parents=True)
    (keep / "keepme.txt").write_text("别删我", encoding="utf-8")
    before = u.snapshot_tree(sandbox.dsh_home)
    _boom_moves(monkeypatch)
    r = _post(client, u.free_port())
    assert r.status_code == 500, r.get_json()
    assert r.get_json()["error"] == "internal", r.get_json()
    assert not (sandbox.dsh_cfg / "xiaoyu.yml").exists(), "回滚没删干净配置"
    assert not (sandbox.dsh_home / "bots" / "xiaoyu").exists(), "回滚没删干净 bot 家"
    assert u.snapshot_tree(sandbox.dsh_home) == before, "回滚没回到操作前"
    assert (keep / "keepme.txt").read_text(encoding="utf-8") == "别删我"


# ------------------------------------------------------------ register 步骤

def test_新建_register两条命令_install与install_jobs_带同一份yml(
        client, sandbox, bun, telegram, launchctl, heartbeat, as_darwin):
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port())
    assert r.status_code == 201, r.get_json()
    calls = bun.calls()
    assert len(calls) == 2, calls
    yml = str(sandbox.dsh_cfg / "xiaoyu.yml")
    first, second = calls[0]["argv"], calls[1]["argv"]
    assert first[0].replace("\\", "/").endswith("gateway/scripts/autostart.ts"), first
    assert first[1:] == ["install", yml], first
    assert second[1:] == ["install-jobs", yml], second


def test_新建_register短路_install失败时install_jobs不跑(
        client, sandbox, bun, telegram, launchctl, heartbeat, as_darwin):
    telegram.mode = "ok"
    bun.expect(exit=3, err="install blew up")
    launchctl.set_default(installed=True, running=True, pid=4242)
    heartbeat.tick("xiaoyu")
    r = _post(client, u.free_port())
    assert r.status_code == 207, r.get_json()
    body = r.get_json()
    assert body["error"] == "bot_created_partial"
    assert len(bun.calls()) == 1, bun.calls()
    assert _steps(body)["register"]["detail"] == "install_failed", body["steps"]


def test_新建_install成功但install_jobs失败_register是install_jobs_failed(
        client, sandbox, bun, telegram, launchctl, heartbeat, as_darwin):
    telegram.mode = "ok"
    bun.expect(exit=0).expect(exit=1, err="jobs blew up")
    launchctl.set_default(installed=True, running=True, pid=4242)
    heartbeat.tick("xiaoyu")
    r = _post(client, u.free_port())
    assert r.status_code == 207, r.get_json()
    body = r.get_json()
    assert len(bun.calls()) == 2, bun.calls()
    assert _steps(body)["register"]["detail"] == "install_jobs_failed", body["steps"]


def test_新建_bun解析不到_207且register是bun_missing(
        client, sandbox, telegram, bun, monkeypatch, as_darwin, launchctl, heartbeat):
    telegram.mode = "ok"
    launchctl.set_default(installed=True, running=True, pid=4242)
    heartbeat.tick("xiaoyu")
    monkeypatch.setenv("HUB_BUN", str(sandbox.tmp / "no-such-bun"))
    r = _post(client, u.free_port())
    assert r.status_code == 207, r.get_json()
    body = r.get_json()
    assert _steps(body)["register"]["detail"] == "bun_missing", body["steps"]


# ------------------------------------------------------------ C 段与提示

def test_新建_C段失败_207且B段产物保留_manual_hint不含子进程原文(
        client, sandbox, bun, telegram, launchctl, heartbeat, as_darwin):
    telegram.mode = "ok"
    leak = "BUN-RAW-OUT-9f3a7c"
    bun.expect(exit=3, err=leak)
    launchctl.set_default(installed=True, running=True, pid=4242)
    heartbeat.tick("xiaoyu")
    r = _post(client, u.free_port())
    assert r.status_code == 207, r.get_json()
    body = r.get_json()
    assert body["error"] == "bot_created_partial"
    assert body.get("manual_hint"), "207 必须给用户能自己补的确切命令"
    assert body["restart_hint"] == {"bot": True, "moments_web": False}, body["restart_hint"]
    assert (sandbox.dsh_cfg / "xiaoyu.yml").exists(), "C 段失败不许回滚 B 段产物"
    assert (sandbox.dsh_home / "bots" / "xiaoyu" / "channel").is_dir()
    assert leak not in json.dumps(body, ensure_ascii=False), "子进程原文进了响应"


def test_新建_restart步骤等不到心跳_207且restart是failed(
        client, sandbox, bun, telegram, launchctl, as_darwin, monkeypatch):
    """register 成功但进程没活起来：restart 判活等不到就是 failed，不是 done。"""
    telegram.mode = "ok"
    bun.expect(exit=0).expect(exit=0)
    launchctl.set_default(installed=True, running=True, pid=9999)
    u.tighten_constant(monkeypatch, value=30, new=1.0)
    r = _post(client, u.free_port())
    assert r.status_code == 207, r.get_json()
    body = r.get_json()
    assert _steps(body)["restart"]["state"] == "failed", body["steps"]


def test_新建_非macOS_register与restart都skipped不假等(
        client, sandbox, bun, telegram, as_linux):
    """非 macOS：没有机制会拉起它 —— skipped + manual_hint，不许假装等 30 秒。"""
    telegram.mode = "ok"
    r = _post(client, u.free_port())
    assert r.status_code == 207, r.get_json()
    body = r.get_json()
    steps = _steps(body)
    assert steps["register"]["state"] == "skipped", steps
    assert steps["restart"]["state"] == "skipped", steps
    assert body.get("manual_hint") or steps["restart"].get("manual_hint"), body
    assert bun.calls() == [], "非 macOS 不该调 autostart.ts"


# ------------------------------------------------------------ register 子进程 env

def test_新建_register子进程PATH前置pyenv_shims(
        client, sandbox, bun, telegram, launchctl, heartbeat, as_darwin):
    shims = sandbox.home / ".pyenv" / "shims"
    shims.mkdir(parents=True)
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port())
    assert r.status_code == 201, r.get_json()
    path = bun.calls()[0]["env"]["PATH"]
    assert path.split(os.pathsep)[0] == str(shims), path
    assert os.environ.get("PATH", "") in path, path


def test_新建_shims目录不在时_PATH不含它(
        client, sandbox, bun, telegram, launchctl, heartbeat, as_darwin):
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port())
    assert r.status_code == 201, r.get_json()
    first_seg = bun.calls()[0]["env"]["PATH"].split(os.pathsep)[0]
    assert first_seg != str(sandbox.home / ".pyenv" / "shims"), first_seg


# ------------------------------------------------------------ 边界

@pytest.mark.parametrize("bot_id", ["ab", "a" + "b" * 31])
def test_新建_id长度边界合法(client, sandbox, bun, telegram, launchctl, heartbeat,
                       as_darwin, bot_id):
    _arm_ok(bun, telegram, launchctl, heartbeat, bot=bot_id)
    r = _post(client, u.free_port(), bot_id=bot_id)
    assert r.status_code == 201, (bot_id, r.get_json())
    assert (sandbox.dsh_cfg / ("%s.yml" % bot_id)).exists()


def test_新建_显示名是unicode与emoji_原样落盘(client, sandbox, bun, telegram, launchctl,
                                       heartbeat, as_darwin):
    name = "小雨🌧️ちゃん"
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port(), display_name=name)
    assert r.status_code == 201, r.get_json()
    assert u.read_yaml(sandbox.dsh_cfg / "xiaoyu.yml")["display_name"] == name


def test_新建_重复提交同名_第二次409且第一次产物不变(
        client, sandbox, bun, telegram, launchctl, heartbeat, as_darwin):
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r1 = _post(client, u.free_port())
    assert r1.status_code == 201, r1.get_json()
    cfg = sandbox.dsh_cfg / "xiaoyu.yml"
    before = cfg.read_bytes()
    r2 = _post(client, u.free_port())
    assert r2.status_code == 409, r2.get_json()
    assert r2.get_json()["error"] == "bot_exists"
    assert cfg.read_bytes() == before
