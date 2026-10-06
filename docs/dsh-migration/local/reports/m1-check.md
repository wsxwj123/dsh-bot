# M1 真机验收（bot5）

- 日期：2026-10-06（日志时间为 UTC 2026-10-05 18:02–18:34）
- 代码：分支 `claude/nice-brahmagupta-dtjsma`，提交 `0beab8a`
- 执行方式：本机 AI 按 `docs/dsh-migration/LOCAL-AGENT-M1.md` 操作，用户在 Telegram 上配合
- 本文不含密钥、聊天内容、Telegram 用户 id 和聊天 id、人设内容、bot 真名。日志里的聊天 id 一律写成 `<chat>`，旧 bot5 的名字写成 `<bot5 旧名>`

## 结论

| 项 | 结论 |
|---|---|
| 1. 自动化测试 | 68 项：通过 65，跳过 3（真 dsh 那组，第 2 步单独跑），失败 0；类型检查通过 |
| 2. 真 dsh 集成测试 | 3 项全部通过，dsh 0.2.0-rc.2 |
| a. 日常聊天 | **符合**。11 条消息合成 5 轮，全部 `ok`；连发合并生效。第 1 轮同一句话发了两遍（问题 1） |
| b. 强杀 | **符合**。发过的段没有重发；停机期间的消息重启后补上了；dsh 子进程已退出。被打断的那条长回复不会补完，这是按设计（问题 3） |
| c. `/clear` | **符合**（重测确认）。第一次用户觉得"它记得"，核对后是巧合（见第四节 c） |
| d. 换模型 | **符合**。下一轮生效，dsh 没有重启 |
| e. 健康 | **符合**，显示"健康" |
| f. 风格 | 用户：基本没变化 |
| `report.ts`"同一轮里同一句话发了不止一次" | **3，预期 0**。原因是模型在第 1 轮自己调用了两次 reply，不是网关重发（问题 1） |
| `report.ts`"日志里有没有机密" | 没有发现 |

## 一、环境

| 项 | 值 |
|---|---|
| 系统 | macOS 15.7.3（Darwin 24.6.0），Apple Silicon arm64 |
| bun | 1.3.11 |
| node | v25.9.0 |
| npm | 11.12.1（npm 源是 npmmirror 镜像） |

## 二、第 1、2 步：自动化测试（不需要密钥）

- `cd gateway && bun install`：装了 5 个包（开发依赖 `@types/bun` 1.4.2、`typescript` 5.9.3 等），`bun.lock` 没有变化。
- `bun run typecheck`：通过。
- `bun run test`：10 个文件 68 项，**通过 65，跳过 3，失败 0**，229 次断言，耗时 21.3 秒。跳过的 3 项是 `test/e2e/real-dsh.test.ts`，没设 `DSH_BOT_HARNESS` 时自动跳过。
- `bun gateway/scripts/setup.ts harness`：最后一行 `✅ dsh 已安装：0.2.0-rc.2`。`npm ci` 装了 540 个包，M1.md 写的是 543 个（问题 7）。
- `DSH_BOT_HARNESS=~/.dsh-bot/harness bun test --timeout 90000 test/e2e/real-dsh.test.ts`：**3 项通过，0 失败**，13 次断言，1.57 秒。

## 三、第 3、4 步：准备 bot5、停旧 bot5

**准备**
- bot5 的旧目录就是上一轮盘点里别名为 bot5 的那个目录（目录名不写）。
- `setup.ts credentials`：凭据文件已存在（上一轮本机准备第 7 步建的模板），没有覆盖，权限 600。密钥由用户自己用编辑器填入。
- `setup.ts bot bot5 --from <bot5 旧目录>`：复制了 `CLAUDE.md`、`access.json`、`relationship.json`、`.env`、`memory/`，生成 `~/.dsh-bot/configs/bot5.yml`。
  - 用 `cmp` 核对过，复制的人设和旧目录里的逐字节相同，内容没有打开看。
  - 目录权限 700，`.env` 权限 600。
- `setup.ts check`：`全部就绪。`（但它认不出旧模板的占位文字，见问题 6；这次是用户亲眼确认已经填好。）

**停旧 bot5（经用户同意）**
- 旧 bot5 的运行方式：
  - 两个 tmux 会话：`tg-<bot5 旧名>-dispatcher` 跑调度器（端口 17801），`tg-<bot5 旧名>-worker` 跑 Claude Code（由调度器拉起）；
  - 另有一个每 600 秒触发一次的主动消息 launchd 任务。
