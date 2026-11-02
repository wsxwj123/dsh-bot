# -*- coding: utf-8 -*-
"""`jiwen/merge.py` 与 `tick.merge_rates` / 心跳摘要 / `reader.read` 接线的单测（PLAN dsh-rhythm ④ T2）。

盯四件事：
1. 两张范围表与 `hub_config._RATE_META / _THRESHOLD_META` 逐项相等，键与 engine 字段相等（R13）；
2. 没有 bot 覆盖时，`merge_rates` 与改动前逐字段相同（R4）——这里冻结了一份改动前的
   `merge_rates` 与阈值读法原文作参照，拿生产全局快照、仓库样例与一份各项都非默认的全局去比；
3. 坏值不抛；
4. `reader.read` 用临时状态文件验 notice 覆盖真的改变触发。
"""
import dataclasses
import json
import math
from pathlib import Path

import pytest
import yaml

from jiwen import engine, merge
from jiwen.tick import merge_rates

ROOT = Path(__file__).resolve().parents[2]

# 生产 `_global.yml` 的 jiwen.rates / jiwen.thresholds 两节（2026-11-02 快照，只有数字，不含路径）
PROD_JIWEN = {
    "rates": {
        "connection_per_min": 0.0007, "pride_decay_per_min": 0.003, "valence_decay_per_min": 0.005,
        "arousal_decay_per_min": 0.01, "valence_lock_factor": 0.3,
        "arousal_connection_rise_rate": 0.002, "pride_defend_rate": 0.003,
        "pride_arousal_conflict_rate": 0.001, "pride_erosion_rate": 0.002,
        "immersion_decay_per_min": 0.01, "activity_connection_relief": 0.0005,
        "connection_accel": 1.0, "valence_severe_multiplier": 0.3, "valence_mild_multiplier": 1.5,
        "valence_severe_low": -0.5, "valence_mild_low": -0.2, "valence_lock_threshold": 0.5,
        "arousal_connection_rise_threshold": 0.5, "pride_defend_threshold": 0.4,
        "pride_defend_target": 0.6, "accel_threshold_min": 5.0,
        "immersion_map": {"reading": 0.6, "cooking": 0.5, "search": 0.4, "browse": 0.35,
                          "observe": 0.15, "selfcare": 0.5},
    },
    "thresholds": {"notice": 0.2, "consider": 0.35, "forced": 0.5},
}

# 每项都不是默认值；混入整数、数字串、超出页面范围的值（全局层改动前不卡范围）
VARIED_JIWEN = {
    "rates": {name: (i + 1) * 0.01 for i, name in enumerate(merge.RATE_BOUNDS)},
    "thresholds": {"notice": 0.25, "consider": "0.4", "forced": 1, "pride_block": 0.45,
                   "valence_activity": -0.7, "arousal_agitation": 0.8, "immersion_block": 0.25},
}
VARIED_JIWEN["rates"].update({"connection_per_min": 3, "connection_accel": "2.5",
                              "valence_severe_low": -7.0, "accel_threshold_min": 5000,
                              "immersion_map": {"reading": 0.9, "custom": 0.1}})


def _example_jiwen():
    data = yaml.safe_load((ROOT / "configs" / "_global.example.yml").read_text(encoding="utf-8"))
    return data["jiwen"]


# ---------------------------------------------------------------- 改动前原文（冻结的参照）


