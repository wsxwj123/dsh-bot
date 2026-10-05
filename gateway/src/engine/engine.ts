// 回合调度：每个聊天一条队列、一次只跑一轮；不同聊天可以并行（有上限）。
// 一轮的结局怎么处理，见方案 3.3：
//   正常结束 → 看我们自己的发送记录判断有没有回复（不看 stopReason）
//   dsh 报错但进程还活着 → 消息已经在 dsh 的历史里，不能原样重发，等一会儿后发"补救提示"，最多 N 次
//   dsh 进程死了 → 不知道消息进没进它的历史，作废这一段，换新会话、用账本补前情
//   已经有回复发出去了 → 不再重试（宁可少回一次，也不重复说话）
//   卡住 → session/cancel；取消不掉就重启 dsh，按"进程死了"处理
import { existsSync, readFileSync, statSync } from 'fs'
import { join } from 'path'
import { loadAccess, reloadBrain, type Access, type Brain, type BotConfig } from '../config'
import { AcpError, AcpExited, AcpTimeout, type SessionUpdate } from '../dsh/acp'
import { buildDshEnv, defaultDshCommand, type DshProcess, type DshSpec } from '../dsh/process'
import { buildPatchRows, modelValue, patchText, restartFingerprint, type PatchInput } from '../dsh/profile'
import type { InboundRow, Ledger, SegmentRow, TurnKind } from '../ledger'
import { safeError, type Logger } from '../log'
import type { McpServer, ToolDef } from '../mcp/server'
import type { TelegramApi } from '../telegram/api'
import { parseCommand } from '../telegram/inbound'
import { describeResults, type Sender } from '../telegram/sender'
import { Backoff, randomToken, sleep } from '../util'
import { formatMessages, formatSeed, NUDGE_NO_ACTION, NUDGE_RETRY } from './format'

type ActiveTurn = {
  turnId: number
  rootId: number
  chatId: string
  segmentId: number
  sessionId: string
  startedAt: number
  lastProgressAt: number
  toolsInFlight: number
  replyCalls: number
  delivered: number
  silent: boolean
  cancelledAt: number | null
}

type ChatState = { running: boolean; timer: ReturnType<typeof setTimeout> | null; active: ActiveTurn | null }

type Outcome = (
  | { type: 'ok'; stopReason: string }
  | { type: 'cancelled' }
  | { type: 'error'; err: string; fatal: boolean }
  | { type: 'crashed'; err: string }
) & { used?: number | null }

const FATAL_RE = /MISSING_CREDENTIAL|INVALID_CREDENTIAL|QUOTA|UNKNOWN_MODEL|unauthori[sz]ed|authentication|invalid api key|insufficient|balance|\b40[123]\b/i

export function brainKey(b: Brain): string {
  return `${b.provider}/${b.model}/${b.reasoningEffort ?? ''}`
}

export class Engine {
  private chats = new Map<string, ChatState>()
  private runningCount = 0
  private waiting = new Set<string>()
  private loaded = new Map<string, number>() // sessionId → dsh 代数
  private dshStarting: Promise<void> | null = null
  private dshBackoff = new Backoff(2_000, 60_000)
  private dshNextTryAt = 0
  private dshFailures = 0
  private fingerprint = ''
  private restartPending = false
  private stopping = false
  private noticeAt = new Map<string, number>()
  private timers: ReturnType<typeof setInterval>[] = []
  private configMtime = 0
  private personaMtime = 0
  private lastProbeOkAt = 0
  private probeFailures = 0
  private unhealthySince: number | null = null
  brain: Brain
  botUsername = ''

  constructor(
    readonly cfg: BotConfig,
    readonly ledger: Ledger,
    readonly log: Logger,
    readonly api: TelegramApi,
    readonly sender: Sender,
    readonly dsh: DshProcess,
    readonly mcp: McpServer,
  ) {
    this.brain = cfg.brain
    this.dsh.onUnexpectedExit = () => { this.loaded.clear() }
  }

  // ─── 工具（挂给模型的 MCP 工具） ───

