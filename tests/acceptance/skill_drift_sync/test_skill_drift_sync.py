# -*- coding: utf-8 -*-
"""skill-drift-sync 黑盒验收：只经公开函数与命令行验证接口契约。

不 import moments.skill_templates / moments.handoff 等内部模块，不 mock。
被测模块在用例内部动态导入：实现不存在时是"用例失败"，不是"收集错误"。
"""
import datetime
import importlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
START = "<!-- portable:start -->"
END = "<!-- portable:end -->"
BACKUP_SUFFIX = ".bak-before-skill-sync"
IMAGE_GENERIC = "image"
NSFW_GENERIC = "nsfw"
# image 模板在拿不到外貌变量时渲染出的 fallback 文案（经公开函数观测）
IMAGE_FALLBACK_APPEARANCE = "按人设里的外貌描写来画。"
GENERIC = "gif"
OTHER_GENERIC = "voice"
OUTSIDE_BEFORE = "圈外前-KEEP-9f3a"
OUTSIDE_AFTER = "圈外后-KEEP-9f3a"
OLD_INNER = "旧圈内-SENTINEL-9f3a"
DRIFT_INNER = "与模板不同的圈内-DRIFT-9f3a"


def _skill_sync():
    """在被测用例内部导入；模块缺失时本用例失败而非收集期报错。"""
    return importlib.import_module("moments.skill_sync")


def _run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "moments.skill_sync", *args],
        cwd=str(PROJECT_ROOT),
        text=True,
        capture_output=True,
        timeout=60,
    )


def _run_verbose_check(bots_root):
    """--verbose 的放置位置接口未规定，按两种合法位置尝试。"""
    first = _run_cli("check", "--bots-root", str(bots_root), "--verbose")
    if first.returncode == 2:
        return _run_cli("--verbose", "check", "--bots-root", str(bots_root))
    return first


def _assert_not_import_crash(result):
    assert "No module named 'moments.skill_sync'" not in result.stderr


def _skill_path(bot_dir, generic=GENERIC):
    return Path(bot_dir) / "channel" / "skills" / ("skill-" + generic) / "SKILL.md"


def _write_skill(bot_dir, content, generic=GENERIC):
    path = _skill_path(bot_dir, generic)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return path


def _skill_file(inner, generic=GENERIC):
    return (
        "---\nname: skill-%s\ndescription: 测试\n---\n" % generic
        + OUTSIDE_BEFORE + "\n"
        + START + "\n" + inner + "\n" + END + "\n"
        + OUTSIDE_AFTER + "\n"
    )


def _backup_path(path):
    """接口约定：备份名为 <原路径><suffix>-<YYYYMMDD>。"""
    return Path(str(path) + BACKUP_SUFFIX + "-" + datetime.date.today().strftime("%Y%m%d"))


def _circle(path):
    text = Path(path).read_text(encoding="utf-8")
    assert START in text and END in text
    return text.split(START, 1)[1].split(END, 1)[0]


def _nsfw_old_section(*examples):
    """构造旧文件圈内“已渲染过例句段”的形态，只依赖模板占位语义。"""
    return (
        "# 情欲场景语气参考\n\n"
        "以下只作语气参考，不是必用清单。按当下情境自然反应，不照搬原句。\n\n"
        + "\n".join("- " + example for example in examples)
        + "\n\n说话特点和回复规则照常驻人设，这份语料不压过人设。"
    )


def _assert_bot_counts(info):
    """bot 级 same/diff/skipped 必须与逐项 state 自洽（跳过的只有 no_marker/missing）。"""
    states = [item["state"] for item in info["skills"].values()]
    assert set(states) <= {"same", "diff", "no_marker", "missing"}
    assert info["same"] == states.count("same")
    assert info["diff"] == states.count("diff")
    assert info["skipped"] == states.count("no_marker") + states.count("missing")


def test_check与sync_all_根目录不存在_返回空结果且退出码0(tmp_path):
    module = _skill_sync()
    missing = tmp_path / "不存在的根目录"

    assert module.check(str(missing)) == {
        "bots": {},
        "totals": {"same": 0, "diff": 0, "skipped": 0},
    }
    assert module.sync_all(str(missing)) == {}

    checked = _run_cli("check", "--bots-root", str(missing))
    assert checked.returncode == 0
    synced = _run_cli("sync", "--all", "--bots-root", str(missing))
    assert synced.returncode == 0


