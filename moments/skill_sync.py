# -*- coding: utf-8 -*-
"""技能文件漂移的检查与刷新（skill-drift-sync）。

每只 bot 的技能文件 ``<bot>/channel/skills/skill-<通用名>/SKILL.md`` 与仓库模板
``templates/skills/skill-<通用名>/SKILL.md`` 各存一份通用内容：新建 bot 时从模板渲染，
运行时读副本。本模块发现两者的圈内差异（``check``），并按模板刷新副本的圈内
（``sync_bot`` / ``sync_all``）：圈外逐字节不动，bot 专属变量（image 的外貌、
nsfw 的成人例句段）保留，写盘前备份原文件。

命令行::

    python3 -m moments.skill_sync check [--bots-root DIR] [--verbose]
    python3 -m moments.skill_sync sync --bot <bot_id> [--skill <通用名>] [--bots-root DIR]
    python3 -m moments.skill_sync sync --all [--bots-root DIR]

默认只输出摘要数字与状态，不打印技能正文（技能文件可能含成人内容）。
"""
import argparse
import datetime
import difflib
import os
import shutil
import stat
import sys
import tempfile

from moments import handoff, skill_templates

#: image 技能里外貌块的位置标记（模板圈内结尾："照片里的你：" 后面是外貌）
APPEARANCE_MARK = "照片里的你："
#: 备份文件后缀（与接口约定一致）
BACKUP_SUFFIX = ".bak-before-skill-sync"
DEFAULT_BOTS_ROOT = os.path.join(os.path.expanduser("~"), ".dsh-bot", "bots")


def _skill_path(bot_dir, generic):
    """这只 bot 的技能文件路径。"""
    return os.path.join(bot_dir, "channel", "skills", "skill-" + generic, "SKILL.md")


def _template_path(generic):
    """仓库里这个通用名的模板路径。"""
    return os.path.join(skill_templates.TEMPLATE_DIR, "skill-" + generic, "SKILL.md")


def _template_generics():
    """模板齐全（``skill-<名>/SKILL.md`` 存在）的通用名，排序后返回。

    按模板目录实际内容枚举，而不是写死 ``skill_templates.GENERICS``：新加一个通用名时
    检查与刷新自动覆盖到它。
    """
    names = []
    try:
        with os.scandir(skill_templates.TEMPLATE_DIR) as entries:
            for entry in entries:
                if entry.name.startswith("skill-") and entry.is_dir() \
                        and os.path.isfile(os.path.join(entry.path, "SKILL.md")):
                    names.append(entry.name[len("skill-"):])
    except OSError:
        return []
    return sorted(names)


def _bot_dirs(bots_root):
    """``bots_root`` 下每个 bot 目录的 ``(bot_id, 目录路径)``，按名字排序；根目录不在就返回空。"""
    try:
        with os.scandir(bots_root) as entries:
            found = [(entry.name, entry.path) for entry in entries if entry.is_dir()]
    except OSError:
        return []
    return sorted(found, key=lambda item: item[0])


def _existing_generics(bot_dir):
    """这只 bot 目录里已有 ``SKILL.md`` 的技能通用名，排序后返回。"""
    skills_dir = os.path.join(bot_dir, "channel", "skills")
    names = []
    try:
        with os.scandir(skills_dir) as entries:
            for entry in entries:
                if not entry.name.startswith("skill-"):
                    continue
                if entry.is_dir() and os.path.isfile(os.path.join(entry.path, "SKILL.md")):
                    names.append(entry.name[len("skill-"):])
    except OSError:
        return []
    return sorted(names)


def _read_skill(path):
    """读技能文件 → ``(text, None)``；超过 256 KiB 或不是 UTF-8 → ``(None, "no_marker")``。

    这两种文件按接口约定当作没有成对标记跳过：不猜内容、不写盘。
    """
    with open(path, "rb") as f:
        raw = f.read(handoff.MAX_BYTES + 1)
    if len(raw) > handoff.MAX_BYTES:
        return None, "no_marker"
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "no_marker"


def _has_crlf(text):
    """原文的换行风格：``\\r\\n`` 出现次数超过 ``\\n`` 总数的一半（接口约定的判断依据）。"""
    return text.count("\r\n") * 2 > text.count("\n")


def _to_lf(text):
    """统一成 LF 再处理；写回时按 ``_has_crlf`` 还原，圈外字节因此保持原样。"""
    return text.replace("\r\n", "\n") if _has_crlf(text) else text


