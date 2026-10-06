# 切换、回滚、故障排查、dsh 升级

新旧两套可以同时在一台机器上跑：目录、端口、状态都分开。唯一的冲突是同一个 bot 令牌同一时刻只能有一个程序收消息，所以按 bot **逐个**切换，每切一个都能单独退回去。

下面的命令都在新仓库的根目录里运行。`<新名>` 是新系统里的 bot 名，`<旧名>` 是旧系统里的名字，`<旧频道目录>` 是旧 bot 的频道目录（`~/.claude/channels/<旧名>`）。

## 一、切换一个 bot

前提：dsh 和凭据文件已经准备好（M1 的 `setup.ts harness`、`setup.ts credentials`）。

1. **停旧 bot**：`bash ~/.claude/dispatcher/stop-bot.sh <旧名>`。它会写一个停用标记，旧系统的重启脚本以后不会再把它拉起来。
2. **准备新 bot**（测试时已经迁过的跳过这一步）：
   - `bun gateway/scripts/setup.ts bot <新名> --from <旧频道目录>`：复制人设、白名单、关系数值、令牌文件、长期记忆。旧目录不动。
   - 在 `~/.dsh-bot/configs/<新名>.yml` 里补上：
     - `display_name`；
     - `life_config: <旧配置文件的完整路径>`（作息、情绪等设置从这里读，朋友圈和画风也按旧名字找）；
     - `gateway:` 下面的 `image_skill_dir`、`botlife_db`（见 M4.md 第二节）。
   - `bun gateway/scripts/setup.ts check ~/.dsh-bot/configs/<新名>.yml`，要"全部就绪"。
3. **导入旧记录**（推荐）：
   - 先看看有多少：`python3 scripts/import_history.py <新名> --from <旧频道目录> --old-bot <旧名> --dry-run`。只出条数。
   - 有 `.promises.json` 的，先看结构：加 `--inspect-promises`。只打印字段名和类型。
   - 确认没问题再去掉 `--dry-run` 正式导入：最近 30 天的私聊（`--days` 可改）和还没到点的承诺。
   - 新网关第一次处理这个私聊时，会用导入的记录补写一份摘要，新会话带着摘要和最近的原话开始。
   - 这个私聊在新系统里已经聊过的（比如测试用过的 bot5），脚本不导入，免得新旧记录交错。
4. **开机自启**：
   - `bun gateway/scripts/autostart.ts install ~/.dsh-bot/configs/<新名>.yml`：网关开机自启，退出了 10 秒后自动拉起；
   - `bun gateway/scripts/autostart.ts install-jobs ~/.dsh-bot/configs/<新名>.yml`：主动消息，每 10 分钟看一次要不要开口；
   - 要在设了代理的终端里运行：安装时会把代理设置带进去，launchd 启动的程序拿不到终端里的环境变量。
5. **抽查**：
   - 给 bot 发一条消息；
   - `bun gateway/scripts/autostart.ts status` 看运行状态；
   - `bun gateway/scripts/logs.ts --config ~/.dsh-bot/configs/<新名>.yml -f` 看实时日志；
   - `bun gateway/scripts/health.ts --config …` 看健康状态。
6. **群聊**（用到的话）：
   - 先停旧的导演，它还在给旧 bot 点名；
   - 再按 M5.md 第二节登记群、打开导演开关；
   - 最后 `bun gateway/scripts/autostart.ts install-director --chat <群 id>`。

Telegram 会把旧程序还没确认的消息投给新网关，账本负责去重，不会重复回复。

## 二、全部 bot 切完以后

旧系统还有几个所有 bot 共用的服务和定时任务。全部 bot 都切完以后，换成新仓库里的同一批脚本：

1. 停掉旧的：`launchctl bootout gui/$UID/<旧任务名>`。旧任务名见 `docs/dsh-migration/local/INVENTORY.md` 的"launchd 任务"一节，要停的是朋友圈网页、记忆整理、电话三个。plist 文件留着，回滚要用。
2. 把旧仓库的 `configs/_global.yml` 复制一份到新仓库的 `configs/`。这个文件在 `.gitignore` 里，不会被提交。
3. `bun gateway/scripts/autostart.ts install-shared --botlife-db ~/claudebotlife/state.db`，会装上四项：
   - 情绪（每 5 分钟）；
   - 记忆整理（每周日 4 点）；
   - 朋友圈网页（8765，常驻）；
   - 电话（8766，常驻）。
   朋友圈库继续用旧的那一份。
4. voice-bridge 和 provider-proxy 新系统也在用，照常留着。旧系统的每日事件、清理两个任务，脚本不在仓库里，没有接手；留着还是停掉由你定。

