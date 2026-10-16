// 刚发现的群（约定 v1.1 5.20）在真网关上的接线：health 带 getMe 的两项能力；
// 被拉进群、没加入的群里有人说话，网关写进 <bot>/state/groups-seen.json。真网关 + 假 Telegram。
import { afterEach, expect, test } from 'bun:test'
import { existsSync, readFileSync } from 'fs'
import { join } from 'path'
import { sleep } from '../../src/util'
import { FakeTelegram } from '../fakes/fake-telegram'
import { cleanup, Gateway, makeBot, OWNER, type BotEnv } from '../harness'

let tg: FakeTelegram | null = null
let b: BotEnv | null = null
let gw: Gateway | null = null

afterEach(async () => {
  await gw?.stop()
  tg?.stop()
  if (b) cleanup(b)
  tg = null; b = null; gw = null
})

async function health(): Promise<Record<string, unknown>> {
  const r = await gw!.api('/v1/health', { headers: { authorization: `Bearer ${gw!.apiToken()}` } })
  return ((await r.json()) as { telegram: Record<string, unknown> }).telegram
}

test('health 的 telegram 段带 can_read_all、can_join_groups（取自启动时的 getMe）', async () => {
  tg = new FakeTelegram({ canReadAll: false, canJoinGroups: true })
  b = makeBot(tg)
  gw = new Gateway(b)
  await gw.start()
  const t = await health()
  expect(t.can_read_all).toBe(false)
  expect(t.can_join_groups).toBe(true)
})

test('getMe 没给这两项时为 null', async () => {
  tg = new FakeTelegram()
  b = makeBot(tg)
  gw = new Gateway(b)
  await gw.start()
  const t = await health()
  expect(t.can_read_all).toBeNull()
  expect(t.can_join_groups).toBeNull()
})

test('被主人拉进群、没加入的群里有人说话：网关记进 state/groups-seen.json', async () => {
  tg = new FakeTelegram()
  b = makeBot(tg)
  gw = new Gateway(b)
  await gw.start()
  const file = join(b.botDir, 'state', 'groups-seen.json')
  tg.pushMyChatMember({ chatId: -1001, fromId: OWNER, oldStatus: 'left', newStatus: 'member', title: '家人群' })
  tg.pushText(7, '大家好', { chatId: -1002 })
  const end = Date.now() + 10_000
  const both = () => { try { const d = JSON.parse(readFileSync(file, 'utf8')); return d['-1001'] && d['-1002'] } catch { return false } }
  while (!both() && Date.now() < end) await sleep(50)
  const d = JSON.parse(readFileSync(file, 'utf8'))
  expect(d['-1001']).toMatchObject({ title: '家人群', status: 'member', added_by_owner: true })
  expect(d['-1002']).toMatchObject({ status: 'member', added_by_owner: null })
  expect(existsSync(file)).toBe(true)
})
