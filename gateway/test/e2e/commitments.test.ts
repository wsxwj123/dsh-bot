// M3：承诺（登记、兜底识别、到点、睡觉顺延、兑现判定）和私聊里的"生活状态"附带
import { afterEach, expect, test } from 'bun:test'
import { mkdirSync, writeFileSync } from 'fs'
import { join } from 'path'
import { FakeTelegram } from '../fakes/fake-telegram'
import { cleanup, Gateway, makeBot, OWNER, prompts, readJsonl, toolResults, until, type BotEnv } from '../harness'

let tg: FakeTelegram | null = null
let b: BotEnv | null = null
let gw: Gateway | null = null

afterEach(async () => {
  await gw?.stop()
  tg?.stop()
  if (b) cleanup(b)
  tg = null; b = null; gw = null
})

async function setup(gwOpts: Record<string, unknown> = {}) {
  tg = new FakeTelegram()
  b = makeBot(tg, { gw: { burst_window_ms: 0, ...gwOpts } })
  mkdirSync(b.acpState, { recursive: true })
  gw = new Gateway(b)
  await gw.start()
  return { tg, b, gw }
}

async function say(tg: FakeTelegram, text: string, reply?: string): Promise<void> {
  const n = tg.sentTo(OWNER).length
  tg.pushText(OWNER, text)
  const want = reply ?? `收到：${text.replace(/![a-z]+(?::\S+)?/g, '').trim()}`
  await until(() => tg.sentTo(OWNER).slice(n).some(s => s.text === want), `reply to ${text}`)
}

const commitments = (gw: Gateway) => { const l = gw.ledger(); const r = l.db.query('SELECT * FROM commitments ORDER BY id').all() as any[]; l.close(); return r }
const situation = (b: BotEnv, o: object) => writeFileSync(join(b.acpState, 'situation.json'), JSON.stringify({ interruptible: true, term_label: '', wakes: [], free_at: null, ...o }))
const events = (b: BotEnv, e: string) => readJsonl<Record<string, any>>(join(b.botDir, 'logs', 'gateway.log')).filter(r => r.event === e)

test('commitment_create 登记 → 到点触发一轮 → 真正送达才算兑现', async () => {
  const { tg, b, gw } = await setup()
  const due = new Date(Date.now() + 2500).toISOString()
  await say(tg, `好呀 !commit:提醒对方喝水|${due}`)
  expect(toolResults(b).find(r => r.name === 'commitment_create')!.text).toContain('已登记 #1')
  await until(() => prompts(b).some(p => p.text.includes('⟦系统·承诺到期⟧') && p.text.includes('提醒对方喝水')), 'due prompt', 20_000)
  await until(() => commitments(gw)[0]?.state === 'done', 'commitment done', 20_000)
})

test('兜底：说了"半小时后提醒你"却没登记 → 自动登记并告诉模型；含糊的说法提醒它自己登记', async () => {
  const { tg, b, gw } = await setup()
  await say(tg, '!say:好的，半小时后提醒你喝水', '好的，半小时后提醒你喝水')
  await until(() => commitments(gw).length > 0, 'auto commitment') // 回复送到之后，这一轮才收尾、做兜底识别
  const c = commitments(gw)
  expect(c.length).toBe(1)
  expect(c[0].source).toBe('auto')
  expect(Math.abs(c[0].due_at - Date.now() - 30 * 60_000)).toBeLessThan(60_000)
  await say(tg, '嗯嗯')
  expect(prompts(b).find(p => p.text.includes('嗯嗯'))!.text).toContain(`程序已替你登记成承诺 #${c[0].id}`)
  await say(tg, '!say:晚点告诉你', '晚点告诉你')
  await say(tg, '好')
  expect(prompts(b).find(p => p.text.endsWith('好'))!.text).toContain('像是答应了对方什么')
  await say(tg, '还有呢')
  const last = prompts(b).find(p => p.text.includes('还有呢'))!.text
  expect(last).not.toContain('⟦系统·承诺⟧') // 只提醒一次
})

