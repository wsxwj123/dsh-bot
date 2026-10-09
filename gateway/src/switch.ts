// 双向切换（旧系统 claudebotlife ↔ 新系统 dsh 网关）的编排：动作序列的规划与执行。
// 外部命令、文件读写、时钟、等待全部注入，白盒单测用假记录器断言"先停后起"；命令行在 scripts/switch.ts；
// 手册 docs/dsh-migration/CUTOVER.md。旧侧脚本的语义以 ~/.claude/dispatcher/ 下的实际脚本为准：
//   stop-bot.sh 写停用标记 .disabled/<旧名> 并杀掉 tg-<旧名>* 会话；
//   start-bot.sh 删标记、清扫积压、再全栈重启（所有旧 bot 都会被重启一遍，不只这一个）。
//
// 铁律（同一个 Telegram 令牌同一时刻只能有一个进程收消息）：
//   离开的一侧必须**确认停住**，才允许启动目标侧；确认不了（进程还活着）就终止，两边都不动、什么都不写。
// 幂等与互斥：
//   目标侧已经在跑 = 重复执行：只重装一遍自启，不重复启动，也不报错；
//   离开的一侧本来没在跑 = 只提示，不算错误；
//   离开的一侧还在跑而我们不被允许停它（--no-stop-old）→ 默认中止，让人先手动停。
import { dirname, join } from 'path'
import { mkdirSync, readFileSync, readdirSync, renameSync, rmSync, statSync, writeFileSync, openSync, closeSync, chmodSync } from 'fs'
import { parseLaunchctlPrint, SHARED } from './autostart'

export const DIRECTOR_LABEL = 'com.dsh-bot.director'
export const gatewayLabel = (bot: string): string => `com.dsh-bot.${bot}`
export const jobsLabel = (bot: string): string => `com.dsh-bot.self-initiate.${bot}`
/** 心跳多久算新鲜。与 scripts/import_history.py 的 _gateway_running 同一条规则（15 秒） */
export const HEARTBEAT_FRESH_MS = 15_000
/** 旧侧"这个 bot 在不在跑"的端口表默认值，与 ~/.claude/dispatcher/bot-enabled.sh 的 WATCHDOG_PORTS 一字不差 */
export const DEFAULT_WATCHDOG_PORTS = 'yasuna:17801 bot2:17802 bot3:17803 bot4:17804'
/** 旧的 .promises.json 每个聊天最多几条没到点的（与旧系统 promise_plan.ts 的 MAX_PENDING_PER_CHAT 一致） */
export const MAX_PENDING_PER_CHAT = 20
/** 带回旧侧时的截断上限：摘要最多多少字、最近原话取几条 */
export const HANDOVER_SUMMARY_MAX = 4_000
export const HANDOVER_QUOTES_MAX = 12
export const HANDOVER_MEMORIES_MAX = 50

export type RunResult = { code: number; out: string }
export type Run = (cmd: string[], env?: Record<string, string | undefined>) => RunResult

export type SwitchFs = {
  exists(p: string): boolean
  read(p: string): string | null
  /** 原子写（先写临时文件再改名），mode 不给按系统默认 */
  write(p: string, text: string, mode?: number): void
  mkdirp(p: string): void
  /** 文件 mtime（毫秒）；取不到返回 null */
  mtimeMs(p: string): number | null
  /** 目录下的条目名；目录不存在返回 [] */
  list(p: string): string[]
}

/** 账本里要带回旧侧的三样东西（back 方向）。只读取出，写入逻辑在 carryBack。 */
export type LedgerPayload = {
  chatId: string
  /** 最近一份非空 segments.summary */
  summary: string | null
  /** 最近的原话（正序，最多 HANDOVER_QUOTES_MAX 条） */
  quotes: { who: 'user' | 'bot'; text: string }[]
  /** memories 表里的长期记忆（升序） */
  memories: { text: string; at: number }[]
  /** state='pending' 的承诺 */
  pending: { chatId: string; text: string; quote: string; dueAt: number }[]
  /** 新系统里这个聊天的第一段/最后一段的时间（写摘要抬头用），没有段就是 null */
  firstAt: number | null
  lastAt: number | null
}

export const EMPTY_PAYLOAD: LedgerPayload = { chatId: '', summary: null, quotes: [], memories: [], pending: [], firstAt: null, lastAt: null }

export type Ctx = {
  bot: string
  oldName: string
  /** 旧 bot 的频道目录（人设、白名单、.promises.json 都在这；yasuna 是 channels/telegram） */
  oldChannelDir: string
  configPath: string
  /** 新仓库根（跑 gateway/scripts/autostart.ts 用） */
  repo: string
  bun: string
  bash: string
  tmux: string | null
  nc: string | null
  /** ~/.dsh-bot/bots/<bot>（心跳、账本在它下面） */
  newBotDir: string
  /** 旧 bot 名 → dispatcher 端口；来源是 bot-enabled.sh 的 WATCHDOG_PORTS 格式 */
  ports: Record<string, number>
}

export type SwitchDeps = {
  home: string
  uid: number
  fs: SwitchFs
  run: Run
  print: (line: string) => void
  now: () => number
  /** 轮询等条件成立，最多等 seconds 秒；脚本层是真 sleep，测试给假的 */
  waitFor: (cond: () => boolean, seconds: number) => boolean
  /** 等新侧网关就绪（心跳/日志），返回结论与一句说明；脚本层实现 */
  waitNewReady: (seconds: number) => { ok: boolean; note: string }
  /** back：只读账本，把三样东西取出来；脚本层用 bun:sqlite 实现 */
  readLedger: () => LedgerPayload
}

