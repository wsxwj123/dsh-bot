# 给本机 AI 的任务书：DeepSeek Harness 迁移的本机准备工作

> 用法：把下面整份交给在作者本机运行的 AI。它读完后按步骤执行，做完推到 `local/setup` 分支，再由云端的 Claude 接手开发。

---

## 背景

用户有一套 Telegram 陪伴机器人引擎，现在用 Claude Code 当"大脑"：

- 公开版在 `euphoriaaaaaa1/claude-tgbot`；
- 用户自己机器上跑的是一套功能更多的私有部署。

现在要把大脑换成 DeepSeek Harness（命令行名 `dsh`）。云端的 Claude 已经写好了方案和验证工具，都在 GitHub 仓库 `wsxwj123/dsh-bot` 的分支 `claude/nice-brahmagupta-dtjsma` 上：

- `docs/dsh-migration/PLAN.md`：方案。第九节是开工前要用户确认的问题。
- `docs/dsh-migration/VERIFIED.md`：dsh 的离线实测核对记录。
- `lab/dsh/`：验证脚本。用法见 `lab/dsh/README.md`。

## 你的任务

只做**必须在用户本机做**的事：

1. 和用户确认方案里的决定，记下来；
2. 经用户同意，给仓库建好 `master` 基准分支；
3. 在用户的系统上跑离线核对；
4. 用用户的 DeepSeek 密钥跑真密钥验证；
5. 对现有部署做脱敏盘点，统计旧系统的用量基线；
6. 写交接说明，推到 `local/setup` 分支。

**你不写新系统的代码。** 新系统由云端 Claude 按方案开发。两边同时写，代码会分叉。

## 铁律（每一步都要遵守）

1. **绝不往仓库提交以下内容**：
   - 任何密钥、令牌、口令；
   - Telegram 的 user_id、chat_id；
   - 人设正文、聊天内容、记忆正文、关系数值；
   - 私有部署的源代码。除非用户逐段看过并明确同意，而且其中不含以上任何一种。
2. **不动正在运行的旧部署**：
   - 不重启、不停止任何 bot；
   - 不修改 `~/.claude` 下的任何文件；
   - 不改 launchd 或计划任务。

   盘点只用仓库里的只读脚本。
3. **不用用户日常的 dsh**：
   - 验证用的 dsh 一律装到 `~/dsh-lab/` 下的独立目录；
   - 不装全局包；
   - 不读写 `~/.dsh`。验证脚本自己会建临时的 HOME 和 DSH_HOME。
4. **密钥只在当前终端的环境变量里**：
   - 用 `read -rs` 让用户输入，不回显，不进命令历史；
   - 不写进任何文件，不打印；
   - 做完就 `unset`。
5. **提交前必做三件事**：
   - 跑第 9 步的密钥扫描；
   - 把要提交的文件清单和 `HANDOFF.md` 给用户看；
   - 用户确认后再提交、推送。
6. **推送范围**：
   - 只推 `local/setup` 分支；
   - `master` 只在第 2 步、经用户同意后创建一次；
   - 不推其它分支，不开拉取请求；
   - 你的产出全部放在 `docs/dsh-migration/local/`，不改 `PLAN.md`、`VERIFIED.md`。

   如果发现验证脚本在用户的系统上有 bug，可以修，但单独做一个提交，并在交接说明里写清楚改了什么、为什么。
7. **不确定就问用户**，不要猜。验证结果和预期不符时如实记录，不要为了"通过"去改脚本里的预期。

---

## 步骤

### 第 0 步：准备

1. 检查工具和版本：`git`、`bun`（≥ 1.1）、`node` 和 `npm`、`python3`（≥ 3.10）。缺什么请用户装，不要自己全局安装。
2. 克隆并切分支：
   ```bash
   git clone https://github.com/wsxwj123/dsh-bot.git ~/dsh-bot-work
   cd ~/dsh-bot-work
   git checkout claude/nice-brahmagupta-dtjsma
   git checkout -b local/setup
   mkdir -p docs/dsh-migration/local/reports docs/dsh-migration/local/locks
   ```