def _old_merge_rates(global_jiwen, bot_overrides):
    """改动前 jiwen/tick.py `merge_rates` 原文（只把 engine 换成包内的 engine）。"""
    rates_dict = dict((global_jiwen or {}).get("rates", {}))
    bot_jw = bot_overrides or {}
    rates_dict.update(bot_jw.get("rates_override", {}))
    rates_dict.update(bot_jw.get("rates", {}))
    default_immersion_map = {
        "reading": 0.6, "cooking": 0.5, "search": 0.4,
        "browse": 0.35, "observe": 0.15, "selfcare": 0.5,
    }
    immersion_map = rates_dict.get("immersion_map") or default_immersion_map
    return engine.Rates(
        connection_per_min=float(rates_dict.get("connection_per_min", 0.0007)),
        pride_decay_per_min=float(rates_dict.get("pride_decay_per_min", 0.003)),
        valence_decay_per_min=float(rates_dict.get("valence_decay_per_min", 0.005)),
        arousal_decay_per_min=float(rates_dict.get("arousal_decay_per_min", 0.01)),
        accel_threshold_min=float(rates_dict.get("accel_threshold_min", 5.0)),
        connection_accel=float(rates_dict.get("connection_accel", 1.0)),
        valence_severe_low=float(rates_dict.get("valence_severe_low", -0.5)),
        valence_severe_multiplier=float(rates_dict.get("valence_severe_multiplier", 0.3)),
        valence_mild_low=float(rates_dict.get("valence_mild_low", -0.2)),
        valence_mild_multiplier=float(rates_dict.get("valence_mild_multiplier", 1.5)),
        valence_lock_threshold=float(rates_dict.get("valence_lock_threshold", 0.5)),
        valence_lock_factor=float(rates_dict.get("valence_lock_factor", 0.3)),
        arousal_connection_rise_threshold=float(rates_dict.get("arousal_connection_rise_threshold", 0.5)),
        arousal_connection_rise_rate=float(rates_dict.get("arousal_connection_rise_rate", 0.002)),
        pride_defend_threshold=float(rates_dict.get("pride_defend_threshold", 0.4)),
        pride_defend_target=float(rates_dict.get("pride_defend_target", 0.6)),
        pride_defend_rate=float(rates_dict.get("pride_defend_rate", 0.003)),
        pride_arousal_conflict_rate=float(rates_dict.get("pride_arousal_conflict_rate", 0.001)),
        pride_erosion_rate=float(rates_dict.get("pride_erosion_rate", 0.002)),
        immersion_decay_per_min=float(rates_dict.get("immersion_decay_per_min", 0.010)),
        activity_connection_relief=float(rates_dict.get("activity_connection_relief", 0.0005)),
        immersion_map=dict(immersion_map),
    )


def _old_reader_thresholds(jcfg):
    """改动前 jiwen/reader.py `read()` 里的阈值读法原文。"""
    th_cfg = jcfg.get("thresholds") or {}
    try:
        return engine.Thresholds(
            notice=float(th_cfg.get("notice", 0.20)),
            consider=float(th_cfg.get("consider", 0.35)),
            forced=float(th_cfg.get("forced", 0.50)),
            pride_block=float(th_cfg.get("pride_block", 0.5)),
            valence_activity=float(th_cfg.get("valence_activity", -0.6)),
            arousal_agitation=float(th_cfg.get("arousal_agitation", 0.7)),
            immersion_block=float(th_cfg.get("immersion_block", 0.3)),
        )
    except (TypeError, ValueError):
        return engine.Thresholds()


def _old_tick_summary_thresholds(jiwen_cfg):
    """改动前 jiwen/tick.py 心跳摘要里的阈值读法原文（只读 4 项）。"""
    return engine.Thresholds(
        notice=float((jiwen_cfg or {}).get("thresholds", {}).get("notice", 0.20)),
        consider=float((jiwen_cfg or {}).get("thresholds", {}).get("consider", 0.35)),
        forced=float((jiwen_cfg or {}).get("thresholds", {}).get("forced", 0.50)),
        pride_block=float((jiwen_cfg or {}).get("thresholds", {}).get("pride_block", 0.5)),
    )


# ---------------------------------------------------------------- 范围表（R13）


def test_rate_bounds_equal_hub_config_rate_meta():
    from moments import hub_config
    assert list(merge.RATE_BOUNDS.items()) == [
        (name, (lo, hi)) for name, (lo, hi, _label, _unit) in hub_config._RATE_META.items()]


def test_threshold_bounds_equal_hub_config_threshold_meta():
    from moments import hub_config
    page = {name: (lo, hi) for name, (lo, hi, _l, _u) in hub_config._THRESHOLD_META.items()}
    assert list(page) == ["notice", "consider", "forced"]
    assert {k: merge.THRESHOLD_BOUNDS[k] for k in page} == page