/* ── 旧侧 ─────────────────────────────────────────────── */

export type OldState = {
  name: string
  channelDir: string
  /** .disabled/<旧名> 在不在（停用标记 = 旧系统唯一的"这个 bot 停了"真相） */
  disabled: boolean
  /** tmux 里属于这个 bot 的会话（tg-<名>、-dispatcher、-worker、-<chat_id>） */
  sessions: string[]
  /** dispatcher 端口开着没有；null = 没配端口 / 没有 nc，不知道 */
  portOpen: boolean | null
  running: boolean
  /** 确认停住 = 有停用标记且没有运行痕迹（少了标记，旧看门狗 5 分钟内会把它拉回来） */
  stopped: boolean
}

export const disabledMarker = (home: string, oldName: string): string => join(home, '.claude', 'dispatcher', '.disabled', oldName)

/** 旧侧频道目录：与 self-initiate.sh / start-bot.sh 同规则——yasuna 住 channels/telegram，有 channels/yasuna 才用它 */
export function oldChannelDirFor(home: string, oldName: string, fs: SwitchFs): string {
  const root = join(home, '.claude', 'channels')
  const own = join(root, oldName)
  if (oldName === 'yasuna') return fs.exists(own) ? own : join(root, 'telegram')
  return own
}

/** "yasuna:17801 bot2:17802 …" → { yasuna: 17801, … }；坏条目丢掉不抛 */
export function parseWatchdogPorts(raw: string): Record<string, number> {
  const out: Record<string, number> = {}
  for (const item of raw.trim().split(/\s+/)) {
    const [name, port] = item.split(':')
    if (!name || !/^\d+$/.test(port ?? '')) continue
    out[name] = Number(port)
  }
  return out
}

/** 会话名是不是这个 bot 的。规则与 stop-bot.sh 的 bot_sessions 一致 */
export function tmuxSessionOfBot(name: string, session: string): boolean {
  if (!/^[A-Za-z0-9_-]+$/.test(name)) return false
  return new RegExp(`^tg-${name}(-dispatcher|-worker|-?\\d+)?$`).test(session)
}

export function probeOld(ctx: Ctx, deps: SwitchDeps): OldState {
  const disabled = deps.fs.exists(disabledMarker(deps.home, ctx.oldName))
  let sessions: string[] = []
  if (ctx.tmux) {
    // 与旧系统同一条：固定 TMUX_TMPDIR，否则 launchd 与用户 shell 各建一套 tmux server 互不相见
    const r = deps.run([ctx.tmux, 'ls', '-F', '#S'], { TMUX_TMPDIR: '/tmp' })
    if (r.code === 0) sessions = r.out.split('\n').map(s => s.trim()).filter(s => tmuxSessionOfBot(ctx.oldName, s))
  }
  const port = ctx.ports[ctx.oldName] ?? null
  const portOpen = port !== null && ctx.nc ? deps.run([ctx.nc, '-z', '127.0.0.1', String(port)]).code === 0 : null
  const running = sessions.length > 0 || portOpen === true
  return { name: ctx.oldName, channelDir: ctx.oldChannelDir, disabled, sessions, portOpen, running, stopped: disabled && !running }
}

/* ── 新侧 ─────────────────────────────────────────────── */

export type NewState = {
  bot: string
  gatewayInstalled: boolean
  gatewayRunning: boolean
  pid: string | null
  jobsInstalled: boolean
  directorInstalled: boolean
  /** 心跳文件还新鲜（网关在写）——手动起的网关也认得出 */
  heartbeatFresh: boolean
  /** 有人在收消息：launchd 说在跑，或者心跳还新鲜 */
  running: boolean
}

function launchctlPrint(deps: SwitchDeps, label: string): { code: number; out: string } {
  return deps.run(['launchctl', 'print', `gui/${deps.uid}/${label}`])
}

export function probeNew(ctx: Ctx, deps: SwitchDeps): NewState {
  const g = launchctlPrint(deps, gatewayLabel(ctx.bot))
  const parsed = parseLaunchctlPrint(g.out, g.code)
  const jobs = launchctlPrint(deps, jobsLabel(ctx.bot))
  const director = launchctlPrint(deps, DIRECTOR_LABEL)
  const hb = deps.fs.mtimeMs(join(ctx.newBotDir, 'state', 'heartbeat'))
  const heartbeatFresh = hb !== null && deps.now() - hb < HEARTBEAT_FRESH_MS
  const gatewayRunning = g.code === 0 && parsed.state === 'running'
  return {
    bot: ctx.bot,
    gatewayInstalled: g.code === 0,
    gatewayRunning,
    pid: parsed.pid,
    jobsInstalled: jobs.code === 0,
    directorInstalled: director.code === 0,
    heartbeatFresh,
    running: gatewayRunning || heartbeatFresh,
  }
}

/** 新系统里除 <bot> 之外还有哪些 bot 的网关在跑（导演是所有新 bot 共用的，退回一个 bot 时不该顺手停掉别人的群聊） */
export function otherNewBotsRunning(ctx: Ctx, deps: SwitchDeps): string[] {
  const dir = join(deps.home, 'Library', 'LaunchAgents')
  const shared = new Set(SHARED.map(j => j.name))
  const out: string[] = []
  for (const f of deps.fs.list(dir)) {
    if (!f.startsWith('com.dsh-bot.') || !f.endsWith('.plist')) continue
    const label = f.slice(0, -'.plist'.length)
    const name = label.slice('com.dsh-bot.'.length)
    if (name === 'director' || name === ctx.bot || name === `self-initiate.${ctx.bot}` || shared.has(name)) continue
    const r = launchctlPrint(deps, label)
    if (r.code === 0 && parseLaunchctlPrint(r.out, r.code).state === 'running') out.push(name)
  }
  return out
}

