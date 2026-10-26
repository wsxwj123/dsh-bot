# -*- coding: utf-8 -*-
"""novelai-public-sync 黑盒验收（只依据 `.devflow/novelai-public-sync-接口.md`）。

覆盖接口第 3 节 12 条行为与第 2 节 2 条错误契约。不 import 任何产品内部模块、
不 mock 内部层；只通过对外接缝验证：
- 技能 CLI 子进程（prompt_builder.py 正常/异常路径、generate_novelai_image.py --help）；
- 技能目录下公开文件的内容（SKILL.md、styles.json）与目录结构。

自保约定（本仓 public-sanitize 扫描也会扫到本文件）：
1. 家目录检查用通用正则，代码里只出现 `/Users/` 前缀字面量，不出现具体用户名；
2. 不出现身份字段位置上的 9 位及以上数字；
3. 所有子进程禁用或重定向字节码缓存，测试自己不生成 __pycache__ 污染仓库。
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_DIR = REPO_ROOT / "skills" / "novelai-skill"
SKILL_MD = SKILL_DIR / "SKILL.md"
PROMPT_BUILDER = SKILL_DIR / "scripts" / "prompt_builder.py"
IMAGE_SCRIPT = SKILL_DIR / "scripts" / "generate_novelai_image.py"
CONFIG = SKILL_DIR / "assets" / "default_config.json"
STYLES = SKILL_DIR / "assets" / "styles.json"

# 接口第 1 条所说的"四个文件"：SKILL.md、两个脚本、styles.json。
DELIVERED_FILES = (SKILL_MD, PROMPT_BUILDER, IMAGE_SCRIPT, STYLES)


def _child_env(tmp_path):
    """子进程环境：不写字节码到仓库；COLUMNS 拉宽避免 argparse 折行。"""
    env = dict(os.environ)
    env.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": str(tmp_path / "_pycache"),
            "PYTHONIOENCODING": "utf-8",
            "COLUMNS": "200",
        }
    )
    return env


def _run_script(script, args, tmp_path):
    return subprocess.run(
        [sys.executable, str(script)] + [str(arg) for arg in args],
        cwd=str(REPO_ROOT),
        env=_child_env(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120,
    )


def _write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _base_payload():
    return {"mode": "new", "prompt": "1girl, solo, long hair, in bedroom"}


def _require_output_keys(data, keys):
    """旧版输出缺新键时，报明确的断言失败而不是 KeyError。"""
    missing = [key for key in keys if key not in data]
    assert missing == [], "输出 JSON 缺少键 %r，实际键: %r" % (
        missing,
        sorted(data),
    )


# ---------------------------------------------------------------- 行为 1

def test_行为1_SKILL_md含角色分段与尺寸选择():
    text = SKILL_MD.read_text(encoding="utf-8")
    required = [
        "### 角色分段（characters）",
        "## 尺寸选择（--ratio）",
        "position_mode",
        '"center"',
        "portrait",
        "landscape",
        "square",
        "wide",
    ]
    missing = [item for item in required if item not in text]
    assert missing == [], "SKILL.md 缺少: %r" % (missing,)


# ---------------------------------------------------------------- 行为 2

def test_行为2_两人manual带center_输出坐标(tmp_path):
    payload = _base_payload()
    payload["characters"] = [
        {"positive": "girl, long hair", "center": {"x": 0.35, "y": 0.5}},
        {"positive": "boy, short hair", "center": {"x": 0.65, "y": 0.5}},
    ]
    payload["position_mode"] = "manual"
    intermediate = _write_json(tmp_path / "intermediate.json", payload)
    output = tmp_path / "output.json"

    proc = _run_script(
        PROMPT_BUILDER,
        ["--intermediate", intermediate, "--config", CONFIG, "--output", output],
        tmp_path,
    )

    assert proc.returncode == 0, "退出码 %d，stderr:\n%s" % (
        proc.returncode,
        proc.stderr,
    )
    assert output.exists(), "--output 指定的文件没有生成"
    data = json.loads(output.read_text(encoding="utf-8"))
    _require_output_keys(data, ("characters", "position_mode", "use_coords"))

    assert len(data["characters"]) == 2, "characters 长度应为 2: %r" % (
        data["characters"],
    )
    assert data["position_mode"] == "manual"
    assert data["use_coords"] is True

    expected = [("girl, long hair", 0.35, 0.5), ("boy, short hair", 0.65, 0.5)]
    for index, (item, (positive, x, y)) in enumerate(zip(data["characters"], expected)):
        assert item["positive"] == positive, "第 %d 个角色 positive 不一致" % index
        assert isinstance(item["center"]["x"], float), (
            "第 %d 个角色 center.x 不是浮点数: %r" % (index, item["center"]["x"])
        )
        assert isinstance(item["center"]["y"], float), (
            "第 %d 个角色 center.y 不是浮点数: %r" % (index, item["center"]["y"])
        )
        assert item["center"]["x"] == x
        assert item["center"]["y"] == y


# ---------------------------------------------------------------- 行为 3

def test_行为3_两人不写position_mode_auto且无坐标(tmp_path):
    payload = _base_payload()
    payload["characters"] = [
        {"positive": "girl, long hair"},
        {"positive": "boy, short hair"},
    ]
    intermediate = _write_json(tmp_path / "intermediate.json", payload)
    output = tmp_path / "output.json"

    proc = _run_script(
        PROMPT_BUILDER,
        ["--intermediate", intermediate, "--config", CONFIG, "--output", output],
        tmp_path,
    )

    assert proc.returncode == 0, "退出码 %d，stderr:\n%s" % (
        proc.returncode,
        proc.stderr,
    )
    assert output.exists(), "--output 指定的文件没有生成"
    data = json.loads(output.read_text(encoding="utf-8"))
    _require_output_keys(data, ("characters", "position_mode", "use_coords"))

    assert data["position_mode"] == "auto"
    assert data["use_coords"] is False
    assert len(data["characters"]) == 2, "characters 长度应为 2: %r" % (
        data["characters"],
    )
    for index, item in enumerate(data["characters"]):
        assert "center" not in item, "第 %d 个角色不应有 center: %r" % (index, item)


# ---------------------------------------------------------------- 行为 4

def test_行为4_characters空列表_输出空列表(tmp_path):
    payload = _base_payload()
    payload["characters"] = []
    intermediate = _write_json(tmp_path / "intermediate.json", payload)
    output = tmp_path / "output.json"

    proc = _run_script(
        PROMPT_BUILDER,
        ["--intermediate", intermediate, "--config", CONFIG, "--output", output],
        tmp_path,
    )

    assert proc.returncode == 0, "退出码 %d，stderr:\n%s" % (
        proc.returncode,
        proc.stderr,
    )
    assert output.exists(), "--output 指定的文件没有生成"
    data = json.loads(output.read_text(encoding="utf-8"))
    _require_output_keys(data, ("characters",))
    assert data["characters"] == [], "characters 应为空列表: %r" % (data["characters"],)


# ---------------------------------------------------------------- 行为 5

# 接口第 2 节列出的四类非法 characters。
INVALID_CHARACTERS_CASES = [
    ("characters不是列表", {"characters": "girl, boy"}),
    (
        "角色缺positive",
        {
            "characters": [
                {"center": {"x": 0.4, "y": 0.5}},
                {"positive": "boy", "center": {"x": 0.65, "y": 0.5}},
            ]
        },
    ),
    (
        "manual缺center",
        {
            "position_mode": "manual",
            "characters": [
                {"positive": "girl", "center": {"x": 0.35, "y": 0.5}},
                {"positive": "boy"},
            ],
        },
    ),
    (
        "center越界",
        {
            "position_mode": "manual",
            "characters": [
                {"positive": "girl", "center": {"x": 0.35, "y": 0.5}},
                {"positive": "boy", "center": {"x": 1.5, "y": 0.5}},
            ],
        },
    ),
]


def test_行为5_characters不合法_非0退出且不写输出(tmp_path):
    problems = []
    for index, (case_name, extra) in enumerate(INVALID_CHARACTERS_CASES):
        payload = _base_payload()
        payload.update(extra)
        intermediate = _write_json(tmp_path / ("invalid_%d.json" % index), payload)
        output = tmp_path / ("invalid_output_%d.json" % index)

        proc = _run_script(
            PROMPT_BUILDER,
            ["--intermediate", intermediate, "--config", CONFIG, "--output", output],
            tmp_path,
        )

        if proc.returncode == 0:
            problems.append("%s: 应以非 0 退出，实际为 0" % case_name)
        if "characters" not in proc.stderr:
            problems.append(
                "%s: stderr 未提及 characters: %r" % (case_name, proc.stderr[-500:])
            )
        if not re.search(r"[一-鿿]", proc.stderr):
            problems.append(
                "%s: stderr 没有中文原因: %r" % (case_name, proc.stderr[-500:])
            )
        if output.exists():
            problems.append("%s: 不应写出 --output 指定的文件" % case_name)

    assert problems == [], "\n".join(problems)


# ---------------------------------------------------------------- 行为 6

def test_行为6_generate帮助_含新参数与尺寸预设(tmp_path):
    proc = _run_script(IMAGE_SCRIPT, ["--help"], tmp_path)
    assert proc.returncode == 0, "--help 退出码 %d，stderr:\n%s" % (
        proc.returncode,
        proc.stderr,
    )
    help_text = proc.stdout + proc.stderr
    required = [
        "--ratio {square,portrait,landscape,wide}",
        "--agent-name",
        "--chat-id",
        "--reuse-seed",
        "portrait=832x1216",
        "landscape=1216x832",
        "square=1024x1024",
        "wide=1536x640",
    ]
    missing = [item for item in required if item not in help_text]
    assert missing == [], "帮助里缺少: %r\n实际帮助:\n%s" % (missing, help_text)


# ---------------------------------------------------------------- 行为 7

def test_行为7_prompt_builder帮助_退出0(tmp_path):
    proc = _run_script(PROMPT_BUILDER, ["--help"], tmp_path)
    assert proc.returncode == 0, "--help 退出码 %d，stderr:\n%s" % (
        proc.returncode,
        proc.stderr,
    )
    assert "usage" in (proc.stdout + proc.stderr).lower(), "--help 没有输出用法说明"


# ---------------------------------------------------------------- 行为 8

def test_行为8_styles结构(tmp_path):
    data = json.loads(STYLES.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "styles.json 顶层不是对象"

    for key in ("active", "styles", "active_by_bot"):
        assert key in data, "styles.json 顶层缺 %r" % key

    assert isinstance(data["styles"], list), "styles 不是列表"
    assert len(data["styles"]) >= 49, "styles 只有 %d 条，少于 49" % len(
        data["styles"]
    )
    assert data["active_by_bot"] == {}, "active_by_bot 应为空对象: %r" % (
        data["active_by_bot"],
    )

    required_keys = (
        "id",
        "name",
        "positive_prefix",
        "negative_prefix",
        "params",
        "sample_image",
    )
    for index, style in enumerate(data["styles"]):
        assert isinstance(style, dict), "第 %d 条样式不是对象" % index
        missing = [key for key in required_keys if key not in style]
        assert missing == [], "第 %d 条样式缺 %r: %r" % (
            index,
            missing,
            style.get("id"),
        )


# ---------------------------------------------------------------- 行为 9

# 通用家目录模式：只认 `/Users/` 后紧跟路径字符的形态，不依赖本机 HOME。
HOME_PATH_RE = re.compile(re.escape("/Users/") + r"""[^/\s"'`<>()]+\b""")


