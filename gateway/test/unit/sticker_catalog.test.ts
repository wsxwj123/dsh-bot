// sticker list 分类汇总与 query 列表：对照接口行为清单 1-8 与错误契约。
// 只通过对外接口（StickerService.list/send）调用，搭法与 sticker.test.ts 相同。
import { expect, test } from 'bun:test'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { basename, dirname, join } from 'path'
import { Ledger } from '../../src/ledger'
import { Logger } from '../../src/log'
import type { TelegramApi } from '../../src/telegram/api'
import { StickerService } from '../../src/stickers/sticker'

function rig(o: { api?: unknown } = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'sticker-catalog-'))
  const root = join(dir, 'stickers')
  mkdirSync(root, { recursive: true })
  const lib = join(dir, 'stickers.json')
  const calls: string[] = []
  const api = o.api ?? { sendFile: async (kind: string, _c: string, p: string) => { calls.push(`${kind}:${basename(p)}`); return { message_id: 7 } } }
  const ledger = new Ledger(':memory:')
  const svc = new StickerService({ api: api as TelegramApi, ledger, log: new Logger({ dir }), file: () => lib, root: () => root })
  const mk = (rel: string, content = 'GIF89a'): string => {
    const p = join(root, rel)
    mkdirSync(dirname(p), { recursive: true })
    writeFileSync(p, content)
    return p
  }
  const writeLib = (data: unknown) => writeFileSync(lib, JSON.stringify(data))
  return { dir, root, lib, mk, writeLib, calls, ledger, svc, done: () => { ledger.close(); rmSync(dir, { recursive: true, force: true }) } }
}

const nonEmptyLines = (text: string) => text.trim().split('\n').filter(l => l.length > 0)

test('行为1：不带 query 输出「分类名：N 张」汇总，按数量从多到少，不出现条目行与本地路径，末尾提示用 query', () => {
  const t = rig()
  t.writeLib({
    sfw: [
      { label: 'A1', category: '生气', path: '/x/a1.gif' },
      { label: 'A2', category: '生气', path: '/x/a2.gif' },
      { label: 'A3', category: '生气', path: '/x/a3.gif' },
      { label: 'H1', category: '开心', path: '/x/h1.gif' },
    ],
    nsfw: [
      { label: 'N1', category: '勾引', path: '/x/n1.gif' },
      { label: 'N2', category: '勾引', path: '/x/n2.gif' },
    ],
  })
  const r = t.svc.list({ action: 'list' })
  expect(r.text).toContain('生气：3 张')
  expect(r.text).toContain('勾引：2 张')
  expect(r.text).toContain('开心：1 张')
  // 数量从多到少
  expect(r.text.indexOf('生气：3 张')).toBeLessThan(r.text.indexOf('勾引：2 张'))
  expect(r.text.indexOf('勾引：2 张')).toBeLessThan(r.text.indexOf('开心：1 张'))
  // 不再逐条列出，也不泄露本地路径
  expect(r.text).not.toContain('#sfw:')
  expect(r.text).not.toContain('#nsfw:')
  expect(r.text).not.toContain('/x/')
  // 末尾提示用 query 查具体分类
  const rows = nonEmptyLines(r.text)
  const last = rows[rows.length - 1]!
  expect(last).toContain('query')
  expect(last).toContain('分类')
  t.done()
})

test('行为2：不带 query 且有条目没写 category 时，汇总出现「未分类：N 张」', () => {
  const t = rig()
  t.writeLib({
    sfw: [
      { label: 'H1', path: '/x/h1.gif' },
      { label: 'H2', path: '/x/h2.gif' },
      { label: 'H3', category: '开心', path: '/x/h3.gif' },
    ],
    nsfw: [{ label: 'N1', category: '勾引', path: '/x/n1.gif' }],
  })
  const r = t.svc.list({ action: 'list' })
  expect(r.text).toContain('未分类：2 张')
  expect(r.text).toContain('开心：1 张')
  expect(r.text).toContain('勾引：1 张')
  expect(r.text).not.toContain('#sfw:')
  expect(r.text).not.toContain('#nsfw:')
  t.done()
})

test('行为3：带 query 等于分类名时列出该分类下的条目，每行含 #sfw:N 和 label，不含路径', () => {
  const t = rig()
  t.writeLib({
    sfw: [
      { label: 'Angry 1', category: '生气', path: '/x/a1.gif' },
      { label: 'Angry 2', category: '生气', path: '/x/a2.gif' },
      { label: 'Happy 1', category: '开心', path: '/x/h1.gif' },
    ],
  })
  const r = t.svc.list({ action: 'list', query: '生气' })
  expect(r.text).toContain('#sfw:1 Angry 1')
  expect(r.text).toContain('#sfw:2 Angry 2')
  expect(r.text).not.toContain('#sfw:3')
  expect(r.text).not.toContain('Happy 1')
  expect(r.text).not.toContain('/x/')
  t.done()
})