/* ── 带回旧侧的三样东西 ─────────────────────────────────── */

/** 旧 bot 的自动记忆文件：Claude Code 的 auto-memory 路径，slug 规则与 chat_history._project_slug_for 一致 */
export const oldProjectSlug = (channelDir: string): string => channelDir.replace(/[^a-zA-Z0-9]/g, '-')
export const oldMemoryPath = (home: string, channelDir: string): string =>
  join(home, '.claude', 'projects', oldProjectSlug(channelDir), 'memory', 'MEMORY.md')
export const oldPromisesPath = (channelDir: string): string => join(channelDir, '.promises.json')

export function dateStamp(ms: number): string {
  const d = new Date(ms)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

const HANDOVER_HEAD = (stamp: string) => `## dsh 交接（${stamp}）`
/** 归一化：去掉空白与列表符，用来判"这句是不是已经在文件里了" */
const norm = (s: string): string => s.replace(/\s+/g, '').replace(/^[-*]+/, '')

export function buildHandoverBlock(payload: LedgerPayload, stamp: string): string {
  const lines: string[] = [HANDOVER_HEAD(stamp), '']
  const range = payload.firstAt && payload.lastAt
    ? `（${dateStamp(payload.firstAt)} 到 ${dateStamp(payload.lastAt)}）`
    : ''
  lines.push(`我在新系统（dsh）上待了一段时间${range}，下面是那边的会话摘要和长期记忆，接在我原来的记忆后面：`)
  lines.push('')
  if (payload.summary) {
    const s = payload.summary.trim()
    lines.push(s.length > HANDOVER_SUMMARY_MAX ? `${s.slice(0, HANDOVER_SUMMARY_MAX)}…（后面略）` : s)
    lines.push('')
  }
  if (payload.quotes.length) {
    lines.push('最近说过的几句：')
    for (const q of payload.quotes) lines.push(`- ${q.who === 'user' ? '他' : '我'}：${q.text}`)
    lines.push('')
  }
  const fresh = payload.memories
  if (fresh.length) {
    lines.push('在新系统里记下的长期记忆：')
    for (const m of fresh) lines.push(`- ${dateStamp(m.at)} ${m.text}`)
    lines.push('')
  }
  return lines.join('\n').replace(/\n+$/, '\n')
}

/** 记忆条目里，归一化后没有出现在 cur 中的那些（去重靠这个） */
export function memoriesNotIn(cur: string, memories: { text: string; at: number }[]): { text: string; at: number }[] {
  const hay = norm(cur)
  const out: { text: string; at: number }[] = []
  for (const m of memories) {
    const t = m.text.trim()
    if (!t || hay.includes(norm(t))) continue
    if (out.some(x => norm(x.text) === norm(t))) continue
    out.push({ text: t, at: m.at })
  }
  return out
}

/**
 * 把交接小节追加进旧的 MEMORY.md：只追加、不重写、不动别的段落。
 * 今天的交接小节已经在（同一天跑第二遍）→ 一个字都不写。
 */
export function appendHandover(cur: string | null, block: string, stamp: string): { text: string; added: boolean; why: string } {
  const text = cur ?? ''
  if (text.includes(HANDOVER_HEAD(stamp))) return { text, added: false, why: '同一个日期的交接小节已经在文件里了，没重复写' }
  // 文件不存在（这个 bot 还没跑过会话）就建一个最小骨架，别让 bot 读到半截文件
  if (!text.length) return { text: `# Memory\n\n${block}\n`, added: true, why: '' }
  return { text: `${text.replace(/\s*$/, '')}\n\n${block}\n`, added: true, why: '' }
}

/** 旧的 .promises.json 一条记录（字段与旧系统 promise_plan.ts 的 PromiseRecord 一致，多带一个 text 给 import_history 认） */
export function promiseRecord(o: { chatId: string; text: string; dueAt: number; now: number }): Record<string, unknown> {
  return {
    chat_id: o.chatId,
    due_at: o.dueAt,
    gist: o.text,
    // 旧系统读 gist；新系统的 import_history.py 认 text —— 两个字段写同一句，两边都读得懂
    text: o.text,
    created_at: o.now,
    state: 'pending',
    attempts: 0,
    fired_at: null,
    message_id: null,
    inbox_key: null,
    deferred_since: null,
    deferred_reason: null,
    defer_until: null,
    probe_fails: 0,
    delivered_at: null,
    source_chat_id: o.chatId,
    requester_id: null,
  }
}

export type MergeResult = { store: Record<string, unknown>; added: number; skipped: number }

/**
 * 把还没到点的承诺并进旧的 .promises.json（不覆盖已有条目，同 chat + 到点时刻 + 同一句话算重复）。
 * 每个聊天最多 MAX_PENDING_PER_CHAT 条（超了按最早到点取，其它报 skipped）。
 */
export function mergePromises(cur: Record<string, unknown>, items: { chatId: string; text: string; dueAt: number }[], now: number): MergeResult {
  const store: Record<string, unknown> = { ...cur }
  const existing = Object.entries(store).map(([, v]) => v as Record<string, unknown>)
  const isoWeek = (ms: number) => Math.floor(ms / 1000).toString(36)
  let added = 0
  let skipped = 0
  const perChat: Record<string, number> = {}
  for (const rec of Object.values(store)) {
    const r = rec as Record<string, unknown>
    if (r && r.state === 'pending' && typeof r.chat_id === 'string') perChat[r.chat_id] = (perChat[r.chat_id] ?? 0) + 1
  }
  for (const it of [...items].sort((a, b) => a.dueAt - b.dueAt)) {
    const text = it.text.trim()
    if (!text) { skipped++; continue }
    const dup = existing.some(r => r && r.chat_id === it.chatId && r.due_at === it.dueAt && norm(String(r.gist ?? r.text ?? '')) === norm(text))
    if (dup) { skipped++; continue }
    if ((perChat[it.chatId] ?? 0) >= MAX_PENDING_PER_CHAT) { skipped++; continue }
    let id = `p-${isoWeek(it.dueAt)}-${Math.abs(hash32(`${it.chatId}|${text}|${it.dueAt}`)).toString(16).slice(0, 8)}`
    while (id in store) id = `${id}x`
    store[id] = promiseRecord({ chatId: it.chatId, text, dueAt: it.dueAt, now })
    existing.push(store[id] as Record<string, unknown>)
    perChat[it.chatId] = (perChat[it.chatId] ?? 0) + 1
    added++
  }
  return { store, added, skipped }
}

function hash32(s: string): number {
  let h = 2166136261
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619) }
  return h | 0
}

