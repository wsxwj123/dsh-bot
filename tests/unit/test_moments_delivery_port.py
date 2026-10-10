# -*- coding: utf-8 -*-
"""投递路径拉起 worker 时连的是哪个端口。

真机形态是同一个 bot 在两套配置根里各有一份。旧栈那份在跑（dispatcher_port 17802，
本机实测在 LISTEN），新栈那份 enabled: false（dispatcher_port 17952，实测拒连）。
全局端口注册表按"新系统优先"合并，按名字查到的是新栈那个 17952，通知写进了正确的
inbox，拉起那一跳却静默失败。

这里用两套 tmp 配置根真读 yml，走完整投递（Flask test_client POST /api/moment，
以及评论回复那条 _trigger_bot_moment_reply），把实际 POST 的地址记下来断言端口。
出站 socket 被拦死，万一 _urlopen 注入点失效也连不到真机上的 17802。
"""
import importlib
import os
import socket
import types
from pathlib import Path
from urllib.parse import urlparse

import pytest

#: 真机形态的两个端口，只出现在被测地址里，连接一律被拦
LEGACY_PORT = 17802
DSH_PORT = 17952


def _write_bot(root, name, **fields):
    """写一份 tmp 的 <name>.yml。字符串值加引号，Windows 路径里的冒号才不会解析歪。"""
    lines = ["id: %s" % name]
    for k, v in sorted(fields.items()):
        if isinstance(v, bool):
            v = "true" if v else "false"
        elif isinstance(v, (str, Path)):
            v = '"%s"' % v
        lines.append("%s: %s" % (k, v))
    (root / ("%s.yml" % name)).write_text("\n".join(lines) + "\n", encoding="utf-8")


class _Recorder:
    """``_urlopen`` 的替身，只记地址不回网络。"""

    def __init__(self):
        self.urls = []

    def __call__(self, req, *a, **k):
        self.urls.append(req if isinstance(req, str) else req.full_url)

        class R:
            def read(self_inner):
                return b'{"ok":true}'

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *x):
                return False
        return R()


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    """两套 tmp 配置根 + 隔离的库与家目录。返回写配置、发通知、取记录的句柄。"""
    legacy = tmp_path / "configs-legacy"
    dsh = tmp_path / "configs-dsh"
    home = tmp_path / "home"
    for d in (legacy, dsh, home / ".claude" / "channels"):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(legacy))
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(dsh))
    monkeypatch.setenv("BOTLIFE_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("HUB_ENV_FILE", str(tmp_path / "hub.env"))
    for k in list(os.environ):
        if k.startswith("DISPATCHER_PORT_"):
            monkeypatch.delenv(k, raising=False)
    for k in ("HUB_ACCESS_PASSWORD", "HUB_ADMIN_PASSWORD"):
        monkeypatch.delenv(k, raising=False)

    # 出站拦死。真机形态的 17802 在生产 bot 手里，任何情况下都不许真连
    def _no_net(sock, addr):
        raise AssertionError("用例不允许真连网络：%r" % (addr,))
    monkeypatch.setattr(socket.socket, "connect", _no_net)

    import db
    importlib.reload(db)
    assert Path(db.DB_PATH).resolve() == (tmp_path / "state.db").resolve(), \
        "朋友圈库没落到 tmp，停手"
    db.init()

    import moments.web as web

    def post(text):
        rec = _Recorder()
        monkeypatch.setattr(web, "_urlopen", rec)
        app = web.app
        app.config["TESTING"] = True
        r = app.test_client().post("/api/moment", json={"text": text})
        assert r.status_code == 200, r.get_data(as_text=True)
        return rec.urls

    return types.SimpleNamespace(legacy=legacy, dsh=dsh, tmp=tmp_path, web=web, post=post)


def _造新旧同名(delivery, *, legacy_port=LEGACY_PORT, dsh_port=DSH_PORT):
    """旧根 bot2 在跑，新根 bot2 停用。返回两条通道目录。"""
    legacy_ch = delivery.tmp / "run" / "legacy-bot2"
    dsh_ch = delivery.tmp / "run" / "dsh-bot2"
    _write_bot(delivery.legacy, "bot2", display_name="李彤彤", dispatcher_port=legacy_port,
               bot_channel_path=legacy_ch, chat_id="77")
    _write_bot(delivery.dsh, "bot2", display_name="李彤彤", dispatcher_port=dsh_port,
               bot_channel_path=dsh_ch, chat_id="77", enabled=False)
    return legacy_ch, dsh_ch


def test_旧栈在跑的bot拉起worker打到旧栈端口(delivery):
    legacy_ch, _dsh_ch = _造新旧同名(delivery)

    urls = delivery.post("今天下雨")
    print("[真验] 投递拉起 worker 实际 POST：%s" % urls)

    # 注册表按名字查到的是新栈那个端口，这条钉住"修前会连错"的事实
    assert delivery.web._bot_port("bot2") == DSH_PORT
    assert [urlparse(u).port for u in urls] == [LEGACY_PORT], urls
    assert all(urlparse(u).path == "/ensure_worker" for u in urls), urls
    # 通知本体照旧写进旧栈那份的 inbox
    assert len(list((legacy_ch / "chats" / "77" / "inbox").glob("user-moment-*.json"))) == 1


def test_新栈在跑的bot拉起worker打到新栈端口(delivery):
    """新栈 bot 的行为不变，端口还是它自己那份配置里的。"""
    dsh_ch = delivery.tmp / "run" / "dsh-bot4"
    _write_bot(delivery.dsh, "bot4", display_name="陈露露", dispatcher_port=DSH_PORT,
               bot_channel_path=dsh_ch, chat_id="77")

    urls = delivery.post("在吗")

    assert [urlparse(u).port for u in urls] == [DSH_PORT], urls


def test_评论回复那条路径也用这份配置的端口(delivery, monkeypatch):
    """_trigger_bot_moment_reply 手里同样是"在跑的那份"配置。"""
    legacy_ch, _dsh_ch = _造新旧同名(delivery)
    cfg = {"_bot_id": "bot2", "id": "bot2", "bot_channel_path": str(legacy_ch),
           "chat_id": "77", "dispatcher_port": LEGACY_PORT}
    rec = _Recorder()
    monkeypatch.setattr(delivery.web, "_urlopen", rec)

    r = delivery.web._trigger_bot_moment_reply(
        cfg, {"id": 1, "text": "下雨了", "visibility": "public", "bot_id": "bot2"},
        "你好", 5, "我")

    assert r and str(r).endswith(".json"), r
    assert [urlparse(u).port for u in rec.urls] == [LEGACY_PORT], rec.urls


def test_env端口覆盖仍然最高优先(delivery, monkeypatch):
    """DISPATCHER_PORT_<BOT> 是运维逃生口，优先级不许被这次改动挤掉。"""
    _造新旧同名(delivery)
    monkeypatch.setenv("DISPATCHER_PORT_BOT2", "18099")

    urls = delivery.post("早")

    assert [urlparse(u).port for u in urls] == [18099], urls
