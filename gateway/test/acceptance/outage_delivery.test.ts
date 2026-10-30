// 黑盒验收：Telegram 断线时不再把「没发出去」当「已送达」（依据 .devflow/outage-delivery-接口.md 的行为清单、错误契约与 R2）。
// 只通过对外接口：配置文件起网关子进程；网关与假 Telegram 之间夹一层可切换故障的 TCP 转发（tg-fault-proxy.ts）；
// 读网关落盘的 gateway.log、请求本机 /v1/health /v1/send /v1/inject、看假 Telegram 实际收到的请求、看假模型收到的提示文字。
// 不 import gateway/src 下任何模块，不 mock 内部层，不读账本。
// 用例名开头的「#N」= 接口文件行为清单第 N 条；「契约」= 错误契约表；「R2」= 相邻回归。
import { describe, expect, setDefaultTimeout, test } from 'bun:test'
import { existsSync, mkdirSync, readFileSync, realpathSync, rmSync, writeFileSync } from 'fs'
import { join } from 'path'
import { FakeTelegram } from '../fakes/fake-telegram'
import { FRIEND, GROUP, Gateway, OWNER, evs, logEvents, logPath, makeBot, prompts, readJsonl, sleep, writeConfig, type BotEnv, type BotOpts, type PromptRec } from './_acc'
import { TgFaultProxy } from './tg-fault-proxy'

setDefaultTimeout(90_000)

/** 测试里把断线判定与补发等待调小（接口文件「配置」一节：1000 是允许的最小值，0 = 恢复后立即补发） */
const FAST = { link_down_after_ms: 1000, outage_resend_delay_ms: 0 }

type Ctx = { tg: FakeTelegram; b: BotEnv; gw: Gateway; px: TgFaultProxy; env: Record<string, string> }
type Opts = BotOpts & { fast?: boolean; before?: (b: BotEnv) => void; env?: (b: BotEnv) => Record<string, string> }

async function withOutage(o: Opts, fn: (c: Ctx) => Promise<void>): Promise<void> {
  const tg = new FakeTelegram()
  const px = new TgFaultProxy(Number(tg.server.port))
  await px.start()
  const b = makeBot(tg, { ...o, gw: { telegram_api: px.url, ...(o.fast === false ? {} : FAST), ...o.gw } })
  const gw = new Gateway(b)
  try {
    o.before?.(b)
    const env = o.env?.(b) ?? {}
    await gw.start(env)
    await fn({ tg, b, gw, px, env })
  } finally {
    await gw.stop().catch(() => {})
    px.stop()
    tg.stop()
    try { rmSync(b.root, { recursive: true, force: true }) } catch {}
  }
}

// ─── 小工具 ───

/** 轮询到 fn 给出真值；超时返回 null（交给 expect 判红，而不是抛超时异常） */
async function poll<T>(fn: () => T | null | undefined | false | Promise<T | null | undefined | false>, ms: number, stepMs = 100): Promise<T | null> {
  const end = Date.now() + ms
  for (;;) {
    const v = await fn()
    if (v) return v as T
    if (Date.now() >= end) return null
    await sleep(stepMs)
  }
}

const recent = (b: BotEnv) => logEvents(b).slice(-15).map(e => e.event).join(', ')

/** 等第 nth 条（从 0 数）满足条件的日志事件；等不到就判红 */
async function nthEvent(b: BotEnv, name: string, nth: number, ms: number, what: string, pred: (e: Record<string, any>) => boolean = () => true): Promise<Record<string, any>> {
  const e = await poll(() => evs(b, name, pred)[nth], ms)
  expect(e ?? null, `${what}：${Math.round(ms / 1000)} 秒内应出现日志 ${name}（第 ${nth + 1} 条）；最近的日志事件：${recent(b)}`).not.toBeNull()
  return e as Record<string, any>
}

const health = async (gw: Gateway) => (await gw.call('/v1/health')).json

/** 网关至少完成过一次拉取（有过回包），断线前的「最近回包」有个起点 */
async function warm(c: Ctx): Promise<void> {
  const ok = await poll(() => c.px.requests('getUpdates').length >= 2, 15_000)
  expect(ok, '搭场景：网关应已开始拉取消息').toBeTruthy()
}

/** 把线路整体切成「读完就断」，等网关判定不通 */
async function goDown(c: Ctx, ms = 25_000): Promise<Record<string, any>> {
  const n = evs(c.b, 'telegram.link_down').length
  c.px.mode = 'reset'
  return nthEvent(c.b, 'telegram.link_down', n, ms, '线路整体掐断后')
}

/** 恢复转发，等网关判定恢复 */
async function goUp(c: Ctx, ms = 12_000): Promise<Record<string, any>> {
  const n = evs(c.b, 'telegram.link_up').length
  c.px.mode = 'forward'
  return nthEvent(c.b, 'telegram.link_up', n, ms, '恢复转发后')
}

