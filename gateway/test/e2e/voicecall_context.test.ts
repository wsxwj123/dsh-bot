// 电话记录（voicecall-dsh 方案 ①.5、①.6、①.8 行为清单）：直接打网关 /v1/inject_context，假 Telegram + 假 ACP。
// 看外部结果：假 Telegram 收到的请求、假模型收到的提示、账本、被晾追问状态文件、导演的私聊标记。
import { afterEach, expect, test } from 'bun:test'
import { Database } from 'bun:sqlite'
import { existsSync, lstatSync, readdirSync, readFileSync } from 'fs'
import { join } from 'path'
import { FakeTelegram } from '../fakes/fake-telegram'
import { cleanup, Gateway, makeBot, OWNER, prompts, readJsonl, sleep, until, type BotEnv } from '../harness'

let tg: FakeTelegram | null = null
let b: BotEnv | null = null
let gw: Gateway | null = null

afterEach(async () => {
  await gw?.stop()
  tg?.stop()
  if (b) cleanup(b)
  tg = null; b = null; gw = null
})

function setup(gwOpts: Record<string, unknown> = {}) {
  tg = new FakeTelegram()
  b = makeBot(tg, { gw: gwOpts })
  gw = new Gateway(b)
  return { tg, b, gw }
}

const CALL = '[voice-recap]（这是刚才哥哥和你打电话说的内容，记住即可，不用回复他）\n哥哥在电话里说了：\n· 明天要去⟦医院⟧复查\n· 想吃糖醋排骨'
const NEEDLE = '想吃糖醋排骨'
const H72 = 72 * 3600_000

async function hold(g: Gateway, body: Record<string, unknown>): Promise<{ status: number; json: any }> {
  const r = await g.api('/v1/inject_context', {
    method: 'POST', headers: { authorization: `Bearer ${g.apiToken()}`, 'content-type': 'application/json' },
    body: JSON.stringify({ chat_id: String(OWNER), source: 'voice_call', ...body }),
  })
  return { status: r.status, json: await r.json() }
}

/** 会让主人看到东西的请求（发消息、"正在输入"、表情回应、编辑） */
const visible = (t: FakeTelegram, since: number) => t.calls.slice(since).filter(c => c.method.startsWith('send') || c.method.startsWith('edit') || c.method === 'setMessageReaction')

function heldRows(g: Gateway) {
  const l = g.ledger()
  try { return l.db.query<{ id: number; state: string; note: string | null; text: string }, []>(`SELECT id, state, note, text FROM inbound WHERE ukey LIKE 'ext:voice_call:%' ORDER BY id`).all() } finally { l.close() }
}

/** 目录下全部文件的内容 + 修改时间（被晾状态、导演私聊标记用） */
function snapshot(dir: string): Record<string, string> {
  const out: Record<string, string> = {}
  if (!existsSync(dir)) return out
  for (const n of readdirSync(dir)) {
    const f = join(dir, n)
    const st = lstatSync(f)
    if (st.isFile()) out[n] = `${st.mtimeMs}|${readFileSync(f, 'utf8')}`
  }
  return out
}

async function ownerSays(t: FakeTelegram, bot: BotEnv, text: string): Promise<string> {
  const k = prompts(bot).length
  t.pushText(OWNER, text)
  return (await until(() => prompts(bot).slice(k).find(p => p.text.includes(text)), `模型收到「${text}」`, 20_000)).text
}

async function warm(t: FakeTelegram, text = '预热') {
  t.pushText(OWNER, text)
  await until(() => t.sentTo(OWNER).some(s => s.text === `收到：${text}`), '预热回复')
}

