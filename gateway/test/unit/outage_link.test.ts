// outage-delivery 第 1 项：线路判定（LinkMonitor）、call() 统一拦截、账本 unsent 与查询。时钟一律注入，不真等
import { afterEach, expect, test } from 'bun:test'
import { Ledger } from '../../src/ledger'
import { classifySendError, LinkMonitor, linkStaleAfterMs, TelegramApi, TgLinkDownError, TgNetworkError, type LinkState } from '../../src/telegram/api'

const stops: (() => void)[] = []
afterEach(() => { for (const f of stops.splice(0)) f() })

/** 可控的探测：每次调用登记一个待决的结果，测试决定它什么时候、以什么结局结束 */
function monitor(o: { staleAfterMs?: number } = {}) {
  let t = 1_000_000
  const probes: ((ok: boolean) => void)[] = []
  const changes: [LinkState, number][] = []
  const m = new LinkMonitor(() => new Promise<boolean>(r => probes.push(r)), { staleAfterMs: o.staleAfterMs ?? 90_000, now: () => t })
  m.onChange = (s, ms) => changes.push([s, ms])
  return { m, probes, changes, advance: (ms: number) => { t += ms }, now: () => t }
}

test('规则 a：距最近回包超过阈值且最近一次请求失败才探测；最近一次成功时再久也不探测', async () => {
  const { m, probes, advance } = monitor()
  advance(200_000)
  expect(m.tick()).toBeNull() // 从没失败过（#6）
  m.reached()
  m.failed()
  advance(90_000)
  expect(m.tick()).toBeNull() // 刚好等于阈值，不算超过
  advance(1)
  const p = m.tick()
  expect(p).not.toBeNull()
  expect(probes.length).toBe(1)
  // 失败后又有一次成功：不再满足"最近一次失败"
  probes[0]!(true)
  expect(await p).toBe('up')
  advance(500_000)
  expect(m.tick()).toBeNull()
  expect(probes.length).toBe(1)
})

test('规则 a：探测以网络层失败结束 → 进入 down，onChange 给出距最近回包的毫秒数', async () => {
  const { m, probes, changes, advance, now } = monitor({ staleAfterMs: 1_000 })
  m.reached()
  advance(5_000)
  m.failed()
  const p = m.tick()!
  probes[0]!(false)
  expect(await p).toBe('down')
  expect(m.state).toBe('down')
  expect(m.downAt).toBe(now())
  expect(changes).toEqual([['down', 5_000]])
})

test('规则 b：发送类请求网络层失败立即探测；非发送类（拉取）失败不立即探测', () => {
  const { m, probes } = monitor()
  m.failed(false)
  expect(probes.length).toBe(0)
  m.failed(true)
  expect(probes.length).toBe(1)
})

test('并发探测合并：探测在跑时，多处要求只发一次探测、拿到同一个结果；结束后再要求才发新的', async () => {
  const { m, probes } = monitor()
  m.failed(true)
  m.failed(true)
  const a = m.probeNow()
  const b = m.probeNow()
  expect(a).toBe(b)
  expect(probes.length).toBe(1)
  probes[0]!(false)
  expect(await a).toBe('down')
  expect(await b).toBe('down')
  const c = m.probeNow()
  expect(probes.length).toBe(2)
  probes[1]!(true)
  expect(await c).toBe('up')
})

test('规则 c：已经 down 时每 60 秒再探一次，没到 60 秒不探', async () => {
  const { m, probes, advance } = monitor()
  const p = m.probeNow()
  probes[0]!(false)
  await p
  expect(m.state).toBe('down')
  advance(59_999)
  expect(m.tick()).toBeNull()
  advance(1)
  const again = m.tick()
  expect(again).not.toBeNull()
  expect(probes.length).toBe(2)
  probes[1]!(false)
  expect(await again).toBe('down')
  advance(10_000)
  expect(m.tick()).toBeNull()
  // down 时发送类失败（发起前就已在路上的请求）不额外探测，仍按 60 秒节奏
  m.failed(true)
  expect(probes.length).toBe(2)
})

test('恢复：down 时任何回包立即回到 up，downAt 清空，onChange 给出不通持续毫秒数；每次翻转只回调一次', async () => {
  const { m, probes, changes, advance } = monitor()
  const p = m.probeNow()
  probes[0]!(false)
  await p
  advance(42_000)
  m.reached()
  m.reached()
  expect(m.state).toBe('up')
  expect(m.downAt).toBeNull()
  expect(changes.map(c => c[0])).toEqual(['down', 'up'])
  expect(changes[1]).toEqual(['up', 42_000])
})

