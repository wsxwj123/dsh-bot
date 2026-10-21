// 持续中的事（接口：ongoing-event-in-chat）：私聊每轮附带的系统提示里注入 ⟦持续中的事⟧。
// 只走公开入口：临时 SQLite 库 + 环境变量 BOTLIFE_STATE_DB，真网关 + 假 Telegram + 假 dsh，
// 断言假 dsh 落盘的 prompts.jsonl 里模型实际收到的提示文本。
import { afterEach, expect, test } from 'bun:test'
import { Database } from 'bun:sqlite'
import { mkdirSync, mkdtempSync, rmSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { FakeTelegram } from '../fakes/fake-telegram'
import { cleanup, Gateway, makeBot, OWNER, prompts, until, writeConfig, type BotEnv } from '../harness'

const GROUP = -1004242
const MARK = '⟦持续中的事⟧'

let tg: FakeTelegram | null = null
let b: BotEnv | null = null
let gw: Gateway | null = null
const dirs: string[] = []

afterEach(async () => {
  await gw?.stop()
  tg?.stop()
  if (b) cleanup(b)
  for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true })
  tg = null; b = null; gw = null
})

const pad = (n: number) => String(n).padStart(2, '0')
/** SQLite 里的时间统一写成 'YYYY-MM-DD HH:MM:SS'（本地时间，SQLite 的常规写法） */
const fmt = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
const daysAgo = (n: number) => { const d = new Date(); d.setHours(0, 0, 0, 0); d.setDate(d.getDate() - n); return d }
const later = (ms: number) => new Date(Date.now() + ms)

type Ev = { name: string; effect?: string; startedDaysAgo: number; expiresAt?: Date }

/** 建一个临时库；events 为空则只建表 */
function makeDb(events: Ev[] = []): string {
  const dir = mkdtempSync(join(tmpdir(), 'ongoing-'))
  dirs.push(dir)
  const path = join(dir, 'state.db')
  const db = new Database(path, { create: true })
  db.run(`CREATE TABLE ongoing_events (bot_id TEXT, event_name TEXT, effect TEXT, mood_delta REAL, started_at TEXT, expires_at TEXT, last_mentioned_at TEXT)`)
  for (const e of events) {
    db.run(
      `INSERT INTO ongoing_events (bot_id, event_name, effect, mood_delta, started_at, expires_at, last_mentioned_at) VALUES (?, ?, ?, ?, ?, ?, NULL)`,
      ['chenlulu', e.name, e.effect ?? '有点不舒服', 0.5, fmt(daysAgo(e.startedDaysAgo)), fmt(e.expiresAt ?? later(3 * 86_400_000))],
    )
  }
  db.close()
  return path
}

/** 拉起一个私聊可用的 bot；life 默认开着，person 映射到陈露露（chenlulu） */
async function start(o: { dbPath: string; features?: Record<string, boolean>; group?: boolean } = { dbPath: '' }) {
  tg = new FakeTelegram()
  b = makeBot(tg, {
    gw: { burst_window_ms: 0 },
    access: o.group ? { groups: { [String(GROUP)]: { allowFrom: [] } } } : {},
  })
  b.extra = { features: o.features ?? { life: true }, life_id: 'chenlulu' }
  mkdirSync(b.acpState, { recursive: true })
  writeConfig(b)
  gw = new Gateway(b)
  await gw.start({ BOTLIFE_STATE_DB: o.dbPath })
  return { tg, b, gw }
}

async function waitPrompt(needle: string) {
  return until(() => prompts(b!).find(p => p.text.includes(needle)), `prompt with ${needle}`)
}

test('私聊首次：有未过期事件时提示里出现「持续中的事」，含事件名和天数（第 3 天）', async () => {
  const db = makeDb([{ name: '例假来了', startedDaysAgo: 2 }])
  await start({ dbPath: db })
  tg!.pushText(OWNER, '今天有点累')
  const p = await waitPrompt('今天有点累')
  expect(p.text).toContain(MARK)
  expect(p.text).toContain('例假来了')
  expect(p.text).toContain('第 3 天')
})

test('同一事件同一聊天只注入一次：第二轮提示里不再出现该行', async () => {
  const db = makeDb([{ name: '例假来了', startedDaysAgo: 2 }])
  await start({ dbPath: db })
  tg!.pushText(OWNER, '第一轮问问')
  const first = await waitPrompt('第一轮问问')
  expect(first.text).toContain(MARK)
  await until(() => tg!.sentTo(OWNER).some(s => s.text?.startsWith('收到：')), 'first reply')
  tg!.pushText(OWNER, '第二轮问问')
  const second = await waitPrompt('第二轮问问')
  expect(second.text).not.toContain(MARK)
  expect(second.text).not.toContain('例假来了')
})

