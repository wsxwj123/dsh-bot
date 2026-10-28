# -*- coding: utf-8 -*-
"""生活设定（约定 5.6）：适配服务 ``GET`` / ``PUT /adapter/v1/bots/{bot_id}/life`` 背后的逻辑。

依据 ``.devflow/INTERFACE-生活设定.md``（下文节号照它）。适配服务只管鉴权、路由、读请求体，
调这里的 ``view`` 与 ``save``；失败一律抛 ``LifeError``，由适配服务翻成约定的失败形状。

- 改的只有一个文件：这只 bot 自己 yml 顶层 ``life_config`` 指向的那份，而且必须直接放在允许目录
  （``DSH_CONSOLE_LIFE_DIR``，缺省 ``<DSH_BOT_HOME>/life``）里；子目录、软链、硬链接、与别的 bot 共用都不改。
  路径先只按字符串判，落在允许目录之外的（另一套系统的目录、频道目录、配置根）连 ``stat`` 都不做。
- 写只换被改的那几个顶层键各自那一段，段外字节不动；写前在内存里自检，拿跨进程锁、锁内重读比对，
  先备份再原子替换。
- 不用 ``config_loader.load_bot``（它会打开 life_config 指向的另一套系统的文件），不导入
  ``generators``（会带进数据库），事件类别直接读仓库根的 ``events.yaml``。
- 报错与响应里不放路径、文件内容、PyYAML 的原话。
"""
import contextlib
import datetime
import math
import os
import re
import stat
import tempfile

import yaml

from moments import config_sources

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 生活设定文件的大小上限（2.3 情形 B）
MAX_FILE = 1024 * 1024
#: 每只 bot 的备份只留最近这么多份（6.5），只认这种名字
BACKUP_KEEP = 2
BACKUP_NAME_RE = re.compile(r"^[0-9]{8}-[0-9]{6}(-[0-9]{2})?\.yml\Z")
#: events.yaml 读不出类别时，「会遇到的生活事件」每项按这个字数检查（3.2）
EVENT_MAX_LEN = 40

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
#: 读生活设定文件：软链打不开；FIFO 之类不会把请求卡住（之后 fstat 再确认是普通文件）
_READ_FLAGS = os.O_RDONLY | _NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | _CLOEXEC

# 2.3 各情形的话
NO_FILE = "这只 bot 没有单独的生活设定文件，不能在这里改"                    # N
NOT_ALLOWED = "这只 bot 的生活设定文件不在允许修改的位置，不能在这里改"       # O
SHARED = "这只 bot 的生活设定文件和别处共用，改了会连带别处，不能在这里改"     # S
MISSING = "这只 bot 的生活设定文件不见了，请检查它的配置"                    # M
UNREADABLE = "这只 bot 的生活设定文件读不出来"                              # B
BAD_YAML = "这只 bot 的生活设定文件格式有错"                                # Y
# 第 4 节：只读字段的说明
RO_OWN_YML = "这只 bot 自己的配置里也写了这一项，并且以那边为准，这里只能看"
RO_ODD = "配置文件里这一项有这里管不了的写法，这里只能看，要改请直接改配置文件"
# 5.1 PUT 各步的话
INVALID = "有几项没填对"
NO_CHANGE = "没有改动"
ODD_SYNTAX = "这只 bot 的生活设定文件写法特殊，这里改不了，请直接改配置文件"
BUSY = "另一次保存正在进行，这次没有保存，请稍后重试"
STALE = "生活设定文件刚被别处改过，这次没有保存，请刷新后重试"
UNSAFE = "这次改动没能安全写进去，文件没有动"
BACKUP_FAILED = "备份失败，这次没有保存"
WRITE_FAILED = "写入失败，文件没有动"
SAVED = "已保存，不用重启，各项在下次用到时生效"


class LifeError(Exception):
    """按约定的失败形状回的拒绝：HTTP 状态码、``code``、给人看的一句话；422 带 ``fields``。"""

    def __init__(self, status, code, message, fields=None):
        super().__init__(code)
        self.status, self.code, self.message, self.fields = status, code, message, fields


def _unsupported(message):
    return LifeError(501, "unsupported", message)


def _internal(message):
    return LifeError(500, "internal", message)


# ---------------------------------------------------------------- 字段表（3.2）
#
# 下划线开头的键只在这里用，不往外给：_int 整数；_required 必填；_check 内容格式；
# _bool 文件里是布尔（页面上是「是」「否」）；_events 选项来自 events.yaml。

_DATE, _MONTHDAY, _SPAN, _URL = "date", "monthday", "span", "url"

