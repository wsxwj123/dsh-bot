// M1 验收 4、5、6：发送异常、模型连续报错、模型不回应
import { afterEach, expect, test } from 'bun:test'
import { writeFileSync } from 'fs'
import { join } from 'path'
import { FakeTelegram } from '../fakes/fake-telegram'
import { cleanup, FRIEND, Gateway, makeBot, OWNER, prompts, sleep, toolResults, until, type BotEnv } from '../harness'

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
  b = makeBot(tg, { gw: gwOpts })
  gw = new Gateway(b)
  await gw.start()
  return { tg, b, gw }
}

const texts = (t: FakeTelegram, chat: number, needle: string) => t.sentTo(chat).filter(s => s.text?.includes(needle))

test('引用的消息不存在：去掉引用重发', async () => {
  const { tg, b } = await setup()
  tg.faults.push({ method: 'sendMessage', when: p => !!p.reply_parameters, status: 400, description: 'Bad Request: message to be replied not found' })
  tg.pushText(OWNER, 'quoteE !replyto:424242')
  await until(() => texts(tg, OWNER, 'quoteE').length > 0, 'reply')
  const s = texts(tg, OWNER, 'quoteE')
  expect(s.length).toBe(1)
  expect(s[0]!.replyTo).toBeUndefined()
  const tried = tg.calls.filter(c => c.method === 'sendMessage' && String(c.params.text).includes('quoteE'))
  expect(tried.length).toBe(2)
  expect(tried[0]!.params.reply_parameters.message_id).toBe(424242)
  // 假模型拿到工具结果后才写记录，比 Telegram 收到消息晚一点
  await until(() => toolResults(b).some(r => r.name === 'reply'), 'tool result recorded')
  expect(toolResults(b).find(r => r.name === 'reply')!.text).toContain('已送达 1/1 段')
})

test('第 2 段被拒（400）：模型收到"第 2 段未送达"，其余段照发', async () => {
  const { tg, b } = await setup()
  tg.faults.push({ method: 'sendMessage', when: p => String(p.text).includes('（第2段）'), status: 400, description: 'Bad Request: message is too long' })
  tg.pushText(OWNER, 'partsF !parts:3')
  await until(() => toolResults(b).some(r => r.name === 'reply'), 'tool result')
  const r = toolResults(b).find(r => r.name === 'reply')!
  expect(r.text).toContain('送达 2/3 段')
  expect(r.text).toContain('第 2 段未送达')
  expect(texts(tg, OWNER, 'partsF').map(s => s.text)).toEqual(['收到：partsF（第1段）', '收到：partsF（第3段）'])
})

test('429 要等很久：如实回报，不阻塞其它聊天', async () => {
  const { tg, b } = await setup({ max_send_wait_ms: 300 })
  tg.faults.push({ method: 'sendMessage', when: p => String(p.chat_id) === String(OWNER), status: 429, description: 'Too Many Requests: retry after 60', retryAfter: 60, times: 5 })
  tg.pushText(OWNER, 'slowG !parts:2 !slow:300')
  tg.pushText(FRIEND, 'friendG')
  await until(() => texts(tg, FRIEND, 'friendG').length > 0, 'other chat replied')
  await until(() => toolResults(b).some(r => r.text.includes('限流')), 'rate limit reported')
  const r = toolResults(b).find(r => r.text.includes('限流'))!
  expect(r.text).toContain('第 1 段未送达')
  expect(r.text).toContain('60 秒')
  expect(r.text).toContain('第 2 段未送达')
  expect(texts(tg, OWNER, 'slowG').length).toBe(0)
})

test('发文件失败：前面已经发出的段不会重复', async () => {
  const { tg, b } = await setup()
  const img = join(b.botDir, 'media', 'pic.png')
  writeFileSync(img, 'not really a png')
  tg.faults.push({ method: 'sendPhoto', status: 400, description: 'Bad Request: IMAGE_PROCESS_FAILED' })
  tg.pushText(OWNER, `fileH !parts:2 !file:${img}`)
  await until(() => toolResults(b).some(r => r.name === 'reply'), 'tool result')
  await sleep(500)
  expect(texts(tg, OWNER, 'fileH').map(s => s.text)).toEqual(['收到：fileH（第1段）', '收到：fileH（第2段）'])
  expect(tg.calls.filter(c => c.method === 'sendPhoto').length).toBe(1)
  expect(toolResults(b).find(r => r.name === 'reply')!.text).toContain('第 3 段未送达')
})

