// 约定遇上 Telegram 断线（outage-delivery 方案 3 commit/service.ts）：到点不触发只记一次、结算三种结局、
// commitment_done 拒绝、重提醒文案、累计 3 次终态（#22）、每日约定跨天只补最近一次（#23）、去重键不撞
import { afterEach, expect, setSystemTime, test } from 'bun:test'
import { Commitments } from '../../src/commit/service'
import { Ledger } from '../../src/ledger'
import { Logger } from '../../src/log'
import type { SituationBridge } from '../../src/life/situation'

const TZ = 'Asia/Shanghai'
const CHAT = '100'
const MIN = 60_000
/** 2026-10-(15+day) 北京时间 hm 的时刻 */
const at = (day: number, hm: string) => Date.parse(`2026-10-${15 + day}T${hm}:00+08:00`)
const now = (day: number, hm: string) => setSystemTime(new Date(at(day, hm)))

afterEach(() => { setSystemTime() })

class CapLog extends Logger {
  events: Record<string, unknown>[] = []
  override log(_level: string, event: string, fields: Record<string, unknown> = {}): void { this.events.push({ event, ...fields }) }
  of(event: string, where: Record<string, unknown> = {}) {
    return this.events.filter(e => e.event === event && Object.entries(where).every(([k, v]) => e[k] === v))
  }
}

type Part = 'sent' | 'ambiguous' | 'unsent'

function setup() {
  const ledger = new Ledger(':memory:')
  const log = new CapLog({ console: false })
  const link = { down: false }
  const injected: { key: string; text: string; id: number }[] = []
  const notified: string[] = []
  const svc = new Commitments({
    ledger, log,
    timeZone: () => TZ,
    situation: { current: () => null, nextWake: () => null, nextFree: () => null } as unknown as SituationBridge,
    inject: (chatId, text, key, meta) => {
      const r = ledger.insertInbound({ ukey: `int:${key}`, chatId, kind: 'synthetic', text, ts: Date.now(), meta })
      if (!r.inserted) return null
      injected.push({ key, text, id: r.id })
      return r.id
    },
    isBusy: () => false,
    notifyOwner: (_key, text) => { notified.push(text) },
    retryMs: [MIN, MIN],
    linkDown: () => link.down,
  })
  /** 提醒那一轮开了头、按 parts 逐段记账（还没结束） */
  const startTurn = (inboundId: number, parts: Part[]) => {
    const t = ledger.startTurn({ chatId: CHAT, segmentId: 1, kind: 'message', inboundIds: [inboundId], attempt: 0 })
    parts.forEach((state, i) => {
      const id = ledger.outboundIntent({ chatId: CHAT, turnId: t.id, part: i, kind: 'text', text: `第${i}段` })
      ledger.outboundResult(id, state, state === 'unsent' ? { error: 'link_down' } : { tgMessageId: i + 1 })
    })
  }
  /** 提醒那一轮跑完了 */
  const endTurn = (inboundId: number, parts: Part[]) => { startTurn(inboundId, parts); ledger.settleInbound([inboundId], 'done') }
  const oneTime = (dueAt: number) => ledger.addCommitment({ chatId: CHAT, text: '提醒对方喝水', quote: null, whenText: '九点', dueAt, source: 'tool' }).id
  const daily = (dueAt: number) => ledger.addCommitment({ chatId: CHAT, text: '叫对方起床', quote: null, whenText: '每天七点', dueAt, source: 'tool', repeatRule: 'daily 07:00' }).id
  return { svc, ledger, log, link, injected, notified, startTurn, endTurn, oneTime, daily }
}

test('到点时线路不通：不触发、不改库，同一次断线每条约定只记一次 deferred；恢复后按实际迟到触发；再断一次再记一次', () => {
  const { svc, ledger, log, link, injected, endTurn, oneTime } = setup()
  now(0, '08:00')
  const id = oneTime(at(0, '09:00'))
  const before = ledger.commitment(id)
  link.down = true
  for (const hm of ['09:00', '09:01', '09:02', '09:30']) { now(0, hm); svc.tick() }
  expect(injected).toEqual([])
  expect(ledger.commitment(id)).toEqual(before)
  expect(log.of('commitment.deferred', { reason: 'link_down', id })).toHaveLength(1)
  expect(log.of('commitment.deferred', { reason: 'sleeping' })).toEqual([])

  link.down = false
  now(0, '09:40'); svc.tick()
  expect(injected).toHaveLength(1)
  expect(log.of('commitment.fire', { id })[0]).toMatchObject({ late_min: 40, attempt: 1 })
  expect(injected[0]!.text).toContain('现在已经晚了 40 分钟')

  // 提醒那一轮又断了：结算回 pending，第二次断线重新记一条 deferred
  link.down = true
  endTurn(injected[0]!.id, ['unsent', 'unsent'])
  now(0, '09:41'); svc.tick(); now(0, '09:42'); svc.tick()
  expect(log.of('commitment.deferred', { reason: 'link_down', id })).toHaveLength(2)
  expect(injected).toHaveLength(1)
})

