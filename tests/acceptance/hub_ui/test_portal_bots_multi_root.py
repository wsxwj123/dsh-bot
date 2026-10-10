# -*- coding: utf-8 -*-
"""门户两页的 bot 名单：新旧两套配置根都要出现在页面上（混跑期）。

朋友圈页的 chip 与画风页的 bot 清单都从配置根现读。新系统那份 ``enabled: false``
的意思是"现在跑在旧系统"，不是"没有这个 bot"，页面上必须照样有入口。
"""
import json
import re
from pathlib import Path


def _write(cfg_dir, bot_id, **fields):
    cfg_dir.mkdir(parents=True, exist_ok=True)
    text = "id: %s\n" % bot_id
    for k, v in fields.items():
        text += "%s: %s\n" % (k, v)
    (cfg_dir / ("%s.yml" % bot_id)).write_text(text, encoding="utf-8")


def _chips(html):
    """朋友圈页顶部 bot-switcher 里的 (href, 文字)。"""
    block = re.search(r'<div class="bot-switcher">(.*?)</div>', html, re.S)
    assert block, "朋友圈页里没有 bot-switcher"
    return re.findall(r'<a class="bot-chip[^"]*" href="([^"]*)"[^>]*>([^<]*)</a>', block.group(1))


def test_朋友圈页列两套根的bot_停用的也在(sandbox, make_client):
    legacy = Path(sandbox["env"]["HUB_CONFIGS_DIR"])
    dsh = Path(sandbox["env"]["HUB_CONFIGS_DSH_DIR"])
    _write(legacy, "bot2", display_name="李彤彤")
    _write(dsh, "bot2", enabled="false", display_name="bot2")   # 跑在旧系统：新系统那份停用
    life = sandbox["tmp"] / "chenlulu.yml"
    life.write_text("id: chenlulu\ndisplay_name: 陈露露\n", encoding="utf-8")
    _write(dsh, "bot4", display_name="陈露露", life_config=life)

    html = make_client().get("/").get_data(as_text=True)
    chips = _chips(html)
    assert ("/?bot=bot2", "bot2") in chips, chips
    # 朋友圈里 bot4 记的是 life 名（chenlulu），chip 得指向它才点得进真数据
    assert ("/?bot=chenlulu", "陈露露") in chips, chips


def test_画风页的bot清单含两套根_且bot4的id是配置文件名(sandbox, make_client):
    dsh = Path(sandbox["env"]["HUB_CONFIGS_DSH_DIR"])
    life = sandbox["tmp"] / "chenlulu.yml"
    life.write_text("id: chenlulu\ndisplay_name: 陈露露\n", encoding="utf-8")
    _write(dsh, "bot4", display_name="陈露露", life_config=life)

    html = make_client().get("/styles").get_data(as_text=True)
    m = re.search(r"const BOTS = (\[[^;]*\]);", html, re.S)
    assert m, "画风页没有下发 BOTS"
    ids = [b["id"] for b in json.loads(m.group(1))]
    # 生图脚本按配置文件名查 active_by_bot，换成 life 别名（chenlulu）会让画风静默失效
    assert ids == ["bot4"], ids
