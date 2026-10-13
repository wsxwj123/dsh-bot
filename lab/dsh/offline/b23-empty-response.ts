// B23 实验一：自建路由（llm-pi-ai）上，模型回"空完成"时真 dsh 发几次请求、报什么错。不需要任何密钥。
// 真 dsh + 本机回环上的假模型服务。只连 127.0.0.1（另设一个不通的代理兜底），密钥是占位值，产物写在 lab/dsh/.runs/ 下。
// 用法：DSH=<dsh 可执行文件> bun lab/dsh/offline/b23-empty-response.ts [A|B|C]
//   A  不写 retryPolicy（dsh 的默认策略）
//   B  retryPolicy 去掉 EMPTY_RESPONSE（网关补丁下发的那个值，见 gateway/src/dsh/profile.ts 的 RETRY_POLICY）
//   C  同 A，空完成换成 content 为空字符串的形状
// 在 0.2.0-rc.2 上的结果（升级 dsh 后重跑，对照这几行）：
//   A 「你好」    7 次请求（调工具 → 空完成 ×6），约 15 秒，报错 turn failed: model "fake-chat" returned a completed response with no content
//   B 「你好」    2 次请求，报错文案同上；「再来 正常」1 次、正常返回；「五百」6 次（重试次数不变）；「直接空」1 次
//   C 「你好」    7 次请求（两种空完成的形状表现相同）
// 网关靠这句英文认"空内容"（gateway/src/engine/engine.ts 的 TURN_ERROR_RULES）。句子变了，那张表和 VERIFIED_DSH_VERSION 要跟着改。
import { mkdirSync, writeFileSync, readFileSync, readdirSync, statSync } from 'fs'
import { join } from 'path'
import { AcpClient } from '../lib/acp-client'
import { handleMcp, type McpTool } from '../lib/mcp-http'
import { dshBin, personaOnlyRows, runsDir } from '../lib/profile'

const OUT = runsDir(`b23-empty-response-${Date.now()}`)
/** 兜底：万一 dsh 想连回环以外的地址，请求会撞到这个不通的代理上，出不了本机 */
const LOOPBACK_ONLY = { NO_PROXY: '127.0.0.1,localhost', no_proxy: '127.0.0.1,localhost', HTTPS_PROXY: 'http://127.0.0.1:1', https_proxy: 'http://127.0.0.1:1' }

type EmptyShape = 'no-content-field' | 'empty-string'

function sse(chunks: object[]): Response {
  const enc = new TextEncoder()
  const body = new ReadableStream({ start(c) { for (const ch of chunks) c.enqueue(enc.encode(`data: ${JSON.stringify(ch)}\n\n`)); c.enqueue(enc.encode('data: [DONE]\n\n')); c.close() } })
  return new Response(body, { headers: { 'content-type': 'text/event-stream' } })
}

