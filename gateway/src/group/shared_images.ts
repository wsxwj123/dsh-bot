// 群记录里别人发的图：水位、排除本 bot 自己写的行、按行序消费、每回合上限。
// 读目录白名单与真正的 open/fstat/read 在 media.readSharedImage 里；这里只负责"该看哪几行、水位推到哪"，
// 另外为了判重和上限只对路径做 realpath（不读文件内容）。
import { realpathSync } from 'fs'
import { extname, isAbsolute } from 'path'
import { SHARED_IMAGE_EXTS } from '../media/media'
import { transcriptTs, type TranscriptLine } from './transcript'

/** 水位：看过/给过的时间点，以及同一毫秒里已经消费过的消息编号（同秒多行不重复给、也不漏） */
export type SharedImageWatermark = { after: number; ids: number[] }

/** 一回合最多给模型几张群图 */
export const MAX_GROUP_IMAGES_PER_TURN = 2
/** 只看最近 30 分钟的记录 */
export const GROUP_IMAGE_WINDOW_MS = 30 * 60_000
/** 一次最多扫多少行记录（记录再长也不拖慢回合） */
export const GROUP_IMAGE_SCAN_LIMIT = 1000

export type SharedImageSkipReason =
  | 'self' | 'bad_kind' | 'bad_path_value' | 'not_absolute' | 'bad_extension'
  | 'realpath_failed' | 'not_file' | 'outside_allowlist' | 'too_large' | 'read_failed'

export function parseWatermark(raw: string | null): SharedImageWatermark {
  try {
    const o = JSON.parse(raw ?? '') as { after?: unknown; ids?: unknown }
    const after = Number(o?.after)
    const ids = Array.isArray(o?.ids) ? o.ids.filter((x): x is number => Number.isInteger(x)) : []
    return { after: Number.isFinite(after) && after > 0 ? after : 0, ids }
  } catch { return { after: 0, ids: [] } }
}

export function watermarkJson(w: SharedImageWatermark): string {
  return JSON.stringify({ after: w.after, ids: w.ids })
}

function isSelfLine(line: TranscriptLine, botUsername: string): boolean {
  if (!botUsername) return false
  const me = botUsername.toLowerCase()
  return [line.from_username, line.observed_by].some(v => typeof v === 'string' && v.toLowerCase() === me)
}

type LineInfo = { kind: 'skip'; reason: SharedImageSkipReason } | { kind: 'image'; path: string }

/** 只看记录里的字段形状：不是图片行返回 null；明显不合格给出原因；合格返回待校验的路径 */
export function inspectImageLine(line: TranscriptLine, botUsername: string): LineInfo | null {
  if (line.image_path === undefined) return null
  if (isSelfLine(line, botUsername)) return { kind: 'skip', reason: 'self' }
  if (line.attachment_kind !== undefined && line.attachment_kind !== 'photo') return { kind: 'skip', reason: 'bad_kind' }
  const p = line.image_path
  if (typeof p !== 'string' || p.length === 0 || p.length > 4096 || p.includes('\0')) return { kind: 'skip', reason: 'bad_path_value' }
  if (!isAbsolute(p)) return { kind: 'skip', reason: 'not_absolute' }
  if (!SHARED_IMAGE_EXTS.has(extname(p).toLowerCase())) return { kind: 'skip', reason: 'bad_extension' }
  return { kind: 'image', path: p }
}

/** 这一行在水位之后吗？同一毫秒里已消费过的消息编号跳过。 */
function afterWatermark(line: TranscriptLine, wm: SharedImageWatermark): boolean {
  const ts = transcriptTs(line.ts)
  if (!Number.isFinite(ts)) return false
  if (ts < wm.after) return false
  return !(ts === wm.after && wm.ids.includes(line.message_id))
}

export type SharedImageHit = { line: TranscriptLine; real: string; mimeType: string; data: string }
export type SharedImageRead = { ok: true; real: string; mimeType: string; data: string } | { ok: false; reason: SharedImageSkipReason }

/**
 * 按记录顺序消费图片：自写行、不合格行、读失败行都跳过并继续（这些行算看过，不反复扫）；
 * 同一张图（真实路径相同）一回合只给一次；一回合最多 max 张。
 * 水位只推进到"最后一张实际给出图片的行"（以及被判定为无需再看的行）：
 * 因为超过 max 而没给出的行**不推进**，下一回合继续看。
 * 水位永远不超过 now：未来时间的行不会把水位推到未来、让之后的普通行永久漏掉。
 */
export function consumeSharedImages(o: {
  lines: TranscriptLine[]
  watermark: SharedImageWatermark
  botUsername: string
  max: number
  /** 本轮开始时刻（毫秒）：时间在它之后的行一律不消费（避免水位被推到未来、也避免反复附图） */
  now: number
  read: (path: string) => SharedImageRead
  onSkip?: (line: TranscriptLine, reason: SharedImageSkipReason) => void
}): { picks: SharedImageHit[]; watermark: SharedImageWatermark; advanced: boolean; selfCheckUnavailable: boolean } {
  const picks: SharedImageHit[] = []
  const seenReal = new Set<string>()
  let selfCheckUnavailable = false
  let lastTs = -1
  let lastIds: number[] = []
  const consumed = (line: TranscriptLine): void => {
    // 未来时间只推到 now，事后补写的行不会让水位越过现在
    const ts = Math.min(transcriptTs(line.ts), o.now)
    if (ts > lastTs) { lastTs = ts; lastIds = [line.message_id] }
    else if (ts === lastTs && !lastIds.includes(line.message_id)) lastIds.push(line.message_id)
  }
  for (const line of o.lines) {
    // 时间晚于 now 的行整行丢弃：它们还没"发生"，既不附图（否则每回合重复附），也不推进水位
    const rawTs = transcriptTs(line.ts)
    if (!(rawTs <= o.now)) continue
    if (!afterWatermark(line, o.watermark)) continue
    const info = inspectImageLine(line, o.botUsername)
    if (!info) continue
    if (info.kind === 'skip') { o.onSkip?.(line, info.reason); consumed(line); continue }
    if (!o.botUsername) selfCheckUnavailable = true
    // 到每回合上限就不再读文件（每张最多 8 MiB + base64），留给下一回合；
    // 只有"本回合已经给过的同一张图"才继续消费，判重单独用 realpath、不读内容
    if (picks.length >= o.max) {
      let real: string | null = null
      try { real = realpathSync(info.path) } catch {}
      if (real !== null && seenReal.has(real)) { consumed(line); continue }
      break
    }
    const r = o.read(info.path)
    if (!r.ok) { o.onSkip?.(line, r.reason); consumed(line); continue }
    if (seenReal.has(r.real)) { consumed(line); continue }
    if (picks.length >= o.max) break
    seenReal.add(r.real)
    picks.push({ line, real: r.real, mimeType: r.mimeType, data: r.data })
    consumed(line)
  }
  const advanced = lastTs >= 0
  return { picks, watermark: advanced ? { after: lastTs, ids: lastIds } : o.watermark, advanced, selfCheckUnavailable }
}
