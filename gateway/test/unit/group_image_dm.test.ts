// group-image-in-dm 纯函数测试（黑盒，依据 .devflow/group-image-in-dm-接口.md 第 1、2、3 节与错误契约）。
// 只调用导出入口：buildGroupRecapBlocks / resetCrossChatWatermarks / consumeSharedImages；
// 图片一律用真实存在的 1x1 PNG 文件，read 依赖由测试注入，不碰磁盘上的真实读图实现。
import { expect, test } from 'bun:test'
import { mkdtempSync, realpathSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { Engine } from '../../src/engine/engine'
import * as format from '../../src/engine/format'
import { consumeSharedImages } from '../../src/group/shared_images'

/** 1x1 透明 PNG（真实文件，不是伪造后缀） */
const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
  'base64',
)
const PNG_B64 = PNG.toString('base64')

/** 每个夹具独立目录，避免测试之间共享可变状态 */
function pngFile(name = 'pic.png'): string {
  const dir = mkdtempSync(join(tmpdir(), 'groupimg-dm-'))
  const p = join(dir, name)
  writeFileSync(p, PNG)
  return p
}

type Block = { type: string; text?: string; data?: string; mimeType?: string }
type BlocksResult = { blocks: Block[]; seenReals: Set<string> } | null

/**
 * 实现还不存在的入口用模块命名空间取：缺失时只有对应用例红，不拖垮整个文件。
 * 断言前先检查入口存在，失败信息能直接看出"接口没实现"。
 */
const buildGroupRecapBlocks = (format as unknown as Record<string, unknown>).buildGroupRecapBlocks as
  | ((rows: never, read: never) => BlocksResult)
  | undefined
const buildGroupRecapText = format.buildGroupRecapText

const readOk = (path: string) => ({
  ok: true as const,
  real: realpathSync(path),
  mimeType: 'image/png',
  data: PNG_B64,
})

const imageBlocks = (blocks: Block[]) => blocks.filter(b => b.type === 'image')
const textBlocks = (blocks: Block[]) => blocks.filter(b => b.type === 'text')
const textOf = (blocks: Block[]) => textBlocks(blocks).map(b => b.text ?? '')
const idxOfText = (blocks: Block[], needle: string) =>
  blocks.findIndex(b => b.type === 'text' && (b.text ?? '').includes(needle))

function recapBlocks(rows: Record<string, unknown>[], read: (p: string) => unknown): BlocksResult {
  expect(typeof buildGroupRecapBlocks, 'format.buildGroupRecapBlocks 必须是可调用入口').toBe('function')
  return buildGroupRecapBlocks!(rows as never, read as never)
}

let keySeq = 0
function row(o: Record<string, unknown> = {}): Record<string, unknown> {
  return { ts: Date.now() - 60_000, from: 'owner', key: `k${++keySeq}`, text: '', ...o }
}

function blocksOf(rows: Record<string, unknown>[], read: (p: string) => unknown): Block[] {
  const res = recapBlocks(rows, read)
  expect(res).not.toBeNull()
  return res!.blocks
}

// ───────── 第 1 节：buildGroupRecapBlocks ─────────

test('清单1 一行文本+一行带图：图片块紧跟在带图那行的文本块之后', () => {
  const img = pngFile()
  const blocks = blocksOf(
    [
      row({ ts: Date.now() - 120_000, text: '第一行' }),
      row({ ts: Date.now() - 60_000, text: '第二行', imagePath: img, attachment_kind: 'photo' }),
    ],
    readOk,
  )
  const i = idxOfText(blocks, '第二行')
  expect(i).toBeGreaterThanOrEqual(0)
  expect(blocks[i + 1]?.type).toBe('image')
  expect(blocks[i + 1]?.data).toBe(PNG_B64)
  expect(imageBlocks(blocks)).toHaveLength(1)
})

test('清单2 纯图片行（text 为空）：文本块是 [图片]，紧跟 image block', () => {
  const blocks = blocksOf([row({ text: '', imagePath: pngFile(), attachment_kind: 'photo' })], readOk)
  const i = idxOfText(blocks, '[图片]')
  expect(i).toBeGreaterThanOrEqual(0)
  expect(textBlocks(blocks)[0]?.text).toContain('[图片]')
  expect(blocks[i + 1]?.type).toBe('image')
})

test('清单3 读图返回 ok:false（任何 reason）：不产生 image block，文本保持 [图片]，不含「看不到内容」', () => {
  for (const reason of ['realpath_failed', 'too_large', 'too_many_pixels', 'outside_allowlist'] as const) {
    const blocks = blocksOf(
      [row({ text: '', imagePath: pngFile(), attachment_kind: 'photo' })],
      () => ({ ok: false, reason }),
    )
    expect(imageBlocks(blocks), `reason=${reason} 不应附图`).toHaveLength(0)
    const joined = textOf(blocks).join('\n')
    expect(joined, `reason=${reason} 应保留 [图片]`).toContain('[图片]')
    expect(joined, `reason=${reason} 不应出现「看不到内容」`).not.toContain('看不到内容')
  }
})