3. 通读三份文件：`docs/dsh-migration/PLAN.md`（重点第一、四、九节）、`docs/dsh-migration/VERIFIED.md`、`lab/dsh/README.md`。

### 第 1 步：和用户确认决定，写进 `DECISIONS.md`

把 `PLAN.md` 第九节的 8 个问题逐个问用户：先念我的建议，再记下用户的回答。另外再问两件事：

- 愿不愿意新建一个**测试用 Telegram bot**，后面真机验收要用。只记 bot 用户名，令牌留在本机。
- 真实人设的 token 测量用**哪个 bot 的人设**。仓库里只记别名，不记真名。别名就是第 6 步盘点给出的 bot1、bot2……，这一格可以等第 6 步跑完再填。

写成 `docs/dsh-migration/local/DECISIONS.md`，格式如下：

```markdown
# 用户决定（日期）

| # | 问题 | 建议 | 用户的决定 | 补充说明 |
|---|---|---|---|---|
| 1 | 替换还是并存；能否创建 master | 替换；同意创建 | … | … |
| 2 | 每次请求输入预算 | 4 万 | … | … |
| 3 | 第一期范围 | M1–M3 | … | … |
| 4 | 是否支持 Windows | 先不做 | … | … |
| 5 | 私有部署实现能否参考 | 不贴就从头做 | … | … |
| 6 | 密钥验证方式 | 本机跑脚本 | … | … |
| 7 | 旧对话是否迁移 | 迁最近 30 天 | … | … |
| 8 | dsh 版本 | 0.2.0-rc.2 | … | … |
| 9 | 测试用 bot | 新建 | … | 用户名：… |
| 10 | 用哪个 bot 的人设测 token | — | … | 别名：… |
```

### 第 2 步：仓库基准分支（只在用户同意问题 1 时做）

```bash
cd ~/dsh-bot-work
git remote add upstream https://github.com/euphoriaaaaaa1/claude-tgbot.git
git fetch upstream master
git ls-remote --heads origin master    # 必须为空，说明远端还没有 master；不为空就停下来问用户
git cat-file -t 7a0529d                # 应输出 commit
git push origin 7a0529d:refs/heads/master
```

- 推的必须**正好是 7a0529d**，方案就是基于它写的。如果上游已有更新的提交，不要用更新的，在交接说明里记一笔。
- 如果用户装了并登录了 `gh`，可以经用户同意把默认分支设成 master：`gh repo edit wsxwj123/dsh-bot --default-branch master`。没有 `gh` 就请用户到 GitHub 网页的 Settings 里改。

### 第 3 步：装两份 dsh 到独立目录

```bash
mkdir -p ~/dsh-lab/020 && cd ~/dsh-lab/020 && npm init -y >/dev/null && npm install --no-fund --no-audit @deepseek-ai/dsh@0.2.0-rc.2
mkdir -p ~/dsh-lab/015 && cd ~/dsh-lab/015 && npm init -y >/dev/null && npm install --no-fund --no-audit @deepseek-ai/dsh@0.1.5-rc.1
~/dsh-lab/020/node_modules/.bin/dsh --version
~/dsh-lab/015/node_modules/.bin/dsh --version
cp ~/dsh-lab/020/package-lock.json ~/dsh-bot-work/docs/dsh-migration/local/locks/dsh-0.2.0-rc.2.package-lock.json
cp ~/dsh-lab/015/package-lock.json ~/dsh-bot-work/docs/dsh-migration/local/locks/dsh-0.1.5-rc.1.package-lock.json
```

锁文件里只有包名、版本和校验值，可以提交。它让云端拿到和你这台机器完全相同的依赖树。

### 第 4 步：离线核对（不需要密钥）