test('settle：0 送达且有段没发出 → 次数 +1、回 pending、next_at=现在、retry reason=link_down；恢复后用新去重键再提醒，文字说线路断了', () => {
  const { svc, ledger, log, link, injected, endTurn, oneTime } = setup()
  now(0, '08:00')
  const id = oneTime(at(0, '09:00'))
  now(0, '09:00'); svc.tick()
  link.down = true
  endTurn(injected[0]!.id, ['unsent', 'unsent', 'unsent'])
  now(0, '09:01'); svc.tick()
  expect(ledger.commitment(id)).toMatchObject({ state: 'pending', attempts: 1, next_at: at(0, '09:01') })
  expect(log.of('commitment.retry', { id })).toEqual([expect.objectContaining({ reason: 'link_down', attempts: 1 })])
  expect(log.of('commitment.done')).toEqual([])

  link.down = false
  now(0, '09:03'); svc.tick()
  expect(injected.map(i => i.key)).toEqual([`commit:${id}:0`, `commit:${id}:1`])
  expect(injected[1]!.text).toContain('上次提醒时线路断了，对方没收到')
  expect(injected[1]!.text).not.toContain('上次没有发出消息')
  expect(ledger.commitment(id)!.state).toBe('firing')
})

test('settle：有一段可能已送达、其余没发出 → 照常完成，不 retry', () => {
  const { svc, ledger, log, injected, endTurn, oneTime } = setup()
  now(0, '08:00')
  const id = oneTime(at(0, '09:00'))
  now(0, '09:00'); svc.tick()
  endTurn(injected[0]!.id, ['ambiguous', 'unsent'])
  now(0, '09:01'); svc.tick()
  expect(ledger.commitment(id)!.state).toBe('done')
  expect(log.of('commitment.retry')).toEqual([])
  expect(log.of('commitment.done', { id })).toHaveLength(1)
})

test('settle：没开口（0 送达、0 没发出）→ 照旧按退避重试，不带 link_down；再提醒的文字仍是「上次没有发出消息」', () => {
  const { svc, ledger, log, injected, endTurn, oneTime } = setup()
  now(0, '08:00')
  const id = oneTime(at(0, '09:00'))
  now(0, '09:00'); svc.tick()
  endTurn(injected[0]!.id, [])
  now(0, '09:01'); svc.tick()
  expect(ledger.commitment(id)).toMatchObject({ state: 'pending', attempts: 1, next_at: at(0, '09:02') })
  const retry = log.of('commitment.retry', { id })
  expect(retry).toHaveLength(1)
  expect(retry[0]!.reason).toBeUndefined()
  now(0, '09:02'); svc.tick()
  expect(injected[1]!.text).toContain('上次没有发出消息')
  expect(injected[1]!.text).not.toContain('线路断了')
})

test('commitment_done：提醒那一轮 0 送达且有段没发出 → 先不标完成、记 done_refused，约定仍在列表里', () => {
  const { svc, ledger, log, link, injected, startTurn, oneTime } = setup()
  now(0, '08:00')
  const id = oneTime(at(0, '09:00'))
  now(0, '09:00'); svc.tick()
  link.down = true
  startTurn(injected[0]!.id, ['unsent']) // 同一轮里先 reply（没发出），再 commitment_done
  const r = svc.done(CHAT, { id })
  expect(r.text).toContain('先不标完成')
  expect(r.isError).toBeFalsy()
  expect(ledger.commitment(id)!.state).toBe('firing')
  expect(log.of('commitment.done_refused', { id, reason: 'link_down' })).toHaveLength(1)
  expect(log.of('commitment.done')).toEqual([])
  expect(svc.list(CHAT).text).toContain('提醒对方喝水')
})

test('commitment_done：一段都还没发、但此刻线路不通 → 也拒绝；线路通 → 照旧标完成', () => {
  const { svc, ledger, log, link, injected, startTurn, oneTime } = setup()
  now(0, '08:00')
  const a = oneTime(at(0, '09:00'))
  now(0, '09:00'); svc.tick()
  startTurn(injected[0]!.id, [])
  link.down = true
  expect(svc.done(CHAT, { id: a }).text).toContain('先不标完成')
  link.down = false
  expect(svc.done(CHAT, { id: a }).text).toContain('已标成完成')
  expect(ledger.commitment(a)!.state).toBe('done')
  expect(log.of('commitment.done_refused')).toHaveLength(1)
})

test('commitment_done：那一轮已有段送达（含可能已送达），线路不通也照常标完成；没在提醒中的约定不受影响', () => {
  const { svc, ledger, link, injected, startTurn, oneTime } = setup()
  now(0, '08:00')
  const a = oneTime(at(0, '09:00'))
  const b = oneTime(at(0, '12:00'))
  now(0, '09:00'); svc.tick()
  startTurn(injected[0]!.id, ['ambiguous', 'unsent'])
  link.down = true
  expect(svc.done(CHAT, { id: a }).text).toContain('已标成完成')
  expect(svc.done(CHAT, { id: b }).text).toContain('已标成完成') // pending、没在提醒：提前做到了照旧能标
  expect(ledger.commitment(b)!.state).toBe('done')
})