type ToolRec = { sessionId: string; name: string; text: string; isError: boolean; args?: any }
const tools = (b: BotEnv) => readJsonl<ToolRec>(join(b.acpState, 'tool-results.jsonl'))
const RESEND_HEAD = /(^|\n)⟦系统·补发⟧/
const resendPrompts = (b: BotEnv) => prompts(b).filter(p => RESEND_HEAD.test(p.text))
const duePrompts = (b: BotEnv) => prompts(b).filter(p => p.text.includes('⟦系统·承诺到期⟧'))
const chatTurns = (b: BotEnv) => prompts(b).filter(p => !p.text.includes('⟦系统·整理记忆⟧'))
const sentTexts = (tg: FakeTelegram, chat: number) => tg.sentTo(chat).map(s => s.text ?? '')
const attempted = (px: TgFaultProxy, needle: string) => px.seen.filter(r => r.body.includes(needle))
const isPart = (n: number) => (r: { tgMethod: string; body: string }) => r.tgMethod === 'sendMessage' && r.body.includes(`（第${n}段）`)

/** 等假模型收到含 needle 的提示（这一轮已经开始） */
async function promptArrived(b: BotEnv, needle: string, ms = 15_000): Promise<PromptRec> {
  const p = await poll(() => prompts(b).find(x => x.text.includes(needle)), ms)
  expect(p ?? null, `搭场景：假模型应收到含「${needle}」的提示`).not.toBeNull()
  return p as PromptRec
}

function setFeatures(b: BotEnv, features: Record<string, boolean>): void {
  const cfg = JSON.parse(readFileSync(b.configPath, 'utf8'))
  cfg.features = { ...(cfg.features ?? {}), ...features }
  writeFileSync(b.configPath, JSON.stringify(cfg))
}

/** 约定内容里的指令用 ! 写「!」：登记那一轮的消息里没有指令（不拖慢、不多发），到点提醒里才出现指令 */
const commitCreate = (content: string, dueAt: number) =>
  `好呀 !tool:commitment_create|{"content":"${content}","when":"${new Date(dueAt).toISOString()}"}`

// ─── 错误契约：配置 ───

describe('错误契约：配置', () => {
  async function expectStartFails(gwCfg: Record<string, unknown>, key: string): Promise<void> {
    const tg = new FakeTelegram()
    const b = makeBot(tg, { gw: gwCfg })
    const gw = new Gateway(b)
    try {
      const code = await gw.startExpectExit()
      expect(code, `配置 ${JSON.stringify(gwCfg)} 应让网关启动失败，实际启动成功了`).not.toBeNull()
      const said = gw.stderr + gw.stdout + (existsSync(logPath(b)) ? readFileSync(logPath(b), 'utf8') : '')
      expect(said, '启动失败时日志/标准错误应说明是哪个键').toContain(key)
    } finally {
      await gw.stop().catch(() => {})
      tg.stop()
      try { rmSync(b.root, { recursive: true, force: true }) } catch {}
    }
  }

  test('契约 link_down_after_ms=999（小于 1000）→ 网关启动失败，说明是 link_down_after_ms', async () => {
    await expectStartFails({ link_down_after_ms: 999 }, 'link_down_after_ms')
  })

  test('契约 outage_resend_delay_ms=-1（负数）→ 网关启动失败，说明是 outage_resend_delay_ms', async () => {
    await expectStartFails({ outage_resend_delay_ms: -1 }, 'outage_resend_delay_ms')
  })
})

// ─── 线路判定 ───

