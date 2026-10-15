# -*- coding: utf-8 -*-
"""适配服务 v0.4 验收：生活设定 GET / PUT /adapter/v1/bots/{bot_id}/life。

依据：约定 v0.4 第 4 节与 5.6；INTERFACE-生活设定.md 全文；补充文件第 1 到 3 节、第 7 节。不读实现。
允许目录经测试接缝 DSH_CONSOLE_LIFE_DIR 指到临时目录；备份与写锁在沙箱的 DSH_BOT_HOME 下。
生活设定文件全是合成样例（全部 14 个字段、多行字面块、不带引号的日期、键间注释、字段表外的键）。
"""
import datetime
import fcntl
import json
import logging
import os
import re
import stat
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import yaml

from . import adapter_util as au
from .adapter_util import adapter, console_guard  # noqa: F401  夹具

REAL_HOME = au.REAL_CONSOLE_DIR.parents[1]      # 只拿来拼字符串比对，绝不访问

KEYS = ["persona_summary", "speaking_threshold", "user_address", "birthday", "schooling",
        "school_calendar", "sleep_hours", "recurring_activities", "personal_hobbies",
        "hobby_cooldown_hours", "event_categories", "city", "interest_keywords", "rss_feeds"]

SAVED = "已保存，不用重启，各项在下次用到时生效"
NO_CHANGE = "没有改动"
MSG = {
    "N": "这只 bot 没有单独的生活设定文件，不能在这里改",
    "O": "这只 bot 的生活设定文件不在允许修改的位置，不能在这里改",
    "S": "这只 bot 的生活设定文件和别处共用，改了会连带别处，不能在这里改",
    "M": "这只 bot 的生活设定文件不见了，请检查它的配置",
    "B": "这只 bot 的生活设定文件读不出来",
}
Y_MSG = re.compile(r"^这只 bot 的生活设定文件格式有错(（第 \d+ 行附近）)?$")
BUSY_LOCK = "另一次保存正在进行，这次没有保存，请稍后重试"
RO_OWN_YML = "这只 bot 自己的配置里也写了这一项，并且以那边为准，这里只能看"
RO_ODD = "配置文件里这一项有这里管不了的写法，这里只能看，要改请直接改配置文件"
BACKUP_NAME = re.compile(r"^\d{8}-\d{6}(-\d{2})?\.yml$")

SAMPLE = """# 生活设定合成样例：测试专用，全是编的
# 头注释第二行

id: botalpha
display_name: 合成样例
chat_id: %s
persona_summary: |
  第一行人设：喜欢下雨天。
  第二行有"引号"和冒号: 还有井号 # 不是注释
speaking_threshold: 有新鲜事或者很久没聊天时可以主动说
user_address: 小测
birthday: 2008-03-15
schooling:
  stage: 高中
  enrollment_year: 2023

# 校历上面的注释
school_calendar:
  summer_start: "07-10"
  summer_end: "08-31"
  early_return: "08-20"
  winter_days: 14
sleep_hours:
  - "23:30-07:00"
  - "13:00-13:30"
recurring_activities:
  weekday:
    - name: 上课
      when: "08:00-12:00"
      state: busy
      description: 在教室听课
      interruptible: false
  weekend:
    - name: 睡懒觉
      when: "07:00-10:00"
      state: sleeping
personal_hobbies:
  - name: 看书
    probability_per_roll: 0.3
    effect: 安静地翻书
    activity_type: reading
    prefer_channel: dm
hobby_cooldown_hours: 4  # 行尾注释
event_categories: []
city: 杭州
# 城市下面的注释
interest_keywords:
  - 天文
  - 猫
rss_feeds:
  - https://example.invalid/feed.xml
bio: 字段表外的签名
voice_id: fake-voice-001
moments: true
jiwen:
  decay: 0.5
# 文件末尾的注释
""" % au.CHAT_ID

SAMPLE_VALUES = {
    "persona_summary": '第一行人设：喜欢下雨天。\n第二行有"引号"和冒号: 还有井号 # 不是注释\n',
    "speaking_threshold": "有新鲜事或者很久没聊天时可以主动说",
    "user_address": "小测",
    "birthday": "2008-03-15",
    "schooling": {"stage": "高中", "enrollment_year": 2023},
    "school_calendar": {"summer_start": "07-10", "summer_end": "08-31", "early_return": "08-20",
                        "winter_days": 14},
    "sleep_hours": ["23:30-07:00", "13:00-13:30"],
    "recurring_activities": {
        "weekday": [{"name": "上课", "when": "08:00-12:00", "state": "busy", "description": "在教室听课",
                     "interruptible": "否"}],
        "weekend": [{"name": "睡懒觉", "when": "07:00-10:00", "state": "sleeping", "description": "",
                     "interruptible": ""}],
        "break": [], "early_return": []},
    "personal_hobbies": [{"name": "看书", "probability_per_roll": 0.3, "effect": "安静地翻书",
                          "activity_type": "reading", "prefer_channel": "dm"}],
    "hobby_cooldown_hours": 4,
    "event_categories": [],
    "city": "杭州",
    "interest_keywords": ["天文", "猫"],
    "rss_feeds": ["https://example.invalid/feed.xml"],
}

