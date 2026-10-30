// 逐段发送并逐段记账。每段的结局都如实返回给调用方（最终告诉模型），已经发出去的段绝不重发。
import { existsSync, realpathSync, rmSync, statSync } from 'fs'
import { tmpdir } from 'os'
import { extname, isAbsolute, join, relative } from 'path'
import type { Access } from '../config'
import type { Ledger, OutboundKind } from '../ledger'
import { MAX_SHARED_IMAGE_BYTES } from '../media/media'
import { maskChatIdIn, safeError, type Logger } from '../log'
import { crashPoint, sleep } from '../util'
import { classifySendError, isReplyNotFound, TgNetworkError, type TelegramApi } from './api'
import { buildSendPlan } from './send_plan'

export type PartResult = {
  index: number
  kind: 'text' | 'file'
  state: 'sent' | 'failed' | 'ambiguous' | 'skipped' | 'duplicate'
  messageId?: number
  reason?: string
  retryAfterSec?: number
  /** 这一段肯定没发出（线路不通被拦下，或连接前就失败、重试用完），账本记 unsent；只和 state:'failed' 一起出现 */
  linkDown?: boolean
}

/** 没发出去那一段的说明（/v1/send 的 parts[i].reason 也用它） */
const LINK_DOWN_REASON = 'Telegram 线路不通，没有发出去'

export type SendRequest = {
  /** 这一次不按空行拆段（access.splitOnParagraph 不生效）：/compact、/summary 的回复要那句话和摘要原文在同一条里 */
  keepParagraphs?: boolean
  chatId: string
  turnId: number | null
  /** 用于幂等键：同一轮第几次调用发送 */
  callSeq: number
  text: string
  replyTo?: number
  files?: string[]
  kind?: OutboundKind
  /** 这些文字（按 normText 比较）这一轮已经发过了，遇到就跳过不发 */
  skipTexts?: Set<string>
  /** 这些文件（真实路径）这一轮已经发过或正在发，遇到就跳过不发 */
  skipFiles?: Set<string>
  /** 发语音：把一段文字合成语音文件，返回路径；合成失败返回 null */
  voice?: (text: string) => Promise<string | null>
  /** 语音朗读稿，按段对应文字段（第 i 段文字配第 i 段朗读稿）。和旧系统一样：每段先发文字，再发语音；
   *  没给朗读稿就读文字本身；某段朗读稿是空的，这一段只发文字 */
  voiceTexts?: string[]
  /** 日志里显示的聊天标识：缺省就是 chatId（现有行为不变）。跨聊天发送时传脱敏值，
   *  此时不写正文对话行（跨聊天的正文不进当前聊天的 chat.log） */
  logChat?: string
}

/** 比较"是不是同一句话"时用：去掉首尾空白，连续空白算一个 */
export function normText(s: string): string {
  return s.trim().replace(/\s+/g, ' ')
}

const MAX_CHUNK = 4096
const PHOTO_EXTS = new Set(['.jpg', '.jpeg', '.png'])

/**
 * 文件用哪条通道发：.gif 走动画（当 photo 发只显示首帧）、.webp/.tgs 走贴纸、常见图片走照片、其余走文件。
 * reply 的 files 和 sticker 工具共用这一条判定，同一个文件在哪儿发都是同一类。
 */
export function fileKind(path: string): 'photo' | 'animation' | 'sticker' | 'document' {
  const ext = extname(path).toLowerCase()
  if (ext === '.gif') return 'animation'
  if (ext === '.webp' || ext === '.tgs') return 'sticker'
  return PHOTO_EXTS.has(ext) ? 'photo' : 'document'
}

export function chunkText(text: string, limit: number, mode: 'length' | 'newline'): string[] {
  if (text.length <= limit) return [text]
  const out: string[] = []
  let rest = text
  while (rest.length > limit) {
    let cut = limit
    if (mode === 'newline') {
      const para = rest.lastIndexOf('\n\n', limit)
      const line = rest.lastIndexOf('\n', limit)
      const space = rest.lastIndexOf(' ', limit)
      cut = para > limit / 2 ? para : line > limit / 2 ? line : space > 0 ? space : limit
    }
    out.push(rest.slice(0, cut))
    rest = rest.slice(cut).replace(/^\n+/, '')
  }
  if (rest) out.push(rest)
  return out
}

