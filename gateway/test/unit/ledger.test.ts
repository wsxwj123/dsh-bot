import { expect, test } from 'bun:test'
import { Ledger } from '../../src/ledger'

function msg(l: Ledger, chat: string, text: string, n: number) {
  return l.insertInbound({ ukey: `tg:${chat}:${n}`, chatId: chat, kind: 'user', tgMessageId: n, text, ts: Date.now() }).id
}

test('唯一键去重；收到消息和推进 offset 在同一个事务里', () => {
  const l = new Ledger(':memory:')
  const a = l.recordUpdate(10, { ukey: 'tg:1:5', chatId: '1', kind: 'user', text: 'hi', ts: 1 })
  const b = l.recordUpdate(11, { ukey: 'tg:1:5', chatId: '1', kind: 'user', text: 'hi', ts: 1 })
  expect(a.inserted).toBe(true)
  expect(b.inserted).toBe(false)
  expect(l.tgOffset()).toBe(12)
  expect(l.pendingFor('1').length).toBe(1)
})

test('恢复：preparing 的轮放回队列，段照常用', () => {
  const l = new Ledger(':memory:')
  const id = msg(l, '1', 'a', 1)
  const seg = l.createSegment('1', 'tok', false)
  l.setSegmentSession(seg.id, 's1', 'm')
  l.startTurn({ chatId: '1', segmentId: seg.id, kind: 'message', inboundIds: [id], attempt: 0 })
  const r = l.recoverOnStartup(3)
  expect(r.abortedTurns.length).toBe(1)
  expect(l.inbound(id)!.state).toBe('pending')
  expect(l.activeSegment('1')!.id).toBe(seg.id)
})

test('恢复：已经发给 dsh 的轮，段作废；没有回复就放回队列', () => {
  const l = new Ledger(':memory:')
  const id = msg(l, '1', 'a', 1)
  const seg = l.createSegment('1', 'tok', false)
  const t = l.startTurn({ chatId: '1', segmentId: seg.id, kind: 'message', inboundIds: [id], attempt: 0 })
  l.markTurnSent(t.id)
  const r = l.recoverOnStartup(3)
  expect(r.crashedTurns).toEqual([t.id])
  expect(r.abandonedSegments).toEqual([seg.id])
  expect(l.inbound(id)!.state).toBe('pending')
  expect(l.inbound(id)!.attempts).toBe(1)
  expect(l.activeSegment('1')).toBeNull()
})

test('恢复：已经发出过一段（或者发了没记账）就不再重试', () => {
  const l = new Ledger(':memory:')
  const id = msg(l, '1', 'a', 1)
  const seg = l.createSegment('1', 'tok', false)
  const t = l.startTurn({ chatId: '1', segmentId: seg.id, kind: 'message', inboundIds: [id], attempt: 0 })
  l.markTurnSent(t.id)
  l.outboundIntent({ chatId: '1', turnId: t.id, part: 1, kind: 'text', text: 'x' }) // 发了，没来得及记结果
  const r = l.recoverOnStartup(3)
  expect(r.ambiguousOutbound.length).toBe(1)
  expect(r.settled).toEqual([id])
  expect(l.inbound(id)!.state).toBe('done')
})

test('恢复：反复崩溃超过上限就进死信', () => {
  const l = new Ledger(':memory:')
  const id = msg(l, '1', 'a', 1)
  for (let i = 0; i < 3; i++) {
    const seg = l.createSegment('1', `tok${i}`, false)
    const t = l.startTurn({ chatId: '1', segmentId: seg.id, kind: 'message', inboundIds: [id], attempt: 0 })
    l.markTurnSent(t.id)
    l.recoverOnStartup(2)
  }
  expect(l.inbound(id)!.state).toBe('dead')
})

test('前情：对方说的 + bot 真正发出去的，按时间排，有字数上限，排除本轮的新消息', () => {
  const l = new Ledger(':memory:')
  let clock = 1_000_000
  l.now = () => (clock += 10)
  const a = l.insertInbound({ ukey: 'a', chatId: '1', kind: 'user', text: '早上好', ts: 1000 }).id
  const seg = l.createSegment('1', 't', false)
  const t = l.startTurn({ chatId: '1', segmentId: seg.id, kind: 'message', inboundIds: [a], attempt: 0 })
  const o = l.outboundIntent({ chatId: '1', turnId: t.id, part: 1, kind: 'text', text: '早呀' })
  l.outboundResult(o, 'sent', { tgMessageId: 9 })
  const f = l.outboundIntent({ chatId: '1', turnId: t.id, part: 2, kind: 'text', text: '没发出去的' })
  l.outboundResult(f, 'failed')
  const sys = l.outboundIntent({ chatId: '1', turnId: null, part: 0, kind: 'system', text: '【系统】' })
  l.outboundResult(sys, 'sent')
  l.settleInbound([a], 'done')
  const b = l.insertInbound({ ukey: 'b', chatId: '1', kind: 'user', text: '新消息', ts: 999999999999 }).id
  const tr = l.recentTranscript('1', { maxChars: 1000, excludeInboundIds: [b] })
  expect(tr.map(e => `${e.who}:${e.text}`)).toEqual(['user:早上好', 'bot:早呀'])
  expect(l.recentTranscript('1', { maxChars: 2 }).length).toBe(1)
})

test('回复发到一半中断：能认出来；整段发完的不算', () => {
  const l = new Ledger(':memory:')
  const id = msg(l, '1', 'a', 1)
  const seg = l.createSegment('1', 'tok', false)
  const t = l.startTurn({ chatId: '1', segmentId: seg.id, kind: 'message', inboundIds: [id], attempt: 0 })
  l.markTurnSent(t.id)
  const o1 = l.outboundIntent({ okey: `turn:${t.id}:c1:p1`, chatId: '1', turnId: t.id, part: 1, ofParts: 3, kind: 'text', text: 'x' })
  l.outboundResult(o1, 'sent', { tgMessageId: 1 })
  l.recoverOnStartup(3)
  expect(l.interruptedReply('1')).toEqual({ sent: 1, total: 3 })
  const t2 = l.startTurn({ chatId: '1', segmentId: l.createSegment('1', 'tok2', true).id, kind: 'message', inboundIds: [], attempt: 0 })
  l.finishTurn(t2.id, 'ok')
  expect(l.interruptedReply('1')).toBeNull()
})

test('老账本自动补列', () => {
  const l = new Ledger(':memory:')
  expect(l.db.query('PRAGMA table_info(outbound)').all().some((c: any) => c.name === 'of_parts')).toBe(true)
})
