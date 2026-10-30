// outage-delivery 第 2 项：发送层三条分支（线路不通被拦 / 已发出后网络失败 / 连接前就失败）、结果文案、
// /v1/send 的 ok 与 delivered 口径、表情包。前三条分支用真的 TelegramApi 对着本机假服务，探测也是真发 getMe
import { afterEach, expect, test } from 'bun:test'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { Ledger } from '../../src/ledger'
import { Logger } from '../../src/log'
import { LinkMonitor, TelegramApi, TgLinkDownError, TgNetworkError } from '../../src/telegram/api'
import { describeResults, Sender } from '../../src/telegram/sender'
import { StickerService } from '../../src/stickers/sticker'

const TOKEN = '123:abc'
const cleanups: (() => void)[] = []
afterEach(() => { for (const f of cleanups.splice(0)) f() })

/** 本机假 Telegram：sendMessage 正文命中 badText 时回非 Telegram 格式的 502（回包读不出 → 可能已送达）；getMe 按 probeOk 回 */
function fakeTelegram(o: { badText: string; probeOk: boolean }) {
  const seen: { method: string; text?: string }[] = []
  let n = 0
  const server = Bun.serve({
    port: 0, hostname: '127.0.0.1',
    async fetch(req) {
      const method = new URL(req.url).pathname.split('/').pop() ?? ''
      const body = method === 'sendMessage' ? await req.json() as { text?: string } : {}
      seen.push({ method, text: body.text })
      if (method === 'getMe') return o.probeOk ? Response.json({ ok: true, result: { id: 1, is_bot: true, first_name: 'b' } }) : new Response('<html>bad gateway</html>', { status: 502 })
      if (method === 'sendMessage' && body.text === o.badText) return new Response('<html>bad gateway</html>', { status: 502 })
      return Response.json({ ok: true, result: { message_id: ++n, date: 0, chat: { id: 1, type: 'private' } } })
    },
  })
  cleanups.push(() => server.stop(true))
  return { base: `http://127.0.0.1:${server.port}`, seen }
}

function rig(api: TelegramApi, o: { paragraphDelay?: number; maxSendWaitMs?: number } = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'outage-sender-'))
  const ledger = new Ledger(':memory:')
  const access = { allowFrom: [], groups: {}, splitOnParagraph: true, paragraphDelay: o.paragraphDelay ?? 0 } as any
  const sender = new Sender(api, ledger, new Logger({ dir }), { allowDirs: () => [dir], maxSendWaitMs: o.maxSendWaitMs ?? 1000, access: () => access })
  const turn = ledger.startTurn({ chatId: '1', segmentId: 1, kind: 'message', inboundIds: [], attempt: 0 })
  cleanups.push(() => { ledger.close(); rmSync(dir, { recursive: true, force: true }) })
  const logEvents = (event: string) => readFileSync(join(dir, 'gateway.log'), 'utf8').trim().split('\n').map(l => JSON.parse(l)).filter(e => e.event === event)
  return { sender, ledger, turn, logEvents }
}

test('已发出后网络失败、探测成功：这段可能已送达，后面照常发；只探测一次（并入 call() 自动发起的那次）', async () => {
  const tg = fakeTelegram({ badText: '二', probeOk: true })
  const api = new TelegramApi(TOKEN, tg.base)
  const r = rig(api)
  const res = await r.sender.send({ chatId: '1', turnId: r.turn.id, callSeq: 1, text: '一\n\n二\n\n三' })
  expect(res.map(x => x.state)).toEqual(['sent', 'ambiguous', 'sent'])
  expect(res.some(x => x.linkDown)).toBe(false)
  expect(r.ledger.outboundForTurn(r.turn.id).map(x => x.state)).toEqual(['sent', 'ambiguous', 'sent'])
  expect(tg.seen.filter(x => x.method === 'getMe')).toHaveLength(1)
  expect(api.link.state).toBe('up')
  expect(r.logEvents('send.failed').map(e => e.cls)).toEqual(['ambiguous'])
})