def test_行为9_四个文件无家目录绝对路径(tmp_path):
    violations = []
    for path in DELIVERED_FILES:
        text = path.read_text(encoding="utf-8", errors="replace")
        hits = sorted(set(HOME_PATH_RE.findall(text)))
        if hits:
            violations.append(
                "%s: %r" % (path.relative_to(REPO_ROOT), hits[:5])
            )
    assert violations == [], "四个文件里存在家目录绝对路径: %s" % "; ".join(
        violations
    )


# ---------------------------------------------------------------- 行为 10

# 令牌形状：sk- / pst- 后跟 20 个以上字母数字/下划线/连字符。
TOKEN_RE = re.compile(r"(?:sk|pst)-[A-Za-z0-9_-]{20,}")


def test_行为10_四个文件无令牌串(tmp_path):
    violations = []
    for path in DELIVERED_FILES:
        text = path.read_text(encoding="utf-8", errors="replace")
        hits = sorted(set(TOKEN_RE.findall(text)))
        if hits:
            violations.append(
                "%s: %r" % (path.relative_to(REPO_ROOT), hits[:5])
            )
    assert violations == [], "四个文件里存在令牌形状串: %s" % "; ".join(violations)


# ---------------------------------------------------------------- 行为 11

def _tracked_skill_files():
    """`git ls-files` 列出 skills/novelai-skill/ 下已跟踪文件的仓库相对路径。

    与 test_public_sanitize.py 同一判定口径：只看 git 跟踪的文件，运行时生成的
    未跟踪 `__pycache__/` 等产物不算残留。
    """
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "ls-files",
            "-z",
            "--",
            "skills/novelai-skill",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert proc.returncode == 0, "git ls-files 失败: %r" % (proc.stderr[:500],)
    names = [raw for raw in proc.stdout.split(b"\x00") if raw]
    return [raw.decode("utf-8", "surrogateescape") for raw in names]


