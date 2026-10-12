// 真 dsh + 真模型 + 假 Telegram + 真网关：量一个"自愿型"工具在真模型手里会不会被自发调用（PLAN-表情包接线 第 2 节的 V1）。
// 参数化通用件：给触发语料、对照语料、工具名、通过阈值。两组语料各起一套全新的 bot（同一份人设、技能、模型，
// 两组互不串上下文），组内逐条发，等这一条处理完再发下一条。
// 只记结构化信号（调没调、几次、哪一档、耗时、token 数、有没有加载技能）和打码截短后的技术报错，不记模型说的任何一句话，也不记语料原文。
// 本地人工跑，不进 CI：会真的花模型的钱。
//
// 密钥怎么走：只从环境变量读（名字由 --key-env 指定），留在本进程内存里。dsh 读到的凭据文件里只有一把随机占位密钥，
// 请求经本机转发时才换成真的（lib/usage-tap 的 rewriteHeaders）。真密钥不落盘，网关和 dsh 的进程、日志里都没有它。
//
// 隔离：网关的主目录换成临时目录（--home-link 把真实主目录里要用的东西只读链接进去）；不起任何 Python、不生图、不连语音服务；
// 临时目录建在 $TMPDIR 下，跑完连同里面的账本和对话日志一起删掉。
//
// 用法（表情包这一批的形状）：
//   DSH_BOT_HARNESS=<装了 dsh 的目录> <密钥环境变量名>=... bun lab/dsh/tools/verify-tool-usage.ts \
//     --tool sticker --threshold 2 --effect-event tool.sticker_send --tally tier --watch-skill skill-gif \
//     --trigger <一句> [--trigger <一句> ...] --control <一句> [--control <一句> ...] \
//     --persona <人设文件> --skills <技能目录> \
//     --base-url <上游地址，如 https://主机/v1> --model <模型 id> --effort low --context-window 1000000 --key-env <密钥环境变量名> \
//     --home-link .claude/channels/_shared/stickers.json --home-link resource/stickers [--out <结果 JSON>]
// 退出码：0 通过；1 不通过或降级；2 参数或环境不对；3 跑的过程中出错。
import { randomBytes } from 'crypto'
import { chmodSync, cpSync, existsSync, mkdirSync, readFileSync, statSync, symlinkSync, unlinkSync, writeFileSync } from 'fs'
import { homedir } from 'os'
import { dirname, isAbsolute, join, normalize, resolve } from 'path'
import { isAlive, processCommandLine } from '../../../gateway/src/dsh/process'
import { toolName } from '../../../gateway/src/dsh/profile'
import { redact, registerSecret } from '../../../gateway/src/log'
import { FakeTelegram } from '../../../gateway/test/fakes/fake-telegram'
import { cleanup, Gateway, makeBot, OWNER, until, writeConfig, type BotEnv } from '../../../gateway/test/harness'
import { startUsageTap, type Tap } from '../lib/usage-tap'

// ─── 参数 ───

export type Group = 'trigger' | 'control'
const GROUP_NAME: Record<Group, string> = { trigger: '触发', control: '对照' }
const EFFORTS = ['off', 'low', 'high', 'max']

export type Args = {
  tool: string
  trigger: string[]
  control: string[]
  threshold: number
  persona: string
  skills?: string
  baseUrl: string
  api: string
  model: string
  effort?: string
  contextWindow?: number
  keyEnv: string
  /** 网关日志里算"这次调用真的生效了"的事件名（表情包是 tool.sticker_send）；不给就按模型发出的调用数算 */
  effectEvent?: string
  /** 顺带记一下这个技能有没有被加载 */
  watchSkill?: string
  /** 把生效事件按这个字段的值分别计数（表情包是 tier） */
  tally?: string
  /** 真实主目录里要只读链接进临时主目录的相对路径 */
  homeLinks: string[]
  turnTimeoutMs: number
  out?: string
}

export class UsageError extends Error {
  override name = 'UsageError'
}

const REPEATABLE = new Set(['trigger', 'control', 'home-link'])
const SINGLE = new Set(['tool', 'threshold', 'persona', 'skills', 'base-url', 'api', 'model', 'effort', 'context-window', 'key-env', 'effect-event', 'watch-skill', 'tally', 'turn-timeout-s', 'out'])

