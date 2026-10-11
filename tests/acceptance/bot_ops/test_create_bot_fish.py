# -*- coding: utf-8 -*-
"""§6b（INTERFACE 第 3 稿新增）：POST /hub/api/bots 的两个可选字段。

`fish_voice_id` 写 `<bot 家>/channel/access.json` 的 `voiceId`；
`fish_api_key` 写 `<bot 家>/channel/.env` 的 `FISH_AUDIO_API_KEY` 行。

覆盖面：
- 四个组合（都不填 / 只音色 / 只 key / 都填）的落盘字节与不报错；
- 「未填」四等价：键缺失、null、空串、纯空白；
- 逐档非法值：确切 400 + `bad_field` + 契约文案；
- 判定顺序：音色三档先于 key 各档；fish 400 排在既有人设二选一之后、
  409 dsh_root_off / bot_exists 之前；
- 安全口径：key 原文不进响应体 / steps / 日志 / 磁盘其他文件；
- 反向断言：都不填时不写任何全局位置、除既有出站（getMe、autostart）外没有别的调用。

契约含糊点（测试按最自然的解读构造，若实现另有官方细则请对照裁决）：
1. 「人设二选一」的具体冲突形态契约未写死（只说"一字不变"的既有校验）。
   本文件按「persona_text 与 persona_template 同时给 = 冲突」构造，
   用于验证 fish 400 排在它之后。
2. access.json 的「确切字节」按 §6b 落盘表字面解读：json 默认分隔符（`, ` 与 `: `）、
   键序 allowFrom→voiceId、尾随换行；表里「均无引号」按 .env 行解读
   （JSON 值引号是语法必需）。
3. 「不调任何语音服务」「key 不进日志」没有契约接缝：测试以
   「除既有 getMe / autostart 出站外无任何调用」+ caplog / capsys / tmp 目录扫描为可观测面。
"""
import json
import logging
import os
import shutil

import pytest

from . import botops_util as u

VOICE = "v_1-A"                        # 合法音色示例：字母数字下划线连字符
KEY = "fk_9aK2mQ7xT4"                  # 合法 key 示例：可打印 ASCII 无空白，>= 8 位
KEY_LEAK = "fk_UniqueMark_a1b2c3d4e5"  # 安全用例专用：带唯一标记，便于全局搜查

ENV_EMPTY = "TELEGRAM_BOT_TOKEN=%s\n" % u.FAKE_TOKEN
ENV_FISH = ENV_EMPTY + "FISH_AUDIO_API_KEY=%s\n" % KEY
ACCESS_EMPTY = '{"allowFrom": [123456789]}\n'
ACCESS_VOICE = '{"allowFrom": [123456789], "voiceId": "%s"}\n' % VOICE

VOICE_BAD_CHARS_DETAIL = "音色 id 只能是字母、数字、下划线或连字符"


def _payload(port, **over):
    body = {"bot_id": "xiaoyu", "telegram_token": u.FAKE_TOKEN, "dispatcher_port": port,
            "chat_id": 123456789, "display_name": "小雨",
            "persona_text": "我是小雨的人设正文。"}
    body.update(over)
    return body


def _post(client, port, **over):
    return client.post("/hub/api/bots", json=_payload(port, **over))


def _arm_ok(bun, telegram, launchctl, heartbeat, bot="xiaoyu", pid=4242):
    telegram.mode = "ok"
    bun.expect(exit=0).expect(exit=0)
    launchctl.set_default(installed=True, running=True, pid=pid)
    heartbeat.tick(bot, pid=pid)


def _channel(sandbox, bot="xiaoyu"):
    return sandbox.dsh_home / "bots" / bot / "channel"


def _env_text(sandbox, bot="xiaoyu"):
    return (_channel(sandbox, bot) / ".env").read_text(encoding="utf-8")


def _access_text(sandbox, bot="xiaoyu"):
    return (_channel(sandbox, bot) / "access.json").read_text(encoding="utf-8")


def _bad(client, sandbox, **over):
    """发一个期望被 400 bad_field 拒掉的请求，顺带断言零落盘。返回响应体。"""
    r = _post(client, u.free_port(), **over)
    assert r.status_code == 400, (r.status_code, r.get_json())
    body = r.get_json()
    assert body["error"] == "bad_field", body
    assert not (sandbox.dsh_cfg / "xiaoyu.yml").exists(), "被 400 拒了还落了盘"
    return body


def _boom_moves(monkeypatch):
    """让"把造物搬到位"这一步必失败（覆盖常见的三种搬移原语）。"""
    def boom(*a, **kw):
        raise OSError("mocked move failure")
    monkeypatch.setattr(shutil, "move", boom)
    monkeypatch.setattr(os, "replace", boom)
    monkeypatch.setattr(os, "rename", boom)


