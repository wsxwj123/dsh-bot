# -*- coding: utf-8 -*-
"""适配服务 v0.4 验收：换供应商整条请求封顶（约定第 3 节、5.4；补充文件 6.1、第 8 节）。

时限用测试接缝 DSH_CONSOLE_PROVIDER_BUDGET 缩短。假网关的某一种请求故意"卡住不回"，
卡住的处理线程最多等 HANG_MAX 秒，用例结束时一律放行。不读实现。
"""
import threading
import time

import pytest

from . import adapter_util as au
from .adapter_util import adapter, console_guard  # noqa: F401  夹具

HANG_MAX = 12.0
MARGIN = 1.5            # 允许超出时限的余量（秒）
UNCONFIRMED = "已发给这只 bot，还没确认生效，请稍后刷新确认"     # 补充文件 6.1


@pytest.fixture
def release():
    """卡住的假网关处理线程靠它放行；用例结束必放行。"""
    ev = threading.Event()
    yield ev
    ev.set()


def stall(gw, method, path, release, delay=None):
    """让假网关的"方法 + 路径"先卡住（或先慢 delay 秒），再照原来的应答回。"""
    orig = gw.routes[(method, path)]

    def fn(rec):
        if delay is None:
            release.wait(HANG_MAX)
        else:
            time.sleep(delay)
        return orig(rec)

    gw.on_fn(method, path, fn)


def timed_post(adapter, payload):
    started = time.time()
    resp = adapter.call("POST", au.provider_url("botalpha"), payload)
    return resp, time.time() - started


WRITE_HANGS = {
    "同一家换模型_写请求卡住": ({"provider_id": au.CFG_PROVIDER, "model": au.VENDOR_A_MODELS[1]}, au.GW_MODEL),
    "只改思考强度_写请求卡住": ({"effort": "high"}, au.GW_EFFORT),
}


@pytest.mark.parametrize("payload,write_path", list(WRITE_HANGS.values()), ids=list(WRITE_HANGS))
def test_换供应商_写请求已发出但网关不回_时限内回200_applied_文案说还没确认(
        adapter, monkeypatch, release, payload, write_path):
    budget = 2.0
    monkeypatch.setenv("DSH_CONSOLE_PROVIDER_BUDGET", "2")
    gw = adapter.model_gateway()
    adapter.running_bot("botalpha", gw)
    stall(gw, "POST", write_path, release)
    resp, took = timed_post(adapter, payload)
    assert [(w["method"], w["path"]) for w in gw.writes()] == [("POST", write_path)], \
        "这条用例要的是写请求已经发出：%r" % gw.requests
    body = adapter.ok(resp)
    assert took < budget + MARGIN, "时限 %.0f 秒，却等了 %.1f 秒才返回" % (budget, took)
    assert body.get("applied") is True and body.get("queued") is False, body
    assert body.get("new_segment") is False, "同一家里换模型或只改强度，new_segment 应为 false：%r" % body
    assert body.get("message") == UNCONFIRMED, "文案应说明还没确认生效：%r" % body.get("message")


def test_换供应商_读清单卡住_写请求还没发出就到时限_回503_unavailable_之后也不补写(
        adapter, monkeypatch, release):
    budget = 2.0
    monkeypatch.setenv("DSH_CONSOLE_PROVIDER_BUDGET", "2")
    gw = adapter.model_gateway()
    adapter.running_bot("botalpha", gw)
    stall(gw, "GET", au.GW_MODEL, release)
    resp, took = timed_post(adapter, {"provider_id": au.VENDOR_B, "model": au.VENDOR_B_MODELS[0]})
    adapter.expect_error(resp, 503, "unavailable")
    assert took < budget + MARGIN, "时限 %.0f 秒，却等了 %.1f 秒才返回" % (budget, took)
    release.set()                     # 放行卡住的读，看适配服务会不会在背后接着写
    time.sleep(1.5)
    assert gw.writes() == [], "回了 503（什么都没写），网关却收到了写请求：%r" % gw.writes()


def test_换供应商_时限管整条请求_读清单慢加写请求卡住_总耗时不超时限(
        adapter, monkeypatch, release):
    """读清单花掉一半时限、写请求再卡住：按"每次请求各给一个完整时限"去做会超出上界。"""
    budget = 5.0
    monkeypatch.setenv("DSH_CONSOLE_PROVIDER_BUDGET", "5")
    gw = adapter.model_gateway()
    adapter.running_bot("botalpha", gw)
    stall(gw, "GET", au.GW_MODEL, release, delay=2.5)
    stall(gw, "POST", au.GW_MODEL, release)
    resp, took = timed_post(adapter, {"provider_id": au.CFG_PROVIDER, "model": au.VENDOR_A_MODELS[2]})
    assert took < budget + MARGIN, "整条请求时限 %.0f 秒，却等了 %.1f 秒才返回" % (budget, took)
    body = au.body_of(resp)
    if gw.writes():                   # 写请求发出去了：只能回"已保存、还没确认"
        assert resp.status_code == 200 and body.get("ok") is True, body
        assert body.get("applied") is True and body.get("message") == UNCONFIRMED, body
    else:                             # 没发出去：只能回 503，什么都没写
        adapter.expect_error(resp, 503, "unavailable")
