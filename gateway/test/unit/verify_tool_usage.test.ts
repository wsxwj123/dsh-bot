// lab/dsh/tools/verify-tool-usage.ts 的纯函数：参数、从模型响应里取工具调用、单条记录、判据、汇总、打码。
// 真跑那一段（真模型、要花钱）不在这里测。
import { expect, test } from 'bun:test'
import { normalize } from 'path'
import { registerSecret } from '../../src/log'
import {
  buildTurn, judge, parseArgs, parseToolCalls, renderReport, safeArgs, summarize, swapKey, toJson, UsageError,
  type Group, type LogLine, type ModelCall, type Result, type TurnInput, type TurnRecord,
} from '../../../lab/dsh/tools/verify-tool-usage'

const BASE = ['--tool', 'sticker', '--threshold', '2', '--trigger', '想你了', '--persona', '/tmp/p.md', '--base-url', 'https://up.example/v1', '--model', 'm-1']

test('参数：可重复项按出现顺序收集，没给的用默认值', () => {
  const a = parseArgs([...BASE, '--trigger', ' 哼 不理你了 ', '--control', '外卖到了', '--home-link', 'resource/stickers', '--effect-event', 'tool.sticker_send', '--tally', 'tier'])
  expect(a.trigger).toEqual(['想你了', '哼 不理你了'])
  expect(a.control).toEqual(['外卖到了'])
  expect(a.threshold).toBe(2)
  expect(a.api).toBe('openai-completions')
  expect(a.keyEnv).toBe('DEEPSEEK_API_KEY')
  expect(a.homeLinks).toEqual([normalize('resource/stickers')]) // 工具按本机的路径写法规整，Windows 上是反斜杠
  expect(a.turnTimeoutMs).toBe(900_000)
  expect(a.effort).toBeUndefined()
})

test('参数：缺项、写错、危险的值都当场拒绝', () => {
  const bad = (extra: string[], base = BASE) => expect(() => parseArgs([...base, ...extra])).toThrow(UsageError)
  bad([], BASE.slice(2)) // 没有 --tool
  bad([], BASE.filter(x => x !== '--trigger' && x !== '想你了')) // 一条触发语料都没有
  bad(['--nope', '1']) // 不认识的参数
  bad(['--model']) // 缺值
  bad(['--tool', 'x']) // 单值参数给了两次
  bad(['--trigger', '/clear']) // 会被网关当成命令
  bad(['--trigger', '   ']) // 空语料
  bad(['--effort', 'ultra'])
  bad(['--api', 'anthropic-messages']) // 响应解析只写了一种协议
  bad(['--home-link', '../.ssh']) // 不许跳出主目录
  bad(['--home-link', '/etc/passwd'])
  bad(['--key-env', 'sk-abc']) // 这里写的是变量名，不是密钥
  bad(['--tally', 'tier']) // 没有生效事件就没法按字段计数
  expect(() => parseArgs(BASE.map(x => (x === '2' ? '0' : x)))).toThrow(UsageError) // 阈值得是正整数
  expect(() => parseArgs(BASE.map(x => (x === 'https://up.example/v1' ? 'ftp://x' : x)))).toThrow(UsageError)
})

const sse = (...chunks: object[]) => [...chunks.map(c => `data: ${JSON.stringify(c)}\n`), 'data: [DONE]\n'].join('\n')
const delta = (tool_calls: object[]) => ({ choices: [{ index: 0, delta: { tool_calls } }] })

test('取工具调用：流式分片按 index 拼回一次调用', () => {
  const body = sse(
    { choices: [{ index: 0, delta: { role: 'assistant', content: '' } }] },
    delta([{ index: 0, id: 'call_1', type: 'function', function: { name: 'mcp__tg__sticker', arguments: '{"action":' } }]),
    delta([{ index: 0, function: { arguments: '"send","id":"sfw:12"}' } }]),
    { choices: [{ index: 0, delta: {}, finish_reason: 'tool_calls' }] },
    { choices: [], usage: { prompt_tokens: 10, completion_tokens: 2 } },
  )
  expect(parseToolCalls(body)).toEqual([{ name: 'mcp__tg__sticker', args: '{"action":"send","id":"sfw:12"}' }])
})

