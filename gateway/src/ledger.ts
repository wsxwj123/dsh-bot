// 送达账本：唯一的真相来源。收到的每条消息、送进模型的每一轮、发给用户的每一段，都先记账再动作。
// 用 bun 自带的 sqlite，不引入第三方依赖。Python 周边脚本只读这个文件。
import { Database } from 'bun:sqlite'
import { mkdirSync } from 'fs'
import { dirname } from 'path'

export type InboundKind = 'user' | 'synthetic' | 'command'
export type InboundState = 'pending' | 'in_turn' | 'done' | 'dropped' | 'dead'
export type TurnKind = 'message' | 'retry' | 'nudge'
export type TurnState = 'preparing' | 'sent' | 'ok' | 'error' | 'cancelled' | 'crashed' | 'aborted'
export type SegmentState = 'active' | 'closed' | 'abandoned'
export type OutboundState = 'pending' | 'sent' | 'failed' | 'ambiguous'
export type OutboundKind = 'text' | 'photo' | 'document' | 'reaction' | 'system' | 'api'

export type InboundRow = {
  id: number
  ukey: string
  chat_id: string
  kind: InboundKind
  tg_message_id: number | null
  sender_id: string | null
  sender_name: string | null
  text: string
  meta: string | null
  ts: number
  received_at: number
  state: InboundState
  turn_id: number | null
  attempts: number
  note: string | null
}

export type SegmentRow = {
  id: number
  chat_id: string
  session_id: string | null
  mcp_token: string
  state: SegmentState
  created_at: number
  closed_at: number | null
  last_used_at: number | null
  used_tokens: number | null
  window: number | null
  model: string | null
  needs_seed: number
  summary: string | null
  close_reason: string | null
}

export type TurnRow = {
  id: number
  root_id: number
  chat_id: string
  segment_id: number
  kind: TurnKind
  attempt: number
  state: TurnState
  inbound_ids: string
  started_at: number
  sent_at: number | null
  ended_at: number | null
  stop_reason: string | null
  error: string | null
  silent: number
  silent_reason: string | null
  used_tokens: number | null
}

export type OutboundRow = {
  id: number
  okey: string | null
  chat_id: string
  turn_id: number | null
  part: number
  kind: OutboundKind
  text: string | null
  file: string | null
  reply_to: number | null
  of_parts: number | null
  state: OutboundState
  tg_message_id: number | null
  error: string | null
  created_at: number
  sent_at: number | null
}

export type TranscriptEntry = { who: 'user' | 'bot'; ts: number; text: string; senderName?: string | null }

const SCHEMA = `
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS inbound (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ukey TEXT NOT NULL UNIQUE,
  chat_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  tg_message_id INTEGER,
  sender_id TEXT,
  sender_name TEXT,
  text TEXT NOT NULL,
  meta TEXT,
  ts INTEGER NOT NULL,
  received_at INTEGER NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',
  turn_id INTEGER,
  attempts INTEGER NOT NULL DEFAULT 0,
  note TEXT
);
CREATE INDEX IF NOT EXISTS inbound_chat_state ON inbound(chat_id, state, id);
CREATE TABLE IF NOT EXISTS segments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id TEXT NOT NULL,
  session_id TEXT,
  mcp_token TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  closed_at INTEGER,
  last_used_at INTEGER,
  used_tokens INTEGER,
  window INTEGER,
  model TEXT,
  needs_seed INTEGER NOT NULL DEFAULT 0,
  summary TEXT,
  close_reason TEXT
);
CREATE INDEX IF NOT EXISTS segments_chat ON segments(chat_id, state);
CREATE TABLE IF NOT EXISTS turns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  root_id INTEGER NOT NULL,
  chat_id TEXT NOT NULL,
  segment_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL,
  inbound_ids TEXT NOT NULL,
  started_at INTEGER NOT NULL,
  sent_at INTEGER,
  ended_at INTEGER,
  stop_reason TEXT,
  error TEXT,
  silent INTEGER NOT NULL DEFAULT 0,
  silent_reason TEXT,
  used_tokens INTEGER
);
CREATE INDEX IF NOT EXISTS turns_state ON turns(state);
CREATE TABLE IF NOT EXISTS outbound (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  okey TEXT UNIQUE,
  chat_id TEXT NOT NULL,
  turn_id INTEGER,
  part INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL,
  text TEXT,
  file TEXT,
  reply_to INTEGER,
  of_parts INTEGER,
  state TEXT NOT NULL,
  tg_message_id INTEGER,
  error TEXT,
  created_at INTEGER NOT NULL,
  sent_at INTEGER
);
CREATE INDEX IF NOT EXISTS outbound_turn ON outbound(turn_id);
CREATE INDEX IF NOT EXISTS outbound_chat ON outbound(chat_id, id);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at INTEGER NOT NULL,
  chat_id TEXT,
  kind TEXT NOT NULL,
  data TEXT
);
`

