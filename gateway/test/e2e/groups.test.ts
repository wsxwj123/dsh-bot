// M5：两个 bot（两个网关、两个假 Telegram）在同一个群里。群消息只记进共用的群聊记录，不直接触发回复；
// 导演点名（本机接口 /v1/inject，source=director）后才在群里说话，说的话也记进群聊记录；
// 私聊时带一段"群里近况"，只带一次
import { afterEach, expect, test } from 'bun:test'
import { existsSync, readFileSync } from 'fs'
import { join } from 'path'
import { FakeTelegram } from '../fakes/fake-telegram'
import { cleanup, Gateway, makeBot, OWNER, prompts, until, type BotEnv } from '../harness'

const GROUP = -1001
let tgs: FakeTelegram[] = []
let bots: BotEnv[] = []
let gws: Gateway[] = []

afterEach(async () => {
  for (const g of gws) await g.stop()
  for (const t of tgs) t.stop()
  if (bots[0]) cleanup(bots[0])
  tgs = []; bots = []; gws = []
})

async function setup() {
  const ta = new FakeTelegram({ botId: 900001, username: 'bot_a' })
  const tb = new FakeTelegram({ botId: 900002, username: 'bot_b' })
  const access = { groups: { [String(GROUP)]: { allowFrom: [] } } }
  const a = makeBot(ta, { name: 'a', gw: { burst_window_ms: 0 }, access })
  const b = makeBot(tb, { name: 'b', root: a.root, gw: { burst_window_ms: 0 }, access })
  tgs = [ta, tb]; bots = [a, b]
  gws = [new Gateway(a), new Gateway(b)]
  for (const g of gws) await g.start()
  return { ta, tb, a, b, ga: gws[0]!, gb: gws[1]! }
}

const transcript = (b: BotEnv) => {
  const p = join(b.root, 'groups', `${GROUP}.jsonl`)
  return existsSync(p) ? readFileSync(p, 'utf8').split('\n').filter(Boolean).map(l => JSON.parse(l)) : []
}

/** 群里真人说一句：两个 bot 都收到（消息编号相同） */
let nextMid = 5000
function groupSay(text: string): number {
  const mid = nextMid++
  for (const t of tgs) t.pushText(OWNER, text, { chatId: GROUP, messageId: mid })
  return mid
}

const director = (g: Gateway, text: string) => g.api('/v1/inject', {
  method: 'POST', headers: { authorization: `Bearer ${g.apiToken()}`, 'content-type': 'application/json' },
  body: JSON.stringify({ chat_id: String(GROUP), source: 'director', key: `d-${Date.now()}`, text }),
})

test('群消息只记一次、不直接触发回复；导演点名后才说话，说的话记进群聊记录；私聊带一次群里近况', async () => {
  const { ta, tb, a, b, ga } = await setup()
  groupSay('大家晚上好')
  await until(() => transcript(a).length === 1, 'human line recorded once')
  await Bun.sleep(500)
  expect(transcript(a)).toHaveLength(1) // 两个网关都收到了，只记了一次
  expect(transcript(a)[0]).toMatchObject({ is_bot: false, from_id: String(OWNER), text: '大家晚上好' })
  expect(prompts(a).length + prompts(b).length).toBe(0) // 没有点名就不说话

  // 导演点 A
  expect((await director(ga, '⟦群聊·导演点到你了⟧ 群里最近的对话：大家晚上好 !say:晚上好呀')).status).toBe(200)
  await until(() => ta.sentTo(GROUP).some(s => s.text === '晚上好呀'), 'A replied in the group')
  await until(() => transcript(a).some(l => l.from_username === 'bot_a' && l.text === '晚上好呀'), 'A line recorded')
  expect(tb.sentTo(GROUP)).toHaveLength(0)
  expect(JSON.parse(readFileSync(join(a.botDir, 'state', 'bot.json'), 'utf8'))).toMatchObject({ id: 900001, username: 'bot_a' })

  // 私聊 A：带一次群里近况（真人的话记作"对方"，自己的话记作"你"）；再聊一句不再带
  ta.pushText(OWNER, '在吗')
  await until(() => prompts(a).some(p => p.text.includes('在吗')), 'private prompt')
  const p1 = prompts(a).find(p => p.text.includes('在吗'))!.text
  expect(p1).toContain('⟦群里近况')
  expect(p1).toContain('对方：大家晚上好')
  expect(p1).toContain('你：晚上好呀')
  ta.pushText(OWNER, '还在吗')
  await until(() => prompts(a).some(p => p.text.includes('还在吗')), 'second private prompt')
  expect(prompts(a).find(p => p.text.includes('还在吗'))!.text).not.toContain('⟦群里近况')
  // 私聊"刚聊过"标记（导演据此不点正在私聊的 bot）
  expect(existsSync(join(a.root, 'director', 'last-user', `a-${OWNER}.last-user`))).toBe(true)
  // 群里的话没有进私聊会话：群和私聊是两个不同的会话
  const sessions = new Set(prompts(a).map(p => p.sessionId))
  expect(sessions.size).toBe(2)
})

test('群里没登记的群、或不在群白名单里的人：不记录', async () => {
  const { a } = await setup()
  for (const t of tgs) t.pushText(OWNER, '别的群', { chatId: -2002, messageId: 7000 })
  await Bun.sleep(800)
  expect(existsSync(join(a.root, 'groups', '-2002.jsonl'))).toBe(false)
})
