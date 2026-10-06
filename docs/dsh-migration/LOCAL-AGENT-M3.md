# 本机 AI 任务书：M3 真机验收（bot5：承诺、主动消息、心情与好感）

你在用户的 Mac 上工作，接着 M2 的活。新代码在仓库 `wsxwj123/dsh-bot` 的分支 `claude/nice-brahmagupta-dtjsma` 上（M1、M2 已经合进 master）。

## 先读

- `docs/dsh-migration/M3.md`：这一期做了什么，重点看第三、四节
- `docs/dsh-migration/LOCAL-AGENT-M2.md`：铁律和收尾方式，这次照样适用

## 铁律

违反任何一条，就停下来问用户。

1. **不读、不打印、不复制任何密钥和令牌。** 检查有没有准备好，只用 `setup.ts check`。
2. **不读聊天内容，也不读私人设定。**
   - 不读：`logs/chat.log`、账本正文、长期记忆正文、`relationship.json` 的内容、人设、作息表里的活动名。
   - 需要判断时，用只出数字或"有/没有"的命令（`grep -c`、`wc -l`、`ls -l`、`stat`）。
   - 下面几处命令的输出里会带活动名或数值，照任务书说的只记类别和数字。
3. **不改旧系统的任何文件。**
   - 停旧 bot5、恢复旧 bot5，都要先告诉用户命令，用户同意后再执行。
   - 恢复用的 `start-bot.sh` 会把所有旧 bot 都重启一遍，先告诉用户。
4. **不改 `gateway/` 下的代码。** 发现问题就记录：怎么复现、看到了什么、日志里的事件名。
5. **提交的报告里不能有：**
   - 密钥
   - 聊天内容、记忆内容、人设和作息内容（包括活动名）
   - 关系数值
   - Telegram 用户 id 和聊天 id（写 `<chat>`）
   - bot 真名（写 bot5）
   - 带系统用户名的路径（写成 `~`）

## 步骤

### 1. 拉代码、跑自动化测试（不需要密钥）

```bash
git fetch origin claude/nice-brahmagupta-dtjsma && git checkout claude/nice-brahmagupta-dtjsma && git pull
cd gateway && bun install && bun run typecheck && bun run test
DSH_BOT_HARNESS=~/.dsh-bot/harness bun test --timeout 90000 test/e2e/real-dsh.test.ts
cd ../tests && python3 -m pytest -q unit/test_chat_history_ledger.py unit/test_chat_history_namespace.py unit/test_project_slug.py unit/test_m3_life.py
```

预期：
- 网关 198 项（通过 193，跳过 5）；
- 真 dsh 5 项通过；
- Python 27 项通过。

### 2. 停旧 bot5（先问用户）

同 M2。

### 3. 让新配置读到旧的作息设置

1. 找到 bot5 的旧配置文件，在旧仓库的 `configs/<bot5 旧名>.yml`。**只确认路径，不打开看内容。**
2. 在 `~/.dsh-bot/configs/bot5.yml` 的 `dispatcher_port` 那行下面加一行：`life_config: <旧配置文件的完整路径>`。
3. 运行 `bun gateway/scripts/setup.ts check ~/.dsh-bot/configs/bot5.yml`，预期"全部就绪"。
4. 检查作息能不能查到（只打印键名和起床次数）：

```bash
HUB_CONFIGS_DIR=~/.dsh-bot/configs python3 hang_situation.py bot5 --plan | python3 -c "import json,sys; o=json.load(sys.stdin); print(sorted(o), len(o.get('wakes', [])))"
```

预期：键名里有 `name`、`state`、`wakes`、`free_at`，起床次数是 2 或 3。

### 4. 启动新网关

```bash
DSH_BOT_LOG_CONSOLE=1 bun gateway/src/main.ts --config ~/.dsh-bot/configs/bot5.yml
```

第一条消息时会出现 `segment.prompt_changed` 和一次换段（运行规则加了一句），这是正常的，记下来就行。

### 5. 真机清单

每一项记"符合"或"不符合"，加一句现象（不写聊天内容）。

