# -*- coding: utf-8 -*-
"""`rhythm_config.py` 单测（PLAN dsh-rhythm ④ T1）。

逐键盯：bot 设了 / 没设 / 设 0 / 越界 / 文字 / 布尔 / `"60"`，以及全局层三种口径；
再钉住默认值与 `hub_config.FIELD_META`、`prefilter`、`self_initiate` 同名默认值一致
（rhythm_config 不 import 它们，漂移只能靠这里抓）。
"""
import math

import pytest

import rhythm_config as rc

KEYS = list(rc.BEHAVIOR_KEYS)

# 每个键挑一个落在 bot 可写范围内、又不等于默认值与全局值的 N
_VALID_N = {
    "moments.daily_post_limit_per_bot": 7,
    "moments.silence_threshold_minutes": 45,
    "silence_threshold_minutes": 45,
    "moments.min_interval_minutes": 15,
    "moments.same_kind_hours": 9,
    "proactive_max_calls_per_5h": 5,
    "proactive_cooldown_min_minutes": 45,
    "proactive_cooldown_max_minutes": 600,
}
# 合规的全局值（各口径下都合规），用来区分「回退到全局」与「回退到默认」
_GLOBAL_N = {k: 2 for k in KEYS}


def _nest(dotted, value):
    out = cur = {}
    parts = dotted.split(".")
    for part in parts[:-1]:
        cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value
    return out


def _bot(key, raw):
    return {rc.BEHAVIOR_KEYS[key]["bot_key"]: raw}


def _glob(key, raw):
    return _nest(rc.BEHAVIOR_KEYS[key]["global_key"], raw)


# ---------------------------------------------------------------- 表本身


def test_keys_order_and_texts_match_interface():
    # INTERFACE 3.1 行为表从上到下；label / unit 逐字
    assert [(k, s["bot_key"], s["global_key"], s["default"], s["min"], s["max"],
             s["label"], s["unit"], s["type"]) for k, s in rc.BEHAVIOR_KEYS.items()] == [
        ("moments.daily_post_limit_per_bot", "moments_daily_limit",
         "moments.daily_post_limit_per_bot", 3, 0, 50, "每个 bot 每天发帖上限", "条", "int"),
        ("moments.silence_threshold_minutes", "moments_silence_threshold_minutes",
         "moments.silence_threshold_minutes", 30, 0, 1440,
         "刚聊完天后多久内不主动发朋友圈（0 = 不设静默期）", "分钟", "int"),
        ("silence_threshold_minutes", "proactive_silence_threshold_minutes",
         "silence_threshold_minutes", 30, 0, 1440,
         "刚聊完天后多久内不主动找你（0 = 不设静默期）", "分钟", "int"),
        ("moments.min_interval_minutes", "moments_min_interval_minutes",
         "moments.min_interval_minutes", 60, 0, 1440,
         "发圈两条之间至少隔多久（0 = 不设间隔）", "分钟", "int"),
        ("moments.same_kind_hours", "moments_same_kind_hours",
         "moments.same_kind_hours", 4, 0, 72,
         "同类朋友圈之间至少隔多久（0 = 不设限制）", "小时", "int"),
        ("proactive_max_calls_per_5h", "proactive_max_calls_per_5h",
         "proactive_max_calls_per_5h", 0, 0, 1000,
         "主动找你每 5 小时最多几次（0 = 不限）", "次", "int"),
        ("proactive_cooldown_min_minutes", "proactive_cooldown_min_minutes",
         "proactive_cooldown_min_minutes", 30, 1, 960, "主动消息最短冷却", "分钟", "int"),
        ("proactive_cooldown_max_minutes", "proactive_cooldown_max_minutes",
         "proactive_cooldown_max_minutes", 1440, 1, 1440, "主动消息最长冷却", "分钟", "int"),
    ]


def test_defaults_match_field_meta_same_name_keys():
    from moments import hub_config
    shared = [k for k in KEYS if k in hub_config.FIELD_META]
    # 三个同名键都得在，否则这条用例形同虚设
    assert shared == ["moments.daily_post_limit_per_bot",
                      "moments.silence_threshold_minutes", "silence_threshold_minutes"]
    for key in shared:
        assert rc.BEHAVIOR_KEYS[key]["default"] == hub_config.FIELD_META[key]["default"], key


