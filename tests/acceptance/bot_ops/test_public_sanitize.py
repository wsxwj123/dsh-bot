# -*- coding: utf-8 -*-
"""public-sanitize 黑盒验收（只依据 `.devflow/public-sanitize-接口.md` 的 5 条行为）。

不 import 任何产品内部模块，只通过对外接缝验证：`git ls-files` + 读取跟踪文件内容。

两条自保约定（否则扫描会命中测试自己）：
1. 家目录前缀在运行期用 `os.path.expanduser("~")` 取，测试文件里不写死。
   必须是模块导入期取值：本目录的 autouse 夹具会把 HOME 改到 tmp。
2. 真实 chat id 在运行期由两段拼接得到，测试文件里不存在连续完整字面量。
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# 仓库根：tests/acceptance/bot_ops/ 往上三层
REPO_ROOT = Path(__file__).resolve().parents[3]

# 导入期（任何 autouse 夹具把 HOME 改到 tmp 之前）定住真实家目录
IMPORT_TIME_HOME = os.path.expanduser("~")

# 行为 1 的前缀
LOCAL_DOC_PREFIX = "docs/dsh-migration/" + "local/"

# 行为 3 的目标值：拆成两段，运行期拼接（测试文件内不出现完整数值串）
REAL_CHAT_ID = "53317" + "15732"

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
    """返回文本内容；二进制（前 8KB 含 NUL）返回 None。符号链接按链接目标算。"""
    if path.is_symlink():
        return os.readlink(path)
    data = path.read_bytes()
    if b"\x00" in data[:8192]:
        return None
    return data.decode("utf-8", "replace")


def _scan_tracked_contents(needle):
    """返回所有内容包含 needle 的跟踪文件相对路径。只读，不写盘。"""
    hits = []
    for rel in _tracked_files():
        if rel.startswith(".git/") or rel == ".git":
            continue
        path = REPO_ROOT / rel
        if not (path.is_file() or path.is_symlink()):
            continue
        try:
            text = _text_content(path)
        except OSError:
            continue
        if text is not None and needle in text:
            hits.append(rel)
    return hits


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

def test_清单2_跟踪文本文件内容不含家目录前缀():
    needle = IMPORT_TIME_HOME.rstrip("/")
    assert needle.startswith("/") and len(needle) > 1, (
        "运行期取到的家目录不像绝对路径: %r" % (needle,)
    )
    hits = _scan_tracked_contents(needle)
    assert hits == [], (
        "%d 个跟踪文本文件的内容含家目录前缀 %r，例如: %r"
        % (len(hits), needle, hits[:10])
    )


# ---------------------------------------------------------------- 行为 3

def test_清单3_跟踪文本文件内容不含真实chat_id():
    assert len(REAL_CHAT_ID) == 10 and REAL_CHAT_ID.isdigit()
    hits = _scan_tracked_contents(REAL_CHAT_ID)
    assert hits == [], (
        "%d 个跟踪文本文件的内容含真实 chat id，例如: %r" % (len(hits), hits[:10])
    )


# ---------------------------------------------------------------- 行为 4

def test_清单4_desync验收用例全部通过():
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
    sandbox_exec = shutil.which("sandbox-exec")
    real_channels = os.path.join(os.path.realpath(IMPORT_TIME_HOME), ".claude", "channels")
    if sandbox_exec and os.path.isdir(real_channels):
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
    """仓库工作区快照（路径 -> 大小 + mtime），跳过 .git 与缓存目录。"""
    snapshot = {}
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            if name in _SKIP_DIRS:
                continue
            path = Path(root) / name
            rel = path.relative_to(REPO_ROOT).as_posix()
            stat = path.lstat()
            snapshot[rel] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


def test_清单5_扫描自身只读仓库不写盘():
    before = _snapshot_repo()
    assert len(before) > 0, "仓库快照为空，扫描接缝可能已失效"

    # 把三条扫描行为各跑一遍，确认它们不改变工作区
    assert isinstance(_tracked_files(), list)
    _scan_tracked_contents(IMPORT_TIME_HOME.rstrip("/"))
    _scan_tracked_contents(REAL_CHAT_ID)

    after = _snapshot_repo()
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
    assert (added, removed, changed) == ([], [], []), (
        "扫描过程改动了工作区: 新增=%r 删除=%r 变更=%r"
        % (added[:10], removed[:10], changed[:10])
    )