```bash
cd ~/dsh-bot-work
DSH=~/dsh-lab/020/node_modules/.bin/dsh bun lab/dsh/offline/facts.ts
DSH=~/dsh-lab/015/node_modules/.bin/dsh bun lab/dsh/offline/facts.ts
```

- 每次跑完，终端最后会打印报告路径。分别复制为 `docs/dsh-migration/local/reports/facts-0.2.0.md` 和 `facts-0.1.5.md`。
- 预期：除"仅记录"项外全部"符合"，"仅记录"项的值和 `VERIFIED.md` 一致。有"不符合"或"出错"的，如实写进交接说明，不要改 dsh，也不要改核对的预期。

### 第 5 步：真密钥验证

1. 让用户输入密钥（不回显）：
   ```bash
   read -rs DEEPSEEK_API_KEY && export DEEPSEEK_API_KEY
   ```
2. **示例人设，0.2.0**（必做）。报告可以整份提交：
   ```bash
   cd ~/dsh-bot-work
   export DSH=~/dsh-lab/020/node_modules/.bin/dsh
   bun lab/dsh/real/real-01-persona-tokens.ts
   bun lab/dsh/real/real-02-reasoning-billing.ts
   bun lab/dsh/real/real-03-epoch-summary.ts
   bun lab/dsh/real/real-04-builtin-compaction.ts
   ```
   每个报告复制为 `docs/dsh-migration/local/reports/real-0X-0.2.0-sample.md`。
3. **用户自己的人设，0.2.0**（必做 real-01 和 real-03）。用第 1 步选定的那个 bot：
   ```bash
   bun lab/dsh/real/real-01-persona-tokens.ts --persona <那个 bot 的 CLAUDE.md 路径>
   bun lab/dsh/real/real-03-epoch-summary.ts  --persona <同上>
   ```
   - 指定 `--persona` 时，脚本默认是"只出数字"模式：不写任何回复、摘要、回答正文。**不要加 `--with-text`。**
   - 复制为 `real-01-0.2.0-own.md` 和 `real-03-0.2.0-own.md`。复制前打开看一遍，确认里面只有数字、合成的测试句子和"是 / 否"。
4. **用户平时用的模型**（如果不是 `deepseek-v4-flash`）：再跑一次 real-01，加 `--model <那个模型>`，复制为 `real-01-0.2.0-<模型>.md`。
5. **0.1.5 对照**（用户同意才做）：把 `DSH` 换成 `~/dsh-lab/015/...`，用示例人设跑 real-01 到 real-03，复制为 `real-0X-0.1.5-sample.md`。
6. 结束后：
   ```bash
   unset DEEPSEEK_API_KEY
   ```

脚本失败时，把报错原文贴进交接说明（先确认报错里没有密钥），不要硬改脚本让它"跑过"。

### 第 6 步：现有部署盘点与用量基线（只读）

```bash
cd ~/dsh-bot-work
python3 lab/dsh/local/inventory.py --repo <旧仓库所在目录> --out docs/dsh-migration/local/INVENTORY.md
python3 lab/dsh/local/baseline_claude_usage.py --days 30 --out docs/dsh-migration/local/BASELINE.md
```

- 私有部署的目录可能和公开版不同，比如 bot 不在 `~/.claude/channels/`，或者会话目录名里没有 `channels`。这时：
  - 先问用户实际在哪；
  - 用 `--claude-home`、`--match`、`--all` 调整；
  - 在交接说明里写明用的什么参数。
- 两个文件生成后**逐行检查**：只应有版本号、文件名、字段名、数量、别名。发现任何像内容、ID、密钥的东西就删掉，并在交接说明里提一句。
- 如果用户同意问题 5，再写 `docs/dsh-migration/local/PRIVATE-NOTES.md`，**用文字描述**私有部署里这三项是怎么实现的：
  - 承诺兑现；
  - 送达账本；
  - 私聊压缩后补回。

  只写用到的文件或表名、字段名、触发时机、失败时怎么办。贴代码片段要用户逐段同意，而且不能含密钥、ID、人设和聊天内容。

