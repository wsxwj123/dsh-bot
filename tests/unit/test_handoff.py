# -*- coding: utf-8 -*-
"""切换底层时人设与技能的交接（约定 5.10）：圈内的取与换、中立目录的读与写。"""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from moments import handoff

NOW = datetime(2026, 10, 16, 14, 0, 0, tzinfo=timezone.utc)
FILE = "---\nname: skill-gif\n---\n圈外前\n<!-- portable:start -->\n旧的圈内\n第二行\n<!-- portable:end -->\n圈外后\n"


def test_取圈内_去掉紧挨标记的空行_没有一对完整标记回None():
    assert handoff.inner(FILE) == "旧的圈内\n第二行"
    assert handoff.inner("没有标记\n") is None
    assert handoff.inner("<!-- portable:start -->\n只有开头\n") is None
    assert handoff.inner("<!-- portable:end -->\n反了\n<!-- portable:start -->\n") is None
    two = FILE + "<!-- portable:start -->\n又一段\n<!-- portable:end -->\n"
    assert handoff.inner(two) is None


def test_换圈内_圈外一字不动_没有标记回None():
    assert handoff.replace(FILE, "新的圈内") == FILE.replace("旧的圈内\n第二行", "新的圈内")
    assert handoff.replace("没有标记\n", "新的") is None


def export_dir(root, person="luna"):
    return root / ("export-" + person)


def make_export(root, person="luna", frm="claude", at=NOW, persona="对方的人设圈内", skills=None):
    d = export_dir(root, person)
    (d / "skills").mkdir(parents=True)
    if persona is not None:
        (d / "persona.md").write_text(persona, encoding="utf-8")
    for name, text in (skills if skills is not None else {"gif": "对方的表情包圈内"}).items():
        (d / "skills" / name).write_text(text, encoding="utf-8")
    meta = {"from": frm, "bot_id": "x", "person": person, "exported_at": at.strftime("%Y-%m-%dT%H:%M:%SZ")}
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


def test_读导出_刚导出的对方那份_回人设与技能圈内(tmp_path):
    make_export(tmp_path, skills={"gif.md": "表情包\n圈内\n", "voice.md": "语音圈内"})
    got = handoff.read_export(str(tmp_path), "luna", "claude", now=NOW + timedelta(minutes=3))
    assert got == {"persona": "对方的人设圈内", "skills": {"gif": "表情包\n圈内", "voice": "语音圈内"}}


@pytest.mark.parametrize("person,frm,at", [
    ("someone", "claude", NOW),                       # person 对不上
    ("luna", "dsh", NOW),                         # 自己导出的那份
    ("luna", "claude", NOW - timedelta(hours=2)),  # 不是刚停下那边导出的
    ("luna", "claude", NOW + timedelta(hours=1)),  # 时间在将来
], ids=["person不对", "from是自己", "太旧", "在将来"])
def test_读导出_不是刚停下的对方导出的_回None(tmp_path, person, frm, at):
    make_export(tmp_path, person=person, frm=frm, at=at)
    if person != "luna":
        (export_dir(tmp_path, person)).rename(export_dir(tmp_path))
    assert handoff.read_export(str(tmp_path), "luna", "claude", now=NOW) is None


def test_读导出_目录不在_是软链_meta坏了_回None(tmp_path):
    assert handoff.read_export(str(tmp_path), "luna", "claude", now=NOW) is None
    real = make_export(tmp_path / "elsewhere")
    os.symlink(real, export_dir(tmp_path))
    assert handoff.read_export(str(tmp_path), "luna", "claude", now=NOW) is None
    os.unlink(export_dir(tmp_path))
    make_export(tmp_path)
    (export_dir(tmp_path) / "meta.json").write_text("{坏的", encoding="utf-8")
    assert handoff.read_export(str(tmp_path), "luna", "claude", now=NOW) is None


def test_读导出_不认识的技能名_软链_带标记_空的_过大_不是UTF8的条目都不要(tmp_path):
    d = make_export(tmp_path, persona="人设里夹了 <!-- portable:end --> 标记", skills={
        "gif.md": "好的", "teleport.md": "不认识", "voice.md": "  \n", "image.md": "<!-- portable:start -->",
        "moments.md": "长" * (handoff.MAX_BYTES // 3 + 1), "group.txt": "扩展名不对"})
    (d / "skills" / "nsfw.md").write_bytes(b"\xff\xfe")
    (tmp_path / "outside.md").write_text("外面的", encoding="utf-8")
    os.symlink(tmp_path / "outside.md", d / "skills" / "proactive.md")
    got = handoff.read_export(str(tmp_path), "luna", "claude", now=NOW)
    assert got == {"persona": None, "skills": {"gif": "好的"}}


def test_写导出_只写圈内_meta写明来自dsh_目录0700文件0600_旧的整份换掉(tmp_path):
    root = tmp_path / "handoff"
    make_export(root, skills={"voice.md": "上次留下的"})
    skills = {"gif": FILE, "voice": "没有标记的技能\n", "image": None}
    done = handoff.write_export(str(root), "luna", "bot4", "人设圈外\n<!-- portable:start -->\n人设圈内\n<!-- portable:end -->\n",
                                skills, now=NOW)
    d = export_dir(root)
    assert done == ["persona", "gif"]
    assert (d / "persona.md").read_text(encoding="utf-8") == "人设圈内"
    assert (d / "skills" / "gif.md").read_text(encoding="utf-8") == "旧的圈内\n第二行"
    assert sorted(os.listdir(d / "skills")) == ["gif.md"]
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert meta == {"from": "dsh", "bot_id": "bot4", "person": "luna", "exported_at": "2026-10-16T14:00:00Z"}
    assert oct(os.stat(d).st_mode & 0o777) == "0o700"
    for p in (d / "persona.md", d / "skills" / "gif.md", d / "meta.json"):
        assert oct(os.stat(p).st_mode & 0o777) == "0o600", p
    assert [n for n in os.listdir(root) if n != "export-luna"] == []
    assert handoff.read_export(str(root), "luna", "dsh", now=NOW) == {"persona": "人设圈内", "skills": {"gif": "旧的圈内\n第二行"}}


def test_写导出_导出目录是软链_不跟过去写(tmp_path):
    root = tmp_path / "handoff"
    root.mkdir()
    (tmp_path / "elsewhere").mkdir()
    os.symlink(tmp_path / "elsewhere", export_dir(root))
    with pytest.raises(OSError):
        handoff.write_export(str(root), "luna", "bot4", FILE, {}, now=NOW)
    assert os.listdir(tmp_path / "elsewhere") == []
