// 表情包（GIF/贴纸）：旧系统与新系统共用 ~/.claude/channels/_shared/stickers.json 这一份库。
// 库分两档（sfw / nsfw）——发哪一档、什么时候发由人设的规则决定，程序只负责"找得到、发得出去"：
//   list：按关键词（英文 label 或中文分类）看有什么，只回标签和 id，不回本地路径（免得模型照着路径拼）
//   send：按 id 或标签把一条发到当前聊天；gif 走 sendAnimation（当 photo 发只显示首帧），webp/tgs 走 sendSticker
// 文件只允许发库条目 path 指向、且落在 ~/resource/stickers/ 下的实体（allowedFile 做 realpath 校验，防越界）。
import { readFileSync } from 'fs'
import { homedir } from 'os'
import { join } from 'path'
import type { Ledger } from '../ledger'
import { safeError, type Logger } from '../log'
import { classifySendError, type TelegramApi } from '../telegram/api'
import { allowedFile, fileKind } from '../telegram/sender'

export type StickerTier = 'sfw' | 'nsfw'
export type StickerEntry = { id: string; tier: StickerTier; label: string; category: string | null; path: string }
export type StickerResult = { text: string; isError?: boolean }

const TIERS: StickerTier[] = ['sfw', 'nsfw']

/** 库文件与文件实体的默认位置（旧系统、新系统、各 bot 共用这一份） */
export const DEFAULT_STICKERS_FILE = join(homedir(), '.claude', 'channels', '_shared', 'stickers.json')
export const DEFAULT_STICKERS_ROOT = join(homedir(), 'resource', 'stickers')

/** 带关键词 list 一次最多列多少行（关键词太泛时防刷屏）；不带关键词时全列，模型好一次看全 */
const LIST_LIMIT = 80

/** 解析库文件：两档都认；条目缺 label / path 的跳过。读不出来返回 []（不抛，由工具层如实回报）。 */
export function loadStickers(file: string): StickerEntry[] {
  let raw: unknown
  try { raw = JSON.parse(readFileSync(file, 'utf8')) } catch { return [] }
  const o = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}
  const out: StickerEntry[] = []
  for (const tier of TIERS) {
    const list = Array.isArray(o[tier]) ? (o[tier] as unknown[]) : []
    let n = 0
    for (const it of list) {
      const e = it && typeof it === 'object' ? (it as Record<string, unknown>) : {}
      const label = typeof e.label === 'string' ? e.label.trim() : ''
      const path = typeof e.path === 'string' ? e.path.trim() : ''
      if (!label || !path) continue
      n++
      out.push({
        id: `${tier}:${n}`, tier, label,
        category: typeof e.category === 'string' && e.category.trim() ? e.category.trim() : null,
        path,
      })
    }
  }
  return out
}

/** 检索用的文本：label + category，全小写 */
function hay(e: StickerEntry): string {
  return `${e.label} ${e.category ?? ''}`.toLowerCase()
}

/** 关键词相关性打分：0 = 不沾边。label 完全等于最高；子串命中次之；拆出的词全部命中再次（"hug 2" 能命中 "Hug 2"）。 */
export function stickerScore(e: StickerEntry, query: string): number {
  const q = query.toLowerCase().trim()
  if (!q) return 0
  const h = hay(e)
  let s = 0
  if (e.label.toLowerCase() === q) s += 1000
  else if (h.includes(q)) s += 100
  const words = q.split(/[^a-z0-9一-龥]+/).filter(Boolean)
  if (words.length > 1 && words.every(w => h.includes(w))) s += 50
  return s
}

/** list 用：按档和关键词过滤（不沾边的一条都不回） */
export function filterStickers(entries: StickerEntry[], o: { query?: string; tier?: string }): StickerEntry[] {
  let list = entries
  if (o.tier === 'sfw' || o.tier === 'nsfw') {
    const t = o.tier
    list = list.filter(e => e.tier === t)
  }
  const q = (o.query ?? '').trim()
  if (!q) return list
  return list
    .map(e => ({ e, s: stickerScore(e, q) }))
    .filter(x => x.s > 0)
    .sort((a, b) => b.s - a.s || a.e.id.localeCompare(b.e.id))
    .map(x => x.e)
}

/**
 * send 用：找一条要发的。
 * id（sfw:3，允许带 #）直接命中；标签精确命中多条取第一条（sameCount 说清有几条）；
 * 剩下的场景按分数取：唯一命中就发，多条沾边不替它挑（near 交候选），一条不沾 near 为空。
 */