def test_defaults_match_consumers():
    import prefilter
    from scripts import self_initiate as si
    spec = rc.BEHAVIOR_KEYS
    assert spec["moments.silence_threshold_minutes"]["default"] == prefilter.DEFAULT_SILENCE_THRESHOLD_MIN
    assert spec["silence_threshold_minutes"]["default"] == prefilter.DEFAULT_SILENCE_THRESHOLD_MIN
    assert spec["proactive_cooldown_min_minutes"]["default"] * 60 == si.DEFAULT_COOLDOWN_MIN_SEC
    assert spec["proactive_cooldown_max_minutes"]["default"] * 60 == si.DEFAULT_COOLDOWN_MAX_SEC


@pytest.mark.parametrize("key", KEYS)
def test_default_inside_bot_range(key):
    s = rc.BEHAVIOR_KEYS[key]
    assert s["min"] <= s["default"] <= s["max"]
    assert s["min"] <= _VALID_N[key] <= s["max"] and _VALID_N[key] != s["default"]


def test_unknown_key_raises():
    with pytest.raises(KeyError):
        rc.resolve("nope", {}, {})
    with pytest.raises(KeyError):
        rc.global_value("nope", {})
    with pytest.raises(KeyError):
        rc.bot_value_valid("nope", 1)


# ---------------------------------------------------------------- bot 层


@pytest.mark.parametrize("key", KEYS)
def test_bot_set_wins_over_global(key):
    n = _VALID_N[key]
    assert rc.resolve(key, _bot(key, n), _glob(key, _GLOBAL_N[key])) == n
    assert rc.bot_value_valid(key, n) is True


@pytest.mark.parametrize("key", KEYS)
def test_bot_unset_falls_to_global_then_default(key):
    g = _GLOBAL_N[key]
    assert rc.resolve(key, {}, _glob(key, g)) == g
    assert rc.resolve(key, {}, {}) == rc.BEHAVIOR_KEYS[key]["default"]
    # bot_cfg / global_cfg 不是 dict 也不抛
    assert rc.resolve(key, None, None) == rc.BEHAVIOR_KEYS[key]["default"]
    assert rc.resolve(key, ["x"], "bad") == rc.BEHAVIOR_KEYS[key]["default"]


@pytest.mark.parametrize("key", KEYS)
def test_bot_zero(key):
    s = rc.BEHAVIOR_KEYS[key]
    got = rc.resolve(key, _bot(key, 0), _glob(key, _GLOBAL_N[key]))
    if s["min"] == 0:
        assert got == 0                     # 0 是有效值（关闸 / 不限），不能被 or 吃掉
        assert rc.bot_value_valid(key, 0) is True
    else:
        assert got == _GLOBAL_N[key]        # 冷却两键下界是 1，手写 0 按没设
        assert rc.bot_value_valid(key, 0) is False


@pytest.mark.parametrize("key", KEYS)
def test_bot_out_of_range_falls_back(key):
    s = rc.BEHAVIOR_KEYS[key]
    g = _GLOBAL_N[key]
    for raw in (s["min"] - 1, s["max"] + 1, -1, 10 ** 9, str(s["max"] + 1)):
        assert rc.resolve(key, _bot(key, raw), _glob(key, g)) == g, raw
        assert rc.bot_value_valid(key, raw) is False, raw
    # 两端都算在范围内
    for raw in (s["min"], s["max"]):
        assert rc.resolve(key, _bot(key, raw), _glob(key, g)) == raw
        assert rc.bot_value_valid(key, raw) is True


@pytest.mark.parametrize("key", KEYS)
def test_bot_text_bool_and_junk_fall_back(key):
    g = _GLOBAL_N[key]
    junk = ("abc", "", "  ", "30.5", "1e400", "nan", True, False, None,
            float("nan"), float("inf"), -float("inf"), 30.5, [5], {"a": 1})
    for raw in junk:
        assert rc.resolve(key, _bot(key, raw), _glob(key, g)) == g, raw
        assert rc.bot_value_valid(key, raw) is False, raw
    # 全局也没有时落到默认
    assert rc.resolve(key, _bot(key, "abc"), {}) == rc.BEHAVIOR_KEYS[key]["default"]


