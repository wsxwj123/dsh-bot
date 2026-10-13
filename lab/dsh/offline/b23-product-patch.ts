// B23 实验三：用网关实际生成的补丁（gateway/src/dsh/profile.ts 的 buildPatchRows）原样启动真 dsh。不需要任何密钥。
// 核对：进程起得来并完成握手；llm-deepseek 行在合成配置里只有一行、出厂内容没被冲掉；模型表与旧补丁相同；
// 回复后回空只发 2 次请求、下一轮正常；旧补丁建的会话换新补丁后能接着用；ACP 通知里没有错误码。
// 只连本机回环（另设一个不通的代理兜底），密钥是占位值，产物写在 lab/dsh/.runs/ 下。
// 用法：DSH=<dsh 可执行文件> bun lab/dsh/offline/b23-product-patch.ts
// 在 0.2.0-rc.2 上的结果（升级 dsh 后重跑，对照这几行）：
//   S（当前路由是自建，凭据里没有 deepseek 密钥）
//     导出配置退出码 0，llm-deepseek 行出现 1 次，config 只有 retryPolicy；握手 protocolVersion=1；模型表与旧补丁逐字相同=true
//     「你好」→ 上游 2 次（调工具→空完成），报错 … returned a completed response with no content；通知里有 EMPTY_RESPONSE 字样=false
//     「再来 正常」→ 上游 1 次，正常返回 end_turn；旧会话 session/resume 成功，接着对话正常
//   D（当前路由是内置 deepseek，补丁里不写地址）
//     导出配置同上；模型表与旧补丁逐字相同=true；「你好」→ 上游 2 次，路径 /v1/messages，报错 … DeepSeek Messages returned no content
// D 里的 DEEPSEEK_BASE_URL 只为把请求留在回环上。没有实测过它连到出厂地址（实验不许连外网）。
import { mkdirSync, writeFileSync, chmodSync } from 'fs'
import { join } from 'path'
import { AcpClient } from '../lib/acp-client'
import { handleMcp, type McpTool } from '../lib/mcp-http'
import { dshBin, runsDir } from '../lib/profile'
import { buildPatchRows } from '../../../gateway/src/dsh/profile'

const OUT = runsDir(`b23-product-patch-${Date.now()}`)
/** 兜底：万一 dsh 不再认 DEEPSEEK_BASE_URL、想连回环以外的地址，请求会撞到这个不通的代理上，出不了本机 */
const LOOPBACK_ONLY = { NO_PROXY: '127.0.0.1,localhost', no_proxy: '127.0.0.1,localhost', HTTPS_PROXY: 'http://127.0.0.1:1', https_proxy: 'http://127.0.0.1:1' }

