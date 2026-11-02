# -*- coding: utf-8 -*-
"""`moments/rhythm_settings.py` 单测（PLAN dsh-rhythm ④ T5）。

盯的是 HTTP 验收看不细的地方：行级编辑的字节保真（注释块、旧写法、父节缺失、空父节删除、
无结尾换行、BOM）、认不出的写法一律 422 且文件不动、自检 / 备份 / 替换失败文件不动、
`global` 只读 `dsh_dir()/_global.yml`（不读 `HUB_CONFIGS_DIR`）、overrides 与告警的来源。
全部落在 tmp：DSH_BOT_HOME、配置根、允许目录都指过去。
"""
import os
import sys

import pytest
import yaml

import rhythm_config
from jiwen import engine
from jiwen import merge as jiwen_merge
from moments import hub_config
from moments import life_settings as ls
from moments import rhythm_settings as rs

# PLAN ②：用到的下划线名显式导入，谁改名这里立刻红
from moments.life_settings import (_backup, _load, _read, _replace, _rotate,  # noqa: F401
                                   _write_lock)
from moments.hub_config import _literal  # noqa: F401
from jiwen.merge import _bot_number, _global_number  # noqa: F401

BOT = "testlulu"
CONN = "jiwen.rates.connection_per_min"
NOTICE = "jiwen.thresholds.notice"
DAILY = "moments.daily_post_limit_per_bot"


class World:
    def __init__(self, tmp_path, monkeypatch):
        self.home = tmp_path / "dsh"
        self.configs = self.home / "configs"
        self.life_dir = self.home / "life"
        for d in (self.configs, self.life_dir):
            d.mkdir(parents=True)
        self.legacy = tmp_path / "legacy-configs"           # 存在、且放了另一份全局：不许被读到
        self.legacy.mkdir()
        (self.legacy / "_global.yml").write_text(
            "moments:\n  daily_post_limit_per_bot: 44\njiwen:\n  rates:\n    connection_per_min: 0.9\n",
            encoding="utf-8")
        monkeypatch.setenv("DSH_BOT_HOME", str(self.home))
        monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(self.configs))
        monkeypatch.setenv("HUB_CONFIGS_DIR", str(self.legacy))
        monkeypatch.delenv("DSH_CONSOLE_LIFE_DIR", raising=False)
        self.life = self.life_dir / (BOT + ".yml")
        self.own = {"id": BOT, "life_config": str(self.life)}

    def write(self, text):
        self.life.write_bytes(text.encode("utf-8"))

    def write_global(self, text):
        (self.configs / "_global.yml").write_text(text, encoding="utf-8")

    @property
    def bot(self):
        return {"id": BOT, "cfg": self.own}

    def view(self):
        return rs.view(self.bot, [self.bot])

    def save(self, set_values=None, remove=None, version=None):
        return rs.save(self.bot, [self.bot], set_values or {}, remove or [],
                       version if version is not None else self.view()["version"])

    def text(self):
        return self.life.read_bytes().decode("utf-8")

    def backups(self):
        d = self.home / "backups" / "rhythm" / BOT
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []

    def stray(self):
        return [p.name for p in self.life_dir.iterdir() if p.name != self.life.name]