FIELDS = [
    {"key": "persona_summary", "label": "人设摘要", "type": "textarea",
     "help": "判断要不要主动说话、情绪怎么变时参考的人物简介，不改聊天时的人设。", "max_len": 4000},
    {"key": "speaking_threshold", "label": "什么时候主动说话", "type": "textarea",
     "help": "写给判断程序看，说明什么情况下值得主动找用户。", "max_len": 4000},
    {"key": "user_address", "label": "主动消息里怎么称呼用户", "type": "text",
     "help": '不填就叫"用户"。', "max_len": 40},
    {"key": "birthday", "label": "生日", "type": "text", "help": "用来算年龄和年级。",
     "placeholder": "YYYY-MM-DD", "max_len": 10, "_check": _DATE},
    {"key": "schooling", "label": "学籍", "type": "object",
     "help": "两项都填才算在读，到时候自动升年级和毕业。", "item_fields": [
         {"key": "stage", "label": "学段", "type": "text", "options": ["高中", "大学"]},
         {"key": "enrollment_year", "label": "入学年份", "type": "number", "min": 1900, "max": 2099,
          "_int": True}]},
    {"key": "school_calendar", "label": "校历", "type": "object",
     "help": "决定什么时候放暑假、寒假和提前返校，全部不填就不跟校历走。", "item_fields": [
         {"key": "summer_start", "label": "暑假开始", "type": "text", "placeholder": "MM-DD",
          "max_len": 5, "_check": _MONTHDAY},
         {"key": "summer_end", "label": "暑假结束", "type": "text", "placeholder": "MM-DD",
          "max_len": 5, "_check": _MONTHDAY},
         {"key": "early_return", "label": "提前返校", "type": "text", "placeholder": "MM-DD",
          "max_len": 5, "_check": _MONTHDAY},
         {"key": "winter_days", "label": "春节前后各放几天寒假", "type": "number", "min": 0, "max": 182,
          "_int": True}]},
    {"key": "sleep_hours", "label": "睡觉时段", "type": "string_list",
     "help": "这些时段里不主动说话，作息按在睡觉算。", "placeholder": "23:30-07:00", "max_items": 6,
     "_check": _SPAN},
    {"key": "recurring_activities", "label": "日常作息", "type": "object_list_map",
     "help": "按时段写这一天在做什么，表里没写到的时段按空闲或睡觉算。", "max_items": 40,
     "groups": [{"key": "weekday", "label": "工作日"}, {"key": "weekend", "label": "周末和节假日"},
                {"key": "break", "label": "寒暑假"}, {"key": "early_return", "label": "提前返校期间"}],
     "item_fields": [
         {"key": "name", "label": "在做什么", "type": "text", "max_len": 40, "_required": True},
         {"key": "when", "label": "时段", "type": "text", "placeholder": "08:00-12:00", "max_len": 11,
          "_required": True, "_check": _SPAN},
         {"key": "state", "label": "状态", "type": "text", "max_len": 40,
          "help": "free 是空闲，sleeping 是睡觉，其他写法都算在忙，不填算空闲"},
         {"key": "description", "label": "说明", "type": "textarea", "max_len": 500},
         {"key": "interruptible", "label": "能不能被打断", "type": "text", "options": ["是", "否"],
          "help": "不填算能", "_bool": True}]},
    {"key": "personal_hobbies", "label": "爱好", "type": "object_list",
     "help": "空闲时会随机想起来做的事。", "max_items": 40, "item_fields": [
         {"key": "name", "label": "名字", "type": "text", "max_len": 40, "_required": True},
         {"key": "probability_per_roll", "label": "每次想起来的概率", "type": "number", "min": 0, "max": 1,
          "_required": True},
         {"key": "effect", "label": "做这件事时的样子", "type": "textarea", "max_len": 500},
         {"key": "activity_type", "label": "活动类型", "type": "text",
          "options": ["reading", "cooking", "browse", "search", "observe", "selfcare"],
          "help": "情绪低落时按类型挑爱好做，不填就按名字猜"},
         {"key": "prefer_channel", "label": "只在私聊里提", "type": "text", "options": ["dm"]}]},
    {"key": "hobby_cooldown_hours", "label": "两次想起爱好至少隔几小时", "type": "number",
     "help": "不填按 4 小时。", "min": 0, "max": 168, "_int": True},
    {"key": "event_categories", "label": "会遇到的生活事件", "type": "string_list",
     "help": "只有选中的类别里的事件会随机发生。", "_events": True},
    {"key": "city", "label": "所在城市", "type": "text", "help": "用来查天气，不填就不查。", "max_len": 40},
    {"key": "interest_keywords", "label": "关心的话题", "type": "string_list",
     "help": "新闻标题里有这些词才会拿来当话题。", "max_len": 40, "max_items": 60},
    {"key": "rss_feeds", "label": "订阅的新闻源", "type": "string_list",
     "help": "生活程序会定时去读这些地址。", "max_len": 500, "max_items": 30, "_check": _URL},
]
_FIELD = {f["key"]: f for f in FIELDS}


