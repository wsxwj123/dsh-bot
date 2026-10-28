# -*- coding: utf-8 -*-
"""silence-threshold-config 验收（黑盒）：静默期拆成「发圈」「主动开口」两个独立数字。

依据只有 .devflow/silence-threshold-config-接口.md 的行为清单与错误契约，一条行为一个用例。
只调用接口文件列出的公开入口：

  * moments.post.should_post_moment(...)   发圈判定（新增关键字参数 global_cfg）
  * prefilter.hard_prefilter(...)          主动开口预筛
  * config_loader.load_global()            全局配置读取
  * moments.hub_config.configs_dir()       管理台配置根
  * moments.hub_config.FIELD_META          管理台参数元数据

输入数据用公开数据类 generators.situation / generators.world 构造；不 import 被测实现模块，
不 mock 被测逻辑。行为 6 用已有的 db_module 注入点喂一条固定历史朋友圈，不碰真实数据库。

运行：在仓库根目录  python3 -m pytest tests/acceptance/silence_threshold_config -q
"""
import datetime
from pathlib import Path

import pytest

import config_loader
import prefilter
from generators.situation import Activity, SituationContext
from generators.world import WorldSnapshot
from moments import hub_config
from moments import post

NOW = datetime.datetime(2026, 10, 28, 12, 0)
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MINUTES = 30
MOMENTS_KEY = "moments.silence_threshold_minutes"
TOP_KEY = "silence_threshold_minutes"


def make_situation():
    """固定的可发状态：自由时间、无临时活动、无爱好触发。"""
    return SituationContext(
        recurring=Activity(name="自由时间", description="在家放松", state="free"),
        sporadic=None, hobby=None)


def run_post(since_user_min, global_cfg=None, *, force=False, db_module=None):
    """走公开的发圈判定入口。

    mood 0.9 是强信号，静默期外必然命中 kind（不会因概率门随机放行/拦截）。
    global_cfg 传 None 表示按旧调用方式不传该参数（行为 6 回归用）。
    """
    kwargs = {
        "situation": make_situation(),
        "world": WorldSnapshot(),
        "wildcard": None,
        "mood": 0.9,
        "jiwen_info": None,
        "bot_cfg": {},
        "since_user_min": since_user_min,
        "now": NOW,
        "db_module": db_module,
        "force": force,
    }
    if global_cfg is not None:
        kwargs["global_cfg"] = global_cfg
    return post.should_post_moment(**kwargs)


def run_prefilter(since_user_min, global_cfg):
    return prefilter.hard_prefilter(
        NOW, make_situation(), WorldSnapshot(), None, 0.9,
        since_user_min, [], global_cfg)


class _DbWithRecentMoment:
    """db_module 注入点：只回答「最近的朋友圈」，喂一条 2 小时前同 kind 的圈。"""

    def list_moments(self, *args, **kwargs):
        return [{
            "id": 1,
            "ts": int(NOW.timestamp()) - 2 * 3600,
            "created_at": NOW - datetime.timedelta(hours=2),
            "kind": "mood_extreme",
            "moment_kind": "mood_extreme",
            "text": "两小时前的朋友圈",
            "visibility": "public",
        }]

    def count_moments_today(self, *args, **kwargs):
        return 0


# ---------------------------------------------------------------- 行为 1

def test_发圈_配置5分钟_距上条消息10分钟_不受静默拦截():
    go, kind, reason = run_post(10, {"moments": {"silence_threshold_minutes": 5}})
    assert go is True, "阈值 5 分钟、距上条消息 10 分钟，应放行：kind=%r reason=%r" % (kind, reason)
    assert "silence" not in reason


# ---------------------------------------------------------------- 行为 2

def test_发圈_配置5分钟_距上条消息3分钟_被静默拦住():
    go, kind, reason = run_post(3, {"moments": {"silence_threshold_minutes": 5}})
    assert go is False, "阈值 5 分钟、距上条消息 3 分钟，应被拦住：kind=%r reason=%r" % (kind, reason)
    assert "silence" in reason


# ---------------------------------------------------------------- 行为 3

def test_发圈_没有该配置_默认30分钟_10分钟被拦40分钟放行():
    blocked, _, blocked_reason = run_post(10, {})
    assert blocked is False, "无配置应按默认 30 分钟，10 分钟被拦：reason=%r" % (blocked_reason,)
    assert "silence" in blocked_reason

    allowed, _, allowed_reason = run_post(40, {})
    assert allowed is True, "无配置应按默认 30 分钟，40 分钟放行：reason=%r" % (allowed_reason,)
    assert "silence" not in allowed_reason


# ---------------------------------------------------------------- 行为 4

@pytest.mark.parametrize("bad_value", [0, -5, "10"], ids=["0", "负数", "非数字字符串"])
def test_发圈_配置非法_按默认30分钟_10分钟被拦(bad_value):
    go, kind, reason = run_post(10, {"moments": {"silence_threshold_minutes": bad_value}})
    assert go is False, "非法值 %r 应按默认 30 分钟拦住 10 分钟：kind=%r reason=%r" % (
        bad_value, kind, reason)
    assert "silence" in reason


# ---------------------------------------------------------------- 行为 5