test('#22 一次性约定：没开口与断线共用 3 次机会，第 3 次 → commitment.failed 并通知主人（说明线路原因）；三次去重键各不相同', () => {
  const { svc, ledger, log, link, injected, notified, endTurn, oneTime } = setup()
  now(0, '08:00')
  const id = oneTime(at(0, '09:00'))
  let t = at(0, '09:00')
  const rounds: Part[][] = [[], ['unsent'], ['unsent', 'unsent']] // 第 1 次没开口，后两次断线
  for (const parts of rounds) {
    link.down = false
    setSystemTime(new Date(t)); svc.tick()
    link.down = parts.length > 0
    endTurn(injected.at(-1)!.id, parts)
    t += MIN; setSystemTime(new Date(t)); svc.tick()
    t += 2 * MIN
  }
  expect(injected.map(i => i.key)).toEqual([`commit:${id}:0`, `commit:${id}:1`, `commit:${id}:2`])
  expect(ledger.commitment(id)).toMatchObject({ state: 'failed', attempts: 3 })
  expect(log.of('commitment.failed', { id })).toHaveLength(1)
  expect(notified).toHaveLength(1)
  expect(notified[0]).toContain('线路')
  // 终态之后线路恢复也不再提醒
  link.down = false
  setSystemTime(new Date(t + 10 * MIN)); svc.tick()
  expect(injected).toHaveLength(3)
})

test('#22 每天的约定：断线 3 次 → commitment.next_day result=failed，排到明天、次数清零，主人收到线路原因的通知', () => {
  const { svc, ledger, log, link, injected, notified, endTurn, daily } = setup()
  now(0, '06:00')
  const id = daily(at(0, '07:00'))
  let t = at(0, '07:00')
  for (let i = 0; i < 3; i++) {
    link.down = false
    setSystemTime(new Date(t)); svc.tick()
    link.down = true
    endTurn(injected.at(-1)!.id, ['unsent'])
    t += MIN; setSystemTime(new Date(t)); svc.tick()
    t += MIN
  }
  expect(new Set(injected.map(i => i.key)).size).toBe(3)
  expect(log.of('commitment.next_day', { id, result: 'failed' })).toHaveLength(1)
  expect(ledger.commitment(id)).toMatchObject({ state: 'pending', due_at: at(1, '07:00'), attempts: 0, inbound_id: null })
  expect(notified).toHaveLength(1)
  expect(notified[0]).toContain('线路')
})

test('#23 每天的约定：断线跨过了第二天的时间点 → 恢复后只补最近这一次（caught_up），不会两天各提醒一次', () => {
  const { svc, ledger, log, link, injected, daily } = setup()
  now(0, '06:00')
  const id = daily(at(0, '07:00'))
  link.down = true
  for (const [d, hm] of [[0, '07:00'], [0, '20:00'], [1, '07:00'], [1, '07:30']] as const) { now(d, hm); svc.tick() }
  expect(injected).toEqual([])
  expect(log.of('commitment.deferred', { id, reason: 'link_down' })).toHaveLength(1)
  link.down = false
  now(1, '08:00'); svc.tick()
  now(1, '08:01'); svc.tick()
  expect(log.of('commitment.caught_up', { id })).toHaveLength(1)
  expect(injected.map(i => i.key)).toEqual([`commit:${id}@${at(1, '07:00')}:0`])
  expect(log.of('commitment.fire', { id })[0]).toMatchObject({ late_min: 60, attempt: 1 })
  expect(ledger.commitment(id)!.state).toBe('firing')
})

test('#23 每天的约定：第一天提醒那轮断了、断线一直到第二天之后 → 只补第二天这一次，次数清零，不再提「上次线路断了」', () => {
  const { svc, ledger, log, link, injected, endTurn, daily } = setup()
  now(0, '06:00')
  const id = daily(at(0, '07:00'))
  now(0, '07:00'); svc.tick()
  link.down = true
  endTurn(injected[0]!.id, ['unsent'])
  now(0, '07:01'); svc.tick()
  expect(ledger.commitment(id)).toMatchObject({ state: 'pending', attempts: 1 })
  now(1, '07:10'); svc.tick()
  link.down = false
  now(1, '07:20'); svc.tick()
  expect(log.of('commitment.caught_up', { id })).toHaveLength(1)
  expect(injected.map(i => i.key)).toEqual([`commit:${id}@${at(0, '07:00')}:0`, `commit:${id}@${at(1, '07:00')}:0`])
  expect(injected[1]!.text).not.toContain('次提醒')
  expect(ledger.commitment(id)).toMatchObject({ state: 'firing', due_at: at(1, '07:00'), attempts: 0 })
})
