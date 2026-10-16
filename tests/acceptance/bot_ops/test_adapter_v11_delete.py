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
