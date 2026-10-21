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
  /** 第几天：开始当天算第 1 天，按本地时区的自然日 */
  days: number
}

/** 一次最多列几条（接口约定 3 条） */
const MAX_EVENTS = 3

/** 从 started_at 那天到今天经过的自然日 +1（本地时区）。
 *  夏令时下一天可能只有 23 或 25 小时，所以按本地零点算完再四舍五入。 */
export function dayNumber(startedAtSec: number, nowMs = Date.now()): number {
  const started = new Date(startedAtSec * 1000)
  const now = new Date(nowMs)
  if (!Number.isFinite(started.getTime())) return 1
  const a = new Date(started.getFullYear(), started.getMonth(), started.getDate()).getTime()
  const b = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  return Math.max(1, Math.round((b - a) / 86_400_000) + 1)
}

/** 查这个 bot 还没过期的持续事件，按开始时间从新到旧，最多 3 条。任何异常返回 []。 */
export function queryOngoingEvents(dbPath: string | null | undefined, botId: string, nowMs = Date.now()): OngoingEvent[] {
  if (!dbPath || !botId) return []
  const nowSec = Math.floor(nowMs / 1000)
  let db: Database | null = null
  try {
    // 注意 bun:sqlite 的打开是惰性的：构造不报错，第一次 query 才真正读库，所以整段一起 catch
    db = new Database(dbPath, { readonly: true })
    const rows = db.query<{ event_name: unknown; effect: unknown; started_at: unknown; expires_at: unknown }, [string, number]>(
      `SELECT event_name, effect, started_at, expires_at FROM ongoing_events
       WHERE bot_id = ? AND CAST(expires_at AS INTEGER) > ?
       ORDER BY CAST(started_at AS INTEGER) DESC LIMIT ${MAX_EVENTS}`,
    ).all(botId, nowSec)
    const out: OngoingEvent[] = []
    for (const r of rows) {
      const name = typeof r.event_name === 'string' ? r.event_name.trim() : ''
      const startedAt = Number(r.started_at)
      const expiresAt = Number(r.expires_at)
      // 字段异常（空名字、不是数字、开始时间不合理）当作这行不存在
      if (!name || !Number.isFinite(startedAt) || startedAt <= 0) continue
      if (!Number.isFinite(expiresAt) || expiresAt <= nowSec) continue
      out.push({
        name,
        effect: typeof r.effect === 'string' ? r.effect.trim() : '',
        startedAt,
        days: dayNumber(startedAt, nowMs),
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

// 直接跑这个文件时做一个最小自检：bun src/life/ongoing.ts
if (import.meta.main) {
  const now = new Date(2026, 9, 21, 12, 0, 0).getTime()
  const sec = (d: Date) => Math.floor(d.getTime() / 1000)
  const at = (daysAgo: number) => { const d = new Date(now); d.setDate(d.getDate() - daysAgo); return sec(d) }
  console.assert(dayNumber(at(0), now) === 1, '当天算第 1 天')
  console.assert(dayNumber(at(2), now) === 3, '两天前算第 3 天')
  console.assert(
    ongoingLine([{ name: '例假来了', effect: '肚子坠痛', startedAt: at(2), days: 3 }]) === '⟦持续中的事⟧ 例假来了（肚子坠痛，第 3 天）',
    '有 effect 的文案',
  )
  console.assert(
    ongoingLine([{ name: '搬家', effect: '', startedAt: at(0), days: 1 }]) === '⟦持续中的事⟧ 搬家（第 1 天）',
    '没 effect 省略效果',
  )
  console.assert(queryOngoingEvents('/definitely/not/here/state.db', 'chenlulu', now).length === 0, '库不存在返回空')
  console.log('ongoing.ts self-check ok')
}