describe('线路判定', () => {
  test('#1 线路正常、发送成功 → Telegram 收到、没有 send.failed、health 的 telegram.link="up"、link_down_ms=null、last_reach_ms 为整数', async () => {
    await withOutage({}, async c => {
      c.tg.pushText(FRIEND, '在吗')
      const got = await poll(() => sentTexts(c.tg, FRIEND).includes('收到：在吗'), 20_000)
      expect(got, 'Telegram 应收到回复「收到：在吗」').toBeTruthy()
      await nthEvent(c.b, 'tool.reply', 0, 5_000, '回复之后')
      expect(evs(c.b, 'send.failed')).toEqual([])
      const h = await health(c.gw)
      expect(h?.telegram?.link).toBe('up')
      expect(h?.telegram?.link_down_ms).toBeNull()
      expect(Number.isInteger(h?.telegram?.last_reach_ms), `last_reach_ms 应为整数：${JSON.stringify(h?.telegram)}`).toBe(true)
      expect(h.telegram.last_reach_ms).toBeGreaterThanOrEqual(0)
    })
  })

  test('#2 线路正常时第 2 段被掐断（ECONNRESET）、随后探测成功 → 这段 send.failed cls=ambiguous，第 3 段照常发出，不判不通', async () => {
    await withOutage({}, async c => {
      await warm(c)
      // times:2——连接被掐断后 HTTP 客户端可能自动换新连接重发一次，两次都掐断，网关才真拿到网络层失败
      c.px.rules.push({ match: isPart(2), action: 'reset', times: 2 })
      c.tg.pushText(FRIEND, '三段话 !parts:3')
      const third = await poll(() => sentTexts(c.tg, FRIEND).some(t => t.includes('（第3段）')), 25_000)
      expect(third, '第 3 段应照常发到 Telegram').toBeTruthy()
      const cuts = attempted(c.px, '（第2段）').map(r => r.action)
      expect(cuts.length >= 1 && cuts.every(a => a === 'reset'), `搭场景：第 2 段每次都应被转发层掐断，实际 ${JSON.stringify(cuts)}`).toBe(true)
      expect(sentTexts(c.tg, FRIEND).some(t => t.includes('（第2段）')), '搭场景：第 2 段不该到达 Telegram').toBe(false)
      expect(evs(c.b, 'send.failed').map(e => e.cls)).toEqual(['ambiguous'])
      expect(evs(c.b, 'telegram.link_down')).toEqual([])
    })
  })

  test('#3 第 2 段网络层失败、随后探测也失败 → 10 秒内 telegram.link_down；剩下的第 3 段 send.failed cls=link_down，网关根本没向 Telegram 发它', async () => {
    await withOutage({}, async c => {
      await warm(c)
      c.px.rules.push({ match: isPart(2), action: 'reset', thenMode: 'reset' })
      c.tg.pushText(FRIEND, '三段话 !parts:3')
      const cut = await poll(() => c.px.seen.find(r => r.action === 'reset' && r.body.includes('（第2段）')), 20_000)
      expect(cut ?? null, '搭场景：第 2 段应被转发层掐断').not.toBeNull()
      await nthEvent(c.b, 'telegram.link_down', 0, 12_000, '第 2 段失败、探测也失败后 10 秒内')
      await nthEvent(c.b, 'send.failed', 0, 15_000, '第 3 段', e => e.cls === 'link_down')
      await sleep(500)
      expect(evs(c.b, 'send.failed', e => e.cls === 'link_down'), '只有第 3 段记 link_down').toHaveLength(1)
      expect(evs(c.b, 'send.failed', e => e.cls !== 'link_down'), '第 2 段按原规则记（不是 link_down）').toHaveLength(1)
      expect(attempted(c.px, '（第3段）'), '不通期间网关不该向 Telegram 发第 3 段').toEqual([])
      expect(sentTexts(c.tg, FRIEND).some(t => t.includes('（第3段）'))).toBe(false)
    })
  })

  test('#4 距最近回包超过 link_down_after_ms 且最近请求网络层失败 → 发生 getMe 探测；探测失败 → telegram.link_down{since_reach_ms}，health 的 link="down"、link_down_ms 为数字', async () => {
    await withOutage({}, async c => {
      await warm(c)
      const t0 = Date.now()
      c.px.mode = 'reset'
      const probe = await poll(() => c.px.requests('getMe', t0)[0], 15_000)
      expect(probe ?? null, '断线后 10 秒检查应向 Telegram 发 getMe 探测').not.toBeNull()
      const down = await nthEvent(c.b, 'telegram.link_down', 0, 25_000, '线路整体掐断后')
      expect(Number.isInteger(down.since_reach_ms), `since_reach_ms 应为整数：${JSON.stringify(down)}`).toBe(true)
      expect(down.since_reach_ms).toBeGreaterThanOrEqual(1000)
      const h = await health(c.gw)
      expect(h?.telegram?.link).toBe('down')
      expect(typeof h?.telegram?.link_down_ms, `link_down_ms 应为数字：${JSON.stringify(h?.telegram)}`).toBe('number')
      expect(h.telegram.link_down_ms).toBeGreaterThanOrEqual(0)
    })
  })

  test('#5 拉取遇到 429、502（Telegram 格式回应）→ 不判不通：没有 telegram.link_down，health 的 link 保持 "up"', async () => {
    await withOutage({}, async c => {
      await warm(c)
      // 先一次 429（retry_after 1 秒），之后一直 502。409 不放进来：实测网关碰到 409 后退避十几秒，窗口里碰不到别的错误
      c.tg.faults.push(
        { method: 'getUpdates', status: 429, description: 'Too Many Requests: retry after 1', retryAfter: 1, times: 1 },
        { method: 'getUpdates', status: 502, description: 'Bad Gateway', times: 1_000_000 },
      )
      const t0 = Date.now()
      await sleep(13_000)
      expect(c.tg.faults.map(f => f.status), '搭场景：429 应已被拉取碰到').toEqual([502])
      expect(c.tg.calls.filter(x => x.method === 'getUpdates' && x.at >= t0).length, '搭场景：之后应碰到 502').toBeGreaterThanOrEqual(2)
      expect(evs(c.b, 'telegram.link_down')).toEqual([])
      expect((await health(c.gw))?.telegram?.link).toBe('up')
    })
  })

  test('#6 长时间没有新回包、但最近一次请求是成功的 → 不探测（没有新的 getMe），link 保持 "up"', async () => {
    // 长轮询 3 秒（假 Telegram 最多挂 2 秒）：两次回包之间隔 2 秒，超过 link_down_after_ms=1000，但每次都成功
    await withOutage({ gw: { poll_timeout_s: 3 } }, async c => {
      await warm(c)
      await sleep(1_000)
      const t0 = Date.now()
      await sleep(13_000)
      expect(c.px.requests('getMe', t0), '最近请求都成功时不该探测').toEqual([])
      expect(evs(c.b, 'telegram.link_down')).toEqual([])
      expect((await health(c.gw))?.telegram?.link).toBe('up')
    })
  })
})

