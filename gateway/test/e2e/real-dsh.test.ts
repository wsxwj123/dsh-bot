// 真 dsh + 假模型：在真实 harness 上核对补丁层、隐私字段、工具挂载、按会话换模型。
// 需要一份装好的 dsh：设置 DSH_BOT_HARNESS=<目录>（目录下有 node_modules/@deepseek-ai/dsh），没有就跳过。
import { afterAll, beforeAll, expect, test } from 'bun:test'
import { chmodSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'fs'
import { join, resolve } from 'path'
import { isAlive, processCommandLine } from '../../src/dsh/process'
import { startFakeLlm, type FakeLlm } from '../../../lab/dsh/lib/fake-llm'
import { FakeTelegram } from '../fakes/fake-telegram'
import { cleanup, Gateway, makeBot, OWNER, sleep, until, writeConfig, type BotEnv } from '../harness'
import { rmSync } from 'fs'
import * as acc from '../acceptance/_acc'
import { startFakeUpstream, type UpstreamMode } from './_fake-upstream'

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
  // 放一个技能，让技能目录非空：Windows 上 dsh 会自动加载 sandbox-windows-acl 沙箱插件，
  // 该插件自带技能 diagnose-windows-sandbox-acl，于是会话第一轮 dsh 会把技能目录作为一条
  // <system-reminder> 开头的 user 消息追加在用户消息之后（macOS/Linux 没这个插件、默认不出现）。
  // 三个平台都放一个技能，把这条 Windows 上必然出现的路径在所有平台覆盖到：假模型的剧本匹配
  // 必须越过这条注入消息（见 lab/dsh/lib/fake-llm.ts 的 lastUserText），否则第一轮匹配不到用户文本。
  mkdirSync(join(b.channelDir, 'skills', 'probe'), { recursive: true })
  writeFileSync(join(b.channelDir, 'skills', 'probe', 'SKILL.md'), '---\nname: probe\ndescription: 测试用探针技能（只用于让技能目录非空）\n---\n\n正文不会被加载。\n')
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

/**
 * 用例失败时把现场打出来。CI 上这个 job 一轮要十几分钟，失败却只剩一句"超时"太贵。
 * 带出：dsh 起没起（pid/存活/命令行）、假模型收到过什么请求（有请求=起了但没走到回复，
 * 一个都没有=dsh 或上游根本没动）、假 Telegram 收到什么、dsh 的 stderr、网关日志尾部。
 * 只打印测试临时目录里的东西（假 key/假令牌），不涉及真实家目录。
 */
async function dumpCrash(reason: unknown): Promise<void> {
  const fileTail = (file: string, n: number): string => {
    try {
      const s = readFileSync(file, 'utf8').trimEnd()
      if (!s) return '(空文件)'
      return s.split('\n').slice(-n).map(l => (l.length > 500 ? `${l.slice(0, 500)}…(截断)` : l)).join('\n')
    } catch (e) {
      const msg = String((e as Error)?.message ?? e)
      return /ENOENT/.test(msg) ? '(文件不存在：没有输出)' : `(读不到：${msg})`
    }
  }
  console.error(`\n──── 真 dsh 失败现场 ──── ${reason instanceof Error ? `${reason.name}: ${reason.message}` : String(reason)}`)
  try {
    const rec = JSON.parse(readFileSync(join(b.botDir, 'state', 'dsh.pid'), 'utf8')) as { pid?: number }
    const pid = Number(rec.pid)
    console.error(`[dsh] pid 文件: ${JSON.stringify(rec)} 存活=${pid > 0 ? isAlive(pid) : '?'} 命令行=${pid > 0 ? await processCommandLine(pid) : '(无 pid)'}`)
  } catch (e) {
    console.error(`[dsh] pid 文件读不到（dsh 可能根本没启动）: ${String((e as Error)?.message ?? e).slice(0, 120)}`)
  }
  if (llm.requests.length === 0) {
    console.error('[假模型] 一个请求都没收到：dsh 没起，或起了但没去请求模型')
  } else {
    console.error(`[假模型] 收到 ${llm.requests.length} 个请求：`)
    for (const r of llm.requests) {
      const us = (r.body?.messages ?? []).filter((m: any) => m.role === 'user')
      const last = JSON.stringify(us.at(-1)?.content ?? '')
      console.error(`  #${r.n} ${r.at} model=${r.body?.model} 最后一条user=${last.slice(0, 200)}`)
    }
  }
  console.error(`[假 Telegram] 发给主人: ${JSON.stringify(tg.sentTo(OWNER).map(s => s.text ?? `(${s.method})`))}`)
  console.error(`[dsh stderr 尾部]\n${fileTail(join(b.botDir, 'logs', 'dsh-stderr.log'), 40)}`)
  console.error(`[网关日志尾部]\n${fileTail(join(b.botDir, 'logs', 'gateway.log'), 80)}`)
  console.error(`[网关进程 stderr 尾部]\n${gw.stderr.trimEnd().split('\n').slice(-40).join('\n') || '(空)'}`)
  console.error('──── 现场结束 ────\n')
}

test.skipIf(!available)('真 dsh：只留人设、只有我们自己的 10 个工具、不带隐私字段，回复能发出去', async () => {
  tg.pushText(OWNER, '你好 realA')
  try {
    // Windows CI 上真 dsh 首轮冷启动明显更慢（实测有 60 秒还起不来一轮的），放宽到 120 秒；
    // 断言本身没变——等的还是那两段回复。
    await until(() => tg.sentTo(OWNER).some(s => s.text === '第二段'), 'reply via real dsh', 120_000)
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
    // 除我们自己的 10 个工具外，还有 dsh 的 skill 工具：人设分层后按需技能要它来加载
    // （技能根只指到本 bot 的 skills 目录，见 profile.ts 的 skill-filesystem 行）。
    expect(body.tools.map((t: any) => t.function.name).sort()).toEqual(['mcp__tg__commitment_cancel', 'mcp__tg__commitment_create', 'mcp__tg__commitment_list', 'mcp__tg__generate_image', 'mcp__tg__image_guide', 'mcp__tg__moments', 'mcp__tg__react', 'mcp__tg__remember', 'mcp__tg__reply', 'mcp__tg__stay_silent', 'mcp__tg__sticker', 'skill'])
    const raw = JSON.stringify(body)
    expect(raw).not.toContain('dsh_session_log')
    expect(raw).not.toContain('dsh_plugin_packages')
    const led = gw.ledger()
    const seg = led.activeSegment(String(OWNER))!
    expect(seg.window).toBe(1000000)
    expect(seg.used_tokens).toBeGreaterThan(0)
    led.close()
  } catch (e) {
    // 超时/断言失败都把现场带出来（见 dumpCrash）：CI 一轮十几分钟，别让下一个人只能猜。
    await dumpCrash(e)
    throw e
  }
}, 240_000) // 用例级超时必须比内部最长的等待链更宽：三段 until 最坏等满 120+30+30=180 秒。
// 命令行 --timeout（CI 里是 90000）只是默认值，这里不写第三参数的话，用例会被它先砍掉，内部的等待根本走不完。

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
}, 120_000) // 用例级超时：内部最长等 60 秒（等新模型的回复）+ 0.5 秒配置生效延迟，留够余量

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
  // 新会话：前情和新消息是同一条用户消息里的两个内容块，所以用户消息应只有 1 条。
  // Windows 上 dsh 会自动加载 @deepseek-ai/dsh-sandbox-windows-acl 插件，它往第一轮再追一条
  // 装着技能清单的 <system-reminder> 消息（macOS/Linux 没有这个插件，是平台固有差异、
  // 不是产品差异）。放行这一条，但形状仍钉死：只接受「摘要」或「摘要 + 至多一条提醒」，
  // 出现别的块（第二条摘要、非提醒的追加、顺序颠倒、一条都没有）都失败。失败时把实际内容
  // 带出来，便于分辨是真差异还是环境噪声。
  const textOf = (m: any): string => {
    if (typeof m.content === 'string') return m.content
    if (Array.isArray(m.content)) return m.content.map((c: any) => typeof c === 'string' ? c : String(c?.text ?? '')).join('')
    return String(m.content)
  }
  const shape = users.map((m: any) => textOf(m).trimStart().startsWith('<system-reminder>') ? '提醒' : '摘要').join('+')
  expect(shape, `新会话第一轮的用户消息形状应为「摘要」或「摘要+提醒」，实际 ${users.length} 条：` +
    JSON.stringify(users.map((m: any) => textOf(m).slice(0, 200))))
    .toMatch(/^摘要(\+提醒)?$/)
  const first = JSON.stringify(users[0].content)
  expect(first).toContain('⟦之前聊天的交接摘要')
  expect(first).toContain('真dsh摘要')
  expect(first).toContain('你：换了模型') // 最近原话里有 bot 真正发出去的话
  expect(first).not.toContain('⟦系统·整理记忆⟧')
  b.brain = { ...b.brain, max_input_tokens: 1_000_000 }
  writeConfig(b)
  await sleep(500)
}, 120_000) // 用例级超时：内部最长等 60 秒（等 realD 被处理），留够余量

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
}, 180_000) // 用例级超时：内部最坏 20（网关重启就绪）+20（等旧 dsh 退出）+60 秒（等新 dsh 干活）=100 秒

