#!/usr/bin/env python3
"""每周日 04:00 跑：维护每个 bot 的 memory.md。

流程：
1. 对每个 enabled bot，读 memory.md + 最近 7 天对话
2. 调用 LLM 提炼新增/变更（新系统的 bot 直连 DeepSeek，旧系统走 claude --print）
3. 输出新版 memory.md（≤2000 字，老化"临时记忆"）
4. USER-MANUAL-LOCK 块原样保留

新系统（dsh-bot 网关）的 bot：对话从送达账本读（含 bot 真正发出去的话），
MEMORY.md 在 <频道目录>/memory/ 下。网关里的 remember 工具也会往这个文件追加，
所以写回前会重读一次：整理期间新记下的条目不会被覆盖掉。写入是原子的（先写临时文件再改名）。
要整理新系统的 bot，把 HUB_CONFIGS_DIR 指到新系统的配置目录（默认 ~/.dsh-bot/configs）。
"""
import os
import sys
import re
import shutil
import time
import traceback
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config_loader
import quota
import chat_history
from claude_cli import call_claude
from memory.memory_inject import memory_path, ensure_memory_exists


COMPACTOR_PROMPT = """你帮一个 AI 角色（人格已定型）维护它的 MEMORY.md 文件。
这是它的长期记忆，bot 自己也在对话中往里记东西。
你的工作是每周一次"压缩+整理"，不是重写人设。

【现有 MEMORY.md】
```
{current_memory}
```

【最近 7 天的对话片段】
```
{recent_dialog}
```

【更新规则（严格遵守）】
1. **保留现有内容的整体结构和风格**，不要强加新的章节标题或模板
2. **不要修改人设核心**（身份、外貌、关系定义等）——这些是 bot 的本体
3. 只做以下三件事：
   a) 添加最近 7 天对话中**新出现的、长期相关的**事实（喜好/约定/经历）
   b) 删除：用户明确说"忘掉"的；7 天未被引用的临时事实
   c) 合并重复条目
4. 总字数 ≤ 2500 字（超过时压缩"临时"部分，保留人设核心）
5. 不记敏感信息（地址、账号、密码、身份证、电话）
6. 如果包含 `<!-- USER-MANUAL-LOCK ... -->` 块，**原样保留**
7. 如果现有内容是 SOUL 风格（# 我是谁 / # 我的样子 / # 日常生活 等），保持这个结构
8. **如果不确定要不要改某段，就不改**
9. 如果有「## 随手记」一节（bot 平时随手记下的条目）：长期有用的并进合适的段落，过时的删掉，并完的条目从随手记里去掉

输出完整的新版 MEMORY.md（保留原有的所有段落标题和大部分内容），不要解释，不要 markdown 包装。"""


# 提取 USER-MANUAL-LOCK 块
LOCK_RE = re.compile(r"<!--\s*USER-MANUAL-LOCK.*?-->", re.DOTALL)


def _call_llm(prompt: str, dsh_bot: bool) -> str:
    """新系统的 bot 直连 DeepSeek（新系统里没有 claude 命令行），失败退避重试 2 次；旧系统照旧。"""
    if not dsh_bot:
        return call_claude(prompt, timeout=180)
    import deepseek_client
    last_err = None
    for wait in (0, 5, 20):
        if wait:
            time.sleep(wait)
        try:
            return deepseek_client.call_text(prompt, timeout=180, max_tokens=6000)
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"DeepSeek 调用失败：{type(last_err).__name__}")


def merge_concurrent_additions(original: str, latest: str, new_memory: str) -> str:
    """original：整理开始时读到的；latest：写回前重读的；new_memory：LLM 整理出的新版。
    latest 里有、original 里没有的行（整理期间 bot 新记下的），追加到新版的「## 随手记」里。"""
    if latest == original:
        return new_memory
    before = set(original.splitlines())
    added = [l for l in latest.splitlines() if l.strip() and l not in before and l not in new_memory]
    if not added:
        return new_memory
    out = new_memory.rstrip()
    if "## 随手记" not in out:
        out += "\n\n## 随手记"
    return out + "\n" + "\n".join(added) + "\n"


