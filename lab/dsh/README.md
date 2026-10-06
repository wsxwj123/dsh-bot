# dsh 验证工具

这里的脚本只用来核对 DeepSeek Harness（dsh）的真实行为，不是机器人本体。分两类：

- `offline/`：不需要任何密钥。用本机假模型服务代替 DeepSeek，逐条核对 dsh 的行为。我已经在 0.1.5-rc.1 和 0.2.0-rc.2 上各跑过一遍，结果见 `docs/dsh-migration/VERIFIED.md`。
- `real/`：需要 DeepSeek 密钥，请你在自己的机器上跑，把生成的 `report.md` 贴回来。

只依赖 bun，不需要 `npm install` 任何东西（dsh 本身除外）。

## 0. 准备一份独立的 dsh

不要用你日常那份全局 dsh，单独装一份到别的目录：

```bash
mkdir -p ~/dsh-lab/020 && cd ~/dsh-lab/020 && npm init -y >/dev/null && npm install @deepseek-ai/dsh@0.2.0-rc.2
export DSH=~/dsh-lab/020/node_modules/.bin/dsh   # 想测 0.1.5 就换成那一份
$DSH --version
```

注意：今天装 `@deepseek-ai/dsh@0.1.5-rc.1`，拿到的 230 个子包是 `0.1.5-rc.3`，你机器上现有的是 `0.1.5-rc.2`。版本号相同不等于代码相同，这也是新系统要用锁文件把版本钉死的原因。

所有脚本都在临时目录里给 dsh 单独准备 HOME 和 DSH_HOME，不会读写你的 `~/.dsh`、`~/.claude`。

## 1. 离线核对（不需要密钥）

```bash
cd <仓库根目录>
DSH=$DSH bun lab/dsh/offline/facts.ts
```

跑完会生成 `lab/dsh/.runs/facts-<时间>/report.md`，耗时约 3–5 分钟。

预期：除"仅记录"项以外全部是"符合"。"仅记录"项在两个版本上的值不同，见 `docs/dsh-migration/VERIFIED.md`。以后升级 dsh 时，先跑这一份，和上次的报告对照。

`offline/dry-run-real.ts` 会把下面四个真密钥脚本指向假模型跑一遍，只验证流程能走通，数字是假的。

## 2. 需要密钥的验证（请在你的机器上跑）

```bash
cd <仓库根目录>
export DEEPSEEK_API_KEY=sk-...     # 只放在当前终端里
export DSH=~/dsh-lab/020/node_modules/.bin/dsh
bun lab/dsh/real/real-01-persona-tokens.ts
bun lab/dsh/real/real-02-reasoning-billing.ts
bun lab/dsh/real/real-03-epoch-summary.ts
bun lab/dsh/real/real-04-builtin-compaction.ts
```

### 公共参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--persona <路径>` | 仓库示例人设 `channels/chenlulu/CLAUDE.md` | 想看自己人设的真实 token 数就指过去，例如 `--persona ~/.claude/channels/<bot>/CLAUDE.md` |
| `--model <id>` | `deepseek-v4-flash` | DeepSeek 模型 |
| `--effort off\|low\|high\|max` | `low` | 思考强度 |

### 密钥怎么走

- 密钥只经环境变量交给 dsh 进程。
- dsh 的请求先发到本机一个"用量记录代理"（`lib/usage-tap.ts`），再由代理原样转发给 `api.deepseek.com`。
- 代理只记录每个请求的形状和 DeepSeek 回报的用量（输入 token、缓存命中/未命中、输出 token）。它不记录密钥，默认也不记录对话正文。
- 报告里不会出现密钥。

### 每个脚本测什么、预期是什么

