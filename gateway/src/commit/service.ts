// 承诺：登记（工具 + 兜底识别）、到点触发、兑现判定。方案 3.8。
//   到点时 bot 在睡觉 → 顺延到起床，提示里写明迟了多久；
//   到点那一轮有真正送达的消息才算兑现；没开口就退避重试，最多 3 次，然后标记失败并通知主人。
//   每天重复的（接口补充「承诺重复」）：这一次做完或彻底失败后不关闭，排到下一天同一时间；停机错过好几天只补最近一次。
import type { CommitmentRow, Ledger } from '../ledger'
import type { Logger } from '../log'
import { escapeUserText } from '../engine/format'
import { isAsleep, type SituationBridge } from '../life/situation'
import { DAILY_RE, defaultWhen, detectPromises, lastDaily, localParts, nextDaily, parseDaily, parseWhen, type WhenCtx } from './when'

const MIN = 60_000
const MAX_ATTEMPTS = 3

export type CommitDeps = {
  ledger: Ledger
  log: Logger
  timeZone: () => string
  situation: SituationBridge
  /** 写一条合成消息进账本并排上这个聊天；返回账本 id（重复投递返回 null） */
  inject: (chatId: string, text: string, key: string, meta: Record<string, unknown>) => number | null
  isBusy: (chatId: string) => boolean
  notifyOwner: (key: string, text: string) => void
  /** 第 1、2 次没兑现后，等多久再提醒 */
  retryMs: number[]
  /** 生活作息开着没有（约定 v1.1 5.19）：关着时到点就提醒，不按作息顺延；不给按开着 */
  lifeOn?: () => boolean
}

export function fmtTime(ms: number, tz: string, now = Date.now()): string {
  const l = localParts(ms, tz)
  const n = localParts(now, tz)
  const p = (x: number) => String(x).padStart(2, '0')
  const same = l.y === n.y && l.mo === n.mo && l.d === n.d
  return `${same ? '今天' : `${l.mo}月${l.d}日`} ${p(l.h)}:${p(l.mi)}`
}

function humanMin(m: number): string {
  return m < 60 ? `${m} 分钟` : m % 60 === 0 || m >= 180 ? `${Math.round(m / 60)} 个小时` : `${Math.floor(m / 60)} 小时 ${m % 60} 分钟`
}

const pad2 = (x: number) => String(x).padStart(2, '0')
const hhmm = (h: number, mi: number) => `${pad2(h)}:${pad2(mi)}`

/** 每天重复的承诺：取出一天里的时间点；一次性的（repeat_rule 为空）返回 null */
function dailyOf(c: CommitmentRow): { h: number; mi: number } | null {
  const m = c.repeat_rule?.match(/^daily (\d{2}):(\d{2})$/)
  return m ? { h: Number(m[1]), mi: Number(m[2]) } : null
}

/** 两句话像不像：按相邻两个字的组合算重合比例（复述旧承诺时措辞往往差不多） */
export function similar(a: string, b: string): number {
  const grams = (s: string) => { const t = s.replace(/[\s，。！？!?,.~～…]/g, ''); const g = new Set<string>(); for (let i = 0; i < t.length - 1; i++) g.add(t.slice(i, i + 2)); return g }
  const x = grams(a)
  const y = grams(b)
  if (x.size === 0 || y.size === 0) return 0
  let n = 0
  for (const g of x) if (y.has(g)) n++
  return n / Math.min(x.size, y.size)
}

export class Commitments {
  /** 每个聊天最近一次 commitment_list 的时刻：查过列表的那一轮，bot 复述旧承诺很正常，不做兜底识别 */
  private listedAt = new Map<string, number>()

  constructor(private readonly d: CommitDeps) {}

  whenCtx(now = Date.now()): WhenCtx {
    const s = this.d.situation
    return { now, timeZone: this.d.timeZone(), nextWake: a => s.nextWake(a), nextFree: a => s.nextFree(a) }
  }

  // ─── 工具 ───

