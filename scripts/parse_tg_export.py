#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telegram Desktop HTML 导出 → 干净 JSON。

用法：parse_tg_export.py <导出目录> <输出.json>

只做格式转换，不改内容。产出结构：
{
  "chat": "<聊天名>",
  "source": "<导出目录>",
  "parsed_at": "...",
  "count": N,
  "messages": [
    {"id": "message48655", "ts": "2026-07-11T15:47:46+08:00",
     "from": "Me", "text": "...", "has_media": false, "media_type": null}
  ]
}
"""
import json
import os
import re
import sys
from datetime import datetime
from glob import glob

from bs4 import BeautifulSoup

DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4}) (\d{2}:\d{2}:\d{2})")


def parse_title(title):
    """'11.07.2026 15:47:46 UTC+08:00' → ISO 时间串。"""
    m = DATE_RE.search(title or "")
    if not m:
        return None
    d, mo, y, t = m.groups()
    tz = re.search(r"(UTC[+-]\d{2}:\d{2})", title)
    offset = tz.group(1).replace("UTC", "") if tz else "+08:00"
    return f"{y}-{mo}-{d}T{t}{offset}"


def parse_file(path, chat):
    soup = BeautifulSoup(open(path, encoding="utf-8", errors="ignore").read(), "lxml")
    out = []
    last_who = ""
    for div in soup.select("div.message"):
        cls = div.get("class") or []
        if "service" in cls:
            continue
        body = div.select_one("div.body")
        if body is None:
            continue
        date_el = body.select_one("div.pull_right.date.details")
        ts = parse_title(date_el.get("title") if date_el else "")
        from_el = body.select_one("div.from_name")
        # joined = 与上一条同一发送者，HTML 里省略了 from_name
        who = from_el.get_text(strip=True) if from_el else last_who
        last_who = who
        text_el = body.select_one("div.text")
        text = text_el.get_text("\n", strip=True) if text_el else ""
        media_type = None
        if body.select_one("div.photo") or div.select_one("a.photo_wrap"):
            media_type = "photo"
        elif body.select_one("div.video"):
            media_type = "video"
        elif body.select_one("div.media_wrap"):
            media_type = "media"
        elif body.select_one("div.voice"):
            media_type = "voice"
        elif body.select_one("div.sticker"):
            media_type = "sticker"
        if not text and not media_type:
            continue
        out.append({
            "id": div.get("id", ""),
            "ts": ts,
            "from": who,
            "text": text,
            "has_media": media_type is not None,
            "media_type": media_type,
        })
    return out


def main():
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    src, dst = sys.argv[1], sys.argv[2]
    files = sorted(glob(os.path.join(src, "messages*.html")))
    if not files:
        print(f"没找到 messages*.html：{src}", file=sys.stderr)
        return 1
    # 聊天名从第一页取
    first = BeautifulSoup(open(files[0], encoding="utf-8", errors="ignore").read(), "lxml")
    name_el = first.select_one("div.text.bold")
    chat = name_el.get_text(strip=True) if name_el else "unknown"
    msgs, seen = [], set()
    for f in files:
        for m in parse_file(f, chat):
            if m["id"] and m["id"] in seen:   # 分页可能重叠
                continue
            seen.add(m["id"])
            msgs.append(m)
    msgs.sort(key=lambda m: m["ts"] or "")
    doc = {
        "chat": chat,
        "source": os.path.abspath(src),
        "parsed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "count": len(msgs),
        "messages": msgs,
    }
    tmp = dst + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, dst)
    print(f"{len(msgs)} 条 → {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
