// dedupGroupKey 的生产路径：群记录去重键本身，以及 consumeSharedImages 用真实键跳过已认领的图。
// 现有 skipKeys 用例传的是消息编号（兜底分支）；这里验证 recap 通道实际写入的 dedupGroupKey 字符串能被命中。
import { expect, test } from 'bun:test'
import { mkdtempSync, realpathSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { consumeSharedImages } from '../../src/group/shared_images'
import { dedupGroupKey } from '../../src/group/transcript'

/** 1x1 透明 PNG（真实文件，不是伪造后缀） */
const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
  'base64',
)
const PNG_B64 = PNG.toString('base64')

const NOW = Date.now()

function transcriptLine(o: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    ts: new Date(NOW - 120_000).toISOString(),
    chat_id: '-1001777',
    message_id: 1,
    from_id: '42',
    from_username: 'someone',
    from_name: '某人',
    is_bot: false,
    text: '',
    observed_by: 'bot_a',
    ...o,
  }
}

/** 每个夹具独立目录，避免测试之间共享可变状态 */
function pngFile(name: string): string {
  const dir = mkdtempSync(join(tmpdir(), 'dedupkey-'))
  const p = join(dir, name)
  writeFileSync(p, PNG)
  return p
}

const readOk = (path: string) => ({
  ok: true as const,
  real: realpathSync(path),
  mimeType: 'image/png',
  data: PNG_B64,
})

test('同 chat、同一秒、同 from 的两行（写方不同、毫秒不同）→ 返回同一个键', () => {
  const a = transcriptLine({ ts: '2026-10-27T10:00:00.123Z', observed_by: 'bot_a', message_id: 101 })
  const b = transcriptLine({ ts: '2026-10-27T10:00:00.987Z', observed_by: 'bot_b', message_id: 102 })
  const keyA = dedupGroupKey(a as never)
  const keyB = dedupGroupKey(b as never)
  expect(keyA).not.toBeNull()
  expect(keyA).toBe(keyB)
})

test('缺 ts 的行 → 键为 null', () => {
  expect(dedupGroupKey(transcriptLine({ ts: undefined }) as never)).toBeNull()
})

test('from_id 为空串的行 → 键为 null', () => {
  expect(dedupGroupKey(transcriptLine({ from_id: '' }) as never)).toBeNull()
})

test('skipKeys 传入真实 dedupGroupKey 字符串：命中行不读文件、不占额度、上报 recap_attached', () => {
  const skippedPath = pngFile('skipped.png')
  const shownPath = pngFile('shown.png')
  const skipped = transcriptLine({ message_id: 201, ts: new Date(NOW - 120_000).toISOString(), image_path: skippedPath })
  const shown = transcriptLine({ message_id: 202, ts: new Date(NOW - 60_000).toISOString(), image_path: shownPath })
  const key = dedupGroupKey(skipped as never)
  expect(key).not.toBeNull()

  const reads: string[] = []
  const skips: Array<{ messageId: unknown; reason: unknown }> = []
  const res = consumeSharedImages({
    lines: [skipped, shown],
    watermark: { after: 0, ids: [] },
    botUsername: 'bot_a',
    max: 1,
    now: NOW,
    read: (p: string) => { reads.push(p); return readOk(p) },
    skipKeys: new Set<string | number>([key!]),
    onSkip: (line: { message_id: number }, reason: string) => { skips.push({ messageId: line.message_id, reason }) },
  } as never)

  // 命中行不读文件
  expect(reads).toEqual([shownPath])
  // 命中行不占额度：max=1 时后面的图仍被选中
  expect(res.picks.map(p => p.line.message_id)).toEqual([202])
  // 命中行被明确标记为 recap_attached
  expect(skips).toEqual([{ messageId: 201, reason: 'recap_attached' }])
})

test('skipKeys 命中的行被标记已消费：水位推进到该行，且没读文件', () => {
  const skipped = transcriptLine({ message_id: 211, ts: new Date(NOW - 120_000).toISOString(), image_path: pngFile('only.png') })
  const key = dedupGroupKey(skipped as never)
  expect(key).not.toBeNull()

  let reads = 0
  const res = consumeSharedImages({
    lines: [skipped],
    watermark: { after: 0, ids: [] },
    botUsername: 'bot_a',
    max: 2,
    now: NOW,
    read: () => { reads++; return readOk(String(skipped.image_path)) },
    skipKeys: new Set<string | number>([key!]),
  } as never)

  expect(res.picks).toEqual([])
  expect(reads).toBe(0)
  expect(res.advanced).toBe(true)
  expect(res.watermark).toEqual({ after: Date.parse(String(skipped.ts)), ids: [211] })
})