def event_options():
    """仓库根 ``events.yaml`` 里各事件的 ``category``，去重、按第一次出现的顺序；读不出或一个都没有回 None。"""
    try:
        with open(os.path.join(_REPO_ROOT, "events.yaml"), encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception:                       # noqa: BLE001  读不出就不给选项（3.2）
        return None
    found = []
    for event in data if isinstance(data, list) else []:
        category = event.get("category") if isinstance(event, dict) else None
        if isinstance(category, str) and category and category not in found:
            found.append(category)
    return found or None


def _public(spec):
    return {k: v for k, v in spec.items() if not k.startswith("_")}


def _fields(readonly, options):
    """GET 的 ``fields``：照字段表的顺序；只读字段的 ``help`` 换成固定说明。"""
    out = []
    for field in FIELDS:
        item = _public(field)
        if "item_fields" in field:
            item["item_fields"] = [_public(sub) for sub in field["item_fields"]]
        if field.get("_events"):
            item.update({"options": options} if options else {"max_len": EVENT_MAX_LEN})
        if field["key"] in readonly:
            item["help"] = readonly[field["key"]]
        out.append(item)
    return out


# ---------------------------------------------------------------- 定位（第 2 节）

def _within(path, base):
    return path == base or path.startswith(base.rstrip(os.sep) + os.sep)


def _off_limits(path, user_home, dsh_home, forbidden):
    """2.1：允许目录不能是家目录、根目录、本系统家目录本身，不能落在禁区里，也不能是本系统家目录下
    除 ``life`` 这一层以外的地方。所有参数都是规范过的绝对路径写法。"""
    if path in (user_home, os.sep) or any(_within(path, base) for base in forbidden):
        return True
    return _within(path, dsh_home) and path != os.path.join(dsh_home, "life")


def _forbidden():
    """2.1 的判据：``(用户家目录, 本系统家目录, 禁区)``，都是规范过的绝对路径写法。"""
    dsh_home = os.path.normpath(os.path.abspath(str(config_sources.dsh_home())))
    user_home = os.path.normpath(os.path.abspath(os.path.expanduser("~")))
    forbidden = [os.path.join(user_home, "claudebotlife"), os.path.join(user_home, ".claude"),
                 str(config_sources.legacy_dir())]
    configs = config_sources.dsh_dir()
    if configs is not None:
        forbidden.append(str(configs))
    return user_home, dsh_home, [os.path.normpath(os.path.abspath(p)) for p in forbidden]


def planned_dir():
    """允许目录的写法，只按字符串判（不碰文件系统，目录还没建出来也回它）；写法本身不合格回 None。

    给新建 bot 用：目录不在时先拿这个写法建出来（0700），建完再用 ``allowed_dir`` 复核真实路径。"""
    user_home, dsh_home, forbidden = _forbidden()
    raw = (os.environ.get("DSH_CONSOLE_LIFE_DIR") or "").strip()
    written = os.path.expanduser(raw) if raw else os.path.join(dsh_home, "life")
    if not os.path.isabs(written):
        return None
    written = os.path.normpath(written)
    return None if _off_limits(written, user_home, dsh_home, forbidden) else written


def allowed_dir():
    """允许目录 ``(写法, 真实路径)``；没有允许目录回 None（所有 bot 都按情形 O）。每次请求现读环境变量。"""
    written = planned_dir()
    if written is None:
        return None
    user_home, dsh_home, forbidden = _forbidden()
    if not os.path.isdir(written):
        return None
    # 再拿真实路径复核：软链绕进禁区的同样不算（禁区本身是软链的，按它指向的真实位置比）
    real = os.path.realpath(written)
    if _off_limits(real, user_home, dsh_home, forbidden) or _off_limits(
            real, os.path.realpath(user_home), os.path.realpath(dsh_home),
            [os.path.realpath(p) for p in forbidden]):
        return None
    return written, real


def _written_path(raw, allowed):
    """2.2 第 1 到 5 步：只看 ``life_config`` 的字符串，不碰文件系统。返回规范过的绝对路径。"""
    if not isinstance(raw, str) or not raw.strip():
        raise _unsupported(NO_FILE)
    path = os.path.expanduser(raw.strip())
    parts = path.split(os.sep)
    if not os.path.isabs(path) or "." in parts or ".." in parts or "\x00" in path:
        raise _unsupported(NOT_ALLOWED)
    path = os.path.normpath(path)
    name = os.path.basename(path)
    if os.path.dirname(path) not in allowed or not name.endswith(".yml") \
            or name.startswith(("_", ".")):
        raise _unsupported(NOT_ALLOWED)
    return path


def _lstat(path):
    """2.2 第 6 步。"""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise _internal(MISSING) from None
    except OSError:
        raise _internal(UNREADABLE) from None
    if stat.S_ISLNK(st.st_mode):
        raise _unsupported(SHARED)
    if not stat.S_ISREG(st.st_mode):
        raise _internal(UNREADABLE)
    if st.st_nlink > 1:
        raise _unsupported(SHARED)
    return st


def locate(bot, bots):
    """第 2 节：这只 bot 的生活设定文件 → ``(允许目录真实路径下的那个路径, lstat 结果)``。

    ``bot`` / ``bots`` 是适配服务 ``_own_bots`` 的条目（``cfg`` 是 bot 自己的 yml）；``bots`` 是本系统
    整份名单，停用的也在里面（第 8 步）。不成立抛 LifeError（2.3 的 N、O、S、M、B）。
    """
    allowed = allowed_dir()
    if allowed is None:
        raise _unsupported(NOT_ALLOWED)
    path = _written_path(bot["cfg"].get("life_config"), allowed)
    st = _lstat(path)
    real = os.path.join(allowed[1], os.path.basename(path))
    if os.path.realpath(path) != real:                                  # 第 7 步
        raise _unsupported(NOT_ALLOWED)
    for other in bots:                                                  # 第 8 步
        if other["id"] == bot["id"]:
            continue
        try:
            theirs = os.lstat(_written_path(other["cfg"].get("life_config"), allowed))
        except (LifeError, OSError):
            continue
        if stat.S_ISREG(theirs.st_mode) and (theirs.st_dev, theirs.st_ino) == (st.st_dev, st.st_ino):
            raise _unsupported(SHARED)
    return real, st


# ---------------------------------------------------------------- 读

def _read(path, expect=None):
    """``O_NOFOLLOW`` 打开，``fstat`` 确认是普通文件（给了 ``expect`` 还要是同一个 ``(st_dev, st_ino)``），
    读原文字节，最多读到上限多一个字节。读不出、超了上限回 ``(None, None)``。"""
    try:
        fd = os.open(path, _READ_FLAGS)
    except OSError:
        return None, None
    try:
        f = os.fdopen(fd, "rb")
    except OSError:                         # 目录在这里抛 IsADirectoryError，传进去的 fd 它不关：自己关，不漏
        with contextlib.suppress(OSError):
            os.close(fd)
        return None, None
    with f:
        try:
            cur = os.fstat(f.fileno())
            if not stat.S_ISREG(cur.st_mode) or (
                    expect is not None and (cur.st_dev, cur.st_ino) != (expect.st_dev, expect.st_ino)):
                return None, None
            data = f.read(MAX_FILE + 1)
        except OSError:
            return None, None
    return (data, cur) if len(data) <= MAX_FILE else (None, None)


def _yaml_message(err):
    """情形 Y 的话：只取行号，不带 PyYAML 的原话（原话里有文件内容）。"""
    mark = getattr(err, "problem_mark", None) or getattr(err, "context_mark", None)
    return "%s（第 %d 行附近）" % (BAD_YAML, mark.line + 1) if mark is not None else BAD_YAML


def _load(path, st):
    """2.2 第 9 步与 2.3 的 B、Y：返回 ``(原文字节, 顶层映射)``。空文件、只有注释的当作空映射。"""
    data, _cur = _read(path, st)
    if data is None:
        raise _internal(UNREADABLE)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise _internal(UNREADABLE) from None
    try:
        doc = yaml.safe_load(text)
    except Exception as e:                  # noqa: BLE001  YAMLError，以及日期越界这类构造时的 ValueError
        raise _internal(_yaml_message(e)) from None
    if doc is None:
        doc = {}
    if not isinstance(doc, dict):
        raise _internal(BAD_YAML)
    return data, doc


# ---------------------------------------------------------------- 取值（第 4 节）

class _Odd(Exception):
    """文件里的值这里认不出：类型不对，或有字段表外的子项、分组。"""


def _is_num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _finite(value):
    return _is_num(value) and not (isinstance(value, float) and not math.isfinite(value))


def _empty(field):
    kind = field["type"]
    if kind in ("text", "textarea"):
        return ""
    if kind == "number":
        return None
    return [] if kind in ("string_list", "object_list") else {}


def _sub_empty(sub):
    return None if sub["type"] == "number" else ""


def _row_from_file(subs, raw):
    if not isinstance(raw, dict) or not set(raw) <= {sub["key"] for sub in subs}:
        raise _Odd()
    row = {}
    for sub in subs:
        value = raw.get(sub["key"])
        if value is None:
            value = _sub_empty(sub)
        elif sub.get("_bool"):
            if not isinstance(value, bool):
                raise _Odd()
            value = "是" if value else "否"
        elif not (_finite(value) if sub["type"] == "number" else isinstance(value, str)):
            raise _Odd()
        row[sub["key"]] = value
    return row


def _from_file(field, raw):
    """文件里（或 bot 自己 yml 里）的值 → GET 给的值。没有或为 null 给空值；认不出抛 ``_Odd``。"""
    kind = field["type"]
    if raw is None:
        return _empty(field)
    if kind in ("text", "textarea"):
        if field.get("_check") == _DATE and type(raw) is datetime.date:
            return raw.isoformat()          # 不带引号的 YAML 日期
        if isinstance(raw, str):
            return raw
    elif kind == "number":
        if _finite(raw):
            return raw
    elif kind == "string_list":
        if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
            return list(raw)
    elif kind == "object":
        return _row_from_file(field["item_fields"], raw)
    elif kind == "object_list":
        if isinstance(raw, list):
            return [_row_from_file(field["item_fields"], row) for row in raw]
    else:
        groups = [g["key"] for g in field["groups"]]
        if isinstance(raw, dict) and set(raw) <= set(groups):
            out = {}
            for group in groups:
                rows = raw.get(group)
                rows = [] if rows is None else rows
                if not isinstance(rows, list):
                    raise _Odd()
                out[group] = [_row_from_file(field["item_fields"], row) for row in rows]
            return out
    raise _Odd()


def _current(doc, own):
    """GET 此刻给的 ``values``，以及只读字段 ``{键: 固定说明}``（第 4 节）。``own`` 是 bot 自己的 yml。"""
    values, readonly = {}, {}
    for field in FIELDS:
        key = field["key"]
        if key in own:                      # 读配置时 bot 自己 yml 的键盖过生活设定文件（load_bot 的合并顺序）
            readonly[key] = RO_OWN_YML
        try:
            values[key] = _from_file(field, own[key] if key in own else doc.get(key))
        except _Odd:
            values[key] = _empty(field)
            readonly.setdefault(key, RO_ODD)
    return values, readonly


# ---------------------------------------------------------------- 逐键判定（5.2）

_CONTROL = re.compile("[\x00-\x1f\x7f\ud800-\udfff]")           # 落单的代理码同样不收：写不进 UTF-8
_CONTROL_MULTI = re.compile("[\x00-\x08\x0b-\x1f\x7f\ud800-\udfff]")     # 多行文字放过换行与制表符
_DATE_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_MONTHDAY_RE = re.compile(r"([0-9]{2})-([0-9]{2})")
_SPAN_RE = re.compile(r"([0-9]{1,2}):([0-9]{2})-([0-9]{1,2}):([0-9]{2})")


def _row(subs, row):
    """一行（或 object）补齐子项：缺的给子项空值。"""
    return {sub["key"]: row.get(sub["key"], _sub_empty(sub)) for sub in subs}


def _canon(field, value):
    """比较与写入用的规范形（值已过类型检查）：object 与每一行补齐子项，``{}`` 即子项全空；分组对象补齐分组。"""
    kind, subs = field["type"], field.get("item_fields")
    if kind == "object":
        return _row(subs, value)
    if kind == "object_list":
        return [_row(subs, row) for row in value]
    if kind == "object_list_map":
        return {g["key"]: [_row(subs, row) for row in value.get(g["key"], [])] for g in field["groups"]}
    return value


def _row_type_error(subs, row, where):
    names = {sub["key"]: sub for sub in subs}
    if not isinstance(row, dict) or not set(row) <= set(names):
        return "%s只能填这几项：%s" % (where, "、".join(sub["label"] for sub in subs))
    for key, value in row.items():
        sub = names[key]
        if sub["type"] == "number" and not (value is None or _is_num(value)):
            return "%s%s要填一个数，或者留空" % (where and where + "的", sub["label"])
        if sub["type"] != "number" and not isinstance(value, str):
            return "%s%s要填一段文字" % (where and where + "的", sub["label"])
    return None


def _type_error(field, value):
    """5.2 第 2 步：类型不对回一句话，对了回 None。"""
    kind = field["type"]
    if kind in ("text", "textarea"):
        return None if isinstance(value, str) else "要填一段文字"
    if kind == "number":
        return None if value is None or _is_num(value) else "要填一个数，或者留空"
    if kind == "string_list":
        ok = isinstance(value, list) and all(isinstance(item, str) for item in value)
        return None if ok else "要填一组文字"
    subs = field["item_fields"]
    if kind == "object":
        return _row_type_error(subs, value, "")
    if kind == "object_list":
        groups = [("", value)]
    else:
        labels = {g["key"]: g["label"] for g in field["groups"]}
        if not isinstance(value, dict) or not set(value) <= set(labels):
            return "要按分组填，分组只能是：" + "、".join(labels.values())
        groups = [(labels[g], value.get(g, [])) for g in labels]
    for label, rows in groups:
        if not isinstance(rows, list):
            return "%s要填若干行" % label
        for i, row in enumerate(rows, 1):
            why = _row_type_error(subs, row, "%s第 %d 行" % (label, i))
            if why:
                return why
    return None


def _format_error(check, value, label, today):
    """文字内容的格式（日期、月日、时段、地址）。"""
    if check == _DATE:
        m = _DATE_RE.fullmatch(value)
        try:
            day = datetime.date(*map(int, m.groups())) if m else None
        except ValueError:
            day = None
        if day is None:
            return label + "要写成 YYYY-MM-DD，并且是存在的日期"
        if day > today:
            return label + "不能晚于今天"
        if today.year - day.year - ((today.month, today.day) < (day.month, day.day)) > 150:
            return label + "算出来的年龄超过了 150 岁"
    elif check == _MONTHDAY and _monthday(value) is None:
        return label + "要写成 MM-DD，并且是存在的日子"
    elif check == _SPAN:
        m = _SPAN_RE.fullmatch(value)
        if not m or int(m.group(1)) > 23 or int(m.group(3)) > 23 \
                or int(m.group(2)) > 59 or int(m.group(4)) > 59:
            return label + "不是有效的时段，写法如 23:30-07:00"
    elif check == _URL and (not value.startswith(("http://", "https://")) or re.search(r"\s", value)):
        return label + "要以 http:// 或 https:// 开头，不能有空白"
    return None


def _monthday(value):
    """``MM-DD`` → ``(月, 日)``；不是闰年里存在的日子回 None。"""
    m = _MONTHDAY_RE.fullmatch(value)
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    try:
        datetime.date(2000, month, day)     # 2000 年是闰年：02-29 算存在
    except ValueError:
        return None
    return month, day


def _text_error(spec, value, label, today):
    """一段文字按 3.1、3.2 判；``spec`` 是字段或子项，``label`` 是报错时它的叫法（顶层字段为空串）。"""
    if spec["type"] == "text" and "\n" in value:
        return label + "不能换行"
    if (_CONTROL_MULTI if spec["type"] == "textarea" else _CONTROL).search(value):
        return label + "有不能用的控制字符"
    if "max_len" in spec and len(value) > spec["max_len"]:
        return "%s不能超过 %d 个字" % (label, spec["max_len"])
    if not value:
        return label + "不能为空" if spec.get("_required") else None
    if "options" in spec and value not in spec["options"]:
        return "%s只能是%s，或者留空" % (label, "、".join(spec["options"]))
    return _format_error(spec.get("_check"), value, label, today)


def _number_error(spec, value, label):
    if value is None:
        return label + "必须填" if spec.get("_required") else None
    if isinstance(value, float) and not math.isfinite(value):
        return label + "要是一个有限的数"
    if spec.get("_int") and value != int(value):
        return label + "要是整数"
    if "min" in spec and value < spec["min"]:
        return "%s不能小于 %s" % (label, spec["min"])
    if "max" in spec and value > spec["max"]:
        return "%s不能大于 %s" % (label, spec["max"])
    return None


def _row_error(subs, row, where, today):
    """一行（或 object）的每个子项按顺序判，报第一条错。``row`` 已补齐子项。"""
    for sub in subs:
        label = where + sub["label"]
        value = row[sub["key"]]
        why = (_number_error(sub, value, label) if sub["type"] == "number"
               else _text_error(sub, value, label, today))
        if why:
            return why
    return None


def _object_rule(key, row):
    """学籍、校历几项之间的规矩（3.2）。``row`` 已补齐子项，各子项本身已经判过。"""
    if key == "schooling" and (row["stage"] == "") != (row["enrollment_year"] is None):
        return "学段和入学年份要么都填，要么都空"
    if key == "school_calendar":
        start, end, back = (_monthday(row[k]) if row[k] else None
                            for k in ("summer_start", "summer_end", "early_return"))
        if (start is None) != (end is None):
            return "暑假开始和暑假结束要么都填，要么都空"
        if start and start > end:
            return "暑假开始不能晚于暑假结束"
        if back and not (start and start <= back <= end):
            return "填了提前返校，就要把暑假开始和结束都填上，并且提前返校要落在两者之间"
    return None


def _content_error(field, value, today, options):
    """5.2 第 5 步：内容不合 3.1、3.2 的规则回一句话（行和项从 1 数）。值已过类型检查。"""
    kind = field["type"]
    if kind in ("text", "textarea"):
        return _text_error(field, value, "", today)
    if kind == "number":
        return _number_error(field, value, "")
    if kind == "string_list":
        limit = field.get("max_items")
        if limit is not None and len(value) > limit:
            return "最多 %d 项" % limit
        item = {"type": "text", "_check": field.get("_check")}
        if field.get("max_len") or (field.get("_events") and not options):
            item["max_len"] = field.get("max_len") or EVENT_MAX_LEN
        for i, text in enumerate(value, 1):
            where = "第 %d 项" % i
            if text == "":
                return where + "是空的"
            if field.get("_events") and options and text not in options:
                return where + "不在可选的类别里"
            why = _text_error(item, text, where, today)
            if why:
                return why
        return None
    subs = field["item_fields"]
    if kind == "object":
        row = _row(subs, value)
        return _row_error(subs, row, "", today) or _object_rule(field["key"], row)
    if kind == "object_list":
        groups = [("", value)]
    else:
        groups = [(g["label"], value.get(g["key"], [])) for g in field["groups"]]
    for label, rows in groups:
        if len(rows) > field["max_items"]:
            return "%s最多 %d 行" % (label, field["max_items"])
        for i, row in enumerate(rows, 1):
            why = _row_error(subs, _row(subs, row), "%s第 %d 行的" % (label, i), today)
            if why:
                return why
    return None


def _judge(values, current, readonly, options):
    """5.2：按请求里的顺序逐键判，每个键只报第一条错。

    返回 ``(要改的 {键: 规范形的新值}, 出错的 {键: 一句话})``。提交值规范化后等于 GET 此刻给的值的
    直接跳过（先于只读判定：整份 values 原样交回不会因为只读字段被拒）。
    """
    today = datetime.date.today()
    changes, errors = {}, {}
    for key, value in values.items():
        field = _FIELD.get(key)
        why = "没有这一项" if field is None else _type_error(field, value)
        if why is None:
            new = _canon(field, value)
            if new == _canon(field, current[key]):
                continue
            why = readonly.get(key) or _content_error(field, value, today, options)
            if why is None:
                changes[key] = new
                continue
        errors[key] = why
    return changes, errors


# ---------------------------------------------------------------- 生成新文本（6.1 到 6.3）

class _Dumper(yaml.SafeDumper):
    """写新的一段用：列表在父键下缩进两格（与手写的生活文件一个样子）。"""

    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


def _represent_str(dumper, value):
    # 含换行的用字面块；写不成字面块的（制表符、行尾空格等）PyYAML 自己改用引号。
    # 结尾两个以上换行时改用双引号：字面块要写成保留结尾的 |+，原文段后的空行会被它吞进去，自检就拦下了
    if "\n" not in value:
        return dumper.represent_scalar("tag:yaml.org,2002:str", value)
    style = '"' if value.endswith("\n\n") else "|"
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _represent_str)


