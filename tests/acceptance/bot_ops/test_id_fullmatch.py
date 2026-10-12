# -*- coding: utf-8 -*-
"""§0 第 6 条（接口约定第 7 稿）：bot id、供应商名、token 的形状校验是整串匹配。

依据 `INTERFACE-新系统bot运维.md` §0 第 6 条那张表与「可断言」那一句，§6「第 7 稿对这一段的
三点收紧」，§11c、§12b 各自的 `bad_provider` 一格：

| 入口 | 形状非法时 |
|---|---|
| §6 新建，请求体的 `bot_id` | 400 `bad_field`，磁盘零产物，零子进程 |
| §4 单只重启、§4b 开关、§10 编辑的读与写，路径里的 `<bot_id>` | 400 `bad_bot_id` |
| §11c 刷新与预览、§12b 测试连接，路径里的供应商名 | 400 `bad_provider`，零转发 |

每条用例旁边都摆着一只**合格的同名 bot**（或一个会答 200 的假网关）：被测代码要是把名字
「修一修」（去掉空白、只比到换行之前）再接着用，就会真的动到那只 bot、真的转发出去，
用例红得明明白白。

名字一律是假名。会走到新建、重启、开关流程的用例，假 launchctl 与假 bun 都在位。
"""
import sqlite3
from urllib.parse import quote

import pytest

from . import botops_util as u

GOOD = "xinbot"                     # 合格的假名；非法形态都由它变出来

#: bot id 的非法形态。前五种是 §0 第 6 条「可断言」那句点名的，后三种是同一类（首尾空白、换行）
BAD_BOT_IDS = [
    pytest.param(GOOD + "\n", id="trailing-newline"),
    pytest.param(" " + GOOD, id="leading-space"),
    pytest.param(GOOD + " ", id="trailing-space"),
    pytest.param("Xinbot", id="uppercase"),
    pytest.param("xin-bot", id="hyphen"),
    pytest.param(GOOD + "\r\n", id="trailing-crlf"),
    pytest.param("\n" + GOOD, id="leading-newline"),
    pytest.param(GOOD + "\t", id="trailing-tab"),
]

PROVIDER = "myproxy"
#: 供应商名的非法形态。只取换行与首尾空白：大写与连字符对供应商名本来就合法（见本文件末尾）
BAD_PROVIDER_NAMES = [
    pytest.param(PROVIDER + "\n", id="trailing-newline"),
    pytest.param(" " + PROVIDER, id="leading-space"),
    pytest.param(PROVIDER + " ", id="trailing-space"),
    pytest.param(PROVIDER + "\r\n", id="trailing-crlf"),
]

PREVIEW = "/hub/api/dsh-model/provider/%s/preview"
REFRESH = "/hub/api/dsh-model/provider/%s/refresh"
TEST = "/hub/api/dsh-model/provider/%s/test"
GW_PATH = {PREVIEW: "/v1/provider/preview", REFRESH: "/v1/provider/refresh", TEST: "/v1/provider/test"}
PROVIDER_ENDPOINTS = [pytest.param(PREVIEW, id="preview"), pytest.param(REFRESH, id="refresh"),
                      pytest.param(TEST, id="test")]


def _seg(name):
    """名字放进路径的一段：换行写作 %0A，空格写作 %20。"""
    return quote(name, safe="")


def _body(r):
    """响应体；不是 JSON 时把原文头 200 字带出来，断言失败时看得见拿到了什么。"""
    j = r.get_json(silent=True)
    return j if isinstance(j, dict) else {"_不是JSON对象": r.get_data(as_text=True)[:200]}


def _create_payload(**over):
    body = {"bot_id": GOOD, "telegram_token": u.FAKE_TOKEN, "dispatcher_port": u.free_port(),
            "chat_id": 123456789, "display_name": "新来的", "persona_text": "新来的人设。"}
    body.update(over)
    return body


def _all_roots(sandbox):
    """新建会落盘的全部位置（§7）加上旧系统两个根。"""
    return u.freeze_roots(sandbox.dsh_cfg, sandbox.dsh_home, sandbox.legacy_cfg,
                          sandbox.channels, sandbox.agents)


@pytest.fixture
def short_waits(monkeypatch):
    """重启等待上限（45 秒）与新建探活上限（30 秒）都是「测试可改」的模块常量（§1），收紧。

    功能还没按第 7 稿实现时，被测代码可能把非法名字放进去、起一个等心跳的流程；
    不收紧的话每条红用例都要陪它等满。
    """
    u.tighten_constant(monkeypatch, value=45, new=3.0)
    u.tighten_constant(monkeypatch, value=30, new=1.0)


# ============================================================ §4 单只重启：路径里的 bot id