- 旧系统有停用单个 bot 的脚本 `~/.claude/dispatcher/stop-bot.sh <名>`：
  - 写停用标记 `~/.claude/dispatcher/.disabled/<名>`，关掉这个 bot 的会话；
  - 看门狗（每 600 秒）和主动消息脚本见到标记就跳过，不会把它拉起来。
- 执行 `bash ~/.claude/dispatcher/stop-bot.sh <bot5 旧名>` 后：
  - 关掉了 2 个会话；调度器进程当即没了，17801 不再监听；
  - Claude Code 工作进程约 5 秒后自行退出；
  - 之后没有任何进程的工作目录还在 bot5 旧目录下；
  - 其它 bot 的会话都还在。
- 恢复命令（用户选择测完再执行）：`bash ~/.claude/dispatcher/start-bot.sh <bot5 旧名>`。

## 四、第 5 步：真机清单

新网关的启动命令：`DSH_BOT_LOG_CONSOLE=1 bun gateway/src/main.ts --config ~/.dsh-bot/configs/bot5.yml`。启动后依次出现 `mcp.listening`、`gateway.starting`、`api.listening {"port": 17950}`、`gateway.ready`，没有 `telegram.conflict`。

本机 AI 只看 `gateway.log` 里的事件名和数字字段（聊天 id 已替换），数回复只用 `grep -c '] bot: '`，没有打开 `chat.log` 正文，也没有查账本里的正文列。

### a. 日常聊天：符合

- 用户共发 11 条，网关合成 5 轮。每轮收到的条数是 1、4、2、3、1，回复途中来的消息在这一轮结束后合成下一轮。
- 5 轮都有一条 `turn.end`，`outcome` 都是 `ok`；每轮都有送达，分别是 8、8、4、3、4 段，共 27 段，和 `chat.log` 里 bot 的行数一致。
- 没有出现 `send.failed`、`turn.no_reply`、`turn.stalled_cancel`。
- 实际聊了 5 轮，比任务书说的"十几轮"少。
- 第 1 轮同一句话发了两遍（问题 1）；第 4 轮在 reply 送达 3 段之后又调用了 stay_silent（问题 5）。

### b. 强杀：符合

- 做法：按任务书挂好监视命令（只数 `chat.log` 里 bot 的行数）。用户发了一条会引出长回复的消息，bot 刚发出第 1 段，网关就被 `kill -9`（18:10:42 UTC）。
- **强杀后 dsh 子进程：已退出。** `pgrep -fl 'dsh/lib/bin.js'` 没有输出，原 dsh 进程不在了。不过这次检查是在强杀约 7 分钟后做的（中间在等用户操作），dsh 也从没写过 stderr 日志，所以没法确定它是不是立刻退出的。
- 停机期间，用户又发了 3 条。重新执行启动命令后：
  - `recovery {"crashed": 1, "aborted": 0, "requeued": 0, "settled": 1, "abandoned_segments": 1}`：被打断的那一轮已经有 1 段送达，按设计记为结束，不再重试；旧会话作废。
  - 开了新会话，`segment.created {"seeded": true, "segment": 2}`，开头补了最近的原话。
  - 停机期间的 3 条合成一轮，`turn.end` 为 `ok`，送达 6 段。
  - bot 发出的段数从 28 变成 34，只多了这 6 段，**之前送达的那 1 段没有重发**。
- 用户看到的现象："强杀前那条消息只收到第 1 段，之后没有接着说完"。这符合方案里"已经有回复发出去的，不再重试"，但用户在意这一点（问题 3）。

### c. `/clear`：符合（重测确认）

- **第一次测试**：
  - 用户发 `/clear` 后收到系统提示（账本里多了 1 条 `system` 类型的发送）；旧会话状态变为 `closed/clear`；之后问的那条进了新会话，正常回复。
  - 用户反馈"它记得"。具体情况：清空前后两次问的都是 bot 自己当天做了什么，两次答出的是同一件事。
  - 核对数据：清空后那一轮的上下文是 10043 token；全新会话第一轮是 10136；强杀后补了原话的新会话是 11201。清空前大约 1800 token 的对话没有进入清空后的会话。
  - 代码里，清空后补前情只取清空时刻之后的记录，这次为空。而"当天做了什么"是 bot 照着人设可以现编的内容，所以判断是两次碰巧编得一样。
- **重测**：
  - 用户先告诉 bot 一个人设编不出来的具体事实（一句专门的测试句），然后 `/clear`，再问这个事实。**bot 回答不知道。**
  - 清空后那一轮的上下文是 9843 token，比清空前那段会话的每一轮（10043–10898）都小。
  - 结论：符合。
- 附带现象：重测时清空后的第一轮，模型既没调用 reply，也没调用 stay_silent（问题 2）。

### d. 换模型：符合

