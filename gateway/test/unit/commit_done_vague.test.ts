// 承诺的两处改动（用户 2026-10-16 同意）：提前做到的用 commitment_done 标成完成，不再记成取消；
// 时间说得含糊的，能定的按表先给一个缺省时间登记上（太含糊的照旧只提示）。
import { afterEach, expect, setSystemTime, test } from 'bun:test'
import { Commitments, fmtTime } from '../../src/commit/service'
import { defaultWhen, type WhenCtx } from '../../src/commit/when'
import { Ledger } from '../../src/ledger'
import { Logger } from '../../src/log'
import type { SituationBridge } from '../../src/life/situation'

const TZ = 'Asia/Shanghai'
const CHAT = '100'
const MIN = 60_000
/** 2026-10-(15+day) 这一天北京时间 hm 的时刻（10-15 是周四，10-17 周六，10-18 周日） */
const at = (day: number, hm: string) => Date.parse(`2026-10-${15 + day}T${hm}:00+08:00`)
const now = (day: number, hm: string) => setSystemTime(new Date(at(day, hm)))
const ctx = (ms: number): WhenCtx => ({ now: ms, timeZone: TZ, nextWake: () => null, nextFree: () => null })

afterEach(() => { setSystemTime() })

function setup() {
  const ledger = new Ledger(':memory:')
  const svc = new Commitments({
    ledger,
    log: new Logger({ console: false }),
    timeZone: () => TZ,
    situation: { current: () => null, nextWake: () => null, nextFree: () => null } as unknown as SituationBridge,
    inject: () => null,
    isBusy: () => false,
    notifyOwner: () => {},
    retryMs: [MIN, MIN],
  })
  return { svc, ledger }
}

// ─── 含糊时间的缺省 ───

test('等会儿一类 30 分钟后，晚点一类 2 小时后，明天、后天上午 10 点', () => {
  const t = at(1, '12:00')
  expect(defaultWhen('等会儿拍给你', ctx(t))).toEqual({ at: t + 30 * MIN, word: '等会儿' })
  for (const w of ['待会儿', '一会儿', '过会儿', '稍后', '等会', '待会']) expect(defaultWhen(`${w}发给你`, ctx(t))?.at).toBe(t + 30 * MIN)
  expect(defaultWhen('晚点再找你', ctx(t))).toEqual({ at: t + 120 * MIN, word: '晚点' })
  expect(defaultWhen('晚些陪你', ctx(t))?.at).toBe(t + 120 * MIN)
  expect(defaultWhen('明天陪你', ctx(t))).toEqual({ at: at(2, '10:00'), word: '明天' })
  expect(defaultWhen('后天带给你', ctx(t))).toEqual({ at: at(3, '10:00'), word: '后天' })
})

test('周末：最近一个还没过的周六或周日上午 10 点', () => {
  expect(defaultWhen('周末好好陪你', ctx(at(1, '12:00')))).toEqual({ at: at(2, '10:00'), word: '周末' })   // 周五 → 周六
  expect(defaultWhen('这周末陪你', ctx(at(2, '09:00')))?.at).toBe(at(2, '10:00'))                         // 周六早上 → 当天
  expect(defaultWhen('周末陪你', ctx(at(2, '11:00')))?.at).toBe(at(3, '10:00'))                           // 周六上午过了 → 周日
  expect(defaultWhen('周末陪你', ctx(at(3, '11:00')))?.at).toBe(at(9, '10:00'))                           // 周日也过了 → 下周六
})

test('太含糊的不给缺省：回头、改天、有空、下次、以后、这周、下周、过几天、过两天', () => {
  for (const w of ['回头', '改天', '有空', '下次', '以后', '这周', '下周', '过几天', '过两天']) {
    expect(defaultWhen(`${w}找你`, ctx(at(1, '12:00')))).toBeNull()
  }
})

test('兜底识别到能定的含糊许诺：先替她登记，提示里写明按哪个词定的、不对怎么改', () => {
  const { svc, ledger } = setup()
  now(1, '12:00')
  const hints = svc.fromCall(CHAT, ['周末好好陪你一整天'])
  const open = ledger.openCommitments(CHAT)
  expect(open.length).toBe(1)
  expect([open[0]!.state, open[0]!.due_at, open[0]!.text]).toEqual(['pending', at(2, '10:00'), '周末好好陪你一整天'])
  expect(hints).toEqual([`⟦系统·承诺⟧ 电话里你说了「周末好好陪你一整天」，没说几点，程序按「周末」先替你登记成承诺 #${open[0]!.id}（${fmtTime(at(2, '10:00'), TZ, at(1, '12:00'))}）。时间不对就用 commitment_cancel 取消，再用 commitment_create 按对的时间登记；如果这不是承诺，直接取消。`])
})

test('太含糊的照旧：不登记，只提示她登记一个具体时间', () => {
  const { svc, ledger } = setup()
  now(1, '12:00')
  const hints = svc.fromCall(CHAT, ['改天陪你'])
  expect(ledger.openCommitments(CHAT)).toEqual([])
  expect(hints.length).toBe(1)
  expect(hints[0]).toContain('像是答应了对方什么')
})

// ─── commitment_done ───

test('commitment_done：一次性的提前做到了，标成完成、记下怎么做到的，不再提醒', () => {
  const { svc, ledger } = setup()
  now(1, '12:00')
  svc.create(CHAT, { content: '拍给对方看', when: '今晚八点' })
  const id = ledger.openCommitments(CHAT)[0]!.id
  expect(svc.done(CHAT, { id, note: '下午就拍好发过去了' }).isError).toBeFalsy()
  expect([ledger.commitment(id)!.state, ledger.commitment(id)!.note]).toEqual(['done', '下午就拍好发过去了'])
  expect(ledger.openCommitments(CHAT)).toEqual([])
})

test('commitment_done：每天重复的只算今天这一次，排到明天同一时间，照常提醒', () => {
  const { svc, ledger } = setup()
  now(1, '06:00')
  svc.create(CHAT, { content: '说早安发照片', when: '每天早上八点' })
  const id = ledger.openCommitments(CHAT)[0]!.id
  expect(ledger.commitment(id)!.due_at).toBe(at(1, '08:00'))
  now(1, '07:30')
  expect(svc.done(CHAT, { id: `#${id}` }).isError).toBeFalsy()
  expect([ledger.commitment(id)!.state, ledger.commitment(id)!.due_at]).toEqual(['pending', at(2, '08:00')])
})

test('commitment_done：已经结束的、不在这个聊天的、没有的编号，都不改', () => {
  const { svc, ledger } = setup()
  now(1, '12:00')
  svc.create(CHAT, { content: '拍给对方看', when: '今晚八点' })
  const id = ledger.openCommitments(CHAT)[0]!.id
  svc.cancel(CHAT, { id, reason: '对方说不用了' })
  expect(svc.done(CHAT, { id }).text).toContain('已经结束了')
  expect(ledger.commitment(id)!.state).toBe('cancelled')
  expect(svc.done('999', { id }).isError).toBe(true)
  expect(svc.done(CHAT, { id: 12345 }).isError).toBe(true)
})
