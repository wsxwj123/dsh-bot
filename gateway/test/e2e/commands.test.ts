// 主人命令：/model、/provider、/compact、/help
import { afterEach, expect, test } from 'bun:test'
import { chmodSync, writeFileSync } from 'fs'
import { join } from 'path'
import { FakeTelegram } from '../fakes/fake-telegram'
import { cleanup, FRIEND, Gateway, makeBot, OWNER, prompts, readJsonl, toolResults, until, writeConfig, type BotEnv } from '../harness'

let tg: FakeTelegram | null = null
let b: BotEnv | null = null
let gw: Gateway | null = null

afterEach(async () => {
  await gw?.stop()
  tg?.stop()
  if (b) cleanup(b)
  tg = null; b = null; gw = null
})

const ROUTE = { api: 'openai-completions', baseURL: 'http://127.0.0.1:9/v1', apiKeyEnv: 'PROXY_KEY', models: [{ id: 'gemini-x' }, { id: 'gemini-y' }] }

async function setup(o: { creds?: string } = {}) {
  tg = new FakeTelegram()
  b = makeBot(tg, { gw: { burst_window_ms: 0 }, brain: { routes: { proxy: ROUTE } } })
  const cred = join(b.root, 'credentials.yaml')
  writeFileSync(cred, o.creds ?? 'version: 1\nrefs:\n  DEEPSEEK_API_KEY: TESTONLY-not-a-real-key\n')
  chmodSync(cred, 0o600)
  gw = new Gateway(b)
  await gw.start()
  return { tg, b, gw }
}

/** 发一条命令，等系统回复 */
async function cmd(tg: FakeTelegram, text: string, chat = OWNER): Promise<string> {
  const n = tg.sentTo(chat).length
  tg.pushText(chat, text)
  await until(() => tg.sentTo(chat).length > n, `reply to ${text}`)
  return tg.sentTo(chat).at(-1)!.text!
}

async function say(tg: FakeTelegram, text: string): Promise<void> {
  const mid = tg.pushText(OWNER, text)
  await until(() => tg.sentTo(OWNER).some(s => s.text === `收到：${text}`), `reply to ${text}`)
  // 等这一轮在账本里收尾：Windows 上停网关是硬杀，没收尾的轮重启后会按崩溃恢复重新处理
  await until(() => {
    const l = gw!.ledger()
    const st = l.inboundByKey(`tg:${OWNER}:${mid}`)?.state
    const busy = (l.db.query(`SELECT COUNT(*) AS n FROM turns WHERE state IN ('preparing','sent')`).get() as { n: number }).n
    l.close()
    return st === 'done' && busy === 0
  }, `turn settled for ${text}`)
}

const modelOf = (b: BotEnv, text: string) => prompts(b).find(p => p.text.includes(text))!.model

test('/help 列出命令；不是主人发的命令不理，也不交给模型', async () => {
  const { tg, b } = await setup()
  const help = await cmd(tg, '/help')
  for (const c of ['/model', '/provider', '/compact', '/clear']) expect(help).toContain(c)
  tg.pushText(FRIEND, '/model list')
  await until(() => { const l = gw!.ledger(); const r = l.db.query(`SELECT state FROM inbound WHERE chat_id = ? ORDER BY id DESC`).get(String(FRIEND)) as { state: string } | null; l.close(); return r?.state === 'dropped' }, 'dropped')
  expect(tg.sentTo(FRIEND).length).toBe(0)
  expect(prompts(b).some(p => p.text.includes('/model'))).toBe(false)
})

test('/model：看现在的、列出能换的、换模型（重启后保持）、换回配置文件里的', async () => {
  const { tg, b } = await setup()
  expect(await cmd(tg, '/model')).toContain('deepseek-official / deepseek-flash')
  const list = await cmd(tg, '/model list')
  expect(list).toContain('✅ deepseek-flash')
  expect(list).toContain('gemini-y')
  expect(await cmd(tg, '/model nope')).toContain('没有 nope 这个模型')
  expect(await cmd(tg, '/model deepseek-v4-pro')).toContain('已换成 deepseek-official / deepseek-v4-pro')
  await say(tg, '换了之后')
  expect(modelOf(b, '换了之后')).toBe(JSON.stringify(['deepseek-official', 'deepseek-v4-pro']))
  // 重启后保持
  await gw!.stop()
  gw = new Gateway(b)
  await gw.start()
  await say(tg, '重启之后')
  expect(modelOf(b, '重启之后')).toBe(JSON.stringify(['deepseek-official', 'deepseek-v4-pro']))
  expect(await cmd(tg, '/model')).toContain('配置文件里是 deepseek-official / deepseek-flash')
  expect(await cmd(tg, '/model default')).toContain('已换回配置文件里的模型')
  await say(tg, '换回之后')
  expect(modelOf(b, '换回之后')).toBe(JSON.stringify(['deepseek-official', 'deepseek-flash']))
})