def _nsfw_block(inner_text):
    """旧圈内里现成的例句段（含 ``NSFW_HEAD``/``NSFW_TAIL``）；没有或中间是空的回 None。"""
    head, tail = skill_templates.NSFW_HEAD, skill_templates.NSFW_TAIL
    at = inner_text.find(head)
    if at < 0:
        return None
    end = inner_text.find(tail, at + len(head))
    if end < 0:
        return None
    block = inner_text[at:end + len(tail)]
    return block if block[len(head):-len(tail)].strip() else None


def _render_inner(generic, old_inner, persona=None):
    """模板渲染结果里的圈内；模板没有成对标记回 None。

    ``image`` 从旧圈内末尾取"照片里的你："之后的内容当外貌，``nsfw`` 优先用调用方给的
    persona 重新提取、没有 persona 就用旧圈内现成的例句段——这两块是 bot 专属的，
    刷新时不能换成别的 bot 的缺省值。
    """
    if generic == "image":
        at = old_inner.find(APPEARANCE_MARK)
        appearance = old_inner[at + len(APPEARANCE_MARK):] if at >= 0 else ""
        rendered = skill_templates.render(generic, appearance=appearance)
    elif generic == "nsfw" and persona is None:
        block = _nsfw_block(old_inner)
        rendered = skill_templates.render(generic)
        if block:
            # render 不传 persona 时填的是缺省句，把它换回这只 bot 原来的例句段
            rendered = rendered.replace(skill_templates.NSFW_FALLBACK, block, 1)
    elif generic == "nsfw":
        rendered = skill_templates.render(generic, persona=persona)
    else:
        rendered = skill_templates.render(generic)
    inner = handoff.inner(rendered)
    # 模板文件可能是 CRLF；按原文风格写回时假定圈内是 LF，不然会写出 \r\r\n
    return None if inner is None else inner.replace("\r\n", "\n")


def _diff_lines(old_inner, new_inner):
    """行级差异条数：数 unified diff 里被删/被加的行，只计数不回传内容。"""
    lines = list(difflib.unified_diff(
        old_inner.splitlines(), new_inner.splitlines(), lineterm="", n=0))
    return sum(1 for line in lines[2:] if line[:1] in "+-")


def _bot_persona(bot_dir):
    """这只 bot 的人设文本（``channel/CLAUDE.md``）；读不到、不是 UTF-8、过大或空白回 None。"""
    path = os.path.join(bot_dir, "channel", "CLAUDE.md")
    try:
        with open(path, "rb") as f:
            raw = f.read(handoff.MAX_BYTES + 1)
    except OSError:
        return None
    if len(raw) > handoff.MAX_BYTES:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text if text.strip() else None


def check(bots_root):
    """扫描每只 bot 的技能圈内，与模板渲染结果比对（不写盘）。

    返回 ``{"bots": {bot_id: {"skills": {通用名: {"state", "diff_lines"}},
    "same", "diff", "skipped"}}, "totals": {"same", "diff", "skipped"}}``。
    ``state`` 为 same/diff/no_marker/missing；比之前两边各自去掉首尾换行，CRLF 统一成
    LF（刷新会按原文风格写回 CRLF，不统一的话刷新完会永远报差异）。
    """
    bots_root = os.fspath(bots_root)
    templates = set(_template_generics())
    bots = {}
    totals = {"same": 0, "diff": 0, "skipped": 0}
    for bot_id, bot_dir in _bot_dirs(bots_root):
        skills = {}
        if os.path.isdir(os.path.join(bot_dir, "channel", "skills")):
            # bot 里可能有模板已经删掉的技能：也要列出来计入 skipped，不能静默丢掉
            for generic in sorted(templates.union(_existing_generics(bot_dir))):
                if generic not in templates:
                    skills[generic] = {"state": "missing", "diff_lines": 0,
                                       "reason": "template_missing"}
                else:
                    skills[generic] = _check_one(_skill_path(bot_dir, generic), generic)
        same = sum(1 for item in skills.values() if item["state"] == "same")
        diff = sum(1 for item in skills.values() if item["state"] == "diff")
        skipped = sum(1 for item in skills.values() if item["state"] in ("no_marker", "missing"))
        bots[bot_id] = {"skills": skills, "same": same, "diff": diff, "skipped": skipped}
        totals["same"] += same
        totals["diff"] += diff
        totals["skipped"] += skipped
    return {"bots": bots, "totals": totals}


