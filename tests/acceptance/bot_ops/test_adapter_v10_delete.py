# -*- coding: utf-8 -*-
"""适配服务 v1.0 验收：删除 POST /adapter/v1/bots/{bot_id}/delete（补充文件 12.4，草案 C）。

依据：INTERFACE-适配服务.md 9.2（「停」）、12.3（要移的那些文件）与 12.4；INTERFACE-适配约定-v1.0-dsh侧.md C。
不读实现。废纸篓全程经测试接缝 DSH_CONSOLE_TRASH_DIR 指到临时目录，绝不碰真的 ~/.Trash。
"""
import json
import os
import re
from pathlib import Path

import pytest

from . import adapter_util as au
from .adapter_util import adapter, console_guard  # noqa: F401  夹具


def mark(path):
    p = Path(path)
    return au.tree_mark(p) if p.is_dir() and not p.is_symlink() else au.file_mark(p)


@pytest.mark.parametrize("bot_id,payload,status,code", [
    ("botalpha", {}, 422, "invalid"),
    ("botalpha", {"confirm": "botbravo"}, 422, "invalid"),
    ("botalpha", {"confirm": "BOTALPHA"}, 422, "invalid"),
    ("botalpha", {"confirm": " botalpha"}, 422, "invalid"),
    ("botalpha", {"confirm": "botalpha", "force": True}, 422, "invalid"),
    ("nobody", {"confirm": "nobody"}, 404, "unknown_bot"),
], ids=["空对象", "写成别的编号", "大小写不同", "多了空格", "多了别的键", "没有这只bot"])
def test_删除_confirm不对422_没有这只404_什么都不做(adapter, monkeypatch, bot_id, payload, status, code):
    trash = au.use_trash(adapter.sandbox, monkeypatch)
    alpha = au.full_bot(adapter, "botalpha")
    before = {str(p): mark(p) for p in au.bot_paths(adapter.sandbox, alpha)}
    adapter.expect_error(adapter.call("POST", au.delete_url(bot_id), payload), status, code)
    assert {k: mark(k) for k in before} == before, "什么都不该动"
    assert list(trash.iterdir()) == []
    assert adapter.launchctl.calls() == [], "不该去停任何 bot"


def test_删除_停掉后移进废纸篓_带manifest_回约定那句话_别的不动(adapter, monkeypatch):
    sb = adapter.sandbox
    trash = au.use_trash(sb, monkeypatch)
    alpha = au.full_bot(adapter, "botalpha")
    bravo = au.full_bot(adapter, "botbravo")
    shared = au.put_file(sb.dsh_home / "stickers" / "wave.gif", b"GIF89a-fake")     # 两边共用的东西：不动
    keep = {str(p): mark(p) for p in au.bot_paths(sb, bravo) + [shared]}
    persona = au.persona_path(alpha).read_bytes()
    originals = au.bot_paths(sb, alpha)

    body = adapter.ok(adapter.call("POST", au.delete_url("botalpha"), {"confirm": "botalpha"}))

    assert body == {"ok": True, "message": au.DELETED_MESSAGE}, body
    for p in originals:
        assert not os.path.lexists(str(p)), "原位置应已移走：%s" % p
    entries = list(trash.iterdir())
    assert len(entries) == 1 and re.match(r"^dsh-bot-botalpha-%s\Z" % au.UTC_STAMP, entries[0].name), \
        "废纸篓里应恰好多一个 dsh-bot-<编号>-<UTC 时间> 目录：%r" % [e.name for e in entries]
    folder = entries[0]
    assert au.find_file(folder, "CLAUDE.md", persona), "人设应整份在废纸篓里，能找回"
    assert au.find_file(folder, "ledger.db", b"fake-ledger-botalpha")
    assert isinstance(json.loads((folder / "manifest.json").read_text(encoding="utf-8")), dict)
    assert {k: mark(k) for k in keep} == keep, "别的 bot、共用的东西不该动"
    rows = adapter.ok(adapter.call("GET", au.BOTS))["bots"]
    assert [r["bot_id"] for r in rows] == ["botbravo"], rows
    assert "bootout" in adapter.launchctl.subcommands(), "删之前应先停掉它"


def test_删除_停不下来_503_什么都不移(adapter, monkeypatch):
    trash = au.use_trash(adapter.sandbox, monkeypatch)
    alpha = au.full_bot(adapter, "botalpha")
    au.stuck_running(adapter, monkeypatch, "botalpha")
    adapter.expect_error(adapter.call("POST", au.delete_url("botalpha"), {"confirm": "botalpha"}),
                         503, "unavailable")
    for p in (alpha.botdir, alpha.yml, alpha.life, au.persona_path(alpha)):
        assert p.exists(), "停不下来时什么都不该移：%s" % p
    assert list(trash.iterdir()) == []


def test_删除_废纸篓目录不存在_直接删掉_不新建废纸篓(adapter, monkeypatch):
    sb = adapter.sandbox
    missing = sb.tmp / "no-trash-here"
    monkeypatch.setenv("DSH_CONSOLE_TRASH_DIR", str(missing))
    alpha = au.full_bot(adapter, "botalpha")
    bravo = adapter.bot("botbravo")
    adapter.ok(adapter.call("POST", au.delete_url("botalpha"), {"confirm": "botalpha"}))
    for p in au.bot_paths(sb, alpha):
        assert not os.path.lexists(str(p)), "应已删掉：%s" % p
    assert not missing.exists(), "废纸篓不存在时直接删，不该替它建目录"
    assert bravo.yml.exists() and bravo.botdir.is_dir()
