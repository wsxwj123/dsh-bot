# -*- coding: utf-8 -*-
"""forced-early-initiate 验收测试 1-12、16-18：随机闸没到时，想念值满能否提前调 life-context。

只通过 scripts/self_initiate.py 的公开函数 run_dsh(bot, chat, cfg, now) 调用；
life-context 子进程换成假的（记 env），网关换成本机假服务；随机闸间隔靠固定 random.randint。
编号对应接口文件第 2 节的行为清单。
"""
import json

from fei_support import DAY, HOUR, MIN, T0, set_connection

FULL = 0.9
NOT_FULL = 0.49
TEXT = {"action": "TEXT", "text": "想你了"}
SKIP = {"action": "SKIP", "reason": "刚说过相同话题"}
GATE_MSG = "还没到下次机会"


def assert_early_call(r):
    assert len(r.calls) == 1, "应调 life-context 1 次，实际 %d 次" % len(r.calls)
    assert r.calls[0].get("SELF_INITIATE_EARLY") == "1"


def assert_no_call(r):
    assert r.calls == [], "不该调 life-context，实际调了 %d 次" % len(r.calls)
    assert r.injects == []
    assert GATE_MSG in r.out, "输出应含 %r，实际：%r" % (GATE_MSG, r.out)


def assert_normal_call(r):
    assert len(r.calls) == 1, "应调 life-context 1 次，实际 %d 次" % len(r.calls)
    assert "SELF_INITIATE_EARLY" not in r.calls[0]


def test_01_闸未到_想念满_从没投递过_提前调life_context并投递一次(early):
    early.pending_gate_without_delivery(T0)
    set_connection(early.root, FULL)
    r = early.run(T0 + 30 * MIN, TEXT)
    assert r.rc == 0
    assert_early_call(r)
    assert len(r.injects) == 1
    path, body = r.injects[0]
    assert path == "/v1/inject" and body["source"] == "self_initiate"
    assert "想你了" in body["text"]


def test_02_闸未到_想念不满_不调life_context_不投递(early):
    early.pending_gate_without_delivery(T0)
    set_connection(early.root, NOT_FULL)
    r = early.run(T0 + 30 * MIN, TEXT)
    assert r.rc == 0
    assert_no_call(r)


def test_03_闸未到_没有积温状态文件_不调life_context_不投递(early):
    early.pending_gate_without_delivery(T0)
    set_connection(early.root, None)
    r = early.run(T0 + 30 * MIN, TEXT)
    assert r.rc == 0
    assert_no_call(r)


def test_04_上次成功投递在2小时59分前_不调life_context(early):
    early.normal_delivery(T0)
    set_connection(early.root, FULL)
    r = early.run(T0 + 2 * HOUR + 59 * MIN, TEXT)
    assert r.rc == 0
    assert_no_call(r)


def test_05_上次成功投递在3小时1分前_提前调(early):
    early.normal_delivery(T0)
    set_connection(early.root, FULL)
    r = early.run(T0 + 3 * HOUR + 1 * MIN, TEXT)
    assert r.rc == 0
    assert_early_call(r)
    assert len(r.injects) == 1


def test_06_同一天提前成功2次后第3次不调_第二天又可以调(early):
    early.normal_delivery(T0)                                   # 06:00 正常投递，闸到次日 06:00
    set_connection(early.root, FULL)
    r1 = early.run(T0 + 3 * HOUR + 30 * MIN, TEXT)              # 09:30 提前 #1
    assert_early_call(r1)
    assert len(r1.injects) == 1
    r2 = early.run(T0 + 6 * HOUR + 40 * MIN, TEXT)              # 12:40 提前 #2
    assert_early_call(r2)
    assert len(r2.injects) == 1
    r3 = early.run(T0 + 10 * HOUR, TEXT)                        # 16:00 同一天第 3 次：名额用完
    assert_no_call(r3)
    r4 = early.run(T0 + DAY - 1 * HOUR, TEXT)                   # 次日 05:00，闸（次日 06:00）未到，换了本地日期
    assert_early_call(r4)
    assert len(r4.injects) == 1


def test_07_当天2次正常投递_不占提前名额_仍可提前(early):
    early.normal_delivery(T0, interval=30 * MIN)                # 06:00 正常投递 #1，闸 06:30
    early.normal_delivery(T0 + 31 * MIN, interval=DAY)          # 06:31 正常投递 #2，闸到次日 06:31
    set_connection(early.root, FULL)
    r = early.run(T0 + 31 * MIN + 3 * HOUR + 1 * MIN, TEXT)     # 09:32，同一天
    assert r.rc == 0
    assert_early_call(r)
    assert len(r.injects) == 1


def test_08_提前调回SKIP_不投递_29分钟内不再调_31分钟后再调(early):
    early.normal_delivery(T0)
    set_connection(early.root, FULL)
    t1 = T0 + 4 * HOUR
    r1 = early.run(t1, SKIP)
    assert r1.rc == 0
    assert_early_call(r1)
    assert r1.injects == [], "life-context 回 SKIP 不该投递"
    r2 = early.run(t1 + 29 * MIN, TEXT)
    assert_no_call(r2)
    r3 = early.run(t1 + 31 * MIN, TEXT)
    assert_early_call(r3)                                       # 带提前标记 = 闸没被 SKIP 改到前面
    assert len(r3.injects) == 1


