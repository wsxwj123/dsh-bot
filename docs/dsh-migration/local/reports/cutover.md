# 正式切换报告（bot5）

- 日期：2026-10-07 下午到 10-08 凌晨（日志时间为 UTC 10-07 08:48–17:37）
- 代码：远端分支 `claude/nice-brahmagupta-dtjsma` 的提交 `5377675`（代码和 master 相同）
- 执行方式：本机 AI 按 `docs/dsh-migration/LOCAL-AGENT-CUTOVER.md` 操作，每个改变现状的动作都先经用户同意；用户在 Telegram 上配合抽查
- 本文不含以下内容：
  - 密钥；
  - 聊天内容、人设内容；
  - 用户 id、聊天 id、群 id；
  - bot 真名（旧 bot5 的名字写成 `<bot5 旧名>`）。

## 结论

- **bot5 已经从旧系统切到新系统，并设成了开机自启。**
  - 网关常驻，意外退出 10 秒后自动拉起；
  - 主动消息每 10 分钟检查一次。
- 旧 bot5 已停用，停用标记在。另外三个旧 bot 照常运行。
- **抽查通过。**
  - 35 轮回复全部正常；
  - 13 条语音全部转成了文字，M4 的 403 问题已解决；
  - 生图 17 次全部成功；
  - 主动消息第一次在真机上投递成功。
- 用户对说话方式的评价是“一股 AI 感”（第七节）。
- 切换中发现一个问题并处理了：仓库放在桌面下，主动消息任务被 macOS 的隐私保护拦住。经用户同意，把仓库挪到 `~/dsh-bot-work` 后正常（第四节、第六节问题 1）。
- 按用户决定：只切 bot5；群聊、共用服务这次不动（第 2、3 步没做）；没有做回滚。
- 用户提了一个新需求：新建供应商的斜杠命令，交给云端（第八节）。

## 一、环境

| 项 | 值 |
|---|---|
| 系统 | macOS 15.7.3（Darwin 24.6.0），Apple Silicon arm64 |
| bun | 1.3.11 |
| node / npm | v25.9.0 / 11.12.1 |
| python3 / pytest | 3.10.11 / 9.0.2 |
| dsh | 0.2.0-rc.2，装在 `~/.dsh-bot/harness` |
| 新仓库位置 | 原来在 `~/Desktop/claude/dshbot/dsh-bot-work`，切换中挪到 `~/dsh-bot-work` |
| 生图 | novelai |

## 二、第 0 步：测试和用户的决定

| 命令 | 结果 |
|---|---|
| `bun install`、`bun run typecheck` | 依赖没有变化；类型检查通过 |
| `bun run test` | 24 个文件 223 项：**通过 218，跳过 5，失败 0** |
| 真 dsh 测试 | **5 项通过** |
| Python 测试（7 个文件） | **42 项全部通过** |

用户的决定：

| 问题 | 决定 |
|---|---|
| 切哪几个 bot | 只切 bot5 |
| 群聊（导演点名） | 这次不处理。bot5 在旧导演管的那个群里，切过去以后在这个群里不再说话；等群里的 bot 全部切完再开新导演 |
| voice-bridge、provider-proxy | 照旧留着 |
| 共用服务（情绪、记忆整理、朋友圈网页、电话） | 这次不动，按任务书全部切完再换。代价是这段时间 bot5 没有关系数值更新、每周记忆整理、旧网页上的评论通知和电话 |

## 三、bot5 的切换记录

| 步骤 | 结果 |
|---|---|
| 1.1 停旧 bot5 | 经用户同意执行 `stop-bot.sh <bot5 旧名>`：杀掉 2 个会话，17801 不再监听，停用标记在，没有残留进程，另外三个旧 bot 不受影响 |
| 1.2 准备新 bot | 迁过的 bot。新旧两份人设比较结果“不同”：旧系统那份当天改过，新副本是 M1 时复制的。用户选旧系统那份：先备份新副本，再把旧的复制过来。`setup.ts check` 全部就绪 |
| 1.3 人设里的旧技术说明 | 命中 19 行 → 0 行。用户逐处同意了 12 处，可选的 2 处选择不改（见下）。一共改 99 行（删 83、加 16）。旧目录里的人设没有动（和改前备份完全相同）。备份在 `~/.dsh-bot/backup/bot5/` |
| 1.4 导入旧记录 | 试算：最近 30 天对方 20 条、bot 19 条，旧承诺 0 条（旧目录没有 `.promises.json`）。bot5 的私聊在新系统账本里已经有会话段，正式导入一定会被拒绝，所以没有导入（第六节问题 2） |
| 1.5 开机自启 | 经用户同意，在带代理的环境里执行 `install` 和 `install-jobs`。网关 running；主动消息任务第一次运行就失败了，挪仓库后重装解决（第四节） |
| 1.6 抽查 | 见下表 |

