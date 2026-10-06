#!/usr/bin/env python3
"""旧部署的 token 用量基线：从 Claude Code 会话文件里统计每次请求的输入、缓存命中、输出。

只输出汇总数字，不输出任何对话内容。在作者本机运行，只读。
新系统上线后用同样的口径再算一遍，就能直接对比省了多少。

Claude Code 会话文件（~/.claude/projects/<目录>/<会话>.jsonl）里，每条 assistant 记录带 message.usage：
  input_tokens（未命中）、cache_creation_input_tokens（写缓存）、cache_read_input_tokens（命中）、output_tokens。
同一次请求的多个内容块会各占一行、重复同一份 usage，所以按 message.id 去重。

用法：
    python3 lab/dsh/local/baseline_claude_usage.py [--claude-home ~/.claude] [--days 30] [--match channels] [--out 文件] [--keep-names]
默认只统计目录名里含 "channels" 的项目（bot 的工作目录）；私有部署路径不同时用 --match 改关键字，或用 --all。
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def pct(values: list[int], q: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    k = max(0, min(len(s) - 1, round(q * (len(s) - 1))))
    return s[k]


def parse_ts(v) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def scan_project(d: Path, since: datetime) -> dict:
    seen: dict[str, dict] = {}
    first_by_session: dict[str, tuple[datetime, int]] = {}
    sessions: set[str] = set()
    for f in d.glob("*.jsonl"):
        try:
            fh = f.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "assistant":
                    continue
                msg = rec.get("message") or {}
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                ts = parse_ts(rec.get("timestamp"))
                if ts is None or ts < since:
                    continue
                key = msg.get("id") or rec.get("requestId") or rec.get("uuid")
                if not key or key in seen:
                    continue
                inp = int(usage.get("input_tokens") or 0)
                cw = int(usage.get("cache_creation_input_tokens") or 0)
                cr = int(usage.get("cache_read_input_tokens") or 0)
                out = int(usage.get("output_tokens") or 0)
                total = inp + cw + cr
                sid = str(rec.get("sessionId") or f.stem)
                sessions.add(sid)
                seen[key] = {"ts": ts, "total": total, "miss": inp, "write": cw, "hit": cr, "out": out, "model": str(msg.get("model") or "?")}
                if sid not in first_by_session or ts < first_by_session[sid][0]:
                    first_by_session[sid] = (ts, total)
    reqs = list(seen.values())
    if not reqs:
        return {"requests": 0}
    totals = [r["total"] for r in reqs]
    days = max(1.0, (max(r["ts"] for r in reqs) - min(r["ts"] for r in reqs)).total_seconds() / 86400)
    by_day: dict[str, int] = {}
    for r in reqs:
        k = r["ts"].date().isoformat()
        by_day[k] = by_day.get(k, 0) + 1
    models: dict[str, int] = {}
    for r in reqs:
        models[r["model"]] = models.get(r["model"], 0) + 1
    sum_total = sum(totals)
    return {
        "requests": len(reqs),
        "sessions": len(sessions),
        "span_days": round(days, 1),
        "req_per_day_median": statistics.median(by_day.values()),
        "input_median": int(statistics.median(totals)),
        "input_p90": pct(totals, 0.9),
        "input_max": max(totals),
        "first_turn_median": int(statistics.median(v for _, v in first_by_session.values())),
        "hit_ratio": round(sum(r["hit"] for r in reqs) / sum_total, 3) if sum_total else 0,
        "miss_median": int(statistics.median(r["miss"] + r["write"] for r in reqs)),
        "output_median": int(statistics.median(r["out"] for r in reqs)),
        "models": "，".join(f"{m} × {n}" for m, n in sorted(models.items(), key=lambda x: -x[1])),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--claude-home", default=str(Path.home() / ".claude"))
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--match", default="channels", help="只统计目录名包含这个关键字的项目")
    ap.add_argument("--all", action="store_true", help="统计全部项目目录")
    ap.add_argument("--out", default=None)
    ap.add_argument("--keep-names", action="store_true")
    a = ap.parse_args()

    projects = Path(a.claude_home).expanduser() / "projects"
    if not projects.is_dir():
        print(f"没有 {projects}", file=sys.stderr)
        return 1
    since = datetime.now(timezone.utc) - timedelta(days=a.days)
    dirs = sorted(p for p in projects.iterdir() if p.is_dir() and (a.all or a.match in p.name))
    lines = [
        f"# 旧部署 token 用量基线（最近 {a.days} 天）",
        "",
        "口径：一次模型请求 = 一条去重后的 assistant 记录；输入 = 未命中 + 写缓存 + 命中。只含数字，不含任何对话内容。",
        "",
        "| 项目 | 请求数 | 会话数 | 覆盖天数 | 日请求中位数 | 输入中位数 | 输入 P90 | 输入最大 | 新会话首轮输入中位数 | 缓存命中率 | 未命中中位数 | 输出中位数 | 模型 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    # 与 inventory.py 同样的别名规则：channels/ 下带 CLAUDE.md 的目录按名字排序，依次叫 bot1、bot2……
    channels = Path(a.claude_home).expanduser() / "channels"
    skip = {"media", "group_transcripts", "_persona_template"}
    bots = sorted(p for p in channels.glob("*") if p.is_dir() and p.name not in skip and (p / "CLAUDE.md").exists()) if channels.is_dir() else []
    by_slug = {re.sub(r"[^A-Za-z0-9]", "-", str(b.resolve())): (b.name if a.keep_names else f"bot{i + 1}") for i, b in enumerate(bots)}
    for i, d in enumerate(dirs):
        name = by_slug.get(d.name) or (d.name if a.keep_names else f"项目{i + 1}")
        s = scan_project(d, since)
        if not s.get("requests"):
            lines.append(f"| {name} | 0 | | | | | | | | | | | |")
            continue
        lines.append(f"| {name} | {s['requests']} | {s['sessions']} | {s['span_days']} | {s['req_per_day_median']} | {s['input_median']} | {s['input_p90']} | {s['input_max']} | {s['first_turn_median']} | {s['hit_ratio']:.0%} | {s['miss_median']} | {s['output_median']} | {s['models']} |")
    text = "\n".join(lines) + "\n"
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"已写入 {a.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