@pytest.mark.parametrize("bad", BAD_BOT_IDS)
def test_单只重启_id不是整串合格_400_bad_bot_id_零调用零任务(
        client, sandbox, launchctl, bun, as_darwin, short_waits, monkeypatch, bad):
    u.dsh_bot(sandbox.dsh_cfg, GOOD)                 # 合格的那只真的在、启用着
    launchctl.set_default(installed=True, running=True, pid=4242)
    frozen = u.freeze_roots(sandbox.dsh_cfg, sandbox.legacy_cfg)
    r = client.post("/hub/api/bots/%s/restart" % _seg(bad))
    assert r.status_code == 400, ("名字 %r 应当是形状非法" % bad, r.status_code, _body(r))
    assert _body(r).get("error") == "bad_bot_id", (bad, _body(r))
    assert launchctl.calls() == [], ("形状非法还调了 launchctl", launchctl.calls())
    u.assert_no_job_left(client, monkeypatch, sandbox.tmp)
    assert launchctl.calls() == [], launchctl.calls()
    assert bun.calls() == [], bun.calls()
    u.assert_roots_untouched(frozen)


# ============================================================ §4b 开关：路径里的 bot id

@pytest.mark.parametrize("bad", BAD_BOT_IDS)
def test_开关_id不是整串合格_400_bad_bot_id_合格那只的配置不动_零调用零任务(
        client, sandbox, launchctl, bun, as_darwin, short_waits, monkeypatch, bad):
    u.dsh_bot(sandbox.dsh_cfg, GOOD)
    launchctl.set_default(installed=True, running=True, pid=4242)
    frozen = u.freeze_roots(sandbox.dsh_cfg, sandbox.legacy_cfg)
    r = client.post("/hub/api/bots/%s/enabled" % _seg(bad), json={"enabled": False})
    assert r.status_code == 400, ("名字 %r 应当是形状非法" % bad, r.status_code, _body(r))
    assert _body(r) == {"error": "bad_bot_id", "detail": "bot id 形状非法"}, (bad, _body(r))
    u.assert_roots_untouched(frozen)
    assert launchctl.calls() == [], ("形状非法还调了 launchctl", launchctl.calls())
    u.assert_no_job_left(client, monkeypatch, sandbox.tmp)
    assert launchctl.calls() == [], launchctl.calls()
    assert bun.calls() == [], bun.calls()


# ============================================================ §10 编辑：路径里的 bot id

GOOD_PERSONA = "合格那只的人设 GOOD-PERSONA-51c2"


def _editable(sandbox):
    """一只能正常编辑的合格 bot：配置在新系统配置根，人设与白名单在它家的默认位置。"""
    yml = u.dsh_bot(sandbox.dsh_cfg, GOOD, display_name="旧名")
    ch = sandbox.dsh_home / "bots" / GOOD / "channel"
    ch.mkdir(parents=True)
    (ch / "CLAUDE.md").write_text(GOOD_PERSONA, encoding="utf-8")
    (ch / "access.json").write_text('{"allowFrom": [1]}\n', encoding="utf-8")
    return yml


@pytest.mark.parametrize("bad", BAD_BOT_IDS)
def test_编辑读_id不是整串合格_400_bad_bot_id_不把合格那只的内容读出来(
        web_client, sandbox, launchctl, bad):
    _editable(sandbox)
    r = web_client.get("/hub/api/bots/%s/edit" % _seg(bad))
    assert r.status_code == 400, ("名字 %r 应当是形状非法" % bad, r.status_code, _body(r))
    assert _body(r).get("error") == "bad_bot_id", (bad, _body(r))
    assert GOOD_PERSONA not in r.get_data(as_text=True), "形状非法的请求读出了合格那只的人设"
    assert launchctl.calls() == [], launchctl.calls()


@pytest.mark.parametrize("bad", BAD_BOT_IDS)
def test_编辑写_id不是整串合格_400_bad_bot_id_文件与朋友圈库都不动(
        web_client, sandbox, launchctl, botlife_db, bad):
    _editable(sandbox)
    frozen = u.freeze_roots(sandbox.dsh_cfg, sandbox.dsh_home, sandbox.legacy_cfg)
    r = web_client.post("/hub/api/bots/%s/edit" % _seg(bad),
                        json={"display_name": "想改成这个", "persona": "想换的人设",
                              "fish_voice_id": "v9"})
    assert r.status_code == 400, ("名字 %r 应当是形状非法" % bad, r.status_code, _body(r))
    body = _body(r)
    assert body.get("error") == "bad_bot_id", (bad, body)
    assert isinstance(body.get("detail"), str) and body["detail"], body
    u.assert_roots_untouched(frozen)
    con = sqlite3.connect(str(botlife_db.DB_PATH))
    try:
        names = [row[0] for row in con.execute("SELECT display_name FROM bot_profiles")]
    finally:
        con.close()
    assert "想改成这个" not in names, ("被拒的编辑写进了朋友圈库", names)
    assert launchctl.calls() == [], launchctl.calls()