export type SendItem = { kind: 'text'; text: string } | { kind: 'file'; path: string }

/** 按 access.json 的分段设置，把一次回复拆成要发的若干段（文字段 + 文件段）。 */
export function planParts(text: string, files: string[], access: Access): SendItem[] {
  const limit = Math.max(1, Math.min(access.textChunkLimit ?? MAX_CHUNK, MAX_CHUNK))
  const mode = access.chunkMode ?? 'length'
  const { plan } = buildSendPlan(text, files.length)
  const items: SendItem[] = []
  for (const it of plan) {
    if (it.kind === 'text') {
      const paragraphs = access.splitOnParagraph
        ? it.text.split(/\n\s*\n/).map(s => s.trim()).filter(Boolean)
        : (it.text.trim() ? [it.text.trim()] : [])
      for (const p of paragraphs) for (const c of chunkText(p, limit, mode)) items.push({ kind: 'text', text: c })
    } else {
      const f = files[it.index]
      if (f) items.push({ kind: 'file', path: f })
    }
  }
  return items
}

/** 大 PNG 先转成 JPEG 再发（Telegram 收到照片本来也会压成 JPEG）。真机上 2MB 的 PNG 经代理要传 30 秒以上（M4 问题 4）。
 *  只在有 sips 的 Mac 上做（系统自带，不加依赖）；转不成就发原图。返回临时文件路径，调用方发完删掉 */
export function shrinkPhoto(path: string): string | null {
  if (process.platform !== 'darwin' || extname(path).toLowerCase() !== '.png') return null
  try { if (statSync(path).size < 1_000_000) return null } catch { return null }
  const sips = Bun.which('sips')
  if (!sips) return null
  const out = join(tmpdir(), `dshbot-photo-${process.pid}-${Date.now()}.jpg`)
  const r = Bun.spawnSync([sips, '-s', 'format', 'jpeg', '-s', 'formatOptions', '85', path, '--out', out], { stdout: 'ignore', stderr: 'ignore' })
  return r.exitCode === 0 && existsSync(out) ? out : null
}

/** 文件只允许来自白名单目录（防止把本机任意文件发出去）。返回规范化后的真实路径，不允许时返回 null。 */
export function allowedFile(path: string, allowDirs: readonly string[]): string | null {
  if (!isAbsolute(path) || !existsSync(path)) return null
  let real: string
  try { real = realpathSync(path) } catch { return null }
  try { if (!statSync(real).isFile()) return null } catch { return null }
  for (const d of allowDirs) {
    let rd: string
    try { rd = realpathSync(d) } catch { continue }
    const rel = relative(rd, real)
    if (rel && !rel.startsWith('..') && !isAbsolute(rel)) return real
  }
  return null
}

export class Sender {
  /** 网关正在停止：还没开始发的段不再发（真机 M4 问题 7：停了以后用户还陆续收到消息） */
  private halted = false
  halt(): void { this.halted = true }

  /**
   * 线路是通的、却有段记成了 unsent（连接前就失败、重试用完）时回调：线路不会翻转，没有"恢复"来触发补发，
   * 由调用方（engine）安排补发检查。用可写属性而不是依赖 engine，sender 不认识 engine；回调抛错不影响发送
   */
  onUnsentWhileUp: (() => void) | null = null

  /** 线路已确认不通。用 ?. ：单测里的假 api 没有 link，当作一直通 */
  private linkDown(): boolean { return this.api.link?.state === 'down' }

  /** 网络层失败后等一次探测结束：call() 已按规则 b 发起的探测在跑时 probeNow 并入它，不会再发一个 */
  private async awaitProbe(): Promise<void> {
    try { await this.api.link?.probeNow() } catch {}
  }

  constructor(
    private readonly api: TelegramApi,
    private readonly ledger: Ledger,
    private readonly log: Logger,
    private readonly o: {
      allowDirs: () => readonly string[]; maxSendWaitMs: number; access: () => Access
      /** 群聊共享图库（默认 sharedMediaDir）：只有这里的原图才写进群记录，和"允许发 Telegram"是两套名单 */
      shareDirs?: () => readonly string[]
      /** 一次 send 的各段都发完后回调一次（群聊记录用：bot 自己在群里说的话由自己记，一次回复记一行）。
       *  text 是发出的各段按顺序以换行连接（非文字段写占位），messageId 是第一条发出的段，at 是最后一段发出的时刻；
       *  photos 是这一回复第一张确实送达、且在共享图库内、不超过 8 MiB 的原图真实路径；一段都没发出就不回调。
       *  logChat 是日志用的脱敏目标（跨聊天时才有值），回调里的任何日志都不要写完整号 */
      onSent?: (chatId: string, messageId: number, text: string, at: Date, photos?: string[], logChat?: string) => void
    },
  ) {}

