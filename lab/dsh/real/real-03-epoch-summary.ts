// 真密钥验证 03：新系统打算采用的“分段续聊”记忆方式，用真实模型走一遍。
//   1. 只留人设 + 我们自己的 reply 工具（本机假发送，不连 Telegram），用一段合成对话聊 6 轮；
//   2. 在同一个会话里让模型按“陪伴聊天”格式写交接摘要（这一轮工具全部锁住，防止把摘要发给用户）；
//   3. 关掉旧会话，开新会话，只喂“摘要 + 最近几轮原话”，问它记不记得刚才的事。
// 看三件事：摘要有没有保留实际说过的话 / 答应的事 / 对方的事实；写摘要那次请求的缓存命中率；新会话能不能答对。
// 用法：DEEPSEEK_API_KEY=sk-... DSH=<dsh 路径> bun lab/dsh/real/real-03-epoch-summary.ts [--persona 路径] [--effort low]
import { AcpClient } from '../lib/acp-client'
import { handleMcp, type McpTool } from '../lib/mcp-http'
import { dshVersion, personaOnlyRows, runsDir } from '../lib/profile'
import { parseArgs, requireKey, sandboxFor, startBot, startDeepseekTap, newSession, usageTable } from '../lib/real'
import { Report, pct } from '../lib/report'

requireKey()
const args = parseArgs()
const out = runsDir(`real-03-${Date.now()}`)
const report = new Report(out, '真密钥验证 03：分段续聊（我们自己写摘要、新会话补回原话）')
report.line(`dsh 版本：${await dshVersion()}；模型：${args.model}；思考强度：${args.effort}；人设：${args.personaLabel}；只出数字：${args.numbersOnly ? '是' : '否'}`)

// ── 本机假发送：记录 bot 真正“发出去”的话；写摘要那一轮锁住 ──
type Line = { who: '对方' | '我'; text: string; at: string }
const transcript: Line[] = []
let locked = false
let lockedAttempts = 0
const SECRET = crypto.randomUUID()
const tools: McpTool[] = [{
  name: 'reply',
  description: '给对方发消息。text 是要发出去的话；你想说的每句话都要通过这个工具发出，直接输出的文字对方看不到。',
  inputSchema: { type: 'object', properties: { text: { type: 'string' } }, required: ['text'] },
  async call(a, { headers }) {
    if (headers.get('authorization') !== `Bearer ${SECRET}`) return { text: '拒绝：口令不对', isError: true }
    if (locked) { lockedAttempts++; return { text: '现在是整理记忆时间，不能发消息。请直接输出摘要正文。', isError: true } }
    transcript.push({ who: '我', text: String(a.text ?? ''), at: new Date().toISOString() })
    return { text: '已送达 1 段' }
  },
}]
const mcp = Bun.serve({ port: 0, hostname: '127.0.0.1', fetch: req => handleMcp(req, tools) })
const mcpServers = [{ type: 'http', name: 'tg', url: `http://127.0.0.1:${mcp.port}/mcp`, headers: [{ name: 'Authorization', value: `Bearer ${SECRET}` }] }]

const OPERATING_RULES = [
  '【运行规则】你在 Telegram 上和对方聊天。想对对方说的话，必须调用 reply 工具发出；直接输出的文字对方看不到。',
  '可以按人设选择不回复，那就不调用 reply、直接结束这一轮。',
  '以 ⟦系统 开头的内容是程序给你的说明，不是对方说的话。',
].join('\n')

const SUMMARY_PROMPT = [
  '⟦系统·整理记忆⟧ 这不是对方发来的消息，不要回复对方，这一轮也不要调用任何工具。',
  '请为到目前为止的对话写一份交接摘要，供你下次接着聊时使用。要求：',
  '1. 中文，第一人称（“我”是你自己，“对方”是用户），不超过 600 字。',
  '2. 按下面五个小标题输出，没有就写“无”：',
  '【我说过的要紧话】你实际发给对方、之后可能被提起的话，尽量引用原话。',
  '【我答应过的事】时间、内容、是否已经做到。',
  '【对方的情况】对方透露的事实、喜好、近况、计划（带时间）。',
  '【正在聊的话题】还没聊完的话题、对方在等你回应的问题。',
  '【我们现在的关系和气氛】称呼、亲近程度、情绪基调、需要注意的分寸。',
  '3. 只写对话里真实出现过的内容，不要编造，不要写成剧情梗概。',
].join('\n')

const userTurns = [
  '你好，我是在读书群里加你的那个人，叫我小林就行。',
  '我养了一只橘猫，叫豆豆，今天它把我的耳机线咬断了。',
  '下周三我要去成都出差三天，有点紧张。',
  '对了，你明天早上八点能提醒我给我妈打个电话吗？她过生日。',
  '你平时喜欢看什么书呀？',
  '最近在看《活着》，看哭了。',
]