  static tools(engine: () => Engine): ToolDef[] {
    return [
      {
        name: 'reply',
        description: '给对方发消息。这是对方能看到你说话的唯一方式。text 里用空行分段，会按段依次发出。返回每段是否送达。',
        inputSchema: {
          type: 'object',
          properties: {
            text: { type: 'string', description: '要发的文字' },
            reply_to: { type: 'number', description: '可选：要引用回复的消息编号（⟦…⟧ 里 # 后面的数字）' },
            files: { type: 'array', items: { type: 'string' }, description: '可选：要一起发的图片或文件的绝对路径' },
          },
          required: ['text'],
        },
        call: (args, ctx) => engine().toolReply(args, ctx),
      },
      {
        name: 'react',
        description: '给某条消息加一个表情回应（例如 ❤️、👍、😂）。',
        inputSchema: {
          type: 'object',
          properties: { message_id: { type: 'number', description: '消息编号' }, emoji: { type: 'string', description: '一个表情' } },
          required: ['message_id', 'emoji'],
        },
        call: (args, ctx) => engine().toolReact(args, ctx),
      },
      {
        name: 'stay_silent',
        description: '这一轮决定不回复对方时调用，写明原因（原因不会发给对方）。',
        inputSchema: { type: 'object', properties: { reason: { type: 'string' } }, required: ['reason'] },
        call: (args, ctx) => engine().toolSilent(args, ctx),
      },
    ]
  }

  private activeFor(chatId: string, segmentId: number): ActiveTurn | null {
    const at = this.chats.get(chatId)?.active
    return at && at.segmentId === segmentId ? at : null
  }

  async toolReply(args: Record<string, unknown>, ctx: { chatId: string; segmentId: number }) {
    const at = this.activeFor(ctx.chatId, ctx.segmentId)
    if (!at) return { text: '这一轮已经结束，消息没有发出。', isError: true }
    const text = typeof args.text === 'string' ? args.text : ''
    const files = Array.isArray(args.files) ? args.files.filter((f): f is string => typeof f === 'string') : []
    if (!text.trim() && files.length === 0) return { text: 'text 不能为空。', isError: true }
    const replyTo = Number(args.reply_to)
    at.toolsInFlight++
    at.replyCalls++
    at.lastProgressAt = Date.now()
    try {
      const results = await this.sender.send({ chatId: at.chatId, turnId: at.turnId, callSeq: at.replyCalls, text, files, replyTo: Number.isInteger(replyTo) && replyTo > 0 ? replyTo : undefined })
      const d = describeResults(results)
      at.delivered += d.delivered
      this.log.info('tool.reply', { turn: at.turnId, parts: results.length, delivered: d.delivered })
      return { text: d.text, isError: d.isError }
    } finally {
      at.toolsInFlight--
      at.lastProgressAt = Date.now()
    }
  }

  async toolReact(args: Record<string, unknown>, ctx: { chatId: string; segmentId: number }) {
    const at = this.activeFor(ctx.chatId, ctx.segmentId)
    if (!at) return { text: '这一轮已经结束。', isError: true }
    const mid = Number(args.message_id)
    const emoji = typeof args.emoji === 'string' ? args.emoji.trim() : ''
    if (!Number.isInteger(mid) || mid <= 0 || !emoji) return { text: '需要 message_id 和 emoji。', isError: true }
    const outId = this.ledger.outboundIntent({ chatId: at.chatId, turnId: at.turnId, part: 0, kind: 'reaction', text: emoji, replyTo: mid })
    try {
      await this.api.setMessageReaction(at.chatId, mid, emoji)
      this.ledger.outboundResult(outId, 'sent')
      return { text: '已加上表情。' }
    } catch (e) {
      this.ledger.outboundResult(outId, 'failed', { error: safeError(e) })
      return { text: '没加上（可能这个表情 Telegram 不支持，或者消息编号不对）。', isError: true }
    }
  }

  async toolSilent(args: Record<string, unknown>, ctx: { chatId: string; segmentId: number }) {
    const at = this.activeFor(ctx.chatId, ctx.segmentId)
    if (!at) return { text: '这一轮已经结束。', isError: true }
    const reason = typeof args.reason === 'string' ? args.reason : ''
    this.ledger.markSilent(at.turnId, reason || '(未写原因)')
    at.silent = true
    this.log.info('tool.stay_silent', { turn: at.turnId })
    return { text: '好的，这次不回复。' }
  }

  // ─── 生命周期 ───

