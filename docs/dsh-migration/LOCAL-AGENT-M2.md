# 本机 AI 任务书：M2 真机验收（bot5，记忆和换段）

你在用户的 Mac 上工作，接着之前 M1 实测的活。这次验收新系统的记忆部分。新代码在 GitHub 仓库 `wsxwj123/dsh-bot` 的分支 `claude/nice-brahmagupta-dtjsma` 上（M1 已经合进 master）。

## 先读这两份

- `docs/dsh-migration/M2.md`：这一期做了什么，重点看第四节
- `docs/dsh-migration/LOCAL-AGENT-M1.md`：上次的铁律，这次照样适用

## 铁律

违反任何一条，就停下来问用户。

1. **不读、不打印、不复制任何密钥和令牌。** 检查有没有准备好，只用 `setup.ts check`。
2. **不读聊天内容。**
   - 包括 `logs/chat.log`、账本（`ledger.sqlite`）里的正文。
   - 也**不读长期记忆的正文**：`<频道目录>/memory/` 下的文件同样是私人内容。
   - 需要判断时，用 `grep -c`、`wc -l`、`ls -l` 这类只出数字的命令。
3. **不改旧系统的任何文件。**
   - 停旧 bot5、恢复旧 bot5，都要先告诉用户命令，用户同意后再执行。
   - 上次的经验：恢复旧 bot5 用的 `start-bot.sh` 会把所有旧 bot 都重启一遍。先把这一点告诉用户。
4. **不改 `gateway/` 下的代码。** 发现问题就记录：怎么复现、看到了什么、日志里的事件名。
5. **提交的报告里不能有：**
   - 密钥
   - 聊天内容
   - 记忆内容
   - Telegram 用户 id 和聊天 id
   - 人设内容
   - bot 真名（一律写 bot5）
   - 带系统用户名的路径（写成 `~`）

## 步骤

### 1. 拉代码、跑自动化测试（不需要密钥）

```bash
git fetch origin claude/nice-brahmagupta-dtjsma && git checkout claude/nice-brahmagupta-dtjsma && git pull
cd gateway && bun install && bun run typecheck && bun run test
DSH_BOT_HARNESS=~/.dsh-bot/harness bun test --timeout 90000 test/e2e/real-dsh.test.ts
cd ../tests && python3 -m pytest -q unit/test_chat_history_ledger.py unit/test_chat_history_namespace.py unit/test_project_slug.py
```

记录每一项是通过、跳过还是失败。如果失败，把报错的路径里的用户名换成 `~` 再记。

### 2. 停旧 bot5（必须先问用户）

和上次一样，用 `stop-bot.sh <bot5 旧名>`。停完后确认旧进程已经没了。

### 3. 导入旧系统的长期记忆

```bash
bun gateway/scripts/setup.ts memory bot5 --from <bot5 的旧目录>
```

- 预期结果是"已导入 Claude Code 的长期记忆：N 个文件，共 X 字节"。
- 新目录里原来那份 `MEMORY.md` 是上次从频道目录复制的种子，会被改名备份成 `MEMORY.md.before-import-<日期>`。
- 只记录文件数和字节数，不看内容。

### 4. 临时把预算调小，方便看到换段

告诉用户你要做什么，然后在 `~/.dsh-bot/configs/bot5.yml` 的 `brain:` 下面加一行：

```yaml
  max_input_tokens: 30000
```

再跑一次 `bun gateway/scripts/setup.ts check ~/.dsh-bot/configs/bot5.yml`，预期是"全部就绪"。

### 5. 启动新网关，陪用户做真机清单

```bash
DSH_BOT_LOG_CONSOLE=1 bun gateway/src/main.ts --config ~/.dsh-bot/configs/bot5.yml
```

每一项做完，记"符合"或"不符合"，加一句现象（不写聊天内容）。

| 项 | 怎么做 | 预期 |
|---|---|---|
| a. 埋几个事实 | 请用户一开始就告诉 bot 两三件人设编不出来的具体事，比如"我的猫叫 XX""我下周三去 XX 出差"。用户自己记住说了什么，不要告诉你 | 正常回复 |
| b. 聊到换段 | 用户正常聊。你只盯 `gateway.log` 里的 `segment.roll_start` 和 `segment.rolled` 两个事件，以及它们的 `reason`、`summary` 字段 | 二三十轮内出现 `segment.rolled`，`summary` 是 `in-session`，`summary_chars` 大于 0。换段那一轮用户不会收到任何多余的消息 |
| c. 换段后不失忆 | 换段后，请用户问 bot 第 a 项里的那几件事 | 能答对。用户判断，你只记"答对几件 / 共几件" |
| d. 新会话第一句 | 看换段后第一轮的 `turn.end` | `kind` 是 `message`，不是 `nudge`，也就是不用提醒就回了。如果是 `nudge`，记下来 |
| e. 记事 | 请用户对 bot 说"帮我记住：……"（内容用户自己定）。然后运行 `grep -c '' <频道目录>/memory/MEMORY.md`，看行数有没有增加 | `gateway.log` 出现 `tool.remember`，`added` 为 true。`MEMORY.md` 的行数比之前多 1 |
| f. `/clear` | 用户发 `/clear`，再问一件清空前聊过的事 | 收到"已清空……（聊过的要点留了一份摘要）"。要点大致记得，细节可能记不全，记下用户的感受 |
| g. 用量 | `bun gateway/scripts/report.ts --config ~/.dsh-bot/configs/bot5.yml` | "会话（段）"一节里有 `closed/budget`，并且有"换段 N 次：摘要在旧会话里写成 N 次……" |

