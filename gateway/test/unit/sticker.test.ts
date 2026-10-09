// 表情包工具：库解析（两档都认）、按标签找（命中/近似/找不到）、路径越界被拒、
// gif 与静态图的分流判定、发送失败如实回报（不假装成功、可能已送达的不重发）
import { expect, test } from 'bun:test'
import { mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { basename, dirname, join } from 'path'
import { Ledger } from '../../src/ledger'
import { Logger } from '../../src/log'
import { TgApiError, TgNetworkError, type TelegramApi } from '../../src/telegram/api'
import { fileKind } from '../../src/telegram/sender'
import { findSticker, loadStickers, StickerService, type StickerEntry } from '../../src/stickers/sticker'

function rig(o: { api?: unknown } = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'sticker-'))
  const root = join(dir, 'stickers')
  mkdirSync(root, { recursive: true })
  const lib = join(dir, 'stickers.json')
  const calls: string[] = []
  const api = o.api ?? { sendFile: async (kind: string, _c: string, p: string) => { calls.push(`${kind}:${basename(p)}`); return { message_id: 7 } } }
  const ledger = new Ledger(':memory:')
  const svc = new StickerService({ api: api as TelegramApi, ledger, log: new Logger(), file: () => lib, root: () => root })
  const mk = (rel: string, content = 'GIF89a'): string => {
    const p = join(root, rel)
    mkdirSync(dirname(p), { recursive: true })
    writeFileSync(p, content)
    return p
  }
  const writeLib = (data: unknown) => writeFileSync(lib, JSON.stringify(data))
  return { dir, root, lib, mk, writeLib, calls, ledger, svc, done: () => { ledger.close(); rmSync(dir, { recursive: true, force: true }) } }
}

test('库解析：sfw / nsfw 两档都认；缺 label / path 的条目跳过；坏文件返回空不抛', () => {
  const t = rig()
  t.writeLib({
    sfw: [
      { label: 'Happy 1', path: '/x/a.gif' },
      { label: '', path: '/x/b.gif' },      // 缺 label
      { path: '/x/c.gif' },                 // 缺 label
      { label: 'NoPath' },                  // 缺 path
      'junk',                               // 不是对象
    ],
    nsfw: [{ label: '露奶勾引', category: 'Foreplay/Seduction (勾引/前戏)', path: '/x/d.gif' }],
  })
  const es = loadStickers(t.lib)
  expect(es.map(e => [e.id, e.tier, e.label])).toEqual([['sfw:1', 'sfw', 'Happy 1'], ['nsfw:1', 'nsfw', '露奶勾引']])
  expect(es[1]!.category).toBe('Foreplay/Seduction (勾引/前戏)')
  expect(es[0]!.category).toBeNull()
  writeFileSync(join(t.dir, 'broken.json'), '{oops')
  expect(loadStickers(join(t.dir, 'broken.json'))).toEqual([])
  expect(loadStickers(join(t.dir, 'missing.json'))).toEqual([])
  t.done()
})

test('发送分流：gif 走动画、webp/tgs 走贴纸、图片走照片、其余走文件', () => {
  expect(fileKind('/x/a.gif')).toBe('animation')
  expect(fileKind('/x/A.GIF')).toBe('animation')
  expect(fileKind('/x/a.webp')).toBe('sticker')
  expect(fileKind('/x/a.tgs')).toBe('sticker')
  expect(fileKind('/x/a.jpg')).toBe('photo')
  expect(fileKind('/x/a.png')).toBe('photo')
  expect(fileKind('/x/a.pdf')).toBe('document')
})

test('找标签：精确命中、子串唯一命中、多条沾边给候选、完全找不到给空；id 可直接给', () => {
  const es: StickerEntry[] = [
    { id: 'sfw:1', tier: 'sfw', label: 'Happy 12', category: null, path: '/x/1.gif' },
    { id: 'sfw:2', tier: 'sfw', label: 'Happy 13', category: null, path: '/x/2.gif' },
    { id: 'sfw:3', tier: 'sfw', label: 'Hug 1', category: null, path: '/x/3.gif' },
    { id: 'nsfw:1', tier: 'nsfw', label: '露奶勾引', category: 'Foreplay/Seduction (勾引/前戏)', path: '/x/4.gif' },
  ]
  expect(findSticker(es, { query: 'Hug 1' }).entry?.id).toBe('sfw:3')       // label 完全等于
  expect(findSticker(es, { query: 'hug' }).entry?.id).toBe('sfw:3')        // 子串唯一命中
  const many = findSticker(es, { query: 'happy' })                          // 多条沾边：不替它挑
  expect(many.entry).toBeUndefined()
  expect(many.near?.map(e => e.id)).toEqual(['sfw:1', 'sfw:2'])
  expect(findSticker(es, { query: '勾引' }).entry?.id).toBe('nsfw:1')       // 中文分类命中
  expect(findSticker(es, { query: 'zzz' }).near).toEqual([])                // 一条不沾
  expect(findSticker(es, { id: '#sfw:2' }).entry?.id).toBe('sfw:2')         // id 允许带 #
  expect(findSticker(es, { id: 'sfw:9' }).entry).toBeUndefined()
  expect(findSticker(es, { query: 'hug', tier: 'nsfw' }).entry).toBeUndefined() // tier 限定后找不到
})