EMPTY_VALUES = {
    "persona_summary": "", "speaking_threshold": "", "user_address": "", "birthday": "",
    "schooling": {}, "school_calendar": {}, "sleep_hours": [], "recurring_activities": {},
    "personal_hobbies": [], "hobby_cooldown_hours": None, "event_categories": [], "city": "",
    "interest_keywords": [], "rss_feeds": [],
}

# key -> (label, type, 限制键)
FIELD_TABLE = {
    "persona_summary": ("人设摘要", "textarea", {"max_len": 4000}),
    "speaking_threshold": ("什么时候主动说话", "textarea", {"max_len": 4000}),
    "user_address": ("主动消息里怎么称呼用户", "text", {"max_len": 40}),
    "birthday": ("生日", "text", {"placeholder": "YYYY-MM-DD", "max_len": 10}),
    "schooling": ("学籍", "object", {}),
    "school_calendar": ("校历", "object", {}),
    "sleep_hours": ("睡觉时段", "string_list", {"placeholder": "23:30-07:00", "max_items": 6}),
    "recurring_activities": ("日常作息", "object_list_map", {"max_items": 40}),
    "personal_hobbies": ("爱好", "object_list", {"max_items": 40}),
    "hobby_cooldown_hours": ("两次想起爱好至少隔几小时", "number", {"min": 0, "max": 168}),
    "event_categories": ("会遇到的生活事件", "string_list", {}),
    "city": ("所在城市", "text", {"max_len": 40}),
    "interest_keywords": ("关心的话题", "string_list", {"max_len": 40, "max_items": 60}),
    "rss_feeds": ("订阅的新闻源", "string_list", {"max_len": 500, "max_items": 30}),
}
HELPS = [
    "判断要不要主动说话、情绪怎么变时参考的人物简介，不改聊天时的人设。",
    "写给判断程序看，说明什么情况下值得主动找用户。",
    '不填就叫"用户"。',
    "用来算年龄和年级。",
    "两项都填才算在读，到时候自动升年级和毕业。",
    "决定什么时候放暑假、寒假和提前返校，全部不填就不跟校历走。",
    "这些时段里不主动说话，作息按在睡觉算。",
    "按时段写这一天在做什么，表里没写到的时段按空闲或睡觉算。",
    "空闲时会随机想起来做的事。",
    "不填按 4 小时。",
    "只有选中的类别里的事件会随机发生。",
    "用来查天气，不填就不查。",
    "新闻标题里有这些词才会拿来当话题。",
    "生活程序会定时去读这些地址。",
]
# 子项：key -> [(子项 key, label, type, options 或 None)]
ITEM_FIELDS = {
    "schooling": [("stage", "学段", "text", ["高中", "大学"]),
                  ("enrollment_year", "入学年份", "number", None)],
    "school_calendar": [("summer_start", "暑假开始", "text", None), ("summer_end", "暑假结束", "text", None),
                        ("early_return", "提前返校", "text", None),
                        ("winter_days", "春节前后各放几天寒假", "number", None)],
    "recurring_activities": [("name", "在做什么", "text", None), ("when", "时段", "text", None),
                             ("state", "状态", "text", None), ("description", "说明", "textarea", None),
                             ("interruptible", "能不能被打断", "text", ["是", "否"])],
    "personal_hobbies": [("name", "名字", "text", None),
                         ("probability_per_roll", "每次想起来的概率", "number", None),
                         ("effect", "做这件事时的样子", "textarea", None),
                         ("activity_type", "活动类型", "text",
                          ["reading", "cooking", "browse", "search", "observe", "selfcare"]),
                         ("prefer_channel", "只在私聊里提", "text", ["dm"])],
}
GROUPS = [{"key": "weekday", "label": "工作日"}, {"key": "weekend", "label": "周末和节假日"},
          {"key": "break", "label": "寒暑假"}, {"key": "early_return", "label": "提前返校期间"}]


# ============================================================ 辅助件

def life_url(bot_id):
    return "%s/%s/life" % (au.BOTS, bot_id)


MISSING = object()


@pytest.fixture
def life(adapter, monkeypatch):
    """允许目录 = 临时目录 life-data（经 DSH_CONSOLE_LIFE_DIR）；备份与锁在沙箱 DSH_BOT_HOME 下。"""
    sb = adapter.sandbox
    d = sb.tmp / "life-data"
    d.mkdir()
    monkeypatch.setenv("DSH_CONSOLE_LIFE_DIR", str(d))
    return SimpleNamespace(dir=d, backups=sb.dsh_home / "backups" / "life",
                           locks=sb.dsh_home / "locks", lock=sb.dsh_home / "locks" / "life.lock")


def life_bot(adapter, life, bot_id="botalpha", *, text=SAMPLE, life_config=None, enabled=True,
             extra=None):
    """摆一只 bot：配置顶层写 life_config（默认指向允许目录下 <bot_id>.yml），text 不为 None 就写进去。"""
    bot = adapter.bot(bot_id, enabled=enabled)
    path = life.dir / ("%s.yml" % bot_id)
    cfg = json.loads(bot.yml.read_text(encoding="utf-8"))
    if life_config is None:
        cfg["life_config"] = str(path)
    elif life_config is not MISSING:
        cfg["life_config"] = life_config
    cfg.update(extra or {})
    bot.yml.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    if text is not None:
        path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
    return SimpleNamespace(id=bot_id, yml=bot.yml, path=path)