function startLlm() {
  const requests: { kind: string; path: string; model: string }[] = []
  let n = 0
  const sse = (frames: string[]) => new Response(frames.join(''), { headers: { 'content-type': 'text/event-stream' } })
  const server = Bun.serve({ port: 0, hostname: '127.0.0.1', async fetch(req) {
    const url = new URL(req.url)
    if (req.method !== 'POST') return Response.json({ object: 'list', data: [{ id: 'fake-chat' }] })
    const body: any = await req.json().catch(() => ({})); const msgs: any[] = body.messages ?? []; const last = msgs.at(-1); n++
    const anth = url.pathname.endsWith('/messages')
    const blocks = (c: any): any[] => Array.isArray(c) ? c : [{ type: 'text', text: String(c ?? '') }]
    const afterTool = anth ? (last?.role === 'user' && blocks(last.content).some(b => b?.type === 'tool_result')) : last?.role === 'tool'
    const lastText = last?.role === 'user' ? blocks(last.content).filter(b => b?.type === 'text').map(b => b.text).join('\n') : ''
    // Anthropic 协议下 dsh 把上一轮的工具结果和本轮新消息并进同一条 user 消息，所以先看文字再看工具结果
    const kind = lastText.includes('正常') ? '文本' : afterTool ? '空完成' : '调工具'
    requests.push({ kind, path: url.pathname, model: String(body.model) })
    if (anth) {
      const ev = (type: string, data: object) => `event: ${type}\ndata: ${JSON.stringify({ type, ...data })}\n\n`
      const start = ev('message_start', { message: { id: `msg_${n}`, type: 'message', role: 'assistant', model: body.model, content: [], stop_reason: null, stop_sequence: null, usage: { input_tokens: 100, output_tokens: 1, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 } } })
      const end = (r: string) => [ev('message_delta', { delta: { stop_reason: r, stop_sequence: null }, usage: { output_tokens: 1 } }), ev('message_stop', {})]
      if (kind === '空完成') return sse([start, ...end('end_turn')])
      if (kind === '文本') return sse([start, ev('content_block_start', { index: 0, content_block: { type: 'text', text: '' } }), ev('content_block_delta', { index: 0, delta: { type: 'text_delta', text: '好的' } }), ev('content_block_stop', { index: 0 }), ...end('end_turn')])
      return sse([start, ev('content_block_start', { index: 0, content_block: { type: 'tool_use', id: `toolu_${n}`, name: 'mcp__tg__reply', input: {} } }), ev('content_block_delta', { index: 0, delta: { type: 'input_json_delta', partial_json: JSON.stringify({ text: 'hi' }) } }), ev('content_block_stop', { index: 0 }), ...end('tool_use')])
    }
    const base = { id: `c${n}`, object: 'chat.completion.chunk', created: 1, model: body.model }
    const d = (delta: object, fin: string | null = null) => `data: ${JSON.stringify({ ...base, choices: [{ index: 0, delta, finish_reason: fin }] })}\n\n`
    const usage = `data: ${JSON.stringify({ ...base, choices: [], usage: { prompt_tokens: 100, completion_tokens: 0, total_tokens: 100 } })}\n\ndata: [DONE]\n\n`
    if (kind === '空完成') return sse([d({ role: 'assistant' }), d({}, 'stop'), usage])
    if (kind === '文本') return sse([d({ role: 'assistant', content: '' }), d({ content: '好的' }), d({}, 'stop'), usage])
    return sse([d({ role: 'assistant', content: '' }), d({ tool_calls: [{ index: 0, id: `call_${n}`, type: 'function', function: { name: 'mcp__tg__reply', arguments: JSON.stringify({ text: 'hi' }) } }] }), d({}, 'tool_calls'), usage])
  } })
  return { port: server.port ?? 0, requests, stop: () => server.stop(true) }
}

type Box = { root: string; home: string; dshHome: string; work: string; patch: string; cred: string; env: Record<string, string> }
function box(name: string, refs: Record<string, string>, extraEnv: Record<string, string> = {}): Box {
  const root = join(OUT, name)
  const b = { root, home: join(root, 'home'), dshHome: join(root, 'dsh-home'), work: join(root, 'work'), patch: join(root, 'dsh-home', 'bot.patch.yml'), cred: join(root, 'credentials.yaml') }
  for (const d of [b.home, b.dshHome, b.work, join(root, 'skills'), join(b.dshHome, 'sessions')]) mkdirSync(d, { recursive: true })
  writeFileSync(b.cred, `version: 1\nrefs:\n${Object.entries(refs).map(([k, v]) => `  ${k}: ${v}`).join('\n')}\n`); chmodSync(b.cred, 0o600)
  return { ...b, env: { PATH: process.env.PATH ?? '/usr/bin:/bin', HOME: b.home, DSH_HOME: b.dshHome, DSH_TELEMETRY_DISABLED: '1', TZ: 'Asia/Shanghai', LANG: 'C.UTF-8', ...LOOPBACK_ONLY, ...extraEnv } }
}
/** 网关现在生成的补丁：每条路由带 retryPolicy，另有一行只有 retryPolicy 的 llm-deepseek */
const productRows = (b: Box, brain: any) => buildPatchRows({ persona: '你是测试人设。', brain, credentialsPath: b.cred, sessionsRoot: join(b.dshHome, 'sessions'), skillDir: join(b.root, 'skills') }) as any[]
/** 这次改动之前的补丁形状：路由不带 retryPolicy，没有 llm-deepseek 行。用来建"旧会话"、取对照用的模型表 */
function legacyRows(rows: any[]): any[] {
  return rows.filter(r => r.id !== 'llm-deepseek').map(r => r.id !== 'llm-pi-ai' ? r : { ...r, config: { providers: Object.fromEntries(Object.entries(r.config.providers).map(([k, v]: [string, any]) => { const { retryPolicy: _dropped, ...rest } = v; return [k, rest] })) } })
}
/** 补丁文件里实际写了什么策略（先确认测的确实是带策略的补丁，再看 dsh 的表现） */
const policyIn = (rows: any[]) => `路由的 retryPolicy=${JSON.stringify(Object.fromEntries(Object.entries(rows.find(r => r.id === 'llm-pi-ai')?.config.providers ?? {}).map(([k, v]: [string, any]) => [k, v.retryPolicy ?? null])))}；llm-deepseek 行=${JSON.stringify(rows.filter(r => r.id === 'llm-deepseek').map(r => r.config))}`