def test_行为11_技能目录无敏感残留文件(tmp_path):
    tracked = _tracked_skill_files()
    assert tracked, "git ls-files 未列出技能目录下任何已跟踪文件，判定接缝已失效"

    offenders = []
    for rel in tracked:
        parts = rel.split("/")
        name = parts[-1]
        if name == ".env.local":
            offenders.append(rel)
        elif ".bak-" in name:
            offenders.append(rel)
        elif ".snapshots" in parts:
            offenders.append(rel)
        elif "__pycache__" in parts:
            offenders.append(rel)
    assert offenders == [], "技能目录已跟踪文件里存在不允许的残留: %r" % (
        sorted(offenders),
    )


# ---------------------------------------------------------------- 行为 12

def test_行为12_技能目录所有py可编译(tmp_path):
    py_files = sorted(SKILL_DIR.rglob("*.py"))
    assert py_files, "技能目录下没有 .py 文件"

    failures = []
    for path in py_files:
        proc = subprocess.run(
            [sys.executable, "-m", "py_compile", str(path)],
            cwd=str(REPO_ROOT),
            env=_child_env(tmp_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            failures.append(
                "%s -> 退出码 %d: %s"
                % (
                    path.relative_to(REPO_ROOT),
                    proc.returncode,
                    proc.stderr.strip()[:500],
                )
            )
    assert failures == [], "py_compile 失败: %r" % (failures,)


# ---------------------------------------------------------------- 错误契约

def test_错误契约_ratio非法值_argparse退出2(tmp_path):
    """`--ratio` 给四个预设以外的值：argparse 以 2 退出。

    非法值放在 `--help` 之前：若实现用 argparse choices，解析到 `--ratio` 就立即
    以 2 退出（不会走到 --help）；若没在 argparse 层校验，--help 会以 0 退出。
    """
    problems = []
    for value in ("diagonal", "16:9", "", "PORTRAIT"):
        proc = _run_script(IMAGE_SCRIPT, ["--ratio", value, "--help"], tmp_path)
        if proc.returncode != 2:
            problems.append(
                "--ratio %r: 应退出 2，实际 %d，stderr: %r"
                % (value, proc.returncode, proc.stderr[-500:])
            )
        if "--ratio" not in proc.stderr:
            problems.append(
                "--ratio %r: 错误信息未提及 --ratio: %r"
                % (value, proc.stderr[-500:])
            )
    assert problems == [], "\n".join(problems)