def test_check_bot目录没有skills_视为没有技能且不报错(tmp_path):
    bots_root = tmp_path / "bots"
    (bots_root / "bot1").mkdir(parents=True)

    result = _skill_sync().check(str(bots_root))

    assert set(result["bots"]) == {"bot1"}
    info = result["bots"]["bot1"]
    assert info == {"skills": {}, "same": 0, "diff": 0, "skipped": 0}
    assert result["totals"] == {"same": 0, "diff": 0, "skipped": 0}
    assert _run_cli("check", "--bots-root", str(bots_root)).returncode == 0


def test_check_文件没有成对标记_记为no_marker并计skipped(tmp_path):
    bot_dir = tmp_path / "bots" / "bot1"
    _write_skill(bot_dir, "圈外\n" + START + "\n只有开头没有结尾\n")

    result = _skill_sync().check(str(tmp_path / "bots"))
    info = result["bots"]["bot1"]

    assert info["skills"][GENERIC]["state"] == "no_marker"
    assert isinstance(info["skills"][GENERIC]["diff_lines"], int)
    assert info["diff"] == 0
    _assert_bot_counts(info)
    assert result["totals"]["diff"] == 0
    assert _run_cli("check", "--bots-root", str(tmp_path / "bots")).returncode == 0


def test_check_圈内与模板一致_记为same且忽略圈内首尾换行(tmp_path):
    bots_root = tmp_path / "bots"
    bot_dir = bots_root / "bot1"
    path = _write_skill(bot_dir, _skill_file(OLD_INNER))
    module = _skill_sync()

    first_sync = module.sync_bot(str(bot_dir), GENERIC)
    assert first_sync["ok"] is True
    assert first_sync["changed"] is True

    result = module.check(str(bots_root))
    info = result["bots"]["bot1"]
    assert info["skills"][GENERIC] == {"state": "same", "diff_lines": 0}
    assert info["same"] == 1
    assert info["diff"] == 0
    _assert_bot_counts(info)
    assert result["totals"]["same"] == 1
    assert result["totals"]["diff"] == 0

    # 契约：比较前各自 strip 首尾换行，圈内两端多出的空行不应造成 diff
    text = path.read_text(encoding="utf-8")
    text = text.replace(START + "\n", START + "\n\n", 1)
    text = text.replace("\n" + END, "\n\n" + END, 1)
    path.write_text(text, encoding="utf-8")
    again = module.check(str(bots_root))
    assert again["bots"]["bot1"]["skills"][GENERIC]["state"] == "same"

    assert _run_cli("check", "--bots-root", str(bots_root)).returncode == 0


def test_check_圈内有差异_记为diff_退出码1且默认不打印正文(tmp_path):
    bots_root = tmp_path / "bots"
    bot_dir = bots_root / "bot1"
    path = _write_skill(bot_dir, _skill_file(DRIFT_INNER))
    module = _skill_sync()
    before = path.stat()

    result = module.check(str(bots_root))
    info = result["bots"]["bot1"]
    item = info["skills"][GENERIC]

    assert item["state"] == "diff"
    assert isinstance(item["diff_lines"], int) and item["diff_lines"] >= 1
    assert info["diff"] >= 1
    _assert_bot_counts(info)
    assert result["totals"]["diff"] >= 1

    after = path.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns), "check 不应写盘"

    plain = _run_cli("check", "--bots-root", str(bots_root))
    assert plain.returncode == 1
    _assert_not_import_crash(plain)
    assert DRIFT_INNER not in plain.stdout, "默认输出不得打印技能正文"

    verbose = _run_verbose_check(bots_root)
    assert verbose.returncode == 1
    assert GENERIC in verbose.stdout
    assert "diff" in verbose.stdout
    assert str(item["diff_lines"]) in verbose.stdout


def test_check_技能文件非UTF8_记为no_marker并跳过(tmp_path):
    bot_dir = tmp_path / "bots" / "bot1"
    content = (OUTSIDE_BEFORE + "\n").encode() + (START + "\n").encode()
    content += b"\xff\xfe\x00\x80\n"
    content += (END + "\n").encode() + (OUTSIDE_AFTER + "\n").encode()
    _write_skill(bot_dir, content)

    result = _skill_sync().check(str(tmp_path / "bots"))
    info = result["bots"]["bot1"]

    assert info["skills"][GENERIC]["state"] == "no_marker"
    assert info["diff"] == 0
    _assert_bot_counts(info)
    assert result["totals"]["diff"] == 0
    assert _run_cli("check", "--bots-root", str(tmp_path / "bots")).returncode == 0


