// 每轮送给模型的文字格式。系统说明一律用 ⟦…⟧ 包起来；用户正文里的 ⟦ ⟧ 会被换掉，防止伪造系统说明（H-27）。
import type { InboundRow, TranscriptEntry } from '../ledger'

export function escapeUserText(s: string): string {
  return s.replace(/⟦/g, '〚').replace(/⟧/g, '〛')
}

const PERIODS: [number, string][] = [[5, '凌晨'], [8, '早上'], [11, '上午'], [13, '中午'], [18, '下午'], [23, '晚上'], [24, '深夜']]
const periodWord = (h: number) => (PERIODS.find(([end]) => h < end) ?? [24, '深夜'])[1]

type Parts = { month: string; day: string; weekday: string; hour: string; minute: string }

function parts(tsMs: number, timeZone: string): Parts {
  const build = (tz?: string) => {
    const out: Record<string, string> = {}
    const f = new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', weekday: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23', ...(tz ? { timeZone: tz } : {}) })
    for (const p of f.formatToParts(new Date(tsMs))) out[p.type] = p.value
    return out as Parts
  }
  try { return build(timeZone) } catch { return build() } // 时区名写错就退回本机时区，不抛
}

/** 10-05 周日 晚上 21:14 */
export function clock(tsMs: number, timeZone: string): string {
  const p = parts(tsMs, timeZone)
  return `${p.month}-${p.day} ${p.weekday} ${periodWord(Number(p.hour))} ${p.hour}:${p.minute}`
}

/** 短格式：只给时分（同一轮里第二条起用） */
export function shortClock(tsMs: number, timeZone: string): string {
  const p = parts(tsMs, timeZone)
  return `${p.hour}:${p.minute}`
}

export function gapPhrase(gapMs: number): string {
  const min = gapMs / 60_000
  if (min < 90) return `约 ${Math.round(min)} 分钟`
  if (min < 1440) return `约 ${Math.round(min / 60)} 小时`
  return `约 ${Math.round(min / 1440)} 天`
}

export type FormatOpts = { timeZone: string; gapThresholdMin?: number }

/**
 * 一轮里的新消息（私聊）。示例：
 *   ⟦10-05 周日 晚上 21:14 · 距上条约 3 小时 · #1234⟧
 *   今天好累啊
 *   ⟦21:15 · #1235⟧
 *   加班到现在
 */
export function formatMessages(rows: InboundRow[], prevTs: number | null, o: FormatOpts): string {
  const out: string[] = []
  const threshold = (o.gapThresholdMin ?? 30) * 60_000
  rows.forEach((r, i) => {
    const bits: string[] = [i === 0 ? clock(r.ts, o.timeZone) : shortClock(r.ts, o.timeZone)]
    if (i === 0 && prevTs !== null && r.ts - prevTs > threshold) bits.push(`距上条${gapPhrase(r.ts - prevTs)}`)
    if (r.tg_message_id) bits.push(`#${r.tg_message_id}`)
    if (r.kind === 'synthetic') bits.push('系统')
    out.push(`⟦${bits.join(' · ')}⟧`)
    const meta = r.meta ? safeJson(r.meta) : null
    const rt = meta?.reply_to as { message_id?: number; from_me?: boolean; text?: string } | undefined
    if (rt?.message_id) out.push(`⟦回复${rt.from_me ? '你' : '对方'}的 #${rt.message_id}：${escapeUserText(String(rt.text ?? '')).replace(/\n/g, ' ')}⟧`)
    out.push(r.kind === 'synthetic' ? r.text : escapeUserText(r.text))
  })
  return out.join('\n')
}

function safeJson(s: string): Record<string, unknown> | null {
  try { return JSON.parse(s) as Record<string, unknown> } catch { return null }
}

/** 新段开头的前情（崩溃恢复、清空之后等）。和新消息分成两个内容块发送。 */
export function formatSeed(entries: TranscriptEntry[], o: FormatOpts): string | null {
  if (entries.length === 0) return null
  const lines = ['⟦前情：以下是你们最近的聊天记录，由程序从记录里整理，帮你接上话题。不要回复这些旧消息本身，回应后面的新消息。⟧']
  for (const e of entries) {
    const who = e.who === 'bot' ? '你' : '对方'
    lines.push(`⟦${clock(e.ts, o.timeZone)}⟧ ${who}：${escapeUserText(e.text)}`)
  }
  lines.push('⟦前情结束⟧')
  return lines.join('\n')
}

export const NUDGE_NO_ACTION = '⟦系统：你这一轮既没有用 reply 发消息，也没有调用 stay_silent。对方看不到你直接输出的文字。要回复就用 reply；决定不回就调用 stay_silent。⟧'
export const NUDGE_RETRY = '⟦系统：上一条消息没能处理完（程序出错，不是对方的问题）。请接着回应对方上面那条消息。⟧'
