# 本机 AI 任务书：M1 真机验收（bot5）

你在用户的 Mac 上工作，接着之前"本机准备"的活。这次的任务是帮用户做新系统 M1 的真机验收。

新系统的代码已经由另一个 AI 写好，在 GitHub 仓库 `wsxwj123/dsh-bot` 的分支 `claude/nice-brahmagupta-dtjsma` 上（拉取请求 #1）。

## 先读这三份

- `docs/dsh-migration/M1.md`：重点是第三节"真机清单"
- `gateway/README.md`
- `docs/dsh-migration/local/DECISIONS.md`：用户之前做的决定

## 铁律

违反任何一条，就停下来问用户。

1. **不读、不打印、不复制任何密钥和令牌。**
   - 不要用 cat、grep、head、less 打开这些文件：
     - `~/.dsh-bot/credentials.yaml`
     - 任何 `.env`
     - `~/.claude/channels/*/.env`
     - 旧仓库的 `configs/_global.yml`
   - 想知道准备好了没有，只用 `bun gateway/scripts/setup.ts check …`，它不会打印机密。
   - 上一轮验证时，完整的 DeepSeek 密钥被打进过会话日志，这次绝不能再发生。
2. **密钥由用户自己用编辑器填。**
   - 你只告诉用户该打开哪个文件。
   - 不要让用户把密钥贴给你，也不要把密钥写进命令行参数。
3. **不读聊天内容。**
   - 不要打开 `logs/chat.log`，也不要查账本（`ledger.sqlite`）里的正文。
   - 需要判断"bot 回了没有"时，用不输出正文的命令，比如 `grep -c`、`grep -q`。
   - 聊天内容由用户自己在 Telegram 里看。
4. **不改旧系统的任何文件。** 包括 `~/.claude`、旧仓库、launchd 配置。
   - 唯一的例外：征得用户同意后，临时停掉旧 bot5；测完按用户的意思恢复。
5. **不改 `gateway/` 下的代码。** 发现问题就记录三样：怎么复现、看到了什么现象、日志里对应的事件名。由另一个 AI 来修。
6. **提交到仓库的内容里，下面这些一样都不能有：**
   - 密钥、令牌
   - 聊天内容
   - Telegram 用户 id、聊天 id
   - 人设内容
   - bot 的真名（一律写 bot5）
   - 带系统用户名的路径（写成 `~`）

   `gateway.log` 里没有聊天正文，但有聊天 id。引用之前，先把 id 换成 `<chat>`。

## 步骤

### 1. 拉代码、跑自动化测试（不需要密钥）

```bash
git fetch origin claude/nice-brahmagupta-dtjsma
git checkout claude/nice-brahmagupta-dtjsma
cd gateway && bun install && bun run typecheck && bun run test
```

记录这几项：

- bun 的版本
- 通过几项、失败几项
- 失败的测试名和报错

### 2. 装钉死版本的 dsh，跑真 dsh 集成测试（不需要密钥）

在仓库根目录运行：

```bash
bun gateway/scripts/setup.ts harness
```

- 预期最后一行：`✅ dsh 已安装：0.2.0-rc.2`
- 如果下载慢或失败：不要改锁文件，把报错原样记下来。

```bash
cd gateway && DSH_BOT_HARNESS=~/.dsh-bot/harness bun test --timeout 90000 test/e2e/real-dsh.test.ts
```

预期结果是 3 项都通过。

### 3. 准备 bot5

1. 找到 bot5 在旧系统里的目录。之前盘点时你记过 bot5 对应哪个目录。目录名不要写进提交。
2. 运行 `bun gateway/scripts/setup.ts credentials`。
3. 请用户执行 `open -e ~/.dsh-bot/credentials.yaml`，自己把 DeepSeek 密钥填进去并保存。等用户说"填好了"再往下走。
4. 运行 `bun gateway/scripts/setup.ts bot bot5 --from <bot5 的旧目录>`。
5. 运行 `bun gateway/scripts/setup.ts check ~/.dsh-bot/configs/bot5.yml`，预期最后一行是"全部就绪。"

### 4. 停掉旧 bot5（必须先问用户）

1. 只读地查清旧 bot5 是怎么跑起来的：launchd 任务、名为 `tg-*` 的 tmux 会话，还是别的方式。
2. 把打算执行的"停止命令"和之后的"恢复命令"都告诉用户，等用户同意后再执行。
3. 只停 bot5。不动其它 bot，也不动公共服务（比如 provider-proxy）。
4. 停完之后，确认 bot5 的旧进程已经没了。

### 5. 启动新网关，陪用户做真机清单

另开一个终端，在仓库根目录运行下面的命令，让它一直跑着：

