// 只为 outage-delivery 验收 #21 服务的 ACP 假件：约定提醒那一轮里，模型先调 reply、随后在同一轮调 commitment_done，再调 commitment_list。
// 共享假件 test/fakes/fake-acp.ts 只认提示里的 !tool 指令，而约定提醒文字是网关生成的，塞不进指令；
// 按派工要求不改共享假件，在本目录另建一份。只实现网关会用到的那几个方法。
// 落盘格式与共享假件一致（state 目录下 prompts.jsonl、tool-results.jsonl），测试里现成的读取工具直接能用。
// 用法：bun fake-acp-due-done.ts --state <目录>
// 行为：
//   提示里含「⟦系统·承诺到期⟧」：先等 state 目录里出现 due-go 文件（测试确认网关已判定线路不通后写入；最多等 40 秒），
//     然后依次调 reply {text: DUE_REPLY}、commitment_done {id:<提示里的 #编号>}、commitment_list {}，三次结果都落盘。
//   最后一块以「⟦系统·整理记忆⟧」开头：直接输出一段假摘要，不调工具。
//   其余提示：逐行执行最后一块里的 !tool:<名字>|<JSON>，再 reply「收到：<最后一句>」（⟦系统…开头的提示回「接着刚才的说」）。
import { appendFileSync, existsSync, mkdirSync, mkdtempSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'

/** 约定提醒那一轮模型要说的话（测试里按这串文字核对它有没有发往 Telegram） */
const DUE_REPLY = '约定那一轮的话丁'
const DUE_GO_WAIT_MS = 40_000

const argv = process.argv.slice(2)
const stateIdx = argv.indexOf('--state')
const stateDir = stateIdx >= 0 ? argv[stateIdx + 1]! : mkdtempSync(join(tmpdir(), 'fake-acp-due-done-'))
mkdirSync(stateDir, { recursive: true })
const P = (f: string) => join(stateDir, f)
const log = (f: string, v: object) => appendFileSync(P(f), JSON.stringify({ pid: process.pid, at: Date.now(), ...v }) + '\n')
const sleep = (ms: number) => new Promise(r => setTimeout(r, ms))

function send(f: object) { process.stdout.write(JSON.stringify({ jsonrpc: '2.0', ...f }) + '\n') }
function update(sessionId: string, u: object) { send({ method: 'session/update', params: { sessionId, update: u } }) }

const MODEL = JSON.stringify(['deepseek-official', 'deepseek-flash'])
function configOptions() {
  return [
    { id: 'model', name: 'Model', type: 'select', currentValue: MODEL, options: [{ group: 'deepseek-official', options: [{ value: MODEL, name: 'deepseek-flash' }] }] },
    { id: 'reasoning_effort', name: 'Effort', type: 'select', currentValue: 'off', options: ['', 'off', 'low', 'high', 'max'].map(v => ({ value: v, name: v || 'default' })) },
  ]
}

type Mcp = { url: string; headers: { name: string; value: string }[] }
const sessions = new Map<string, Mcp | null>()
let seq = 0
let mcpSeq = 1

/** 调网关的 MCP 工具，结果落盘到 tool-results.jsonl（格式同共享假件） */
async function callTool(sessionId: string, name: string, args: object): Promise<string> {
  const mcp = sessions.get(sessionId)
  if (!mcp) { log('tool-results.jsonl', { sessionId, name, args, text: 'no mcp', isError: true }); return 'no mcp' }
  const headers: Record<string, string> = { 'content-type': 'application/json' }
  for (const h of mcp.headers) headers[h.name] = h.value
  const callId = `t${mcpSeq}`
  update(sessionId, { sessionUpdate: 'tool_call', toolCallId: callId, title: name, status: 'pending' })
  let text: string
  let isError = false
  try {
    const res = await fetch(mcp.url, { method: 'POST', headers, body: JSON.stringify({ jsonrpc: '2.0', id: mcpSeq++, method: 'tools/call', params: { name, arguments: args } }) })
    const body = await res.json() as any
    text = String(body?.result?.content?.[0]?.text ?? body?.error?.message ?? `http ${res.status}`)
    isError = body?.result?.isError === true
  } catch (e) {
    text = `fetch failed: ${(e as Error)?.message}`
    isError = true
  }
  log('tool-results.jsonl', { sessionId, name, args, text, isError })
  update(sessionId, { sessionUpdate: 'tool_call_update', toolCallId: callId, status: 'completed' })
  return text
}

function lastUserLine(text: string): string {
  const lines = text.split('\n').filter(l => l.trim() && !l.startsWith('⟦'))
  return (lines[lines.length - 1] ?? '').replace(/![a-z]+(?::\S+)?/g, '').trim()
}

async function prompt(id: number, sessionId: string, blocks0: { type: string; text?: string }[]) {
  const texts = blocks0.filter(b => b.type !== 'image').map(b => b.text ?? '')
  const all = texts.join('\n')
  const last = texts[texts.length - 1] ?? ''
  log('prompts.jsonl', { sessionId, text: all, blocks: texts.length, images: blocks0.length - texts.length, model: MODEL })
  update(sessionId, { sessionUpdate: 'usage_update', used: all.length, size: 1_000_000 })

  if (last.startsWith('⟦系统·整理记忆⟧')) {
    update(sessionId, { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: '【我说过的要紧话】无\n【我答应过的事】无\n【对方的情况】无\n【正在聊的话题】无\n【我们现在的关系和气氛】平常' } })
    return send({ id, result: { stopReason: 'end_turn' } })
  }

  if (all.includes('⟦系统·承诺到期⟧')) {
    const end = Date.now() + DUE_GO_WAIT_MS
    while (!existsSync(P('due-go')) && Date.now() < end) await sleep(100)
    await callTool(sessionId, 'reply', { text: DUE_REPLY })
    const idMatch = all.match(/取消 #(\d+)/) ?? all.match(/#(\d+)/)
    await callTool(sessionId, 'commitment_done', { id: idMatch ? Number(idMatch[1]) : null })
    await callTool(sessionId, 'commitment_list', {})
  } else {
    for (const m of last.matchAll(/!tool:(\w+)\|(.+)/g)) await callTool(sessionId, m[1]!, JSON.parse(m[2]!.trim()))
    await callTool(sessionId, 'reply', { text: last.startsWith('⟦系统') ? '接着刚才的说' : `收到：${lastUserLine(last)}` })
  }
  update(sessionId, { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: '好' } })
  send({ id, result: { stopReason: 'end_turn' } })
}