test('/model 换过之后，配置文件里的模型又改了：以配置文件为准', async () => {
  const { tg, b } = await setup()
  await cmd(tg, '/model deepseek-v4-pro')
  b.brain = { ...b.brain, model: 'other-model' }
  writeConfig(b)
  await until(() => readJsonl<{ event: string }>(join(b.botDir, 'logs', 'gateway.log')).some(r => r.event === 'brain.override_cleared'), 'override cleared')
  await say(tg, '改了配置文件')
  expect(modelOf(b, '改了配置文件')).toBe(JSON.stringify(['deepseek-official', 'other-model']))
})

test('/provider：列出供应商和密钥情况（不出现密钥本身）；缺密钥不换；有密钥就换到它的第一个模型', async () => {
  const { tg, b } = await setup()
  const list = await cmd(tg, '/provider')
  expect(list).toContain('✅ deepseek-official：3 个模型，密钥已配')
  expect(list).toContain('proxy：2 个模型，缺密钥（凭据文件里填 PROXY_KEY）')
  expect(list).not.toContain('TESTONLY')
  expect(await cmd(tg, '/provider proxy')).toContain('还没配密钥')
  writeFileSync(join(b.root, 'credentials.yaml'), 'version: 1\nrefs:\n  DEEPSEEK_API_KEY: TESTONLY-not-a-real-key\n  PROXY_KEY: TESTONLY-proxy-key\n')
  expect(await cmd(tg, '/provider proxy')).toContain('已换成 proxy / gemini-x')
  expect(await cmd(tg, '/model proxy/gemini-y')).toContain('已换成 proxy / gemini-y')
  await say(tg, '用代理')
  expect(modelOf(b, '用代理')).toBe(JSON.stringify(['proxy', 'gemini-y']))
})

test('/compact：在旧会话里写摘要（工具锁住、带上主人交代的重点），新会话带着摘要和最近的原话', async () => {
  const { tg, b } = await setup()
  expect(await cmd(tg, '/compact')).toContain('还没聊什么')
  await say(tg, '压缩前第一句')
  await say(tg, '压缩前第二句')
  const old = prompts(b).find(p => p.text.includes('压缩前第二句'))!.sessionId
  const r = await cmd(tg, '/compact 记住猫的名字')
  expect(r).toContain('已把这段对话压缩成一份摘要')
  const sum = prompts(b).find(p => p.text.startsWith('⟦系统·整理记忆⟧'))!
  expect(sum.sessionId).toBe(old)
  expect(sum.text).toContain('记住猫的名字')
  expect(toolResults(b).find(t => t.sessionId === old && t.name === 'react')!.isError).toBe(true)
  await say(tg, '压缩后第一句')
  const p = prompts(b).find(x => x.text.includes('压缩后第一句'))!
  expect(p.sessionId).not.toBe(old)
  expect(p.text).toContain('假摘要#')
  expect(p.text).toContain('压缩前第二句') // 和 /clear 不同：最近的原话也带上
})

test('/compact 在网关重启之后：先续接旧会话，摘要照样在旧会话里写', async () => {
  const { tg, b } = await setup()
  await say(tg, '重启前第一句')
  await say(tg, '重启前第二句')
  const old = prompts(b).find(p => p.text.includes('重启前第二句'))!.sessionId
  await gw!.stop()
  gw = new Gateway(b)
  await gw.start()
  expect(await cmd(tg, '/compact')).toContain('已把这段对话压缩成一份摘要')
  expect(prompts(b).find(p => p.text.startsWith('⟦系统·整理记忆⟧'))!.sessionId).toBe(old)
})

test('本机接口 /v1/model（管理台用）：看模型、换模型、换回；要口令', async () => {
  const { tg, b, gw } = await setup()
  const auth = { authorization: `Bearer ${gw.apiToken()}`, 'content-type': 'application/json' }
  expect((await gw.api('/v1/model')).status).toBe(401)
  const info = await (await gw.api('/v1/model', { headers: auth })).json() as any
  expect(info.current).toEqual({ provider: 'deepseek-official', model: 'deepseek-flash' })
  expect(info.config).toEqual(info.current)
  expect(info.choices.some((c: any) => c.provider === 'proxy' && c.model === 'gemini-y')).toBe(true)
  const bad = await gw.api('/v1/model', { method: 'POST', headers: auth, body: JSON.stringify({ spec: 'nope' }) })
  expect(bad.status).toBe(400)
  expect(((await bad.json()) as any).text).toContain('没有 nope 这个模型')
  const ok = await (await gw.api('/v1/model', { method: 'POST', headers: auth, body: JSON.stringify({ spec: 'deepseek-v4-pro' }) })).json() as any
  expect(ok.ok).toBe(true)
  expect(ok.current.model).toBe('deepseek-v4-pro')
  await say(tg, '管理台换了之后')
  expect(modelOf(b, '管理台换了之后')).toBe(JSON.stringify(['deepseek-official', 'deepseek-v4-pro']))
  const back = await (await gw.api('/v1/model', { method: 'POST', headers: auth, body: JSON.stringify({ spec: 'default' }) })).json() as any
  expect(back.current).toEqual({ provider: 'deepseek-official', model: 'deepseek-flash' })
})
