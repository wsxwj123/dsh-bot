# gateway：每个 bot 一个网关进程

取代旧的 `dispatcher/`（调度器 + worker 管理器）。大脑换成 DeepSeek Harness（dsh），通过 ACP 协议驱动。
设计见 `docs/dsh-migration/PLAN.md`，各期的说明和真机清单见 `docs/dsh-migration/M1.md`、`M2.md`、`M3.md`、`M4.md`、`M5.md`、`M6.md`；切换手册见 `CUTOVER.md`。

## 运行

```bash
bun gateway/scripts/setup.ts harness                       # 装钉死版本的 dsh
bun gateway/scripts/setup.ts credentials                   # 生成密钥文件模板，然后自己填
bun gateway/scripts/setup.ts bot <名> --from <旧频道目录>   # 复制人设等文件、导入旧长期记忆，生成配置
bun gateway/scripts/setup.ts memory <名> --from <旧频道目录> # 再导一次旧系统的长期记忆（Claude Code 的 auto-memory）
bun gateway/scripts/setup.ts check ~/.dsh-bot/configs/<名>.yml
bun gateway/src/main.ts --config ~/.dsh-bot/configs/<名>.yml
```

查看状态：`bun gateway/scripts/health.ts --config …`；运行报告（只有数字）：`bun gateway/scripts/report.ts --config …`。

## 主人命令（在 Telegram 里发，只有主人能用）

| 命令 | 作用 |
|---|---|
| `/model` | 看现在用的模型、这段对话用了多少；私聊里不带参数时打开按钮引导（切换模型、思考强度、管理自建供应商的模型、换回配置文件里的） |
| `/model list`（`/models`） | 列出能换的模型（dsh 回报的列表，含配置里的自定义路由） |
| `/model <模型>`、`/model <供应商>/<模型>` | 换模型。这个 bot 的所有聊天都换，重启后保持；配置文件里的模型改了就以配置文件为准 |
| `/model default` | 换回配置文件里的模型 |
| `/provider`（`/providers`） | 列出供应商和密钥配没配（只说配没配，不显示密钥）；私聊里不带参数时打开按钮引导（新建、修改、删除、刷新模型、切到这家） |
| `/provider <供应商>` | 换到这个供应商的第一个模型；缺密钥时不换 |
| `/provider add <名字> <地址> <密钥> [openai]` | 一行新建/更新自建供应商；收到后马上删掉这条消息，末尾 `openai` 表示 OpenAI 格式（不写按 Anthropic） |
| `/provider refresh <名字>` | 重新拉自建供应商的模型列表 |
| `/compact [要特别留意的事]` | 现在就换段：在旧会话里写摘要（可带重点），新会话带着摘要和最近的原话 |
| `/clear` | 清空：写摘要后换新会话，不带清空前的原话 |
| `/help`（`/commands`） | 命令说明 |

用法参照 OpenClaw 的同名命令。命令不会交给模型；别人发的命令网关直接忽略。

## 自建供应商与按钮引导

新人也能用：主人私聊里发 `/provider`、`/model`（不带参数）会打开按钮引导，一步一步新建、修改、删除供应商，刷新或手工增删模型，切换模型、调思考强度。每一步都有「取消」，10 分钟没有动作自动退出。需要输密钥的那一步，bot 收到后会立刻删掉那条消息，删不掉会提醒手动删。

**共用文件**（所有新系统 bot 共用，都在 `<根>` 下，默认 `~/.dsh-bot`）：
- `providers.json`：自建供应商的地址、接口格式、模型、密钥键名；权限 600，不存密钥本身。
- `credentials.yaml`：dsh 的凭据文件；自建供应商的密钥存在 `refs.<键名>` 下，引导只增删改自己那几行，其它行原样保留。
- `providers.lock`：改上面两个文件时用的锁。多个 bot 会同时读写同一份 `providers.json`，所以按"抢到锁 → 重读 → 改 → 写"串行来做，避免同时改丢了别人的更新；手动编辑这两个文件前，先确认没有引导正在保存。

