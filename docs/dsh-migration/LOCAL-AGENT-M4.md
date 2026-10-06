# 本机 AI 任务书：M4 真机验收（bot5：语音、图片、生图、朋友圈、管理台换模型）

你在用户的 Mac 上工作，接着 M3 的活。新代码在仓库 `wsxwj123/dsh-bot` 的分支 `claude/nice-brahmagupta-dtjsma` 上（M1–M3 已经合进 master）。

## 先读

- `docs/dsh-migration/M4.md`：这一期做了什么，重点看第二、三节
- `docs/dsh-migration/LOCAL-AGENT-M3.md`：铁律和收尾方式，这次照样适用

## 铁律

和 M3 相同，违反任何一条就停下来问用户。补充四条：

1. **不看图片和语音的内容。** 只用 `ls`、`wc -c` 确认文件在不在、大小。
2. **不读 NovelAI 令牌和 voice-bridge 口令。** 有没有配，只看 `setup.ts check` 的输出。
3. **不读朋友圈的文字。** 查朋友圈库只数条数，不打印内容。
4. **不改旧系统正在跑的任何服务。** 这次要另起的朋友圈网页用 8767 端口，不动旧网页（8765）。电话那一项要先停旧的电话服务，属于可选项，先问用户。

## 步骤

### 1. 拉代码、跑自动化测试（不需要密钥）

```bash
git fetch origin claude/nice-brahmagupta-dtjsma && git checkout claude/nice-brahmagupta-dtjsma && git pull
cd gateway && bun install && bun run typecheck && bun run test
DSH_BOT_HARNESS=~/.dsh-bot/harness bun test --timeout 90000 test/e2e/real-dsh.test.ts
cd ../tests && python3 -m pytest -q unit/test_chat_history_ledger.py unit/test_chat_history_namespace.py unit/test_project_slug.py unit/test_m3_life.py unit/test_m4_dsh.py
```

预期：
- 网关 211 项（通过 206，跳过 5）；
- 真 dsh 5 项通过；
- Python 34 项通过。`test_m4_dsh.py` 需要 flask 和 requests。旧系统的朋友圈网页本来就要用这两个包；如果缺，用旧系统跑网页的那个 Python 来跑测试。

### 2. 停旧 bot5（先问用户）

同 M2。

### 3. 补配置

先查旧系统给 Telegram 生图用的是哪个服务（只打印服务名）：

```bash
python3 -c "import yaml,os; g=yaml.safe_load(open(os.path.expanduser('~/claudebotlife/configs/_global.yml'))) or {}; i=((g.get('moments') or {}).get('image_generation') or {}); print(i.get('provider_telegram') or i.get('provider') or 'novelai')"
```

然后在 `~/.dsh-bot/configs/bot5.yml` 的 `gateway:` 下加下面几行（没有 `gateway:` 就新建这一节）：

```yaml
  image_skill_dir: ~/.claude/skills/novelai-skill   # 上面打印的是 comfyui 时，改成 ~/.claude/skills/comfyui-skill，再加一行 image_provider: comfyui
  botlife_db: ~/claudebotlife/state.db              # 和旧朋友圈网页共用一个库
```

- 先用 `ls ~/.claude/skills/novelai-skill/SKILL.md` 确认这个目录在。不在就找一下这份技能在哪，只记路径。
- 原来那份技能目录的 `.env.local` 里有 NovelAI 令牌，网关会让生图脚本自己去读，不用复制。

把旧频道的音色复制到新频道（只打印"有/没有"）：

```bash
python3 - <<'EOF'
import json, os, yaml
new = yaml.safe_load(open(os.path.expanduser('~/.dsh-bot/configs/bot5.yml'), encoding='utf-8'))
old = yaml.safe_load(open(os.path.expanduser(new['life_config']), encoding='utf-8'))
src = os.path.join(os.path.expanduser(old['bot_channel_path']), 'access.json')
dst = os.path.join(os.path.expanduser(new['bot_channel_path']), 'access.json')
v = json.load(open(src, encoding='utf-8')).get('voiceId')
a = json.load(open(dst, encoding='utf-8'))
print('旧频道有音色' if v else '旧频道没有音色', '/', '新频道原来有音色' if a.get('voiceId') else '新频道原来没有音色')
if v and not a.get('voiceId'):
    a['voiceId'] = v
    tmp = dst + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(a, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, os.stat(dst).st_mode)
    os.replace(tmp, dst)
    print('已复制到新频道')
EOF
```

如果 voice-bridge 设了口令（旧系统启动它时带了 `VOICE_BRIDGE_TOKEN`），请用户自己在 `~/.dsh-bot/credentials.yaml` 的 `refs` 下加一项 `VOICE_BRIDGE_TOKEN: <值>`，你不要碰这个值。不确定的话，先跳过这一步，第 5 步 a 项会看出来。

最后运行：

```bash
bun gateway/scripts/setup.ts check ~/.dsh-bot/configs/bot5.yml
```

预期：前面几项和 M3 一样，是"全部就绪"。新加的几行（以 ✅ 或 · 开头）应当是：音色已配、voice-bridge 在跑、生图说明文件在、NovelAI 令牌已配、朋友圈库在、按旧系统里的名字找。以 · 开头的项不算问题，但对应的功能用不了，记下来。

### 4. 启动新网关和新网页

终端一：

```bash
DSH_BOT_LOG_CONSOLE=1 bun gateway/src/main.ts --config ~/.dsh-bot/configs/bot5.yml
```

