# M4 真机验收（bot5：语音、图片、生图、朋友圈、管理台换模型）

- 日期：2026-10-07 凌晨（日志时间为 UTC 2026-10-06 15:36–16:12，北京时间 23:36–00:12）
- 代码：分支 `claude/nice-brahmagupta-dtjsma`，提交 `7639beb`
- 执行方式：本机 AI 按 `docs/dsh-migration/LOCAL-AGENT-M4.md` 操作，用户在 Telegram 和网页上配合
- 本文不含以下内容：
  - 密钥、NovelAI 令牌、voice-bridge 口令；
  - 聊天内容、记忆、人设和作息内容；
  - 图片和语音的内容、朋友圈文字；
  - Telegram 用户 id 和聊天 id；
  - bot 真名（旧 bot5 的名字写成 `<bot5 旧名>`）。

## 结论

| 项 | 结论 |
|---|---|
| 1. 自动化测试 | 网关 211 项：通过 206，跳过 5，失败 0；真 dsh 5 项全部通过；Python 34 项全部通过；类型检查通过 |
| 3. 补配置 | `setup.ts check` 显示全部就绪，新加的音色、voice-bridge、生图说明、NovelAI 令牌、朋友圈库都是 ✅ |
| a. 收语音 | **不符合**。3 条语音都没转成文字（voice-bridge 返回 403），bot 看到的只是“[语音消息，没转成文字]”（问题 1） |
| b. 发语音 | **不符合**（按用户的标准）。能发出可播放的语音（5 段全部成功）。但说的是中文、没有文字对照；旧系统是中文文字加日语语音（问题 2） |
| c. 收图片 | **符合**。模型能看图（没出现 `prompt.images_unsupported`），用户确认 bot 说得出图里的东西 |
| d. 自拍 | **符合**（能出图）。但有三个问题：同一张图发了两遍；发一张图要 26–45 秒；每次生图那一轮都要等满 45 秒（问题 3、4、5） |
| e. 网页评论 | **符合**。出现一条来源为 `moment_reply` 的合成消息和 `tool.moments`（`reply`，`code` 0），网页上能看到 bot5 的回复 |
| f. 发朋友圈 | **不符合**。当时作息是“睡觉”，发圈前的检查第一条就挡住了，连试 3 次都是 `code` 3（问题 6） |
| g. 管理台换模型 | **符合**。换成别的模型、发消息正常回复、再换回配置文件里的模型，都正常 |
| h. 电话 | 没做（用户选择不做） |

## 一、环境

| 项 | 值 |
|---|---|
| 系统 | macOS 15.7.3（Darwin 24.6.0），Apple Silicon arm64 |
| bun | 1.3.11 |
| node / npm | v25.9.0 / 11.12.1 |
| python3 / pytest | 3.10.11 / 9.0.2；flask 3.1.3、requests 2.33.1（和旧系统跑朋友圈网页的是同一个 Python） |
| dsh | 0.2.0-rc.2（M1 时装在 `~/.dsh-bot/harness`，没有重装） |
| 生图 | **novelai**（旧系统 `_global.yml` 里打印出来的服务名） |
| voice-bridge | 旧系统正在跑的那份，代码在 `~/.claude/voice-bridge/`，端口 7788 |

## 二、第 1 步：自动化测试（不需要密钥）

| 命令 | 结果 |
|---|---|
| `cd gateway && bun install` | 依赖没有变化 |
| `bun run typecheck` | 通过 |
| `bun run test` | 20 个文件 211 项：**通过 206，跳过 5，失败 0**，626 次断言，53.4 秒。跳过的 5 项是 `real-dsh.test.ts`（下一行单独跑） |
| `DSH_BOT_HARNESS=~/.dsh-bot/harness bun test --timeout 90000 test/e2e/real-dsh.test.ts` | **5 项通过，0 失败**，22 次断言，4.6 秒 |
| `python3 -m pytest -q unit/test_chat_history_ledger.py unit/test_chat_history_namespace.py unit/test_project_slug.py unit/test_m3_life.py unit/test_m4_dsh.py` | **34 项全部通过**，1.9 秒 |

## 三、第 2–3 步：旧 bot5、补配置

**第 2 步：旧 bot5**
- M3 收尾时用户选择“先不恢复”，所以开始时旧 bot5 一直是停着的：停用标记在，17801 不监听。不用再停。
- 另外三个旧 bot、旧朋友圈网页（8765）、旧电话服务（8766）都在跑。