test('已发出后网络失败、探测也失败：这段照旧可能已送达；后面的段不发出，记 unsent、结果 failed+linkDown', async () => {
  const tg = fakeTelegram({ badText: '二', probeOk: false })
  const api = new TelegramApi(TOKEN, tg.base)
  const r = rig(api)
  const res = await r.sender.send({ chatId: '1', turnId: r.turn.id, callSeq: 1, text: '一\n\n二\n\n三\n\n四' })
  expect(res.map(x => x.state)).toEqual(['sent', 'ambiguous', 'failed', 'failed'])
  expect(res.map(x => x.linkDown ?? false)).toEqual([false, false, true, true])
  expect(res[2]!.reason).toContain('线路不通，没有发出去')
  const rows = r.ledger.outboundForTurn(r.turn.id)
  expect(rows.map(x => x.state)).toEqual(['sent', 'ambiguous', 'unsent', 'unsent'])
  expect(rows[2]!.error).toStartWith('link_down')
  expect(rows[2]!.text).toBe('三') // 账本里有完整内容，补发能用
  expect(tg.seen.filter(x => x.method === 'sendMessage').map(x => x.text)).toEqual(['一', '二'])
  expect(tg.seen.filter(x => x.method === 'getMe')).toHaveLength(1)
  expect(api.link.state).toBe('down')
  expect(r.logEvents('send.failed').map(e => e.cls)).toEqual(['ambiguous', 'link_down', 'link_down'])
})

test('连接前就失败、探测也失败：不再原地等重试，整条回复记 unsent，不报"被限流"', async () => {
  // 假 api 照 call() 的约定：不通时直接抛 TgLinkDownError；否则连接被拒，并按规则 b 上报（触发探测）。
  // 不用真端口：本机代理环境下连空端口会被代理接走，拿不到"连接被拒"
  const link = new LinkMonitor(async () => false)
  const sent: string[] = []
  const api = {
    link,
    sendMessage: async (_c: string, text: string) => {
      if (link.state === 'down') throw new TgLinkDownError('sendMessage')
      sent.push(text)
      link.failed(true)
      throw new TgNetworkError('sendMessage', 'ConnectionRefused', '')
    },
    sendChatAction: async () => true,
  } as unknown as TelegramApi
  const r = rig(api, { maxSendWaitMs: 10_000 })
  const t0 = Date.now()
  const res = await r.sender.send({ chatId: '1', turnId: r.turn.id, callSeq: 1, text: '一\n\n二' })
  expect(Date.now() - t0).toBeLessThan(900) // 没有走 1 秒的重试等待
  expect(res.map(x => [x.state, x.linkDown])).toEqual([['failed', true], ['failed', true]])
  expect(res.every(x => !String(x.reason).includes('限流'))).toBe(true)
  expect(r.ledger.outboundForTurn(r.turn.id).map(x => x.state)).toEqual(['unsent', 'unsent'])
  expect(sent).toEqual(['一']) // 探测确认不通后，第 1 段的重试和第 2 段都没再发出
  expect(link.state).toBe('down')
})

test('连接前就失败、探测成功但重试用完：这段记 unsent（肯定没发出），结果 failed+linkDown，后面的段照常尝试', async () => {
  let sends = 0
  const link = new LinkMonitor(async () => true)
  const api = {
    link,
    sendMessage: async (_c: string, text: string) => {
      sends++
      if (text === '一') { link.failed(true); throw new TgNetworkError('sendMessage', 'ECONNREFUSED', '') }
      link.reached()
      return { message_id: sends }
    },
    sendChatAction: async () => true,
  } as unknown as TelegramApi
  const r = rig(api, { maxSendWaitMs: 0 })
  const res = await r.sender.send({ chatId: '1', turnId: r.turn.id, callSeq: 1, text: '一\n\n二' })
  expect(res.map(x => x.state)).toEqual(['failed', 'sent'])
  expect(res[0]!.linkDown).toBe(true)
  const rows = r.ledger.outboundForTurn(r.turn.id)
  expect(rows.map(x => x.state)).toEqual(['unsent', 'sent'])
  expect(rows[0]!.error).toStartWith('link_down')
})

test('线路已不通：段间停顿和"正在输入"都跳过，每段各自记 unsent', async () => {
  const link = new LinkMonitor(async () => false)
  await link.probeNow()
  expect(link.state).toBe('down')
  let actions = 0
  const api = {
    link,
    sendMessage: async () => { throw new TgLinkDownError('sendMessage') },
    sendChatAction: async () => { actions++; return true },
  } as unknown as TelegramApi
  const r = rig(api, { paragraphDelay: 3_000 })
  const t0 = Date.now()
  const res = await r.sender.send({ chatId: '1', turnId: r.turn.id, callSeq: 1, text: '甲\n\n乙\n\n丙' })
  expect(Date.now() - t0).toBeLessThan(1_000)
  expect(actions).toBe(0)
  expect(res.map(x => x.linkDown)).toEqual([true, true, true])
  expect(r.ledger.outboundForTurn(r.turn.id).map(x => x.state)).toEqual(['unsent', 'unsent', 'unsent'])
})

