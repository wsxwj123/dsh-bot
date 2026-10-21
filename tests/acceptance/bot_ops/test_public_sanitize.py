# -*- coding: utf-8 -*-
"""public-sanitize 黑盒验收（只依据 `.devflow/public-sanitize-接口.md` 的 5 条行为）。

不 import 任何产品内部模块，只通过对外接缝验证：`git ls-files` + 读取跟踪文件内容。

两条自保约定（否则扫描会命中测试自己）：
1. 家目录检查用通用模式匹配，不依赖运行机器的 HOME；
   测试文件里出现的假路径全部列入白名单。
2. 数字串检查只匹配身份字段（chat_id/from_id/user_id/peer_id/sender_id/group_id，
   含 JSON 键、嵌套 id、变量赋值、类型注解、getenv 兜底等写法）位置上的连续
   9 位及以上数字；白名单只放明显的测试假值。测试文件里不保存任何真实编号
   （包括拆分拼接）。
"""

import codecs
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# 仓库根：tests/acceptance/bot_ops/ 往上三层
REPO_ROOT = Path(__file__).resolve().parents[3]

# 行为 1 的前缀
LOCAL_DOC_PREFIX = "docs/dsh-migration/" + "local/"

# 行为 2：通用家目录模式，带不带结尾斜杠都命中。
# 用户名段只取路径字符（遇到空白/引号/括号即停），避免把后面的散文一起吞进来；
# `\b` 保留词边界语义。源码里不出现完整模式字面量。
HOME_PATTERN = re.compile("/Users/" + r"""[^/\s"'`<>()]+\b""")
# 白名单只放测试夹具里刻意使用的假路径（按实际匹配串，不含结尾斜杠）。
HOME_WHITELIST = frozenset({
    "/Users/you",
    "/Users/someone",
    "/Users/x",
    "/Users/u",
    "/Users/john",  # test_install_compact_hook_quote.py 里假路径 "john smith" 的首段
})

# 行为 3：身份字段位置上的 9 位及以上数字串。
# 身份字段覆盖 JSON/字典键、变量赋值、类型注解、嵌套对象、函数默认值与环境变量兜底；
# 时间戳、哈希常量、随机种子等不在这些位置上的数字不进入扫描范围。
_ID_WORDS = r"(?:chat_?id|from_?id|user_?id|peer_?id|sender_?id|group_?id)"
_ID_VAR_WORD = r"(?:CHAT|FROM_ID|USER_ID|PEER_ID|SENDER_ID|GROUP)"
_VALUE = r"[\"']?-?(\d{9,})"

IDENTITY_PATTERNS = (
    # JSON / 字典键、普通赋值、带类型注解的赋值、函数默认值
    re.compile(
        r"(?<![A-Za-z0-9_])" + _ID_WORDS + r"[\"']?\s*[:=]\s*"
        r"(?:[A-Za-z_][\w\[\]|., ]*\s*=\s*)?" + _VALUE,
        re.IGNORECASE,
    ),
    # 变量名包含身份词：CHAT / NEW_CHAT / GROUP_ID / SENDER_ID / OTHER_CHAT_ID 等
    re.compile(
        r"(?<![A-Za-z0-9])\w*(?:" + _ID_VAR_WORD + r")\w*"
        r"\s*(?::\s*[\w\[\]|., ]+\s*)?=\s*" + _VALUE,
        re.IGNORECASE,
    ),
    # 嵌套形态："chat": {"id": ...} / "from": {"id": ...}
    re.compile(
        r"[\"']?(?:chat|from)[\"']?\s*:\s*\{[^{}]*?[\"']?id[\"']?\s*:\s*" + _VALUE,
        re.IGNORECASE,
    ),
    # 环境变量兜底：os.getenv("CHAT_ID", "...") / os.environ.get(...)
    re.compile(
        r"\b(?:getenv|get)\(\s*[\"'][^\"']*(?:" + _ID_VAR_WORD + r")[^\"']*[\"']"
        r"\s*,\s*" + _VALUE,
        re.IGNORECASE,
    ),
)
# 白名单只放仓库里确实存在的测试假值（捕获组只取数字本身，负号不参与比对）。
IDENTITY_WHITELIST = frozenset({
    "1000000001",
    "1001234567890",
    "1005550001112",
    "1009876543210777",
    "1009876543210888",
    "1009876543",       # adapter_util.py NEW_CHAT
    "1009998887776",    # group-turn 测试的 OTHER_GROUP / GROUP_B
    "123456789",        # create bot 系列测试的占位 id
    "987654321",        # hub2 addbot 测试的占位 id
    "5550001777",       # adapter internals 测试的 CHAT 常量
})

