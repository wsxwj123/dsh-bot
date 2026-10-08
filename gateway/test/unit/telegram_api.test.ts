// Telegram 客户端的按钮相关方法与删消息重试（方案 3.3.2、3.11）。对着假 Telegram 跑，它按真实 Bot API 的限制与报错文案回应
import { afterEach, expect, test } from 'bun:test'
import { deleteMessageWithRetry, isNotModified, TelegramApi, TgApiError } from '../../src/telegram/api'
import { FakeTelegram } from '../fakes/fake-telegram'

const OWNER = 42
const stops: (() => void)[] = []
afterEach(() => { for (const f of stops.splice(0)) f() })
function fake() {
  const tg = new FakeTelegram()
  stops.push(() => tg.stop())
  return { tg, api: new TelegramApi(tg.token, tg.url, 5_000) }
}
const kb = (...labels: string[]) => ({ inline_keyboard: [labels.map((l, i) => ({ text: l, callback_data: `w|abcdefgh|a${i}` }))] })
const waits = () => { const w: number[] = []; return { w, wait: async (ms: number) => { w.push(ms) } } }

test('发带按钮的菜单、编辑文字和按钮、不给按钮就去掉；内容没变报 not modified', async () => {
  const { tg, api } = fake()
  const m = await api.sendMessage(String(OWNER), '【系统】菜单', { replyMarkup: kb('新建', '关闭') })
  expect(tg.sent[0]!.replyMarkup).toEqual(kb('新建', '关闭'))
  expect(tg.buttonsOf(OWNER, m.message_id).map(b => b.text)).toEqual(['新建', '关闭'])
  await api.editMessageText(String(OWNER), m.message_id, '【系统】第二步', { replyMarkup: kb('取消') })
  expect(tg.lastMenu(OWNER)).toMatchObject({ text: '【系统】第二步', buttons: [{ text: '取消' }] })
  const same = await api.editMessageText(String(OWNER), m.message_id, '【系统】第二步', { replyMarkup: kb('取消') }).catch(e => e)
  expect(isNotModified(same)).toBe(true)
  await api.editMessageReplyMarkup(String(OWNER), m.message_id)
  expect(tg.buttonsOf(OWNER, m.message_id)).toEqual([])
  await api.editMessageText(String(OWNER), m.message_id, '已取消。')
  expect(tg.textOf(OWNER, m.message_id)).toBe('已取消。')
})

test('回应按钮：带文字或不带；同一个回调第二次回应被 Telegram 拒绝', async () => {
  const { tg, api } = fake()
  const a = tg.pushCallback(OWNER, OWNER, 1, 'w|abcdefgh|x')
  const b = tg.pushCallback(OWNER, OWNER, 1, 'w|abcdefgh|y')
  await api.answerCallbackQuery(a, '正在拉取模型列表…')
  await api.answerCallbackQuery(b)
  expect(tg.answers.map(x => [x.id, x.text])).toEqual([[a, '正在拉取模型列表…'], [b, undefined]])
  const again = await api.answerCallbackQuery(a).catch(e => e)
  expect(again).toBeInstanceOf(TgApiError)
  expect((again as TgApiError).status).toBe(400)
})

test('删消息：成功；找不到、太旧（超过 48 小时）不重试，返回 ok:false 和脱敏后的原因', async () => {
  const { tg, api } = fake()
  const mid = tg.pushText(OWNER, 'sk-secret-message')
  expect(await deleteMessageWithRetry(api, String(OWNER), mid)).toEqual({ ok: true })
  expect(tg.isDeleted(OWNER, mid)).toBe(true)
  const gone = await deleteMessageWithRetry(api, String(OWNER), mid)
  expect(gone.ok).toBe(false)
  expect(!gone.ok && gone.err).toContain('message to delete not found')
  const old = tg.pushText(OWNER, 'old one')
  tg.markTooOld(OWNER, old)
  const before = tg.calls.length
  expect((await deleteMessageWithRetry(api, String(OWNER), old)).ok).toBe(false)
  expect(tg.calls.length - before).toBe(1)
})

test('删消息：429 按 retry_after 等（单次最多 5 秒），最多再试 2 次', async () => {
  const { tg, api } = fake()
  const mid = tg.pushText(OWNER, 'k')
  tg.faults.push({ method: 'deleteMessage', times: 2, status: 429, description: 'Too Many Requests: retry after 30', retryAfter: 30 })
  const a = waits()
  expect(await deleteMessageWithRetry(api, String(OWNER), mid, a.wait)).toEqual({ ok: true })
  expect(a.w).toEqual([5_000, 5_000])
  const mid2 = tg.pushText(OWNER, 'k2')
  tg.faults.push({ method: 'deleteMessage', times: 3, status: 429, description: 'Too Many Requests', retryAfter: 1 })
  const b = waits()
  expect((await deleteMessageWithRetry(api, String(OWNER), mid2, b.wait)).ok).toBe(false)
  expect(b.w).toEqual([1_000, 1_000])
  expect(tg.isDeleted(OWNER, mid2)).toBe(false)
})

test('删消息：网络层出错（回包读不出、合法 JSON 但不是对象）再试 2 次后返回 ok:false，不抛', async () => {
  let n = 0
  const srv = Bun.serve({ port: 0, hostname: '127.0.0.1', fetch: () => (++n % 2 ? new Response('<html>bad gateway') : new Response('null', { status: 502 })) })
  stops.push(() => srv.stop(true))
  const api = new TelegramApi('123:fake', `http://127.0.0.1:${srv.port}`, 2_000)
  const a = waits()
  const r = await deleteMessageWithRetry(api, String(OWNER), 1, a.wait)
  expect(r.ok).toBe(false)
  expect(!r.ok && r.err).toContain('TgNetworkError')
  expect(a.w).toEqual([1_000, 1_000])
  expect(n).toBe(3)
})

test('删消息：上一次回包读不出（可能已删成），重试时报找不到 → 算删掉了', async () => {
  let n = 0
  const srv = Bun.serve({ port: 0, hostname: '127.0.0.1', fetch: () => (++n === 1 ? new Response('<html>bad gateway') : Response.json({ ok: false, error_code: 400, description: 'Bad Request: message to delete not found' }, { status: 400 })) })
  stops.push(() => srv.stop(true))
  const api = new TelegramApi('123:fake', `http://127.0.0.1:${srv.port}`, 2_000)
  const a = waits()
  expect(await deleteMessageWithRetry(api, String(OWNER), 1, a.wait)).toEqual({ ok: true })
  expect(n).toBe(2)
})
