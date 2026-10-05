
# dsh 离线事实核对（0.1.5-rc.1）

生成时间：2026-10-05T15:33:13.689Z

## 结果

| 编号 | 核对的事实 | 结论 | 实际观察 |
|---|---|---|---|
| C1 | 握手：ACP v1，服务端名 deepseek-harness-acp，支持 HTTP MCP 与 close/list/resume | 符合 | protocolVersion=1 agent=deepseek-harness-acp@0.0.1 启动+握手 1552ms caps={"mcpCapabilities":{"http":true},"promptCapabilities":{"image":false,"audio":false,"embeddedContext":false},"sessionCapabilities":{"close":{},"list":{},"resume":{}}} |
| C2 | session/new：会话 id 由服务端生成；cwd 必须是绝对路径 | 符合 | sessionId=cf817841-7a1b-4f51-b2ab-39b8c4fed71e；相对路径 → -32602 Invalid params: cwd must be an absolute path: relative/dir |
| C3 | configOptions 里列出可选模型（按供应商分组） | 符合 | 当前=["fake","fake-chat"]；可选=["deepseek-official","deepseek-flash"] ["deepseek-official","deepseek-v4-flash"] ["deepseek-official","deepseek-v4-pro"] ["deepseek-official","deepseek-v4-flash-vision-exp"] ["fake","fake-chat"]；思考强度=当前模型不支持，不出现该选项 |
| C4 | session/set_config_option：按会话切模型，非法值报 -32602 | 符合 | 切到 ["deepseek-official","deepseek-v4-flash-vision-exp"]；非法值 → -32602 Invalid params: unknown model option: ["nope","nope"] |
| C5 | session/list 不列活跃会话；close 之后才列出 | 符合 | 关闭前 0 个；关闭后 1 个 |
| C6 | session/resume：cwd 必须一致；不回放历史；同一会话不能重复续接 | 符合 | 换 cwd → -32602 Invalid params: session cwd does not match: /Users/wsxwj/Desktop/claude/dshbot/dsh-bot-work/lab/dsh/.runs/facts-1791214393593/sb1/work/other；回放更新数=0；续接后模型=["fake","fake-chat"]；重复续接 → Invalid params: session is already active: cf817841-7a1b-4f51-b2ab-39b8c4fed71e |
| C7 | 同一会话同时只能有一个 prompt | 符合 | 第一个 → end_turn；第二个 → Invalid params: a prompt is already in flight for this session |
| C8 | 没有密钥时 prompt 以 JSON-RPC 错误返回（不是 stopReason） | 符合 | -32603 Internal error: turn failed: llm-deepseek: no API key for provider route "deepseek-official"; store DEEPSEEK_API_KEY through the credentials service (the web Mo |
| C9 | 只留人设：系统提示词逐字等于人设；0 个工具；不注入 AGENTS.md / CLAUDE.md / 技能目录 / 运行时上下文 | 符合 | 系统提示词 3734 字（人设 3733 字）；工具 0 个；非系统消息 1 条  |
| C10 | 出厂配置对照（同一份人设）：多出的工具说明与注入消息 | 仅记录 | 系统提示词 8246 字；工具 25 个（25880 字符）；非系统消息 3 条，其中注入 2 条 |
| C11 | 人设里的 {{user}} 不转义会让请求失败；插入 U+2060 后正常 | 符合 | 不转义 → Internal error: turn failed: unknown prompt variable "{{user}}" in section "deployment:persona-prefix"; registered variables: provider, mode；转义后 → 正常 |
| C12 | HTTP MCP 工具按会话挂载：名字 mcp__<服务名>__<工具>；鉴权头送达；不触发权限请求；工具报错时模型看到错误文字 | 符合 | 请求里的工具=mcp__tg__reply；调用=mcp__tg__reply,mcp__tg__reply；鉴权头=Bearer per-session-check；权限请求=0；失败更新=1 |
| C13 | session/cancel：stopReason=cancelled；被取消的用户消息留在历史里且没有回复 | 符合 | stopReason=cancelled；历史里“取消测试”之后紧跟 user |
| C14 | 生成中强杀 dsh 再续接：那条在飞消息留在历史里、没有回复（再原样重发 = 模型看到两遍） | 符合 | prompt → process exited before response to session/prompt；历史里“强杀测试”之后紧跟 user |
| C15 | 上游 500/429 自动重试（本测试配了最多 2 次），400 不重试；失败以 JSON-RPC 错误返回；失败轮的用户消息留在历史里 | 符合 | 五百: 3 次请求 → -32603；限流: 3 次请求 → -32603；坏请求: 1 次请求 → -32603；失败轮用户消息仍在历史：true |
| C16a | deepseek-official 路由请求（出厂）：协议、插件清单字段、会话日志上传字段、旧思考是否回传 | 仅记录 | 路径=/chat/completions；字段=model,messages,stream,stream_options,thinking,reasoning_effort,max_tokens,dsh_plugin_packages；插件清单=有(3423字符)；会话日志上传=无；第二轮历史带回旧思考=true |
| C16b | deepseek-official 路由请求（关掉上传类行）：协议、插件清单字段、会话日志上传字段、旧思考是否回传 | 仅记录 | 路径=/chat/completions；字段=model,messages,stream,stream_options,thinking,reasoning_effort,max_tokens；插件清单=无；会话日志上传=无；第二轮历史带回旧思考=true |
| C17 | 补丁文件：空文件或只有注释 → 启动失败；写 [] 才算空；指向不存在的行只警告 | 符合 | 空=1 注释=1 []=0 不存在的行=0（dsh: [/Users/wsxwj/Desktop/claude/dshbot/dsh-bot-work/lab/dsh/.runs/facts-179121） |
| C18 | 凭据文件 $DSH_HOME/.credentials.yaml：权限 644 拒绝启动，600 正常并能用其中的密钥 | 符合 | 644 → 退出码 1；600 → 退出码 0，上游看到的鉴权头前缀=Bearer fro… |
| C19 | headless + 只留人设：一次一个任务，stdout 只有最终文本，请求里没有工具（适合后台小任务） | 符合 | 退出码 0，耗时 548ms，stdout={"score": 3}，工具 0 个 |
| C20 | 挂 MCP 时 dsh 自己加的东西（0.2.0：3 个资源工具 + 系统提示词末尾一段）；禁用 mcp-resources 行后消失 | 仅记录 | 默认：工具=mcp__tg__reply；系统提示词="你是测试人设。" ｜ 禁用后：工具=mcp__tg__reply；系统提示词="你是测试人设。" |
| C21 | 出厂压缩与中文：单条消息按“4 字符 1 token”估，保留量设大会一直不压缩、设小会频繁压缩 | 仅记录 | 保留 2000：阈值 4700，dsh 估值 3229→6462，压缩请求 0 次 ｜ 保留 500：阈值 4700，dsh 估值 3229→4866，压缩请求 18 次 |

dsh 可执行文件：/Users/wsxwj/dsh-lab/015/node_modules/.bin/dsh

报告文件：/Users/wsxwj/Desktop/claude/dshbot/dsh-bot-work/lab/dsh/.runs/facts-1791214393593/report.md
