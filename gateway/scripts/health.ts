// 查看一个 bot 的健康状况：bun gateway/scripts/health.ts --config <配置文件>
// 退出码：0 健康，1 不健康，2 连不上网关。看门狗可以直接用这个退出码。
import { readFileSync } from 'fs'
import { join } from 'path'
import { loadBotConfig } from '../src/config'

const i = process.argv.indexOf('--config')
const path = i >= 0 ? process.argv[i + 1] : undefined
if (!path) { console.log('用法：bun gateway/scripts/health.ts --config <配置文件>'); process.exit(2) }
const cfg = loadBotConfig(path)
const token = readFileSync(join(cfg.stateDir, 'api.key'), 'utf8').trim()
let res: Response
try {
  res = await fetch(`http://127.0.0.1:${cfg.apiPort}/v1/health`, { headers: { authorization: `Bearer ${token}` }, signal: AbortSignal.timeout(5_000) })
} catch {
  console.log(`连不上 ${cfg.id} 的网关（127.0.0.1:${cfg.apiPort}），它可能没在运行。`)
  process.exit(2)
}
const h = await res.json() as any
const sec = (ms: number | null) => (ms === null || ms === undefined ? '—' : `${Math.round(ms / 1000)} 秒前`)
console.log(`${cfg.id}：${h.ok ? '健康' : '不健康'}`)
for (const p of h.problems ?? []) console.log(`  ⚠️  ${p}`)
console.log(`  Telegram 最近一次拉取成功：${sec(h.telegram?.last_ok_ms)}`)
console.log(`  dsh：${h.dsh?.running ? `运行中（pid ${h.dsh.pid}，第 ${h.dsh.generation} 次启动）` : '没在运行（有消息时会自动启动）'}`)
console.log(`  模型：${h.brain?.provider} / ${h.brain?.model}（思考 ${h.brain?.effort ?? '默认'}）`)
console.log(`  正在进行的回合：${h.turns_running}`)
for (const c of h.chats ?? []) console.log(`  聊天 ${c.chat_id}：最老一条未处理消息等了 ${Math.round(c.oldest_unprocessed_ms / 1000)} 秒${c.turn ? `，这一轮 ${Math.round(c.turn.since_progress_ms / 1000)} 秒没有进展` : ''}`)
process.exit(h.ok ? 0 : 1)
