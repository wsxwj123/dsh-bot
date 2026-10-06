// M2 验收：预算与换段、写摘要时工具锁住、新段开头（长期记忆 + 摘要 + 最近原话）、空摘要不覆盖、私聊之间互不串、长期记忆
import { afterEach, expect, test } from 'bun:test'
import { existsSync, readFileSync, writeFileSync } from 'fs'
import { join } from 'path'
import { FakeTelegram } from '../fakes/fake-telegram'
import { cleanup, FRIEND, Gateway, makeBot, OWNER, prompts, readJsonl, sessionsState, sleep, toolResults, until, type BotEnv } from '../harness'

let tg: FakeTelegram | null = null
let b: BotEnv | null = null
let gw: Gateway | null = null

afterEach(async () => {
  await gw?.stop()
  tg?.stop()
  if (b) cleanup(b)
  tg = null; b = null; gw = null
})

const BUDGET = 2500

async function setup(o: { gw?: Record<string, unknown>; brain?: Record<string, unknown> } = {}) {
  tg = new FakeTelegram()
  b = makeBot(tg, {
    gw: { burst_window_ms: 0, seed_recent_chars: 600, ...o.gw },
    brain: { max_input_tokens: BUDGET, ...o.brain },
  })
  gw = new Gateway(b)
  await gw.start()
  return { tg, b, gw }
}

const summaries = (b: BotEnv) => readJsonl<{ sessionId: string; n: number; mode: string; withTools: boolean }>(join(b.acpState, 'summaries.jsonl'))

/** 发一条，等它的回复到了、这一轮也结束了（包括可能紧跟着的换段） */
async function say(tg: FakeTelegram, gw: Gateway, chat: number, text: string): Promise<void> {
  const mid = tg.pushText(chat, text)
  const expected = `收到：${text.replace(/![a-z]+(?::\S+)?/g, '').trim()}`
  await until(() => tg.sentTo(chat).some(s => s.text === expected), `reply to ${text}`)
  await until(() => {
    const l = gw.ledger()
    const st = l.inboundByKey(`tg:${chat}:${mid}`)?.state
    const busy = (l.db.query(`SELECT COUNT(*) AS n FROM turns WHERE state IN ('preparing','sent')`).get() as { n: number }).n
    l.close()
    return st === 'done' && busy === 0
  }, `turn settled for ${text}`)
}

test('聊 200 轮：每个请求都不超过预算，换段次数和预期一致', async () => {
  const { tg, b, gw } = await setup()
  for (let i = 1; i <= 200; i++) await say(tg, gw, OWNER, `第${String(i).padStart(3, '0')}条消息，${'随便聊聊今天的天气和吃饭的事'.repeat(2)}`)
  await sleep(300)
  const ps = prompts(b)
  // 按会话累加，算出每个请求发出时这个会话的历史有多大（假 ACP 回报的用量就是这个数）
  const size = new Map<string, number>()
  let maxChat = 0
  let maxSummary = 0
  for (const p of ps) {
    const now = (size.get(p.sessionId) ?? 0) + p.text.length
    size.set(p.sessionId, now)
    if (p.text.startsWith('⟦系统·整理记忆⟧')) maxSummary = Math.max(maxSummary, now)
    else maxChat = Math.max(maxChat, now)
  }
  // 聊天请求：不超过预算（留一条消息的余量：预算是在这一轮之后才看的）
  expect(maxChat).toBeLessThanOrEqual(BUDGET + 200)
  // 写摘要那次请求：就是到线的那个会话再加一段指令，不超过窗口（预算 / 80%）
  expect(maxSummary).toBeLessThanOrEqual(BUDGET / 0.8)
  const chatSessions = new Set(ps.filter(p => !p.text.startsWith('⟦系统·整理记忆⟧')).map(p => p.sessionId))
  const rolls = summaries(b).length
  const totalChars = ps.filter(p => !p.text.startsWith('⟦系统·整理记忆⟧')).reduce((n, p) => n + p.text.length, 0)
  expect(rolls).toBe(chatSessions.size - 1)
  expect(rolls).toBeGreaterThanOrEqual(Math.floor(totalChars / BUDGET))
  expect(rolls).toBeLessThanOrEqual(Math.ceil(totalChars / (BUDGET / 2)))
  // 每条消息只回了一次，没有因为换段多说或漏说
  expect(tg.sentTo(OWNER).length).toBe(200)
}, 180_000)