**第 3 步：补配置**
- 旧系统给 Telegram 生图用的服务：`novelai`。
- `~/.claude/skills/novelai-skill/SKILL.md` 在；技能目录的 `.env.local` 在（90 字节，没有打开）。
- 在 `~/.dsh-bot/configs/bot5.yml` 原有的 `gateway:` 下面加了两行：`image_skill_dir: ~/.claude/skills/novelai-skill` 和 `botlife_db: ~/claudebotlife/state.db`。改之前备份过。
- 音色：脚本输出“旧频道有音色 / 新频道原来有音色”，没有改动（M1 复制频道文件时已经带过来了）。
- `VOICE_BRIDGE_TOKEN`：按任务书先跳过。第 a 项返回的是 403 不是 401，说明口令这一项不是问题。
- `setup.ts check` 的输出：

```
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
```

以 · 开头的那一行只是说明会按旧名字找，不是哪个功能用不了。

## 四、第 4–5 步：启动网关和网页、真机清单

**运行前先查了这次会写到新系统以外的地方**（读代码确认）：

| 写到哪里 | 写什么 | 说明 |
|---|---|---|
| `~/claudebotlife/state.db`（旧朋友圈库） | bot5 的评论、点赞、新圈 | 任务书的设计：新旧系统共用一个朋友圈库。新旧仓库的 `db.py` 建表语句完全相同，连上旧库不会改表结构 |
| `~/resource/media/<bot5 旧名>/telegram-<chat>/` | 生成的图片、生图脚本的“上次请求”和历史记录 | 生图脚本没改，按旧名字存，和旧 bot5 平时生图写的是同一个目录 |

- 新网关自己的文件（收到的语音和图片、合成的语音、生图的中间文件）都在 `~/.dsh-bot/bots/bot5/media/` 下。
- 8767 的网页导入时不会起后台任务，也不会执行建表，只在用户操作时才动作。
- 用户打开过旧的“模型来源”页面，但只是查看，网页日志里没有对它的提交。

**启动**
- 网关 15:37:20 就绪。启动时把 M3 留下、已经过期的追问状态丢掉了（`hang_drop ... reason=stale`），正常。
- 8767 的网页正常响应：首页和 `/hub/dsh-model` 都返回 200。
- 第一条消息时出现 `segment.prompt_changed` 和一次换段（摘要 `in-session`，729 字），和任务书说的一样。

**整体数字（UTC 15:38–16:12）**
- 收到：用户消息 7 条（其中语音 3 条、图片 1 张）；“生图好了”的系统消息 7 条；网页评论通知（`moment_reply`）1 条。
- 回合：
  - 正常结束的回复 10 轮，平均 74.6 秒，最长 139.2 秒；
  - 提醒（nudge）3 轮，写摘要 1 轮；
  - 停网关时取消 1 轮。
- 发出：文字 28 段（其中 5 段用语音发出）；图片消息 10 条，只对应 6 个不同的文件，4 个各发了两遍。
- 生图 7 次，全部成功，每次都超过 45 秒，所以都是“先回一句、好了再发”。其中 1 张生成了但没发出去。
- 每轮上下文 2.4 万–3.6 万 token。

### a. 收语音：不符合

- 3 条语音都是：`media.transcribe_failed`（HTTP 403）→ `media.prepared {"kind": "voice", "transcribed": false}`。
- 原因（读代码确认）：
  - 正在跑的 voice-bridge 是旧系统 `~/.claude/voice-bridge/server_http.py`，只读白名单里的目录，文件不在白名单里就返回 403；
  - 新网关把语音存在 `~/.dsh-bot/bots/bot5/media/`，不在它的白名单里；
  - 新仓库里的 `voice-bridge/server_http.py` 已经把 `~/.dsh-bot` 加进了白名单，但正在跑的不是这一份。
- voice-bridge 不记录每个请求的状态码，日志里查不到这几次 403，结论以代码为准。
- 用户起初以为语音都收到了（反馈“可以收发语音”），其实 bot 并不知道语音说了什么（问题 1）。

### b. 发语音：不符合（和旧系统的做法不一样）

- 用户让 bot 用语音回复后，出现 `tool.reply`（带 `voice: true`）和 5 次 `send.voice`（全部 `ok: true`），用户收到了能播放的语音。没有出现 `tool.reply_voice_unconfigured` 或 `media.synthesize_failed`。
- 用户指出：旧系统里她发的是**日语**语音，并且附中文文字对照；现在发的是**中文**语音，也没有文字（问题 2）。

### c. 收图片：符合

`media.prepared {"kind": "photo"}`，没有出现 `prompt.images_unsupported`。模型能看图，用户确认 bot 说得出图里有什么。