| 项 | 怎么做 | 预期 |
|---|---|---|
| a. 生活状态 | 用户发第一条消息，再发第二条 | 第一条之后 `gateway.log` 有 `life.lines`，`relationship`、`situation` 是 true（前提是 bot5 有 `relationship.json`、作息能查到）。第二条之后没有 `life.lines`，因为没变化 |
| b. 承诺 | 请用户对 bot 说"5 分钟后提醒我……"（内容用户自己定） | 出现 `commitment.created`，`source` 是 `tool`（模型自己登记）或 `auto`（网关补登记）；约 5 分钟后出现 `commitment.fire`，用户收到提醒；接着出现 `commitment.done`。记下 `source` 和从登记到提醒隔了几分钟 |
| c. 承诺列表 | 请用户问 bot"你答应过我什么" | 用户判断回答对不对，你只记"对 / 不对" |
| d. 主动消息 | 见下面的命令 | 输出"已投递给网关；下次机会在 N 分钟后"，或"skip: 原因；下次机会在 N 分钟后"。投递了的话，用户应该在一两分钟内收到 bot 主动发来的消息 |
| e. 积温情绪 | 见下面的命令 | `relationship.json` 的修改时间变新（前提是第 a、b 项之后有新对话）。不看内容 |
| f. 被晾追问（可选） | bot 回完话后，请用户 10 分钟内不回 | 日志里出现 `hang` 事件，`detail` 以 `hang_followup`（追问了一句）或 `hang_archive`（她在忙，只记档案）开头。如果是追问，用户会收到一句 |
| g. 睡觉顺延（可选，要过夜） | 睡前请用户让 bot "半夜两点提醒我……"，网关整夜开着 | 夜里出现 `commitment.deferred`（`reason` 是 `sleeping`），起床后才 `commitment.fire`，用户收到的提醒里说明晚了多久 |
| h. 报告 | `bun gateway/scripts/report.ts --config ~/.dsh-bot/configs/bot5.yml --hours 3` | 有"## 承诺"一节，能看到第 b 项那条 |

**第 d 项命令**：聊天 id 从 access.json 里读进变量，不打印出来。

```bash
CHAT=$(python3 -c "import json,os; print(json.load(open(os.path.expanduser('~/.dsh-bot/bots/bot5/channel/access.json')))['allowFrom'][0])")
HUB_CONFIGS_DIR=~/.dsh-bot/configs python3 scripts/self_initiate.py bot5 "$CHAT" --force
```

- 输出的跳过原因里如果带活动名（比如"专注时段(…)无突发"），只记类别"专注时段"。
- 不加 `--force` 再跑一次，预期是"skip: 还没到下次机会（还有 N 分钟）"。

**第 e 项命令**：

```bash
stat -f %Sm ~/.dsh-bot/bots/bot5/channel/relationship.json
HUB_CONFIGS_DIR=~/.dsh-bot/configs python3 jiwen/tick.py > /dev/null 2>&1; echo "exit=$?"
stat -f %Sm ~/.dsh-bot/bots/bot5/channel/relationship.json
```

tick 的输出里有关系数值，所以丢掉不看，只记退出码和两次修改时间。

### 6. 收尾

1. 网关还在跑的时候，运行 `setup.ts check`、`health.ts`、`report.ts --hours 3`，输出原样放进报告。
2. 按 Ctrl+C 停掉网关。
3. 问用户要不要恢复旧 bot5，并说明会重启所有旧 bot。按用户的意思执行。
4. `life_config` 那一行保留（以后切换也要用）。

## 交付

1. 写 `docs/dsh-migration/local/reports/m3-check.md`，包括：
   - 环境；
   - 第 1 步的测试结果；
   - 第 3 步的检查结果；
   - 第 5 步每一项的结果；
   - 第 6 步三条命令的输出；
   - 发现的问题：复现步骤、现象、事件名。
2. 提交前，在 `git diff` 里搜一遍：`sk-`、令牌格式、用户的 Telegram 用户 id、系统用户名。确认都没有。
3. 从 `claude/nice-brahmagupta-dtjsma` 拉出新分支 `local/m3-check`，推到这个分支。
4. 向用户汇报时用平实的中文，先说结论。最后一句写："M3 实测已推到 local/m3-check"。
