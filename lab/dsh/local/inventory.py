#!/usr/bin/env python3
"""现有部署的脱敏盘点：只输出结构（文件名、字段名、数量、版本号），不输出任何内容。

在作者本机运行，结果交给新系统的开发者用来写迁移工具、估预算。只读，不改任何文件。

不会输出：密钥、令牌、user_id / chat_id、人设正文、对话、记忆正文、关系数值、配置里的任何值。
bot 名默认替换成 bot1、bot2……（加 --keep-names 才保留原名）。

用法：
    python3 lab/dsh/local/inventory.py [--claude-home ~/.claude] [--repo <旧仓库目录>] [--out 文件] [--keep-names]
只依赖 Python 标准库；装了 PyYAML 会多列出 yml 的二级字段名。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

SKIP_CHANNEL_DIRS = {"media", "group_transcripts", "_persona_template"}
VAR_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


def sh(cmd: list[str], timeout: int = 15) -> str:
    """跑一个只读命令，取第一行输出；失败返回说明文字。"""
    exe = shutil.which(cmd[0])
    if not exe:
        return "未安装"
    try:
        r = subprocess.run([exe, *cmd[1:]], capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or r.stderr).strip().splitlines()
        return out[0][:200] if out else f"(退出码 {r.returncode})"
    except Exception as e:  # noqa: BLE001
        return f"出错：{type(e).__name__}"


def slug_for(path: Path) -> str:
    """与旧仓库 chat_history.py 相同的规则：绝对路径里非字母数字一律换成 '-'。"""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path.resolve()))


def tilde(p) -> str:
    """把用户主目录换成 ~，避免盘点结果里出现系统用户名。"""
    home = str(Path.home())
    t = str(p)
    return "~" + t[len(home):] if t.startswith(home) else t


def load_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def yaml_keys(p: Path) -> str:
    text = p.read_text(encoding="utf-8", errors="replace")
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text)
        if isinstance(data, dict):
            parts = []
            for k, v in data.items():
                if isinstance(v, dict):
                    parts.append(f"{k}{{{', '.join(map(str, v.keys()))}}}")
                else:
                    parts.append(str(k))
            return "，".join(parts)
    except Exception:  # noqa: BLE001
        pass
    keys = re.findall(r"^([A-Za-z_][\w-]*)\s*:", text, flags=re.M)
    return "，".join(dict.fromkeys(keys))


def dsh_subpackage_versions() -> str:
    root = sh(["npm", "root", "-g"])
    if root in ("未安装",) or root.startswith("出错"):
        return "无法定位 npm 全局目录"
    base = Path(root) / "@deepseek-ai" / "dsh" / "node_modules" / "@deepseek-ai"
    if not base.is_dir():
        return "全局 npm 目录下没有 @deepseek-ai/dsh"
    counts: dict[str, int] = {}
    for pkg in base.glob("dsh-*/package.json"):
        v = (load_json(pkg) or {}).get("version", "?")
        counts[v] = counts.get(v, 0) + 1
    return "，".join(f"{v} × {n}" for v, n in sorted(counts.items())) or "没有子包"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--claude-home", default=str(Path.home() / ".claude"))
    ap.add_argument("--dsh-home", default=os.environ.get("DSH_HOME", str(Path.home() / ".dsh")))
    ap.add_argument("--repo", default=None, help="旧仓库（claude-tgbot / claudebotlife）所在目录，用来读 configs/")
    ap.add_argument("--out", default=None)
    ap.add_argument("--keep-names", action="store_true")
    a = ap.parse_args()

    lines: list[str] = []
    w = lines.append
    claude_home = Path(a.claude_home).expanduser()
    channels = claude_home / "channels"
    projects = claude_home / "projects"

    w("# 现有部署脱敏盘点")
    w("")
    w("## 系统与版本")
    w("")
    w("| 项 | 值 |")
    w("|---|---|")
    w(f"| 系统 | {platform.system()} {platform.release()} {platform.machine()} |")
    w(f"| Python | {sys.version.split()[0]} |")
    for name, cmd in [("bun", ["bun", "--version"]), ("node", ["node", "--version"]), ("npm", ["npm", "--version"]),
                      ("全局 dsh", ["dsh", "--version"]), ("claude", ["claude", "--version"]), ("ffmpeg", ["ffmpeg", "-version"]),
                      ("tmux", ["tmux", "-V"])]:
        w(f"| {name} | {sh(cmd)} |")
    w(f"| 全局 dsh 子包版本分布 | {dsh_subpackage_versions()} |")
    dsh_home = Path(a.dsh_home).expanduser()
    if dsh_home.is_dir():
        profiles = sorted(p.name for p in (dsh_home / "profiles").glob("*") if p.is_dir()) if (dsh_home / "profiles").is_dir() else []
        sessions = sum(1 for _ in (dsh_home / "sessions").glob("*/*")) if (dsh_home / "sessions").is_dir() else 0
        w(f"| {tilde(dsh_home)}（日常用的 DSH_HOME） | profiles：{', '.join(profiles) or '无'}；会话目录数：{sessions}；settings.yaml：{'有' if any(dsh_home.glob('settings.y*ml')) else '无'}；.credentials.yaml：{'有' if (dsh_home / '.credentials.yaml').exists() else '无'} |")
    else:
        w(f"| {tilde(dsh_home)} | 不存在 |")
    w("")

    bot_dirs = sorted(p for p in channels.glob("*") if p.is_dir() and p.name not in SKIP_CHANNEL_DIRS and (p / "CLAUDE.md").exists()) if channels.is_dir() else []
    alias = {p.name: (p.name if a.keep_names else f"bot{i + 1}") for i, p in enumerate(bot_dirs)}
    w(f"## bot 目录（{tilde(channels)}）")
    w("")
    w(f"共 {len(bot_dirs)} 个带 CLAUDE.md 的 bot 目录。" + ("" if a.keep_names else "名字已替换为 bot1、bot2……"))
    w("")
    for d in bot_dirs:
        name = alias[d.name]
        w(f"### {name}")
        w("")
        top = sorted(p.name + ("/" if p.is_dir() else "") for p in d.iterdir())
        w(f"- 顶层文件：{', '.join(top)}")
        persona = (d / "CLAUDE.md").read_text(encoding="utf-8", errors="replace")
        braces = re.findall(r"\{\{([^{}]*)\}\}", persona)
        valid = sum(1 for b in braces if VAR_NAME.match(b))
        headings = len(re.findall(r"^#{1,3} ", persona, flags=re.M))
        w(f"- CLAUDE.md：{len(persona)} 字符，{persona.count(chr(10)) + 1} 行，标题 {headings} 个；成对 {{{{…}}}} 占位 {len(braces)} 处（其中合法变量名格式 {valid} 处）；全部 {{{{ 出现 {persona.count('{{')} 次")
        acc = load_json(d / "access.json")
        if isinstance(acc, dict):
            groups = acc.get("groups") or {}
            gdesc = []
            for g in groups.values() if isinstance(groups, dict) else []:
                if isinstance(g, dict):
                    gdesc.append(f"requireMention={g.get('requireMention')}, allowFrom {len(g.get('allowFrom') or [])} 个, selfAliases {len(g.get('selfAliases') or [])}, otherBotUsernames {len(g.get('otherBotUsernames') or [])}")
            w(f"- access.json：字段 {', '.join(sorted(acc.keys()))}；dmPolicy={acc.get('dmPolicy')}；allowFrom {len(acc.get('allowFrom') or [])} 个；群 {len(groups) if isinstance(groups, dict) else '?'} 个" + (f"（{'；'.join(gdesc)}）" if gdesc else ""))
        else:
            w("- access.json：无或读不了")
        rel = load_json(d / "relationship.json")
        w(f"- relationship.json：{'字段 ' + ', '.join(sorted(rel.keys())) if isinstance(rel, dict) else '无'}")
        mem = d / "memory"
        if mem.is_dir():
            w(f"- memory/：{', '.join(f'{p.name}({p.stat().st_size}B)' for p in sorted(mem.iterdir()) if p.is_file())}")
        settings = load_json(d / ".claude" / "settings.json")
        if isinstance(settings, dict):
            hooks = settings.get("hooks") or {}
            hdesc = [f"{ev}[{', '.join(str(h.get('matcher', '')) for h in (hs or []) if isinstance(h, dict))}]" for ev, hs in hooks.items()] if isinstance(hooks, dict) else []
            w(f"- .claude/settings.json：字段 {', '.join(sorted(settings.keys()))}；钩子 {', '.join(hdesc) or '无'}")
        inbox = d / "inbox"
        if inbox.is_dir():
            w(f"- inbox/ 现有文件数：{sum(1 for _ in inbox.glob('*.json'))}")
        chats = d / "chats"
        if chats.is_dir():
            w(f"- chats/ 子目录数：{sum(1 for p in chats.iterdir() if p.is_dir())}；voice_log.jsonl 个数：{sum(1 for _ in chats.glob('*/voice_log.jsonl'))}")
        logs = d / "logs"
        if logs.is_dir():
            size = sum(p.stat().st_size for p in logs.rglob("*") if p.is_file())
            w(f"- logs/：{sum(1 for p in logs.rglob('*') if p.is_file())} 个文件，共 {size // 1024} KB")
        proj = projects / slug_for(d)
        if proj.is_dir():
            js = list(proj.glob("*.jsonl"))
            w(f"- 对应的 Claude Code 会话目录：存在；会话文件 {len(js)} 个，共 {sum(p.stat().st_size for p in js) // 1024} KB；memory/ 文件：{', '.join(f'{p.name}({p.stat().st_size}B)' for p in sorted((proj / 'memory').glob('*')) if p.is_file()) if (proj / 'memory').is_dir() else '无'}")
        else:
            w("- 对应的 Claude Code 会话目录：按旧规则没找到（私有部署的路径规则可能不同）")
        w("")

    other = sorted(p.name for p in channels.glob("*") if p.is_dir() and p not in bot_dirs) if channels.is_dir() else []
    if other:
        w(f"channels/ 下其它目录：{', '.join(other)}")
        w("")
    if projects.is_dir():
        chan_projects = [p for p in projects.iterdir() if p.is_dir() and "channels" in p.name]
        w(f"## Claude Code 会话目录（{tilde(projects)}）")
        w("")
        w(f"名字里含 channels 的项目目录 {len(chan_projects)} 个；全部项目目录 {sum(1 for p in projects.iterdir() if p.is_dir())} 个。")
        w("")

    la = Path.home() / "Library" / "LaunchAgents"
    if la.is_dir():
        labels = sorted(p.stem for p in la.glob("*.plist") if re.search(r"claudebotlife|claude-tgbot|tgbot|cliproxy|voice|moments", p.stem, re.I))
        w("## launchd 任务（名字里像本项目的）")
        w("")
        w(", ".join(labels) or "无")
        w("")

    if a.repo:
        repo = Path(a.repo).expanduser()
        cfg = repo / "configs"
        w(f"## 旧仓库配置（{tilde(repo)}）")
        w("")
        if cfg.is_dir():
            for y in sorted(cfg.glob("*.yml")):
                shown = y.name
                stem = y.stem
                if not a.keep_names and stem in alias:
                    shown = f"{alias[stem]}.yml"
                w(f"- {shown}：{yaml_keys(y)}")
        else:
            w("- 没有 configs/ 目录")
        head = sh(["git", "-C", str(repo), "log", "-1", "--format=%h %cs"])
        w(f"- 旧仓库当前提交：{head}")
        w("")

    text = "\n".join(lines) + "\n"
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"已写入 {a.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
