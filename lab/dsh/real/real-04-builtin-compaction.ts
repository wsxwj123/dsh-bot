// 真密钥验证 04：dsh 出厂压缩（compaction-basic）在真实分词下何时触发、写出的摘要长什么样；
// 顺带核对 dsh 自己估的上下文大小（ACP usage_update.used，压缩就是拿它和阈值比）与 DeepSeek 回报的输入 token 差多少。
// 做法：先开一个会话发一句话，读出 dsh 估的大小 U0；把阈值设成 U0 + 1500（--extra 可改），保留最近 2000 token 原文；
// 然后分别用“保留最近 2000 / 500 token 原文”各聊最多 20 轮，记录何时压缩、压缩写出什么。
// 用法：DEEPSEEK_API_KEY=sk-... DSH=<dsh 路径> bun lab/dsh/real/real-04-builtin-compaction.ts [--persona 路径] [--extra 1500]
import { AcpClient } from '../lib/acp-client'
import { dshVersion, personaOnlyRows, runsDir } from '../lib/profile'
import { parseArgs, requireKey, sandboxFor, startBot, startDeepseekTap, newSession, usageTable } from '../lib/real'
import { Report } from '../lib/report'

requireKey()
const args = parseArgs()
const extraArg = process.argv.indexOf('--extra')
const EXTRA = extraArg >= 0 ? Number(process.argv[extraArg + 1]) : 1500
const out = runsDir(`real-04-${Date.now()}`)
const report = new Report(out, '真密钥验证 04：出厂压缩何时触发')
report.line(`dsh 版本：${await dshVersion()}；模型：${args.model}；思考强度：${args.effort}；人设：${args.personaLabel}；只出数字：${args.numbersOnly ? '是' : '否'}`)

const tap = startDeepseekTap(args.tapPort)
const route = { kind: 'deepseek' as const, model: args.model }
const labelOf = new Map<number, string>()
const lastUsed = (c: AcpClient, from: number) => {
  const u = c.updates.slice(from).filter(x => x.update?.sessionUpdate === 'usage_update').pop()?.update
  return u ? { used: u.used as number, size: u.size as number } : undefined
}

// ── 1. 量基线：dsh 估的大小 U0 与上下文窗口 W ──
let u0: { used: number; size: number } | undefined
{
  const sb = sandboxFor(out, 'probe', personaOnlyRows({ persona: args.persona, route, compaction: 'off' }))
  const c = await startBot(sb, tap)
  try {
    const sid = await newSession(c, sb, { effort: args.effort })
    const before = tap.records.length
    const from = c.updates.length
    await c.prompt(sid, '你好')
    await tap.settled()
    for (const r of tap.records.slice(before)) labelOf.set(r.n, '量基线')
    u0 = lastUsed(c, from)
  } finally { await c.close() }
}
if (!u0) { report.line('没有拿到 usage_update（dsh 不知道该模型的上下文窗口），停止。'); tap.stop(); process.exit(1) }
const thresholdTokens = u0.used + EXTRA
const ratio = Number((thresholdTokens / u0.size).toFixed(8))
report.line(`dsh 估的基线 U0 = ${u0.used}，上下文窗口 W = ${u0.size}；阈值 = ${thresholdTokens}（thresholdRatio=${ratio}）；保留最近 2000 token 原文。`)

