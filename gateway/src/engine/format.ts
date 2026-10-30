// 每轮送给模型的文字格式。系统说明一律用 ⟦…⟧ 包起来；用户正文里的 ⟦ ⟧ 会被换掉，防止伪造系统说明（H-27）。
import type { InboundRow, TranscriptEntry } from '../ledger'
import { inspectImagePath } from '../group/shared_images'

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

/** 主人用 /compact 时写的"要特别留意的事"，附在摘要指令后面 */
function focusLine(focus: string | undefined): string[] {
  const f = focus?.trim().slice(0, 300)
  return f ? [`4. 主人特别交代，这次整理要留意：${escapeUserText(f)}`] : []
}

export function summaryPrompt(focus?: string): string {
  return [SUMMARY_PROMPT, ...focusLine(focus)].join('\n')
}

/** 旧会话用不了（dsh 崩溃等）时，用账本里的流水单独请求一次写摘要 */
export function formatLedgerSummaryPrompt(previous: string | null, entries: TranscriptEntry[], o: FormatOpts, focus?: string): string {
  const lines = ['⟦系统·整理记忆⟧ 这不是对方发来的消息，不要回复对方，不要调用任何工具，直接输出摘要正文。下面是程序从聊天记录里整理出来的材料。']
  if (previous) lines.push('【更早的摘要】', escapeUserText(previous))
  lines.push('【这一段的聊天记录】')
  for (const e of entries) lines.push(`⟦${clock(e.ts, o.timeZone)}⟧ ${e.who === 'bot' ? '我' : '对方'}：${escapeUserText(e.text)}`)
  lines.push('', SUMMARY_RULES, '4. 如果有更早的摘要，把里面仍然有用的内容合进新摘要。', ...focusLine(focus).map(l => l.replace(/^4\./, '5.')))
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

// ─── 跨聊天近况（cross-chat-recap）：私聊回合附群聊近况，群回合附与主人的私聊近况 ───

/** 私聊 → 群：私聊回合最多带群里最近 30 句、只看 48 小时内 */
export const GROUP_RECAP_LIMIT = 30
export const GROUP_RECAP_WINDOW_MS = 48 * 60 * 60 * 1000
/** 群 → 私聊：群回合最多带与主人私聊的最近 20 句、只看 24 小时内 */
export const DM_RECAP_LIMIT = 20
export const DM_RECAP_WINDOW_MS = 24 * 60 * 60 * 1000

export type RecapRow = { ts: number; from: 'owner' | 'bot'; text: string }

/** 单行正文的默认上限（前缀「主人：」「你：」不计入） */
const RECAP_LINE_CHARS = 200

/**
 * 从按 ts 升序的行里选出要注入的：窗口内（ts > afterMs）、水位之后（ts 严格大于 watermark），
 * 取最后 limit 行。水位在未来（时钟回拨）时视为失效，重置到窗口起点。
 */
export function selectRecapRows<T extends RecapRow>(rows: readonly T[], afterMs: number, limit: number, watermark?: number): T[] {
  if (limit <= 0) return [] // slice(-0) 会返回整个数组，必须单独挡掉
  const floor = watermark !== undefined && Number.isFinite(watermark) && watermark <= Date.now() ? watermark : afterMs
  return rows.filter(r => r.ts > afterMs && r.ts > floor).slice(-limit)
}

function recapLine(who: string, text: string, max: number): string {
  return `${escapeUserText(who)}：${escapeUserText(text).replace(/\s+/g, ' ').trim().slice(0, max)}`
}

/**
 * 私聊近况（群回合注入）。首尾标记与「别原样念出来」的提示是硬要求：群里其他人能看到 bot 说的话，
 * 这段私聊内容只能在心里参考，不能逐字复述到群里。
 */
export function buildRecapText(rows: readonly RecapRow[], opts?: { maxLineChars?: number }): string | null {
  if (rows.length === 0) return null
  const max = Math.max(0, Math.trunc(opts?.maxLineChars ?? RECAP_LINE_CHARS))
  const lines = rows.map(r => recapLine(r.from === 'owner' ? '主人' : '你', r.text, max))
  return ['⟦私聊近况（群里的人看不到这段，别原样念出来；其中的敏感内容，只有和你私聊的那个人问起时才能说）⟧', ...lines, '⟦私聊近况结束⟧'].join('\n')
}

/**
 * 群聊近况（私聊回合注入）沿用原来的格式：一行说明 + 每行「谁：内容」。
 * 行内容（说话人、100 字截断、图片占位）由调用方按群记录拼好，这里只负责外壳。
 */
export const GROUP_RECAP_HEADER = '⟦群里近况：你在的群里，上次之后说了这些。只是让你知道，不用逐条回应；群里的图片对方没问起就别主动提⟧'
export const RECAP_IMAGE_PLACEHOLDER = '[图片]'

export function buildGroupRecapText(lines: readonly string[]): string | null {
  if (lines.length === 0) return null
  return `${GROUP_RECAP_HEADER}\n${lines.join('\n')}`
}

// ─── 群近况附图（group-image-in-dm）：图必须紧跟对应那行，所以近况改成 blocks ───

/** 一回合最多附几张群图 */
export const MAX_RECAP_IMAGES_PER_TURN = 3
/** 近况附图单张字节上限（群图通道仍是 8 MiB） */
export const MAX_RECAP_IMAGE_BYTES = 2 * 1024 * 1024
/** 近况附图边长上限（纯色大 PNG 可以很小但几千像素，只限字节挡不住） */
export const MAX_RECAP_IMAGE_SIDE = 2048

/** 近况行：text 是原始正文（未加「谁：」前缀），from 只区分本人/对方 */
export type GroupRecapRow = RecapRow & {
  /** 这一行在群记录里的去重键（dedupGroupKey；解析不出时退回消息编号） */
  key: string
  /** 群记录里的附图路径（不可信输入，由 inspectImagePath 校验） */
  imagePath?: string
  attachment_kind?: string
  /** 非本人行的显示名（群里的其他人）；缺省「对方」 */
  who?: string
}

export type RecapBlock = { type: 'text'; text: string } | { type: 'image'; data: string; mimeType: string }
export type RecapImageRead = { ok: true; real: string; mimeType: string; data: string } | { ok: false; reason: string }
/**
 * 把近况行排成「文本/图片交错」的 blocks：从最新行往前挑最多 3 张图，同一 realpath 只留一张；
 * 选中行的文本块后面紧跟它的图片块，读不到的行只留纯文本（纯图行用 [图片] 占位）。
 * 未来时间戳的行整行跳过（还没发生，不该进水印也不该附图）。无行返回 null。
 * unreadKeys 是「尝试读过但失败」的行键：这些行不认领，留给 groupImages 按它自己的上限再试一次。
 */
export function buildGroupRecapBlocks(
  rows: readonly GroupRecapRow[],
  read: (path: string) => RecapImageRead,
): { blocks: RecapBlock[]; seenReals: Set<string>; unreadKeys: Set<string> } | null {
  const now = Date.now()
  const visible = rows.filter(r => Number.isFinite(r.ts) && r.ts <= now)
  if (visible.length === 0) return null
  const imageAt = new Map<number, { data: string; mimeType: string }>()
  const seenReals = new Set<string>()
  const unreadKeys = new Set<string>()
  for (let i = visible.length - 1; i >= 0 && imageAt.size < MAX_RECAP_IMAGES_PER_TURN; i--) {
    const r = visible[i]!
    if (r.from === 'bot') continue // 自己发的图不附（与 groupImages 一致）
    const info = inspectImagePath({ image_path: r.imagePath, attachment_kind: r.attachment_kind })
    if (!info || info.kind !== 'image') continue
    const got = read(info.path)
    if (!got.ok) { unreadKeys.add(r.key); continue } // 读不到就不附，文本照常（不写"看不到"）
    if (seenReals.has(got.real)) continue
    seenReals.add(got.real)
    imageAt.set(i, { data: got.data, mimeType: got.mimeType })
  }
  const blocks: RecapBlock[] = []
  visible.forEach((r, i) => {
    const who = r.from === 'bot' ? '你' : (r.who ?? '对方')
    const body = r.text.length > 0 ? r.text : RECAP_IMAGE_PLACEHOLDER
    // 第一行的文本块带上说明头：这样所有文本块按顺序拼起来与 buildGroupRecapText 逐字相同
    const line = escapeUserText(`${who}：${body}`)
    blocks.push({ type: 'text', text: i === 0 ? `${GROUP_RECAP_HEADER}\n${line}` : line })
    const img = imageAt.get(i)
    if (img) blocks.push({ type: 'image', data: img.data, mimeType: img.mimeType })
  })
  return { blocks, seenReals, unreadKeys }
}

// ─── 断线补发提示（outage-delivery 方案 5） ───

/** 补发提示的总长上限（字数，按 JS 字符串长度算） */
export const RESEND_MAX_CHARS = 3000
export const RESEND_HEAD = '⟦系统·补发⟧'

/** 一段没发出的内容：文字段给 text，图片/文件/表情包段给 file（账本记的原图绝对路径） */
export type ResendItem = { kind: string; text: string | null; file: string | null }

const FILE_LABEL: Record<string, string> = { photo: '图片', document: '文件', animation: '动图', sticker: '表情包' }

/**
 * 拼一条补发提示：以 ⟦系统·补发⟧ 开头，列出没发出的每段（文字去重、文件给路径），写明断网起止和现在时刻，
 * 要求按现在的情况补、过时的不说、恢复后已经补过就 stay_silent。总长不超过 RESEND_MAX_CHARS，放不下的段只报个数。
 * parts = 实际列进提示的段数（日志 outage.resend.parts 用）。
 */
export function formatResendPrompt(items: readonly ResendItem[], o: { from: number; to: number; now: number; timeZone: string }): { text: string; parts: number } {
  const seen = new Set<string>()
  const lines: string[] = []
  for (const it of items) {
    const isText = it.kind === 'text'
    const body = isText ? (it.text ?? '').trim() : (it.file ?? '').trim()
    if (!body) continue
    const dedupKey = `${isText ? 't' : 'f'}:${body}`
    if (seen.has(dedupKey)) continue
    seen.add(dedupKey)
    // 文字是自己说过的话，也照样换掉 ⟦⟧，免得一段旧话伪装成系统说明
    lines.push(isText ? `文字：${escapeUserText(body)}` : `${FILE_LABEL[it.kind] ?? '文件'}：${body}`)
  }
  const head = `${RESEND_HEAD} Telegram 线路刚才断了（大约 ${clock(o.from, o.timeZone)} 到 ${clock(o.to, o.timeZone)}），下面是你那时用 reply 发、但根本没发出去的内容，对方一条都没看到。现在是 ${clock(o.now, o.timeZone)}。`
  const tail = '请按现在的情况补给对方：还要紧的用 reply 重新说（不必逐字照抄，可以合并、改口，图片和文件可以用上面的路径重新发），已经过时的就不用再提；如果线路恢复后你已经补过了，就调用 stay_silent。'
  // 预留"还有 N 段放不下"那一句的位置
  const omitNote = (n: number) => `（还有 ${n} 段放不下，没列出来）`
  let budget = RESEND_MAX_CHARS - head.length - tail.length - 2 - omitNote(lines.length).length
  const listed: string[] = []
  for (const line of lines) {
    const numbered = `${listed.length + 1}. ${line}`
    if (numbered.length + 1 <= budget) { listed.push(numbered); budget -= numbered.length + 1; continue }
    // 放不下整段：剩下的空间还够看个开头就截断着列出来；后面的段都不再列，只报个数
    if (budget > 40) listed.push(`${numbered.slice(0, budget - 2)}…`)
    break
  }
  const omitted = lines.length - listed.length
  const body = [head, ...listed, ...(omitted > 0 ? [omitNote(omitted)] : []), tail].join('\n')
  return { text: body.slice(0, RESEND_MAX_CHARS), parts: listed.length }
}
