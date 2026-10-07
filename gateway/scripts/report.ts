// 真机验收用的报告：只出数字和"有 / 没有"，不出任何聊天内容、用户 id、密钥。可以整份贴回来。
//   bun gateway/scripts/report.ts --config <配置文件> [--hours 24]
import { existsSync, readdirSync, readFileSync, statSync } from 'fs'
import { join } from 'path'
import { loadBotConfig, readTelegramToken } from '../src/config'
import { Ledger } from '../src/ledger'

const arg = (n: string) => { const i = process.argv.indexOf(`--${n}`); return i >= 0 ? process.argv[i + 1] : undefined }
const path = arg('config')
if (!path) { console.log('用法：bun gateway/scripts/report.ts --config <配置文件> [--hours 24]'); process.exit(2) }
const cfg = loadBotConfig(path)
const hours = Number(arg('hours') ?? 24)
const since = Date.now() - hours * 3600_000
const led = new Ledger(join(cfg.stateDir, 'ledger.sqlite'))
const q = <T>(sql: string, ...p: (string | number)[]) => led.db.query(sql).all(...p) as T[]
const rows = (r: { k: string; n: number }[]) => r.map(x => `${x.k} ${x.n}`).join('，') || '无'

const out: string[] = []
const w = (s = '') => out.push(s)
w(`# ${cfg.id} 运行报告（最近 ${hours} 小时）`)
w()
w('只含数字，不含聊天内容、用户 id 和密钥。')
w()
w('## 收到的消息')
w(`- 按状态：${rows(q(`SELECT state AS k, COUNT(*) AS n FROM inbound WHERE received_at > ? GROUP BY state`, since))}`)
w(`- 按类型：${rows(q(`SELECT kind AS k, COUNT(*) AS n FROM inbound WHERE received_at > ? GROUP BY kind`, since))}`)
w(`- 现在还没处理完的：${(q<{ n: number }>(`SELECT COUNT(*) AS n FROM inbound WHERE state IN ('pending','in_turn')`)[0]?.n ?? 0)}`)
w()
w('## 回合')
w(`- 按结局：${rows(q(`SELECT state AS k, COUNT(*) AS n FROM turns WHERE started_at > ? GROUP BY state`, since))}`)
w(`- 按类型：${rows(q(`SELECT kind AS k, COUNT(*) AS n FROM turns WHERE started_at > ? GROUP BY kind`, since))}（retry = 报错后的补救，nudge = 没回也没选择沉默时的提醒）`)
w(`- 选择不回复（stay_silent）：${(q<{ n: number }>(`SELECT COUNT(*) AS n FROM turns WHERE started_at > ? AND silent = 1`, since)[0]?.n ?? 0)}`)
const used = q<{ u: number }>(`SELECT used_tokens AS u FROM turns WHERE started_at > ? AND used_tokens IS NOT NULL ORDER BY used_tokens`, since).map(r => r.u)
if (used.length) w(`- 每轮上下文大小（dsh 回报，token）：中位数 ${used[Math.floor(used.length / 2)]}，最大 ${used[used.length - 1]}，共 ${used.length} 轮`)
w()
w('## 发出去的消息')
w(`- 按状态：${rows(q(`SELECT kind || '/' || state AS k, COUNT(*) AS n FROM outbound WHERE created_at > ? GROUP BY kind, state`, since))}`)
const dup = q<{ n: number }>(`SELECT COUNT(*) AS n FROM (SELECT chat_id, text FROM outbound WHERE created_at > ? AND kind = 'text' AND state IN ('sent','ambiguous') GROUP BY chat_id, text, turn_id HAVING COUNT(*) > 1)`, since)[0]?.n ?? 0
w(`- 同一轮里同一句话发了不止一次：${dup}`)
w()
w('## 会话（段）')
w(`- 按状态：${rows(q(`SELECT state || COALESCE('/' || close_reason, '') AS k, COUNT(*) AS n FROM segments WHERE created_at > ? GROUP BY state, close_reason`, since))}（closed/budget = 用量到线换段，closed/pre-budget = 新消息太长先换段，closed/clear = /clear，closed/prompt-changed = 人设、运行规则或 dsh 版本变了）`)
const rolled = q<{ data: string }>(`SELECT data FROM events WHERE kind = 'segment_rolled' AND at > ?`, since).map(r => JSON.parse(r.data) as { summary: string; summary_chars: number })
if (rolled.length) {
  const by = (k: string) => rolled.filter(r => r.summary === k).length
  w(`- 换段 ${rolled.length} 次：摘要在旧会话里写成 ${by('in-session')} 次，用账本补写 ${by('ledger')} 次，没写成（沿用上一份）${by('none')} 次；摘要平均 ${Math.round(rolled.reduce((n, r) => n + r.summary_chars, 0) / rolled.length)} 字`)
}
const fromLedger = q<{ n: number }>(`SELECT COUNT(*) AS n FROM events WHERE kind = 'summary_from_ledger' AND at > ?`, since)[0]?.n ?? 0
if (fromLedger) w(`- 崩溃或续接失败后用账本补写摘要：${fromLedger} 次`)
w()
w('## 承诺')
w(`- 按状态：${rows(q(`SELECT state || '/' || source AS k, COUNT(*) AS n FROM commitments WHERE created_at > ? AND note IS NOT 'vague: hinted' GROUP BY state, source`, since))}（source：tool = 模型自己登记，auto = 网关从说过的话里补登记）`)
w(`- 时间说得含糊、提醒模型自己登记：${q<{ n: number }>(`SELECT COUNT(*) AS n FROM commitments WHERE created_at > ? AND note = 'vague: hinted'`, since)[0]?.n ?? 0}`)
w(`- 合成消息（程序塞给模型的）：${rows(q(`SELECT COALESCE(json_extract(meta, '$.source'), '?') AS k, COUNT(*) AS n FROM inbound WHERE kind = 'synthetic' AND received_at > ? GROUP BY k`, since))}`)
w()
w('## 长期记忆')
w(`- 新记下的条数（remember）：${q<{ n: number }>(`SELECT COUNT(*) AS n FROM memories WHERE at > ?`, since)[0]?.n ?? 0}`)
w()
w('## 日志、账本里有没有机密')
const secrets: string[] = []
try { secrets.push(readTelegramToken(cfg.channelDir, {})) } catch {}
try {
  // 按 YAML 读：/provider add 写进去的值带引号
  const refs = (Bun.YAML.parse(readFileSync(cfg.credentialsPath, 'utf8')) as { refs?: Record<string, unknown> })?.refs ?? {}
  for (const v of Object.values(refs)) if (typeof v === 'string' && v.trim().length >= 8) secrets.push(v.trim())
} catch {}
let hits = 0
let scanned = 0
// 日志目录、状态目录（账本和它的 WAL）、dsh 的补丁层、/provider add 的登记文件
const files: string[] = [join(cfg.dshHome, 'bot.patch.yml'), cfg.providersPath]
for (const dir of [cfg.logsDir, cfg.stateDir]) {
  if (!existsSync(dir)) continue
  for (const f of readdirSync(dir)) files.push(join(dir, f))
}
for (const p of files) {
  if (!existsSync(p) || !statSync(p).isFile()) continue
  scanned++
  const buf = readFileSync(p)
  for (const s of secrets) if (buf.includes(s)) { hits++; w(`- ⚠️ ${p.replace(cfg.root, '<根>')} 里出现了机密原文`) }
}
w(`- 检查了 ${scanned} 个文件（日志、账本、补丁层、供应商登记）、${secrets.length} 个机密值：${hits === 0 ? '没有发现' : `发现 ${hits} 处`}`)
led.close()
console.log(out.join('\n'))
