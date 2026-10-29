# -*- coding: utf-8 -*-
"""proactive-cooldown-config 验收（黑盒）：主动开口冷却区间可配置。

依据只有 .devflow/proactive-cooldown-config-接口.md 的行为清单与错误契约，
一条行为一个用例。

被测入口：scripts/self_initiate.py 中将新增的冷却区间计算函数。
配置读取：config_loader.load_bot(bot_id)、config_loader.load_global()。
冷却区间 = 两次主动开口之间的随机等待秒数，由 min/max 分钟数换算。

取值优先级：bot yml > 全局 _global.yml > 代码默认（min 30 / max 1440 分钟）。

函数签名约定（实现后才有）：
    get_proactive_cooldown_range(bot_cfg, global_cfg) -> (min_sec, max_sec)

如果 self_initiate 尚未暴露该函数，测试会在 import 阶段 skip，
实现后移除 skip 即可全部变绿。

运行：cd /Users/wsxwj/dsh-bot-dev && python3 -m pytest tests/acceptance/proactive_cooldown_config -q
"""
import sys
from pathlib import Path

import pytest

# 确保仓库根目录在 sys.path 以便 import scripts 下的模块
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ------------------------------------------------------------------
# 尝试从 self_initiate 导入冷却区间函数；尚未实现时全部 skip
# ------------------------------------------------------------------
try:
    from scripts.self_initiate import get_proactive_cooldown_range  # type: ignore[import]
    _IMPL_READY = True
except ImportError:
    _IMPL_READY = False

    def get_proactive_cooldown_range(bot_cfg, global_cfg):
        """占位桩，不会真正被调用。"""
        raise NotImplementedError

skip_if_not_impl = pytest.mark.skipif(
    not _IMPL_READY,
    reason="get_proactive_cooldown_range 尚未实现，等实现后再跑",
)

# ------------------------------------------------------------------
# 常量
# ------------------------------------------------------------------
DEFAULT_MIN_MIN = 30     # 默认最短分钟
DEFAULT_MAX_MIN = 1440   # 默认最长分钟
DEFAULT_MIN_SEC = DEFAULT_MIN_MIN * 60   # 1800
DEFAULT_MAX_SEC = DEFAULT_MAX_MIN * 60   # 86400


# ------------------------------------------------------------------
# 行为 1：bot yml 写 min=10 max=60，全局不写 -> 600-3600 秒
# ------------------------------------------------------------------
@skip_if_not_impl
def test_行为1_bot配置10到60_全局不写_冷却600到3600秒():
    bot_cfg = {
        "proactive_cooldown_min_minutes": 10,
        "proactive_cooldown_max_minutes": 60,
    }
    global_cfg = {}
    min_sec, max_sec = get_proactive_cooldown_range(bot_cfg, global_cfg)
    assert min_sec == 600, "bot 配 min=10 分钟，期望 600 秒，实际 %d" % min_sec
    assert max_sec == 3600, "bot 配 max=60 分钟，期望 3600 秒，实际 %d" % max_sec


# ------------------------------------------------------------------
# 行为 2：bot 不写，全局写 min=5 max=120 -> 300-7200 秒
# ------------------------------------------------------------------
@skip_if_not_impl
def test_行为2_bot不写_全局配置5到120_冷却300到7200秒():
    bot_cfg = {}
    global_cfg = {
        "proactive_cooldown_min_minutes": 5,
        "proactive_cooldown_max_minutes": 120,
    }
    min_sec, max_sec = get_proactive_cooldown_range(bot_cfg, global_cfg)
    assert min_sec == 300, "全局配 min=5 分钟，期望 300 秒，实际 %d" % min_sec
    assert max_sec == 7200, "全局配 max=120 分钟，期望 7200 秒，实际 %d" % max_sec


# ------------------------------------------------------------------
# 行为 3：两处都不写 -> 默认 1800-86400 秒
# ------------------------------------------------------------------
@skip_if_not_impl
def test_行为3_两处都不写_默认1800到86400秒():
    min_sec, max_sec = get_proactive_cooldown_range({}, {})
    assert min_sec == DEFAULT_MIN_SEC, "默认 min 期望 1800 秒，实际 %d" % min_sec
    assert max_sec == DEFAULT_MAX_SEC, "默认 max 期望 86400 秒，实际 %d" % max_sec


# ------------------------------------------------------------------
# 行为 4：bot 写 10、全局写 5 -> bot 优先用 10
# ------------------------------------------------------------------
@skip_if_not_impl
def test_行为4_bot写10全局写5_bot优先():
    bot_cfg = {"proactive_cooldown_min_minutes": 10}
    global_cfg = {"proactive_cooldown_min_minutes": 5}
    min_sec, _ = get_proactive_cooldown_range(bot_cfg, global_cfg)
    assert min_sec == 600, "bot 优先，期望 600 秒（10 分钟），实际 %d" % min_sec