写完各 bot 网关会在空闲时重启 dsh，几秒内生效；只改密钥不用重启。换到另一家供应商会换段（先在旧会话写一份交接摘要，再开新会话），同一家换模型不换段。

**新增的配置键**（`gateway` 段，毫秒，不是整数或超出范围启动就报错）：`wizard_timeout_ms`（引导超时，默认 600000）、`late_secret_window_ms`（引导异常结束后把"像密钥"的消息删掉的时间窗，默认 600000）、`provider_lock_wait_ms`（等锁上限，默认 10000）、`model_fetch_timeout_ms`（拉模型列表每个请求的超时，默认 15000）、`provider_key_grace_ms`（删供应商后多久才删它的密钥，默认 600000）、`provider_switch_summary_ms`（换段时写交接摘要的上限，默认 30000）。

**相关入口**：本机接口 `GET /v1/model` 多返回一个 `providers` 字段、`POST /v1/provider/refresh` 刷新自建供应商；管理台在 `/hub/dsh-model` 页（bot 表格下面有自建供应商小表和「刷新模型列表」按钮）。旧系统的 `/hub/provider` 不动。

## 测试

```bash
cd gateway && bun install && bun run typecheck && bun run test
# 真 dsh + 假模型（可选）：先在 harness/ 里 npm ci，再
DSH_BOT_HARNESS=../harness bun test --timeout 90000 test/e2e/real-dsh.test.ts
```

测试不需要任何密钥：Telegram 和 dsh 都用假的（`test/fakes/`），网关以子进程方式真实启动，崩溃测试是真的 `kill -9`。

## 目录

```
src/
  main.ts            入口：加载配置、单实例锁、拉起各部分、优雅退出
  config.ts          configs/<bot>.yml（brain、gateway 两段）、access.json、令牌、凭据文件检查
  ledger.ts          送达账本（sqlite）：收到 / 开轮 / 发送，启动时的恢复规则；段、摘要、记忆事件
  ledger-schema.sql  账本表结构（Python 周边的测试也用这一份）
  memory.ts          长期记忆：<频道目录>/memory/MEMORY.md，remember 工具往"随手记"里追加
  commit/            承诺：中文时间说法解析、许诺识别（when.ts），登记、到点、睡觉顺延、兑现判定（service.ts）
  life/              作息查询（调 hang_situation.py --plan）、被晾追问（从旧调度器原样搬来）
  log.ts             落盘、按大小轮转、写盘前脱敏
  util.ts            小工具；测试用的崩溃点（DSH_BOT_CRASH_AT）
  telegram/
    api.ts           最小 Bot API 客户端，发送失败三分类
    inbound.ts       白名单闸门、消息 → 账本记录、命令解析
    poller.ts        长轮询
    sender.ts        分段发送、逐段记账、文件白名单
    send_plan.ts     [[图N]] 插图规则（从旧调度器原样搬来）
  dsh/
    acp.ts           ACP 客户端（JSON-RPC over stdio）
    process.ts       dsh 进程：环境变量白名单、单实例、逐级停止
    profile.ts       "只留人设"补丁层、运行规则
  mcp/server.ts      给 dsh 的工具服务（每个会话一个地址和口令）
  engine/
    engine.ts        回合调度、出错补救、崩溃恢复、换段与交接摘要、换模型、/clear、健康
    format.ts        每轮送给模型的格式、新段开头（长期记忆 + 摘要 + 最近原话）、摘要指令
  providers/         自建供应商：读写 providers.json 与凭据、拉模型列表、新建/修改/删除/刷新
  wizard/            /provider、/model 的按钮引导（状态机、文案与键盘、两步流程）
  api/server.ts      本机接口（口令、只听 127.0.0.1、拒绝 Origin、只收 JSON）
scripts/             setup / health / report
test/                unit/、e2e/、fakes/
```