test('换段那一轮：工具锁住；摘要和最近原话（含 bot 真正发出去的话）进了新段开头', async () => {
  const { tg, b, gw } = await setup()
  let i = 0
  while (summaries(b).length === 0) await say(tg, gw, OWNER, `换段测试 ${++i} ${'一二三四五六七八九十'.repeat(5)}`)
  const sum = summaries(b)[0]!
  // 写摘要时试图调工具：被拒，用户什么也没收到
  const locked = toolResults(b).find(r => r.sessionId === sum.sessionId && r.name === 'react')!
  expect(locked.isError).toBe(true)
  expect(locked.text).toContain('整理记忆')
  expect(tg.reactions.some(r => r.emoji === '❤️')).toBe(false)
  expect(tg.sentTo(OWNER).length).toBe(i)
  // 换段之后的第一个聊天请求在新会话里：开头有摘要、最近原话（对方说的和 bot 真正发出去的），和新消息分成两个内容块
  await say(tg, gw, OWNER, '换段之后再说一句')
  const ps = prompts(b)
  const sumIdx = ps.findIndex(p => p.sessionId === sum.sessionId && p.text.startsWith('⟦系统·整理记忆⟧'))
  const first = ps.slice(sumIdx + 1).find(p => !p.text.startsWith('⟦系统·整理记忆⟧') && p.sessionId !== sum.sessionId)!
  expect(first).toBeDefined()
  expect(first.blocks).toBe(2)
  expect(first.text).toContain(`假摘要#${sum.n}`)
  // 和账本一致：新会话开头的最近原话，就是账本里在它之前最近的那几条（对方说的和 bot 真正发出去的）
  const led = gw.ledger()
  const firstMsg = first.text.split('\n').filter(l => l.startsWith('换段测试') || l.startsWith('换段之后')).at(-1)!
  const firstRow = led.db.query(`SELECT id FROM inbound WHERE text = ?`).get(firstMsg) as { id: number }
  // 只看新会话开头生成那一刻之前的记录，按同样的字数上限从新往旧取
  const all = led.recentTranscript(String(OWNER), { maxChars: 1e9, excludeInboundIds: [firstRow.id] }).filter(e => e.ts < first.at)
  led.close()
  const tr: typeof all = []
  let used = 0
  for (const e of [...all].reverse()) {
    if (used + e.text.length > 600 && tr.length > 0) break
    tr.unshift(e)
    used += e.text.length
  }
  expect(tr.length).toBeGreaterThan(1)
  expect(tr.some(e => e.who === 'bot')).toBe(true)
  for (const e of tr) expect(first.text).toContain(`${e.who === 'bot' ? '你' : '对方'}：${e.text}`)
})

test('新消息一进来就会超预算：先换段再处理', async () => {
  const { tg, b, gw } = await setup()
  await say(tg, gw, OWNER, '先说一句')
  await say(tg, gw, OWNER, '再说一句') // 一段里至少聊过两轮才会换段
  const long = `很长的一条 ${'长'.repeat(BUDGET - 100)}`
  await say(tg, gw, OWNER, long)
  const p = prompts(b).find(x => x.text.includes('很长的一条'))!
  const before = prompts(b).filter(x => x.sessionId === p.sessionId && x.at < p.at)
  expect(before.length).toBe(0) // 长消息是新会话的第一个请求（先换段再处理）
  expect(summaries(b).length).toBeGreaterThanOrEqual(1) // 处理完它又到线了，可能紧接着再换一次
})