  start(): void {
    // 崩溃重来的次数上限和报错重试一致：最多重来 maxTurnRetries 次（共 maxTurnRetries + 1 次）
    const rep = this.ledger.recoverOnStartup(this.cfg.gw.maxTurnRetries + 1)
    if (rep.crashedTurns.length || rep.abortedTurns.length || rep.ambiguousOutbound.length) {
      this.log.warn('recovery', { crashed: rep.crashedTurns.length, aborted: rep.abortedTurns.length, requeued: rep.requeued.length, settled: rep.settled.length, abandoned_segments: rep.abandonedSegments.length, ambiguous_outbound: rep.ambiguousOutbound.length })
    }
    this.fingerprint = restartFingerprint(this.patchInput())
    this.configMtime = mtime(this.cfg.configPath)
    this.personaMtime = mtime(this.personaPath())
    const g = this.cfg.gw
    this.timers.push(setInterval(() => this.watchConfig(), g.configPollMs))
    this.timers.push(setInterval(() => void this.probe(), g.probeMs))
    this.timers.push(setInterval(() => this.watchHealth(), Math.min(10_000, g.heartbeatMs)))
    for (const chatId of this.ledger.chatsWithPending()) this.schedule(chatId)
  }

  async stop(waitMs = 60_000): Promise<void> {
    this.stopping = true
    for (const t of this.timers) clearInterval(t)
    for (const st of this.chats.values()) if (st.timer) clearTimeout(st.timer)
    // 不打断正在生成的回复：等它们结束（有上限）
    const end = Date.now() + waitMs
    while (this.runningCount > 0 && Date.now() < end) await sleep(100)
    await this.dsh.stop()
  }

  private personaPath(): string { return join(this.cfg.channelDir, 'CLAUDE.md') }

  private patchInput(): PatchInput {
    let persona = ''
    try { persona = readFileSync(this.personaPath(), 'utf8') } catch {}
    return { persona, brain: this.brain, credentialsPath: this.cfg.credentialsPath, sessionsRoot: join(this.cfg.dshHome, 'sessions') }
  }

  dshSpec(): DshSpec {
    const input = this.patchInput()
    return {
      command: this.cfg.gw.dshCommand ?? defaultDshCommand(this.cfg.harnessDir),
      patchPath: join(this.cfg.dshHome, 'bot.patch.yml'),
      patchText: patchText(buildPatchRows(input)),
      cwd: this.cfg.workDir,
      env: buildDshEnv({ dshHome: this.cfg.dshHome, homeDir: this.cfg.homeDir, timezone: this.cfg.gw.timezone }),
      pidFile: join(this.cfg.stateDir, 'dsh.pid'),
      stderrLog: join(this.cfg.logsDir, 'dsh-stderr.log'),
    }
  }

  private async ensureDsh(): Promise<void> {
    if (this.dsh.running) return
    if (this.dshStarting) return this.dshStarting
    const wait = this.dshNextTryAt - Date.now()
    if (wait > 0) throw new Error(`dsh restart backoff ${wait}ms`)
    this.dshStarting = (async () => {
      try {
        if (!existsSync(this.personaPath())) throw new Error(`persona file missing: ${this.personaPath()}`)
        const input = this.patchInput()
        await this.dsh.start(this.dshSpec())
        this.fingerprint = restartFingerprint(input)
        this.loaded.clear()
        this.lastProbeOkAt = Date.now()
        this.dshBackoff.reset()
        this.dshFailures = 0
      } catch (e) {
        this.dshFailures++
        this.dshNextTryAt = Date.now() + this.dshBackoff.next()
        this.log.error('dsh.start_failed', { err: safeError(e), failures: this.dshFailures })
        if (this.dshFailures === 3) void this.notifyOwner('dsh-start', `模型进程连续 ${this.dshFailures} 次没能启动（${safeError(e).slice(0, 120)}）。消息都还在队列里，恢复后会补上。`)
        throw e
      } finally {
        this.dshStarting = null
      }
    })()
    return this.dshStarting
  }

  // ─── 调度 ───

  onInbound(chatId: string): void { this.schedule(chatId) }

  private chat(chatId: string): ChatState {
    let st = this.chats.get(chatId)
    if (!st) { st = { running: false, timer: null, active: null }; this.chats.set(chatId, st) }
    return st
  }