export function parseArgs(argv: string[]): Args {
  const one = new Map<string, string>()
  const many = new Map<string, string[]>()
  for (let i = 0; i < argv.length; i += 2) {
    const flag = argv[i]!
    const name = flag.startsWith('--') ? flag.slice(2) : ''
    if (!REPEATABLE.has(name) && !SINGLE.has(name)) throw new UsageError(`不认识的参数：${flag}`)
    const value = argv[i + 1]
    if (value === undefined || value.startsWith('--')) throw new UsageError(`${flag} 后面要跟一个值`)
    if (REPEATABLE.has(name)) many.set(name, [...(many.get(name) ?? []), value])
    else if (one.has(name)) throw new UsageError(`${flag} 只能给一次`)
    else one.set(name, value)
  }
  const need = (name: string): string => {
    const v = one.get(name)?.trim()
    if (!v) throw new UsageError(`缺少 --${name}`)
    return v
  }
  const posInt = (name: string, raw: string): number => {
    const n = Number(raw)
    if (!Number.isInteger(n) || n <= 0) throw new UsageError(`--${name} 必须是正整数`)
    return n
  }
  // 语料：去掉首尾空白；空的不要；以 / 开头的会被网关当成主人命令，根本到不了模型
  const corpus = (name: string): string[] => (many.get(name) ?? []).map(s => s.trim()).map(s => {
    if (!s) throw new UsageError(`--${name} 不能是空的`)
    if (s.startsWith('/')) throw new UsageError(`--${name} 不能以 / 开头（会被当成命令）：${s.slice(0, 20)}`)
    return s
  })

  const tool = need('tool')
  if (!/^[a-z][a-z0-9_]*$/.test(tool)) throw new UsageError('--tool 写工具的短名（小写字母、数字、下划线），比如 sticker')
  const trigger = corpus('trigger')
  if (trigger.length === 0) throw new UsageError('至少给一条 --trigger')
  const baseUrl = need('base-url')
  let parsedUrl: URL
  try { parsedUrl = new URL(baseUrl) } catch { throw new UsageError('--base-url 不是合法的地址') }
  if (!['http:', 'https:'].includes(parsedUrl.protocol)) throw new UsageError('--base-url 只能是 http 或 https')
  const api = one.get('api')?.trim() || 'openai-completions'
  // 工具调用是从模型的响应里解析的，目前只写了 OpenAI Chat Completions 这一种协议
  if (api !== 'openai-completions') throw new UsageError('--api 目前只支持 openai-completions')
  const effort = one.get('effort')?.trim()
  if (effort !== undefined && !EFFORTS.includes(effort)) throw new UsageError('--effort 只能是 off、low、high、max')
  const keyEnv = one.get('key-env')?.trim() || 'DEEPSEEK_API_KEY'
  if (!/^[A-Z][A-Z0-9_]*$/.test(keyEnv)) throw new UsageError('--key-env 写环境变量的名字（大写字母、数字、下划线）')
  const homeLinks = (many.get('home-link') ?? []).map(p => {
    const rel = normalize(p.trim())
    if (!rel || rel === '.' || isAbsolute(rel) || rel.split(/[\\/]/).includes('..')) throw new UsageError(`--home-link 要写主目录下的相对路径：${p}`)
    return rel
  })
  const effectEvent = one.get('effect-event')?.trim()
  const tally = one.get('tally')?.trim()
  if (tally && !effectEvent) throw new UsageError('--tally 要和 --effect-event 一起用（按生效事件的字段计数）')
  const contextWindow = one.get('context-window')
  const timeoutS = one.get('turn-timeout-s')
  return {
    tool, trigger, control: corpus('control'),
    threshold: posInt('threshold', need('threshold')),
    persona: resolve(need('persona')),
    skills: one.get('skills') ? resolve(one.get('skills')!) : undefined,
    baseUrl, api, model: need('model'), effort,
    contextWindow: contextWindow === undefined ? undefined : posInt('context-window', contextWindow),
    keyEnv,
    effectEvent: effectEvent || undefined,
    watchSkill: one.get('watch-skill')?.trim() || undefined,
    tally: tally || undefined,
    homeLinks,
    // 默认 15 分钟：网关自己的"卡住取消 + 重试两次"最坏约 10 分钟，要等它走完
    turnTimeoutMs: (timeoutS === undefined ? 900 : posInt('turn-timeout-s', timeoutS)) * 1000,
    out: one.get('out') ? resolve(one.get('out')!) : undefined,
  }
}

// ─── 从模型的响应里取工具调用（OpenAI Chat Completions） ───

export type ModelCall = { name: string; args: string }