### 6. 记忆整理任务（可选，会调一次 DeepSeek，先问用户）

```bash
HUB_CONFIGS_DIR=~/.dsh-bot/configs python3 memory/memory_compactor.py
```

- 预期输出是"[bot5] memory.md 已更新（备份至 .bak）"和"完成：1/1 成功"。
- 只记录这两行输出，以及整理前后 `MEMORY.md` 的行数。不看内容。

### 7. 收尾

1. 先把第 4 步加的 `max_input_tokens` 那一行删掉，或者改成用户想要的数。
2. 网关还在跑的时候，运行 `setup.ts check`、`health.ts`、`report.ts --hours 24`，把输出原样放进报告。
3. 按 Ctrl+C 停掉网关。
4. 问用户要不要恢复旧 bot5，并说明会重启所有旧 bot。按用户的意思执行。

## 交付

1. 写 `docs/dsh-migration/local/reports/m2-check.md`，包括：
   - 环境；
   - 第 1 步的测试结果；
   - 第 3 步的导入结果（只写数字）；
   - 第 5 步每一项的结果；
   - 第 6 步的结果（如果做了）；
   - 第 7 步三条命令的输出；
   - 发现的问题：复现步骤、现象、事件名，聊天 id 换成 `<chat>`。
2. 提交前，在 `git diff` 里搜一遍下面这些，确认都没有：
   - `sk-`
   - 令牌格式
   - 用户的 Telegram 用户 id
   - 系统用户名
3. 从 `claude/nice-brahmagupta-dtjsma` 拉出新分支 `local/m2-check`，推到这个分支。
4. 向用户汇报时用平实的中文，先说结论。最后一句写："M2 实测已推到 local/m2-check"。

## 复测：真机报告里的问题修好之后（约 15 分钟）

铁律同上。这次只复测两件事，报告写到 `docs/dsh-migration/local/reports/m2-recheck.md`，推到新分支 `local/m2-recheck`。

### 1. 拉代码、跑测试

和第 1 步一样，再跑一遍三条测试命令。预期：网关 85 项（通过 80，跳过 5），真 dsh 5 项通过，Python 22 项通过。

### 2. 停旧 bot5（先问用户），启动新网关

```bash
DSH_BOT_LOG_CONSOLE=1 bun gateway/src/main.ts --config ~/.dsh-bot/configs/bot5.yml
```

### 3. 系统提示词变了以后自动换段（问题 2）

新代码会给每个会话记下开会话时系统提示词的指纹。bot5 现有的会话是旧代码开的，没有指纹，所以应该在下一条消息时自动换一次段。

- 请用户随便发一条消息。
- 预期 `gateway.log` 里依次出现：
  - `segment.prompt_changed`
  - `segment.roll_start` 和 `segment.rolled`，`reason` 都是 `prompt-changed`，`summary` 是 `in-session`
  - `segment.created`、`segment.seed`
- 用户正常收到回复，没有多余的消息。
- 记下 `segment.seed` 的 `chars`、`recent_cap`，以及 `report.ts --hours 1` 里"每轮上下文大小"的最大值，作为参考。
- 再发一条消息：这次不应该再出现 `segment.prompt_changed`。
- 按 Ctrl+C 停网关再启动，再发一条：也不应该出现 `segment.prompt_changed`（没改人设，接着用会话）。

### 4. 记忆整理（问题 1，会调一次 DeepSeek，先问用户）

```bash
HUB_CONFIGS_DIR=~/.dsh-bot/configs python3 memory/memory_compactor.py
```

- 预期输出"[bot5] memory.md 已更新（备份至 .bak）"和"完成：1/1 成功"。
- 记下整理前后 `MEMORY.md` 的行数，以及有没有生成 `MEMORY.md.bak`。不看内容。
- 如果输出的是"校验失败：……"，原样记下这一行（这是整理结果没通过长度检查，不是程序出错）。

### 5. 收尾

和第 7 步一样：跑 `check`、`health`、`report.ts --hours 2` 放进报告，Ctrl+C 停网关，问用户要不要恢复旧 bot5。最后一句写："M2 复测已推到 local/m2-recheck"。