def test_09_提前回SKIP后_随机闸原定时刻不变_到点走正常流程不带提前标记(early):
    early.normal_delivery(T0)                                   # 闸 = T0 + 24h
    set_connection(early.root, FULL)
    r1 = early.run(T0 + 4 * HOUR, SKIP)
    assert_early_call(r1)
    r2 = early.run(T0 + DAY + 1, TEXT)                          # 原定时刻之后
    assert r2.rc == 0
    assert_normal_call(r2)
    assert len(r2.injects) == 1


def test_10_提前投递成功后_随机闸原定时刻不变_到点走正常流程不带提前标记(early):
    early.normal_delivery(T0)                                   # 闸 = T0 + 24h
    set_connection(early.root, FULL)
    r1 = early.run(T0 + 4 * HOUR, TEXT)
    assert_early_call(r1)
    assert len(r1.injects) == 1
    r2 = early.run(T0 + DAY + 1, TEXT)                          # 原定时刻之后（若提前投递重抽了闸，这里还没到点）
    assert r2.rc == 0
    assert_normal_call(r2)
    assert len(r2.injects) == 1


def test_11_功能开关关着_想念满_闸未到_不调_不投递(early):
    early.normal_delivery(T0)
    set_connection(early.root, FULL)
    early.cfg["features"] = {"proactive": False}
    r = early.run(T0 + 4 * HOUR, TEXT)
    assert r.rc == 0
    assert r.calls == []
    assert r.injects == []


def test_12_闸已到点_想念满_走正常流程不带提前标记(early):
    early.normal_delivery(T0)
    set_connection(early.root, FULL)
    r = early.run(T0 + DAY + 1, TEXT)
    assert r.rc == 0
    assert_normal_call(r)
    assert len(r.injects) == 1



def test_16_提前调回FALLBACK_不投递_29分钟内不再调_31分钟后再调(early):
    early.normal_delivery(T0)
    set_connection(early.root, FULL)
    t1 = T0 + 4 * HOUR
    r1 = early.run(t1, {"action": "FALLBACK"})
    assert r1.rc == 0
    assert_early_call(r1)
    assert r1.injects == [], "提前那次 life-context 回 FALLBACK 不该投递"
    r2 = early.run(t1 + 29 * MIN, TEXT)
    assert_no_call(r2)
    r3 = early.run(t1 + 31 * MIN, TEXT)
    assert_early_call(r3)
    assert len(r3.injects) == 1


def test_17_提前调退出码为1_不投递_随机闸原定时刻不变_到点走正常流程(early):
    early.normal_delivery(T0)                                   # 闸 = T0 + 24h
    set_connection(early.root, FULL)

    class Failed:
        returncode = 1
        stdout = json.dumps(TEXT, ensure_ascii=False)           # 即使 stdout 有 TEXT，退出码非 0 也不能投递

    def fake_run(*a, **k):
        env = k.get("env")
        early.calls.append(dict(env) if env is not None else {})
        return Failed()

    early.monkeypatch.setattr(early.si.subprocess, "run", fake_run)
    before = len(early.calls), len(early.got)
    early.capsys.readouterr()
    rc = early.si.run_dsh("bot5", "42", early.cfg, T0 + 4 * HOUR)
    early.capsys.readouterr()
    new_calls, new_injects = early.calls[before[0]:], early.got[before[1]:]
    assert rc == 0
    assert len(new_calls) == 1 and new_calls[0].get("SELF_INITIATE_EARLY") == "1", "应提前调 1 次，实际 %r" % new_calls
    assert new_injects == [], "life-context 退出码为 1 不该投递"
    r2 = early.run(T0 + DAY + 1, TEXT)                          # 原定时刻之后
    assert r2.rc == 0
    assert_normal_call(r2)
    assert len(r2.injects) == 1


def test_18_睡眠时段内不提前_移出睡眠时段才提前(early):
    sleep = ["00:00-07:30"]
    early.normal_delivery(T0, interval=2 * DAY)                 # 06:00 正常投递，闸到后天 06:00
    set_connection(early.root, FULL)
    yml = early.root.parent.parent / "configs" / "bot5.yml"
    yml.write_text(yml.read_text(encoding="utf-8") + 'sleep_hours: ["00:00-07:30"]\n', encoding="utf-8")
    early.cfg["sleep_hours"] = sleep
    r1 = early.run(T0 + DAY + 30 * MIN, TEXT)                   # 次日 06:30，在睡眠时段内，距上次投递 >3h
    assert r1.rc == 0
    n_calls = len(r1.calls)
    assert n_calls == 0, "睡眠时段内不该调 life-context，实际调了 %d 次" % n_calls
    assert r1.injects == []
    r2 = early.run(T0 + DAY + 2 * HOUR, TEXT)                   # 次日 08:00，移出睡眠时段
    assert r2.rc == 0
    assert_early_call(r2)
    assert len(r2.injects) == 1