@pytest.fixture
def w(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def _err(excinfo):
    e = excinfo.value
    return e.status, e.code, e.message, e.fields


# ======================================================================== schema 与 global


def test_schema_32_rows_order_and_tables(w):
    w.write("events: []\n")
    w.write_global("user_display_name: 我\n")
    body = w.view()
    keys = [r["key"] for r in body["schema"]]
    rates = [k for k in hub_config.FIELD_META
             if k.startswith("jiwen.rates.") and k[len("jiwen.rates."):] in jiwen_merge.RATE_BOUNDS]
    assert keys == rates + ["jiwen.thresholds." + f for f in ("notice", "consider", "forced")] \
        + list(rhythm_config.BEHAVIOR_KEYS)
    assert len(keys) == 32
    for row in body["schema"]:
        assert set(row) == {"key", "bot_key", "label", "unit", "type", "default", "global",
                            "global_is_default", "tab", "min", "max"}
    rows = {r["key"]: r for r in body["schema"]}
    assert rows[CONN]["default"] == engine.Rates().connection_per_min
    assert (rows[CONN]["min"], rows[CONN]["max"]) == jiwen_merge.RATE_BOUNDS["connection_per_min"]
    assert (rows[NOTICE]["bot_key"], rows[NOTICE]["label"]) == (
        "jiwen.thresholds_override.notice", hub_config.FIELD_META[NOTICE]["label"])
    assert rows[DAILY]["bot_key"] == "moments_daily_limit" and rows[DAILY]["type"] == "int"


def test_global_from_dsh_dir_not_hub_configs_dir(w):
    w.write("events: []\n")
    w.write_global("moments:\n  daily_post_limit_per_bot: 7\njiwen:\n  rates:\n    connection_per_min: 0.002\n")
    rows = {r["key"]: r for r in w.view()["schema"]}
    assert (rows[DAILY]["global"], rows[DAILY]["global_is_default"]) == (7, False)
    assert (rows[CONN]["global"], rows[CONN]["global_is_default"]) == (0.002, False)
    # 与消费方同一组函数：GET 的 global 就是 bot 没覆盖时 resolve / rate_values 的结果
    g = yaml.safe_load((w.configs / "_global.yml").read_text(encoding="utf-8"))
    assert rows[DAILY]["global"] == rhythm_config.resolve(DAILY, {}, g)
    assert rows[CONN]["global"] == jiwen_merge.rate_values(g["jiwen"], None)["connection_per_min"]


@pytest.mark.parametrize("arrange", ["missing", "bad_yaml", "not_mapping", "off"])
def test_global_unreadable_all_default(w, monkeypatch, arrange):
    w.write("events: []\n")
    if arrange == "bad_yaml":
        w.write_global("a: [1\n")
    elif arrange == "not_mapping":
        w.write_global("- 1\n")
    elif arrange == "off":
        w.write_global("moments:\n  daily_post_limit_per_bot: 7\n")
        monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", "off")
    body = w.view()
    assert "global_unreadable" in body["warnings"]
    assert all(r["global"] == r["default"] and r["global_is_default"] is True for r in body["schema"])


def test_global_bool_true_counts_as_one_like_consumer(w):
    w.write("events: []\n")
    w.write_global("jiwen:\n  thresholds:\n    notice: true\n    consider: .nan\n")
    rows = {r["key"]: r for r in w.view()["schema"]}
    assert (rows[NOTICE]["global"], rows[NOTICE]["global_is_default"]) == (1.0, False)
    assert (rows["jiwen.thresholds.consider"]["global"],
            rows["jiwen.thresholds.consider"]["global_is_default"]) == (0.35, True)


# ======================================================================== overrides 与告警


def test_overrides_and_warnings(w):
    w.write("jiwen:\n"
            "  rates:\n"
            "    connection_per_min: 0.01\n"
            "    immersion_map: {reading: 0.5}\n"
            "    no_such_rate: 1\n"
            "  thresholds_override:\n"
            "    notice: 0.5\n"
            "    pride_block: 0.4\n"
            "  rates_override: 5\n"
            "moments_daily_limit: 99\n"
            "moments_same_kind_hours: [1, 2]\n"
            "moments_min_interval_minutes: .nan\n"
            "proactive_max_calls_per_5h: 0\n"
            "proactive_cooldown_min_minutes: \"45\"\n")
    w.write_global("user_display_name: 我\n")
    body = w.view()
    assert body["overrides"] == {CONN: 0.01, NOTICE: 0.5, DAILY: 99,
                                 "proactive_max_calls_per_5h": 0,
                                 "proactive_cooldown_min_minutes": "45"}
    assert set(body["warnings"]) == {
        "legacy_key:" + CONN, "not_editable:immersion_map", "not_editable:jiwen.rates.no_such_rate",
        "not_editable:jiwen.thresholds.pride_block", "invalid_section:jiwen.rates_override",
        "invalid_value:" + DAILY, "invalid_value:moments.same_kind_hours",
        "invalid_value:moments.min_interval_minutes"}
    assert len(body["warnings"]) == len(set(body["warnings"]))
    assert body["bot_id"] == BOT


def test_bot_own_yml_wins(w):
    w.write("moments_daily_limit: 2\njiwen:\n  rates_override:\n    connection_per_min: 0.01\n")
    w.own.update({"moments_daily_limit": 9, "jiwen": {"thresholds_override": {"notice": 0.7}}})
    body = w.view()
    assert body["overrides"] == {DAILY: 9, NOTICE: 0.7}
    assert {"bot_yml_wins:jiwen", "bot_yml_wins:" + DAILY} <= set(body["warnings"])
    before = w.text()
    with pytest.raises(ls.LifeError) as e:
        w.save({DAILY: 3, NOTICE: 0.2}, ["jiwen.rates.pride_decay_per_min"])
    assert _err(e) == (422, "invalid", ls.INVALID, {
        DAILY: ls.RO_OWN_YML, NOTICE: ls.RO_OWN_YML, "jiwen.rates.pride_decay_per_min": ls.RO_OWN_YML})
    assert w.text() == before and w.backups() == []


def test_invalid_jiwen_section_itself(w):
    w.write("jiwen: 5\n")
    assert "invalid_section:jiwen" in w.view()["warnings"]


# ======================================================================== 写：字节保真


COMMENTED = (
    "# 头注释\n"
    "life_id: testlulu\n"
    "\n"
    "jiwen:\n"
    "  # delta_hints：给模型看的提示，整块不许动\n"
    "  #   - 早上容易想人\n"
    "  delta_hints:\n"
    "    morning: \"早上\"   # 行尾注释\n"
    "  rates_override:\n"
    "    connection_per_min: 0.001   # 我调过\n"
    "\n"
    "# 事件\n"
    "events: []\n"
)


def test_set_existing_leaf_only_value_bytes_change(w):
    w.write(COMMENTED)
    out = w.save({CONN: 0.02})
    assert out["changed"] == [CONN]
    assert w.text() == COMMENTED.replace("connection_per_min: 0.001", "connection_per_min: 0.02")


def test_set_missing_section_inserted_inside_jiwen_block(w):
    w.write(COMMENTED)
    w.save({NOTICE: 1})
    assert w.text() == COMMENTED.replace(
        "    connection_per_min: 0.001   # 我调过\n",
        "    connection_per_min: 0.001   # 我调过\n  thresholds_override:\n    notice: 1.0\n")


def test_set_behavior_appended_at_end(w):
    w.write(COMMENTED)
    w.save({DAILY: 0, "moments.same_kind_hours": 6})
    assert w.text() == COMMENTED + "moments_daily_limit: 0\nmoments_same_kind_hours: 6\n"
    assert w.view()["overrides"][DAILY] == 0


def test_remove_leaf_drops_empty_parent_keeps_rest(w):
    w.write(COMMENTED)
    w.save(remove=[CONN])
    assert w.text() == COMMENTED.replace(
        "  rates_override:\n    connection_per_min: 0.001   # 我调过\n", "")


def test_remove_last_emotion_drops_jiwen_too(w):
    original = "# 头\nevents: []\njiwen:\n  rates_override:\n    connection_per_min: 0.01\nx: 1\n"
    w.write(original)
    w.save(remove=[CONN])
    assert w.text() == "# 头\nevents: []\nx: 1\n"


def test_legacy_written_in_place_and_remove_clears_both(w):
    original = ("jiwen:\n"
                "  rates:\n"
                "    connection_per_min: 0.001\n"
                "    pride_decay_per_min: 0.0005\n"
                "  rates_override:\n"
                "    connection_per_min: 0.003\n"
                "events: []\n")
    w.write(original)
    w.save({CONN: 0.02})
    assert w.text() == original.replace("connection_per_min: 0.001", "connection_per_min: 0.02")
    w.save(remove=[CONN])
    assert w.text() == "jiwen:\n  rates:\n    pride_decay_per_min: 0.0005\nevents: []\n"


@pytest.mark.parametrize("parent_line, kept", [
    ("jiwen:\n", "jiwen:"), ("jiwen: ~\n", "jiwen:"), ("jiwen: {}\n", "jiwen:"),
    ("jiwen: null   # 先空着\n", "jiwen:  # 先空着"),
])
def test_empty_parent_rewritten_as_block(w, parent_line, kept):
    w.write("events: []\n" + parent_line + "x: 1\n")
    w.save({CONN: 0.5})
    assert w.text() == "events: []\n%s\n  rates_override:\n    connection_per_min: 0.5\nx: 1\n" % kept


def test_empty_leaf_value_filled(w):
    w.write("moments_daily_limit:   # 待填\nevents: []\n")
    w.save({DAILY: 4})
    assert w.text() == "moments_daily_limit: 4  # 待填\nevents: []\n"
    w.write("moments_daily_limit:\nevents: []\n")
    w.save(remove=[DAILY])
    assert w.text() == "events: []\n"


def test_no_trailing_newline_empty_file_and_bom(w):
    w.write("events: []")
    w.save({DAILY: 1})
    assert w.text() == "events: []\nmoments_daily_limit: 1\n"
    w.write("")
    w.save({CONN: 0.25})
    assert w.text() == "jiwen:\n  rates_override:\n    connection_per_min: 0.25\n"
    w.write("﻿# 带 BOM\nmoments_daily_limit: 1\n")
    w.save({DAILY: 2})
    assert w.text() == "﻿# 带 BOM\nmoments_daily_limit: 2\n"


def test_quoted_and_four_space_indent(w):
    w.write("jiwen:\n    rates_override:\n        connection_per_min: \"0.1\"\nmoments_daily_limit: '3'\n")
    w.save({CONN: 0.2, DAILY: 5, NOTICE: 0.3})
    assert w.text() == ("jiwen:\n    rates_override:\n        connection_per_min: 0.2\n"
                        "    thresholds_override:\n      notice: 0.3\nmoments_daily_limit: 5\n")


# ======================================================================== 写：认不出 → 422，文件不动


@pytest.mark.parametrize("text, key, op", [
    ("jiwen: {rates_override: {connection_per_min: 0.1}}\n", CONN, "set"),
    ("jiwen: {rates_override: {connection_per_min: 0.1}}\n", CONN, "remove"),
    ("jiwen: {thresholds: {notice: 0.1}}\n", CONN, "set"),          # 父节非空流式：补不进去
    ("jiwen: 5\n", CONN, "set"),
    ("jiwen:\n  rates_override: [1]\n", CONN, "set"),
    ("base: &b 3\nmoments_daily_limit: *b\n", DAILY, "set"),
    ("moments_daily_limit: &d 3\nother: *d\n", DAILY, "set"),
    ("jiwen: &j\n  rates_override:\n    connection_per_min: 0.1\n", CONN, "set"),
    ("moments_daily_limit: !!int 3\n", DAILY, "set"),
    ("moments_daily_limit:\n  3\n", DAILY, "set"),
    ("moments_same_kind_hours:\n  - 1\n  - 2\n", "moments.same_kind_hours", "remove"),
    ("moments_daily_limit: {a: 1}\n", DAILY, "set"),
    ("events: []\r\nmoments_daily_limit: 3\r\n", DAILY, "set"),
    ("{moments_daily_limit: 3}\n", DAILY, "set"),
    ("jiwen:\n  rates_override:\n    connection_per_min: 0.1\n    connection_per_min: 0.2\n", CONN, "set"),
    # 多行流式：键都在行首，只有「流式映射」这一条拦得住（删完会留下 `rates_override: {` 的空壳）
    ("jiwen: {\n  rates_override: {\n    connection_per_min: 0.1\n  }\n}\n", CONN, "remove"),
    ("jiwen: {\n  rates_override: {\n    connection_per_min: 0.1\n  }\n}\n", CONN, "set"),
], ids=["flow_set", "flow_remove", "flow_parent", "scalar_parent", "list_parent", "alias_leaf",
        "anchor_leaf", "anchor_parent", "tag", "value_next_line", "block_list_remove", "map_leaf",
        "crlf", "flow_root", "duplicate_key", "flow_multiline_remove", "flow_multiline_set"])
def test_unrecognized_layout_422_file_untouched(w, text, key, op):
    w.write(text)
    before = w.life.read_bytes()
    try:
        version = w.view()["version"]
    except ls.LifeError:
        pytest.fail("GET 不该因为这种写法失败")
    with pytest.raises(ls.LifeError) as e:
        if op == "set":
            w.save({key: 3 if key == DAILY else 0.5}, version=version)
        else:
            w.save(remove=[key], version=version)
    assert _err(e) == (422, "invalid", ls.INVALID, {key: rs.UNRECOGNIZED})
    assert w.life.read_bytes() == before and w.backups() == [] and w.stray() == []


def test_unrecognized_reports_all_keys_and_writes_nothing(w):
    w.write("jiwen: {rates_override: {connection_per_min: 0.1}}\nmoments_daily_limit: !!int 3\n")
    before = w.life.read_bytes()
    with pytest.raises(ls.LifeError) as e:
        w.save({CONN: 0.2, DAILY: 1, "moments.same_kind_hours": 5})
    assert _err(e)[3] == {CONN: rs.UNRECOGNIZED, DAILY: rs.UNRECOGNIZED}
    assert w.life.read_bytes() == before and w.backups() == []


# ======================================================================== 写：自检 / 备份 / 替换失败


def test_self_check_failure_leaves_file(w, monkeypatch):
    w.write(COMMENTED)
    before = w.life.read_bytes()
    monkeypatch.setattr(rs, "_set_leaf", lambda text, parts, literal: text + "moments_daily_limit: 7\n")
    with pytest.raises(ls.LifeError) as e:
        w.save({CONN: 0.02})
    assert _err(e) == (500, "internal", rs.SELF_CHECK_FAILED, None)
    assert w.life.read_bytes() == before and w.backups() == [] and w.stray() == []


def test_self_check_unparseable_leaves_file(w, monkeypatch):
    w.write(COMMENTED)
    before = w.life.read_bytes()
    monkeypatch.setattr(rs, "_set_leaf", lambda text, parts, literal: text + "x: [\n")
    with pytest.raises(ls.LifeError) as e:
        w.save({CONN: 0.02})
    assert _err(e)[:3] == (500, "internal", rs.SELF_CHECK_FAILED)
    assert w.life.read_bytes() == before


def test_backup_failure_leaves_file(w, monkeypatch):
    w.write(COMMENTED)
    before = w.life.read_bytes()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(ls, "_backup", boom)
    with pytest.raises(ls.LifeError) as e:
        w.save({CONN: 0.02})
    assert _err(e)[:3] == (500, "internal", ls.BACKUP_FAILED)
    assert w.life.read_bytes() == before and w.stray() == []


def test_replace_failure_leaves_file_and_no_temp(w, monkeypatch):
    w.write(COMMENTED)
    before = w.life.read_bytes()
    real_replace = os.replace

    def boom(src, dst):
        raise OSError("ro")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(ls.LifeError) as e:
        w.save({CONN: 0.02})
    monkeypatch.setattr(os, "replace", real_replace)
    assert _err(e)[:3] == (500, "internal", ls.WRITE_FAILED)
    assert w.life.read_bytes() == before and w.stray() == []


# ======================================================================== 版本、并发、备份


def test_version_stable_then_changes_and_stale(w):
    w.write(COMMENTED)
    v0 = w.view()["version"]
    assert w.view()["version"] == v0
    out = w.save({DAILY: 2}, version=v0)
    assert out["version"] != v0 and out["version"] == w.view()["version"]
    before = w.life.read_bytes()
    with pytest.raises(ls.LifeError) as e:
        w.save({DAILY: 9}, version=v0)
    assert _err(e) == (409, "stale", rs.STALE, None)
    assert w.life.read_bytes() == before and len(w.backups()) == 1


def test_hand_edit_after_get_is_stale(w):
    w.write("events: []\n")
    v0 = w.view()["version"]
    w.write("events: []\n")                    # 内容相同的手工保存：inode / mtime 变了
    os.utime(str(w.life), ns=(1, 1))
    with pytest.raises(ls.LifeError) as e:
        w.save({DAILY: 2}, version=v0)
    assert _err(e)[1] == "stale"


def test_same_bytes_same_mtime_new_inode_is_stale(w):
    # mtime 粒度粗（某些盘只到秒）、大小又相同时，只有 inode 能看出文件被换过（R7）
    w.write("events: []\n")
    v0 = w.view()["version"]
    st = w.life.stat()
    tmp = w.life_dir / "swap.tmp"
    tmp.write_bytes(w.life.read_bytes())
    os.replace(str(tmp), str(w.life))
    os.utime(str(w.life), ns=(st.st_atime_ns, st.st_mtime_ns))
    assert w.life.stat().st_ino != st.st_ino
    with pytest.raises(ls.LifeError) as e:
        w.save({DAILY: 2}, version=v0)
    assert _err(e)[1] == "stale"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 没有跨进程文件锁")
def test_busy_when_lock_held(w):
    import fcntl
    w.write("events: []\n")
    v0 = w.view()["version"]
    lock_dir = w.home / "locks"
    lock_dir.mkdir(exist_ok=True)
    fd = os.open(str(lock_dir / "life.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ls.LifeError) as e:
            w.save({DAILY: 2}, version=v0)
        assert _err(e)[:3] == (409, "busy", ls.BUSY)
    finally:
        os.close(fd)
    assert w.text() == "events: []\n" and w.backups() == []


def test_backups_keep_two_with_previous_bytes_and_0600(w):
    w.write("events: []\n")
    originals = []
    for n in (1, 2, 3):
        originals.append(w.life.read_bytes())
        w.save({DAILY: n})
    names = w.backups()
    assert len(names) == 2 and all(ls.BACKUP_NAME_RE.match(n) for n in names)
    folder = w.home / "backups" / "rhythm" / BOT
    assert {(folder / n).read_bytes() for n in names} == {originals[1], originals[2]}
    if os.name != "nt":
        assert all(((folder / n).stat().st_mode & 0o777) == 0o600 for n in names)


def test_mode_preserved(w):
    w.write("events: []\n")
    os.chmod(str(w.life), 0o640)
    w.save({DAILY: 1})
    if os.name != "nt":
        assert (w.life.stat().st_mode & 0o777) == 0o640


def test_no_op_cases_no_write_no_backup(w):
    w.write(COMMENTED)
    v0 = w.view()["version"]
    assert w.save(version=v0) == {"version": v0, "changed": []}
    out = w.save(remove=["moments.same_kind_hours", "moments.same_kind_hours", NOTICE], version=v0)
    assert out == {"version": v0, "changed": ["moments.same_kind_hours", NOTICE]}
    assert w.text() == COMMENTED and w.backups() == []


def test_changed_order_set_then_remove(w):
    w.write(COMMENTED)
    out = w.save({"moments.same_kind_hours": 2, DAILY: 1}, [CONN, "silence_threshold_minutes", CONN])
    assert out["changed"] == ["moments.same_kind_hours", DAILY, CONN, "silence_threshold_minutes"]


# ======================================================================== 请求体与逐键校验


@pytest.mark.parametrize("set_values, remove, version, message, fields", [
    ([], [], "v", rs.BODY_SHAPE, {}),
    ({}, {}, "v", rs.BODY_SHAPE, {}),
    ({}, [], "", rs.BODY_SHAPE, {}),
    ({}, [], None, rs.BODY_SHAPE, {}),
    ({}, [1], "v", rs.KEY_NOT_TEXT, {}),
    ({DAILY: 1, CONN: 0.1}, [CONN, DAILY], "v", rs.SET_AND_REMOVE,
     {DAILY: rs.SET_AND_REMOVE, CONN: rs.SET_AND_REMOVE}),
])
def test_check_body(set_values, remove, version, message, fields):
    with pytest.raises(ls.LifeError) as e:
        rs.check_body(set_values, remove, version)
    assert _err(e) == (422, "invalid", message, fields)


def test_field_errors_all_reported_nothing_written(w):
    w.write("events: []\n")
    before = w.life.read_bytes()
    with pytest.raises(ls.LifeError) as e:
        w.save({DAILY: True, "proactive_max_calls_per_5h": 5.0, "moments.min_interval_minutes": "5",
                "moments.same_kind_hours": 73, "proactive_cooldown_min_minutes": 0,
                CONN: 1.5, NOTICE: "0.5", "jiwen.thresholds.consider": float("nan"),
                "jiwen.thresholds.forced": float("inf"), "jiwen.rates.connection_accel": 10 ** 400,
                "jiwen.rates.immersion_map": 1, "silence_threshold_minutes": 10},
               ["moments.no_such_key"])
    assert _err(e) == (422, "invalid", ls.INVALID, {
        DAILY: rs.NEED_INT, "proactive_max_calls_per_5h": rs.NEED_INT,
        "moments.min_interval_minutes": rs.NEED_INT,
        "moments.same_kind_hours": "超出允许范围 [0, 72]",
        "proactive_cooldown_min_minutes": "超出允许范围 [1, 960]",
        CONN: "超出允许范围 [0.0, 1.0]", NOTICE: rs.NEED_NUMBER,
        "jiwen.thresholds.consider": rs.NEED_FINITE, "jiwen.thresholds.forced": rs.NEED_FINITE,
        "jiwen.rates.connection_accel": "超出允许范围 [0.0, 5.0]",
        "jiwen.rates.immersion_map": rs.NOT_HERE, "moments.no_such_key": rs.NOT_HERE})
    assert w.life.read_bytes() == before and w.backups() == []


def test_bounds_inclusive_and_float_row_takes_int(w):
    w.write("events: []\n")
    w.save({"moments.same_kind_hours": 72, "proactive_cooldown_min_minutes": 1, CONN: 1,
            "jiwen.rates.valence_severe_low": -1})
    doc = yaml.safe_load(w.text())
    assert doc["moments_same_kind_hours"] == 72 and doc["proactive_cooldown_min_minutes"] == 1
    assert doc["jiwen"]["rates_override"] == {"connection_per_min": 1.0, "valence_severe_low": -1.0}
    assert "connection_per_min: 1.0" in w.text()


# ======================================================================== 写完消费方读得到


def test_written_values_are_what_consumers_read(w):
    w.write("events: []\n")
    w.save({CONN: 0.0, NOTICE: 0.9, "moments.min_interval_minutes": 0})
    doc = yaml.safe_load(w.text())
    assert jiwen_merge.rate_values({}, doc["jiwen"])["connection_per_min"] == 0.0
    assert jiwen_merge.threshold_values({}, doc["jiwen"])["notice"] == 0.9
    assert rhythm_config.resolve("moments.min_interval_minutes", doc, {}) == 0
    assert w.view()["overrides"] == {CONN: 0.0, NOTICE: 0.9, "moments.min_interval_minutes": 0}


# ======================================================================== 定位失败沿用生活设定


def test_no_life_config_is_no_file_even_without_allowed_dir(w):
    # INTERFACE 3.2：没写 life_config 一律「没有单独的生活设定文件」；允许目录不存在时 locate 本身会先回
    # 「不在允许位置」，那句话对没有文件的 bot 不对
    w.life_dir.rmdir()
    w.own.pop("life_config")
    for call in (w.view, lambda: rs.save(w.bot, [w.bot], {DAILY: 1}, [], "v")):
        with pytest.raises(ls.LifeError) as e:
            call()
        assert _err(e)[:3] == (501, "unsupported", ls.NO_FILE)
    w.own["life_config"] = "   "
    with pytest.raises(ls.LifeError) as e:
        w.view()
    assert _err(e)[:3] == (501, "unsupported", ls.NO_FILE)
    w.own["life_config"] = str(w.life)                    # 写了但允许目录不在：照 locate 回「不在允许位置」
    with pytest.raises(ls.LifeError) as e:
        w.view()
    assert _err(e)[:3] == (501, "unsupported", ls.NOT_ALLOWED)


def test_locate_errors_propagate(w):
    w.own.pop("life_config")
    with pytest.raises(ls.LifeError) as e:
        w.view()
    assert _err(e)[:3] == (501, "unsupported", ls.NO_FILE)
    w.own["life_config"] = str(w.life)
    w.write("a: [1\n")
    with pytest.raises(ls.LifeError) as e:
        w.view()
    assert e.value.status == 500 and e.value.message.startswith(ls.BAD_YAML)
    w.write("- 1\n")
    with pytest.raises(ls.LifeError) as e:
        w.save({DAILY: 1}, version="x")
    assert _err(e)[:3] == (500, "internal", ls.BAD_YAML)


# ======================================================================== 小数写法：读回必须是同值 float


@pytest.mark.parametrize("value", [1e-5, 5e-05, 1e-10, 0.0, 1.0, 0.1, 0.00005, 1e16, 1.5e300, 123456789.25])
def test_float_literal_round_trips_through_yaml(value):
    lit = rs._float_literal(value)
    got = yaml.safe_load("x: %s\n" % lit)["x"]
    assert isinstance(got, float) and got == value, (lit, got)


@pytest.mark.parametrize("value", [0.00005, 1e-5, 1e-10, 0.0])
def test_save_tiny_rate_writes_float_not_string(w, value):
    w.write("events: []\n")
    w.save({CONN: value})
    doc = yaml.safe_load(w.text())
    got = doc["jiwen"]["rates_override"]["connection_per_min"]
    assert isinstance(got, float) and got == value
    assert w.view()["overrides"][CONN] == value
    assert "invalid_value:" + CONN not in w.view()["warnings"]


# ======================================================================== 删除：改动前就空的父节原样保留


@pytest.mark.parametrize("original, want", [
    ("jiwen:\n  rates_override: {}\n  rates:\n    connection_per_min: 0.1\n",
     "jiwen:\n  rates_override: {}\n"),
    ("jiwen:\n  rates_override:\n  rates:\n    connection_per_min: 0.1\n",
     "jiwen:\n  rates_override:\n"),
    ("jiwen:\n  rates: {}\n  rates_override:\n    connection_per_min: 0.1\nx: 1\n",
     "jiwen:\n  rates: {}\nx: 1\n"),
    ("jiwen: {}\nmoments_daily_limit: 3\n", "jiwen: {}\n"),
], ids=["override_flow_empty", "override_null", "legacy_flow_empty", "jiwen_empty_remove_behavior"])
def test_remove_keeps_parents_that_were_already_empty(w, original, want):
    w.write(original)
    key = DAILY if "moments_daily_limit" in original else CONN
    out = w.save(remove=[key])
    assert out["changed"] == [key]
    assert w.text() == want
    assert yaml.safe_load(w.text()) == yaml.safe_load(want)


def test_expected_del_only_drops_parents_emptied_now():
    expected = {"jiwen": {"rates_override": {}, "rates": {"connection_per_min": 0.1}}}
    rs._expected_del(expected, ["jiwen", "rates_override", "connection_per_min"])
    assert expected == {"jiwen": {"rates_override": {}, "rates": {"connection_per_min": 0.1}}}
    rs._expected_del(expected, ["jiwen", "rates", "connection_per_min"])
    assert expected == {"jiwen": {"rates_override": {}}}
    gone = {"jiwen": {"rates_override": {"connection_per_min": 0.1}}, "x": 1}
    rs._expected_del(gone, ["jiwen", "rates_override", "connection_per_min"])
    assert gone == {"x": 1}
