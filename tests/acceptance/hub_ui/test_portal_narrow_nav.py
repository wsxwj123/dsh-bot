# -*- coding: utf-8 -*-
"""门户窄屏导航契约：bot 那排要独占一整行，工具项让到第二行。

手机上（≤480）bot-switcher 原来与画风/管理台/日期几个不定宽工具项抢宽度，
被挤到几乎看不到。实测 390 宽下：bot-switcher 独占 362x31 的第一行、
nav-tools 在第二行（y=51），五个 chip 全部可见可横滑。
"""
import re


def test_窄屏导航bot那排独占一整行_工具项换到第二行(sandbox, make_client):
    html = make_client().get("/").get_data(as_text=True)

    # 结构：三个工具项与日期包在一个容器里，窄屏规则才能把整排挪到第二行
    tools = re.search(r'<div class="nav-tools">(.*?)</div>', html, re.S)
    assert tools, "工具项没有独立容器，窄屏规则挪不动整排"
    for keep in ("/styles", "/hub", "date-link", "img-provider-chip"):
        assert keep in tools.group(1), "工具项 %s 被挪出容器或被删" % keep

    block = re.search(r"@media \(max-width:480px\) \{(.*?)\n\}", html, re.S)
    assert block, "找不到 480 断点"
    css = block.group(1)
    assert ".topnav-inner { flex-wrap:wrap" in css, "窄屏导航不折行"
    assert ".bot-switcher { flex:1 0 100%; }" in css, "bot 那排没有独占整行的规则"
    assert ".nav-tools { width:100%" in css, "工具项没有让到第二行"