test('send：gif 走动画通道发出，账本记成已送达（这一轮算已回复），回执不带本地路径', async () => {
  const t = rig()
  const gif = t.mk('sfw/happy.gif')
  t.writeLib({ sfw: [{ label: 'Happy 1', path: gif }] })
  const turn = t.ledger.startTurn({ chatId: '1', segmentId: 1, kind: 'message', inboundIds: [], attempt: 0 })
  const res = await t.svc.send('1', turn.id, { action: 'send', query: 'Happy 1' })
  expect(res.isError).toBeFalsy()
  expect(res.text).toContain('Happy 1')
  expect(res.text).not.toContain(t.root)
  expect(t.calls).toEqual(['animation:happy.gif'])
  expect(t.ledger.deliveredInChain(turn.id)).toBe(1)
  t.done()
})

test('send：jpg 走照片、webp 走贴纸通道', async () => {
  const t = rig()
  const jpg = t.mk('sfw/a.jpg')
  const webp = t.mk('sfw/b.webp')
  t.writeLib({ sfw: [{ label: 'Static 1', path: jpg }, { label: 'Sticker 1', path: webp }] })
  await t.svc.send('1', null, { action: 'send', query: 'Static 1' })
  await t.svc.send('1', null, { action: 'send', query: 'Sticker 1' })
  expect(t.calls).toEqual(['photo:a.jpg', 'sticker:b.webp'])
  t.done()
})

test('send：路径越界（库目录外的文件、或符号链接指出去）拒发，一个字节都不调 Telegram', async () => {
  const t = rig()
  const outside = join(t.dir, 'secret.gif')
  writeFileSync(outside, 'GIF89a')
  const escape = t.mk('sfw/escape.gif')
  if (process.platform !== 'win32') {
    rmSync(escape)
    symlinkSync(outside, escape)
  }
  t.writeLib({ sfw: [{ label: 'Outside 1', path: outside }, { label: 'Escape 1', path: escape }] })
  const a = await t.svc.send('1', null, { action: 'send', query: 'Outside 1' })
  expect(a.isError).toBe(true)
  expect(a.text).toContain('没有发出去')
  if (process.platform !== 'win32') {
    const b = await t.svc.send('1', null, { action: 'send', query: 'Escape 1' })
    expect(b.isError).toBe(true)
  }
  expect(t.calls).toEqual([])
  t.done()
})

test('send：Telegram 拒收如实回报（isError）；网络异常按可能已送达回报（不标 isError，账本算已送达）', async () => {
  const denied = rig({ api: { sendFile: async () => { throw new TgApiError('sendAnimation', 400, 'Bad Request: file is too big') } } })
  denied.mk('sfw/a.gif')
  denied.writeLib({ sfw: [{ label: 'Happy 1', path: join(denied.root, 'sfw/a.gif') }] })
  const r1 = await denied.svc.send('1', null, { action: 'send', query: 'Happy 1' })
  expect(r1.isError).toBe(true)
  expect(r1.text).toContain('没发出去')
  denied.done()

  const flaky = rig({ api: { sendFile: async () => { throw new TgNetworkError('sendAnimation', 'ECONNRESET', 'boom') } } })
  flaky.mk('sfw/a.gif')
  flaky.writeLib({ sfw: [{ label: 'Happy 1', path: join(flaky.root, 'sfw/a.gif') }] })
  const turn = flaky.ledger.startTurn({ chatId: '1', segmentId: 1, kind: 'message', inboundIds: [], attempt: 0 })
  const r2 = await flaky.svc.send('1', turn.id, { action: 'send', query: 'Happy 1' })
  expect(r2.isError).toBeFalsy()
  expect(r2.text).toContain('可能已经发出去')
  expect(flaky.ledger.deliveredInChain(turn.id)).toBe(1)
  flaky.done()
})

test('list：只给标签与 id 不给本地路径；找不到关键词时说明标签语言；库读不出来如实说', () => {
  const t = rig()
  t.writeLib({ sfw: [{ label: 'Happy 1', path: '/x/a.gif' }], nsfw: [{ label: '露奶勾引', category: 'Foreplay/Seduction (勾引/前戏)', path: '/x/b.gif' }] })
  const all = t.svc.list({ action: 'list' })
  expect(all.text).toContain('#sfw:1 Happy 1')
  expect(all.text).toContain('#nsfw:1 露奶勾引')
  expect(all.text).toContain('Foreplay/Seduction (勾引/前戏)')
  expect(all.text).not.toContain('/x/a.gif')
  const miss = t.svc.list({ action: 'list', query: 'zzz' })
  expect(miss.text).toContain('没有匹配')
  expect(miss.text).toContain('英文')
  expect(t.svc.list({ action: 'list', tier: 'nsfw' }).text).not.toContain('Happy 1')
  t.done()

  const broken = rig()
  expect(broken.svc.list({ action: 'list' }).isError).toBe(true)
  broken.done()
})

test('send：标签找不到时列候选、不硬发；要给 id 或 label', async () => {
  const t = rig()
  t.writeLib({ sfw: [{ label: 'Happy 1', path: '/x/a.gif' }, { label: 'Happy 2', path: '/x/b.gif' }] })
  const r = await t.svc.send('1', null, { action: 'send', label: 'happy' })
  expect(r.isError).toBe(true)
  expect(r.text).toContain('#sfw:1')
  expect(r.text).toContain('#sfw:2')
  const none = await t.svc.send('1', null, { action: 'send', label: 'zzz' })
  expect(none.isError).toBe(true)
  expect(none.text).toContain('没有确切的一条')
  const empty = await t.svc.send('1', null, { action: 'send' })
  expect(empty.isError).toBe(true)
  expect(t.calls).toEqual([])
  t.done()
})