async function dump(b: Box): Promise<string> {
  const p = Bun.spawn([dshBin(), '--profile', 'acp', '--patch', b.patch, '--dump-config'], { env: b.env, cwd: b.work, stdout: 'pipe', stderr: 'pipe', stdin: 'ignore' })
  const [o, , code] = await Promise.all([new Response(p.stdout).text(), new Response(p.stderr).text(), p.exited])
  const lines = o.split('\n'); const idx = lines.map((l, i) => l.startsWith('- id: llm-deepseek') && !l.includes('account') ? i : -1).filter(i => i >= 0)
  const block = idx.length ? lines.slice(idx[0]!, idx[0]! + 12).join('\n').split(/\n(?=#|- id)/)[0]! : ''
  return `退出码 ${code}；llm-deepseek 行出现 ${idx.length} 次；内容：${block.replace(/\n\s*/g, ' | ')}`
}
const modelTable = (s: any) => JSON.stringify(s.configOptions?.find((o: any) => o.id === 'model')?.options ?? null)
const tools: McpTool[] = [{ name: 'reply', description: '发消息', inputSchema: { type: 'object', properties: { text: { type: 'string' } }, required: ['text'] }, async call() { return { text: '已送达 1/1 段' } } }]
const mcp = Bun.serve({ port: 0, hostname: '127.0.0.1', fetch: req => handleMcp(req, tools) })
const mcpServers = [{ type: 'http', name: 'tg', url: `http://127.0.0.1:${mcp.port}/mcp/chat-1`, headers: [{ name: 'Authorization', value: 'Bearer lab' }] }]
const start = async (b: Box) => { const c = new AcpClient([dshBin(), '--profile', 'acp', '--patch', b.patch], { env: b.env, cwd: b.work }); const init = await c.initialize(); return { c, init } }
async function turn(c: AcpClient, sid: string, llm: ReturnType<typeof startLlm>, text: string): Promise<string> {
  const n0 = llm.requests.length, u0 = c.updates.length
  let res: string
  try { res = `正常返回 ${(await c.prompt(sid, text, 60_000)).stopReason}` } catch (e: any) { res = `报错 ${e.code} ${String(e.message).slice(0, 110)}` }
  const reqs = llm.requests.slice(n0); const ups = c.updates.slice(u0)
  return `「${text}」→ 上游 ${reqs.length} 次（${reqs.map(r => r.kind).join('→')}；路径 ${[...new Set(reqs.map(r => r.path))].join(',')}；模型 ${[...new Set(reqs.map(r => r.model))].join(',')}）；${res}；通知类型 ${[...new Set(ups.map(u => u.update?.sessionUpdate))].join(',')}；通知里有 EMPTY_RESPONSE 字样=${JSON.stringify(ups).includes('EMPTY_RESPONSE')}`
}

// ── S：当前路由是自建；没有任何 deepseek 密钥 ──
{
  const llm = startLlm()
  const brain = { provider: 'fake', model: 'fake-chat', routes: { fake: { api: 'openai-completions', baseURL: `http://127.0.0.1:${llm.port}/v1`, apiKeyEnv: 'FAKE_LLM_KEY', models: [{ id: 'fake-chat', contextWindow: 1000000 }] } }, emergencyCompaction: false }
  const b = box('S-custom', { FAKE_LLM_KEY: 'dummy-not-a-key' })
  const rows = productRows(b, brain)
  // 先用旧形状的补丁（不带 retryPolicy、没有 llm-deepseek 行）建一个会话，留给后面验证"旧会话 + 新补丁"
  writeFileSync(b.patch, JSON.stringify(legacyRows(rows), null, 1) + '\n')
  let { c } = await start(b)
  const old = await c.request('session/new', { cwd: b.work, mcpServers })
  const tableOld = modelTable(old)
  console.log(`[S 旧补丁] ${await turn(c, old.sessionId, llm, '再来 正常')}`)
  await c.close()
  // 换成网关现在生成的补丁
  writeFileSync(b.patch, JSON.stringify(rows, null, 1) + '\n')
  console.log(`[S 新补丁 补丁文件] ${policyIn(rows)}`)
  console.log(`[S 新补丁 导出配置] ${await dump(b)}`)
  const st = await start(b); c = st.c
  console.log(`[S 新补丁] 握手 protocolVersion=${st.init.protocolVersion} agent=${st.init.agentInfo?.name}@${st.init.agentInfo?.version}`)
  const s = await c.request('session/new', { cwd: b.work, mcpServers })
  console.log(`[S 新补丁] 模型表与旧补丁逐字相同=${modelTable(s) === tableOld}`)
  console.log(`[S 新补丁] ${await turn(c, s.sessionId, llm, '你好')}`)
  console.log(`[S 新补丁] ${await turn(c, s.sessionId, llm, '再来 正常')}`)
  let resumed = '成功'
  try { await c.request('session/resume', { sessionId: old.sessionId, cwd: b.work, mcpServers }) } catch (e: any) { resumed = `失败 ${e.code} ${String(e.message).slice(0, 100)}` }
  console.log(`[S 旧会话 + 新补丁] session/resume ${resumed}；${resumed === '成功' ? await turn(c, old.sessionId, llm, '再来 正常') : ''}`)
  console.log(`[S] dsh stderr 尾部：${c.stderr.trim().split('\n').slice(-2).join(' ⏎ ').slice(0, 300) || '(空)'}`)
  await c.close(); llm.stop()
}
// ── D：当前路由是内置 deepseek；补丁里不写地址。DEEPSEEK_BASE_URL 只为把请求留在回环上 ──
{
  const llm = startLlm()
  const brain = { provider: 'deepseek-official', model: 'deepseek-flash', routes: {}, emergencyCompaction: false }
  const b = box('D-builtin', { DEEPSEEK_API_KEY: 'dummy-not-a-key' }, { DEEPSEEK_BASE_URL: `http://127.0.0.1:${llm.port}` })
  const rows = productRows(b, brain)
  writeFileSync(b.patch, JSON.stringify(legacyRows(rows), null, 1) + '\n')
  let { c } = await start(b)
  const tableOld = modelTable(await c.request('session/new', { cwd: b.work, mcpServers }))
  await c.close()
  writeFileSync(b.patch, JSON.stringify(rows, null, 1) + '\n')
  console.log(`[D 新补丁 补丁文件] ${policyIn(rows)}`)
  console.log(`[D 新补丁 导出配置] ${await dump(b)}`)
  const st = await start(b); c = st.c
  console.log(`[D 新补丁] 握手 protocolVersion=${st.init.protocolVersion} agent=${st.init.agentInfo?.name}@${st.init.agentInfo?.version}`)
  const s = await c.request('session/new', { cwd: b.work, mcpServers })
  console.log(`[D 新补丁] 模型表与旧补丁逐字相同=${modelTable(s) === tableOld}；模型表前 160 字：${modelTable(s).slice(0, 160)}`)
  console.log(`[D 新补丁] ${await turn(c, s.sessionId, llm, '你好')}`)
  console.log(`[D 新补丁] ${await turn(c, s.sessionId, llm, '再来 正常')}`)
  console.log(`[D] dsh stderr 尾部：${c.stderr.trim().split('\n').slice(-2).join(' ⏎ ').slice(0, 300) || '(空)'}`)
  await c.close(); llm.stop()
}
mcp.stop(true)
console.log(`产物目录：${OUT}`)