/** args 是模型给的 JSON 原文，只在本进程里用来取几个短参数，不往外写 */
export function parseToolCalls(body: string): ModelCall[] {
  const argText = (v: unknown): string => typeof v === 'string' ? v : v && typeof v === 'object' ? JSON.stringify(v) : ''
  const trimmed = body.trim()
  if (trimmed.startsWith('{')) {
    // 非流式：每一项就是一次完整的调用
    let list: unknown
    try { list = JSON.parse(trimmed)?.choices?.[0]?.message?.tool_calls } catch { return [] }
    return (Array.isArray(list) ? list : [])
      .map((tc: any) => ({ name: typeof tc?.function?.name === 'string' ? tc.function.name : '', args: argText(tc?.function?.arguments) }))
      .filter(c => c.name)
  }
  const calls: { id: string; name: string; args: string }[] = []
  const slot = new Map<number, number>() // 流里的 index → calls 的下标
  for (const line of body.split('\n')) {
    if (!line.startsWith('data:')) continue
    const data = line.slice(5).trim()
    if (!data || data === '[DONE]') continue
    let list: unknown
    try { list = JSON.parse(data)?.choices?.[0]?.delta?.tool_calls } catch { continue }
    if (!Array.isArray(list)) continue
    for (const tc of list as any[]) {
      const index = typeof tc?.index === 'number' ? tc.index : 0
      const id = typeof tc?.id === 'string' ? tc.id : ''
      const name = typeof tc?.function?.name === 'string' ? tc.function.name : ''
      let at = slot.get(index)
      const cur = at === undefined ? undefined : calls[at]
      // 规范的流里，一次调用只有头一片带 id 和 name，后面的片只续 arguments。有的转接服务把并行的几次调用都报成 index 0：
      // 换了 id，或者没有 id 却又带了一次 name，都当成另一次调用，不然两次调用会被拼成一次
      if (!cur || (id && cur.id && id !== cur.id) || (!id && name && cur.name)) {
        at = calls.push({ id, name: '', args: '' }) - 1
        slot.set(index, at)
      }
      const c = calls[at!]!
      if (id && !c.id) c.id = id
      if (name && !c.name) c.name = name
      c.args += argText(tc?.function?.arguments)
    }
  }
  return calls.filter(c => c.name).map(({ name, args }) => ({ name, args }))
}

/** 只留"看得出怎么调的"短参数，而且值得是短标识符。其余一概不要：reply 的 text 之类是模型说的话，不能记 */
const SAFE_ARG_KEYS = ['action', 'tier', 'id', 'name']
export function safeArgs(rawJson: string): Record<string, string> {
  let o: unknown
  try { o = JSON.parse(rawJson || '{}') } catch { return {} }
  const out: Record<string, string> = {}
  if (!o || typeof o !== 'object') return out
  for (const k of SAFE_ARG_KEYS) {
    const v = (o as Record<string, unknown>)[k]
    if (typeof v === 'string' && /^[A-Za-z0-9:_-]{1,40}$/.test(v)) out[k] = v
  }
  return out
}

/** 把请求头里的占位密钥换成真密钥，返回换了几处。头里没有占位密钥的请求不是我们的 dsh 发的，不给它真密钥 */
export function swapKey(headers: Headers, placeholder: string, real: string): number {
  let n = 0
  for (const [k, v] of [...headers.entries()]) {
    if (!v.includes(placeholder)) continue
    headers.set(k, v.split(placeholder).join(real))
    n++
  }
  return n
}

// ─── 一条语料的记录 ───

export type LogLine = { event?: string; [k: string]: unknown }
/** 一次发给模型的请求：HTTP 状态、上游回报的用量、这次响应里模型发出的工具调用 */
export type Exchange = { status: number; input?: number; output?: number; calls: ModelCall[] }
type Scalar = string | number | boolean | null

export type TurnInput = {
  group: Group
  /** 组内序号，从 1 起 */
  index: number
  rerun: boolean
  ms: number
  /** 账本里这条消息的终态（done / dead / dropped）；等不到就是 timeout */
  inboundState: string
  /** 这一条处理期间网关新写的日志行 */
  logs: LogLine[]
  exchanges: Exchange[]
  /** 这一条处理期间，账本里各轮记下的报错原文（dsh 或上游给的技术报错，不是对话） */
  errors: string[]
}

export type TurnRecord = {
  group: Group
  index: number
  rerun: boolean
  ms: number
  inboundState: string
  /** 网关依次跑了哪几种轮（message / nudge 催一次 / retry 重试）和各自的结局 */
  turnKinds: string[]
  outcomes: string[]
  delivered: number
  silent: boolean
  requests: number
  /** HTTP 状态不是 2xx 的请求数。上游回 200、内容却被 dsh 判成失败的（比如空回复）不在这里，看 errors */
  upstreamErrors: number
  /** 各轮的报错摘要（打码后截到 160 字）。回复送达之后才报错的条目照样算有效，但报错要留痕 */
  errors: string[]
  tokens: { input: number | null; output: number | null }
  /** 模型侧：这一条里模型对目标工具发出的每次调用（只留短参数） */
  calls: Record<string, string>[]
  /** 模型侧：还调了哪些别的工具，各几次 */
  otherTools: Record<string, number>
  /** 网关侧：目标工具留下的日志事件，各几次 */
  gatewayEvents: Record<string, number>
  /** 网关侧：生效事件的字段（给了 --effect-event 才有） */
  effects: Record<string, Scalar>[]
  /** 判据用的生效次数：给了 --effect-event 就是它的条数，否则是模型侧调用数 */
  hits: number
  skillLoaded: boolean
  valid: boolean
  invalidReason?: string
}