test('摘要为空：不覆盖上一份', async () => {
  const { tg, b, gw } = await setup({ brain: { max_input_tokens: 100_000 } })
  await say(tg, gw, OWNER, '第一段的话')
  tg.pushText(OWNER, '/clear')
  await until(() => tg.sentTo(OWNER).some(s => s.text?.includes('已清空')), 'first clear')
  await say(tg, gw, OWNER, '第二段的话')
  const p2 = prompts(b).find(x => x.text.includes('第二段的话'))!
  expect(p2.text).toContain('假摘要#1')
  expect(p2.text).not.toContain('第一段的话') // 清空之后不带原话
  writeFileSync(join(b.acpState, 'summary-mode'), 'empty')
  tg.pushText(OWNER, '/clear')
  await until(() => tg.sentTo(OWNER).filter(s => s.text?.includes('已清空')).length === 2, 'second clear')
  await say(tg, gw, OWNER, '第三段的话')
  const p3 = prompts(b).find(x => x.text.includes('第三段的话'))!
  expect(p3.text).toContain('假摘要#1') // 第二次的摘要是空的，沿用第一份
  const led = gw.ledger()
  const segs = led.db.query(`SELECT summary FROM segments ORDER BY id`).all() as { summary: string | null }[]
  led.close()
  expect(segs[1]!.summary).toBeNull()
})

test('两个私聊各用各的会话：请求里从不出现另一个聊天的话', async () => {
  const { tg, b, gw } = await setup({ brain: { max_input_tokens: 1000 } })
  const filler = '今天聊点别的事情吧，比如周末去哪里玩，吃点什么好吃的'
  for (let i = 0; i < 15; i++) {
    await say(tg, gw, OWNER, `主人专属话题${i} ${filler}`)
    await say(tg, gw, FRIEND, `朋友专属话题${i} ${filler}`)
  }
  expect(summaries(b).length).toBeGreaterThan(0) // 中间换过段，前情也只补自己聊天的
  for (const s of Object.values(sessionsState(b))) {
    const h = s.history.join('\n')
    expect(h.includes('主人专属话题') && h.includes('朋友专属话题')).toBe(false)
  }
})

test('长期记忆：remember 写进 MEMORY.md，别的聊天下一轮会被提醒，新段开头带上', async () => {
  const { tg, b, gw } = await setup({ brain: { max_input_tokens: 100_000 } })
  await say(tg, gw, FRIEND, '朋友先打个招呼')
  await say(tg, gw, OWNER, '我最喜欢橘猫 !remember:对方最喜欢橘猫')
  const mem = join(b.channelDir, 'memory', 'MEMORY.md')
  expect(existsSync(mem)).toBe(true)
  expect(readFileSync(mem, 'utf8')).toMatch(/## 随手记\n- \d{4}-\d{2}-\d{2} 对方最喜欢橘猫/)
  expect(toolResults(b).find(r => r.name === 'remember')!.text).toBe('记下了。')
  // 别的聊天：下一轮前面有一句提醒，只提一次
  await say(tg, gw, FRIEND, '朋友的第二句')
  await say(tg, gw, FRIEND, '朋友的第三句')
  const f2 = prompts(b).find(p => p.text.includes('朋友的第二句'))!
  const f3 = prompts(b).find(p => p.text.includes('朋友的第三句'))!
  expect(f2.text).toContain('你在别的聊天里新记下了')
  expect(f2.text).toContain('对方最喜欢橘猫')
  expect(f3.text).not.toContain('你在别的聊天里新记下了')
  // 记过的同一件事不重复记
  await say(tg, gw, OWNER, '再说一遍 !remember:对方最喜欢橘猫')
  expect(readFileSync(mem, 'utf8').split('对方最喜欢橘猫').length - 1).toBe(1)
  // 新段开头带上长期记忆
  tg.pushText(OWNER, '/clear')
  await until(() => tg.sentTo(OWNER).some(s => s.text?.includes('已清空')), 'clear')
  await say(tg, gw, OWNER, '清空后的第一句')
  const p = prompts(b).find(x => x.text.includes('清空后的第一句'))!
  expect(p.text).toContain('⟦长期记忆')
  expect(p.text).toContain('对方最喜欢橘猫')
})

test('预算比新段开头还小：不会每轮都换段', async () => {
  const { tg, b, gw } = await setup({ brain: { max_input_tokens: 50 } })
  for (let i = 0; i < 6; i++) await say(tg, gw, OWNER, `预算太小的第${i}句`)
  // 每段至少聊两轮才换：6 轮最多换 3 次
  expect(summaries(b).length).toBeLessThanOrEqual(3)
  expect(tg.sentTo(OWNER).length).toBe(6)
})
