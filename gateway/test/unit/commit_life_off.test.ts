// 生活作息关着（约定 v1.1 5.19）：约定提醒照常，但不按作息顺延；到点她在睡觉也照样提醒。开着时照旧顺延到起床。
import { afterEach, expect, setSystemTime, test } from 'bun:test'
import { Commitments } from '../../src/commit/service'
import { Ledger } from '../../src/ledger'
import { Logger } from '../../src/log'
import type { SituationBridge } from '../../src/life/situation'

const TZ = 'Asia/Shanghai'
const CHAT = '100'
const MIN = 60_000
const T0 = Date.parse('2026-10-16T23:00:00+08:00')

afterEach(() => { setSystemTime() })

function setup(lifeOn: boolean) {
  const ledger = new Ledger(':memory:')
  const injected: string[] = []
  const svc = new Commitments({
    ledger,
    log: new Logger({ console: false }),
    timeZone: () => TZ,
    situation: { current: () => ({ state: 'sleeping', name: '睡觉' }), nextWake: () => T0 + 8 * 60 * MIN, nextFree: () => null } as unknown as SituationBridge,
    inject: (_c, text) => { injected.push(text); return injected.length },
    isBusy: () => false,
    notifyOwner: () => {},
    retryMs: [MIN, MIN],
    lifeOn: () => lifeOn,
  })
  return { svc, ledger, injected }
}

test('生活作息开着：到点在睡觉，顺延到起床', () => {
  const { svc, ledger, injected } = setup(true)
  setSystemTime(new Date(T0 - 10 * MIN))
  svc.create(CHAT, { content: '提醒对方喝水', when: '今晚十一点' })
  setSystemTime(new Date(T0 + MIN))
  svc.tick(T0 + MIN)
  expect(injected).toEqual([])
  expect(ledger.openCommitments(CHAT)[0]!.note).toBe('deferred_sleep')
})

test('生活作息关着：到点在睡觉也照样提醒', () => {
  const { svc, ledger, injected } = setup(false)
  setSystemTime(new Date(T0 - 10 * MIN))
  svc.create(CHAT, { content: '提醒对方喝水', when: '今晚十一点' })
  setSystemTime(new Date(T0 + MIN))
  svc.tick(T0 + MIN)
  expect(injected.length).toBe(1)
  expect(ledger.openCommitments(CHAT)[0]!.note).not.toBe('deferred_sleep')
})
