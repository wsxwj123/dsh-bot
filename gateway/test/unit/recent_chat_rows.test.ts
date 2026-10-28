// recentChatRows：跨聊天近况的账本查询（跨 inbound/outbound、kind/state 混杂、空文本、limit）。
import { expect, test } from 'bun:test'
import { Ledger, type InboundKind } from '../../src/ledger'

const HOUR = 3_600_000
const DM = '5331715732'

type Row = { ts: number; from: 'owner' | 'bot'; text: string }

function setup() {
  const now = Date.now()
  const l = new Ledger(':memory:')
  const inbound = (ukey: string, kind: InboundKind, text: string, ts: number, chatId = DM) =>
    l.insertInbound({ ukey, chatId, kind, text, ts })
  const outbound = (okey: string, text: string | null, kind: string, state: string, sentAt: number | null) =>
    l.db.query(`INSERT INTO outbound (okey, chat_id, part, kind, text, state, created_at, sent_at) VALUES (?, ?, 0, ?, ?, ?, ?, ?)`)
      .run(okey, DM, kind, text, state, now - 5 * HOUR, sentAt)
  return { l, now, inbound, outbound }
}

test('只取真实用户消息与送达的文字：kind/state/空文本/别的聊天都排除，按时间升序', () => {
  const { l, now, inbound, outbound } = setup()
  inbound('u1', 'user', '主人甲', now - 4 * HOUR)
  inbound('u2', 'user', '   ', now - 3 * HOUR) // 纯空白
  inbound('u3', 'synthetic', '合成提示', now - 2.5 * HOUR)
  inbound('u4', 'command', '/clear', now - 2.4 * HOUR)
  inbound('u5', 'user', '别的聊天', now - 2 * HOUR, 'other')
  inbound('u6', 'user', '主人乙', now - HOUR)
  inbound('u7', 'user', '太旧', now - 30 * HOUR)
  outbound('o1', '你甲', 'text', 'sent', now - 3.5 * HOUR)
  outbound('o2', '❤️', 'reaction', 'sent', now - 3.4 * HOUR) // 表情不该算"你："
  outbound('o3', '【系统】通知', 'system', 'sent', now - 3.3 * HOUR) // 运维通知不该算
  outbound('o4', '没送达', 'text', 'failed', now - 3.2 * HOUR)
  outbound('o5', '疑似送达', 'text', 'ambiguous', now - 3.1 * HOUR)
  outbound('o6', '', 'text', 'sent', now - 3.0 * HOUR)
  outbound('o7', '   ', 'text', 'sent', now - 2.9 * HOUR)
  outbound('o8', null, 'text', 'sent', now - 2.8 * HOUR)
  outbound('o9', '你乙', 'text', 'sent', now - 30 * 60_000)
  outbound('o10', '太旧', 'text', 'sent', now - 30 * HOUR)

  const got = l.recentChatRows(DM, now - 24 * HOUR, 20)
  expect(got.map(r => `${r.from}:${r.text}`)).toEqual(['owner:主人甲', 'bot:你甲', 'owner:主人乙', 'bot:你乙'])
  for (let i = 1; i < got.length; i++) expect(got[i]!.ts).toBeGreaterThanOrEqual(got[i - 1]!.ts)
  l.close()
})

test('limit 取合并后的最后 N 条；limit<=0 或时间起点在未来都返回空', () => {
  const { l, now, inbound, outbound } = setup()
  inbound('u1', 'user', '一', now - 4 * HOUR)
  outbound('o1', '二', 'text', 'sent', now - 3 * HOUR)
  inbound('u2', 'user', '三', now - 2 * HOUR)
  outbound('o2', '四', 'text', 'sent', now - HOUR)

  expect(l.recentChatRows(DM, now - 24 * HOUR, 2).map(r => r.text)).toEqual(['三', '四'])
  expect(l.recentChatRows(DM, now - 24 * HOUR, 0)).toEqual([])
  expect(l.recentChatRows(DM, now + HOUR, 20)).toEqual([])
  expect(l.recentChatRows('nobody', now - 24 * HOUR, 20)).toEqual([])
  l.close()
})

test('空文本先过滤再 LIMIT：最近 25 条空 text 不会挤掉更早的真实文字', () => {
  const { l, now, inbound, outbound } = setup()
  inbound('real', 'user', '更早的真实文字', now - 2 * HOUR)
  for (let i = 0; i < 25; i++) outbound(`empty-${i}`, i % 2 ? '' : null, 'text', 'sent', now - i * 60_000)
  const got = l.recentChatRows(DM, now - 24 * HOUR, 20)
  expect(got.map(r => r.text)).toEqual(['更早的真实文字'])
  l.close()
})

test('查询失败返回空数组，不抛', () => {
  const l = new Ledger(':memory:')
  l.close()
  expect(l.recentChatRows(DM, 0, 20)).toEqual([])
})

test('同一毫秒的行都保留（按 id 兜底排序）', () => {
  const { l, now, inbound, outbound } = setup()
  const ts = now - HOUR
  inbound('u1', 'user', '先', ts)
  outbound('o1', '后', 'text', 'sent', ts)
  const got: Row[] = l.recentChatRows(DM, now - 24 * HOUR, 20)
  expect(got.map(r => r.text).sort()).toEqual(['先', '后'])
  expect(got).toHaveLength(2)
  l.close()
})
