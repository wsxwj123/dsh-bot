# -*- coding: utf-8 -*-
"""适配服务 v1.0 补充验收：归档的 shell_meta 与列表字段、恢复的回包（13.4），删除先抹 .env（13.5），
令牌转交（13.6），停着的 bot 换供应商（13.7），能力清单二十一个键（13.8）。

依据：INTERFACE-适配服务.md 4.4、12.3、12.4 与第 13 节。不读实现。
已交记录文件的形状接口没写死：「只留最近 200 条」那条先真交一次，照它的样子补满 200 条再交一次。
"""
import json
import logging
import os
from pathlib import Path

import pytest

from . import adapter_util as au
from .adapter_util import adapter, console_guard  # noqa: F401  夹具

TOKEN_TAIL = au.TG_TOKEN.split(":")[1]
GOOD = {"op_id": au.op_id("switch"), "to": "claude", "purpose": "switch"}
LIST_KEYS = {"archive_id", "bot_id", "display_name", "person", "archived_at", "status", "shell_meta"}


def archives_by_id(adapter):
    return {a["archive_id"]: a for a in adapter.ok(adapter.call("GET", au.ARCHIVES))["archives"]}


def handoff(adapter, bot_id, **over):
    return adapter.call("POST", au.handoff_url(bot_id), dict(GOOD, **over))


def test_meta_能力清单恰好二十二个键_token_handoff为真(adapter):
    caps = adapter.ok(adapter.call("GET", au.META)).get("capabilities")
    assert caps == au.CAPABILITIES_V11, "能力清单应恰好二十二个键（13.8，v1.1 加 bot.groups_discovered）：%r" % (caps,)


def test_归档_shell_meta原样进manifest_列表七个键_少了文件为incomplete(adapter):
    sb = adapter.sandbox
    alpha = au.full_bot(adapter, "botalpha")
    au.set_cfg(alpha, display_name="阿尔法", life_id="lucy")
    meta = {"from": "shell", "n": 3, "nested": {"名字": ["a", None, True]}}
    aid = adapter.ok(adapter.call("POST", au.archive_url("botalpha"), {"shell_meta": meta}))["archive_id"]
    au.full_bot(adapter, "botbravo")
    bid = adapter.ok(adapter.call("POST", au.archive_url("botbravo"), {}))["archive_id"]
    folder = sb.dsh_home / "archive" / aid
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert manifest.get("shell_meta") == meta, "shell_meta 应原样存进 manifest：%r" % manifest

    rows = archives_by_id(adapter)
    a, b = rows[aid], rows[bid]
    assert set(a) == LIST_KEYS and set(b) == LIST_KEYS, rows
    assert (a["bot_id"], a["display_name"], a["person"], a["status"], a["shell_meta"]) == \
        ("botalpha", "阿尔法", "lucy", "complete", meta), a
    assert (b["display_name"], b["person"], b["status"], b["shell_meta"]) == \
        ("botbravo", "botbravo", "complete", None), b

    victim = next(folder.rglob("com.dsh-bot.botalpha.plist*"))     # manifest 里列着的一样东西没了
    victim.unlink()
    rows = archives_by_id(adapter)
    assert (rows[aid]["status"], rows[bid]["status"]) == ("incomplete", "complete"), rows


@pytest.mark.parametrize("body", [
    {"shell_meta": [1, 2]},
    {"shell_meta": "text"},
    {"shell_meta": {"blob": "x" * (64 * 1024)}},
    {"shell_meta": {}, "extra": 1},
], ids=["是数组", "是字符串", "超过64KiB", "多了别的键"])
def test_归档_shell_meta不合规_422_什么都不移(adapter, body):
    sb = adapter.sandbox
    alpha = au.full_bot(adapter, "botalpha")
    before = [au.file_mark(alpha.yml), au.tree_mark(alpha.botdir), au.tree_mark(sb.agents)]
    adapter.expect_error(adapter.call("POST", au.archive_url("botalpha"), body), 422, "invalid")
    assert [au.file_mark(alpha.yml), au.tree_mark(alpha.botdir), au.tree_mark(sb.agents)] == before
    archive = sb.dsh_home / "archive"
    assert not archive.exists() or list(archive.iterdir()) == []
    assert adapter.launchctl.actions() == [], "422 时不该去停它"


