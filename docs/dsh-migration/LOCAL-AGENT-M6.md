# 本机 AI 任务书：M6 真机验收（开机自启、回滚演练、导入试算）

你在用户的 Mac 上工作，接着 M5 的活。新代码在仓库 `wsxwj123/dsh-bot` 的分支 `claude/nice-brahmagupta-dtjsma` 上（M1–M5 已经合进 master）。

## 先读

- `docs/dsh-migration/CUTOVER.md`：切换、回滚、排查、升级手册。这次就是按它演练
- `docs/dsh-migration/M6.md`
- `docs/dsh-migration/LOCAL-AGENT-M3.md`：铁律和收尾方式，这次照样适用

## 铁律

和前几期相同，违反任何一条就停下来问用户。补充三条：

1. **导入脚本这次只用 `--dry-run` 和 `--inspect-promises`**：只看条数和结构，不写账本。正式导入等用户决定正式切换时再做。
2. **不打开 `.promises.json` 和旧会话文件看内容。** 只看脚本打印的数字和字段名。
3. **launchd 只动 `com.dsh-bot.*` 开头的任务。** 旧系统的任务一个都不碰。

## 步骤

### 1. 拉代码、跑自动化测试

```bash
git fetch origin claude/nice-brahmagupta-dtjsma && git checkout claude/nice-brahmagupta-dtjsma && git pull
cd gateway && bun install && bun run typecheck && bun run test
DSH_BOT_HARNESS=~/.dsh-bot/harness bun test --timeout 90000 test/e2e/real-dsh.test.ts
cd ../tests && python3 -m pytest -q unit/test_chat_history_ledger.py unit/test_chat_history_namespace.py unit/test_project_slug.py unit/test_m3_life.py unit/test_m4_dsh.py unit/test_m5_director.py unit/test_m6_import.py
```

预期：
- 网关 223 项（通过 218，跳过 5）；
- 真 dsh 5 项通过；
- Python 42 项通过。

### 2. bot5 改成开机自启

1. 旧 bot5 停着（同前几期；在跑就先问用户再停）。之前用 tmux 或终端开着的新网关，先停掉。
2. 在**设了代理的终端**里运行（launchd 启动的程序拿不到终端里的环境变量，安装时会把代理设置带进去）：

```bash
bun gateway/scripts/autostart.ts install ~/.dsh-bot/configs/bot5.yml
bun gateway/scripts/autostart.ts install-jobs ~/.dsh-bot/configs/bot5.yml
bun gateway/scripts/autostart.ts status
```

预期：`status` 列出 `com.dsh-bot.bot5`（状态 running，带 pid）和 `com.dsh-bot.self-initiate.bot5`（定时任务，平时不在运行是正常的）。

3. 看安装出来的配置里没有密钥（只出数字）：

```bash
grep -c -E 'sk-|TOKEN|KEY' ~/Library/LaunchAgents/com.dsh-bot.*.plist
```

预期每个文件都是 0。

### 3. 真机清单

每一项记"符合"或"不符合"，加一句现象（不写聊天内容）。

| 项 | 怎么做 | 预期 |
|---|---|---|
| a. 自启的网关能聊 | 请用户给 bot5 发一条消息 | 正常回复。`bun gateway/scripts/logs.ts --config ~/.dsh-bot/configs/bot5.yml -n 20` 里有这一轮的 `turn.end` |
| b. 退出后自动拉起 | `kill $(launchctl print gui/$UID/com.dsh-bot.bot5 \| awk '/pid =/{print $3}')`，等 15 秒，再看 `status` | pid 变了，状态又是 running。日志里先有 `gateway.stopping`，再有 `gateway.ready` |
| c. 主动消息的定时任务 | 等 10 分钟以上，看 `tail -5 ~/.dsh-bot/logs/self-initiate.bot5.log` | 有"skip: 原因；下次机会在 N 分钟后"或"已投递给网关"之类的行。跳过原因里带活动名的，只记类别 |
| d. 重启电脑（可选，先问用户） | 重启后登录 | 不用手动启动，`status` 显示 bot5 在跑，给它发消息能回 |
| e. 回滚演练（可选，先问用户：会重启所有旧 bot） | 按 CUTOVER.md 第三节退回旧 bot5；请用户给 bot5 发一条消息；再按第一节第 4 步装回新的 | 退回后是旧 bot5 在回复（旧系统的日志里有这条），新网关没在跑；装回后又是新网关在回复 |
| f. 导入试算 | 问用户选一个还没迁的旧 bot，见下面的命令 | 只打印条数和字段名，`~/.dsh-bot/bots/<新名>` 不会被创建 |

**第 f 项命令**（`<新名>` 用一个还没用过的名字，比如 `trial`；报告里旧 bot 写 `botY`）：

```bash
python3 scripts/import_history.py trial --from ~/.claude/channels/<旧名> --old-bot <旧名> --dry-run
python3 scripts/import_history.py trial --from ~/.claude/channels/<旧名> --inspect-promises
ls ~/.dsh-bot/bots | grep -c '^trial$'
```

预期：
- 第一条打印"对方 N 条，bot M 条"和"还没到点的旧承诺：K 条"；
- 第二条打印字段名、类型和"会导入 K 条"；
- 第三条是 0。
- 字段名里如果看不出哪个是时间、哪个是内容（脚本认得的字段名见 `scripts/import_history.py` 的 `TEXT_KEYS`、`DUE_KEYS`），记下来报告，不要猜。

### 4. 收尾

1. 跑一遍 `setup.ts check`、`health.ts`、`report.ts --hours 24`，输出放进报告（路径里的系统用户名改成 `~`，聊天 id 改成 `<chat>`）。
2. 问用户 bot5 要不要保持开机自启：
   - 保持：什么都不用做；
   - 不保持：`autostart.ts uninstall bot5` 和 `uninstall self-initiate.bot5`，再按用户的意思恢复旧 bot5。

## 交付

1. 写 `docs/dsh-migration/local/reports/m6-check.md`，包括：
   - 环境；
   - 第 1 步的测试结果；
   - 第 2 步的输出；
   - 第 3 步每一项的结果；
   - 第 4 步三条命令的输出；
   - 发现的问题：复现步骤、现象、事件名。
2. 提交前，在 `git diff` 里搜一遍：`sk-`、令牌格式、用户的 Telegram 用户 id、系统用户名、bot 的旧名字。确认都没有。
3. 从 `claude/nice-brahmagupta-dtjsma` 拉出新分支 `local/m6-check`，推到这个分支。
4. 向用户汇报时用平实的中文，先说结论。最后一句写："M6 实测已推到 local/m6-check"。