def get(adapter, bot_id="botalpha"):
    return adapter.call("GET", life_url(bot_id))


def put(adapter, values, bot_id="botalpha"):
    return adapter.call("PUT", life_url(bot_id), {"values": values})


def tree(d):
    """目录下每一项：(权限位, 修改时间, 普通文件的字节)。不跟软链。不存在给 None。"""
    d = str(d)
    if not os.path.lexists(d):
        return None
    out = {}
    for root, dirs, files in os.walk(d):
        for name in dirs + files:
            p = os.path.join(root, name)
            st = os.lstat(p)
            data = None
            if stat.S_ISREG(st.st_mode):
                with open(p, "rb") as f:
                    data = f.read()
            out[os.path.relpath(p, d)] = (st.st_mode, st.st_mtime_ns, data)
    return out


def snapshot(life):
    return {"允许目录": tree(life.dir), "备份目录": tree(life.backups), "锁目录": tree(life.locks)}


def backups_of(life, bot_id="botalpha"):
    d = life.backups / bot_id
    return sorted(p.name for p in d.iterdir() if BACKUP_NAME.match(p.name)) if d.is_dir() else []


def parsed(path):
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def norm(doc):
    """比较用：日期换成 YYYY-MM-DD；去掉值为空的顶层键（新值为空时约定就是把键去掉）。"""
    out = {}
    for k, v in (doc or {}).items():
        if isinstance(v, datetime.date):
            v = v.isoformat()
        if v in ("", None, [], {}):
            continue
        out[k] = v
    return out


KEY_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):(?:[ \t]|$)")


def around(text, key):
    """按生活设定 6.1 切出某个顶层键那一段之外的"段前"与"段后"文本。"""
    lines = text.splitlines(keepends=True)
    tops = [i for i, line in enumerate(lines) if KEY_LINE.match(line)]
    start = next(i for i in tops if KEY_LINE.match(lines[i]).group(1) == key)
    end = next((i for i in tops if i > start), len(lines))
    while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
        end -= 1
    return "".join(lines[:start]), "".join(lines[end:])


# ---- 审计钩子：记下进程里打开过的路径（钩子只能装不能卸，所以只装一次、靠开关）
_OPENS = {"log": None}


def _audit(event, args):
    log = _OPENS["log"]
    if log is not None and event == "open" and args:
        p = args[0]
        if isinstance(p, bytes):
            p = os.fsdecode(p)
        if isinstance(p, str):
            log.append(p)


if not getattr(sys, "_adapter_v04_audit", False):
    sys.addaudithook(_audit)
    sys._adapter_v04_audit = True


@contextmanager
def recording_opens():
    log = []
    _OPENS["log"] = log
    try:
        yield log
    finally:
        _OPENS["log"] = None


def opened_any(log, targets):
    """log 里有没有打开过 targets 里的任何一个（字符串相同或真实路径相同）。"""
    real = {str(t) for t in targets}                      # str 目标只按字符串比，不碰文件系统
    real |= {os.path.realpath(str(t)) for t in targets if not isinstance(t, str)}
    return [p for p in log if p in real or (os.path.isabs(p) and os.path.realpath(p) in real)]


# ============================================================ GET

def test_生活设定GET_字段表的顺序标签类型说明限制与子项分组如约(adapter, life):
    life_bot(adapter, life)
    fields = adapter.ok(get(adapter))["fields"]
    assert [f.get("key") for f in fields] == KEYS, "fields 的键和顺序应照字段表"
    for f, help_text in zip(fields, HELPS):
        label, typ, limits = FIELD_TABLE[f["key"]]
        assert (f.get("label"), f.get("type"), f.get("help")) == (label, typ, help_text), f
        for k, v in limits.items():
            assert f.get(k) == v, "%s 的 %s 应为 %r：%r" % (f["key"], k, v, f)
    by_key = {f["key"]: f for f in fields}
    for key, subs in ITEM_FIELDS.items():
        items = by_key[key].get("item_fields")
        assert [(i.get("key"), i.get("label"), i.get("type")) for i in items or []] == \
            [s[:3] for s in subs], "%s 的子项不对：%r" % (key, items)
        for item, sub in zip(items, subs):
            if sub[3] is not None:
                assert item.get("options") == sub[3], "%s.%s 的 options 不对：%r" % (key, sub[0], item)
    assert by_key["recurring_activities"].get("groups") == GROUPS
    opts = by_key["event_categories"].get("options")
    if opts is not None:
        assert opts and len(set(opts)) == len(opts) and all(isinstance(o, str) and o for o in opts), opts


def test_生活设定GET_values取值如约_日期给字符串_能否打断映射成是否_缺的子项和分组补齐(adapter, life):
    life_bot(adapter, life)
    body = adapter.ok(get(adapter))
    assert body["values"] == SAMPLE_VALUES, "values 与文件内容对不上：%r" % body["values"]


