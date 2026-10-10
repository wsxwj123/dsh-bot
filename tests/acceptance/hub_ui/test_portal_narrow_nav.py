# -*- coding: utf-8 -*-
"""窄屏与桌面下 bot 那排的可见性。守的是"保证结果的样式属性"，渲染结果必须人工看。

用户报的问题（原话）。手机上朋友圈主页完全看不到所有 bot，被 novelai/comfyui 画风
与管理台挤掉了。真正的判定要浏览器按 flex 布局算一遍，CI 上没有浏览器，所以这里退
一层，只算"最终生效的样式属性"里那几条**保证 bot 那排还看得见**的。

  1. 顶栏容器允许换行（最终 flex-wrap 是 wrap）。一行放不下时整排换到下一行，
     而不是被其他项压缩；
  2. bot 那排有最小宽度（最终 min-width 的下界不小于 300px，5 个 chip 的实测所需）。
     换行或被压缩时它也拿得住宽度，不会缩成一条缝；
  3. bot 那排没有被藏起来（最终 display 不是 none、visibility 不是 hidden、
     width / height 不是 0、opacity 不是 0）；
  4. 工具项（画风 / 管理台 / 日期）不抢宽度（最终 flex-grow 是 0）。

三种视口（390 窄屏、481 断点之上、900 桌面）乘亮暗两种配色都要成立。

注意。本文件守的是"保证结果的那些属性"，不是渲染结果本身。"样式属性都对但浏览器里
还是看不到"在理论上仍可能（比如未来换了别的布局机制），真正的判定要人工在窄屏
（约 390 宽）与桌面（约 900 宽）各看一眼，五个 chip（主页 / 李彤彤 / 菜菜 / 陈露露 /
淑仪）要全部在顶栏里看得见。
"""
import pytest

from . import css_probe

# 5 个 chip（主页加 4 只 bot）实测需要的宽度，来自修复记录里 headless Chrome 的实量。
# bot 数量明显增加后这个常量要人重新量，别让它一路保持旧值。
CHIP_ROW_NEED_PX = 300


def _check_bot_row_guarantees(html, width, scheme):
    """把该视口下最终生效的四条保证属性验一遍。"""
    sheet = css_probe.Sheet(css_probe.extract_style(html))

    wrap = sheet.final(".topnav-inner", "flex-wrap", width, scheme)
    assert wrap == "wrap", (
        "视口 %s（%s）下顶栏容器最终 flex-wrap=%r，放不下时不会整排换行，"
        "bot 那排会被工具项压缩" % (width, scheme, wrap))

    raw = sheet.final(".bot-switcher", "min-width", width, scheme)
    floor = css_probe.min_width_floor(raw)
    assert floor is not None, (
        "视口 %s（%s）下 bot 那排的 min-width=%r 本工具不认识，先扩展 css_probe "
        "的解析再放行" % (width, scheme, raw))
    assert floor >= CHIP_ROW_NEED_PX, (
        "视口 %s（%s）下 bot 那排的最小宽度下界只有 %spx，5 个 chip 要 %spx，"
        "会被缩到看不全" % (width, scheme, floor, CHIP_ROW_NEED_PX))

    for prop, bad in (("display", "none"), ("visibility", "hidden"),
                      ("opacity", "0"), ("width", "0"), ("height", "0")):
        got = sheet.final(".bot-switcher", prop, width, scheme)
        assert got not in (bad, "0px", "0%"), (
            "视口 %s（%s）下 bot 那排最终 %s=%r，等于把它藏起来了"
            % (width, scheme, prop, got))

    grow = sheet.final(".nav-tools", "flex-grow", width, scheme)
    if grow is None:
        grow = css_probe.flex_grow(sheet.final(".nav-tools", "flex", width, scheme))
    assert grow == 0, (
        "视口 %s（%s）下工具项最终 flex-grow=%r，会去抢 bot 那排的宽度"
        % (width, scheme, grow))


@pytest.fixture
def feed_html(hub):
    return hub.get("/").get_data(as_text=True)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_窄屏390_bot排可见的保证属性_真渲染要人工看(feed_html, scheme):
    _check_bot_row_guarantees(feed_html, 390, scheme)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_481宽_bot排可见的保证属性_480断点之上也要有(feed_html, scheme):
    """上一轮修复只护了 480 以下，481 到 1080 又回退被裁（后来补修）。
    这里在断点之上取一档，基础规则必须照样给出保证，不许只活在媒体块里。"""
    _check_bot_row_guarantees(feed_html, 481, scheme)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_桌面900_bot排可见的保证属性_不许比改动前差(feed_html, scheme):
    """改动前 900 宽下 bot chip 都看得见，现在也得看得见。同样只能守属性，
    渲染结果要人工在桌面宽度看一眼。"""
    _check_bot_row_guarantees(feed_html, 900, scheme)
