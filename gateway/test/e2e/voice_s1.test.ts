// 语音模型生效为 s1（约定 v1.1 5.14.5 第 6 条）：s1 不认方括号，情绪与语气提示也不带给语音桥（桥会把它们变成方括号前缀）。
import { afterEach, expect, test } from 'bun:test'
import { mkdirSync } from 'fs'
import { FakeTelegram } from '../fakes/fake-telegram'
import { cleanup, Gateway, makeBot, OWNER, until, type BotEnv } from '../harness'

let tg: FakeTelegram | null = null
let b: BotEnv | null = null
let gw: Gateway | null = null
let bridge: ReturnType<typeof Bun.serve> | null = null
let bodies: Record<string, unknown>[] = []

afterEach(async () => {
  await gw?.stop()
  tg?.stop()
  bridge?.stop(true)
  if (b) cleanup(b)
  tg = null; b = null; gw = null; bridge = null; bodies = []
})

async function setup(voiceModel: string) {
  bridge = Bun.serve({ hostname: '127.0.0.1', port: 0, async fetch(req) {
    bodies.push(await req.json() as Record<string, unknown>)
    return new Response(new Uint8Array([79, 103, 103, 83]), { headers: { 'content-type': 'audio/ogg' } })
  } })
  tg = new FakeTelegram()
  b = makeBot(tg, { gw: { burst_window_ms: 0, voice_bridge_url: `http://127.0.0.1:${bridge.port}` }, access: { voiceId: 'fake-voice', voiceModel } })
  mkdirSync(b.acpState, { recursive: true })
  gw = new Gateway(b)
  await gw.start()
  return tg
}

test('s1：发语音不带情绪与语气提示（情绪按 NEUTRAL、语气提示为空）', async () => {
  const tg = await setup('s1')
  tg.pushText(OWNER, '发语音给我 !voice')
  await until(() => bodies.length >= 2, 'two synth calls')
  for (const body of bodies) expect([body.emotion, body.instruct, body.model]).toEqual(['NEUTRAL', '', 's1'])
})

test('别的模型照旧把情绪带给语音桥', async () => {
  const tg = await setup('drama-3')
  tg.pushText(OWNER, '发语音给我 !voice')
  await until(() => bodies.length >= 2, 'two synth calls')
  expect(bodies[0]!.emotion).toBe('HAPPY')
})