export const SCHEMA_VERSION = 1

export type NewInbound = {
  ukey: string
  chatId: string
  kind: InboundKind
  tgMessageId?: number | null
  senderId?: string | null
  senderName?: string | null
  text: string
  meta?: Record<string, unknown> | null
  ts: number
  state?: InboundState
  note?: string | null
}

export type RecoveryReport = { crashedTurns: number[]; abortedTurns: number[]; requeued: number[]; settled: number[]; abandonedSegments: number[]; ambiguousOutbound: number[] }

export class Ledger {
  readonly db: Database
  now: () => number = () => Date.now()

  constructor(readonly path: string) {
    if (path !== ':memory:') mkdirSync(dirname(path), { recursive: true })
    this.db = new Database(path, { create: true, strict: true })
    this.db.exec('PRAGMA journal_mode = WAL')
    this.db.exec('PRAGMA synchronous = FULL')
    this.db.exec('PRAGMA busy_timeout = 5000')
    this.db.exec(SCHEMA)
    this.migrate()
    if (!this.getMeta('schema_version')) this.setMeta('schema_version', String(SCHEMA_VERSION))
  }

  close(): void { this.db.close() }

  /** 老账本补列（只加不删） */
  private migrate(): void {
    const cols = new Set(this.db.query<{ name: string }, []>('PRAGMA table_info(outbound)').all().map(c => c.name))
    if (!cols.has('of_parts')) this.db.exec('ALTER TABLE outbound ADD COLUMN of_parts INTEGER')
  }

  tx<T>(fn: () => T): T { return this.db.transaction(fn)() }

  // ─── meta ───
  getMeta(k: string): string | null {
    const r = this.db.query<{ v: string }, [string]>('SELECT v FROM meta WHERE k = ?').get(k)
    return r?.v ?? null
  }
  setMeta(k: string, v: string): void {
    this.db.query('INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v').run(k, v)
  }

  // ─── inbound ───
  insertInbound(m: NewInbound): { inserted: boolean; id: number } {
    const r = this.db.query(`INSERT INTO inbound (ukey, chat_id, kind, tg_message_id, sender_id, sender_name, text, meta, ts, received_at, state, note)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(ukey) DO NOTHING`).run(
      m.ukey, m.chatId, m.kind, m.tgMessageId ?? null, m.senderId ?? null, m.senderName ?? null, m.text,
      m.meta ? JSON.stringify(m.meta) : null, m.ts, this.now(), m.state ?? 'pending', m.note ?? null,
    )
    if (r.changes === 0) {
      const ex = this.db.query<{ id: number }, [string]>('SELECT id FROM inbound WHERE ukey = ?').get(m.ukey)
      return { inserted: false, id: ex?.id ?? 0 }
    }
    return { inserted: true, id: Number(r.lastInsertRowid) }
  }

  /** 收到一条 Telegram 更新：消息入账和推进 offset 在同一个事务里，崩溃时两者要么都生效要么都不生效。 */
  recordUpdate(updateId: number, m: NewInbound | null): { inserted: boolean; id: number } {
    return this.tx(() => {
      const res = m ? this.insertInbound(m) : { inserted: false, id: 0 }
      this.setMeta('tg_offset', String(updateId + 1))
      return res
    })
  }

  tgOffset(): number { return Number(this.getMeta('tg_offset') ?? 0) }

