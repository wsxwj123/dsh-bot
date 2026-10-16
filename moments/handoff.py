# -*- coding: utf-8 -*-
"""切换底层时人设与技能的交接（约定 5.10）：中立目录 ``~/.config/claude-dsh-console/handoff/export-<person>/``。

目录里三样：``persona.md``（人设的圈内原文）、``skills/<通用名>.md``（每个技能的圈内原文）、``meta.json``
（``from``、``bot_id``、``person``、``exported_at``）。圈内指文件里 ``<!-- portable:start -->`` 与
``<!-- portable:end -->`` 之间那一段：导出只导圈内，导入只换自己文件的圈内，圈外原样；没有一对完整标记的
不导出也不导入。两边各自导出自己的、导入对方的，谁都不读对方目录里的文件。
"""
import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone

from moments import skill_templates

START = "<!-- portable:start -->"
END = "<!-- portable:end -->"
#: 一段圈内按 UTF-8 最多多大（同人设、技能读写的上限）
MAX_BYTES = 256 * 1024
#: 导出要是"刚停下的那边"导出的：导出时间离现在不超过这么久（也不许在将来，留一点钟差）
FRESH = timedelta(minutes=30)
CLOCK_SLACK = timedelta(minutes=5)
_MARK = "<!-- portable:"


def _span(text):
    """圈内在 ``text`` 里的起止下标；没有正好一对、或者顺序反了，回 None。"""
    if text.count(START) != 1 or text.count(END) != 1:
        return None
    a, b = text.index(START) + len(START), text.index(END)
    return (a, b) if a <= b else None


def inner(text):
    """文件里的圈内原文，去掉紧挨标记的换行；没有一对完整标记回 None。"""
    span = _span(text)
    return None if span is None else text[span[0]:span[1]].strip("\r\n")


def replace(text, segment):
    """把 ``text`` 的圈内换成 ``segment``，标记和圈外一字不动；没有一对完整标记回 None。"""
    span = _span(text)
    return None if span is None else text[:span[0]] + "\n" + segment + "\n" + text[span[1]:]


def _piece(path):
    """读导出目录里的一段：不是普通文件（软链也不算）、过大、不是 UTF-8、去掉换行后是空的、自己带着标记的，回 None。"""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as f:
            raw = f.read(MAX_BYTES + 1)
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    text = text.strip("\r\n")
    if len(raw) > MAX_BYTES or not text.strip() or _MARK in text:
        return None
    return text


def _when(value):
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return at if at.tzinfo else None


def read_export(root, person, expect_from, now=None):
    """读 ``<root>/export-<person>/`` → ``{"persona": 圈内或 None, "skills": {通用名: 圈内}}``。

    只认刚停下的对方导出的那份：``meta.json`` 的 ``from`` 是 ``expect_from``、``person`` 对得上、``exported_at``
    在最近 30 分钟内。目录不在、是软链、meta 不对，回 None。单个条目不合规（见 ``_piece``）、技能名不认识的，跳过。
    """
    now = now or datetime.now(timezone.utc)
    folder = os.path.join(root, "export-" + person)
    if os.path.islink(folder) or not os.path.isdir(folder):
        return None
    meta = _piece(os.path.join(folder, "meta.json"))
    try:
        meta = json.loads(meta) if meta else None
    except ValueError:
        meta = None
    at = _when(meta.get("exported_at")) if isinstance(meta, dict) else None
    if at is None or meta.get("from") != expect_from or meta.get("person") != person \
            or not now - FRESH <= at <= now + CLOCK_SLACK:
        return None
    skills = {}
    skill_dir = os.path.join(folder, "skills")
    if not os.path.islink(skill_dir) and os.path.isdir(skill_dir):
        for generic in skill_templates.GENERICS:
            text = _piece(os.path.join(skill_dir, generic + ".md"))
            if text is not None:
                skills[generic] = text
    return {"persona": _piece(os.path.join(folder, "persona.md")), "skills": skills}


def _write(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def write_export(root, person, bot_id, persona_text, skill_texts, now=None):
    """把这只 bot 的圈内导出到 ``<root>/export-<person>/``，``meta.json`` 写明来自 dsh。返回导出了的（"persona"、通用名）。

    ``persona_text`` 与 ``skill_texts`` 的值是文件全文（没有就是 None），没有一对完整标记的不导出。先在旁边的
    临时目录写好，再把整个导出目录换上去（上一份不论谁导出的都换掉：以刚停下的这边为准）。导出目录是软链抛 OSError。
    """
    now = now or datetime.now(timezone.utc)
    pieces = []
    if persona_text and inner(persona_text):
        pieces.append(("persona", "persona.md", inner(persona_text)))
    for generic in skill_templates.GENERICS:
        text = skill_texts.get(generic)
        if text and inner(text):
            pieces.append((generic, os.path.join("skills", generic + ".md"), inner(text)))
    meta = {"from": "dsh", "bot_id": bot_id, "person": person, "exported_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    os.makedirs(root, mode=0o700, exist_ok=True)
    final = os.path.join(root, "export-" + person)
    if os.path.islink(final):
        raise OSError("export folder is a symlink")
    tmp = tempfile.mkdtemp(prefix=".export-%s-" % person, dir=root)       # mkdtemp 建的就是 0700
    old = None
    try:
        os.mkdir(os.path.join(tmp, "skills"), 0o700)
        for _name, rel, text in pieces:
            _write(os.path.join(tmp, rel), text)
        _write(os.path.join(tmp, "meta.json"), json.dumps(meta, ensure_ascii=False) + "\n")
        if os.path.lexists(final):
            old = tmp + ".old"
            os.rename(final, old)
        os.rename(tmp, final)
    except BaseException:
        if old and not os.path.lexists(final):
            os.rename(old, final)                   # 新的没换上：上一份放回原处
            old = None
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if old:
        shutil.rmtree(old, ignore_errors=True)
    return [name for name, _rel, _text in pieces]
