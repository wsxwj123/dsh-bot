# -*- coding: utf-8 -*-
"""适配服务 v1.0 验收：归档、归档列表、恢复（补充文件 12.3，草案 B）。

依据：INTERFACE-适配服务.md 9.2（「停」）与 12.3；INTERFACE-适配约定-v1.0-dsh侧.md B。不读实现。
归档目录里怎么摆、manifest.json 长什么样，接口只说「保持原来的相对结构」「记下每个文件原来的位置」，
这里只验：原位置都空了、东西整份还在归档目录里、manifest.json 是 JSON 对象且写着每个原位置。
archive_id 合规但不存在回 404 unknown_archive（13.4）。
"""
import json
import os
import re
import time
from pathlib import Path

import pytest

from . import adapter_util as au
from .adapter_util import adapter, console_guard  # noqa: F401  夹具


def archive_root(adapter):
    return adapter.sandbox.dsh_home / "archive"


def mark(path):
    p = Path(path)
    return au.tree_mark(p) if p.is_dir() and not p.is_symlink() else au.file_mark(p)


def do_archive(adapter, bot_id="botalpha"):
    body = adapter.ok(adapter.call("POST", au.archive_url(bot_id), {}))
    assert set(body) == {"ok", "archive_id"}, body
    assert re.match(r"^%s-%s\Z" % (bot_id, au.UTC_STAMP), body["archive_id"]), body
    return body["archive_id"]


def test_归档_停掉后整份移进archive_写manifest_列表里不再有它_别的bot不动(adapter):
    alpha = au.full_bot(adapter, "botalpha")
    bravo = au.full_bot(adapter, "botbravo")
    keep = {str(p): mark(p) for p in au.bot_paths(adapter.sandbox, bravo)}
    persona, life = au.persona_path(alpha).read_bytes(), alpha.life.read_bytes()
    originals = au.bot_paths(adapter.sandbox, alpha)

    archive_id = do_archive(adapter)

    for p in originals:
        assert not os.path.lexists(str(p)), "原位置应已移走：%s" % p
    folder = archive_root(adapter) / archive_id
    assert folder.is_dir(), "应建 <DSH_BOT_HOME>/archive/<archive_id>/"
    assert au.find_file(folder, "CLAUDE.md", persona), "人设应整份在归档目录里"
    assert au.find_file(folder, "ledger.db", b"fake-ledger-botalpha"), "bot 目录里的账本应一起移进归档"
    assert au.find_file(folder, "botalpha.yml", life), "只给它用的生活设定文件应一起移进归档"
    names = [n for _d, _s, files in os.walk(str(folder)) for n in files]
    for label in au.unit_labels("botalpha"):
        assert any(n.startswith(label + ".plist") for n in names), "启动项 %s 的文件应在归档里：%r" % (label, names)
    raw = (folder / "manifest.json").read_text(encoding="utf-8")
    assert isinstance(json.loads(raw), dict), "manifest.json 应是 JSON 对象"
    for p in (alpha.botdir, alpha.yml, alpha.life):
        assert str(p) in raw or os.path.realpath(str(p)) in raw, "manifest 应记下原位置：%s" % p
    rows = adapter.ok(adapter.call("GET", au.BOTS))["bots"]
    assert [r["bot_id"] for r in rows] == ["botbravo"], "归档后 bots 不再列它：%r" % rows
    assert {k: mark(k) for k in keep} == keep, "别的 bot 的东西被动了"
    assert "bootout" in adapter.launchctl.subcommands(), "归档前应先停掉它"


def test_归档_停不下来_503_什么都不移(adapter, monkeypatch):
    alpha = au.full_bot(adapter, "botalpha")
    au.stuck_running(adapter, monkeypatch, "botalpha")
    adapter.expect_error(adapter.call("POST", au.archive_url("botalpha"), {}), 503, "unavailable")
    for p in (alpha.botdir, alpha.yml, alpha.life, au.persona_path(alpha)):
        assert p.exists(), "停不下来时什么都不该移：%s" % p
    root = archive_root(adapter)
    assert not root.exists() or list(root.iterdir()) == []