test('结果文案：有没发出的段时写线路不通、别重发，不附"对方已经看到了"，不计入送达；/v1/send 的 ok = !(有段没送达且送达数为 0)', () => {
  const lost = { index: 1, kind: 'text' as const, state: 'failed' as const, reason: 'Telegram 线路不通，没有发出去', linkDown: true }
  const all = describeResults([lost, { ...lost, index: 2 }])
  expect(all.text).toContain('Telegram 线路不通，没有发出去，对方没看到；不要再用 reply 重发，也别说已经发了；恢复后程序会提醒你补')
  expect(all.text).not.toContain('对方已经看到了')
  expect(all.text).not.toContain('可以稍后用 reply 补发')
  expect(all.delivered).toBe(0)
  expect(all.isError).toBe(true) // /v1/send：ok = !isError → false

  const mixed = describeResults([{ index: 1, kind: 'text', state: 'sent' }, { index: 2, kind: 'text', state: 'ambiguous', reason: '网络异常，可能已送达（network ECONNRESET）' }, { ...lost, index: 3 }])
  expect(mixed.delivered).toBe(2) // 可能已送达计入，linkDown 段不计
  expect(mixed.isError).toBe(false) // 部分发出 → ok:true
  expect(mixed.text).not.toContain('对方已经看到了')
  expect(mixed.text).toContain('不要再用 reply 重发')

  // 拒收与没发出同时出现：拒收那段不给"可以用 reply 补发"的说法，免得和"别重发"打架
  const both = describeResults([{ index: 1, kind: 'file', state: 'failed', reason: 'Telegram 拒收（x）' }, { ...lost, index: 2 }])
  expect(both.text).not.toContain('可以稍后用 reply 补发')
  expect(both.text).toContain('不是线路原因')

  // 没有没发出的段：文案与原来一致
  expect(describeResults([{ index: 1, kind: 'text', state: 'failed', reason: 'Telegram 拒收' }]).text).toContain('需要的话可以稍后用 reply 补发')
})

test('表情包：线路不通 → 记 unsent，日志 tool.sticker_failed cls=link_down，工具结果说没有发出去', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'outage-sticker-'))
  const root = join(dir, 'stickers')
  mkdirSync(join(root, 'sfw'), { recursive: true })
  writeFileSync(join(root, 'sfw', 'a.gif'), 'GIF89a')
  const lib = join(dir, 'stickers.json')
  writeFileSync(lib, JSON.stringify({ sfw: [{ label: 'Happy 1', path: join(root, 'sfw', 'a.gif') }], nsfw: [] }))
  const ledger = new Ledger(':memory:')
  cleanups.push(() => { ledger.close(); rmSync(dir, { recursive: true, force: true }) })
  const api = { sendFile: async () => { throw new TgLinkDownError('sendAnimation') } } as unknown as TelegramApi
  const svc = new StickerService({ api, ledger, log: new Logger({ dir }), file: () => lib, root: () => root })
  const turn = ledger.startTurn({ chatId: '1', segmentId: 1, kind: 'message', inboundIds: [], attempt: 0 })
  const res = await svc.send('1', turn.id, { action: 'send', query: 'Happy 1' })
  expect(res.isError).toBe(true)
  expect(res.text).toContain('没有发出去')
  expect(res.text).not.toContain('可能已经发出去')
  const rows = ledger.outboundForTurn(turn.id)
  expect(rows.map(x => [x.state, x.error])).toEqual([['unsent', 'link_down']])
  expect(ledger.chainOutcome(turn.id)).toEqual({ delivered: 0, unsent: 1 })
  const failed = readFileSync(join(dir, 'gateway.log'), 'utf8').trim().split('\n').map(l => JSON.parse(l)).filter(e => e.event === 'tool.sticker_failed')
  expect(failed.map(e => e.cls)).toEqual(['link_down'])
})