# ------------------------------------------------------------ 落盘字节：四个组合

def test_新建_两字段都不填_201且字节与第2稿一致(
        client, sandbox, bun, telegram, launchctl, heartbeat, as_darwin):
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port())
    assert r.status_code == 201, r.get_json()
    assert _env_text(sandbox) == ENV_EMPTY
    assert _access_text(sandbox) == ACCESS_EMPTY


def test_新建_只填音色_201_access带voiceId且env不变(
        client, sandbox, bun, telegram, launchctl, heartbeat, as_darwin):
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port(), fish_voice_id=VOICE)
    assert r.status_code == 201, r.get_json()
    assert _access_text(sandbox) == ACCESS_VOICE
    assert _env_text(sandbox) == ENV_EMPTY


def test_新建_只填key_201_env加key行且access不变(
        client, sandbox, bun, telegram, launchctl, heartbeat, as_darwin):
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port(), fish_api_key=KEY)
    assert r.status_code == 201, r.get_json()
    assert _env_text(sandbox) == ENV_FISH
    assert _access_text(sandbox) == ACCESS_EMPTY


def test_新建_两字段都填_201_两文件都带新增内容(
        client, sandbox, bun, telegram, launchctl, heartbeat, as_darwin):
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port(), fish_voice_id=VOICE, fish_api_key=KEY)
    assert r.status_code == 201, r.get_json()
    assert _env_text(sandbox) == ENV_FISH
    assert _access_text(sandbox) == ACCESS_VOICE


@pytest.mark.parametrize("field", ["fish_voice_id", "fish_api_key"])
@pytest.mark.parametrize("value", [None, "", "   ", "\t"])
def test_新建_未填的四种形态_与都不填完全等价(
        client, sandbox, bun, telegram, launchctl, heartbeat, as_darwin, field, value):
    """「未填」= 键缺失 / null / 空串 / 纯空白，四者等价（键缺失由前一条用例覆盖）。"""
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port(), **{field: value})
    assert r.status_code == 201, (field, repr(value), r.get_json())
    assert _env_text(sandbox) == ENV_EMPTY
    assert _access_text(sandbox) == ACCESS_EMPTY


def test_新建_音色带首尾空白_按去空白后的值落盘(
        client, sandbox, bun, telegram, launchctl, heartbeat, as_darwin):
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port(), fish_voice_id="  " + VOICE + "  ")
    assert r.status_code == 201, r.get_json()
    assert _access_text(sandbox) == ACCESS_VOICE


def test_新建_key带首尾空白_按去空白后的值落盘(
        client, sandbox, bun, telegram, launchctl, heartbeat, as_darwin):
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port(), fish_api_key="  " + KEY + "  ")
    assert r.status_code == 201, r.get_json()
    assert _env_text(sandbox) == ENV_FISH


# ------------------------------------------------------------ 逐档非法值：400 bad_field

@pytest.mark.parametrize("value", [123, True, [], {}])
def test_新建_音色非字符串_400且文案确切(client, sandbox, telegram, value):
    body = _bad(client, sandbox, fish_voice_id=value)
    assert body["detail"] == "fish_voice_id 必须是字符串"


def test_新建_音色超长65_400且文案确切(client, sandbox, telegram):
    body = _bad(client, sandbox, fish_voice_id="a" * 65)
    assert body["detail"] == "音色 id 最长 64 个字符"


@pytest.mark.parametrize("value", ["a b", "中文", "a.b", "a@b"])
def test_新建_音色字符非法_400且文案确切(client, sandbox, telegram, value):
    body = _bad(client, sandbox, fish_voice_id=value)
    assert body["detail"] == VOICE_BAD_CHARS_DETAIL


@pytest.mark.parametrize("value", [123, True, [], {}])
def test_新建_key非字符串_400且文案确切(client, sandbox, telegram, value):
    body = _bad(client, sandbox, fish_api_key=value)
    assert body["detail"] == "fish_api_key 必须是字符串"


@pytest.mark.parametrize("value", ["abc", "abcdefg", "  ab  "])
def test_新建_key太短_400且文案确切(client, sandbox, telegram, value):
    body = _bad(client, sandbox, fish_api_key=value)
    assert body["detail"] == "Fish key 太短（至少 8 个字符）"


def test_新建_key太长257_400且文案确切(client, sandbox, telegram):
    body = _bad(client, sandbox, fish_api_key="a" * 257)
    assert body["detail"] == "Fish key 太长（最多 256 个字符）"


