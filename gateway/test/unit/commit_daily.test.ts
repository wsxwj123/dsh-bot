// 承诺支持「每天重复」（接口补充 .devflow/INTERFACE-承诺重复.md）：登记、到点续期、重启补发、查看与取消、旧库补列
import { afterEach, expect, setSystemTime, test } from 'bun:test'
import { Database } from 'bun:sqlite'
import { mkdtempSync, rmSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { Commitments } from '../../src/commit/service'
import { Ledger } from '../../src/ledger'
import { Logger } from '../../src/log'
import type { SituationBridge } from '../../src/life/situation'

const TZ = 'Asia/Shanghai'
const CHAT = '100'
const MIN = 60_000
/** 2026-10-(15+day) 这一天北京时间 hm 的时刻（10-15 是周四） */
const at = (day: number, hm: string) => Date.parse(`2026-10-${15 + day}T${hm}:00+08:00`)
const now = (day: number, hm: string) => setSystemTime(new Date(at(day, hm)))

afterEach(() => { setSystemTime() })

function setup(ledger = new Ledger(':memory:')) {
  const injected: { key: string; text: string; id: number }[] = []
  const notified: string[] = []
  const svc = new Commitments({
    ledger,
    log: new Logger({ console: false }),
    timeZone: () => TZ,
    // 没在睡觉、查不到作息
    situation: { current: () => null, nextWake: () => null, nextFree: () => null } as unknown as SituationBridge,
    // 和网关一样：合成消息写进账本，同一个 key 重复投递返回 null
    inject: (chatId, text, key, meta) => {
      const r = ledger.insertInbound({ ukey: `int:${key}`, chatId, kind: 'synthetic', text, ts: Date.now(), meta })
      if (!r.inserted) return null
      injected.push({ key, text, id: r.id })
      return r.id
    },
    isBusy: () => false,
    notifyOwner: (_key, text) => { notified.push(text) },
    retryMs: [MIN, MIN],
  })
  /** 到点提醒的那一轮结束了；delivered：这一轮 bot 真的发出了消息 */
  const endTurn = (inboundId: number, delivered: boolean) => {
    if (delivered) {
      const t = ledger.startTurn({ chatId: CHAT, segmentId: 1, kind: 'message', inboundIds: [inboundId], attempt: 0 })
      ledger.outboundResult(ledger.outboundIntent({ chatId: CHAT, turnId: t.id, part: 0, kind: 'text', text: '起床啦' }), 'sent', { tgMessageId: 1 })
    }
    ledger.settleInbound([inboundId], 'done')
  }
  return { svc, ledger, injected, notified, endTurn }
}

test('when 以「每天」「每日」「天天」开头，没传 repeat 也按每天登记：今天这个时间还没过就是今天，过了就是明天', () => {
  const { svc, ledger } = setup()
  now(0, '08:00')
  expect(svc.create(CHAT, { content: '叫对方起床', when: '每天早上七点' }).isError).toBeFalsy()
  expect(svc.create(CHAT, { content: '提醒对方吃午饭', when: '每日中午十二点' }).isError).toBeFalsy()
  expect(svc.create(CHAT, { content: '提醒对方早点睡', when: '天天晚上十一点半' }).isError).toBeFalsy()
  const due = Object.fromEntries(ledger.openCommitments(CHAT).map(c => [c.text, c.due_at]))
  expect(due).toEqual({ 叫对方起床: at(1, '07:00'), 提醒对方吃午饭: at(0, '12:00'), 提醒对方早点睡: at(0, '23:30') })
  const list = svc.list(CHAT).text
  for (const hm of ['07:00', '12:00', '23:30']) expect(list).toContain(`每天 ${hm}`)
})

test('repeat: "daily" 登记：when 写一天里的时间点（07:30、凌晨五点半）', () => {
  const { svc, ledger } = setup()
  now(0, '06:00')
  expect(svc.create(CHAT, { content: '发早安照片', when: '07:30', repeat: 'daily' }).isError).toBeFalsy()
  expect(svc.create(CHAT, { content: '提醒对方吃药', when: '凌晨五点半', repeat: 'daily' }).isError).toBeFalsy()
  const due = Object.fromEntries(ledger.openCommitments(CHAT).map(c => [c.text, c.due_at]))
  expect(due).toEqual({ 发早安照片: at(0, '07:30'), 提醒对方吃药: at(1, '05:30') })
  const list = svc.list(CHAT).text
  expect(list).toContain('每天 07:30')
  expect(list).toContain('每天 05:30')
})

test('到点做完后不关闭：排到下一天同一时间、回到待触发、尝试次数清零、记下这一次的结果；第二天照样提醒', () => {
  const { svc, ledger, injected, endTurn } = setup()
  now(0, '06:00')
  svc.create(CHAT, { content: '发照片叫对方起床', when: '每天早上七点' })
  const id = ledger.openCommitments(CHAT)[0]!.id
  now(0, '07:00'); svc.tick()
  expect(injected.length).toBe(1)
  expect(ledger.commitment(id)!.state).toBe('firing')
  endTurn(injected[0]!.id, true)
  now(0, '07:01'); svc.tick()
  const c = ledger.commitment(id)!
  expect(c).toMatchObject({ state: 'pending', due_at: at(1, '07:00'), next_at: at(1, '07:00'), attempts: 0 })
  expect(c.note).toContain('done')
  expect(c.note).toContain('07:00')
  now(1, '07:00'); svc.tick()
  expect(injected.length).toBe(2)
  expect(injected[1]!.text).toContain('⟦系统·承诺到期⟧')
  expect(ledger.commitment(id)!.state).toBe('firing')
})

test('提醒 3 次都没开口（彻底失败）也排到下一天：回到待触发、尝试次数清零，照常通知主人', () => {
  const { svc, ledger, injected, notified, endTurn } = setup()
  now(0, '06:00')
  svc.create(CHAT, { content: '叫对方起床', when: '07:00', repeat: 'daily' })
  const id = ledger.openCommitments(CHAT)[0]!.id
  let t = at(0, '07:00')
  for (let i = 0; i < 3; i++) {
    setSystemTime(new Date(t)); svc.tick()
    expect(injected.length).toBe(i + 1)
    endTurn(injected[i]!.id, false)
    t += MIN; setSystemTime(new Date(t)); svc.tick()
    t += MIN
  }
  const c = ledger.commitment(id)!
  expect(c).toMatchObject({ state: 'pending', due_at: at(1, '07:00'), next_at: at(1, '07:00'), attempts: 0 })
  expect(c.note).toContain('failed')
  expect(notified.length).toBe(1)
})

test('网关停了好几天：重启时只补最近这一次，不连发多天，然后照常排到下一天', () => {
  const { svc, ledger, injected, endTurn } = setup()
  now(0, '06:00')
  svc.create(CHAT, { content: '叫对方起床', when: '每天早上七点' })
  const id = ledger.openCommitments(CHAT)[0]!.id
  now(4, '09:00'); svc.tick() // 第 4 天上午九点才重启
  expect(injected.length).toBe(1)
  expect(ledger.commitment(id)!.due_at).toBe(at(4, '07:00'))
  endTurn(injected[0]!.id, true)
  now(4, '09:01'); svc.tick()
  now(4, '09:02'); svc.tick()
  expect(injected.length).toBe(1)
  expect(ledger.commitment(id)!).toMatchObject({ state: 'pending', due_at: at(5, '07:00'), attempts: 0 })
})

test('commitment_list：每天重复的写成「每天 HH:MM」并给出下一次的时间；一次性的照旧', () => {
  const { svc } = setup()
  now(0, '08:00')
  svc.create(CHAT, { content: '叫对方起床', when: '每天早上七点' })
  svc.create(CHAT, { content: '提醒对方喝水', when: '今晚八点' })
  const lines = svc.list(CHAT).text.split('\n')
  const daily = lines.find(l => l.includes('叫对方起床'))!
  expect(daily).toContain('每天 07:00')
  expect(daily).toContain('10月16日 07:00')
  const once = lines.find(l => l.includes('提醒对方喝水'))!
  expect(once).toContain('今天 20:00')
  expect(once).not.toContain('每天')
})

test('commitment_cancel 取消整个每天重复的承诺：以后都不再提醒', () => {
  const { svc, ledger, injected, endTurn } = setup()
  now(0, '06:00')
  svc.create(CHAT, { content: '叫对方起床', when: '每天早上七点' })
  const id = ledger.openCommitments(CHAT)[0]!.id
  now(0, '07:00'); svc.tick()
  endTurn(injected[0]!.id, true)
  now(0, '07:01'); svc.tick() // 第一天做完，排到了第二天
  expect(svc.cancel(CHAT, { id, reason: '对方说以后不用叫了' }).isError).toBeFalsy()
  expect(ledger.commitment(id)!.state).toBe('cancelled')
  for (const d of [1, 2, 3]) { now(d, '07:00'); svc.tick(); now(d, '07:05'); svc.tick() }
  expect(injected.length).toBe(1)
  expect(ledger.openCommitments(CHAT)).toEqual([])
})

test('只说「每天早上」没说几点：按含糊处理不登记；兜底识别到这种许诺，下一轮提醒她问清时间', () => {
  const { svc, ledger } = setup()
  now(0, '21:00')
  svc.create(CHAT, { content: '发照片叫对方起床', when: '每天早上' })
  svc.create(CHAT, { content: '发照片叫对方起床', when: '早上', repeat: 'daily' })
  expect(ledger.openCommitments(CHAT)).toEqual([])
  const hints = svc.fromCall(CHAT, ['好呀，以后每天早上给你发照片叫你起床'])
  expect(hints.length).toBe(1)
  expect(hints[0]).toMatch(/几点|时间/)
  expect(ledger.openCommitments(CHAT)).toEqual([])
})

test('旧库（承诺表没有新列）启动时自动加一列：已有的列和数据不变，新列为空，旧承诺照旧是一次性的', () => {
  const dir = mkdtempSync(join(tmpdir(), 'commit-daily-'))
  try {
    const path = join(dir, 'ledger.db')
    const old = new Database(path, { create: true })
    old.exec(`CREATE TABLE commitments (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL, text TEXT NOT NULL, quote TEXT, when_text TEXT,
      due_at INTEGER NOT NULL, next_at INTEGER NOT NULL, state TEXT NOT NULL, source TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
      inbound_id INTEGER, created_at INTEGER NOT NULL, closed_at INTEGER, note TEXT)`)
    old.query(`INSERT INTO commitments (chat_id, text, when_text, due_at, next_at, state, source, created_at) VALUES (?, '提醒对方喝水', '明天下午三点', ?, ?, 'pending', 'tool', ?)`)
      .run(CHAT, at(1, '15:00'), at(1, '15:00'), at(0, '08:00'))
    const before = old.query('SELECT * FROM commitments').all() as Record<string, unknown>[]
    old.close()

    now(0, '09:00')
    const ledger = new Ledger(path)
    const after = ledger.db.query('SELECT * FROM commitments').all() as Record<string, unknown>[]
    expect(after.length).toBe(1)
    const added = Object.keys(after[0]!).filter(k => !(k in before[0]!))
    expect(added.length).toBe(1)
    expect(after[0]![added[0]!]).toBeNull()
    for (const [k, v] of Object.entries(before[0]!)) expect(after[0]![k]).toEqual(v)
    const { svc } = setup(ledger)
    const list = svc.list(CHAT).text
    expect(list).toContain('10月16日 15:00')
    expect(list).not.toContain('每天')
    ledger.close()
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})
