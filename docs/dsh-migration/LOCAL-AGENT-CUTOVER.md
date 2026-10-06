# 本机 AI 任务书：正式切换（人设里的旧技术说明、逐个迁移、开机自启、回滚）

你在用户的 Mac 上工作，接着 M6 的活。M1–M6 已经全部合进 master，这份任务书在分支 `claude/nice-brahmagupta-dtjsma` 上，代码和 master 相同。

这次是**正式切换**：把用户选定的 bot 一个一个从旧系统搬到新系统，设成开机自启，出问题能退回去。

## 先读

- `docs/dsh-migration/CUTOVER.md`：切换、回滚、故障排查、dsh 升级。本任务书的每一步都对应它
- `docs/dsh-migration/M4.md` 第二节：生图、朋友圈、语音要准备的配置
- `docs/dsh-migration/M5.md` 第二节：群聊（导演）怎么开
- `docs/dsh-migration/M6.md`：导入旧记录、开机自启

## 铁律

违反任何一条就停下来问用户。

1. **每个会改变现状的动作，先告诉用户要做什么，用户同意再做。** 包括：停旧 bot、改人设、正式导入、设开机自启、停旧系统的共用服务、回滚。
2. **人设只改旧系统的技术说明，角色设定一个字不动。**
   - 角色设定包括性格、语气、称呼、经历、关系、作息、喜好、说话方式等等，看起来"可以改进"的也不动。
   - 只改新副本 `~/.dsh-bot/bots/<名>/channel/CLAUDE.md`。旧目录 `~/.claude/channels/<旧名>/CLAUDE.md` 绝不碰，回滚靠它。
   - 改之前先备份。每一处改动都把"原文 → 改后"在对话里给用户看，用户同意才写入。
   - 人设内容不写进报告，不进 git。
3. **不读、不打印任何密钥和令牌。** 检查有没有配好，只用 `setup.ts check`。
4. **报告里不能有：** 密钥、聊天内容、人设内容、用户 id、聊天 id、群 id、bot 真名（用 bot5、botX、botY 这样的代号）、带系统用户名的路径（写成 `~`）。
5. **旧系统的代码和配置文件不改。** 旧的 launchd 任务只用 `launchctl bootout` 停，plist 文件不删，回滚要用。
6. **一个 bot 切完、抽查通过，再切下一个。**

## 第 0 步：准备

```bash
git fetch origin claude/nice-brahmagupta-dtjsma && git checkout claude/nice-brahmagupta-dtjsma && git pull
cd gateway && bun install && bun run typecheck && bun run test
DSH_BOT_HARNESS=~/.dsh-bot/harness bun test --timeout 90000 test/e2e/real-dsh.test.ts
cd ../tests && python3 -m pytest -q unit/test_chat_history_ledger.py unit/test_chat_history_namespace.py unit/test_project_slug.py unit/test_m3_life.py unit/test_m4_dsh.py unit/test_m5_director.py unit/test_m6_import.py
cd ..
```

预期：
- 网关 223 项（通过 218，跳过 5）；
- 真 dsh 5 项通过；
- Python 42 项通过。

然后问用户三件事：

1. 这次切哪几个 bot、按什么顺序。建议先切 bot5（M1 起一直在测），再一个一个来。
2. 用不用群聊（导演点名）。用的话，群里的 bot 全部切完后再开导演。
3. 旧系统里在用的 voice-bridge、provider-proxy 是否照旧留着。新系统也用它们，建议留着。

## 第 1 步：每个 bot 的切换

对每个 bot 按 1.1 → 1.7 做完，再做下一个。

### 1.1 停旧 bot（先问用户）

`bash ~/.claude/dispatcher/stop-bot.sh <旧名>`。用户在测试时已经停掉的，确认一下：停用标记在，它的旧端口不监听。

### 1.2 准备新 bot

**没迁过的 bot：**

1. `bun gateway/scripts/setup.ts bot <新名> --from ~/.claude/channels/<旧名>`。
2. 在 `~/.dsh-bot/configs/<新名>.yml` 里补上：
   - `display_name`（问用户）；
   - `life_config: <旧配置文件的完整路径>`；
   - `gateway:` 下面的 `image_skill_dir`、`botlife_db`。见 M4.md 第二节，和 bot5 一样。
3. 新频道的 `access.json` 里有没有音色，用 M4 任务书第 3 步的脚本检查、复制。

**迁过的 bot（比如 bot5）：** 迁移以后用户可能在旧系统里又改过人设。只比较新旧两份人设是否相同，只出"相同"或"不同"：

```bash
[ "$(shasum -a 256 < ~/.claude/channels/<旧名>/CLAUDE.md)" = "$(shasum -a 256 < ~/.dsh-bot/bots/<新名>/channel/CLAUDE.md)" ] && echo 相同 || echo 不同
```

不同的话，问用户以哪一份为准。要用旧的那份，就先备份新副本，再把旧的复制过来。

最后运行 `bun gateway/scripts/setup.ts check ~/.dsh-bot/configs/<新名>.yml`，要"全部就绪"。

### 1.3 人设里的旧技术说明