@pytest.mark.parametrize("key", KEYS)
def test_bot_numeric_string_and_integral_float(key):
    s = rc.BEHAVIOR_KEYS[key]
    g = _GLOBAL_N[key]
    # 字面 "60"：在范围内就认成 60（daily 上限 50，"60" 越界 → 回全局）
    want = 60 if s["min"] <= 60 <= s["max"] else g
    for raw in ("60", " 60 ", "60.0", 60.0):
        got = rc.resolve(key, _bot(key, raw), _glob(key, g))
        assert got == want and type(got) is int, (raw, got)
        assert rc.bot_value_valid(key, raw) is (want == 60), raw
    n = _VALID_N[key]
    assert rc.resolve(key, _bot(key, str(n)), _glob(key, g)) == n


# ---------------------------------------------------------------- 全局层

# (原值, non_negative 期望, positive 期望, in_range 期望)；None 表示回默认
_GLOBAL_CASES = [
    (5, 5, 5, 5),
    (0, 0, None, 0),
    (-1, None, None, None),
    ("10", None, None, None),      # 全局不认数字串（改动前口径，锁定用例要求按 30）
    (True, None, None, None),
    (False, None, None, None),
    (5.0, None, None, None),       # 全局只认真正的 int
    (float("nan"), None, None, None),
    (None, None, None, None),
    ([5], None, None, None),
]
_RULE_COL = {rc.GLOBAL_NON_NEGATIVE: 1, rc.GLOBAL_POSITIVE: 2, rc.GLOBAL_IN_RANGE: 3}


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("case", _GLOBAL_CASES, ids=lambda c: repr(c[0]))
def test_global_layer_rules(key, case):
    s = rc.BEHAVIOR_KEYS[key]
    expect = case[_RULE_COL[s["global_rule"]]]
    # in_range 口径另受 bot 可写范围约束：冷却下界是 1，但冷却走 positive，与这里不冲突
    if expect is not None and s["global_rule"] == rc.GLOBAL_IN_RANGE and not (
            s["min"] <= expect <= s["max"]):
        expect = None
    got = rc.global_value(key, _glob(key, case[0]))
    if expect is None:
        assert got == (s["default"], True)
    else:
        assert got == (expect, False)
    # resolve 在没 bot 覆盖时与 global_value 同值（GET 的 global 与消费方同一口径）
    assert rc.resolve(key, {}, _glob(key, case[0])) == got[0]


def test_global_rule_assignment():
    rule = {k: s["global_rule"] for k, s in rc.BEHAVIOR_KEYS.items()}
    assert rule == {
        "moments.daily_post_limit_per_bot": rc.GLOBAL_NON_NEGATIVE,
        "moments.silence_threshold_minutes": rc.GLOBAL_POSITIVE,
        "silence_threshold_minutes": rc.GLOBAL_POSITIVE,
        "moments.min_interval_minutes": rc.GLOBAL_IN_RANGE,
        "moments.same_kind_hours": rc.GLOBAL_IN_RANGE,
        "proactive_max_calls_per_5h": rc.GLOBAL_IN_RANGE,
        "proactive_cooldown_min_minutes": rc.GLOBAL_POSITIVE,
        "proactive_cooldown_max_minutes": rc.GLOBAL_POSITIVE,
    }


def test_global_large_values():
    # non_negative / positive 不设上限（改动前就是这样）；in_range 超范围回默认
    assert rc.global_value("moments.daily_post_limit_per_bot", _glob(
        "moments.daily_post_limit_per_bot", 999)) == (999, False)
    assert rc.global_value("silence_threshold_minutes", {"silence_threshold_minutes": 5000}) == (5000, False)
    assert rc.global_value("proactive_cooldown_max_minutes",
                           {"proactive_cooldown_max_minutes": 3000}) == (3000, False)
    assert rc.global_value("moments.same_kind_hours", _glob("moments.same_kind_hours", 73)) == (4, True)
    assert rc.global_value("proactive_max_calls_per_5h",
                           {"proactive_max_calls_per_5h": 1001}) == (0, True)