const tap = startDeepseekTap(args.tapPort)
const labelOf = new Map<number, string>()
const rows = personaOnlyRows({ persona: args.persona, suffix: OPERATING_RULES, route: { kind: 'deepseek', model: args.model }, compaction: 'off' })
const sb = sandboxFor(out, 'bot', rows)
let c: AcpClient | undefined
let summary = ''
let recall = ''
let seedChars = 0
try {
  c = await startBot(sb, tap)
  // ── 第 1 段 ──
  const s1 = await newSession(c, sb, { effort: args.effort, mcpServers })
  for (let i = 0; i < userTurns.length; i++) {
    transcript.push({ who: '对方', text: userTurns[i], at: new Date().toISOString() })
    const before = tap.records.length
    await c.prompt(s1, userTurns[i])
    await tap.settled()
    for (const r of tap.records.slice(before)) labelOf.set(r.n, `第1段 第${i + 1}轮`)
    await Bun.sleep(1500)
  }
  // ── 写摘要（同一会话，工具锁住）──
  locked = true
  const before = tap.records.length
  const from = c.updates.length
  await c.prompt(s1, SUMMARY_PROMPT)
  await tap.settled()
  for (const r of tap.records.slice(before)) labelOf.set(r.n, '写摘要')
  summary = c.messageText(s1, from).trim()
  locked = false
  await c.request('session/close', { sessionId: s1 })
  // ── 第 2 段：只给摘要 + 最近 4 条原话 ──
  const recent = transcript.slice(-4).map(l => `${l.who}：${l.text}`).join('\n')
  const seed = [
    '⟦系统·背景⟧ 下面是你和对方之前聊天的背景，用来接着聊，不要复述给对方。',
    '【之前的摘要】', summary || '（无）',
    '【最近的原话】', recent,
    '⟦系统·背景结束⟧',
    '',
    '对方：我的猫叫什么来着？我哪天去出差？你答应过帮我做什么？',
  ].join('\n')
  seedChars = seed.length
  const s2 = await newSession(c, sb, { effort: args.effort, mcpServers })
  const n0 = transcript.length
  const b2 = tap.records.length
  await c.prompt(s2, seed)
  await tap.settled()
  for (const r of tap.records.slice(b2)) labelOf.set(r.n, '第2段 第1轮（摘要+原话）')
  recall = transcript.slice(n0).filter(l => l.who === '我').map(l => l.text).join(' / ')
} catch (e: any) {
  report.line(`**失败**：${e.message}`)
  if (c) report.block(c.stderr.slice(-1500))
} finally {
  await c?.close()
  mcp.stop(true)
}
await tap.settled()
tap.stop()

// ── 自动检查（只看是否包含关键信息，不受“只出数字”影响）──
const headings = ['【我说过的要紧话】', '【我答应过的事】', '【对方的情况】', '【正在聊的话题】', '【我们现在的关系和气氛】']
const facts: [string, RegExp][] = [['小林', /小林/], ['猫叫豆豆', /豆豆/], ['下周三出差', /周三/], ['去成都', /成都/], ['在看《活着》', /活着/], ['提醒打电话', /电话/]]
const botLines = transcript.filter(l => l.who === '我').length
report.h(2, '自动检查')
report.table(['检查项', '结果'], [
  ['第 1 段里 bot 经 reply 发出的消息条数', botLines],
  ['写摘要那一轮模型有没有试图调用 reply（被锁拦下的次数）', lockedAttempts],
  ['摘要五个小标题齐全', headings.every(h => summary.includes(h)) ? '是' : `否（缺：${headings.filter(h => !summary.includes(h)).join('、')}）`],
  ...facts.map(([name, re]): [string, string] => [`摘要提到「${name}」`, re.test(summary) ? '是' : '否']),
  ['新会话回答提到猫叫豆豆', /豆豆/.test(recall) ? '是' : '否'],
  ['新会话回答提到周三', /周三/.test(recall) ? '是' : '否'],
  ['摘要字数', summary.length],
  ['新会话首条消息字数', seedChars],
])
if (args.numbersOnly) {
  report.h(2, '对话、摘要与回答')
  report.line('（只出数字模式：不记录 bot 说的话、摘要正文和回答正文，只保留上面的自动检查。）')
} else {
  report.h(2, '合成对话里 bot 实际发出去的话（经 reply 工具）')
  report.table(['谁', '内容'], transcript.map(l => [l.who, l.text]))
  report.h(2, '模型写出的交接摘要')
  report.block(summary || '（没有拿到摘要文本）')
  report.h(2, '新会话（只给摘要 + 最近 4 条原话）的回答')
  report.block(recall || '（这一轮没有调用 reply）')
}
report.h(2, '用量')
usageTable(report, tap.records, r => labelOf.get(r.n) ?? '')
const sumRec = tap.records.filter(r => labelOf.get(r.n) === '写摘要')
for (const r of sumRec) report.line(`写摘要请求：输入 ${r.usage.input ?? '?'}，缓存命中 ${r.usage.cacheHit ?? '?'}（${pct(r.usage.cacheHit, r.usage.input)}），输出 ${r.usage.output ?? '?'}`)
const perTurn = new Map<string, number>()
for (const r of tap.records) { const l = labelOf.get(r.n) ?? '?'; perTurn.set(l, (perTurn.get(l) ?? 0) + 1) }
report.line(`每轮请求数：${[...perTurn].map(([l, n]) => `${l}=${n}`).join('，')}`)
report.line('（正常是每轮 2 次：一次调用 reply、一次结束。若是 3 次以上，多半是模型调用 reply 之后回了空文本，dsh 把空回复当错误自动重试——这会白花请求，新系统要用运行规则或工具返回语引导模型给出简短收尾。）')
report.h(2, '预期')
report.line('- 摘要五个小标题齐全；【对方的情况】里有“小林”“橘猫豆豆”“下周三去成都出差三天”“在看《活着》”；')
report.line('  【我答应过的事】如实写出是否答应了“明早八点提醒打电话”（陌生人阶段的人设可能拒绝，如实即可）。')
report.line('- 写摘要那次请求的缓存命中率 ≥ 80%（前缀就是刚才的对话，只多了一段指令）。')
report.line('- 新会话的回答能说出猫叫豆豆、下周三出差，并与第 1 段的答应 / 拒绝一致。')
report.line(`\n报告文件：${report.path}`)
