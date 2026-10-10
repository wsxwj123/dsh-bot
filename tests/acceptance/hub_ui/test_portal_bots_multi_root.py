# -*- coding: utf-8 -*-
"""朋友圈页与画风页的 bot 名单。守用户看得见的结果，不守实现结构。

用户报的问题（原话）。为什么你改完之后 bot4 不在朋友圈和画风里面了。正面口径是下面三条。

1. 朋友圈页顶部列出两套配置根（旧系统 HUB_CONFIGS_DIR、新系统 HUB_CONFIGS_DSH_DIR）
   里所有 bot，含 enabled:false 的（那只表示"这会儿跑在另一套系统"，不是没有这个
   bot）。每只一个入口，点进去是它自己的朋友圈。名单按完整相等判，少一个、多一个、
   重复一个都是红。
2. 画风页每一条的 id 必须是配置文件名（bot4 那条就是 bot4，不是 life 别名 chenlulu）。
   画风绑定按这个 id 存进画风数据文件，换成别名会让已存在的绑定在页面上落空。
3. 同一个朋友圈名（life 名）在两套配置里各有一条记录时，朋友圈页只出一个入口（数据
   只有一份），画风页两条都保留（画风按配置存），并且撞车要在 stderr 留痕，不许静默。

场景按真实混跑期的形状摆。旧根三只在跑（bot2、bot3、yasuna）；新根 bot2 与 bot5 的
那份写着 enabled:false，bot4 在新系统跑；bot5 的 life 指向旧根的 yasuna，两处撞同一个
朋友圈名（真实机器上就是这么撞的）。配置里还混着 .bak 与下划线开头的干扰文件，名单
里出现它们同样是红。
"""
import json
import re
from pathlib import Path

import pytest

from .conftest import write_bot_cfg

# 朋友圈页顶部 bot-switcher 里应该出现的全部入口，(链接, 显示名)。
# bot4 在朋友圈里记的是 life 名 chenlulu，它的入口就该指向 chenlulu。
EXPECTED_CHIPS = [
    ("/", "主页"),
    ("/?bot=bot2", "李彤彤"),
    ("/?bot=bot3", "菜菜"),
    ("/?bot=chenlulu", "陈露露"),
    ("/?bot=yasuna", "淑仪"),
]

# "点进去看它自己的朋友圈"只对 bot 入口成立，主页不属于任何一只 bot
BOT_ENTRIES = [(h, n) for h, n in EXPECTED_CHIPS if h != "/"]

# 画风页的全部条目 id，一律是配置文件名。bot5 与 yasuna 是两条记录，不许合并。
EXPECTED_STYLE_BOT_IDS = ["bot2", "bot3", "bot4", "bot5", "yasuna"]


def _chips(html):
    """朋友圈页顶部 bot-switcher 里的 (href, 文字)。属性顺序无关。"""
    block = re.search(r'<div class="[^"]*bot-switcher[^"]*">(.*?)</div>', html, re.S)
    assert block, "朋友圈页里找不到 bot-switcher（顶栏 bot 那排）"
    out = []
    for attrs, label in re.findall(r"<a\b([^>]*)>(.*?)</a>", block.group(1), re.S):
        m = re.search(r'href="([^"]*)"', attrs)
        assert m, "bot 入口的 <a> 没有 href，属性=%r" % attrs
        out.append((m.group(1), label.strip()))
    return out


def _page_bots(html):
    """画风页下发的 BOTS 数组。"""
    m = re.search(r"const BOTS = (\[.*?\]);", html, re.S)
    assert m, "画风页没有下发 BOTS"
    return json.loads(m.group(1))


