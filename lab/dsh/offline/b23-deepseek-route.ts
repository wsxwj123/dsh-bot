// B23 实验二：dsh 自带的 deepseek-official 路由上，模型回"空完成"时发几次请求、报什么错。不需要任何密钥。
// 真 dsh + 本机回环上的假服务（Anthropic Messages 协议）。三重保险不连外网：补丁里的 baseURL、DEEPSEEK_BASE_URL 环境变量
// 都指 127.0.0.1；另设一个不通的代理兜底。密钥是占位值，产物写在 lab/dsh/.runs/ 下。
// 用法：DSH=<dsh 可执行文件> bun lab/dsh/offline/b23-deepseek-route.ts [A|B]
//   A  llm-deepseek 行不写 retryPolicy（dsh 的默认策略）
//   B  llm-deepseek 行带 retryPolicy，去掉 EMPTY_RESPONSE（网关补丁下发的那个值）
// 在 0.2.0-rc.2 上的结果（升级 dsh 后重跑，对照这几行）：
//   A 「你好」 7 次请求，约 15 秒；「五百」 6 次
//   B 「你好」 2 次请求，报错 turn failed: DeepSeek Messages returned no content；「五百」 6 次（重试次数不变）
// 这一句和自建路由的那一句不同，网关两句都认（gateway/src/engine/engine.ts 的 TURN_ERROR_RULES）。
// 这里为了把地址钉在回环上，llm-deepseek 行同时写了 baseURL；产品只写 retryPolicy，那个形状由 b23-product-patch.ts 验证。
import { mkdirSync, writeFileSync, readFileSync, readdirSync, statSync } from 'fs'
import { join } from 'path'
import { AcpClient } from '../lib/acp-client'
import { handleMcp, type McpTool } from '../lib/mcp-http'
import { dshBin, personaOnlyRows, runsDir } from '../lib/profile'

const OUT = runsDir(`b23-deepseek-route-${Date.now()}`)

function startLlm() {
  const requests: { n: number; kind: string; path: string }[] = []
  let n = 0
  const server = Bun.serve({ port: 0, hostname: '127.0.0.1', async fetch(req) {
    const url = new URL(req.url)
    if (req.method !== 'POST') return new Response('not found', { status: 404 })
    const body: any = await req.json().catch(() => ({}))
    const msgs: any[] = body.messages ?? []
    const last = msgs.at(-1)
    const blocks = (c: any): any[] => Array.isArray(c) ? c : [{ type: 'text', text: String(c ?? '') }]
    const lastIsToolResult = last?.role === 'user' && blocks(last.content).some(b => b?.type === 'tool_result')
    const lastText = last?.role === 'user' ? blocks(last.content).filter(b => b?.type === 'text').map(b => b.text).join('\n') : ''
    n++
    const ev = (type: string, data: object) => `event: ${type}\ndata: ${JSON.stringify({ type, ...data })}\n\n`
    const start = ev('message_start', { message: { id: `msg_${n}`, type: 'message', role: 'assistant', model: body.model, content: [], stop_reason: null, stop_sequence: null, usage: { input_tokens: 100, output_tokens: 1, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 } } })
    const sse = (frames: string[]) => new Response(frames.join(''), { headers: { 'content-type': 'text/event-stream' } })
    const rec = (kind: string) => requests.push({ n, kind, path: url.pathname })
    if (lastText.includes('五百')) { rec('500'); return new Response('{"type":"error","error":{"type":"api_error","message":"boom"}}', { status: 500, headers: { 'content-type': 'application/json' } }) }
    if (lastIsToolResult) { rec('空完成(工具结果之后)'); return sse([start, ev('message_delta', { delta: { stop_reason: 'end_turn', stop_sequence: null }, usage: { output_tokens: 0 } }), ev('message_stop', {})]) }
    rec('调工具')
    return sse([start,
      ev('content_block_start', { index: 0, content_block: { type: 'tool_use', id: `toolu_${n}`, name: 'mcp__tg__reply', input: {} } }),
      ev('content_block_delta', { index: 0, delta: { type: 'input_json_delta', partial_json: JSON.stringify({ text: 'hi' }) } }),
      ev('content_block_stop', { index: 0 }),
      ev('message_delta', { delta: { stop_reason: 'tool_use', stop_sequence: null }, usage: { output_tokens: 10 } }), ev('message_stop', {})])
  } })
  return { port: server.port ?? 0, requests, stop: () => server.stop(true) }
}