test('已过期事件（expires_at 早于当前时间）：不出现', async () => {
  const db = makeDb([{ name: '搬家忙完了', startedDaysAgo: 5, expiresAt: later(-86_400_000) }])
  await start({ dbPath: db })
  tg!.pushText(OWNER, '在干嘛呢')
  const p = await waitPrompt('在干嘛呢')
  expect(p.text).toContain('在干嘛呢')
  expect(p.text).not.toContain(MARK)
  expect(p.text).not.toContain('搬家忙完了')
})

test('群聊：不出现「持续中的事」', async () => {
  const db = makeDb([{ name: '例假来了', startedDaysAgo: 2 }])
  await start({ dbPath: db, group: true })
  // 群聊只在导演点名时开一轮；真人直接在群里说话不会触发模型
  const r = await gw!.api('/v1/inject', {
    method: 'POST',
    headers: { authorization: `Bearer ${gw!.apiToken()}`, 'content-type': 'application/json' },
    body: JSON.stringify({ chat_id: String(GROUP), source: 'director', key: `g-${Date.now()}`, text: '群里聊两句' }),
  })
  expect(r.ok).toBe(true)
  const p = await waitPrompt('群里聊两句')
  expect(p.text).toContain('群里聊两句')
  expect(p.text).not.toContain(MARK)
  expect(p.text).not.toContain('例假来了')
  // 群这一轮照常发出回复
  await until(() => tg!.sent.some(s => s.chatId === String(GROUP) && s.text?.startsWith('收到：')), 'reply in group')
})

test('功能开关 life 关闭：不出现「持续中的事」，聊天照常', async () => {
  const db = makeDb([{ name: '例假来了', startedDaysAgo: 2 }])
  await start({ dbPath: db, features: { life: false } })
  tg!.pushText(OWNER, '今天有什么安排')
  const p = await waitPrompt('今天有什么安排')
  expect(p.text).toContain('今天有什么安排')
  expect(p.text).not.toContain(MARK)
  expect(p.text).not.toContain('例假来了')
  await until(() => tg!.sentTo(OWNER).some(s => s.text?.startsWith('收到：')), 'reply')
})

test('库文件不存在：不报错，该轮聊天正常发出', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'ongoing-'))
  dirs.push(dir)
  const missing = join(dir, 'nope', 'state.db')   // 目录都不存在
  await start({ dbPath: missing })
  tg!.pushText(OWNER, '晚上好呀')
  const p = await waitPrompt('晚上好呀')
  expect(p.text).toContain('晚上好呀')
  expect(p.text).not.toContain(MARK)
  const reply = await until(() => tg!.sentTo(OWNER).find(s => s.text?.startsWith('收到：')), 'reply')
  expect(reply.text).toBe('收到：晚上好呀')
  // 网关没被这条坏路径拖死：还能继续聊
  tg!.pushText(OWNER, '再问一句')
  await until(() => prompts(b!).some(p => p.text.includes('再问一句')), 'second prompt')
})

test('同时多条有效事件：按开始时间从新到旧，最多 3 条', async () => {
  // 甲 1 天前（最新）、丙 2 天前、乙 3 天前、丁 4 天前（最旧，应被截掉）
  const db = makeDb([
    { name: '事件丁', startedDaysAgo: 4 },
    { name: '事件乙', startedDaysAgo: 3 },
    { name: '事件甲', startedDaysAgo: 1 },
    { name: '事件丙', startedDaysAgo: 2 },
  ])
  await start({ dbPath: db })
  tg!.pushText(OWNER, '这几天怎么样')
  const p = await waitPrompt('这几天怎么样')
  expect(p.text).toContain(MARK)
  expect(p.text).toContain('事件甲')
  expect(p.text).toContain('事件丙')
  expect(p.text).toContain('事件乙')
  expect(p.text).not.toContain('事件丁')     // 第 4 条不列
  const at = (s: string) => p.text.indexOf(s)
  expect(at('事件甲')).toBeLessThan(at('事件丙'))  // 从新到旧
  expect(at('事件丙')).toBeLessThan(at('事件乙'))
})