# ------------------------------------------------------------------
# 行为 5：配置写 0 / 负数 / 字符串 / 布尔 -> 该键回退默认，不抛异常
# ------------------------------------------------------------------
@skip_if_not_impl
@pytest.mark.parametrize(
    "bad_min, bad_max",
    [
        (0, 60),
        (-5, 60),
        ("abc", 60),
        (True, 60),
        (10, 0),
        (10, -1),
        (10, "xyz"),
        (10, False),
        (None, None),
    ],
    ids=[
        "min=0", "min=负数", "min=字符串", "min=布尔",
        "max=0", "max=负数", "max=字符串", "max=布尔",
        "两个都是None",
    ],
)
def test_行为5_非法值回退默认_不抛异常(bad_min, bad_max):
    bot_cfg = {}
    global_cfg = {}
    if bad_min is not None:
        bot_cfg["proactive_cooldown_min_minutes"] = bad_min
    if bad_max is not None:
        bot_cfg["proactive_cooldown_max_minutes"] = bad_max
    # 不应抛异常
    min_sec, max_sec = get_proactive_cooldown_range(bot_cfg, global_cfg)
    # 非法的那个键应回退默认
    if bad_min is not None and not (isinstance(bad_min, int) and not isinstance(bad_min, bool) and bad_min > 0):
        assert min_sec == DEFAULT_MIN_SEC, (
            "非法 min=%r 应回退默认 %d 秒，实际 %d" % (bad_min, DEFAULT_MIN_SEC, min_sec))
    if bad_max is not None and not (isinstance(bad_max, int) and not isinstance(bad_max, bool) and bad_max > 0):
        assert max_sec == DEFAULT_MAX_SEC, (
            "非法 max=%r 应回退默认 %d 秒，实际 %d" % (bad_max, DEFAULT_MAX_SEC, max_sec))
    # 确保 min <= max
    assert min_sec <= max_sec, "min_sec=%d 不应大于 max_sec=%d" % (min_sec, max_sec)


# ------------------------------------------------------------------
# 行为 6：min > max（如 min=120 max=10）-> 自动交换，冷却 600-7200 秒
# ------------------------------------------------------------------
@skip_if_not_impl
def test_行为6_min大于max_自动交换():
    bot_cfg = {
        "proactive_cooldown_min_minutes": 120,
        "proactive_cooldown_max_minutes": 10,
    }
    min_sec, max_sec = get_proactive_cooldown_range(bot_cfg, {})
    assert min_sec == 600, "交换后 min 期望 600 秒（10 分钟），实际 %d" % min_sec
    assert max_sec == 7200, "交换后 max 期望 7200 秒（120 分钟），实际 %d" % max_sec


# ------------------------------------------------------------------
# 行为 7：只写 min 不写 max -> max 取默认 1440 分钟
# ------------------------------------------------------------------
@skip_if_not_impl
def test_行为7_只写min不写max_max取默认1440分钟():
    bot_cfg = {"proactive_cooldown_min_minutes": 10}
    min_sec, max_sec = get_proactive_cooldown_range(bot_cfg, {})
    assert min_sec == 600, "min 期望 600 秒，实际 %d" % min_sec
    assert max_sec == DEFAULT_MAX_SEC, "max 期望默认 86400 秒，实际 %d" % max_sec


# ------------------------------------------------------------------
# 行为 8：只写 max 不写 min -> min 取默认 30 分钟
# ------------------------------------------------------------------
@skip_if_not_impl
def test_行为8_只写max不写min_min取默认30分钟():
    bot_cfg = {"proactive_cooldown_max_minutes": 60}
    min_sec, max_sec = get_proactive_cooldown_range(bot_cfg, {})
    assert min_sec == DEFAULT_MIN_SEC, "min 期望默认 1800 秒，实际 %d" % min_sec
    assert max_sec == 3600, "max 期望 3600 秒，实际 %d" % max_sec


# ------------------------------------------------------------------
# 行为 9：想念值满提前开口的三个约束不受影响
# ------------------------------------------------------------------
@skip_if_not_impl
def test_行为9_想念值满提前开口约束不受冷却配置影响():
    """冷却配置不应改变 EARLY_MIN_GAP / EARLY_MAX_PER_DAY / EARLY_RETRY 常量。

    直接从 self_initiate 读取这三个常量，断言它们保持原值。
    """
    from scripts import self_initiate as si  # type: ignore[import]
    assert si.EARLY_MIN_GAP == 3 * 3600, (
        "提前开口最短间隔应保持 3 小时（10800 秒），实际 %d" % si.EARLY_MIN_GAP)
    assert si.EARLY_MAX_PER_DAY == 2, (
        "提前开口每天上限应保持 2 次，实际 %d" % si.EARLY_MAX_PER_DAY)
    assert si.EARLY_RETRY == 1800, (
        "提前开口重试间隔应保持 1800 秒，实际 %d" % si.EARLY_RETRY)


# ------------------------------------------------------------------
# 行为 10：正常投递后冷却 = random.randint(min_sec, max_sec)
# ------------------------------------------------------------------
@skip_if_not_impl
def test_行为10_正常投递冷却在配置范围内():
    """多次调用 get_proactive_cooldown_range 确认返回的区间可直接用于 randint。

    断言 min_sec <= max_sec 且类型为 int，保证 random.randint(min_sec, max_sec) 不报错。
    """
    import random

    bot_cfg = {
        "proactive_cooldown_min_minutes": 15,
        "proactive_cooldown_max_minutes": 45,
    }
    min_sec, max_sec = get_proactive_cooldown_range(bot_cfg, {})
    assert isinstance(min_sec, int), "min_sec 应为 int，实际 %s" % type(min_sec).__name__
    assert isinstance(max_sec, int), "max_sec 应为 int，实际 %s" % type(max_sec).__name__
    assert min_sec == 900, "15 分钟期望 900 秒，实际 %d" % min_sec
    assert max_sec == 2700, "45 分钟期望 2700 秒，实际 %d" % max_sec
    # 验证可直接喂给 randint
    cooldown = random.randint(min_sec, max_sec)
    assert 900 <= cooldown <= 2700, "randint 结果 %d 不在 900-2700" % cooldown