def test_发圈_用户明确要求_距上条消息1分钟也放行():
    go, kind, reason = run_post(1, {}, force=True)
    assert go is True, "force=True 时静默期不拦：kind=%r reason=%r" % (kind, reason)
    assert "silence" not in reason


# ---------------------------------------------------------------- 行为 6

def test_发圈_用户明确要求_同一天同kind四小时内仍可发():
    go, kind, reason = run_post(120, None, force=True, db_module=_DbWithRecentMoment())
    assert go is True, "force=True 时同 kind 4 小时闸不应拦住：kind=%r reason=%r" % (kind, reason)


# ---------------------------------------------------------------- 行为 7

def test_主动开口_顶层配置5分钟_距上条消息10分钟_不受静默拦截():
    reason = run_prefilter(10, {"silence_threshold_minutes": 5})
    assert reason is None, "阈值 5 分钟、距上条消息 10 分钟，不该被静默拦住：%r" % (reason,)


# ---------------------------------------------------------------- 行为 8

def test_主动开口_顶层配置20分钟_距上条消息10分钟_被静默拦住():
    reason = run_prefilter(10, {"silence_threshold_minutes": 20})
    assert reason is not None, "阈值 20 分钟、距上条消息 10 分钟，应被静默拦住"
    assert "静默" in reason


# ---------------------------------------------------------------- 行为 9

def test_主动开口_顶层配置缺失或非法_都按默认30分钟():
    missing_blocked = run_prefilter(10, {})
    assert missing_blocked is not None, "配置缺失应按默认 30 分钟拦住 10 分钟"
    assert "静默" in missing_blocked

    missing_allowed = run_prefilter(40, {})
    assert missing_allowed is None, "配置缺失应按默认 30 分钟放行 40 分钟：%r" % (missing_allowed,)

    for bad_value in (0, -3, "abc"):
        illegal_reason = run_prefilter(10, {"silence_threshold_minutes": bad_value})
        assert illegal_reason is not None, "非法值 %r 应按默认 30 分钟拦住 10 分钟" % (bad_value,)
        assert "静默" in illegal_reason


# ---------------------------------------------------------------- 行为 10

def test_两个阈值互相独立_嵌套5顶层200_发圈放行主动开口被拦():
    cfg = {"moments": {"silence_threshold_minutes": 5},
           "silence_threshold_minutes": 200}

    go, kind, reason = run_post(10, cfg)
    assert go is True, "发圈按嵌套的 5 分钟判，10 分钟应放行：kind=%r reason=%r" % (kind, reason)

    pref_reason = run_prefilter(10, cfg)
    assert pref_reason is not None, "主动开口按顶层的 200 分钟判，10 分钟应被拦住"
    assert "静默" in pref_reason


# ---------------------------------------------------------------- 行为 11

def test_全局配置读取_尊重HUB_CONFIGS_DIR_目录或文件不存在返回空配置(monkeypatch, tmp_path):
    present = tmp_path / "present"
    present.mkdir()
    (present / "_global.yml").write_text(
        "silence_threshold_minutes: 7\n"
        "moments:\n"
        "  silence_threshold_minutes: 9\n",
        encoding="utf-8")
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(present))
    loaded = config_loader.load_global()
    assert loaded["silence_threshold_minutes"] == 7
    assert loaded["moments"]["silence_threshold_minutes"] == 9

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(empty))
    assert config_loader.load_global() == {}

    monkeypatch.setenv("HUB_CONFIGS_DIR", str(tmp_path / "missing"))
    assert config_loader.load_global() == {}


# ---------------------------------------------------------------- 行为 12

def test_管理台配置根_DSH目录优先_其次HUB_CONFIGS_DIR_最后仓库configs(monkeypatch, tmp_path):
    dsh_dir = tmp_path / "dsh-configs"
    legacy_dir = tmp_path / "legacy-configs"
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", str(dsh_dir))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(legacy_dir))
    assert Path(hub_config.configs_dir()) == dsh_dir

    monkeypatch.delenv("HUB_CONFIGS_DSH_DIR")
    assert Path(hub_config.configs_dir()) == legacy_dir

    monkeypatch.delenv("HUB_CONFIGS_DIR")
    assert Path(hub_config.configs_dir()) == REPO_ROOT / "configs"


# ---------------------------------------------------------------- 行为 13

def test_管理台参数元数据_两个静默阈值字段齐全():
    meta = hub_config.FIELD_META
    assert MOMENTS_KEY in meta, "缺少发圈静默阈值字段：%r" % (MOMENTS_KEY,)
    assert TOP_KEY in meta, "缺少主动开口静默阈值字段：%r" % (TOP_KEY,)

    assert meta[MOMENTS_KEY]["label"] == "刚聊完天后多久内不主动发朋友圈"
    assert meta[TOP_KEY]["label"] == "刚聊完天后多久内不主动找你"

    for key in (MOMENTS_KEY, TOP_KEY):
        assert meta[key]["default"] == DEFAULT_MINUTES, "%s 默认值应为 30" % key
        assert meta[key]["min"] == 1, "%s 下限应为 1" % key
        assert meta[key]["max"] == 1440, "%s 上限应为 1440" % key
        assert meta[key]["unit"] == "分钟", "%s 单位应为分钟" % key