function startLlm(shape: EmptyShape) {
  const requests: { n: number; kind: string; roles: string }[] = []
  let n = 0
  const server = Bun.serve({ port: 0, hostname: '127.0.0.1', async fetch(req) {
    if (req.method === 'GET') return Response.json({ object: 'list', data: [{ id: 'fake-chat', object: 'model' }] })
    const body: any = await req.json().catch(() => ({}))
    const msgs: any[] = body.messages ?? []
    const text = (c: any) => typeof c === 'string' ? c : Array.isArray(c) ? c.filter((b: any) => b?.type === 'text').map((b: any) => b.text).join('\n') : ''
    const last = msgs.at(-1)
    let user = ''
    for (let i = msgs.length - 1; i >= 0; i--) { if (msgs[i].role === 'user' && text(msgs[i].content) && !text(msgs[i].content).trimStart().startsWith('<system-reminder>')) { user = text(msgs[i].content); break } }
    n++
    const base = { id: `chatcmpl-${n}`, object: 'chat.completion.chunk', created: Math.floor(Date.now() / 1000), model: body.model }
    const usage = { ...base, choices: [], usage: { prompt_tokens: 100, completion_tokens: 0, total_tokens: 100 } }
    const rec = (kind: string) => requests.push({ n, kind, roles: msgs.map(m => m.role[0]).join('') })
    const empty = () => shape === 'no-content-field'
      ? sse([{ ...base, choices: [{ index: 0, delta: { role: 'assistant' }, finish_reason: null }] }, { ...base, choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] }, usage])
      : sse([{ ...base, choices: [{ index: 0, delta: { role: 'assistant', content: '' }, finish_reason: null }] }, { ...base, choices: [{ index: 0, delta: { content: '' }, finish_reason: null }] }, { ...base, choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] }, usage])
    if (last?.role === 'user' && user.includes('五百')) { rec('500'); return new Response('{"error":{"message":"boom"}}', { status: 500, headers: { 'content-type': 'application/json' } }) }
    if (last?.role === 'tool') { rec('空完成(工具结果之后)'); return empty() }
    if (user.includes('直接空')) { rec('空完成(一上来)'); return empty() }
    if (user.includes('正常')) { rec('文本'); return sse([{ ...base, choices: [{ index: 0, delta: { role: 'assistant', content: '' }, finish_reason: null }] }, { ...base, choices: [{ index: 0, delta: { content: '好的' }, finish_reason: null }] }, { ...base, choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] }, usage]) }
    rec('调工具')
    return sse([{ ...base, choices: [{ index: 0, delta: { role: 'assistant', content: '' }, finish_reason: null }] }, { ...base, choices: [{ index: 0, delta: { tool_calls: [{ index: 0, id: `call_${n}`, type: 'function', function: { name: 'mcp__tg__reply', arguments: JSON.stringify({ text: 'hi' }) } }] }, finish_reason: null }] }, { ...base, choices: [{ index: 0, delta: {}, finish_reason: 'tool_calls' }] }, usage])
  } })
  return { port: server.port ?? 0, requests, stop: () => server.stop(true) }
}

