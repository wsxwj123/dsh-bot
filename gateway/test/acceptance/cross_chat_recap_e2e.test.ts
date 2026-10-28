// cross-chat-recap 端到端：私聊留一句话 → 导演点名群回合 → prompt 带「私聊近况」；
// 下一轮群回合同一批私聊行不再重复。只走对外接口：假 Telegram、/v1/inject、假 ACP 的 prompt 落盘。
import { afterEach, expect, test } from 'bun:test'
import { rmSync } from 'fs'
import { FakeTelegram } from '../fakes/fake-telegram'
import { Gateway, GROUP, makeBot, OWNER, prompts, sleep, until, type BotEnv } from './_acc'

let tg: FakeTelegram | null = null
let b: BotEnv | null = null
let gw: Gateway | null = null

afterEach(async () => {
  await gw?.stop().catch(() => {})
  tg?.stop()
  if (b) { try { rmSync(b.root, { recursive: true, force: true }) } catch {} }
  tg = null; b = null; gw = null
})

const promptTexts = () => prompts(b!).map(p => p.text)

async function directorSay(text: string): Promise<void> {
  const r = await gw!.call('/v1/inject', { chat_id: String(GROUP), text, source: 'director', key: `recap-${crypto.randomUUID()}` })
  expect(r.status).toBe(200)
  expect(r.json).toMatchObject({ ok: true })
}

test('群回合带一次与主人的私聊近况，包含私聊原文与隐私提示；下一轮不再重复', async () => {
  tg = new FakeTelegram({ botId: 900555, username: 'bot_recap' })
  b = makeBot(tg, { name: 'recap', gw: { burst_window_ms: 0 }, access: { groups: { [String(GROUP)]: { allowFrom: [] } } } })
  gw = new Gateway(b)
  await gw.start()

  // 私聊留一句只有私聊里说过的话；等这一轮入账、回复真正送达
  const secret = '暗号是蓝鲸2468'
  tg.pushText(OWNER, secret)
  await until(() => promptTexts().some(t => t.includes(secret)), '私聊回合的 prompt')
  await until(() => tg!.sentTo(OWNER).length > 0, '私聊回复送达')
  await sleep(300) // 等 outbound 的 state/sent_at 落账

  // 导演点名群回合：群消息本身不写 inbound，靠 synthetic+source=director 触发
  const firstAsk = '群里有人问：刚才说的暗号是什么'
  await directorSay(`⟦群聊·导演点到你了⟧ ${firstAsk}`)
  const p1 = await until(
    () => promptTexts().find(t => t.includes(firstAsk) && t.includes('⟦私聊近况')),
    '群回合 prompt 带私聊近况',
  )
  expect(p1).toContain('⟦私聊近况（群里的人看不到这段，别原样念出来')
  expect(p1).toContain(`主人：${secret}`)
  expect(p1).toContain('⟦私聊近况结束⟧')
  // 等这一轮真正结束，避免第二轮和它并进同一批
  await until(() => tg!.sentTo(GROUP).length > 0, '群回复送达')

  const secondAsk = '再问一次：暗号是什么'
  await directorSay(`⟦群聊·导演点到你了⟧ ${secondAsk}`)
  const p2 = await until(
    () => promptTexts().find(t => t.includes(secondAsk) && !t.includes(firstAsk)),
    '第二个群回合 prompt',
  )
  expect(p2).not.toContain('⟦私聊近况')
  expect(p2).not.toContain(secret)
})
