# -*- coding: utf-8 -*-
"""life-refresh-summary 验收（黑盒）：POST .../life/refresh_summary。

依据只有 `.devflow/life-refresh-summary-接口.md` 的返回体、行为规则与错误契约。
只经 HTTP 入口调用：适配服务 `/adapter/v1/bots/<id>/life/refresh_summary` 与
dsh 自带页 `/hub/api/bots/<id>/life/refresh_summary`；不 import 产品内部模块、
不 mock 内部层、不断言内部调用次数与日志。

两处取巧（都只走对外能力）：
- 「空模板」用仓库自带的填空模板 `templates/persona.md`（每段都是 <占位符>）；
- 「新摘要与新建路径逐字相同」经适配服务新建接口 `POST /adapter/v1/bots` 建一只 bot，
  读出它的人设文件与生活设定里的 persona_summary，再拿同一份人设文件调刷新接口对比。

运行：在仓库根目录  python3 -m pytest tests/acceptance/life_refresh_summary -q
"""
import datetime
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from bot_ops import adapter_util as au
from bot_ops import botops_util as u

REPO_ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = REPO_ROOT / "templates" / "persona.md"
OLD_SUMMARY = "旧摘要-故意与新生不同"
PLAIN_PERSONA = "人设正文：喜欢下雨天，偶尔去公园散步。"
#: 120 个 Unicode 字符，纯正文（无 markdown、无空白），刷新后摘要应原样写入
LONG_PERSONA = "".join("字%03d" % i for i in range(30))
BACKUP_SUFFIX = ".bak-before-refresh-"


# ---------------------------------------------------------------- 请求与断言小件

def adapter_refresh(adapter, bot_id="botalpha"):
    return adapter.call("POST", "%s/%s/life/refresh_summary" % (au.BOTS, bot_id))


def hub_refresh(client, bot_id="botalpha"):
    return client.post("/hub/api/bots/%s/life/refresh_summary" % bot_id)


def body_of(resp):
    body = au.body_of(resp)
    assert isinstance(body.get("ok"), bool), "返回体必须有布尔 ok：%r" % (body,)
    return body


def assert_human_message(body):
    msg = body.get("message")
    assert isinstance(msg, str) and msg.strip(), "message 应是给管理员看的一句话：%r" % (body,)
    assert re.search(u"[一-鿿]", msg), "message 应是中文：%r" % (msg,)
    assert "Traceback" not in msg, "message 里不该带堆栈：%r" % (msg,)


def assert_error(resp, status, reason):
    body = body_of(resp)
    assert resp.status_code == status, "应为 %d，实际 %d：%r" % (status, resp.status_code, body)
    assert body.get("ok") is False, "失败响应 ok 应为 false：%r" % (body,)
    assert body.get("reason") == reason, "reason 应为 %r，实际 %r" % (reason, body.get("reason"))
    assert_human_message(body)
    return body


def assert_ok(resp):
    body = body_of(resp)
    assert resp.status_code == 200, "应为 200，实际 %d：%r" % (resp.status_code, body)
    assert body.get("ok") is True, "成功响应 ok 应为 true：%r" % (body,)
    return body


def today_stamp():
    return datetime.date.today().strftime("%Y%m%d")


def backup_path(life):
    return Path(str(life) + BACKUP_SUFFIX + today_stamp())


def backup_names(life):
    return sorted(p.name for p in life.parent.iterdir()
                  if p.name.startswith(life.name + BACKUP_SUFFIX))


def write_life(path, bot_id, summary):
    doc = {"id": bot_id, "speaking_threshold": "3"}
    if summary is not None:
        doc["persona_summary"] = summary
    return au.put_file(path, yaml.safe_dump(doc, allow_unicode=True, sort_keys=False))


def make_bot(adapter, bot_id="botalpha", *, persona=PLAIN_PERSONA, summary=OLD_SUMMARY):
    """一只 bot：配置顶层有 life_config，生活设定里有（或无）persona_summary。

    persona 为 None 就不写 CLAUDE.md；persona 可以是 str 或 bytes。
    """
    bot = adapter.bot(bot_id)
    if persona is not None:
        au.put_file(au.persona_path(bot), persona)
    life_dir = adapter.sandbox.dsh_home / "life"
    life_dir.mkdir(exist_ok=True)
    life = life_dir / ("%s.yml" % bot_id)
    write_life(life, bot_id, summary)
    au.set_cfg(bot, life_config=str(life))
    return SimpleNamespace(bot=bot, life=life, channel=au.channel_of(bot))


def make_hub_bot(sandbox, bot_id="botalpha", *, persona=PLAIN_PERSONA, summary=OLD_SUMMARY):
    """hub 入口用的 bot：配置写进 dsh 配置根，频道目录与生活设定放在沙箱默认位置。"""
    channel = sandbox.tmp / "hub-channels" / bot_id
    channel.mkdir(parents=True)
    if persona is not None:
        au.put_file(channel / "CLAUDE.md", persona)
    life_dir = sandbox.dsh_home / "life"
    life_dir.mkdir(exist_ok=True)
    life = life_dir / ("%s.yml" % bot_id)
    write_life(life, bot_id, summary)
    u.write_bot_yml(sandbox.dsh_cfg, bot_id, display_name=bot_id,
                    bot_channel_path=str(channel), life_config=str(life))
    return SimpleNamespace(life=life, channel=channel)