  inbound(id: number): InboundRow | null {
    return this.db.query<InboundRow, [number]>('SELECT * FROM inbound WHERE id = ?').get(id)
  }
  inboundByKey(ukey: string): InboundRow | null {
    return this.db.query<InboundRow, [string]>('SELECT * FROM inbound WHERE ukey = ?').get(ukey)
  }
  pendingFor(chatId: string): InboundRow[] {
    return this.db.query<InboundRow, [string]>(`SELECT * FROM inbound WHERE chat_id = ? AND state = 'pending' ORDER BY id`).all(chatId)
  }
  chatsWithPending(): string[] {
    return this.db.query<{ chat_id: string }, []>(`SELECT DISTINCT chat_id FROM inbound WHERE state = 'pending'`).all().map(r => r.chat_id)
  }
  /** 每个聊天最老一条未处理消息（pending 或 in_turn）的收到时间 */
  oldestUnprocessed(): { chat_id: string; received_at: number }[] {
    return this.db.query<{ chat_id: string; received_at: number }, []>(
      `SELECT chat_id, MIN(received_at) AS received_at FROM inbound WHERE state IN ('pending', 'in_turn') GROUP BY chat_id`).all()
  }
  /** 这个聊天里、给定消息之前最近一条已处理的用户消息的时间（用来算"距上条多久"） */
  previousUserTs(chatId: string, beforeId: number): number | null {
    const r = this.db.query<{ ts: number }, [string, number]>(
      `SELECT ts FROM inbound WHERE chat_id = ? AND id < ? AND kind = 'user' ORDER BY id DESC LIMIT 1`).get(chatId, beforeId)
    return r?.ts ?? null
  }
  settleInbound(ids: number[], state: InboundState, note?: string): void {
    const q = this.db.query(`UPDATE inbound SET state = ?, note = COALESCE(?, note) WHERE id = ?`)
    this.tx(() => { for (const id of ids) q.run(state, note ?? null, id) })
  }
  /** 放回队列重来（崩溃后），记一次尝试 */
  requeueInbound(ids: number[]): void {
    const q = this.db.query(`UPDATE inbound SET state = 'pending', turn_id = NULL, attempts = attempts + 1 WHERE id = ?`)
    this.tx(() => { for (const id of ids) q.run(id) })
  }

  // ─── segments ───
  activeSegment(chatId: string): SegmentRow | null {
    return this.db.query<SegmentRow, [string]>(`SELECT * FROM segments WHERE chat_id = ? AND state = 'active' ORDER BY id DESC LIMIT 1`).get(chatId)
  }
  segment(id: number): SegmentRow | null {
    return this.db.query<SegmentRow, [number]>('SELECT * FROM segments WHERE id = ?').get(id)
  }
  createSegment(chatId: string, mcpToken: string, needsSeed: boolean): SegmentRow {
    return this.tx(() => {
      this.db.query(`UPDATE segments SET state = 'closed', closed_at = ?, close_reason = COALESCE(close_reason, 'superseded') WHERE chat_id = ? AND state = 'active'`).run(this.now(), chatId)
      const r = this.db.query(`INSERT INTO segments (chat_id, mcp_token, state, created_at, needs_seed) VALUES (?, ?, 'active', ?, ?)`).run(chatId, mcpToken, this.now(), needsSeed ? 1 : 0)
      return this.segment(Number(r.lastInsertRowid))!
    })
  }
  setSegmentSession(id: number, sessionId: string, model: string): void {
    this.db.query('UPDATE segments SET session_id = ?, model = ? WHERE id = ?').run(sessionId, model, id)
  }
  setSegmentModel(id: number, model: string): void {
    this.db.query('UPDATE segments SET model = ? WHERE id = ?').run(model, id)
  }
  segmentSeeded(id: number): void {
    this.db.query('UPDATE segments SET needs_seed = 0 WHERE id = ?').run(id)
  }
  segmentUsage(id: number, used: number | null, window: number | null): void {
    this.db.query('UPDATE segments SET used_tokens = COALESCE(?, used_tokens), window = COALESCE(?, window), last_used_at = ? WHERE id = ?').run(used, window, this.now(), id)
  }
  closeSegment(id: number, state: Exclude<SegmentState, 'active'>, reason: string): void {
    this.db.query(`UPDATE segments SET state = ?, closed_at = ?, close_reason = ? WHERE id = ? AND state = 'active'`).run(state, this.now(), reason, id)
  }
  segmentsByToken(token: string): SegmentRow | null {
    return this.db.query<SegmentRow, [string]>('SELECT * FROM segments WHERE mcp_token = ?').get(token)
  }
  /** 这个聊天以前有没有聊过（决定新段要不要补前情） */
  hasHistory(chatId: string): boolean {
    const r = this.db.query<{ n: number }, [string]>(`SELECT COUNT(*) AS n FROM inbound WHERE chat_id = ? AND state = 'done' AND kind = 'user'`).get(chatId)
    return (r?.n ?? 0) > 0
  }

