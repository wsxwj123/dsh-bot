#!/usr/bin/env python3
"""每天 0:15 为启用中的 dsh bot 生成当天的 wildcard（"今日突然想做的事"）卡片。

写入共用库 ``BOTLIFE_STATE_DB`` 的 ``daily_wildcards`` 表，按 person 记账
（如 bot4 记在 chenlulu 名下）。当天已有卡就跳过，重复跑不会覆盖。

用法：
    BOTLIFE_STATE_DB=/path/to/state.db python3 scripts/daily_wildcards.py
    BOTLIFE_STATE_DB=... python3 scripts/daily_wildcards.py --dry-run
    BOTLIFE_STATE_DB=... python3 scripts/daily_wildcards.py --bot chenlulu

    --dry-run   只打印将写入的卡片，不写库
    --bot ID    只处理一只 bot（排查用）

退出码：
    0  正常结束；单只 bot 的 LLM/解析/入库失败只记一行日志，不影响其它 bot
    2  BOTLIFE_STATE_DB 未设置（拒绝运行，防止写进仓库本地库）
"""
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 用 append 而不是 insert(0)：PYTHONPATH 上顶替 deepseek_client 的模块要优先于仓库里的同名模块。
sys.path.append(_REPO_ROOT)

if not os.environ.get("BOTLIFE_STATE_DB", "").strip():
    print(
        "BOTLIFE_STATE_DB 未设置：拒绝运行。请把它指向共用状态库（例如旧栈的 state.db），"
        "否则卡片会写进仓库本地库、读卡的一侧看不到。",
        file=sys.stderr,
    )
    sys.exit(2)

import argparse  # noqa: E402
from datetime import datetime  # noqa: E402

# deepseek_client 必须先于 generators.world 导入：后者导入时会把仓库根插到 sys.path 最前。
import deepseek_client  # noqa: E402
import db  # noqa: E402
import config_loader  # noqa: E402
from generators import world as world_mod  # noqa: E402
from moments import config_sources  # noqa: E402


_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

_PROMPT = """今天是 {date}（{weekday}）。
{today_bg}
请基于以上今日背景，为人设【{persona}】生成 5-8 张"今日突然想做的事"卡片。

要求：
- 卡片描述用第一人称（"我想……"），不要出现你字——卡片是角色自己看的内部素材
- 每张卡片和今天的日期、新闻或季节有具体联系，避免"突然想散步"这种空话
- 每张卡片带一个触发时的情绪关键词
- 严格只输出 JSON 数组，不要 markdown、不要解释：
[{{"card": "...", "emotion": "..."}}, ...]
"""


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="为启用中的 dsh bot 生成当天的 wildcard 卡片（每天 0:15 跑一次）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将写入的卡片，不写库")
    parser.add_argument("--bot", metavar="ID",
                        help="只处理这一只 bot（排查用）")
    return parser.parse_args(argv)


def _first_text(item: dict, keys) -> str:
    """按顺序取第一个非空字符串字段。"""
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _normalize_cards(raw) -> list[dict]:
    """LLM 返回值 → [{"card", "emotion"}]。

    只接受非空数组；元素可以是对象（card/text/content/title 取内容，
    emotion/type/category 取情绪）或字符串（直接当 card）。
    """
    if not isinstance(raw, list):
        return []
    cards = []
    for item in raw:
        if isinstance(item, str):
            text, emotion = item.strip(), ""
        elif isinstance(item, dict):
            text = _first_text(item, ("card", "text", "content", "title"))
            emotion = _first_text(item, ("emotion", "type", "category"))
        else:
            continue
        if text:
            cards.append({"card": text, "emotion": emotion})
    return cards