@pytest.mark.parametrize("value", ["abcd efgh", "abcdefg中文"])
def test_新建_key含空白或非ASCII_400且文案确切(client, sandbox, telegram, value):
    body = _bad(client, sandbox, fish_api_key=value)
    assert body["detail"] == "Fish key 只能是 8-256 位的可打印 ASCII，不含空格"


@pytest.mark.parametrize("value", ["2468135790:" + "A" * 30, "12345678:" + "B" * 31])
def test_新建_key像Telegram_token_400且文案确切(client, sandbox, telegram, value):
    body = _bad(client, sandbox, fish_api_key=value)
    assert body["detail"] == "这看起来是 Telegram bot token，不是 Fish Audio 的 key"


def test_新建_key像模型供应商的_400且文案确切(client, sandbox, telegram):
    body = _bad(client, sandbox, fish_api_key="sk-abcdefgh1234")
    assert body["detail"] == "这看起来是模型供应商的 key（sk- 开头），不是 Fish Audio 的 key"


def test_新建_key是大写SK开头_不算供应商key_201(
        client, sandbox, bun, telegram, launchctl, heartbeat, as_darwin):
    """契约：`sk-` 前缀区分大小写；`SK-` 开头不是该档，形状又合法 → 照常建。"""
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port(), fish_api_key="SK-abcdefgh1234")
    assert r.status_code == 201, r.get_json()
    assert _env_text(sandbox) == ENV_EMPTY + "FISH_AUDIO_API_KEY=SK-abcdefgh1234\n"


def test_新建_音色合法但key太短_照报key的错(client, sandbox, telegram):
    body = _bad(client, sandbox, fish_voice_id=VOICE, fish_api_key="abc")
    assert body["detail"] == "Fish key 太短（至少 8 个字符）"


# ------------------------------------------------------------ 判定顺序

def test_新建_人设二选一错与fish错并存_先报人设错(client, sandbox, telegram):
    """fish 400 排在"人设二选一"之后（§6 第 3 稿新增句）。

    人设冲突按「persona_text 与 persona_template 同时给」构造（契约含糊点，见文件头）。
    """
    body = _payload(u.free_port(), fish_voice_id="bad id!")
    body["persona_template"] = "chenlulu"
    r = client.post("/hub/api/bots", json=body)
    assert r.status_code == 400, r.get_json()
    resp = r.get_json()
    detail = resp.get("detail") or ""
    assert "音色" not in detail and "fish_voice_id" not in detail, resp


def test_新建_fish错与同名已存在并存_先报fish错不报409(client, sandbox, telegram):
    cfg = u.write_bot_yml(sandbox.dsh_cfg, "xiaoyu",
                          display_name="先来的", dispatcher_port=17811)
    before = cfg.read_bytes()
    r = _post(client, u.free_port(), fish_voice_id="bad id!")
    assert r.status_code == 400, r.get_json()
    body = r.get_json()
    assert body["error"] == "bad_field", body
    assert body["detail"] == VOICE_BAD_CHARS_DETAIL
    assert cfg.read_bytes() == before, "既有配置被动过"


def test_新建_fish合法但同名已存在_照旧409_bot_exists(client, sandbox, telegram):
    u.write_bot_yml(sandbox.dsh_cfg, "xiaoyu", display_name="先来的", dispatcher_port=17811)
    r = _post(client, u.free_port(), fish_voice_id=VOICE, fish_api_key=KEY)
    assert r.status_code == 409, r.get_json()
    assert r.get_json()["error"] == "bot_exists", r.get_json()


def test_新建_fish错与新系统根关掉并存_先报fish错(client, sandbox, telegram, monkeypatch):
    monkeypatch.setenv("HUB_CONFIGS_DSH_DIR", "off")
    body = _bad(client, sandbox, fish_voice_id="bad id!")
    assert body["detail"] == VOICE_BAD_CHARS_DETAIL


def test_新建_音色与key都非法_先报音色的错(client, sandbox, telegram):
    body = _bad(client, sandbox, fish_voice_id="bad id!", fish_api_key="abc")
    assert body["detail"] == VOICE_BAD_CHARS_DETAIL


def test_新建_音色既超长又字符非法_先报超长(client, sandbox, telegram):
    body = _bad(client, sandbox, fish_voice_id="!" * 65)
    assert body["detail"] == "音色 id 最长 64 个字符"


def test_新建_key既太短又含空格_先报太短(client, sandbox, telegram):
    body = _bad(client, sandbox, fish_api_key="a b")
    assert body["detail"] == "Fish key 太短（至少 8 个字符）"