export type TurnOpts = { tool: string; effectEvent?: string; watchSkill?: string }

const LOG_META = new Set(['at', 'level', 'event', 'bot'])

export function buildTurn(input: TurnInput, o: TurnOpts): TurnRecord {
  const full = toolName(o.tool)
  const modelCalls = input.exchanges.flatMap(e => e.calls)
  const calls = modelCalls.filter(c => c.name === full).map(c => safeArgs(c.args))
  const otherTools: Record<string, number> = {}
  for (const c of modelCalls) {
    if (c.name === full) continue
    const short = c.name.replace(/^mcp__tg__/, '')
    otherTools[short] = (otherTools[short] ?? 0) + 1
  }
  // 事件名要么正好是 tool.<工具>，要么是 tool.<工具>_xxx：只按前缀比会把 tool.re 算到 tool.reply 头上
  const mine = (ev: string) => ev === `tool.${o.tool}` || ev.startsWith(`tool.${o.tool}_`)
  const gatewayEvents: Record<string, number> = {}
  const effects: Record<string, Scalar>[] = []
  for (const l of input.logs) {
    const ev = typeof l.event === 'string' ? l.event : ''
    if (mine(ev)) gatewayEvents[ev] = (gatewayEvents[ev] ?? 0) + 1
    if (o.effectEvent && ev === o.effectEvent) {
      const fields: Record<string, Scalar> = {}
      for (const [k, v] of Object.entries(l)) {
        if (!LOG_META.has(k) && (v === null || ['string', 'number', 'boolean'].includes(typeof v))) fields[k] = v as Scalar
      }
      effects.push(fields)
    }
  }
  const ends = input.logs.filter(l => l.event === 'turn.end')
  const last = ends.at(-1)
  const delivered = typeof last?.delivered === 'number' ? last.delivered : 0
  const silent = last?.silent === true
  // 有效 = 网关把这条消息正常结了案，而且对方确实收到了东西（文字或表情包都算），或者模型按人设明确选择了不回
  const valid = input.inboundState === 'done' && (delivered > 0 || silent)
  const invalidReason = valid ? undefined
    : input.inboundState === 'timeout' ? '等待超时'
    : input.inboundState === 'dead' ? '网关放弃了这条（重试用尽或上游拒绝）'
    : input.inboundState === 'done' ? '空回复（模型既没回话也没选择不回）'
    : `消息终态是 ${input.inboundState}`
  const sum = (pick: (e: Exchange) => number | undefined): number | null => {
    const got = input.exchanges.map(pick).filter((n): n is number => typeof n === 'number')
    return got.length ? got.reduce((a, b) => a + b, 0) : null
  }
  return {
    group: input.group, index: input.index, rerun: input.rerun, ms: input.ms, inboundState: input.inboundState,
    turnKinds: ends.map(l => String(l.kind ?? '')),
    outcomes: ends.map(l => String(l.outcome ?? '')),
    delivered, silent,
    requests: input.exchanges.length,
    upstreamErrors: input.exchanges.filter(e => e.status < 200 || e.status >= 300).length,
    // 先打码再截断：反过来的话半截密钥会漏在截断点前面
    errors: input.errors.map(e => redact(e).replace(/\s+/g, ' ').trim().slice(0, 160)),
    tokens: { input: sum(e => e.input), output: sum(e => e.output) },
    calls, otherTools, gatewayEvents, effects,
    hits: o.effectEvent ? effects.length : calls.length,
    skillLoaded: !!o.watchSkill && modelCalls.some(c => c.name === 'skill' && safeArgs(c.args).name === o.watchSkill),
    valid,
    ...(invalidReason ? { invalidReason } : {}),
  }
}

// ─── 判据与汇总 ───

export type Verdict = '通过' | '降级' | '不通过'

/** 触发组命中的条数够阈值才算过；过了但对照组也出现调用，结论降级（和"不分场合乱调"区分不开） */
export function judge(o: { hitTurns: number; controlCalls: number; threshold: number }): Verdict {
  if (o.hitTurns < o.threshold) return '不通过'
  return o.controlCalls > 0 ? '降级' : '通过'
}

export type GroupStat = {
  total: number
  /** 有有效结果的条数（原跑无效但补跑有效的算补跑那次） */
  valid: number
  /** 有效结果里，至少生效一次的条数：判据按它算（一条里连发两次不算两条证据） */
  hitTurns: number
  /** 有效结果里的生效总次数 */
  hits: number
  /** 这一组全部记录（含无效的和补跑的）里目标工具被调用的次数：模型侧与网关侧取大的 */
  calls: number
}