test('清单4 4 行都带图：只附最新 3 行的图，最早那行只有文本', () => {
  const blocks = blocksOf(
    [
      row({ ts: Date.now() - 240_000, text: '最早行', imagePath: pngFile('a.png'), attachment_kind: 'photo' }),
      row({ ts: Date.now() - 180_000, text: '第二行', imagePath: pngFile('b.png'), attachment_kind: 'photo' }),
      row({ ts: Date.now() - 120_000, text: '第三行', imagePath: pngFile('c.png'), attachment_kind: 'photo' }),
      row({ ts: Date.now() - 60_000, text: '最新行', imagePath: pngFile('d.png'), attachment_kind: 'photo' }),
    ],
    readOk,
  )
  expect(imageBlocks(blocks)).toHaveLength(3)
  const oldest = idxOfText(blocks, '最早行')
  expect(blocks[oldest + 1]?.type).not.toBe('image')
  for (const marker of ['第二行', '第三行', '最新行']) {
    const i = idxOfText(blocks, marker)
    expect(blocks[i + 1]?.type, `${marker} 后应紧跟图片`).toBe('image')
  }
})

test('清单5 两行指向同一 realpath：图片只附一次', () => {
  const img = pngFile()
  const res = recapBlocks(
    [
      row({ ts: Date.now() - 120_000, text: '同一张一', imagePath: img, attachment_kind: 'photo' }),
      row({ ts: Date.now() - 60_000, text: '同一张二', imagePath: img, attachment_kind: 'photo' }),
    ],
    readOk,
  )
  expect(res).not.toBeNull()
  const blocks = res!.blocks
  expect(imageBlocks(blocks)).toHaveLength(1)
  expect([...res!.seenReals]).toEqual([realpathSync(img)])
})

test('清单6 self 行（bot 自己发的）：不附图', () => {
  const res = recapBlocks(
    [row({ from: 'bot', text: '我自己发的', imagePath: pngFile(), attachment_kind: 'photo' })],
    readOk,
  )
  expect(res).not.toBeNull()
  const blocks = res!.blocks
  expect(imageBlocks(blocks)).toHaveLength(0)
  expect(res!.seenReals.size).toBe(0)
  expect(idxOfText(blocks, '我自己发的')).toBeGreaterThanOrEqual(0)
})

test('清单7 attachment_kind 不是 photo（document）：不附图', () => {
  const res = recapBlocks(
    [row({ text: '一个文件', imagePath: pngFile(), attachment_kind: 'document' })],
    readOk,
  )
  expect(res).not.toBeNull()
  expect(imageBlocks(res!.blocks)).toHaveLength(0)
  expect(res!.seenReals.size).toBe(0)
})

test('清单8 空数组：返回 null', () => {
  expect(recapBlocks([], readOk)).toBeNull()
})

test('清单9 无图但有文本：文本与 buildGroupRecapText 的输出逐字相同', () => {
  const blocks = blocksOf(
    [
      row({ ts: Date.now() - 120_000, from: 'owner', text: '第一句' }),
      row({ ts: Date.now() - 60_000, from: 'bot', text: '第二句' }),
    ],
    readOk,
  )
  expect(imageBlocks(blocks)).toHaveLength(0)
  const expected = buildGroupRecapText(['对方：第一句', '你：第二句'])
  expect(textOf(blocks).join('\n')).toBe(expected!)   // buildGroupRecapText 返回 string | null，这里保证非空
})

test('清单10 行的 ts 在未来：该行被跳过（不选、不附图）', () => {
  const res = recapBlocks(
    [
      row({ ts: Date.now() - 60_000, text: '现在的话' }),
      row({ ts: Date.now() + 60 * 60_000, text: '未来的话', imagePath: pngFile(), attachment_kind: 'photo' }),
    ],
    readOk,
  )
  expect(res).not.toBeNull()
  const blocks = res!.blocks
  expect(textOf(blocks).join('\n')).not.toContain('未来的话')
  expect(imageBlocks(blocks)).toHaveLength(0)
  expect(res!.seenReals.size).toBe(0)
  expect(textOf(blocks).join('\n')).toContain('现在的话')
})

// ───────── 第 2 节：resetCrossChatWatermarks ─────────

const wmKeys = (chatId: string) => [
  `group_recap:${chatId}`,
  `group_images_dm:${chatId}`,
  `group_images:${chatId}`,
  `dm_recap:${chatId}`,
]

function fakeLedger(seed: Record<string, string> = {}) {
  const store = new Map<string, string>(Object.entries(seed))
  return {
    store,
    getMeta: (k: string) => store.get(k) ?? null,
    setMeta: (k: string, v: string) => { store.set(k, v) },
    deleteMeta: (k: string) => { store.delete(k) },
  }
}