  create(chatId: string, args: Record<string, unknown>): { text: string; isError?: boolean } {
    const content = str(args.content).slice(0, 300)
    const when = str(args.when).slice(0, 100)
    const quote = str(args.quote).slice(0, 300) || null
    if (!content || !when) return { text: 'content 和 when 都要填。', isError: true }
    const repeat = str(args.repeat).toLowerCase()
    if (repeat && repeat !== 'daily') return { text: 'repeat 目前只支持 daily（每天）；一次性的事不用填 repeat。', isError: true }
    const now = Date.now()
    // 写了 repeat=daily，或者 when 以"每天""每日""天天"开头：每天重复
    if (repeat === 'daily' || when.search(DAILY_RE) === 0) return this.createDaily(chatId, content, when, quote, now)
    const r = parseWhen(when, this.whenCtx(now))
    if (!r) return { text: `时间看不懂：「${when}」。请写具体一点，比如"今晚八点""明天早上""30分钟后""起床后"，或者 2026-10-07T09:00 这样的时间。`, isError: true }
    if (r.at < now - MIN) return { text: `这个时间已经过了（${fmtTime(r.at, this.d.timeZone(), now)}），请写一个将来的时间。`, isError: true }
    const c = this.d.ledger.addCommitment({ chatId, text: content, quote, whenText: when, dueAt: Math.max(r.at, now), source: 'tool' })
    this.d.log.info('commitment.created', { chat: chatId, id: c.id, source: 'tool', how: r.how, due_in_min: Math.round((c.due_at - now) / MIN) })
    return { text: `已登记 #${c.id}：${fmtTime(c.due_at, this.d.timeZone(), now)}，${content}。到时候程序会提醒你。` }
  }

  private createDaily(chatId: string, content: string, when: string, quote: string | null, now: number): { text: string; isError?: boolean } {
    const d = parseDaily(when, this.whenCtx(now))
    // 只说"每天早上"没说几点：太含糊，不登记
    if (!d) return { text: `每天的事要写几点：「${when}」没说具体时间，没有登记。先问清对方每天几点，再用 commitment_create 登记（repeat 写 daily，when 写"早上七点"或 07:30 这样的时间）。`, isError: true }
    const c = this.d.ledger.addCommitment({ chatId, text: content, quote, whenText: when, dueAt: d.at, source: 'tool', repeatRule: `daily ${hhmm(d.h, d.mi)}` })
    this.d.log.info('commitment.created', { chat: chatId, id: c.id, source: 'tool', how: 'daily', due_in_min: Math.round((c.due_at - now) / MIN) })
    return { text: `已登记 #${c.id}：每天 ${hhmm(d.h, d.mi)}，${content}（第一次是 ${fmtTime(c.due_at, this.d.timeZone(), now)}）。每天到点程序都会提醒你；以后不用做了就用 commitment_cancel 取消。` }
  }

  list(chatId: string): { text: string } {
    this.listedAt.set(chatId, Date.now())
    const rows = this.d.ledger.openCommitments(chatId)
    if (rows.length === 0) return { text: '现在没有还没兑现的承诺。' }
    const tz = this.d.timeZone()
    return { text: rows.map(c => {
      const d = dailyOf(c)
      const when = d ? `每天 ${hhmm(d.h, d.mi)}（下一次 ${fmtTime(c.due_at, tz)}）` : fmtTime(c.due_at, tz)
      return `#${c.id} ${when} ${c.text}${c.state === 'firing' ? '（正在提醒）' : c.note === 'deferred_sleep' ? '（你在睡觉，已顺延到起床后）' : ''}`
    }).join('\n') }
  }