def _format_world(snap) -> str:
    """把 WorldSnapshot 压成提示词里的几行今日背景。"""
    parts = []
    date_info = getattr(snap, "date_info", None) or {}
    head = " · ".join(x for x in (date_info.get("weekday_zh"), date_info.get("lunar")) if x)
    if head:
        parts.append(head)
    for label, key in (("节日", "festival"), ("农历节日", "lunar_festival"), ("节气", "solar_term")):
        if date_info.get(key):
            parts.append(f"{label}：{date_info[key]}")

    weather = getattr(snap, "weather", None)
    if weather:
        desc = weather.get("desc_zh") or weather.get("main") or ""
        temp = weather.get("temp")
        if desc:
            parts.append(f"天气：{desc}" + (f"，{temp}℃" if temp is not None else ""))

    news = getattr(snap, "matched_news", None) or getattr(snap, "rss_items", None) or []
    news_lines = [f"- {item['title'].strip()}" for item in news[:5]
                  if isinstance(item, dict) and isinstance(item.get("title"), str)
                  and item["title"].strip()]
    if news_lines:
        parts.append("今日新闻：\n" + "\n".join(news_lines))

    events = []
    for event in (getattr(snap, "on_this_day", None) or [])[:3]:
        if not isinstance(event, dict):
            continue
        text = (event.get("text") or "").strip()
        if text:
            events.append(f"- {event.get('year', '')}：{text}")
    if events:
        parts.append("历史上的今天：\n" + "\n".join(events))

    return "\n".join(parts)


def _build_prompt(cfg: dict, now: datetime, bot_id: str) -> str:
    """拼提示词。collect_world 整体失败时只用日期和人设摘要。"""
    date_str = now.strftime("%Y-%m-%d")
    try:
        context = _format_world(world_mod.collect_world(cfg, now))
    except Exception as exc:
        print(f"[{bot_id}] 今日背景收集失败，改用日期和人设：{type(exc).__name__}: {exc}",
              file=sys.stderr)
        context = ""
    persona = cfg.get("persona_summary") or "（未提供人设摘要）"
    return _PROMPT.format(
        date=date_str,
        weekday=_WEEKDAYS[now.weekday()],
        today_bg=context or "（今日背景暂缺，只按日期和人设生成）",
        persona=persona,
    )


def generate_for_bot(cfg: dict, bot_id: str, date_str: str, now: datetime,
                     dry_run: bool = False) -> int:
    """为一只 bot 生成并写入当日卡片，返回写入（或 dry-run 下将写入）的张数。"""
    person = config_sources.person_key(cfg, bot_id)
    existing = db.get_today_wildcards(person, date_str)
    if existing:
        print(f"[{bot_id}] 当天已有 {len(existing)} 张卡（person={person}），跳过",
              file=sys.stderr)
        return 0

    raw = deepseek_client.call_json(
        _build_prompt(cfg, now, bot_id), timeout=120, max_tokens=1200)
    cards = _normalize_cards(raw)
    if not cards:
        print(f"[{bot_id}] LLM 返回空数组或非数组，跳过（不写库）", file=sys.stderr)
        return 0

    if dry_run:
        print(f"[{bot_id}] dry-run：将为 person={person} 写入 {len(cards)} 张卡")
        for card in cards:
            print(f"  - {card['card']}  [{card['emotion']}]")
        return len(cards)

    db.save_today_wildcards(person, date_str, cards)
    print(f"[{bot_id}] 已为 person={person} 写入 {len(cards)} 张卡", file=sys.stderr)
    return len(cards)


def main(argv=None) -> int:
    args = _parse_args(argv)
    db.init()

    bots = config_loader.list_enabled_bots()
    if args.bot:
        bots = [cfg for cfg in bots if cfg.get("_bot_id") == args.bot]
        if not bots:
            print(f"[{args.bot}] 未找到或未启用，跳过", file=sys.stderr)
            return 0
    if not bots:
        print("没有启用中的 bot，跳过", file=sys.stderr)
        return 0

    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    print(f"为 {len(bots)} 只 bot 生成 {date_str} 的 wildcard...", file=sys.stderr)

    total = 0
    for cfg in bots:
        bot_id = cfg.get("_bot_id") or cfg.get("id") or "?"
        try:
            total += generate_for_bot(cfg, bot_id, date_str, now, dry_run=args.dry_run)
        except Exception as exc:
            # 单只失败不影响其它 bot，整体退出码仍是 0。
            print(f"[{bot_id}] 生成失败，跳过：{type(exc).__name__}: {exc}", file=sys.stderr)

    print(f"完成：共 {total} 张", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
