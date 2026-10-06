# 本机 AI 任务书：M5 真机验收（两个新系统 bot 在一个测试群里，导演点名）

你在用户的 Mac 上工作，接着 M4 的活。新代码在仓库 `wsxwj123/dsh-bot` 的分支 `claude/nice-brahmagupta-dtjsma` 上（M1–M4 已经合进 master）。

## 先读

- `docs/dsh-migration/M5.md`：这一期做了什么、怎么用
- `docs/dsh-migration/LOCAL-AGENT-M3.md`：铁律和收尾方式，这次照样适用

## 铁律

和 M3、M4 相同，违反任何一条就停下来问用户。补充三条：

1. **不读群聊记录的内容。** `~/.dsh-bot/groups/*.jsonl` 里是群里的聊天，只数行数（`wc -l`），或者用下面给的命令只出数字。
2. **群 id 不写进报告**，写成 `<group>`。
3. **停旧 bot 之前先问用户**：这次要再停一个旧 bot。停了以后，它在旧群里就不说话了。

## 步骤

### 1. 拉代码、跑自动化测试

```bash
git fetch origin claude/nice-brahmagupta-dtjsma && git checkout claude/nice-brahmagupta-dtjsma && git pull
cd gateway && bun install && bun run typecheck && bun run test
DSH_BOT_HARNESS=~/.dsh-bot/harness bun test --timeout 90000 test/e2e/real-dsh.test.ts
cd ../tests && python3 -m pytest -q unit/test_chat_history_ledger.py unit/test_chat_history_namespace.py unit/test_project_slug.py unit/test_m3_life.py unit/test_m4_dsh.py unit/test_m5_director.py
```

预期：
- 网关 220 项（通过 215，跳过 5）；
- 真 dsh 5 项通过；
- Python 39 项通过。

### 2. 再迁一个 bot（先问用户迁哪个）

1. 问用户第二个 bot 用哪个旧 bot，下面叫它 `<新名>`；报告里写 `botX`。
2. 停旧 bot5（如果在跑）和这个旧 bot，做法同 M2。先把命令告诉用户，用户同意再执行。
3. 迁过来：`bun gateway/scripts/setup.ts bot <新名> --from <旧频道目录>`。
   - 新配置的端口会自动用 17951，不会和 bot5 冲突。
   - 在新配置里加一行 `life_config: <它的旧配置文件完整路径>`，和 M3 给 bot5 加的一样。
   - `gateway:` 下面加 M4 那两行（`image_skill_dir`、`botlife_db`）。
4. 运行 `bun gateway/scripts/setup.ts check ~/.dsh-bot/configs/<新名>.yml`，预期"全部就绪"。

### 3. 建测试群

请用户做这几件事：
1. 新建一个 Telegram 群，把 bot5 和 botX 拉进去；
2. 在 BotFather 里给这两个 bot 关掉隐私模式（`/setprivacy` → 选 bot → Disable），关掉以后要把 bot 移出群再拉回来才生效；
3. 在群里随便说一句。

### 4. 启动两个网关

用 tmux 启动，用户想看的时候可以 `tmux attach`：

```bash
tmux new -d -s dsh-bot5 'bun gateway/src/main.ts --config ~/.dsh-bot/configs/bot5.yml'
tmux new -d -s dsh-botx 'bun gateway/src/main.ts --config ~/.dsh-bot/configs/<新名>.yml'
```

查群 id：群还没登记，网关会记一条 `inbound.dropped`。下面的命令把群 id 读进变量，不打印出来：

```bash
G=$(grep -h '"inbound.dropped"' ~/.dsh-bot/bots/bot5/logs/gateway.log | grep -o '"chat":-[0-9]*' | tail -1 | cut -d: -f2)
[ -n "$G" ] && echo "找到了群 id" || echo "没找到：请用户在群里再说一句"
```

把群写进两个 bot 的 `access.json`（保留原来的内容和权限）：

```bash
for b in bot5 <新名>; do python3 - "$b" "$G" <<'EOF'
import json, os, sys
p = os.path.expanduser(f"~/.dsh-bot/bots/{sys.argv[1]}/channel/access.json")
a = json.load(open(p, encoding="utf-8"))
a.setdefault("groups", {})[sys.argv[2]] = {"allowFrom": []}
tmp = p + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(a, f, ensure_ascii=False, indent=2)
os.chmod(tmp, os.stat(p).st_mode)
os.replace(tmp, p)
print(sys.argv[1], "已登记群")
EOF
done
```

`access.json` 是热读取的，不用重启网关。

### 5. 启动导演

```bash
mkdir -p ~/.dsh-bot/director/mode && touch ~/.dsh-bot/director/mode/$G
tmux new -d -s dsh-director "cd $(pwd) && HUB_CONFIGS_DIR=$HOME/.dsh-bot/configs DIRECTOR_CHAT_ID=$G python3 director.py --run 2>&1 | tee -a $HOME/.dsh-bot/director/director.log"
sleep 3; head -1 ~/.dsh-bot/director/director.log
```