def test_恢复_成功回bot_id_enabled假_running假(adapter):
    au.full_bot(adapter, "botalpha")
    aid = adapter.ok(adapter.call("POST", au.archive_url("botalpha"), {}))["archive_id"]
    got = adapter.ok(adapter.call("POST", au.restore_url(aid), {}))
    assert got == {"ok": True, "bot_id": "botalpha", "enabled": False, "running": False}, got


def test_删除_先删env_令牌不进废纸篓_其余照旧进废纸篓(adapter, monkeypatch):
    trash = au.use_trash(adapter.sandbox, monkeypatch)
    alpha = au.full_bot(adapter, "botalpha")
    persona = au.persona_path(alpha).read_bytes()
    adapter.ok(adapter.call("POST", au.delete_url("botalpha"), {"confirm": "botalpha"}))
    assert au.find_file(trash, "CLAUDE.md", persona), "人设应照旧进废纸篓"
    assert not os.path.lexists(str(alpha.botdir)), "原位置应已移走"
    for dirpath, _dirs, files in os.walk(str(trash)):
        assert ".env" not in files, "频道目录的 .env 应直接删掉，不进废纸篓：%s" % dirpath
        for name in files:
            assert TOKEN_TAIL.encode() not in (Path(dirpath) / name).read_bytes(), \
                "废纸篓里出现了 Telegram 令牌：%s" % name


def test_令牌转交_成功回令牌_no_store_记录与日志里没有令牌(adapter, caplog):
    caplog.set_level(logging.DEBUG)
    adapter.bot("botalpha")                 # 在跑的也能交（13.6 不要求先停用）
    resp = handoff(adapter, "botalpha")
    got = adapter.ok(resp)
    assert got == {"ok": True, "telegram_token": au.TG_TOKEN}, "成功应恰好回 ok 与令牌"
    assert "no-store" in (resp.headers.get("Cache-Control") or ""), resp.headers
    raw = au.handoff_records(adapter.sandbox).read_text(encoding="utf-8")
    assert "botalpha" in raw and GOOD["op_id"] in raw, "记录应有 bot_id 与 op_id：%r" % raw
    assert TOKEN_TAIL not in raw, "记录里不该有令牌"
    assert TOKEN_TAIL not in caplog.text, "日志里不该有令牌"


BAD = [
    ("缺purpose", {"op_id": GOOD["op_id"], "to": "claude"}),
    ("to不是claude", dict(GOOD, to="dsh")),
    ("purpose不是switch", dict(GOOD, purpose="new")),
    ("op_id种类不是switch", dict(GOOD, op_id=au.op_id("create"))),
    ("op_id格式不对", dict(GOOD, op_id="20261015-switch-1a2b3c4d")),
    ("op_id为null", dict(GOOD, op_id=None)),
    ("多了别的键", dict(GOOD, force=True)),
]


@pytest.mark.parametrize("payload", [b for _n, b in BAD], ids=[n for n, _b in BAD])
def test_令牌转交_请求体不合规_422_不交令牌_不记(adapter, payload):
    adapter.bot("botalpha")
    resp = adapter.call("POST", au.handoff_url("botalpha"), payload)
    adapter.expect_error(resp, 422, "invalid")
    assert TOKEN_TAIL not in au.response_text(resp)
    assert not au.handoff_records(adapter.sandbox).exists(), "422 时不该记"


def test_令牌转交_同一op_id再来409_同一bot十分钟内429_重建应用后照样生效_没有这只404(adapter):
    adapter.bot("botalpha")
    adapter.bot("botbravo")
    adapter.ok(handoff(adapter, "botalpha"))

    def refused():
        again = handoff(adapter, "botalpha")
        adapter.expect_error(again, 409, "already_handed")
        other = handoff(adapter, "botalpha", op_id=au.op_id("switch", "0000ffff"))
        adapter.expect_error(other, 429, "too_many")
        for resp in (again, other):
            assert TOKEN_TAIL not in au.response_text(resp), "拒绝时不该带令牌"

    refused()
    adapter._client = None                  # 换一个新建的应用：记录要从文件里读回来
    refused()
    adapter.expect_error(handoff(adapter, "nobody", op_id=au.op_id("switch", "abcdef01")), 404, "unknown_bot")
    got = adapter.ok(handoff(adapter, "botbravo", op_id=au.op_id("switch", "bbbbbbbb")))
    assert got["telegram_token"] == au.TG_TOKEN


