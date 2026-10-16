# -*- coding: utf-8 -*-
"""适配服务 v1.1 验收：切换时同步人设与技能（约定 5.10，5.9 第 3 步）。

两个方向都经中立目录 ``~/.config/claude-dsh-console/handoff/export-<person>/``（假家目录里）：
- 切进 dsh（带 import_from 的启用）：增量导入之后、启动之前，读对方（claude）刚导出的那份，只换自己文件的圈内；
  自己没有的技能按通用名新建（自己模板的圈外加对方的圈内）；导入前备份；synced 照实报。
- 切换任务里停用 dsh（带 moved_to）：停稳之后把自己人设与技能的圈内导出，meta 写明来自 dsh。
"""
import json
import os
from datetime import datetime, timedelta, timezone

from . import adapter_util as au
from .adapter_util import adapter, console_guard  # noqa: F401  夹具
from .test_adapter_v06_switch_in import sw, switch_in  # noqa: F401  夹具：旧频道目录、停着的 botalpha

PERSONA = "# 新系统专有的开头\n<!-- portable:start -->\n旧的人设圈内\n<!-- portable:end -->\n新系统备注留着\n"
GIF = "---\nname: skill-gif\ndescription: 表情包\n---\n怎么调工具\n<!-- portable:start -->\n旧的表情包圈内\n<!-- portable:end -->\n"


def handoff_root(adapter):
    return adapter.sandbox.home / ".config" / "claude-dsh-console" / "handoff"


def put_export(adapter, person="botalpha", frm="claude", minutes_ago=1, persona="对方的人设圈内",
               skills=(("gif", "对方的表情包圈内"), ("voice", "对方的语音圈内"))):
    d = handoff_root(adapter) / ("export-" + person)
    (d / "skills").mkdir(parents=True)
    if persona is not None:
        (d / "persona.md").write_text(persona, encoding="utf-8")
    for name, text in skills:
        (d / "skills" / (name + ".md")).write_text(text, encoding="utf-8")
    at = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (d / "meta.json").write_text(json.dumps({"from": frm, "bot_id": "bot2", "person": person, "exported_at": at}),
                                 encoding="utf-8")
    return d


def test_切进_对方刚导出的人设与技能只换圈内_没有的技能按模板新建_先备份_synced照实报(adapter, sw):
    au.put_file(au.persona_path(sw.alpha), PERSONA)
    au.put_file(au.skill_path(sw.alpha, "gif"), GIF)
    put_export(adapter)
    body = adapter.ok(switch_in(adapter))
    assert body["synced"] == ["persona", "skills"], body
    persona = au.persona_path(sw.alpha).read_text(encoding="utf-8")
    assert persona == PERSONA.replace("旧的人设圈内", "对方的人设圈内"), persona
    gif = au.skill_path(sw.alpha, "gif").read_text(encoding="utf-8")
    assert gif == GIF.replace("旧的表情包圈内", "对方的表情包圈内"), gif
    voice = au.skill_path(sw.alpha, "voice").read_text(encoding="utf-8")
    assert voice.startswith("---\nname: skill-voice\n") and "<!-- portable:start -->\n对方的语音圈内\n<!-- portable:end -->" in voice, voice
    assert "disable-model-invocation" not in voice, voice
    assert oct(au.skill_path(sw.alpha, "voice").stat().st_mode & 0o777) == "0o600"
    backups = adapter.sandbox.dsh_home / "backups"
    assert any("旧的人设圈内" in p.read_text(encoding="utf-8") for p in (backups / "persona" / "botalpha").iterdir())
    assert any("旧的表情包圈内" in p.read_text(encoding="utf-8") for p in (backups / "skills" / "botalpha").iterdir())


def test_切进_自己的人设没有标记_不动人设_synced不报persona(adapter, sw):
    au.put_file(au.persona_path(sw.alpha), "# 没有标记的人设\n")
    put_export(adapter, skills=(("gif", "对方的表情包圈内"),))
    body = adapter.ok(switch_in(adapter))
    assert body["synced"] == ["skills"], body
    assert au.persona_path(sw.alpha).read_text(encoding="utf-8") == "# 没有标记的人设\n"


def test_切进_功能关着的技能新建成停用(adapter, sw):
    au.set_cfg(sw.alpha, features={"voice": False})
    put_export(adapter, persona=None, skills=(("voice", "对方的语音圈内"),))
    adapter.ok(switch_in(adapter))
    assert "disable-model-invocation: true" in au.skill_path(sw.alpha, "voice").read_text(encoding="utf-8")


def test_切进_导出不是对方刚导出的_不导入(adapter, sw):
    au.put_file(au.persona_path(sw.alpha), PERSONA)
    put_export(adapter, minutes_ago=120)
    assert adapter.ok(switch_in(adapter))["synced"] == []
    assert au.persona_path(sw.alpha).read_text(encoding="utf-8") == PERSONA
    assert not au.skill_path(sw.alpha, "voice").exists()


def test_切换任务里停用_停稳后把圈内导出到中立目录_meta写明来自dsh(adapter):
    bot = adapter.bot("botalpha")
    au.put_file(au.persona_path(bot), PERSONA)
    au.put_file(au.skill_path(bot, "gif"), GIF)
    au.put_file(au.skill_path(bot, "voice"), "---\nname: skill-voice\n---\n没有标记\n")
    adapter.ok(adapter.call("POST", au.enabled_url("botalpha"),
                            {"enabled": False, "moved_to": "pending", "op_id": au.op_id("switch")}))
    d = handoff_root(adapter) / "export-botalpha"
    assert (d / "persona.md").read_text(encoding="utf-8") == "旧的人设圈内"
    assert sorted(os.listdir(d / "skills")) == ["gif.md"]
    assert (d / "skills" / "gif.md").read_text(encoding="utf-8") == "旧的表情包圈内"
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert (meta["from"], meta["bot_id"], meta["person"]) == ("dsh", "botalpha", "botalpha"), meta
    assert oct((d / "persona.md").stat().st_mode & 0o777) == "0o600"


def test_平时停用_不导出(adapter):
    bot = adapter.bot("botalpha")
    au.put_file(au.persona_path(bot), PERSONA)
    adapter.ok(adapter.call("POST", au.enabled_url("botalpha"), {"enabled": False}))
    adapter.ok(adapter.call("POST", au.enabled_url("botalpha"), {"enabled": False, "moved_to": None}))
    assert not (handoff_root(adapter) / "export-botalpha").exists()