  cancel(chatId: string, args: Record<string, unknown>): { text: string; isError?: boolean } {
    const id = Number(String(args.id ?? '').replace(/^#/, ''))
    const c = Number.isInteger(id) ? this.d.ledger.commitment(id) : null
    if (!c || c.chat_id !== chatId) return { text: `没有 #${args.id} 这个承诺。用 commitment_list 看看有哪些。`, isError: true }
    if (!['pending', 'firing'].includes(c.state)) return { text: `#${c.id} 已经结束了（${c.state}），不用取消。` }
    this.d.ledger.updateCommitment(c.id, { state: 'cancelled', note: str(args.reason).slice(0, 200) || 'cancelled' })
    this.d.log.info('commitment.cancelled', { chat: chatId, id: c.id })
    return { text: `已取消 #${c.id}${dailyOf(c) ? '（每天的这件事，以后都不再提醒）' : ''}。` }
  }

  /** commitment_done（用户 2026-10-16 定）：提前做到了或刚做完。一次性的标成完成；每天重复的只算这一次，排到下一天 */
  done(chatId: string, args: Record<string, unknown>): { text: string; isError?: boolean } {
    const id = Number(String(args.id ?? '').replace(/^#/, ''))
    const c = Number.isInteger(id) ? this.d.ledger.commitment(id) : null
    if (!c || c.chat_id !== chatId) return { text: `没有 #${args.id} 这个承诺。用 commitment_list 看看有哪些。`, isError: true }
    if (!['pending', 'firing'].includes(c.state)) return { text: `#${c.id} 已经结束了（${c.state}），不用再标。` }
    const now = Date.now()
    if (this.nextDay(c, 'done', now)) {
      const next = this.d.ledger.commitment(c.id)!
      this.d.log.info('commitment.done', { chat: chatId, id: c.id, daily: true })
      return { text: `已记下 #${c.id} 这一次做到了，下一次是 ${fmtTime(next.due_at, this.d.timeZone(), now)}。` }
    }
    this.d.ledger.updateCommitment(c.id, { state: 'done', note: str(args.note).slice(0, 200) || 'done' })
    this.d.log.info('commitment.done', { chat: chatId, id: c.id, daily: false })
    return { text: `已标成完成 #${c.id}。` }
  }

  // ─── 兜底：这一轮 bot 说了"三点提醒你"却没登记 ───

  /** 一轮结束后调用。返回要在下一轮告诉模型的话（没有就是空数组）。dueTurn：这一轮是承诺到期的提醒（bot 会复述那件事）。 */
  afterTurn(chatId: string, since: number, dueTurn = false): string[] {
    const L = this.d.ledger
    if (dueTurn || (this.listedAt.get(chatId) ?? 0) >= since) return []
    if (L.commitmentsCreatedSince(chatId, since, 'tool') > 0) return []
    const texts = L.sentTextsSince(chatId, since)
    if (texts.length === 0) return []
    return this.register(chatId, texts.join('\n'), 'auto', '你刚才说了')
  }

  /** 电话里 bot 说过的话（voicecall 挂断后投来）：同样找许诺登记成承诺 */
  fromCall(chatId: string, lines: string[]): string[] {
    return this.register(chatId, lines.join('\n'), 'call', '电话里你说了')
  }

  private register(chatId: string, text: string, source: 'auto' | 'call', said: string): string[] {
    const L = this.d.ledger
    const now = Date.now()
    const hints: string[] = []
    const open = L.openCommitments(chatId)
    for (const p of detectPromises(text, this.whenCtx(now))) {
      let dft: ReturnType<typeof defaultWhen> = null
      if (L.hasCommitmentQuote(chatId, p.sentence, now - 24 * 60 * MIN)) continue
      // 复述还没兑现的承诺（真机：问"你答应过我什么"时 bot 把"1 分钟后提醒你"又说了一遍）：时间接近或说法相近就不再登记
      const dup = open.find(c => (p.when && Math.abs(c.due_at - p.when.at) <= 30 * MIN) || similar(c.quote ?? c.text, p.sentence) >= 0.6 || similar(c.text, p.sentence) >= 0.6)
      if (dup) { this.d.log.info('commitment.restated', { chat: chatId, id: dup.id }); continue }
      // 答应的是每天都要做的事：不替她登记，下一轮提醒她问清几点、按每天登记
      const daily = DAILY_RE.test(p.sentence)
      if (p.when && p.when.at > now && !daily) {
        const c = L.addCommitment({ chatId, text: p.sentence, quote: p.sentence, dueAt: p.when.at, source })
        this.d.log.info('commitment.created', { chat: chatId, id: c.id, source, how: p.when.how, due_in_min: Math.round((c.due_at - now) / MIN) })
        hints.push(`⟦系统·承诺⟧ ${said}「${escapeUserText(p.sentence)}」，程序已替你登记成承诺 #${c.id}（${fmtTime(c.due_at, this.d.timeZone(), now)}）。如果这不是承诺，用 commitment_cancel 取消。`)
      } else if (!p.when && !daily && (dft = defaultWhen(p.sentence, this.whenCtx(now))) && dft.at > now) {
        // 时间含糊但能定（等会儿、晚点、明天、后天、周末）：先按缺省时间替她登记，告诉她按哪个词定的、不对怎么改
        const c = L.addCommitment({ chatId, text: p.sentence, quote: p.sentence, dueAt: dft.at, source })
        L.updateCommitment(c.id, { note: `default: ${dft.word}` })
        this.d.log.info('commitment.created', { chat: chatId, id: c.id, source, how: 'default', due_in_min: Math.round((c.due_at - now) / MIN) })
        hints.push(`⟦系统·承诺⟧ ${said}「${escapeUserText(p.sentence)}」，没说几点，程序按「${dft.word}」先替你登记成承诺 #${c.id}（${fmtTime(c.due_at, this.d.timeZone(), now)}）。时间不对就用 commitment_cancel 取消，再用 commitment_create 按对的时间登记；如果这不是承诺，直接取消。`)
      } else {
        // 时间说得含糊，换算不了：记一条空的占位，免得每轮都提醒同一句
        const v = L.addCommitment({ chatId, text: p.sentence, quote: p.sentence, dueAt: now, source })
        L.updateCommitment(v.id, { state: 'cancelled', note: 'vague: hinted' })
        this.d.log.info('commitment.vague', { chat: chatId })
        hints.push(daily
          ? `⟦系统·承诺⟧ ${said}「${escapeUserText(p.sentence)}」，像是答应了对方每天都要做的事。如果是，用 commitment_create 登记（repeat 写 daily，when 写每天几点；还没说定几点就先问清对方）；不是就不用管。`
          : `⟦系统·承诺⟧ ${said}「${escapeUserText(p.sentence)}」，像是答应了对方什么。如果是，用 commitment_create 登记一个具体时间；不是就不用管。`)
      }
    }
    return hints
  }

  // ─── 到点 ───

  /** 调度器每 30 秒调一次 */
  tick(now = Date.now()): void {
    const L = this.d.ledger
    // 先结算正在提醒的：那一轮结束了（处理完、作废），看有没有真正送达
    for (const c of L.commitmentsInState('firing')) {
      const row = c.inbound_id ? L.inbound(c.inbound_id) : null
      if (row && (row.state === 'pending' || row.state === 'in_turn')) continue
      this.settle(c, c.inbound_id ? L.deliveredForInbound(c.inbound_id) > 0 : false, now)
    }
    const due = L.commitmentsInState('pending', now)
    if (due.length === 0) return
    const sit = this.d.situation.current(now)
    for (const pending of due) {
      const c = this.catchUp(pending, now)
      if (this.d.isBusy(c.chat_id)) continue // 这个聊天正在回话，下一轮再提醒
      if (this.d.lifeOn?.() !== false && isAsleep(sit)) {
        const wake = this.d.situation.nextWake(now)
        if (wake && wake > now) {
          L.updateCommitment(c.id, { next_at: wake, note: 'deferred_sleep' })
          this.d.log.info('commitment.deferred', { chat: c.chat_id, id: c.id, reason: 'sleeping', until_min: Math.round((wake - now) / MIN) })
          continue
        }
      }
      // 每天重复的同一个承诺每天都要投递一次：key 带上这一次的时间，免得第二天被当成重复投递
      const key = dailyOf(c) ? `commit:${c.id}@${c.due_at}:${c.attempts}` : `commit:${c.id}:${c.attempts}`
      const id = this.d.inject(c.chat_id, this.duePrompt(c, now), key, { source: 'commitment', commitment_id: c.id })
      L.updateCommitment(c.id, { state: 'firing', inbound_id: id ?? c.inbound_id })
      this.d.log.info('commitment.fire', { chat: c.chat_id, id: c.id, attempt: c.attempts + 1, late_min: Math.max(0, Math.round((now - c.due_at) / MIN)) })
    }
  }

  private duePrompt(c: CommitmentRow, now: number): string {
    const tz = this.d.timeZone()
    const late = Math.round((now - c.due_at) / MIN)
    const quote = c.quote && c.quote !== c.text ? `（原话：「${escapeUserText(c.quote)}」）` : ''
    const lateText = late >= 10
      ? `现在已经晚了 ${humanMin(late)}${c.note === 'deferred_sleep' ? '（那会儿你在睡觉）' : ''}，回的时候自然地说一下。`
      : '现在到时间了。'
    const retry = c.attempts > 0 ? `这是第 ${c.attempts + 1} 次提醒，上次没有发出消息。` : ''
    return `⟦系统·承诺到期⟧ 你答应过对方：${escapeUserText(c.text)}${quote}，约定的是 ${fmtTime(c.due_at, tz, now)}。${lateText}${retry}按你的性子去做，用 reply 发给对方；如果已经不需要了，用 commitment_cancel 取消 #${c.id} 并写明原因。`
  }

  private settle(c: CommitmentRow, delivered: boolean, now: number): void {
    const L = this.d.ledger
    if (L.commitment(c.id)?.state !== 'firing') return // 那一轮里模型自己取消了
    if (delivered) {
      if (!this.nextDay(c, 'done', now)) L.updateCommitment(c.id, { state: 'done' })
      this.d.log.info('commitment.done', { chat: c.chat_id, id: c.id, attempts: c.attempts + 1 })
      return
    }
    const attempts = c.attempts + 1
    if (attempts >= MAX_ATTEMPTS) {
      const daily = this.nextDay(c, 'failed', now)
      if (!daily) L.updateCommitment(c.id, { state: 'failed', attempts })
      this.d.log.warn('commitment.failed', { chat: c.chat_id, id: c.id, attempts })
      this.d.notifyOwner(`commitment:${c.id}`, `有一件答应过的事提醒了 ${attempts} 次都没开口，${daily ? '这一次已放弃（每天的事，下一天照常提醒）' : '已放弃'}：「${c.text.slice(0, 60)}」`)
      return
    }
    const wait = this.d.retryMs[Math.min(attempts, this.d.retryMs.length) - 1] ?? 15 * MIN
    L.updateCommitment(c.id, { state: 'pending', attempts, next_at: now + wait })
    this.d.log.info('commitment.retry', { chat: c.chat_id, id: c.id, attempts, in_min: Math.round(wait / MIN) })
  }

  /** 每天重复的：这一次做完或彻底失败后不关闭，排到下一天同一时间，回到待触发、尝试次数清零，note 记下这一次的结果和时间。一次性的返回 false。 */
  private nextDay(c: CommitmentRow, result: 'done' | 'failed', now: number): boolean {
    const d = dailyOf(c)
    if (!d) return false
    const tz = this.d.timeZone()
    const next = nextDaily(d.h, d.mi, Math.max(now, c.due_at), tz)
    const l = localParts(c.due_at, tz)
    const note = `last ${result} ${l.y}-${pad2(l.mo)}-${pad2(l.d)} ${pad2(l.h)}:${pad2(l.mi)}`
    this.d.ledger.updateCommitment(c.id, { state: 'pending', due_at: next, next_at: next, attempts: 0, inbound_id: null, note })
    this.d.log.info('commitment.next_day', { chat: c.chat_id, id: c.id, result, next_in_min: Math.round((next - now) / MIN) })
    return true
  }

  /** 每天重复的：网关停过一阵、错过了好几天，只补最近这一次（不一天天连发），尝试次数清零 */
  private catchUp(c: CommitmentRow, now: number): CommitmentRow {
    const d = dailyOf(c)
    if (!d) return c
    const latest = lastDaily(d.h, d.mi, now, this.d.timeZone())
    if (c.due_at >= latest) return c
    const f = { due_at: latest, next_at: latest, attempts: 0, note: c.note === 'deferred_sleep' ? null : c.note }
    this.d.ledger.updateCommitment(c.id, f)
    this.d.log.info('commitment.caught_up', { chat: c.chat_id, id: c.id, skipped_min: Math.round((latest - c.due_at) / MIN) })
    return { ...c, ...f }
  }
}

function str(v: unknown): string {
  return typeof v === 'string' ? v.replace(/\s+/g, ' ').trim() : ''
}