  // ─── turns ───
  /** 开一轮：记下"哪几条消息、送进哪个段的哪一轮"，消息转为 in_turn。之后才允许发给 dsh。 */
  startTurn(o: { chatId: string; segmentId: number; kind: TurnKind; inboundIds: number[]; attempt: number; rootId?: number }): TurnRow {
    return this.tx(() => {
      const r = this.db.query(`INSERT INTO turns (root_id, chat_id, segment_id, kind, attempt, state, inbound_ids, started_at) VALUES (0, ?, ?, ?, ?, 'preparing', ?, ?)`)
        .run(o.chatId, o.segmentId, o.kind, o.attempt, JSON.stringify(o.inboundIds), this.now())
      const id = Number(r.lastInsertRowid)
      this.db.query('UPDATE turns SET root_id = ? WHERE id = ?').run(o.rootId ?? id, id)
      const q = this.db.query(`UPDATE inbound SET state = 'in_turn', turn_id = ? WHERE id = ?`)
      for (const iid of o.inboundIds) q.run(id, iid)
      return this.turn(id)!
    })
  }
  turn(id: number): TurnRow | null {
    return this.db.query<TurnRow, [number]>('SELECT * FROM turns WHERE id = ?').get(id)
  }
  /** 马上要发给 dsh：从这一刻起，这条消息可能已经进了 dsh 的历史 */
  markTurnSent(id: number): void {
    this.db.query(`UPDATE turns SET state = 'sent', sent_at = ? WHERE id = ?`).run(this.now(), id)
  }
  finishTurn(id: number, state: TurnState, o: { stopReason?: string; error?: string; usedTokens?: number | null } = {}): void {
    this.db.query(`UPDATE turns SET state = ?, ended_at = ?, stop_reason = ?, error = ?, used_tokens = COALESCE(?, used_tokens) WHERE id = ?`)
      .run(state, this.now(), o.stopReason ?? null, o.error ?? null, o.usedTokens ?? null, id)
  }
  markSilent(id: number, reason: string): void {
    this.db.query('UPDATE turns SET silent = 1, silent_reason = ? WHERE id = ?').run(reason.slice(0, 500), id)
  }
  /** 这一串（原始轮 + 补救轮）里已经送达或可能送达的段数。大于 0 就绝不重试。 */
  deliveredInChain(rootId: number): number {
    const r = this.db.query<{ n: number }, [number]>(
      `SELECT COUNT(*) AS n FROM outbound o JOIN turns t ON o.turn_id = t.id WHERE t.root_id = ? AND o.kind IN ('text','photo','document') AND o.state IN ('sent','ambiguous','pending')`).get(rootId)
    return r?.n ?? 0
  }
  /** 这一串里已经发出（或可能发出）的文字段，用来拦住模型把同样的话再发一遍 */
  sentTextsInChain(rootId: number): string[] {
    return this.db.query<{ text: string }, [number]>(
      `SELECT o.text FROM outbound o JOIN turns t ON o.turn_id = t.id WHERE t.root_id = ? AND o.kind = 'text' AND o.state IN ('sent','ambiguous','pending') AND o.text IS NOT NULL`).all(rootId).map(r => r.text)
  }
  clearSilent(rootId: number): void {
    this.db.query('UPDATE turns SET silent = 0 WHERE root_id = ?').run(rootId)
  }
  segmentTurnCount(segmentId: number): number {
    return this.db.query<{ n: number }, [number]>('SELECT COUNT(*) AS n FROM turns WHERE segment_id = ?').get(segmentId)?.n ?? 0
  }
  /**
   * 这个聊天最近一轮是不是"回复发到一半网关就断了"：最近一轮状态是 crashed，
   * 且它最后一次 reply 计划发的段数多于实际发出（或可能发出）的段数。
   */
  interruptedReply(chatId: string): { sent: number; total: number } | null {
    const t = this.db.query<{ id: number; state: string }, [string]>('SELECT id, state FROM turns WHERE chat_id = ? ORDER BY id DESC LIMIT 1').get(chatId)
    if (!t || t.state !== 'crashed') return null
    const rows = this.db.query<{ okey: string | null; of_parts: number | null; state: string }, [number]>(
      `SELECT okey, of_parts, state FROM outbound WHERE turn_id = ? AND okey IS NOT NULL ORDER BY id`).all(t.id)
    if (rows.length === 0) return null
    const call = rows[rows.length - 1]!.okey!.replace(/:p\d+$/, ':')
    const mine = rows.filter(r => r.okey!.startsWith(call))
    const total = Math.max(...mine.map(r => r.of_parts ?? 0))
    const sent = mine.filter(r => r.state === 'sent' || r.state === 'ambiguous').length
    return total > sent && sent > 0 ? { sent, total } : null
  }

