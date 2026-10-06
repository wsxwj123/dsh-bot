# M2 真机验收（bot5，记忆和换段）

- 日期：2026-10-06（日志时间为 UTC 02:17–02:42）
- 代码：分支 `claude/nice-brahmagupta-dtjsma`，提交 `187a0b1`
- 执行方式：本机 AI 按 `docs/dsh-migration/LOCAL-AGENT-M2.md` 操作，用户在 Telegram 上配合
- 本文不含密钥、聊天内容、记忆内容、Telegram 用户 id 和聊天 id、人设内容、bot 真名。旧 bot5 的名字写成 `<bot5 旧名>`

## 结论

| 项 | 结论 |
|---|---|
| 1. 自动化测试 | 网关 82 项：通过 77，跳过 5，失败 0；真 dsh 5 项全部通过；Python 20 项全部通过；类型检查通过 |
| 3. 导入长期记忆 | 5 个文件，共 9429 字节 |
| a. 埋事实 | **符合** |
| b. 聊到换段 | **符合**。摘要在旧会话里写成（`in-session`，599 字），换段时用户没收到任何多余消息 |
| c. 换段后不失忆 | **符合**。用户说全部答对（没报具体件数） |
| d. 新会话第一句 | **符合**。换段后第一轮是 `message`，不需要提醒；整个 M2 一次提醒（nudge）都没出现 |
| e. 记事 | **符合**。`tool.remember` 的 `added` 为 true，`MEMORY.md` 从 60 行变成 61 行 |
| f. `/clear` | **符合**。先写摘要（521 字）再清空，新会话不带清空前的原话；用户说要点记得 |
| g. 用量 | **符合**。有 `closed/budget`，并且有"换段 2 次：摘要在旧会话里写成 2 次……" |
| 6. 记忆整理 | **不符合**：`0/1 成功`。DeepSeek 已经调用成功，但随后记用量时出错，整理结果被丢掉（问题 1） |
| 新发现 | 改了运行规则后接着用旧会话，上下文一次多出约 9.4k token，像是整份新系统提示词被追加进了历史（问题 2） |

## 一、环境

| 项 | 值 |
|---|---|
| 系统 | macOS 15.7.3（Darwin 24.6.0），Apple Silicon arm64 |
| bun | 1.3.11 |
| node / npm | v25.9.0 / 11.12.1 |
| python3 / pytest | 3.10.11 / 9.0.2 |
| dsh | 0.2.0-rc.2（M1 时用 `setup.ts harness` 装在 `~/.dsh-bot/harness`；`harness/` 的锁文件从 M1 起没有变化，没有重装） |

## 二、第 1 步：自动化测试（不需要密钥）

| 命令 | 结果 |
|---|---|
| `cd gateway && bun install` | 依赖没有变化 |
| `bun run typecheck` | 通过 |
| `bun run test` | 11 个文件 82 项：**通过 77，跳过 5，失败 0**，294 次断言，38.0 秒。跳过的 5 项是 `real-dsh.test.ts`（下一行单独跑） |
| `DSH_BOT_HARNESS=~/.dsh-bot/harness bun test --timeout 90000 test/e2e/real-dsh.test.ts` | **5 项通过，0 失败**，22 次断言，3.45 秒 |
| `python3 -m pytest -q unit/test_chat_history_ledger.py unit/test_chat_history_namespace.py unit/test_project_slug.py` | **20 项全部通过**，0.10 秒 |

## 三、第 2–4 步：停旧 bot5、导入记忆、调小预算

**第 2 步：停旧 bot5（经用户同意）**
- 执行 `bash ~/.claude/dispatcher/stop-bot.sh <bot5 旧名>`。
- 当时 bot5 只剩调度器 1 个会话（工作会话还没被拉起），关掉后：
  - 调度器进程没了，17801 不再监听；
  - 没有任何进程的工作目录还在 bot5 旧目录下；
  - 其它三个旧 bot 不受影响。

**第 3 步：导入旧系统的长期记忆**
- 执行 `bun gateway/scripts/setup.ts memory bot5 --from <bot5 旧目录>`，输出：`已导入 Claude Code 的长期记忆：5 个文件，共 9429 字节`。
- 导入后 `MEMORY.md` 有 56 行（只数了行数）。
- 和任务书说的不一样：新目录在导入前是**空的**，因为 M1 时从频道目录复制过来的 `memory/` 本来就是空的，所以没有生成 `MEMORY.md.before-import-<日期>` 备份。但提示语仍写着"新目录里原有的同名文件已改名备份"（问题 6）。