  schedule(chatId: string, minDelayMs = 0): void {
    if (this.stopping) return
    const st = this.chat(chatId)
    if (st.running) return
    if (st.timer) { clearTimeout(st.timer); st.timer = null }
    const pending = this.ledger.pendingFor(chatId)
    if (pending.length === 0) return
    const now = Date.now()
    let wait = minDelayMs
    const g = this.cfg.gw
    if (g.burstWindowMs > 0 && pending.every(p => p.kind === 'user')) {
      const first = pending[0]!.received_at
      const last = pending[pending.length - 1]!.received_at
      const untilQuiet = g.burstWindowMs - (now - last)
      const untilCap = g.burstMaxMs > 0 ? g.burstMaxMs - (now - first) : untilQuiet
      wait = Math.max(wait, Math.min(untilQuiet, untilCap))
    }
    if (wait > 0) {
      st.timer = setTimeout(() => { st.timer = null; this.schedule(chatId) }, wait)
      return
    }
    if (this.runningCount >= g.maxConcurrentTurns) { this.waiting.add(chatId); return }
    this.waiting.delete(chatId)
    st.running = true
    this.runningCount++
    let retryDelay = 0
    void this.runChat(chatId)
      .then(d => { retryDelay = d })
      .catch(e => { this.log.error('engine.run_chat_failed', { chat: chatId, err: safeError(e) }); retryDelay = 5_000 })
      .finally(() => {
        st.running = false
        this.runningCount--
        if (this.restartPending && this.runningCount === 0) void this.restartIdle()
        this.schedule(chatId, retryDelay)
        for (const c of [...this.waiting]) { if (this.runningCount >= g.maxConcurrentTurns) break; this.schedule(c) }
      })
  }

  /** 处理这个聊天积压的一批消息。返回值：下次再试前至少等多久（毫秒）。 */
  private async runChat(chatId: string): Promise<number> {
    let pending = this.ledger.pendingFor(chatId)
    while (pending.length && pending[0]!.kind === 'command') {
      await this.handleCommand(pending[0]!)
      pending = this.ledger.pendingFor(chatId)
    }
    const cut = pending.findIndex(p => p.kind === 'command')
    const batch = cut >= 0 ? pending.slice(0, cut) : pending
    if (batch.length === 0) return 0
    try {
      await this.ensureDsh()
    } catch {
      return Math.max(1_000, this.dshNextTryAt - Date.now())
    }
    return this.runBatch(chatId, batch)
  }