def test_新建_key既超长又含空格_先报太长(client, sandbox, telegram):
    body = _bad(client, sandbox, fish_api_key="a " + "b" * 256)
    assert body["detail"] == "Fish key 太长（最多 256 个字符）"


# ------------------------------------------------------------ 合法边界与既有约束不变

@pytest.mark.parametrize("voice", ["a", "a" * 64, "v_1-A"])
def test_新建_音色合法边界_201且原样落盘(
        client, sandbox, bun, telegram, launchctl, heartbeat, as_darwin, voice):
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port(), fish_voice_id=voice)
    assert r.status_code == 201, (voice, r.get_json())
    assert _access_text(sandbox) == '{"allowFrom": [123456789], "voiceId": "%s"}\n' % voice


@pytest.mark.parametrize("key", ["abcdefgh", "k" * 256])
def test_新建_key合法边界_201且原样落盘(
        client, sandbox, bun, telegram, launchctl, heartbeat, as_darwin, key):
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port(), fish_api_key=key)
    assert r.status_code == 201, (len(key), r.get_json())
    assert _env_text(sandbox) == ENV_EMPTY + "FISH_AUDIO_API_KEY=%s\n" % key


@pytest.mark.skipif(os.name == "nt", reason="Windows 无 POSIX 权限位")
def test_新建_两字段都填_两文件权限仍是0600(
        client, sandbox, bun, telegram, launchctl, heartbeat, as_darwin):
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port(), fish_voice_id=VOICE, fish_api_key=KEY)
    assert r.status_code == 201, r.get_json()
    assert u.mode_of(_channel(sandbox) / ".env") == 0o600
    assert u.mode_of(_channel(sandbox) / "access.json") == 0o600


def test_新建_两字段都填_files失败_回滚到操作前(
        client, sandbox, telegram, bun, monkeypatch, as_darwin):
    telegram.mode = "ok"
    before = u.snapshot_tree(sandbox.dsh_home)
    _boom_moves(monkeypatch)
    r = _post(client, u.free_port(), fish_voice_id=VOICE, fish_api_key=KEY)
    assert r.status_code == 500, r.get_json()
    assert not (sandbox.dsh_cfg / "xiaoyu.yml").exists(), "回滚没删干净配置"
    assert not (sandbox.dsh_home / "bots" / "xiaoyu").exists(), "回滚没删干净 bot 家"
    assert u.snapshot_tree(sandbox.dsh_home) == before, "回滚没回到操作前"
    assert KEY not in r.get_data(as_text=True), "500 异常响应里搜到了 key 原文"


# ------------------------------------------------------------ 安全口径：key 原文不许外漏

def test_新建_两字段都填_key原文不进响应体与steps(
        client, sandbox, bun, telegram, launchctl, heartbeat, as_darwin):
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port(), fish_voice_id=VOICE, fish_api_key=KEY_LEAK)
    assert r.status_code == 201, r.get_json()
    raw = r.get_data(as_text=True)
    assert KEY_LEAK not in raw, "响应体里搜到了 key 原文"
    assert KEY_LEAK not in json.dumps(r.get_json().get("steps"), ensure_ascii=False)
    assert KEY_LEAK in _env_text(sandbox), "key 根本没落盘，这条断言就没意义了"


def test_新建_两字段都填_key只落channel的env_其他文件零泄漏(
        client, sandbox, bun, telegram, launchctl, heartbeat, as_darwin):
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port(), fish_api_key=KEY_LEAK)
    assert r.status_code == 201, r.get_json()
    hits = []
    for p in sandbox.tmp.rglob("*"):
        if p.is_file():
            try:
                if KEY_LEAK in p.read_text(encoding="utf-8", errors="ignore"):
                    hits.append(p)
            except OSError:
                pass
    assert hits == [_channel(sandbox) / ".env"], hits


def test_新建_两字段都填_key原文不进日志与标准输出(
        client, sandbox, bun, telegram, launchctl, heartbeat, as_darwin, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port(), fish_voice_id=VOICE, fish_api_key=KEY_LEAK)
    assert r.status_code == 201, r.get_json()
    out, err = capsys.readouterr()
    assert KEY_LEAK not in out and KEY_LEAK not in err, "key 原文进了 stdout/stderr"
    assert KEY_LEAK not in caplog.text, "key 原文进了 logging 记录"


