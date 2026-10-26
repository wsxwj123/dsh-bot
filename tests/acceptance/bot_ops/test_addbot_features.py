# -*- coding: utf-8 -*-
"""新建 bot 的 features 勾选（接口：.devflow/addbot-features-接口.md §3，共 12 条）。

只经对外接口 POST /hub/api/bots 验证，不 import 实现、不 mock 内部层。
每条用例对应行为清单里的一条，断言具体值/状态码/文件落点。
"""
import os
import shutil
from pathlib import Path

import pytest

from . import botops_util as u

NINE = ("voice", "image", "stickers", "moments", "proactive",
        "life", "relationship", "groups", "nsfw")


# ------------------------------------------------------------ 小工具

def _payload(port, **over):
    body = {"bot_id": "xiaoyu", "telegram_token": u.FAKE_TOKEN, "dispatcher_port": port,
            "chat_id": 123456789, "display_name": "小雨",
            "persona_text": "我是小雨的人设正文。"}
    body.update(over)
    return body


def _post(client, port, **over):
    return client.post("/hub/api/bots", json=_payload(port, **over))


def _arm_ok(bun, telegram, launchctl, heartbeat, bot="xiaoyu", pid=4242):
    """把新建全流程成功要的外部条件摆好（同 test_create_bot._arm_ok）。"""
    telegram.mode = "ok"
    bun.expect(exit=0).expect(exit=0)
    launchctl.set_default(installed=True, running=True, pid=pid)
    heartbeat.tick(bot, pid=pid)


def _boom_moves(monkeypatch):
    """让「把造物搬到位」这一步必失败（同 test_create_bot._boom_moves）。"""
    def boom(*a, **kw):
        raise OSError("mocked move failure")
    monkeypatch.setattr(shutil, "move", boom)
    monkeypatch.setattr(os, "replace", boom)
    monkeypatch.setattr(os, "rename", boom)


def _channel(sandbox, bot="xiaoyu"):
    return sandbox.dsh_home / "bots" / bot / "channel"


def _skill(sandbox, generic, bot="xiaoyu"):
    return _channel(sandbox, bot) / "skills" / ("skill-" + generic) / "SKILL.md"


def _features_with(*on):
    """九个键的完整布尔表，只有 `on` 里点名的为 True。"""
    return {k: (k in on) for k in NINE}


def _skipped_keys(body):
    """features_skipped 里的 key 列表（兼容对象项与裸字符串两种形状）。"""
    out = []
    for item in body.get("features_skipped") or []:
        if isinstance(item, dict):
            out.append(item.get("key"))
        else:
            out.append(item)
    return out


def _with_novelai_token(sandbox):
    """照 test_adapter_v11_create.with_novelai_token：令牌写进 credentials.yaml 的 refs。"""
    (sandbox.dsh_home / "credentials.yaml").write_text(
        "version: 1\nrefs:\n  NOVELAI_BEARER_TOKEN: pst-fake-token-0123456789\n", encoding="utf-8")


def _life_config_path(sandbox, body, cfg, bot="xiaoyu"):
    """life_config 指向的文件：值可能出现在回包或 yml，相对路径按常见落点找。"""
    val = body.get("life_config") or cfg.get("life_config")
    assert val, "features.life=true 但回包与 yml 都没有 life_config（%r / %r）" % (body, cfg)
    p = Path(str(val))
    if p.is_absolute():
        return p
    for cand in (sandbox.dsh_home / "bots" / bot / p, _channel(sandbox, bot) / p,
                 sandbox.dsh_home / p, sandbox.tmp / p):
        if cand.exists():
            return cand
    return sandbox.dsh_home / "bots" / bot / p


# ------------------------------------------------------------ 12 条行为

def test_1_不传features_九全关_不落技能(client, sandbox, bun, telegram, launchctl,
                               heartbeat, as_darwin):
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port())
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body.get("features") == _features_with(), body
    assert body.get("features_skipped") == [], body
    assert body.get("skills") == [], body
    cfg_path = sandbox.dsh_cfg / "xiaoyu.yml"
    assert cfg_path.is_file(), "配置没落盘"
    assert u.read_yaml(cfg_path).get("features") == _features_with()
    skills = _channel(sandbox) / "skills"
    assert (not skills.exists()) or (list(skills.iterdir()) == []), "没勾功能却落了技能：%r" % (
        list(skills.iterdir()) if skills.exists() else None)


@pytest.mark.skipif(os.name == "nt", reason="Windows 无 POSIX 权限位")
def test_2_勾image且有NovelAI令牌_打开且生图技能0600非空(
        client, sandbox, bun, telegram, launchctl, heartbeat, as_darwin):
    _with_novelai_token(sandbox)
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port(), features=["image"])
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert (body.get("features") or {}).get("image") is True, body
    p = _skill(sandbox, "image")
    assert p.is_file(), "skill-image/SKILL.md 不存在"
    assert u.mode_of(p) == 0o600, oct(u.mode_of(p))
    assert p.read_text(encoding="utf-8").strip(), "SKILL.md 内容为空"


