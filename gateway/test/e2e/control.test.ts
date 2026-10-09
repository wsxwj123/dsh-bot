// M1 验收 7、8、10：本机接口的鉴权、/clear 的操作者和目标校验、按 bot 换模型
import { afterAll, beforeAll, expect, test } from 'bun:test'
import { writeFileSync } from 'fs'
import { join } from 'path'
import { FakeTelegram } from '../fakes/fake-telegram'
import { cleanup, configCalls, FRIEND, Gateway, lifecycle, makeBot, OWNER, prompts, readJsonl, sleep, until, writeConfig, type BotEnv } from '../harness'

let tg: FakeTelegram
let b: BotEnv
let gw: Gateway
let b2: BotEnv
let gw2: Gateway
let tg2: FakeTelegram

beforeAll(async () => {
  tg = new FakeTelegram()
  b = makeBot(tg)
  gw = new Gateway(b)
  await gw.start()
})

afterAll(async () => {
  await gw.stop()
  await gw2?.stop()
  tg.stop()
  tg2?.stop()
  cleanup(b)
  if (b2) cleanup(b2)
})

const auth = () => ({ authorization: `Bearer ${gw.apiToken()}` })

test('本机接口：没有口令 401，口令不对 401', async () => {
  expect((await gw.api('/v1/health')).status).toBe(401)
  expect((await gw.api('/v1/health', { headers: { authorization: 'Bearer wrong' } })).status).toBe(401)
  expect((await gw.api('/v1/health', { headers: auth() })).status).toBe(200)
})

test('本机接口：带 Origin 头 403（即使口令正确）', async () => {
  const r = await gw.api('/v1/health', { headers: { ...auth(), origin: 'https://evil.example' } })
  expect(r.status).toBe(403)
})

test('本机接口：不是 JSON 415', async () => {
  const r = await gw.api('/v1/send', { method: 'POST', headers: { ...auth(), 'content-type': 'text/plain' }, body: 'chat_id=1&text=hi' })
  expect(r.status).toBe(415)
  const r2 = await gw.api('/v1/send', { method: 'POST', headers: { ...auth(), 'content-type': 'application/x-www-form-urlencoded' }, body: 'chat_id=1&text=hi' })
  expect(r2.status).toBe(415)
})

test('本机接口：/send 请求白名单外的文件被拒绝，白名单内的能发', async () => {
  const outside = join(b.root, 'outside.txt')
  writeFileSync(outside, 'x')
  const r = await gw.api('/v1/send', { method: 'POST', headers: { ...auth(), 'content-type': 'application/json' }, body: JSON.stringify({ chat_id: String(OWNER), text: 'apiSend', files: [outside] }) })
  expect(r.status).toBe(403)
  expect(tg.calls.some(c => c.method === 'sendDocument')).toBe(false)
  const traversal = join(b.botDir, 'media', '..', '..', '..', 'outside.txt')
  const r2 = await gw.api('/v1/send', { method: 'POST', headers: { ...auth(), 'content-type': 'application/json' }, body: JSON.stringify({ chat_id: String(OWNER), text: 'apiSend', files: [traversal] }) })
  expect(r2.status).toBe(403)
  const inside = join(b.botDir, 'media', 'ok.txt')
  writeFileSync(inside, 'ok')
  const r3 = await gw.api('/v1/send', { method: 'POST', headers: { ...auth(), 'content-type': 'application/json' }, body: JSON.stringify({ chat_id: String(OWNER), text: 'apiSendOk', files: [inside] }) })
  expect(r3.status).toBe(200)
  expect((await r3.json() as any).delivered).toBe(2)
})

test('本机接口：/send 不能发给白名单以外的聊天', async () => {
  const r = await gw.api('/v1/send', { method: 'POST', headers: { ...auth(), 'content-type': 'application/json' }, body: JSON.stringify({ chat_id: '999', text: 'x' }) })
  expect(r.status).toBe(403)
})

test('本机接口：/inject 写一条合成消息，带 key 重复投递只算一次', async () => {
  const body = JSON.stringify({ chat_id: String(OWNER), text: '⟦系统：测试投递 injectZ⟧', key: 'k1', source: 'test' })
  const r1 = await gw.api('/v1/inject', { method: 'POST', headers: { ...auth(), 'content-type': 'application/json' }, body })
  const r2 = await gw.api('/v1/inject', { method: 'POST', headers: { ...auth(), 'content-type': 'application/json' }, body })
  expect((await r1.json() as any).duplicate).toBe(false)
  expect((await r2.json() as any).duplicate).toBe(true)
  await until(() => prompts(b).some(p => p.text.includes('injectZ')), 'model saw injected message')
  await sleep(300)
  expect(prompts(b).filter(p => p.text.includes('injectZ')).length).toBe(1)
})