  private async runBatch(chatId: string, batch: InboundRow[]): Promise<number> {
    const ids = batch.map(b => b.id)
    let seg: SegmentRow
    let seed: string | null
    try {
      ({ seg, seed } = await this.ensureSegment(chatId, ids))
    } catch (e) {
      this.log.error('segment.prepare_failed', { chat: chatId, err: safeError(e) })
      if (e instanceof AcpExited || !this.dsh.running) return 1_000
      return 5_000
    }
    const prevTs = this.ledger.previousUserTs(chatId, batch[0]!.id)
    let blocks = [...(seed ? [seed] : []), formatMessages(batch, prevTs, { timeZone: this.cfg.gw.timezone })]
    let kind: TurnKind = 'message'
    let attempt = 0
    let rootId: number | undefined
    let nudgedNoAction = false
    const g = this.cfg.gw

    for (;;) {
      const turn = this.ledger.startTurn({ chatId, segmentId: seg.id, kind, inboundIds: ids, attempt, rootId })
      rootId ??= turn.id
      const outcome = await this.runPrompt(turn.id, rootId, seg, blocks)
      const delivered = this.ledger.deliveredInChain(rootId)
      const silent = this.ledger.silentInChain(rootId)
      const done = delivered > 0 || silent
      this.log.info('turn.end', { turn: turn.id, kind, attempt, outcome: outcome.type, delivered, silent })

      if (outcome.type === 'ok') {
        this.ledger.finishTurn(turn.id, 'ok', { stopReason: outcome.stopReason, usedTokens: outcome.used })
        if (done) { this.ledger.settleInbound(ids, 'done'); return 0 }
        if (!nudgedNoAction) { nudgedNoAction = true; kind = 'nudge'; blocks = [NUDGE_NO_ACTION]; continue }
        this.log.warn('turn.no_reply', { turn: turn.id })
        this.ledger.settleInbound(ids, 'done', 'model neither replied nor chose silence')
        return 0
      }
      if (outcome.type === 'crashed') {
        this.ledger.finishTurn(turn.id, 'crashed', { error: outcome.err })
        this.ledger.closeSegment(seg.id, 'abandoned', 'crash')
        this.loaded.delete(seg.session_id ?? '')
        if (done) { this.ledger.settleInbound(ids, 'done', 'replied before crash'); return 0 }
        const dead: number[] = []
        for (const id of ids) {
          const row = this.ledger.inbound(id)!
          if (row.attempts + 1 > g.maxTurnRetries + 1) dead.push(id)
        }
        if (dead.length) {
          this.ledger.settleInbound(dead, 'dead', 'dsh crashed repeatedly')
          void this.notifyOwner('dead', `有 ${dead.length} 条消息处理时模型进程反复崩溃，已放弃。详情见网关日志。`)
        }
        this.ledger.requeueInbound(ids.filter(id => !dead.includes(id)))
        return 1_000
      }
      if (outcome.type === 'cancelled') {
        this.ledger.finishTurn(turn.id, 'cancelled', { stopReason: 'cancelled', usedTokens: outcome.used })
        if (done) { this.ledger.settleInbound(ids, 'done', 'replied before cancel'); return 0 }
      } else {
        this.ledger.finishTurn(turn.id, 'error', { error: outcome.err, usedTokens: outcome.used })
        if (done) { this.ledger.settleInbound(ids, 'done', 'replied before error'); return 0 }
        if (outcome.fatal) {
          this.ledger.settleInbound(ids, 'dead', outcome.err.slice(0, 200))
          void this.notifyOwner('fatal', `模型那边拒绝了请求（${outcome.err.slice(0, 160)}），可能是密钥、余额或模型名的问题。${ids.length} 条消息没处理。`)
          return 0
        }
      }
      if (attempt >= g.maxTurnRetries) {
        this.ledger.settleInbound(ids, 'dead', 'retries exhausted')
        void this.notifyOwner('dead', `有 ${ids.length} 条消息重试 ${attempt} 次后仍然失败，已放弃（${outcome.type === 'error' ? outcome.err.slice(0, 120) : '一直卡住'}）。`)
        return 0
      }
      const wait = g.retryBackoffMs[Math.min(attempt, g.retryBackoffMs.length - 1)] ?? 0
      if (wait > 0) await sleep(wait)
      if (!this.dsh.running) {
        // 等待期间进程没了：这一段作废，消息放回队列，下一轮开新段补前情
        this.ledger.closeSegment(seg.id, 'abandoned', 'dsh gone during retry wait')
        this.ledger.requeueInbound(ids)
        return 1_000
      }
      attempt++
      kind = 'retry'
      blocks = [NUDGE_RETRY]
    }
  }

  /** 发一轮 prompt 并盯着它：卡住就取消，取消不掉就重启 dsh。 */
  private async runPrompt(turnId: number, rootId: number, seg: SegmentRow, blocks: string[]): Promise<Outcome> {
    const conn = this.dsh.conn
    const sessionId = seg.session_id!
    if (!conn) return { type: 'crashed', err: 'dsh not running' }
    const st = this.chat(seg.chat_id)
    const at: ActiveTurn = {
      turnId, rootId, chatId: seg.chat_id, segmentId: seg.id, sessionId,
      startedAt: Date.now(), lastProgressAt: Date.now(), toolsInFlight: 0, replyCalls: 0, delivered: 0, silent: false, cancelledAt: null,
    }
    st.active = at
    let used: number | null = null
    let size: number | null = null
    conn.onSession(sessionId, (u: SessionUpdate) => {
      at.lastProgressAt = Date.now()
      if (u.update.sessionUpdate === 'usage_update') {
        if (typeof u.update.used === 'number') used = u.update.used
        if (typeof u.update.size === 'number') size = u.update.size
      }
    })
    const g = this.cfg.gw
    const typing = setInterval(() => { void this.api.sendChatAction(seg.chat_id).catch(() => {}) }, 4_500)
    void this.api.sendChatAction(seg.chat_id).catch(() => {})
    const cancelGrace = Math.max(1_000, Math.min(15_000, g.stallCancelMs / 2))
    const watchdog = setInterval(() => {
      const now = Date.now()
      if (at.toolsInFlight > 0) return
      if (at.cancelledAt === null && now - at.lastProgressAt > g.stallCancelMs) {
        at.cancelledAt = now
        this.log.warn('turn.stalled_cancel', { turn: turnId, idle_ms: now - at.lastProgressAt })
        conn.notify('session/cancel', { sessionId })
      } else if (at.cancelledAt !== null && now - at.cancelledAt > cancelGrace) {
        at.cancelledAt = Number.POSITIVE_INFINITY
        this.log.error('turn.cancel_ignored_restart_dsh', { turn: turnId })
        void this.dsh.stop(2_000)
      }
    }, Math.max(50, Math.min(1_000, g.stallCancelMs / 4)))

    this.ledger.markTurnSent(turnId)
    if (seg.needs_seed) { this.ledger.segmentSeeded(seg.id); seg.needs_seed = 0 }
    try {
      const res = await conn.request<{ stopReason?: string }>('session/prompt', { sessionId, prompt: blocks.map(text => ({ type: 'text', text })) }, 0)
      if (at.cancelledAt !== null || res?.stopReason === 'cancelled') return { type: 'cancelled', used }
      return { type: 'ok', stopReason: String(res?.stopReason ?? ''), used }
    } catch (e) {
      if (e instanceof AcpExited || !this.dsh.running) return { type: 'crashed', err: safeError(e) }
      if (at.cancelledAt !== null) return { type: 'cancelled', used }
      const err = e instanceof AcpError ? `${e.code} ${e.message}` : safeError(e)
      return { type: 'error', err, fatal: FATAL_RE.test(err), used }
    } finally {
      clearInterval(typing)
      clearInterval(watchdog)
      conn.onSession(sessionId, null)
      st.active = null
      if (used !== null || size !== null) this.ledger.segmentUsage(seg.id, used, size)
    }
  }

