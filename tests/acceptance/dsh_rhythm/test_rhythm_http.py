# -*- coding: utf-8 -*-
"""dsh 适配服务「情绪与节奏」HTTP 入口验收（INTERFACE 3.2、3.3、3.5 末条）。

只经 `GET/PUT /adapter/v1/bots/{bot_id}/rhythm` 与 `GET /adapter/v1/meta` 观察；
另外只读 INTERFACE 点名的 `moments.hub_config.FIELD_META` 与 `jiwen.engine.Rates`（schema 对表用）。
"""
import dataclasses
import os

import pytest

from . import rhythm_util as ru

DAILY = "moments.daily_post_limit_per_bot"
CONN = "jiwen.rates.connection_per_min"


# ======================================================================== 能力表

def test_能力表_多了bot_rhythm且为true_其余项不变(w):
    body = ru.expect_ok(w.call("GET", ru.META))
    caps = ru.capabilities_of(body)
    assert caps.get("bot.rhythm") is True, "能力表里应有 bot.rhythm: true，实际 %r" % (caps,)
    # rel-init 之后又多了 bot.create.relationship（rel-init 接口第 4 节），一并剔除
    # voicecall-dsh 之后又多了 bot.voicecall.read、bot.notify.voice_call（INTERFACE ③.4，都为 true），一并剔除
    later = ("bot.voicecall.read", "bot.notify.voice_call")
    for key in later:
        assert caps.get(key) is True, "能力表里应有 %s: true，实际 %r" % (key, caps)
    rest = {k: v for k, v in caps.items() if k not in ("bot.rhythm", "bot.create.relationship") + later}
    assert rest == ru.CAPABILITIES_BEFORE, "除 bot.rhythm、bot.create.relationship、bot.voicecall.read、bot.notify.voice_call 外其余能力项应与改动前逐项相同：%r" % (rest,)


# ======================================================================== GET：schema 与 global

def test_GET_schema_恰好32行_顺序键位类型范围默认与文案逐行对表(w):
    from jiwen.engine import Rates
    from moments.hub_config import FIELD_META
    w.bot()
    body = ru.expect_ok(w.get())
    assert body["bot_id"] == ru.BOT
    schema = body["schema"]
    assert len(schema) == 32, "schema 应恰好 32 行，实际 %d" % len(schema)

    rate_order = [k[len("jiwen.rates."):] for k in FIELD_META
                  if k.startswith("jiwen.rates.") and k[len("jiwen.rates."):] in ru.RATE_RANGES]
    assert set(rate_order) == set(ru.RATE_RANGES), "FIELD_META 里应有 21 个速率字段"
    want_keys = (["jiwen.rates." + f for f in rate_order]
                 + ["jiwen.thresholds." + f for f, _ in ru.THRESHOLDS]
                 + [b[0] for b in ru.BEHAVIOR])
    assert [r["key"] for r in schema] == want_keys, "32 行的顺序不对"

    rate_defaults = {f.name: f.default for f in dataclasses.fields(Rates)}
    for row in schema:
        assert ru.ROW_KEYS <= set(row), "行 %s 缺字段 %r" % (row["key"], ru.ROW_KEYS - set(row))
    for i, f in enumerate(rate_order):
        row = schema[i]
        lo, hi = ru.RATE_RANGES[f]
        assert (row["bot_key"], row["type"], row["tab"]) == \
            ("jiwen.rates_override." + f, "float", "emotion"), row
        assert (row["min"], row["max"]) == (lo, hi), row
        assert row["default"] == rate_defaults[f], row
        meta = FIELD_META[row["key"]]
        assert (row["label"], row["unit"]) == (meta["label"], meta["unit"]), row
    for j, (f, dflt) in enumerate(ru.THRESHOLDS):
        row = schema[21 + j]
        assert (row["bot_key"], row["type"], row["tab"], row["min"], row["max"], row["default"]) == \
            ("jiwen.thresholds_override." + f, "float", "emotion", 0.0, 1.0, dflt), row
        meta = FIELD_META[row["key"]]
        assert (row["label"], row["unit"]) == (meta["label"], meta["unit"]), row
    for j, (key, bot_key, lo, hi, dflt, label, unit) in enumerate(ru.BEHAVIOR):
        row = schema[24 + j]
        assert (row["bot_key"], row["type"], row["tab"], row["min"], row["max"], row["default"],
                row["label"], row["unit"]) == \
            (bot_key, "int", "behavior", lo, hi, dflt, label, unit), row


