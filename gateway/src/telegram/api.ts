// 自己写的最小 Telegram Bot API 客户端：只用 fetch，不引入第三方依赖。
// 原因：错误要能精确分成"确定没送达 / 确定被拒 / 可能已送达"三类，且错误里绝不能带出含令牌的请求地址。
import { readFileSync } from 'fs'
import { basename } from 'path'
import { redact, safeError } from '../log'
import { sleep } from '../util'

/** 内联按钮（按钮引导用，方案 3.3.2）。callback_data 的硬性上限是 64 字节，引导自己限 40 */
export type TgInlineButton = { text: string; callback_data: string }
export type TgInlineKeyboard = { inline_keyboard: TgInlineButton[][] }
export type TgUser = { id: number; is_bot?: boolean; first_name?: string; last_name?: string; username?: string
  /** 只有 getMe 返回：BotFather 里是否允许进群、是否关了隐私模式（能看到群里全部消息） */
  can_join_groups?: boolean; can_read_all_group_messages?: boolean }
export type TgChat = { id: number; type: 'private' | 'group' | 'supergroup' | 'channel'; title?: string; username?: string; first_name?: string }
export type TgMessage = {
  message_id: number
  date: number
  chat: TgChat
  from?: TgUser
  text?: string
  caption?: string
  entities?: { type: string; offset: number; length: number }[]
  /** 群升级成超级群：新群里的第一条消息带原群号，原群里的最后一条带新群号 */
  migrate_from_chat_id?: number
  migrate_to_chat_id?: number
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
  reply_markup?: TgInlineKeyboard
}
/**
 * 按钮点击。拿不到原消息时 message 是 InaccessibleMessage：只有 chat、message_id、date，且 date 固定为 0（TG调研第 2 题），
 * 所以用 date === 0 区分。
 */
export type TgCallbackQuery = { id: string; from: TgUser; message?: TgMessage; inline_message_id?: string; chat_instance?: string; data?: string }
/** bot 自己在某个聊天里的身份。restricted 时 is_member 表示此刻还在不在群里 */
export type TgChatMember = { status: string; user: TgUser; is_member?: boolean }
/** my_chat_member：bot 被拉进群、被设成管理员、被移出群（不受隐私模式影响） */
export type TgChatMemberUpdated = { chat: TgChat; from: TgUser; date: number; old_chat_member: TgChatMember; new_chat_member: TgChatMember }
export type TgUpdate = { update_id: number; message?: TgMessage; edited_message?: TgMessage; callback_query?: TgCallbackQuery; my_chat_member?: TgChatMemberUpdated }

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

/**
 * 线路已确认不通（LinkMonitor.state === 'down'），这次发送类请求根本没发出去（outage-delivery 方案 2）。
 * 继承 TgNetworkError：现有按网络错误处理的地方（删消息重试、向导兜底）照旧走网络错误分支。
 */
export class TgLinkDownError extends TgNetworkError {
  override name = 'TgLinkDownError'
  constructor(method: string) { super(method, 'LinkDown', 'telegram link is down, request not sent') }
}

/** link_down：线路不通、请求没发出，肯定没送达（与 retryable 不同：不该原地等着重试） */
export type SendClass = 'retryable' | 'undelivered' | 'ambiguous' | 'link_down'
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
    if (e instanceof TgLinkDownError) return { cls: 'link_down', reason: 'link down' }
    if (e instanceof TgNetworkError) {
      return { cls: PRE_CONNECT_CODES.has(e.code) ? 'retryable' : 'ambiguous', reason: `network ${e.code}` }
    }
  } catch {}
  return { cls: 'ambiguous', reason: 'unknown' }
}

export function isReplyNotFound(e: unknown): boolean {
  return e instanceof TgApiError && e.status === 400 && /reply|replied/i.test(e.description) && /not found/i.test(e.description)
}

/** 编辑时文字和按钮都没变：Telegram 回 400 "message is not modified"，当成功处理（方案 3.11） */
export function isNotModified(e: unknown): boolean {
  return e instanceof TgApiError && e.status === 400 && /message is not modified/i.test(e.description)
}