def _check_one(path, generic):
    """一个 bot 一个技能的比对结果；跳过的情况带 ``reason`` 供 --verbose 说明。"""
    if not os.path.isfile(path):
        return {"state": "missing", "diff_lines": 0}
    text, problem = _read_skill(path)
    if problem:
        return {"state": "no_marker", "diff_lines": 0}
    old_inner = handoff.inner(_to_lf(text))
    if old_inner is None:
        return {"state": "no_marker", "diff_lines": 0}
    new_inner = _render_inner(generic, old_inner)
    if new_inner is None:
        return {"state": "no_marker", "diff_lines": 0, "reason": "template_no_marker"}
    if old_inner == new_inner:
        return {"state": "same", "diff_lines": 0}
    return {"state": "diff", "diff_lines": _diff_lines(old_inner, new_inner)}


def _backup_path(path, suffix):
    """``<原路径><suffix>-<YYYYMMDD>``：同一天重复刷新覆盖当天那一份。"""
    return path + suffix + "-" + datetime.date.today().strftime("%Y%m%d")


def _write_with_backup(path, text, backup):
    """备份原文件（同目录、同权限）后，用临时文件 + 原子替换写新内容。

    先备份再写；写临时文件失败时原文件还没动过，删掉临时文件即可。
    """
    st = os.stat(path)
    mode = stat.S_IMODE(st.st_mode)
    shutil.copyfile(path, backup)
    os.chmod(backup, mode)
    data = text.encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=".skill-sync-", suffix=".tmp", dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def sync_bot(bot_dir, generic, *, persona=None, backup_suffix=BACKUP_SUFFIX):
    """把这只 bot 这个技能的圈内换成模板渲染结果，圈外逐字节不动。

    ``persona`` 是这只 bot 的人设文本（有就传给 nsfw 重新提取例句段；没有就从旧圈内取）。
    返回 ``{"ok": True, "changed": bool, "backup": str | None}`` 或
    ``{"ok": False, "reason": "no_marker" | "missing" | "template_missing"}``。
    """
    bot_dir = os.fspath(bot_dir)
    if generic not in _template_generics():
        return {"ok": False, "reason": "template_missing"}
    path = _skill_path(bot_dir, generic)
    if not os.path.isfile(path):
        return {"ok": False, "reason": "missing"}
    text, problem = _read_skill(path)
    if problem:
        return {"ok": False, "reason": problem}
    crlf = _has_crlf(text)
    text = _to_lf(text)
    old_inner = handoff.inner(text)
    if old_inner is None:
        return {"ok": False, "reason": "no_marker"}
    new_inner = _render_inner(generic, old_inner, persona)
    if new_inner is None:
        return {"ok": False, "reason": "template_missing"}
    if new_inner == old_inner:
        return {"ok": True, "changed": False, "backup": None}
    new_text = handoff.replace(text, new_inner)
    if new_text is None:
        return {"ok": False, "reason": "no_marker"}
    if crlf:
        new_text = new_text.replace("\n", "\r\n")
    backup = _backup_path(path, backup_suffix)
    _write_with_backup(path, new_text, backup)
    return {"ok": True, "changed": True, "backup": backup}


def _sync_one(bot_dir, generic, options):
    """sync_all 用：单个技能的磁盘错误只记这一项失败，不打断整批。"""
    try:
        return sync_bot(bot_dir, generic, **options)
    except OSError:
        return {"ok": False, "reason": "io_error"}


def sync_all(bots_root, **kw):
    """刷新 ``bots_root`` 下每只 bot 的每个已有技能，返回 ``{bot_id: {通用名: sync_bot 的结果}}``。

    ``kw`` 原样传给 ``sync_bot``（``backup_suffix`` 等）；没给 ``persona`` 时按每只 bot 的
    ``channel/CLAUDE.md`` 现读，供 nsfw 重新提取例句段。单个技能失败（含写盘 ``OSError``）
    不影响其余，``OSError`` 记作 ``{"ok": False, "reason": "io_error"}``。
    """
    bots_root = os.fspath(bots_root)
    result = {}
    for bot_id, bot_dir in _bot_dirs(bots_root):
        options = dict(kw)
        if "persona" not in options:
            persona = _bot_persona(bot_dir)
            if persona is not None:
                options["persona"] = persona
        result[bot_id] = {generic: _sync_one(bot_dir, generic, options)
                          for generic in _existing_generics(bot_dir)}
    return result


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="python3 -m moments.skill_sync", description="检查并刷新各 bot 技能文件的圈内")
    parser.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS,
                        help="列出每只 bot 每项的状态与差异行数")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="只报告，不写盘")
    p_check.add_argument("--bots-root", default=DEFAULT_BOTS_ROOT, help="bot 根目录（默认 ~/.dsh-bot/bots）")
    p_check.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS)

    p_sync = sub.add_parser("sync", help="按模板刷新圈内")
    which = p_sync.add_mutually_exclusive_group(required=True)
    which.add_argument("--bot", metavar="BOT_ID", help="只刷新这只 bot")
    which.add_argument("--all", action="store_true", help="刷新全部 bot")
    p_sync.add_argument("--skill", metavar="通用名", help="只刷新这一个技能（配合 --bot）")
    p_sync.add_argument("--bots-root", default=DEFAULT_BOTS_ROOT, help="bot 根目录（默认 ~/.dsh-bot/bots）")
    return parser