- 把 `~/.dsh-bot/configs/bot5.yml` 的 `model:` 改成 `deepseek-v4-pro`，约 7 秒内出现 `config.brain_changed {"model": "deepseek-v4-pro"}`。
- 用户发一条后出现 `brain.applied {"provider": "deepseek-official", "model": "deepseek-v4-pro", "effort": "low", "segment": 3}`；`turn.end` 为 `ok`，送达 3 段。
- `health.ts` 显示：`dsh：运行中（pid 49264，第 1 次启动）`，模型 `deepseek-official / deepseek-v4-pro`。dsh 没有重启。
- 测完改回 `deepseek-flash`，又出现一次 `config.brain_changed`。

### e. 健康：符合

`health.ts` 显示 `bot5：健康`（几次运行都一样），完整输出见第五节。

### f. 风格

用户的一句话感受：基本没变化。

## 五、第 6 步：收尾

网关停止之前跑了下面三条命令，输出原样照录（只把本机主目录写成了 `~`）：

```
$ bun gateway/scripts/setup.ts check ~/.dsh-bot/configs/bot5.yml
  ✅ 配置能读：bot bot5，模型 deepseek-official / deepseek-flash，时区 Asia/Shanghai
  ✅ 人设文件在
  ✅ Telegram 令牌在 channel/.env 里
  ✅ 凭据文件在，权限正确
  ✅ 主人：1 个
  ✅ dsh 已安装在 ~/.dsh-bot/harness
  ✅ node 在
全部就绪。

$ bun gateway/scripts/health.ts --config ~/.dsh-bot/configs/bot5.yml
bot5：健康
  Telegram 最近一次拉取成功：24 秒前
  dsh：运行中（pid 49264，第 1 次启动）
  模型：deepseek-official / deepseek-flash（思考 low）
  正在进行的回合：0

$ bun gateway/scripts/report.ts --config ~/.dsh-bot/configs/bot5.yml --hours 24
# bot5 运行报告（最近 24 小时）

只含数字，不含聊天内容、用户 id 和密钥。

## 收到的消息
- 按状态：done 22
- 按类型：command 2，user 20
- 现在还没处理完的：0

## 回合
- 按结局：crashed 1，ok 12
- 按类型：message 12，nudge 1（retry = 报错后的补救，nudge = 没回也没选择沉默时的提醒）
- 选择不回复（stay_silent）：1
- 每轮上下文大小（dsh 回报，token）：中位数 10665，最大 11954，共 12 轮

## 发出去的消息
- 按状态：reaction/sent 1，system/sent 2，text/sent 57
- 同一轮里同一句话发了不止一次：3

## 会话（段）
- 按状态：abandoned/crash 1，active 1，closed/clear 2

## 日志里有没有机密
- 检查了 2 个日志文件、2 个机密值：没有发现
```

- "同一轮里同一句话发了不止一次"是 **3，不是预期的 0**。原因见问题 1：模型自己发了两遍，不是网关重发。
- "日志里有没有机密"：**没有发现**。

**停网关**：在网关终端按 Ctrl+C，日志依次出现 `gateway.stopping` → `dsh.exited {"code": 130, "unexpected": false}` → `gateway.stopped`，在同一秒内完成。之后网关和 dsh 进程都不在了，17950 端口已释放。

**恢复旧 bot5**：按用户的选择执行了 `start-bot.sh <bot5 旧名>`。
- 它删掉停用标记，然后调用旧系统的全栈重启脚本，所以：
  - 4 个 bot 的调度器都重启了一次；
  - 所有 Claude Code 工作会话被清掉，下次来消息时再拉起；
  - voice-bridge 重启了一次；
  - 5 个 launchd 定时任务被卸载再加载（3 个主动消息任务、`jiwen-tick`、`reap-stale-workers`）。
- 这些都是旧重启脚本每次的固定动作，launchd 配置文件本身没有被修改。
- 恢复后，17801–17804 四个端口都在监听。

## 六、第 7 步：provider-proxy（只读）