**1.3 改了哪些地方（按对照表的类别，不含原文）**

| 类别 | 处数 |
|---|---|
| 以 `[self-initiate]` 开头的消息 → 以 `⟦系统·主动开口⟧` 开头 | 1 |
| 生图：novelai-skill / comfyui-skill 目录、用 Bash 查引擎、脚本路径 → 先 `image_guide` 再 `generate_image`，图用 `reply` 的 `files` 发 | 4 |
| 用 Bash 调旧网页接口切换生图引擎 → 告诉对方要在后台设置里改 | 1 |
| reply 的 `chat_id` 参数 → 删掉（两个调用示例、群里的一句说明） | 2 |
| `add_group_alias` → 删掉（整小节） | 1 |
| 自己去读群聊记录文件 → 改成“导演点到你时会把最近的对话一起给你” | 1 |
| 用 Bash 跑 `recent_moments.py` → `moments` 工具 `action=recent`（说明和示例） | 2 |
| **合计** | **12** |

没改的：
- 可选的 2 处技术说明，用户选择不改：一处讲生图和回复的先后，一处讲只能发哪张图；
- 群聊里“被 @ 才回”的旧机制（4 行），群聊这次没开，留到开群聊时再改。

**1.6 抽查（UTC 08:48–17:37）**

| 项 | 结果 |
|---|---|
| 消息正常回复 | 35 轮（34 轮回复、1 轮提醒）全部 `ok`；没有出错事件；警告只有 10 次 `telegram.poll_failed`（网络断开，都自动重连了） |
| 改过人设 | 出现 1 次 `segment.prompt_changed`，符合预期。说话方式用户评价“一股 AI 感”（第七节） |
| 语音 | 13 条全部转写成功。每条都出现一次 `media.transcribe_retry`：网关换了一个 voice-bridge 允许的目录重试，M4 报告里的 403 问题已解决 |
| 生图 | `tool.generate_image` 17 次全部成功；发图前压缩 16 次（`send.photo_shrunk`）；有 1 次 `tool.reply_slow` |
| 朋友圈 | 这段时间没用到，没有测 |
| 主动消息 | 挪仓库后手动触发一次：退出码 0，日志写“已投递给网关；下次机会在 891 分钟后”。网关收到来源为 `self_initiate` 的合成消息，这一轮发出 6 段。这是主动消息第一次在真机上投递成功 |
| `health.ts` | 健康 |
| 导入过旧记录的 | 不适用 |

## 四、偏离任务书的地方

1. **拉代码。**
   - 远端工作分支被强制改写过（M4–M6 合并后整理），本地分支无法快进，`git pull` 失败。
   - 本地多出来的 3 个提交是 M4 合并前的旧版本，已经保存在 `local/m4-check` 上。
   - 没有重置本地分支，直接从远端最新提交拉出 `local/cutover-report` 来干活和放报告。
2. **先后顺序。** 先做不影响旧 bot 的准备（换人设、改技术说明、导入试算），再停旧 bot5、设开机自启，缩短旧 bot5 的停机时间。每一步的内容没变。
3. **挪仓库（经用户同意）。**
   - 主动消息任务装上后第一次运行就失败了，launchd 输出里是 `getcwd: cannot access parent directories: Operation not permitted`。原因是仓库在 `~/Desktop` 下，macOS 不让 launchd 直接拉起的 python 访问桌面文件夹（第六节问题 1）。
   - 处理步骤：
     1. 卸掉两个开机自启项；
     2. 把仓库从 `~/Desktop/claude/dshbot/dsh-bot-work` 挪到 `~/dsh-bot-work`；
     3. 在新位置重装；
     4. 手动触发一次主动消息，验证通过。
   - bot5 离线约 25 秒。
   - 挪之前的约 5.5 小时里，这个任务每 10 分钟失败一次，launchd 输出累计约 9 KB。网关本身一直正常。
