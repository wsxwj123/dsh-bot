# -*- coding: utf-8 -*-
"""适配服务 v0.6/v0.7 验收：meta 十三项能力（补充文件 9.1）与同跑提示（9.4，约定 5.11）。

依据：INTERFACE-适配服务.md 第 9 节；INTERFACE-适配约定-v0.7摘录.md 5.2、5.11、第 6 节。不读实现。
假网关的 GET /v1/health 只放接口文件写明的两个字段：telegram.conflict、telegram.username。
"""
import threading
import time

import pytest

from . import adapter_util as au
from .adapter_util import adapter, console_guard  # noqa: F401  夹具


def bots_by_id(adapter):
    body = adapter.ok(adapter.call("GET", au.BOTS))
    return {b["bot_id"]: b for b in body["bots"]}


def health_hits(gw):
    return [r for r in gw.requests if (r["method"], r["path"]) == ("GET", au.GW_HEALTH)]


def test_meta_能力清单恰好十三个键_新增启停切进群聊三项都为真(adapter):
    caps = adapter.ok(adapter.call("GET", au.META)).get("capabilities")
    assert isinstance(caps, dict), "capabilities 应是对象：%r" % (caps,)
    missing = sorted(set(au.CAPABILITIES_V10) - set(caps))
    extra = sorted(set(caps) - set(au.CAPABILITIES_V10))
    assert not missing and not extra, "能力清单应恰好二十个键：缺 %r，多 %r" % (missing, extra)
    for key, want in au.CAPABILITIES_V10.items():
        assert caps[key] is want, "能力 %s 应为布尔 %r，实际 %r" % (key, want, caps[key])


def test_bots_同跑提示_conflict为真的那只detail恰好是telegram_conflict_别的照旧_停用的不问health(adapter):
    gw_a = au.serve_health(adapter.model_gateway(), conflict=True, username="alpha_fake_bot")
    gw_b = au.serve_health(adapter.model_gateway(), conflict=False, username="bravo_fake_bot")
    gw_c = au.serve_health(adapter.model_gateway(), conflict=True, username="charlie_fake_bot")
    adapter.running_bot("botalpha", gw_a)
    adapter.running_bot("botbravo", gw_b)
    adapter.bot("botcharlie", enabled=False, gw_port=gw_c.port)       # 停用的：网关恰好也在应答
    bots = bots_by_id(adapter)
    assert bots["botalpha"]["state"] == "running", bots["botalpha"]
    assert bots["botalpha"]["detail"] == {"telegram_conflict": True}, \
        "网关报 telegram.conflict 为真，detail 应恰好是 {telegram_conflict: true}：%r" % bots["botalpha"]["detail"]
    assert bots["botbravo"]["detail"] == {}, "没冲突的那只 detail 应照旧是 {}：%r" % bots["botbravo"]["detail"]
    assert bots["botcharlie"]["state"] == "stopped", bots["botcharlie"]
    assert bots["botcharlie"]["detail"] == {}, "停用的 bot 不该带同跑提示：%r" % bots["botcharlie"]["detail"]
    assert health_hits(gw_c) == [], "停用的 bot 不在跑，不该去问它的 /v1/health：%r" % gw_c.requests
    assert len(health_hits(gw_a)) == 1, "在跑的 bot 应恰好问一次 /v1/health：%r" % gw_a.requests


BAD_HEALTH = {"网关没有health接口_回404": "404", "回的不是JSON": "text", "卡住6秒不回": "hang"}


@pytest.mark.parametrize("mode", list(BAD_HEALTH.values()), ids=list(BAD_HEALTH))
def test_bots_同跑提示_health问不到_detail照旧_仍200_每只最多等2秒(adapter, mode):
    release = threading.Event()
    late = {"ok": True, "telegram": {"conflict": True, "username": "late_fake_bot"}}
    gw = adapter.model_gateway()
    if mode == "text":
        gw.on("GET", au.GW_HEALTH, 200, "ok")
    elif mode == "hang":
        gw.on_fn("GET", au.GW_HEALTH, lambda rec: (release.wait(6.0), (200, late))[1])
    adapter.running_bot("botalpha", gw)                  # mode 为 404 时不配应答，假网关一律回 404
    started = time.time()
    try:
        resp = adapter.call("GET", au.BOTS)
        took = time.time() - started
    finally:
        release.set()
    bot = adapter.ok(resp)["bots"][0]
    assert bot["state"] == "running", bot
    assert bot["detail"] == {}, "问不到 /v1/health 不算错，detail 应照旧是 {}：%r" % bot["detail"]
    assert took < 4.5, "/v1/health 每只最多等 2 秒，整个列表却等了 %.1f 秒" % took
