// 真 dsh + 假模型：在真实 harness 上核对补丁层、隐私字段、工具挂载、按会话换模型。
// 需要一份装好的 dsh：设置 DSH_BOT_HARNESS=<目录>（目录下有 node_modules/@deepseek-ai/dsh），没有就跳过。
import { afterAll, beforeAll, expect, test } from 'bun:test'
import { chmodSync, existsSync, readFileSync, writeFileSync } from 'fs'
import { join, resolve } from 'path'
import { isAlive } from '../../src/dsh/process'
import { startFakeLlm, type FakeLlm } from '../../../lab/dsh/lib/fake-llm'
import { FakeTelegram } from '../fakes/fake-telegram'
import { cleanup, Gateway, makeBot, OWNER, sleep, until, writeConfig, type BotEnv } from '../harness'

const harness = process.env.DSH_BOT_HARNESS ? resolve(process.env.DSH_BOT_HARNESS) : ''
const available = !!harness && existsSync(join(harness, 'node_modules', '@deepseek-ai', 'dsh', 'lib', 'bin.js'))

let tg: FakeTelegram
let llm: FakeLlm
let b: BotEnv
let gw: Gateway

beforeAll(async () => {
  if (!available) return
  tg = new FakeTelegram()
  llm = startFakeLlm({
    port: 0,
    script: [
      { match: 'realA', tool: { name: 'mcp__tg__reply', arguments: { text: '真的收到了\n\n第二段' } } },
      { match: '已送达', text: '' },
      // 换模型后 dsh 会在用户消息后面插一条英文说明 "[model changed: …]"，所以最后一条用户消息是它
      { match: 'model changed', tool: { name: 'mcp__tg__reply', arguments: { text: '换了模型' } } },
      { match: '已送达', text: '' },
      // 换到 /provider add 建的供应商：同样是"[model changed: … continues with fake2/…]"在最后
      { match: 'continues with fake2', tool: { name: 'mcp__tg__reply', arguments: { text: '新供应商回的' } } },
      { match: '已送达', text: '' },
      { match: '⟦系统·整理记忆⟧', text: '【我说过的要紧话】说过"真的收到了"\n【我答应过的事】无\n【对方的情况】对方在测试\n【正在聊的话题】换段\n【我们现在的关系和气氛】轻松（真dsh摘要）' },
    ],
  })
  b = makeBot(tg, {
    gw: { dsh_command: undefined, burst_window_ms: 0 },
    brain: {
      provider: 'fake', model: 'fake-chat',
      routes: { fake: { api: 'openai-completions', baseURL: `http://127.0.0.1:${llm.port}/v1`, apiKeyEnv: 'FAKE_LLM_KEY', retryPolicy: { mode: 'normal', maxRetries: 0 }, models: [{ id: 'fake-chat', contextWindow: 1000000 }, { id: 'fake-chat-2', contextWindow: 500000 }] } },
    },
  })
  delete (b.gw as Record<string, unknown>).dsh_command
  writeConfig(b)
  const cred = join(b.root, 'credentials.yaml')
  writeFileSync(cred, 'version: 1\nrefs:\n  FAKE_LLM_KEY: fake-key-for-tests\n')
  chmodSync(cred, 0o600)
  gw = new Gateway(b)
  await gw.start({ DSH_BOT_HARNESS: harness })
}, 60_000)

afterAll(async () => {
  if (!available) return
  await gw.stop()
  llm.stop()
  tg.stop()
  cleanup(b)
})

test.skipIf(!available)('真 dsh：只留人设、只有我们自己的 10 个工具、不带隐私字段，回复能发出去', async () => {
  tg.pushText(OWNER, '你好 realA')
  await until(() => tg.sentTo(OWNER).some(s => s.text === '第二段'), 'reply via real dsh', 60_000)
  // 工具结果回到模型那里（逐段送达情况），这一轮才算结束
  await until(() => llm.requests.find(r => JSON.stringify(r.body.messages).includes('已送达 2/2 段')), 'tool result reached the model', 30_000)
  await until(() => { const l = gw.ledger(); const ok = l.activeSegment(String(OWNER))?.used_tokens; l.close(); return ok }, 'turn finished', 30_000)
  expect(tg.sentTo(OWNER).map(s => s.text)).toEqual(['真的收到了', '第二段'])
  const first = llm.requests.find(r => JSON.stringify(r.body.messages).includes('realA'))!
  const body = first.body
  const system = body.messages.find((m: any) => m.role === 'system')
  const systemText = typeof system.content === 'string' ? system.content : system.content.map((c: any) => c.text).join('')
  expect(systemText.startsWith('# 测试人设')).toBe(true)
  expect(systemText).toContain('{⁠{user}}')
  expect(systemText).toContain('运行规则')
  expect(systemText).not.toContain('MCP resource')
  expect(body.tools.map((t: any) => t.function.name).sort()).toEqual(['mcp__tg__commitment_cancel', 'mcp__tg__commitment_create', 'mcp__tg__commitment_list', 'mcp__tg__generate_image', 'mcp__tg__image_guide', 'mcp__tg__moments', 'mcp__tg__react', 'mcp__tg__remember', 'mcp__tg__reply', 'mcp__tg__stay_silent'])
  const raw = JSON.stringify(body)
  expect(raw).not.toContain('dsh_session_log')
  expect(raw).not.toContain('dsh_plugin_packages')
  const led = gw.ledger()
  const seg = led.activeSegment(String(OWNER))!
  expect(seg.window).toBe(1000000)
  expect(seg.used_tokens).toBeGreaterThan(0)
  led.close()
})

