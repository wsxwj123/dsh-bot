// 自己写的最小 Telegram Bot API 客户端：只用 fetch，不引入第三方依赖。
// 原因：错误要能精确分成"确定没送达 / 确定被拒 / 可能已送达"三类，且错误里绝不能带出含令牌的请求地址。
import { readFileSync } from 'fs'
import { basename } from 'path'
import { redact } from '../log'

export type TgUser = { id: number; is_bot?: boolean; first_name?: string; last_name?: string; username?: string }
export type TgChat = { id: number; type: 'private' | 'group' | 'supergroup' | 'channel'; title?: string; username?: string; first_name?: string }
export type TgMessage = {
  message_id: number
  date: number
  chat: TgChat
  from?: TgUser
  text?: string
  caption?: string
  entities?: { type: string; offset: number; length: number }[]
  reply_to_message?: TgMessage
  photo?: { file_id: string; file_size?: number; width?: number; height?: number }[]
  voice?: { file_id: string; duration?: number; mime_type?: string; file_size?: number }
  audio?: unknown
  video?: unknown
  video_note?: unknown
  document?: { file_name?: string }
  sticker?: { emoji?: string }
  animation?: unknown
  location?: unknown
  contact?: unknown
}
export type TgUpdate = { update_id: number; message?: TgMessage; edited_message?: TgMessage }

/** Telegram 明确回了 ok:false */
export class TgApiError extends Error {
  override name = 'TgApiError'
  constructor(readonly method: string, readonly status: number, readonly description: string, readonly retryAfter?: number) {
    super(`${method}: ${status} ${redact(description).slice(0, 200)}`)
  }
}

/** 网络层出错（连不上、超时、连接被掐断……）。code 是底层错误码 */
export class TgNetworkError extends Error {
  override name = 'TgNetworkError'
  constructor(readonly method: string, readonly code: string, readonly detail: string) {
    super(`${method}: network ${code} ${redact(detail).slice(0, 160)}`)
  }
}

export type SendClass = 'retryable' | 'undelivered' | 'ambiguous'
export type SendErrorInfo = { cls: SendClass; reason: string; retryAfterSec?: number }

// 只收录确证属于"请求体发出之前"的失败码（bun 驼峰写法与 Node 大写写法各一份）。不在集合里的一律当"可能已送达"。
const PRE_CONNECT_CODES = new Set(['ConnectionRefused', 'ECONNREFUSED', 'ENOTFOUND', 'EAI_AGAIN', 'ENETUNREACH', 'EHOSTUNREACH', 'FailedToOpenSocket'])

/** 发送失败三分类：retryable 确定没送达可重试；undelivered 确定被拒；ambiguous 可能已送达，绝不重发。永不抛。 */
export function classifySendError(e: unknown): SendErrorInfo {
  try {
    if (e instanceof TgApiError) {
      const reason = `telegram ${e.status}`
      if (e.status === 429) return { cls: 'retryable', reason, retryAfterSec: e.retryAfter ?? 5 }
      if (e.status >= 400 && e.status < 500) return { cls: 'undelivered', reason }
      return { cls: 'ambiguous', reason }
    }
    if (e instanceof TgNetworkError) {
      return { cls: PRE_CONNECT_CODES.has(e.code) ? 'retryable' : 'ambiguous', reason: `network ${e.code}` }
    }
  } catch {}
  return { cls: 'ambiguous', reason: 'unknown' }
}

export function isReplyNotFound(e: unknown): boolean {
  return e instanceof TgApiError && e.status === 400 && /reply|replied/i.test(e.description) && /not found/i.test(e.description)
}

export type FileUpload = { field: string; path: string; filename?: string }

export class TelegramApi {
  constructor(private readonly token: string, private readonly base = 'https://api.telegram.org', private readonly timeoutMs = 30_000) {}

  private url(method: string): string { return `${this.base}/bot${this.token}/${method}` }

  async call<T>(method: string, params: Record<string, unknown> = {}, o: { timeoutMs?: number; file?: FileUpload; signal?: AbortSignal } = {}): Promise<T> {
    const timeout = o.timeoutMs ?? this.timeoutMs
    const signals = [AbortSignal.timeout(timeout), ...(o.signal ? [o.signal] : [])]
    let res: Response
    try {
      if (o.file) {
        const { body, contentType } = buildMultipart(params, o.file)
        res = await fetch(this.url(method), { method: 'POST', headers: { 'content-type': contentType }, body, signal: AbortSignal.any(signals) })
      } else {
        res = await fetch(this.url(method), { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(params), signal: AbortSignal.any(signals) })
      }
    } catch (e) {
      const err = e as { code?: unknown; name?: unknown; message?: unknown; cause?: { code?: unknown } }
      const code = String(err?.code ?? err?.cause?.code ?? err?.name ?? 'unknown')
      throw new TgNetworkError(method, code, String(err?.message ?? ''))
    }
    let data: { ok?: boolean; result?: T; error_code?: number; description?: string; parameters?: { retry_after?: number } }
    try {
      data = await res.json() as typeof data
    } catch {
      // 回包读不出来：Telegram 可能已经处理了，按"可能已送达"
      throw new TgNetworkError(method, res.ok ? 'BadResponseBody' : `HTTP${res.status}`, '')
    }
    if (data.ok) return data.result as T
    throw new TgApiError(method, data.error_code ?? res.status, data.description ?? '', data.parameters?.retry_after)
  }