# 扫描时永远不进入的目录/文件
_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache"}


def _tracked_files():
    """git 跟踪的相对路径列表（`-z` 以正确处理含空格/非 ASCII 的文件名）。"""
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert proc.returncode == 0, "git ls-files 失败: %r" % (proc.stderr[:500],)
    names = [raw for raw in proc.stdout.split(b"\x00") if raw]
    return [raw.decode("utf-8", "surrogateescape") for raw in names]


def _text_content(path):
    """返回文本内容；二进制返回 None。符号链接按链接目标算。

    带 UTF-16/UTF-32 BOM 的文件按文本解码，不因为前 8KB 含 NUL 就被误判为二进制。
    """
    if path.is_symlink():
        return os.readlink(path)
    data = path.read_bytes()
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return data.decode("utf-32", "replace")
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", "replace")
    if b"\x00" in data[:8192]:
        return None
    return data.decode("utf-8", "replace")


def _scan_tracked(regex, whitelist=()):
    """扫描跟踪文件内容。只读，不写盘。

    返回 (hits, skipped)：
    - hits：命中的文件及未白名单化的匹配值；
    - skipped：读取失败（OSError）而无法扫描的文件，不能静默丢弃。
    """
    whitelist = frozenset(whitelist)
    hits = []
    skipped = []
    for rel in _tracked_files():
        if rel.startswith(".git/") or rel == ".git":
            continue
        path = REPO_ROOT / rel
        if not (path.is_file() or path.is_symlink()):
            continue
        try:
            text = _text_content(path)
        except OSError as exc:
            skipped.append("%s (%s: %s)" % (rel, exc.__class__.__name__, exc))
            continue
        if text is None:
            # 二进制文件无法按文本扫描：不计为失败，但打印出来，避免"没扫到"被当成"没问题"。
            print("[scan] 跳过二进制文件: %s" % rel, file=sys.stderr)
            continue
        # 有捕获组时只比对捕获到的值（例如身份字段后面的数字），否则比对整个匹配。
        found = sorted(
            {(m.group(1) if regex.groups else m.group(0)) for m in regex.finditer(text)}
            - whitelist
        )
        if found:
            hits.append("%s -> %r" % (rel, found))
    return hits, skipped


def _scan_tracked_many(regexes, whitelist=()):
    """对同一批文件依次跑多个正则，合并命中与读取失败列表。"""
    hits = set()
    skipped = set()
    for regex in regexes:
        part_hits, part_skipped = _scan_tracked(regex, whitelist)
        hits.update(part_hits)
        skipped.update(part_skipped)
    return sorted(hits), sorted(skipped)


def _assert_scan_clean(description, regexes, whitelist):
    """命中或读取失败都算失败，两类信息一并列出。"""
    if not isinstance(regexes, (list, tuple)):
        regexes = [regexes]
    hits, skipped = _scan_tracked_many(regexes, whitelist)
    problems = []
    if hits:
        problems.append(
            "%d 个跟踪文本文件%s: %r" % (len(hits), description, hits[:10])
        )
    if skipped:
        problems.append(
            "%d 个跟踪文件读取失败、未能扫描（不排除漏报）: %r"
            % (len(skipped), skipped[:10])
        )
    assert problems == [], "\n".join(problems)


def _tail(text, limit=1500):
    text = text if isinstance(text, str) else text.decode("utf-8", "replace")
    return text[-limit:]


# ---------------------------------------------------------------- 行为 1

def test_清单1_跟踪文件里不存在_local目录下的文件():
    offenders = [rel for rel in _tracked_files() if rel.startswith(LOCAL_DOC_PREFIX)]
    assert offenders == [], (
        "仍有 %d 个 %s 下的文件处于 git 跟踪状态，例如: %r"
        % (len(offenders), LOCAL_DOC_PREFIX, offenders[:10])
    )


# ---------------------------------------------------------------- 行为 2

def test_清单2_跟踪文本文件内容不含通用家目录绝对路径():
    _assert_scan_clean("内容含通用家目录绝对路径模式", HOME_PATTERN, HOME_WHITELIST)