先只数一数（不显示内容）：

```bash
grep -c -E 'Bash|python3|telegram-worker|novelai-skill|comfyui-skill|intermediate\.json|download_attachment|add_group_alias|MEDIA:|chat_id|\[self-initiate\]|\[director\]|【群聊】|【私聊】' ~/.dsh-bot/bots/<新名>/channel/CLAUDE.md
```

**是 0：** 这一步跳过。

**不是 0：**

1. 备份：
   ```bash
   mkdir -p ~/.dsh-bot/backup/<新名> && cp -p ~/.dsh-bot/bots/<新名>/channel/CLAUDE.md ~/.dsh-bot/backup/<新名>/CLAUDE.md.$(date +%Y%m%d-%H%M%S)
   ```
2. 打开新副本，找到命中的那几处，按下面的对照表起草改法。只改技术操作的那几句；同一段里描述角色的话保持原样。
3. 在对话里把每一处的"原文 → 改后"给用户看，用户逐处同意后再写入。用户说某处不改，就不改。
4. 写入后，用 `diff` 只数改了几行，不打印内容：`diff <备份> <新副本> | grep -c '^[<>]'`。
5. 再跑一次上面的计数命令。剩下的命中如果是角色设定里正常的用词，就保持原样；记在报告里，只写类别和次数。

**对照表：**

| 旧系统的写法 | 新系统里改成 |
|---|---|
| 用 Bash 跑 `bot_post_moment.py` 发朋友圈 | 用 `moments` 工具，`action=post`（可写 `topic`、`visibility`） |
| 用 Bash 跑 `moment_reply.py`、`moment_like.py`、`moment_set_image.py`、`moment_delete_comment.py` | `moments` 工具，`action` 分别是 `reply`、`like`、`set_image`、`delete_comment` |
| 用 Bash 跑 `recent_moments.py` 查朋友圈 | `moments` 工具，`action=recent`（`whose`、`days`） |
| 调用 novelai-skill 或 comfyui-skill、写 `intermediate.json`、跑 `generate_novelai_image.py` | 先用 `image_guide` 看写法，再用 `generate_image` 生成（参数 `intermediate`、`ratio`、`reuse_seed`），生成好的图用 `reply` 的 `files` 发 |
| `mcp__telegram-worker__reply`、`send_message` | `reply` 工具 |
| reply 的 `chat_id` 参数、跨聊天发消息 | 删掉：reply 只发到当前这个聊天 |
| `download_attachment` | 删掉：对方发的图片会自动附给你看，语音会自动转成文字 |
| `add_group_alias`、别名路由、"被 @ 才回"之类的群聊规则 | 删掉，或者改成"导演点到你时才在群里说话" |
| `MEDIA:` 行 | 删掉 |
| 以 `[self-initiate]` 开头的消息 | 改成以 `⟦系统·主动开口⟧` 开头的消息 |
| 以 `[director]` 开头的消息 | 改成以 `⟦群聊·导演点到你了⟧` 开头的消息 |
| 按【群聊】/【私聊】标注区分场景 | 新系统不加这个标注：以 `⟦群聊·导演点到你了⟧` 开头的是群聊，其余都是私聊 |

**这些新系统照旧支持，不用改：**
- reply 的 `text`、`files`、`reply_to`、`as_voice`、`voice_text`、`voice_emotion`、`voice_instruct`；
- `[[图1]]` 这类插图标记；
- 表情包用 `files` 发（`text` 可以为空）；
- `react`、`stay_silent`；
- 以 `[moment-interaction]` 开头的朋友圈通知；
- `/clear` 等命令。

人设改了以后，这个 bot 的每个聊天在下一条消息时会换一次会话（日志里有 `segment.prompt_changed`），这是正常的。

### 1.4 导入旧记录

1. 试算，只出条数：
   ```bash
   python3 scripts/import_history.py <新名> --from ~/.claude/channels/<旧名> --old-bot <旧名> --dry-run
   ```
2. 有 `.promises.json` 的，看结构：同一条命令把 `--dry-run` 换成 `--inspect-promises`。只打印字段名和类型。
   - 字段名里看不出哪个是内容、哪个是时间（脚本认的字段名见 `scripts/import_history.py` 的 `TEXT_KEYS`、`DUE_KEYS`），就停下来报告，不要猜。
3. 把条数告诉用户，用户同意后去掉 `--dry-run` 正式导入。导入前这个 bot 的网关要停着。
4. 脚本说"这个聊天在新系统里已经聊过"就是不导入（bot5 就是这种），属于正常，跳过。

### 1.5 开机自启

1. 这个 bot 以前用 tmux 或终端开着的新网关，先停掉（`tmux kill-session -t …` 或 Ctrl+C）。
2. 在**设了代理的终端**里运行：
   ```bash
   bun gateway/scripts/autostart.ts install ~/.dsh-bot/configs/<新名>.yml
   bun gateway/scripts/autostart.ts install-jobs ~/.dsh-bot/configs/<新名>.yml
   bun gateway/scripts/autostart.ts status
   ```
