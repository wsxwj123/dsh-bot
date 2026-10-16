# -*- coding: utf-8 -*-
"""适配服务 v1.1 验收：删除时直接抹掉的东西（约定 v1.1 5.18.4 逐项清单，决定 C）。

dsh 侧直接抹掉、不进废纸篓的：Telegram 令牌（频道目录的 .env，v1.0 已有）与网关本机接口口令 state/api.key；
其余照旧整份进废纸篓。
"""
import os

from . import adapter_util as au
from .adapter_util import adapter, console_guard  # noqa: F401  夹具


def test_删除_网关口令文件直接抹掉_不进废纸篓_账本照旧进废纸篓(adapter, monkeypatch):
    trash = au.use_trash(adapter.sandbox, monkeypatch)
    alpha = au.full_bot(adapter, "botalpha")
    assert (alpha.botdir / "state" / "api.key").exists()
    adapter.ok(adapter.call("POST", au.delete_url("botalpha"), {"confirm": "botalpha"}))
    assert not os.path.lexists(str(alpha.botdir)), "原位置应已移走"
    assert au.find_file(trash, "ledger.db", b"fake-ledger-botalpha"), "账本应照旧进废纸篓"
    for dirpath, _dirs, files in os.walk(str(trash)):
        assert "api.key" not in files, "state/api.key 应直接抹掉，不进废纸篓：%s" % dirpath
        for name in files:
            with open(os.path.join(dirpath, name), "rb") as f:
                assert au.GW_KEY.encode() not in f.read(), "废纸篓里出现了网关口令：%s" % name


def _no_secret_in(trash, name):
    for dirpath, _dirs, files in os.walk(str(trash)):
        assert name not in files, "%s 应直接抹掉，不进废纸篓：%s" % (name, dirpath)


def test_删除_密钥文件是软链_只删链接本身_指向的文件不碰(adapter, monkeypatch, tmp_path):
    trash = au.use_trash(adapter.sandbox, monkeypatch)
    alpha = au.full_bot(adapter, "botalpha")
    key = alpha.botdir / "state" / "api.key"
    real_key = tmp_path / "real-api.key"
    real_key.write_bytes(key.read_bytes())
    key.unlink()
    key.symlink_to(real_key)
    env = au.persona_path(alpha).parent / ".env"
    real_env = tmp_path / "real.env"
    real_env.write_text("TELEGRAM_BOT_TOKEN=fake\n", encoding="utf-8")
    if os.path.lexists(str(env)):
        env.unlink()
    env.symlink_to(real_env)
    adapter.ok(adapter.call("POST", au.delete_url("botalpha"), {"confirm": "botalpha"}))
    _no_secret_in(trash, "api.key")
    _no_secret_in(trash, ".env")
    assert real_key.exists() and real_env.read_text(encoding="utf-8") == "TELEGRAM_BOT_TOKEN=fake\n"


def test_删除_频道目录写到别处_自己目录里的口令照样抹掉_别处的不碰(adapter, monkeypatch, tmp_path):
    trash = au.use_trash(adapter.sandbox, monkeypatch)
    alpha = au.full_bot(adapter, "botalpha")
    elsewhere = tmp_path / "other-system" / "channel"
    elsewhere.mkdir(parents=True)
    (elsewhere / ".env").write_text("TELEGRAM_BOT_TOKEN=other\n", encoding="utf-8")
    au.set_cfg(alpha, bot_channel_path=str(elsewhere))
    assert (alpha.botdir / "state" / "api.key").exists()
    adapter.ok(adapter.call("POST", au.delete_url("botalpha"), {"confirm": "botalpha"}))
    _no_secret_in(trash, "api.key")
    assert (elsewhere / ".env").read_text(encoding="utf-8") == "TELEGRAM_BOT_TOKEN=other\n"
