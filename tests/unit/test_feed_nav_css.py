# -*- coding: utf-8 -*-
"""朋友圈页顶栏的宽度契约。

原来折行规则只写在 480 断点里，481 往上回到旧行为：容器上限 580，工具项
（画风/管理台/日期）固定吃掉约 296px，bot 那排被挤到 137px，横滑条又被
scrollbar-width:none 藏了。现在的口径是基础规则里就折行，且给 bot 那排一个
最小宽度（放不下整排换行，而不是互相压缩），完整覆盖各宽度。
"""
import re
from pathlib import Path

TPL = Path(__file__).resolve().parents[2] / "moments" / "templates" / "feed.html"


def _rule(css: str, selector: str) -> str:
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, "模板里找不到规则 %s" % selector
    return m.group(1)


def test_折行与最小宽度写在基础规则里不靠断点():
    css = TPL.read_text(encoding="utf-8")
    # 取到的都是第一条（基础）规则；断点里的同名规则在文件更后面
    assert "flex-wrap:wrap" in _rule(css, ".topnav-inner")
    switcher = _rule(css, ".bot-switcher")
    assert "flex:1 1 auto" in switcher
    assert "min-width:min(320px, 100%)" in switcher, "bot 那排没有最小宽度，会被工具项压成一条缝"
    assert "flex:0 1 auto" in _rule(css, ".nav-tools")
