# 本机 AI 任务书：bot5 切换后的修复验收、新建供应商命令

你在用户的 Mac 上工作，接着上次 bot5 正式切换的活（报告：分支 `local/cutover-report` 上的 `docs/dsh-migration/local/reports/cutover.md`）。

- 本机仓库：`~/dsh-bot-work`（上次从桌面挪过来的）。下面的命令都在这里运行。
- 新代码在远端分支 `claude/nice-brahmagupta-dtjsma` 上，是在上次的 `5377675` 后面**直接追加**的提交，没有改写历史。

这次要验收的：

1. 上次报告第六节的问题：
   - 开机自启拒绝装在桌面等目录；
   - `status` 显示定时任务上次的退出码；
   - 导入试算提示"已经聊过"；
   - 生图慢时，"图好了"的系统消息写明就是刚才那张。
2. 新功能 `/provider add`：主人在私聊里发一条命令就能新建模型供应商。
3. 可选：先装情绪、记忆整理两个共用任务（只管新系统的 bot）。
4. 可选：换一个模型对比"AI 感"。

## 先读

- `docs/dsh-migration/PROVIDER.md`：`/provider add` 怎么用、怎么生效、安全措施
- `docs/dsh-migration/CUTOVER.md`：第一节"前提"（仓库位置）、第二节"共用服务"
- `docs/dsh-migration/LOCAL-AGENT-CUTOVER.md`：上次的任务书，报告格式照旧

## 铁律

违反任何一条就停下来问用户。

1. **不读密钥。**
   - 凭据文件、`.env`、令牌文件都不打开，不 `cat`、不 `grep` 内容。
   - 检查有没有配好，只用三样：`setup.ts check`、`report.ts` 最后一节、Telegram 里 `/provider` 显示的"密钥已配"。
   - `/provider add` 由**用户自己**在 Telegram 里发。不要让用户把密钥贴给你，也不要替用户拼这条命令。
2. **不看聊天、记忆和人设。**
   - `logs.ts` 不加 `--chat`；
   - 不打开 `chat.log`、账本、`MEMORY.md`、`CLAUDE.md`；
   - 聊得怎么样只问用户的感受，不记内容。
3. **动旧 bot、开机自启项之前先问用户。** 包括：
   - 装、卸、重启任何 `com.dsh-bot.*` 任务（重启 bot5 的网关也算）；
   - 碰旧系统的任何 launchd 任务。
   - 旧系统的任务这次一个都不用动。
4. **报告里不能有：**
   - 密钥、聊天内容、人设内容；
   - 用户 id、聊天 id、群 id、bot 真名（旧 bot5 写成 `<bot5 旧名>`）；
   - 带系统用户名的路径（写成 `~`）；
   - 供应商的接口地址（写"本机 provider-proxy"或"某家 OpenAI 兼容接口"这样的类别）。

## 第 0 步：拉代码、跑测试

上次本地的 `claude/nice-brahmagupta-dtjsma` 是改写前的旧版，它多出来的 3 个提交已经存在 `local/m4-check` 上。先确认再换：

```bash
cd ~/dsh-bot-work
git status --short          # 应该是空的；有未提交的改动就停下来问用户
git fetch origin claude/nice-brahmagupta-dtjsma
for c in $(git log --format=%h origin/claude/nice-brahmagupta-dtjsma..claude/nice-brahmagupta-dtjsma 2>/dev/null); do git branch --contains $c | grep -q 'local/m4-check' && echo "$c 在 local/m4-check 上" || echo "$c 不在！"; done
```

- 每一行都是"在 local/m4-check 上"（或者一行都没有）：
  ```bash
  git checkout -B claude/nice-brahmagupta-dtjsma origin/claude/nice-brahmagupta-dtjsma
  ```
- 有"不在！"：停下来问用户，不要换。
- 以后这个分支只追加，`git pull --ff-only` 就能更新。

然后跑测试：

```bash
cd gateway && bun install && bun run typecheck && bun run test
DSH_BOT_HARNESS=~/.dsh-bot/harness bun test --timeout 120000 test/e2e/real-dsh.test.ts
cd ../tests && python3 -m pytest -q unit/test_chat_history_ledger.py unit/test_chat_history_namespace.py unit/test_project_slug.py unit/test_m3_life.py unit/test_m4_dsh.py unit/test_m5_director.py unit/test_m6_import.py
cd ..
```

预期：
- 网关 237 项（通过 231，跳过 6）；
- 真 dsh 6 项通过（多了一项：用 `/provider add` 建的供应商在真 dsh 上能切过去）；
- Python 43 项通过。

## 第 1 步：让 bot5 用上新代码（先问用户）

开机自启跑的是 `~/dsh-bot-work` 里的代码，换了代码要重启一次网关才生效。bot5 会离线几秒。

1. 问用户可不可以现在重启 bot5 的网关。同意后：