/** 实现按接口约定是 engine.ts 里的 resetCrossChatWatermarks(chatId)；用 Engine 原型调用，不构造整个引擎 */
async function callReset(ledger: ReturnType<typeof fakeLedger>, chatId: string): Promise<void> {
  const fn = (Engine.prototype as unknown as Record<string, unknown>).resetCrossChatWatermarks as
    | ((this: unknown, id: string) => unknown)
    | undefined
  expect(typeof fn, 'Engine.prototype.resetCrossChatWatermarks 必须是可调用入口').toBe('function')
  await fn!.call({ ledger, log: { info() {}, warn() {}, debug() {}, error() {} } }, chatId)
}

test('清单11 调用后：四个水位键都为空', async () => {
  const chatId = '-1001234'
  const ledger = fakeLedger(Object.fromEntries(wmKeys(chatId).map(k => [k, '1700000000000'])))
  await callReset(ledger, chatId)
  for (const k of wmKeys(chatId)) expect(ledger.getMeta(k) ?? '', `${k} 应被清空`).toBe('')
})

test('清单12 另一个 chatId 有水位：不受影响', async () => {
  const chatId = '-1001234'
  const other = '-1005678'
  const ledger = fakeLedger({
    ...Object.fromEntries(wmKeys(chatId).map(k => [k, '1700000000000'])),
    ...Object.fromEntries(wmKeys(other).map(k => [k, 'keep-me'])),
  })
  await callReset(ledger, chatId)
  for (const k of wmKeys(other)) expect(ledger.getMeta(k)).toBe('keep-me')
})

// ───────── 第 3 节：consumeSharedImages 的 skipKeys / initialSeenReal ─────────

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

function countedRead() {
  const calls: string[] = []
  return {
    calls,
    read: (p: string) => {
      calls.push(p)
      return readOk(p)
    },
  }
}

test('消费 skipKeys 命中的行：不读文件、不出图、水位标记为已消费', () => {
  const a = transcriptLine({ ts: new Date(NOW - 120_000).toISOString(), message_id: 11, image_path: pngFile('a.png'), attachment_kind: 'photo' })
  const b = transcriptLine({ ts: new Date(NOW - 60_000).toISOString(), message_id: 12, image_path: pngFile('b.png'), attachment_kind: 'photo' })
  const r = countedRead()
  const res = consumeSharedImages({
    lines: [a, b],
    watermark: { after: 0, ids: [] },
    botUsername: 'bot_a',
    max: 2,
    now: NOW,
    read: r.read,
    skipKeys: new Set<unknown>([11, '11']),
  } as never)
  expect(res.picks.map(p => p.line.message_id)).toEqual([12])
  expect(r.calls).not.toContain(a.image_path)
  expect(res.watermark.after).toBeGreaterThanOrEqual(Date.parse(String(a.ts)))
  expect(res.advanced).toBe(true)
})

test('skipKeys 命中的行不占每轮额度：max=1 时后面的图仍能被选中', () => {
  const a = transcriptLine({ ts: new Date(NOW - 120_000).toISOString(), message_id: 21, image_path: pngFile('a.png'), attachment_kind: 'photo' })
  const b = transcriptLine({ ts: new Date(NOW - 60_000).toISOString(), message_id: 22, image_path: pngFile('b.png'), attachment_kind: 'photo' })
  const r = countedRead()
  const res = consumeSharedImages({
    lines: [a, b],
    watermark: { after: 0, ids: [] },
    botUsername: 'bot_a',
    max: 1,
    now: NOW,
    read: r.read,
    skipKeys: new Set<unknown>([21, '21']),
  } as never)
  expect(res.picks.map(p => (p.line as any).message_id)).toEqual([22])
  expect(r.calls as string[]).toEqual([b.image_path as string])
})

test('initialSeenReal 命中：读之前就跳过该 realpath，不重复读、不出图、标记已消费', () => {
  const aPath = pngFile('a.png')
  const a = transcriptLine({ ts: new Date(NOW - 120_000).toISOString(), message_id: 31, image_path: aPath, attachment_kind: 'photo' })
  const b = transcriptLine({ ts: new Date(NOW - 60_000).toISOString(), message_id: 32, image_path: pngFile('b.png'), attachment_kind: 'photo' })
  const r = countedRead()
  const res = consumeSharedImages({
    lines: [a, b],
    watermark: { after: 0, ids: [] },
    botUsername: 'bot_a',
    max: 2,
    now: NOW,
    read: r.read,
    initialSeenReal: new Set([realpathSync(aPath)]),
  } as never)
  expect(res.picks.map(p => p.line.message_id)).toEqual([32])
  expect(r.calls).not.toContain(aPath)
  expect(res.watermark.after).toBeGreaterThanOrEqual(Date.parse(String(a.ts)))
  expect(res.advanced).toBe(true)
})