### d. 自拍：符合（能出图），但有问题

- 7 次 `tool.generate_image` 全部 `ok: true`。用户收到了图。
- 用户说明：除了要求的那张自拍，其余是她在聊天中自己决定生成、发来的。本机 AI 起初把“生图好了 → 又生图”的日志看成了死循环，经用户纠正，不是循环。
- 每次生图都超过 45 秒：那一轮先等满 45 秒，再回一句，图好了以后用系统消息通知，再开一轮发图。
- 发现的问题见第六节问题 3、4、5：
  - 同一张图发了两遍；
  - 发一张图要 26–45 秒；
  - 每次生图让那一轮多等 45 秒。

### e. 网页评论：符合

- 用户在 8767 的网页上评论了 bot5 的一条圈：
  - 15:46:23 收到一条来源为 `moment_reply` 的合成消息；
  - 15:46:31 出现 `tool.moments {"action": "reply", "code": 0}`。
- 用户刷新网页能看到 bot5 的回复。
- 朋友圈库的计数（只出数字）：
  - 近 1 小时评论数从 0 变成 2，一条是用户的评论，一条是 bot5 的回复；
  - bot5 的评论从 1 条变成 2 条。

### f. 发朋友圈：不符合

- 15:48:06–15:48:28 出现 3 次 `tool.moments {"action": "post", "code": 3}`，bot 告诉用户发不了。bot5 在库里的圈数前后都是 13 条。
- 原因（读代码，并查了作息状态的类别，没有看活动名）：
  - 当时作息状态是 `sleeping`；
  - `moments/post.py` 的 `should_post_moment` 第一条就是“睡觉不发圈”；
  - 用户主动要求时，`bot_post_moment.py` 只放开了“距离上次聊天太近”和“每天上限”，没放开睡觉这一条。
- 脚本退出时打印的是“没生成（LLM 返回空 / 触发独白闸 / 无素材）”，没有说出真正的原因（问题 6）。

### g. 管理台换模型：符合

- 页面上有 bot5，显示当前模型。
- 16:07:57 换成 `deepseek-v4-pro`：`brain.override {"provider": "deepseek-official", "model": "deepseek-v4-pro", "from_config": false}`。
- 用户接着发了一条消息：`brain.applied` 显示 v4-pro，26 秒后发出 3 段，正常回复。
- 16:09:37 选“配置文件里的”：`brain.override {"model": "deepseek-flash", "from_config": true}`，换回来了。

### h. 电话：没做

用户选择不做，旧电话服务（8766）没有动。

## 五、第 6 步：收尾

1. 网关停止前（16:09:55 UTC）跑了三条命令。输出原样照录，只把本机主目录写成 `~`、聊天 id 写成 `<chat>`：

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
  Telegram 最近一次拉取成功：1 秒前
  dsh：运行中（pid 32884，第 1 次启动）
  模型：deepseek-official / deepseek-flash（思考 low）
  正在进行的回合：1
  聊天 <chat>：最老一条未处理消息等了 13 秒，这一轮 11 秒没有进展

$ bun gateway/scripts/report.ts --config ~/.dsh-bot/configs/bot5.yml --hours 3
# bot5 运行报告（最近 3 小时）

只含数字，不含聊天内容、用户 id 和密钥。

## 收到的消息
- 按状态：done 13，in_turn 1
- 按类型：synthetic 7，user 7
- 现在还没处理完的：1

## 回合
- 按结局：ok 12，sent 1
- 按类型：message 10，nudge 2，summary 1（retry = 报错后的补救，nudge = 没回也没选择沉默时的提醒）
- 选择不回复（stay_silent）：3
- 每轮上下文大小（dsh 回报，token）：中位数 32072，最大 36409，共 12 轮

## 发出去的消息
- 按状态：photo/sent 8，text/sent 26
- 同一轮里同一句话发了不止一次：0

## 会话（段）
- 按状态：active 1（closed/budget = 用量到线换段，closed/pre-budget = 新消息太长先换段，closed/clear = /clear，closed/prompt-changed = 人设、运行规则或 dsh 版本变了）
- 换段 1 次：摘要在旧会话里写成 1 次，用账本补写 0 次，没写成（沿用上一份）0 次；摘要平均 729 字

## 承诺
- 按状态：无（source：tool = 模型自己登记，auto = 网关从说过的话里补登记）
- 时间说得含糊、提醒模型自己登记：0
- 合成消息（程序塞给模型的）：image 6，moment_reply 1

## 长期记忆
- 新记下的条数（remember）：1

