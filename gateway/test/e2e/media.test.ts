// M4：语音进出、图片输入（用假 voice-bridge）
import { afterEach, expect, test } from 'bun:test'
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from 'fs'
import { join } from 'path'
import { FakeTelegram } from '../fakes/fake-telegram'
import { cleanup, Gateway, makeBot, OWNER, prompts, until, type BotEnv } from '../harness'

let tg: FakeTelegram | null = null
let b: BotEnv | null = null
let gw: Gateway | null = null
let bridge: ReturnType<typeof Bun.serve> | null = null
let bridgeCalls: { path: string; body: any; auth: string | null }[] = []

afterEach(async () => {
  await gw?.stop()
  tg?.stop()
  bridge?.stop(true)
  if (b) cleanup(b)
  tg = null; b = null; gw = null; bridge = null; bridgeCalls = []
})

/** 假 voice-bridge：转写固定返回一句话，合成返回几个字节 */
function startBridge(o: { transcript?: string; fail?: boolean } = {}) {
  bridge = Bun.serve({
    hostname: '127.0.0.1', port: 0,
    async fetch(req) {
      const url = new URL(req.url)
      const body = await req.json().catch(() => ({}))
      bridgeCalls.push({ path: url.pathname, body, auth: req.headers.get('authorization') })
      if (o.fail) return new Response('down', { status: 503 })
      if (url.pathname === '/transcribe_file') return Response.json({ text: o.transcript ?? '今天好累啊' })
      if (url.pathname === '/synthesize_voice') return new Response(new Uint8Array([79, 103, 103, 83]), { headers: { 'content-type': 'audio/ogg' } })
      return new Response('nf', { status: 404 })
    },
  })
  return `http://127.0.0.1:${bridge.port}`
}

async function setup(o: { bridge?: string; access?: Record<string, unknown>; before?: (b: BotEnv) => void } = {}) {
  tg = new FakeTelegram()
  b = makeBot(tg, { gw: { burst_window_ms: 0, voice_bridge_url: o.bridge ?? startBridge() }, access: o.access })
  mkdirSync(b.acpState, { recursive: true })
  o.before?.(b)
  gw = new Gateway(b)
  await gw.start()
  return { tg, b, gw }
}

test('语音进：下载后交给 voice-bridge 转写（只给媒体目录里的文件路径，不给令牌），模型看到转写的文字', async () => {
  const { tg, b } = await setup()
  tg.pushVoice(OWNER)
  await until(() => prompts(b).some(p => p.text.includes('[语音] 今天好累啊')), 'transcript reached the model')
  const call = bridgeCalls.find(c => c.path === '/transcribe_file')!
  expect(call.body.path.startsWith(join(b.botDir, 'media'))).toBe(true)
  expect(JSON.stringify(call.body)).not.toContain(tg.token)
  expect(existsSync(call.body.path)).toBe(true)
})

test('语音进：voice-bridge 没起来也不挡聊天，模型看到"没转成文字"', async () => {
  const { tg, b } = await setup({ bridge: startBridge({ fail: true }) })
  tg.pushVoice(OWNER)
  await until(() => prompts(b).some(p => p.text.includes('[语音消息，没转成文字]')), 'placeholder reached the model')
})

test('语音出：as_voice 的每一段合成语音、网关用 sendVoice 发；账本里照样记文字。没配音色就改发文字', async () => {
  const { tg, gw } = await setup({ access: { voiceId: 'test-voice' } })
  tg.pushText(OWNER, '发语音给我 !voice')
  await until(() => tg.sentTo(OWNER).filter(s => s.method === 'sendVoice').length === 2, 'two voice messages')
  const synth = bridgeCalls.filter(c => c.path === '/synthesize_voice')
  expect(synth.map(c => c.body.text)).toEqual(['语音第一段', '语音第二段'])
  expect(synth[0]!.body.voice_id).toBe('test-voice')
  expect(synth[0]!.body.emotion).toBe('HAPPY')
  const l = gw.ledger()
  const out = l.db.query(`SELECT text FROM outbound WHERE state = 'sent' ORDER BY id`).all() as { text: string }[]
  l.close()
  expect(out.map(o => o.text)).toEqual(['语音第一段', '语音第二段'])
})

test('语音出：access.json 没配音色时改发文字，内容不丢', async () => {
  const { tg } = await setup()
  tg.pushText(OWNER, '发语音给我 !voice')
  await until(() => tg.sentTo(OWNER).filter(s => s.text?.startsWith('语音第')).length === 2, 'fallback to text')
  expect(tg.sentTo(OWNER).some(s => s.method === 'sendVoice')).toBe(false)
})

test('图片进：附给模型看；模型看不了图时整条被拒，网关换成一句说明再发，不卡住', async () => {
  const { tg, b } = await setup()
  tg.pushPhoto(OWNER, '看我拍的')
  await until(() => prompts(b).some(p => p.text.includes('看我拍的') && (p as any).images === 1), 'image attached')
  writeFileSync(join(b.acpState, 'no-images'), '1')
  tg.pushPhoto(OWNER, '再看这张')
  await until(() => prompts(b).some(p => p.text.includes('再看这张') && p.text.includes('看不到图片内容')), 'text note instead')
  // 图片存在媒体目录里
  const media = join(b.botDir, 'media')
  const photo = readdirSync(media).find(f => f.startsWith('photo-'))!
  expect(readFileSync(join(media, photo), 'utf8')).toContain('fake file photos/')
})