def test_rate_bounds_keys_equal_engine_rates_scalar_fields():
    scalar = [f.name for f in dataclasses.fields(engine.Rates) if f.name != "immersion_map"]
    assert len(scalar) == 21
    assert set(merge.RATE_BOUNDS) == set(scalar)


def test_threshold_bounds_keys_equal_engine_thresholds_fields():
    assert set(merge.THRESHOLD_BOUNDS) == {f.name for f in dataclasses.fields(engine.Thresholds)}


def test_defaults_inside_bounds():
    for cls, table in ((engine.Rates, merge.RATE_BOUNDS), (engine.Thresholds, merge.THRESHOLD_BOUNDS)):
        base = cls()
        for name, (lo, hi) in table.items():
            assert lo <= getattr(base, name) <= hi, name


# ---------------------------------------------------------------- 没有 bot 覆盖时与改动前逐字段相同（R4）


@pytest.mark.parametrize("global_jiwen", [
    PROD_JIWEN, VARIED_JIWEN, {}, None, {"rates": {}}, {"rates": {"connection_per_min": 0.002}},
    {"rates": {"immersion_map": {}}}, {"rates": {"immersion_map": {"reading": 0.1}}},
], ids=["prod", "varied", "empty", "none", "empty_rates", "one_rate", "empty_imap", "custom_imap"])
@pytest.mark.parametrize("bot", [{}, None, {"enabled": True}], ids=["bot_empty", "bot_none", "bot_other"])
def test_merge_rates_without_override_matches_old(global_jiwen, bot):
    assert dataclasses.asdict(merge_rates(global_jiwen, bot)) == dataclasses.asdict(
        _old_merge_rates(global_jiwen, bot))


def test_merge_rates_example_global_matches_old():
    g = _example_jiwen()
    assert dataclasses.asdict(merge_rates(g, {})) == dataclasses.asdict(_old_merge_rates(g, {}))


@pytest.mark.parametrize("global_jiwen", [PROD_JIWEN, VARIED_JIWEN, {}, {"thresholds": None}],
                         ids=["prod", "varied", "empty", "none_section"])
def test_thresholds_without_override_match_old_reader(global_jiwen):
    assert merge.threshold_values(global_jiwen, {}) == dataclasses.asdict(
        _old_reader_thresholds(global_jiwen))


def test_thresholds_example_global_match_old_reader():
    g = _example_jiwen()
    assert merge.threshold_values(g, None) == dataclasses.asdict(_old_reader_thresholds(g))


def test_tick_summary_thresholds_prod_match_old():
    # 改动前摘要只读全局 4 项；生产全局只写了 3 项，所以对生产配置结果逐字段相同
    assert merge.threshold_values(PROD_JIWEN, {}) == dataclasses.asdict(
        _old_tick_summary_thresholds(PROD_JIWEN))


# ---------------------------------------------------------------- bot 层口径


def test_bot_override_and_zero():
    got = merge.rate_values({"rates": {"connection_per_min": 0.002}},
                            {"rates_override": {"connection_per_min": 0, "pride_decay_per_min": 0.5}})
    assert got["connection_per_min"] == 0.0          # 0 也算设了
    assert got["pride_decay_per_min"] == 0.5
    assert got["valence_decay_per_min"] == engine.Rates().valence_decay_per_min


def test_bot_bounds_inclusive_and_out_of_range_skipped():
    g = {"rates": {"connection_accel": 2.0, "valence_severe_low": -0.4}}
    for raw, want in ((5, 5.0), (0, 0.0), (5.0001, 2.0), (-0.1, 2.0), (9, 2.0)):
        assert merge.rate_values(g, {"rates_override": {"connection_accel": raw}})["connection_accel"] == want, raw
    for raw, want in ((-1, -1.0), (1, 1.0), (-1.01, -0.4), (1.01, -0.4)):
        assert merge.rate_values(g, {"rates_override": {"valence_severe_low": raw}})["valence_severe_low"] == want