  async send(req: SendRequest): Promise<PartResult[]> {
    const access = req.keepParagraphs ? { ...this.o.access(), splitOnParagraph: false } : this.o.access()
    const items = planParts(req.text, req.files ?? [], access)
    // 日志上的聊天标识：同聊天就是真实号（与改动前逐字节一致）；跨聊天是脱敏值，且不写正文对话行
    const logChat = req.logChat ?? req.chatId
    const crossChat = logChat !== req.chatId
    const delay = access.splitOnParagraph ? Math.max(0, access.paragraphDelay ?? 0) : 0
    const replyMode = access.replyToMode ?? 'first'
    const results: PartResult[] = []
    let waitBudget = this.o.maxSendWaitMs
    let stopAfter: { retryAfterSec: number } | null = null

    // 已发出的段，发完后汇总成一次回调
    const sentTexts: string[] = []
    let firstSentId = 0
    let lastSentAt = new Date()
    /** 这一回复第一张可共享的原图（真实路径，且不超过上限） */
    let sharedPhoto: string | null = null

    let textIdx = 0
    try {
      for (let i = 0; i < items.length; i++) {
        const item = items[i]!
        const ti = item.kind === 'text' ? textIdx++ : -1
        if (this.halted) {
          this.log.info('send.halted', { chat: logChat, part: i + 1 })
          results.push({ index: i + 1, kind: item.kind, state: 'skipped', reason: '网关正在停止' })
          continue
        }
        if (stopAfter) { results.push({ index: i + 1, kind: item.kind, state: 'skipped', reason: '被 Telegram 限流', retryAfterSec: stopAfter.retryAfterSec }); continue }
        // 线路不通时后面的段反正发不出，不再停顿、不发"正在输入"（outage-delivery 方案 3）
        if (i > 0 && delay > 0 && !this.linkDown()) {
          void this.api.sendChatAction(req.chatId).catch(() => {})
          await sleep(delay)
        }
        const replyTo = replyMode === 'off' ? undefined : (replyMode === 'all' || i === 0) ? req.replyTo : undefined
        if (item.kind === 'text' && req.skipTexts?.has(normText(item.text))) {
          results.push({ index: i + 1, kind: 'text', state: 'duplicate' })
          continue
        }

        let filePath: string | null = null
        if (item.kind === 'file') {
          filePath = allowedFile(item.path, this.o.allowDirs())
          if (!filePath) { results.push({ index: i + 1, kind: 'file', state: 'failed', reason: '文件不存在或不在允许发送的目录里' }); continue }
          // 同一轮里同一个文件只发一次（真机 M4 问题 3：发图慢、模型以为没发出去又调一次 reply，图发了两遍）
          if (req.skipFiles?.has(filePath)) { results.push({ index: i + 1, kind: 'file', state: 'duplicate' }); continue }
          req.skipFiles?.add(filePath)
        }
        const fk = item.kind === 'file' ? fileKind(filePath!) : null
        const outKind: OutboundKind = fk ?? req.kind ?? 'text'
        const okey = req.turnId !== null ? `turn:${req.turnId}:c${req.callSeq}:p${i + 1}` : null
        const outId = this.ledger.outboundIntent({ okey, chatId: req.chatId, turnId: req.turnId, part: i + 1, ofParts: items.length, kind: outKind, text: item.kind === 'text' ? item.text : null, file: filePath, replyTo: replyTo ?? null })

        let attempt = 0
        let useReply = replyTo
        let triedWithoutReply = false
        const shrunk = fk === 'photo' && !this.linkDown() ? shrinkPhoto(filePath!) : null
        if (shrunk) this.log.info('send.photo_shrunk', { chat: logChat, part: i + 1, from: statSync(filePath!).size, to: statSync(shrunk).size })
        for (;;) {
          attempt++
          try {
            const m = item.kind === 'text'
              ? await this.api.sendMessage(req.chatId, item.text, { replyTo: useReply })
              : await this.api.sendFile(fk!, req.chatId, shrunk ?? filePath!, { replyTo: useReply })
            crashPoint('after_send_before_record')
            this.ledger.outboundResult(outId, 'sent', { tgMessageId: m.message_id })
            results.push({ index: i + 1, kind: item.kind, state: 'sent', messageId: m.message_id })
            // 群记录只记第一张：photo 通道、原图在共享图库内、原图不超过 8 MiB（压缩件/临时件不算）
            if (!sharedPhoto && fk === 'photo') {
              const shared = allowedFile(filePath!, this.o.shareDirs?.() ?? [])
              try { if (shared && statSync(shared).size <= MAX_SHARED_IMAGE_BYTES) sharedPhoto = shared } catch {}
            }
            if (item.kind === 'text' && !crossChat) this.log.chatLine('bot', req.chatId, item.text)
            if (sentTexts.length === 0) firstSentId = m.message_id
            sentTexts.push(item.kind === 'text' ? item.text : fk === 'photo' ? '[图片]' : fk === 'animation' ? '[表情包]' : fk === 'sticker' ? '[贴纸]' : '[文件]')
            lastSentAt = new Date()
            if (item.kind === 'text' && req.voice) await this.sendVoice(req, i + 1, req.voiceTexts ? (req.voiceTexts[ti] ?? '') : item.text)
            if (i === 0 && items.length > 1) crashPoint('mid_reply')
            break
          } catch (e) {
            if (useReply && !triedWithoutReply && isReplyNotFound(e)) {
              triedWithoutReply = true
              useReply = undefined
              this.log.info('send.reply_target_missing', { chat: logChat, part: i + 1 })
              continue
            }
            const info = classifySendError(e)
            this.log.warn('send.failed', { chat: logChat, part: i + 1, cls: info.cls, reason: info.reason, err: crossChat ? maskChatIdIn(safeError(e), req.chatId) : safeError(e) })
            if (info.cls === 'link_down') {
              // 请求根本没发出：肯定没送达，账本记 unsent（D3），补发靠它。后面的段照样逐段记账，会各自被拦下
              this.ledger.outboundResult(outId, 'unsent', { error: 'link_down' })
              results.push({ index: i + 1, kind: item.kind, state: 'failed', reason: LINK_DOWN_REASON, linkDown: true })
              break
            }
            // 连接前就失败（429 不算，那是 TgApiError）：先等探测，确认不通就不必再等，下一次尝试会被直接拦下记 unsent
            const preConnect = info.cls === 'retryable' && e instanceof TgNetworkError
            if (preConnect) {
              await this.awaitProbe()
              // attempt 封顶：防止线路在通/不通之间来回翻时这里无限循环
              if (this.linkDown() && attempt < 4) continue
            }
            if (info.cls === 'retryable') {
              const waitMs = info.retryAfterSec !== undefined ? info.retryAfterSec * 1000 : 1000 * attempt
              if (attempt < 4 && waitMs <= waitBudget) {
                waitBudget -= waitMs
                await sleep(waitMs)
                continue
              }
              if (preConnect) {
                // 连接前就失败、重试用完：同样肯定没发出，不是"被限流"
                this.ledger.outboundResult(outId, 'unsent', { error: `link_down ${info.reason}` })
                results.push({ index: i + 1, kind: item.kind, state: 'failed', reason: LINK_DOWN_REASON, linkDown: true })
                // 线路不通时恢复那一刻会安排补发；线路一直通就没人管它，这里主动要一次
                if (!this.linkDown()) { try { this.onUnsentWhileUp?.() } catch {} }
                break
              }
              this.ledger.outboundResult(outId, 'failed', { error: info.reason })
              const ra = info.retryAfterSec ?? Math.ceil(waitMs / 1000)
              results.push({ index: i + 1, kind: item.kind, state: 'skipped', reason: '被 Telegram 限流', retryAfterSec: ra })
              stopAfter = { retryAfterSec: ra }
              break
            }
            if (info.cls === 'undelivered') {
              this.ledger.outboundResult(outId, 'failed', { error: info.reason })
              results.push({ index: i + 1, kind: item.kind, state: 'failed', reason: `Telegram 拒收（${info.reason}）` })
              break
            }
            this.ledger.outboundResult(outId, 'ambiguous', { error: info.reason })
            results.push({ index: i + 1, kind: item.kind, state: 'ambiguous', reason: `网络异常，可能已送达（${info.reason}）` })
            // 已发出的请求网络层失败：照旧算可能已送达，但等探测结束，后面的段按最新线路状态走（不通就直接拦下）
            if (e instanceof TgNetworkError) await this.awaitProbe()
            break
          }
        }
        if (shrunk) rmSync(shrunk, { force: true })
      }
    } finally {
      // 中途抛异常也把已经发出去的段记上（对方已经看到了）
      if (sentTexts.length) {
        // 记群记录失败不能影响已经发出的结果：回调内部再兜一层
        try { this.o.onSent?.(req.chatId, firstSentId, sentTexts.join('\n'), lastSentAt, sharedPhoto ? [sharedPhoto] : undefined, crossChat ? logChat : undefined) }
        catch { this.log.warn('send.on_sent_failed', { chat: logChat }) }
      }
    }
    return results
  }