def test_令牌转交_记录只留最近200条(adapter):
    adapter.bot("botalpha")
    adapter.bot("botbravo")
    adapter.ok(handoff(adapter, "botalpha", op_id=au.op_id("switch", "00000001")))
    path = au.handoff_records(adapter.sandbox)
    data = json.loads(path.read_text(encoding="utf-8"))
    records = data if isinstance(data, list) else next(v for v in data.values() if isinstance(v, list))
    assert len(records) == 1, data
    first = records[0]
    records.extend(dict(first, bot_id="ghost%03d" % i, op_id=au.op_id("switch", "%08x" % (0x100 + i)))
                   for i in range(199))
    path.write_text(json.dumps(data), encoding="utf-8")
    newest = au.op_id("switch", "0000beef")
    adapter.ok(handoff(adapter, "botbravo", op_id=newest))
    data = json.loads(path.read_text(encoding="utf-8"))
    records = data if isinstance(data, list) else next(v for v in data.values() if isinstance(v, list))
    assert len(records) == 200, "记录应只留最近 200 条，实际 %d 条" % len(records)
    assert newest in json.dumps(data), "最新的一条应在"
    assert TOKEN_TAIL not in json.dumps(data)


@pytest.mark.parametrize("payload,want", [
    ({"provider_id": au.VENDOR_B, "model": au.VENDOR_B_MODELS[1]},
     {"provider": au.VENDOR_B, "model": au.VENDOR_B_MODELS[1], "reasoning_effort": None}),
    ({"effort": "high"}, {"provider": au.CFG_PROVIDER, "model": au.CFG_MODEL, "reasoning_effort": "high"}),
    ({"provider_id": au.VENDOR_A, "model": au.VENDOR_A_MODELS[2], "effort": "off"},
     {"provider": au.VENDOR_A, "model": au.VENDOR_A_MODELS[2], "reasoning_effort": "off"}),
], ids=["换供应商与模型", "只改强度", "三样一起"])
def test_停用的bot换供应商_写进配置_回queued_不启动_不碰它的网关(adapter, payload, want):
    gw_live, gw_stopped = adapter.model_gateway(), adapter.model_gateway()
    adapter.running_bot("botalpha", gw_live)
    bot = adapter.bot("botbravo", enabled=False, gw_port=gw_stopped.port)
    got = adapter.ok(adapter.call("POST", au.provider_url("botbravo"), payload))
    assert got == {"ok": True, "applied": False, "queued": True, "new_segment": False,
                   "message": au.QUEUED_MESSAGE}, got
    cfg = au.read_cfg(bot)
    assert {k: (cfg.get("brain") or {}).get(k) for k in want} == want, cfg
    chosen = (cfg.get("brain") or {}).get("chosen_at")
    assert isinstance(chosen, str) and chosen, "每次替停着的 bot 写 brain 都要带 chosen_at（网关凭它清掉聊天里临时切的）：%r" % cfg
    assert cfg.get("enabled") is False and cfg.get("id") == "botbravo", cfg
    assert gw_stopped.requests == [], "不该去找停着的 bot 自己的网关"
    assert gw_live.writes() == [], "不该往任何网关写"
    assert adapter.launchctl.actions() == [], "不该启动它"
    row = au.rows_by_id(adapter)["botbravo"]["provider"]
    assert (row["id"], row["model"], row["effort"]) == (want["provider"], want["model"], want["reasoning_effort"]), row


@pytest.mark.parametrize("live,payload,status,code", [
    (False, {"provider_id": au.VENDOR_B, "model": au.VENDOR_B_MODELS[0]}, 503, "unavailable"),
    (True, {"provider_id": "no-such-vendor", "model": "m"}, 404, "unknown_provider"),
    (True, {"provider_id": au.VENDOR_B, "model": "no-such-model"}, 422, "invalid"),
], ids=["没有在跑的bot核对不了", "不在清单里", "这家没有这个模型"])
def test_停用的bot换供应商_核对没过_什么都不写(adapter, live, payload, status, code):
    if live:
        adapter.running_bot("botalpha", adapter.model_gateway())
    bot = adapter.bot("botbravo", enabled=False)
    before = au.file_mark(bot.yml)
    adapter.expect_error(adapter.call("POST", au.provider_url("botbravo"), payload), status, code)
    assert au.file_mark(bot.yml) == before, "核对没过时配置不该动"
    assert adapter.launchctl.actions() == []