def _yaml_row(subs, row):
    """一行（或 object）写进文件的样子：按子项顺序，空的不写，「是」「否」写成布尔，整数写成整数。"""
    out = {}
    for sub in subs:
        value = row[sub["key"]]
        if value is None or value == "":
            continue
        if sub.get("_bool"):
            value = value == "是"
        elif sub.get("_int"):
            value = int(value)
        out[sub["key"]] = value
    return out


def _to_yaml(field, value):
    """规范形的新值 → 写进文件的值（6.2）。新值为空回 None：这个键要从文件里去掉（6.1）。"""
    kind = field["type"]
    if kind == "number":
        return None if value is None else (int(value) if field.get("_int") else value)
    if kind in ("text", "textarea", "string_list"):
        return value or None
    subs = field["item_fields"]
    if kind == "object":
        return _yaml_row(subs, value) or None
    if kind == "object_list":
        return [_yaml_row(subs, row) for row in value] or None
    return {g: [_yaml_row(subs, row) for row in rows] for g, rows in value.items() if rows} or None


def _section(key, data):
    """新的一段：单键映射，中文原样、不排序、块风格、不折行。"""
    return yaml.dump({key: data}, Dumper=_Dumper, allow_unicode=True, sort_keys=False,
                     default_flow_style=False, width=float("inf"))


