# M3 真机验收（bot5：承诺、主动消息、心情与好感）

- 日期：2026-10-06（日志时间为 UTC 04:59–05:43，北京时间 12:59–13:43）
- 代码：分支 `claude/nice-brahmagupta-dtjsma`，提交 `f11f556`
- 执行方式：本机 AI 按 `docs/dsh-migration/LOCAL-AGENT-M3.md` 操作，用户在 Telegram 上配合
- 本文不含以下内容：
  - 密钥；
  - 聊天内容、记忆内容、人设和作息内容（包括活动名）；
  - 关系数值；
  - Telegram 用户 id 和聊天 id；
  - bot 真名（旧 bot5 的名字写成 `<bot5 旧名>`）。

## 结论

| 项 | 结论 |
|---|---|
| 1. 自动化测试 | 网关 198 项：通过 193，跳过 5，失败 0；真 dsh 5 项全部通过；Python 27 项全部通过；类型检查通过 |
| 3. 接上旧的作息设置 | **符合**。`check` 显示全部就绪；作息查得到，键名齐全，起床 3 次 |
| a. 生活状态 | **符合** |
| b. 承诺 | **符合**。用户把时间改成了 1 分钟。模型自己登记，到点提醒，随后判为兑现 |
| c. 承诺列表 | **符合**（用户判断答对） |
| d. 主动消息 | **符合**。两次都按原因跳过，输出格式对。但“投到网关、bot 主动开口”这条路这次在真机上没走到 |
| e. 积温情绪 | **不符合**。`tick.py` 读不到情绪设置，直接跳过，关系数值没更新（问题 1） |
| f. 被晾追问 | **符合**。触发 2 次，都是追问 |
| g. 睡觉顺延 | 没做（用户选择不做） |
| h. 报告 | **符合** |
| 补测 e 后半段 | 读聊天记录、调 DeepSeek、算出变化都正常。但关系数值写进了旧系统目录，新建了一个 bot5 目录（问题 2）。已经停下告知用户，经同意删除 |
| 新发现 | 网关把 bot 复述的旧承诺当成新许诺，补登记了一条重复的（问题 3） |

## 一、环境

| 项 | 值 |
|---|---|
| 系统 | macOS 15.7.3（Darwin 24.6.0），Apple Silicon arm64 |
| bun | 1.3.11 |
| node / npm | v25.9.0 / 11.12.1 |
| python3 / pytest | 3.10.11 / 9.0.2 |
| dsh | 0.2.0-rc.2（M1 时装在 `~/.dsh-bot/harness`；`harness/` 的锁文件没有变化，没有重装） |

## 二、第 1 步：自动化测试（不需要密钥）

| 命令 | 结果 |
|---|---|
| `cd gateway && bun install` | 依赖没有变化 |
| `bun run typecheck` | 通过 |
| `bun run test` | 17 个文件 198 项：**通过 193，跳过 5，失败 0**，574 次断言，49.4 秒。跳过的 5 项是 `real-dsh.test.ts`（下一行单独跑） |
| `DSH_BOT_HARNESS=~/.dsh-bot/harness bun test --timeout 90000 test/e2e/real-dsh.test.ts` | **5 项通过，0 失败**，22 次断言，3.5 秒 |
| `python3 -m pytest -q unit/test_chat_history_ledger.py unit/test_chat_history_namespace.py unit/test_project_slug.py unit/test_m3_life.py` | **27 项全部通过**，0.72 秒 |

## 三、第 2–3 步：停旧 bot5、接上旧的作息设置

**第 2 步：停旧 bot5（经用户同意）**
- 执行 `~/.claude/dispatcher/stop-bot.sh <bot5 旧名>`。输出：已停用，写了停用标记，杀掉 2 个 tmux 会话，二次清理 0 个。
- 之后的状态：
  - bot5 的调度器和工作会话都没了，17801 不再监听；
  - 没有残留进程还在用 bot5 的旧频道目录；
  - 其它三个旧 bot 不受影响。

**第 3 步：让新配置读到旧的作息设置**
- 旧配置文件是 `~/claudebotlife/configs/<bot5 旧名>.yml`。
  - 用 `ls` 确认文件在；
  - 用 `grep -c` 确认里面的 `bot_channel_path` 指向旧 bot5 的频道目录，只出了计数；
  - 没有打开内容。