  // ─── 段（= dsh 会话） ───

  private async ensureSegment(chatId: string, batchIds: number[]): Promise<{ seg: SegmentRow; seed: string | null }> {
    const conn = this.dsh.conn
    if (!conn) throw new AcpExited('session/new')
    const gen = this.dsh.generation
    let seg = this.ledger.activeSegment(chatId)
    if (seg?.session_id && this.loaded.get(seg.session_id) !== gen) {
      try {
        await conn.request('session/resume', { sessionId: seg.session_id, cwd: this.cfg.workDir, mcpServers: this.mcp.serverSpec(seg.id, seg.mcp_token) }, 60_000)
        this.loaded.set(seg.session_id, gen)
        this.log.info('segment.resumed', { chat: chatId, segment: seg.id })
      } catch (e) {
        if (e instanceof AcpExited) throw e
        this.log.warn('segment.resume_failed', { chat: chatId, segment: seg.id, err: safeError(e) })
        this.ledger.closeSegment(seg.id, 'abandoned', 'resume failed')
        seg = null
      }
    }
    if (!seg || !seg.session_id) {
      if (!seg) seg = this.ledger.createSegment(chatId, randomToken(), this.ledger.hasHistory(chatId))
      const s = await conn.request<{ sessionId: string; configOptions?: any[] }>('session/new', { cwd: this.cfg.workDir, mcpServers: this.mcp.serverSpec(seg.id, seg.mcp_token) }, 60_000)
      this.ledger.setSegmentSession(seg.id, s.sessionId, '')
      this.loaded.set(s.sessionId, gen)
      this.log.info('segment.created', { chat: chatId, segment: seg.id, seeded: seg.needs_seed === 1 })
      seg = this.ledger.segment(seg.id)!
    }
    if (seg.model !== brainKey(this.brain)) await this.applyBrain(seg)
    let seed: string | null = null
    if (seg.needs_seed) {
      const clearAt = Number(this.ledger.getMeta(`clear_at:${chatId}`) ?? 0)
      const entries = this.ledger.recentTranscript(chatId, { maxChars: this.cfg.gw.seedRecentChars, sinceMs: clearAt, excludeInboundIds: batchIds })
      seed = formatSeed(entries, { timeZone: this.cfg.gw.timezone })
    }
    return { seg: this.ledger.segment(seg.id)!, seed }
  }

