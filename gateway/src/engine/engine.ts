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
import { buildPatchRows, modelValue, patchText, promptFingerprint, restartFingerprint, type PatchInput } from '../dsh/profile'
import type { InboundRow, Ledger, SegmentRow, TurnKind } from '../ledger'
import { safeError, type Logger } from '../log'
import type { McpServer, ToolDef } from '../mcp/server'
import type { TelegramApi } from '../telegram/api'
import { parseCommand } from '../telegram/inbound'
import { describeResults, normText, planParts, type Sender } from '../telegram/sender'
import { Backoff, randomToken, sleep } from '../util'
import { MemoryStore } from '../memory'
import { Commitments } from '../commit/service'
import { situLine, SituationBridge } from '../life/situation'
import { createHangRuntime, takeHangArchive, type HangRuntime } from '../life/hang_runtime'
import {
  cleanSummary, formatLedgerSummaryPrompt, formatMemoryHint, formatMessages, formatSeedParts,
  NEW_SEGMENT_HINT, NUDGE_NO_ACTION, NUDGE_RETRY, summaryPrompt,
} from './format'
import { formatModelList, formatProviders, HELP_TEXT, parseModelChoices, providersOf, resolveModel, type KeyStatus, type ModelChoice } from './commands'

/** 旧调度器的被晾提示以 [hang-check] 开头；新系统统一用 ⟦系统…⟧ 标记程序说明 */
function hangText(t: string): string {
  return t.replace(/^\[hang-check\]\s*/, '⟦系统·被晾⟧ ')
}

/** 主人能用的命令（/start 等其它命令照样被网关吞掉，不交给模型） */
const OWNER_COMMANDS = new Set(['clear', 'compact', 'model', 'models', 'provider', 'providers', 'help', 'commands'])

/** 预算小的时候，新段开头的原话最多占预算的这个比例（按一个字一个 token 算，宁可少带） */
const SEED_RECENT_RATIO = 0.1

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
  /** summary：换段时写摘要的一轮，所有工具都锁住 */
  mode: 'chat' | 'summary'
  /** 模型直接输出的文字（写摘要那一轮要用） */
  textOut: string
}

type ChatState = { running: boolean; timer: ReturnType<typeof setTimeout> | null; active: ActiveTurn | null }

type Outcome = (
  | { type: 'ok'; stopReason: string }
  | { type: 'cancelled' }
  | { type: 'error'; err: string; fatal: boolean }
  | { type: 'crashed'; err: string }
) & { used?: number | null; text?: string }

