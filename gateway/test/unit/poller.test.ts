// 收消息层的拦截钩子、按钮回调、入账前机密替换（方案 3.6、3.3.1、Q2、3.12）。真账本（内存）+ 假 Telegram
import { afterEach, expect, test } from 'bun:test'
import { mkdtempSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { Ledger } from '../../src/ledger'
import { Logger, registerSecret } from '../../src/log'
import { TelegramApi, type TgCallbackQuery, type TgMessage } from '../../src/telegram/api'
import { Poller } from '../../src/telegram/poller'
import { sleep } from '../../src/util'
import { FakeTelegram } from '../fakes/fake-telegram'

const OWNER = 42
class CapLog extends Logger {
  events: Record<string, unknown>[] = []
  override log(_level: string, event: string, fields: Record<string, unknown> = {}): void { this.events.push({ event, ...fields }) }
}

const cleanups: (() => Promise<void> | void)[] = []
afterEach(async () => { for (const f of cleanups.splice(0).reverse()) await f() })

function setup(hooks: Partial<ConstructorParameters<typeof Poller>[3]> = {}) {
  const tg = new FakeTelegram()
  const dir = mkdtempSync(join(tmpdir(), 'poller-'))
  writeFileSync(join(dir, 'access.json'), JSON.stringify({ allowFrom: [String(OWNER)] }))
  const ledger = new Ledger(':memory:')
  const log = new CapLog()
  const inbound: string[] = []
  const human: TgMessage[] = []
  const poller = new Poller(new TelegramApi(tg.token, tg.url, 5_000), ledger, log, {
    channelDir: dir, botId: tg.bot.id, pollTimeoutS: 1, onInbound: c => inbound.push(c), onFatal: () => {},
    onHumanMessage: m => human.push(m), ...hooks,
  })
  poller.start()
  cleanups.push(() => { tg.stop(); rmSync(dir, { recursive: true, force: true }) }, () => poller.stop())
  return { tg, ledger, log, inbound, human }
}

async function until(f: () => boolean, what: string, ms = 5_000) {
  const end = Date.now() + ms
  while (!f()) { if (Date.now() > end) throw new Error(`等不到：${what}`); await sleep(20) }
}
const ukey = (chat: number, mid: number) => `tg:${chat}:${mid}`

test('getUpdates 显式声明 message 和 callback_query 两类', async () => {
  const { tg } = setup()
  await until(() => tg.lastAllowedUpdates !== null, '第一次 getUpdates')
  expect([...tg.lastAllowedUpdates!].sort()).toEqual(['callback_query', 'message'])
})

test('按钮回调：先推进 offset 再交给 onCallback；不入账', async () => {
  const got: { cq: TgCallbackQuery; offsetAtCall: number }[] = []
  const s = setup({ onCallback: cq => { got.push({ cq, offsetAtCall: s.ledger.tgOffset() }) } })
  const { tg, ledger } = s
  const mid = tg.pushText(OWNER, '先来一条')
  await until(() => ledger.inboundByKey(ukey(OWNER, mid)) !== null, '普通消息入账')
  tg.pushCallback(OWNER, OWNER, 1234, 'w|abcdefgh|new')
  await until(() => got.length === 1, '回调交给钩子')
  expect(got[0]!.cq.data).toBe('w|abcdefgh|new')
  expect(got[0]!.cq.from.id).toBe(OWNER)
  expect(got[0]!.offsetAtCall).toBe(tg.lastUpdateId + 1)
  expect(ledger.db.query('SELECT COUNT(*) AS n FROM inbound').get()).toEqual({ n: 1 })
})

test('闸门之前的钩子：任何聊天、任何人都先过它；接走的不入账、不进群聊记录、不交给调度', async () => {
  const seen: number[] = []
  const s = setup({
    interceptBeforeGate: (msg, updateId) => {
      seen.push(msg.chat.id)
      if (!/^\/provider(@\S+)?\s+add(\s|$)/i.test(msg.text ?? '')) return false
      s.ledger.recordUpdate(updateId, null) // 接走者自己推进 offset（真实实现与状态同一事务）
      return true
    },
  })
  const { tg, ledger, inbound, human } = s
  const inGroup = tg.pushText(7777, '/provider add x https://a.example k3y-in-group', { chatId: -100123 })
  const fromStranger = tg.pushText(9999, '/provider add x https://a.example k3y-stranger')
  const fromOwner = tg.pushText(OWNER, '/provider add x https://a.example k3y-owner')
  await until(() => ledger.tgOffset() === tg.lastUpdateId + 1, 'offset 推进到最后')
  expect(seen).toEqual([-100123, 9999, OWNER])
  for (const [c, m] of [[-100123, inGroup], [9999, fromStranger], [OWNER, fromOwner]] as const) expect(ledger.inboundByKey(ukey(c, m))).toBeNull()
  expect(inbound).toEqual([])
  expect(human).toEqual([])
})

test('钩子返回 true 却没推进 offset：收消息层补上，同一条不会被反复拉到', async () => {
  let calls = 0
  const { tg, ledger } = setup({ interceptBeforeGate: () => { calls++; return true } })
  tg.pushText(OWNER, '随便什么')
  await until(() => ledger.tgOffset() === tg.lastUpdateId + 1, 'offset 推进')
  await sleep(300)
  expect(calls).toBe(1)
})

test('闸门之后的钩子：只看放行的消息；接走的不入账', async () => {
  const seen: string[] = []
  const s = setup({
    intercept: (msg, updateId) => {
      seen.push(msg.text ?? '')
      if (msg.text !== '/provider') return false
      s.ledger.recordUpdate(updateId, null)
      return true
    },
  })
  const { tg, ledger, inbound } = s
  tg.pushText(9999, '陌生人的话')
  const taken = tg.pushText(OWNER, '/provider')
  const normal = tg.pushText(OWNER, '你好')
  await until(() => ledger.inboundByKey(ukey(OWNER, normal)) !== null, '普通消息入账')
  expect(seen).toEqual(['/provider', '你好'])
  expect(ledger.inboundByKey(ukey(OWNER, taken))).toBeNull()
  expect(inbound).toEqual([String(OWNER)])
})

test('钩子抛错：当作接走（可能含密钥，宁可丢也不入账），记 inbound.dropped', async () => {
  const { tg, ledger, log, inbound } = setup({ intercept: () => { throw new Error('boom') } })
  const mid = tg.pushText(OWNER, 'k3y-could-be-secret-123')
  await until(() => ledger.tgOffset() === tg.lastUpdateId + 1, 'offset 推进')
  expect(ledger.inboundByKey(ukey(OWNER, mid))).toBeNull()
  expect(inbound).toEqual([])
  expect(log.events.find(e => e.event === 'inbound.dropped')).toMatchObject({ reason: 'intercept failed' })
})

test('入账前机密替换：正文和引用文字里登记过的密钥换成 ***，引用文字先换再截断（不留半截）', async () => {
  const key = 'Unit-Test-Key-9f8e7d6c5b4a3210-zz'
  registerSecret(key)
  const { tg, ledger, human } = setup()
  const quoted = `${'前'.repeat(100)}${key}`
  const mid = tg.pushText(OWNER, `看这条 ${key} 怎么办`, { replyTo: { message_id: 5, text: quoted } })
  await until(() => ledger.inboundByKey(ukey(OWNER, mid)) !== null, '入账')
  const row = ledger.inboundByKey(ukey(OWNER, mid))!
  expect(row.text).toBe('看这条 *** 怎么办')
  const meta = JSON.parse(row.meta!) as { reply_to: { text: string } }
  expect(meta.reply_to.text).toBe(`${'前'.repeat(100)}***`)
  expect(JSON.stringify(row)).not.toContain('Unit-Test-Key')
  expect(JSON.stringify(human)).not.toContain('Unit-Test-Key')
})