预期第一行是"[director] 启动，……开关=开，新系统 bot：bot5、<新名>"。报告里把 `<新名>` 写成 botX。

### 6. 真机清单

每一项记"符合"或"不符合"，加一句现象（不写聊天内容）。

| 项 | 怎么做 | 预期 |
|---|---|---|
| a. 点名 | 用户在群里说一句话 | 两个网关都出现 `inbound.observed`。群聊记录只多 1 行（`wc -l ~/.dsh-bot/groups/$G.jsonl`）。十几秒内导演日志出现 `"action": "inject"`，被点名的那个 bot 在群里回话，另一个不说 |
| b. 接话有上限 | 用户说一句之后不再说话，等 3 分钟 | 两个 bot 你一句我一句，最多 5 句就停（导演的接话额度）。数一下最后一条真人消息之后 bot 说了几句：用下面的命令 |
| c. "闭嘴" | 用户在群里说"闭嘴" | 导演日志出现 `"action": "lock"`，之后 30 分钟内用户再说话也没有 bot 接（可以只观察 5 分钟） |
| d. 群里近况 | 群里聊过之后，用户私聊 bot5 一句 | bot5 的日志出现 `group.recap`（带句数）；bot5 的回复能接上群里的话（用户判断）。再私聊一句，不再出现 `group.recap` |
| e. 正在私聊不被点 | 用户和 bot5 私聊的同时（30 分钟内），群里冷场 45 分钟以上（可选，比较久） | 导演自己开场时不选 bot5（日志里 `busy_skip` 或开场人是 botX） |
| f. 跑一晚上 | 网关和导演整夜开着，第二天早上看 | 统计见下面。没有进程退出，`✗` 错误很少；导演的 `llm_failed` 如果有，间隔越来越长，不是每 2 秒一次 |

**第 b 项命令**（只出数字）：

```bash
python3 - "$G" <<'EOF'
import json, os, sys
rows = [json.loads(l) for l in open(os.path.expanduser(f"~/.dsh-bot/groups/{sys.argv[1]}.jsonl"), encoding="utf-8") if l.strip()]
last = max((i for i, r in enumerate(rows) if not r["is_bot"]), default=-1)
print("最后一条真人消息之后 bot 说了", sum(1 for r in rows[last + 1:] if r["is_bot"]), "句")
EOF
```

**第 f 项统计**（只出数字）：

```bash
for b in bot5 <新名>; do echo "$b 错误条数：$(grep -c '"level":"error"' ~/.dsh-bot/bots/$b/logs/gateway.log)"; done
echo "导演点名：$(grep -c '"action": "inject"' ~/.dsh-bot/director/director.log)"
echo "自主开场/续轮：$(grep -c '"action": "scene_' ~/.dsh-bot/director/director.log)"
echo "调模型失败：$(grep -c 'llm_failed' ~/.dsh-bot/director/director.log)"
python3 - "$G" <<'EOF'
import collections, json, os, sys
rows = [json.loads(l) for l in open(os.path.expanduser(f"~/.dsh-bot/groups/{sys.argv[1]}.jsonl"), encoding="utf-8") if l.strip()]
c = collections.Counter("bot" if r["is_bot"] else "真人" for r in rows)
print("群聊记录：共", len(rows), "行；真人", c["真人"], "行；bot", c["bot"], "行")
EOF
```

错误条数不为 0 时，用 `bun gateway/scripts/logs.ts --config <配置> -n 500 | grep ✗` 看事件名，只记事件名和次数。

### 7. 收尾

1. 两个网关还在跑的时候，各跑一遍 `setup.ts check`、`health.ts`、`report.ts --hours 24`，输出放进报告（路径里的系统用户名改成 `~`，聊天 id 和群 id 改成 `<chat>`、`<group>`）。
2. 停导演和网关：`tmux kill-session -t dsh-director`，再停另外两个（`tmux kill-session -t dsh-bot5`、`-t dsh-botx`）。
3. 问用户要不要恢复旧 bot5 和旧 botX，并说明会重启所有旧 bot。按用户的意思执行。
4. 新加的配置、`access.json` 里的群、导演开关文件都保留。

## 交付

1. 写 `docs/dsh-migration/local/reports/m5-check.md`，包括：
   - 环境；
   - 第 1 步的测试结果；
   - 第 2 步的检查结果；
   - 第 6 步每一项的结果和统计数字；
   - 第 7 步三条命令的输出；
   - 发现的问题：复现步骤、现象、事件名。
2. 提交前，在 `git diff` 里搜一遍：`sk-`、令牌格式、用户的 Telegram 用户 id、群 id、系统用户名、bot 的旧名字。确认都没有。
3. 从 `claude/nice-brahmagupta-dtjsma` 拉出新分支 `local/m5-check`，推到这个分支。
4. 向用户汇报时用平实的中文，先说结论。最后一句写："M5 实测已推到 local/m5-check"。
