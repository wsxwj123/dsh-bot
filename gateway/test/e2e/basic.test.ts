// M1 验收 1、2、9：收发、账本三类记录、连发合并、环境变量与日志里没有令牌
import { afterAll, beforeAll, expect, test } from 'bun:test'
import { readdirSync, readFileSync } from 'fs'
import { join } from 'path'
import { FakeTelegram } from '../fakes/fake-telegram'
import { cleanup, envDumps, Gateway, makeBot, OWNER, prompts, sleep, toolResults, until, type BotEnv } from '../harness'

let tg: FakeTelegram
let b: BotEnv
let gw: Gateway

beforeAll(async () => {
  tg = new FakeTelegram()
  b = makeBot(tg)
  gw = new Gateway(b)
  await gw.start()
})

afterAll(async () => {
  await gw.stop()
  tg.stop()
  cleanup(b)
})

test('一条消息收到一条回复，账本里收到、开轮、发送三类记录齐全', async () => {
  const mid = tg.pushText(OWNER, '你好呀 alpha')
  await until(() => tg.sentTo(OWNER).find(s => s.text === '收到：你好呀 alpha'), 'reply')
  // 回复发出去之后，这一轮还要等模型收尾才算结束
  await until(() => { const l = gw.ledger(); const st = l.inboundByKey(`tg:${OWNER}:${mid}`)?.state; l.close(); return st === 'done' }, 'turn settled')
  const led = gw.ledger()
  const inb = led.inboundByKey(`tg:${OWNER}:${mid}`)!
  expect(inb.state).toBe('done')
  const turn = led.turn(inb.turn_id!)!
  expect(turn.state).toBe('ok')
  const out = led.outboundForTurn(turn.id)
  expect(out.map(o => [o.kind, o.state, o.text])).toEqual([['text', 'sent', '收到：你好呀 alpha']])
  led.close()
  // 模型看到的格式：时间标注 + 消息编号 + 正文
  const p = prompts(b).find(x => x.text.includes('你好呀 alpha'))!
  expect(p.text).toMatch(new RegExp(`⟦\\d{2}-\\d{2} 周. \\S+ \\d{2}:\\d{2} · #${mid}⟧\\n你好呀 alpha`))
  // 新会话的第一轮：提醒和新消息是两个内容块
  expect(p.blocks).toBe(2)
  expect(p.text).toContain('新的会话从这里开始')
  // 已读回执
  expect(tg.reactions.some(r => r.messageId === mid && r.emoji === '👀')).toBe(true)
})

test('回复途中连发 3 条，合并成 1 轮', async () => {
  const before = prompts(b).length
  tg.pushText(OWNER, '先慢一点 !slow:1200')
  await until(() => prompts(b).length > before, 'first prompt')
  tg.pushText(OWNER, '第一条 burst1')
  tg.pushText(OWNER, '第二条 burst2')
  tg.pushText(OWNER, '第三条 burst3')
  await until(() => tg.sentTo(OWNER).find(s => s.text?.includes('burst3')), 'merged reply')
  const after = prompts(b).slice(before)
  expect(after.length).toBe(2)
  expect(after[1]!.text).toContain('burst1')
  expect(after[1]!.text).toContain('burst2')
  expect(after[1]!.text).toContain('burst3')
})

test('人设里的 {{ 被转义，运行规则在人设后面，隐私相关的行都禁用', () => {
  const rows = JSON.parse(readFileSync(join(b.acpState, 'patch-seen.json'), 'utf8')) as any[]
  const sp = rows.find(r => r.id === 'system-prompt')
  expect(sp.config.personaPrefix).toContain('{⁠{user}}')
  expect(sp.config.personaPrefix).not.toContain('{{')
  expect(sp.config.includeHarnessIdentity).toBe(false)
  expect(sp.config.personaSuffix).toContain('mcp__tg__reply')
  for (const id of ['session-log-deepseek', 'plugin-package-inventory-deepseek', 'session-telemetry-otel', 'mcp-resources', 'agent-instructions']) {
    expect(rows.some(r => r.id === id && r.disabled === true)).toBe(true)
  }
  expect(rows.find(r => r.id === 'compaction-basic').config).toEqual({ auto: false })
})

test('dsh 进程的环境里没有 Telegram 令牌、没有网关的本机口令', () => {
  const dumps = envDumps(b)
  expect(dumps.length).toBeGreaterThan(0)
  const apiKey = gw.apiToken()
  for (const env of dumps) {
    const all = JSON.stringify(env)
    expect(all).not.toContain(tg.token)
    expect(all).not.toContain(apiKey)
    expect(env.DSH_TELEMETRY_DISABLED).toBe('1')
    expect(env.DSH_HOME).toBe(join(b.botDir, 'dsh-home'))
    expect(Object.keys(env).some(k => k.startsWith('TELEGRAM'))).toBe(false)
  }
})

test('日志里没有令牌', () => {
  const dir = join(b.botDir, 'logs')
  const files = readdirSync(dir)
  expect(files).toContain('gateway.log')
  for (const f of files) {
    const text = readFileSync(join(dir, f), 'utf8')
    expect(text).not.toContain(tg.token)
    expect(text).not.toContain(tg.token.split(':')[1]!)
  }
})

test('同一轮里模型把同样的话再发一遍：拦住，用户只收到一次', async () => {
  tg.pushText(OWNER, 'dupP !parts:2 !dup')
  await until(() => toolResults(b).filter(r => r.name === 'reply').some(r => r.text.includes('没有重复发送')), 'second call blocked')
  await sleep(300)
  expect(tg.sentTo(OWNER).filter(s => s.text?.includes('dupP')).map(s => s.text)).toEqual(['收到：dupP（第1段）', '收到：dupP（第2段）'])
})

test('只有个别段重复：跳过重复的段，其余照发', async () => {
  tg.pushText(OWNER, 'dupQ !parts:2 !dupsome')
  await until(() => tg.sentTo(OWNER).some(s => s.text === '新的一段乙'), 'second call partly sent')
  await sleep(300)
  expect(tg.sentTo(OWNER).filter(s => s.text?.includes('dupQ') || s.text?.startsWith('新的一段')).map(s => s.text))
    .toEqual(['收到：dupQ（第1段）', '收到：dupQ（第2段）', '新的一段甲', '新的一段乙'])
  await until(() => toolResults(b).some(r => r.name === 'reply' && r.text.includes('第 1 段这一轮已经发过（或正在发）')), 'duplicate part reported to the model')
})

test('回复之后又说"不回复"：不算沉默', async () => {
  const mid = tg.pushText(OWNER, 'silR !silentafter')
  await until(() => toolResults(b).some(r => r.name === 'stay_silent' && r.text.includes('已经回复过')), 'silent ignored')
  await until(() => { const l = gw.ledger(); const st = l.inboundByKey(`tg:${OWNER}:${mid}`)?.state; l.close(); return st === 'done' }, 'turn settled')
  const led = gw.ledger()
  const t = led.turn(led.inboundByKey(`tg:${OWNER}:${mid}`)!.turn_id!)!
  expect(t.silent).toBe(0)
  led.close()
})
