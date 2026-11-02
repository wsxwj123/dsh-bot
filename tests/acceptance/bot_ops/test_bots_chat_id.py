# -*- coding: utf-8 -*-
"""bots-chat-id 验收：GET /adapter/v1/bots 每一行多一个 chat_id（黑盒）。

依据只有 `.devflow/bots-chat-id-接口.md` 的行为清单 1 到 9 与错误契约。不读实现。
假 bot 的摆法借用本目录现成支架（adapter_util.AdapterEnv.bot / set_cfg）：配置文件
是 <HUB_CONFIGS_DSH_DIR>/<bot_id>.yml，内容按 JSON 写（YAML 的子集），所以整数、
字符串、布尔、小数、列表、对象都能原样落进配置顶层的 chat_id。

注意：adapter.bot 会在频道目录 .env 里放一个假 TELEGRAM_CHAT_ID。接口说 chat_id
只取配置文件顶层，所以「配置里没写」时必须是 null，不能从 .env 兜底（第 4 条顺带验）。
"""
import pytest

from . import adapter_util as au
from .adapter_util import adapter, console_guard  # noqa: F401  夹具

GOOD_ID = 123456789
OTHER_ID = 987654321


def _bot_with_chat_id(adapter, bot_id, value, **kw):
    bot = adapter.bot(bot_id, **kw)
    au.set_cfg(bot, chat_id=value)
    return bot


def _chat_ids(rows, ids):
    """{bot_id: chat_id}；行里没有这个键记成字符串 '<缺键>'，与 null 区分开。"""
    return {i: rows[i].get("chat_id", "<缺键>") for i in ids}


# 1
def test_chat_id_配置为整数_原样返回整数(adapter):
    _bot_with_chat_id(adapter, "botalpha", GOOD_ID)
    row = au.rows_by_id(adapter)["botalpha"]
    assert "chat_id" in row, "行里应有 chat_id 键：%r" % (row,)
    assert row["chat_id"] == GOOD_ID and type(row["chat_id"]) is int, \
        "应为整数 %d，实际 %r（%s）" % (GOOD_ID, row["chat_id"], type(row["chat_id"]).__name__)


# 2
def test_chat_id_配置为纯数字串_转成同值整数(adapter):
    _bot_with_chat_id(adapter, "botalpha", str(OTHER_ID))
    row = au.rows_by_id(adapter)["botalpha"]
    assert row.get("chat_id", "<缺键>") == OTHER_ID and type(row["chat_id"]) is int, \
        "数字串应转成整数 %d，实际 %r" % (OTHER_ID, row.get("chat_id", "<缺键>"))


# 3
def test_chat_id_停用的bot照样带(adapter):
    adapter.launchctl.set_default(installed=False)    # 停着的 bot
    _bot_with_chat_id(adapter, "botalpha", GOOD_ID, enabled=False)
    row = au.rows_by_id(adapter)["botalpha"]
    assert row["enabled"] is False, "该行 enabled 应为假：%r" % (row,)
    assert row.get("chat_id", "<缺键>") == GOOD_ID and type(row["chat_id"]) is int, \
        "停用的 bot 也应带 chat_id=%d，实际 %r" % (GOOD_ID, row.get("chat_id", "<缺键>"))


# 4
def test_chat_id_配置里没写_有键且为null_不从频道env兜底(adapter):
    adapter.bot("botalpha")       # 配置无 chat_id；频道 .env 里有假 TELEGRAM_CHAT_ID
    row = au.rows_by_id(adapter)["botalpha"]
    assert "chat_id" in row, "没配置时也应有 chat_id 键：%r" % (sorted(row),)
    assert row["chat_id"] is None, "没配置时应为 null，实际 %r" % (row["chat_id"],)


# 5：非字符串的不合规值。每只 bot 各放一个，一次请求里同时验；再放一只合规的，验列表不被拖垮。
BAD_NON_STR = {
    "botzero": 0,
    "botneg": -GOOD_ID,
    "bottrue": True,
    "botfalse": False,          # 接口「其余一律 null」里点名的 false
    "botfloat": 123456789.0,
}