def test_GET_global_取全局配置里合规的值_不合规或缺失回默认并标记(w):
    w.write_global(
        "moments:\n"
        "  daily_post_limit_per_bot: 7\n"
        "  silence_threshold_minutes: 0\n"
        "jiwen:\n"
        "  rates:\n"
        "    connection_per_min: 0.002\n"
        "  thresholds:\n"
        "    notice: abc\n"
        "proactive_max_calls_per_5h: 2000\n")
    w.bot()
    rows = ru.rows_by_key(ru.expect_ok(w.get()))
    got = {k: (rows[k]["global"], rows[k]["global_is_default"]) for k in (
        DAILY, "moments.silence_threshold_minutes", CONN, "jiwen.thresholds.notice",
        "proactive_max_calls_per_5h", "moments.same_kind_hours")}
    assert got == {
        DAILY: (7, False),                                  # 合规 → 用全局
        "moments.silence_threshold_minutes": (30, True),    # 全局层要正整数，0 → 默认
        CONN: (0.002, False),
        "jiwen.thresholds.notice": (0.2, True),             # 转不成数字 → 默认
        "proactive_max_calls_per_5h": (0, True),            # 超出 [0, 1000] → 默认
        "moments.same_kind_hours": (4, True),               # 全局没写 → 默认
    }


def test_GET_全局配置不存在_告警global_unreadable_所有global都是默认(w):
    w.bot()
    w.global_path().unlink()
    body = ru.expect_ok(w.get())
    assert "global_unreadable" in body["warnings"], body["warnings"]
    bad = [r["key"] for r in body["schema"]
           if r["global"] != r["default"] or r["global_is_default"] is not True]
    assert bad == [], "全局读不出时这些行的 global 不是默认：%r" % bad


# ======================================================================== GET：overrides 与告警

def test_GET_旧写法与不合规值_各出一条只带键名的告警_非标量不进overrides(w):
    w.bot(life_text=ru.LIFE_SAMPLE
          + "jiwen:\n  rates:\n    connection_per_min: 0.01\n"
          + "moments_daily_limit: 99\n"
          + "moments_same_kind_hours: [1, 2]\n")
    body = ru.expect_ok(w.get())
    assert body["overrides"].get(CONN) == 0.01
    assert body["overrides"].get(DAILY) == 99, "越界的值也照原值列出：%r" % body["overrides"]
    assert "moments.same_kind_hours" not in body["overrides"], "列表值不该进 overrides"
    want = {"legacy_key:" + CONN, "invalid_value:" + DAILY, "invalid_value:moments.same_kind_hours"}
    assert want <= set(body["warnings"]), body["warnings"]
    for text in body["warnings"]:
        assert "99" not in text and str(w.sb.tmp) not in text, "告警里带了值或路径：%r" % text


def test_bot自己yml顶层写了同一键_GET列出它的值并告警_PUT不让改且文件不动(w):
    bot = w.bot(life_text=ru.LIFE_SAMPLE + "moments_daily_limit: 2\n",
                extra_yml="moments_daily_limit: 9\n")
    body = ru.expect_ok(w.get())
    assert body["overrides"].get(DAILY) == 9, "被自己 yml 盖住的行应取自己 yml 的值：%r" % body["overrides"]
    assert "bot_yml_wins:" + DAILY in body["warnings"], body["warnings"]
    before = ru.file_mark(bot.life)
    resp = w.put({"set": {DAILY: 4}, "remove": [], "version": body["version"]})
    err = ru.expect_error(resp, 422, "invalid", ru.FIELDS_BAD)
    assert err.get("fields") == {DAILY: ru.BOT_YML_WINS}, err
    assert ru.file_mark(bot.life) == before, "被拒了生活设定文件却变了"
    assert w.backups() == [], "被拒了却建了备份"


# ======================================================================== GET/PUT：失败契约

def test_PUT_令牌不对_401固定回体_文件不动(w):
    bot = w.bot()
    before = ru.file_mark(bot.life)
    resp = w.put({"set": {DAILY: 1}, "remove": [], "version": "x"}, token=ru.WRONG_TOKEN)
    assert resp.status_code == 401
    assert ru.body_of(resp) == ru.UNAUTHORIZED
    assert ru.file_mark(bot.life) == before


def test_GET_bot编号是中文_422固定文案_fields为空(w):
    w.bot()
    err = ru.expect_error(w.get("测试露露"), 422, "invalid", ru.BAD_BOT_ID)
    assert err.get("fields") == {}, err


