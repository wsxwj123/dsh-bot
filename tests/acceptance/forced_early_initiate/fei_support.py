# -*- coding: utf-8 -*-
"""forced-early-initiate 验收测试的共用常量与小工具（测试文件和 conftest 都从这里取）。"""
import importlib.util
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SCHEMA = (ROOT / "gateway" / "src" / "ledger-schema.sql").read_text(encoding="utf-8")

BOT = "bot5"
CHAT = "42"
HOUR = 3600
MIN = 60
DAY = 86400
# 固定在一个本地日期的清晨（6 月无夏令时），便于推算"当天"
T0 = int(datetime(2026, 6, 10, 6, 0, 0).timestamp())


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_bot_layout(base: Path):
    """新系统布局：configs/bot5.yml + bots/bot5/{channel,state/ledger.sqlite}"""
    root = base / "bots" / BOT
    (root / "channel").mkdir(parents=True)
    (root / "state").mkdir()
    with closing(sqlite3.connect(root / "state" / "ledger.sqlite")) as db:
        db.executescript(SCHEMA)
        db.commit()
    cfgs = base / "configs"
    cfgs.mkdir()
    (cfgs / f"{BOT}.yml").write_text(
        f"id: {BOT}\nchat_id: '{CHAT}'\nbot_channel_path: {json.dumps(str(root / 'channel'))}\n"
        f"dispatcher_port: 0\npersona_summary: 测试人设\n", encoding="utf-8")
    return root, cfgs


def set_connection(root: Path, value):
    """写想念值状态文件；value 为 None 表示没有这个文件。"""
    f = root / "state" / "jiwen" / f"{BOT}-{CHAT}.json"
    if value is None:
        if f.exists():
            f.unlink()
        return
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"connection": value}), encoding="utf-8")