  silentInChain(rootId: number): boolean {
    const r = this.db.query<{ n: number }, [number]>('SELECT COUNT(*) AS n FROM turns WHERE root_id = ? AND silent = 1').get(rootId)
    return (r?.n ?? 0) > 0
  }

  // ─── outbound ───
  /** 先记"打算发"，再真的发；崩溃时留下的 pending 一律按"可能已送达"处理，绝不重发。 */
  outboundIntent(o: { okey?: string | null; chatId: string; turnId: number | null; part: number; ofParts?: number | null; kind: OutboundKind; text?: string | null; file?: string | null; replyTo?: number | null }): number {
    const r = this.db.query(`INSERT INTO outbound (okey, chat_id, turn_id, part, of_parts, kind, text, file, reply_to, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)`)
      .run(o.okey ?? null, o.chatId, o.turnId, o.part, o.ofParts ?? null, o.kind, o.text ?? null, o.file ?? null, o.replyTo ?? null, this.now())
    return Number(r.lastInsertRowid)
  }
  outboundResult(id: number, state: Exclude<OutboundState, 'pending'>, o: { tgMessageId?: number | null; error?: string | null } = {}): void {
    this.db.query('UPDATE outbound SET state = ?, tg_message_id = ?, error = ?, sent_at = ? WHERE id = ?')
      .run(state, o.tgMessageId ?? null, o.error ?? null, state === 'sent' ? this.now() : null, id)
  }
  outboundForTurn(turnId: number): OutboundRow[] {
    return this.db.query<OutboundRow, [number]>('SELECT * FROM outbound WHERE turn_id = ? ORDER BY id').all(turnId)
  }

