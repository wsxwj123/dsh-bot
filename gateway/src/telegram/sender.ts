// 逐段发送并逐段记账。每段的结局都如实返回给调用方（最终告诉模型），已经发出去的段绝不重发。
import { existsSync, realpathSync, rmSync, statSync } from 'fs'
import { tmpdir } from 'os'
import { extname, isAbsolute, join, relative } from 'path'
import type { Access } from '../config'
import type { Ledger, OutboundKind } from '../ledger'
import { safeError, type Logger } from '../log'
import { crashPoint, sleep } from '../util'
import { classifySendError, isReplyNotFound, type TelegramApi } from './api'
import { buildSendPlan } from './send_plan'

export type PartResult = {
  index: number
  kind: 'text' | 'file'
  state: 'sent' | 'failed' | 'ambiguous' | 'skipped' | 'duplicate'
  messageId?: number
  reason?: string
  retryAfterSec?: number
}

export type SendRequest = {
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
}

/** 比较"是不是同一句话"时用：去掉首尾空白，连续空白算一个 */
export function normText(s: string): string {
  return s.trim().replace(/\s+/g, ' ')
}

const MAX_CHUNK = 4096
const PHOTO_EXTS = new Set(['.jpg', '.jpeg', '.png', '.gif', '.webp'])

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
export function allowedFile(path: string, allowDirs: string[]): string | null {
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

  constructor(
    private readonly api: TelegramApi,
    private readonly ledger: Ledger,
    private readonly log: Logger,
    private readonly o: { allowDirs: () => string[]; maxSendWaitMs: number; access: () => Access },
  ) {}

  async send(req: SendRequest): Promise<PartResult[]> {
    const access = this.o.access()
    const items = planParts(req.text, req.files ?? [], access)
    const delay = access.splitOnParagraph ? Math.max(0, access.paragraphDelay ?? 0) : 0
    const replyMode = access.replyToMode ?? 'first'
    const results: PartResult[] = []
    let waitBudget = this.o.maxSendWaitMs
    let stopAfter: { retryAfterSec: number } | null = null

    let textIdx = 0
    for (let i = 0; i < items.length; i++) {
      const item = items[i]!
      const ti = item.kind === 'text' ? textIdx++ : -1
      if (this.halted) {
        this.log.info('send.halted', { chat: req.chatId, part: i + 1 })
        results.push({ index: i + 1, kind: item.kind, state: 'skipped', reason: '网关正在停止' })
        continue
      }
      if (stopAfter) { results.push({ index: i + 1, kind: item.kind, state: 'skipped', reason: '被 Telegram 限流', retryAfterSec: stopAfter.retryAfterSec }); continue }
      if (i > 0 && delay > 0) {
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
      const outKind: OutboundKind = item.kind === 'text' ? (req.kind ?? 'text') : PHOTO_EXTS.has(extname(filePath!).toLowerCase()) ? 'photo' : 'document'
      const okey = req.turnId !== null ? `turn:${req.turnId}:c${req.callSeq}:p${i + 1}` : null
      const outId = this.ledger.outboundIntent({ okey, chatId: req.chatId, turnId: req.turnId, part: i + 1, ofParts: items.length, kind: outKind, text: item.kind === 'text' ? item.text : null, file: filePath, replyTo: replyTo ?? null })

      let attempt = 0
      let useReply = replyTo
      let triedWithoutReply = false
      const shrunk = outKind === 'photo' ? shrinkPhoto(filePath!) : null
      if (shrunk) this.log.info('send.photo_shrunk', { chat: req.chatId, part: i + 1, from: statSync(filePath!).size, to: statSync(shrunk).size })
      for (;;) {
        attempt++
        try {
          const m = item.kind === 'text'
            ? await this.api.sendMessage(req.chatId, item.text, { replyTo: useReply })
            : await this.api.sendFile(outKind === 'photo' ? 'photo' : 'document', req.chatId, shrunk ?? filePath!, { replyTo: useReply })
          crashPoint('after_send_before_record')
          this.ledger.outboundResult(outId, 'sent', { tgMessageId: m.message_id })
          results.push({ index: i + 1, kind: item.kind, state: 'sent', messageId: m.message_id })
          if (item.kind === 'text') this.log.chatLine('bot', req.chatId, item.text)
          if (item.kind === 'text' && req.voice) await this.sendVoice(req, i + 1, req.voiceTexts ? (req.voiceTexts[ti] ?? '') : item.text)
          if (i === 0 && items.length > 1) crashPoint('mid_reply')
          break
        } catch (e) {
          if (useReply && !triedWithoutReply && isReplyNotFound(e)) {
            triedWithoutReply = true
            useReply = undefined
            this.log.info('send.reply_target_missing', { chat: req.chatId, part: i + 1 })
            continue
          }
          const info = classifySendError(e)
          this.log.warn('send.failed', { chat: req.chatId, part: i + 1, cls: info.cls, reason: info.reason, err: safeError(e) })
          if (info.cls === 'retryable') {
            const waitMs = info.retryAfterSec !== undefined ? info.retryAfterSec * 1000 : 1000 * attempt
            if (attempt < 4 && waitMs <= waitBudget) {
              waitBudget -= waitMs
              await sleep(waitMs)
              continue
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
          break
        }
      }
      if (shrunk) rmSync(shrunk, { force: true })
    }
    return results
  }

  /** 文字段发出后补发语音（文字已经在了，语音失败只记日志，不影响这一段算送达） */
  private async sendVoice(req: SendRequest, part: number, text: string): Promise<void> {
    if (!text.trim() || !req.voice) return
    const audio = await req.voice(text)
    let ok = false
    if (audio) {
      try { await this.api.sendFile('voice', req.chatId, audio); ok = true } catch (e) { this.log.warn('send.voice_failed', { chat: req.chatId, part, err: safeError(e) }) }
    }
    this.log.info('send.voice', { chat: req.chatId, part, ok })
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
  if (bad.length === 0 && amb.length === 0 && dup.length === 0) return { text: `已送达 ${sent.length}/${results.length} 段。${DONE_HINT}`, delivered: sent.length, isError: false }
  const lines = [`送达 ${sent.length}/${results.length} 段。`]
  for (const r of dup) lines.push(`第 ${r.index} 段这一轮已经发过（或正在发），没有重复发送。`)
  for (const r of amb) lines.push(`第 ${r.index} 段：${r.reason}，不要重发这一段。`)
  for (const r of bad) {
    lines.push(r.state === 'skipped'
      ? `第 ${r.index} 段未送达：${r.reason}，大约 ${r.retryAfterSec ?? '?'} 秒后才能再发。`
      : `第 ${r.index} 段未送达：${r.reason}。`)
  }
  if (bad.length) lines.push('未送达的段对方没有看到。需要的话可以稍后用 reply 补发，已送达的段不要重复。')
  else lines.push(DONE_HINT)
  return { text: lines.join('\n'), delivered: sent.length + amb.length, isError: bad.length > 0 && sent.length + amb.length === 0 }
}
