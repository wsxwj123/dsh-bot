// 断网期间重启（outage-delivery 裁判修订 重要 1）：网关启动时先 getMe 探一次线路，拿到结果再安排补发检查、开始约定巡检。
// 真网关 + 假 Telegram，中间夹验收用的 TCP 故障转发：网关自己启动时那次 getMe 放行，之后的探测起全部掐断（网络层失败）。
import { afterEach, expect, test } from 'bun:test'
import { mkdirSync, readFileSync } from 'fs'
import { join } from 'path'
import { TgFaultProxy } from '../acceptance/tg-fault-proxy'
import { FakeTelegram } from '../fakes/fake-telegram'
import { cleanup, FRIEND, Gateway, makeBot, OWNER, sleep, until, type BotEnv } from '../harness'
import { Ledger } from '../../src/ledger'

let tg: FakeTelegram | null = null
let px: TgFaultProxy | null = null
let b: BotEnv | null = null
let gw: Gateway | null = null

afterEach(async () => {
  await gw?.stop().catch(() => {})
  px?.stop()
  tg?.stop()
  if (b) cleanup(b)
  tg = null; px = null; b = null; gw = null
})

const events = (env: BotEnv, name: string) => {
  let raw = ''
  try { raw = readFileSync(join(env.botDir, 'logs', 'gateway.log'), 'utf8') } catch { return [] }
  return raw.split('\n').filter(Boolean).map(l => { try { return JSON.parse(l) } catch { return {} } }).filter((e: Record<string, unknown>) => e.event === name)
}

/** 启动前在账本里放好：一段上次断网没发出的话（补发检查一跑就会补）+ 一条已经逾期的约定（巡检一跑就会提醒） */
function seed(env: BotEnv): void {
  const dir = join(env.botDir, 'state')
  mkdirSync(dir, { recursive: true })
  const l = new Ledger(join(dir, 'ledger.sqlite'))
  try {
    const chat = String(FRIEND)
    const iid = l.insertInbound({ ukey: `tg:${chat}:1`, chatId: chat, kind: 'user', text: '在吗', ts: Date.now() - 60_000, meta: null }).id
    const seg = l.createSegment(chat, 'tok-startup', false)
    const t = l.startTurn({ chatId: chat, segmentId: seg.id, kind: 'message', inboundIds: [iid], attempt: 0 })
    const out = l.outboundIntent({ okey: `turn:${t.id}:c1:p1`, chatId: chat, turnId: t.id, part: 1, ofParts: 1, kind: 'text', text: '断网时没发出的话', file: null })
    l.outboundResult(out, 'unsent', { error: 'link_down' })
    l.finishTurn(t.id, 'ok')
    l.settleInbound([iid], 'done')
    l.addCommitment({ chatId: String(OWNER), text: '提醒对方喝水', quote: null, whenText: '刚才', dueAt: Date.now() - 60_000, source: 'tool' })
  } finally {
    l.close()
  }
}

test('启动时 getMe 探测以网络层失败结束 → 直接判不通（telegram.link_down、health link=down）；不做补发检查，逾期约定按不通延后、不触发', async () => {
  tg = new FakeTelegram()
  px = new TgFaultProxy(Number(tg.server.port))
  await px.start()
  b = makeBot(tg, { gw: { telegram_api: px.url, outage_resend_delay_ms: 0, link_down_after_ms: 1000 } })
  seed(b)
  // 第 1 次 getMe 是启动时问用户名（放行），第 2 次是 engine 启动的线路探测：掐断，之后所有请求都掐断（线路不通）
  let getMes = 0
  px.rules.push({ match: r => r.tgMethod === 'getMe' && ++getMes === 2, action: 'reset', thenMode: 'reset' })
  gw = new Gateway(b)
  await gw.start()
  const env = b
  await until(() => events(env, 'telegram.link_down').length > 0, '启动探测失败后应记 telegram.link_down', 15_000)
  await until(() => events(env, 'commitment.deferred').some(e => e.reason === 'link_down'), '逾期约定应按不通延后', 15_000)
  // 多等几个巡检周期（commit_poll_ms=200）与补发等待（0）：都不该有动作
  await sleep(1_500)
  expect(events(env, 'commitment.fire')).toEqual([])
  expect(events(env, 'outage.resend')).toEqual([])
  expect(events(env, 'outage.resend_dropped')).toEqual([])
  const r = await gw.api('/v1/health', { headers: { authorization: `Bearer ${gw.apiToken()}` } })
  const body = await r.json() as { telegram?: Record<string, unknown> }
  expect(body.telegram?.link).toBe('down')
  // 假 Telegram 一条都没收到（补发、约定提醒都没发出去）
  expect(tg.sent).toEqual([])
}, 40_000)