async function segmentCount(): Promise<number> {
  const led = gw.ledger()
  const n = (led.db.query('SELECT COUNT(*) AS n FROM segments').get() as { n: number }).n
  led.close()
  return n
}

test('/clear：非主人发无效', async () => {
  tg.pushText(OWNER, 'beforeClear')
  await until(() => tg.sentTo(OWNER).some(s => s.text === '收到：beforeClear'), 'reply')
  const segs = await segmentCount()
  tg.pushText(FRIEND, '/clear')
  await sleep(800)
  expect(tg.sentTo(FRIEND).some(s => s.text?.includes('已清空'))).toBe(false)
  expect(await segmentCount()).toBe(segs)
  const led = gw.ledger()
  expect(led.db.query(`SELECT state, note FROM inbound WHERE text = '/clear' AND sender_id = ?`).get(String(FRIEND))).toEqual({ state: 'dropped', note: 'not owner' })
  led.close()
})

test('/clear：@ 的是别的 bot 也无效', async () => {
  tg.pushText(OWNER, '/clear@some_other_bot')
  await sleep(800)
  expect(tg.sentTo(OWNER).some(s => s.text?.includes('已清空'))).toBe(false)
  const led = gw.ledger()
  expect((led.db.query(`SELECT note FROM inbound WHERE text = '/clear@some_other_bot'`).get() as any).note).toBe('command addressed to another bot')
  led.close()
})

test('/clear：主人发、@ 本 bot（或不带 @）有效，下一条消息开新会话、不带清空前的内容', async () => {
  const before = prompts(b).filter(p => p.text.includes('beforeClear'))[0]!.sessionId
  tg.pushText(OWNER, `/clear@${tg.bot.username}`)
  await until(() => tg.sentTo(OWNER).some(s => s.text?.includes('已清空')), 'clear ack')
  tg.pushText(OWNER, 'afterClear')
  await until(() => tg.sentTo(OWNER).some(s => s.text === '收到：afterClear'), 'reply after clear')
  const p = prompts(b).find(x => x.text.includes('afterClear'))!
  expect(p.sessionId).not.toBe(before)
  expect(p.text).not.toContain('beforeClear')
})

test('改了这个 bot 的模型：下一轮生效，不重启 dsh；另一个 bot 不受影响', async () => {
  tg2 = new FakeTelegram()
  b2 = makeBot(tg2, { name: 'otherbot' })
  gw2 = new Gateway(b2)
  await gw2.start()
  tg2.pushText(OWNER, 'warm2')
  await until(() => tg2.sentTo(OWNER).some(s => s.text === '收到：warm2'), 'other bot reply')
  tg.pushText(OWNER, 'beforeModel')
  await until(() => tg.sentTo(OWNER).some(s => s.text === '收到：beforeModel'), 'before model')
  const pidBefore = prompts(b).at(-1)!.pid
  const otherCallsBefore = configCalls(b2).length

  b.brain = { ...b.brain, model: 'deepseek-v4-pro', reasoning_effort: 'high' }
  // 不能固定 sleep：网关按配置轮询间隔读文件，慢机器（Windows CI）上 500 毫秒还没读到，
  // 下一句就会用旧模型。等它真的记下这次改动再发。
  const brainChanges = () => readJsonl<{ event?: string }>(join(b.botDir, 'logs', 'gateway.log')).filter(e => e.event === 'config.brain_changed').length
  const seenChanges = brainChanges()
  writeConfig(b)
  await until(() => brainChanges() > seenChanges, '网关读到新配置')
  tg.pushText(OWNER, 'afterModel')
  await until(() => tg.sentTo(OWNER).some(s => s.text === '收到：afterModel'), 'after model')
  const p = prompts(b).find(x => x.text.includes('afterModel'))!
  expect(p.model).toBe(JSON.stringify(['deepseek-official', 'deepseek-v4-pro']))
  expect(p.pid).toBe(pidBefore) // 同一个 dsh 进程
  expect(configCalls(b).some(c => c.configId === 'reasoning_effort' && c.value === 'high')).toBe(true)
  expect(lifecycle(b).filter(l => l.event === 'start').length).toBe(1)
  // 另一个 bot：没有任何新的配置调用，还在用原来的模型
  tg2.pushText(OWNER, 'other2')
  await until(() => tg2.sentTo(OWNER).some(s => s.text === '收到：other2'), 'other bot reply 2')
  expect(configCalls(b2).length).toBe(otherCallsBefore)
  expect(prompts(b2).find(x => x.text.includes('other2'))!.model).toBe(JSON.stringify(['deepseek-official', 'deepseek-flash']))
})