  /** 按配置给会话设模型和思考强度。失败不挡聊天，记一条错误并通知主人。 */
  private async applyBrain(seg: SegmentRow): Promise<void> {
    const conn = this.dsh.conn
    if (!conn || !seg.session_id) return
    const b = this.brain
    try {
      const r = await conn.request<{ configOptions?: any[] }>('session/set_config_option', { sessionId: seg.session_id, configId: 'model', value: modelValue(b) }, 30_000)
      if (b.reasoningEffort) {
        const opt = (r?.configOptions ?? []).find((o: any) => o?.id === 'reasoning_effort')
        if (opt) await conn.request('session/set_config_option', { sessionId: seg.session_id, configId: 'reasoning_effort', value: b.reasoningEffort }, 30_000)
        else this.log.info('brain.effort_unsupported', { model: b.model })
      }
      this.ledger.setSegmentModel(seg.id, brainKey(b))
      this.log.info('brain.applied', { segment: seg.id, provider: b.provider, model: b.model, effort: b.reasoningEffort ?? null })
    } catch (e) {
      if (e instanceof AcpExited) throw e
      this.log.error('brain.apply_failed', { segment: seg.id, err: safeError(e) })
      void this.notifyOwner('brain', `换模型没成功（${b.provider} / ${b.model}）：${safeError(e).slice(0, 120)}。先继续用原来的模型。`)
      this.ledger.setSegmentModel(seg.id, brainKey(b)) // 不在每轮反复重试
    }
  }

  // ─── 命令 ───

  isOwner(senderId: string | null): boolean {
    return senderId !== null && this.cfg.gw.owners.includes(senderId)
  }

  private async handleCommand(row: InboundRow): Promise<void> {
    const cmd = parseCommand(row.text)
    const targetOk = !cmd?.target || (this.botUsername !== '' && cmd.target.toLowerCase() === this.botUsername.toLowerCase())
    if (!cmd || cmd.name !== 'clear') { this.ledger.settleInbound([row.id], 'dropped', 'command ignored'); return }
    if (!targetOk) { this.ledger.settleInbound([row.id], 'dropped', 'command addressed to another bot'); this.log.info('command.wrong_target', { chat: row.chat_id }); return }
    if (!this.isOwner(row.sender_id)) { this.ledger.settleInbound([row.id], 'dropped', 'not owner'); this.log.warn('command.not_owner', { chat: row.chat_id }); return }
    const seg = this.ledger.activeSegment(row.chat_id)
    if (seg) { this.ledger.closeSegment(seg.id, 'closed', 'clear'); this.loaded.delete(seg.session_id ?? '') }
    this.ledger.setMeta(`clear_at:${row.chat_id}`, String(Date.now()))
    this.ledger.event(row.chat_id, 'clear', { segment: seg?.id ?? null })
    this.ledger.settleInbound([row.id], 'done', 'clear')
    this.log.info('command.clear', { chat: row.chat_id, segment: seg?.id ?? null })
    await this.sender.send({ chatId: row.chat_id, turnId: null, callSeq: 0, text: '【系统】已清空这段对话的上下文，下一条消息从头开始。', kind: 'system' }).catch(() => {})
  }

  // ─── 通知主人 ───

  async notifyOwner(key: string, text: string): Promise<void> {
    const last = this.noticeAt.get(key) ?? 0
    if (Date.now() - last < 10 * 60_000) return
    this.noticeAt.set(key, Date.now())
    this.ledger.event(null, 'owner_notice', { key })
    for (const owner of this.cfg.gw.owners) {
      await this.sender.send({ chatId: owner, turnId: null, callSeq: 0, text: `【系统】${text}`, kind: 'system' }).catch(e => this.log.warn('notify_owner_failed', { err: safeError(e) }))
    }
  }

  // ─── 配置热更新：换模型不重启；改人设或路由要等全部空闲再重启 dsh ───

  private watchConfig(): void {
    const cm = mtime(this.cfg.configPath)
    const pm = mtime(this.personaPath())
    if (cm === this.configMtime && pm === this.personaMtime) return
    this.configMtime = cm
    this.personaMtime = pm
    const nb = reloadBrain(this.cfg.configPath)
    if (!nb) { this.log.warn('config.reload_failed', { path: 'configs/<bot>.yml' }); return }
    const before = brainKey(this.brain)
    this.brain = nb
    if (brainKey(nb) !== before) this.log.info('config.brain_changed', { provider: nb.provider, model: nb.model, effort: nb.reasoningEffort ?? null })
    const fp = restartFingerprint(this.patchInput())
    if (fp !== this.fingerprint && this.dsh.running) {
      this.log.info('config.restart_needed', { reason: 'persona or routes changed' })
      this.restartPending = true
      if (this.runningCount === 0) void this.restartIdle()
    }
  }

