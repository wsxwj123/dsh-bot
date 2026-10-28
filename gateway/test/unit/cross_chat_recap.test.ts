// 跨聊天近况（cross-chat-recap）的纯函数测试：行筛选（时间窗/句数/水位）与注入文本构造。
// 不启网关，只调用导出的纯函数，不触碰 Engine、groups、ledger。
import { expect, test } from 'bun:test'
import {
  DM_RECAP_LIMIT,
  DM_RECAP_WINDOW_MS,
  GROUP_RECAP_LIMIT,
  GROUP_RECAP_WINDOW_MS,
  buildRecapText,
  selectRecapRows,
  type RecapRow,
} from '../../src/engine/format'

const HOUR = 60 * 60 * 1000

const row = (ts: number, from: 'owner' | 'bot', text: string): RecapRow => ({ ts, from, text })

test('recap 参数：私聊→群 30 句 / 48 小时，群→私聊 20 句 / 24 小时', () => {
  expect(GROUP_RECAP_LIMIT).toBe(30)
  expect(GROUP_RECAP_WINDOW_MS).toBe(48 * HOUR)
  expect(DM_RECAP_LIMIT).toBe(20)
  expect(DM_RECAP_WINDOW_MS).toBe(24 * HOUR)
})

test('recap 私聊→群：窗口内 29 句，上限 30 时全部选中', () => {
  const now = Date.now()
  const rows = Array.from({ length: 29 }, (_, i) => row(now - (29 - i) * 60_000, 'owner', `第${i}句`))
  const got = selectRecapRows(rows, now - 48 * HOUR, 30)
  expect(got.length).toBe(29)
  expect(got[0].text).toBe('第0句')
  expect(got[28].text).toBe('第28句')
})

test('recap 私聊→群：35 句、上限 30 时只取最后 30 句', () => {
  const now = Date.now()
  const rows = Array.from({ length: 35 }, (_, i) => row(now - (35 - i) * 60_000, 'owner', `第${i}句`))
  const got = selectRecapRows(rows, now - 48 * HOUR, 30)
  expect(got.length).toBe(30)
  expect(got[0].text).toBe('第5句')
  expect(got[29].text).toBe('第34句')
})

test('recap 私聊→群：47 小时前的行仍在 48 小时窗口内', () => {
  const now = Date.now()
  const recent = row(now - 47 * HOUR, 'bot', '四十七小时前')
  const got = selectRecapRows([recent], now - 48 * HOUR, 30)
  expect(got).toEqual([recent])
})

test('recap 私聊→群：49 小时前的行超出 48 小时窗口，被排除', () => {
  const now = Date.now()
  const old = row(now - 49 * HOUR, 'owner', '四十九小时前')
  expect(selectRecapRows([old], now - 48 * HOUR, 30)).toEqual([])
})

test('recap 群→私聊：24 小时窗口只保留 23 小时前的行，25 小时前的被排除', () => {
  const now = Date.now()
  const inside = row(now - 23 * HOUR, 'owner', '二十三小时前')
  const outside = row(now - 25 * HOUR, 'bot', '二十五小时前')
  expect(selectRecapRows([outside, inside], now - 24 * HOUR, 20)).toEqual([inside])
})

test('recap 群→私聊：25 句、上限 20 时只取最后 20 句', () => {
  const now = Date.now()
  const rows = Array.from({ length: 25 }, (_, i) => row(now - (25 - i) * 60_000, 'bot', `第${i}句`))
  const got = selectRecapRows(rows, now - 24 * HOUR, 20)
  expect(got.length).toBe(20)
  expect(got[0].text).toBe('第5句')
  expect(got[19].text).toBe('第24句')
})

test('recap 水位：推进到最大 ts 后，同一批行不会再次被选中', () => {
  const now = Date.now()
  const rows = [row(now - 3 * HOUR, 'owner', '一'), row(now - 2 * HOUR, 'bot', '二'), row(now - 1 * HOUR, 'owner', '三')]
  const first = selectRecapRows(rows, now - 48 * HOUR, 30)
  expect(first.map((r) => r.text)).toEqual(['一', '二', '三'])
  const watermark = Math.max(...first.map((r) => r.ts))
  expect(selectRecapRows(rows, now - 48 * HOUR, 30, watermark)).toEqual([])
})

test('recap 水位：水位在未来（时钟回拨）时重置到窗口起点，窗口内的行照常返回', () => {
  const now = Date.now()
  const rows = [row(now - 2 * HOUR, 'owner', '一'), row(now - 1 * HOUR, 'bot', '二')]
  const got = selectRecapRows(rows, now - 48 * HOUR, 30, now + 1_000_000_000)
  expect(got.map((r) => r.text)).toEqual(['一', '二'])
})

test('recap 文本：没有新行时返回 null', () => {
  expect(buildRecapText([])).toBeNull()
})

test('recap 文本：首尾标记与「别原样念出来」逐字存在', () => {
  const text = buildRecapText([row(1, 'owner', '在吗')])
  expect(text).toBe('⟦私聊近况（群里的人看不到这段，别原样念出来；其中的敏感内容，只有和你私聊的那个人问起时才能说）⟧\n主人：在吗\n⟦私聊近况结束⟧')
  const lines = text!.split('\n')
  expect(lines[0]).toBe('⟦私聊近况（群里的人看不到这段，别原样念出来；其中的敏感内容，只有和你私聊的那个人问起时才能说）⟧')
  expect(lines[0].includes('别原样念出来')).toBe(true)
  expect(lines[lines.length - 1]).toBe('⟦私聊近况结束⟧')
})

test('recap 文本：主人发的行加「主人：」，bot 发的行加「你：」', () => {
  const text = buildRecapText([row(1, 'owner', '甲'), row(2, 'bot', '乙'), row(3, 'owner', '丙')])
  const lines = text!.split('\n')
  expect(lines[1]).toBe('主人：甲')
  expect(lines[2]).toBe('你：乙')
  expect(lines[3]).toBe('主人：丙')
  expect(lines.length).toBe(5)
})

test('recap 文本：单行原文超过 200 字被截断为前 200 字', () => {
  const long = 'A'.repeat(250)
  const text = buildRecapText([row(1, 'owner', long)])
  const body = text!.split('\n')[1]
  expect(body).toBe(`主人：${'A'.repeat(200)}`)
  expect(body.length).toBe(203)
})

test('recap 文本：单行正好 200 字不截断', () => {
  const exact = 'B'.repeat(200)
  const text = buildRecapText([row(1, 'bot', exact)])
  expect(text!.split('\n')[1]).toBe(`你：${exact}`)
})