_LINE_END = re.compile("(?<=[\n\x85  ])")


def _lines(text):
    """按 YAML 认的换行切行（回车符在第 10 步已经拒了），行尾保留；第 i 行就是解析器行号 i 的那一行。"""
    lines = _LINE_END.split(text)
    return lines[:-1] if lines and lines[-1] == "" else lines


def _blank_or_comment(line):
    body = line.lstrip(" \t")
    return body.rstrip("\n\x85  ") == "" or body.startswith("#")


def _layout(text, keys):
    """第 10 步：写法特殊的不改（顶层流风格、有回车符、要改的键出现不止一次）。返回顶层映射节点，空文件为 None。"""
    if "\r" in text:
        raise _internal(ODD_SYNTAX)
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    if root is None:
        return None
    if not isinstance(root, yaml.MappingNode) or root.flow_style:
        raise _internal(ODD_SYNTAX)
    for key in keys:
        if sum(isinstance(k, yaml.ScalarNode) and k.value == key for k, _v in root.value) > 1:
            raise _internal(ODD_SYNTAX)
    return root


def _splice(text, root, rendered):
    """6.1：要改的键各自那一段换成新的一段（新值为空就去掉）；文件里没有的追加到末尾。段外字节不动。

    「那一段」从键所在行的行首到下一个顶层键所在行的行首（没有就到文件末尾），段尾连着的空行和注释行
    留在原处。几个键一起改时从后往前换，前面的行号不受影响。
    """
    lines = _lines(text)
    keys = [k for k, _v in root.value] if root is not None else []
    tops = sorted({k.start_mark.line for k in keys})
    spans, appended = [], ""
    for key, data in rendered.items():
        new = "" if data is None else _section(key, data)
        node = next((k for k in keys if isinstance(k, yaml.ScalarNode) and k.value == key), None)
        if node is None:
            appended += new                 # 文件里本来没有：新值为空就什么都不做
            continue
        start = node.start_mark.line
        end = next((line for line in tops if line > start), len(lines))
        while end > start + 1 and _blank_or_comment(lines[end - 1]):
            end -= 1
        spans.append((start, end, new))
    for start, end, new in sorted(spans, reverse=True):
        lines[start:end] = [new]
    out = "".join(lines)
    if appended and out and not out.endswith("\n"):
        out += "\n"
    return out + appended