## 日志里有没有机密
- 检查了 2 个日志文件、2 个机密值：没有发现
```

   - 跑命令时有一轮正在进行，所以 `health` 显示“正在进行的回合：1”。
   - 报告里的“发出去的图片 8 条”是当时的数，停网关前后又发了 2 条，合计 10 条。
   - “同一轮里同一句话发了不止一次：0”只统计文字，同一张图发两遍不算在内（问题 3）。
2. 按 Ctrl+C 停网关和 8767 的网页：
   - 网页立刻停了。
   - 网关 16:11:18 记 `gateway.stopping`，同时把正在进行的第 69 轮记为 `cancelled`，`dsh.exited` 记为 `"unexpected": true`（error 级）。
   - 但这一轮里已经开始的 `reply` 还在继续发，两张图分别在 16:11:55 和 16:12:29 发出，之后才记 `gateway.stopped`，前后 71 秒（问题 7）。
   - 之后网关进程不在了，17950、8767 都已释放。
3. 经用户同意恢复旧 bot5：执行 `bash ~/.claude/dispatcher/start-bot.sh <bot5 旧名>`，退出码 0。
   - 停用标记删掉了；四个旧 bot 的调度器都在，17801–17804 都在监听；
   - 旧网页 8765、旧电话 8766、voice-bridge 7788、provider-proxy 8770 都在监听。
   - 这一版重启脚本列出了 24 个旧系统定时任务。
4. 第 3 步加的配置保留。

## 六、发现的问题

**1. 收语音转写全部 403（a 项）**
- 复现：
  1. 新网关用旧系统正在跑的 voice-bridge（`~/.claude/voice-bridge/server_http.py`，7788 端口）；
  2. 给 bot 发一条语音。
- 现象：`media.transcribe_failed`（403）→ `media.prepared {"kind": "voice", "transcribed": false}`。
- 原因：旧 voice-bridge 的读取白名单里没有 `~/.dsh-bot`。新仓库的 voice-bridge 已经加上了，但切换前正在跑的仍是旧的。
- 另外：转写失败后 bot 仍照常回复，用户以为它听懂了。
- 建议：
  - 切换期让网关把语音放到旧白名单里的目录，或者给旧 bridge 设 `VOICE_BRIDGE_ALLOWED_ROOTS`（这要改旧服务，需用户同意）；
  - 或者把新 bridge 的上线时间提前，不等 M6；
  - 转写失败时，在给模型的提示里要求它告诉对方没听清。

**2. 发语音是中文、没有中文对照，和旧系统不一样（b 项）**
- 现象：开了语音后，每段中文直接合成语音，只发语音，不发文字，也不带附言。
- 旧系统的做法：
  - 旧 `reply` 工具有 `voice_text` 参数（朗读稿）；
  - 人设里的语音规则是“中文气泡 + 日文语音”，`text` 写中文，`voice_text` 写日语；
  - 每段先发中文文字，再发日语语音。
- 新网关的 `reply` 只有 `as_voice`、`voice_emotion`、`voice_instruct`，没有 `voice_text`。见 `gateway/src/engine/engine.ts` 的 reply 定义和 `gateway/src/telegram/sender.ts` 的语音分支。
- 建议：补上 `voice_text`。有朗读稿时，先发中文文字，再发朗读稿合成的语音；没有朗读稿时保持现在的做法。人设里的语音规则不用改。

**3. 同一张图发了两遍（d 项）**
- 现象：发出 10 条图片消息，只对应 6 个文件，4 个各发了两遍。第 60、65 两轮都是这样：

| 轮 | 第一次 reply 开始 | 第二次 reply 开始 | 结果 |
|---|---|---|---|
| 60 | 15:44:20 | 15:45:24（64 秒后） | 2 张图各发两遍；15:46:25 又来第三次，重复的 2 段文字全部被拦（`tool.reply_duplicate_blocked`） |
| 65 | 15:51:00 | 15:52:05（65 秒后） | 2 张图各发两遍；第二次里重复的 3 段文字被拦 |

- 原因：
  - dsh 等一次工具调用最多 60 秒（`@deepseek-ai/dsh-mcp-client` 的 `toolCallTimeoutMs` 默认 60000）。网关通过 ACP 的 `mcpServers` 把自己的 MCP 服务交给 dsh 时，没有设这个值。
  - 一次带两张图的 `reply` 光发图就要 60–75 秒，超过上限，dsh 判超时，模型以为没发出去，再调一次 `reply`。
  - 网关这边第一次其实还在发。网关只拦重复的文字，不拦重复的文件，所以图片发了两遍。
  - 同样的原因，日志里出现了“这一轮已经 `turn.end`，`tool.reply` 才完成”的情况。
- 建议：
  - 同一轮里同一个文件只发一次；
  - `reply` 收下就立刻返回，在后台发送；或者把工具调用的上限调大（看 ACP 的 `mcpServers` 能不能带这个设置）；
  - 和问题 4 一起解决最好。

**4. 发图很慢**
- 现象：
  - 一张 1.77–1.99 MB 的 PNG，从开始发到发出要 26–45 秒。
  - 对照：旧 voice-bridge 的日志里，旧系统发一张 1.75 MB 的图用了 4 秒（`Telegram sendPhoto OK … 1751763 bytes, 4030ms`）。
- 已查：
  - 网关发文件时把整个请求体拼好再用 `fetch` 一次发出，没有重试；
  - 网关进程和 voice-bridge 进程里都带着同样的 `http_proxy`、`https_proxy`（都指向本机的代理端口）。
  - 所以不只是“走代理”的问题，怀疑和 bun 经 HTTP 代理上传大文件有关。本机没有再做实验。
- 建议：
  - 发照片前转成 JPEG（Telegram 本来也会压缩），体积能小很多；
  - 对比一下 bun 直连和走代理时的上传速度。

**5. 回复慢，大头在生图**
- 现象：正常结束的 10 轮回复平均 74.6 秒，最长 139.2 秒。
- 拆开看：
  - 每次生图，`generate_image` 都要先等满 45 秒，这一轮才往下走。7 次生图全都超过 45 秒，所以每次都白等 45 秒。
  - 发图每张 26–45 秒（问题 4）。
  - 语音逐段合成，每段约 6–9 秒。
  - 发圈失败重试了 3 次（问题 6）。
- 建议：同步等待缩短到 10 秒左右，或者生图一律在后台跑，模型先回话。

**6. 发朋友圈被“睡觉”挡住，失败原因报错了（f 项）**
- 复现：在作息状态为 `sleeping` 的时段，用户在聊天里让 bot 发圈。
- 现象：3 次 `tool.moments {"action": "post", "code": 3}`，库里没有新圈。
- 原因：
  - `should_post_moment` 第一条：`sleeping` → 不发；
  - `bot_post_moment.py` 为“用户主动要求”放开了静默期和每日上限，没放开这一条；
  - 退出时打印的是“没生成（LLM 返回空 / 触发独白闸 / 无素材）”，和真实原因不符；
  - 模型拿到失败后又试了两次。
- 用户意见：她正在跟我聊天，状态就不应该是“睡觉”。
- 建议：
  - 对方正在聊天、或者明确要求时，按醒着处理，至少用户主动要求的发圈不受睡觉这条限制；
  - 失败时把真实原因（比如 `moment skip: sleeping`）返回给模型和日志。

**7. 停网关时，正在进行的那一轮还会继续发 71 秒**
- 复现：bot 正在发带图的回复时按 Ctrl+C。
- 现象：
  - `gateway.stopping` 后，这一轮立刻记为 `cancelled`（`delivered: 2`），`dsh.exited` 记为 `"unexpected": true`（error 级）；
  - 但已经开始的 `reply` 继续发完两张图（16:11:55、16:12:29），才记 `gateway.stopped`；
  - 用户在“停了”之后还会收到消息。
- 建议：
  - 停止时要么尽快停止发送，要么把“正在把这一轮发完”写进日志；
  - 网关自己停 dsh 时，不应记成意外退出。

**8. 文档前后不一致**
- `docs/dsh-migration/M4.md` 第三节说新网页用 8766 端口，任务书用 8767。8766 被旧电话服务占着，所以这次用的是 8767。

## 七、用户的意见

- **作息**：在跟她聊天时，她的状态就不应该是“睡觉”（见问题 6）。
- **语音**：要和旧系统一样，发日语语音并附中文文字对照（见问题 2）。
- **实时看日志**：旧系统可以用 `tmux attach` 看实时日志。这次本机 AI 先给了临时办法：
  - 网关开在终端面板的标签页里，点开就能看到实时事件；
  - 也可以用 `tail -f ~/.dsh-bot/bots/bot5/logs/gateway.log`，配合 `jq` 整理成一行一条；
  - 想用 tmux 的话，下次把网关放进 `tmux new -d -s dsh-bot5 '…'` 里启动，再 `tmux attach -t dsh-bot5`。

  建议 M6 做开机自启时一并提供这种看法，最好有个现成的命令把事件格式化输出。
- **生图**：用户确认，bot 在聊天中自己生图、发图是她的正常行为，不是死循环。