- 在 `~/.dsh-bot/configs/bot5.yml` 的 `dispatcher_port` 那行下面加了一行 `life_config: ~/claudebotlife/configs/<bot5 旧名>.yml`。文件里写的是完整路径。
- `setup.ts check`：全部就绪，输出见第五节。
- 作息检查（只打印键名和起床次数）：

```
['free_at', 'interruptible', 'name', 'state', 'term_label', 'wakes'] 3
```

键名里有 `name`、`state`、`wakes`、`free_at`，起床次数是 3。**符合**。

## 四、第 4–5 步：启动新网关、真机清单

本机 AI 读日志和账本时只看这些：
- `gateway.log` 里的事件名和数字、是否类字段（聊天 id 已替换）；
- 账本里的编号、状态、时间、来源、数量。

没有打开 `chat.log`，没有查账本正文、承诺内容和记忆，也没有看 `relationship.json` 的内容。几个脚本的输出都先经过过滤，只保留类别和分钟数。

**整体数字（UTC 05:18–05:43）**
- 用户发了 5 条消息。
- 程序塞给模型的系统消息 4 条：承诺到点 2 条，被晾追问 2 条。主动消息 0 条，没投出去。
- 网关一共跑了 11 轮，全部 `ok`：
  - 10 轮回复，其中 1 轮是提醒（nudge）；
  - 1 轮写摘要。
- bot 一共发出 28 段。
- 每轮上下文中位数 15465，最大 23418。
- 日志里没有任何警告或错误事件。

**偏离任务书**
1. 第 b 项用户说的是“1 分钟后提醒我……”，不是 5 分钟。
2. 可选项只做了 f，没做 g。
3. 经用户同意，加做了两项补测：
   - d 项：打开旧代码自带的跳过静默期开关，再跑一次；
   - e 项：临时补一个只有情绪设置的 `configs/_global.yml` 再跑一次，测完已删。

   见下面 d、e 两节。
4. 收尾时按用户的意思，没有恢复旧 bot5。

### a. 生活状态：符合

- 第一条消息（05:18:57）进来后，依次出现：
  - `segment.prompt_changed {"segment": 6}`
  - `segment.roll_start {"reason": "prompt-changed", "segment": 6, "used": 13371, "budget": 800000}`
  - `segment.rolled {"reason": "prompt-changed", "summary": "in-session", "summary_chars": 550}`
  - `segment.created {"segment": 7}`
  - `segment.seed {"chars": 5901, "entries": 4, "memory_chars": 5087, "recent_cap": 12000}`

  和任务书说的一样：运行规则加了承诺那一句，所以换了一次段。
- 05:19:05 出现 `life.lines {"relationship": true, "situation": true, "late": false}`，这一轮在 05:19:18 发完 4 段。
- 之后用户又发了 4 条消息，都没有再出现 `life.lines`。符合“没变化就不带”。

### b. 承诺：符合（时间改成了 1 分钟）

- 05:19:21 `commitment.created {"id": 1, "source": "tool", "how": "relative", "due_in_min": 1}`：模型自己登记的。账本里约定的时间是 05:20:21，登记后 59 秒。
- 05:20:41 `commitment.fire {"id": 1, "attempt": 1, "late_min": 0}`：比约定时间晚 20 秒，因为网关每 30 秒查一次。这一轮发了 3 段，用户确认收到了提醒。
- 05:21:11 `commitment.done {"id": 1, "attempts": 1}`。
- 从登记到提醒隔了 1 分 20 秒。

### c. 承诺列表：符合

- 用户在提醒到点之前（05:19:26）问了“你答应过我什么”，判断回答是对的。
- 注：`commitment_list` 只列还没兑现的承诺，所以要在到点之前问，这一项才有意义。
- 副作用：这一轮 bot 的回答里复述了那句“1 分钟后提醒”，网关当成新许诺，又补登记了一条（问题 3）。

### d. 主动消息：符合；但投递到网关这条路没走到

| 时间（UTC） | 命令 | 输出 |
|---|---|---|
| 05:30:25 | 任务书的命令，带 `--force` | `skip: <类别：刚聊过（非静默期）>；下次机会在 41 分钟后` |
| 紧接着 | 同一命令，不带 `--force` | `skip: 还没到下次机会（还有 41 分钟）` |
| 05:38:25 | 补测（经用户同意）：加 `CLAUDEBOTLIFE_BYPASS_SILENCE=1` 再带 `--force` | `skip: <类别：其他>；下次机会在 96 分钟后` |