def test_check_技能文件大小阈值_超过256KiB跳过而恰好256KiB仍比对(tmp_path):
    bots_root = tmp_path / "bots"
    big_bot = bots_root / "bot_big"
    big = (START + "\n" + OLD_INNER + "\n" + END + "\n").encode() + b"x" * (256 * 1024)
    assert len(big) > 256 * 1024
    _write_skill(big_bot, big)

    edge_bot = bots_root / "bot_edge"
    core = (START + "\n" + DRIFT_INNER + "\n" + END + "\n").encode()
    edge = b"y" * (256 * 1024 - len(core) - len(b"tail\n")) + core + b"tail\n"
    assert len(edge) == 256 * 1024
    _write_skill(edge_bot, edge)

    result = _skill_sync().check(str(bots_root))
    big_info = result["bots"]["bot_big"]
    edge_info = result["bots"]["bot_edge"]

    assert big_info["skills"][GENERIC]["state"] == "no_marker"
    _assert_bot_counts(big_info)
    assert edge_info["skills"][GENERIC]["state"] == "diff"
    _assert_bot_counts(edge_info)
    assert result["totals"]["diff"] == 1
    assert _run_cli("check", "--bots-root", str(bots_root)).returncode == 1


def test_sync_无成对标记_返回no_marker_不写盘不备份_命令行退出码1(tmp_path):
    bots_root = tmp_path / "bots"
    bot_dir = bots_root / "bot1"
    path = _write_skill(bot_dir, "圈外\n" + START + "\n正文不能泄露-9f3a\n")
    before_bytes = path.read_bytes()
    before_stat = path.stat()
    backup = _backup_path(path)

    result = _skill_sync().sync_bot(str(bot_dir), GENERIC)

    assert result == {"ok": False, "reason": "no_marker"}
    assert path.read_bytes() == before_bytes
    assert path.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert not backup.exists()

    cli = _run_cli("sync", "--bot", "bot1", "--skill", GENERIC, "--bots-root", str(bots_root))
    assert cli.returncode == 1
    _assert_not_import_crash(cli)
    assert "正文不能泄露-9f3a" not in cli.stdout, "默认输出不得打印技能正文"


def test_sync_技能文件不存在_返回missing且不产生任何文件(tmp_path):
    bot_dir = tmp_path / "bots" / "bot1"
    bot_dir.mkdir(parents=True)

    result = _skill_sync().sync_bot(str(bot_dir), GENERIC)

    assert result == {"ok": False, "reason": "missing"}
    assert list(bot_dir.rglob("*")) == []


def test_sync_模板不存在_返回template_missing且原文不变(tmp_path):
    bot_dir = tmp_path / "bots" / "bot1"
    unknown = "not-a-template-9f3a"
    path = _write_skill(bot_dir, _skill_file(OLD_INNER, unknown), unknown)
    before = path.read_bytes()

    result = _skill_sync().sync_bot(str(bot_dir), unknown)

    assert result == {"ok": False, "reason": "template_missing"}
    assert path.read_bytes() == before
    assert not _backup_path(path).exists()


def test_sync_圈内有变化_只换圈内_生成同权限备份并保留文件权限(tmp_path):
    bot_dir = tmp_path / "bots" / "bot1"
    path = _write_skill(bot_dir, _skill_file(OLD_INNER))
    os.chmod(path, 0o640)
    original = path.read_bytes()
    backup = _backup_path(path)

    result = _skill_sync().sync_bot(str(bot_dir), GENERIC)

    assert result["ok"] is True
    assert result["changed"] is True
    assert result["backup"] == str(backup)
    assert backup.exists()
    assert backup.read_bytes() == original
    assert backup.parent == path.parent

    new_bytes = path.read_bytes()
    if os.name == "posix":
        assert (backup.stat().st_mode & 0o777) == 0o640
        assert (path.stat().st_mode & 0o777) == 0o640
    else:
        # Windows 只有只读位，只断言可读
        assert os.access(backup, os.R_OK) and os.access(path, os.R_OK)
    assert new_bytes.startswith(original.split(START.encode(), 1)[0]), "圈外前半必须逐字节不动"
    assert new_bytes.endswith(original.split(END.encode(), 1)[1]), "圈外后半必须逐字节不动"
    assert new_bytes.count(START.encode()) == 1
    assert new_bytes.count(END.encode()) == 1
    assert OLD_INNER.encode() not in new_bytes, "旧圈内必须被模板渲染结果替换"
    assert OUTSIDE_BEFORE.encode() in new_bytes
    assert OUTSIDE_AFTER.encode() in new_bytes