async function handle(f: any) {
  if (f.id === undefined) return
  const p = f.params ?? {}
  switch (f.method) {
    case 'initialize':
      return send({ id: f.id, result: { protocolVersion: 1, agentInfo: { name: 'fake-acp-due-done', version: '0.0.0' }, agentCapabilities: { loadSession: false } } })
    case 'session/new': {
      const sid = `duedone-${process.pid}-${++seq}`
      const http = (p.mcpServers ?? []).find((m: any) => m.type === 'http')
      sessions.set(sid, http ? { url: http.url, headers: http.headers ?? [] } : null)
      return send({ id: f.id, result: { sessionId: sid, configOptions: configOptions() } })
    }
    case 'session/resume':
      return send({ id: f.id, error: { code: -32602, message: 'session not found' } })
    case 'session/set_config_option':
      return send({ id: f.id, result: { configOptions: configOptions() } })
    case 'session/list':
      return send({ id: f.id, result: { sessions: [] } })
    case 'session/close':
      return send({ id: f.id, result: {} })
    case 'session/prompt':
      return void prompt(f.id, p.sessionId, p.prompt ?? []).catch(e => send({ id: f.id, error: { code: -32603, message: `turn failed: ${e?.message}` } }))
    default:
      return send({ id: f.id, error: { code: -32601, message: `method not found: ${f.method}` } })
  }
}

const dec = new TextDecoder()
let buf = ''
for await (const chunk of Bun.stdin.stream()) {
  buf += dec.decode(chunk, { stream: true })
  let i: number
  while ((i = buf.indexOf('\n')) >= 0) {
    const line = buf.slice(0, i).trim()
    buf = buf.slice(i + 1)
    if (line) void handle(JSON.parse(line))
  }
}
process.exit(0)
