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

/** 新会话第一轮的提醒（很短，只在每个会话的第一轮出现一次） */
export const NEW_SEGMENT_HINT = '⟦系统：新的会话从这里开始。回复对方请用 reply（直接输出的文字对方看不到）；决定不回就调用 stay_silent。⟧'

/** 上一条回复发到一半程序就断了：告诉模型对方只收到了前几段，由它决定要不要补完 */
export function formatInterrupted(sent: number, total: number): string {
  return `⟦系统：你上一条回复发到一半程序中断了：本来要发 ${total} 段，对方只收到了前 ${sent} 段（就是上面最后的"你："那几句）。如果需要，可以自然地把没说完的意思补上，已经发出的部分不要重复。⟧`
}

// ─── 换段：交接摘要（格式在 real-03 里用真模型验证过：五个小标题齐全、事实保留、命中缓存） ───

export const SUMMARY_HEADINGS = ['【我说过的要紧话】', '【我答应过的事】', '【对方的情况】', '【正在聊的话题】', '【我们现在的关系和气氛】']

const SUMMARY_RULES = [
  '请为到目前为止的对话写一份交接摘要，供你下次接着聊时使用。要求：',
  '1. 中文，第一人称（"我"是你自己，"对方"是用户），不超过 600 字。',
  '2. 按下面五个小标题输出，没有就写"无"：',
  `${SUMMARY_HEADINGS[0]}你实际发给对方、之后可能被提起的话，尽量引用原话。`,
  `${SUMMARY_HEADINGS[1]}时间、内容、是否已经做到。`,
  `${SUMMARY_HEADINGS[2]}对方透露的事实、喜好、近况、计划（带时间）。`,
  `${SUMMARY_HEADINGS[3]}还没聊完的话题、对方在等你回应的问题。`,
  `${SUMMARY_HEADINGS[4]}称呼、亲近程度、情绪基调、需要注意的分寸。`,
  '3. 只写对话里真实出现过的内容，不要编造，不要写成剧情梗概。',
].join('\n')

/** 在旧会话里写摘要（这一轮所有工具都锁着） */
export const SUMMARY_PROMPT = [
  '⟦系统·整理记忆⟧ 这不是对方发来的消息，不要回复对方，这一轮也不要调用任何工具，直接输出摘要正文。',
  SUMMARY_RULES,
].join('\n')

/** 旧会话用不了（dsh 崩溃等）时，用账本里的流水单独请求一次写摘要 */
export function formatLedgerSummaryPrompt(previous: string | null, entries: TranscriptEntry[], o: FormatOpts): string {
  const lines = ['⟦系统·整理记忆⟧ 这不是对方发来的消息，不要回复对方，不要调用任何工具，直接输出摘要正文。下面是程序从聊天记录里整理出来的材料。']
  if (previous) lines.push('【更早的摘要】', escapeUserText(previous))
  lines.push('【这一段的聊天记录】')
  for (const e of entries) lines.push(`⟦${clock(e.ts, o.timeZone)}⟧ ${e.who === 'bot' ? '我' : '对方'}：${escapeUserText(e.text)}`)
  lines.push('', SUMMARY_RULES, '4. 如果有更早的摘要，把里面仍然有用的内容合进新摘要。')
  return lines.join('\n')
}

/** 摘要清理与校验。不像样（太短、小标题缺太多）就返回 null，调用方保留上一份，不覆盖。 */
export function cleanSummary(text: string | null | undefined): string | null {
  if (!text) return null
  let t = text.trim()
  if (t.startsWith('```')) t = t.replace(/^```[a-z]*\n?/i, '').replace(/\n?```\s*$/, '').trim()
  if (t.length < 20) return null
  const hits = SUMMARY_HEADINGS.filter(h => t.includes(h)).length
  if (hits < 3) return null
  return t.length > 6000 ? t.slice(0, 6000) : t
}

export type SeedParts = {
  memory: string | null
  summary: string | null
  entries: TranscriptEntry[]
  interrupted: { sent: number; total: number } | null
}

/** 新段开头：长期记忆 + 最近一份摘要 + 最近原话（+ 回复被中断的说明）。各部分都没有就返回 null。 */
export function formatSeedParts(p: SeedParts, o: FormatOpts): string | null {
  const out: string[] = []
  if (p.memory?.trim()) out.push('⟦长期记忆：你自己记下的事，供参考，不要照念⟧', escapeUserText(p.memory.trim()), '⟦长期记忆结束⟧')
  if (p.summary?.trim()) out.push('⟦之前聊天的交接摘要（你自己写的）⟧', escapeUserText(p.summary.trim()), '⟦摘要结束⟧')
  const raw = formatSeed(p.entries, o)
  if (raw) out.push(raw)
  if (p.interrupted) out.push(formatInterrupted(p.interrupted.sent, p.interrupted.total))
  return out.length ? out.join('\n') : null
}

export function formatMemoryHint(items: string[]): string {
  return ['⟦系统：你在别的聊天里新记下了这些事（供参考）：', ...items.map(t => `- ${escapeUserText(t)}`), '⟧'].join('\n')
}