```bash
launchctl kickstart -k gui/$UID/com.dsh-bot.bot5
sleep 15
bun gateway/scripts/autostart.ts status
```

2. 预期 `status` 的样子（主动消息任务不用重装，它每次运行都读仓库里的最新代码）：

```
   com.dsh-bot.bot5：运行中（pid 12345）
   com.dsh-bot.self-initiate.bot5：定时任务，没在运行，跑过 N 次，上次退出码 0
```

- 定时任务要显示完整的"没在运行"，并且有"跑过 N 次""上次退出码"（报告问题 5）。
- 有问题的行开头是 ⚠️，并写了该看哪个日志。
- 有 ⚠️ 时，`status` 的退出码是 1。
- 上次桌面那 5.5 小时失败的记录可能还在 launchd 里，显示成退出码 1。等它再跑一次（10 分钟内）应该变成 0。

## 第 2 步：真机清单

每一项记"符合"或"不符合"，加一句现象（不写聊天内容）。

| 项 | 怎么做 | 预期 |
|---|---|---|
| a. 导入试算提示"已经聊过" | `python3 scripts/import_history.py bot5 --from ~/.claude/channels/<bot5 旧名> --old-bot <bot5 旧名> --dry-run` | 照常打印条数和旧承诺数，**多一行** "⚠️ 这个私聊在新系统里已经聊过…正式导入会跳过聊天记录…"。不写任何东西（只读） |
| b. 生图慢时不重复生成 | 请用户让 bot5 发 2～3 张自拍或照片，每次说一句就等它发完。问用户一共要了几张 | `bun gateway/scripts/logs.ts --config ~/.dsh-bot/configs/bot5.yml -n 200` 里：`tool.generate_image` 的次数 = 用户要的张数；`report.ts --hours 1` 里 `photo/sent` 的数量也一样。生成得慢的那几次，日志里能看到一条来源为 image 的合成消息（`report.ts` 的"合成消息"一行里 `image` 的数），之后紧跟着发图，没有再调一次 `generate_image` |
| c. `/provider add` 格式不对 | 请用户在和 bot5 的私聊里发 `/provider add test` | 用户那边：这条消息消失了；bot 回"格式不对。什么都没做。用法：…"，最后一行"你发的那条（带密钥）已经删掉了"。日志里有 `inbound.secret_command`、`provider.add_received … deleted=true`，**没有**这条消息的 `inbound.recorded` |
| d. `/provider add` 建一个真的供应商 | 见下面"第 d 项" | 见下面 |
| e. 对比"AI 感"（可选，和 d 一起做） | 见下面"第 e 项" | 用户的感受，一句话 |

### 第 d 项：新建一个真的供应商

1. 问用户用哪家。
   - 建议用本机的 provider-proxy：旧系统的 bot 就是通过它用 Claude 的。它是 Anthropic 接口，所以命令末尾不写 `openai`。地址见 `docs/dsh-migration/local/INVENTORY.md`。
   - 它要不要密钥、密钥是什么，用户自己知道。你不经手，也不问内容。
   - 如果 provider-proxy 不要密钥，随便写一串 8 位以上的字母数字就行。
2. 请用户自己在和 bot5 的私聊里发：
   ```
   /provider add <名字> <接口地址> <密钥>
   ```
   名字用英文，比如 `proxy`。
3. 预期：
   - 用户那边：这条消息很快消失。bot 回"已添加供应商「<名字>」，拉到 N 个模型。切过去：/provider <名字>"，最后一行是"已经删掉了"。
   - 没拉到模型列表时，回复里写着原因。记下原因的类别（密钥不对 / 地址不对 / 对方没有模型列表），按 `CUTOVER.md` 故障排查表的最后一行处理，再请用户重发一次 add。
4. 检查（都不碰密钥）：
   ```bash
   bun gateway/scripts/logs.ts --config ~/.dsh-bot/configs/bot5.yml -n 40
   python3 -c "import json,os;d=json.load(open(os.path.expanduser('~/.dsh-bot/providers.json')));print({k:(v['api'],len(v['models']),v['fetchError']) for k,v in d['providers'].items()})"
   stat -f '%Lp %N' ~/.dsh-bot/credentials.yaml ~/.dsh-bot/providers.json
   bun gateway/scripts/report.ts --config ~/.dsh-bot/configs/bot5.yml --hours 1 | tail -3
   ```
   预期：
   - 日志里依次有：
     - `inbound.secret_command`；
     - `provider.add_received … deleted=true`；
     - `provider.added … models=N`；
     - `config.restart_needed`、`dsh.restart_idle`、`dsh.started`。
   - 日志里没有这条消息的 `inbound.recorded`。
   - 登记文件里有这个名字，协议是 `anthropic-messages`，模型数 N，`fetchError` 是 None。
   - 两个文件的权限都是 `600`。
   - `report.ts` 最后一行："检查了 … 个文件（日志、账本、补丁层、供应商登记）、… 个机密值：没有发现"。机密值的个数应该比加之前多 1。
