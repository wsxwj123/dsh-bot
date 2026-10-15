// 摘要查看（.devflow/INTERFACE-摘要查看.md）：/summary 命令、/compact 回复附摘要原文、超长按段拆、账本取最近一份摘要
import { expect, test } from 'bun:test'
import { compactReply, HELP_TEXT, splitReply, summaryReply } from '../../src/engine/commands'
import { OWNER_COMMANDS } from '../../src/engine/engine'
import { Ledger } from '../../src/ledger'
import { GATEWAY_COMMANDS, parseCommand } from '../../src/telegram/inbound'

const TZ = 'Asia/Shanghai'

test('/summary 是网关命令：不交给模型，只认主人（与 /compact 同一张表），帮助里列出', () => {
  expect(parseCommand('/summary')?.name).toBe('summary')
  for (const name of ['compact', 'summary']) {
    expect(GATEWAY_COMMANDS.has(name)).toBe(true)
    expect(OWNER_COMMANDS.has(name)).toBe(true)
  }
  expect(HELP_TEXT).toContain('/summary')
})

test('没有任何摘要：回「还没有摘要」', () => {
  expect(summaryReply(null, TZ)).toContain('还没有摘要')
})

test('有摘要：第一行写起止时间与换段原因，下面是摘要原文一字不改', () => {
  const seg = { created_at: Date.UTC(2026, 9, 15, 0, 5), closed_at: Date.UTC(2026, 9, 15, 2, 30), close_reason: 'compact', summary: '第一段要点\n\n第二段  原样' }
  const out = summaryReply(seg, TZ)
  const head = out.slice(0, out.indexOf('\n'))
  expect(head).toContain('08:05')        // 按 bot 的时区（UTC+8）写
  expect(head).toContain('10:30')
  expect(head).toContain('手动压缩')
  expect(out.slice(out.indexOf('\n')).trim()).toBe(seg.summary)
  const headOf = (reason: string) => { const s = summaryReply({ ...seg, close_reason: reason }, TZ); return s.slice(0, s.indexOf('\n')) }
  expect(headOf('prompt-changed')).toContain('人设或技能改了')
  expect(headOf('budget')).toContain('budget')     // 其他原因照原词写
})

test('/compact 成功的回复：原来那句之后另起一段附上摘要原文；摘要没写成时不附', () => {
  const ok = compactReply({ how: 'in-session', chars: 6, summary: '这是摘要原文' }, 1234)
  expect(ok.startsWith('【系统】已把这段对话压缩成一份摘要（6 字，压缩前约 1234 token）')).toBe(true)
  expect(ok.endsWith('\n\n这是摘要原文')).toBe(true)
  expect(compactReply({ how: 'ledger', chars: 3, summary: '甲乙丙' }, null).endsWith('\n\n甲乙丙')).toBe(true)
  for (const how of ['none', 'crashed'] as const) {
    const r = compactReply({ how, chars: 0, summary: null }, 10)
    expect(r.startsWith('【系统】')).toBe(true)
    expect(r).not.toContain('\n\n')
  }
})

test('超过 Telegram 单条上限：按段拆成几条，每条不超过 4096 字，内容一个字不少', () => {
  const text = Array.from({ length: 12 }, (_, i) => `第${i}段` + '字'.repeat(900)).join('\n\n')
  const parts = splitReply(text)
  expect(parts.length).toBeGreaterThan(1)
  for (const p of parts) expect(p.length).toBeLessThanOrEqual(4096)
  expect(parts.join('\n\n')).toBe(text)          // 在段与段之间切开，拼回去就是原文
  const huge = '长'.repeat(10000)                 // 单独一段就超长：硬切，也不丢字
  const hp = splitReply(huge)
  for (const p of hp) expect(p.length).toBeLessThanOrEqual(4096)
  expect(hp.join('')).toBe(huge)
  expect(splitReply('短短一句')).toEqual(['短短一句'])
})

test('账本：取最近一份已关闭且有摘要的段；进行中的段与没写成摘要的段不算；没有时为 null', () => {
  const l = new Ledger(':memory:')
  expect(l.lastSummarySegment('1')).toBeNull()
  const a = l.createSegment('1', 't', false)
  l.setSegmentSummary(a.id, '甲的摘要')
  l.closeSegment(a.id, 'closed', 'compact')
  const b = l.createSegment('1', 't', false)
  l.closeSegment(b.id, 'closed', 'clear (summary failed)')
  l.createSegment('1', 't', false)                // 进行中
  const got = l.lastSummarySegment('1')!
  expect([got.id, got.summary, got.close_reason]).toEqual([a.id, '甲的摘要', 'compact'])
  expect(got.closed_at).not.toBeNull()
  expect(l.lastSummarySegment('2')).toBeNull()    // 别的聊天
})
