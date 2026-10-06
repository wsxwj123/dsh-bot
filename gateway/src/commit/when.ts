// 承诺的时间：把"下午三点""明早""一睁眼""半小时后""周五晚上"这类说法换算成具体时刻；
// 再从 bot 实际发出的话里找"许诺未来"的句子（兜底登记用）。纯函数，时钟和作息都由调用方传入。

export type WhenCtx = {
  now: number
  timeZone: string
  /** 作息表：afterMs 之后第一次起床的时刻；查不到返回 null */
  nextWake?: (afterMs: number) => number | null
  /** 作息表：afterMs 时手上的事（上课、上班）做完的时刻；没在忙或查不到返回 null */
  nextFree?: (afterMs: number) => number | null
}

export type WhenResult = { at: number; how: 'iso' | 'relative' | 'clock' | 'wake' | 'free' }

const MIN = 60_000
const HOUR = 60 * MIN
const DAY = 24 * HOUR

// ─── 时区里的年月日时分 ───

type Local = { y: number; mo: number; d: number; h: number; mi: number; wd: number }
const WD: Record<string, number> = { Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6, Sun: 7 }

export function localParts(ms: number, tz: string): Local {
  const f = new Intl.DateTimeFormat('en-US', { timeZone: tz, hourCycle: 'h23', year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: 'numeric', weekday: 'short' })
  const p: Record<string, string> = {}
  for (const x of f.formatToParts(new Date(ms))) p[x.type] = x.value
  return { y: +p.year!, mo: +p.month!, d: +p.day!, h: +p.hour! % 24, mi: +p.minute!, wd: WD[p.weekday!] ?? 1 }
}

/** 某时区的墙上时间 → 毫秒时间戳（日、时可以越界，会自动进位） */
export function zoned(y: number, mo: number, d: number, h: number, mi: number, tz: string): number {
  const guess = Date.UTC(y, mo - 1, d, h, mi)
  const off = (ms: number) => { const l = localParts(ms, tz); return Date.UTC(l.y, l.mo - 1, l.d, l.h, l.mi) - Math.floor(ms / MIN) * MIN }
  let t = guess - off(guess)
  t = guess - off(t)
  return t
}

// ─── 数字 ───

const DIGIT: Record<string, number> = { 零: 0, 〇: 0, 一: 1, 二: 2, 两: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9 }
const NUM = '[0-9０-９零〇一二两三四五六七八九十]+'

export function cnNum(s: string): number | null {
  const t = s.replace(/[０-９]/g, c => String.fromCharCode(c.charCodeAt(0) - 0xfee0))
  if (/^\d+$/.test(t)) return Number(t)
  if (!/^[零〇一二两三四五六七八九十]+$/.test(t)) return null
  if (!t.includes('十')) return t.length === 1 ? DIGIT[t]! : Number([...t].map(c => DIGIT[c]).join(''))
  const [a, b] = t.split('十')
  const tens = a ? DIGIT[a] : 1
  const ones = b ? DIGIT[b] : 0
  if (tens === undefined || ones === undefined || (b && b.length > 1) || (a && a.length > 1)) return null
  return tens * 10 + ones
}

// ─── 说法 ───

const PERIOD_HOUR: Record<string, number> = { 凌晨: 5, 早上: 8, 早晨: 8, 清早: 7, 一早: 8, 上午: 10, 中午: 12, 下午: 15, 傍晚: 18, 晚上: 20, 夜里: 22, 半夜: 0, 深夜: 23 }
const PERIOD_RE = '凌晨|早上|早晨|清早|一早|上午|中午|下午|傍晚|晚上|夜里|半夜|深夜'
const WEEKDAY: Record<string, number> = { 一: 1, 二: 2, 三: 3, 四: 4, 五: 5, 六: 6, 日: 7, 天: 7, 末: 6 }

/** 太含糊、换算不成具体时刻，但确实指将来的说法（兜底时提醒模型自己登记） */
export const VAGUE_RE = /等会儿?|待会儿?|一会儿?|过会儿?|晚点|晚些|回头|改天|过几天|过两天|有空|下次|以后|稍后|明天|后天|周末|这周|下周/

export function hasVagueTime(text: string): boolean {
  return VAGUE_RE.test(text)
}