def test_global_path_shapes():
    # 嵌套键只认嵌套写法；顶层键只认顶层
    key = "moments.silence_threshold_minutes"
    assert rc.global_value(key, {"moments": {"silence_threshold_minutes": 10}}) == (10, False)
    assert rc.global_value(key, {"moments.silence_threshold_minutes": 10}) == (30, True)
    assert rc.global_value(key, {"silence_threshold_minutes": 10}) == (30, True)   # 那是主动开口的键
    assert rc.global_value(key, {"moments": "bad"}) == (30, True)
    assert rc.global_value(key, {"moments": None}) == (30, True)
    assert rc.global_value("silence_threshold_minutes",
                           {"moments": {"silence_threshold_minutes": 10}}) == (30, True)
    for bad in (None, [], "x", 3):
        assert rc.global_value(key, bad) == (30, True)


def test_two_silence_keys_independent():
    bot = {"moments_silence_threshold_minutes": 0, "proactive_silence_threshold_minutes": 90}
    glob = {"silence_threshold_minutes": 15, "moments": {"silence_threshold_minutes": 20}}
    assert rc.resolve("moments.silence_threshold_minutes", bot, glob) == 0
    assert rc.resolve("silence_threshold_minutes", bot, glob) == 90
    assert rc.resolve("moments.silence_threshold_minutes", {}, glob) == 20
    assert rc.resolve("silence_threshold_minutes", {}, glob) == 15


def test_bot_layer_reads_flat_bot_key_only():
    # bot_cfg 里写逻辑键（嵌套或同名全局写法）不算 bot 覆盖
    key = "moments.min_interval_minutes"
    assert rc.resolve(key, {"moments": {"min_interval_minutes": 5}}, {}) == 60
    assert rc.resolve(key, {"moments_min_interval_minutes": 5}, {}) == 5


def test_no_exceptions_on_weird_inputs():
    for key in KEYS:
        for bot in ({}, None, {"x": 1}, _bot(key, object())):
            for glob in ({}, None, {"moments": [1]}, _glob(key, object())):
                v = rc.resolve(key, bot, glob)
                assert isinstance(v, int) and not isinstance(v, bool)
                assert not (isinstance(v, float) and math.isnan(v))


# ---------------------------------------------------------------- 锁内复核位置（life-private-dir）

def test_save_rejects_file_swapped_inside_lock(tmp_path, monkeypatch):
    """锁外 locate 之后文件被换掉：锁内那次 locate 的 (st_dev, st_ino) 与刚打开的不同 → 501，文件不动。"""
    from types import SimpleNamespace

    from moments import life_settings as ls
    from moments import rhythm_settings as rs

    dsh = tmp_path / "dsh"
    configs = dsh / "configs"
    life_dir = dsh / "life"
    legacy = tmp_path / "legacy"
    for d in (configs, life_dir, legacy):
        d.mkdir(parents=True)
    monkeypatch.setenv("DSH_BOT_HOME", str(dsh))
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(configs))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(legacy))
    monkeypatch.delenv("DSH_CONSOLE_LIFE_DIR", raising=False)
    life = life_dir / "testlulu.yml"
    life.write_text("city: 杭州\n", encoding="utf-8")
    bot = {"id": "testlulu", "cfg": {"life_config": str(life)}}

    before = life.read_bytes()
    version = rs.view(bot, [bot])["version"]        # 这一调用也走 _locate，先取版本再打桩

    real_locate = rs._locate
    calls = {"n": 0}

    def fake_locate(b, bots):
        calls["n"] += 1
        path, st = real_locate(b, bots)
        if calls["n"] == 1:                         # 锁外那次：照常
            return path, st
        return path, SimpleNamespace(st_dev=st.st_dev, st_ino=st.st_ino + 1)

    monkeypatch.setattr(rs, "_locate", fake_locate)

    with pytest.raises(ls.LifeError) as excinfo:
        rs.save(bot, [bot], {KEYS[0]: 7}, [], version)
    assert excinfo.value.status == 501
    assert calls["n"] == 2
    assert life.read_bytes() == before, "被拒了文件却变了"
    assert not (dsh / "backups").exists(), "被拒了不该建备份"