@pytest.mark.parametrize("text", ["", "# 只有注释\n\n# 第二行注释\n"], ids=["空文件", "只有注释"])
def test_生活设定GET_空文件或只有注释_每个字段给该类型的空值(adapter, life, text):
    life_bot(adapter, life, text=text)
    assert adapter.ok(get(adapter))["values"] == EMPTY_VALUES


# ============================================================ PUT 成功

def test_生活设定PUT_整份values原样交回_回没有改动_文件逐字节不变_不建备份(adapter, life):
    life_bot(adapter, life)
    values = adapter.ok(get(adapter))["values"]
    before = snapshot(life)
    body = adapter.ok(put(adapter, values))
    assert (body.get("applied"), body.get("queued"), body.get("message")) == (True, False, NO_CHANGE), body
    after = snapshot(life)
    assert after["允许目录"] == before["允许目录"], "没有改动，生活设定文件或允许目录却变了"
    assert backups_of(life) == [], "没有改动却建了备份"


NEW = {   # key -> (提交的新值, 写进文件后解析出来应是的值)
    "persona_summary": ("新的人设第一行\n第二行：带冒号 # 带井号\n",) * 2,
    "speaking_threshold": ("有趣的事就主动说",) * 2,
    "user_address": ("阿测",) * 2,
    "birthday": ("2010-05-06",) * 2,
    "schooling": ({"stage": "大学", "enrollment_year": 2024},) * 2,
    "school_calendar": ({"summer_start": "07-01", "summer_end": "08-25", "early_return": "08-15",
                         "winter_days": 10},) * 2,
    "sleep_hours": (["00:30-08:00"],) * 2,
    "recurring_activities": (
        {"weekday": [{"name": "自习", "when": "19:00-21:00", "state": "busy", "description": "",
                      "interruptible": "是"}]},
        {"weekday": [{"name": "自习", "when": "19:00-21:00", "state": "busy", "interruptible": True}]}),
    "personal_hobbies": (
        [{"name": "画画", "probability_per_roll": 0.5, "effect": "", "activity_type": "", "prefer_channel": ""}],
        [{"name": "画画", "probability_per_roll": 0.5}]),
    "hobby_cooldown_hours": (6.0, 6),
    "event_categories": (None, None),        # 用例里按 GET 给的 options 现定
    "city": ("苏州",) * 2,
    "interest_keywords": (["音乐", "电影"],) * 2,
    "rss_feeds": (["https://example.invalid/other.xml"],) * 2,
}


@pytest.mark.parametrize("key", KEYS)
def test_生活设定PUT_单改一个字段_只换这一段_先备份原文_改回原值后解析与最初相同(adapter, life, key):
    bot = life_bot(adapter, life)
    os.chmod(bot.path, 0o640)
    got = adapter.ok(get(adapter))
    old_value = got["values"][key]
    new_value, in_file = NEW[key]
    if key == "event_categories":
        opts = next(f for f in got["fields"] if f["key"] == key).get("options") or ["测试类别"]
        new_value = in_file = [opts[0]]
    original = bot.path.read_bytes()
    first = parsed(bot.path)
    names = sorted(os.listdir(life.dir))
    body = adapter.ok(put(adapter, {key: new_value}))
    assert (body.get("applied"), body.get("queued"), body.get("message")) == (True, False, SAVED), body
    text = bot.path.read_text(encoding="utf-8")
    head, tail = around(SAMPLE, key)
    assert text.startswith(head) and text.endswith(tail), "这一段以外的文字变了"
    doc = parsed(bot.path)
    assert doc[key] == in_file and type(doc[key]) is type(in_file), "%s 写进文件后是 %r" % (key, doc[key])
    assert {k: v for k, v in doc.items() if k != key} == {k: v for k, v in first.items() if k != key}, \
        "别的键变了"
    assert stat.S_IMODE(os.stat(bot.path).st_mode) == 0o640, "写完后权限应与原文件相同"
    assert sorted(os.listdir(life.dir)) == names, "允许目录里多了或少了文件"
    saved = backups_of(life)
    assert len(saved) == 1, "改之前应建一份备份：%r" % saved
    copy = life.backups / "botalpha" / saved[0]
    assert copy.read_bytes() == original, "备份应是改之前的原文，逐字节"
    assert stat.S_IMODE(os.stat(copy).st_mode) == 0o600, "备份文件权限应为 0600"
    adapter.ok(put(adapter, {key: old_value}))
    assert norm(parsed(bot.path)) == norm(first), "改回原值后解析结果应与最初相同"


TRICKY = {
    "像数字": ("city", "123"), "像小数": ("city", "3.14"), "像布尔": ("city", "true"),
    "像空值": ("city", "null"), "波浪号": ("city", "~"), "像日期": ("city", "2020-01-01"),
    "井号开头": ("city", "#井号开头"), "横线开头": ("city", "- 横线开头"), "空格开头": ("city", " 空格开头"),
    "行尾空格": ("city", "行尾空格  "), "冒号": ("city", "冒号: 在中间"),
    "引号": ("city", "'单引号' \"双引号\""), "表情": ("city", "表情😀"),
    "多行各种行首": ("persona_summary", "第一行\n- 横线开头的行\n  空格开头的行\n# 井号开头的行\n行尾空格  \n"),
    "不以换行结尾": ("persona_summary", "不以换行结尾\n第二行"),
    "以换行开头_带制表符": ("persona_summary", "\n以换行开头\t带制表符😀\n\n中间有空行\n"),
}