test('探测失败但探测期间别的请求拿到过回包 → 不判不通', async () => {
  const { m, probes, changes } = monitor()
  const p = m.probeNow()
  m.reached()
  probes[0]!(false)
  expect(await p).toBe('up')
  expect(changes).toEqual([])
})

test('探测函数抛错按网络层失败处理；onChange 抛错不影响判定', async () => {
  const m = new LinkMonitor(async () => { throw new Error('boom') })
  m.onChange = () => { throw new Error('listener') }
  expect(await m.probeNow()).toBe('down')
})

test('linkStaleAfterMs：缺省 max(90 秒, 长轮询×3)，配置了就用配置', () => {
  expect(linkStaleAfterMs({ pollTimeoutS: 25 })).toBe(90_000)
  expect(linkStaleAfterMs({ pollTimeoutS: 50 })).toBe(150_000)
  expect(linkStaleAfterMs({ pollTimeoutS: 25, linkDownAfterMs: 1_500 })).toBe(1_500)
})

test('classifySendError：TgLinkDownError → link_down，且它仍是 TgNetworkError；其他分类不变', () => {
  const e = new TgLinkDownError('sendMessage')
  expect(e).toBeInstanceOf(TgNetworkError)
  expect(e.code).toBe('LinkDown')
  expect(classifySendError(e).cls).toBe('link_down')
  expect(classifySendError(new TgNetworkError('sendMessage', 'ECONNRESET', '')).cls).toBe('ambiguous')
  expect(classifySendError(new TgNetworkError('sendMessage', 'ECONNREFUSED', '')).cls).toBe('retryable')
})

// ─── call() 统一上报与拦截（对着本机 HTTP 服务跑） ───

type Reply = (method: string) => Response
function server(reply: Reply) {
  const hits: string[] = []
  const srv = Bun.serve({ port: 0, hostname: '127.0.0.1', fetch: req => { const m = req.url.split('/').pop()!; hits.push(m); return reply(m) } })
  stops.push(() => srv.stop(true))
  return { hits, api: new TelegramApi('123:fake', `http://127.0.0.1:${srv.port}`, 2_000) }
}
const tgOk = (result: unknown = true) => Response.json({ ok: true, result })
const tgErr = (code: number) => Response.json({ ok: false, error_code: code, description: `err ${code}` }, { status: code })

test('拦截范围：不通时 getUpdates/getMe/getFile 照常发出，其余方法都不发出、抛 TgLinkDownError', async () => {
  let down = true
  const { hits, api } = server(m => (m === 'getMe' && down ? new Response('<html>proxy</html>', { status: 502 }) : m === 'getUpdates' ? tgOk([]) : m === 'getFile' ? tgOk({ file_path: 'x' }) : tgOk({ message_id: 1, date: 0, chat: { id: 1, type: 'private' } })))
  expect(await api.link.probeNow()).toBe('down')
  hits.length = 0
  const blocked: [string, () => Promise<unknown>][] = [
    ['sendMessage', () => api.sendMessage('1', 'hi')],
    ['editMessageText', () => api.editMessageText('1', 1, 'x')],
    ['editMessageReplyMarkup', () => api.editMessageReplyMarkup('1', 1)],
    ['deleteMessage', () => api.deleteMessage('1', 1)],
    ['answerCallbackQuery', () => api.answerCallbackQuery('q')],
    ['sendPhoto', () => api.sendFile('photo', '1', import.meta.path)],
    ['sendChatAction', () => api.sendChatAction('1')],
    ['setMessageReaction', () => api.setMessageReaction('1', 1, '👍')],
    ['sendSticker', () => api.call('sendSticker', { chat_id: '1' })],
  ]
  for (const [name, f] of blocked) {
    const e = await f().then(() => null, x => x)
    expect(e, name).toBeInstanceOf(TgLinkDownError)
    expect((e as TgLinkDownError).method).toBe(name)
  }
  expect(hits).toEqual([])
  expect(api.link.state).toBe('down')
  // getFile 照常发出（拿到回包顺带让线路恢复），再关回 down 验 getMe、getUpdates
  await api.getFile('f')
  expect(api.link.state).toBe('up')
  await api.link.probeNow()
  expect(api.link.state).toBe('down')
  down = false
  await api.getMe()
  expect(api.link.state).toBe('up')
  down = true
  await api.link.probeNow()
  await api.getUpdates(0, 0)
  expect(hits.filter(h => h !== 'getMe')).toEqual(['getFile', 'getUpdates'])
  expect(api.link.state).toBe('up')
})