test('取工具调用：并行的两次调用不会被拼成一次（index 不同 / index 相同但换了 id / 没有 id 又带了一次名字）', () => {
  const a = { name: 'mcp__tg__sticker', arguments: '{"action":"list"}' }
  const b = { name: 'mcp__tg__reply', arguments: '{"text":"x"}' }
  const want = [{ name: a.name, args: a.arguments }, { name: b.name, args: b.arguments }]
  expect(parseToolCalls(sse(delta([{ index: 0, id: 'c1', function: a }, { index: 1, id: 'c2', function: b }])))).toEqual(want)
  expect(parseToolCalls(sse(delta([{ index: 0, id: 'c1', function: a }]), delta([{ index: 0, id: 'c2', function: b }])))).toEqual(want)
  expect(parseToolCalls(sse(delta([{ function: a }]), delta([{ function: b }])))).toEqual(want)
})

test('取工具调用：非流式整包、arguments 给成对象、坏行、没有调用', () => {
  const whole = JSON.stringify({ choices: [{ message: { role: 'assistant', tool_calls: [{ id: 'c1', function: { name: 'skill', arguments: { name: 'skill-gif' } } }, { id: 'c2', function: { name: 'mcp__tg__reply', arguments: '{}' } }] } }] })
  expect(parseToolCalls(whole)).toEqual([{ name: 'skill', args: '{"name":"skill-gif"}' }, { name: 'mcp__tg__reply', args: '{}' }])
  expect(parseToolCalls(`data: {坏的\n\n${sse(delta([{ index: 0, id: 'c1', function: { name: 'skill', arguments: '{}' } }]))}`)).toEqual([{ name: 'skill', args: '{}' }])
  expect(parseToolCalls(sse({ choices: [{ index: 0, delta: { content: '你好' } }] }))).toEqual([])
  expect(parseToolCalls('{不是 JSON')).toEqual([])
  expect(parseToolCalls('')).toEqual([])
})

test('短参数：只留白名单里的短标识符，模型说的话一个字不留', () => {
  expect(safeArgs('{"action":"send","id":"sfw:12","tier":"sfw","query":"想你","text":"宝宝我也想你"}')).toEqual({ action: 'send', id: 'sfw:12', tier: 'sfw' })
  expect(safeArgs('{"action":"发一条很长很长的话"}')).toEqual({}) // 值不是短标识符
  expect(safeArgs('{"name":"skill-gif"}')).toEqual({ name: 'skill-gif' })
  expect(safeArgs('{"action":')).toEqual({})
  expect(safeArgs('')).toEqual({})
  expect(safeArgs('"action"')).toEqual({})
})

test('换密钥：只动带占位密钥的头，返回换了几处；没有占位密钥的请求拿不到真密钥', () => {
  const h = new Headers({ authorization: 'Bearer PLACEHOLDER-1', 'content-type': 'application/json' })
  expect(swapKey(h, 'PLACEHOLDER-1', 'REAL-KEY')).toBe(1)
  expect(h.get('authorization')).toBe('Bearer REAL-KEY')
  expect(h.get('content-type')).toBe('application/json')
  const other = new Headers({ authorization: 'Bearer someone-else' })
  expect(swapKey(other, 'PLACEHOLDER-1', 'REAL-KEY')).toBe(0)
  expect(other.get('authorization')).toBe('Bearer someone-else')
})

// ─── 单条记录 ───

const OPTS = { tool: 'sticker', effectEvent: 'tool.sticker_send', watchSkill: 'skill-gif' }
const call = (name: string, args: object): ModelCall => ({ name, args: JSON.stringify(args) })
const log = (event: string, fields: object = {}): LogLine => ({ at: '2026-10-12T00:00:00.000Z', level: 'info', event, bot: 'verify-trigger', ...fields })

