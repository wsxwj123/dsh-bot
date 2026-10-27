// v1.4 读侧去重（约定第 22 节）：chat_id + ts取整到秒 + from_id 分组，组内按 observed_by 取行数最多那组。
import { expect, test } from 'bun:test'
import { dedupTranscriptLines } from '../../src/group/transcript'

const line = (o: Partial<Record<string, unknown>>) => ({
  ts: '2026-10-27T10:00:00.000Z', chat_id: '-5316971372', message_id: 1, from_id: '42',
  from_username: 'someone', from_name: '某人', is_bot: false, text: '你好', observed_by: 'bot_a',
  ...o,
} as never)

test('同一条消息被三只 bot 各记一遍 → 去重成 1 行', () => {
  const out = dedupTranscriptLines([
    line({ observed_by: 'bot_a' }), line({ observed_by: 'bot_b' }), line({ observed_by: 'bot_c' }),
  ])
  expect(out).toHaveLength(1)
})

test('ts 精度不同（毫秒 vs 整秒）也归一组——bot 自己发言带毫秒、peer 记的是秒级', () => {
  // 生产实证：1793115717.836 vs 1793115717.0，不取整会被分成两组、去重失效
  const out = dedupTranscriptLines([
    line({ ts: '2026-10-27T10:00:00.836Z', observed_by: 'bot_a' }),
    line({ ts: '2026-10-27T10:00:00.000Z', observed_by: 'bot_b' }),
  ])
  expect(out).toHaveLength(1)
})

test('同人同秒发两条 → 保留 2 行（不误合并）', () => {
  const out = dedupTranscriptLines([
    line({ message_id: 1, text: '第一句' }), line({ message_id: 2, text: '第二句' }),
  ])
  expect(out.map(l => l.text)).toEqual(['第一句', '第二句'])
})

test('某只漏收：两只各 2 行、一只 1 行 → 取 2 行那份', () => {
  const out = dedupTranscriptLines([
    line({ message_id: 1, text: 'A', observed_by: 'bot_a' }),
    line({ message_id: 2, text: 'B', observed_by: 'bot_a' }),
    line({ message_id: 1, text: 'A', observed_by: 'bot_b' }),
    line({ message_id: 2, text: 'B', observed_by: 'bot_b' }),
    line({ message_id: 1, text: 'A', observed_by: 'bot_c' }),
  ])
  expect(out).toHaveLength(2)
})

test('不同 from_id 同秒同文 → 不合并', () => {
  const out = dedupTranscriptLines([
    line({ from_id: '1', text: '好' }), line({ from_id: '2', text: '好' }),
  ])
  expect(out).toHaveLength(2)
})

test('ts 或 from_id 缺失的行原样保留', () => {
  const out = dedupTranscriptLines([
    line({ ts: undefined }), line({ from_id: '' }),
  ])
  expect(out).toHaveLength(2)
})

test('结果按 ts 升序（consumeSharedImages 靠数组顺序推进水位）', () => {
  const out = dedupTranscriptLines([
    line({ ts: '2026-10-27T10:00:02.000Z', message_id: 2 }),
    line({ ts: '2026-10-27T10:00:01.000Z', message_id: 1 }),
  ])
  expect(out.map(l => l.message_id)).toEqual([1, 2])
})

test('行数并列时取 observed_by 字典序最小那组（各读方结果一致）', () => {
  const out = dedupTranscriptLines([
    line({ observed_by: 'zeta', text: '来自 zeta' }),
    line({ observed_by: 'alpha', text: '来自 alpha' }),
  ])
  expect(out[0]!.text).toBe('来自 alpha')
})

// ─── 写侧：一条消息一行（v1.4 第 22 节 3.2）─────────

import { mkdtempSync, readFileSync, rmSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { GroupTranscript } from '../../src/group/transcript'
import { Logger } from '../../src/log'

function makeTranscript() {
  const dir = mkdtempSync(join(tmpdir(), 'gt-'))
  const t = new GroupTranscript(dir, () => ({ id: 1, username: 'bot_a', name: 'A' }), new Logger({}))
  const read = (chat = '-5316971372') => readFileSync(join(dir, `${chat}.jsonl`), 'utf8').trim().split('\n').filter(Boolean).map(l => JSON.parse(l))
  return { t, read, cleanup: () => rmSync(dir, { recursive: true, force: true }) }
}

const tgMsg = (o: Record<string, unknown> = {}) => ({
  message_id: 100, date: 1793115717, chat: { id: -5316971372, type: 'supergroup' },
  from: { id: 42, username: 'someone', first_name: '某人', is_bot: false }, text: '你好', ...o,
} as never)

test('有图：observe(msg, path) 只写一行，带 image_path', () => {
  const { t, read, cleanup } = makeTranscript()
  try {
    t.observe(tgMsg(), '/abs/pic.jpg')
    const rows = read()
    expect(rows).toHaveLength(1)
    expect(rows[0].image_path).toBe('/abs/pic.jpg')
    expect(rows[0].attachment_kind).toBe('photo')
    expect(rows[0].ts).toBe(new Date(1793115717 * 1000).toISOString())   // 用消息自带 date，不是本地时刻
  } finally { cleanup() }
})

test('下载失败/超时：observe(msg) 仍写一行，不带 image_path', () => {
  const { t, read, cleanup } = makeTranscript()
  try {
    t.observe(tgMsg())
    const rows = read()
    expect(rows).toHaveLength(1)
    expect(rows[0].image_path).toBeUndefined()
  } finally { cleanup() }
})

test('不再有 .seen 目录：每只 bot 都写自己的一行', () => {
  const { t, read, cleanup } = makeTranscript()
  try {
    t.observe(tgMsg())   // 同一网关写两次模拟「不同 bot」
    t.observe(tgMsg({ chat: { id: -5316971372, type: 'supergroup' } }))
    expect(read()).toHaveLength(2)   // 旧的抢锁会挡掉第二次
  } finally { cleanup() }
})