// ─── 各入口在不通时 ───

describe('不通时的各入口', () => {
  test('#7 不通时模型调用 reply → Telegram 收不到；工具结果含「线路不通」「没有发出去」「不要再用 reply 重发」、不含「对方已经看到了」；tool.reply delivered=0；turn.end link_lost=1；没有催促回合', async () => {
    await withOutage({}, async c => {
      await warm(c)
      c.tg.pushText(FRIEND, '!slow:20000 !say:断线时的话甲')
      await promptArrived(c.b, '断线时的话甲')
      await goDown(c, 18_000)
      const tr = await poll(() => tools(c.b).find(t => t.name === 'reply'), 30_000)
      expect(tr ?? null, '搭场景：模型应调用了 reply').not.toBeNull()
      expect(tr!.text).toContain('线路不通')
      expect(tr!.text).toContain('没有发出去')
      expect(tr!.text).toContain('不要再用 reply 重发')
      expect(tr!.text).not.toContain('对方已经看到了')
      expect(attempted(c.px, '断线时的话甲'), '不通期间网关不该向 Telegram 发这段').toEqual([])
      const rep = await nthEvent(c.b, 'tool.reply', 0, 5_000, 'reply 之后')
      expect(rep.delivered).toBe(0)
      expect(evs(c.b, 'send.failed', e => e.cls === 'link_down')).toHaveLength(1)
      const end = await nthEvent(c.b, 'turn.end', 0, 10_000, '这一轮结束')
      expect(end.link_lost).toBe(1)
      await sleep(3_000)
      expect(chatTurns(c.b), '不该有催促/重试回合').toHaveLength(1)
    })
  })

  test('#8 不通时模型发表情包 → tool.sticker_failed cls=link_down；工具结果说没发出去；Telegram 收不到任何发图/表情请求', async () => {
    let home = ''
    await withOutage({
      before: b => {
        home = realpathSync(b.fakeHome)
        mkdirSync(join(home, '.claude', 'channels', '_shared'), { recursive: true })
        mkdirSync(join(home, 'resource', 'stickers', 'sfw'), { recursive: true })
        writeFileSync(join(home, 'resource', 'stickers', 'sfw', 'a.gif'), 'GIF89a')
        writeFileSync(join(home, '.claude', 'channels', '_shared', 'stickers.json'), JSON.stringify({ sfw: [{ label: 'Happy 1', path: join(home, 'resource', 'stickers', 'sfw', 'a.gif') }], nsfw: [] }))
        b.gw.image_dirs = [join(home, 'resource', 'stickers')]
        writeConfig(b)
      },
      env: () => ({ HOME: home, USERPROFILE: home }),
    }, async c => {
      await warm(c)
      c.tg.pushText(FRIEND, '!slow:20000\n!tool:sticker|{"action":"send","id":"sfw:1"}')
      await promptArrived(c.b, 'tool:sticker')
      await goDown(c, 18_000)
      const st = await poll(() => tools(c.b).find(t => t.name === 'sticker'), 30_000)
      expect(st ?? null, '搭场景：模型应调用了 sticker').not.toBeNull()
      expect(st!.text).toMatch(/没(有)?发出去/)
      await nthEvent(c.b, 'tool.sticker_failed', 0, 5_000, '表情包发送之后', e => e.cls === 'link_down')
      const fileSends = c.px.seen.filter(r => ['sendSticker', 'sendAnimation', 'sendPhoto', 'sendDocument'].includes(r.tgMethod))
      expect(fileSends, '不通期间网关不该发表情包').toEqual([])
    })
  })

  test('#9 不通时 POST /v1/send → HTTP 200，ok:false、delivered:0，每段 state=failed、linkDown:true、reason 含「线路不通，没有发出去」；恢复后这些段也不进补发', async () => {
    await withOutage({}, async c => {
      await warm(c)
      await goDown(c)
      const r = await c.gw.call('/v1/send', { chat_id: String(FRIEND), text: '接口甲\n\n接口乙' })
      expect(r.status).toBe(200)
      expect(r.json?.ok).toBe(false)
      expect(r.json?.delivered).toBe(0)
      expect(Array.isArray(r.json?.parts) && r.json.parts.length >= 1, `parts 应为非空数组：${r.text}`).toBe(true)
      for (const p of r.json.parts) {
        expect(p.state).toBe('failed')
        expect(p.linkDown).toBe(true)
        expect(String(p.reason)).toContain('线路不通，没有发出去')
      }
      expect(attempted(c.px, '接口甲').concat(attempted(c.px, '接口乙')), '不通期间网关不该向 Telegram 发').toEqual([])
      await goUp(c)
      await sleep(3_000)
      expect(resendPrompts(c.b).filter(p => p.text.includes('接口甲') || p.text.includes('接口乙')), '/v1/send 的段不进补发').toEqual([])
    })
  })

  test('#9 /v1/send 只有部分段发出（第 1 段送达、第 2 段被掐断、随后不通）→ ok:true、delivered:1；第 3 段 linkDown:true，第 2 段没有 linkDown', async () => {
    await withOutage({}, async c => {
      await warm(c)
      c.px.rules.push({ match: r => r.tgMethod === 'sendMessage' && r.body.includes('接口乙'), action: 'reset', thenMode: 'reset' })
      const r = await c.gw.call('/v1/send', { chat_id: String(FRIEND), text: '接口甲\n\n接口乙\n\n接口丙' })
      expect(r.status).toBe(200)
      expect(r.json?.parts?.length, `应分成 3 段：${r.text}`).toBe(3)
      // 第 2 段「可能已送达」算不算进 delivered，接口文件没写死：只要求 1 ≤ delivered ≤ 2（第 3 段肯定没发出）
      expect(r.json.delivered, r.text).toBeGreaterThanOrEqual(1)
      expect(r.json.delivered, r.text).toBeLessThanOrEqual(2)
      expect(r.json.ok, r.text).toBe(true)
      expect(r.json.parts[1].linkDown, `可能已送达的段不能标 linkDown（不许重发）：${r.text}`).not.toBe(true)
      expect(r.json.parts[2].state).toBe('failed')
      expect(r.json.parts[2].linkDown, r.text).toBe(true)
      expect(sentTexts(c.tg, FRIEND)).toContain('接口甲')
      expect(attempted(c.px, '接口丙').map(x => x.action), '不通后网关不该再发第 3 段').toEqual([])
    })
  })

  test('#10 不通时产生给主人的通知（模型鉴权失败）→ notify_owner_held{key}，主人收不到；恢复后 10 秒内主人收到', async () => {
    await withOutage({}, async c => {
      await warm(c)
      await goDown(c)
      writeFileSync(join(c.b.acpState, 'next-error.txt'), 'MISSING_CREDENTIAL deepseek-official requires DEEPSEEK_API_KEY')
      const ownerBefore = c.tg.sentTo(OWNER).length
      const r = await c.gw.call('/v1/inject', { chat_id: String(FRIEND), source: 'self_initiate', key: `k-${crypto.randomUUID()}`, text: '⟦系统·主动开口⟧ 想找对方聊聊' })
      expect(r.status, `搭场景：/v1/inject 应受理：${r.text}`).toBe(200)
      const held = await nthEvent(c.b, 'notify_owner_held', 0, 20_000, '不通时产生主人通知')
      expect(typeof held.key === 'string' && held.key.length > 0, `key 应为非空字符串：${JSON.stringify(held)}`).toBe(true)
      expect(c.px.seen.filter(x => x.tgMethod === 'sendMessage' && x.body.includes(String(OWNER))), '不通期间不该给主人发').toEqual([])
      await goUp(c)
      const got = await poll(() => c.tg.sentTo(OWNER).length > ownerBefore, 11_000)
      expect(got, '恢复后 10 秒内主人应收到攒着的通知').toBeTruthy()
    })
  })

  test('#11 不通后任意请求拿到回包 → 10 秒内 telegram.link_up{down_ms}（只一条），health 变 link="up"、link_down_ms=null', async () => {
    await withOutage({}, async c => {
      await warm(c)
      await goDown(c)
      await sleep(500)
      const up = await goUp(c, 11_000)
      expect(Number.isInteger(up.down_ms), `down_ms 应为整数：${JSON.stringify(up)}`).toBe(true)
      expect(up.down_ms).toBeGreaterThan(0)
      const h = await health(c.gw)
      expect(h?.telegram?.link).toBe('up')
      expect(h?.telegram?.link_down_ms).toBeNull()
      await sleep(1_000)
      expect(evs(c.b, 'telegram.link_up'), '每次翻转只记一条').toHaveLength(1)
    })
  })
})