def write_atomic(path: str, text: str) -> None:
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def compact_one(bot_cfg: dict) -> bool:
    """返回是否成功更新。"""
    bot_id = bot_cfg["_bot_id"]
    bot_dir = bot_cfg["bot_channel_path"]

    # 确保 memory.md 存在
    ensure_memory_exists(bot_dir, bot_cfg.get("display_name", bot_id))

    p = memory_path(bot_dir)
    try:
        current = open(p, encoding="utf-8").read()
    except Exception as e:
        sys.stderr.write(f"[{bot_id}] 读 memory.md 失败：{e}\n")
        return False

    # 提取 lock 块（待会儿强制还原）
    lock_match = LOCK_RE.search(current)
    lock_content = lock_match.group(0) if lock_match else ""

    # 取最近 7 天对话（Telegram/微信 session）+ 电话侧 voice_log，一起喂给 LLM，
    # 电话里说的长期事实也能被提炼进 MEMORY.md（P5：语音记忆进长期）。
    recent = chat_history.get_recent_dialog(bot_dir, days=7, max_chars=10000)
    voice = chat_history.get_recent_voice_log(bot_dir, days=7, max_chars=4000)
    if voice.strip():
        voice_block = "【最近电话通话】\n" + voice
        recent = (recent.strip() + "\n\n" + voice_block) if recent.strip() else voice_block
    if not recent.strip():
        sys.stderr.write(f"[{bot_id}] 最近 7 天无新对话，跳过\n")
        return False

    prompt = COMPACTOR_PROMPT.format(current_memory=current, recent_dialog=recent)

    try:
        new_memory = _call_llm(prompt, dsh_bot=chat_history.is_dsh_bot(bot_dir))
        quota.record_call("memory_compact", quota.MEMORY_COMPACT_WEIGHT)
        # 剥掉 LLM 可能加的 ```markdown … ``` 包裹：否则 Claude Code auto-memory
        # 解析器会把整个 MEMORY.md 当成一个代码块 → 记忆条目全部失效（bot3 已中招）。
        _nm = new_memory.strip()
        if _nm.startswith("```"):
            _lines = _nm.split("\n")[1:]
            if _lines and _lines[-1].strip().startswith("```"):
                _lines = _lines[:-1]
            new_memory = "\n".join(_lines).strip()
    except Exception as e:
        sys.stderr.write(f"[{bot_id}] LLM 调用失败：{e}\n")
        return False

    # 校验：放宽——保留 bot 自己的格式，不强求章节
    # 仅检查长度合理 + 没被改成空文件
    if len(new_memory) < len(current) * 0.3:
        sys.stderr.write(f"[{bot_id}] 校验失败：输出 {len(new_memory)} 字过短（< 原 30%），可能内容丢失，跳过\n")
        return False
    if len(new_memory) > 5000:
        sys.stderr.write(f"[{bot_id}] 校验失败：输出 {len(new_memory)} 字超 5000\n")
        return False

    # 强制保留 lock 块（如果原来有但 LLM 删了）
    if lock_content and "USER-MANUAL-LOCK" not in new_memory:
        new_memory = new_memory.rstrip() + "\n\n" + lock_content + "\n"

    # 整理期间 bot 又记了新条目（网关的 remember 工具）：并进来，不覆盖
    try:
        latest = open(p, encoding="utf-8").read()
    except Exception:
        latest = current
    new_memory = merge_concurrent_additions(current, latest, new_memory)

    # 备份 + 原子写入
    backup_path = p + ".bak"
    try:
        shutil.copy(p, backup_path)
        write_atomic(p, new_memory)
        sys.stderr.write(f"[{bot_id}] memory.md 已更新（备份至 .bak）\n")
        return True
    except Exception as e:
        sys.stderr.write(f"[{bot_id}] 写入失败：{e}\n")
        return False


def main():
    bots = config_loader.list_enabled_bots()
    if not bots:
        sys.stderr.write("没有 enabled 的 bot，跳过\n")
        return

    sys.stderr.write(f"开始为 {len(bots)} 个 bot 压缩记忆...\n")
    success = 0
    for cfg in bots:
        if compact_one(cfg):
            success += 1
        time.sleep(3)
    sys.stderr.write(f"完成：{success}/{len(bots)} 成功\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.stderr.write(f"compactor 异常：{e}\n{traceback.format_exc()}")
        sys.exit(1)