export type FileUpload = { field: string; path: string; filename?: string }

/**
 * 上传本地文件用的通道。gif 必须走 animation（当 photo 发 Telegram 只显示首帧）；webp/tgs 贴纸走 sticker。
 * 哪个文件走哪条路见 sender 的 fileKind；这里只管通道本身。
 */
export type SendFileKind = 'photo' | 'document' | 'voice' | 'animation' | 'sticker'

// ─── 线路判定（outage-delivery 方案 1，claude 侧照同一口径写） ───

export type LinkState = 'up' | 'down'

/** 不通时照常发的方法：拉取、探测、取文件信息。其余一律算"发送类"，不通时不发出 */
export const LINK_UNGUARDED_METHODS: ReadonlySet<string> = new Set(['getUpdates', 'getMe', 'getFile'])

/** 距最近一次回包多久算"久"：配置 gateway.link_down_after_ms，缺省 max(90 秒, 长轮询超时×3)。拉取报警用同一个阈值 */
export function linkStaleAfterMs(gw: { pollTimeoutS: number; linkDownAfterMs?: number | null }): number {
  return gw.linkDownAfterMs ?? Math.max(90_000, gw.pollTimeoutS * 3_000)
}

/**
 * Telegram 线路状态。收发各处共用这一个状态。
 * - 回包 = 拿到带 ok 字段的 JSON（ok 真假、409/429/5xx 都算）；其余是网络层失败。
 * - 只有一次探测（getMe）以网络层失败结束才进入 down；任何回包立即回到 up。
 * - 探测时机：a. tick 时距最近回包超过 staleAfterMs 且最近一次结束的请求失败；b. 发送类请求网络层失败（failed(true)）；
 *   c. 已经 down 时 tick 每 reprobeMs 再探一次。同一时刻只跑一个探测，多处要求合并成一个。
 */
export class LinkMonitor {
  state: LinkState = 'up'
  lastReachAt: number
  /** 进入 down 的时刻；up 时为 null */
  downAt: number | null = null
  /** 可在构造后改（main 按配置设）；缺省对应长轮询 25 秒时的 90 秒 */
  staleAfterMs: number
  /** 状态翻转时回调：down 时 ms = 距最近回包毫秒数，up 时 ms = 不通持续毫秒数。给日志用，回调抛错不影响判定 */
  onChange: ((state: LinkState, ms: number) => void) | null = null
  private lastEndedFailed = false
  /** 回包计数：探测期间有没有别的请求拿到回包，用它判断（不用时间比较，避免同一毫秒的边界） */
  private reaches = 0
  private lastProbeAt = 0
  private inflight: Promise<LinkState> | null = null
  private readonly now: () => number
  private readonly reprobeMs: number

  constructor(private readonly probeFn: () => Promise<boolean>, o: { staleAfterMs?: number; reprobeMs?: number; now?: () => number } = {}) {
    this.now = o.now ?? Date.now
    this.staleAfterMs = o.staleAfterMs ?? 90_000
    this.reprobeMs = o.reprobeMs ?? 60_000
    this.lastReachAt = this.now()
  }

  /** 某次请求拿到了 Telegram 格式回包 */
  reached(): void {
    const now = this.now()
    this.lastReachAt = now
    this.lastEndedFailed = false
    this.reaches++
    if (this.state === 'down') {
      const downMs = now - (this.downAt ?? now)
      this.state = 'up'
      this.downAt = null
      this.emit('up', downMs)
    }
  }

  /** 某次请求以网络层失败结束；sendClass = 发送类请求（规则 b：立即探测） */
  failed(sendClass = false): void {
    this.lastEndedFailed = true
    if (sendClass && this.state === 'up') void this.probeNow()
  }

  /** 每 10 秒巡检一次（规则 a、c）。发起或并入了探测时返回它的结果，否则 null */
  tick(): Promise<LinkState> | null {
    const now = this.now()
    if (this.state === 'down') return now - this.lastProbeAt >= this.reprobeMs ? this.probeNow() : null
    if (this.lastEndedFailed && now - this.lastReachAt > this.staleAfterMs) return this.probeNow()
    return null
  }