// ─── 补发 ───

describe('补发', () => {
  test('#12 恢复后（outage_resend_delay_ms=0）→ 模型收到以 ⟦系统·补发⟧ 开头、含没发出那段原文的提示并跑一轮；outage.resend{chat,parts=1,turns=1,round=1}；用户看到的是新一轮回复而不是原文', async () => {
    await withOutage({}, async c => {
      await warm(c)
      c.tg.pushText(FRIEND, '!slow:20000 !say:断线时的话乙')
      await promptArrived(c.b, '断线时的话乙')
      await goDown(c, 18_000)
      await nthEvent(c.b, 'send.failed', 0, 30_000, '断线时回复', e => e.cls === 'link_down')
      const before = c.tg.sentTo(FRIEND).length
      await goUp(c)
      const rp = await poll(() => resendPrompts(c.b)[0], 15_000)
      expect(rp ?? null, '恢复后模型应收到 ⟦系统·补发⟧ 提示').not.toBeNull()
      expect(rp!.text).toContain('断线时的话乙')
      const ev = await nthEvent(c.b, 'outage.resend', 0, 10_000, '补发')
      expect(String(ev.chat)).toBe(String(FRIEND))
      expect(ev.parts).toBe(1)
      expect(ev.turns).toBe(1)
      expect(ev.round).toBe(1)
      const replied = await poll(() => c.tg.sentTo(FRIEND).length > before, 15_000)
      expect(replied, '补发那一轮的回复应发到 Telegram').toBeTruthy()
      expect(sentTexts(c.tg, FRIEND).slice(before), '不是把原文逐字重发').not.toContain('断线时的话乙')
    })
  })

  test('#13 补发提示里只有从没发出去的段：第 2 段（可能已送达）、第 1 段（已送达）都不出现，第 3 段出现', async () => {
    await withOutage({}, async c => {
      await warm(c)
      c.px.rules.push({ match: isPart(2), action: 'reset', thenMode: 'reset' })
      c.tg.pushText(FRIEND, '补发核对 !parts:3')
      await nthEvent(c.b, 'telegram.link_down', 0, 25_000, '第 2 段失败、探测也失败后')
      await nthEvent(c.b, 'send.failed', 0, 15_000, '第 3 段', e => e.cls === 'link_down')
      await goUp(c)
      const rp = await poll(() => resendPrompts(c.b)[0], 15_000)
      expect(rp ?? null, '恢复后模型应收到 ⟦系统·补发⟧ 提示').not.toBeNull()
      expect(rp!.text).toContain('收到：补发核对（第3段）')
      expect(rp!.text, '可能已送达的段不进补发').not.toContain('（第2段）')
      expect(rp!.text, '已送达的段不进补发').not.toContain('（第1段）')
    })
  })

  test('#16 群聊里因断线没发出的内容 → 恢复后 outage.resend_dropped reason=group，不补（没有 ⟦系统·补发⟧ 提示、没有 outage.resend）', async () => {
    await withOutage({
      access: { groups: { [String(GROUP)]: { allowFrom: [] } } },
      gw: { burst_window_ms: 0 },
      before: b => setFeatures(b, { groups: true }),
    }, async c => {
      await warm(c)
      await goDown(c)
      const r = await c.gw.call('/v1/inject', { chat_id: String(GROUP), source: 'director', key: `k-${crypto.randomUUID()}`, text: '⟦群聊·导演点到你了⟧ 群里最近的对话：大家好 !say:群里没发出的话' })
      expect(r.status, `搭场景：导演点名应受理：${r.text}`).toBe(200)
      await nthEvent(c.b, 'send.failed', 0, 20_000, '群里断线时回复', e => e.cls === 'link_down')
      await goUp(c)
      await nthEvent(c.b, 'outage.resend_dropped', 0, 15_000, '恢复后处理群聊内容', e => e.reason === 'group')
      await sleep(2_000)
      expect(resendPrompts(c.b)).toEqual([])
      expect(evs(c.b, 'outage.resend')).toEqual([])
    })
  })

  test('#17 断网期间网关重启 → 启动后执行一次补发检查：模型收到含原文的 ⟦系统·补发⟧ 提示，outage.resend round=1', async () => {
    await withOutage({}, async c => {
      await warm(c)
      c.tg.pushText(FRIEND, '!slow:20000 !say:断线时的话丙')
      await promptArrived(c.b, '断线时的话丙')
      await goDown(c, 18_000)
      await nthEvent(c.b, 'send.failed', 0, 30_000, '断线时回复', e => e.cls === 'link_down')
      await c.gw.stop()
      c.px.mode = 'forward'
      await c.gw.start(c.env)
      const rp = await poll(() => resendPrompts(c.b).find(p => p.text.includes('断线时的话丙')), 20_000)
      expect(rp ?? null, '重启后模型应收到含原文的 ⟦系统·补发⟧ 提示').not.toBeNull()
      const ev = await nthEvent(c.b, 'outage.resend', 0, 10_000, '重启后的补发检查')
      expect(ev.round).toBe(1)
    })
  })
})