def test_bot_numeric_string_accepted_bool_and_junk_rejected():
    g = {"thresholds": {"notice": 0.3}}
    assert merge.threshold_values(g, {"thresholds_override": {"notice": "0.9"}})["notice"] == 0.9
    assert merge.threshold_values(g, {"thresholds_override": {"notice": " 0.4 "}})["notice"] == 0.4
    for raw in (True, False, None, "abc", "", "nan", float("nan"), float("inf"), [0.5], {"a": 1}, 10 ** 400):
        assert merge.threshold_values(g, {"thresholds_override": {"notice": raw}})["notice"] == 0.3, raw


def test_legacy_wins_and_invalid_legacy_keeps_override():
    g = {"thresholds": {"notice": 0.3}}
    both = {"thresholds_override": {"notice": 0.5}, "thresholds": {"notice": 0.9}}
    assert merge.threshold_values(g, both)["notice"] == 0.9
    bad_legacy = {"thresholds_override": {"notice": 0.5}, "thresholds": {"notice": "bad"}}
    assert merge.threshold_values(g, bad_legacy)["notice"] == 0.5
    only_legacy = {"rates": {"connection_per_min": 0.01}}
    assert merge.rate_values({}, only_legacy)["connection_per_min"] == 0.01


def test_non_page_thresholds_still_overridable():
    # 后四项页面不编辑，但手写 yml 仍认（与 claude 侧同），越界照样跳过
    got = merge.threshold_values({}, {"thresholds_override": {"pride_block": 0.8, "valence_activity": -2}})
    assert got["pride_block"] == 0.8
    assert got["valence_activity"] == engine.Thresholds().valence_activity


# ---------------------------------------------------------------- 全局层口径


def test_global_not_range_limited_and_non_finite_dropped():
    got = merge.rate_values({"rates": {"connection_per_min": 3, "connection_accel": "7",
                                       "pride_decay_per_min": float("nan"),
                                       "valence_decay_per_min": "abc",
                                       "arousal_decay_per_min": None}}, {})
    d = engine.Rates()
    assert got["connection_per_min"] == 3.0          # 超出页面范围也照用（同改动前）
    assert got["connection_accel"] == 7.0            # 数字串照用（同改动前 float()）
    assert got["pride_decay_per_min"] == d.pride_decay_per_min
    assert got["valence_decay_per_min"] == d.valence_decay_per_min
    assert got["arousal_decay_per_min"] == d.arousal_decay_per_min
    # 全局写 true：改动前 float(True) = 1.0 照用，全局层不改口径
    assert merge.threshold_values({"thresholds": {"notice": True}}, {})["notice"] == 1.0


def test_global_one_bad_threshold_no_longer_resets_whole_group():
    got = merge.threshold_values({"thresholds": {"notice": 0.25, "consider": "bad"}}, {})
    assert got["notice"] == 0.25
    assert got["consider"] == engine.Thresholds().consider


# ---------------------------------------------------------------- immersion_map（整表替换，同改动前）


def test_immersion_map_whole_table_replacement():
    g = {"rates": {"immersion_map": {"reading": 0.1, "cooking": 0.2}}}
    assert merge.immersion_map(g, {}) == {"reading": 0.1, "cooking": 0.2}
    assert merge.immersion_map(g, {"rates_override": {"immersion_map": {"browse": 0.3}}}) == {"browse": 0.3}
    assert merge.immersion_map(g, {"rates_override": {"immersion_map": {"browse": 0.3}},
                                   "rates": {"immersion_map": {"observe": 0.4}}}) == {"observe": 0.4}
    default = engine.Rates().immersion_map
    assert merge.immersion_map(g, {"rates_override": {"immersion_map": {}}}) == default   # 空表回默认（同改动前）
    assert merge.immersion_map(g, {"rates_override": {"immersion_map": None}}) == default
    assert merge.immersion_map(g, {"rates_override": {"immersion_map": "bad"}}) == default  # 改动前会抛
    out = merge.immersion_map(g, {})
    out["reading"] = 9                                  # 返回副本，不改调用方的配置
    assert g["rates"]["immersion_map"]["reading"] == 0.1