# ============================================================ §6 新建：请求体的 bot_id

@pytest.mark.parametrize("bad", BAD_BOT_IDS)
def test_新建_bot_id不是整串合格_400_bad_field_磁盘零产物_零子进程_零getMe(
        client, sandbox, bun, telegram, launchctl, as_darwin, short_waits, bad):
    telegram.mode = "ok"                             # 一切外部条件都就绪：放进去就真能建出来
    launchctl.set_default(installed=True, running=True, pid=4242)
    frozen = _all_roots(sandbox)
    r = client.post("/hub/api/bots", json=_create_payload(bot_id=bad))
    assert r.status_code == 400, ("bot_id %r 应当是形状非法" % bad, r.status_code, _body(r))
    assert _body(r).get("error") == "bad_field", (bad, _body(r))
    u.assert_roots_untouched(frozen)
    assert bun.calls() == [], ("形状非法还起了 bun", bun.calls())
    assert launchctl.calls() == [], ("形状非法还调了 launchctl", launchctl.calls())
    assert telegram.hits == [], ("形状非法还发了 getMe", telegram.hits)


@pytest.mark.parametrize("reserved", u.RESERVED_IDS)
def test_新建_保留名后面跟一个换行_400_bad_field_零产物_假件里没有它的单元名(
        client, sandbox, bun, telegram, launchctl, as_darwin, short_waits, reserved):
    """§6：`"jiwen"` 后面跟一个换行在第一段就回 400，不许「形状过了、值却不等于保留名」。"""
    telegram.mode = "ok"
    launchctl.set_default(installed=True, running=True, pid=4242)
    frozen = _all_roots(sandbox)
    r = client.post("/hub/api/bots", json=_create_payload(bot_id=reserved + "\n"))
    assert r.status_code == 400, (reserved, r.status_code, _body(r))
    assert _body(r).get("error") == "bad_field", (reserved, _body(r))
    u.assert_roots_untouched(frozen)
    assert bun.calls() == [], ("保留名加换行还起了 bun", bun.calls())
    assert launchctl.calls() == [], ("保留名加换行还调了 launchctl", launchctl.calls())
    assert telegram.hits == [], telegram.hits
    u.assert_units_never_named(launchctl, *u.RESERVED_IDS)
    u.assert_units_never_named(bun, *u.RESERVED_IDS)


def test_新建_在跑的bot名后面跟一个换行_400_bad_field_既有那只一个字节不动(
        client, sandbox, bun, telegram, launchctl, heartbeat, as_darwin, short_waits):
    """§6：同名配置与家目录占用这两项检查拿的是原值逐字比；带换行的在形状这一段就被挡掉。

    `runbot` 是这条用例自己造出来的假 bot：配置在、家目录在、心跳新鲜、launchd 说在跑。
    """
    u.dsh_bot(sandbox.dsh_cfg, "runbot")
    ch = sandbox.dsh_home / "bots" / "runbot" / "channel"
    ch.mkdir(parents=True)
    (ch / ".env").write_text("TELEGRAM_BOT_TOKEN=%s\n" % u.FAKE_TOKEN_OTHER_HEAD, encoding="utf-8")
    heartbeat.write("runbot", pid=4242)              # 只写一次：下面要逐字节比对它的家目录
    launchctl.set_unit("runbot", installed=True, running=True, pid=4242)
    telegram.mode = "ok"
    frozen = _all_roots(sandbox)
    r = client.post("/hub/api/bots", json=_create_payload(bot_id="runbot\n"))
    assert r.status_code == 400, (r.status_code, _body(r))
    assert _body(r).get("error") == "bad_field", _body(r)
    u.assert_roots_untouched(frozen)
    assert bun.calls() == [], bun.calls()
    assert launchctl.calls() == [], launchctl.calls()
    assert telegram.hits == [], telegram.hits


# ============================================================ §6 token：整串匹配

TOKEN_TAILS = [pytest.param("\n", id="trailing-newline"), pytest.param("\r\n", id="trailing-crlf")]


@pytest.mark.parametrize("tail", TOKEN_TAILS)
def test_新建_token末尾带换行_本地预检就是malformed_一个请求都不发_零产物(
        client, sandbox, bun, telegram, launchctl, as_darwin, short_waits, tail):
    telegram.mode = "ok"                             # 真发出去的话桩会答「这个 token 是好的」
    launchctl.set_default(installed=True, running=True, pid=4242)
    frozen = _all_roots(sandbox)
    r = client.post("/hub/api/bots", json=_create_payload(telegram_token=u.FAKE_TOKEN + tail))
    assert r.status_code == 200, (r.status_code, _body(r))
    body = _body(r)
    assert body.get("ok") is False and body.get("stage") == "malformed", body
    assert telegram.hits == [], ("带换行的 token 被发出去了", telegram.hits)
    u.assert_roots_untouched(frozen)
    assert bun.calls() == [], bun.calls()
    assert launchctl.calls() == [], launchctl.calls()