4. **群聊、共用服务这次不动**（用户决定），任务书第 2、3 步没做。
5. **没有做回滚。**

## 五、收尾时的输出

时间 UTC 17:36。输出原样照录，只把本机主目录写成 `~`：

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
  ✅ 音色已配（access.json 的 voiceId）
  ✅ voice-bridge 在跑
  ✅ 生图（novelai）的说明文件在
  ✅ NovelAI 令牌已配（在技能目录的 .env.local里，没有显示内容）
  ✅ 朋友圈库在
  ·  朋友圈、画风按旧系统里的名字找（取自 life_config 或 life_id）
全部就绪。

$ bun gateway/scripts/health.ts --config ~/.dsh-bot/configs/bot5.yml
bot5：健康
  Telegram 最近一次拉取成功：4 秒前
  dsh：运行中（pid 79844，第 1 次启动）
  模型：deepseek-official / deepseek-flash（思考 low）
  正在进行的回合：0

$ bun gateway/scripts/report.ts --config ~/.dsh-bot/configs/bot5.yml --hours 24
# bot5 运行报告（最近 24 小时）

只含数字，不含聊天内容、用户 id 和密钥。

## 收到的消息
- 按状态：done 46
- 按类型：command 4，synthetic 22，user 20
- 现在还没处理完的：0

## 回合
- 按结局：ok 39
- 按类型：message 37，nudge 1，summary 1（retry = 报错后的补救，nudge = 没回也没选择沉默时的提醒）
- 选择不回复（stay_silent）：11
- 每轮上下文大小（dsh 回报，token）：中位数 37131，最大 49319，共 39 轮

## 发出去的消息
- 按状态：photo/sent 18，system/sent 4，text/sent 115
- 同一轮里同一句话发了不止一次：0

## 会话（段）
- 按状态：active 1（closed/budget = 用量到线换段，closed/pre-budget = 新消息太长先换段，closed/clear = /clear，closed/prompt-changed = 人设、运行规则或 dsh 版本变了）
- 换段 1 次：摘要在旧会话里写成 1 次，用账本补写 0 次，没写成（沿用上一份）0 次；摘要平均 694 字

## 承诺
- 按状态：无（source：tool = 模型自己登记，auto = 网关从说过的话里补登记）
- 时间说得含糊、提醒模型自己登记：0
- 合成消息（程序塞给模型的）：hang 7，image 14，self_initiate 1

## 长期记忆
- 新记下的条数（remember）：3

## 日志里有没有机密
- 检查了 3 个日志文件、2 个机密值：没有发现

$ bun gateway/scripts/autostart.ts status
  com.dsh-bot.bot5：running（pid 78785）
  com.dsh-bot.self-initiate.bot5：not