// ─── 模型回空内容时发给上游的请求次数（INTERFACE-dsh修补 1.3） ───
// 每条用例自己起一套环境：假 Telegram + 网关 + 真 dsh + 本机假模型服务。路由不手写 retryPolicy（用网关补上的那一个）。
// 对方用 FRIEND 发消息，主人（OWNER）聊天里出现的只会是系统通知。

type UpApi = 'openai-completions' | 'openai-responses' | 'anthropic-messages'
type UpCtx = { tg: FakeTelegram; b: acc.BotEnv; up: ReturnType<typeof startFakeUpstream> }

async function withUpstream(api: UpApi, mode: UpstreamMode, fn: (c: UpCtx) => Promise<void>): Promise<void> {
  const tg2 = new FakeTelegram()
  const up = startFakeUpstream(mode)
  const base = `http://127.0.0.1:${up.port}`
  const bot = acc.makeBot(tg2, {
    gw: { burst_window_ms: 0 },
    brain: { provider: 'up', model: 'up-chat', routes: { up: { api, baseURL: api === 'anthropic-messages' ? base : `${base}/v1`, apiKeyEnv: 'UP_KEY', models: [{ id: 'up-chat', contextWindow: 1000000 }] } } },
  })
  delete (bot.gw as Record<string, unknown>).dsh_command
  acc.writeConfig(bot)
  acc.seedCreds(bot.root, { UP_KEY: 'fake-key-for-tests' })
  const g = new acc.Gateway(bot)
  try {
    await g.start({ DSH_BOT_HARNESS: harness })
    await fn({ tg: tg2, b: bot, up })
  } finally {
    await g.stop().catch(() => {})
    up.stop()
    tg2.stop()
    try { rmSync(bot.root, { recursive: true, force: true }) } catch {}
  }
}