  private async restartIdle(): Promise<void> {
    if (!this.restartPending || this.runningCount > 0) return
    this.restartPending = false
    this.log.info('dsh.restart_idle')
    await this.dsh.stop()
    this.loaded.clear()
    for (const c of this.ledger.chatsWithPending()) this.schedule(c)
  }

  // ─── 健康 ───

  private async probe(): Promise<void> {
    const conn = this.dsh.conn
    if (!conn || !this.dsh.running) return
    try {
      await conn.request('session/list', {}, 5_000)
      this.lastProbeOkAt = Date.now()
      this.probeFailures = 0
    } catch (e) {
      if (!(e instanceof AcpTimeout)) { this.lastProbeOkAt = Date.now(); return } // 回了错误也说明它还活着
      this.probeFailures++
      this.log.warn('dsh.probe_timeout', { failures: this.probeFailures })
      if (this.probeFailures >= 2 && this.runningCount === 0) {
        this.log.error('dsh.unresponsive_restart')
        await this.dsh.stop(2_000)
        this.loaded.clear()
      }
    }
  }

  health(extra: { pollLastOkAt: number; pollConflict: boolean; pollError: string | null }) {
    const now = Date.now()
    const g = this.cfg.gw
    const problems: string[] = []
    const pollAge = now - extra.pollLastOkAt
    if (pollAge > Math.max(90_000, g.pollTimeoutS * 3_000)) problems.push(`Telegram 已经 ${Math.round(pollAge / 1000)} 秒没有拉取成功`)
    if (extra.pollConflict) problems.push('同一个令牌有别的程序在收消息（旧系统的 bot 停了吗？）')
    const chats = this.ledger.oldestUnprocessed().map(r => {
      const st = this.chats.get(r.chat_id)
      const at = st?.active
      const age = now - r.received_at
      const item: Record<string, unknown> = { chat_id: r.chat_id, oldest_unprocessed_ms: age }
      if (at) {
        item.turn = { id: at.turnId, running_ms: now - at.startedAt, since_progress_ms: now - at.lastProgressAt }
        if (at.toolsInFlight === 0 && now - at.lastProgressAt > g.stallWarnMs) problems.push(`聊天 ${r.chat_id} 这一轮已经 ${Math.round((now - at.lastProgressAt) / 1000)} 秒没有进展`)
      }
      const limit = Math.max(g.stallCancelMs * 2, 10 * 60_000)
      if (!at && age > limit) problems.push(`聊天 ${r.chat_id} 有消息等了 ${Math.round(age / 1000)} 秒还没处理`)
      return item
    })
    if (!this.dsh.running && this.dshFailures > 0) problems.push(`模型进程没在运行（连续启动失败 ${this.dshFailures} 次）`)
    return {
      ok: problems.length === 0,
      problems,
      bot: this.cfg.id,
      telegram: { last_ok_ms: pollAge, conflict: extra.pollConflict, last_error: extra.pollError },
      dsh: { running: this.dsh.running, pid: this.dsh.pid, generation: this.dsh.generation, version: this.dsh.agentVersion, last_probe_ok_ms: this.lastProbeOkAt ? now - this.lastProbeOkAt : null },
      brain: { provider: this.brain.provider, model: this.brain.model, effort: this.brain.reasoningEffort ?? null },
      turns_running: this.runningCount,
      chats,
    }
  }

  healthSource?: () => { pollLastOkAt: number; pollConflict: boolean; pollError: string | null }

  private watchHealth(): void {
    if (!this.healthSource) return
    const h = this.health(this.healthSource())
    if (!h.ok) {
      if (this.unhealthySince === null) { this.unhealthySince = Date.now(); this.log.warn('health.unhealthy', { problems: h.problems }) }
    } else if (this.unhealthySince !== null) {
      const mins = Math.round((Date.now() - this.unhealthySince) / 60_000)
      this.unhealthySince = null
      this.log.info('health.recovered', { minutes: mins })
      if (mins >= 2) void this.notifyOwner('recovered', `刚才卡了约 ${mins} 分钟，已经恢复，积压的消息会陆续补上。`)
    }
  }

  access(): Access { return loadAccess(this.cfg.channelDir) }
}

function mtime(p: string): number {
  try { return statSync(p).mtimeMs } catch { return 0 }
}
