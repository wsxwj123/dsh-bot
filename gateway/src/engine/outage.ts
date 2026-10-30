// 断线补发（outage-delivery 方案 5）：线路恢复后（及网关启动后），把因断线没发出去的段交给模型按现在的情况补说。
//   候选：属于某个回合、账本 state=unsent、在水位之后的内容段（文字、图片、文件、表情包）。
//   过滤：群聊不补（D1）；串里有约定提醒且 0 段送达 → 约定自己会重新提醒；超过 12 小时、同一内容已补满 3 轮 → 丢弃并通知主人。
//   水位：meta outage_resend_upto:<chat>，补了的和丢弃的都前移——同一批只处理一次，重启或再次恢复不会重复记。
//   投递：一条合成消息（meta {source:'outage_resend', round}，去重键 int:outage:<chat>:<最大行号>），与水位更新同一事务。
// 另：线路不通时攒着的主人通知（HeldNotices），恢复后再发。
import type { Ledger, UnsentRow } from '../ledger'
import type { Logger } from '../log'
import { isGroupChat } from '../telegram/inbound'
import { formatResendPrompt } from './format'

export const RESEND_MAX_AGE_MS = 12 * 60 * 60 * 1000
export const RESEND_MAX_ROUNDS = 3
export const RESEND_SOURCE = 'outage_resend'
export const resendWatermarkKey = (chatId: string) => `outage_resend_upto:${chatId}`

export type DropReason = 'group' | 'commitment' | 'too_old' | 'rounds'

export type OutageDeps = {
  ledger: Ledger
  log: Logger
  timeZone: () => string
  /** 合成消息已经写进账本（事务提交后）：给这个聊天排一轮 */
  schedule: (chatId: string) => void
  notifyOwner: (key: string, text: string) => void
}

export class OutageResend {
  constructor(private readonly d: OutageDeps) {}

  /**
   * 跑一次补发检查。outage = 最近一次断网的起止（不知道时 null，按没发出那些段的时间估）。
   * 返回本次投递了补发消息的聊天。
   */
  run(now: number, outage: { from: number; to: number | null } | null): string[] {
    const L = this.d.ledger
    const resent: string[] = []
    // 不按时间筛聊天：超过 12 小时的也要拿出来记 too_old、推水位
    for (const chatId of L.chatsWithUnsent(0)) {
      const key = resendWatermarkKey(chatId)
      const upto = Number(L.getMeta(key) ?? 0) || 0
      const rows = L.unsentRows(chatId, upto, 0)
      if (rows.length === 0) continue
      const maxId = rows[rows.length - 1]!.id
      const dropped = new Map<DropReason, UnsentRow[]>()
      const keep: UnsentRow[] = []
      const deliveredByRoot = new Map<number, number>()
      const delivered = (rootId: number) => {
        let n = deliveredByRoot.get(rootId)
        if (n === undefined) { n = L.chainOutcome(rootId).delivered; deliveredByRoot.set(rootId, n) }
        return n
      }
      for (const r of rows) {
        const reason: DropReason | null = isGroupChat(chatId) ? 'group'
          // 与约定结算同一条件：那一串 0 送达，约定会自己重新提醒，这里再补就说两遍
          : r.sources.includes('commitment') && delivered(r.root_id) === 0 ? 'commitment'
          : now - r.created_at > RESEND_MAX_AGE_MS ? 'too_old'
          : r.round >= RESEND_MAX_ROUNDS ? 'rounds'
          : null
        if (!reason) { keep.push(r); continue }
        const list = dropped.get(reason) ?? []
        list.push(r)
        dropped.set(reason, list)
      }
      const event = L.tx((): { parts: number; turns: number; round: number } | null => {
        let ev: { parts: number; turns: number; round: number } | null = null
        if (keep.length) {
          const round = 1 + Math.max(...keep.map(r => r.round))
          const from = Math.min(outage?.from ?? Infinity, ...keep.map(r => r.created_at))
          const to = outage?.to ?? Math.max(...keep.map(r => r.created_at))
          const p = formatResendPrompt(keep, { from, to, now, timeZone: this.d.timeZone() })
          const ins = L.insertInbound({ ukey: `int:outage:${chatId}:${maxId}`, chatId, kind: 'synthetic', text: p.text, ts: now, meta: { source: RESEND_SOURCE, round } })
          if (ins.inserted) ev = { parts: p.parts, turns: new Set(keep.map(r => r.turn_id)).size, round }
        }
        L.setMeta(key, String(maxId))
        return ev
      })
      for (const [reason, list] of dropped) {
        this.d.log.info('outage.resend_dropped', { chat: chatId, parts: list.length, reason })
        if (reason === 'too_old') this.d.notifyOwner(`outage_dropped:${chatId}:too_old`, `Telegram 断线期间有 ${list.length} 段话没发出去，已经超过 12 小时，不再补发。`)
        if (reason === 'rounds') this.d.notifyOwner(`outage_dropped:${chatId}:rounds`, `Telegram 断线期间有 ${list.length} 段话补发了 ${RESEND_MAX_ROUNDS} 轮都没送到，已放弃补发。`)
      }
      if (event) {
        this.d.log.info('outage.resend', { chat: chatId, ...event })
        resent.push(chatId)
        this.d.schedule(chatId)
      }
    }
    return resent
  }
}

/** 线路不通时攒着的主人通知：同一 key 只留最新一条，最多 max 个 key（超了丢最早的） */
export class HeldNotices {
  private readonly held = new Map<string, string>()
  constructor(private readonly max = 20) {}
  hold(key: string, text: string): void {
    this.held.delete(key) // 重新插到最后：按"最近一次"排先后
    this.held.set(key, text)
    while (this.held.size > this.max) this.held.delete(this.held.keys().next().value as string)
  }
  /** 取出全部（按攒的先后）并清空 */
  drain(): [string, string][] {
    const out = [...this.held.entries()]
    this.held.clear()
    return out
  }
  get size(): number { return this.held.size }
}
