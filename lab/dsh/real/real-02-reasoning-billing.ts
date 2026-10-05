// 真密钥验证 02：上一轮的“思考内容”会不会被当成输入计费。
// dsh 会把此前每一轮的思考内容原样放回历史（0.1.5 的 reasoning_content、0.2.0 的 thinking 块，均已离线核实）。
// 如果 DeepSeek 按输入计费，开着思考的长会话会越聊越贵；这决定了陪伴聊天该不该关思考、每段该多长。
//
// 第一部分：直接调 DeepSeek（不经过 dsh），同一段历史“带 / 不带”一大段旧思考，对比回报的输入 token。
// 第二部分：经 dsh 跑 4 轮，思考强度 high 与 off 各一遍，看输入 token 的增长。
// 用法：DEEPSEEK_API_KEY=sk-... DSH=<dsh 路径> bun lab/dsh/real/real-02-reasoning-billing.ts [--model deepseek-v4-flash]
import { AcpClient } from '../lib/acp-client'
import { dshVersion, personaOnlyRows, runsDir } from '../lib/profile'
import { parseArgs, requireKey, sandboxFor, startBot, startDeepseekTap, newSession, usageTable } from '../lib/real'
import { Report } from '../lib/report'

const key = requireKey()
const args = parseArgs()
const out = runsDir(`real-02-${Date.now()}`)
const report = new Report(out, '真密钥验证 02：旧思考内容是否计入输入')
report.line(`dsh 版本：${await dshVersion()}；模型：${args.model}`)
const root = (process.env.DEEPSEEK_UPSTREAM ?? 'https://api.deepseek.com').replace(/\/+$/, '')
const LONG = '我先想一想对方为什么这么问，可能是想确认我在不在，也可能只是随口打招呼。'.repeat(40) // 约 1500 字

async function chatProbe(withReasoning: boolean, thinking: 'enabled' | 'disabled') {
  const assistant: any = { role: 'assistant', content: '我在的。' }
  if (withReasoning) assistant.reasoning_content = LONG
  const body = {
    model: args.model,
    stream: false,
    max_tokens: 16,
    thinking: { type: thinking },
    messages: [
      { role: 'system', content: '你是一个简短回答的助手。' },
      { role: 'user', content: '你在吗？' },
      assistant,
      { role: 'user', content: '好的，只回一个字：嗯' },
    ],
  }
  const r = await fetch(`${root}/chat/completions`, { method: 'POST', headers: { 'content-type': 'application/json', authorization: `Bearer ${key}` }, body: JSON.stringify(body) })
  const text = await r.text()
  let j: any = {}
  try { j = JSON.parse(text) } catch {}
  return { status: r.status, input: j?.usage?.prompt_tokens, error: r.ok ? '' : text.slice(0, 160) }
}

async function messagesProbe(withThinking: boolean) {
  const content: any[] = []
  if (withThinking) content.push({ type: 'thinking', thinking: LONG, signature: 'probe' })
  content.push({ type: 'text', text: '我在的。' })
  const body = {
    model: args.model,
    max_tokens: 16,
    system: '你是一个简短回答的助手。',
    messages: [
      { role: 'user', content: '你在吗？' },
      { role: 'assistant', content },
      { role: 'user', content: '好的，只回一个字：嗯' },
    ],
  }
  const r = await fetch(`${root}/anthropic/v1/messages`, { method: 'POST', headers: { 'content-type': 'application/json', 'x-api-key': key, 'anthropic-version': '2023-06-01' }, body: JSON.stringify(body) })
  const text = await r.text()
  let j: any = {}
  try { j = JSON.parse(text) } catch {}
  const u = j?.usage ?? {}
  return { status: r.status, input: u.input_tokens !== undefined ? (u.input_tokens ?? 0) + (u.cache_read_input_tokens ?? 0) : undefined, error: r.ok ? '' : text.slice(0, 160) }
}

report.h(2, '第一部分：直接调接口（同一段历史，带 / 不带约 1500 字的旧思考）')
const rows: any[][] = []
for (const thinking of ['enabled', 'disabled'] as const) {
  const a = await chatProbe(true, thinking)
  const b = await chatProbe(false, thinking)
  rows.push([`Chat Completions，thinking=${thinking}`, a.status, a.input, b.status, b.input, a.input !== undefined && b.input !== undefined ? a.input - b.input : '', a.error || b.error])
}
{
  const a = await messagesProbe(true)
  const b = await messagesProbe(false)
  rows.push(['Anthropic Messages（/anthropic）', a.status, a.input, b.status, b.input, a.input !== undefined && b.input !== undefined ? a.input - b.input : '', a.error || b.error])
}
report.table(['接口', '带思考 HTTP', '带思考 输入', '不带 HTTP', '不带 输入', '差值', '错误信息（如有）'], rows)
report.line('判读：差值接近 0 → 旧思考不计费（接口自己丢掉了）；差值约 1000–1500 → 旧思考按输入计费；某行报错 → 该接口不接受这种历史写法。')

report.h(2, '第二部分：经 dsh 连聊 4 轮（只留人设），思考 high 与 off 对比')
const tap = startDeepseekTap(args.tapPort)
const labelOf = new Map<number, string>()
for (const effort of ['high', 'off'] as const) {
  const sb = sandboxFor(out, `effort-${effort}`, personaOnlyRows({ persona: args.persona, route: { kind: 'deepseek', model: args.model } }))
  let c: AcpClient | undefined
  try {
    c = await startBot(sb, tap)
    const sid = await newSession(c, sb, { effort })
    const turns = ['在吗？想问你个事。', '你觉得一个人去旅行好还是跟朋友去好？', '为什么呀，说说你的理由。', '好吧，那你最近有出去玩吗？']
    for (let i = 0; i < turns.length; i++) {
      const before = tap.records.length
      await c.prompt(sid, turns[i])
      await tap.settled()
      for (const r of tap.records.slice(before)) labelOf.set(r.n, `effort=${effort} 第${i + 1}轮`)
      await Bun.sleep(2000)
    }
  } catch (e: any) {
    report.line(`**effort=${effort} 失败**：${e.message}`)
    if (c) report.block(c.stderr.slice(-1500))
  } finally {
    await c?.close()
  }
}
await tap.settled()
tap.stop()
usageTable(report, tap.records, r => labelOf.get(r.n) ?? '')
report.line('判读：看“历史里思考字数”一列在 high 组是否逐轮变大；若第一部分判定“计费”，high 组每轮输入会比 off 组多出大致这么多 token。')
report.line(`\n报告文件：${report.path}`)