3. 预期：`com.dsh-bot.<新名>` 是 running；`com.dsh-bot.self-initiate.<新名>` 是定时任务，平时不在运行是正常的。

### 1.6 抽查

| 项 | 预期 |
|---|---|
| 用户给 bot 发一条消息 | 正常回复。`bun gateway/scripts/logs.ts --config ~/.dsh-bot/configs/<新名>.yml -n 30` 里有这一轮的 `turn.end`，没有 ✗ |
| 导入过旧记录的 | `report.ts --hours 1` 里有一行"用账本补写摘要：1 次"（就是用导入的记录写的）；bot 的回复接得上以前的话题（用户判断） |
| 改过人设的 | 出现一次 `segment.prompt_changed`；用户觉得说话方式没变（用户判断） |
| 请用户让 bot 发一张自拍、发一条朋友圈（可选） | `tool.generate_image`、`tool.moments` 成功 |
| `bun gateway/scripts/health.ts --config …` | 健康 |

抽查不通过的：
- 先按 CUTOVER.md 第四节的故障排查表查；
- 查不出来就按第 4 步把这个 bot 回滚，记下现象再继续；
- 不要自己改代码。

### 1.7 记录

记下这个 bot 的结果：
- 每一步是否完成；
- 人设改了几处、各属于对照表哪一类；
- 导入的条数；
- 开机自启的状态；
- 抽查结果。

然后做下一个 bot。

## 第 2 步：群聊（用户用导演群聊的话）

群里的 bot 全部切完以后：

1. 停旧的导演（先问用户）。用 `launchctl list | grep -i director` 找它，或者问用户它是怎么启动的。旧导演还在给旧 bot 点名，不停会两个导演一起点。
2. 按 M5 任务书第 4 步把群登记进每个 bot 的 `access.json`，群 id 只放在变量里，不打印。
3. 打开导演开关：`mkdir -p ~/.dsh-bot/director/mode && touch ~/.dsh-bot/director/mode/$G`。
4. 设导演开机自启：`bun gateway/scripts/autostart.ts install-director --chat $G`。
5. 抽查：用户在群里说一句，十几秒内有一个 bot 接话（M5 任务书第 6 步 a 项）。
6. 旧系统的群聊记录不迁，新的群聊记录从这时开始。

## 第 3 步：全部切完以后（先问用户）

按 CUTOVER.md 第二节：

1. 停掉旧系统的朋友圈网页、记忆整理、电话三个 launchd 任务（任务名见 `docs/dsh-migration/local/INVENTORY.md`）：用 `launchctl bootout gui/$UID/<任务名>`，plist 文件留着。
2. 把旧仓库的 `configs/_global.yml` 复制到新仓库的 `configs/`。这个文件在 `.gitignore` 里，确认 `git status` 里看不到它。
3. `bun gateway/scripts/autostart.ts install-shared --botlife-db ~/claudebotlife/state.db`。
4. 检查（只出状态码）：`curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/` 和 `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/`，预期 200 或者登录页的 30x/401。
5. 旧系统的"每日事件"（daily-wildcard）、"清理"（cleanup）两个任务，新系统没有接手。问用户是留着还是停掉。

## 第 4 步：回滚（出问题，或者用户要求时）

**回滚一个 bot：**

```bash
bun gateway/scripts/autostart.ts uninstall <新名>
bun gateway/scripts/autostart.ts uninstall self-initiate.<新名>
bash ~/.claude/dispatcher/start-bot.sh <旧名>    # 会把所有旧 bot 重启一遍，先告诉用户
```

请用户给它发一条消息，确认是旧 bot 在回复。新系统的数据（账本、改过的人设副本）都留着，下次再切不用重来。

**回滚导演：** `autostart.ts uninstall director`，删掉 `~/.dsh-bot/director/mode/$G`，再按用户的意思启动旧导演。

**回滚共用服务：**
1. `uninstall` 掉 `jiwen`、`memory-compactor`、`moments-web`、`voicecall`；
2. 对每个旧任务运行 `launchctl bootstrap gui/$UID ~/Library/LaunchAgents/<旧任务名>.plist`。

**人设改坏了：** 把第 1.3 步的备份复制回新副本，网关会自己发现人设变了并重启 dsh。

## 交付

1. 写 `docs/dsh-migration/local/reports/cutover.md`，包括：
   - 环境；
   - 第 0 步的测试结果；
   - 每个 bot 第 1.7 步的记录；
   - 群聊和共用服务的结果；
   - 做过的回滚和原因；
   - 收尾时每个 bot 的 `setup.ts check`、`health.ts`、`report.ts --hours 24` 输出（按铁律 4 处理）；
   - 发现的问题：复现步骤、现象、事件名。
2. 提交前，在 `git diff` 里搜一遍：`sk-`、令牌格式、用户的 Telegram 用户 id、群 id、系统用户名、bot 的旧名字、人设里的句子。确认都没有。
3. 从 `claude/nice-brahmagupta-dtjsma` 拉出新分支 `local/cutover-report`，推到这个分支。
4. 向用户汇报时用平实的中文，先说结论。最后一句写："切换报告已推到 local/cutover-report"。