**第 4 步：临时调小预算**
- 在 `brain:` 下加了 `max_input_tokens: 30000`，`setup.ts check` 显示"全部就绪"。
- 这次的 `check` 多了一项"DEEPSEEK_API_KEY 已填，格式像真的密钥"，M1 报告里的问题 6 已经修好。

## 四、第 5 步：真机清单

本机 AI 只看 `gateway.log` 里的事件名和数字字段（聊天 id 已替换），以及账本里的编号、状态、数量、字数；没有打开 `chat.log`，没有查账本正文，也没有打开记忆文件。

**整体数字（M2 期间）**
- 用户发了 93 条消息、1 个 `/clear`；网关跑了 28 轮回复、2 轮写摘要，30 轮全部 `ok`。
- 每轮平均合并 3.3 条消息，最多 9 条；bot 一共发出 349 段。
- 警告和错误只有 1 次 `telegram.poll_failed`（`ECONNRESET`，网络偶发），之后正常。

**偏离任务书（经用户同意）**：中途改了两处临时配置，并重启了一次网关。
- 原因：聊到 2.6 万 token 时，这一段一共才约 2100 字聊天，而换段后新会话开头默认会原样带上最近约 1.2 万字原话。这样用户埋的事实换段后还在原话里，第 c 项就测不到摘要。
- 做法：02:32 在 `gateway:` 下加 `seed_recent_chars: 300`（只带最近约 300 字原话），同时把 `max_input_tokens` 从 30000 改成 26000（加快换段）。`gateway` 段的设置只在启动时读取，所以等当时那一轮回复完，按 Ctrl+C 正常停止再启动（02:32:51–02:33:08）。
- 停止时，用户刚发的 3 条已经记进账本，重启后接着处理了，没有丢。
- 收尾时两行都删掉了。

### a. 埋几个事实：符合

用户一开始就告诉 bot 几件人设编不出来的具体事，内容只有用户知道。bot 正常回复。

### b. 聊到换段：符合

- `segment.roll_start {"reason": "budget", "segment": 4, "used": 26041, "budget": 26000}`（02:34:14）
- `segment.rolled {"reason": "budget", "segment": 4, "summary": "in-session", "summary_chars": 599}`（02:34:17）
- 写摘要单独一轮（`kind: summary`），历时 3 秒，**这一轮一段都没发给用户**；02:34:14–02:34:18 之间账本里没有任何发送记录。
- 新会话（第 5 段）开头：`segment.seed {"entries": 46, "memory_chars": 4966, "memory_truncated": false, "summary_from_segment": 4, "chars": 7052}`。46 条原话合计约 300 字，正好是临时设的上限。
- 这一段在 M2 里聊了 23 轮才换段（前 20 轮预算是 3 万，最后 3 轮是 2.6 万），在"二三十轮内"。按 3 万算，还要再聊约 15 轮。

### c. 换段后不失忆：符合

- 用户问了第 a 项里的那几件事，反馈"都没问题"，没有报具体件数。
- 说明：这些事早已不在换段后那约 300 字原话里。不过模型在换段前后**自己**调用过两次 `remember`：第 25 轮 24 字，换段后第一轮 56 字，内容没有看。所以它答对的部分，可能来自摘要，也可能来自这两条长期记忆。

### d. 新会话第一句：符合

换段后第一轮（第 38 轮）`kind` 是 `message`，没有提醒就回复了 13 段，上下文 14755。`/clear` 后的第一轮同样是 `message`。整个 M2 没有出现 `nudge`。

### e. 记事：符合

- 用户说完"帮我记住：……"后出现 `tool.remember {"turn": 41, "chars": 37, "added": true}`。
- `MEMORY.md` 行数从 60 变成 61，多 1 行。行数用 `grep -c ''` 数，没看内容。
- 补充：前面两次模型自己记的时候，行数是 56 → 59 → 60。第一次多出 3 行，应该是新建了"随手记"小节。

### f. `/clear`：符合

- `segment.roll_start {"reason": "clear", "segment": 5, "used": 15579}` → `segment.rolled {"reason": "clear", "summary": "in-session", "summary_chars": 521}` → `command.clear`。
- "已清空"系统提示发出 1 条。
- 新会话（第 6 段）开头：`segment.seed {"entries": 0, "memory_chars": 5087, "summary_from_segment": 5, "chars": 5668}`。没带清空前的原话，带了摘要和长期记忆。
- 清空后第一轮是 `message`，上下文 13371。
- 用户的感受：没问题，要点记得。

### g. 用量：符合