export function findSticker(entries: StickerEntry[], o: { id?: string; query?: string; tier?: string }): { entry?: StickerEntry; sameCount?: number; near?: StickerEntry[] } {
  const id = (o.id ?? '').trim().replace(/^#/, '')
  if (id) {
    const hit = entries.find(e => e.id === id)
    return hit ? { entry: hit } : { near: [] }
  }
  const q = (o.query ?? '').trim()
  if (!q) return {}
  let pool = entries
  if (o.tier === 'sfw' || o.tier === 'nsfw') {
    const t = o.tier
    pool = pool.filter(e => e.tier === t)
  }
  const ql = q.toLowerCase()
  const exact = pool.filter(e => e.label.toLowerCase() === ql)
  if (exact.length > 0) return { entry: exact[0]!, sameCount: exact.length }
  const scored = filterStickers(pool, { query: q })
  if (scored.length === 1) return { entry: scored[0]! }
  return { near: scored.slice(0, 8) }
}

function s(v: unknown): string {
  return typeof v === 'string' ? v.trim() : ''
}

export class StickerService {
  constructor(private readonly d: {
    api: TelegramApi
    ledger: Ledger
    log: Logger
    /** 库文件位置；不设用共用库（测试注入） */
    file?: () => string
    /** 文件实体允许的根目录；不设用 ~/resource/stickers */
    root?: () => string
  }) {}

  private read(): { list: StickerEntry[]; error?: string } {
    const list = loadStickers(this.d.file ? this.d.file() : DEFAULT_STICKERS_FILE)
    return list.length > 0 ? { list } : { list, error: '表情包库读不出来（文件不在或格式不对），这次先用不了。' }
  }

  private tierHead(tier: StickerTier, part: StickerEntry[]): string {
    if (tier === 'sfw') return `sfw（${part.length} 条；标签是英文，如 Happy、Hug）`
    const cats = [...new Set(part.filter(e => e.category).map(e => e.category!))]
    return `nsfw（${part.length} 条；标签是中文${cats.length ? `，分类：${cats.join(' | ')}` : ''}）`
  }

  /** list：按关键词看库里有什么。只给标签与 id，不给本地路径。 */
  list(args: Record<string, unknown>): StickerResult {
    const r = this.read()
    if (r.error) return { text: r.error, isError: true }
    const all = r.list
    const query = s(args.query)
    const hits = filterStickers(all, { query, tier: s(args.tier) })
    if (hits.length === 0) {
      return {
        text: query
          ? `「${query}」在库里没有匹配的。sfw 的标签是英文（如 Happy、Hug、Kiss），nsfw 是中文；去掉关键词可以看全部。`
          : '这一档里没有表情包。',
      }
    }
    const cap = query ? LIST_LIMIT : hits.length
    const shown = hits.slice(0, cap)
    const lines: string[] = [query ? `匹配「${query}」的 ${hits.length} 条：` : `表情包库，共 ${all.length} 条。发的时候按人设里的表情包规则选用：`]
    for (const tier of TIERS) {
      const part = shown.filter(e => e.tier === tier)
      if (part.length === 0) continue
      lines.push(this.tierHead(tier, part))
      for (const e of part) lines.push(`#${e.id} ${e.label}`)
    }
    if (hits.length > shown.length) lines.push(`（只列了前 ${shown.length} 条，还有 ${hits.length - shown.length} 条；加个更具体的关键词再找）`)
    this.d.log.info('tool.sticker_list', { query: query !== '', tier: s(args.tier) || null, hits: hits.length })
    return { text: lines.join('\n') }
  }

  /** send：发一条到当前聊天。发送失败/文件不在：如实回报，不假装成功。 */
  async send(chatId: string, turnId: number | null, args: Record<string, unknown>): Promise<StickerResult> {
    const r = this.read()
    if (r.error) return { text: r.error, isError: true }
    const id = s(args.id)
    const query = s(args.label) || s(args.query)
    if (!id && !query) return { text: '要给 id（list 里的 #sfw:12 这种）或者 label（标签，可以只写一部分）。', isError: true }
    const tier = s(args.tier)
    const found = findSticker(r.list, { id, query, tier })
    if (!found.entry) {
      const near = found.near ?? []
      const head = id
        ? `id ${id} 不在库里（tier 只能是 sfw / nsfw）。`
        : `「${query}」在库里没有确切的一条。sfw 的标签是英文（如 Happy、Hug），nsfw 是中文。`
      if (near.length === 0) return { text: `${head}可以不带关键词 list 看全部。`, isError: true }
      const lines = [`${head}有几条沾边的，用 id 再发一次：`]
      for (const e of near) lines.push(`#${e.id} ${e.label}`)
      return { text: lines.join('\n'), isError: true }
    }
    const e = found.entry
    const real = allowedFile(e.path, [this.d.root ? this.d.root() : DEFAULT_STICKERS_ROOT])
    if (!real) {
      this.d.log.warn('tool.sticker_blocked', { tier: e.tier, label: e.label })
      return { text: `「${e.label}」的文件不在库里（或不在允许的目录里），没有发出去。`, isError: true }
    }
    const kind = fileKind(real)
    const outId = this.d.ledger.outboundIntent({ chatId, turnId, part: 0, kind, file: real })
    this.d.log.info('tool.sticker_send', { tier: e.tier, label: e.label, kind })
    try {
      const m = await this.d.api.sendFile(kind, chatId, real)
      this.d.ledger.outboundResult(outId, 'sent', { tgMessageId: m.message_id })
      const same = found.sameCount && found.sameCount > 1 ? `（「${e.label}」同名 ${found.sameCount} 条，这次发的是第 1 条；要别的按 list 里的 id 发）` : ''
      return { text: `表情包发出去了：${e.label}${same}` }
    } catch (err) {
      const info = classifySendError(err)
      this.d.log.warn('tool.sticker_failed', { tier: e.tier, label: e.label, cls: info.cls, reason: info.reason, err: safeError(err) })
      if (info.cls === 'ambiguous') {
        // 可能已经发出去了：宁可少发一次，绝不重发
        this.d.ledger.outboundResult(outId, 'ambiguous', { error: info.reason })
        return { text: '网络异常，这条表情包可能已经发出去了，不要再发一遍。' }
      }
      this.d.ledger.outboundResult(outId, 'failed', { error: info.reason })
      return {
        text: info.cls === 'retryable'
          ? `没发出去（被 Telegram 限流，${info.retryAfterSec ?? '?'} 秒后可以再试）。`
          : `没发出去（Telegram 拒收：${info.reason}）。`,
        isError: true,
      }
    }
  }
}
