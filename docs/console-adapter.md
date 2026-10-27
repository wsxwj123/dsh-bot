# 适配服务：让合并管理台管理新系统的 bot

两套系统（旧的 Claude Code 那套、新的 dsh 网关这套）的 bot 正在合进同一个管理台。管理台的页面由旧系统那边提供（下文叫「壳」），新系统这一半由本仓的**适配服务**接入：壳不直接读写本系统的文件，所有操作都经适配服务的本机接口完成。

接口的权威说明是两边共同维护的「统一管理台适配约定」，放在旧系统那边仓库的 `docs/unified-console-adapter.md`。本文只讲本仓这一半怎么跑、能做什么。

## 怎么跑

- macOS 上由启动项 `com.dsh-bot.console` 常驻，日志在 `~/.dsh-bot/logs/console.launchd.log`。
- 手动运行：在仓库根执行 `python3 -m moments.adapter_service`。
- 只听本机回环地址，默认 `127.0.0.1:17990`。

| 环境变量 | 作用 | 缺省 |
|---|---|---|
| `DSH_CONSOLE_HOST` | 监听地址，只许 `127.0.0.1`、`::1`、`localhost` | `127.0.0.1` |
| `DSH_CONSOLE_PORT` | 监听端口 | `17990` |
| `DSH_CONSOLE_TOKEN_FILE` | 令牌文件，权限 0600；不存在或为空时拒绝启动 | `~/.config/claude-dsh-console/adapter.token` |
| `DSH_BOT_HOME` | 新系统的家目录 | `~/.dsh-bot` |
| `HUB_CONFIGS_DSH_DIR` | 新系统的 bot 配置目录 | `~/.dsh-bot/configs` |

另有几个只供测试用的变量（`DSH_CONSOLE_PROVIDER_BUDGET`、`DSH_CONSOLE_STOP_WAIT`、`DSH_CONSOLE_LIFE_DIR`、`DSH_CONSOLE_CHANNELS_DIR`、`DSH_CONSOLE_CLAUDE_PROJECTS_DIR`），生产不设。

## 鉴权

每个请求都要带请求头 `Authorization: Bearer <令牌文件的内容>`。没带或不对一律回 401，什么都不做。令牌不进日志，也不出现在任何响应和报错里。

## 能做什么（接口前缀 `/adapter/v1`）

| 接口 | 用途 |
|---|---|
| `GET /meta` | 版本与能力清单（壳据此决定显示哪些功能） |
| `GET /bots` | 本系统全部 bot：状态、启用开关、网关在不在收消息、Telegram 用户名、当前模型、朋友圈里记的名字（`person`）、是否已迁到另一种底层、同一账号两边同时在跑的提示 |
| `POST /bots`、`GET /bots/templates` | 新建一只 bot（可以先建好不启动）；列出可用的人设模板。新建时可以带 `features`（九个功能键的数组，省略等于全不勾），勾了就写好配置、放对应技能，缺就绪条件的（音色、生图令牌）不打开、列在 `features_skipped`。新建时会在生活设定目录生成 `<bot_id>.yml` 并写进 `life_config`，之后就能用 `/bots/{id}/life` 读写；该目录已有同名文件时回 409 `exists` |
| `GET /providers`、`POST /bots/{id}/provider` | 看可选的供应商和模型；给一只 bot 换供应商、模型、思考强度，或带 `{"reset": true}` 回到配置文件里那一组。bot 停着时只存进配置，下次启用生效 |
| `POST /bots/{id}/restart` | 重启一只 bot。不带 `force` 时等它这一轮回完再重启：正忙就立刻回 `queued` 并在后台等，空闲或问不到网关就立即重启；`{"force": true}` 立刻重启、打断当前回合 |
| `POST /bots/{id}/enabled` | 启用或停用；从旧系统切过来时可顺带导入旧的私聊记录与没到点的约定。停用时可记下「已迁到另一种底层」，有这个标记就不许再启用。真正启动（停用后再启用）成功后还会异步触发一次日志看板重建（launchd 任务 `com.dsh-bot.tmux-logs`）；触发失败只记日志，不影响回包与耗时 |
| `POST /bots/{id}/archive`、`GET /archives`、`POST /archives/{archive_id}/restore` | 归档（整份移走，不删）、列出归档、恢复（恢复后是停用状态） |
| `POST /bots/{id}/delete` | 删除本地数据：Telegram 令牌文件直接抹掉，其余移进废纸篓；Telegram 上的 bot 本身不受影响 |
| `GET`、`PUT /bots/{id}/profile` | 显示名 |
| `POST /bots/{id}/token_handoff` | 切换到另一种底层时，把这只 bot 的 Telegram 令牌交给对方 |
| `POST /bots/{id}/notify` | 投递朋友圈通知、群聊轮到它说话 |
| `GET`、`PUT /bots/{id}/life` | 生活设定（作息、爱好、关注的话题等） |
| `GET`、`PUT /bots/{id}/persona` | 人设全文（带版本号，防止两人同时改互相覆盖） |
| `GET`、`PUT /bots/{id}/skills/{名字}` | 技能全文，名字是 `image`、`voice`、`gif`、`moments`、`proactive`、`group`、`nsfw` |
| `GET`、`PUT /bots/{id}/voice` | 语音编号、语音模型、文字语言、语音语言 |
| `GET`、`POST /bots/{id}/groups` | 在哪些群、各群的设置；进群、退群 |

适配服务还提供 `/hub/bots`、`/hub/addbot`、`/hub/dsh-model` 三个页面及其接口，供壳转发显示。oauth 账号管理的 6 个接口（`GET /hub/api/oauth/accounts`、`POST /hub/api/oauth/start`、`POST /hub/api/oauth/submit`、`GET /hub/api/oauth/status`、`POST /hub/api/oauth/accounts/<provider>/<email_hash>/activate`、`DELETE /hub/api/oauth/accounts/<provider>/<email_hash>`）也在同一份名单里，经同一套适配令牌鉴权后转发。

## 安全边界

- 只读写本系统自己的文件（`~/.dsh-bot` 下），不碰旧系统的目录。唯一例外：从旧系统切过来时，只读旧频道目录里的约定文件和 Claude Code 为它建的会话记录，这是用户当面同意的。
- 写文件一律先备份（每样只留最近 2 份）、再原子写入；软链、落在本系统目录外的文件一律拒绝。
- 密钥只回「有没有」，不回原文。唯一的例外是令牌转交：每个切换任务编号只交一次，同一只 bot 10 分钟内只交一次；令牌只出现在那一个成功响应里（带 `Cache-Control: no-store`），记录里只有 bot 编号、任务编号和时间。
- 「已迁到另一种底层」的标记是 `~/.dsh-bot/bots/<编号>/.moved`。标记在时，适配服务、管理台页面的启用开关、网关的开机自启安装都拒绝启动这只 bot，免得两边用同一个令牌同时收消息。标记要由壳在停用请求里清掉。