  getMe() { return this.call<TgUser>('getMe') }

  getUpdates(offset: number, timeoutS: number, signal?: AbortSignal) {
    return this.call<TgUpdate[]>('getUpdates', { offset, timeout: timeoutS, allowed_updates: ['message'] }, { timeoutMs: (timeoutS + 15) * 1000, signal })
  }

  sendMessage(chatId: string, text: string, o: { replyTo?: number } = {}) {
    return this.call<TgMessage>('sendMessage', {
      chat_id: chatId,
      text,
      ...(o.replyTo ? { reply_parameters: { message_id: o.replyTo, allow_sending_without_reply: true } } : {}),
    })
  }

  getFile(fileId: string) {
    return this.call<{ file_path?: string; file_size?: number }>('getFile', { file_id: fileId }, { timeoutMs: 20_000 })
  }

  /** 下载 getFile 拿到的文件。地址里带令牌，只在这里用，不写日志。 */
  async download(filePath: string, maxBytes = 20 * 1024 * 1024): Promise<Uint8Array> {
    const res = await fetch(`${this.base}/file/bot${this.token}/${filePath}`, { signal: AbortSignal.timeout(60_000) })
    if (!res.ok) throw new TgApiError('download', res.status, `HTTP ${res.status}`)
    const buf = new Uint8Array(await res.arrayBuffer())
    if (buf.byteLength > maxBytes) throw new TgApiError('download', 413, 'file too large')
    return buf
  }

  sendFile(kind: 'photo' | 'document' | 'voice', chatId: string, path: string, o: { replyTo?: number; caption?: string } = {}) {
    const method = kind === 'photo' ? 'sendPhoto' : kind === 'voice' ? 'sendVoice' : 'sendDocument'
    return this.call<TgMessage>(method, {
      chat_id: chatId,
      ...(o.caption ? { caption: o.caption } : {}),
      ...(o.replyTo ? { reply_parameters: JSON.stringify({ message_id: o.replyTo, allow_sending_without_reply: true }) } : {}),
    }, { file: { field: kind, path }, timeoutMs: 120_000 })
  }

  sendChatAction(chatId: string, action = 'typing') {
    return this.call<boolean>('sendChatAction', { chat_id: chatId, action }, { timeoutMs: 10_000 })
  }

  setMessageReaction(chatId: string, messageId: number, emoji: string | null) {
    return this.call<boolean>('setMessageReaction', {
      chat_id: chatId, message_id: messageId, reaction: emoji ? [{ type: 'emoji', emoji }] : [],
    }, { timeoutMs: 10_000 })
  }
}

/**
 * 把整个 multipart 拼成一个缓冲区再发（带 Content-Length）。
 * 旧系统实测：bun 的 fetch 经 HTTPS_PROXY 发流式 body 会失败，缓冲后就稳定了。
 */
export function buildMultipart(params: Record<string, unknown>, file: FileUpload): { body: Uint8Array; contentType: string } {
  const boundary = `----dshbot${Date.now().toString(16)}${Math.random().toString(16).slice(2)}`
  const enc = new TextEncoder()
  const parts: Uint8Array[] = []
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null) continue
    parts.push(enc.encode(`--${boundary}\r\nContent-Disposition: form-data; name="${k}"\r\n\r\n${typeof v === 'string' ? v : JSON.stringify(v)}\r\n`))
  }
  const data = readFileSync(file.path)
  const name = (file.filename ?? basename(file.path)).replace(/["\r\n]/g, '_')
  parts.push(enc.encode(`--${boundary}\r\nContent-Disposition: form-data; name="${file.field}"; filename="${name}"\r\nContent-Type: application/octet-stream\r\n\r\n`))
  parts.push(new Uint8Array(data.buffer, data.byteOffset, data.byteLength))
  parts.push(enc.encode(`\r\n--${boundary}--\r\n`))
  const total = parts.reduce((n, p) => n + p.byteLength, 0)
  const body = new Uint8Array(total)
  let off = 0
  for (const p of parts) { body.set(p, off); off += p.byteLength }
  return { body, contentType: `multipart/form-data; boundary=${boundary}` }
}
