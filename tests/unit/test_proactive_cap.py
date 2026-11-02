# -*- coding: utf-8 -*-
"""「每 5 小时最多主动找你几次」整条链路的白盒单测（PLAN dsh-rhythm ④ T4）。

验收只覆盖了「没设上限时不查库」；这里把「真的超了」这条链走完：
- db.count_self_initiates_since / quota.check_proactive_quota：按 person 计数（bot4 → chenlulu），
  只数 5 小时内 judge_speak=1 且 skipped=0 的行，>= cap 即 over；cap=0 不查库；查库异常向上抛。
- life-context.py main()：over 时记一条 skipped 心跳、跳过预筛与判官、朋友圈照走、回
  SKIP proactive_cap:<used>/<cap>；jiwen forced、提前开口同样受限；/clear bypass 不查；
  查库出错放行并记类名；dry-run 照查但不写心跳；cap=0 时输出与改动前（BASELINE_REV 原文）逐字相同。
- scripts/self_initiate.py run_dsh()：FALLBACK 且 over 时不投、按 SKIP 排下次机会；查库出错放行
  并记类名；按 person 计数；cap=0 时 stderr、投递、下次机会与改动前（BASELINE_REV 原文）相同。

库一律是 tmp 下的真实 sqlite（`db._connect` 每次现读 `db.DB_PATH`）；只替换收集情境的输入、
判官与正文拼装（会调模型或读真实目录的那几处），闸门与计数走真实代码。
"""
import importlib.util
import json
import random
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BOT, PERSON, CHAT = "bot4", "chenlulu", "123456789"


# ======================================================================== 公共夹具


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    import db
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "state.db"))
    db.init()
    return db


def _speak(db, who, seconds_ago, *, speak=True, skipped=False):
    db.log_heartbeat(who, int(time.time()) - seconds_ago, 0.5, [], "自由时间", "", "", "",
                     speak, "r", skipped, "", "正文" if speak else "")


def _rows(db, who):
    with sqlite3.connect(db.DB_PATH) as c:
        return c.execute("SELECT judge_speak, judge_reason, skipped, skip_reason, final_text "
                         "FROM heartbeat_log WHERE bot_id=? ORDER BY id", (who,)).fetchall()


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# T4 改动前的提交（dsh-rhythm T3）。取不到（浅克隆、不在 git 工作树里）时只跳过逐字对比，
# 结构断言照跑。不用 HEAD：T4 提交之后 HEAD 就是新代码，对比会变成自己比自己。
BASELINE_REV = "8790177"