@pytest.mark.parametrize("tail", TOKEN_TAILS)
def test_测token_token末尾带换行_本地预检就是malformed_一个请求都不发(
        client, sandbox, telegram, tail):
    telegram.mode = "ok"
    r = client.post("/hub/api/bots/test_token", json={"telegram_token": u.FAKE_TOKEN + tail})
    assert r.status_code == 200, (r.status_code, _body(r))
    body = _body(r)
    assert body.get("ok") is False and body.get("stage") == "malformed", body
    assert telegram.hits == [], ("带换行的 token 被发出去了", telegram.hits)


def test_测token_合格的token照常发出去_对照(client, sandbox, telegram):
    """对照：同一个桩、同一个端点，不带换行的 token 是会发出去并测通的。

    没有这条，上面两条的「一个请求都不发」分不清是预检拦住了还是桩根本没接上。
    """
    telegram.mode = "ok"
    r = client.post("/hub/api/bots/test_token", json={"telegram_token": u.FAKE_TOKEN})
    assert r.status_code == 200, (r.status_code, _body(r))
    assert _body(r).get("ok") is True, _body(r)
    assert len(telegram.hits) == 1, telegram.hits


# ============================================================ §11c、§12b：路径里的供应商名

def _gateway_that_says_yes(gw_bot, gw_stub):
    """一个在跑的假网关，三条路由都答 200：名字要是被放过去转发了，这里会留下记录。"""
    gw_bot(port=gw_stub.port)
    for path in GW_PATH.values():
        gw_stub.on(path, 200, {"ok": True, "count": 0, "models": [], "text": "假网关答的"})


@pytest.mark.parametrize("tpl", PROVIDER_ENDPOINTS)
@pytest.mark.parametrize("name", BAD_PROVIDER_NAMES)
def test_供应商名不是整串合格_400_bad_provider_零转发(web_client, gw_stub, gw_bot, tpl, name):
    _gateway_that_says_yes(gw_bot, gw_stub)
    r = web_client.post(tpl % _seg(name), json={})
    assert r.status_code == 400, ("供应商名 %r 应当是形状非法" % name, r.status_code, _body(r))
    assert _body(r) == {"ok": False, "error": "bad_provider", "text": "供应商名字不合法"}, _body(r)
    assert gw_stub.requests == [], ("形状非法还转发给了网关", gw_stub.requests)


@pytest.mark.parametrize("tpl", PROVIDER_ENDPOINTS)
def test_供应商名带大写下划线连字符_本来就合法_整串匹配之后照常转发(
        web_client, gw_stub, gw_bot, tpl):
    """反向：整串匹配只是不再放过首尾多出来的东西，供应商名自己的字符集没有变。

    供应商名的规则在 `INTERFACE-管理台UI.md`：`^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$`，
    大写、下划线、连字符都合法（本约定 §11a 也写着「大小写与下划线经 normName 匹配」）。
    §0 第 6 条「可断言」那句把大写、带连字符两种写法一并套到供应商名入口上，与这条规则对不上，
    这里按供应商名自己的规则量；矛盾已写进交回报告。
    """
    _gateway_that_says_yes(gw_bot, gw_stub)
    name = "My-Proxy_2"
    r = web_client.post(tpl % _seg(name), json={})
    assert r.status_code == 200, (r.status_code, _body(r))
    hits = gw_stub.hits(GW_PATH[tpl])
    assert len(hits) == 1, gw_stub.requests
    assert hits[0]["body"] == {"name": name}, hits[0]["body"]


# ============================================================ §5b 内部契约：同一把尺

@pytest.mark.parametrize("bad", BAD_BOT_IDS)
def test_内部契约_dsh_bot_dir_名字不是整串合格就回None(sandbox, bad):
    """§5b 第 3 条：`config_sources.dsh_bot_dir(bot_id)` 名字不过形状校验回 None；
    第 5 条：形状那条正则只留一份，整串匹配。拼家目录路径的地方不许放过带换行的名字。"""
    from moments import config_sources
    got = config_sources.dsh_bot_dir(bad)
    assert got is None, ("名字 %r 不合格，不该拼出家目录路径" % bad, got)


def test_内部契约_dsh_bot_dir_合格的名字照常给出家目录_对照(sandbox):
    from moments import config_sources
    got = config_sources.dsh_bot_dir(GOOD)
    assert got is not None and str(got) == str(sandbox.dsh_home / "bots" / GOOD), got