## 三、回滚

**回滚一个 bot：**
1. `bun gateway/scripts/autostart.ts uninstall <新名>`，再 `uninstall self-initiate.<新名>`；
2. `bash ~/.claude/dispatcher/start-bot.sh <旧名>`：会把所有旧 bot 重启一遍。

旧数据切换时没动过，旧 bot 直接接着用。新系统运行期间的聊天不在旧会话里；需要的话可以从账本里读出来（`chat_history.py` 能读账本）。

**回滚共用服务：**
1. `uninstall` 掉 `jiwen`、`memory-compactor`、`moments-web`、`voicecall`；
2. 再 `launchctl bootstrap gui/$UID ~/Library/LaunchAgents/<旧任务名>.plist`。

## 四、故障排查

| 现象 | 先看什么 | 怎么办 |
|---|---|---|
| bot 不回消息 | `health.ts`；日志里的 ✗ 和 ⚠ | 见下面几行 |
| 日志里有 `telegram.conflict` | — | 还有别的程序在用这个令牌收消息，多半是旧 bot 没停干净：再跑一次 `stop-bot.sh <旧名>` |
| 日志里有 `telegram.unauthorized` | — | 令牌不对：检查频道目录的 `.env` |
| `dsh.exited` 反复出现 | `~/.dsh-bot/bots/<名>/logs/dsh-stderr.log` | 跑 `setup.ts check`；dsh 没装好就再跑一次 `setup.ts harness` |
| `turn.failed` 里有 `MISSING_CREDENTIAL` | `setup.ts check` | 凭据文件里缺对应的密钥 |
| 开机后没起来 | `autostart.ts status`；`~/.dsh-bot/bots/<名>/logs/launchd.log` | 找不到 bun 或 python：在能运行它们的终端里重新 `install`。连不上 Telegram：在设了代理的终端里重新 `install` |
| 回复很慢 | `logs.ts` 里 `turn.end` 前后的时间；`send.photo_shrunk`、`tool.reply_slow` | 发图慢看代理；模型慢可以用 `/model` 换 |
| 语音转不成文字 | `media.transcribe_failed` 后面的状态码 | 401：voice-bridge 设了口令，凭据文件里要加 `VOICE_BRIDGE_TOKEN`；403：目录不在它的白名单里（网关会自动换目录再试一次） |
| 群里没人说话 | 导演日志 `~/.dsh-bot/director/director.log`；网关日志里的 `inbound.dropped` | 导演开关文件在不在；`access.json` 的 `groups` 里有没有这个群；bot 的隐私模式关了没有 |
| 朋友圈通知没到新 bot | 网页是哪个仓库起的 | 要用新仓库起的网页（全部切完以后用 `install-shared`）；`state/api.key`、`state/api.port` 要在 |
| 主动消息一直不来 | `~/.dsh-bot/logs/self-initiate.<名>.log` | 每次都会写跳过的原因和下次机会的时间 |
| 换段太勤、太费钱 | `report.ts` 的"会话（段）"一节 | 看是用量到线换段，还是人设或运行规则变了 |
| 担心日志里有密钥 | `report.ts` 最后一节 | 它会拿凭据文件里的值逐个检查日志 |

## 五、升级 dsh

dsh 的版本钉死在仓库的 `harness/package.json` 和锁文件里，所有 bot 共用 `~/.dsh-bot/harness` 这一份。

1. **改版本**：在开发机上改 `harness/package.json` 的版本号，`cd harness && npm install --package-lock-only` 重新生成锁文件，走拉取请求合进来。
2. **离线核对**：`DSH=<新版本的 dsh> bun lab/dsh/offline/facts.ts`，和 `docs/dsh-migration/VERIFIED.md` 对照，看行为有没有变。
3. **网关测试**：`cd gateway && bun run test`，再用新版本跑 `test/e2e/real-dsh.test.ts`。CI 会自动按锁文件跑真 dsh 测试。
4. **真密钥脚本**：在本机跑 `lab/dsh/real/` 下的四个脚本（见 `lab/dsh/README.md`），看用量和行为。
5. **换上新版本**：
   - 本机拉代码，`bun gateway/scripts/setup.ts harness`；
   - 逐个重启网关：`launchctl kickstart -k gui/$UID/com.dsh-bot.<名>`，每重启一个就抽查一个。
   - 升级后每个聊天的第一条消息会换一次段，因为 dsh 版本是会话指纹的一部分。这是正常的。
6. **退回旧版本**：把 `harness/package.json` 改回去，再跑一次 `setup.ts harness`，然后重启网关。
