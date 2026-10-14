// GET /v1/health 的 telegram.username：本机适配服务从这里读 bot 的 Telegram 用户名（适配约定 v0.7 5.12.1）。
// 真网关 + 假 Telegram：用户名是网关启动时 getMe 问到的那个，不带 @。
import { afterEach, expect, test } from 'bun:test'
import { FakeTelegram } from '../fakes/fake-telegram'
import { cleanup, Gateway, makeBot, type BotEnv } from '../harness'

let tg: FakeTelegram | null = null
let b: BotEnv | null = null
let gw: Gateway | null = null

afterEach(async () => {
  await gw?.stop()
  tg?.stop()
  if (b) cleanup(b)
  tg = null; b = null; gw = null
})

test('health 的 telegram 段带上启动时问到的用户名，不带 @', async () => {
  tg = new FakeTelegram({ username: 'health_probe_bot' })
  b = makeBot(tg)
  gw = new Gateway(b)
  await gw.start()
  const r = await gw.api('/v1/health', { headers: { authorization: `Bearer ${gw.apiToken()}` } })
  const body = await r.json() as { telegram?: Record<string, unknown> }
  expect(body.telegram?.username).toBe('health_probe_bot')
  expect(typeof body.telegram?.conflict).toBe('boolean')
})
