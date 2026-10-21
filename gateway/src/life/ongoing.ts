// 持续中的事：只读共用库（BOTLIFE_STATE_DB）的 ongoing_events，取还没结束的事件（例假、生病这类）。
// 库/表/字段有任何问题都当作"没有持续事件"返回空数组，绝不把异常带进聊天。
// 每次查询独立打开、查完立刻关闭：不常驻连接、不写库；只读打开不会拿写锁。
import { Database } from 'bun:sqlite'

export type OngoingEvent = {
  name: string
  /** 效果描述；没有就是空串 */
  effect: string
  /** 开始时刻（Unix epoch 秒） */
  startedAt: number
  /** 第几天：开始当天算第 1 天，按配置时区的自然日 */
  days: number
}

/** 每轮最多注入几条（接口约定 3 条）。截取由引擎在滤掉已见事件之后做。 */
export const MAX_ONGOING_EVENTS = 3

/** 某时刻在指定时区是哪一天（年月日）。时区没配或不合法（Intl 会抛）时退回宿主本地日期，绝不上抛。 */
function ymd(ms: number, timeZone?: string): [number, number, number] {
  const d = new Date(ms)
  if (!timeZone) return [d.getFullYear(), d.getMonth() + 1, d.getDate()]
  try {
    const parts = new Intl.DateTimeFormat('en-US', { timeZone, year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(d)
    const get = (t: string) => Number(parts.find(p => p.type === t)?.value)
    const [y, m, day] = [get('year'), get('month'), get('day')]
    if (![y, m, day].every(Number.isFinite)) throw new Error('bad date')
    return [y, m, day]
  } catch {
    return [d.getFullYear(), d.getMonth() + 1, d.getDate()]
  }
}

/** 从 started_at 那天到今天经过的自然日 +1，按 timeZone（配置时区，默认 Asia/Shanghai）。
 *  用 UTC 日期序号相减，不受夏令时 23/25 小时影响。 */
export function dayNumber(startedAtSec: number, nowMs = Date.now(), timeZone?: string): number {
  const started = new Date(startedAtSec * 1000)
  if (!Number.isFinite(started.getTime())) return 1
  const [ay, am, ad] = ymd(started.getTime(), timeZone)
  const [by, bm, bd] = ymd(nowMs, timeZone)
  const diff = (Date.UTC(by, bm - 1, bd) - Date.UTC(ay, am - 1, ad)) / 86_400_000
  return Math.max(1, Math.round(diff) + 1)
}

/** 事件名/效果要拼进提示词：去掉控制字符、换行和 U+2028/2029，⟦⟧ 换成 〚〛（与 escapeUserText 一致，
 *  防止伪造系统行），压缩空白，各截到 80 字。 */
function cleanText(v: unknown): string {
  if (typeof v !== 'string') return ''
  const s = v
    .replace(/[\u0000-\u001f\u007f-\u009f]/g, ' ')
    .replace(/[\p{Zl}\p{Zp}]/gu, ' ')
    .replace(/⟦/g, '〚')
    .replace(/⟧/g, '〛')
    .replace(/\s+/g, ' ')
    .trim()
  return Array.from(s).slice(0, 80).join('')
}

/** 查这个 bot 还没过期的持续事件，按开始时间从新到旧（不在这里 LIMIT，
 *  由调用方先滤掉"已经注入过"的再截取，否则旧事件会被前面已见的行永久挡住）。任何异常返回 []。 */
export function queryOngoingEvents(dbPath: string | null | undefined, botId: string, nowMs = Date.now(), timeZone?: string): OngoingEvent[] {
  if (!dbPath || !botId) return []
  const nowSec = Math.floor(nowMs / 1000)
  let db: Database | null = null
  try {
    // 注意 bun:sqlite 的打开是惰性的：构造不报错，第一次 query 才真正读库，所以整段一起 catch
    db = new Database(dbPath, { readonly: true })
    const rows = db.query<{ event_name: unknown; effect: unknown; started_at: unknown; expires_at: unknown }, [string, number]>(
      `SELECT event_name, effect, started_at, expires_at FROM ongoing_events
       WHERE bot_id = ? AND CAST(expires_at AS INTEGER) > ?
       ORDER BY CAST(started_at AS INTEGER) DESC`,
    ).all(botId, nowSec)
    const out: OngoingEvent[] = []
    for (const r of rows) {
      const name = cleanText(r.event_name)
      const startedAt = Number(r.started_at)
      const expiresAt = Number(r.expires_at)
      // 字段异常（空名字、不是数字、开始时间不合理）当作这行不存在
      if (!name || !Number.isFinite(startedAt) || startedAt <= 0) continue
      if (!Number.isFinite(expiresAt) || expiresAt <= nowSec) continue
      out.push({
        name,
        effect: cleanText(r.effect),
        startedAt,
        days: dayNumber(startedAt, nowMs, timeZone),
      })
    }
    return out
  } catch {
    return []
  } finally {
    try { db?.close() } catch {}
  }
}

/** 拼成一行：⟦持续中的事⟧ 例假来了（肚子坠痛，第 3 天）；……。没有事件返回空串。 */
export function ongoingLine(events: OngoingEvent[]): string {
  if (events.length === 0) return ''
  const parts = events.map(e => `${e.name}（${e.effect ? `${e.effect}，` : ''}第 ${e.days} 天）`)
  return `⟦持续中的事⟧ ${parts.join('；')}`
}