def _baseline_copy(tmp_path, rel):
    """BASELINE_REV 里这份文件的原文写到 tmp 下；取不到返回 None。"""
    try:
        src = subprocess.run(["git", "show", "%s:%s" % (BASELINE_REV, rel)], cwd=ROOT,
                             capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    out = tmp_path / ("old_" + Path(rel).name.replace("-", "_"))
    out.write_bytes(src)
    return out


# ======================================================================== quota / db


def test_count_by_person_window_and_flags(tmp_db):
    _speak(tmp_db, PERSON, 60)
    _speak(tmp_db, PERSON, 4 * 3600)
    _speak(tmp_db, PERSON, 6 * 3600)                          # 窗口外
    _speak(tmp_db, PERSON, 60, skipped=True)                  # 跳过的不算
    _speak(tmp_db, PERSON, 60, speak=False, skipped=True)     # 没开口的不算
    _speak(tmp_db, BOT, 60)                                   # 编号名下的不算在 person 头上
    start = int(time.time()) - 5 * 3600
    assert tmp_db.count_self_initiates_since(PERSON, start) == 2
    assert tmp_db.count_self_initiates_since(BOT, start) == 1
    assert tmp_db.count_self_initiates_since("nobody", start) == 0


def test_check_proactive_quota_over_ok_and_sources(tmp_db):
    import quota
    _speak(tmp_db, PERSON, 60)
    _speak(tmp_db, PERSON, 120)
    assert quota.check_proactive_quota(PERSON, {}, {"proactive_max_calls_per_5h": 2}) == ("over", 2, 2)
    assert quota.check_proactive_quota(PERSON, {}, {"proactive_max_calls_per_5h": 3}) == ("ok", 3, 2)
    assert quota.check_proactive_quota(PERSON, {"proactive_max_calls_per_5h": 1}, {}) == ("over", 1, 2)
    # bot 覆盖优先；坏覆盖回落全局；字符串整数在 bot 层认
    assert quota.check_proactive_quota(PERSON, {"proactive_max_calls_per_5h": 1},
                                       {"proactive_max_calls_per_5h": 5}) == ("ok", 5, 2)
    assert quota.check_proactive_quota(PERSON, {"proactive_max_calls_per_5h": 1},
                                       {"proactive_max_calls_per_5h": "abc"}) == ("over", 1, 2)
    assert quota.check_proactive_quota(PERSON, {}, {"proactive_max_calls_per_5h": "2"}) == ("over", 2, 2)
    # 传编号（数据其实记在 person 名下）就永远数到 0——所以调用方必须传 person
    assert quota.check_proactive_quota(BOT, {}, {"proactive_max_calls_per_5h": 2}) == ("ok", 2, 0)


def test_check_proactive_quota_cap0_does_not_touch_db(tmp_db, monkeypatch):
    import quota
    import rhythm_config

    def boom(*a, **k):
        raise AssertionError("cap=0 不该查库")

    monkeypatch.setattr(tmp_db, "count_self_initiates_since", boom)
    assert quota.check_proactive_quota(PERSON, {}, {}) == ("ok", 0, 0)
    assert quota.check_proactive_quota(PERSON, {"proactive_max_calls_per_5h": 0}, None) == ("ok", 0, 0)
    assert quota.check_proactive_quota(PERSON, {"proactive_max_calls_per_5h": 9},
                                       {"proactive_max_calls_per_5h": 0}) == ("ok", 0, 0)   # bot 0 = 不限
    assert quota.DEFAULT_PROACTIVE_MAX_PER_5H == rhythm_config.BEHAVIOR_KEYS[
        "proactive_max_calls_per_5h"]["default"] == 0


def test_check_proactive_quota_db_error_propagates(tmp_db, monkeypatch):
    import quota

    def broken(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(tmp_db, "count_self_initiates_since", broken)
    with pytest.raises(sqlite3.OperationalError):
        quota.check_proactive_quota(PERSON, {}, {"proactive_max_calls_per_5h": 2})


# ======================================================================== prefilter


def test_hard_prefilter_bot_silence_override():
    import datetime
    import prefilter
    from generators.situation import Activity, SituationContext
    from generators.world import WorldSnapshot
    sit = SituationContext(recurring=Activity(name="自由时间", description="", state="free"),
                           sporadic=None, hobby={"name": "想画画", "effect": "", "kind": "obsession"})
    now = datetime.datetime(2026, 10, 28, 14, 0)

    def run(since, g, bot=None):
        return prefilter.hard_prefilter(now, sit, WorldSnapshot(), None, 0.5, since, [], g, bot_cfg=bot)

    assert run(20, {}) == "非静默期(距上次20min<30min)"                 # 默认 30
    assert run(20, {}, {"proactive_silence_threshold_minutes": 0}) is None   # bot 0 = 不拦
    assert run(20, {"silence_threshold_minutes": 5}, {"proactive_silence_threshold_minutes": 60}) == \
        "非静默期(距上次20min<60min)"
    assert run(20, {"silence_threshold_minutes": "10"}) == "非静默期(距上次20min<30min)"   # 全局口径不变
    assert run(20, {"silence_threshold_minutes": 0}) == "非静默期(距上次20min<30min)"
    # 不传 bot_cfg（老调用方式）与改动前一样
    assert prefilter.hard_prefilter(now, sit, WorldSnapshot(), None, 0.5, 20, [],
                                    {"silence_threshold_minutes": 10}) is None


# ======================================================================== life-context.py main()


class LifeWorld:
    """把 life-context 依赖的「外部输入」换成固定值；记录预筛、判官、朋友圈各被调了几次。"""

    def __init__(self, tmp_path, monkeypatch, bot_cfg_extra=None, global_cfg=None, jiwen=None):
        import chat_history
        import config_loader
        import formatter
        import judge
        import pause
        import prefilter
        import signature
        from generators import mood, recent, situation, wildcard, world
        from generators.situation import Activity, SituationContext
        from generators.world import WorldSnapshot
        from jiwen import reader

        self.calls = {"prefilter": 0, "judge": 0, "moment": 0}
        self.monkeypatch = monkeypatch
        ch = tmp_path / "bots" / BOT / "channel"
        ch.mkdir(parents=True, exist_ok=True)
        self.bot_cfg = {"_life_id": PERSON, "bot_channel_path": str(ch), **(bot_cfg_extra or {})}
        self.global_cfg = global_cfg or {}
        sit = SituationContext(recurring=Activity(name="自由时间", description="在家", state="free"),
                               sporadic=None, hobby={"name": "想画画", "effect": "", "kind": "obsession"})
        real_prefilter = prefilter.hard_prefilter

        def spy_prefilter(*a, **k):
            self.calls["prefilter"] += 1
            return real_prefilter(*a, **k)

        def spy_judge(*a, **k):
            self.calls["judge"] += 1
            return "dm", "想说点什么"

        m = monkeypatch
        for var in ("CLAUDEBOTLIFE_BYPASS_SILENCE", "SELF_INITIATE_EARLY"):
            m.delenv(var, raising=False)
        m.setattr(config_loader, "load_global", lambda: self.global_cfg)
        m.setattr(config_loader, "load_bot", lambda bot_id: dict(self.bot_cfg))
        m.setattr(config_loader, "in_sleep_hours", lambda cfg, now: False)
        m.setattr(chat_history, "is_dsh_bot", lambda *a, **k: True)    # dsh bot 不查全站 5h 配额
        m.setattr(chat_history, "mins_since_last_user_msg", lambda *a, **k: 120)
        m.setattr(chat_history, "last_user_msg_ts", lambda *a, **k: 0)
        m.setattr(pause, "is_paused", lambda bot_id: None)
        m.setattr(situation, "collect", lambda *a, **k: sit)
        m.setattr(world, "collect_world", lambda *a, **k: WorldSnapshot())
        m.setattr(wildcard, "pick_today_wildcard", lambda *a, **k: None)
        m.setattr(mood, "mood_at", lambda *a, **k: (0.5, []))
        m.setattr(recent, "recent_assistant_messages", lambda *a, **k: [])
        m.setattr(signature, "maybe_update_signature", lambda *a, **k: None)
        m.setattr(reader, "read", lambda *a, **k: jiwen)
        m.setattr(prefilter, "hard_prefilter", spy_prefilter)
        m.setattr(judge, "judge_action", spy_judge)
        m.setattr(formatter, "format_self_initiate", lambda *a, **k: "主动开口正文")

    def run(self, path, capsys, *args, name="life_context_cap"):
        mod = _load(path, name)
        mod.read_memory_brief = lambda *a, **k: ""
        mod.pick_memory_hook = lambda *a, **k: None

        def spy_moment(*a, **k):
            self.calls["moment"] += 1
            return None

        mod._try_post_moment = spy_moment
        self.monkeypatch.setattr("sys.argv", ["life-context.py", BOT, CHAT, *args])
        capsys.readouterr()
        mod.main()
        cap = capsys.readouterr()
        return json.loads(cap.out.strip().splitlines()[-1]), cap.err


LC = ROOT / "life-context.py"


def test_life_context_over_skips_dm_keeps_moments_logs_person(tmp_path, monkeypatch, capsys, tmp_db):
    w = LifeWorld(tmp_path, monkeypatch, {"proactive_max_calls_per_5h": 2})
    _speak(tmp_db, PERSON, 600)
    _speak(tmp_db, PERSON, 1200)
    out, err = w.run(LC, capsys)
    assert out == {"action": "SKIP", "reason": "proactive_cap:2/2"}
    assert w.calls == {"prefilter": 0, "judge": 0, "moment": 1}, "预筛判官都跳过，朋友圈照走"
    assert "主动上限命中(2/2)" in err
    rows = _rows(tmp_db, PERSON)
    assert rows[-1] == (0, "proactive_cap:2/2", 1, "proactive_cap:2/2", "")   # 记一条 skipped，不计数
    assert len(rows) == 3 and _rows(tmp_db, BOT) == []
    assert tmp_db.count_self_initiates_since(PERSON, int(time.time()) - 5 * 3600) == 2


def test_life_context_counts_person_not_bot_id_and_reaches_cap(tmp_path, monkeypatch, capsys, tmp_db):
    w = LifeWorld(tmp_path, monkeypatch, {"proactive_max_calls_per_5h": 2})
    _speak(tmp_db, BOT, 600)      # 记在编号名下的行不算这个人的
    _speak(tmp_db, BOT, 1200)
    _speak(tmp_db, PERSON, 600)
    out, _ = w.run(LC, capsys, name="lc_a")
    assert out["action"] == "TEXT" and w.calls["judge"] == 1
    assert _rows(tmp_db, PERSON)[-1][0] == 1                  # 这次开口记在 person 名下
    out, _ = w.run(LC, capsys, name="lc_b")                   # 第 2 次开口后已满 → 第 3 轮被拦
    assert out == {"action": "SKIP", "reason": "proactive_cap:2/2"}


def test_life_context_over_also_binds_jiwen_forced_and_early(tmp_path, monkeypatch, capsys, tmp_db):
    w = LifeWorld(tmp_path, monkeypatch, {"proactive_max_calls_per_5h": 1},
                  jiwen={"forced": True, "description": "撑不住了"})
    _speak(tmp_db, PERSON, 60)
    out, _ = w.run(LC, capsys, name="lc_forced")
    assert out == {"action": "SKIP", "reason": "proactive_cap:1/1"}
    monkeypatch.setenv("SELF_INITIATE_EARLY", "1")
    out, _ = w.run(LC, capsys, name="lc_early")
    assert out == {"action": "SKIP", "reason": "proactive_cap:1/1"}
    assert w.calls["judge"] == 0


def test_life_context_bypass_does_not_check(tmp_path, monkeypatch, capsys, tmp_db):
    w = LifeWorld(tmp_path, monkeypatch, {"proactive_max_calls_per_5h": 1})
    _speak(tmp_db, PERSON, 60)
    monkeypatch.setenv("CLAUDEBOTLIFE_BYPASS_SILENCE", "1")
    out, _ = w.run(LC, capsys)
    assert out["action"] == "TEXT"


def test_life_context_db_error_passes_and_logs_class(tmp_path, monkeypatch, capsys, tmp_db):
    w = LifeWorld(tmp_path, monkeypatch, {"proactive_max_calls_per_5h": 1})

    def broken(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(tmp_db, "count_self_initiates_since", broken)
    out, err = w.run(LC, capsys)
    assert out["action"] == "TEXT" and w.calls["prefilter"] == 1 and w.calls["judge"] == 1
    assert "[bot4] 主动上限检查失败（放行）：OperationalError" in err
    assert "database is locked" not in err                     # 只记类名


def test_life_context_dry_run_checks_but_writes_no_heartbeat(tmp_path, monkeypatch, capsys, tmp_db):
    w = LifeWorld(tmp_path, monkeypatch, {"proactive_max_calls_per_5h": 1})
    _speak(tmp_db, PERSON, 60)
    out, _ = w.run(LC, capsys, "--dry-run")
    assert out == {"action": "SKIP", "reason": "proactive_cap:1/1"}
    assert len(_rows(tmp_db, PERSON)) == 1


@pytest.mark.parametrize("bot_extra, glob, jiwen", [
    ({}, {}, None),
    ({"proactive_max_calls_per_5h": 0}, {"proactive_max_calls_per_5h": 3}, None),
    ({}, {}, {"forced": True, "description": "撑不住了"}),
], ids=["no_cap", "bot_cap0", "jiwen_forced"])
def test_life_context_cap0_output_identical_to_before(tmp_path, monkeypatch, capsys, tmp_db,
                                                      bot_extra, glob, jiwen):
    old = _baseline_copy(tmp_path, "life-context.py")
    w = LifeWorld(tmp_path, monkeypatch, bot_extra, glob, jiwen)
    _speak(tmp_db, PERSON, 60)
    _speak(tmp_db, PERSON, 120)

    def boom(*a, **k):
        raise AssertionError("cap=0 不该查库")

    real_count = tmp_db.count_self_initiates_since
    monkeypatch.setattr(tmp_db, "count_self_initiates_since", boom)
    out_new, err_new = w.run(LC, capsys, name="lc_new")
    calls_new = dict(w.calls)
    rows_new = _rows(tmp_db, PERSON)
    # 结构断言（不依赖 git）：没查库、没多记心跳、stderr 里没有上限相关的行
    assert out_new["action"] == "TEXT" and calls_new["judge"] == (0 if jiwen else 1)
    assert len(rows_new) == 3 and rows_new[-1][0] == 1 and rows_new[-1][2] == 0
    assert "主动上限" not in err_new
    if old is None:
        pytest.skip("取不到改动前的原文（%s），只做了结构断言" % BASELINE_REV)
    monkeypatch.setattr(tmp_db, "count_self_initiates_since", real_count)
    w.calls = {"prefilter": 0, "judge": 0, "moment": 0}
    out_old, err_old = w.run(old, capsys, name="lc_old")
    rows_old = _rows(tmp_db, PERSON)
    assert out_new == out_old and out_new["action"] == "TEXT"
    assert calls_new == w.calls
    assert rows_new[-1] == rows_old[-1] and len(rows_old) == len(rows_new) + 1
    assert err_new == err_old


# ======================================================================== self_initiate.py run_dsh() FALLBACK


T = 1_780_000_000


class SiWorld:
    def __init__(self, tmp_path, monkeypatch, si, bot_cfg_extra=None, global_cfg=None,
                 lc_out=None):
        import config_loader
        import gateway_client
        ch = tmp_path / "bots" / BOT / "channel"
        ch.mkdir(parents=True, exist_ok=True)
        self.state = tmp_path / "bots" / BOT / "state" / "self-initiate"
        self.cfg = {"_life_id": PERSON, "bot_channel_path": str(ch), **(bot_cfg_extra or {})}
        self.sent = []
        self.lc_calls = 0

        class R:
            returncode = 0 if lc_out is not None else 1
            stdout = json.dumps(lc_out) if lc_out is not None else ""

        def fake_run(*a, **k):
            self.lc_calls += 1
            return R()

        def inject(cfg, chat, text, key):
            self.sent.append(text)
            return True

        monkeypatch.setattr(config_loader, "load_global", lambda: dict(global_cfg or {}))
        monkeypatch.setattr(gateway_client, "telegram_link", lambda *a, **k: "unknown")
        monkeypatch.setattr(si.subprocess, "run", fake_run)
        monkeypatch.setattr(si, "_inject", inject)

    def next_in(self):
        return int((self.state / (CHAT + ".next")).read_text(encoding="utf-8")) - T


SI = ROOT / "scripts" / "self_initiate.py"


def test_fallback_over_not_delivered_scheduled_as_skip(tmp_path, monkeypatch, capsys, tmp_db):
    si = _load(SI, "si_cap_over")
    w = SiWorld(tmp_path, monkeypatch, si, {"proactive_max_calls_per_5h": 2})
    _speak(tmp_db, PERSON, 600)
    _speak(tmp_db, PERSON, 1200)
    capsys.readouterr()
    assert si.run_dsh(BOT, CHAT, w.cfg, T) == 0
    err = capsys.readouterr().err
    assert w.lc_calls == 1 and w.sent == [], "FALLBACK 且超上限：不投递"
    assert "skip: 主动找你上限(2/2)" in err
    assert 3600 <= w.next_in() <= 7200                       # 按 SKIP 排下次机会（retry_after 缺省档）
    assert not (w.state / (CHAT + ".sent.json")).exists()     # 没投递就不记投递时刻


def test_fallback_under_cap_delivers_and_counts_person(tmp_path, monkeypatch, capsys, tmp_db):
    si = _load(SI, "si_cap_under")
    w = SiWorld(tmp_path, monkeypatch, si, {}, {"proactive_max_calls_per_5h": 2})   # 全局上限
    _speak(tmp_db, BOT, 600)        # 编号名下的不算
    _speak(tmp_db, BOT, 1200)
    _speak(tmp_db, PERSON, 600)
    assert si.run_dsh(BOT, CHAT, w.cfg, T) == 0
    assert len(w.sent) == 1 and w.sent[0].startswith("⟦系统·主动开口⟧")


def test_fallback_db_error_passes_and_logs_class(tmp_path, monkeypatch, capsys, tmp_db):
    si = _load(SI, "si_cap_err")
    w = SiWorld(tmp_path, monkeypatch, si, {"proactive_max_calls_per_5h": 1})

    def broken(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(tmp_db, "count_self_initiates_since", broken)
    capsys.readouterr()
    assert si.run_dsh(BOT, CHAT, w.cfg, T) == 0
    err = capsys.readouterr().err
    assert len(w.sent) == 1
    assert "主动上限检查失败（放行）：OperationalError" in err and "locked" not in err


def test_fallback_global_unreadable_uses_bot_cap(tmp_path, monkeypatch, capsys, tmp_db):
    import config_loader
    si = _load(SI, "si_cap_noglobal")
    w = SiWorld(tmp_path, monkeypatch, si, {"proactive_max_calls_per_5h": 1})

    def bad_global():
        raise OSError("读不出")

    monkeypatch.setattr(config_loader, "load_global", bad_global)
    _speak(tmp_db, PERSON, 60)
    assert si.run_dsh(BOT, CHAT, w.cfg, T) == 0
    assert w.sent == []


def test_text_path_not_rechecked(tmp_path, monkeypatch, capsys, tmp_db):
    # TEXT 已经过 life-context 里的闸：self_initiate 不再查一次（查了会和 life-context 刚记的那条重复算）
    si = _load(SI, "si_cap_text")
    w = SiWorld(tmp_path, monkeypatch, si, {"proactive_max_calls_per_5h": 1},
                lc_out={"action": "TEXT", "text": "想你了"})
    _speak(tmp_db, PERSON, 60)
    assert si.run_dsh(BOT, CHAT, w.cfg, T) == 0
    assert w.sent == ["⟦系统·主动开口⟧ 想你了"]


@pytest.mark.parametrize("bot_extra, glob, lc_out", [
    ({}, {}, None),
    ({"proactive_max_calls_per_5h": 0}, {"proactive_max_calls_per_5h": 2}, None),
    ({}, {}, {"action": "TEXT", "text": "想你了"}),
    ({}, {}, {"action": "SKIP", "reason": "毫无情境素材"}),
], ids=["fallback_no_cap", "fallback_bot_cap0", "text", "skip"])
def test_self_initiate_cap0_identical_to_before(tmp_path, monkeypatch, capsys, tmp_db,
                                                bot_extra, glob, lc_out):
    old_path = _baseline_copy(tmp_path, "scripts/self_initiate.py")
    results = []
    for path, name in ((SI, "si_new"), (old_path, "si_old")):
        if path is None:
            break
        si = _load(path, name)
        w = SiWorld(tmp_path / name, monkeypatch, si, bot_extra, glob, lc_out)
        _speak(tmp_db, PERSON, 60)
        _speak(tmp_db, PERSON, 120)

        def boom(*a, **k):
            raise AssertionError("cap=0 不该查库")

        monkeypatch.setattr(tmp_db, "count_self_initiates_since", boom)
        random.seed(7)
        capsys.readouterr()
        rc = si.run_dsh(BOT, CHAT, w.cfg, T)
        results.append((rc, capsys.readouterr().err, list(w.sent), w.next_in()))
    # 结构断言（不依赖 git）：stderr 里没有上限相关的行；FALLBACK / TEXT 照常投递，SKIP 不投
    assert "主动上限" not in results[0][1]
    assert len(results[0][2]) == (0 if lc_out and lc_out["action"] == "SKIP" else 1)
    if old_path is None:
        pytest.skip("取不到改动前的原文（%s），只做了结构断言" % BASELINE_REV)
    assert results[0] == results[1]
