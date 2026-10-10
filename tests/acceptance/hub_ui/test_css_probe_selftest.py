# -*- coding: utf-8 -*-
"""css_probe 的自测。它是测试工具，判错会让上层守卫假绿或假红，得先钉住它自己。

全部用内联 CSS，不读任何文件。
"""
import pytest

from . import css_probe


def _sheet(css):
    return css_probe.Sheet(css)


def test_后写的同优先级声明覆盖先写的():
    s = _sheet(".x { color: red; } .x { color: blue; }")
    assert s.final(".x", "color", 390) == "blue"


def test_important赢过位置更靠后的普通声明():
    s = _sheet(".x { color: red !important; } .x { color: blue; }")
    assert s.final(".x", "color", 390) == "red"


def test_媒体块只在视口命中时生效():
    css = ".x { color: red; } @media (max-width:480px) { .x { color: blue; } }"
    s = _sheet(css)
    assert s.final(".x", "color", 390) == "blue", "窄屏下媒体块应生效"
    assert s.final(".x", "color", 900) == "red", "桌面下媒体块不该生效"


def test_min_width条件与配色条件():
    css = ("@media (max-width:480px) and (prefers-color-scheme: dark) "
           "{ .x { color: dark; } } @media (min-width:481px) { .x { color: wide; } }")
    s = _sheet(css)
    assert s.final(".x", "color", 390, "dark") == "dark"
    assert s.final(".x", "color", 390, "light") is None, "亮色下暗色块不生效"
    assert s.final(".x", "color", 900, "light") == "wide"


def test_逗号选择器列表里的每一项都登记():
    s = _sheet(".a, .x .b { color: red; }")
    assert s.final(".a", "color", 390) == "red"
    assert s.final(".x .b", "color", 390) == "red"


def test_注释被剥掉不参与匹配():
    s = _sheet("/* .x { color: red; } */ .y { color: blue; }")
    assert s.final(".x", "color", 390) is None
    assert s.final(".y", "color", 390) == "blue"


def test_认不出的媒体条件按不生效处理():
    s = _sheet("@media (hover: hover) { .x { color: red; } }")
    assert s.final(".x", "color", 390) is None


def test_认不出的选择器不误命中():
    s = _sheet(".x::-webkit-scrollbar { display: none; }")
    assert s.final(".x", "display", 390) is None


@pytest.mark.parametrize("value, expect", [
    ("min(320px, 100%)", 320.0),
    ("320px", 320.0),
    ("min(300px,100%)", 300.0),
    ("100%", None),
    ("min(50%, 100%)", None),
    ("calc(100% - 20px)", None),
])
def test_min_width_floor的解析(value, expect):
    assert css_probe.min_width_floor(value) == expect


@pytest.mark.parametrize("value, expect", [
    ("0 1 auto", 0.0),
    ("1 1 auto", 1.0),
    ("0", 0.0),
    ("1", 1.0),
    ("none", 0.0),
    ("auto", 1.0),
    ("initial", 0.0),
    ("banana", None),
    (None, None),
])
def test_flex_grow的解析(value, expect):
    assert css_probe.flex_grow(value) == expect


def test_提取style块():
    html = "<html><style>.x { color: red; }</style><script>1</script></html>"
    assert css_probe.extract_style(html).strip() == ".x { color: red; }"