async function runCase(name: string, retryPolicy: object | undefined, prompts: string[]) {
  console.log(`\n===== ${name} =====  llm-deepseek.retryPolicy=${JSON.stringify(retryPolicy ?? '(不写，用 dsh 默认)')}`)
  const llm = startLlm()
  let toolCalls = 0
  const tools: McpTool[] = [{ name: 'reply', description: '发消息', inputSchema: { type: 'object', properties: { text: { type: 'string' } }, required: ['text'] }, async call() { toolCalls++; return { text: '已送达 1/1 段' } } }]
  const mcp = Bun.serve({ port: 0, hostname: '127.0.0.1', fetch: req => handleMcp(req, tools) })
  const root = join(OUT, name)
  const sb = { home: join(root, 'home'), dshHome: join(root, 'dshhome'), work: join(root, 'work'), sessions: join(root, 'sessions'), patch: join(root, 'bot.patch.yml') }
  for (const d of [sb.home, sb.dshHome, sb.work, sb.sessions]) mkdirSync(d, { recursive: true })
  const base = `http://127.0.0.1:${llm.port}`
  const rows = [
    ...personaOnlyRows({ persona: '你是测试人设。', route: { kind: 'deepseek', model: 'deepseek-v4-flash' }, sessionsRoot: sb.sessions, compaction: 'off' }),
    { id: 'llm-deepseek', config: { baseURL: base, ...(retryPolicy ? { retryPolicy } : {}) } },
  ]
  writeFileSync(sb.patch, JSON.stringify(rows, null, 1))
  const c = new AcpClient([dshBin(), '--profile', 'acp', '--patch', sb.patch], {
    env: { PATH: process.env.PATH ?? '/usr/bin:/bin', HOME: sb.home, DSH_HOME: sb.dshHome, DSH_TELEMETRY_DISABLED: '1', LANG: 'C.UTF-8',
      DEEPSEEK_API_KEY: 'dummy-not-a-key', DEEPSEEK_BASE_URL: base,
      NO_PROXY: '127.0.0.1,localhost', no_proxy: '127.0.0.1,localhost', HTTPS_PROXY: 'http://127.0.0.1:1', https_proxy: 'http://127.0.0.1:1' },
    cwd: sb.work,
  })
  try {
    await c.initialize()
    const s = await c.request('session/new', { cwd: sb.work, mcpServers: [{ type: 'http', name: 'tg', url: `http://127.0.0.1:${mcp.port}/mcp/chat-1`, headers: [{ name: 'Authorization', value: 'Bearer lab' }] }] })
    for (const p of prompts) {
      const n0 = llm.requests.length, t0 = Date.now(), tc0 = toolCalls
      let outcome: string
      try { const r = await c.prompt(s.sessionId, p, 120_000); outcome = `正常返回 stopReason=${r.stopReason}` }
      catch (e: any) { outcome = `报错 code=${e.code} message=${JSON.stringify(e.message)}` }
      const reqs = llm.requests.slice(n0)
      console.log(`[发「${p}」] 假服务收到 ${reqs.length} 次（${reqs.map(r => r.kind).join(' → ')}）；路径=${[...new Set(reqs.map(r => r.path))].join(',')}；工具调用 ${toolCalls - tc0} 次；耗时 ${((Date.now() - t0) / 1000).toFixed(1)}s`)
      console.log(`    session/prompt：${outcome}`)
    }
  } finally {
    await c.close()
    llm.stop(); mcp.stop(true)
  }
  const files: string[] = []
  const walk = (d: string) => { for (const f of readdirSync(d)) { const p = join(d, f); statSync(p).isDirectory() ? walk(p) : files.push(p) } }
  walk(sb.sessions)
  let retries = 0; const codes: Record<string, number> = {}; const keys = new Set<string>()
  for (const f of files.filter(f => f.endsWith('.jsonl'))) for (const line of readFileSync(f, 'utf8').split('\n')) {
    if (!line.trim()) continue
    let e: any; try { e = JSON.parse(line) } catch { continue }
    if (e.type === 'llm/retry') { retries++; codes[e.data?.failure?.code] = (codes[e.data?.failure?.code] ?? 0) + 1; keys.add(String(e.data?.policyKey)) }
  }
  console.log(`[会话记录] llm/retry ${retries} 条，按 failure.code：${JSON.stringify(codes)}；policyKey：${[...keys].join(' | ') || '(无)'}`)
  if (c.stderr.trim()) console.log(`[dsh stderr 尾部] ${c.stderr.trim().split('\n').slice(-4).join(' ⏎ ').slice(0, 600)}`)
}

const NO_EMPTY = { mode: 'normal', retryableCodes: ['RATE_LIMIT', 'SERVER', 'TIMEOUT', 'TRANSPORT'] }
const which = process.argv[2] ?? 'all'
if (which === 'all' || which === 'A') await runCase('A-default', undefined, ['你好', '五百'])
if (which === 'all' || which === 'B') await runCase('B-no-empty', NO_EMPTY, ['你好', '五百'])
console.log(`\n产物目录：${OUT}`)