/** 造一条语料的原始观测：sends 是这一条里发出的表情包各自的档位；lists 是只查不发的次数 */
function input(group: Group, index: number, o: { sends?: string[]; lists?: number; replied?: boolean; state?: string; rerun?: boolean; skill?: boolean; errors?: string[] } = {}): TurnInput {
  const sends = o.sends ?? []
  const replied = o.replied ?? true
  const calls: ModelCall[] = []
  const logs: LogLine[] = []
  if (o.skill) calls.push(call('skill', { name: 'skill-gif' }))
  for (let i = 0; i < (o.lists ?? 0); i++) {
    calls.push(call('mcp__tg__sticker', { action: 'list', query: '开心' }))
    logs.push(log('tool.sticker_list', { query: true, tier: null, hits: 3 }))
  }
  for (const tier of sends) {
    calls.push(call('mcp__tg__sticker', { action: 'send', id: `${tier}:1` }))
    logs.push(log('tool.sticker_send', { tier, label: 'Happy 1', kind: 'animation' }))
  }
  if (replied) { calls.push(call('mcp__tg__reply', { text: '这是模型说的话' })); logs.push(log('tool.reply', { parts: 1 })) }
  const delivered = sends.length + (replied ? 1 : 0)
  const state = o.state ?? 'done'
  if (state !== 'timeout') logs.push(log('turn.end', { turn: index, kind: 'message', attempt: 0, outcome: state === 'dead' ? 'error' : 'ok', delivered, silent: false }))
  return { group, index, rerun: o.rerun ?? false, ms: 1500, inboundState: state, logs, exchanges: [{ status: 200, input: 100, output: 7, calls }, { status: 200, input: 120, output: 3, calls: [] }], errors: o.errors ?? [] }
}
const turn = (...a: Parameters<typeof input>): TurnRecord => buildTurn(input(...a), OPTS)

test('单条：发了一个表情包又回了话，记下档位、技能加载、用量；记录里没有模型说的话', () => {
  const r = turn('trigger', 1, { sends: ['sfw'], lists: 1, skill: true })
  expect(r.valid).toBe(true)
  expect(r.hits).toBe(1)
  expect(r.calls).toEqual([{ action: 'list' }, { action: 'send', id: 'sfw:1' }])
  expect(r.effects).toEqual([{ tier: 'sfw', label: 'Happy 1', kind: 'animation' }])
  expect(r.gatewayEvents).toEqual({ 'tool.sticker_list': 1, 'tool.sticker_send': 1 })
  expect(r.otherTools).toEqual({ skill: 1, reply: 1 })
  expect(r.skillLoaded).toBe(true)
  expect(r.tokens).toEqual({ input: 220, output: 10 })
  expect(r.requests).toBe(2)
  expect(r.upstreamErrors).toBe(0)
  expect(JSON.stringify(r)).not.toContain('这是模型说的话')
  expect(JSON.stringify(r)).not.toContain('开心')
})

test('单条：只发表情包不回话也算有效（表情包本身就是一次回复）', () => {
  const r = turn('trigger', 1, { sends: ['sfw'], replied: false })
  expect(r.valid).toBe(true)
  expect(r.delivered).toBe(1)
})

test('单条：空回复、网关放弃、等待超时都是无效，原因各不相同', () => {
  expect(turn('trigger', 1, { replied: false }).invalidReason).toBe('空回复（模型既没回话也没选择不回）')
  expect(turn('trigger', 1, { replied: false, state: 'dead' }).invalidReason).toBe('网关放弃了这条（重试用尽或上游拒绝）')
  const t = turn('trigger', 1, { replied: false, state: 'timeout' })
  expect(t.invalidReason).toBe('等待超时')
  expect(t.valid).toBe(false)
  expect(t.outcomes).toEqual([])
})