def test_归档_生活设定文件别的bot也指着它_不移(adapter):
    bravo = au.full_bot(adapter, "botbravo")
    alpha = au.full_bot(adapter, "botalpha", life=str(bravo.life))
    before = au.file_mark(bravo.life)
    do_archive(adapter)
    assert au.file_mark(bravo.life) == before, "共用的生活设定文件应原样留着"
    assert not os.path.lexists(str(alpha.botdir)) and not os.path.lexists(str(alpha.yml))


def test_归档列表_三个键_按时间倒序(adapter):
    au.full_bot(adapter, "botalpha")
    au.full_bot(adapter, "botbravo")
    assert adapter.ok(adapter.call("GET", au.ARCHIVES)) == {"ok": True, "archives": []}
    first = do_archive(adapter, "botalpha")
    time.sleep(1.1)                          # 两份归档的时间差开一秒
    second = do_archive(adapter, "botbravo")
    got = adapter.ok(adapter.call("GET", au.ARCHIVES))["archives"]
    assert [a.get("archive_id") for a in got] == [second, first], "应按时间倒序：%r" % got
    for a in got:
        assert set(a) == {"archive_id", "bot_id", "display_name", "person", "archived_at", "status",
                      "shell_meta"}, a      # 13.4
        assert a["bot_id"] == a["archive_id"].rsplit("-", 1)[0], a
        assert isinstance(a["archived_at"], str) and a["archived_at"].strip(), a


def test_恢复_移回原处_是停用状态_不装载启动项_归档列表里没了(adapter):
    alpha = au.full_bot(adapter, "botalpha")
    persona, life = au.persona_path(alpha).read_bytes(), alpha.life.read_bytes()
    archive_id = do_archive(adapter)
    n_calls = len(adapter.launchctl.calls())

    adapter.ok(adapter.call("POST", au.restore_url(archive_id), {}))

    assert au.persona_path(alpha).read_bytes() == persona and alpha.life.read_bytes() == life
    assert (alpha.botdir / "state" / "ledger.db").read_bytes() == b"fake-ledger-botalpha"
    assert au.read_cfg(alpha).get("enabled") is False, "恢复后应是停用状态"
    for label in au.unit_labels("botalpha"):
        assert au.plist_state(adapter.sandbox, label) == "收起", "恢复后启动项应是收起的：%s" % label
    after = [c.get("cmd") for c in adapter.launchctl.calls()[n_calls:]]
    assert not set(after) & {"bootstrap", "kickstart", "enable", "load"}, "恢复不该装载启动项：%r" % after
    rows = adapter.ok(adapter.call("GET", au.BOTS))["bots"]
    assert [(r["bot_id"], r["state"]) for r in rows] == [("botalpha", "stopped")], rows
    assert adapter.ok(adapter.call("GET", au.ARCHIVES))["archives"] == []


def test_恢复_原处已有同名_409_exists_什么都不移(adapter):
    au.full_bot(adapter, "botalpha")
    archive_id = do_archive(adapter)
    folder = archive_root(adapter) / archive_id
    before = au.tree_mark(folder)
    newer = adapter.bot("botalpha")          # 归档之后又建了一只同名的
    yml_before = newer.yml.read_bytes()
    adapter.expect_error(adapter.call("POST", au.restore_url(archive_id), {}), 409, "exists")
    assert au.tree_mark(folder) == before, "409 时归档里的东西一个都不该动"
    assert newer.yml.read_bytes() == yml_before
    listed = [a["archive_id"] for a in adapter.ok(adapter.call("GET", au.ARCHIVES))["archives"]]
    assert listed == [archive_id], listed


@pytest.mark.parametrize("archive_id,status,code", [
    ("botalpha", 422, "invalid"),
    ("BotAlpha-20261015T080102Z", 422, "invalid"),
    ("botalpha-2026-10-15", 422, "invalid"),
    ("..", 422, "invalid"),
    ("botalpha-20261015T080102Z", 404, "unknown_archive"),
], ids=["没有时间", "编号有大写", "时间写法不对", "两个点", "合规但不存在"])
def test_恢复_archive_id不合规422_不存在404(adapter, archive_id, status, code):
    alpha = adapter.bot("botalpha")
    before = au.file_mark(alpha.yml)
    adapter.expect_error(adapter.call("POST", au.restore_url(archive_id), {}), status, code)
    assert au.file_mark(alpha.yml) == before