def test_sync_圈内已一致_不写盘不备份且命令行退出码0(tmp_path):
    bots_root = tmp_path / "bots"
    bot_dir = bots_root / "bot1"
    path = _write_skill(bot_dir, _skill_file(OLD_INNER))
    module = _skill_sync()
    assert module.sync_bot(str(bot_dir), GENERIC)["changed"] is True

    backup = _backup_path(path)
    assert backup.exists(), "首次同步应生成带当天日期的备份"
    backup.unlink()
    before_bytes = path.read_bytes()
    before_stat = path.stat()

    result = module.sync_bot(str(bot_dir), GENERIC)

    assert result == {"ok": True, "changed": False, "backup": None}
    assert path.read_bytes() == before_bytes
    after_stat = path.stat()
    assert after_stat.st_ino == before_stat.st_ino, "无变化时不应原子替换写盘"
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns, "无变化时不应写盘"
    assert not backup.exists(), "无变化时不应产生备份"

    cli = _run_cli("sync", "--bot", "bot1", "--skill", GENERIC, "--bots-root", str(bots_root))
    assert cli.returncode == 0
    assert not backup.exists()


def test_sync_原文CRLF_写回仍为CRLF且圈外不变(tmp_path):
    bot_dir = tmp_path / "bots" / "bot1"
    original = "\r\n".join([
        "---",
        "name: skill-gif",
        "description: 测试",
        "---",
        OUTSIDE_BEFORE,
        START,
        OLD_INNER,
        END,
        OUTSIDE_AFTER,
        "",
    ]).encode("utf-8")
    path = _write_skill(bot_dir, original)

    result = _skill_sync().sync_bot(str(bot_dir), GENERIC)
    assert result["ok"] is True and result["changed"] is True

    new_bytes = path.read_bytes()
    assert new_bytes.count(b"\n") > 0
    assert new_bytes.count(b"\r\n") == new_bytes.count(b"\n"), "原文是 CRLF 时写回必须保持 CRLF"
    assert new_bytes.startswith(original.split(START.encode(), 1)[0])
    assert new_bytes.endswith(original.split(END.encode(), 1)[1])
    assert OLD_INNER.encode() not in new_bytes


def test_sync_all_逐个同步_单个失败不影响其余(tmp_path):
    bots_root = tmp_path / "bots"
    good_path = _write_skill(bots_root / "bot_good", _skill_file(OLD_INNER))
    bad_path = _write_skill(bots_root / "bot_bad", "圈外\n" + START + "\n没有结尾\n")
    bad_before = bad_path.read_bytes()

    result = _skill_sync().sync_all(str(bots_root))

    assert set(result) == {"bot_good", "bot_bad"}
    assert set(result["bot_good"]) == {GENERIC}
    assert set(result["bot_bad"]) == {GENERIC}
    assert result["bot_good"][GENERIC]["ok"] is True
    assert result["bot_good"][GENERIC]["changed"] is True
    assert result["bot_bad"][GENERIC] == {"ok": False, "reason": "no_marker"}
    assert OLD_INNER.encode() not in good_path.read_bytes(), "成功的 bot 应真正写入"
    assert bad_path.read_bytes() == bad_before, "失败的 bot 不得被改动"
    assert not _backup_path(bad_path).exists()


def test_check_一个技能存在另一个技能缺失_缺失记为missing(tmp_path):
    bots_root = tmp_path / "bots"
    bot_dir = bots_root / "bot1"
    _write_skill(bot_dir, _skill_file(DRIFT_INNER), GENERIC)

    result = _skill_sync().check(str(bots_root))
    info = result["bots"]["bot1"]

    assert info["skills"][GENERIC]["state"] == "diff"
    assert info["skills"][OTHER_GENERIC]["state"] == "missing"
    assert info["skipped"] == len(info["skills"]) - 1, "除 diff 外其余模板技能都应记为 missing/skipped"
    _assert_bot_counts(info)
    assert result["totals"]["diff"] == 1
    assert _run_cli("check", "--bots-root", str(bots_root)).returncode == 1