@pytest.mark.parametrize("key,value", list(TRICKY.values()), ids=list(TRICKY))
def test_生活设定PUT_文字往返_GET读回与文件解析都逐字相同(adapter, life, key, value):
    bot = life_bot(adapter, life)
    adapter.ok(put(adapter, {key: value}))
    assert adapter.ok(get(adapter))["values"][key] == value, "GET 读回的不是原样"
    assert parsed(bot.path)[key] == value, "写进文件解析出来不是原样（类型或内容变了）"


CLEARS = {
    "单行文字清空": ("city", ""),
    "对象子项全空": ("school_calendar", {"summer_start": "", "summer_end": "", "early_return": "",
                                   "winter_days": None}),
    "分组全空": ("recurring_activities", {"weekday": [], "weekend": []}),
}


@pytest.mark.parametrize("key,empty", list(CLEARS.values()), ids=list(CLEARS))
def test_生活设定PUT_清空一个字段_这个键从文件里去掉_前后注释和别的键都在(adapter, life, key, empty):
    bot = life_bot(adapter, life)
    first = parsed(bot.path)
    adapter.ok(put(adapter, {key: empty}))
    head, tail = around(SAMPLE, key)
    assert bot.path.read_text(encoding="utf-8") == head + tail, "清空应只去掉这一段，段外逐字不变"
    doc = parsed(bot.path)
    assert key not in doc
    assert doc == {k: v for k, v in first.items() if k != key}


def test_生活设定PUT_文件里没有这个键且不以换行结尾_补一个换行后追加到末尾(adapter, life):
    original = "# 只有头注释和一个字段表外的键\nbio: 签名"
    bot = life_bot(adapter, life, text=original)
    adapter.ok(put(adapter, {"city": "苏州"}))
    text = bot.path.read_text(encoding="utf-8")
    assert text.startswith(original + "\n"), "原文应原样留在前面，并先补一个换行：%r" % text
    assert yaml.safe_load(text[len(original) + 1:]) == {"city": "苏州"}, "追加的那一段不对：%r" % text
    assert parsed(bot.path) == {"bio": "签名", "city": "苏州"}


OK_CHANGE = {"speaking_threshold": "换一个说法：有趣的事就说"}
BAD_VALUES = {
    "不在字段表里": ({"nickname": "小名"}, "没有这一项"),
    "字段表外的已知键bio": ({"bio": "新签名"}, "没有这一项"),
    "注册键chat_id": ({"chat_id": 1}, "没有这一项"),
    "单行文字有换行": ({"city": "苏\n州"}, None),
    "有控制字符": ({"city": "苏\x07州"}, None),
    "超过40个字": ({"city": "城" * 41}, None),
    "文字给了数字": ({"city": 123}, None),
    "生日不存在的日子": ({"birthday": "2023-02-30"}, None),
    "生日晚于今天": ({"birthday": "2999-01-01"}, None),
    "年龄超过150": ({"birthday": "1800-01-01"}, None),
    "整数给了小数": ({"hobby_cooldown_hours": 1.5}, None),
    "整数超过上限": ({"hobby_cooldown_hours": 169}, None),
    "数字给了布尔": ({"hobby_cooldown_hours": True}, None),
    "时段小时是24": ({"sleep_hours": ["24:00-07:00"]}, None),
    "时段带空格": ({"sleep_hours": ["23:30 - 07:00"]}, None),
    "时段超过6项": ({"sleep_hours": ["01:00-02:00"] * 7}, None),
    "列表项是空串": ({"interest_keywords": [""]}, None),
    "订阅源不是http": ({"rss_feeds": ["ftp://example.invalid/x"]}, None),
    "学籍只填一项": ({"schooling": {"stage": "大学", "enrollment_year": None}}, None),
    "学段不在选项里": ({"schooling": {"stage": "初中", "enrollment_year": 2020}}, None),
    "对象里有不认识的子项": ({"schooling": {"stage": "大学", "enrollment_year": 2020, "major": "x"}}, None),
    "暑假开始晚于结束": ({"school_calendar": {"summer_start": "09-01", "summer_end": "07-01"}}, None),
    "提前返校不在暑假里": ({"school_calendar": {"summer_start": "07-01", "summer_end": "08-20",
                                        "early_return": "09-05"}}, None),
    "月日不存在": ({"school_calendar": {"summer_start": "02-30", "summer_end": "08-31"}}, None),
    "作息有不认识的分组": ({"recurring_activities": {"holiday": []}}, None),
    "作息行缺了必填的name": ({"recurring_activities": {"weekday": [{"when": "08:00-09:00"}]}}, None),
    "爱好概率超过1": ({"personal_hobbies": [{"name": "画画", "probability_per_roll": 1.5}]}, None),
    "爱好类型不在选项里": ({"personal_hobbies": [{"name": "画画", "probability_per_roll": 0.5,
                                         "activity_type": "dance"}]}, None),
    "两项都错": ({"city": 1, "user_address": "称\n呼"}, None),
}