### 第 7 步（可选）：为后续真机验收准备本机环境

只在用户同意时做，这些都**不提交**：

- 建 `~/.dsh-bot/` 目录（权限 700），以及 `~/.dsh-bot/credentials.yaml`（权限 600）。内容是 dsh 的凭据格式，值由用户自己填：
  ```yaml
  version: 1
  refs:
    DEEPSEEK_API_KEY: <用户自己填>
  ```
- 如果用户新建了测试 bot：令牌也写进这个文件，键名用 `TG_TESTBOT_TOKEN`。
- 在 `DECISIONS.md` 里只记"已建好，含哪些键名"，不记值。

### 第 8 步：写交接说明 `docs/dsh-migration/local/HANDOFF.md`

必须包含：

1. **环境**：系统、芯片架构、bun、node、python 的版本；两份 dsh 的版本和子包版本分布。
2. **做了哪些步骤**：每一步的结果（成功、失败、跳过，以及原因）。
3. **关键数字**，从报告里抄：
   - real-01：B 首轮输入、A − B、C 首轮输入，示例人设和自己的人设各一组；第 2 轮缓存命中。
   - real-02：直接调接口时，"带 / 不带"旧思考的差值，以及你的判读（计费 / 不计费 / 报错）。
   - real-03：写摘要那次请求的缓存命中率；"自动检查"表的结果；每轮请求数。
   - real-04：两种保留量下各自是否压缩、在第几轮压；"dsh 估"和"DeepSeek 回报"两列的差距。
   - BASELINE：每个 bot 的输入中位数、P90、缓存命中率、日请求数。
4. **和预期不符的地方**、你怀疑的问题。
5. **偏离任务书的地方**（包括修过的脚本）。
6. **用户决定的摘要**（链接到 `DECISIONS.md`）。
7. **想问云端 Claude 的问题**。

### 第 9 步：检查、提交、推送

```bash
cd ~/dsh-bot-work
git add docs/dsh-migration/local
git diff --cached --name-only
# 密钥扫描：有任何输出就停下来逐条确认
git diff --cached | grep -nE 'sk-[A-Za-z0-9]{16,}|[0-9]{8,10}:[A-Za-z0-9_-]{30,}|Bearer [A-Za-z0-9._-]{20,}|api_key\s*[:=]\s*\S{8,}|BEGIN [A-Z ]*PRIVATE KEY' || echo "未发现疑似密钥"
```

1. 把文件清单和 `HANDOFF.md` 全文给用户看，等用户确认。
2. 提交并推送：
   ```bash
   git commit -m "local(setup): 本机验证报告、用户决定、部署盘点与用量基线"
   git push -u origin local/setup
   ```
3. 告诉用户：可以回到云端 Claude 的会话说"本机准备已推到 local/setup"。

---

## 完成标准

- [ ] `DECISIONS.md`：10 项都有答案。
- [ ] `master` 已在 7a0529d 创建，或者用户明确不同意。
- [ ] `reports/facts-0.2.0.md`、`reports/facts-0.1.5.md`。
- [ ] `reports/real-01` 到 `real-04` 的 `-0.2.0-sample.md`。
- [ ] `reports/real-01-0.2.0-own.md`、`reports/real-03-0.2.0-own.md`，均为只出数字。
- [ ] `INVENTORY.md`、`BASELINE.md`，已逐行检查。
- [ ] `locks/` 下两个锁文件。
- [ ] `HANDOFF.md`。
- [ ] 密钥扫描通过，用户已过目，已推到 `local/setup`。

预计耗时 30–60 分钟（不含等用户回答）。用 `deepseek-v4-flash` 时，所有真密钥脚本加起来只有几十万输入 token，而且大部分命中缓存。