def _inner_content_start(text):
    """圈内第一行内容在 ``text`` 里的下标（跳过紧挨 START 的换行，与 ``handoff.inner`` 一致）。"""
    at = text.index(handoff.START) + len(handoff.START)
    while at < len(text) and text[at] in "\r\n":
        at += 1
    return at


def _diff_line_numbers(path, generic):
    """这只 bot 这个技能里有差异的行的文件行号（1 起）；读不出来就回空列表。"""
    try:
        text, problem = _read_skill(path)
    except OSError:
        return []
    if problem:
        return []
    text = _to_lf(text)
    old_inner = handoff.inner(text)
    if old_inner is None:
        return []
    new_inner = _render_inner(generic, old_inner)
    if new_inner is None:
        return []
    old_lines, new_lines = old_inner.splitlines(), new_inner.splitlines()
    offsets = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old_lines, new_lines).get_opcodes():
        if tag == "equal":
            continue
        # 新旧两侧受影响的都列出来：改前的行号与刷新后会变的位置
        offsets.extend(range(i1, i2))
        offsets.extend(range(j1, j2))
    base = text.count("\n", 0, _inner_content_start(text)) + 1
    return sorted(set(base + offset for offset in offsets))


#: check 里跳过项的 --verbose 说明（不打印任何技能正文）
_REASON_TEXT = {"template_missing": "模板不存在", "template_no_marker": "模板缺成对标记"}


def _cli_check(args):
    bots_root = os.fspath(args.bots_root)
    result = check(bots_root)
    verbose = getattr(args, "verbose", False)
    for bot_id, info in result["bots"].items():
        for generic, item in info["skills"].items():
            line = "%s  %s  %s  %d 行" % (bot_id, generic, item["state"], item["diff_lines"])
            if verbose:
                reason = _REASON_TEXT.get(item.get("reason"))
                if reason:
                    line += "  （%s）" % reason
                if item["state"] == "diff":
                    numbers = _diff_line_numbers(
                        _skill_path(os.path.join(bots_root, bot_id), generic), generic)
                    if numbers:
                        line += "  第 " + "、".join(str(n) for n in numbers) + " 行"
            print(line)
    totals = result["totals"]
    print("检查完成：%d 只 bot；一致 %d，有差异 %d，跳过 %d" % (
        len(result["bots"]), totals["same"], totals["diff"], totals["skipped"]))
    return 1 if totals["diff"] else 0


def _cli_sync(args, parser):
    if args.all and args.skill:
        parser.error("--skill 只能和 --bot 一起用")
    if args.all:
        results = sync_all(args.bots_root)
    else:
        bot_id = args.bot
        if not bot_id or bot_id in (".", "..") or "/" in bot_id or "\\" in bot_id \
                or os.path.isabs(bot_id) or (len(bot_id) > 1 and bot_id[1] == ":"):
            parser.error("bot_id 不合法：%r" % bot_id)
        bot_dir = os.path.join(args.bots_root, bot_id)
        persona = _bot_persona(bot_dir)
        options = {"persona": persona} if persona is not None else {}
        if args.skill:
            results = {bot_id: {args.skill: sync_bot(bot_dir, args.skill, **options)}}
        else:
            results = {bot_id: {generic: sync_bot(bot_dir, generic, **options)
                                for generic in _existing_generics(bot_dir)}}

    changed = unchanged = failed = 0
    for bot_id, skills in results.items():
        for generic, item in skills.items():
            if item["ok"]:
                if item["changed"]:
                    changed += 1
                    print("已刷新 %s/%s；备份：%s" % (bot_id, generic, item["backup"]))
                else:
                    unchanged += 1
            else:
                failed += 1
                print("跳过 %s/%s：%s" % (bot_id, generic, item["reason"]))
    print("刷新完成：已改 %d，无需改 %d，失败 %d" % (changed, unchanged, failed))
    return 1 if failed else 0


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "check":
        return _cli_check(args)
    return _cli_sync(args, parser)


if __name__ == "__main__":
    sys.exit(main())