test('收下后不开轮、不发任何东西；主人下一句时电话块和新消息同一轮交给模型（⟦⟧ 已转义），提示成功后改 done', async () => {
  const { tg, b, gw } = setup()
  await gw.start()
  await warm(tg)
  await sleep(300)
  const calls0 = tg.calls.length
  const prompts0 = prompts(b).length
  const r = await hold(gw, { text: CALL, key: 'voice-call:c1' })
  expect(r.status).toBe(200)
  expect(r.json).toMatchObject({ ok: true, duplicate: false, held: true })
  await sleep(2_000)
  expect(visible(tg, calls0)).toEqual([])
  expect(prompts(b).length).toBe(prompts0)
  expect(heldRows(gw).map(x => x.state)).toEqual(['held'])

  const text = await ownerSays(tg, b, '我到家了')
  expect(text).toContain('⟦系统：下面是你们打电话的记录')
  expect(text).toMatch(/⟦在 \d\d-\d\d 周. \S+ \d\d:\d\d 你们通过电话⟧/)
  expect(text).toContain('明天要去〚医院〛复查')
  expect(text).not.toContain('⟦医院⟧')
  // 电话块在本轮新消息之前
  expect(text.indexOf(NEEDLE)).toBeLessThan(text.indexOf('我到家了'))
  await until(() => tg.sentTo(OWNER).some(s => s.text === '收到：我到家了'), '照常回应新消息')
  await until(() => heldRows(gw).every(x => x.state === 'done'), '交出后改 done')
  expect(heldRows(gw)[0]!.note).toBe('call context shown')
  // 之后的轮不再重复带
  const next = await ownerSays(tg, b, '再说一句')
  expect(next).not.toContain(NEEDLE)
  const log = readJsonl<{ event: string; rows?: number; dropped?: number }>(join(b.botDir, 'logs', 'gateway.log')).filter(e => e.event === 'call.context')
  expect(log.map(e => [e.rows, e.dropped])).toEqual([[1, 0]])
}, 60_000)

test('同 key 投两次：第二次 duplicate，账本一条，模型只看到一遍', async () => {
  const { tg, b, gw } = setup()
  await gw.start()
  const a = await hold(gw, { text: CALL, key: 'voice-call:dup' })
  const c = await hold(gw, { text: CALL, key: 'voice-call:dup' })
  expect(c.json).toEqual({ ok: true, id: a.json.id, duplicate: true, held: true })
  expect(heldRows(gw).length).toBe(1)
  const text = await ownerSays(tg, b, '刚挂电话')
  expect(text.split(NEEDLE).length - 1).toBe(1)
}, 60_000)

test('群号回 400，账本不多', async () => {
  const { gw } = setup()
  await gw.start()
  const r = await hold(gw, { chat_id: '-100777', text: CALL, key: 'g' })
  expect(r).toEqual({ status: 400, json: { error: 'hold is for private chats only' } })
  expect(heldRows(gw)).toEqual([])
}, 30_000)

test('那一轮崩了：电话记录仍是 held，下一次（新会话）再带一次，成功后才 done', async () => {
  const { tg, b, gw } = setup()
  await gw.start()
  await hold(gw, { text: CALL, key: 'voice-call:crash' })
  tg.pushText(OWNER, '崩一下 !crash:1:vc')
  await until(() => prompts(b).filter(p => p.text.includes('崩一下')).length >= 2, '崩溃后重试那条消息', 30_000)
  const ps = prompts(b).filter(p => p.text.includes('崩一下'))
  expect(ps[0]!.text).toContain(NEEDLE)
  expect(ps[1]!.text).toContain(NEEDLE)
  expect(ps[1]!.sessionId).not.toBe(ps[0]!.sessionId)
  await until(() => heldRows(gw).every(x => x.state === 'done'), '重试成功后改 done', 20_000)
}, 60_000)

test('收下后网关重启：重启不开轮，主人再发消息时照样带上', async () => {
  const { tg, b, gw } = setup()
  await gw.start()
  await hold(gw, { text: CALL, key: 'voice-call:restart' })
  await gw.stop()
  const calls0 = tg.calls.length
  const prompts0 = prompts(b).length
  await gw.start()
  await sleep(2_000)
  expect(visible(tg, calls0)).toEqual([])
  expect(prompts(b).length).toBe(prompts0)
  expect(await ownerSays(tg, b, '我回来了')).toContain(NEEDLE)
}, 60_000)