def test_sync_image_旧圈内照片里的你之后的外貌_同步后原文写回新圈内(tmp_path):
    bot_dir = tmp_path / "bots" / "bot1"
    appearance = "外貌原文-APPEARANCE-9f3a"
    path = _write_skill(
        bot_dir,
        _skill_file("照片里的你：\n" + appearance, IMAGE_GENERIC),
        IMAGE_GENERIC,
    )

    result = _skill_sync().sync_bot(str(bot_dir), IMAGE_GENERIC)

    assert result["ok"] is True
    assert result["changed"] is True
    assert appearance in _circle(path), "旧圈内的外貌必须原样进入新圈内"


def test_sync_image_旧圈内没有照片里的你_同步后写回fallback文案(tmp_path):
    bot_dir = tmp_path / "bots" / "bot1"
    old_only = "旧圈内没有外貌标记-NOAPPEARANCE-9f3a"
    path = _write_skill(bot_dir, _skill_file(old_only, IMAGE_GENERIC), IMAGE_GENERIC)

    result = _skill_sync().sync_bot(str(bot_dir), IMAGE_GENERIC)

    assert result["ok"] is True
    circle = _circle(path)
    assert IMAGE_FALLBACK_APPEARANCE in circle
    assert old_only not in circle, "拿不到外貌变量时不应沿用旧圈内正文"


def test_sync_nsfw_传入persona_用persona的例句替换旧例句(tmp_path):
    bot_dir = tmp_path / "bots" / "bot1"
    path = _write_skill(
        bot_dir,
        _skill_file(_nsfw_old_section("旧例句NSFW-OLD-9f3a"), NSFW_GENERIC),
        NSFW_GENERIC,
    )
    persona = (
        "## 语料库\n"
        "1.日常对话\n- 日常一句DAILY-9f3a\n\n"
        "2.NSFW\n- 新例句NSFW-NEW-9f3a\n- 新例句二NSFW-NEW2-9f3a\n\n"
        "## USER.md：\n- Name: 测试\n"
    )

    result = _skill_sync().sync_bot(str(bot_dir), NSFW_GENERIC, persona=persona)

    assert result["ok"] is True
    circle = _circle(path)
    assert "新例句NSFW-NEW-9f3a" in circle
    assert "新例句二NSFW-NEW2-9f3a" in circle
    assert "旧例句NSFW-OLD-9f3a" not in circle, "传了 persona 就不该沿用旧例句"
    assert "日常一句DAILY-9f3a" not in circle, "只提取 NSFW 段，日常语料不进入技能"


def test_sync_nsfw_不传persona_旧圈内例句段原样保留(tmp_path):
    bot_dir = tmp_path / "bots" / "bot1"
    path = _write_skill(
        bot_dir,
        _skill_file(
            _nsfw_old_section("旧例句NSFW-KEEP-9f3a", "旧例句二NSFW-KEEP2-9f3a"),
            NSFW_GENERIC,
        ),
        NSFW_GENERIC,
    )

    result = _skill_sync().sync_bot(str(bot_dir), NSFW_GENERIC)

    assert result["ok"] is True
    circle = _circle(path)
    assert "旧例句NSFW-KEEP-9f3a" in circle
    assert "旧例句二NSFW-KEEP2-9f3a" in circle


def test_check_默认输出_每个技能一行含名字状态行数且不含正文(tmp_path):
    bots_root = tmp_path / "bots"
    _write_skill(bots_root / "bot1", _skill_file(DRIFT_INNER))

    result = _run_cli("check", "--bots-root", str(bots_root))

    assert result.returncode == 1
    assert re.search(
        r"bot1\s+%s\s+diff\s+\d+\s+行" % re.escape(GENERIC), result.stdout
    ), "默认输出应在同一行给出 bot 名、技能名、diff 与差异行数"
    assert DRIFT_INNER not in result.stdout, "默认输出不得打印技能正文"


def test_check_verbose_输出差异行号且仍不含正文(tmp_path):
    bots_root = tmp_path / "bots"
    _write_skill(bots_root / "bot1", _skill_file(DRIFT_INNER))

    result = _run_verbose_check(bots_root)

    assert result.returncode == 1
    assert re.search(r"第\s*\d+(?:\s*、\s*\d+)*\s*行", result.stdout), \
        "verbose 应给出差异行号"
    assert DRIFT_INNER not in result.stdout, "verbose 也不得打印技能正文"
