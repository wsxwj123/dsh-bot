# 现有部署脱敏盘点

## 系统与版本

| 项 | 值 |
|---|---|
| 系统 | Darwin 24.6.0 arm64 |
| Python | 3.10.11 |
| bun | 1.3.11 |
| node | v25.9.0 |
| npm | 11.12.1 |
| 全局 dsh | 0.1.5-rc.1 |
| claude | 2.1.288 (Claude Code) |
| ffmpeg | ffmpeg version 8.0 Copyright (c) 2000-2025 the FFmpeg developers |
| tmux | tmux 3.6a |
| 全局 dsh 子包版本分布 | 0.1.5-rc.2 × 230 |
| ~/.dsh（日常用的 DSH_HOME） | 按任务书铁律 3 未读取（用户确认跳过） |

## bot 目录（~/.claude/channels）

共 5 个带 CLAUDE.md 的 bot 目录。名字已替换为 bot1、bot2……

### bot1

- 顶层文件：.burst/, .last-chat-id, .last-inbound-ts.json, CLAUDE.md, inbox/, logs/, worker-mcp.json
- CLAUDE.md：71 字符，3 行，标题 1 个；成对 {{…}} 占位 0 处（其中合法变量名格式 0 处）；全部 {{ 出现 0 次
- access.json：无或读不了
- relationship.json：无
- inbox/ 现有文件数：1
- logs/：2 个文件，共 145 KB
- 对应的 Claude Code 会话目录：存在；会话文件 0 个，共 0 KB；memory/ 文件：无

### bot2

- 顶层文件：.DS_Store, .claude/, .env, .hang-archive.json, .hang-state.json, .last-inbound-ts.json, .mcp.json, .promises.json, .spawn-worker.lock, CLAUDE.md, access.json, chats/, inbox/, memory/, peer_relations.json, relationship.json；另有 人设备份/副本 16 个、access.json 备份 4 个（文件名含日期、改动说明、真名或人设内容，已省略）
- CLAUDE.md：17401 字符，468 行，标题 40 个；成对 {{…}} 占位 1 处（其中合法变量名格式 1 处）；全部 {{ 出现 1 次
- access.json：字段 allowFrom, dmPolicy, groups, mentionPatterns, paragraphDelay, pending, splitOnParagraph, tmuxSession, voiceId；dmPolicy=allowlist；allowFrom 1 个；群 1 个（requireMention=False, allowFrom 3 个, selfAliases 5, otherBotUsernames 2）
- relationship.json：字段 affection, desire, energy, prompt_snippet, stage, trust, updated_ts
- memory/：
- .claude/settings.json：字段 enabledPlugins, env, hooks；钩子 SessionStart[compact, compact, resume]
- inbox/ 现有文件数：0
- chats/ 子目录数：2；voice_log.jsonl 个数：1
- 对应的 Claude Code 会话目录：存在；会话文件 1 个，共 3399 KB；memory/ 文件：MEMORY.md(3262B), MEMORY.md.bak(2536B), recent_conversation.md(2021B)；另有主题记忆文件 8 个，共 7767B（文件名描述记忆主题，已省略）

### bot3

- 顶层文件：${CHANNEL_DIR}/, .DS_Store, .claude/, .env, .hang-archive.json, .hang-state.json, .last-inbound-ts.json, .mcp.json, .promises.json, .spawn-worker.lock, CLAUDE.md, LEARNINGS.md, access.json, chats/, inbox/, memory/, peer_relations.json, relationship.json；另有 人设备份/副本 20 个、access.json 备份 4 个（文件名含日期、改动说明、真名或人设内容，已省略）
- CLAUDE.md：18163 字符，488 行，标题 34 个；成对 {{…}} 占位 2 处（其中合法变量名格式 2 处）；全部 {{ 出现 2 次
- access.json：字段 allowFrom, dmPolicy, groups, mentionPatterns, paragraphDelay, pending, splitOnParagraph, tmuxSession, voiceId；dmPolicy=allowlist；allowFrom 1 个；群 1 个（requireMention=False, allowFrom 3 个, selfAliases 5, otherBotUsernames 2）
- relationship.json：字段 affection, desire, energy, prompt_snippet, stage, trust, updated_ts
- memory/：
- .claude/settings.json：字段 enabledPlugins, env, hooks；钩子 SessionStart[compact, compact, resume]
- inbox/ 现有文件数：0
- chats/ 子目录数：2；voice_log.jsonl 个数：1
- 对应的 Claude Code 会话目录：存在；会话文件 1 个，共 3292 KB；memory/ 文件：MEMORY.md(2020B), MEMORY.md.bak(1966B), recent_conversation.md(454B)；另有主题记忆文件 5 个，共 4319B（文件名描述记忆主题，已省略）

### bot4

- 顶层文件：.DS_Store, .claude/, .env, .hang-state.json, .last-inbound-ts.json, .mcp.json, .spawn-worker.lock, CLAUDE.md, access.json, chats/, inbox/, memory/, relationship.json；另有 人设备份/副本 11 个（文件名含日期、改动说明、真名或人设内容，已省略）
- CLAUDE.md：16823 字符，443 行，标题 40 个；成对 {{…}} 占位 1 处（其中合法变量名格式 1 处）；全部 {{ 出现 1 次
- access.json：字段 allowFrom, dmPolicy, groups, mentionPatterns, paragraphDelay, pending, splitOnParagraph, tmuxSession；dmPolicy=allowlist；allowFrom 1 个；群 0 个
- relationship.json：字段 affection, desire, energy, prompt_snippet, stage, trust, updated_ts
- memory/：
- .claude/settings.json：字段 env, hooks；钩子 SessionStart[compact, compact]
- inbox/ 现有文件数：0
- chats/ 子目录数：1；voice_log.jsonl 个数：0
- 对应的 Claude Code 会话目录：存在；会话文件 1 个，共 1329 KB；memory/ 文件：

### bot5

- 顶层文件：.DS_Store, .claude/, .env, .hang-state.json, .last-inbound-ts.json, .mcp.json, .spawn-worker.lock, CLAUDE.md, access.json, approved/, chats/, inbox/, memory/, peer_relations.json, relationship.json；另有 人设备份/副本 21 个、access.json 备份 4 个、其它文件 1 个（文件名含日期、改动说明、真名或人设内容，已省略）
- CLAUDE.md：16515 字符，443 行，标题 30 个；成对 {{…}} 占位 1 处（其中合法变量名格式 1 处）；全部 {{ 出现 1 次
- access.json：字段 allowFrom, dmPolicy, groups, mentionPatterns, paragraphDelay, pending, splitOnParagraph, tmuxSession, voiceId；dmPolicy=allowlist；allowFrom 1 个；群 1 个（requireMention=False, allowFrom 1 个, selfAliases 4, otherBotUsernames 2）
- relationship.json：字段 affection, desire, energy, prompt_snippet, stage, trust, updated_ts
- memory/：
- .claude/settings.json：字段 enabledPlugins, env, hooks；钩子 PreToolUse[mcp__telegram-worker__reply], SessionStart[compact, compact]
- inbox/ 现有文件数：0
- chats/ 子目录数：2；voice_log.jsonl 个数：1
- 对应的 Claude Code 会话目录：存在；会话文件 1 个，共 4621 KB；memory/ 文件：MEMORY.md(5847B), MEMORY.md.bak(4299B), recent_conversation.md(720B)；另有主题记忆文件 4 个，共 3582B（文件名描述记忆主题，已省略）

channels/ 下其它目录：_shared, group_transcripts, media

## Claude Code 会话目录（~/.claude/projects）

名字里含 channels 的项目目录 5 个；全部项目目录 222 个。

## launchd 任务（名字里像本项目的）

com.<用户名>.claudebotlife-cleanup, com.<用户名>.claudebotlife-daily-wildcard, com.<用户名>.claudebotlife-memory-compactor, com.<用户名>.claudebotlife-moments-web, com.<用户名>.claudebotlife-provider-proxy, com.<用户名>.voice-bridge-http, com.<用户名>.voicecall

## 旧仓库配置（~/claudebotlife）

- _example.yml：id，display_name，bot_channel_path，chat_id，user_address，bio，birthday，city，sleep_hours，recurring_activities{weekday, weekend}，personal_hobbies，interest_keywords，rss_feeds，event_categories，persona_summary
- _global.yml：user_display_name，user_bio，max_calls_per_5h，silence_threshold_minutes，moments{enabled, daily_post_limit_per_bot, silence_threshold_minutes, web_port, image_generation}，jiwen{enabled, tick_interval_min, state_dir, delta_llm, rates, thresholds}，relations{enabled, reflect_min_msgs, short_ttl}
- bot2.yml：id，display_name，bot_channel_path，chat_id，user_address，moment_voice，bio，birthday，city，sleep_hours，hobby_cooldown_hours，school_calendar{summer_start, summer_end, early_return, winter_days}，recurring_activities{weekday, weekend, break, early_return}，personal_hobbies，interest_keywords，rss_feeds，event_categories，face_traits，persona_summary，speaking_threshold，anchor_image，anchor_denoise，jiwen{delta_hints}，voice_id，dispatcher_port，user_name
- bot3.yml：id，display_name，bot_channel_path，chat_id，user_address，moment_voice，bio，birthday，schooling{stage, enrollment_year}，city，sleep_hours，moments_daily_limit，school_calendar{summer_start, summer_end, early_return, winter_days}，recurring_activities{weekday, weekend, break, early_return}，personal_hobbies，interest_keywords，rss_feeds，event_categories，face_traits，anchor_image，anchor_denoise，persona_summary，speaking_threshold，jiwen{rates_override, delta_hints}，voice_id，dispatcher_port，user_name
- bot5.yml（原文件名是 bot 真名，已替换为别名）：id，display_name，bot_channel_path，chat_id，user_address，moment_voice，bio，birthday，city，sleep_hours，moments_daily_limit，school_calendar{summer_start, summer_end, winter_days}，recurring_activities{weekday, weekend, break}，personal_hobbies，interest_keywords，rss_feeds，event_categories，face_traits，persona_summary，speaking_threshold，anchor_image，anchor_denoise，jiwen{rates_override, delta_hints}，voice_id，dispatcher_port，user_name
- 旧仓库当前提交：060f191 2026-10-05

## 本机 AI 补充说明

- 生成命令：`python3 lab/dsh/local/inventory.py --repo ~/claudebotlife --dsh-home <一个不存在的路径> --out …`。`--dsh-home` 指向不存在的路径是为了按铁律 3 跳过 `~/.dsh`（用户确认）。bot 目录在默认位置 `~/.claude/channels/`。
- 仓库是公开的，生成后逐行检查，只折叠了名字，没有改任何数字：
  - 顶层文件里的人设备份/副本和 access.json 备份：文件名含日期、改动说明、bot 真名或人设内容，改成只写个数；另有 1 个文件名涉及人设内容的文件同样只写个数（bot5）。
  - Claude Code 会话目录 `memory/` 里描述记忆主题的文件名改成"个数 + 总字节"，保留 `MEMORY.md`、`MEMORY.md.bak`、`recent_conversation.md`。
  - launchd 任务名里的系统用户名换成 `<用户名>`。
  - 旧仓库 `configs/` 里有一个以 bot 真名命名的配置文件，脚本没有替换（它只替换和 bot 目录同名的配置），已按其中 `bot_channel_path` 指向的目录换成别名 bot5。
- bot3 顶层有一个名字就叫 `${CHANNEL_DIR}/` 的目录，看起来是旧部署某个脚本没展开变量就建了目录，迁移时可以忽略。
- 会话目录补充计数（只数文件个数和大小，没读内容）。`baseline_claude_usage.py` 只统计 `*.jsonl`，而旧部署的历史大部分在 `*.jsonl.bak*` 备份里：

| bot | `*.jsonl` | `*.jsonl.bak*` | 其它文件 |
|---|---|---|---|
| bot1 | 0 个 | 5 个 / 586 KB | 3 个 / 0 KB |
| bot2 | 1 个 / 3399 KB | 11 个 / 67736 KB | 7 个 / 6499 KB |
| bot3 | 1 个 / 3292 KB | 13 个 / 49957 KB | 10 个 / 9581 KB |
| bot4 | 1 个 / 1329 KB | 2 个 / 1551 KB | 4 个 / 0 KB |
| bot5 | 1 个 / 4621 KB | 14 个 / 48337 KB | 23 个 / 16292 KB |