export type Summary = {
  tool: string
  threshold: number
  trigger: GroupStat
  control: GroupStat
  /** 一条里生效两次及以上的条数 */
  multiPerTurn: number
  tally: Record<string, number>
  skillLoads: number
  /** 有效、但处理过程中报过错的条数（典型的是回复已经送达、这一轮收尾时才报错） */
  erroredValid: number
  invalid: { group: Group; index: number; reason: string; rerun: '未补跑' | '补跑有效' | '补跑仍无效' }[]
  /** 网关那边看得到动作（目标工具有事件，或对方收到了消息）、模型侧却没解析到对应调用的条数：不是 0 说明响应解析漏了，模型侧的数字不可信 */
  parseGaps: number
  verdict: Verdict
  notes: string[]
}

const gatewayTotal = (r: TurnRecord): number => Object.values(r.gatewayEvents).reduce((a, b) => a + b, 0)
const callsSeen = (r: TurnRecord): number => Math.max(r.calls.length, gatewayTotal(r))

export function summarize(records: TurnRecord[], o: { tool: string; threshold: number; totals: Record<Group, number>; tally?: string }): Summary {
  const invalid: Summary['invalid'] = []
  const stat = (group: Group): GroupStat => {
    const mine = records.filter(r => r.group === group)
    const s: GroupStat = { total: o.totals[group], valid: 0, hitTurns: 0, hits: 0, calls: mine.reduce((a, r) => a + callsSeen(r), 0) }
    for (let index = 1; index <= s.total; index++) {
      const first = mine.find(r => r.index === index && !r.rerun)
      const again = mine.find(r => r.index === index && r.rerun)
      const final = first?.valid ? first : again?.valid ? again : undefined
      if (!first?.valid) {
        invalid.push({
          group, index,
          reason: first ? (first.invalidReason ?? '无效') : '没跑到（本组中途中止）',
          rerun: !again ? '未补跑' : again.valid ? '补跑有效' : '补跑仍无效',
        })
      }
      if (!final) continue
      s.valid++
      s.hits += final.hits
      if (final.hits > 0) s.hitTurns++
    }
    return s
  }
  const trigger = stat('trigger')
  const control = stat('control')
  const tally: Record<string, number> = {}
  if (o.tally) for (const r of records) for (const e of r.effects) {
    const k = String(e[o.tally] ?? '（无）')
    tally[k] = (tally[k] ?? 0) + 1
  }
  // 对方收到消息只有两条路：reply 或目标工具自己发。收到了、模型侧却一次工具调用都没有，只能是解析漏了
  const blind = (r: TurnRecord) => r.delivered > 0 && r.calls.length === 0 && Object.keys(r.otherTools).length === 0
  const parseGaps = records.filter(r => (gatewayTotal(r) > 0 && r.calls.length === 0) || blind(r)).length
  const notes: string[] = []
  if (trigger.valid < trigger.total) notes.push(`触发组 ${trigger.total} 条里只有 ${trigger.valid} 条有有效结果，样本比方案规定的少。`)
  if (control.valid < control.total) notes.push(`对照组 ${control.total} 条里只有 ${control.valid} 条有有效结果，对照不完整。`)
  if (trigger.hits >= o.threshold && trigger.hitTurns < o.threshold) notes.push(`触发组生效 ${trigger.hits} 次，但集中在 ${trigger.hitTurns} 条里；判据按条数算，所以不过。`)
  if (parseGaps > 0) notes.push(`有 ${parseGaps} 条网关那边看得到动作、模型侧没解析到调用：模型侧的数字（含技能加载）偏小，调用次数以网关侧为准。`)
  const erroredValid = records.filter(r => r.valid && r.errors.length > 0).length
  if (erroredValid > 0) notes.push(`有 ${erroredValid} 条有效但报过错：对方收到了回复，这一条处理过程中仍有轮以报错收尾，报错摘要见各条。`)
  return {
    tool: o.tool, threshold: o.threshold, trigger, control,
    multiPerTurn: records.filter(r => r.hits >= 2).length,
    tally,
    skillLoads: records.filter(r => r.skillLoaded).length,
    erroredValid, invalid, parseGaps,
    verdict: judge({ hitTurns: trigger.hitTurns, controlCalls: control.calls, threshold: o.threshold }),
    notes,
  }
}

// ─── 输出（往外写的每一个字都过 redact：登记过的密钥、令牌先换掉，再套网关日志那套通用规则） ───

export type RunMeta = { model: string; api: string; upstreamHost: string; effort: string | null; dsh: string; startedAt: string; unkeyedRequests: number }
export type Result = { meta: RunMeta; records: TurnRecord[]; summary: Summary }

export function toJson(result: Result): string {
  return redact(JSON.stringify(result, null, 1))
}