export type CarryResult = { ok: boolean; memoryAdded: boolean; memories: number; promises: number; notes: string[] }

/** 把摘要+记忆写进旧侧 MEMORY.md、把没到点的承诺写进旧侧 .promises.json（都在临时目录里可测） */
export function carryBack(ctx: Ctx, payload: LedgerPayload, deps: SwitchDeps): CarryResult {
  const notes: string[] = []
  const stamp = dateStamp(deps.now())
  const memPath = oldMemoryPath(deps.home, ctx.oldChannelDir)
  const memCur = deps.fs.read(memPath)
  const fresh = memoriesNotIn(memCur ?? '', payload.memories)
  let memoryAdded = false
  if (!payload.summary && !payload.quotes.length && !fresh.length) {
    notes.push(`新系统里没有可带回来的摘要/记忆（${memPath} 没动）`)
  } else {
    const block = buildHandoverBlock({ ...payload, memories: fresh }, stamp)
    const r = appendHandover(memCur, block, stamp)
    if (!r.added) notes.push(`${memPath}：${r.why}`)
    else { deps.fs.mkdirp(dirname(memPath)); deps.fs.write(memPath, r.text); memoryAdded = true; notes.push(`交接小节已追加进 ${memPath}（摘要 ${payload.summary ? '1' : '0'} 份、原话 ${payload.quotes.length} 条、记忆 ${fresh.length} 条，原来的段落没动）`) }
  }
  const path = oldPromisesPath(ctx.oldChannelDir)
  const raw = deps.fs.read(path)
  let cur: Record<string, unknown> = {}
  if (raw !== null && raw.trim()) {
    try {
      const parsed = JSON.parse(raw)
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('not_object')
      cur = parsed as Record<string, unknown>
    } catch {
      notes.push(`${path} 不是能读的 JSON（旧承诺的账本），按不动它处理：承诺没带回，其它照常`)
      return { ok: false, memoryAdded, memories: fresh.length, promises: 0, notes }
    }
  }
  const items = payload.pending.map(p => ({ chatId: p.chatId || payload.chatId, text: p.text, dueAt: p.dueAt })).filter(p => p.chatId)
  const merged = mergePromises(cur, items, deps.now())
  if (merged.added > 0) {
    const text = JSON.stringify(merged.store)
    deps.fs.mkdirp(dirname(path))
    deps.fs.write(path, text, 0o600)
    notes.push(`没到点的承诺 ${merged.added} 条已写进 ${path}（原有的 ${Object.keys(cur).length} 条没动）`)
  } else if (items.length) {
    notes.push(`${path} 里已经有这些承诺（或每个聊天已满 ${MAX_PENDING_PER_CHAT} 条），没重复写`)
  } else {
    notes.push('新系统里没有还没到点的承诺，承诺文件没动')
  }
  if (merged.skipped && items.length - merged.added > 0) notes.push(`另有 ${items.length - merged.added} 条承诺没写进旧侧（重复或超出每个聊天的上限）`)
  return { ok: true, memoryAdded, memories: fresh.length, promises: merged.added, notes }
}

/* ── 配置里的停用标记（enabled） ─────────────────────────── */

/** 切回旧栈时写进配置的那行：与手工写的（bot3.yml）一字不差 */
export const ENABLED_FALSE_LINE = 'enabled: false   # 现在跑在旧栈（claudebotlife）；切回新系统时删掉这行'

const ENABLED_RE = /^enabled[ \t]*:/
const DISPATCHER_PORT_RE = /^dispatcher_port[ \t]*:/

/**
 * 一行 enabled 的值算不算"启用"。口径与 dsh-bot 侧的 config_loader.list_enabled_bots 相同：
 * 没写字段 = 启用；只有显式假值（false/no/off/0）才停；`enabled:` 空值按没写算；
 * 带引号的 `"false"` 是字符串（非空即启用），不是假值。
 */