  /** 马上探测一次（不看缓存）；已有探测在跑就并入它。结果是探测结束后的状态 */
  probeNow(): Promise<LinkState> {
    if (this.inflight) return this.inflight
    this.lastProbeAt = this.now()
    const reachesBefore = this.reaches
    const run = (async (): Promise<LinkState> => {
      let ok = false
      try { ok = await this.probeFn() } catch { ok = false }
      if (ok) { this.reached(); return this.state }
      this.lastEndedFailed = true
      // 探测期间别的请求拿到过回包：线路明明能通，不因这次探测失败判不通
      if (this.state === 'up' && this.reaches === reachesBefore) {
        this.state = 'down'
        this.downAt = this.now()
        this.emit('down', this.downAt - this.lastReachAt)
      }
      return this.state
    })()
    this.inflight = run
    void run.finally(() => { if (this.inflight === run) this.inflight = null })
    return run
  }

  private emit(state: LinkState, ms: number): void {
    try { this.onChange?.(state, ms) } catch {}
  }
}

export class TelegramApi {
  /** 线路状态（方案 2）：call() 统一上报回包/网络层失败，不通时统一拦下发送类请求 */
  readonly link: LinkMonitor

  constructor(private readonly token: string, private readonly base = 'https://api.telegram.org', private readonly timeoutMs = 30_000, linkOpts: { staleAfterMs?: number; reprobeMs?: number; now?: () => number } = {}) {
    // 探测绕过 call()：结果由 LinkMonitor 自己处理，不重复上报
    this.link = new LinkMonitor(async () => {
      let reached = false
      await this.request('getMe', {}, { timeoutMs: 10_000 }, () => { reached = true }).catch(() => {})
      return reached
    }, linkOpts)
  }

  private url(method: string): string { return `${this.base}/bot${this.token}/${method}` }

  async call<T>(method: string, params: Record<string, unknown> = {}, o: { timeoutMs?: number; file?: FileUpload; signal?: AbortSignal } = {}): Promise<T> {
    const guarded = !LINK_UNGUARDED_METHODS.has(method)
    // 不通时发送类请求不发出：这样记"没发出"才是确定的，补发不会重复
    if (guarded && this.link.state === 'down') throw new TgLinkDownError(method)
    let reached = false
    try {
      return await this.request<T>(method, params, o, () => { reached = true })
    } finally {
      if (reached) this.link.reached()
      // 调用方自己取消的（如停止时打断长轮询）不算线路失败
      else if (!o.signal?.aborted) this.link.failed(guarded)
    }
  }

