// 刚发现的群（约定 v1.1 5.20）：网关把 bot 在群里的身份变化、没加入的群里被挡下的消息，记进 state/groups-seen.json。
// 白盒：直接构造 GroupsSeen，时钟注入；收消息层的接线另由下面几条用真轮询 + 假 Telegram 验证。
import { afterEach, expect, test } from 'bun:test'
import { existsSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { Ledger } from '../../src/ledger'
import { Logger } from '../../src/log'
import { TelegramApi, type TgChatMemberUpdated, type TgMessage } from '../../src/telegram/api'
import { GroupsSeen, MAX_GROUPS, type SeenGroup } from '../../src/telegram/groups_seen'
import { Poller } from '../../src/telegram/poller'
import { sleep } from '../../src/util'
import { FakeTelegram } from '../fakes/fake-telegram'

const OWNER = '42'
const T0 = Date.parse('2026-10-16T08:00:00Z')
const cleanups: (() => Promise<void> | void)[] = []
afterEach(async () => { for (const f of cleanups.splice(0).reverse()) await f() })

function setup() {
  const dir = mkdtempSync(join(tmpdir(), 'gseen-'))
  cleanups.push(() => rmSync(dir, { recursive: true, force: true }))
  let now = T0
  const path = join(dir, 'state', 'groups-seen.json')
  const seen = new GroupsSeen(path, () => [OWNER], new Logger({}), () => now)
  const read = (): Record<string, SeenGroup> => JSON.parse(readFileSync(path, 'utf8'))
  return { path, seen, read, tick: (ms: number) => { now += ms } }
}

const member = (chatId: number, fromId: string, oldStatus: string, newStatus: string, o: { title?: string; isMember?: boolean; type?: 'group' | 'supergroup' } = {}): TgChatMemberUpdated => ({
  chat: { id: chatId, type: o.type ?? 'supergroup', title: o.title ?? `群${chatId}` },
  from: { id: Number(fromId), is_bot: false, first_name: 'u' },
  date: Math.floor(T0 / 1000),
  old_chat_member: { status: oldStatus, user: { id: 1, is_bot: true } },
  new_chat_member: { status: newStatus, user: { id: 1, is_bot: true }, ...(o.isMember !== undefined ? { is_member: o.isMember } : {}) },
})
const msg = (chatId: number, o: Partial<TgMessage> = {}): TgMessage => ({
  message_id: 1, date: Math.floor(T0 / 1000), chat: { id: chatId, type: 'supergroup', title: `群${chatId}` }, from: { id: 7, is_bot: false, first_name: 'x' }, text: '大家好', ...o,
})

test('被主人拉进群：记群名、类型、身份、是主人拉的、首次与最后看到的时间（UTC，到秒）；文件权限 0600、不记消息内容', () => {
  const s = setup()
  s.seen.onMember(member(-1001, OWNER, 'left', 'member', { title: '家人群' }))
  const g = s.read()['-1001']!
  expect(g).toEqual({ title: '家人群', type: 'supergroup', status: 'member', added_by_owner: true, first_seen: '2026-10-16T08:00:00Z', last_seen: '2026-10-16T08:00:00Z', migrated_from: null })
  expect(statSync(s.path).mode & 0o777).toBe(0o600)
  s.seen.onDropped(msg(-1001, { text: '这句不该被记下' }))
  expect(readFileSync(s.path, 'utf8')).not.toContain('这句不该被记下')
})

test('别人拉的记 false；只从群消息里见过的记 null（不知道是谁拉的）；设成管理员、被移出群照样更新身份', () => {
  const s = setup()
  s.seen.onMember(member(-1002, '99', 'left', 'member'))
  expect(s.read()['-1002']!.added_by_owner).toBe(false)
  s.seen.onDropped(msg(-1003))
  expect(s.read()['-1003']).toMatchObject({ status: 'member', added_by_owner: null })
  s.seen.onMember(member(-1002, OWNER, 'member', 'administrator'))
  expect(s.read()['-1002']).toMatchObject({ status: 'administrator', added_by_owner: false })   // 设管理员的人不算"拉进群的人"
  s.seen.onMember(member(-1002, '99', 'administrator', 'kicked'))
  expect(s.read()['-1002']!.status).toBe('kicked')
  s.seen.onMember(member(-1002, OWNER, 'kicked', 'member'))                                      // 又被主人拉回来
  expect(s.read()['-1002']).toMatchObject({ status: 'member', added_by_owner: true })
})

test('被限制（restricted）照记；restricted 而 is_member 为假记作 left（其实已经不在群里）', () => {
  const s = setup()
  s.seen.onMember(member(-1004, OWNER, 'left', 'restricted', { isMember: true }))
  expect(s.read()['-1004']!.status).toBe('restricted')
  s.seen.onMember(member(-1004, '99', 'restricted', 'restricted', { isMember: false }))
  expect(s.read()['-1004']!.status).toBe('left')
})

test('群升级成超级群：新群号记下 migrated_from，原群号记作 left；私聊里的 my_chat_member 不记', () => {
  const s = setup()
  s.seen.onDropped(msg(-555, { chat: { id: -555, type: 'group', title: '老群' } }))
  s.seen.onDropped(msg(-1009, { migrate_from_chat_id: -555 }))
  const all = s.read()
  expect(all['-1009']!.migrated_from).toBe('-555')
  expect(all['-555']!.status).toBe('left')
  s.seen.onMember({ ...member(42, OWNER, 'member', 'kicked'), chat: { id: 42, type: 'private' } })
  expect(s.read()['42']).toBeUndefined()
})

test('同一个群的"又看到一次"一分钟内最多写一次；身份变化随时写', () => {
  const s = setup()
  s.seen.onDropped(msg(-1005))
  s.tick(30_000)
  s.seen.onDropped(msg(-1005))
  expect(s.read()['-1005']!.last_seen).toBe('2026-10-16T08:00:00Z')
  s.seen.onMember(member(-1005, '99', 'member', 'kicked'))
  expect(s.read()['-1005']).toMatchObject({ status: 'kicked', last_seen: '2026-10-16T08:00:30Z' })
  s.tick(61_000)
  s.seen.onDropped(msg(-1005))
  expect(s.read()['-1005']!.last_seen).toBe('2026-10-16T08:01:31Z')
})

test('最多留 50 个：满了先挤不是主人拉的里面最旧的；全是主人拉的就挤最旧的', () => {
  const s = setup()
  s.seen.onDropped(msg(-2000))                                          // null，最旧
  for (let i = 1; i < MAX_GROUPS; i++) { s.tick(1_000); s.seen.onMember(member(-2000 - i, OWNER, 'left', 'member')) }
  expect(Object.keys(s.read()).length).toBe(MAX_GROUPS)
  s.tick(1_000)
  s.seen.onMember(member(-3000, OWNER, 'left', 'member'))
  const a = s.read()
  expect(Object.keys(a).length).toBe(MAX_GROUPS)
  expect(a['-2000']).toBeUndefined()                                    // 不是主人拉的先走
  s.tick(1_000)
  s.seen.onMember(member(-3001, OWNER, 'left', 'member'))
  const b = s.read()
  expect(b['-2001']).toBeUndefined()                                    // 都是主人拉的：挤 last_seen 最旧的
  expect(b['-3001']).toBeDefined()
})

test('文件坏了不拦收消息：当成空的重新记', () => {
  const s = setup()
  s.seen.onDropped(msg(-1006))
  writeFileSync(s.path, '{坏的')
  s.seen.onMember(member(-1007, OWNER, 'left', 'member'))
  expect(Object.keys(s.read())).toEqual(['-1007'])
})

test('文件里有形状不对的条目：读的时候扔掉，满了也照样记得进新群', () => {
  const s = setup()
  s.seen.onDropped(msg(-1008))
  const good = s.read()['-1008']!
  const all: Record<string, unknown> = { '-1': null, '-2': '坏', '-3': { title: '缺 last_seen' }, 'abc': good }
  for (let i = 0; i < MAX_GROUPS; i++) all[String(-5000 - i)] = good
  writeFileSync(s.path, JSON.stringify(all))
  s.tick(1_000)
  s.seen.onMember(member(-3000, OWNER, 'left', 'member'))
  const a = s.read()
  expect(a['-3000']).toBeDefined()
  expect(['-1', '-2', '-3', 'abc'].filter(k => k in a)).toEqual([])
  expect(Object.keys(a).length).toBe(MAX_GROUPS)
})

// ─── 收消息层的接线：真轮询 + 假 Telegram ───

async function until(f: () => boolean, what: string, ms = 5_000) {
  const end = Date.now() + ms
  while (!f()) { if (Date.now() > end) throw new Error(`等不到：${what}`); await sleep(20) }
}

test('轮询：my_chat_member 推进 offset 并交给 onMyChatMember；没加入的群里的消息被挡下时交给 onGroupDropped，加入了的群不交', async () => {
  const tg = new FakeTelegram()
  const dir = mkdtempSync(join(tmpdir(), 'poller-g-'))
  writeFileSync(join(dir, 'access.json'), JSON.stringify({ allowFrom: [OWNER], groups: { '-1777': { requireMention: true, allowFrom: [] } } }))
  const ledger = new Ledger(':memory:')
  const members: TgChatMemberUpdated[] = []
  const dropped: TgMessage[] = []
  const poller = new Poller(new TelegramApi(tg.token, tg.url, 5_000), ledger, new Logger({}), {
    channelDir: dir, botId: tg.bot.id, pollTimeoutS: 1, onInbound: () => {}, onFatal: () => {},
    onMyChatMember: u => members.push(u), onGroupDropped: m => dropped.push(m),
  })
  poller.start()
  cleanups.push(() => { tg.stop(); rmSync(dir, { recursive: true, force: true }) }, () => poller.stop())
  await until(() => tg.lastAllowedUpdates !== null, '第一次 getUpdates')
  tg.pushMyChatMember({ chatId: -1888, fromId: Number(OWNER), oldStatus: 'left', newStatus: 'member', title: '新群' })
  tg.pushText(7, '没加入的群', { chatId: -1888 })
  tg.pushText(7, '加入了的群', { chatId: -1777 })
  await until(() => ledger.tgOffset() > tg.lastUpdateId, '三条都处理完')
  expect(members.map(m => [m.chat.id, m.new_chat_member.status])).toEqual([[-1888, 'member']])
  expect(dropped.map(m => m.chat.id)).toEqual([-1888])
  expect(existsSync(join(dir, 'groups-seen.json'))).toBe(false)         // 写文件是网关接线的事，轮询本身不写
})