test('单条：模型选择不回（stay_silent）是有效的；上游报错的请求单独计数、没有用量时记空', () => {
  const r = buildTurn({
    group: 'control', index: 1, rerun: false, ms: 10, inboundState: 'done',
    logs: [log('turn.end', { kind: 'message', outcome: 'error', delivered: 0, silent: false }), log('turn.end', { kind: 'retry', outcome: 'ok', delivered: 0, silent: true })],
    exchanges: [{ status: 429, calls: [] }, { status: 0, calls: [] }, { status: 200, calls: [call('mcp__tg__stay_silent', { reason: '不想回' })] }],
    errors: ['-32603 upstream 429'],
  }, OPTS)
  expect(r.valid).toBe(true)
  expect(r.silent).toBe(true)
  expect(r.turnKinds).toEqual(['message', 'retry'])
  expect(r.outcomes).toEqual(['error', 'ok'])
  expect(r.upstreamErrors).toBe(2)
  expect(r.errors).toEqual(['-32603 upstream 429'])
  expect(r.tokens).toEqual({ input: null, output: null })
  expect(r.hits).toBe(0)
})

test('单条：别的工具的事件不会算到目标工具头上；不给生效事件时按模型侧调用数算', () => {
  const base: TurnInput = {
    group: 'trigger', index: 1, rerun: false, ms: 10, inboundState: 'done',
    logs: [log('tool.reply'), log('tool.reply_slow'), log('tool.react'), log('tool.remember'), log('turn.end', { delivered: 1, silent: false })],
    exchanges: [{ status: 200, calls: [call('mcp__tg__react', { message_id: 3, emoji: '❤️' }), call('mcp__tg__reply', { text: 'x' }), call('mcp__tg__react', { message_id: 4, emoji: '👍' })] }],
    errors: [],
  }
  expect(buildTurn(base, { tool: 're' }).gatewayEvents).toEqual({})
  expect(buildTurn(base, { tool: 'reply' }).gatewayEvents).toEqual({ 'tool.reply': 1, 'tool.reply_slow': 1 })
  const react = buildTurn(base, { tool: 'react' })
  expect(react.hits).toBe(2)
  expect(react.effects).toEqual([])
  expect(react.skillLoaded).toBe(false)
})

test('单条：回复送达之后才报错的条目照样有效，但报错要留痕：先打码、压成一行、再截到 160 字', () => {
  const secret = 'zk-77aa55cc33ee11bb99dd-not-a-real-key'
  registerSecret(secret)
  const r = turn('trigger', 1, { sends: ['sfw'], errors: [`-32603 model returned\n  an empty response; key=${secret} ${'x'.repeat(300)}`] })
  expect(r.valid).toBe(true)
  expect(r.errors).toHaveLength(1)
  expect(r.errors[0]).toStartWith('-32603 model returned an empty response; key=*** xxx')
  expect(r.errors[0]!.length).toBe(160)
  expect(r.errors[0]).not.toContain(secret)
})

// ─── 判据与汇总 ───

const sum = (records: TurnRecord[], totals: Record<Group, number>, threshold = 2) => summarize(records, { tool: 'sticker', threshold, totals, tally: 'tier' })

test('判据：命中条数到阈值才过；过了但对照组有调用就降级；没到阈值就是不过', () => {
  expect(judge({ hitTurns: 2, controlCalls: 0, threshold: 2 })).toBe('通过')
  expect(judge({ hitTurns: 5, controlCalls: 1, threshold: 2 })).toBe('降级')
  expect(judge({ hitTurns: 1, controlCalls: 0, threshold: 2 })).toBe('不通过')
  expect(judge({ hitTurns: 1, controlCalls: 3, threshold: 2 })).toBe('不通过')
})

test('汇总：触发组两条命中、对照组没动静 → 通过；档位、技能加载、同条多发都如实计', () => {
  const s = sum([
    turn('trigger', 1, { sends: ['sfw'], skill: true }), turn('trigger', 2), turn('trigger', 3, { sends: ['nsfw', 'sfw'] }),
    turn('control', 1), turn('control', 2),
  ], { trigger: 3, control: 2 })
  expect(s.verdict).toBe('通过')
  expect(s.trigger).toEqual({ total: 3, valid: 3, hitTurns: 2, hits: 3, calls: 3 })
  expect(s.control).toEqual({ total: 2, valid: 2, hitTurns: 0, hits: 0, calls: 0 })
  expect(s.tally).toEqual({ sfw: 2, nsfw: 1 })
  expect(s.multiPerTurn).toBe(1)
  expect(s.skillLoads).toBe(1)
  expect(s.invalid).toEqual([])
  expect(s.erroredValid).toBe(0)
  expect(s.notes).toEqual([])
})