test('超过 72 小时才有下一轮：作废（dropped），那一轮不带；超过 5 条只带最新 5 条', async () => {
  const { tg, b, gw } = setup()
  await gw.start()
  await hold(gw, { text: `${CALL}\n· 过期那通`, key: 'voice-call:old' })
  for (let i = 1; i <= 6; i++) await hold(gw, { text: `第${i}通电话的内容`, key: `voice-call:n${i}` })
  await gw.stop()
  const db = new Database(join(b.botDir, 'state', 'ledger.sqlite'))
  try {
    expect(db.query(`UPDATE inbound SET ts = ts - ?1, received_at = received_at - ?1 WHERE ukey = 'ext:voice_call:voice-call:old'`).run(H72 + 60_000).changes).toBe(1)
  } finally { db.close() }
  await gw.start()
  const text = await ownerSays(tg, b, '好几天没聊了')
  expect(text).not.toContain('过期那通')
  expect(text).not.toContain('第1通电话')
  for (let i = 2; i <= 6; i++) expect(text).toContain(`第${i}通电话的内容`)
  await until(() => heldRows(gw).every(x => x.state !== 'held'), '都结算了')
  const rows = heldRows(gw)
  expect(rows.map(r => [r.state, r.note])).toEqual([
    ['dropped', 'call record expired'],
    ['dropped', 'superseded by newer call records'],
    ...Array.from({ length: 5 }, () => ['done', 'call context shown']),
  ])
}, 60_000)

test('电话记录自己永远不引起一轮；别的事引起的一轮（主动消息）会把它带上', async () => {
  const { tg, b, gw } = setup()
  await gw.start()
  await hold(gw, { text: CALL, key: 'voice-call:si' })
  await sleep(1_500)
  expect(prompts(b).length).toBe(0)
  const r = await gw.api('/v1/inject', {
    method: 'POST', headers: { authorization: `Bearer ${gw.apiToken()}`, 'content-type': 'application/json' },
    body: JSON.stringify({ chat_id: String(OWNER), text: '[self-initiate] 主动找他聊聊', source: 'self_initiate', key: 'si1' }),
  })
  expect(r.status).toBe(200)
  const p = await until(() => prompts(b).find(x => x.text.includes('主动找他聊聊')), '主动消息那一轮')
  expect(p.text).toContain(NEEDLE)
  await until(() => heldRows(gw).every(x => x.state === 'done'), '交出后 done')
  expect(tg.sentTo(OWNER).length).toBeGreaterThan(0)
}, 60_000)

test('不算主人说过话：投 held 前后，被晾追问状态与导演私聊标记都不变，追问计时器照常跑也不动它', async () => {
  // 被晾追问 60 秒才查一次，这里调到 200 毫秒，让投 held 之后真的跑过好几次 tick
  const { tg, b, gw } = setup({ hang_tick_ms: 200 })
  await gw.start()
  // 主人说一句、bot 回了：被晾追问武装（状态写进 state 目录的 .hang-state.json），私聊标记写进导演目录
  await warm(tg, '先聊一句')
  const stateDir = join(b.botDir, 'state')
  const hangFile = join(stateDir, '.hang-state.json')
  await until(() => existsSync(hangFile) && readFileSync(hangFile, 'utf8').includes(String(OWNER)), '被晾追问已武装')
  await sleep(600)
  const markerDir = String(b.gw.last_user_dir)
  const hang0 = snapshot(stateDir)['.hang-state.json']
  const marker0 = snapshot(markerDir)
  expect(Object.keys(marker0).length).toBeGreaterThan(0)
  const hangLog = () => readJsonl<{ event: string }>(join(b.botDir, 'logs', 'gateway.log')).filter(e => e.event === 'hang').length
  const hangLog0 = hangLog()

  expect((await hold(gw, { text: CALL, key: 'voice-call:hang' })).status).toBe(200)
  await sleep(1_500)
  expect(snapshot(stateDir)['.hang-state.json']).toBe(hang0)
  expect(snapshot(markerDir)).toEqual(marker0)
  expect(hangLog()).toBe(hangLog0)
  const l = gw.ledger()
  try { expect(l.db.query(`SELECT COUNT(*) AS n FROM inbound WHERE kind = 'user'`).get()).toEqual({ n: 1 }) } finally { l.close() }
}, 60_000)