第一条消息时会出现 `segment.prompt_changed` 和一次换段，因为运行规则加了一句。这是正常的。

终端二：在新仓库根目录起一个朋友圈网页，用 8767 端口。它和旧网页共用朋友圈库，登录密码也一样。这样启动不会去改模型来源的设置：

```bash
HUB_CONFIGS_DIR=~/.dsh-bot/configs BOTLIFE_STATE_DB=~/claudebotlife/state.db \
  python3 -c "import sys; sys.path.insert(0, '.'); from moments.web import app; app.run(host='127.0.0.1', port=8767)"
```

`python3` 缺 flask 时，换成旧系统跑网页的那个 Python。

### 5. 真机清单

每一项记"符合"或"不符合"，加一句现象（不写聊天内容）。日志事件都在 `~/.dsh-bot/bots/bot5/logs/gateway.log`。

| 项 | 怎么做 | 预期 |
|---|---|---|
| a. 收语音 | 请用户给 bot 发一条语音 | 出现 `media.prepared`，`kind` 是 `voice`，`transcribed` 是 true。bot 的回复对得上用户说的话（用户判断）。`transcribed` 是 false 时，看 voice-bridge 的日志有没有 401（口令没配） |
| b. 发语音 | 请用户让 bot "用语音回我" | 出现 `tool.reply`，带 `voice: true`；用户收到的是语音消息，能听。出现 `tool.reply_voice_unconfigured` 说明没配音色；出现 `media.synthesize_failed` 说明合成出错，记下来 |
| c. 收图片 | 请用户给 bot 发一张图（用户自己选，不涉及隐私的图） | 出现 `media.prepared`，`kind` 是 `photo`。模型能看图时，bot 能说出图里大概有什么；不能看图时，会出现一次 `prompt.images_unsupported`，bot 说它看不到图。两种都算符合，记下是哪一种 |
| d. 自拍 | 请用户让 bot 发一张自拍 | 依次出现 `tool.generate_image`（`ok` 是 true）、`tool.reply`；用户收到一张图。超过 45 秒的话，bot 会先回一句，图好了再发。记下从用户发消息到收到图隔了多久。出现 `tool.generate_image_failed` 时，记下 `err` 里的报错类型（不写令牌） |
| e. 网页评论 | 请用户打开 `http://127.0.0.1:8767`，在 bot5 的一条朋友圈下面评论 | `gateway.log` 出现一轮来源为 `moment_reply` 的合成消息，接着出现 `tool.moments`（`action` 是 `reply`，`code` 是 0）；刷新网页，能看到 bot5 的回复 |
| f. 发朋友圈 | 请用户在 Telegram 里让 bot "发条朋友圈" | 出现 `tool.moments`，`action` 是 `post`，`code` 是 0；网页上多了一条 bot5 的圈，用户确认名字和头像是原来那个，不是一个新冒出来的 bot |
| g. 管理台换模型 | 请用户打开 `http://127.0.0.1:8767/hub/dsh-model` | 页面上有 bot5，显示现在用的模型。选一个别的模型点"换"，`gateway.log` 出现 `brain.override`；用户再发一条消息，正常回复。最后选"配置文件里的"换回来 |
| h. 电话（可选，先问用户） | 见下面 | 挂断后 `gateway.log` 出现来源为 `call` 的合成消息。如果 bot 在电话里答应了"几点提醒你"之类的事，出现 `commitment.created`，`source` 是 `call` |

**第 e 项之后**，数一下 bot5 在朋友圈库里的评论有没有变多（只出数字）：

```bash
python3 -c "import sqlite3,os; c=sqlite3.connect(os.path.expanduser('~/claudebotlife/state.db')); print(c.execute('select count(*) from moment_comments where ts > strftime(\"%s\",\"now\",\"-1 hour\")').fetchone()[0])"
```

**第 h 项**：旧的电话服务占着 8766 端口，要先停掉它，再在新仓库里用 `HUB_CONFIGS_DIR=~/.dsh-bot/configs python3 voicecall/server.py` 起新的。停的这段时间，其他 bot 打不了电话。先把这一点告诉用户，用户同意再做；测完停掉新的，恢复旧的。

### 6. 收尾

1. 网关还在跑的时候，运行 `setup.ts check`、`health.ts`、`report.ts --hours 3`，输出原样放进报告（路径里的系统用户名改成 `~`）。
2. 按 Ctrl+C 停掉网关和 8767 的网页。
3. 问用户要不要恢复旧 bot5，并说明会重启所有旧 bot。按用户的意思执行。
4. 第 3 步加的配置保留。

## 交付

1. 写 `docs/dsh-migration/local/reports/m4-check.md`，包括：
   - 环境（以及生图用的是 novelai 还是 comfyui）；
   - 第 1 步的测试结果；
   - 第 3 步 `setup.ts check` 的输出；
   - 第 5 步每一项的结果；
   - 第 6 步三条命令的输出；
   - 发现的问题：复现步骤、现象、事件名。
2. 提交前，在 `git diff` 里搜一遍：`sk-`、令牌格式、用户的 Telegram 用户 id、系统用户名、bot5 的旧名字。确认都没有。
3. 从 `claude/nice-brahmagupta-dtjsma` 拉出新分支 `local/m4-check`，推到这个分支。
4. 向用户汇报时用平实的中文，先说结论。最后一句写："M4 实测已推到 local/m4-check"。
