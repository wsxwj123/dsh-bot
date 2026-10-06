// 承诺时间的中文说法解析（M3 验收 1：50 条以上）和许诺识别（验收 2）
import { expect, test } from 'bun:test'
import { detectPromises, localParts, parseWhen, zoned, type WhenCtx } from '../../src/commit/when'

const TZ = 'Asia/Shanghai'
const NOW = Date.parse('2026-10-06T14:00:00+08:00') // 周二 14:00
// 假作息：每天 7:30 起床；今天 17:00 下班
const ctx: WhenCtx = {
  now: NOW,
  timeZone: TZ,
  nextWake: after => { const l = localParts(after, TZ); let t = zoned(l.y, l.mo, l.d, 7, 30, TZ); if (t <= after) t = zoned(l.y, l.mo, l.d + 1, 7, 30, TZ); return t },
  nextFree: after => { const t = Date.parse('2026-10-06T17:00:00+08:00'); return after < t ? t : null },
}
const show = (ms: number | undefined) => {
  if (ms === undefined) return null
  const l = localParts(ms, TZ)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(l.mo)}-${p(l.d)} ${p(l.h)}:${p(l.mi)}`
}

const CASES: [string, string | null][] = [
  ['三点提醒你', '10-06 15:00'],
  ['下午三点', '10-06 15:00'],
  ['下午三点半', '10-06 15:30'],
  ['3点15', '10-06 15:15'],
  ['三点一刻', '10-06 15:15'],
  ['三点三刻', '10-06 15:45'],
  ['15:30', '10-06 15:30'],
  ['晚上八点', '10-06 20:00'],
  ['八点', '10-06 20:00'],
  ['十点', '10-06 22:00'],
  ['7:05', '10-06 19:05'],
  ['十二点', '10-07 00:00'],
  ['中午十二点', '10-07 12:00'],
  ['今天三点', '10-06 15:00'],
  ['今天早上八点', '10-06 08:00'],
  ['今晚', '10-06 20:00'],
  ['今晚十一点', '10-06 23:00'],
  ['晚上十二点', '10-07 00:00'],
  ['半夜十二点', '10-07 00:00'],
  ['凌晨两点', '10-07 02:00'],
  ['傍晚', '10-06 18:00'],
  ['下午', '10-06 15:00'],
  ['中午', '10-07 12:00'],
  ['早上', '10-07 08:00'],
  ['明天下午三点', '10-07 15:00'],
  ['明天三点', '10-07 15:00'],
  ['明天九点', '10-07 09:00'],
  ['明天晚上九点半', '10-07 21:30'],
  ['明早', '10-07 07:30'],
  ['明天早上', '10-07 07:30'],
  ['明早八点', '10-07 08:00'],
  ['明晚', '10-07 20:00'],
  ['明晚九点半', '10-07 21:30'],
  ['后天晚上', '10-08 20:00'],
  ['大后天上午', '10-09 10:00'],
  ['半小时后', '10-06 14:30'],
  ['30分钟后', '10-06 14:30'],
  ['十分钟之后', '10-06 14:10'],
  ['一刻钟后', '10-06 14:15'],
  ['过十分钟', '10-06 14:10'],
  ['一个小时后', '10-06 15:00'],
  ['两个小时后', '10-06 16:00'],
  ['一个半小时后', '10-06 15:30'],
  ['3天后', '10-09 14:00'],
  ['周五晚上', '10-09 20:00'],
  ['周五下午两点', '10-09 14:00'],
  ['星期四早上九点', '10-08 09:00'],
  ['礼拜六上午十点', '10-10 10:00'],
  ['周日中午', '10-11 12:00'],
  ['下周一早上九点', '10-12 09:00'],
  ['下周三晚上', '10-14 20:00'],
  ['20号晚上', '10-20 20:00'],
  ['2号上午', '11-02 10:00'],
  ['一睁眼', '10-07 07:30'],
  ['起床后', '10-07 07:30'],
  ['睡醒了', '10-07 07:30'],
  ['下班后', '10-06 17:00'],
  ['放学后', '10-06 17:00'],
  ['2026-10-07T09:00:00+08:00', '10-07 09:00'],
  ['2026-10-07 09:00', '10-07 09:00'],
  ['待会儿', null],
  ['晚点', null],
  ['明天', null],
  ['星期五', null],
  ['改天吧', null],
]

test(`中文时间说法：${CASES.length} 条`, () => {
  expect(CASES.length).toBeGreaterThanOrEqual(50)
  const wrong = CASES.map(([t, want]) => [t, want, show(parseWhen(t, ctx)?.at)] as const).filter(([, want, got]) => want !== got)
  expect(wrong).toEqual([])
})

test('作息查不到时，"起床后""下班后"换算不了', () => {
  const bare: WhenCtx = { now: NOW, timeZone: TZ }
  expect(parseWhen('起床后', bare)).toBeNull()
  expect(parseWhen('下班后', bare)).toBeNull()
  expect(show(parseWhen('明天早上', bare)?.at)).toBe('10-07 08:00')
})

test('许诺识别：正例', () => {
  const one = (s: string) => { const r = detectPromises(s, ctx); expect(r.length).toBe(1); return r[0]! }
  expect(show(one('好，我三点提醒你。').when?.at)).toBe('10-06 15:00')
  expect(show(one('明早叫你起床～').when?.at)).toBe('10-07 07:30')
  expect(show(one('半小时后我打给你').when?.at)).toBe('10-06 14:30')
  expect(show(one('下班后我去接你').when?.at)).toBe('10-06 17:00')
  expect(show(one('周五晚上我陪你看电影！').when?.at)).toBe('10-09 20:00')
  // 时间说得含糊：识别出来，但换算不了（下一轮提醒模型自己登记）
  for (const s of ['晚点告诉你', '待会儿发给你', '回头找你', '明天告诉你结果']) expect(one(s).when).toBeNull()
  // 多句话里只挑出许诺的那一句
  const r = detectPromises('今天好累啊。晚上八点我给你打电话。先去洗澡了', ctx)
  expect(r.map(x => x.sentence)).toEqual(['晚上八点我给你打电话'])
})

test('许诺识别：反例', () => {
  for (const s of [
    '要不要我三点提醒你？',
    '我三点提醒你好不好',
    '你三点记得提醒我',
    '我不会提醒你的',
    '昨天我告诉你了',
    '我爱你',
    '提醒你一下，记得吃饭',
    '你明天几点下班',
  ]) expect(detectPromises(s, ctx)).toEqual([])
})