- **第一次跳过**：用户 20 秒前刚给 bot 发过消息。静默期默认是 60 分钟，所以按“刚聊过”跳过，30–60 分钟后再试。和 M3.md 写的一致。
- **补测时跳过**：`CLAUDEBOTLIFE_BYPASS_SILENCE=1` 是旧代码自带的开关，会跳过静默期这类硬规则。
  - 新仓库 `state.db` 的心跳记录显示，这次是模型自己判断“现在不说”：`skip_reason` 以 `action=skip:` 开头，原因原文没有看。
  - 当时 bot 2 分钟前刚追问过一次。
  - 下次机会落在“没素材、不想说”那一档（1–2 小时）。
- 所以“投到网关、bot 主动开口”这条路，这次在真机上没走到。自动化测试里两端各有覆盖，但两边连起来没有在真机上验证过：
  - 网关端：`gateway/test/e2e/control.test.ts`；
  - Python 端：`tests/unit/test_m3_life.py`，用的是假接口。
- 三次运行前后，旧系统 `~/.claude/dispatcher/.self-initiate-state` 和 `.jiwen-state` 里都没有新增带 bot5 名字的文件。

### e. 积温情绪：不符合

**按任务书运行**

```
Oct  6 01:20:06 2026
exit=0
Oct  6 01:20:06 2026
```

修改时间没有变。原因是我先读了代码，再用过滤后的输出确认：`tick.py` 只打印一行“`_global.yml.jiwen` 未配置，跳过”就退出，退出码是 0（问题 1）。

**补测（经用户同意）**
- 做法：临时建了 `configs/_global.yml`。这个文件在 git 忽略名单里，只有三项设置：
  - `jiwen.enabled: true`
  - `jiwen.bots: [bot5]`
  - `jiwen.state_dir`：指到本次会话的临时目录
- 运行同一条命令，输出按类别过滤后是：

```
[jiwen.tick] 开始（bot 列表和状态目录已隐藏）
[jiwen.tick] bot5:
    delta applied（调了 DeepSeek 并应用，数值已隐藏）
    关系数值已重算并保存（数值已隐藏）
    状态已保存（路径已隐藏）
[其它输出 0 行，已隐藏]
exit=0
```

- 但新系统的 `relationship.json` 修改时间仍然是 01:20:06。
- 关系数值实际写进了 `~/.claude/channels/bot5/relationship.json`（625 字节）。也就是说，它在旧系统的目录里新建了一个 bot5 目录（问题 2）。
- 处理：
  - 发现后先停下告知用户。补测前我只查了入口脚本和它直接调用的两个脚本的写入位置，漏查了它引用的 `relationship.py`。
  - 经用户同意，删掉了这个文件和空目录，临时 `_global.yml` 也删了。
  - 删除后，`~/.claude/channels/` 下的目录和测试开始时一样。
  - 旧系统其它目录里，没有新增带 bot5 名字的文件。
- 结论：前半段是通的，读聊天记录、调 DeepSeek、算出变化都正常；写回的位置错了。

### f. 被晾追问：符合（触发了 2 次）

| 时间（UTC） | 事件 |
|---|---|
| 05:21:12 | bot 回完话，`hang_arm chat=<chat> stage=0` |
| 05:27:41 | 沉默 6.5 分钟后，`hang {"detail": "hang_followup chat=<chat> stage=1"}`，这一轮发了 3 段，用户收到追问 |
| 05:30:05 | 用户回话，`hang_cancel chat=<chat> reason=inbound` |
| 05:30:22 | bot 回完话，再次 `hang_arm chat=<chat> stage=0` |
| 05:36:41 | 沉默 6.3 分钟后，第 2 次 `hang_followup chat=<chat> stage=1`，发了 3 段 |

- 两次都判定“此刻可以打扰”，所以是追问，没有出现 `hang_archive`。
- 每天最多追 3 次。用户对追问频率的意见见第七节。

### g. 睡觉顺延：没做

用户选择不做（要过夜）。

### h. 报告：符合

`report.ts --hours 3` 有“## 承诺”一节，完整输出见第五节：
- `cancelled/auto 1，done/tool 1`：
  - `done/tool` 是第 b 项那条；
  - `cancelled/auto` 是问题 3 里网关补登记、模型到点后自己取消的那条。
- 合成消息：`commitment 2，hang 2`。

## 五、第 6 步：收尾

