// 长轮询收消息。每条更新：先过闸门，再和 offset 一起写进账本（同一个事务），最后通知调度器。
// 按钮引导（方案 1.1）另加两个拦截点：闸门之前（`/provider add`，任何聊天）、入账之前（引导）；被接走的消息
// 不入账、不写 chat.log、不进群聊记录、不交给模型。按钮回调推进 offset 后交给 onCallback。
import { loadAccess } from '../config'
import type { Ledger } from '../ledger'
import { safeError, type Logger } from '../log'
import { Backoff, crashPoint } from '../util'
import { TgApiError, type TelegramApi, type TgCallbackQuery, type TgMessage, type TgUpdate } from './api'
import { gate, redactMessage, toInbound } from './inbound'

/**
 * 拦截钩子：返回 true = 这条由它接走，不入账、不写 chat.log、不进群聊记录、不交给模型。
 * 接走者要在同步返回 true 之前自己推进 offset（和它自己的状态写在同一个账本事务里，方案 Q2）。
 */
export type InterceptHook = (msg: TgMessage, updateId: number) => boolean

export type PollerHealth = { lastOkAt: number; lastError: string | null; conflict: boolean }

export class Poller {
  readonly health: PollerHealth = { lastOkAt: 0, lastError: null, conflict: false }
  private stopped = false
  private ctrl = new AbortController()
  private loopDone: Promise<void> | null = null

  constructor(
    private readonly api: TelegramApi,
    private readonly ledger: Ledger,
    private readonly log: Logger,
    private readonly o: {
      channelDir: string; botId: number | null; pollTimeoutS: number; onInbound: (chatId: string) => void; onFatal: (why: string) => void
      /** 真人发来的消息：群消息（记进群聊记录）和私聊消息（记"刚聊过"，导演据此不点正在私聊的 bot） */
      onHumanMessage?: (msg: TgMessage, observed: boolean) => void
      /** 闸门之前（任何聊天、任何人）：`/provider add` 含密钥，排在闸门和群聊"只记录"之前（方案 3.6） */
      interceptBeforeGate?: InterceptHook
      /** 闸门放行之后、入账之前：引导进行中、迟到密钥防护、/provider /model /cancel（方案 3.3.1） */
      intercept?: InterceptHook
      /** 按钮回调：offset 推进之后交给它 */
      onCallback?: (cq: TgCallbackQuery, updateId: number) => void
    },
  ) {}

  start(): void {
    this.health.lastOkAt = Date.now()
    this.loopDone = this.loop()
  }

  async stop(): Promise<void> {
    this.stopped = true
    this.ctrl.abort()
    await this.loopDone?.catch(() => {})
  }

  /** 停机时能被打断的等待 */
  private pause(ms: number): Promise<void> {
    return new Promise(r => {
      const t = setTimeout(r, ms)
      this.ctrl.signal.addEventListener('abort', () => { clearTimeout(t); r() }, { once: true })
    })
  }

  private async loop(): Promise<void> {
    const backoff = new Backoff(1_000, 30_000)
    while (!this.stopped) {
      let updates: TgUpdate[]
      try {
        updates = await this.api.getUpdates(this.ledger.tgOffset(), this.o.pollTimeoutS, this.ctrl.signal)
        this.health.lastOkAt = Date.now()
        this.health.lastError = null
        this.health.conflict = false
        backoff.reset()
      } catch (e) {
        if (this.stopped) return
        this.health.lastError = safeError(e)
        if (e instanceof TgApiError && e.status === 401) {
          this.log.error('telegram.unauthorized', { err: safeError(e) })
          this.o.onFatal('Telegram 令牌无效（401）')
          return
        }
        if (e instanceof TgApiError && e.status === 409) {
          // 同一个令牌有别的程序在收消息（比如旧系统的 bot 还没停）
          this.health.conflict = true
          this.log.error('telegram.conflict', { hint: 'another program is polling with the same bot token' })
          await this.pause(30_000)
          continue
        }
        const wait = e instanceof TgApiError && e.status === 429 ? (e.retryAfter ?? 5) * 1000 : backoff.next()
        this.log.warn('telegram.poll_failed', { err: safeError(e), wait_ms: wait })
        await this.pause(wait)
        continue
      }
      for (const u of updates) this.handle(u)
    }
  }

  private handle(u: TgUpdate): void {
    if (u.callback_query) {
      this.ledger.recordUpdate(u.update_id, null)
      try { this.o.onCallback?.(u.callback_query, u.update_id) } catch (e) { this.log.error('inbound.dropped', { reason: 'callback handler failed', err: safeError(e) }) }
      return
    }
    const msg = u.message
    if (!msg) { this.ledger.recordUpdate(u.update_id, null); return }
    if (this.intercepted(this.o.interceptBeforeGate, msg, u.update_id)) return
    const access = loadAccess(this.o.channelDir)
    const g = gate(msg, access)
    if (g.deliver === false) {
      this.ledger.recordUpdate(u.update_id, null)
      this.log.info('inbound.dropped', { chat: msg.chat.id, reason: g.reason })
      return
    }
    if (g.deliver === 'observe') {
      this.ledger.recordUpdate(u.update_id, null)
      this.log.info('inbound.observed', { chat: msg.chat.id })
      this.o.onHumanMessage?.(redactMessage(msg), true)
      return
    }
    if (this.intercepted(this.o.intercept, msg, u.update_id)) return
    const clean = redactMessage(msg)
    const row = toInbound(clean, this.o.botId)
    const r = this.ledger.recordUpdate(u.update_id, row)
    if (!r.inserted) { this.log.info('inbound.duplicate', { ukey: row.ukey }); return }
    crashPoint('after_inbound')
    this.log.info('inbound.recorded', { id: r.id, chat: row.chatId, kind: row.kind, chars: row.text.length })
    this.log.chatLine(row.senderName || '对方', row.chatId, row.text)
    // 已读回执：私聊没有原生的"已读"，沿用旧系统的 👀
    if (row.kind === 'user') void this.api.setMessageReaction(row.chatId, msg.message_id, '👀').catch(() => {})
    if (row.kind === 'user') this.o.onHumanMessage?.(clean, false)
    this.o.onInbound(row.chatId)
  }

  /**
   * 交给拦截钩子。钩子抛错也当"接走"：这条可能含密钥（/provider add、引导的密钥步），宁可丢这一条，
   * 也不能让它照常入账、交给模型。offset 本该由钩子推进，它没推进的这里补上，免得同一条被反复拉到。
   */
  private intercepted(hook: InterceptHook | undefined, msg: TgMessage, updateId: number): boolean {
    if (!hook) return false
    let taken: boolean
    try {
      taken = hook(msg, updateId)
    } catch (e) {
      taken = true
      this.log.error('inbound.dropped', { chat: msg.chat.id, reason: 'intercept failed', err: safeError(e) })
    }
    if (taken && this.ledger.tgOffset() <= updateId) this.ledger.recordUpdate(updateId, null)
    return taken
  }
}