test.skipIf(!available)('真 dsh 空完成：先调 reply、工具结果之后回空完成（content 是空字符串），这一轮假模型只收到 2 个请求，主人没有消息', async () => {
  await withUpstream('openai-completions', 'reply-then-empty', async ({ tg, b, up }) => {
    tg.pushText(acc.FRIEND, '你好 emptyA')
    await acc.until(() => tg.sentTo(acc.FRIEND).some(s => s.text === up.replyText), '回复经真 dsh 发出', 120_000)
    await acc.until(() => acc.evs(b, 'turn.end').length >= 1, '这一轮有了结局', 90_000)
    await acc.sleep(2_000)
    expect(up.generations().length, `假模型收到的请求数；网关日志里这一轮的结局：${JSON.stringify(acc.evs(b, 'turn.end'))}`).toBe(2)
    expect(tg.sentTo(acc.FRIEND).map(s => s.text)).toEqual([up.replyText])
    expect(tg.sentTo(acc.OWNER)).toEqual([])
  })
}, 300_000)

for (const [api, mode] of [['openai-completions', 'empty'], ['openai-responses', 'empty'], ['anthropic-messages', 'empty']] as const) {
  test.skipIf(!available)(`真 dsh 空完成（${api}）：一上来就回空完成，每次 prompt 只发 1 个请求，连同网关的补救整批共 3 个，主人收到「…已放弃（模型回了空内容）。」`, async () => {
    await withUpstream(api, mode, async ({ tg, up }) => {
      tg.pushText(acc.FRIEND, '你好 emptyB')
      const note = await acc.waitSentText(tg, acc.OWNER, '已放弃', 0, 240_000)
      await acc.sleep(1_000)
      expect(up.generations().length, `假模型收到的请求数；主人收到的通知：${note.text}`).toBe(3)
      expect(note.text).toBe('【系统】有 1 条消息重试 2 次后仍然失败，已放弃（模型回了空内容）。')
      expect(tg.sentTo(acc.FRIEND)).toEqual([])
    })
  }, 360_000)
}