const LOCKED = { text: '现在是整理记忆的时间，不能调用工具，也不会发给对方。请直接输出摘要正文。', isError: true }

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
  /** 启动时清理旧 dsh 的任务；拉起新 dsh 之前必须等它做完，免得把新进程当成旧的清掉 */
  private reaping: Promise<void> = Promise.resolve()
  private dshBackoff = new Backoff(2_000, 60_000)
  private dshNextTryAt = 0
  private dshFailures = 0
  private fingerprint = ''
  /** 配置文件里的模型；this.brain 是实际在用的（可能被 /model 换过） */
  private configBrain: Brain
  /** dsh 回报的可选模型（最近一次看到的） */
  private modelChoices: ModelChoice[] = []
  /** 正在跑的 dsh 用的系统提示词的指纹（dsh 启动时算） */
  private promptKey = ''
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
  readonly memory: MemoryStore
  readonly situation: SituationBridge
  readonly commitments: Commitments
  readonly hang: HangRuntime

  constructor(
    readonly cfg: BotConfig,
    readonly ledger: Ledger,
    readonly log: Logger,
    readonly api: TelegramApi,
    readonly sender: Sender,
    readonly dsh: DshProcess,
    readonly mcp: McpServer,
  ) {
    this.configBrain = cfg.brain
    this.brain = this.withOverride(cfg.brain)
    this.memory = new MemoryStore(cfg.memoryDir)
    this.situation = new SituationBridge(cfg.id, cfg.configPath, log, cfg.gw.situationCmd)
    this.commitments = new Commitments({
      ledger, log, situation: this.situation,
      timeZone: () => this.cfg.gw.timezone,
      inject: (chatId, text, key, meta) => this.injectSynthetic(chatId, text, key, meta),
      isBusy: chatId => this.chat(chatId).running,
      notifyOwner: (key, text) => void this.notifyOwner(key, text),
      retryMs: cfg.gw.commitRetryMs,
    })
    // 被晾追问（从旧调度器原样搬来）：状态文件放在 state 目录；追问写成账本里的合成消息
    this.hang = createHangRuntime({
      channelDir: cfg.stateDir,
      inject: (chatId, text) => { this.injectSynthetic(chatId, hangText(text), `hang:${chatId}:${Date.now()}`, { source: 'hang' }) },
      probe: () => this.situation.current(),
      log: line => this.log.info('hang', { detail: line }),
    })
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
        name: 'remember',
        description: '把值得长期记住的事记下来（对方的喜好、重要的日子、你们的约定、你们之间发生的事）。一次一条，写清楚是什么。不要记密码、证件号这类敏感信息。',
        inputSchema: { type: 'object', properties: { text: { type: 'string', description: '要记住的一件事，一两句话' } }, required: ['text'] },
        call: (args, ctx) => engine().toolRemember(args, ctx),
      },
      {
        name: 'commitment_create',
        description: '登记一件答应对方将来要做的事（比如"三点提醒你""明早叫你起床""周五陪你看电影"）。到点时程序会提醒你去做。',
        inputSchema: {
          type: 'object',
          properties: {
            content: { type: 'string', description: '要做的事，一句话' },
            when: { type: 'string', description: '什么时候：可以写"今晚八点""明天早上""30分钟后""起床后""下班后""周五晚上"，或 2026-10-07T09:00' },
            quote: { type: 'string', description: '你当时对对方说的原话（可选）' },
          },
          required: ['content', 'when'],
        },
        call: (args, ctx) => engine().toolCommit('create', args, ctx),
      },
      {
        name: 'commitment_list',
        description: '查看这个聊天里还没兑现的承诺。',
        inputSchema: { type: 'object', properties: {} },
        call: (args, ctx) => engine().toolCommit('list', args, ctx),
      },
      {
        name: 'commitment_cancel',
        description: '取消一件承诺（不打算做了、对方说不用了、登记错了）。',
        inputSchema: { type: 'object', properties: { id: { type: 'number', description: '承诺编号' }, reason: { type: 'string' } }, required: ['id'] },
        call: (args, ctx) => engine().toolCommit('cancel', args, ctx),
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
    if (at.mode === 'summary') return LOCKED
    const text = typeof args.text === 'string' ? args.text : ''
    const files = Array.isArray(args.files) ? args.files.filter((f): f is string => typeof f === 'string') : []
    if (!text.trim() && files.length === 0) return { text: 'text 不能为空。', isError: true }
    const replyTo = Number(args.reply_to)
    // 模型偶尔会在同一轮里把刚说过的话再发一遍（真机验收里见过）。这里拦住：
    // 大半段落和这一轮已发出的相同，就整次不发；只有个别段相同，就只跳过那几段。
    const already = new Set(this.ledger.sentTextsInChain(at.rootId).map(normText))
    if (already.size > 0 && files.length === 0) {
      const texts = planParts(text, [], this.access()).flatMap(i => (i.kind === 'text' ? [normText(i.text)] : []))
      const dup = texts.filter(t => already.has(t)).length
      if (texts.length > 0 && dup * 2 >= texts.length) {
        this.log.warn('tool.reply_duplicate_blocked', { turn: at.turnId, parts: texts.length, duplicates: dup })
        return { text: '这些话这一轮已经发给对方了，对方都看到了，这次没有重复发送。这一轮可以结束了。' }
      }
    }
    at.toolsInFlight++
    at.replyCalls++
    at.lastProgressAt = Date.now()
    try {
      const results = await this.sender.send({ chatId: at.chatId, turnId: at.turnId, callSeq: at.replyCalls, text, files, replyTo: Number.isInteger(replyTo) && replyTo > 0 ? replyTo : undefined, skipTexts: already })
      const d = describeResults(results)
      at.delivered += d.delivered
      if (d.delivered > 0 && at.silent) { this.ledger.clearSilent(at.rootId); at.silent = false }
      this.log.info('tool.reply', { turn: at.turnId, parts: results.length, delivered: d.delivered, duplicates: results.filter(r => r.state === 'duplicate').length })
      return { text: d.text, isError: d.isError }
    } finally {
      at.toolsInFlight--
      at.lastProgressAt = Date.now()
    }
  }

  async toolReact(args: Record<string, unknown>, ctx: { chatId: string; segmentId: number }) {
    const at = this.activeFor(ctx.chatId, ctx.segmentId)
    if (!at) return { text: '这一轮已经结束。', isError: true }
    if (at.mode === 'summary') return LOCKED
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

  async toolRemember(args: Record<string, unknown>, ctx: { chatId: string; segmentId: number }) {
    const at = this.activeFor(ctx.chatId, ctx.segmentId)
    if (!at) return { text: '这一轮已经结束。', isError: true }
    if (at.mode === 'summary') return LOCKED
    const text = (typeof args.text === 'string' ? args.text : '').replace(/\s+/g, ' ').trim()
    if (!text) return { text: 'text 不能为空。', isError: true }
    if (text.length > 300) return { text: '太长了，请压缩成一两句话再记。', isError: true }
    const date = new Intl.DateTimeFormat('sv-SE', { timeZone: this.cfg.gw.timezone, year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date())
    try {
      const r = await this.memory.append(text, date)
      if (r.added) this.ledger.addMemory(at.chatId, text)
      this.log.info('tool.remember', { turn: at.turnId, chars: text.length, added: r.added })
      return { text: r.added ? '记下了。' : '这件事之前已经记过了。' }
    } catch (e) {
      this.log.error('tool.remember_failed', { err: safeError(e) })
      return { text: '这次没记上（写文件出错）。', isError: true }
    }
  }

  async toolCommit(op: 'create' | 'list' | 'cancel', args: Record<string, unknown>, ctx: { chatId: string; segmentId: number }) {
    const at = this.activeFor(ctx.chatId, ctx.segmentId)
    if (!at) return { text: '这一轮已经结束。', isError: true }
    if (at.mode === 'summary') return LOCKED
    if (op === 'list') return this.commitments.list(at.chatId)
    return op === 'create' ? this.commitments.create(at.chatId, args) : this.commitments.cancel(at.chatId, args)
  }

  async toolSilent(args: Record<string, unknown>, ctx: { chatId: string; segmentId: number }) {
    const at = this.activeFor(ctx.chatId, ctx.segmentId)
    if (!at) return { text: '这一轮已经结束。', isError: true }
    if (at.mode === 'summary') return LOCKED
    if (this.ledger.deliveredInChain(at.rootId) > 0) return { text: '这一轮你已经回复过对方了，不需要再调用 stay_silent，直接结束这一轮就好。' }
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
    // 上次网关被强杀时，正在处理一轮的 dsh 不会马上退出（Windows 上子进程也不会跟着父进程结束）。启动时就清理，不等下一条消息。
    this.reaping = this.dsh.reapStale(this.dshSpec()).catch(e => this.log.warn('dsh.reap_failed', { err: safeError(e) }))
    this.configMtime = mtime(this.cfg.configPath)
    this.personaMtime = mtime(this.personaPath())
    const g = this.cfg.gw
    this.timers.push(setInterval(() => this.watchConfig(), g.configPollMs))
    this.timers.push(setInterval(() => void this.probe(), g.probeMs))
    this.timers.push(setInterval(() => this.watchHealth(), Math.min(10_000, g.heartbeatMs)))
    this.timers.push(setInterval(() => void this.situation.get().then(() => this.commitments.tick()).catch(e => this.log.error('commitment.tick_failed', { err: safeError(e) })), g.commitPollMs))
    this.timers.push(setInterval(() => void this.situation.get().then(() => this.hang.tick()).catch(e => this.log.error('hang.tick_failed', { err: safeError(e) })), g.hangTickMs))
    void this.situation.get()
    for (const chatId of this.ledger.chatsWithPending()) this.schedule(chatId)
  }

  private pendingHints(chatId: string): string[] {
    try { const v = JSON.parse(this.ledger.getMeta(`hints:${chatId}`) || '[]'); return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : [] } catch { return [] }
  }

  /**
   * 对方发来消息的这一轮，附带几行"生活状态"（旧系统每条私聊都带，这里只在变化时或新会话开头带，省 token）：
   * 关系数值的提示（relationship.json 的 prompt_snippet）、她此刻在干什么、被晾之后迟到的反应（带完即清）。
   */
  private lifeLines(chatId: string, segmentId: number, fresh: boolean): string[] {
    const out: string[] = []
    try {
      const snippet = JSON.parse(readFileSync(join(this.cfg.channelDir, 'relationship.json'), 'utf8'))?.prompt_snippet
      if (typeof snippet === 'string' && snippet.trim()) {
        const k = `rel_seen:${segmentId}`
        if (fresh || this.ledger.getMeta(k) !== snippet) { out.push(`⟦关系状态⟧ ${snippet.trim()}`); this.ledger.setMeta(k, snippet) }
      }
    } catch {}
    const situ = situLine(this.situation.current())
    if (situ && (fresh || this.ledger.getMeta(`situ_seen:${chatId}`) !== situ)) { out.push(situ); this.ledger.setMeta(`situ_seen:${chatId}`, situ) }
    const late = takeHangArchive(this.cfg.stateDir, chatId)
    if (late) out.push(hangText(late))
    if (out.length) this.log.info('life.lines', { chat: chatId, relationship: out.some(l => l.startsWith('⟦关系状态')), situation: !!situ && out.includes(situ), late: !!late })
    return out
  }

  /** 程序自己往某个聊天里塞一条要模型处理的消息（承诺到期、被晾追问）。重复的 key 不会再塞。返回账本 id。 */
  injectSynthetic(chatId: string, text: string, key: string, meta: Record<string, unknown>): number | null {
    const r = this.ledger.insertInbound({ ukey: `int:${key}`, chatId, kind: 'synthetic', text, ts: Date.now(), meta })
    if (!r.inserted) return null
    this.schedule(chatId)
    return r.id
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

  private harnessVersion(): string {
    try {
      return String(JSON.parse(readFileSync(join(this.cfg.harnessDir, 'node_modules', '@deepseek-ai', 'dsh', 'package.json'), 'utf8')).version ?? '')
    } catch { return '' }
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
        await this.reaping
        if (!existsSync(this.personaPath())) throw new Error(`persona file missing: ${this.personaPath()}`)
        const input = this.patchInput()
        await this.dsh.start(this.dshSpec())
        this.fingerprint = restartFingerprint(input)
        this.promptKey = promptFingerprint(input, this.harnessVersion())
        this.modelChoices = []
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

  onInbound(chatId: string): void {
    const last = this.ledger.pendingFor(chatId).at(-1)
    if (last?.kind === 'user') this.hang.onInbound(chatId, last.received_at) // 对方回话了：取消还没发的追问
    this.schedule(chatId)
  }

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
    // 新消息一进来就会超预算：先换段再处理
    await this.maybeRoll(chatId, batch)
    const since = Date.now()
    const delay = await this.runBatch(chatId, batch)
    // 这一轮说了"三点提醒你"却没登记：能换算时间的替它登记，下一轮告诉它；换算不了的下一轮提醒它自己登记
    const hints = this.commitments.afterTurn(chatId, since)
    if (hints.length) this.ledger.setMeta(`hints:${chatId}`, JSON.stringify([...this.pendingHints(chatId), ...hints]))
    if (this.ledger.sentTextsSince(chatId, since).length > 0) this.hang.onOutbound(chatId)
    // 这一轮之后用量到线了：趁这个聊天还占着，马上换段（下一条消息就不用等摘要了）
    if (delay === 0) await this.maybeRoll(chatId, [])
    return delay
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
    // 新会话的第一轮：前情（如果有）后面加一句提醒，和新消息分成两个内容块（real-03 与真机验收里新会话第一轮偶尔不用 reply）
    const head = this.ledger.segmentTurnCount(seg.id) === 0 ? [seed ? `${seed}\n${NEW_SEGMENT_HINT}` : NEW_SEGMENT_HINT] : seed ? [seed] : []
    // 别的聊天里新记下的长期记忆：在这一轮前面提一句
    const news = this.ledger.memoriesFromOtherChats(chatId, seg.memory_seen)
    if (news.length) this.ledger.setMemorySeen(seg.id, news[news.length - 1]!.id)
    const hint = news.length ? [formatMemoryHint(news.map(n => n.text))] : []
    const commitHints = this.pendingHints(chatId)
    if (commitHints.length) this.ledger.setMeta(`hints:${chatId}`, '')
    const life = batch.some(r => r.kind === 'user') ? this.lifeLines(chatId, seg.id, this.ledger.segmentTurnCount(seg.id) === 0) : []
    let blocks = [...head, ...hint, ...(commitHints.length || life.length ? [[...commitHints, ...life].join('\n')] : []), formatMessages(batch, prevTs, { timeZone: this.cfg.gw.timezone })]
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
  private async runPrompt(turnId: number, rootId: number, seg: SegmentRow, blocks: string[], mode: ActiveTurn['mode'] = 'chat'): Promise<Outcome> {
    const conn = this.dsh.conn
    const sessionId = seg.session_id!
    if (!conn) return { type: 'crashed', err: 'dsh not running' }
    const st = this.chat(seg.chat_id)
    const at: ActiveTurn = {
      turnId, rootId, chatId: seg.chat_id, segmentId: seg.id, sessionId,
      startedAt: Date.now(), lastProgressAt: Date.now(), toolsInFlight: 0, replyCalls: 0, delivered: 0, silent: false, cancelledAt: null,
      mode, textOut: '',
    }
    st.active = at
    let used: number | null = null
    let size: number | null = null
    conn.onSession(sessionId, (u: SessionUpdate) => {
      at.lastProgressAt = Date.now()
      if (u.update.sessionUpdate === 'agent_message_chunk' && u.update.content?.type === 'text') at.textOut += String(u.update.content.text ?? '')
      if (u.update.sessionUpdate === 'usage_update') {
        if (typeof u.update.used === 'number') used = u.update.used
        if (typeof u.update.size === 'number') size = u.update.size
      }
    })
    const g = this.cfg.gw
    // 写摘要的那一轮不显示"正在输入"
    const typing = mode === 'chat' ? setInterval(() => { void this.api.sendChatAction(seg.chat_id).catch(() => {}) }, 4_500) : undefined
    if (mode === 'chat') void this.api.sendChatAction(seg.chat_id).catch(() => {})
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
      return { type: 'ok', stopReason: String(res?.stopReason ?? ''), used, text: at.textOut }
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
        const r = await conn.request<{ configOptions?: any[] }>('session/resume', { sessionId: seg.session_id, cwd: this.cfg.workDir, mcpServers: this.mcp.serverSpec(seg.id, seg.mcp_token) }, 60_000)
        this.noteOptions(r?.configOptions)
        this.loaded.set(seg.session_id, gen)
        this.log.info('segment.resumed', { chat: chatId, segment: seg.id })
      } catch (e) {
        if (e instanceof AcpExited) throw e
        this.log.warn('segment.resume_failed', { chat: chatId, segment: seg.id, err: safeError(e) })
        this.ledger.closeSegment(seg.id, 'abandoned', 'resume failed')
        seg = null
      }
    }
    // 人设、运行规则或 dsh 版本变了：旧会话不再接着用，写一份交接摘要后换新会话。
    // 接着用的话，dsh 会把新的系统提示词整份追加进历史，新旧两份都在，以后每轮都要多付一份的钱。
    if (seg?.session_id && seg.prompt_key !== this.promptKey) {
      this.log.info('segment.prompt_changed', { chat: chatId, segment: seg.id })
      if (this.ledger.segmentTurnsOfKind(seg.id, 'message') === 0) {
        this.ledger.closeSegment(seg.id, 'closed', 'prompt-changed')
        this.loaded.delete(seg.session_id)
        void conn.request('session/close', { sessionId: seg.session_id }, 15_000).catch(() => {})
      } else {
        try {
          await this.rollSegment(chatId, seg, 'prompt-changed')
        } catch (e) {
          if (e instanceof AcpExited) throw e
          this.log.error('segment.roll_failed', { chat: chatId, segment: seg.id, err: safeError(e) })
          this.ledger.closeSegment(seg.id, 'closed', 'prompt-changed (summary failed)')
          this.loaded.delete(seg.session_id)
        }
      }
      seg = this.ledger.activeSegment(chatId)
    }
    if (!seg || !seg.session_id) {
      // 上一段是崩溃作废的、还没有摘要：先用账本流水补写一份（旧会话已经用不了）
      if (!seg) await this.summarizeAbandoned(chatId)
      if (!seg) seg = this.ledger.createSegment(chatId, randomToken(), true)
      const s = await conn.request<{ sessionId: string; configOptions?: any[] }>('session/new', { cwd: this.cfg.workDir, mcpServers: this.mcp.serverSpec(seg.id, seg.mcp_token) }, 60_000)
      this.ledger.setSegmentSession(seg.id, s.sessionId, '', this.promptKey)
      this.noteOptions(s.configOptions)
      this.loaded.set(s.sessionId, gen)
      this.log.info('segment.created', { chat: chatId, segment: seg.id, needs_seed: seg.needs_seed === 1 })
      seg = this.ledger.segment(seg.id)!
    }
    if (seg.model !== brainKey(this.brain)) await this.applyBrain(seg)
    let seed: string | null = null
    if (seg.needs_seed) {
      const clearAt = Number(this.ledger.getMeta(`clear_at:${chatId}`) ?? 0)
      const recentCap = this.seedRecentChars(chatId)
      const entries = this.ledger.recentTranscript(chatId, { maxChars: recentCap, sinceMs: clearAt, excludeInboundIds: batchIds })
      const intr = this.ledger.interruptedReply(chatId)
      const mem = this.memory.readForSeed(this.cfg.gw.memoryMaxChars)
      const summary = this.ledger.latestSummary(chatId)
      seed = formatSeedParts({ memory: mem.text, summary: summary?.summary ?? null, entries, interrupted: intr }, { timeZone: this.cfg.gw.timezone })
      this.log.info('segment.seed', {
        chat: chatId, segment: seg.id, entries: entries.length, recent_cap: recentCap, interrupted: intr !== null,
        memory_chars: mem.text.length, memory_truncated: mem.truncated, summary_from_segment: summary?.segmentId ?? null, chars: seed?.length ?? 0,
      })
    }
    return { seg: this.ledger.segment(seg.id)!, seed }
  }

  /**
   * 新段开头带多少字原话：配置的上限；预算小的时候再收紧到预算的一成。
   * 不收紧的话，预算 3 万时一整段的原话都会原样带进新会话，摘要等于白写，新会话一开头就占掉一大半预算（M2 真机报告问题 4）。
   */
  private seedRecentChars(chatId: string): number {
    const prev = this.ledger.lastClosedSegment(chatId)
    const budget = prev ? this.budgetFor(prev) : this.brain.maxInputTokens ?? null
    const n = this.cfg.gw.seedRecentChars
    return budget ? Math.min(n, Math.floor(budget * SEED_RECENT_RATIO)) : n
  }

  /** 给一个会话设模型和思考强度。返回是否成功。 */
  private async setSessionBrain(sessionId: string): Promise<void> {
    const conn = this.dsh.conn
    if (!conn) throw new AcpExited('session/set_config_option')
    const b = this.brain
    const r = await conn.request<{ configOptions?: any[] }>('session/set_config_option', { sessionId, configId: 'model', value: modelValue(b) }, 30_000)
    this.noteOptions(r?.configOptions)
    if (b.reasoningEffort) {
      const opt = (r?.configOptions ?? []).find((o: any) => o?.id === 'reasoning_effort')
      if (opt) await conn.request('session/set_config_option', { sessionId, configId: 'reasoning_effort', value: b.reasoningEffort }, 30_000)
      else this.log.info('brain.effort_unsupported', { model: b.model })
    }
  }

  /** 按配置给段设模型和思考强度。失败不挡聊天，记一条错误并通知主人。 */
  private async applyBrain(seg: SegmentRow): Promise<void> {
    if (!seg.session_id) return
    const b = this.brain
    try {
      await this.setSessionBrain(seg.session_id)
      this.ledger.setSegmentModel(seg.id, brainKey(b))
      this.log.info('brain.applied', { segment: seg.id, provider: b.provider, model: b.model, effort: b.reasoningEffort ?? null })
    } catch (e) {
      if (e instanceof AcpExited) throw e
      this.log.error('brain.apply_failed', { segment: seg.id, err: safeError(e) })
      void this.notifyOwner('brain', `换模型没成功（${b.provider} / ${b.model}）：${safeError(e).slice(0, 120)}。先继续用原来的模型。`)
      this.ledger.setSegmentModel(seg.id, brainKey(b)) // 不在每轮反复重试
    }
  }

  // ─── 换段 ───

  /** 这一段的预算：配置了硬上限就用它（不超过窗口的比例），否则用 dsh 回报的窗口 × 比例。都不知道就不换段。 */
  budgetFor(seg: SegmentRow): number | null {
    const byWindow = seg.window ? Math.floor(seg.window * this.cfg.gw.rollRatio) : null
    const cap = this.brain.maxInputTokens ?? null
    if (cap !== null && byWindow !== null) return Math.min(cap, byWindow)
    return cap ?? byWindow
  }

  /** 用量到线（或者新消息一进来就会超）就换段。incoming 是马上要送进去的新消息。 */
  private async maybeRoll(chatId: string, incoming: InboundRow[]): Promise<void> {
    const seg = this.ledger.activeSegment(chatId)
    if (!seg?.session_id || !seg.used_tokens) return
    const budget = this.budgetFor(seg)
    if (budget === null) return
    // 一段里至少聊过两轮才换：否则预算比"新段开头"本身还小时，会每一轮都换段、每一轮都写摘要
    if (this.ledger.segmentTurnsOfKind(seg.id, 'message') < 2) {
      if (seg.used_tokens >= budget) this.log.warn('segment.budget_too_small', { chat: chatId, segment: seg.id, used: seg.used_tokens, budget })
      return
    }
    // 估新消息的大小：按字数算（中文一个字大约一个 token 以内，英文更少），宁可估多
    const est = incoming.reduce((n, r) => n + r.text.length + 40, 0)
    if (seg.used_tokens + est < budget) return
    try {
      await this.rollSegment(chatId, seg, incoming.length ? 'pre-budget' : 'budget')
    } catch (e) {
      // 写摘要出了意外也要换段，不然会一直超预算
      this.log.error('segment.roll_failed', { chat: chatId, segment: seg.id, err: safeError(e) })
      this.ledger.closeSegment(seg.id, 'closed', 'budget (summary failed)')
      if (seg.session_id) this.loaded.delete(seg.session_id)
    }
  }

  /**
   * 换段：先在旧会话里写交接摘要（这一轮工具全锁），写不成就用账本流水单独写一份；
   * 摘要不像样或为空就不存（新段开头沿用上一份）。然后关掉旧会话，下一轮开新会话。
   */
  async rollSegment(chatId: string, seg: SegmentRow, reason: string, focus?: string): Promise<{ how: 'in-session' | 'ledger' | 'none' | 'crashed'; chars: number }> {
    this.log.info('segment.roll_start', { chat: chatId, segment: seg.id, reason, used: seg.used_tokens, budget: this.budgetFor(seg) })
    let summary: string | null = null
    let how: 'in-session' | 'ledger' | 'none' = 'none'
    // 网关重启后旧会话还没载入：先续接上，摘要照样在旧会话里写（命中缓存）
    if (seg.session_id && this.dsh.running && this.loaded.get(seg.session_id) !== this.dsh.generation) await this.resumeForSummary(seg)
    if (seg.session_id && this.dsh.running && this.loaded.get(seg.session_id) === this.dsh.generation) {
      const r = await this.summarizeInSession(chatId, seg, focus)
      if (r === 'crashed') return { how: 'crashed', chars: 0 } // 段已作废，下一轮开新段时用账本补写摘要
      if (r) { summary = r; how = 'in-session' }
    }
    if (!summary) {
      summary = await this.summarizeFromLedger(chatId, seg, focus).catch(e => { this.log.warn('summary.ledger_failed', { err: safeError(e) }); return null })
      if (summary) how = 'ledger'
    }
    if (summary) this.ledger.setSegmentSummary(seg.id, summary)
    this.ledger.closeSegment(seg.id, 'closed', reason)
    if (seg.session_id) {
      this.loaded.delete(seg.session_id)
      void this.dsh.conn?.request('session/close', { sessionId: seg.session_id }, 15_000).catch(() => {})
    }
    this.ledger.event(chatId, 'segment_rolled', { segment: seg.id, reason, summary: how, summary_chars: summary?.length ?? 0, used: seg.used_tokens })
    this.log.info('segment.rolled', { chat: chatId, segment: seg.id, reason, summary: how, summary_chars: summary?.length ?? 0 })
    return { how, chars: summary?.length ?? 0 }
  }

  private async resumeForSummary(seg: SegmentRow): Promise<void> {
    const conn = this.dsh.conn
    if (!conn || !seg.session_id) return
    try {
      await conn.request('session/resume', { sessionId: seg.session_id, cwd: this.cfg.workDir, mcpServers: this.mcp.serverSpec(seg.id, seg.mcp_token) }, 60_000)
      this.loaded.set(seg.session_id, this.dsh.generation)
    } catch (e) {
      this.log.warn('summary.resume_failed', { segment: seg.id, err: safeError(e) })
    }
  }

  /** 在旧会话里写摘要。返回摘要；不像样返回 null；dsh 中途死了返回 'crashed'。 */
  private async summarizeInSession(chatId: string, seg: SegmentRow, focus?: string): Promise<string | null | 'crashed'> {
    const turn = this.ledger.startTurn({ chatId, segmentId: seg.id, kind: 'summary', inboundIds: [], attempt: 0 })
    const outcome = await this.runPrompt(turn.id, turn.id, seg, [summaryPrompt(focus)], 'summary')
    if (outcome.type === 'crashed') {
      this.ledger.finishTurn(turn.id, 'crashed', { error: outcome.err })
      this.ledger.closeSegment(seg.id, 'abandoned', 'crash during summary')
      if (seg.session_id) this.loaded.delete(seg.session_id)
      return 'crashed'
    }
    if (outcome.type !== 'ok') {
      this.ledger.finishTurn(turn.id, outcome.type === 'cancelled' ? 'cancelled' : 'error', { error: outcome.type === 'error' ? outcome.err : undefined, usedTokens: outcome.used })
      this.log.warn('summary.in_session_failed', { segment: seg.id, outcome: outcome.type })
      return null
    }
    this.ledger.finishTurn(turn.id, 'ok', { stopReason: outcome.stopReason, usedTokens: outcome.used })
    const s = cleanSummary(outcome.text)
    if (!s) this.log.warn('summary.rejected', { segment: seg.id, chars: outcome.text?.length ?? 0 })
    return s
  }

  /** 旧会话用不了：把这一段的账本流水（加上更早的摘要）交给一个不带工具的临时会话写摘要。 */
  private async summarizeFromLedger(chatId: string, seg: SegmentRow, focus?: string): Promise<string | null> {
    let entries = this.ledger.segmentTranscript(seg.id)
    if (entries.length === 0) return null
    // 太长就只取后面的部分
    let total = entries.reduce((n, e) => n + e.text.length, 0)
    while (total > this.cfg.gw.summarySourceMaxChars && entries.length > 1) { total -= entries[0]!.text.length; entries = entries.slice(1) }
    const prev = this.ledger.latestSummary(chatId)
    const prompt = formatLedgerSummaryPrompt(prev?.summary ?? null, entries, { timeZone: this.cfg.gw.timezone }, focus)
    const text = await this.oneShot(prompt)
    const s = cleanSummary(text)
    this.ledger.event(chatId, 'summary_from_ledger', { segment: seg.id, entries: entries.length, ok: s !== null })
    if (!s) this.log.warn('summary.rejected', { segment: seg.id, chars: text.length, from: 'ledger' })
    return s
  }

  /** 新开段之前：上一段是作废的（崩溃、续接失败）且没有摘要，就用账本补写一份。每段只试一次。 */
  private async summarizeAbandoned(chatId: string): Promise<void> {
    const prev = this.ledger.lastClosedSegment(chatId)
    if (!prev || prev.state !== 'abandoned' || prev.summary) return
    const key = `ledger_summary_tried:${prev.id}`
    if (this.ledger.getMeta(key)) return
    this.ledger.setMeta(key, '1')
    try {
      const s = await this.summarizeFromLedger(chatId, prev)
      if (s) this.ledger.setSegmentSummary(prev.id, s)
    } catch (e) {
      if (e instanceof AcpExited) throw e
      this.log.warn('summary.ledger_failed', { segment: prev.id, err: safeError(e) })
    }
  }

  /** 在一个不挂任何工具的临时会话里跑一次请求，返回模型直接输出的文字，用完就关。 */
  private async oneShot(prompt: string): Promise<string> {
    await this.ensureDsh()
    const conn = this.dsh.conn
    if (!conn) throw new AcpExited('session/new')
    const s = await conn.request<{ sessionId: string }>('session/new', { cwd: this.cfg.workDir, mcpServers: [] }, 60_000)
    let text = ''
    conn.onSession(s.sessionId, u => {
      if (u.update.sessionUpdate === 'agent_message_chunk' && u.update.content?.type === 'text') text += String(u.update.content.text ?? '')
    })
    try {
      await this.setSessionBrain(s.sessionId).catch(e => { if (e instanceof AcpExited) throw e })
      await conn.request('session/prompt', { sessionId: s.sessionId, prompt: [{ type: 'text', text: prompt }] }, this.cfg.gw.summaryTimeoutMs)
      return text
    } finally {
      conn.onSession(s.sessionId, null)
      void conn.request('session/close', { sessionId: s.sessionId }, 15_000).catch(() => {})
    }
  }

  // ─── 命令 ───

  isOwner(senderId: string | null): boolean {
    return senderId !== null && this.cfg.gw.owners.includes(senderId)
  }

  private async handleCommand(row: InboundRow): Promise<void> {
    const cmd = parseCommand(row.text)
    const targetOk = !cmd?.target || (this.botUsername !== '' && cmd.target.toLowerCase() === this.botUsername.toLowerCase())
    if (!cmd || !OWNER_COMMANDS.has(cmd.name)) { this.ledger.settleInbound([row.id], 'dropped', 'command ignored'); return }
    if (!targetOk) { this.ledger.settleInbound([row.id], 'dropped', 'command addressed to another bot'); this.log.info('command.wrong_target', { chat: row.chat_id }); return }
    if (!this.isOwner(row.sender_id)) { this.ledger.settleInbound([row.id], 'dropped', 'not owner'); this.log.warn('command.not_owner', { chat: row.chat_id }); return }
    let text: string
    try {
      switch (cmd.name) {
        case 'clear': text = await this.cmdClear(row.chat_id); break
        case 'compact': text = await this.cmdCompact(row.chat_id, cmd.args); break
        case 'model': text = await this.cmdModel(row.chat_id, cmd.args); break
        case 'models': text = await this.cmdModel(row.chat_id, 'list'); break
        case 'provider': case 'providers': text = await this.cmdProvider(cmd.args); break
        default: text = HELP_TEXT
      }
    } catch (e) {
      this.log.warn('command.failed', { name: cmd.name, err: safeError(e) })
      text = `【系统】/${cmd.name} 没做成：${safeError(e).slice(0, 120)}`
    }
    this.ledger.settleInbound([row.id], 'done', cmd.name)
    this.log.info('command.done', { chat: row.chat_id, name: cmd.name })
    await this.sender.send({ chatId: row.chat_id, turnId: null, callSeq: 0, text, kind: 'system' }).catch(() => {})
  }

  /** 清空 = 这一段收尾写一份摘要（写不出来或为空就沿用上一份，不覆盖）+ 下一条消息开新段、不再带清空前的原话 */
  private async cmdClear(chatId: string): Promise<string> {
    const seg = this.ledger.activeSegment(chatId)
    if (seg) {
      try {
        await this.ensureDsh().catch(() => {})
        await this.rollSegment(chatId, seg, 'clear')
      } catch (e) {
        this.log.warn('command.clear_summary_failed', { err: safeError(e) })
        this.ledger.closeSegment(seg.id, 'closed', 'clear')
        if (seg.session_id) this.loaded.delete(seg.session_id)
      }
    }
    this.ledger.setMeta(`clear_at:${chatId}`, String(Date.now()))
    this.ledger.event(chatId, 'clear', { segment: seg?.id ?? null })
    this.log.info('command.clear', { chat: chatId, segment: seg?.id ?? null })
    return '【系统】已清空这段对话的上下文（聊过的要点留了一份摘要），下一条消息从新会话开始。'
  }

  /** 压缩 = 现在就换段：写一份摘要（可以带主人交代的重点），新会话带着摘要和最近的原话（和 /clear 的区别：原话也带） */
  private async cmdCompact(chatId: string, focus: string): Promise<string> {
    const seg = this.ledger.activeSegment(chatId)
    if (!seg?.session_id || this.ledger.segmentTurnsOfKind(seg.id, 'message') === 0) return '【系统】这段对话还没聊什么，不用压缩。'
    await this.ensureDsh()
    const r = await this.rollSegment(chatId, seg, 'compact', focus || undefined)
    this.log.info('command.compact', { chat: chatId, segment: seg.id, summary: r.how, summary_chars: r.chars, focus: focus.length > 0 })
    if (r.how === 'crashed') return '【系统】压缩时模型进程出了问题。下一条消息会用聊天记录补写摘要，再开新会话。'
    if (r.how === 'none') return '【系统】已换新会话，但这次摘要没写成，沿用上一份摘要；最近的原话照常带上。'
    return `【系统】已把这段对话压缩成一份摘要（${r.chars} 字，压缩前约 ${seg.used_tokens ?? '?'} token）。下一条消息从新会话开始，带着摘要和最近的原话。`
  }

  private async cmdModel(chatId: string, args: string): Promise<string> {
    const a = args.trim()
    const b = this.brain
    const cur = { provider: b.provider, model: b.model }
    const file = this.configBrain
    if (a === '' || a === 'status') {
      const seg = this.ledger.activeSegment(chatId)
      const budget = seg ? this.budgetFor(seg) : null
      return [
        `【系统】现在用的模型：${b.provider} / ${b.model}${b.reasoningEffort ? `（思考 ${b.reasoningEffort}）` : ''}`,
        b.provider === file.provider && b.model === file.model ? '来自配置文件。' : `这是用命令换的；配置文件里是 ${file.provider} / ${file.model}，/model default 换回去。`,
        ...(seg?.used_tokens ? [`这段对话已用 ${seg.used_tokens} token${budget ? `，到 ${budget} 换段` : ''}。`] : []),
        '看能换哪些：/model list；换模型：/model <模型名>。',
      ].join('\n')
    }
    if (a === 'default') {
      if (b.provider === file.provider && b.model === file.model) return `【系统】现在用的就是配置文件里的模型：${file.provider} / ${file.model}。`
      this.setOverride(null)
      return `【系统】已换回配置文件里的模型：${file.provider} / ${file.model}，下一条消息起生效。`
    }
    const choices = await this.modelChoicesNow()
    if (choices.length === 0) return '【系统】暂时拿不到可选模型的列表，稍后再试。'
    if (a === 'list') return formatModelList(choices, cur)
    const r = resolveModel(a, choices)
    if ('error' in r) return `【系统】${r.error}`
    return this.switchTo(r.ok)
  }

  private async cmdProvider(args: string): Promise<string> {
    const a = args.trim()
    const choices = await this.modelChoicesNow()
    if (choices.length === 0) return '【系统】暂时拿不到可选模型的列表，稍后再试。'
    const cur = { provider: this.brain.provider, model: this.brain.model }
    if (a === '') return formatProviders(choices, cur, p => this.keyStatus(p))
    const p = providersOf(choices).find(x => x.toLowerCase() === a.toLowerCase())
    if (!p) return `【系统】没有 ${a} 这个供应商。用 /provider 看有哪些。`
    if (p === cur.provider) return `【系统】现在用的就是 ${p}（模型 ${cur.model}）。`
    // 配置文件里用的就是这个供应商：换回配置文件里的模型；否则用它的第一个模型
    const file = this.configBrain
    const pick = choices.find(c => c.provider === p && p === file.provider && c.model === file.model) ?? choices.find(c => c.provider === p)!
    return this.switchTo(pick)
  }

  private switchTo(c: ModelChoice): string {
    if (c.provider === this.brain.provider && c.model === this.brain.model) return `【系统】现在用的就是 ${c.provider} / ${c.model}。`
    const k = this.keyStatus(c.provider)
    if (k.status === 'missing') return `【系统】${c.provider} 还没配密钥（凭据文件里填 ${k.env}），先不换。`
    this.setOverride(c)
    return `【系统】已换成 ${c.provider} / ${c.model}，下一条消息起生效。这个 bot 的所有聊天都换，重启后保持；/model default 换回配置文件里的。`
  }

  // ─── /model、/provider 换的模型：记在账本里，重启后保持；配置文件里的模型改了就作废（以后改的为准） ───

  private withOverride(file: Brain): Brain {
    let o: { provider: string; model: string; configKey: string } | null = null
    try { o = JSON.parse(this.ledger.getMeta('brain_override') || 'null') } catch {}
    if (!o) return file
    if (o.configKey !== brainKey(file)) {
      this.ledger.setMeta('brain_override', '')
      this.log.info('brain.override_cleared', { reason: 'config changed' })
      return file
    }
    return { ...file, provider: o.provider, model: o.model }
  }

  private setOverride(c: ModelChoice | null): void {
    const file = this.configBrain
    const same = !c || (c.provider === file.provider && c.model === file.model)
    this.ledger.setMeta('brain_override', same ? '' : JSON.stringify({ provider: c!.provider, model: c!.model, configKey: brainKey(file) }))
    this.brain = this.withOverride(file)
    this.log.info('brain.override', { provider: this.brain.provider, model: this.brain.model, from_config: same })
  }

  private noteOptions(opts: unknown): void {
    const c = parseModelChoices(opts)
    if (c.length) this.modelChoices = c
  }

  /** 可选模型：用最近一次 dsh 回报的；还没有就开一个临时会话问一下（不发请求，不花钱） */
  private async modelChoicesNow(): Promise<ModelChoice[]> {
    if (this.modelChoices.length) return this.modelChoices
    await this.ensureDsh()
    const conn = this.dsh.conn
    if (!conn) throw new AcpExited('session/new')
    const s = await conn.request<{ sessionId: string; configOptions?: any[] }>('session/new', { cwd: this.cfg.workDir, mcpServers: [] }, 60_000)
    void conn.request('session/close', { sessionId: s.sessionId }, 15_000).catch(() => {})
    this.noteOptions(s.configOptions)
    return this.modelChoices
  }

  /** 供应商的密钥配没配：只看凭据文件里有没有像样的值，绝不读出、打印值本身 */
  private keyStatus(provider: string): { status: KeyStatus; env?: string } {
    const route = this.brain.routes[provider]
    if (provider !== 'deepseek-official' && !route) return { status: 'unknown' }
    const env = route ? route.apiKeyEnv : 'DEEPSEEK_API_KEY'
    if (!env) return { status: 'none' }
    let refs: Record<string, unknown> = {}
    try { refs = (Bun.YAML.parse(readFileSync(this.cfg.credentialsPath, 'utf8')) as { refs?: Record<string, unknown> })?.refs ?? {} } catch {}
    const v = refs[env]
    return { status: typeof v === 'string' && /^[\x21-\x7e]{8,}$/.test(v.trim()) ? 'ok' : 'missing', env }
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
    const beforeCap = this.brain.maxInputTokens ?? null
    this.configBrain = nb
    this.brain = this.withOverride(nb)
    const b = this.brain
    if (brainKey(b) !== before || (b.maxInputTokens ?? null) !== beforeCap) {
      this.log.info('config.brain_changed', { provider: b.provider, model: b.model, effort: b.reasoningEffort ?? null, max_input_tokens: b.maxInputTokens ?? null })
    }
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