`report.ts` 的"会话（段）"一节有 `closed/budget 1`，并有一行"换段 2 次：摘要在旧会话里写成 2 次，用账本补写 0 次，没写成（沿用上一份）0 次；摘要平均 560 字"。完整输出见第六节。

## 五、第 6 步：记忆整理（经用户同意）

- 命令：`HUB_CONFIGS_DIR=~/.dsh-bot/configs python3 memory/memory_compactor.py`
- 输出（原样，三行）：

```
开始为 1 个 bot 压缩记忆...
[bot5] LLM 调用失败：no such table: call_log
完成：0/1 成功
```

- 整理前后 `MEMORY.md` 都是 61 行，没有生成 `.bak`。
- 没有重试。原因见问题 1。

## 六、第 7 步：收尾

1. 删掉了第 4 步临时加的 `max_input_tokens`（用户选择删掉，回到"按模型窗口 80%"），也删掉了临时加的 `seed_recent_chars`。配置和测试前一样。
2. 网关停止之前跑了三条命令，输出原样照录（只把本机主目录写成了 `~`）：

```
$ bun gateway/scripts/setup.ts check ~/.dsh-bot/configs/bot5.yml
  ✅ 配置能读：bot bot5，模型 deepseek-official / deepseek-flash，时区 Asia/Shanghai
  ✅ 人设文件在
  ✅ Telegram 令牌在 channel/.env 里
  ✅ 凭据文件在，权限正确
  ✅ DEEPSEEK_API_KEY 已填，格式像真的密钥
  ✅ 主人：1 个
  ✅ dsh 已安装在 ~/.dsh-bot/harness
  ✅ node 在
全部就绪。

$ bun gateway/scripts/health.ts --config ~/.dsh-bot/configs/bot5.yml
bot5：健康
  Telegram 最近一次拉取成功：14 秒前
  dsh：运行中（pid 43518，第 1 次启动）
  模型：deepseek-official / deepseek-flash（思考 low）
  正在进行的回合：0

$ bun gateway/scripts/report.ts --config ~/.dsh-bot/configs/bot5.yml --hours 24
# bot5 运行报告（最近 24 小时）

只含数字，不含聊天内容、用户 id 和密钥。

## 收到的消息
- 按状态：done 116
- 按类型：command 3，user 113
- 现在还没处理完的：0

## 回合
- 按结局：crashed 1，ok 42
- 按类型：message 40，nudge 1，summary 2（retry = 报错后的补救，nudge = 没回也没选择沉默时的提醒）
- 选择不回复（stay_silent）：1
- 每轮上下文大小（dsh 回报，token）：中位数 20780，最大 26629，共 42 轮

## 发出去的消息
- 按状态：reaction/sent 1，system/sent 3，text/sent 406
- 同一轮里同一句话发了不止一次：3

## 会话（段）
- 按状态：abandoned/crash 1，active 1，closed/budget 1，closed/clear 3（closed/budget = 用量到线换段，closed/pre-budget = 新消息太长先换段，closed/clear = /clear）
- 换段 2 次：摘要在旧会话里写成 2 次，用账本补写 0 次，没写成（沿用上一份）0 次；摘要平均 560 字

## 长期记忆
- 新记下的条数（remember）：3

## 日志里有没有机密
- 检查了 2 个日志文件、2 个机密值：没有发现
```

   - 24 小时窗口把 M1 的数据也算进去了。"同一句话发了不止一次：3"全部来自 M1 的第 1 轮（M1 报告的问题 1），M2 期间没有新增。`tool.reply` 的 `duplicates` 字段在 M2 里一直是 0。
   - crashed 1、nudge 1 也都是 M1 的。
3. 按 Ctrl+C 停网关：`gateway.stopping` → `dsh.exited {"code": 130, "unexpected": false}` → `gateway.stopped`，在同一秒内完成。之后网关和 dsh 进程都不在了，17950 端口已释放。
4. 按用户在开始时的选择（已提前说明会重启所有旧 bot），执行 `start-bot.sh <bot5 旧名>`：
   - 停用标记删掉；四个旧 bot 的调度器都重启了一次；
   - 清掉 0 个工作会话；voice-bridge 正常；5 个 launchd 定时任务被卸载再加载；
   - 17801–17804 都在监听。

## 七、发现的问题

