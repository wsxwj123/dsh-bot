// sticker-variety：对照接口行为清单 1-7。
// 只通过对外接口（StickerService.list/send）调用，搭法与 sticker.test.ts / sticker_catalog.test.ts 相同。
// 顺序相关断言只比较多次调用，不依赖任何一次的具体顺序。
import { expect, test } from 'bun:test'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { basename, dirname, join } from 'path'
import { Ledger } from '../../src/ledger'
import { Logger } from '../../src/log'
import type { TelegramApi } from '../../src/telegram/api'
import { StickerService } from '../../src/stickers/sticker'

function rig(o: { api?: unknown } = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'sticker-variety-'))
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

// 从 list 文本中解析条目行（形如 "#sfw:1 Happy 1"），返回按出现顺序的整行文本
const entryRows = (text: string): string[] =>
  text.split('\n').map(l => l.trim()).filter(l => /#(?:sfw|nsfw):\d+/.test(l))

// 只取条目 id 的序列，用来比较顺序
const entryIds = (text: string): string[] =>
  entryRows(text).map(l => l.match(/#(?:sfw|nsfw):\d+/)![0])

test('行为1：list 带关键词且多条同分时，连续多次调用返回顺序不完全相同', () => {
  const t = rig()
  // 12 条等长标签，查询词相同，匹配分数应一致
  t.writeLib({
    sfw: Array.from({ length: 12 }, (_, i) => ({ label: `Same ${String(i + 1).padStart(2, '0')}`, path: `/x/${i + 1}.gif` })),
  })
  const orders = new Set<string>()
  for (let i = 0; i < 20; i++) {
    orders.add(entryIds(t.svc.list({ action: 'list', query: 'Same' }).text).join(','))
  }
  // 允许偶发相同，但 20 次里至少出现两种不同顺序
  expect(orders.size).toBeGreaterThan(1)
  t.done()
})

test('行为2：list 带关键词返回的条目集合与基线一致，只换顺序不增不减', () => {
  const t = rig()
  t.writeLib({
    sfw: [
      { label: 'Happy 1', path: '/x/1.gif' },
      { label: 'Happy 2', path: '/x/2.gif' },
      { label: 'Happy 3', path: '/x/3.gif' },
      { label: 'Sad 1', path: '/x/4.gif' },
    ],
    nsfw: [{ label: 'Happy 4', path: '/x/5.gif' }],
  })
  const baseline = new Set(entryRows(t.svc.list({ action: 'list', query: 'Happy' }).text))
  // 命中恰好 4 条，不包含不匹配的 Sad 1
  expect(baseline.size).toBe(4)
  expect([...baseline].some(l => l.includes('Sad 1'))).toBe(false)
  for (let i = 0; i < 10; i++) {
    const got = new Set(entryRows(t.svc.list({ action: 'list', query: 'Happy' }).text))
    expect(got).toEqual(baseline)
  }
  t.done()
})

test('行为3：不同分数时分数高的排在前面（精确标签优先于子串命中）', () => {
  const t = rig()
  t.writeLib({
    sfw: [
      { label: 'Hug 10', path: '/x/10.gif' }, // 子串命中
      { label: 'Hug 1', path: '/x/1.gif' },   // 精确命中
    ],
  })
  const rows = entryRows(t.svc.list({ action: 'list', query: 'Hug 1' }).text)
  const exact = rows.findIndex(l => l.includes('#sfw:2 Hug 1'))
  const fuzzy = rows.findIndex(l => l.includes('#sfw:1 Hug 10'))
  expect(exact).toBeGreaterThanOrEqual(0)
  expect(fuzzy).toBeGreaterThanOrEqual(0)
  expect(exact).toBeLessThan(fuzzy)
  t.done()
})

test('行为4：send 按 label 精确命中，行为不变', async () => {
  const t = rig()
  const a = t.mk('sfw/happy.gif')
  t.mk('sfw/hug.gif')
  t.writeLib({ sfw: [{ label: 'Happy 1', path: a }, { label: 'Hug 1', path: join(t.root, 'sfw/hug.gif') }] })
  const res = await t.svc.send('1', null, { action: 'send', label: 'Happy 1' })
  expect(res.isError).toBeFalsy()
  expect(res.text).toContain('Happy 1')
  expect(res.text).not.toContain(t.root)
  expect(t.calls).toEqual(['animation:happy.gif'])
  t.done()
})

test('行为5：send 按 id 命中，行为不变', async () => {
  const t = rig()
  t.mk('sfw/a.gif')
  t.mk('sfw/b.gif')
  t.writeLib({ sfw: [{ label: 'First 1', path: join(t.root, 'sfw/a.gif') }, { label: 'Second 1', path: join(t.root, 'sfw/b.gif') }] })
  const res = await t.svc.send('1', null, { action: 'send', id: '#sfw:2' })
  expect(res.isError).toBeFalsy()
  expect(res.text).toContain('Second 1')
  expect(res.text).not.toContain(t.root)
  expect(t.calls).toEqual(['animation:b.gif'])
  t.done()
})

test('行为6：不带关键词的 list 仍返回分类汇总', () => {
  const t = rig()
  t.writeLib({
    sfw: [
      { label: 'A1', category: '生气', path: '/x/a1.gif' },
      { label: 'A2', category: '生气', path: '/x/a2.gif' },
      { label: 'H1', category: '开心', path: '/x/h1.gif' },
      { label: 'U1', path: '/x/u1.gif' },
    ],
    nsfw: [{ label: 'N1', category: '勾引', path: '/x/n1.gif' }],
  })
  const r = t.svc.list({ action: 'list' })
  expect(r.isError).toBeFalsy()
  expect(r.text).toContain('生气：2 张')
  expect(r.text).toContain('开心：1 张')
  expect(r.text).toContain('未分类：1 张')
  expect(r.text).toContain('勾引：1 张')
  // 分类汇总不逐条列条目，也不泄露本地路径
  expect(entryRows(r.text)).toEqual([])
  expect(r.text).not.toContain('/x/')
  t.done()
})

test('行为7：技能模板不再说「sfw 的标签是英文」，且说明标签中英都有', () => {
  const p = join(import.meta.dir, '../../../templates/skills/skill-gif/SKILL.md')
  const text = readFileSync(p, 'utf8')
  // 不得出现把 sfw 标签限定为英文的表述
  expect(text).not.toMatch(/sfw\s*的标签[^，。\n]*英文/)
  expect(text).not.toContain('sfw 的标签是英文')
  // 必须说明标签中英都有：出现「中英」，或中文与英文同时出现
  const mentionsBoth = text.includes('中英') || (text.includes('中文') && text.includes('英文'))
  expect(mentionsBoth).toBe(true)
})