function iso(text: string, ctx: WhenCtx): number | null {
  const m = text.match(/(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?/)
  if (!m) return null
  if (m[6]) {
    const t = Date.parse(m[0].replace(' ', 'T'))
    return Number.isFinite(t) ? t : null
  }
  return zoned(+m[1]!, +m[2]!, +m[3]!, +m[4]!, +m[5]!, ctx.timeZone)
}

function relative(text: string, now: number): number | null {
  if (/一刻钟(?:之?后|以后)/.test(text)) return now + 15 * MIN
  const m = text.match(new RegExp(`(?:过)?(${NUM})(?:个)?(半)?(?:个)?(小时|钟头|分钟|分|天)(?:之?后|以后|再)`)) ?? text.match(new RegExp(`过(${NUM})(?:个)?(半)?(?:个)?(小时|钟头|分钟)`))
  if (!m) return /半(?:个)?(?:小时|钟头)(?:之?后|以后)/.test(text) ? now + 30 * MIN : null
  const n = cnNum(m[1]!)
  if (n === null) return null
  const unit = m[3] === '天' ? DAY : m[3]!.startsWith('分') ? MIN : HOUR
  return now + (n + (m[2] ? 0.5 : 0)) * unit
}

type DayRef = { offset?: number; weekday?: number; nextWeek?: boolean; dom?: number; period?: string }

function dayRef(text: string): DayRef | null {
  if (/大后天/.test(text)) return { offset: 3 }
  if (/后天/.test(text)) return { offset: 2 }
  if (/明早|明儿?早/.test(text)) return { offset: 1, period: '早上' }
  if (/明晚|明儿?晚/.test(text)) return { offset: 1, period: '晚上' }
  if (/明天|明儿|明日/.test(text)) return { offset: 1 }
  if (/今晚|今儿?晚/.test(text)) return { offset: 0, period: '晚上' }
  if (/今早|今儿?早/.test(text)) return { offset: 0, period: '早上' }
  if (/今天|今儿|今日/.test(text)) return { offset: 0 }
  const w = text.match(/(下个?|这个?|本)?(?:周|星期|礼拜)([一二三四五六日天末])/)
  if (w) return { weekday: WEEKDAY[w[2]!], nextWeek: !!w[1]?.startsWith('下') }
  const dom = text.match(new RegExp(`(${NUM})[号日]`))
  if (dom) { const n = cnNum(dom[1]!); if (n && n >= 1 && n <= 31) return { dom: n } }
  return null
}

function clockOf(text: string): { h: number; mi: number } | null {
  const hm = text.match(/(\d{1,2})[:：](\d{2})/)
  if (hm) return { h: +hm[1]!, mi: +hm[2]! }
  const m = text.match(new RegExp(`(${NUM})\\s*(?:点|時|时)(?:钟)?(?:(半)|(一刻)|(三刻)|(${NUM})(?:分)?)?`))
  if (!m) return null
  const h = cnNum(m[1]!)
  if (h === null || h > 24) return null
  let mi = 0
  if (m[2]) mi = 30
  else if (m[3]) mi = 15
  else if (m[4]) mi = 45
  else if (m[5]) { const n = cnNum(m[5]); if (n !== null && n < 60) mi = n }
  return { h, mi }
}

/** 时段 + 钟点 → 24 小时制 */
function applyPeriod(h: number, period: string | undefined): number {
  if (!period) return h
  if (/下午|傍晚|晚上|夜里|深夜/.test(period)) return h < 12 ? h + 12 : h === 12 ? 24 : h
  if (period === '中午') return h <= 3 ? h + 12 : h
  if (/凌晨|半夜/.test(period)) return h === 12 ? 0 : h
  return h === 12 && /早|上午/.test(period) ? 12 : h
}

function wakeOrDefault(dayStart: number, ctx: WhenCtx): number | null {
  const w = ctx.nextWake?.(dayStart)
  return w && w - dayStart < DAY ? w : null
}

export function parseWhen(text: string, ctx: WhenCtx): WhenResult | null {
  const t = text.replace(/\s+/g, '')
  const isoAt = iso(text, ctx)
  if (isoAt !== null) return { at: isoAt, how: 'iso' }
  const rel = relative(t, ctx.now)
  if (rel !== null) return { at: rel, how: 'relative' }
  if (/起床后|起床了|起来后|起来了|醒了|醒来|睡醒|一睁眼|睁眼/.test(t)) {
    const w = ctx.nextWake?.(ctx.now)
    return w ? { at: w, how: 'wake' } : null
  }
  if (/下班后|下班了|放学后|放学了|下课后|下课了|忙完/.test(t)) {
    const f = ctx.nextFree?.(ctx.now)
    return f ? { at: f, how: 'free' } : null
  }

  const day = dayRef(t)
  const pm = t.match(new RegExp(PERIOD_RE))
  const period = pm?.[0] ?? day?.period
  const clock = clockOf(t)
  if (!day && !period && !clock) return null

  const now = localParts(ctx.now, ctx.timeZone)
  // 这一天是哪天
  let base: { y: number; mo: number; d: number } | null = null
  if (day?.offset !== undefined) base = { y: now.y, mo: now.mo, d: now.d + day.offset }
  else if (day?.weekday) {
    // 下周X = 下周一 + (X-1)；周X = 这周还没过就是这周的，过了就是下一个
    const delta = day.nextWeek ? (8 - now.wd) + (day.weekday - 1) : (day.weekday - now.wd + 7) % 7
    base = { y: now.y, mo: now.mo, d: now.d + delta }
  } else if (day?.dom) {
    base = { y: now.y, mo: now.mo + (day.dom < now.d ? 1 : 0), d: day.dom }
  }

  if (clock) {
    let h = clock.h
    const explicit = !!period || clock.h > 12 || clock.h === 0
    h = applyPeriod(h, period)
    if (base) {
      // 明天三点：没说上午下午时，1–6 点按下午算，7–11 点按上午算
      if (!explicit && h >= 1 && h <= 6) h += 12
      return { at: zoned(base.y, base.mo, base.d, h, clock.mi, ctx.timeZone), how: 'clock' }
    }
    // 没说哪天：取将来最近的一个（1–6 点没说上下午时只考虑下午）
    const cands: number[] = []
    const hours = explicit ? [h] : h >= 1 && h <= 6 ? [h + 12] : [h, h + 12 === 24 ? 24 : h + 12]
    for (const add of [0, 1]) for (const hh of hours) cands.push(zoned(now.y, now.mo, now.d + add, hh, clock.mi, ctx.timeZone))
    const fut = cands.filter(c => c > ctx.now).sort((a, b) => a - b)
    return fut.length ? { at: fut[0]!, how: 'clock' } : null
  }

  if (period) {
    const d0 = base ?? { y: now.y, mo: now.mo, d: now.d }
    // 早上没说几点：有作息表就用那天起床的时刻
    if (/早上|早晨|清早|一早/.test(period)) {
      const w = wakeOrDefault(zoned(d0.y, d0.mo, d0.d, 0, 0, ctx.timeZone), ctx)
      if (w && w > ctx.now) return { at: w, how: 'wake' }
    }
    let at = zoned(d0.y, d0.mo, d0.d, PERIOD_HOUR[period] ?? 12, 0, ctx.timeZone)
    if (!base && at <= ctx.now) at += DAY
    return { at, how: 'clock' }
  }
  return null // 只有"明天""周五"，没有时段和钟点：太含糊
}

// ─── 兜底：从 bot 发出的话里找许诺 ───

/** 对对方许诺要做的事（宾语是"你"的动作） */
const PROMISE_VERB = /提醒你|叫你|喊你|叫醒你|喊醒你|告诉你|跟你说|和你说|给你说|讲给你|发给你|发你|给你发|拍给你|找你|陪你|打给你|给你打|回你|回复你|带给你|给你带|做给你|通知你|问你|接你|等你/
const NEGATION = /不(?:会|用|要|能)|别|没法|没空/

export type Promise_ = { sentence: string; when: WhenResult | null }

export function detectPromises(text: string, ctx: WhenCtx): Promise_[] {
  const out: Promise_[] = []
  for (const raw of text.split(/(?<=[。！？!?；;\n～~…])/)) {
    const s = raw.trim()
    if (!s || !PROMISE_VERB.test(s)) continue
    if (/[？?吗呢]\s*$/.test(s) || /要不要|好不好|行不行|可以吗/.test(s)) continue // 问对方的，不算许诺
    const verbAt = s.search(PROMISE_VERB)
    if (NEGATION.test(s.slice(Math.max(0, verbAt - 4), verbAt))) continue
    const when = parseWhen(s, ctx)
    if (!when && !hasVagueTime(s)) continue
    out.push({ sentence: s.replace(/[。！？!?；;～~…\s]+$/, ''), when })
  }
  return out
}