  /** 文字段发出后补发语音（文字已经在了，语音失败只记日志，不影响这一段算送达） */
  private async sendVoice(req: SendRequest, part: number, text: string): Promise<void> {
    if (!text.trim() || !req.voice) return
    const logChat = req.logChat ?? req.chatId
    // 线路不通：语音反正发不出，不白白合成
    if (this.linkDown()) { this.log.info('send.voice', { chat: logChat, part, ok: false, link_down: true }); return }
    const audio = await req.voice(text)
    let ok = false
    if (audio) {
      try { await this.api.sendFile('voice', req.chatId, audio); ok = true } catch (e) { this.log.warn('send.voice_failed', { chat: logChat, part, err: logChat !== req.chatId ? maskChatIdIn(safeError(e), req.chatId) : safeError(e) }) }
    }
    this.log.info('send.voice', { chat: logChat, part, ok })
  }
}

/** 给模型看的发送结果说明 */
export const DONE_HINT = '对方已经看到了。说完了就直接结束这一轮，不要把同样的话再发一遍。'

export function describeResults(results: PartResult[]): { text: string; delivered: number; isError: boolean } {
  if (results.length === 0) return { text: '没有可发送的内容（文字为空）。', delivered: 0, isError: true }
  const sent = results.filter(r => r.state === 'sent')
  const amb = results.filter(r => r.state === 'ambiguous')
  const bad = results.filter(r => r.state === 'failed' || r.state === 'skipped')
  const dup = results.filter(r => r.state === 'duplicate')
  const lost = bad.filter(r => r.linkDown)
  if (bad.length === 0 && amb.length === 0 && dup.length === 0) return { text: `已送达 ${sent.length}/${results.length} 段。${DONE_HINT}`, delivered: sent.length, isError: false }
  const lines = [`送达 ${sent.length}/${results.length} 段。`]
  for (const r of dup) lines.push(`第 ${r.index} 段这一轮已经发过（或正在发），没有重复发送。`)
  for (const r of amb) lines.push(`第 ${r.index} 段：${r.reason}，不要重发这一段。`)
  for (const r of bad) {
    lines.push(r.state === 'skipped'
      ? `第 ${r.index} 段未送达：${r.reason}，大约 ${r.retryAfterSec ?? '?'} 秒后才能再发。`
      : `第 ${r.index} 段未送达：${r.reason}。`)
  }
  // 线路不通没发出的段由程序在恢复后提醒补发：模型此刻重发只会再被拦下，说"已经发了"更是错的
  if (lost.length) lines.push('Telegram 线路不通，没有发出去，对方没看到；不要再用 reply 重发，也别说已经发了；恢复后程序会提醒你补。')
  if (bad.length > lost.length) lines.push(lost.length ? '其余未送达的段（不是线路原因）对方也没有看到，已送达的段不要重复。' : '未送达的段对方没有看到。需要的话可以稍后用 reply 补发，已送达的段不要重复。')
  else if (!bad.length) lines.push(DONE_HINT)
  return { text: lines.join('\n'), delivered: sent.length + amb.length, isError: bad.length > 0 && sent.length + amb.length === 0 }
}
