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
w('## 长期记忆')
w(`- 新记下的条数（remember）：${q<{ n: number }>(`SELECT COUNT(*) AS n FROM memories WHERE at > ?`, since)[0]?.n ?? 0}`)
w()
w('## 日志里有没有机密')
const secrets: string[] = []
try { secrets.push(readTelegramToken(cfg.channelDir, {})) } catch {}
try {
  const cred = readFileSync(cfg.credentialsPath, 'utf8')
  for (const m of cred.matchAll(/^\s{2,}[A-Z0-9_]+:\s*(\S.*)$/gm)) if (m[1]!.length >= 8) secrets.push(m[1]!.trim())
} catch {}
let hits = 0
let scanned = 0
for (const dir of [cfg.logsDir]) {
  if (!existsSync(dir)) continue
  for (const f of readdirSync(dir)) {
    const p = join(dir, f)
    if (!statSync(p).isFile()) continue
    scanned++
    const text = readFileSync(p, 'utf8')
    for (const s of secrets) if (text.includes(s)) { hits++; w(`- ⚠️ ${f} 里出现了机密原文`) }
  }
}
w(`- 检查了 ${scanned} 个日志文件、${secrets.length} 个机密值：${hits === 0 ? '没有发现' : `发现 ${hits} 处`}`)
led.close()
console.log(out.join('\n'))
