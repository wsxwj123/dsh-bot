# -*- coding: utf-8 -*-
"""forced-early-initiate 验收测试 13-15：life-context.py 对 SELF_INITIATE_EARLY=1 的处理。

真跑命令行 `life-context.py bot5 42`（子进程）；判官（LLM）换成 fake-claude 且一律判 skip，
整个子进程断网。不设 CLAUDEBOTLIFE_BYPASS_SILENCE（self_initiate 调 life-context 时没传 env）。
想念值满：state/jiwen/bot5-42.json 里 connection=0.9。
硬规则情境（13、15 共用）：账本里放一条 20 分钟前的用户消息（静默阈值 30 分钟，未到阈值）。
  现状下想念满且静默超过约 15 分钟会直接开口（不过预筛、不问判官）：这个情境不被拦、直接给 TEXT；新行为是带提前标记时被"非静默期"拦下。
编号对应接口文件第 2 节的行为清单。
"""
SILENCE_PREFIX = "非静默期"
RECENT = 20 * 60        # 距用户消息 20 分钟：不到 30 分钟静默阈值
LONG_AGO = 2 * 3600     # 距用户消息 2 小时：过了阈值，硬规则不拦


def test_13_带提前标记_想念满_硬规则会拦_输出SKIP原因是那条规则_判官没被调用(lc):
    lc.put_inbound("在吗", RECENT)
    p, out, judge = lc.life(early_flag=True)
    assert out["action"] == "SKIP", out
    assert out["reason"].startswith(SILENCE_PREFIX), out
    assert judge == 0, "硬规则已拦下，不该问判官"


def test_14_带提前标记_想念满_硬规则不拦_不问判官_直接给TEXT(lc):
    lc.put_inbound("在吗", LONG_AGO)
    p, out, judge = lc.life(early_flag=True)
    assert out["action"] == "TEXT", out          # 判官假件会判 skip：拿到 TEXT 说明没听判官的
    assert out.get("text"), out
    assert judge == 0, "提前开口不该问判官"


def test_15_不带提前标记_想念满_同样情境不被硬规则拦_判官没被调用(lc):
    lc.put_inbound("在吗", RECENT)
    p, out, judge = lc.life(early_flag=False)
    assert out["action"] == "TEXT", "不带提前标记时不该被静默规则拦：%s" % out
    assert out.get("text"), out
    assert judge == 0, "想念满的现状行为不问判官"