| 脚本 | 测什么 | 预期 | 大致请求数 |
|---|---|---|---|
| `real-01-persona-tokens` | 真实对话；"只留人设"后一次请求的实际输入 token；和"出厂配置 + 人设""出厂配置无人设"对比 | B（只留人设）首轮输入 ≈ 人设 token + 几十；A − B 在 0.1.5 上约 7000–9000、在 0.2.0 上约 5000–7000；第 2 轮缓存命中 ≈ 第 1 轮输入 | 6–10 |
| `real-02-reasoning-billing` | 上一轮的"思考内容"会不会被当成输入计费（先直接调接口对比，再经 dsh 跑 4 轮，思考开/关各一遍） | 差值接近 0 = 不计费；约 1000–1500 = 计费。结果决定陪伴聊天该不该开思考、每段聊多长 | 约 14 |
| `real-03-epoch-summary` | 新系统打算用的"分段续聊"：在会话里让模型写陪伴聊天格式的交接摘要，再开新会话只给"摘要 + 最近几轮原话" | 摘要五个小标题齐全，保留实际说过的话、答应的事、对方的事实；写摘要那次请求缓存命中 ≥ 80%；新会话能答对 | 约 15–20 |
| `real-04-builtin-compaction` | dsh 出厂压缩何时触发、写出什么；dsh 估的上下文大小和 DeepSeek 回报的差多少 | 保留 2000 时很可能一直不压缩（中文被按 4 字符 1 token 低估）；保留 500 时会压，摘要是英文"编程助手检查点"格式；两列数字大致同步 | 最多约 45 |

用 `deepseek-v4-flash` 时，四个脚本的输入加起来大约几十万 token，而且大部分会命中缓存。

### 用自己的人设时："只出数字"

- 加了 `--persona` 时，脚本默认进入"只出数字"模式：报告里不写模型按人设说出的任何文字（回复、摘要、回答），只保留用量数字和"是 / 否"式的自动检查。
- 只想自己看文字、不上传的话，加 `--with-text`。
- 用示例人设时也想只出数字，加 `--numbers-only`。

### 跑完贴什么

每个脚本最后都会打印报告文件的路径，路径形如 `lab/dsh/.runs/real-0X-<时间>/report.md`，把这几个 `report.md` 贴回来就行。

- 用的是示例人设：报告里只有合成对话和数字，可以整份贴。
- 用的是你自己的人设：报告里会有 bot 按你的人设说的话。请删掉"模型可见回复"和"交接摘要"两节再贴，只留数字。

## 3. 现有部署的脱敏盘点与用量基线（在作者本机跑，只读）

```bash
python3 lab/dsh/local/inventory.py --repo <旧仓库目录> --out INVENTORY.md
python3 lab/dsh/local/baseline_claude_usage.py --days 30 --out BASELINE.md
```

- `inventory.py`：列出各 bot 目录里有哪些文件、配置里有哪些字段、人设有多长、有几处 `{{…}}`、对应的 Claude Code 会话目录有多大、装了哪些 launchd 任务，以及各工具的版本。
- `baseline_claude_usage.py`：从旧的 Claude Code 会话文件里统计每次请求的输入、缓存命中率、输出（按 message.id 去重）。新系统上线后用同一口径对比。
- 两个脚本都只输出结构和数字，不输出任何内容、ID、密钥。bot 名默认换成 bot1、bot2……（`--keep-names` 才保留）。
- 只依赖 Python 标准库。

本机 AI 的完整任务书见 `docs/dsh-migration/LOCAL-AGENT-PROMPT.md`。

## 目录

```
lab/dsh/
  lib/acp-client.ts   最小 ACP 客户端（按行 JSON-RPC）
  lib/fake-llm.ts     假模型服务（OpenAI Chat Completions 与 Anthropic Messages 两种协议，记录 dsh 发出的请求）
  lib/mcp-http.ts     最小 MCP 服务（HTTP），给 dsh 挂我们自己的工具
  lib/profile.ts      "只留人设"补丁层、隔离目录与环境变量白名单
  lib/usage-tap.ts    用量记录代理
  lib/real.ts         真密钥脚本的公共部分
  lib/report.ts       生成 report.md
  offline/facts.ts    离线事实核对（21 项）
  offline/dry-run-real.ts  用假模型演练真密钥脚本
  real/real-0*.ts     真密钥脚本
  local/inventory.py  现有部署脱敏盘点
  local/baseline_claude_usage.py  旧部署用量基线
```