test('汇总：有效但报过错的条目单独计数并提醒；判据不受影响', () => {
  const s = sum([turn('trigger', 1, { sends: ['sfw'], errors: ['-32603 empty response'] }), turn('trigger', 2, { sends: ['sfw'] }), turn('control', 1, { errors: ['-32603 empty response'] })], { trigger: 2, control: 1 })
  expect(s.verdict).toBe('通过')
  expect(s.erroredValid).toBe(2)
  expect(s.notes.some(n => n.includes('有 2 条有效但报过错'))).toBe(true)
})

test('汇总：对照组只查了一下库、没发出去，也算出现调用 → 降级', () => {
  const s = sum([turn('trigger', 1, { sends: ['sfw'] }), turn('trigger', 2, { sends: ['sfw'] }), turn('control', 1, { lists: 1 })], { trigger: 2, control: 1 })
  expect(s.control.calls).toBe(1)
  expect(s.control.hits).toBe(0)
  expect(s.verdict).toBe('降级')
})

test('汇总：两次都发在同一条里，按条数算只有一条命中 → 不过，并写明原因', () => {
  const s = sum([turn('trigger', 1, { sends: ['sfw', 'sfw'] }), turn('trigger', 2), turn('control', 1)], { trigger: 2, control: 1 })
  expect(s.trigger.hits).toBe(2)
  expect(s.trigger.hitTurns).toBe(1)
  expect(s.verdict).toBe('不通过')
  expect(s.notes.some(n => n.includes('集中在 1 条里'))).toBe(true)
})

test('汇总：原跑无效的条目用补跑结果顶上；补跑也无效、根本没跑到的都单独列出，不计入命中', () => {
  const s = sum([
    turn('trigger', 1, { replied: false }), // 空回复
    turn('trigger', 2, { sends: ['sfw'] }),
    turn('trigger', 3, { replied: false, state: 'dead' }),
    turn('trigger', 1, { sends: ['sfw'], rerun: true }), // 补跑有效且命中
    turn('trigger', 3, { replied: false, rerun: true }), // 补跑仍无效
    turn('control', 1, { replied: false, state: 'timeout' }), // 卡死，本组中止，第 2 条没跑到
  ], { trigger: 4, control: 2 })
  expect(s.trigger).toEqual({ total: 4, valid: 2, hitTurns: 2, hits: 2, calls: 2 })
  expect(s.invalid).toEqual([
    { group: 'trigger', index: 1, reason: '空回复（模型既没回话也没选择不回）', rerun: '补跑有效' },
    { group: 'trigger', index: 3, reason: '网关放弃了这条（重试用尽或上游拒绝）', rerun: '补跑仍无效' },
    { group: 'trigger', index: 4, reason: '没跑到（本组中途中止）', rerun: '未补跑' },
    { group: 'control', index: 1, reason: '等待超时', rerun: '未补跑' },
    { group: 'control', index: 2, reason: '没跑到（本组中途中止）', rerun: '未补跑' },
  ])
  expect(s.verdict).toBe('通过')
  expect(s.notes.some(n => n.includes('触发组 4 条里只有 2 条'))).toBe(true)
  expect(s.notes.some(n => n.includes('对照组 2 条里只有 0 条'))).toBe(true)
})

test('汇总：无效的对照条目里出现过调用，照样算数（不因为这条无效就当没发生）', () => {
  const s = sum([turn('trigger', 1, { sends: ['sfw'] }), turn('trigger', 2, { sends: ['sfw'] }), turn('control', 1, { lists: 1, replied: false })], { trigger: 2, control: 1 })
  expect(s.control.valid).toBe(0)
  expect(s.control.calls).toBe(1)
  expect(s.verdict).toBe('降级')
})