async function runCase(name: string, retryPolicy: object | undefined, shape: EmptyShape, prompts: string[]) {
  console.log(`\n===== ${name} =====  retryPolicy=${JSON.stringify(retryPolicy ?? '(不写，用 dsh 默认)')}  空完成形状=${shape}`)
  const llm = startLlm(shape)
  let toolCalls = 0
  const tools: McpTool[] = [{ name: 'reply', description: '发消息', inputSchema: { type: 'object', properties: { text: { type: 'string' } }, required: ['text'] }, async call() { toolCalls++; return { text: '已送达 1/1 段' } } }]
  const mcp = Bun.serve({ port: 0, hostname: '127.0.0.1', fetch: req => handleMcp(req, tools) })
  const root = join(OUT, name.replace(/[^A-Za-z0-9_-]/g, '_'))
  const sb = { home: join(root, 'home'), dshHome: join(root, 'dshhome'), work: join(root, 'work'), sessions: join(root, 'sessions'), patch: join(root, 'bot.patch.yml') }
  for (const d of [sb.home, sb.dshHome, sb.work, sb.sessions]) mkdirSync(d, { recursive: true })
  const provider: Record<string, unknown> = { api: 'openai-completions', baseURL: `http://127.0.0.1:${llm.port}/v1`, apiKeyEnv: 'FAKE_LLM_KEY', models: [{ id: 'fake-chat', contextWindow: 1000000 }] }
  if (retryPolicy) provider.retryPolicy = retryPolicy
  const rows = personaOnlyRows({ persona: '你是测试人设。', route: { kind: 'openai-compatible', name: 'fake', baseURL: String(provider.baseURL), apiKeyEnv: 'FAKE_LLM_KEY', model: 'fake-chat', contextWindow: 1000000 }, sessionsRoot: sb.sessions, compaction: 'off' })
    .map((r: any) => r.id === 'llm-pi-ai' ? { id: 'llm-pi-ai', config: { providers: { fake: provider } } } : r)
  writeFileSync(sb.patch, JSON.stringify(rows, null, 1))
  const c = new AcpClient([dshBin(), '--profile', 'acp', '--patch', sb.patch], {
    env: { PATH: process.env.PATH ?? '/usr/bin:/bin', HOME: sb.home, DSH_HOME: sb.dshHome, DSH_TELEMETRY_DISABLED: '1', LANG: 'C.UTF-8', ...LOOPBACK_ONLY, FAKE_LLM_KEY: 'dummy-not-a-key' },
    cwd: sb.work,
  })
  try {
    await c.initialize()
    const s = await c.request('session/new', { cwd: sb.work, mcpServers: [{ type: 'http', name: 'tg', url: `http://127.0.0.1:${mcp.port}/mcp/chat-1`, headers: [{ name: 'Authorization', value: 'Bearer lab' }] }] })
    for (const p of prompts) {
      const n0 = llm.requests.length, t0 = Date.now(), tc0 = toolCalls
      let outcome: string
      try { const r = await c.prompt(s.sessionId, p, 120_000); outcome = `正常返回 stopReason=${r.stopReason}` }
      catch (e: any) { outcome = `报错 code=${e.code} message=${JSON.stringify(e.message)} data=${JSON.stringify(e.data)}` }
      const reqs = llm.requests.slice(n0)
      console.log(`[发「${p}」] 模型请求 ${reqs.length} 次（${reqs.map(r => r.kind).join(' → ')}）；工具调用 ${toolCalls - tc0} 次；耗时 ${((Date.now() - t0) / 1000).toFixed(1)}s`)
      console.log(`    session/prompt：${outcome}`)
      console.log(`    最后一次请求的消息角色序列：${reqs.at(-1)?.roles}`)
    }
  } finally {
    await c.close()
    llm.stop(); mcp.stop(true)
  }
  // 读沙箱里这次会话的记录，数 llm/retry
  const files: string[] = []
  const walk = (d: string) => { for (const f of readdirSync(d)) { const p = join(d, f); statSync(p).isDirectory() ? walk(p) : files.push(p) } }
  walk(sb.sessions)
  let retries = 0; const codes: Record<string, number> = {}; const keys = new Set<string>(); const ends: string[] = []
  for (const f of files.filter(f => f.endsWith('.jsonl'))) for (const line of readFileSync(f, 'utf8').split('\n')) {
    if (!line.trim()) continue
    let ev: any; try { ev = JSON.parse(line) } catch { continue }
    const type = ev.type ?? ev.event?.type; const data = ev.data ?? ev.event?.data
    if (type === 'llm/retry') { retries++; codes[data?.failure?.code] = (codes[data?.failure?.code] ?? 0) + 1; keys.add(String(data?.policyKey)) }
    if (type === 'turn/end') ends.push(JSON.stringify(data?.end ?? data).slice(0, 160))
  }
  console.log(`[会话记录] llm/retry ${retries} 条，按 failure.code：${JSON.stringify(codes)}；policyKey：${[...keys].join(' | ') || '(无)'}`)
  console.log(`[会话记录] 各轮 turn/end：${ends.join(' ; ')}`)
  if (c.stderr.trim()) console.log(`[dsh stderr 尾部] ${c.stderr.trim().split('\n').slice(-4).join(' ⏎ ').slice(0, 600)}`)
}

const NO_EMPTY = { mode: 'normal', retryableCodes: ['RATE_LIMIT', 'SERVER', 'TIMEOUT', 'TRANSPORT'] }
const which = process.argv[2] ?? 'all'
if (which === 'all' || which === 'A') await runCase('A-default', undefined, 'no-content-field', ['你好', '再来 正常', '五百', '直接空'])
if (which === 'all' || which === 'B') await runCase('B-no-empty', NO_EMPTY, 'no-content-field', ['你好', '再来 正常', '五百', '直接空'])
if (which === 'all' || which === 'C') await runCase('C-default-emptystring', undefined, 'empty-string', ['你好'])
console.log(`\n产物目录：${OUT}`)