```

## 六、发现的问题

**1. 仓库放在桌面下时，launchd 直接拉起 python 的任务会被系统拦住，而且看不出来**
- 复现：
  1. 仓库在 `~/Desktop` 下；
  2. 执行 `autostart.ts install-jobs <配置>`；
  3. 等 10 分钟。
- 现象：
  - `~/.dsh-bot/logs/self-initiate.<名>.launchd.log` 里出现 `shell-init: error retrieving current directory: getcwd: cannot access parent directories: Operation not permitted` 和 `pyenv: cannot change working directory`；
  - `launchctl print` 显示 `last exit code = 1`。
- 网关（bun）不受影响，它调用的 python（查作息、生图、朋友圈）也正常。只有 launchd 直接拉起 python 的任务会失败：主动消息，以及以后的导演和共用服务。
- `autostart.ts status` 对这个任务只显示 `not`（问题 5），看不到退出码。这次失败持续了约 5.5 小时才发现。
- 建议：
  - `autostart.ts` 安装时检查仓库是否在桌面、文稿、下载这几个受保护的目录下，是的话提示或拒绝；
  - `status` 显示最后一次退出码；
  - CUTOVER.md 写明仓库不要放在这几个目录。

**2. 导入试算不检查“已经聊过”**
- 复现：对已经在新系统里聊过的 bot 运行 `import_history.py … --dry-run`。
- 现象：照常给出条数（这次是对方 20 条、bot 19 条），看起来能导入；去掉 `--dry-run` 才会因为“已经聊过”被拒绝。
- 建议：试算时也检查并提示“正式导入会跳过”。

**3. 人设对照表只靠关键词命中，漏掉了相关的技术说明**
- 现象：bot5 的人设里，有两句和生图有关的技术说明没有命中关键词：一句讲生图和回复的先后，一句要求只能发“这一轮新生成的图”。
- 后一句和网关的做法冲突：生图超过 45 秒时，网关在下一轮才用系统消息告诉模型图片路径。照这句话，模型可能不敢发这张图，转头再生成一张。M4 实测里看到过“生成了不发、又生成”的情况。
- 用户这次选择不改这两句。
- 建议：
  - 在“生图好了”的系统消息里写明“这张就是刚才那次请求生成的，可以直接发”；
  - 或者把这类说法加进对照表。

**4. 远端工作分支被改写过，任务书第 0 步的 `git pull` 会失败**
- 现象：本地已有旧版分支的机器上，`git pull` 报“无法快进”。
- 建议：任务书里写明怎么处理；或者以后不改写已经推送的分支。

**5. `autostart.ts status` 对定时任务显示 `not`**
- 应为 `not running`，被截断了；也没有显示上次运行的结果。

**6. 网络（仅供参考）**
- 约 9 小时里出现 10 次 `telegram.poll_failed`（ECONNRESET），都自动重连了，没影响收消息。

## 七、用户的意见

- **说话方式**：用户评价“一股 AI 感”。
  - 本机 AI 的推测（没有验证）：旧系统用的是 Claude，新系统现在用的是 deepseek-flash，思考强度 low。可以先用 `/model` 换更强的模型对比一下。
- **改人设和看日志**：已经告诉用户：
  - 人设文件是 `~/.dsh-bot/bots/bot5/channel/CLAUDE.md`，旧目录那份不再生效；保存后下一条消息自动生效，不用重启；
  - 实时日志：`bun gateway/scripts/logs.ts --config ~/.dsh-bot/configs/bot5.yml -f`（加 `--chat` 能看到正文）；
  - 主动消息的日志：`~/.dsh-bot/logs/self-initiate.bot5.log`。

## 八、新需求：新建供应商的斜杠命令（交给云端）

用户的设想（照录，命令示例原话里没有给出）：
- 在和任意一个 bot 的私聊里发一条命令，带上供应商名字、接口地址、密钥。对方是 OpenAI 格式的接口，末尾加一个 `openai`；不加就按 Anthropic 处理。
- bot 收到后：
  1. 马上删掉这条消息，因为里面有密钥。删掉了告诉用户；没删掉就提醒用户手动删。
  2. 在面板里新建这个供应商，接着去对方那里把模型列表拉下来存好。
  3. 回复结果，比如“已添加供应商「X」，拉到 N 个模型。切过去：/provider X”。
  4. 模型列表没拉到的话，供应商照样建好，回复里写明原因，用户之后可以在面板上再刷新一次。

待定（请和用户确认）：
- 命令写法。本机 AI 建议 `/provider add <名字> <接口地址> <密钥> [openai]`，以用户为准；
- “面板”指新系统管理台的模型页，还是旧系统管理台的供应商页；
- dsh 怎么登记自定义供应商，以及几个 bot 共用一份时怎么生效。

安全要求：
- 只有主人能用；
- 这条消息在写进账本之前就要拦下：密钥不进账本、`chat.log`、`gateway.log`，也不交给模型；
- 用 Telegram 的删除消息接口删掉原消息（私聊里 bot 可以删对方发来的消息），删除结果告诉用户；
- 密钥只写进 `~/.dsh-bot/credentials.yaml`，权限保持只有本人可读写；
- 回复和日志里只出现供应商名字和模型数量，不出现密钥。