# ---------------------------------------------------------------- 坏值不抛


@pytest.mark.parametrize("global_jiwen", [None, "bad", [], {"rates": None, "thresholds": None},
                                          {"rates": "x", "thresholds": [1]}, {"rates": {1: 2}}])
@pytest.mark.parametrize("bot", [None, "bad", [], {"rates_override": None, "thresholds_override": None},
                                 {"rates_override": "x", "rates": [1], "thresholds": "y"},
                                 {"rates_override": {"connection_per_min": object()}}])
def test_bad_shapes_never_raise(global_jiwen, bot):
    rates = merge_rates(global_jiwen, bot)
    assert dataclasses.is_dataclass(rates)
    th = engine.Thresholds(**merge.threshold_values(global_jiwen, bot))
    for v in list(merge.rate_values(global_jiwen, bot).values()) + list(dataclasses.asdict(th).values()):
        assert isinstance(v, float) and math.isfinite(v)


def test_merge_rates_returns_tick_engine_rates():
    # tick.py 用 `import engine` 拿到的模块对象构造；merge 只回 dict，不会混出「两个 Rates 类」
    from jiwen import tick
    assert type(merge_rates({}, {})) is tick.engine.Rates


# ---------------------------------------------------------------- reader.read 接线


def test_reader_read_applies_bot_notice_override(tmp_path, monkeypatch):
    from jiwen import reader
    configs = tmp_path / "configs"
    life = tmp_path / "life"
    state_dir = tmp_path / "state"
    for d in (configs, life, state_dir):
        d.mkdir()
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(configs))
    bot_id, chat_id = "testlulu", "123456789"
    # 覆盖写在生活设定文件里（节奏页写的就是这份），经 load_bot 浅合并进来
    (life / "testlulu.yml").write_text(
        "jiwen:\n  thresholds_override:\n    notice: 0.9\n", encoding="utf-8")
    bot_yml = configs / "testlulu.yml"
    bot_yml.write_text("life_config: %s\n" % json.dumps(str(life / "testlulu.yml")), encoding="utf-8")
    state = engine.State(connection=0.25, last_tick_ts=1, last_user_msg_ts=1)
    (state_dir / ("%s-%s.json" % (bot_id, chat_id))).write_text(
        json.dumps(engine.state_to_dict(state)), encoding="utf-8")
    global_cfg = {"jiwen": {"enabled": True, "state_dir": str(state_dir),
                            "thresholds": {"notice": 0.2}}}

    def actions(info):
        return [t.get("action") for t in info["triggers"]]

    with_override = reader.read(bot_id, chat_id, global_cfg)
    assert with_override is not None
    assert "observation" not in actions(with_override)      # notice 抬到 0.9，0.25 不再起念头

    (life / "testlulu.yml").write_text("jiwen: {}\n", encoding="utf-8")
    without = reader.read(bot_id, chat_id, global_cfg)
    assert "observation" in actions(without)                 # 回到全局 0.2

    (life / "testlulu.yml").write_text(
        "jiwen:\n  thresholds_override:\n    notice: abc\n", encoding="utf-8")
    bad = reader.read(bot_id, chat_id, global_cfg)            # 坏覆盖不抛，当没设
    assert "observation" in actions(bad)


def test_reader_read_bot_config_unreadable_falls_back(tmp_path, monkeypatch):
    from jiwen import reader
    configs = tmp_path / "configs"
    state_dir = tmp_path / "state"
    configs.mkdir()
    state_dir.mkdir()
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(configs))     # 没有 bot yml：load_bot 抛，按旧系统走
    state = engine.State(connection=0.25)
    (state_dir / "ghost-1.json").write_text(json.dumps(engine.state_to_dict(state)), encoding="utf-8")
    info = reader.read("ghost", "1", {"jiwen": {"enabled": True, "state_dir": str(state_dir)}})
    assert info is not None and "observation" in [t.get("action") for t in info["triggers"]]
    assert reader.resolve("ghost", {"enabled": True, "state_dir": str(state_dir)}) == (True, str(state_dir))