# ---------------------------------------------------------------- 行为 3

def test_清单3_跟踪文本文件身份字段不含未白名单的真实编号():
    _assert_scan_clean(
        "身份字段位置含未列入白名单的 9 位及以上数字串",
        IDENTITY_PATTERNS,
        IDENTITY_WHITELIST,
    )


# ---------------------------------------------------------------- 行为 4

def test_清单4_desync验收用例全部通过():
    if sys.platform != "darwin":
        pytest.skip(
            "非 macOS 平台：无法用 sandbox-exec 封闭真实 ~/.claude/channels 写路径，"
            "按接口要求显式跳过，不裸跑会写用户数据的用例"
        )
    sandbox_exec = shutil.which("sandbox-exec")
    if not sandbox_exec:
        pytest.skip(
            "macOS 上找不到 sandbox-exec：拒绝静默裸跑（会真实写用户数据），"
            "按接口要求显式跳过该条"
        )

    desync_dir = REPO_ROOT / "tests" / "acceptance" / "desync"
    assert desync_dir.is_dir(), "desync 验收目录不存在: %s" % desync_dir

    env = dict(os.environ)
    for key in ("PYTEST_CURRENT_TEST", "PYTEST_ADDOPTS", "PYTEST_XDIST_WORKER"):
        env.pop(key, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    command = [sys.executable, "-m", "pytest", str(desync_dir), "-q", "-p", "no:cacheprovider"]
    # 嵌套跑 desync 时，外层 bot_ops 夹具的反向断言要求真实 ~/.claude/channels 条目不变。
    # 实测该嵌套进程的个别旁路写入会落到真实 channels 目录；用 macOS 沙箱把这条写路径封死，
    # 既不碰用户数据，也不改变 desync 用例自己的断言（它们的 home 都在 tmp）。
    # 真实家目录用 passwd 取，不依赖可能被夹具改写的 HOME。
    import pwd

    real_home = pwd.getpwuid(os.getuid()).pw_dir
    real_channels = os.path.join(os.path.realpath(real_home), ".claude", "channels")
    policy = '(version 1)(allow default)(deny file-write* (subpath "%s"))' % real_channels
    command = [sandbox_exec, "-p", policy] + command

    proc = subprocess.run(
        command,
        cwd=str(REPO_ROOT),  # 必须从仓库根启动，子目录 conftest 才会加载 tests/conftest.py 总闸
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    output = proc.stdout.decode("utf-8", "replace")
    assert proc.returncode == 0, (
        "desync 验收退出码 %d，输出尾部:\n%s" % (proc.returncode, _tail(output))
    )
    matched = re.search(r"(\d+) passed", output)
    assert matched is not None, "输出里找不到 passed 计数:\n%s" % _tail(output)
    assert int(matched.group(1)) > 0, "没有一条 desync 用例通过:\n%s" % _tail(output)


# ---------------------------------------------------------------- 行为 5

def _snapshot_repo():
    """仓库工作区快照（路径 -> 大小 + mtime），跳过 .git 与缓存目录。

    同时跳过测试运行器自己的输出文件：harness 会在用例执行期间持续追加
    `.devflow/test-output-*.txt`，它不是扫描器写的，排除以避免误报。
    """
    snapshot = {}
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            path = Path(root) / name
            rel = path.relative_to(REPO_ROOT).as_posix()
            if name in _SKIP_DIRS or rel.startswith(".devflow/test-output-"):
                continue
            stat = path.lstat()
            snapshot[rel] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


def test_清单5_扫描器只读仓库不写盘():
    """接口第 5 条的全量回归由外层流程负责，本文件只验证扫描器只读、不写盘。"""
    before = _snapshot_repo()
    assert len(before) > 0, "仓库快照为空，扫描接缝可能已失效"

    # 把扫描行为各跑一遍，确认它们不改变工作区
    assert isinstance(_tracked_files(), list)
    _scan_tracked(HOME_PATTERN, HOME_WHITELIST)
    _scan_tracked_many(IDENTITY_PATTERNS, IDENTITY_WHITELIST)

    after = _snapshot_repo()
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
    assert (added, removed, changed) == ([], [], []), (
        "扫描过程改动了工作区: 新增=%r 删除=%r 变更=%r"
        % (added[:10], removed[:10], changed[:10])
    )
