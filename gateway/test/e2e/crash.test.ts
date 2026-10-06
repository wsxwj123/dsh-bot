// M1 验收 3：在四个时刻强杀网关，重启后：不丢、模型不会两次看到同一条、用户不会两次收到同一句。
// 判断依据只看外部：假 Telegram 收到的发送、假模型（假 ACP）看到的历史、账本。
import { afterEach, expect, test } from 'bun:test'
import { FakeTelegram } from '../fakes/fake-telegram'
import { isAlive } from '../../src/dsh/process'
import { join } from 'path'
import { cleanup, Gateway, lifecycle, makeBot, maxTimesSeen, OWNER, prompts, readJsonl, sleep, until, type BotEnv } from '../harness'

let tg: FakeTelegram | null = null
let b: BotEnv | null = null
let gw: Gateway | null = null

afterEach(async () => {
  await gw?.stop()
  tg?.stop()
  if (b) cleanup(b)
  tg = null; b = null; gw = null
})

function setup() {
  tg = new FakeTelegram()
  b = makeBot(tg)
  gw = new Gateway(b)
  return { tg, b, gw }
}

const texts = (t: FakeTelegram, needle: string) => t.sentTo(OWNER).filter(s => s.text?.includes(needle)).map(s => s.text)

test('时刻一：消息刚记进账本、还没处理', async () => {
  const { tg, b, gw } = setup()
  await gw.start({ DSH_BOT_CRASH_AT: 'after_inbound' })
  const mid = tg.pushText(OWNER, 'crashA')
  expect(await gw.waitExit()).not.toBe(0)
  await gw.start()
  await until(() => texts(tg, 'crashA').length > 0, 'reply after restart')
  await sleep(800)
  expect(texts(tg, 'crashA')).toEqual(['收到：crashA'])
  expect(maxTimesSeen(b, 'crashA')).toBe(1)
  const led = gw.ledger()
  expect(led.inboundByKey(`tg:${OWNER}:${mid}`)!.state).toBe('done')
  led.close()
})

test('时刻二：消息已经送进 dsh，模型还在想', async () => {
  const { tg, b, gw } = setup()
  await gw.start()
  tg.pushText(OWNER, 'warmupB')
  await until(() => texts(tg, 'warmupB').length > 0, 'warmup reply')
  const mid = tg.pushText(OWNER, 'crashB !hang:1:b')
  await until(() => prompts(b).some(p => p.text.includes('crashB')), 'prompt reached dsh')
  const oldDsh = prompts(b).find(p => p.text.includes('crashB'))!.pid
  await gw.kill()
  // Mac / Linux：网关一死，dsh 的标准输入关闭，自己退出。
  // Windows：子进程可能还挂着；网关重启时按 pid 文件核对命令行后清理掉它。
  if (process.platform !== 'win32') await until(() => lifecycle(b).some(l => l.event === 'stdin-closed'), 'old dsh exits when the gateway dies', 10_000)
  await gw.start()
  await until(() => !isAlive(oldDsh), 'old dsh is gone after restart', 15_000)
  await until(() => texts(tg, 'crashB').length > 0, 'reply after restart')
  await sleep(800)
  expect(texts(tg, 'crashB')).toEqual(['收到：crashB'])
  // 旧会话里有一次（dsh 已经记进历史），新会话里有一次；同一个会话里从没出现两次
  expect(maxTimesSeen(b, 'crashB')).toBe(1)
  const ps = prompts(b).filter(p => p.text.includes('crashB'))
  expect(new Set(ps.map(p => p.sessionId)).size).toBe(ps.length)
  // 新会话开头补了前情（上一轮的原话），并且前情和新消息是两个内容块
  const first = prompts(b).find(p => p.sessionId === ps[ps.length - 1]!.sessionId)!
  expect(first.blocks).toBe(2)
  expect(first.text).toContain('对方：warmupB')
  expect(first.text).toContain('你：收到：warmupB')
  // 旧会话用不了：用账本流水在一个不带工具的临时会话里补写了摘要，带进了新段开头；补写摘要的材料里没有那条还没处理完的消息
  const sums = readJsonl<{ sessionId: string; n: number; withTools: boolean }>(join(b.acpState, 'summaries.jsonl'))
  expect(sums.length).toBe(1)
  expect(sums[0]!.withTools).toBe(false)
  expect(first.text).toContain(`假摘要#${sums[0]!.n}`)
  const material = prompts(b).find(p => p.sessionId === sums[0]!.sessionId)!
  expect(material.text).toContain('对方：warmupB')
  expect(material.text).not.toContain('crashB')
  const led = gw.ledger()
  const row = led.inboundByKey(`tg:${OWNER}:${mid}`)!
  expect(row.state).toBe('done')
  expect(led.db.query(`SELECT COUNT(*) AS n FROM turns WHERE state = 'crashed'`).get()).toEqual({ n: 1 })
  expect(led.db.query(`SELECT close_reason FROM segments WHERE id = 1`).get()).toEqual({ close_reason: 'crash' })
  led.close()
})

test('时刻三：回复发出一半', async () => {
  const { tg, b, gw } = setup()
  await gw.start({ DSH_BOT_CRASH_AT: 'mid_reply' })
  const mid = tg.pushText(OWNER, 'crashC !parts:3')
  expect(await gw.waitExit()).not.toBe(0)
  expect(texts(tg, 'crashC')).toEqual(['收到：crashC（第1段）'])
  await gw.start()
  await sleep(1_500)
  // 已经有一段送达：宁可少说，也不重复。这条消息不会再作为新消息交给模型。
  expect(texts(tg, 'crashC')).toEqual(['收到：crashC（第1段）'])
  expect(prompts(b).filter(p => p.text.includes('crashC')).length).toBe(1)
  // 下一条消息进新会话：前情里有发出去的那一段，并且注明上一条回复只发出了前 1 段
  tg.pushText(OWNER, 'afterC')
  await until(() => texts(tg, 'afterC').length > 0, 'reply after restart')
  const p = prompts(b).find(x => x.text.includes('afterC'))!
  expect(p.text).toContain('你：收到：crashC（第1段）')
  expect(p.text).toContain('本来要发 3 段，对方只收到了前 1 段')
  expect(texts(tg, 'crashC')).toEqual(['收到：crashC（第1段）'])
  const led = gw.ledger()
  expect(led.inboundByKey(`tg:${OWNER}:${mid}`)!.state).toBe('done')
  led.close()
})

test('时刻四：回复发完了，还没来得及记账', async () => {
  const { tg, b, gw } = setup()
  await gw.start({ DSH_BOT_CRASH_AT: 'after_send_before_record' })
  const mid = tg.pushText(OWNER, 'crashD')
  expect(await gw.waitExit()).not.toBe(0)
  await gw.start()
  await sleep(1_500)
  expect(texts(tg, 'crashD')).toEqual(['收到：crashD'])
  expect(prompts(b).filter(p => p.text.includes('crashD')).length).toBe(1)
  const led = gw.ledger()
  expect(led.inboundByKey(`tg:${OWNER}:${mid}`)!.state).toBe('done')
  expect(led.db.query(`SELECT state FROM outbound WHERE text = '收到：crashD'`).get()).toEqual({ state: 'ambiguous' })
  led.close()
  // 之后照常聊
  tg.pushText(OWNER, 'afterD')
  await until(() => texts(tg, 'afterD').length > 0, 'normal reply afterwards')
})
