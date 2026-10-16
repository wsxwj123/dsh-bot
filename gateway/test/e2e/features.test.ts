// 功能开关（约定 v1.1 5.19）在真网关上的效果：开关写在这只 bot 的 yml 里，网关按它办事；改了开关等空闲重启换新会话。
import { afterEach, expect, test } from 'bun:test'
import { mkdirSync, writeFileSync } from 'fs'
import { join } from 'path'
import { FakeTelegram } from '../fakes/fake-telegram'
import { cleanup, Gateway, makeBot, OWNER, prompts, readJsonl, toolResults, until, writeConfig, type BotEnv } from '../harness'

let tg: FakeTelegram | null = null
let b: BotEnv | null = null
let gw: Gateway | null = null
let bridge: ReturnType<typeof Bun.serve> | null = null
let synth = 0

afterEach(async () => {
  await gw?.stop()
  tg?.stop()
  bridge?.stop(true)
  if (b) cleanup(b)
  tg = null; b = null; gw = null; bridge = null; synth = 0
})

const events = (e: string) => readJsonl<Record<string, any>>(join(b!.botDir, 'logs', 'gateway.log')).filter(r => r.event === e)

async function setup(features: Record<string, boolean>, access: Record<string, unknown> = {}) {
  bridge = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch: () => { synth++; return new Response(new Uint8Array([79, 103, 103, 83]), { headers: { 'content-type': 'audio/ogg' } }) } })
  tg = new FakeTelegram()
  b = makeBot(tg, { gw: { burst_window_ms: 0, voice_bridge_url: `http://127.0.0.1:${bridge.port}` }, access: { voiceId: 'fake-voice', ...access } })
  b.extra = { features }
  writeConfig(b)
  mkdirSync(b.acpState, { recursive: true })
  gw = new Gateway(b)
  await gw.start()
  return { tg, b }
}

test('发语音关着：as_voice 不生效，只发文字、不找语音桥', async () => {
  const { tg } = await setup({ voice: false })
  tg.pushText(OWNER, '发语音给我 !voice')
  await until(() => tg.sent.filter(s => s.method === 'sendMessage').length >= 2, 'two text parts')
  expect(tg.sent.some(s => s.method === 'sendVoice')).toBe(false)
  expect(synth).toBe(0)
})

test('表情包关着：模型调 sticker 也不执行，回"这个功能现在关着。"', async () => {
  const { tg, b } = await setup({ stickers: false })
  tg.pushText(OWNER, '!tool:sticker|{"action":"list"}')
  await until(() => toolResults(b).some(r => r.name === 'sticker'), 'sticker tool result')
  expect(toolResults(b).find(r => r.name === 'sticker')).toMatchObject({ text: '这个功能现在关着。', isError: true })
})

test('关系进展关着：私聊不带关系状态那句', async () => {
  const { tg, b } = await setup({ relationship: false })
  writeFileSync(join(b.channelDir, 'relationship.json'), JSON.stringify({ prompt_snippet: '【当前关系状态】测试好感五十' }))
  tg.pushText(OWNER, '第一句')
  await until(() => prompts(b).some(p => p.text.includes('第一句')), 'first prompt')
  expect(prompts(b).find(p => p.text.includes('第一句'))!.text).not.toContain('⟦关系状态⟧')
})

test('群聊关着：群消息一律不处理（不记、不交给模型），access.json 里的群条目照留', async () => {
  const group = -1004242
  const { tg, b } = await setup({ groups: false }, { groups: { [String(group)]: { requireMention: false, allowFrom: [] } } })
  tg.pushText(OWNER, '群里的话', { chatId: group })
  await until(() => events('inbound.dropped').some(e => e.reason === 'groups feature off'), 'group dropped')
  expect(events('inbound.observed')).toEqual([])
  expect(prompts(b).some(p => p.text.includes('群里的话'))).toBe(false)
})

test('改开关：网关发现 yml 里的 features 变了，记一次需要重启（等空闲换新会话）', async () => {
  const { tg, b } = await setup({})
  tg.pushText(OWNER, '先聊一句')
  await until(() => prompts(b).some(p => p.text.includes('先聊一句')), 'first turn')
  b.extra = { features: { image: false } }
  writeConfig(b)
  await until(() => events('config.restart_needed').length > 0, 'restart needed', 20_000)
})