@pytest.mark.parametrize("bad,text", list(BAD_VALUES.values()), ids=list(BAD_VALUES))
def test_生活设定PUT_一项对一项错_回422只列错的键_文件备份锁目录都不动(adapter, life, bad, text):
    life_bot(adapter, life)
    before = snapshot(life)
    body = adapter.expect_error(put(adapter, dict(OK_CHANGE, **bad)), 422, "invalid")
    assert body["message"] == "有几项没填对", body
    fields = body.get("fields")
    assert isinstance(fields, dict) and set(fields) == set(bad), "fields 应只列出错的键：%r" % fields
    assert all(isinstance(v, str) and v.strip() for v in fields.values()), fields
    if text:
        assert all(text in v for v in fields.values()), fields
    assert snapshot(life) == before, "校验没过却动了生活设定文件、备份目录或锁目录"


def _sized(n):
    """正好 n 个字节的请求体：values 为空对象，另带一个会被忽略的顶层键。"""
    shell = '{"values": {}, "pad": "%s"}'
    return shell % ("x" * (n - len(shell % "")))


SHAPES = {
    "正好256KiB_不因体积被拒": (_sized(262144), 200, NO_CHANGE),
    "超出一个字节": (_sized(262145), 422, "请求体太大，上限 256 KiB"),
    "数组": ("[]", 422, "请求体必须是一个 JSON 对象"),
    "坏JSON": ("{bad", 422, "请求体必须是一个 JSON 对象"),
    "空体": ("", 422, "请求体必须是一个 JSON 对象"),
    "没有values": ('{"other": 1}', 422, "请求体里要有 values，而且是一个对象"),
    "values是数组": ('{"values": []}', 422, "请求体里要有 values，而且是一个对象"),
    "values是null": ('{"values": null}', 422, "请求体里要有 values，而且是一个对象"),
}


@pytest.mark.parametrize("raw,status,message", list(SHAPES.values()), ids=list(SHAPES))
def test_生活设定PUT_请求体形状与体积上限_文案如约_fields为空对象(adapter, life, raw, status, message):
    life_bot(adapter, life)
    before = snapshot(life)
    resp = adapter.call("PUT", life_url("botalpha"), raw=raw)
    if status == 200:
        assert adapter.ok(resp).get("message") == message
    else:
        body = adapter.expect_error(resp, 422, "invalid")
        assert (body["message"], body.get("fields")) == (message, {}), body
    assert snapshot(life)["允许目录"] == before["允许目录"]
    assert backups_of(life) == []


# ============================================================ 路由与通用错误

@pytest.mark.parametrize("method", ["GET", "PUT"])
@pytest.mark.parametrize("bot_id,status,code", [("..", 422, "invalid"), ("a%2Fb", 422, "invalid"),
                                                ("a" * 65, 422, "invalid"), ("ghost", 404, "unknown_bot")],
                         ids=["两个点", "编码的斜杠", "65个字符", "没有这只bot"])
def test_生活设定_bot_id不合规回422且fields为空_没有这只回404(adapter, life, method, bot_id, status, code):
    life_bot(adapter, life)
    before = snapshot(life)
    body = {"values": {"city": "苏州"}} if method == "PUT" else au.NO_BODY
    got = adapter.expect_error(adapter.call(method, life_url(bot_id), body), status, code)
    if status == 422:
        assert got.get("fields") == {}, got
    assert snapshot(life) == before


@pytest.mark.parametrize("method,path,allow", [
    ("POST", life_url("botalpha"), True), ("DELETE", life_url("botalpha"), True),
    ("PATCH", life_url("botalpha"), True), ("HEAD", life_url("botalpha"), True),
    ("GET", "%s/botalpha/notify" % au.BOTS, False), ("PUT", "%s/botalpha/notify" % au.BOTS, False),
], ids=["生活设定POST", "生活设定DELETE", "生活设定PATCH", "生活设定HEAD", "通知GET", "通知PUT"])
def test_路由_v04新接口方法不对_回405_生活设定带Allow头(adapter, life, method, path, allow):
    life_bot(adapter, life)
    resp = adapter.call(method, path, {"values": {}} if method != "HEAD" else au.NO_BODY)
    assert resp.status_code == 405, "应为 405，实际 %s" % resp.status_code
    if method != "HEAD":
        assert au.body_of(resp).get("code") == "method_not_allowed"
    if allow:
        got = {m.strip() for m in (resp.headers.get("Allow") or "").split(",") if m.strip()}
        assert got == {"GET", "PUT"}, "Allow 头应为 GET, PUT：%r" % resp.headers.get("Allow")


# ============================================================ 改哪个文件：白名单之外一律不碰