@pytest.fixture
def two_roots(sandbox):
    """摆好两套配置根与 life 配置，返回路径字典。"""
    legacy = Path(sandbox["env"]["HUB_CONFIGS_DIR"])
    dsh = Path(sandbox["env"]["HUB_CONFIGS_DSH_DIR"])
    run = sandbox["tmp"] / "run"
    life = sandbox["tmp"] / "life"
    for name, disp in (("bot2", "李彤彤"), ("chenlulu", "陈露露"), ("yasuna", "淑仪")):
        write_bot_cfg(life, name, display_name=disp)

    write_bot_cfg(legacy, "bot2", display_name="李彤彤",
               bot_channel_path=run / "legacy-bot2", chat_id="77")
    write_bot_cfg(legacy, "bot3", display_name="菜菜",
               bot_channel_path=run / "legacy-bot3", chat_id="77")
    write_bot_cfg(legacy, "yasuna", display_name="淑仪",
               bot_channel_path=run / "legacy-yasuna", chat_id="77")

    write_bot_cfg(dsh, "bot2", enabled="false", display_name="李彤彤",
               life_config=life / "bot2.yml",
               bot_channel_path=run / "dsh-bot2", chat_id="77")
    write_bot_cfg(dsh, "bot4", display_name="陈露露",
               life_config=life / "chenlulu.yml",
               bot_channel_path=run / "dsh-bot4", chat_id="77")
    write_bot_cfg(dsh, "bot5", enabled="false", display_name="bot5",
               life_config=life / "yasuna.yml",
               bot_channel_path=run / "dsh-bot5", chat_id="77")

    # 干扰文件。备份与下划线开头的一律不许进名单
    (legacy / "bot3.yml.bak-20261004").write_text("id: bot3\n", encoding="utf-8")
    (legacy / "_example.yml").write_text("id: example\n", encoding="utf-8")
    (dsh / "bot4.yml.orig").write_text("id: bot4\n", encoding="utf-8")
    return {"legacy": legacy, "dsh": dsh, "life": life, "tmp": sandbox["tmp"]}


def test_朋友圈页顶部名单_两套根齐全_含停用的bot4(two_roots, hub):
    html = hub.get("/").get_data(as_text=True)
    chips = _chips(html)
    got = sorted(chips)
    assert got == sorted(EXPECTED_CHIPS), (
        "朋友圈页顶部名单不对（少一个、多一个、混进备份文件、或某只指向了别人的"
        "朋友圈都算）。期望 %s，实得 %s" % (sorted(EXPECTED_CHIPS), got))
    hrefs = [h for h, _n in chips]
    assert len(hrefs) == len(set(hrefs)), "同一个入口在页面上出现了两次，%s" % chips


@pytest.mark.parametrize("href, name", BOT_ENTRIES)
def test_朋友圈页每个入口点进去是它自己的朋友圈(two_roots, hub, href, name):
    r = hub.get(href)
    assert r.status_code == 200, "%s 期望 200，实得 %s" % (href, r.status_code)
    m = re.search(r"<title>(.*?)</title>", r.get_data(as_text=True), re.S)
    assert m, "页面没有 <title>"
    assert name in m.group(1), (
        "%s 打开的页面标题是 %r，不是 %s 的朋友圈" % (href, m.group(1), name))


def test_画风页每条的id就是配置文件名(two_roots, hub, styles_env):
    html = hub.get("/styles").get_data(as_text=True)
    bots = _page_bots(html)
    ids = sorted(b["id"] for b in bots)
    assert ids == EXPECTED_STYLE_BOT_IDS, (
        "画风页的 id 必须是配置文件名（bot4 那条不许写成 life 别名 chenlulu），"
        "停用的 bot 也要在（画风是数据，切回来要接着用）。期望 %s，实得 %s"
        % (EXPECTED_STYLE_BOT_IDS, ids))
    names = {b["id"]: b.get("name") for b in bots}
    assert names["bot4"] == "陈露露", "bot4 条目的显示名不对，实得 %r" % names["bot4"]