```bash
DSH_BOT_LOG_CONSOLE=1 bun gateway/src/main.ts --config ~/.dsh-bot/configs/bot5.yml
```

- 如果启动失败，终端会打出一句中文原因，里面不含机密，照着处理就行。
- 分工：用户在 Telegram 上发消息并看回复；你看 `gateway.log` 和终端输出，并执行命令。
- 每一项做完，记"符合"或"不符合"，再加一句现象描述。

| 项 | 怎么做 | 预期 |
|---|---|---|
| a. 日常聊天 | 用户私聊十几轮，其中两三次连发两三条 | 每次都回；连发的几条合成一次回。`gateway.log` 里每轮有一条 `turn.end`，`outcome` 是 `ok` |
| b. 强杀 | 先在另一个终端运行下面这条（它只数行数，不把正文输出到屏幕；bot 一发出第一段就强杀网关）：`L=~/.dsh-bot/bots/bot5/logs/chat.log; n=$(grep -c '] bot: ' $L); until [ $(grep -c '] bot: ' $L) -gt $n ]; do sleep 0.1; done; pkill -9 -f 'gateway/src/main.ts'`。然后请用户发一条会引出长回复的消息，比如"讲讲你今天都做了什么，多说一点"。网关被杀后，运行 `pgrep -fl 'dsh/lib/bin.js'`，看 dsh 子进程有没有跟着退出，记下结果。最后重新执行启动命令 | 已经发出的段不会再发；还没回的消息，重启后会补上。如果 bot5 的回复不分段，这一项就记"回复只有一段，没法测到中途"，并记下用户看到的现象 |
| c. `/clear` | 用户发 `/clear`，然后问一个只有清空前才聊过的事 | 收到"【系统】已清空这段对话的上下文……"；之后它不记得清空前的内容 |
| d. 换模型 | 把 `~/.dsh-bot/configs/bot5.yml` 里的 `model:` 改成 `deepseek-v4-pro`，等 5 秒，请用户发一条 | 照常回复；`gateway.log` 出现 `brain.applied`，模型是新的；`health.ts` 显示 dsh 还是"第 1 次启动"。测完改回 `deepseek-flash` |
| e. 健康 | `bun gateway/scripts/health.ts --config ~/.dsh-bot/configs/bot5.yml` | 显示"健康" |
| f. 风格 | 问用户：回复风格和旧系统比有没有变化 | 只记用户的一句话感受，不记聊天内容 |

### 6. 收尾

1. 先在网关还没停的时候，跑这三条命令，把输出原样放进报告：

   ```bash
   bun gateway/scripts/setup.ts check ~/.dsh-bot/configs/bot5.yml
   bun gateway/scripts/health.ts --config ~/.dsh-bot/configs/bot5.yml
   bun gateway/scripts/report.ts --config ~/.dsh-bot/configs/bot5.yml --hours 24
   ```

   `report.ts` 里有两行要重点看：
   - "同一轮里同一句话发了不止一次"应该是 0。
   - "日志里有没有机密"应该是"没有发现"。
2. 在网关的终端按 Ctrl+C 停掉它。
3. 问用户要不要恢复旧 bot5，按用户的意思执行。

### 7. 顺便查一件事（只读）

用 gemini 的那几个 bot，走的是本机的 provider-proxy。只读地查出下面这几项：

- 它监听的地址和端口；
- 它对外用的是什么协议：Anthropic Messages，还是 OpenAI 兼容；
- bot 用的模型名；
- 模型的上下文窗口（配置或文档里写了才记）。

不要输出它的任何密钥或令牌配置。

## 交付

1. 写 `docs/dsh-migration/local/reports/m1-check.md`，包括：
   - 环境：系统版本、bun 版本、node 版本；
   - 第 1、2 步的测试结果；
   - 第 5 步每一项的结果；
   - 强杀之后 dsh 子进程有没有退出；
   - 第 6 步三条命令的输出；
   - 第 7 步查到的结果；
   - 发现的问题：复现步骤、现象、事件名（id 已替换）。
2. 提交前自查：在 `git diff` 里搜一遍这几样，确认都没有：
   - `sk-`
   - 令牌格式（一串数字、冒号、一长串字母）
   - 用户的 Telegram 用户 id
   - 系统用户名
3. 从 `claude/nice-brahmagupta-dtjsma` 拉出新分支 `local/m1-check`，推到这个分支。不要推到 master，也不要推到以 `claude/` 开头的分支。
4. 向用户汇报时用平实的中文，先说结论。最后告诉用户："M1 实测已推到 local/m1-check"，用户会把这句话转给另一个 AI。