export function enabledValueOf(line: string): boolean {
  const rest = line.slice(line.indexOf(':') + 1)
  const ci = rest.search(/(^|[ \t])#/)
  const v = (ci >= 0 ? rest.slice(0, ci) : rest).trim()
  if (v === '') return true
  if (v.startsWith('"') || v.startsWith("'")) {
    const end = v.indexOf(v[0]!, 1)
    return (end >= 0 ? v.slice(1, end) : v.slice(1)).trim() !== ''
  }
  return !/^(false|no|off|0)$/i.test(v)
}

/**
 * 顶层 enabled 行的行级编辑：切回旧栈写 false（有就改值、没有就插在 dispatcher_port 后面），
 * 切回新系统把这行删掉。只动这一行，文件其它字节原样（缩进与注释风格跟手工写的一致）。
 */
export function editEnabledYml(text: string, on: boolean): { text: string; changed: boolean; what: string } {
  const lines = text.split('\n')
  const i = lines.findIndex(l => ENABLED_RE.test(l))
  if (on) {
    if (i < 0) return { text, changed: false, what: '没有 enabled 行（本来就按新系统算），不用改' }
    const gone = lines[i]!.trim()
    lines.splice(i, 1)
    return { text: lines.join('\n'), changed: true, what: `删掉第 ${i + 1} 行「${gone}」` }
  }
  if (i >= 0) {
    const line = lines[i]!
    if (!enabledValueOf(line)) return { text, changed: false, what: `第 ${i + 1} 行已经是停用（${line.trim()}），不用改` }
    // 只把值换成 false：冒号后的缩进、值两边的空白、行尾注释原样保留
    const head = line.slice(0, line.indexOf(':') + 1)
    const rest = line.slice(head.length)
    const ci = rest.search(/(^|[ \t])#/)
    const value = ci >= 0 ? rest.slice(0, ci) : rest
    const comment = ci >= 0 ? rest.slice(ci) : ''
    lines[i] = `${head}${/^[ \t]*/.exec(value)![0]}false${/[ \t]*$/.exec(value)![0]}${comment}`
    return { text: lines.join('\n'), changed: true, what: `第 ${i + 1} 行改成 enabled: false` }
  }
  const dp = lines.findIndex(l => DISPATCHER_PORT_RE.test(l))
  const at = dp >= 0 ? dp + 1 : 0
  lines.splice(at, 0, ENABLED_FALSE_LINE)
  return { text: lines.join('\n'), changed: true, what: `在第 ${at + 1} 行插入 enabled: false（${dp >= 0 ? 'dispatcher_port 后面' : '文件开头'}）` }
}

/* ── 动作 ─────────────────────────────────────────────── */

export type Action =
  | { kind: 'stop-old' }
  | { kind: 'start-old' }
  | { kind: 'bootout'; label: string; what: string }
  | { kind: 'install'; what: 'gateway' | 'jobs' }
  | { kind: 'carry-back'; payload: LedgerPayload }
  | { kind: 'set-enabled'; on: boolean }

export function describeAction(a: Action, ctx: Ctx, deps: SwitchDeps): string {
  switch (a.kind) {
    case 'stop-old':
      return `bash ~/.claude/dispatcher/stop-bot.sh ${ctx.oldName}（写停用标记，杀掉 tmux 会话）`
    case 'start-old':
      return `bash ~/.claude/dispatcher/start-bot.sh ${ctx.oldName}（删标记，全栈重启旧 bot——其它旧 bot 也会被重启一遍）`
    case 'bootout':
      return `launchctl bootout gui/${deps.uid}/${a.label}（停 ${a.what}；plist 留着，切回来还用它）`
    case 'install':
      return `${ctx.bun} gateway/scripts/autostart.ts ${a.what === 'gateway' ? 'install' : 'install-jobs'} ${ctx.configPath}`
    case 'carry-back':
      return `把新系统期间的摘要、长期记忆写进 ${oldMemoryPath(deps.home, ctx.oldChannelDir)}，把 ${a.payload.pending.length} 条没到点的承诺写进 ${oldPromisesPath(ctx.oldChannelDir)}（只追加，不重写）`
    case 'set-enabled': {
      const text = deps.fs.read(ctx.configPath)
      const what = text === null ? '读不了这个文件，做不了' : editEnabledYml(text, a.on).what
      return `${ctx.configPath}：${what}`
    }
  }
}

/** 跑一个动作；返回一句话说明。任何失败都只回结果，不抛。 */
export function execAction(a: Action, ctx: Ctx, deps: SwitchDeps): { ok: boolean; note: string } {
  switch (a.kind) {
    case 'stop-old': {
      const r = deps.run([ctx.bash, join(deps.home, '.claude', 'dispatcher', 'stop-bot.sh'), ctx.oldName])
      return { ok: r.code === 0, note: r.code === 0 ? `旧侧 ${ctx.oldName} 已停用` : `stop-bot.sh 退出码 ${r.code}：${firstLine(r.out)}` }
    }
    case 'start-old': {
      const r = deps.run([ctx.bash, join(deps.home, '.claude', 'dispatcher', 'start-bot.sh'), ctx.oldName])
      return { ok: r.code === 0, note: r.code === 0 ? `旧侧 ${ctx.oldName} 已拉起（所有旧 bot 一起重启了一遍）` : `start-bot.sh 退出码 ${r.code}：${firstLine(r.out)}` }
    }
    case 'bootout': {
      const r = deps.run(['launchctl', 'bootout', `gui/${deps.uid}/${a.label}`])
      // 本来就没装/没在跑：bootout 会报 Could not find service，按"已经是不在跑的状态"算成功
      const gone = /could not find|no such process|not find/i.test(r.out)
      return { ok: r.code === 0 || gone, note: r.code === 0 ? `已停 ${a.label}` : gone ? `${a.label} 本来就没在跑` : `launchctl bootout ${a.label} 退出码 ${r.code}：${firstLine(r.out)}` }
    }
    case 'install': {
      const r = deps.run([ctx.bun, join(ctx.repo, 'gateway', 'scripts', 'autostart.ts'), a.what === 'gateway' ? 'install' : 'install-jobs', ctx.configPath])
      const what = a.what === 'gateway' ? '网关' : '主动消息'
      return { ok: r.code === 0, note: r.code === 0 ? `${what}已设为开机自启并启动` : `装${what}自启失败（退出码 ${r.code}）：${firstLine(r.out)}` }
    }
    case 'carry-back': {
      // 写回失败（旧文件坏了、磁盘不让写）不该拦住"把旧侧起回来"：只回 ok=false，由编排层降级成警告
      try {
        const c = carryBack(ctx, a.payload, deps)
        return { ok: c.ok, note: c.notes.join('；') }
      } catch (e) {
        return { ok: false, note: `往旧侧写摘要/记忆/承诺时出错（${(e as Error).name}）` }
      }
    }
    case 'set-enabled': {
      const text = deps.fs.read(ctx.configPath)
      if (text === null) return { ok: false, note: `读不了配置 ${ctx.configPath}，enabled 没改` }
      const e = editEnabledYml(text, a.on)
      if (!e.changed) return { ok: true, note: `${ctx.configPath}：${e.what}` }
      try {
        deps.fs.write(ctx.configPath, e.text)
        return { ok: true, note: `${ctx.configPath}：${e.what}` }
      } catch (err) {
        return { ok: false, note: `配置写不进去（${(err as Error).name}），enabled 没改` }
      }
    }
  }
}

/** 标/清 enabled 的收尾：失败不拦路（它只是给共用服务/管理台看的标记），但报告里要说清楚 */
const isSetEnabled = (x: Action): x is Extract<Action, { kind: 'set-enabled' }> => x.kind === 'set-enabled'

function applySetEnabled(a: Extract<Action, { kind: 'set-enabled' }>, ctx: Ctx, deps: SwitchDeps, done: string[], p: (l: string) => void): void {
  const r = execAction(a, ctx, deps)
  if (r.ok) { done.push(r.note); p(`  · ${r.note}`) }
  else {
    p(`  ⚠️  ${r.note}（手工补：${a.on ? '删掉配置顶层的 enabled 行' : `在配置顶层加一行 ${ENABLED_FALSE_LINE.split('   #')[0]}`}）`)
    done.push(`配置标记：失败（${r.note}）`)
  }
}

const firstLine = (s: string): string => (s.split('\n').map(x => x.trim()).filter(Boolean)[0] ?? '').slice(0, 200)

/* ── 编排 ─────────────────────────────────────────────── */

function report(deps: SwitchDeps, done: string[], todo: string[], back: string): void {
  const p = deps.print
  p('')
  p(done.length ? '已做：' : '已做：无')
  for (const d of done) p(`  ✓ ${d}`)
  if (todo.length) {
    p('没做：')
    for (const t of todo) p(`  · ${t}`)
  }
  p(`怎么退回去：${back}`)
}

function describeOld(o: OldState): string {
  const where = o.running ? `还在跑（${o.sessions.length} 个 tmux 会话${o.portOpen === true ? '，端口还开着' : ''}）` : '没在跑'
  return `旧侧 ${o.name}：${o.disabled ? '已停用（停用标记在）' : '未停用（停用标记不在）'}，${where}`
}

function describeNew(n: NewState): string {
  const gw = n.gatewayRunning ? `在跑（pid ${n.pid ?? '?'}）` : n.gatewayInstalled ? '装了但没在跑' : '没装'
  const jobs = n.jobsInstalled ? '已装' : '没装'
  return `新侧 ${n.bot}：网关${gw}；主动消息任务${jobs}`
}

export function switchToDsh(ctx: Ctx, deps: SwitchDeps, opts: { dryRun: boolean; stopOld: boolean; waitSeconds: number }): number {
  const p = deps.print
  p(`切换 ${ctx.bot}：旧系统 → 新系统（旧名 ${ctx.oldName}）`)
  const old = probeOld(ctx, deps)
  const nw = probeNew(ctx, deps)
  p(`  ${describeOld(old)}`)
  p(`  ${describeNew(nw)}`)
  if (nw.gatewayRunning) p('  · 新侧网关本来就在跑：这次是重复执行，只会重装一遍自启（中途它会重启一次）')

  if (old.running && !opts.stopOld) {
    p(`⛔ 旧侧 ${ctx.oldName} 还在跑，而这次带了 --no-stop-old（本脚本不动旧侧）。`)
    p(`   同一个令牌同一时刻只能一个进程收消息：先手动跑 bash ~/.claude/dispatcher/stop-bot.sh ${ctx.oldName}，`)
    p(`   确认 tmux 里 tg-${ctx.oldName} 的会话没了，再重跑本命令。这次什么都没做。`)
    return 1
  }

  const actions: Action[] = []
  if (old.running) actions.push({ kind: 'stop-old' })
  else p(`  · 旧侧 ${ctx.oldName} 没在跑，跳过 stop-bot.sh`)
  // 旧侧确认停住之后才清停用标记：中途失败时配置还指着旧栈，共用服务不会误把它当新系统的 bot
  actions.push({ kind: 'set-enabled', on: true })
  actions.push({ kind: 'install', what: 'gateway' }, { kind: 'install', what: 'jobs' })

  const back = `bun gateway/scripts/switch.ts back ${ctx.bot}`
  const todo = actions.map(a => describeAction(a, ctx, deps))
  if (opts.dryRun) {
    p('')
    p('（--dry-run：下面这些一步都不执行，只打印）')
    for (const t of todo) p(`  · ${t}`)
    p('  然后：等网关就绪（心跳 + 日志里的 gateway.ready），打印抽查结果')
    p('')
    p(`下一步（真机抽查）：给 ${ctx.bot} 发一条消息；bun gateway/scripts/autostart.ts status；bun gateway/scripts/logs.ts --config ${ctx.configPath} -f`)
    p(`群聊（用到的话）：先停旧导演，再 bun gateway/scripts/autostart.ts install-director --chat <群 id>`)
    p(`怎么退回去：${back}（这次没动任何东西）`)
    return 0
  }

  const done: string[] = []
  if (old.running) {
    const r = execAction(actions[0]!, ctx, deps)
    if (!r.ok) {
      p(`⛔ ${r.note}`)
      report(deps, done, todo, `bash ~/.claude/dispatcher/start-bot.sh ${ctx.oldName}`)
      return 1
    }
    done.push(r.note)
    p(`  ✓ ${r.note}`)
    // 确认停住才往下走：少了停用标记，旧看门狗 5 分钟内会把它拉回来
    const after = probeOld(ctx, deps)
    if (!after.stopped) {
      p('⛔ 旧侧没能确认停住，不敢起新侧（同一个令牌会打架）。')
      p(`   ${describeOld(after)}`)
      p(`   手动收尾：bash ~/.claude/dispatcher/stop-bot.sh ${ctx.oldName}，确认 tg-${ctx.oldName} 会话和端口都没了，再重跑本命令。`)
      report(deps, done, todo, `bash ~/.claude/dispatcher/start-bot.sh ${ctx.oldName}`)
      return 1
    }
    done.push('确认旧侧已停（停用标记在、tmux 会话没了、端口关了）')
    p(`  ✓ ${done[done.length - 1]}`)
  }
  for (const a of actions.filter(isSetEnabled)) applySetEnabled(a, ctx, deps, done, p)
  for (const a of actions.filter(x => x.kind === 'install')) {
    const r = execAction(a, ctx, deps)
    if (!r.ok) {
      p(`⛔ ${r.note}`)
      report(deps, done, todo.filter(t => t !== describeAction(a, ctx, deps)), back)
      return 1
    }
    done.push(r.note)
    p(`  ✓ ${r.note}`)
  }
  const ready = deps.waitNewReady(opts.waitSeconds)
  p(ready.ok ? `  ✓ 新侧网关就绪：${ready.note}` : `  ⚠️ 等了 ${opts.waitSeconds} 秒还没确认网关就绪：${ready.note}`)
  p('')
  p(`下一步（真机抽查）：`)
  p(`  · 给 ${ctx.bot} 发一条消息，看有没有回`)
  p(`  · bun gateway/scripts/autostart.ts status`)
  p(`  · bun gateway/scripts/logs.ts --config ${ctx.configPath} -f`)
  p(`  · bun gateway/scripts/health.ts --config ${ctx.configPath}`)
  p(`  · 旧记录还没导（可选，推荐先试算）：python3 scripts/import_history.py ${ctx.bot} --from ${ctx.oldChannelDir} --old-bot ${ctx.oldName} --dry-run`)
  p(`群聊（用到的话）：先停旧导演，再 bun gateway/scripts/autostart.ts install-director --chat <群 id>`)
  p(`怎么退回去：${back}`)
  return ready.ok ? 0 : 1
}

export function switchBack(ctx: Ctx, deps: SwitchDeps, opts: { dryRun: boolean; waitSeconds: number }): number {
  const p = deps.print
  p(`切换 ${ctx.bot}：新系统 → 旧系统（旧名 ${ctx.oldName}）`)
  const old = probeOld(ctx, deps)
  const nw = probeNew(ctx, deps)
  p(`  ${describeNew(nw)}`)
  p(`  ${describeOld(old)}`)
  if (!old.disabled && old.running) p(`  · 旧侧本来就在跑：start-bot.sh 会把所有旧 bot 重启一遍（本来就幂等）`)

  if (nw.running && !nw.gatewayInstalled) {
    p(`⛔ 新侧 ${ctx.bot} 的心跳还新鲜，但它不是 launchd 起的（多半是手动跑的）。`)
    p('   本脚本不会去杀别人的进程：请在那个终端里 Ctrl-C，或先 bun gateway/scripts/autostart.ts install <配置文件> 让它归 launchd 管，')
    p('   确认心跳停了再重跑本命令。这次什么都没做（没停旧侧，也没动数据）。')
    return 1
  }

  const payload = deps.readLedger()
  const actions: Action[] = []
  // 装了就要 bootout：plist 挂在 launchd 里，KeepAlive 随时会把它拉回来（哪怕此刻没在跑）
  if (nw.gatewayInstalled) actions.push({ kind: 'bootout', label: gatewayLabel(ctx.bot), what: '网关' })
  else p(`  · 新侧网关没装（launchd 里没有这项），跳过 bootout`)
  if (nw.jobsInstalled) actions.push({ kind: 'bootout', label: jobsLabel(ctx.bot), what: '主动消息任务' })
  if (nw.directorInstalled) {
    const others = otherNewBotsRunning(ctx, deps)
    if (others.length) p(`  · 导演（所有新系统 bot 共用的）没停：新系统里还有 ${others.join('、')} 在跑，群聊还要用它`)
    else actions.push({ kind: 'bootout', label: DIRECTOR_LABEL, what: '群聊导演' })
  }
  actions.push({ kind: 'set-enabled', on: false }, { kind: 'carry-back', payload }, { kind: 'start-old' })

  const back = `bun gateway/scripts/switch.ts to-dsh ${ctx.bot}`
  const todo = actions.map(a => describeAction(a, ctx, deps))
  if (opts.dryRun) {
    p('')
    p('（--dry-run：下面这些一步都不执行，只打印）')
    for (const t of todo) p(`  · ${t}`)
    p(`  然后：等旧侧起来（tmux 会话 / dispatcher 端口），打印结果`)
    p('')
    p(`带回旧侧的东西：摘要 ${payload.summary ? '1 份' : '没有'}、原话 ${payload.quotes.length} 条、长期记忆 ${payload.memories.length} 条、没到点的承诺 ${payload.pending.length} 条（只读账本，什么都没写）`)
    p(`怎么退回去：${back}（这次没动任何东西）`)
    return 0
  }

  const done: string[] = []
  for (const a of actions.filter(x => x.kind === 'bootout')) {
    const r = execAction(a, ctx, deps)
    if (!r.ok) {
      p(`⛔ ${r.note}`)
      report(deps, done, todo, back)
      return 1
    }
    done.push(r.note)
    p(`  ✓ ${r.note}`)
  }
  // 确认新侧停了才敢起旧侧：心跳停了、launchd 也不说在跑
  const gone = deps.waitFor(() => { const s = probeNew(ctx, deps); return !s.running && !s.gatewayRunning }, 20)
  if (!gone) {
    const now = probeNew(ctx, deps)
    p(`⛔ 停完 launchd 之后新侧还在活着（心跳${now.heartbeatFresh ? '还新鲜' : '不新鲜'}${now.pid ? `，pid ${now.pid}` : ''}），不敢起旧侧。`)
    p(`   手动收尾：kill ${now.pid ?? '<网关的 pid>'}（或看 bun gateway/scripts/logs.ts --config ${ctx.configPath} -f 找原因），确认停了再重跑。`)
    report(deps, done, todo, back)
    return 1
  }
  done.push('确认新侧已停（launchd 不认在跑，心跳也停了）')
  p(`  ✓ ${done[done.length - 1]}`)
  // 新侧停住了才标停用：共用服务（jiwen、memory-compactor）和管理台按这行认"谁是新系统的 bot"
  for (const a of actions.filter(isSetEnabled)) applySetEnabled(a, ctx, deps, done, p)
  for (const a of actions.filter(x => x.kind === 'carry-back' || x.kind === 'start-old')) {
    const r = execAction(a, ctx, deps)
    if (!r.ok) {
      if (a.kind === 'carry-back') { for (const n of r.note.split('；')) p(`  ⚠️  ${n}`); p('  · 带回来的东西没写全；旧侧照常起回来，缺的可以对照账本手工补'); done.push(`带回旧侧：部分失败（${r.note}）`); continue }
      p(`⛔ ${r.note}`)
      report(deps, done, todo.filter(t => t !== describeAction(a, ctx, deps)), back)
      return 1
    }
    done.push(r.note)
    if (a.kind === 'start-old') p(`  ✓ ${r.note}`)
    else for (const n of r.note.split('；')) p(`  · ${n}`)
  }
  const up = deps.waitFor(() => probeOld(ctx, deps).running, opts.waitSeconds)
  const after = probeOld(ctx, deps)
  p(up ? `  ✓ 旧侧 ${ctx.oldName} 起来了（${after.sessions.length} 个 tmux 会话）` : `  ⚠️ 等了 ${opts.waitSeconds} 秒还没看到旧侧 ${ctx.oldName} 起来的痕迹`)
  p('')
  p('下一步（真机抽查）：')
  p(`  · 给 ${ctx.oldName} 发一条消息，看有没有回（旧侧 tmux：TMUX_TMPDIR=/tmp tmux ls）`)
  p(`  · 旧侧日志：tail -f /tmp/bots-watchdog.log；端口探活：nc -z 127.0.0.1 ${ctx.ports[ctx.oldName] ?? '<旧 bot 的端口>'}`)
  p(`  · 回新系统：${back}`)
  return up ? 0 : 1
}

/* ── 真实文件系统（命令行用；单测也可以用它在临时目录里跑真写入） ── */

export function nodeSwitchFs(): SwitchFs {
  return {
    exists: (p) => { try { return statSync(p) !== null } catch { return false } },
    read: (p) => { try { return readFileSync(p, 'utf8') } catch { return null } },
    write: (p, text, mode) => {
      mkdirSync(dirname(p), { recursive: true })
      // 没显式给权限、而目标文件已经存在：沿用它的权限——只改内容，不顺手把文件权限也改掉
      if (mode === undefined) { try { mode = statSync(p).mode & 0o777 } catch {} }
      const tmp = `${p}.tmp-${process.pid}`
      try {
        try { rmSync(tmp, { force: true }) } catch {}
        const fd = openSync(tmp, 'wx', mode ?? 0o666)
        try { writeFileSync(fd, text) } finally { closeSync(fd) }
        if (mode !== undefined && process.platform !== 'win32') chmodSync(tmp, mode)
        renameSync(tmp, p)
      } catch (e) {
        try { rmSync(tmp, { force: true }) } catch {}
        throw e
      }
    },
    mkdirp: (p) => { mkdirSync(p, { recursive: true }) },
    mtimeMs: (p) => { try { return statSync(p).mtimeMs } catch { return null } },
    list: (p) => { try { return readdirSync(p) } catch { return [] } },
  }
}