test('汇总：网关侧有事件、模型侧没解析到调用，要报出来，并且按网关侧计调用', () => {
  const raw = input('control', 1, { lists: 1 })
  raw.exchanges = [{ status: 200, calls: [] }] // 假装响应解析漏了
  const s = sum([turn('trigger', 1, { sends: ['sfw'] }), turn('trigger', 2, { sends: ['sfw'] }), buildTurn(raw, OPTS)], { trigger: 2, control: 1 })
  expect(s.parseGaps).toBe(1)
  expect(s.control.calls).toBe(1)
  expect(s.verdict).toBe('降级')
  expect(s.notes.some(n => n.includes('模型侧没解析到调用'))).toBe(true)
})

test('汇总：对方收到了消息、模型侧却一次工具调用都没解析到，同样算解析漏了', () => {
  const raw = input('trigger', 2)
  raw.exchanges = [{ status: 200, calls: [] }]
  expect(sum([turn('trigger', 1), buildTurn(raw, OPTS)], { trigger: 2, control: 0 }).parseGaps).toBe(1)
  // 空回复的条目本来就没有调用，不算漏
  expect(sum([turn('trigger', 1, { replied: false })], { trigger: 1, control: 0 }).parseGaps).toBe(0)
})

// ─── 打码 ───

test('打码：报告和结果 JSON 里出现的密钥、令牌一律换掉', () => {
  const secret = 'qx-9f3c1e7a5b2d4c6e8f0a-not-a-real-key'
  registerSecret(secret)
  const leaky = turn('trigger', 1, { sends: ['sfw'] })
  leaky.effects[0]!.label = `Happy ${secret}` // 假设密钥混进了某个日志字段
  leaky.invalidReason = `上游说 Bearer abcdefghijklmnop123456 不对，密钥 sk-abcdefghijklmnop123456`
  leaky.valid = false
  const result: Result = {
    meta: { model: `m-${secret}`, api: 'openai-completions', upstreamHost: 'up.example', effort: 'low', dsh: '0.2.0', startedAt: '2026-10-12T00:00:00.000Z', unkeyedRequests: 0 },
    records: [leaky],
    summary: sum([leaky], { trigger: 1, control: 0 }),
  }
  for (const text of [renderReport(result, 'tier'), toJson(result)]) {
    expect(text).not.toContain(secret)
    expect(text).not.toContain('abcdefghijklmnop123456')
    expect(text).toContain('***')
  }
})

test('报告：一行一条语料，只有组别、序号和数字，结论与无效条目写在表后', () => {
  const records = [turn('trigger', 1, { sends: ['nsfw'], skill: true }), turn('trigger', 2, { replied: false }), turn('control', 1, { errors: ['-32603 empty response'] })]
  const text = renderReport({
    meta: { model: 'm-1', api: 'openai-completions', upstreamHost: 'up.example', effort: null, dsh: '0.2.0', startedAt: '2026-10-12T00:00:00.000Z', unkeyedRequests: 1 },
    records, summary: sum(records, { trigger: 2, control: 1 }),
  }, 'tier')
  expect(text).toContain('| 触发 | 1 | 否 | 是 | 1 | 1 | nsfw | 是 | 1.5 | 2 | 0 | 220/10 |')
  expect(text).toContain('| 触发 | 2 | 否 | 否（空回复（模型既没回话也没选择不回）） | 0 | 0 | - | 否 |')
  expect(text).toContain('结论：不通过')
  expect(text).toContain('按 tier 计 nsfw=1')
  expect(text).toContain('无效：触发组第 2 条，空回复（模型既没回话也没选择不回）；未补跑')
  expect(text).toContain('报错：对照组第 1 条，-32603 empty response')
  expect(text).toContain('思考 未设')
  expect(text).toContain('有 1 个请求的头里没有占位密钥')
  expect(text).not.toContain('这是模型说的话')
})