test.skipIf(!available)('真 dsh：dsh 进程环境里没有 Telegram 令牌', async () => {
  if (process.platform !== 'linux') return
  const pid = JSON.parse(readFileSync(join(b.botDir, 'state', 'dsh.pid'), 'utf8')).pid
  const env = readFileSync(`/proc/${pid}/environ`, 'utf8')
  expect(env).not.toContain(tg.token)
  expect(env).not.toContain('TELEGRAM')
  expect(env).not.toContain('fake-key-for-tests')
})

test.skipIf(!available)('真 dsh：改配置换模型，下一轮请求就用新模型，进程不重启', async () => {
  const pid = JSON.parse(readFileSync(join(b.botDir, 'state', 'dsh.pid'), 'utf8')).pid
  b.brain = { ...b.brain, model: 'fake-chat-2' }
  writeConfig(b)
  await sleep(500)
  tg.pushText(OWNER, 'realB')
  await until(() => tg.sentTo(OWNER).some(s => s.text === '换了模型'), 'reply after switch', 60_000)
  const req = llm.requests.find(r => JSON.stringify(r.body.messages).includes('realB'))!
  expect(req.body.model).toBe('fake-chat-2')
  expect(JSON.stringify(req.body.messages)).toContain('[model changed:')
  expect(JSON.parse(readFileSync(join(b.botDir, 'state', 'dsh.pid'), 'utf8')).pid).toBe(pid)
})

test.skipIf(!available)('真 dsh：用量快到线时来了新消息，先在旧会话里写摘要换段，新消息进新会话、开头带上摘要', async () => {
  const led = gw.ledger()
  const used = led.activeSegment(String(OWNER))!.used_tokens!
  led.close()
  // 把预算压到只比现在多一点：下一条消息一进来就会超
  b.brain = { ...b.brain, model: 'fake-chat', max_input_tokens: used + 20 }
  writeConfig(b)
  await sleep(500)
  tg.pushText(OWNER, 'realD')
  await until(() => llm.requests.some(r => JSON.stringify(r.body.messages).includes('realD')), 'realD processed', 60_000)
  const sumReq = llm.requests.find(r => JSON.stringify(r.body.messages).includes('⟦系统·整理记忆⟧'))!
  expect(sumReq).toBeDefined()
  const sumMsgs = JSON.stringify(sumReq.body.messages)
  expect(sumMsgs).toContain('realB') // 在旧会话里写：前面的对话都在
  expect(sumMsgs).not.toContain('realD')
  const next = llm.requests.find(r => JSON.stringify(r.body.messages).includes('realD'))!
  const users = next.body.messages.filter((m: any) => m.role === 'user')
  expect(users.length).toBe(1) // 新会话：前情和新消息是同一条用户消息里的两个内容块
  const first = JSON.stringify(users[0].content)
  expect(first).toContain('⟦之前聊天的交接摘要')
  expect(first).toContain('真dsh摘要')
  expect(first).toContain('你：换了模型') // 最近原话里有 bot 真正发出去的话
  expect(first).not.toContain('⟦系统·整理记忆⟧')
  b.brain = { ...b.brain, max_input_tokens: 1_000_000 }
  writeConfig(b)
  await sleep(500)
})

test.skipIf(!available)('真 dsh：/provider add 建的 Anthropic 接口供应商进了路由；切过去以后按它的协议、带凭据文件里的密钥发请求', async () => {
  const key = 'sk-TESTONLY-real-dsh-provider-key'
  tg.pushText(OWNER, `/provider add Fake2 http://127.0.0.1:${llm.port} ${key}`)
  await until(() => tg.sentTo(OWNER).some(s => (s.text ?? '').includes('已添加供应商「Fake2」，拉到 1 个模型')), 'provider added', 30_000)
  tg.pushText(OWNER, '/provider Fake2')
  await until(() => tg.sentTo(OWNER).some(s => (s.text ?? '').includes('已换成 fake2 / fake-chat')), 'switched to the new provider', 60_000)
  tg.pushText(OWNER, 'realP')
  await until(() => tg.sentTo(OWNER).some(s => s.text === '新供应商回的'), 'reply via the new provider', 60_000)
  const req = llm.requests.find(r => JSON.stringify(r.body.messages).includes('realP'))!
  expect(req.path).toBe('/v1/messages')
  expect(req.headers['x-api-key']).toBe(`${key.slice(0, 10)}…`)
  tg.pushText(OWNER, '/model default')
  await until(() => tg.sentTo(OWNER).some(s => (s.text ?? '').includes('已换回配置文件里的模型')), 'back to config model', 30_000)
  expect(readFileSync(join(b.root, 'credentials.yaml'), 'utf8')).toContain('PROVIDER_FAKE2_API_KEY')
})

test.skipIf(!available)('真 dsh：网关被强杀后，旧 dsh 不会一直留着（自己退出，或者网关重启时被清理）', async () => {
  const pid = JSON.parse(readFileSync(join(b.botDir, 'state', 'dsh.pid'), 'utf8')).pid
  expect(isAlive(pid)).toBe(true)
  // 空闲的 dsh 标准输入一关就退出；正在处理一轮的，会先把这一轮做完（期间调我们的工具都会失败，发不出任何消息）。
  // Windows 上子进程不会因为父进程退出而结束。所以统一用"重启网关后旧进程一定不在了"来验收。
  await gw.kill()
  await gw.start({ DSH_BOT_HARNESS: harness })
  await until(() => !isAlive(pid), 'old dsh is gone after the gateway restarts', 20_000)
  tg.pushText(OWNER, 'realC')
  await until(() => llm.requests.some(r => JSON.stringify(r.body.messages).includes('realC')), 'new dsh works', 60_000)
})