def _place(adapter, life, case, monkeypatch):
    """按情形摆好 bot；返回 (期望的情形, 不许被打开的路径, 要求逐字节不变的文件)。"""
    sb, d = adapter.sandbox, life.dir
    outside = sb.tmp / "outside"
    outside.mkdir(exist_ok=True)

    def plant(p, text=SAMPLE):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    if case in ("没写", "不是字符串", "空白"):
        lc = {"没写": MISSING, "不是字符串": 123, "空白": "   "}[case]
        life_bot(adapter, life, life_config=lc)
        return "N", [], [d / "botalpha.yml"]
    if case == "软链到外面":
        target = plant(outside / "real.yml")
        life_bot(adapter, life, text=None)
        os.symlink(str(target), str(d / "botalpha.yml"))
        return "S", [target], [target]
    if case == "硬链接":
        target = plant(outside / "hard.yml")
        life_bot(adapter, life, text=None)
        os.link(str(target), str(d / "botalpha.yml"))
        return "S", [], [target]
    if case == "两只bot共用_另一只停用":
        shared = plant(d / "shared.yml")
        life_bot(adapter, life, life_config=str(shared), text=None)
        life_bot(adapter, life, "botbravo", life_config=str(shared), text=None, enabled=False)
        return "S", [], [shared]
    if case == "允许目录指到本系统配置根":
        target = plant(sb.dsh_cfg / "life" / "botalpha.yml")
        monkeypatch.setenv("DSH_CONSOLE_LIFE_DIR", str(target.parent))
        life_bot(adapter, life, life_config=str(target), text=None)
        return "O", [target], [target]
    tilde = {"旧系统目录": "~/claudebotlife/configs/botalpha.yml",
             "频道目录": "~/.claude/channels/botalpha/life.yml"}
    if case in tilde:
        target = plant(sb.home / tilde[case][2:])
        life_bot(adapter, life, life_config=tilde[case], text=None)
        return "O", [target, str(REAL_HOME / tilde[case][2:])], [target]
    inside = {"本系统配置根": (sb.dsh_cfg / "life" / "botalpha.yml", None),
              "下划线开头": (d / "_global.yml", None), "点开头": (d / ".botalpha.yml", None),
              "扩展名不是yml": (d / "botalpha.yaml", None), "子目录": (d / "sub" / "botalpha.yml", None),
              "相对路径": (d / "botalpha.yml", "life-data/botalpha.yml"),
              "含两个点": (d / "botalpha.yml", str(d / "sub" / ".." / "botalpha.yml")),
              "含一个点": (d / "botalpha.yml", str(d) + "/./botalpha.yml")}
    target, lc = inside[case]
    plant(target)
    (d / "sub").mkdir(exist_ok=True)
    life_bot(adapter, life, life_config=lc or str(target), text=None)
    forbid = [target] if case in ("本系统配置根", "子目录") else []
    return "O", forbid, [target]


CASES_501 = ["没写", "不是字符串", "空白", "旧系统目录", "频道目录", "本系统配置根", "允许目录指到本系统配置根",
             "下划线开头", "点开头", "扩展名不是yml", "子目录", "相对路径", "含两个点", "含一个点",
             "软链到外面", "硬链接", "两只bot共用_另一只停用"]


@pytest.mark.parametrize("method", ["GET", "PUT"])
@pytest.mark.parametrize("case", CASES_501)
def test_生活设定_文件不在白名单或与别处共用_回501_文案如约_不读不写(adapter, life, monkeypatch, case, method):
    kind, forbid, watched = _place(adapter, life, case, monkeypatch)
    before = {str(p): p.read_bytes() for p in watched if p.exists()}
    body = {"values": {"city": "苏州"}} if method == "PUT" else au.NO_BODY
    with recording_opens() as log:
        resp = adapter.call(method, life_url("botalpha"), body)
    got = adapter.expect_error(resp, 501, "unsupported")
    assert got["message"] == MSG[kind], "应按情形 %s 回：%r" % (kind, got["message"])
    assert opened_any(log, forbid) == [], "打开了允许目录以外的文件：%r" % opened_any(log, forbid)
    assert {str(p): p.read_bytes() for p in watched if p.exists()} == before, "文件被改了"
    assert backups_of(life) == [] and not life.locks.exists(), "不该建备份或锁"


BROKEN = {
    "文件不存在": ("M", None),
    "是个目录": ("B", "dir"),
    "超过1MiB": ("B", b"# " + b"x" * (1 << 20) + b"\n"),
    "不是UTF8": ("B", b"city: \xff\xfe\n"),
    "解析不出": ("Y", "city: [没闭合\nuser_address: 小测\n"),
    "顶层是列表": ("Y", "- 一\n- 二\n"),
}


@pytest.mark.parametrize("method", ["GET", "PUT"])
@pytest.mark.parametrize("kind,content", list(BROKEN.values()), ids=list(BROKEN))
def test_生活设定_文件本身有问题_回500_文案如约_不写(adapter, life, method, kind, content):
    bot = life_bot(adapter, life, text=None if content in (None, "dir") else content)
    if content == "dir":
        bot.path.mkdir()
    before = snapshot(life)
    body = {"values": {"city": "苏州"}} if method == "PUT" else au.NO_BODY
    got = adapter.expect_error(adapter.call(method, life_url("botalpha"), body), 500, "internal")
    if kind == "Y":
        assert Y_MSG.match(got["message"]), got["message"]
    else:
        assert got["message"] == MSG[kind], got["message"]
    assert snapshot(life) == before, "出错时动了文件、备份或锁"


# ============================================================ 写锁、停用、只读、备份