  // ─── 启动时恢复 ───
  /**
   * 上次没正常退出时调用。规则：
   * - 还在 preparing 的轮：没发给 dsh，消息放回队列，段照常续用。
   * - 已经 sent 的轮：不知道消息进没进 dsh 的历史，段作废（下一轮开新段、用账本补前情）。
   *   这一串已经有回复送达（或可能送达）就不再重试，否则放回队列。
   * - 还在 pending 的发送记录：可能已经发出去了，记为 ambiguous，绝不重发。
   */
  recoverOnStartup(maxAttempts: number): RecoveryReport {
    return this.tx(() => {
      const rep: RecoveryReport = { crashedTurns: [], abortedTurns: [], requeued: [], settled: [], abandonedSegments: [], ambiguousOutbound: [] }
      for (const o of this.db.query<{ id: number }, []>(`SELECT id FROM outbound WHERE state = 'pending'`).all()) {
        this.outboundResult(o.id, 'ambiguous', { error: 'gateway restarted before the result was recorded' })
        rep.ambiguousOutbound.push(o.id)
      }
      const open = this.db.query<TurnRow, []>(`SELECT * FROM turns WHERE state IN ('preparing', 'sent') ORDER BY id`).all()
      for (const t of open) {
        const ids = JSON.parse(t.inbound_ids) as number[]
        const stillMine = ids.filter(id => this.inbound(id)?.turn_id === t.id && this.inbound(id)?.state === 'in_turn')
        if (t.state === 'preparing') {
          this.finishTurn(t.id, 'aborted', { error: 'gateway restarted before sending' })
          rep.abortedTurns.push(t.id)
          const q = this.db.query(`UPDATE inbound SET state = 'pending', turn_id = NULL WHERE id = ?`)
          for (const id of stillMine) { q.run(id); rep.requeued.push(id) }
          continue
        }
        this.finishTurn(t.id, 'crashed', { error: 'gateway restarted during the turn' })
        rep.crashedTurns.push(t.id)
        const seg = this.segment(t.segment_id)
        if (seg && seg.state === 'active') {
          this.closeSegment(seg.id, 'abandoned', 'crash')
          rep.abandonedSegments.push(seg.id)
        }
        if (this.deliveredInChain(t.root_id) > 0 || this.silentInChain(t.root_id)) {
          this.settleInbound(stillMine, 'done', 'replied before crash')
          rep.settled.push(...stillMine)
        } else {
          for (const id of stillMine) {
            const row = this.inbound(id)!
            if (row.attempts + 1 > maxAttempts) {
              this.settleInbound([id], 'dead', 'too many crashes')
            } else {
              this.requeueInbound([id])
              rep.requeued.push(id)
            }
          }
        }
      }
      // 防御：in_turn 但对应的轮已经结束（理论上不会出现）
      for (const r of this.db.query<{ id: number }, []>(`SELECT i.id FROM inbound i LEFT JOIN turns t ON i.turn_id = t.id WHERE i.state = 'in_turn' AND (t.id IS NULL OR t.state NOT IN ('preparing','sent'))`).all()) {
        this.db.query(`UPDATE inbound SET state = 'pending', turn_id = NULL WHERE id = ?`).run(r.id)
        rep.requeued.push(r.id)
      }
      return rep
    })
  }

  // ─── 前情：从账本取最近原话（对方说的 + bot 真正发出去的） ───
  recentTranscript(chatId: string, o: { maxChars: number; sinceMs?: number; excludeInboundIds?: number[] }): TranscriptEntry[] {
    const since = o.sinceMs ?? 0
    const exclude = new Set(o.excludeInboundIds ?? [])
    const users = this.db.query<{ id: number; ts: number; text: string; sender_name: string | null }, [string, number]>(
      `SELECT id, ts, text, sender_name FROM inbound WHERE chat_id = ? AND kind = 'user' AND state IN ('done', 'dead') AND received_at > ? ORDER BY id DESC LIMIT 400`).all(chatId, since)
    const bots = this.db.query<{ ts: number; text: string }, [string, number]>(
      `SELECT COALESCE(sent_at, created_at) AS ts, text FROM outbound WHERE chat_id = ? AND kind = 'text' AND state IN ('sent', 'ambiguous') AND turn_id IS NOT NULL AND created_at > ? ORDER BY id DESC LIMIT 400`).all(chatId, since)
    const all: TranscriptEntry[] = [
      ...users.filter(u => !exclude.has(u.id)).map(u => ({ who: 'user' as const, ts: u.ts, text: u.text, senderName: u.sender_name })),
      ...bots.map(b => ({ who: 'bot' as const, ts: b.ts, text: b.text })),
    ].sort((a, b) => b.ts - a.ts)
    const out: TranscriptEntry[] = []
    let used = 0
    for (const e of all) {
      if (used + e.text.length > o.maxChars && out.length > 0) break
      out.push(e)
      used += e.text.length
    }
    return out.reverse()
  }

  // ─── 事件 ───
  event(chatId: string | null, kind: string, data?: Record<string, unknown>): void {
    this.db.query('INSERT INTO events (at, chat_id, kind, data) VALUES (?, ?, ?, ?)').run(this.now(), chatId, kind, data ? JSON.stringify(data) : null)
  }
  lastEvent(chatId: string, kind: string): { at: number; data: string | null } | null {
    return this.db.query<{ at: number; data: string | null }, [string, string]>('SELECT at, data FROM events WHERE chat_id = ? AND kind = ? ORDER BY id DESC LIMIT 1').get(chatId, kind)
  }
}