export function renderReport(result: Result, tallyField?: string): string {
  const { meta, records, summary: s } = result
  const lines: string[] = [
    `工具 ${s.tool} · 模型 ${meta.model} · 上游 ${meta.upstreamHost}（${meta.api}）· 思考 ${meta.effort ?? '未设'} · dsh ${meta.dsh} · 阈值 ${s.threshold}`,
    '',
    '| 组别 | 序号 | 补跑 | 有效 | 模型侧调用 | 生效次数 | 明细 | 加载技能 | 耗时(秒) | 请求数 | 上游报错 | token 入/出 |',
    '|---|---|---|---|---|---|---|---|---|---|---|---|',
  ]
  for (const r of records) {
    const detail = tallyField && r.effects.length ? r.effects.map(e => String(e[tallyField] ?? '?')).join(',') : '-'
    lines.push(`| ${GROUP_NAME[r.group]} | ${r.index} | ${r.rerun ? '是' : '否'} | ${r.valid ? '是' : `否（${r.invalidReason}）`} | ${r.calls.length} | ${r.hits} | ${detail} | ${r.skillLoaded ? '是' : '否'} | ${(r.ms / 1000).toFixed(1)} | ${r.requests} | ${r.upstreamErrors} | ${r.tokens.input ?? '?'}/${r.tokens.output ?? '?'} |`)
  }
  lines.push(
    '',
    `触发组：${s.trigger.total} 条，有效 ${s.trigger.valid} 条，命中 ${s.trigger.hitTurns} 条（生效 ${s.trigger.hits} 次），阈值 ${s.threshold}`,
    `对照组：${s.control.total} 条，有效 ${s.control.valid} 条，出现调用 ${s.control.calls} 次（其中生效 ${s.control.hits} 次）`,
    `结论：${s.verdict}`,
    `旁证：一条里生效多次 ${s.multiPerTurn} 条；技能加载 ${s.skillLoads} 条${tallyField ? `；按 ${tallyField} 计 ${Object.entries(s.tally).map(([k, n]) => `${k}=${n}`).join('、') || '无'}` : ''}`,
  )
  for (const i of s.invalid) lines.push(`无效：${GROUP_NAME[i.group]}组第 ${i.index} 条，${i.reason}；${i.rerun}`)
  for (const r of records) for (const e of r.errors) lines.push(`报错：${GROUP_NAME[r.group]}组第 ${r.index} 条${r.rerun ? '（补跑）' : ''}，${e}`)
  for (const n of s.notes) lines.push(`注意：${n}`)
  if (meta.unkeyedRequests > 0) lines.push(`注意：有 ${meta.unkeyedRequests} 个请求的头里没有占位密钥，没给它真密钥。`)
  return redact(lines.join('\n'))
}

// ─── 真跑 ───

const TERMINAL = new Set(['done', 'dead', 'dropped'])
const DAY_MS = 86_400_000
/** 凭据文件里占位密钥的名字（dsh 路由的 apiKeyEnv 指它） */
const KEY_REF = 'VERIFY_UPSTREAM_KEY'

type Live = { gw: Gateway; b: BotEnv; tg: FakeTelegram; links: string[] }
let live: Live | null = null

/** 网关正常退出会带走 dsh。万一网关是被强杀的，按 pid 文件把留下的 dsh 收掉：命令行里有本次的临时目录才杀，防 pid 被别的进程复用 */
async function reapDsh(b: BotEnv): Promise<void> {
  try {
    const pid = Number(JSON.parse(readFileSync(join(b.botDir, 'state', 'dsh.pid'), 'utf8')).pid)
    if (pid > 0 && isAlive(pid) && (await processCommandLine(pid))?.includes(b.root)) process.kill(pid, 'SIGKILL')
  } catch {}
}

async function teardown(env: Live, hard = false): Promise<void> {
  await (hard ? env.gw.kill() : env.gw.stop()).catch(() => {})
  await reapDsh(env.b)
  env.tg.stop()
  // 先只摘掉链接，再删临时目录：绝不顺着链接删到真实主目录里的东西
  for (const l of env.links) { try { unlinkSync(l) } catch {} }
  cleanup(env.b)
}

function readLogLines(file: string, fromByte: number): LogLine[] {
  let buf: Buffer
  try { buf = readFileSync(file) } catch { return [] }
  return buf.subarray(fromByte).toString('utf8').split('\n').filter(Boolean).flatMap(l => {
    try { return [JSON.parse(l) as LogLine] } catch { return [] }
  })
}

type Ctx = { args: Args; persona: string; harness: string; placeholder: string; tap: Tap; callsByReq: Map<number, ModelCall[]> }