def test_画风页的id与画风数据文件里的绑定键对得上(two_roots, hub, styles_env):
    """最要紧的一条。绑定按配置文件名存，页面给出的 id 必须和它对齐。"""
    styles_env.write({
        "active": "s1",
        "active_by_bot": {"bot4": "s1"},
        "styles": [{"id": "s1", "name": "蓝调", "positive_prefix": "blue",
                    "negative_prefix": "", "params": {},
                    "sample_image": None, "created_at": 0}],
    })
    html = hub.get("/styles").get_data(as_text=True)
    ids = {b["id"] for b in _page_bots(html)}
    assert "bot4" in ids, "画风页没有 bot4 条目，实得 %s" % sorted(ids)

    # 数据文件里已经存在的绑定键，页面上必须有对应条目，否则那条绑定没人看得见也改不了
    stale = set(styles_env.read()["active_by_bot"]) - ids
    assert not stale, "画风数据文件里这些绑定键在画风页上没有条目 %s" % sorted(stale)

    # 拿页面给出的 id 设置绑定必须成功，写进文件的键就是它
    r = hub.post("/api/styles/active", json={"id": "s1", "bot": "bot4"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert styles_env.read()["active_by_bot"]["bot4"] == "s1"

    # life 别名要明确拒绝。静默接受等于把绑定写进没人查的键，画风静默失效
    r2 = hub.post("/api/styles/active", json={"id": "s1", "bot": "chenlulu"})
    assert r2.status_code == 400, (
        "life 别名 chenlulu 被当成 bot id 接受了（%s），绑定会写到错键上"
        % r2.status_code)


def test_撞同一个朋友圈名时_朋友圈只出一个入口且留痕(two_roots, hub, capsys, styles_env):
    html = hub.get("/").get_data(as_text=True)
    chips = _chips(html)
    yasuna = [(h, n) for h, n in chips if h == "/?bot=yasuna"]
    assert yasuna == [("/?bot=yasuna", "淑仪")], (
        "朋友圈名 yasuna 撞车后应只出一个入口，且是在跑的旧根那份（淑仪）。实得 %s"
        % yasuna)

    # 不许静默。撞了哪两份配置要能在 stderr 里查出来
    err = capsys.readouterr().err
    lines = [ln for ln in err.splitlines() if "yasuna" in ln and "bot5" in ln]
    assert lines, ("撞车没有留痕，stderr 里找不到同时提到 yasuna 与 bot5 的行。"
                   "stderr 尾部 %r" % err[-400:])

    # 画风页按配置列，撞名不合并。少一条等于数据静默丢人
    ids = {b["id"] for b in _page_bots(hub.get("/styles").get_data(as_text=True))}
    assert {"bot5", "yasuna"} <= ids, (
        "画风页少了撞名里的某一条，实得 %s" % sorted(ids))


def test_撞名两份都在跑时_入口留在新系统那份(sandbox, hub):
    """两份配置都在跑是异常状态，但要有个确定行为。按修复时定的挑法，在跑的
    那份优先，都跑着时新系统优先，用户看到的显示名与数据指向跟正在跑的系统一致。"""
    legacy = Path(sandbox["env"]["HUB_CONFIGS_DIR"])
    dsh = Path(sandbox["env"]["HUB_CONFIGS_DSH_DIR"])
    life = sandbox["tmp"] / "life"
    write_bot_cfg(life, "yasuna", display_name="淑仪")
    write_bot_cfg(legacy, "yasuna", display_name="旧系统淑仪",
                  bot_channel_path=sandbox["tmp"] / "run" / "legacy-yasuna", chat_id="77")
    write_bot_cfg(dsh, "bot5", display_name="新系统五号",
                  life_config=life / "yasuna.yml",
                  bot_channel_path=sandbox["tmp"] / "run" / "dsh-bot5", chat_id="77")

    chips = _chips(hub.get("/").get_data(as_text=True))
    yasuna = [(h, n) for h, n in chips if h == "/?bot=yasuna"]
    assert yasuna == [("/?bot=yasuna", "新系统五号")], (
        "两份都在跑时应留新系统那份，显示名跟着它。实得 %s" % yasuna)