def test_chat_id_零_负数_布尔_小数_都为null_列表照常200(adapter):
    for bot_id, value in BAD_NON_STR.items():
        _bot_with_chat_id(adapter, bot_id, value)
    _bot_with_chat_id(adapter, "botgood", GOOD_ID)
    rows = au.rows_by_id(adapter)          # 内含 200 与 ok:true 的断言
    got = _chat_ids(rows, list(BAD_NON_STR) + ["botgood"])
    want = dict({k: None for k in BAD_NON_STR}, botgood=GOOD_ID)
    assert got == want, "不合规值应为 null、合规那只不受影响：实际 %r" % (got,)
    assert type(rows["botgood"]["chat_id"]) is int


# 6：字符串与容器类的不合规值。全角、阿拉伯-印度数字是 Python int()/isdigit() 都会放行的陷阱。
BAD_STR_OR_CONTAINER = {
    "botlead": " 123456789",
    "bottrail": "123456789 ",
    "botplus": "+123456789",
    "botminus": "-123456789",    # 接口「其余一律 null」里点名的 "-123"
    "botempty": "",
    "botfull": u"１２３",
    "botarabic": u"١٢٣",         # 其它非 ASCII 数字
    "botlist": [GOOD_ID],
    "botdict": {"id": GOOD_ID},  # 接口点名的「对象」
}


def test_chat_id_带空白正负号_空串_非ASCII数字_列表对象_都为null_列表照常200(adapter):
    for bot_id, value in BAD_STR_OR_CONTAINER.items():
        _bot_with_chat_id(adapter, bot_id, value)
    _bot_with_chat_id(adapter, "botgood", str(GOOD_ID))
    rows = au.rows_by_id(adapter)
    got = _chat_ids(rows, list(BAD_STR_OR_CONTAINER) + ["botgood"])
    want = dict({k: None for k in BAD_STR_OR_CONTAINER}, botgood=GOOD_ID)
    assert got == want, "不合规值应为 null、合规那只不受影响：实际 %r" % (got,)


# 7
def test_chat_id_多只bot各取各的_不串(adapter):
    _bot_with_chat_id(adapter, "botalpha", GOOD_ID)
    _bot_with_chat_id(adapter, "botbravo", OTHER_ID)
    adapter.bot("botcharlie")      # 没配置的那只不能沾邻居的值
    rows = au.rows_by_id(adapter)
    got = _chat_ids(rows, ["botalpha", "botbravo", "botcharlie"])
    assert got == {"botalpha": GOOD_ID, "botbravo": OTHER_ID, "botcharlie": None}, got


# 8（改动前就成立）
def test_chat_id_其余行字段与没配chat_id时逐键一致(adapter):
    adapter.launchctl.set_default(installed=False)    # 停着，行内容与时间无关、可重复
    bot = adapter.bot("botalpha", enabled=False, display_name="Alpha")
    au.set_cfg(bot, telegram_username="alpha_cfg_bot")
    before = au.rows_by_id(adapter)["botalpha"]
    au.set_cfg(bot, chat_id=GOOD_ID)
    after = au.rows_by_id(adapter)["botalpha"]

    def strip(r):
        return {k: v for k, v in r.items() if k != "chat_id"}

    assert strip(after) == strip(before), \
        "配上 chat_id 后其它键不该变：\n之前 %r\n之后 %r" % (strip(before), strip(after))
    missing = (au.BOT_KEYS | au.ROW_KEYS_V10S) - set(after)
    assert not missing, "原有的行字段缺了：%r" % (missing,)
    assert (after["bot_id"], after["display_name"], after["enabled"], after["polling"],
            after["username"], after["moved_to"]) == \
        ("botalpha", "Alpha", False, False, "alpha_cfg_bot", None), after


# 9（改动前就成立）
@pytest.mark.parametrize("auth", ["Bearer " + au.WRONG_TOKEN, None], ids=["令牌不对", "不带令牌"])
def test_chat_id_令牌不对_401固定回体_不含chat_id(adapter, auth):
    _bot_with_chat_id(adapter, "botalpha", GOOD_ID)
    resp = adapter.call("GET", au.BOTS, auth=auth)
    au.assert_unauthorized(resp)       # 401 且回体与改动前那份一字不差
    text = au.response_text(resp)
    assert "chat_id" not in text, "401 响应里不该出现 chat_id"
    assert str(GOOD_ID) not in text, "401 响应里不该出现配置里的编号"