test('Telegram 格式的报错（409、429、502）算回包：不通时拿到就恢复；代理自己回的 JSON（没有 ok 字段）不算', async () => {
  const proxyPage = () => new Response('{"error":"bad gateway"}', { status: 502, headers: { 'content-type': 'application/json' } })
  let next: () => Response = proxyPage
  const { api } = server(m => (m === 'getMe' ? new Response('', { status: 502 }) : next()))
  for (const code of [409, 429, 502]) {
    await api.link.probeNow()
    expect(api.link.state).toBe('down')
    next = () => tgErr(code)
    await api.getUpdates(0, 0).catch(() => {})
    expect(api.link.state, `getUpdates ${code}`).toBe('up')
    next = proxyPage
  }
  // 代理页面：线路保持 up 时它让"最近一次请求"变成失败（规则 a 的前提），但不会自己翻成 up
  await api.link.probeNow()
  await api.getUpdates(0, 0).catch(() => {})
  expect(api.link.state).toBe('down')
})

test('发送类请求网络层失败 → 立即 getMe 探测（规则 b）；拉取失败不立即探测；调用方自己取消的不算失败', async () => {
  const { hits, api } = server(m => (m === 'getMe' ? tgOk({ id: 1, is_bot: true }) : new Response('<html>reset</html>', { status: 502 })))
  await api.getUpdates(0, 0).catch(() => {})
  await Bun.sleep(20)
  expect(hits).toEqual(['getUpdates'])
  await api.sendMessage('1', 'x').catch(() => {})
  const st = await api.link.probeNow() // 并入那次探测（或它已结束则另发一次）
  expect(st).toBe('up')
  expect(hits.slice(0, 3)).toEqual(['getUpdates', 'sendMessage', 'getMe'])
  // 取消：signal 已中止的请求不上报失败，也不触发探测
  const before = hits.length
  const ctrl = new AbortController()
  ctrl.abort()
  await api.call('sendMessage', {}, { signal: ctrl.signal }).catch(() => {})
  await Bun.sleep(20)
  expect(hits.length).toBe(before)
})

// ─── 账本：unsent ───

function chainSetup(l: Ledger, chat = '1', meta?: Record<string, unknown>) {
  const iid = l.insertInbound({ ukey: `x:${chat}:${Math.random()}`, chatId: chat, kind: meta ? 'synthetic' : 'user', text: 'hi', ts: 1, meta: meta ?? null }).id
  const seg = l.activeSegment(chat) ?? l.createSegment(chat, 'tok' + chat, false)
  const t = l.startTurn({ chatId: chat, segmentId: seg.id, kind: 'message', inboundIds: [iid], attempt: 0 })
  return { iid, t }
}
function part(l: Ledger, chat: string, turnId: number | null, n: number, state: 'sent' | 'ambiguous' | 'failed' | 'unsent', kind: 'text' | 'photo' | 'reaction' = 'text', text = `段${n}`) {
  const id = l.outboundIntent({ okey: turnId ? `turn:${turnId}:c1:p${n}` : null, chatId: chat, turnId, part: n, ofParts: 3, kind, text: kind === 'photo' ? null : text, file: kind === 'photo' ? '/tmp/a.png' : null })
  l.outboundResult(id, state, { error: state === 'unsent' ? 'link_down' : null })
  return id
}

test('unsent 不被现有读取当成已送达：送达计数、已发文字/文件、前情、中断识别、约定兑现都不算它', () => {
  const l = new Ledger(':memory:')
  const { iid, t } = chainSetup(l)
  part(l, '1', t.id, 1, 'unsent')
  part(l, '1', t.id, 2, 'unsent', 'photo')
  expect(l.deliveredInChain(t.root_id)).toBe(0)
  expect(l.deliveredForInbound(iid)).toBe(0)
  expect(l.sentTextsInChain(t.root_id)).toEqual([])
  expect(l.sentFilesInChain(t.root_id)).toEqual([])
  expect(l.sentTextsSince('1', 0)).toEqual([])
  expect(l.recentTranscript('1', { maxChars: 1000 }).filter(e => e.who === 'bot')).toEqual([])
  expect(l.segmentTranscript(t.segment_id).filter(e => e.who === 'bot')).toEqual([])
  l.finishTurn(t.id, 'crashed')
  expect(l.interruptedReply('1')).toBeNull()
  // 启动恢复只处理 pending，不动 unsent
  expect(l.recoverOnStartup(3).ambiguousOutbound).toEqual([])
  expect(l.outboundForTurn(t.id).map(o => o.state)).toEqual(['unsent', 'unsent'])
})