// ── 2. 两种“保留最近原文”的设置各聊最多 20 轮 ──
// dsh 给单条消息估 token 时按“每 4 个字符 1 个”算（两个版本源码里都是 CHARS_PER_TOKEN = 4），中文会被低估好几倍；
// 而“是否超过阈值”用的是对得上 DeepSeek 回报的总量。两者口径不一，保留量设大了可能永远找不到可压的部分。
type VariantResult = { retain: number; compactTurns: number[]; perTurn: (string | number | undefined)[][]; summary?: string; error?: string }
const results: VariantResult[] = []
for (const retain of [2000, 500]) {
  const vr: VariantResult = { retain, compactTurns: [], perTurn: [] }
  results.push(vr)
  const sb = sandboxFor(out, `bot-retain${retain}`, personaOnlyRows({ persona: args.persona, route, compaction: { thresholdRatio: ratio, retainTokens: retain } }))
  let c: AcpClient | undefined
  try {
    c = await startBot(sb, tap)
    const sid = await newSession(c, sb, { effort: args.effort })
    const days = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
    for (let i = 1; i <= 20 && vr.compactTurns.length < 3; i++) {
      const text = `第${i}条：跟你说说我${days[i % 7]}的事。早上挤地铁差点迟到，中午和同事吃了楼下的牛肉面，下午开了两个会，`
        + '晚上回家给自己炒了个番茄炒蛋，然后窝在沙发上看了会儿纪录片，讲的是深海里的鱼，挺有意思的。你今天过得怎么样？'
      const before = tap.records.length
      const from = c.updates.length
      await c.prompt(sid, text)
      await tap.settled()
      const recs = tap.records.slice(before)
      for (const r of recs) labelOf.set(r.n, `保留${retain} 第${i}轮`)
      const comp = recs.filter(r => r.purpose === 'compaction')
      if (comp.length) { vr.compactTurns.push(i); vr.summary ??= comp[0].outputText }
      const chat = recs.filter(r => r.purpose === 'chat').pop()
      vr.perTurn.push([i, lastUsed(c, from)?.used, chat?.usage.input, comp.length || ''])
    }
  } catch (e: any) {
    vr.error = e.message
  } finally {
    await c?.close()
  }
}
await tap.settled()
tap.stop()

for (const vr of results) {
  report.h(2, `保留最近 ${vr.retain} token：每轮 dsh 估的大小 vs DeepSeek 回报的输入`)
  if (vr.error) report.line(`**失败**：${vr.error}`)
  report.table(['轮', 'dsh 估（usage_update.used）', 'DeepSeek 回报的输入', '本轮压缩请求数'], vr.perTurn)
  const maxUsed = Math.max(0, ...vr.perTurn.map(r => Number(r[1]) || 0))
  report.line(vr.compactTurns.length
    ? `在第 ${vr.compactTurns.join('、')} 轮发生压缩。`
    : maxUsed >= thresholdTokens
      ? `没有发生压缩，但 dsh 估值最高 ${maxUsed} 已超过阈值 ${thresholdTokens}。`
      : `没有发生压缩：dsh 估值最高 ${maxUsed}，没到阈值 ${thresholdTokens}（可用 --extra 调低阈值重跑）。`)
  if (vr.summary) {
    report.line(`摘要是否为英文“编程助手检查点”格式（含 ## Primary Request and Intent）：${/Primary Request and Intent/.test(vr.summary) ? '是' : '否'}；摘要字数 ${vr.summary.length}`)
    if (!args.numbersOnly) { report.h(3, '出厂压缩写出的摘要（前 1500 字）'); report.block(vr.summary.slice(0, 1500)) }
  }
}
report.h(2, '每个请求的用量')
usageTable(report, tap.records, r => labelOf.get(r.n) ?? '')
report.h(2, '预期')
report.line('- 保留 2000：估值越过阈值后很可能一直不压缩，上下文继续长——中文被按 4 字符 1 token 低估，dsh 认为最近 2000 token 就是全部对话。')
report.line('- 保留 500：会压缩，看两次压缩相隔几轮、每次多花多少 token。')
report.line('- “dsh 估”与“DeepSeek 回报”两列应大致同步（差异在一两成以内）。差得多，说明新系统不能只看 dsh 的估值来控预算。')
report.line('- 摘要是英文的“编程助手检查点”格式（## Primary Request and Intent、## Files and Code 等小标题），不适合陪伴聊天——这正是新系统不用它的原因。')
report.line(`\n报告文件：${report.path}`)