/** 中途出错不抛：已经跑出来的记录照样交回去（真跑是花了钱的，不能因为后半截出错把前半截丢掉） */
async function runGroup(group: Group, corpus: string[], ctx: Ctx): Promise<{ records: TurnRecord[]; error?: string }> {
  const { args, tap } = ctx
  if (corpus.length === 0) return { records: [] }
  const upstream = new URL(args.baseUrl)
  const tg = new FakeTelegram()
  const b = makeBot(tg, {
    name: `verify-${group}`,
    brain: {
      provider: 'verify', model: args.model, reasoning_effort: args.effort ?? null,
      routes: { verify: {
        api: args.api,
        // 路径前缀照抄上游的（比如 /v1），本机转发时原样拼回上游主机
        baseURL: `http://127.0.0.1:${tap.port}${upstream.pathname.replace(/\/+$/, '')}`,
        apiKeyEnv: KEY_REF,
        models: [{ id: args.model, ...(args.contextWindow ? { contextWindow: args.contextWindow } : {}) }],
      } },
    },
  })
  // 网关配置只写下面这几项，其余全用线上默认值（重试、卡住取消的时限都和线上一样）
  b.gw = {
    telegram_api: tg.url,
    poll_timeout_s: 1,
    burst_window_ms: 0, // 一条消息一轮，不等"连发合并"的窗口
    commit_poll_ms: DAY_MS, hang_tick_ms: DAY_MS, // 不让"承诺到点""被晾追问"在中途插进额外的轮
    image_provider: 'off', // 不真生图
    voice_bridge_url: 'http://127.0.0.1:9', // 语音服务指到没人听的端口
    botlife_db: join(b.root, 'no-botlife.db'),
  }
  writeConfig(b)
  const links: string[] = []
  const env: Live = { gw: new Gateway(b), b, tg, links }
  live = env
  const records: TurnRecord[] = []
  let error: string | undefined
  try {
    writeFileSync(join(b.channelDir, 'CLAUDE.md'), ctx.persona)
    if (args.skills) cpSync(args.skills, join(b.channelDir, 'skills'), { recursive: true })
    const cred = join(b.root, 'credentials.yaml')
    writeFileSync(cred, `version: 1\nrefs:\n  ${KEY_REF}: ${ctx.placeholder}\n`, { mode: 0o600 })
    chmodSync(cred, 0o600)
    const fakeHome = join(b.root, 'fakehome')
    const tmp = join(b.root, 'tmp')
    mkdirSync(fakeHome, { recursive: true })
    mkdirSync(tmp, { recursive: true })
    for (const rel of args.homeLinks) {
      const dst = join(fakeHome, rel)
      mkdirSync(dirname(dst), { recursive: true })
      symlinkSync(join(homedir(), rel), dst)
      links.push(dst)
    }
    await env.gw.start({
      DSH_BOT_HARNESS: ctx.harness, HOME: fakeHome, USERPROFILE: fakeHome, TMPDIR: tmp,
      // 网关里所有要起 Python 的地方（朋友圈、作息查询）都读这个变量：指到 false，一个 Python 进程都不起
      DSH_BOT_PYTHON: Bun.which('false') ?? '/usr/bin/false',
    })
    const led = env.gw.ledger()
    try {
      const logFile = join(b.botDir, 'logs', 'gateway.log')
      const lastUser = () => led.db.query<{ id: number; state: string }, [string]>(`SELECT id, state FROM inbound WHERE chat_id = ? AND kind = 'user' ORDER BY id DESC LIMIT 1`).get(String(OWNER))
      const lastTurnId = () => led.db.query<{ id: number | null }, [string]>(`SELECT MAX(id) AS id FROM turns WHERE chat_id = ?`).get(String(OWNER))?.id ?? 0
      const play = async (index: number, rerun: boolean): Promise<TurnRecord> => {
        const prevId = lastUser()?.id ?? 0
        let logFrom = 0
        try { logFrom = statSync(logFile).size } catch {}
        const reqFrom = tap.records.length
        const turnFrom = lastTurnId()
        const startedAt = Date.now()
        tg.pushText(OWNER, corpus[index - 1]!)
        let inboundState = 'timeout'
        try {
          inboundState = (await until(() => {
            if (env.gw.proc?.exitCode !== null && env.gw.proc?.exitCode !== undefined) throw new Error('网关进程中途退出了')
            const row = lastUser()
            return row && row.id > prevId && TERMINAL.has(row.state) ? row : null
          }, 'turn settled', args.turnTimeoutMs, 250)).state
        } catch (e) {
          if (!String((e as Error)?.message).startsWith('timed out')) throw e
        }
        const ms = Date.now() - startedAt
        await tap.settled()
        const exchanges: Exchange[] = tap.records.slice(reqFrom).map(r => ({ status: r.status, input: r.usage.input, output: r.usage.output, calls: ctx.callsByReq.get(r.n) ?? [] }))
        const errors = led.db.query<{ error: string }, [string, number]>(`SELECT error FROM turns WHERE chat_id = ? AND id > ? AND error IS NOT NULL ORDER BY id`).all(String(OWNER), turnFrom).map(r => r.error)
        const rec = buildTurn({ group, index, rerun, ms, inboundState, logs: readLogLines(logFile, logFrom), exchanges, errors }, args)
        console.error(`[${GROUP_NAME[group]} ${index}/${corpus.length}${rerun ? ' 补跑' : ''}] ${rec.valid ? '有效' : `无效（${rec.invalidReason}）`}，生效 ${rec.hits} 次，${(ms / 1000).toFixed(1)} 秒`)
        return rec
      }
      // 等不到终态说明这套环境卡死了：后面的消息只会排在它后头，归不到各自的条目上，所以这一组到此为止
      const stuck = () => records.at(-1)?.inboundState === 'timeout'
      for (let i = 1; i <= corpus.length && !stuck(); i++) records.push(await play(i, false))
      // 无效的条目各补跑一次，接在同一个会话后面
      for (const r of records.filter(x => !x.valid)) {
        if (stuck()) break
        records.push(await play(r.index, true))
      }
    } finally {
      led.close()
    }
  } catch (e) {
    error = redact((e as Error)?.message ?? String(e)).slice(0, 300)
    console.error(redact(`${GROUP_NAME[group]}组中途出错：${error}\n网关 stderr 尾部：\n${env.gw.stderr.slice(-1200)}`))
  } finally {
    await teardown(env)
    live = null
  }
  return { records, ...(error ? { error } : {}) }
}