test('chainOutcome / chainOutcomeForInbound：区分送达数和没发出数；表情回应不算；没进过轮的消息为 0', () => {
  const l = new Ledger(':memory:')
  const { iid, t } = chainSetup(l)
  part(l, '1', t.id, 1, 'ambiguous')
  part(l, '1', t.id, 2, 'unsent')
  part(l, '1', t.id, 3, 'unsent', 'photo')
  part(l, '1', t.id, 4, 'unsent', 'reaction')
  const retry = l.startTurn({ chatId: '1', segmentId: t.segment_id, kind: 'retry', inboundIds: [iid], attempt: 1, rootId: t.root_id })
  part(l, '1', retry.id, 1, 'unsent')
  expect(l.chainOutcome(t.root_id)).toEqual({ delivered: 1, unsent: 3 })
  expect(l.chainOutcomeForInbound(iid)).toEqual({ delivered: 1, unsent: 3 })
  const lone = l.insertInbound({ ukey: 'x:lone', chatId: '1', kind: 'user', text: 'x', ts: 1 }).id
  expect(l.chainOutcomeForInbound(lone)).toEqual({ delivered: 0, unsent: 0 })
  expect(l.chainOutcomeForInbound(99_999)).toEqual({ delivered: 0, unsent: 0 })
})

test('unsentRows：只给属于回合的没发出内容段，按水位和时间过滤，带那串的来源与最大 round', () => {
  const l = new Ledger(':memory:')
  let t = 1_000
  l.now = () => t
  const a = chainSetup(l, '1', { source: 'commitment' })
  const old = part(l, '1', a.t.id, 1, 'unsent')
  t = 5_000
  part(l, '1', a.t.id, 2, 'sent')
  part(l, '1', a.t.id, 3, 'unsent', 'reaction')
  const p3 = part(l, '1', a.t.id, 4, 'unsent', 'photo')
  const b = chainSetup(l, '1', { source: 'outage_resend', round: 2 })
  const p4 = part(l, '1', b.t.id, 1, 'unsent')
  part(l, '1', null, 1, 'unsent') // /v1/send、通知主人：不属于回合
  part(l, '2', chainSetup(l, '2').t.id, 1, 'unsent')

  const all = l.unsentRows('1', 0, 0)
  expect(all.map(r => r.id)).toEqual([old, p3, p4])
  expect(all[0]).toMatchObject({ root_id: a.t.root_id, sources: ['commitment'], round: 0, text: '段1', state: 'unsent' })
  expect(all[1]!.file).toBe('/tmp/a.png')
  expect(all[2]).toMatchObject({ root_id: b.t.root_id, sources: ['outage_resend'], round: 2 })
  expect(l.unsentRows('1', old, 0).map(r => r.id)).toEqual([p3, p4]) // 水位之后
  expect(l.unsentRows('1', 0, 5_000).map(r => r.id)).toEqual([p3, p4]) // 时间之后
  expect(l.unsentRows('3', 0, 0)).toEqual([])
})

test('unsentRows：一轮带多条消息时来源去重合并、round 取最大；坏的 meta 不抛', () => {
  const l = new Ledger(':memory:')
  const i1 = l.insertInbound({ ukey: 'x:1', chatId: '1', kind: 'synthetic', text: 'a', ts: 1, meta: { source: 'outage_resend', round: 1 } }).id
  const i2 = l.insertInbound({ ukey: 'x:2', chatId: '1', kind: 'user', text: 'b', ts: 1 }).id
  const i3 = l.insertInbound({ ukey: 'x:3', chatId: '1', kind: 'synthetic', text: 'c', ts: 1, meta: { source: 'outage_resend', round: 3 } }).id
  l.db.query('UPDATE inbound SET meta = ? WHERE id = ?').run('{bad', i2)
  const seg = l.createSegment('1', 'tok', false)
  const t = l.startTurn({ chatId: '1', segmentId: seg.id, kind: 'message', inboundIds: [i1, i2, i3], attempt: 0 })
  part(l, '1', t.id, 1, 'unsent')
  expect(l.unsentRows('1', 0, 0)[0]).toMatchObject({ sources: ['outage_resend'], round: 3 })
})

test('chatsWithUnsent：有属于回合的没发出内容段的聊天，按时间过滤', () => {
  const l = new Ledger(':memory:')
  let t = 1_000
  l.now = () => t
  part(l, '1', chainSetup(l, '1').t.id, 1, 'unsent')
  t = 9_000
  part(l, '2', chainSetup(l, '2').t.id, 1, 'unsent')
  part(l, '3', chainSetup(l, '3').t.id, 1, 'unsent', 'reaction')
  part(l, '4', null, 1, 'unsent')
  part(l, '5', chainSetup(l, '5').t.id, 1, 'ambiguous')
  expect(l.chatsWithUnsent(0)).toEqual(['1', '2'])
  expect(l.chatsWithUnsent(5_000)).toEqual(['2'])
})