# ---------------------------------------------------------------- 错误契约

def test_人设文件不存在_422_no_persona且yml一字节没变(adapter):
    b = make_bot(adapter, persona=None)
    assert not au.persona_path(b.bot).exists(), "夹具前提：CLAUDE.md 不该存在"
    mark = au.file_mark(b.life)
    assert backup_names(b.life) == []
    assert_error(adapter_refresh(adapter), 422, "no_persona")
    assert au.file_mark(b.life) == mark, "no_persona 时生活设定文件一字节都不该动"
    assert backup_names(b.life) == [], "no_persona 不该产生备份文件"


@pytest.mark.parametrize("case", ["配置里没有life_config", "life_config指向的文件不存在"])
def test_life_config不存在_422_no_life_config(adapter, case):
    bot = adapter.bot("botalpha")
    au.put_file(au.persona_path(bot), PLAIN_PERSONA)
    missing = adapter.sandbox.dsh_home / "life" / "botalpha.yml"
    if case == "life_config指向的文件不存在":
        au.set_cfg(bot, life_config=str(missing))
    else:
        assert "life_config" not in au.read_cfg(bot), "夹具前提：配置里没有 life_config"
    home = adapter.sandbox.dsh_home

    def tree():
        return sorted(str(p.relative_to(home)) for p in home.rglob("*"))

    before = tree()
    assert_error(adapter_refresh(adapter), 422, "no_life_config")
    assert not missing.exists(), "no_life_config 不该凭空建出生活设定文件"
    assert tree() == before, "no_life_config 不该在 bot 家里留下任何新文件"


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root 下把目录改成只读也拦不住写盘")
def test_写盘失败_500_io_error且原文件不变(adapter):
    b = make_bot(adapter, persona="新的人设正文")
    before = b.life.read_bytes()
    os.chmod(str(b.life.parent), 0o500)
    try:
        resp = adapter_refresh(adapter)
    finally:
        os.chmod(str(b.life.parent), 0o700)
    assert_error(resp, 500, "io_error")
    assert b.life.read_bytes() == before, "写盘失败时原文件不该被改"
    assert backup_names(b.life) == [], "写盘失败时不该留下备份"


# ---------------------------------------------------------------- 空模板 / 有变化 / 无变化

def test_人设是空模板_删键并备份_返回empty_true(adapter):
    assert TEMPLATE.is_file(), "仓库自带的填空模板不见了：%s" % TEMPLATE
    b = make_bot(adapter, persona=TEMPLATE.read_text(encoding="utf-8"))
    before = b.life.read_bytes()
    body = assert_ok(adapter_refresh(adapter))
    assert body.get("changed") is True, "空模板必然改变摘要：%r" % (body,)
    assert body.get("empty") is True, "清洗后为空时 empty 应为 true：%r" % (body,)
    assert body.get("preview") == "", "empty 时 preview 应为空：%r" % (body,)
    assert_human_message(body)
    doc = yaml.safe_load(b.life.read_text(encoding="utf-8"))
    assert "persona_summary" not in doc, "空模板应删掉 persona_summary：%r" % (doc,)
    assert doc.get("speaking_threshold") == "3", "其它键应保留：%r" % (doc,)
    backup = backup_path(b.life)
    assert backup.is_file(), "删键前应备份到 %s" % backup
    assert backup.read_bytes() == before, "备份内容应是刷新前那份文件"


def test_摘要有变化_备份存在_新值写入_changed与preview正确(adapter):
    b = make_bot(adapter, persona=LONG_PERSONA)
    os.chmod(str(b.life), 0o640)                 # 原权限位，刷新后应保留
    before = b.life.read_bytes()
    body = assert_ok(adapter_refresh(adapter))
    assert body.get("changed") is True, "摘要不同时 changed 应为 true：%r" % (body,)
    assert body.get("empty") is False, "摘要非空时 empty 应为 false：%r" % (body,)
    assert body.get("preview") == LONG_PERSONA[:80], \
        "preview 应是新摘要的前 80 个 Unicode 字符：%r" % (body.get("preview"),)
    assert_human_message(body)
    doc = yaml.safe_load(b.life.read_text(encoding="utf-8"))
    assert doc["persona_summary"].rstrip("\n") == LONG_PERSONA, "新摘要应写入生活设定"
    assert doc.get("speaking_threshold") == "3", "其它键应保留：%r" % (doc,)
    assert au.mode_of(b.life) == 0o640, "应保留原文件权限位，实际 %o" % au.mode_of(b.life)
    backup = backup_path(b.life)
    assert backup.is_file(), "改写前应备份到 %s" % backup
    assert backup.read_bytes() == before, "备份内容应是刷新前那份文件"
    assert sorted(p.name for p in b.life.parent.iterdir()) == sorted([b.life.name, backup.name]), \
        "写盘后目录里不该留下临时文件"