test('白名单外的文件不发', async () => {
  const { tg, b } = await setup()
  const outside = join(b.root, 'secret.txt')
  writeFileSync(outside, 'secret')
  tg.pushText(OWNER, `fileI !file:${outside}`)
  await until(() => toolResults(b).some(r => r.name === 'reply'), 'tool result')
  expect(tg.calls.filter(c => c.method === 'sendDocument' || c.method === 'sendPhoto').length).toBe(0)
  expect(toolResults(b).find(r => r.name === 'reply')!.text).toContain('不在允许发送的目录里')
})

test('假模型连续返回 500：退避，最多重试 2 次，然后进死信并通知主人，不会无限重放', async () => {
  const { tg, b, gw } = await setup()
  const mid = tg.pushText(FRIEND, 'errJ !err:99:j')
  await until(() => tg.sentTo(OWNER).some(s => s.text?.startsWith('【系统】')), 'owner notice')
  await sleep(800)
  const ps = prompts(b).filter(p => p.sessionId === prompts(b).find(x => x.text.includes('errJ'))!.sessionId)
  expect(ps.length).toBe(3) // 原消息 + 2 次补救提示
  expect(ps[0]!.text).toContain('errJ')
  expect(ps[1]!.text).toContain('上一条消息没能处理完')
  expect(ps.filter(p => p.text.includes('errJ')).length).toBe(1) // 原文只发过一次
  expect(tg.sentTo(FRIEND).length).toBe(0)
  const led = gw.ledger()
  expect(led.inboundByKey(`tg:${FRIEND}:${mid}`)!.state).toBe('dead')
  led.close()
})

test('假模型报 500 一次后恢复：补救提示之后正常回复', async () => {
  const { tg, b } = await setup()
  tg.pushText(OWNER, 'errK !err:1:k')
  await until(() => tg.sentTo(OWNER).some(s => s.text === '接着刚才的说'), 'reply after nudge')
  expect(prompts(b).filter(p => p.text.includes('errK')).length).toBe(1)
})

test('鉴权失败：不重试，直接通知主人', async () => {
  const { tg, b } = await setup()
  tg.pushText(FRIEND, 'authL !errauth')
  await until(() => tg.sentTo(OWNER).some(s => s.text?.includes('密钥')), 'owner notice')
  await sleep(500)
  expect(prompts(b).length).toBe(1)
})

test('假模型不回应：健康检查报"活着但不干活"，超时后取消并补救', async () => {
  const { tg, b, gw } = await setup({ turn_stall_cancel_ms: 1_500, turn_stall_warn_ms: 300 })
  tg.pushText(OWNER, 'hangM !hang:1:m')
  await until(() => prompts(b).some(p => p.text.includes('hangM')), 'prompt')
  await sleep(600)
  const h = await gw.api('/v1/health', { headers: { authorization: `Bearer ${gw.apiToken()}` } })
  expect(h.status).toBe(503)
  const body = await h.json() as { ok: boolean; problems: string[] }
  expect(body.ok).toBe(false)
  expect(body.problems.join('\n')).toContain('没有进展')
  await until(() => tg.sentTo(OWNER).some(s => s.text === '接着刚才的说'), 'reply after cancel + nudge', 10_000)
  const h2 = await gw.api('/v1/health', { headers: { authorization: `Bearer ${gw.apiToken()}` } })
  expect(h2.status).toBe(200)
})

test('假模型连取消都不理：重启 dsh，换新会话补上', async () => {
  const { tg, b } = await setup({ turn_stall_cancel_ms: 800, turn_stall_warn_ms: 300 })
  tg.pushText(OWNER, 'hangN !hangforever:1:n')
  await until(() => tg.sentTo(OWNER).some(s => s.text === '收到：hangN'), 'reply after dsh restart', 15_000)
  const ps = prompts(b).filter(p => p.text.includes('hangN'))
  expect(ps.length).toBe(2)
  expect(ps[0]!.pid).not.toBe(ps[1]!.pid)
  expect(ps[0]!.sessionId).not.toBe(ps[1]!.sessionId)
})