def test_GET_名单里没有这只bot_404_unknown_bot(w):
    w.bot()
    ru.expect_error(w.get("nobody-123"), 404, "unknown_bot", ru.UNKNOWN_BOT)


def _no_life_config(w):
    w.bot(life_text=None)


def _shared_with_other_bot(w):
    shared = w.bot().life
    w.bot(ru.OTHER_BOT, life_path=shared)


def _broken_yaml(w):
    w.bot(life_text="schedule: [1, 2\n  - 坏了\n")


@pytest.mark.parametrize("arrange, status, code, message, prefix", [
    (_no_life_config, 501, "unsupported", ru.NO_LIFE, None),
    (_shared_with_other_bot, 501, "unsupported", ru.SHARED_LIFE, None),
    (_broken_yaml, 500, "internal", None, ru.LIFE_BAD_YAML),
], ids=["没写life_config_501", "与别的bot共用一份_501", "YAML格式有错_500"])
def test_GET_生活设定文件不成立_按情形回固定状态与文案(w, arrange, status, code, message, prefix):
    arrange(w)
    ru.expect_error(w.get(), status, code, message, prefix)


# ======================================================================== PUT：写入

def test_PUT_写行为键为0_末尾追加一行_原有注释中文逐字保留_GET读回0(w):
    bot = w.bot()
    v0 = w.version()
    body = ru.expect_ok(w.put({"set": {DAILY: 0}, "remove": [], "version": v0}))
    assert body["changed"] == [DAILY]
    assert isinstance(body["version"], str) and body["version"] and body["version"] != v0
    text = ru.read_text(bot.life)
    assert text.startswith(ru.LIFE_SAMPLE), "原有内容（注释、空行、行尾注释）应逐字保留在前面"
    assert text[len(ru.LIFE_SAMPLE):].strip() == "moments_daily_limit: 0", repr(text)
    after = ru.expect_ok(w.get())
    assert after["overrides"] == {DAILY: 0}, "写 0 也算设了：%r" % after["overrides"]
    assert after["version"] == body["version"]


def test_PUT_写情绪键_原来没有jiwen节_补出块状三层_整数1写成1点0(w):
    bot = w.bot()
    ru.expect_ok(w.put({"set": {CONN: 1}, "remove": [], "version": w.version()}))
    new_lines = ru.read_text(bot.life).splitlines()
    old_lines = ru.LIFE_SAMPLE.splitlines()
    it = iter(new_lines)
    assert all(line in it for line in old_lines), "原有各行应按原顺序逐字保留"
    added = list(new_lines)
    for line in old_lines:
        added.remove(line)
    assert [a.strip() for a in added] == ["jiwen:", "rates_override:", "connection_per_min: 1.0"], added
    assert ru.load_yaml(bot.life)["jiwen"]["rates_override"]["connection_per_min"] == 1.0


def test_PUT_情绪键已用旧写法_改旧写法那一行_不新建override_其余行不动(w):
    original = ("# 情绪（旧写法）\n"
                "jiwen:\n"
                "  rates:\n"
                "    connection_per_min: 0.001\n"
                "    pride_decay_per_min: 0.0005\n"
                "events: []\n")
    bot = w.bot(life_text=original)
    ru.expect_ok(w.put({"set": {CONN: 0.02}, "remove": [], "version": w.version()}))
    assert ru.read_text(bot.life) == original.replace("connection_per_min: 0.001",
                                                      "connection_per_min: 0.02")


def test_PUT_删掉最后一个情绪覆盖_变空的父节一并删掉_其余字节不变(w):
    w.write_global("jiwen:\n  rates:\n    connection_per_min: 0.003\n")
    bot = w.bot(life_text=("# 头注释\n"
                           "events: []\n"
                           "jiwen:\n"
                           "  rates_override:\n"
                           "    connection_per_min: 0.01\n"
                           "proactive_max_calls_per_5h: 3\n"))
    body = ru.expect_ok(w.put({"set": {}, "remove": [CONN], "version": w.version()}))
    assert body["changed"] == [CONN]
    assert ru.read_text(bot.life) == "# 头注释\nevents: []\nproactive_max_calls_per_5h: 3\n"
    after = ru.expect_ok(w.get())
    assert CONN not in after["overrides"]
    assert ru.rows_by_key(after)[CONN]["global"] == 0.003


def test_PUT_删本来没有的键且写了两遍_changed只出现一次_不写盘不备份版本不变(w):
    bot = w.bot()
    v0 = w.version()
    before = ru.file_mark(bot.life)
    key = "moments.same_kind_hours"
    body = ru.expect_ok(w.put({"set": {}, "remove": [key, key], "version": v0}))
    assert body["changed"] == [key]
    assert body["version"] == v0
    assert ru.file_mark(bot.life) == before, "新文与原文相同，不该写盘"
    assert w.backups() == []