def test_生活设定PUT_外部持有写锁_回409_busy_文件不变_放锁后能保存(adapter, life):
    bot = life_bot(adapter, life)
    life.locks.mkdir(mode=0o700)
    fd = os.open(str(life.lock), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        original = bot.path.read_bytes()
        body = adapter.expect_error(put(adapter, {"city": "苏州"}), 409, "busy")
        assert body["message"] == BUSY_LOCK, body
        assert bot.path.read_bytes() == original and backups_of(life) == []
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    adapter.ok(put(adapter, {"city": "苏州"}))
    assert parsed(bot.path)["city"] == "苏州", "放锁之后应能照常保存"


def test_生活设定_停用的bot_GET照常_PUT照常改好(adapter, life):
    bot = life_bot(adapter, life, enabled=False)
    assert adapter.ok(get(adapter))["values"]["city"] == "杭州"
    body = adapter.ok(put(adapter, {"city": "苏州"}))
    assert body.get("message") == SAVED, body
    assert parsed(bot.path)["city"] == "苏州"


READ_ONLY = {
    "bot自己的yml顶层也写了": ({"city": "上海"}, SAMPLE, RO_OWN_YML, "上海"),
    "文件里的值认不出": ({}, SAMPLE.replace("city: 杭州\n", "city:\n  - 1\n  - 2\n"), RO_ODD, ""),
}


@pytest.mark.parametrize("extra,text,help_text,value", list(READ_ONLY.values()), ids=list(READ_ONLY))
def test_生活设定_只读字段_GET给固定说明_PUT回422用同一句(adapter, life, extra, text, help_text, value):
    bot = life_bot(adapter, life, text=text, extra=extra)
    got = adapter.ok(get(adapter))
    city = next(f for f in got["fields"] if f["key"] == "city")
    assert city.get("help") == help_text and got["values"]["city"] == value, (city, got["values"]["city"])
    original = bot.path.read_bytes()
    body = adapter.expect_error(put(adapter, {"city": "苏州"}), 422, "invalid")
    assert body.get("fields") == {"city": help_text}, body
    assert bot.path.read_bytes() == original


def test_生活设定PUT_备份只留最近2份_不认的文件不动(adapter, life):
    life_bot(adapter, life)
    d = life.backups / "botalpha"
    d.mkdir(parents=True)
    old = ["20200101-00000%d.yml" % i for i in range(6)]
    for name in old + ["keep-me.txt", "20200101-000000.yml.bak"]:
        (d / name).write_text("旧备份", encoding="utf-8")
    adapter.ok(put(adapter, {"city": "苏州"}))
    left = backups_of(life)
    assert len(left) == 2 and left[:1] == old[5:], "应只留最近 2 份：%r" % left
    assert (d / "keep-me.txt").exists() and (d / "20200101-000000.yml.bak").exists(), "不认的文件被删了"


# ============================================================ 鉴权与敏感信息

@pytest.mark.parametrize("method,suffix,body", [("GET", "life", au.NO_BODY),
                                                ("PUT", "life", {"values": {"city": "苏州"}}),
                                                ("POST", "notify", {"source": "user_moment",
                                                                    "text": "通知", "key": "user-moment:1"})],
                         ids=["生活设定GET", "生活设定PUT", "投递通知"])
def test_鉴权_v04新接口没带令牌_回固定401_什么都不做(adapter, life, method, suffix, body):
    gw = adapter.gateway().on("POST", "/v1/inject", 200, {"ok": True, "duplicate": False})
    bot = life_bot(adapter, life)
    yml = json.loads(bot.yml.read_text(encoding="utf-8"))
    yml.update({"chat_id": int(au.CHAT_ID)})
    bot.yml.write_text(json.dumps(yml), encoding="utf-8")
    (adapter.sandbox.dsh_home / "bots" / "botalpha" / "state" / "api.port").write_text("%d\n" % gw.port)
    before = snapshot(life)
    au.assert_unauthorized(adapter.call(method, "%s/botalpha/%s" % (au.BOTS, suffix), body, auth=None))
    assert gw.requests == [] and snapshot(life) == before, "没过鉴权却有了动作"


def test_泄密_生活设定各种响应和日志里_没有令牌密钥聊天号和文件路径(adapter, life, caplog, capfd):
    caplog.set_level(logging.DEBUG)
    life_bot(adapter, life)
    life_bot(adapter, life, "botbravo", life_config="~/claudebotlife/configs/botbravo.yml", text=None)
    life_bot(adapter, life, "botcharlie", text=None)
    resps = [get(adapter), put(adapter, {"city": "苏州"}), put(adapter, {"city": "苏\n州", "chat_id": 1}),
             get(adapter, "botbravo"), get(adapter, "botcharlie")]
    assert [r.status_code for r in resps] == [200, 200, 422, 501, 500], \
        "前提没满足（各条路径没真走到）：%r" % [r.status_code for r in resps]
    texts = [au.response_text(r) for r in resps]
    out, err = capfd.readouterr()
    texts += [caplog.text, out, err]
    assert "chat_id" not in texts[0], "GET 带出了注册键"
    for i, text in enumerate(texts):
        for name, secret in au.SECRETS.items():
            assert secret not in text, "第 %d 段（响应或日志）里出现了%s" % (i + 1, name)
        if i < 5:
            assert str(life.dir) not in text, "第 %d 个响应里带了文件路径" % (i + 1)