1. 网关停止前（05:43 UTC）跑了三条命令。输出原样照录，只把本机主目录写成了 `~`：

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
  Telegram 最近一次拉取成功：9 秒前
  dsh：运行中（pid 79734，第 1 次启动）
  模型：deepseek-official / deepseek-flash（思考 low）
  正在进行的回合：0

$ bun gateway/scripts/report.ts --config ~/.dsh-bot/configs/bot5.yml --hours 3
# bot5 运行报告（最近 3 小时）

只含数字，不含聊天内容、用户 id 和密钥。

## 收到的消息
- 按状态：done 9
- 按类型：synthetic 4，user 5
- 现在还没处理完的：0

## 回合
- 按结局：ok 11
- 按类型：message 9，nudge 1，summary 1（retry = 报错后的补救，nudge = 没回也没选择沉默时的提醒）
- 选择不回复（stay_silent）：1
- 每轮上下文大小（dsh 回报，token）：中位数 15465，最大 23418，共 11 轮

## 发出去的消息
- 按状态：text/sent 28
- 同一轮里同一句话发了不止一次：0

## 会话（段）
- 按状态：active 1（closed/budget = 用量到线换段，closed/pre-budget = 新消息太长先换段，closed/clear = /clear，closed/prompt-changed = 人设、运行规则或 dsh 版本变了）
- 换段 1 次：摘要在旧会话里写成 1 次，用账本补写 0 次，没写成（沿用上一份）0 次；摘要平均 550 字

## 承诺
- 按状态：cancelled/auto 1，done/tool 1（source：tool = 模型自己登记，auto = 网关从说过的话里补登记）
- 时间说得含糊、提醒模型自己登记：0
- 合成消息（程序塞给模型的）：commitment 2，hang 2

## 长期记忆
- 新记下的条数（remember）：0