// ─── 约定 ───

describe('约定', () => {
  test('#18 约定到点时线路不通 → 不触发，commitment.deferred reason=link_down（这次断线只记一条）；恢复后触发 commitment.fire，late_min=实际迟到分钟数，提醒文字提到晚了几分钟', async () => {
    await withOutage({}, async c => {
      await warm(c)
      const due = Date.now() + 30_000
      c.tg.pushText(OWNER, `好呀 !commit:提醒对方喝水|${new Date(due).toISOString()}`)
      const created = await poll(() => tools(c.b).find(t => t.name === 'commitment_create'), 15_000)
      expect(created ?? null, '搭场景：约定应登记').not.toBeNull()
      await goDown(c, 22_000)
      expect(Date.now(), '搭场景：应在到点前判定不通').toBeLessThan(due)
      await nthEvent(c.b, 'commitment.deferred', 0, due - Date.now() + 15_000, '到点时线路不通', e => e.reason === 'link_down')
      await sleep(Math.max(0, due + 62_000 - Date.now()))
      expect(evs(c.b, 'commitment.deferred', e => e.reason === 'link_down'), '同一次断线每条约定只记一次').toHaveLength(1)
      expect(duePrompts(c.b), '不通期间模型不该收到提醒').toEqual([])
      await goUp(c)
      const fire = await nthEvent(c.b, 'commitment.fire', 0, 12_000, '恢复后')
      const lateNow = Math.floor((Date.now() - due) / 60_000)
      expect(Number.isInteger(fire.late_min), `late_min 应为整数：${JSON.stringify(fire)}`).toBe(true)
      expect(fire.late_min).toBeGreaterThanOrEqual(Math.max(1, lateNow - 1))
      expect(fire.late_min).toBeLessThanOrEqual(lateNow)
      const p = await poll(() => duePrompts(c.b)[0], 10_000)
      expect(p ?? null, '恢复后模型应收到到期提醒').not.toBeNull()
      expect(p!.text).toContain('提醒对方喝水')
      // 补充说明第 3 条：迟到 ≥10 分钟写「现在已经晚了 <时长>」，不足 10 分钟写「现在到时间了。」
      expect(p!.text).toContain(lateNow >= 10 ? '现在已经晚了' : '现在到时间了')
    })
  }, 180_000)

  test('#19 约定触发后那一轮一段都没送达 → 不标完成，commitment.retry reason=link_down；恢复后再提醒一次、文字含「上次提醒时线路断了」；这一轮内容不进补发', async () => {
    await withOutage({}, async c => {
      await warm(c)
      const due = Date.now() + 12_000
      c.tg.pushText(OWNER, commitCreate('\\u0021slow:15000 提醒对方喝水', due))
      const created = await poll(() => tools(c.b).find(t => t.name === 'commitment_create'), 15_000)
      expect(created?.text ?? '', '搭场景：约定应登记').toContain('已登记')
      const first = await poll(() => duePrompts(c.b)[0], 30_000)
      expect(first ?? null, '搭场景：到点后模型应收到提醒').not.toBeNull()
      await goDown(c, 14_000)
      await nthEvent(c.b, 'commitment.retry', 0, 30_000, '提醒那一轮全部没发出', e => e.reason === 'link_down')
      await goUp(c)
      const again = await poll(() => prompts(c.b).find(p => p.at > first!.at && p.text.includes('上次提醒时线路断了')), 20_000)
      expect(again ?? null, '恢复后应再提醒一次，文字含「上次提醒时线路断了」').not.toBeNull()
      await sleep(3_000)
      expect(evs(c.b, 'outage.resend'), '约定那一轮的内容不进补发').toEqual([])
      expect(resendPrompts(c.b)).toEqual([])
    })
  }, 150_000)

  test('#20 约定那一轮第 1 段送达、第 2 段可能已送达、第 3 段没发出 → 约定照常完成（不 retry、不再提醒），第 3 段进补发', async () => {
    await withOutage({}, async c => {
      await warm(c)
      const due = Date.now() + 12_000
      c.tg.pushText(OWNER, commitCreate('\\u0021parts:3 提醒对方带伞', due))
      const created = await poll(() => tools(c.b).find(t => t.name === 'commitment_create'), 15_000)
      expect(created?.text ?? '', '搭场景：约定应登记').toContain('已登记')
      c.px.rules.push({ match: isPart(2), action: 'reset', thenMode: 'reset' })
      const first = await poll(() => duePrompts(c.b)[0], 30_000)
      expect(first ?? null, '搭场景：到点后模型应收到提醒').not.toBeNull()
      await nthEvent(c.b, 'send.failed', 0, 25_000, '提醒那一轮第 3 段', e => e.cls === 'link_down')
      await sleep(1_500)
      expect(evs(c.b, 'commitment.retry'), '有段发出或可能已送达时约定照常完成').toEqual([])
      await goUp(c)
      const rp = await poll(() => resendPrompts(c.b)[0], 15_000)
      expect(rp ?? null, '没发出的第 3 段应进补发').not.toBeNull()
      expect(rp!.text).toContain('（第3段）')
      expect(rp!.text).not.toContain('（第2段）')
      await sleep(2_000)
      expect(duePrompts(c.b), '约定已完成，不该再提醒').toHaveLength(1)
    })
  }, 150_000)

  test('#21 约定提醒那一轮线路不通：模型先调 reply（没发出）、同一轮再调 commitment_done → 工具结果「先不标完成」，commitment.done_refused reason=link_down，随后 commitment_list 里仍有这条', async () => {
    // 约定提醒文字是网关生成的，塞不进 !tool 指令，所以换用本目录的专用假件：提醒那一轮等测试写 due-go 后依次调 reply、commitment_done、commitment_list
    const DUE_DONE_ACP = join(import.meta.dir, 'fake-acp-due-done.ts')
    const DUE_REPLY = '约定那一轮的话丁'
    const CONTENT = '提醒对方吃药丁'
    await withOutage({
      before: b => { b.gw.dsh_command = ['bun', DUE_DONE_ACP, '--state', b.acpState]; writeConfig(b) },
    }, async c => {
      await warm(c)
      const due = Date.now() + 12_000
      c.tg.pushText(OWNER, commitCreate(CONTENT, due))
      const created = await poll(() => tools(c.b).find(t => t.name === 'commitment_create'), 15_000)
      expect(created?.text ?? '', '搭场景：约定应登记').toContain('已登记')
      await promptArrived(c.b, '⟦系统·承诺到期⟧', 30_000)
      await goDown(c)
      writeFileSync(join(c.b.acpState, 'due-go'), '1')
      const rep = await poll(() => tools(c.b).find(t => t.name === 'reply' && t.args?.text === DUE_REPLY), 15_000)
      expect(rep ?? null, '搭场景：提醒那一轮模型应先调了 reply').not.toBeNull()
      expect(attempted(c.px, DUE_REPLY), '前提：线路不通时这段不该发往 Telegram（0 送达）').toEqual([])
      const all = await poll(() => { const t = tools(c.b); const i = t.findIndex(x => x.name === 'commitment_done'); return i >= 0 && t.slice(i + 1).some(x => x.name === 'commitment_list') ? t : null }, 15_000)
      expect(all ?? null, '搭场景：同一轮里模型应随后调了 commitment_done 和 commitment_list').not.toBeNull()
      const iDone = all!.findIndex(x => x.name === 'commitment_done')
      const done = all![iDone]!
      expect(Number.isInteger(done.args?.id), `搭场景：假件应从提醒文字里解析出约定编号：${JSON.stringify(done.args)}`).toBe(true)
      expect(done.text, 'commitment_done 的工具结果').toContain('先不标完成')
      await nthEvent(c.b, 'commitment.done_refused', 0, 5_000, 'commitment_done 被拒之后', e => e.reason === 'link_down')
      const list = all!.slice(iDone + 1).find(x => x.name === 'commitment_list')!
      expect(list.isError, `commitment_list 不该报错：${list.text}`).toBe(false)
      expect(list.text, '没标完成的约定应仍在 commitment_list 里').toContain(CONTENT)
    })
  }, 150_000)
})

// ─── 相邻回归 ───

describe('相邻回归', () => {
  test('R2 缺省配置（不写 link_down_after_ms）下拉取一直失败：「…秒没有拉取成功」健康问题 85 秒时还没有，约 91 秒出现（85–100 秒之间）', async () => {
    await withOutage({ fast: false }, async c => {
      await warm(c)
      const t0 = Date.now()
      c.px.mode = 'reset'
      const pullProblem = (h: any) => Array.isArray(h?.problems) && h.problems.some((p: unknown) => String(p).includes('秒没有拉取成功'))
      await sleep(85_000)
      expect(pullProblem(await health(c.gw)), '85 秒时还不该出现拉取问题').toBe(false)
      const seenAt = await poll(async () => (pullProblem(await health(c.gw)) ? Date.now() : null), 20_000, 500)
      expect(seenAt ?? null, '100 秒内应出现「…秒没有拉取成功」').not.toBeNull()
      expect(seenAt! - t0).toBeLessThanOrEqual(100_000)
    })
  }, 150_000)
})