5. 请用户发 `/provider`：新供应商那一行写着"密钥已配"。
6. 请用户发 `/provider <名字>`：回"已换成 <名字> / <模型>"。再随便聊一句，正常回复。
7. 管理台（可选）：
   - 新仓库的网页这次没有常驻。临时起一个（和 M4 验收一样用 8767，不动旧网页）：
     ```bash
     MOMENTS_WEB_PORT=8767 HUB_CONFIGS_DIR=~/.dsh-bot/configs python3 moments/web.py
     ```
   - 请用户打开 `http://127.0.0.1:8767/hub/dsh-model`。预期：下面多了一块"用 /provider add 建的供应商"，有这个名字、模型数、"刷新模型列表"按钮。点一下，提示"拉到 N 个模型"。
   - 用完 Ctrl-C 停掉。
   - 不起网页也行：请用户在 Telegram 里发 `/provider refresh <名字>`，效果一样。

### 第 e 项：对比"AI 感"（可选）

用户觉得 bot5 的回复"一股 AI 感"。现在 bot5 用的是 deepseek-flash（思考强度 low），旧系统用的是 Claude，人设是在 Claude 上调出来的。

1. 做完第 d 项、切到 provider-proxy 以后，请用户像平时一样和 bot5 聊几轮。只问用户觉得怎么样，不看内容。
2. 想再对比一个：请用户发 `/model list` 看能换哪些，比如 DeepSeek 更大的模型，再发 `/model <模型名>` 换过去聊几轮。
3. 最后问用户留哪个：
   - 留 Claude：什么都不用做（重启后保持）。注意按 Claude 的价格计费。
   - 换回原来的：发 `/model default`。
4. 报告里只写：试了哪几个（写类别，比如"provider-proxy 上的 Claude""DeepSeek 更大的模型"）、用户的感受（一句话）、最后留了哪个。

## 第 3 步：可选，先装情绪、记忆整理（先问用户）

`CUTOVER.md` 第二节说明了为什么这两项现在就能装、和旧系统的同名任务互不干扰。问用户要不要装。同意后：

```bash
bun gateway/scripts/autostart.ts install-shared --only jiwen,memory-compactor
bun gateway/scripts/autostart.ts status
```

- 这里不加 `--botlife-db`：这两项用不到朋友圈库。
- 想沿用旧系统调过的情绪速率：问用户后，把旧仓库的 `configs/_global.yml` 复制到 `~/dsh-bot-work/configs/`。不要打开看内容，它可能带密钥；这个文件在 `.gitignore` 里，不会被提交。不复制就按默认速率。
- 等 5 分钟以上，再看：
  ```bash
  grep 'jiwen.tick' ~/.dsh-bot/logs/jiwen.log | tail -3
  bun gateway/scripts/autostart.ts status
  ```
  预期：
  - 有一行"开始 … bots=['bot5']"，只有 bot5，没有旧 bot；
  - `com.dsh-bot.jiwen` 的上次退出码是 0。
- 记忆整理每周日 4 点才跑，这次不手动跑：它会改 bot5 的长期记忆。
- 旧系统的情绪、记忆整理任务照常在跑，不用动。用 `launchctl list | grep -i -E 'jiwen|compactor'` 看，新旧都在就对了。只记个数，不记旧任务的全名。

## 第 4 步：收尾

1. 跑一遍下面三条，输出放进报告（路径里的系统用户名改成 `~`，聊天 id 改成 `<chat>`）：
   - `bun gateway/scripts/setup.ts check ~/.dsh-bot/configs/bot5.yml`
   - `bun gateway/scripts/health.ts --config ~/.dsh-bot/configs/bot5.yml`
   - `bun gateway/scripts/report.ts --config ~/.dsh-bot/configs/bot5.yml --hours 24`
2. `bun gateway/scripts/autostart.ts status` 的输出也放进去。
3. 第 d 项建的供应商留不留，问用户：
   - 留：什么都不用做；
   - 不留：请用户发 `/provider remove <名字>`。它会连密钥一起删掉。

## 交付

1. 写 `docs/dsh-migration/local/reports/bot5-followup.md`，包括：
   - 环境；
   - 第 0 步的测试结果；
   - 第 1 步的 `status` 输出；
   - 第 2 步每一项的结果；
   - 第 3 步做没做、结果；
   - 第 4 步的输出；
   - 发现的问题：复现步骤、现象、事件名。
2. 提交前，在 `git diff` 里搜一遍：
   - `sk-`、令牌格式；
   - 用户的 Telegram 用户 id、系统用户名、bot 的旧名字；
   - 供应商的接口地址和端口。
   确认都没有。
3. 从当前的 `claude/nice-brahmagupta-dtjsma` 拉出新分支 `local/bot5-followup`，提交并推到这个分支。
4. 向用户汇报时用平实的中文，先说结论。最后一句写："bot5 后续验收已推到 local/bot5-followup"。