**1. 记忆整理：DeepSeek 已经调用成功，记用量时出错，整理结果被丢掉**
- 复现：在新克隆的仓库里（仓库根目录没有 `state.db`）运行 `HUB_CONFIGS_DIR=~/.dsh-bot/configs python3 memory/memory_compactor.py`。
- 现象：输出"`[bot5] LLM 调用失败：no such table: call_log`"和"完成：0/1 成功"；`MEMORY.md` 没变，没有 `.bak`；仓库根目录多出一个 0 字节、没有任何表的 `state.db`（已被 git 忽略）。
- 原因（只读代码得出）：
  - `memory_compactor.py` 先调用 `_call_llm(...)` 请求 DeepSeek，成功后才执行 `quota.record_call(...)` → `db.insert_call_log(...)`。
  - `db.py` 的 `DB_PATH` 是 `db.py` 所在目录下的 `state.db`。文件不存在时，sqlite 会新建一个空库，而这条路径上没有调用 `db.init()` 建表，于是报"no such table"。
  - 这个异常落在同一个 `try` 里，被当成"LLM 调用失败"，整理好的结果被丢弃。也就是说花了一次调用，却没有写回。
- 修法供参考：记用量之前确保建表（`db.init()`，或在 `insert_call_log` 里建表）；记用量失败不应该影响整理结果（单独 `try`）；输出里把"模型调用失败"和"记账失败"分开写。
- 旧系统的数据库不受影响：`db.py` 用的是新仓库自己目录下的 `state.db`，不是旧仓库的。

**2. 改了运行规则后接着用旧会话，上下文一次多出约 9.4k token**
- 现象：
  - M2 开始时，网关接着用 M1 留下的会话（`segment.resumed {"segment": 4}`）。这个会话在 M1 结束时的上下文是 10014，M2 第一轮变成 19391，多了约 9.4k，大约是一整份人设的量。
  - 中途按 Ctrl+C 正常重启那一次（期间什么都没改），同样续用会话 4，上下文只从 25572 涨到 25810，是正常增长。
- 本机查到的依据：
  - M1（`0beab8a`）到 M2（`187a0b1`）之间，`gateway/src/dsh/profile.ts` 的运行规则多了 1 行，所以系统提示词变了；
  - dsh 0.2.0-rc.2 的模型目录里，`deepseek-flash` 标着 `systemPromptUpdate: "in-history"`；
  - 网关只在新会话第一轮才在前面加前情，续用旧会话时不会额外加东西。
- 判断：很可能是 dsh 发现系统提示词变了以后，把整份新的系统提示词（人设加运行规则）当成一条消息追加进了历史，没有替换原来的。没有看会话内容，没法直接确认，请用假模型核实。
- 影响：方案里"改人设时等空闲重启 dsh，会话续上"，按这个行为，每改一次人设或运行规则，每个续用的会话里都会多出一整份人设。这样既多花 token，模型也可能看到新旧两份人设。
- 建议：系统提示词变化后不要续用旧会话，直接换段（新会话开头补前情）。

**3. 回复期间用户感觉"消息没合并"**
- 现象：用户反映连着发的消息好像没有合并。数据显示合并是正常的（M2 期间 93 条合成 28 轮，平均 3.3 条，最多 9 条）。
- 原因：每轮从开始到发出第一段只要 3–5 秒，但这一轮要发 12–18 段，逐段发送、段间有停顿，前后共 28–49 秒。这段时间里用户的新消息只能排到下一轮，用户看到的就是"它还在回前面的，我又发了好几条"。
- 建议：交给用户决定要不要调。比如段数多时缩短段间停顿；或者回复途中收到新消息时，加快发完剩下的段。

**4. 预算小的时候，"最近原话"会把整段对话都带上**
- 现象：预算 3 万左右时，一段对话只有两三千字，而默认原样带最近 1.2 万字原话。换段后整段原话都会带进新会话，摘要实际上没起作用。本次是临时把 `seed_recent_chars` 调到 300 才测到摘要。
- 另外：`gateway` 段的设置（比如 `seed_recent_chars`）只在网关启动时读取，改了要重启。M2.md 里没有写这一点。
- 建议：默认原话窗口按预算的一定比例定，或者在文档里写明这两点。

**5. 改 `max_input_tokens` 能热生效，但日志里没有任何记录**
- 现象：删掉 `max_input_tokens` 之后，网关重新读了 `brain` 段，但 `config.brain_changed` 只在供应商、模型、思考强度变化时才写，没法从日志确认新的预算已经生效。
- 建议：`config.brain_changed` 里也记上 `max_input_tokens`。

**6. `setup.ts memory` 的提示语和实际情况不符**
- 现象：新目录里原本没有同名文件，没有改名备份任何东西，但提示语仍写"新目录里原有的同名文件已改名备份"。
- 建议：按实际备份的文件数显示。