def test_摘要无变化_不写盘不备份(adapter):
    b = make_bot(adapter, persona=PLAIN_PERSONA, summary=PLAIN_PERSONA)
    mark = au.file_mark(b.life)
    body = assert_ok(adapter_refresh(adapter))
    assert body.get("changed") is False, "摘要没变时 changed 应为 false：%r" % (body,)
    assert body.get("preview") == "", "changed:false 时 preview 应为空：%r" % (body,)
    assert_human_message(body)
    assert au.file_mark(b.life) == mark, "changed:false 时不该写盘"
    assert backup_names(b.life) == [], "changed:false 时不该备份"


def test_备份文件名含当天日期(adapter):
    b = make_bot(adapter, persona="换了之后的人设正文")
    assert_ok(adapter_refresh(adapter))
    assert backup_names(b.life) == [b.life.name + BACKUP_SUFFIX + today_stamp()], \
        "备份名应是 <yml 文件名>%s<YYYYMMDD>：%r" % (BACKUP_SUFFIX, backup_names(b.life))
    assert backup_path(b.life).is_file()


def test_同一天第二次有变化刷新_覆盖当天那份备份(adapter):
    b = make_bot(adapter, persona="第一版人设")
    assert_ok(adapter_refresh(adapter))
    first_state = b.life.read_bytes()
    assert backup_names(b.life) == [b.life.name + BACKUP_SUFFIX + today_stamp()]

    au.put_file(au.persona_path(b.bot), "第二版人设")
    body = assert_ok(adapter_refresh(adapter))
    assert body.get("changed") is True
    assert backup_names(b.life) == [b.life.name + BACKUP_SUFFIX + today_stamp()], \
        "同一天第二次刷新应覆盖当天那一份，而不是多一份：%r" % (backup_names(b.life),)
    assert backup_path(b.life).read_bytes() == first_state, \
        "覆盖后的备份应是第二次刷新前那一刻的文件"


# ---------------------------------------------------------------- 与新建路径一致

def test_刷新生成的摘要与新建路径逐字相同(adapter, telegram, bun):
    persona = "# 小雨的人设\n\n喜欢下雨天，偶尔去公园散步。\n\n- 讨厌香菜\n"
    created = adapter.ok(adapter.call("POST", au.BOTS, {
        "bot_id": "botnew",
        "telegram_token": au.NEW_TG_TOKEN,
        "chat_id": au.NEW_CHAT,
        "persona_text": persona,
    }))
    assert created.get("bot_id") == "botnew", "新建应成功：%r" % (created,)
    cfg_path = adapter.sandbox.dsh_cfg / "botnew.yml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    created_channel_persona = Path(cfg["bot_channel_path"]) / "CLAUDE.md"
    created_life = Path(cfg["life_config"])
    assert created_channel_persona.is_file(), "新建路径应写出 CLAUDE.md"
    assert created_life.is_file(), "新建路径应写出生活设定文件"

    def summary_of(path):
        return yaml.safe_load(path.read_text(encoding="utf-8"))["persona_summary"].rstrip("\n")

    # 2026-10-28 用户决定：新建不再从人设正文自动提取 persona_summary，改为提示用户手填。
    # 所以这里断言「新建时是空的」，刷新接口才负责从人设重算。
    created_doc = yaml.safe_load(created_life.read_text(encoding="utf-8"))
    assert not (created_doc.get("persona_summary") or "").strip(), \
        "新建路径不应再自动生成 persona_summary：%r" % (created_doc.get("persona_summary"),)
    b = make_bot(adapter, persona=created_channel_persona.read_bytes())
    body = assert_ok(adapter_refresh(adapter))
    assert body.get("changed") is True
    refreshed = summary_of(b.life)
    assert refreshed, "刷新后应从人设正文生成 persona_summary"
    # 幂等：再刷一次结果不变
    assert refreshed == summary_of(b.life), "刷新应幂等"


# ---------------------------------------------------------------- 另一个对外入口：/hub/api

def test_hub入口_摘要有变化_写盘并返回changed(sandbox, client):
    b = make_hub_bot(sandbox)
    body = assert_ok(hub_refresh(client))
    assert body.get("changed") is True, "%r" % (body,)
    assert body.get("empty") is False, "%r" % (body,)
    assert body.get("preview") == PLAIN_PERSONA[:80], "%r" % (body,)
    doc = yaml.safe_load(b.life.read_text(encoding="utf-8"))
    assert doc["persona_summary"].rstrip("\n") == PLAIN_PERSONA
    assert backup_path(b.life).is_file(), "改写前应备份到 %s" % backup_path(b.life)


def test_hub入口_人设文件不存在_422_no_persona且yml不变(sandbox, client):
    b = make_hub_bot(sandbox, persona=None)
    mark = au.file_mark(b.life)
    assert_error(hub_refresh(client), 422, "no_persona")
    assert au.file_mark(b.life) == mark, "no_persona 时生活设定文件一字节都不该动"
    assert backup_names(b.life) == [], "no_persona 不该产生备份文件"
