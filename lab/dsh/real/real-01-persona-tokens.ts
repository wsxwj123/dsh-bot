// 真密钥验证 01：真实对话 + 「只留人设」后一次请求的实际输入 token 数。
// 三种配置各开一个新会话、发同样的两句话，对比 DeepSeek 回报的输入 token 与缓存命中：
//   A 出厂 acp 配置 + 人设（把人设原样搬进 dsh、其它不动）
//   B 只留人设（新系统的配法）
//   C 出厂 acp 配置、不放人设（dsh 自带的固定开销）
// 用法：DEEPSEEK_API_KEY=sk-... DSH=<dsh 路径> bun lab/dsh/real/real-01-persona-tokens.ts [--persona 路径] [--model deepseek-v4-flash] [--effort low]
import { AcpClient } from '../lib/acp-client'
import { dshVersion, personaOnlyRows, runsDir, shippedRows } from '../lib/profile'
import { parseArgs, requireKey, sandboxFor, startBot, startDeepseekTap, newSession, usageTable } from '../lib/real'
import { Report } from '../lib/report'

requireKey()
const args = parseArgs()
const out = runsDir(`real-01-${Date.now()}`)
const report = new Report(out, '真密钥验证 01：只留人设后的实际输入 token')
report.line(`dsh 版本：${await dshVersion()}；模型：${args.model}；思考强度：${args.effort}；人设：${args.personaLabel}（${args.persona.length} 字符）；只出数字：${args.numbersOnly ? '是' : '否'}`)

const tap = startDeepseekTap(args.tapPort)
const route = { kind: 'deepseek' as const, model: args.model }
const configs: [string, object[]][] = [
  ['A 出厂+人设', shippedRows({ route, persona: args.persona })],
  ['B 只留人设', personaOnlyRows({ persona: args.persona, route })],
  ['C 出厂无人设', shippedRows({ route })],
]
const turns = ['你好呀，在吗？', '今天有点累，刚下班。']
const labelOf = new Map<number, string>()
const replies: string[][] = []
for (const [name, rows] of configs) {
  const sb = sandboxFor(out, name.split(' ')[0], rows)
  let c: AcpClient | undefined
  try {
    c = await startBot(sb, tap)
    const sid = await newSession(c, sb, { effort: args.effort })
    for (const t of turns) {
      const before = tap.records.length
      const from = c.updates.length
      const r = await c.prompt(sid, t)
      await tap.settled()
      for (const rec of tap.records.slice(before)) labelOf.set(rec.n, `${name} · 第${turns.indexOf(t) + 1}轮`)
      replies.push([name, t, r.stopReason, c.messageText(sid, from)])
      await Bun.sleep(3000) // 给供应商建缓存留一点时间
    }
  } catch (e: any) {
    report.line(`**${name} 失败**：${e.message}`)
    if (c) report.block(c.stderr.slice(-1500))
  } finally {
    await c?.close()
  }
}
await tap.settled()
tap.stop()

report.h(2, '每个请求的用量（DeepSeek 回报值）')
usageTable(report, tap.records, r => labelOf.get(r.n) ?? '')
if (args.numbersOnly) {
  report.h(2, '模型可见回复')
  report.line('（只出数字模式：不记录回复内容。）')
  report.table(['配置', '发送', 'stopReason', '回复字数'], replies.map(([n, t, s, r]) => [n, t, s, r.length]))
} else {
  report.h(2, '模型可见回复（只截前 60 字）')
  report.table(['配置', '发送', 'stopReason', '回复（模型直接输出的文本）'], replies.map(([n, t, st, r]) => [n, t, st, r.slice(0, 60)]))
}

const first = (name: string) => tap.records.find(r => labelOf.get(r.n) === `${name} · 第1轮`)?.usage.input
const a = first('A 出厂+人设'), b = first('B 只留人设'), cc = first('C 出厂无人设')
report.h(2, '结论（脚本自动算）')
report.line(`- B 首轮输入 = ${b ?? '?'} token：这就是「人设 + 一句话」的真实成本。`)
report.line(`- A − B = ${a !== undefined && b !== undefined ? a - b : '?'} token：出厂配置额外带的工具说明与注入上下文。`)
report.line(`- C 首轮输入 = ${cc ?? '?'} token：dsh 出厂固定开销（不含人设）。`)
report.line('')
report.line('预期：B 首轮 ≈ 人设 token 数 + 几十；A − B 在 0.1.5 上约 7000–9000、在 0.2.0 上约 5000–7000；')
report.line('第 2 轮的“缓存命中”应接近第 1 轮的输入（前缀被复用），未命中只剩新增的那几句。')
report.line(`\n报告文件：${report.path}`)