## 日志里有没有机密
- 检查了 2 个日志文件、2 个机密值：没有发现
```

2. 按 Ctrl+C 停网关：
   - `gateway.stopping` → `dsh.exited {"code": 130, "unexpected": false}` → `gateway.stopped`，在同一秒内完成；
   - 之后网关和 dsh 进程都不在了，17950 端口已释放。
3. 恢复旧 bot5：用户选择**先不恢复**。
   - 现在旧 bot5 仍是停用状态：停用标记在，17801 不监听；其它三个旧 bot 正常。
   - 恢复命令是 `bash ~/.claude/dispatcher/start-bot.sh <bot5 旧名>`，会把 4 个旧 bot 全部重启一遍。
4. `life_config` 那一行保留。

## 六、发现的问题

**1. 积温情绪读不到设置，悄悄跳过（e 项）**
- 复现：运行 `HUB_CONFIGS_DIR=~/.dsh-bot/configs python3 jiwen/tick.py; echo $?`。
- 现象：
  - 输出里只有一行“`[jiwen.tick] _global.yml.jiwen 未配置，跳过`”；
  - 退出码是 0；
  - 关系数值没有更新。
- 原因（只读代码得出）：
  - `load_jiwen_config()` 调 `config_loader.load_global()`。后者读的是写死的 `<仓库>/configs/_global.yml`（`GLOBAL_CFG_PATH`），不看 `HUB_CONFIGS_DIR`。
  - 新仓库里没有这个文件，旧系统那份在旧仓库里。
  - `life_config` 只合并进 `load_bot()` 的结果，对 `load_global()` 不起作用。
- 就算补上 `_global.yml`，还有两处默认值指向旧系统：
  - 没写 `jiwen.bots` 时，bot 列表是扫 `~/.claude/channels/` 下带 `access.json` 的目录，扫不到新系统的 bot；
  - 没写 `jiwen.state_dir` 时，状态写进 `~/.claude/dispatcher/.jiwen-state`，这是旧系统的目录。`life-context.py` 里的 `_write_back_activity` 也默认写这里。
- 跳过时退出码是 0，定时任务和人工检查都看不出失败。
- 建议：
  - 新系统的 bot 从新配置目录（或 `~/.dsh-bot/`）读情绪设置；
  - bot 列表和状态目录按新系统的目录给默认值；
  - 没有设置时返回非 0，或者至少写一条警告。

**2. 关系数值写回的位置写死在旧系统目录（e 项补测时发现，已清理）**
- 复现：
  1. 在新仓库建 `configs/_global.yml`，写上 `jiwen: {enabled: true, bots: [bot5], state_dir: <临时目录>}`；
  2. 运行 `HUB_CONFIGS_DIR=~/.dsh-bot/configs python3 jiwen/tick.py`。
- 现象：
  - 输出显示调了 DeepSeek、关系数值已保存；
  - 但 `~/.dsh-bot/bots/bot5/channel/relationship.json` 没有变；
  - 旧系统里多出了目录 `~/.claude/channels/bot5/` 和里面的 `relationship.json`（625 字节）。
- 原因：
  - `jiwen/tick.py` 里写的是 `_bot_dir = bot_id`，注释说“bot_id 即 channels 下的目录名”；
  - `relationship.py` 的 `_path()` 写死成 `~/.claude/channels/<bot_dir>/relationship.json`。
  - 读聊天记录那一步已经改成按 `bot_channel_path` 找账本，读写关系数值这一步没有改。
- 影响：
  - 新系统 bot 的关系数值永远不会被积温情绪更新。网关只读 `<频道目录>/relationship.json`，所以发给模型的关系提示也永远不变。
  - 积温情绪会在旧系统目录里另起一份关系数值，从默认值开始，以后一直读写那一份。
  - 如果新系统里的 bot 名和旧系统某个 bot 的目录名相同（比如以后迁 bot2、bot3、bot4 时沿用原名），新旧两边会读写同一个旧文件，数值会互相覆盖。
- 已处理：经用户同意，删掉了误建的文件和空目录；临时 `_global.yml` 也已删除。
- 建议：
  - `relationship.load/save` 改成接收频道目录（`bot_channel_path`）；
  - 新系统的 bot 绝不回落到 `~/.claude/channels`；
  - 补一个测试：对新系统的 bot 跑完 tick 后，`~/.claude/channels` 下不应该多出任何东西。

**3. 网关把 bot 复述的旧承诺当成新许诺，补登记了一条重复的**
- 复现：
  1. 让 bot 登记“1 分钟后提醒我……”；
  2. 在到点之前问它“你答应过我什么”。
- 现象：
  - 05:19:21 `commitment.created {"id": 1, "source": "tool"}`；
  - 05:19:42，也就是回答“答应过什么”的那一轮结束时，出现 `commitment.created {"id": 2, "source": "auto", "how": "relative", "due_in_min": 1}`。网关从这一轮发出的话里认出了“1 分钟后提醒”，又登记了一条，时间从复述的那一刻重新算；
  - 05:21:41 `commitment.fire {"id": 2, "late_min": 1}` → 05:21:42 `commitment.cancelled {"id": 2}` → 第 50 轮 `delivered: 0`，没调 reply 也没调 stay_silent → 第 51 轮 `nudge` → `tool.stay_silent`。
- 结果：用户没收到重复提醒，但多跑了 2 轮模型。要是模型这次没有取消，用户会再收到一次提醒。
- 建议：
  - 补登记之前，先和还没兑现的承诺比一比，时间接近或内容相近就不登记；
  - 这一轮调用过 `commitment_list` 时，不做兜底识别；
  - 到点时模型取消了承诺，允许这一轮直接结束，不再触发 nudge。

**4. 被晾追问比较频繁（对设计的反馈，不是故障）**
- 现象：16 分钟里追问了 2 次。只要 bot 回完话后用户 5–9 分钟没回，就会被追问一次，每天最多 3 次。
- 用户的意见见第七节。

**5. 主动消息投递这条路没在真机上测到（测试没覆盖到）**
- 现象：三次运行都被跳过：
  - 静默期（60 分钟）规则跳过一次；
  - 还没到下次机会，跳过一次；
  - 模型判断不说，跳过一次。
- 建议二选一：
  - 任务书里写明，测 d 项之前用户要先 60 分钟以上不和 bot 聊天；
  - 给 `self_initiate.py` 加一个只在测试时用的“直接投递”开关，跳过模型判断，把默认文本投给网关。

## 七、用户的建议

- **追问间隔应该看关系。** 黏人的可以追得快，不黏人的可以等久一点。
  - 现在的间隔是固定的：第一次 7±2 分钟，第二次 25±5 分钟，第三次 90±15 分钟。
  - 在“写进配置的固定档位”“按关系数值自动调”“两者都要”三种做法里，用户选的是**按关系数值自动调**：好感、亲密度越高，追得越快；关系一般就等久一点。
- 本机 AI 补充两点，供方案参考：
  - 追几次、每天最多追几次，要不要也跟着关系一起变；
  - 这条要等问题 2 修好、关系数值真的会更新以后才有意义。