| 项 | 结果 |
|---|---|
| 程序 | `~/Library/Application Support/claudebotlife-provider-proxy/cli-proxy-api`，由 launchd 常驻运行 |
| 监听地址 | `127.0.0.1:8770`（配置里的 host、port 和实际监听一致） |
| 对外协议 | **两种都有**：Anthropic Messages（`POST /v1/messages`）和 OpenAI 兼容（`POST /v1/chat/completions`），另有 `GET /v1/models`。不带密钥访问这三个路径都返回 401；访问一个不存在的路径返回 404，说明这三个路径确实存在 |
| 配置里有哪些项 | 只列了名字，没有读值：`host`、`port`、`auth-dir`、`api-keys`、`remote-management`、`claude-api-key`。没有模型映射，也没有上下文窗口 |
| bot 用的模型名 | 旧部署会话记录里，bot2、bot3、bot5 是 `gemini-3.8-flash-high`。bot4 的项目设置直接指向一个第三方 Anthropic 兼容中转（**不经过**本机 provider-proxy；域名不写进公开报告），模型名是 `[1000k按次计费]gemini-3.8-flash`；它的会话记录里还有 `deepseek-flash` |
| 上下文窗口 | 配置和文档里都没写。只有 bot4 的模型名前缀写着"1000k"，没有核实 |
| 没查清的 | bot2、bot3、bot5 现在的设置文件（项目级和 local）里，没有任何指向 8770 的地址；用户级设置里只有 `ANTHROPIC_MODEL: claude-opus-4-6`。它们怎么走到代理，由旧系统"换模型来源"功能的供应商清单决定，这份清单多半在旧仓库的 `configs/_global.yml` 里，按铁律没有打开 |

## 七、发现的问题

事件里的聊天 id 都已去掉。

**1. 同一轮里同一句话发了两遍**（`report.ts` 计数 3 的来源）
- 复现：网关启动后的第一轮，即新会话的第一轮，用户发了一条普通问候。
- 现象：模型调用了两次 reply，每次 4 段；第二次的第 1、3、4 段和第一次逐字相同（分别 4、7、8 个字）。用户看到同样的话说了两遍。之后 12 轮没再出现。
- 事件：`tool.reply {"turn": 1, "parts": 4, "delivered": 4}` 出现两次；`turn.end {"turn": 1, "outcome": "ok", "delivered": 8}`。账本里发送编号 `turn:1:c1:p1/p3/p4` 和 `turn:1:c2:p1/p3/p4` 的正文相同。
- 判断：不是网关重发。发送编号是唯一的，c1、c2 是两次不同的调用。运行规则里写了"发完就结束这一轮"，模型还是又调了一次。
- 修法供参考：网关在同一轮里拦下和已送达内容重复的段，并告诉模型；或者在 reply 的返回语里写明"已全部送达，本轮结束"。

**2. 新会话第一轮偶尔不回话，要靠提醒**
- 复现：`/clear` 之后发一条消息（第二次清空后出现）。
- 现象：这一轮结束时一段都没送达，也没选择沉默；网关补发一次提醒后，模型才回复了 6 段。用户多等了约 20 秒。
- 事件：`segment.created {"seeded": true, "segment": 4}`、`turn.end {"turn": 12, "kind": "message", "delivered": 0, "silent": false}`、`turn.end {"turn": 13, "kind": "nudge", "delivered": 6}`。
- 和上一轮 real-03 里"新会话第一条消息模型不用 reply 回话"是同一类问题。这次其它三个新会话的第一轮都正常回复了，所以是偶发。

**3. 被强杀打断的回复不会补完**（按设计的取舍，但用户在意）
- 复现：长回复刚发出第 1 段时 `kill -9` 网关，然后重启。
- 现象：那条消息只收到第 1 段；重启后这一轮被记为已结束，不会接着说完。
- 事件：`recovery {"crashed": 1, "settled": 1, "requeued": 0, "abandoned_segments": 1}`；账本里第 6 轮状态是 `crashed`，送达 1 段。
- 建议：在新会话的前情里注明"上一条回复被中断，只发出了第 1 段"，让模型自己决定要不要补完。

**4. `segment.created` 的 `seeded` 字段会误导**
- 现象：清空后的新会话也记成 `seeded: true`，但实际补进去的前情是空的（`formatSeed` 返回了 null）。
- 建议：日志里记实际补了几条。

**5. 同一轮里既回复又"选择不回复"**
- 现象：第 4 轮 reply 送达 3 段之后，模型又调用了 stay_silent：`turn.end {"turn": 4, "delivered": 3, "silent": true}`。`report.ts` 因此记了"选择不回复 1"，但用户其实收到了回复。

**6. `setup.ts check` 认不出别的占位文字**
- 现象：凭据文件如果是上一轮本机准备生成的模板，占位文字是 `<用户自己填>`。`check` 只认它自己模板里的"把这里换成你的密钥"，所以照样显示"凭据文件在，权限正确"和"全部就绪"。
- 建议：只判断值的样子（比如是不是 DeepSeek 密钥的固定前缀开头、长度够不够），不要打印出来。

**7. 小差异**
- `setup.ts harness` 在本机装了 540 个包，M1.md 写的是 543 个；dsh 版本一致。本机 npm 源是 npmmirror。