  /** 发一次请求。拿到 Telegram 格式回包（带布尔 ok 字段的 JSON 对象）时先调 onReached，再按 ok 返回或抛 TgApiError */
  private async request<T>(method: string, params: Record<string, unknown>, o: { timeoutMs?: number; file?: FileUpload; signal?: AbortSignal }, onReached: () => void): Promise<T> {
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
      // 合法 JSON 但不是对象（如中间的代理回了 null）：同样当"回包读不出"，不能让下面的 data.ok 抛 TypeError 打乱错误分类
      if (!data || typeof data !== 'object') throw new Error('not an object')
    } catch {
      // 回包读不出来：Telegram 可能已经处理了，按"可能已送达"
      throw new TgNetworkError(method, res.ok ? 'BadResponseBody' : `HTTP${res.status}`, '')
    }
    // 只有带布尔 ok 字段的才是 Telegram 的回包；代理/中转站自己回的 JSON 页面不算（抛出的错误类型照旧）
    if (typeof data.ok === 'boolean') onReached()
    if (data.ok) return data.result as T
    throw new TgApiError(method, data.error_code ?? res.status, data.description ?? '', data.parameters?.retry_after)
  }

  getMe() { return this.call<TgUser>('getMe') }

  getUpdates(offset: number, timeoutS: number, signal?: AbortSignal) {
    // 两类都要显式写：不写时 Telegram 沿用上一次的声明，旧网关声明过只要 message，按钮点击就永远收不到（TG调研第 2 题）
    return this.call<TgUpdate[]>('getUpdates', { offset, timeout: timeoutS, allowed_updates: ['message', 'callback_query', 'my_chat_member'] }, { timeoutMs: (timeoutS + 15) * 1000, signal })
  }

  sendMessage(chatId: string, text: string, o: { replyTo?: number; replyMarkup?: TgInlineKeyboard } = {}) {
    return this.call<TgMessage>('sendMessage', {
      chat_id: chatId,
      text,
      ...(o.replyTo ? { reply_parameters: { message_id: o.replyTo, allow_sending_without_reply: true } } : {}),
      ...(o.replyMarkup ? { reply_markup: o.replyMarkup } : {}),
    })
  }

  /** 改 bot 自己发的一条消息的文字。不给 replyMarkup = 去掉按钮（Telegram 的行为，TG调研第 4 题） */
  editMessageText(chatId: string, messageId: number, text: string, o: { replyMarkup?: TgInlineKeyboard } = {}) {
    return this.call<TgMessage | true>('editMessageText', { chat_id: chatId, message_id: messageId, text, ...(o.replyMarkup ? { reply_markup: o.replyMarkup } : {}) })
  }

  /** 只改按钮。不给 replyMarkup = 去掉按钮 */
  editMessageReplyMarkup(chatId: string, messageId: number, replyMarkup?: TgInlineKeyboard) {
    return this.call<TgMessage | true>('editMessageReplyMarkup', { chat_id: chatId, message_id: messageId, ...(replyMarkup ? { reply_markup: replyMarkup } : {}) })
  }

  /** 删一条消息。要带重试和"删没删掉"的结论时用 deleteMessageWithRetry */
  deleteMessage(chatId: string, messageId: number) {
    return this.call<boolean>('deleteMessage', { chat_id: chatId, message_id: messageId }, { timeoutMs: 10_000 })
  }

  /** 回应一次按钮点击；不给 text = 不显示文字。每个回调只能回一次，第二次 Telegram 报 400 */
  answerCallbackQuery(callbackQueryId: string, text?: string) {
    return this.call<boolean>('answerCallbackQuery', { callback_query_id: callbackQueryId, ...(text ? { text } : {}) }, { timeoutMs: 10_000 })
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

  sendFile(kind: SendFileKind, chatId: string, path: string, o: { replyTo?: number; caption?: string } = {}) {
    const method = kind === 'photo' ? 'sendPhoto' : kind === 'voice' ? 'sendVoice' : kind === 'animation' ? 'sendAnimation' : kind === 'sticker' ? 'sendSticker' : 'sendDocument'
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
 * 删一条消息（含密钥的消息靠它清掉，方案 3.11）：网络错误或 429 最多再试 2 次（429 按 retry_after，单次最多等 5 秒）。
 * 永不抛；ok:false 时 err 已脱敏，可以直接写日志。
 */
export async function deleteMessageWithRetry(api: TelegramApi, chatId: string, messageId: number, wait: (ms: number) => Promise<void> = sleep): Promise<{ ok: true } | { ok: false; err: string }> {
  let maybeDone = false
  for (let attempt = 0; ; attempt++) {
    try {
      await api.deleteMessage(chatId, messageId)
      return { ok: true }
    } catch (e) {
      // 上一次是"可能已送达"的网络错误，这次报找不到：多半上一次其实删成了，反正消息已经不在
      if (maybeDone && e instanceof TgApiError && e.status === 400 && /message to delete not found/i.test(e.description)) return { ok: true }
      const is429 = e instanceof TgApiError && e.status === 429
      if (attempt >= 2 || !(is429 || e instanceof TgNetworkError)) return { ok: false, err: safeError(e) }
      if (classifySendError(e).cls === 'ambiguous') maybeDone = true
      await wait(is429 ? Math.min(5, (e as TgApiError).retryAfter ?? 1) * 1000 : 1_000)
    }
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