test('到点时在睡觉：顺延到起床，提示里写明迟了多久', async () => {
  const { b, gw } = await setup()
  situation(b, { name: '睡觉中', state: 'sleeping', wakes: [Math.floor(Date.now() / 1000) + 3] })
  const l = gw.ledger()
  l.addCommitment({ chatId: String(OWNER), text: '叫对方起床', dueAt: Date.now() - 20 * 60_000, source: 'tool' })
  l.close()
  await until(() => events(b, 'commitment.deferred').length > 0, 'deferred while sleeping')
  expect(prompts(b).some(p => p.text.includes('⟦系统·承诺到期⟧'))).toBe(false)
  await until(() => prompts(b).some(p => p.text.includes('⟦系统·承诺到期⟧')), 'fires after waking', 20_000)
  const p = prompts(b).find(x => x.text.includes('⟦系统·承诺到期⟧'))!.text
  expect(p).toContain('叫对方起床')
  expect(p).toMatch(/晚了 2\d 分钟（那会儿你在睡觉）/)
})

test('到点那一轮没开口不算兑现：重试有上限，最后标记失败并通知主人', async () => {
  const { tg, b, gw } = await setup({ commit_retry_ms: [200, 200] })
  writeFileSync(join(b.acpState, 'due-mode'), 'mute')
  const l = gw.ledger()
  l.addCommitment({ chatId: String(OWNER), text: '提醒对方交作业', dueAt: Date.now(), source: 'tool' })
  l.close()
  await until(() => commitments(gw)[0]?.state === 'failed', 'failed after retries', 30_000)
  expect(prompts(b).filter(p => p.text.includes('⟦系统·承诺到期⟧')).length).toBe(3)
  expect(prompts(b).some(p => p.text.includes('这是第 3 次提醒'))).toBe(true)
  await until(() => tg.sentTo(OWNER).some(s => s.text?.includes('提醒了 3 次都没开口')), 'owner notified')
})

test('生活状态：关系提示、"此刻在干什么"只在新会话开头和变化时附带；被晾的迟到反应带一次', async () => {
  const { tg, b } = await setup()
  writeFileSync(join(b.channelDir, 'relationship.json'), JSON.stringify({ prompt_snippet: '【当前关系状态】测试好感五十' }))
  situation(b, { name: '在图书馆', state: 'busy_other' })
  await new Promise(r => setTimeout(r, 600)) // 等作息查询刷新
  const at = (s: string) => prompts(b).find(p => p.text.includes(s))!.text
  await say(tg, '第一句')
  expect(at('第一句')).toContain('⟦关系状态⟧ 【当前关系状态】测试好感五十')
  expect(at('第一句')).toContain('⟦你此刻：在图书馆⟧')
  await say(tg, '第二句')
  expect(at('第二句')).not.toContain('⟦关系状态⟧')
  expect(at('第二句')).not.toContain('⟦你此刻')
  writeFileSync(join(b.channelDir, 'relationship.json'), JSON.stringify({ prompt_snippet: '【当前关系状态】测试好感六十' }))
  await say(tg, '第三句')
  expect(at('第三句')).toContain('测试好感六十')
  writeFileSync(join(b.botDir, 'state', '.hang-archive.json'), JSON.stringify({ [String(OWNER)]: { minutes: 30, activity: '上课', at: Date.now() } }))
  await say(tg, '第四句')
  expect(at('第四句')).toContain('⟦系统·被晾⟧ 他那条消息你隔了 30 分钟才看见，那会儿你在「上课」')
  await say(tg, '第五句')
  expect(at('第五句')).not.toContain('⟦系统·被晾⟧')
})

test('复述还没兑现的承诺不会被当成新许诺再登记一次；到点时取消了就不再追问', async () => {
  const { tg, b, gw } = await setup()
  await say(tg, `好呀 !commit:提醒对方喝水|${new Date(Date.now() + 10 * 60_000).toISOString()}`)
  await say(tg, '!say:我答应过你，10分钟后提醒你喝水', '我答应过你，10分钟后提醒你喝水')
  expect(commitments(gw).length).toBe(1)
  expect(events(b, 'commitment.restated').length).toBe(1)
  // 到点：模型觉得不需要了，取消（没回复，也没调 stay_silent）→ 不追一句"你还没回复"
  writeFileSync(join(b.acpState, 'due-mode'), 'cancel')
  const l = gw.ledger()
  l.addCommitment({ chatId: String(OWNER), text: '提醒对方收快递', dueAt: Date.now(), source: 'tool' })
  l.close()
  await until(() => commitments(gw)[1]?.state === 'cancelled', 'cancelled at due', 20_000)
  await new Promise(r => setTimeout(r, 800))
  const led = gw.ledger()
  const nudges = (led.db.query(`SELECT COUNT(*) AS n FROM turns WHERE kind = 'nudge'`).get() as { n: number }).n
  led.close()
  expect(nudges).toBe(0)
})
