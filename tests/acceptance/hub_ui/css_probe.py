# -*- coding: utf-8 -*-
"""feed.html 里 <style> 的极简层叠求值器，只服务本目录的验收测试。

为什么需要它。用户报的"手机上完全看不到所有 bot"要真浏览器按 flex 布局算一遍才能
下结论，CI 上没有浏览器。退而求其次，本模块回答一个确定性的小问题。某个选择器的
某条属性，在给定视口宽度与配色下**最终生效**的值是什么。按文档顺序算层叠，后面的
规则覆盖前面的、!important 优先，所以"某条规则写着，但被后面的规则覆盖掉"这种情形
不会漏。它不模拟 flex 布局、不解释 calc() 等复杂值，布局结果仍需人工在真浏览器里看。

本模块认得的写法（认不出的返回 None，调用方必须把 None 当失败处理，不许放行）。
- 媒体条件 max-width:Npx、min-width:Npx、prefers-color-scheme:dark|light 及 and 组合
- min-width 值 Npx 与 min(Npx, 100%)
- flex 简写的 flex-grow 首个数值

已知局限（新增写法时要先扩展本模块）。
- 声明按分号切，不处理值里带分号的 url() 数据（那种 key 是垃圾名，查询用不到它）
- 认不出的媒体特征一律按"不生效"处理
- 选择器只做逗号列表里的逐字匹配，不做组合器与伪类推导
"""
import re

_COND_NUM = re.compile(r"^(min|max)-width\s*:\s*([\d.]+)px$")
_COND_COLOR = re.compile(r"^prefers-color-scheme\s*:\s*(dark|light)$")
_MEDIA_TYPES = {"screen": True, "all": True, "print": False}


def extract_style(html):
    """页面里 <style> 块的 CSS 文本。没有就断言失败（页面结构变了）。"""
    m = re.search(r"<style>(.*?)</style>", html, re.S)
    assert m, "页面里没有 <style> 块，没法判定样式"
    return m.group(1)


def _media_matches(cond, width, scheme):
    """媒体条件在（视口宽度, 配色）下是否生效。"""
    if cond is None:
        return True
    for part in re.split(r"\band\b", cond, flags=re.I):
        part = part.strip().strip("()").strip()
        if not part:
            continue
        low = part.lower()
        if low in _MEDIA_TYPES:
            if not _MEDIA_TYPES[low]:
                return False
            continue
        m = _COND_NUM.match(low)
        if m:
            n = float(m.group(2))
            if m.group(1) == "max" and width > n:
                return False
            if m.group(1) == "min" and width < n:
                return False
            continue
        m = _COND_COLOR.match(low)
        if m:
            if m.group(1) != scheme:
                return False
            continue
        return False
    return True


def _match_brace(text, i):
    """text[i] 是 '{'，返回配对的 '}' 下标。"""
    depth = 0
    for k in range(i, len(text)):
        if text[k] == "{":
            depth += 1
        elif text[k] == "}":
            depth -= 1
            if depth == 0:
                return k
    raise ValueError("CSS 花括号不配对")


def _parse_decls(body):
    """一条规则体内的声明。值超简单，键小写。返回 {prop: (value, important)}。"""
    decls = {}
    for item in body.split(";"):
        if ":" not in item:
            continue
        key, _, val = item.partition(":")
        key = key.strip().lower()
        val = val.strip()
        if not key:
            continue
        important = False
        if val.lower().endswith("!important"):
            important = True
            val = val[: -len("!important")].rstrip()
        decls[key] = (val, important)
    return decls


class Sheet:
    """一份样式表。rules 按文档顺序展开为 (媒体条件, 选择器, 声明表)。"""

    def __init__(self, css):
        self.rules = []
        self._parse(re.sub(r"/\*.*?\*/", "", css, flags=re.S), None)

    def _parse(self, text, media):
        i = 0
        while True:
            j = text.find("{", i)
            if j == -1:
                return
            head = text[i:j].strip()
            k = _match_brace(text, j)
            body = text[j + 1:k]
            i = k + 1
            if not head:
                continue
            if head.startswith("@"):
                if head.lower().startswith("@media"):
                    cond = head[len("@media"):].strip()
                    inner = ("%s and %s" % (media, cond)) if media else cond
                    self._parse(body, inner)
                # 其它 at 规则（@keyframes 等）里没有要查的选择器规则，跳过
                continue
            decls = _parse_decls(body)
            for sel in head.split(","):
                sel = sel.strip()
                if sel:
                    self.rules.append((media, sel, decls))

    def final(self, selector, prop, width, scheme="light"):
        """最终生效值。没有匹配的声明返回 None。

        层叠规则按 CSS 简化，!important 优先，同级别看文档顺序，靠后的赢。
        """
        best = None
        for order, (media, sel, decls) in enumerate(self.rules):
            if sel != selector or prop not in decls:
                continue
            if not _media_matches(media, width, scheme):
                continue
            value, important = decls[prop]
            rank = (1 if important else 0, order)
            if best is None or rank >= best[0]:
                best = (rank, value)
        return None if best is None else best[1]


def min_width_floor(value):
    """min-width 声明值翻译成"保证的下界像素"。认不得返回 None。

    认两种写法。
    - min(320px, 100%)。极窄屏被 100% 封顶，容器 >=320 宽时保证 320
    - 320px
    """
    if not isinstance(value, str):
        return None
    v = value.strip()
    m = re.fullmatch(r"min\(\s*([\d.]+)px\s*,\s*100%\s*\)", v)
    if m:
        return float(m.group(1))
    m = re.fullmatch(r"([\d.]+)px", v)
    if m:
        return float(m.group(1))
    return None


def flex_grow(value):
    """flex 简写或 flex-grow 值的 flex-grow。0 1 auto 返回 0.0。认不得返回 None。"""
    if not isinstance(value, str):
        return None
    parts = value.split()
    if not parts:
        return None
    special = {"none": 0.0, "auto": 1.0, "initial": 0.0}
    if parts[0] in special:
        return special[parts[0]]
    try:
        return float(parts[0])
    except ValueError:
        return None