def _same(a, b):
    """深度相等，并且每一层类型相同（1、1.0、True 不算相同；nan 与 nan 算相同）。"""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, float) and a != a:
        return b != b
    return a == b


def _self_check(old, new_text, changes, rendered):
    """6.3（写盘之前，在内存里）：新文本能解析、顶层是映射；顶层键集合 = 原来的减去去掉的加上新加的；
    改了的键写进去的值与要写的类型、值都相同，规范化后等于提交值；其余顶层键与原来深度相等、类型相同。"""
    try:
        new = yaml.safe_load(new_text)
    except Exception:                       # noqa: BLE001  解析不了就是没通过
        return False
    new = {} if new is None else new
    if not isinstance(new, dict):
        return False
    gone = {k for k, data in rendered.items() if data is None}
    if set(new) != (set(old) - gone) | (set(rendered) - gone):
        return False
    for key, data in rendered.items():
        if data is None:
            continue
        field = _FIELD[key]
        try:
            back = _canon(field, _from_file(field, new[key]))
        except _Odd:
            return False
        if not _same(new[key], data) or back != changes[key]:
            return False
    return all(_same(new[k], old[k]) for k in old if k not in rendered)


# ---------------------------------------------------------------- 写（6.4 到 6.6）

def _private_dir(*parts):
    """逐层建目录，没有的才建、权限 0700，返回最里层。"""
    path = parts[0]
    for part in parts[1:]:
        path = os.path.join(path, part)
        with contextlib.suppress(FileExistsError):
            os.mkdir(path, 0o700)
    return path