def test_新建_register失败207_key原文不进响应与manual_hint(
        client, sandbox, bun, telegram, launchctl, heartbeat, as_darwin):
    telegram.mode = "ok"
    bun.expect(exit=3, err="install blew up")
    launchctl.set_default(installed=True, running=True, pid=4242)
    heartbeat.tick("xiaoyu")
    r = _post(client, u.free_port(), fish_voice_id=VOICE, fish_api_key=KEY_LEAK)
    assert r.status_code == 207, r.get_json()
    body = r.get_json()
    assert KEY_LEAK not in r.get_data(as_text=True), "207 响应里搜到了 key 原文"
    assert KEY_LEAK not in (body.get("manual_hint") or "")
    assert KEY_LEAK in _env_text(sandbox), "C 段失败时 B 段产物应保留（含 key）"


def test_新建_key非法400_响应不回显key原文(client, sandbox, telegram):
    leak = "sk-UNIQUEMARK918273645"
    body = _bad(client, sandbox, fish_api_key=leak)
    assert body["detail"] == "这看起来是模型供应商的 key（sk- 开头），不是 Fish Audio 的 key"
    assert leak not in json.dumps(body, ensure_ascii=False)
    assert "UNIQUEMARK918273645" not in json.dumps(body, ensure_ascii=False)


def test_重启全部_带key的bot重启失败_tail不含key(client, sandbox, launchctl, as_darwin):
    """页面唯一能看到失败详情的地方是 tail：带 key 的 bot 重启失败时里面不许有 key。

    创建流程本身不产生 job/tail；tail 的唯一出口是重启 job。这里让这只 bot 的重启
    走必失败路径（kickstart 找不到、plist 也不在），tail 会带失败详情。
    """
    u.dsh_bot(sandbox.dsh_cfg, "xiaoyu")
    ch = sandbox.dsh_home / "bots" / "xiaoyu" / "channel"
    ch.mkdir(parents=True, exist_ok=True)
    (ch / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=%s\nFISH_AUDIO_API_KEY=%s\n" % (u.FAKE_TOKEN, KEY_LEAK),
        encoding="utf-8")
    launchctl.set_default(installed=False)          # kickstart 报找不到、plist 也不在 → 该只失败
    r = client.post("/hub/api/bots/restart")
    assert r.status_code == 202, (r.status_code, r.get_json())
    st = u.wait_job(client, r.get_json()["job_id"])
    assert st["state"] == "failed", st
    assert KEY_LEAK not in repr(st), st["tail"]


def test_重启全部_tail里不许出现fish_key(client, monkeypatch):
    """§6b 安全口径点名 tail：与既有 token 测试同一模式（§8 先脱敏后截尾）。

    把 key 从覆盖命令的输出通道推进 tail —— 出来时必须已被加工掉。
    """
    monkeypatch.setenv("BOTOPS_FISH_KEY", KEY_LEAK)
    monkeypatch.setenv("HUB_RESTART_CMD",
                       u.python_cmd("import os;print(os.environ['BOTOPS_FISH_KEY'])"))
    r = client.post("/hub/api/bots/restart")
    assert r.status_code == 202, (r.status_code, r.get_json())
    st = u.wait_job(client, r.get_json()["job_id"])
    assert KEY_LEAK not in repr(st), st["tail"]
    assert "***" in st["tail"], "输出根本没被采集：%r" % st["tail"]


# ------------------------------------------------------------ 反向断言

def test_新建_两字段都不填_不写任何全局位置(
        client, sandbox, bun, telegram, launchctl, heartbeat, as_darwin):
    before_home = u.snapshot_tree(sandbox.home)
    before_agents = u.snapshot_tree(sandbox.agents)
    assert before_agents == {}, "用例前提：LaunchAgents 目录本来该是空的"
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port())
    assert r.status_code == 201, r.get_json()
    assert u.snapshot_tree(sandbox.home) == before_home, "假 HOME 下有全局位置被写"
    assert u.snapshot_tree(sandbox.agents) == before_agents, "LaunchAgents 目录被写"
    assert not (sandbox.home / ".zshrc").exists()
    assert not (sandbox.home / ".dsh-bot").exists()


@pytest.mark.parametrize("over", [{}, {"fish_voice_id": VOICE, "fish_api_key": KEY}])
def test_新建_fish字段不带来额外调用_出站只有getMe与autostart(
        client, sandbox, bun, telegram, launchctl, heartbeat, as_darwin, over):
    """「不因 key 未填或填了而调任何语音服务」没有契约接缝；这里以
    「除既有 getMe 与 autostart 外没有任何出站痕迹」旁证（局限见文件头）。"""
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port(), **over)
    assert r.status_code == 201, r.get_json()
    assert telegram.hits, "getMe 没发生：桩没被用上"
    assert all(m == "GET" and p.endswith("/getMe") for m, p in telegram.hits), telegram.hits
    assert [c["argv"][1] for c in bun.calls()] == ["install", "install-jobs"], bun.calls()