def test_3_勾voice且带音色_打开且voice技能存在(client, sandbox, bun, telegram, launchctl,
                                    heartbeat, as_darwin):
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port(), features=["voice"], fish_voice_id="voice_abc-1")
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert (body.get("features") or {}).get("voice") is True, body
    assert _skill(sandbox, "voice").is_file(), "skill-voice/SKILL.md 不存在"


def test_4_勾voice不带音色_不打开_跳过且技能带停用标记(client, sandbox, bun, telegram,
                                          launchctl, heartbeat, as_darwin):
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port(), features=["voice"])
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert (body.get("features") or {}).get("voice") is False, body
    assert _skipped_keys(body) == ["voice"], body.get("features_skipped")
    p = _skill(sandbox, "voice")
    assert p.is_file(), "skill-voice/SKILL.md 仍应照放"
    assert "disable-model-invocation" in p.read_text(encoding="utf-8"), "缺少停用标记"


def test_5_勾五个功能_生成对应五个技能且功能为真(client, sandbox, bun, telegram, launchctl,
                                   heartbeat, as_darwin):
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port(),
              features=["stickers", "moments", "proactive", "groups", "nsfw"])
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    want = _features_with("stickers", "moments", "proactive", "groups", "nsfw")
    assert body.get("features") == want, body
    for generic in ("gif", "moments", "proactive", "group", "nsfw"):
        assert _skill(sandbox, generic).is_file(), "缺少 skill-%s/SKILL.md" % generic


def test_6_勾life_打开且生活设定文件已生成(client, sandbox, bun, telegram, launchctl,
                                heartbeat, as_darwin):
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port(), features=["life"])
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert (body.get("features") or {}).get("life") is True, body
    cfg_path = sandbox.dsh_cfg / "xiaoyu.yml"
    assert cfg_path.is_file(), "配置没落盘"
    cfg = u.read_yaml(cfg_path)
    life = _life_config_path(sandbox, body, cfg)
    assert life.is_file(), "life_config 指向的文件不存在：%s" % life


def test_7_勾image但无NovelAI令牌_不打开_跳过且技能带停用标记(client, sandbox, bun,
                                             telegram, launchctl, heartbeat, as_darwin):
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port(), features=["image"])
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert (body.get("features") or {}).get("image") is False, body
    assert "image" in _skipped_keys(body), body.get("features_skipped")
    p = _skill(sandbox, "image")
    assert p.is_file(), "skill-image/SKILL.md 仍应照放"
    assert "disable-model-invocation" in p.read_text(encoding="utf-8"), "缺少停用标记"


def test_8_features含未知键_400_bad_body且不落文件(client, sandbox, telegram):
    r = _post(client, u.free_port(), features=["bogus"])
    assert r.status_code == 400, (r.status_code, r.get_json())
    assert (r.get_json() or {}).get("error") == "bad_body", r.get_json()
    assert not (sandbox.dsh_cfg / "xiaoyu.yml").exists(), "被拒了还落了配置"
    assert not (sandbox.dsh_home / "bots" / "xiaoyu").exists(), "被拒了还落了 bot 家"


def test_9_features有重复项_400_bad_body(client, sandbox, telegram):
    r = _post(client, u.free_port(), features=["voice", "voice"])
    assert r.status_code == 400, (r.status_code, r.get_json())
    assert (r.get_json() or {}).get("error") == "bad_body", r.get_json()


def test_10_features不是数组_400_bad_body(client, sandbox, telegram):
    r = _post(client, u.free_port(), features="voice")
    assert r.status_code == 400, (r.status_code, r.get_json())
    assert (r.get_json() or {}).get("error") == "bad_body", r.get_json()


def test_11_同时勾image与voice且都就绪_skills两者都在(client, sandbox, bun, telegram,
                                          launchctl, heartbeat, as_darwin):
    _with_novelai_token(sandbox)
    _arm_ok(bun, telegram, launchctl, heartbeat)
    r = _post(client, u.free_port(), features=["image", "voice"], fish_voice_id="voice_abc-1")
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert {"image", "voice"} <= set(body.get("skills") or []), body.get("skills")


def test_12_搬移步骤失败_回滚到操作前_skills不留在原位(client, sandbox, telegram,
                                          monkeypatch, as_darwin):
    """照 test_create_bot 的 files 步骤失败回滚写法：产物全部撤回。"""
    _with_novelai_token(sandbox)
    telegram.mode = "ok"
    _boom_moves(monkeypatch)
    r = _post(client, u.free_port(), features=["image"])
    assert r.status_code == 500, (r.status_code, r.get_json())
    assert (r.get_json() or {}).get("error") == "internal", r.get_json()
    assert not (_channel(sandbox) / "skills").exists(), "回滚后 skills 还留在原位"
    assert not (sandbox.dsh_home / "bots" / "xiaoyu").exists(), "回滚没删干净 bot 家"
    assert not (sandbox.dsh_cfg / "xiaoyu.yml").exists(), "回滚没删干净配置"