@contextlib.contextmanager
def _write_lock():
    """6.4：``<DSH_BOT_HOME>/locks/life.lock`` 上的非阻塞排他锁，所有 bot、本服务的所有进程共用一把。

    Windows 没有 ``fcntl``：降级成不加锁（不能因为拿不到锁就让保存/刷新整条失败）；
    单用户桌面下本来也没有跨进程并发写这一层。
    """
    try:
        import fcntl
    except ImportError:
        yield
        return
    folder = _private_dir(str(config_sources.dsh_home()), "locks")
    fd = os.open(os.path.join(folder, "life.lock"), os.O_RDWR | os.O_CREAT | _NOFOLLOW | _CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LifeError(409, "busy", BUSY) from None
        yield
    finally:
        os.close(fd)                        # 关掉描述符即放锁


def _backup(bot_id, data, kind="life", suffix=".yml"):
    """6.5：原文逐字节存一份到 ``<DSH_BOT_HOME>/backups/<kind>/<bot_id>/``，返回那个目录。

    ``O_CREAT|O_EXCL`` 一次建成、权限 0600；同一秒重名依次加 ``-01`` 到 ``-99``，从这一秒已用的最后一个
    往后接：轮换删掉了这一秒较早的那份时不再拿回它的名字，否则最新的一份排到最前、下次轮换先删它。
    ``bot_id`` 已经与名单精确匹配过（名单只收合格的 bot 编号），可以拼路径。
    适配服务的人设备份（INTERFACE-适配服务 10.2）也用这里：``kind="persona"``、不带后缀。
    """
    folder = _private_dir(str(config_sources.dsh_home()), "backups", kind, bot_id)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    names = [stamp + suffix] + ["%s-%02d%s" % (stamp, i, suffix) for i in range(1, 100)]
    taken = set(os.listdir(folder))
    for name in names[max((i + 1 for i, n in enumerate(names) if n in taken), default=0):]:
        target = os.path.join(folder, name)
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as f:
                os.fchmod(f.fileno(), 0o600)        # 不受 umask 影响
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(target)
            raise
        return folder
    raise FileExistsError("backup names used up")


def _backup_order(name):
    """备份名按时间先后排：``20261015-084153.yml`` 在 ``20261015-084153-01.yml`` 之前。
    直接按字符串排时 ``-01`` 会排到不带序号的那份前面，同一秒存了几份时会删错。"""
    parts = name.split(".", 1)[0].split("-")
    return parts[0], parts[1], int(parts[2]) if len(parts) > 2 else 0


def _rotate(folder, name_re=BACKUP_NAME_RE):
    """6.5：只留最近 2 份，按文件名排序删更早的；只认备份名字（``name_re``）的文件；删失败不报错。"""
    try:
        names = sorted((n for n in os.listdir(folder) if name_re.match(n)), key=_backup_order)
    except OSError:
        return
    for name in names[:-BACKUP_KEEP]:
        with contextlib.suppress(OSError):
            os.unlink(os.path.join(folder, name))


def _replace(path, text, mode):
    """6.6：同目录 ``mkstemp`` → 写全 → ``fsync`` → 权限同原文件 → ``os.replace``；任何一步失败删掉临时文件。"""
    folder, name = os.path.split(path)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix="." + name + "-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
            if hasattr(os, "fchmod"):       # Windows 没有 POSIX 权限位，跳过
                os.fchmod(f.fileno(), mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------- 两个入口

def view(bot, bots):
    """GET（第 4 节）：``{"fields": [...], "values": {...}}``。定位或读文件不成立抛 LifeError。"""
    path, st = locate(bot, bots)
    _data, doc = _load(path, st)
    values, readonly = _current(doc, bot["cfg"])
    return {"fields": _fields(readonly, event_options()), "values": values}


#: 刷新摘要前的备份文件名后缀（接口约定：``<yml>.bak-before-refresh-<YYYYMMDD>``，同日覆盖）
REFRESH_BAK = ".bak-before-refresh-"


def refresh_summary(channel, life_path, today=None):
    """按 ``<channel>/CLAUDE.md`` 重算 ``persona_summary``（life-refresh-summary 接口）。

    两个 HTTP 入口（管理台、适配服务）共用这一份：清洗函数固定调用新建路径的
    ``hub_addbot._persona_summary``，绝不在这里复制一份九步清洗，否则两边会漂。

    - 人设文件不存在 → ``LifeError(422, "no_persona")``，生活设定一个字节都不动；
    - 人设清洗后为空 → 备份后删键，``empty: true``；
    - 摘要没变 → 不写盘、不备份，``changed: false``；
    - 其余 → 备份到 ``<yml>.bak-before-refresh-<YYYYMMDD>``（同一天覆盖），原子改写、
      保留原权限位与换行风格。
    """
    from moments import hub_addbot        # 延迟 import：它在导入期就 import 本模块，顶部会循环

    persona_path = os.path.join(channel, "CLAUDE.md")
    try:
        fd = os.open(persona_path, _READ_FLAGS)
    except FileNotFoundError:
        raise LifeError(422, "no_persona", "这只 bot 还没有人设文件，先写好 CLAUDE.md 再来刷新") from None
    except OSError:
        raise LifeError(500, "io_error", "这只 bot 的人设文件读不出来") from None
    with os.fdopen(fd, "rb") as f:
        try:
            opened = os.fstat(f.fileno())
            if not stat.S_ISREG(opened.st_mode):        # FIFO / 设备：不把它当成空人设
                raise LifeError(500, "io_error", "这只 bot 的人设文件不是普通文件")
            persona_text = f.read().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            raise LifeError(500, "io_error", "这只 bot 的人设文件不是合法的 UTF-8 文本") from None
    summary = hub_addbot._persona_summary(persona_text)
    # 人设正文只在内存里过一遍清洗，绝不进日志、响应或报错文案。

    # 读 → 比较 → 写整个放进 save() 同一把锁，且锁内重新读：与生活设定页的 PUT 并发时，
    # 不会拿锁外的旧快照把对方刚保存的改动整体盖掉。Windows 上 _write_lock 降级为不加锁。
    with _write_lock():
        try:
            before = os.lstat(life_path)
        except FileNotFoundError:
            raise LifeError(422, "no_life_config", "这只 bot 还没有生活设定文件，先建好再来刷新") from None
        except OSError:
            before = None
        if before is None or stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise LifeError(500, "io_error", "这只 bot 的生活设定文件读不出来")
        if before.st_nlink > 1:             # 与别处共用（硬链接）的文件不写
            raise LifeError(500, "io_error", "这只 bot 的生活设定文件和别处共用，这里不改")
        data, current = _read(life_path, before)
        if data is None:
            raise LifeError(500, "io_error", "这只 bot 的生活设定文件读不出来")
        try:
            text = data.decode("utf-8")
            doc = yaml.safe_load(text)
        except UnicodeDecodeError:
            raise LifeError(500, "io_error", "这只 bot 的生活设定文件不是合法的 UTF-8 文本") from None
        except Exception:                   # noqa: BLE001  坏 YAML 的原话带文件内容，不往外带
            raise LifeError(500, "io_error", "这只 bot 的生活设定文件格式有错，请先修好") from None
        doc = {} if doc is None else doc
        if not isinstance(doc, dict):
            raise LifeError(500, "io_error", "这只 bot 的生活设定文件格式有错，请先修好")

        old = doc.get("persona_summary")
        if isinstance(old, str):
            old_summary = old.rstrip("\n")
        elif old is None:
            old_summary = ""
        else:
            old_summary = None              # 认不出的类型：当作不同，照常覆盖
        if old_summary is not None and old_summary == summary:
            return {"ok": True, "changed": False, "preview": "", "message": "摘要没变"}

        # 换行风格跟着原文件：CRLF 的文件先按 LF 改，写回时再还原
        crlf = "\r\n" in text
        work = text.replace("\r\n", "\n") if crlf else text
        try:
            root = _layout(work, ("persona_summary",))
        except LifeError:
            raise LifeError(500, "io_error", "生活设定文件的写法这里改不了，请直接改文件") from None
        new_text = _splice(work, root, {"persona_summary": summary or None})
        if crlf:
            new_text = new_text.replace("\n", "\r\n")

        stamp = today or datetime.date.today().strftime("%Y%m%d")
        backup = life_path + REFRESH_BAK + stamp
        had_backup = os.path.lexists(backup)    # 当天已有备份：失败时不能把它删掉
        try:
            _replace(backup, text, 0o600)   # 原文逐字节；同日覆盖
        except OSError:
            raise LifeError(500, "io_error", "备份生活设定失败，文件没有动") from None
        try:
            _replace(life_path, new_text, stat.S_IMODE(current.st_mode))
        except OSError:
            if not had_backup:              # 这次才建的备份：写失败就不留
                with contextlib.suppress(OSError):
                    os.unlink(backup)
            raise LifeError(500, "io_error", "写生活设定失败，原文件没有动") from None

    if not summary:
        return {"ok": True, "changed": True, "empty": True, "preview": "",
                "message": "人设是空的，已删掉摘要"}
    return {"ok": True, "changed": True, "empty": False, "preview": summary[:80],
            "message": "摘要已按人设更新"}


def save(bot, bots, values):
    """PUT 第 6 步起（5.1）：成功回给人看的那句话，失败抛 LifeError。``values`` 已确认是对象。

    第 2 到 10 步失败时不碰锁、不建备份与临时文件；第 11 到 13 步失败时不建备份与临时文件；
    第 14、15 步失败时生活设定文件不变、不留临时文件（5.3）。
    """
    path, st = locate(bot, bots)                                        # 第 6 步
    data, doc = _load(path, st)                                         # 第 7 步
    current, readonly = _current(doc, bot["cfg"])
    changes, errors = _judge(values, current, readonly, event_options())   # 第 8 步
    if errors:
        raise LifeError(422, "invalid", INVALID, fields=errors)
    if not changes:                                                     # 第 9 步
        return NO_CHANGE
    text = data.decode("utf-8")
    root = _layout(text, changes)                                       # 第 10 步
    rendered = {key: _to_yaml(_FIELD[key], value) for key, value in changes.items()}
    with _write_lock():                                                 # 第 11 步
        again, now = _read(path)                                        # 第 12 步
        if again != data or now.st_nlink != 1:
            raise LifeError(409, "busy", STALE)
        new_text = _splice(text, root, rendered)                        # 第 13 步
        if not _self_check(doc, new_text, changes, rendered):
            raise _internal(UNSAFE)
        try:
            folder = _backup(bot["id"], data)                           # 第 14 步
        except OSError:
            raise _internal(BACKUP_FAILED) from None
        try:
            _replace(path, new_text, stat.S_IMODE(now.st_mode))         # 第 15 步
        except OSError:
            raise _internal(WRITE_FAILED) from None
        _rotate(folder)
    return SAVED