// ─── 补充（验收后补的盲区）：空内容不再重试之后，别的重试还在 ───
// 网关补给 dsh 的重试名单里只拿掉了"空内容"，服务端错误与限流仍在（INTERFACE-dsh修补 1.1），次数没动。
// 数的是"一次提问之内"的请求：网关之后的补救轮会在请求体里多出那句补救提示（1.2 的 ⟦系统：上一条消息没能处理完…⟧），不算在内。

const RESCUE_MARK = '⟦系统：上一条消息没能处理完'

/** 让假模型服务每次都回同一种 HTTP 报错，发一条消息，等第一次提问有了结局，返回这次提问之内假模型收到的请求数 */
async function requestsInFirstPrompt(mode: UpstreamMode, text: string): Promise<{ first: number; note: string }> {
  let out = { first: -1, note: '' }
  await withUpstream('openai-completions', mode, async ({ tg, b, up }) => {
    const isRescue = (r: { body: any }) => JSON.stringify(r.body ?? null).includes(RESCUE_MARK)
    tg.pushText(acc.FRIEND, text)
    await acc.until(() => up.generations().length >= 1, '真 dsh 向假模型发出第一个请求', 120_000)
    // 第一次提问结束的三种看得见的迹象，哪个先到都行：日志里这一轮有了结局、补救轮的请求到了、主人收到了通知
    await acc.until(() => acc.evs(b, 'turn.end').length >= 1 || up.generations().some(isRescue) || tg.sentTo(acc.OWNER).length > 0, '第一次提问有了结局', 150_000)
    await acc.sleep(1_500)
    const all = up.generations()
    out = {
      first: all.filter(r => !isRescue(r)).length,
      note: `假模型共收到 ${all.length} 个请求，其中带补救提示的 ${all.filter(isRescue).length} 个；这一轮在日志里的结局：${JSON.stringify(acc.evs(b, 'turn.end'))}`,
    }
    expect(tg.sentTo(acc.FRIEND), '模型一个字都没回，对方不该收到任何消息').toEqual([])
  })
  return out
}

test.skipIf(!available)('真 dsh 重试还在：模型服务每次都回服务端错误（HTTP 500），一次提问之内假模型收到 6 个请求（与修补前一样，1 次加重试 5 次）', async () => {
  // 依据：INTERFACE-dsh修补 1.3 的表，"上游一直回 HTTP 500"一行，改前改后都是 6。
  const { first, note } = await requestsInFirstPrompt('http-500', '你好 retryA')
  expect(first, note).toBe(6)
}, 360_000)

test.skipIf(!available)('真 dsh 重试还在：模型服务每次都回限流（HTTP 429），一次提问之内假模型收到 6 个请求（1 次加重试 5 次）', async () => {
  // 依据：需求说明里现场记下的重试策略是"最多重试 5 次"，限流与服务端错误在同一张可重试名单里，并要求限流的重试行为不变；
  // 网关补的策略只换了名单、没给次数（INTERFACE-dsh修补 1.1）。1.3 的表里没有单列 429 这一行，6 这个数是按上面两处推出来的。
  const { first, note } = await requestsInFirstPrompt('http-429', '你好 retryB')
  expect(first, note).toBe(6)
}, 360_000)
