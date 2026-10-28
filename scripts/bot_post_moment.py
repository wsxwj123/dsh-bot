#!/usr/bin/env python3
"""bot 在 telegram 私聊里被用户要求"发个朋友圈"时调用本脚本。

bot CLAUDE.md 加规则即可：用户说"发个朋友圈/晒一下/分享个 X"时，
调 Bash: python3 ~/claudebotlife/scripts/bot_post_moment.py <bot_id> [topic_hint] [visibility]

参数：
    bot_id        必填，chenlulu
    topic_hint    可选，主题提示（"刚做的菜"、"下午的咖啡"），传给 LLM 引导
    visibility    可选，public/private，默认 LLM 自决

返回：朋友圈 id + 文案
"""
import dataclasses
import sys
import os
from datetime import datetime
from collections import namedtuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config_loader
import db
from generators import situation as sit_mod
from generators import world as world_mod
from generators import wildcard as wc_mod
from generators.mood import mood_at
from moments.post import maybe_post_moment


SitNS = namedtuple("Situation", ["recurring", "sporadic", "hobby"])


USAGE = """用法：bot_post_moment.py <bot_id> [topic_hint] [visibility]

参数：
  bot_id          要发圈的 bot 标识（不是选项）
  topic_hint      可选：这条圈想说什么（不传会自动找素材）
  visibility      可选：public / private

说明：在聊天里被要求「发条朋友圈」时由 moments 工具调用。这条路径会绕过
静默阈值、每日上限、睡觉/上课和素材判定——是"对方让我发"，不是"程序自己决定发"。
"""


def main():
    if len(sys.argv) >= 2 and sys.argv[1] in ("-h", "--help", "help"):
        print(USAGE); sys.exit(0)
    if len(sys.argv) < 2:
        print(USAGE, file=sys.stderr); sys.exit(2)
    bot_id = sys.argv[1]
    topic_hint = sys.argv[2] if len(sys.argv) > 2 else None
    visibility_hint = sys.argv[3] if len(sys.argv) > 3 else None

    cfg = config_loader.load_bot(bot_id); cfg["_bot_id"] = bot_id
    g = config_loader.load_global()
    now = datetime.now()

    sit = sit_mod.collect(bot_id, now, cfg)
    world = world_mod.collect_world(cfg, now)
    wildcard = wc_mod.pick_today_wildcard(bot_id, now)
    mood, factors = mood_at(now, bot_id, sit, world, cfg)

    # 用户主动要求 → 强行注入 hobby。没传 topic 也要注入：素材判定会给"no material"挡掉，
    # 而这条路径本来就是"对方在聊天里让我发一条"，不该再要求程序自己找到发圈理由
    if not sit.hobby:
        name = f"想分享：{topic_hint}" if topic_hint else "想发条朋友圈"
        sit = SitNS(sit.recurring, sit.sporadic,
                    {"name": name, "effect": topic_hint or "", "kind": "obsession"})

    # 用户正在聊天里让她发圈：人显然醒着、也不在上课，作息表上的"睡觉/上课"不拦（真机 M4：半夜聊着天要发圈，被"睡觉"挡住）
    if sit.recurring is not None and sit.recurring.state in ("sleeping", "busy_class"):
        sit = SitNS(dataclasses.replace(sit.recurring, state="free"), sit.sporadic, sit.hobby)

    # 关掉 silence/limit（用户主动要求，应优先于自动节流）
    g_force = {**g, "moments": {**(g.get("moments") or {}), "silence_threshold_minutes": 0}}
    cfg_force = {**cfg, "moments_daily_limit": 999}

    why = {}
    mid = maybe_post_moment(bot_id, cfg_force, now, sit, world, wildcard,
                            mood, factors, 999, g_force, force=True, out=why)
    if not mid:
        # 2026-10-28：把真因照实写出来。原来笼统一句「LLM 返回空 / 触发独白闸 / 无素材」
        # 会把上游给的具体原因（daily_limit(N) / same_kind(X) within 4h 等）盖掉，排查被误导过。
        reason = (why or {}).get("skip") or ""
        print(f"FAIL 没生成：{reason}" if reason else "FAIL 没生成（原因未记录，见 stderr 上游日志）",
              file=sys.stderr)
        sys.exit(3)

    m = db.get_moment(mid)
    # visibility 校正（用户明确要 private 但 LLM 出了 public，强改）
    if visibility_hint in ("public", "private") and m["visibility"] != visibility_hint:
        import sqlite3
        with sqlite3.connect(db.DB_PATH) as c:
            c.execute("UPDATE moments SET visibility=? WHERE id=?", (visibility_hint, mid))
            c.commit()
        m["visibility"] = visibility_hint

    print(f"OK moment_id={mid} visibility={m['visibility']}")
    print(f"text: {m['text']}")


if __name__ == "__main__":
    main()
