// 长轮询收消息。每条更新：先过闸门，再和 offset 一起写进账本（同一个事务），最后通知调度器。
import { loadAccess } from '../config'
import type { Ledger } from '../ledger'
import { safeError, type Logger } from '../log'
import { Backoff, crashPoint } from '../util'
import { TgApiError, type TelegramApi, type TgUpdate } from './api'
import { gate, toInbound } from './inbound'

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
    private readonly o: { channelDir: string; botId: number | null; pollTimeoutS: number; onInbound: (chatId: string) => void; onFatal: (why: string) => void },
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
    const msg = u.message
    if (!msg) { this.ledger.recordUpdate(u.update_id, null); return }
    const access = loadAccess(this.o.channelDir)
    const g = gate(msg, access)
    if (!g.deliver) {
      this.ledger.recordUpdate(u.update_id, null)
      this.log.info('inbound.dropped', { chat: msg.chat.id, reason: g.reason })
      return
    }
    const row = toInbound(msg, this.o.botId)
    const r = this.ledger.recordUpdate(u.update_id, row)
    if (!r.inserted) { this.log.info('inbound.duplicate', { ukey: row.ukey }); return }
    crashPoint('after_inbound')
    this.log.info('inbound.recorded', { id: r.id, chat: row.chatId, kind: row.kind, chars: row.text.length })
    this.log.chatLine(row.senderName || '对方', row.chatId, row.text)
    // 已读回执：私聊没有原生的"已读"，沿用旧系统的 👀
    if (row.kind === 'user') void this.api.setMessageReaction(row.chatId, msg.message_id, '👀').catch(() => {})
    this.o.onInbound(row.chatId)
  }
}