async function main(): Promise<number> {
  let args: Args
  try { args = parseArgs(process.argv.slice(2)) } catch (e) {
    if (!(e instanceof UsageError)) throw e
    console.error(`${e.message}\n用法见本文件开头的注释。`)
    return 2
  }
  const harness = process.env.DSH_BOT_HARNESS ? resolve(process.env.DSH_BOT_HARNESS) : ''
  const dshPkg = join(harness, 'node_modules', '@deepseek-ai', 'dsh', 'package.json')
  if (!harness || !existsSync(dshPkg)) { console.error('需要 DSH_BOT_HARNESS=<装了 dsh 的目录>'); return 2 }
  const key = process.env[args.keyEnv]?.trim() ?? ''
  if (!key) { console.error(`环境变量 ${args.keyEnv} 里没有密钥`); return 2 }
  registerSecret(key)
  let persona: string
  try { persona = readFileSync(args.persona, 'utf8') } catch { console.error('读不了 --persona 指的文件'); return 2 }
  if (args.skills && !existsSync(args.skills)) { console.error('--skills 指的目录不存在'); return 2 }
  for (const rel of args.homeLinks) if (!existsSync(join(homedir(), rel))) { console.error(`--home-link 的 ${rel} 在主目录里不存在`); return 2 }

  const placeholder = `verify-placeholder-${randomBytes(12).toString('hex')}`
  const upstream = new URL(args.baseUrl)
  const callsByReq = new Map<number, ModelCall[]>()
  let unkeyedRequests = 0
  const tap = startUsageTap({
    port: 0,
    upstream: path => upstream.origin + path,
    rewriteHeaders: h => { if (swapKey(h, placeholder, key) === 0) unkeyedRequests++ },
    onResponse: (rec, text) => { callsByReq.set(rec.n, parseToolCalls(text)) },
  })
  const startedAt = new Date().toISOString()
  const ctx: Ctx = { args, persona, harness, placeholder, tap, callsByReq }
  const stop = (): void => { void (live ? teardown(live, true) : Promise.resolve()).finally(() => { tap.stop(); process.exit(130) }) }
  process.on('SIGINT', stop)
  process.on('SIGTERM', stop)
  try {
    const records: TurnRecord[] = []
    const errors: string[] = []
    for (const group of ['trigger', 'control'] as const) {
      const got = await runGroup(group, args[group], ctx)
      records.push(...got.records)
      if (got.error) errors.push(`${GROUP_NAME[group]}组中途出错，没跑完：${got.error}`)
    }
    const summary = summarize(records, { tool: args.tool, threshold: args.threshold, totals: { trigger: args.trigger.length, control: args.control.length }, tally: args.tally })
    summary.notes.push(...errors)
    const result: Result = {
      meta: { model: args.model, api: args.api, upstreamHost: upstream.host, effort: args.effort ?? null, dsh: String(JSON.parse(readFileSync(dshPkg, 'utf8')).version ?? ''), startedAt, unkeyedRequests },
      records, summary,
    }
    console.log(renderReport(result, args.tally))
    if (args.out) { writeFileSync(args.out, toJson(result) + '\n'); console.error(`结果 JSON：${args.out}`) }
    return errors.length ? 3 : summary.verdict === '通过' ? 0 : 1
  } catch (e) {
    console.error(redact(`跑的过程中出错：${(e as Error)?.message ?? String(e)}`))
    return 3
  } finally {
    tap.stop()
  }
}

if (import.meta.main) process.exit(await main())