test('行为4：带 query 等于某条 label 时照旧列出该条', () => {
  const t = rig()
  t.writeLib({
    sfw: [
      { label: 'Happy 1', path: '/x/a.gif' },
      { label: 'Hug 1', path: '/x/b.gif' },
    ],
  })
  const r = t.svc.list({ action: 'list', query: 'Happy 1' })
  expect(r.text).toContain('#sfw:1 Happy 1')
  expect(r.text).not.toContain('#sfw:2')
  expect(r.text).not.toContain('/x/a.gif')
  t.done()
})

test('行为5：带 query 零命中提示没有匹配，并说明标签是英文/中文', () => {
  const t = rig()
  t.writeLib({
    sfw: [{ label: 'Happy 1', path: '/x/a.gif' }],
    nsfw: [{ label: '露奶勾引', path: '/x/b.gif' }],
  })
  const r = t.svc.list({ action: 'list', query: 'zzz-no-such' })
  expect(r.text).toContain('没有匹配')
  expect(r.text).toContain('英文')
  expect(r.text).toContain('中文')
  t.done()
})

test('行为6a：tier=nsfw 且不带 query 时汇总只统计 nsfw', () => {
  const t = rig()
  t.writeLib({
    sfw: [
      { label: 'H1', category: '开心', path: '/x/h1.gif' },
      { label: 'H2', category: '开心', path: '/x/h2.gif' },
    ],
    nsfw: [
      { label: 'N1', category: '勾引', path: '/x/n1.gif' },
      { label: 'N2', category: '勾引', path: '/x/n2.gif' },
      { label: 'N3', path: '/x/n3.gif' },
    ],
  })
  const r = t.svc.list({ action: 'list', tier: 'nsfw' })
  expect(r.text).toContain('勾引：2 张')
  expect(r.text).toContain('未分类：1 张')
  expect(r.text).not.toContain('开心')
  expect(r.text).not.toContain('#nsfw:')
  t.done()
})

test('行为6b：tier=nsfw 且带 query 时列表只列 nsfw', () => {
  const t = rig()
  t.writeLib({
    sfw: [{ label: 'Happy 1', path: '/x/a.gif' }],
    nsfw: [{ label: 'Happy 2', path: '/x/b.gif' }],
  })
  const r = t.svc.list({ action: 'list', tier: 'nsfw', query: 'Happy' })
  expect(r.text).toContain('#nsfw:1 Happy 2')
  expect(r.text).not.toContain('#sfw:')
  t.done()
})

test('行为7：带 query 匹配超过 80 条时只列前 80 条', () => {
  const t = rig()
  t.writeLib({
    sfw: Array.from({ length: 85 }, (_, i) => ({ label: `Match ${i + 1}`, path: `/x/${i + 1}.gif` })),
  })
  const r = t.svc.list({ action: 'list', query: 'Match' })
  const rows = nonEmptyLines(r.text).filter(l => l.includes('#sfw:'))
  // 列表顺序接口未约定，只断言截断在 80 条
  expect(rows.length).toBe(80)
  expect(new Set(rows).size).toBe(80)
  expect(rows.every(l => l.includes('Match'))).toBe(true)
  t.done()
})

test('行为8a：send 正常发送走动画通道，回执含 label 且不含本地路径', async () => {
  const t = rig()
  const gif = t.mk('sfw/happy.gif')
  t.writeLib({ sfw: [{ label: 'Happy 1', path: gif }] })
  const turn = t.ledger.startTurn({ chatId: '1', segmentId: 1, kind: 'message', inboundIds: [], attempt: 0 })
  const r = await t.svc.send('1', turn.id, { action: 'send', query: 'Happy 1' })
  expect(r.isError).toBeFalsy()
  expect(r.text).toContain('Happy 1')
  expect(r.text).not.toContain(t.root)
  expect(t.calls).toEqual(['animation:happy.gif'])
  expect(t.ledger.deliveredInChain(turn.id)).toBe(1)
  t.done()
})

test('行为8b：send 找不到确切一条时列出候选且一个字节都不发', async () => {
  const t = rig()
  t.writeLib({
    sfw: [
      { label: 'Happy 1', path: '/x/a.gif' },
      { label: 'Happy 2', path: '/x/b.gif' },
    ],
  })
  const r = await t.svc.send('1', null, { action: 'send', query: 'happy' })
  expect(r.isError).toBe(true)
  expect(r.text).toContain('#sfw:1')
  expect(r.text).toContain('#sfw:2')
  expect(t.calls).toEqual([])
  t.done()
})

test('错误契约：库文件缺失或损坏时 list 返回 isError:true 的错误文本', () => {
  const missing = rig()
  const r1 = missing.svc.list({ action: 'list' })
  expect(r1.isError).toBe(true)
  expect(r1.text.length).toBeGreaterThan(0)
  missing.done()

  const broken = rig()
  writeFileSync(broken.lib, '{oops')
  const r2 = broken.svc.list({ action: 'list' })
  expect(r2.isError).toBe(true)
  expect(r2.text.length).toBeGreaterThan(0)
  broken.done()
})