def test_PUT_两人拿同一版本先后保存_后者409_stale_不覆盖前者也不备份(w):
    bot = w.bot()
    v0 = w.version()
    ru.expect_ok(w.put({"set": {DAILY: 2}, "remove": [], "version": v0}))
    after_first = ru.file_mark(bot.life)
    backups_after_first = w.backups()
    resp = w.put({"set": {DAILY: 9}, "remove": [], "version": v0})
    ru.expect_error(resp, 409, "stale", ru.STALE)
    assert ru.file_mark(bot.life) == after_first, "stale 不许改文件"
    assert w.backups() == backups_after_first, "stale 不许建备份"
    assert ru.load_yaml(bot.life)["moments_daily_limit"] == 2


def test_PUT_多个值不合规_一次全报_fields逐字_合规的那项也不写(w):
    bot = w.bot()
    before = ru.file_mark(bot.life)
    req = {"set": {
        DAILY: True,                                   # 布尔不收
        "proactive_max_calls_per_5h": 5.0,             # int 行不收小数
        "moments.min_interval_minutes": "5",           # int 行不收数字串
        "moments.same_kind_hours": 73,                 # 上界 72 外一格
        "proactive_cooldown_min_minutes": 0,           # 下界 1 外一格
        CONN: 1.5,                                     # float 行越界
        "jiwen.thresholds.notice": "0.5",              # float 行不收字符串
        "jiwen.rates.immersion_map": 1,                # 不是 32 个逻辑键之一
        "moments.silence_threshold_minutes": 10,       # 这一项本身合规
    }, "remove": ["moments.no_such_key"], "version": w.version()}
    err = ru.expect_error(w.put(req), 422, "invalid", ru.FIELDS_BAD)
    assert err.get("fields") == {
        DAILY: ru.NEED_INT,
        "proactive_max_calls_per_5h": ru.NEED_INT,
        "moments.min_interval_minutes": ru.NEED_INT,
        "moments.same_kind_hours": "超出允许范围 [0, 72]",
        "proactive_cooldown_min_minutes": "超出允许范围 [1, 960]",
        CONN: "超出允许范围 [0.0, 1.0]",
        "jiwen.thresholds.notice": ru.NEED_NUMBER,
        "jiwen.rates.immersion_map": ru.NOT_HERE,
        "moments.no_such_key": ru.NOT_HERE,
    }, err
    assert ru.file_mark(bot.life) == before, "整单被拒，文件一个字节都不许变"
    assert w.backups() == []


def test_PUT_同一键既设又删_422并点名该键(w):
    bot = w.bot()
    before = ru.file_mark(bot.life)
    key = "moments.same_kind_hours"
    err = ru.expect_error(w.put({"set": {key: 2}, "remove": [key], "version": w.version()}),
                          422, "invalid", ru.SET_AND_REMOVE)
    assert err.get("fields") == {key: ru.SET_AND_REMOVE}, err
    assert ru.file_mark(bot.life) == before


def test_PUT_连写三次_备份只留最近两份_内容是每次写前的原文(w):
    bot = w.bot()
    originals = []
    for n in (1, 2, 3):
        originals.append(bot.life.read_bytes())
        ru.expect_ok(w.put({"set": {DAILY: n}, "remove": [], "version": w.version()}))
    files = w.backups()
    assert len(files) == 2, "每只 bot 只留最近 2 份备份，实际 %r" % [p.name for p in files]
    assert all(ru.BACKUP_NAME.match(p.name) for p in files), [p.name for p in files]
    assert {p.read_bytes() for p in files} == {originals[1], originals[2]}
    if os.name != "nt":
        assert all((p.stat().st_mode & 0o777) == 0o600 for p in files), "备份权限应为 0600"


def test_PUT_文件里是流式写法的非空映射_422写法认不出_文件不动(w):
    bot = w.bot(life_text="events: []\njiwen: {rates_override: {connection_per_min: 0.1}}\n")
    before = ru.file_mark(bot.life)
    err = ru.expect_error(w.put({"set": {CONN: 0.2}, "remove": [], "version": w.version()}),
                          422, "invalid", ru.FIELDS_BAD)
    assert err.get("fields") == {CONN: ru.UNRECOGNIZED}, err
    assert ru.file_mark(bot.life) == before
    assert w.backups() == []
