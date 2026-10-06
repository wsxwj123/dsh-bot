// 假 ACP 服务（代替 dsh）：按消息里的指令行事，能调用网关的 MCP 工具、报错、卡住、崩溃。
// 状态落盘（会话历史、收到的每个 prompt），这样网关被强杀重启后还能核对"模型有没有两次看到同一条消息"。
//
// 指令写在用户消息里（只看本轮最后一个内容块）：
//   !err:N:tag        这个会话接下来 N 次请求都返回 turn failed（上游 500），补救提示也算
//   !errauth          返回鉴权失败
//   !hang:N:tag       前 N 次卡住，直到收到 session/cancel
//   !hangforever:N:tag 前 N 次卡住，收到 cancel 也不理
//   !crash:N:tag      前 N 次在记下 prompt 后直接退出进程
//   !noreply:N:tag    前 N 次什么工具都不调就结束
//   !silent           调用 stay_silent
//   !parts:K          回复 K 段
//   !replyto:ID       回复时引用消息 ID
//   !file:PATH        回复时附带文件
//   !slow:MS          回复前等 MS 毫秒
//   !react:ID         给消息 ID 加 ❤️
//   !dup              同一轮里把同样的回复再发一遍（模拟真模型偶尔的重复）
//   !dupsome          第二次回复里只有一段和第一次相同
//   !silentafter      回复之后又调用 stay_silent
//   !remember:文字     先调用 remember（文字里的下划线换成空格）
//   !commit:事|时间    先调用 commitment_create（下划线换成空格）
//   !say:文字          回复这句话（下划线换成空格），代替"收到：…"
//   !voice             用语音回复两段（as_voice）
//   !voicetext         回复两段中文，带两段朗读稿（voice_text）
//   !tool:名字|JSON     先调用这个工具（JSON 里不能有空白）
// 程序发来的"⟦系统·承诺到期⟧"：回复一句；state 目录里 due-mode=mute 时改为 stay_silent（模拟没兑现），=cancel 时取消这件承诺
// 程序发来的"⟦系统·整理记忆⟧"：先试着调一次 react（应被锁），再输出一份假摘要（state 目录里 summary-mode=empty 时输出空）
// 没有指令时回复"收到：<对方最后一句>"；补救提示（⟦系统…）回复"接着刚才的说"。
import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'fs'
import { join } from 'path'

const argv = process.argv.slice(2)
const get = (n: string) => { const i = argv.indexOf(`--${n}`); return i >= 0 ? argv[i + 1] : undefined }
const stateDir = get('state')!
const patchPath = get('patch')
mkdirSync(stateDir, { recursive: true })

const P = (f: string) => join(stateDir, f)
const readJson = <T>(f: string, d: T): T => { try { return JSON.parse(readFileSync(P(f), 'utf8')) as T } catch { return d } }
const writeJson = (f: string, v: unknown) => writeFileSync(P(f), JSON.stringify(v, null, 1))
const log = (f: string, v: unknown) => appendFileSync(P(f), JSON.stringify({ pid: process.pid, at: Date.now(), ...v as object }) + '\n')

writeJson(`env-${process.pid}.json`, process.env)
if (patchPath && existsSync(patchPath)) writeFileSync(P('patch-seen.json'), readFileSync(patchPath))
log('lifecycle.jsonl', { event: 'start', argv })

type Sess = { cwd: string; history: string[]; mcp: { url: string; headers: { name: string; value: string }[] } | null; model: string; effort: string; closed?: boolean }
const sessions: Record<string, Sess> = readJson('sessions.json', {})
const loadedHere = new Set<string>()
const counters: Record<string, number> = readJson('counters.json', {})
const saveSessions = () => writeJson('sessions.json', sessions)
const bump = (k: string) => { counters[k] = (counters[k] ?? 0) + 1; writeJson('counters.json', counters); return counters[k]! }

const cancels = new Map<string, () => void>()

function send(f: object) { process.stdout.write(JSON.stringify({ jsonrpc: '2.0', ...f }) + '\n') }
function update(sessionId: string, u: object) { send({ method: 'session/update', params: { sessionId, update: u } }) }

const MODELS = ['deepseek-flash', 'deepseek-v4-pro', 'other-model']
// 和真 dsh 一样，补丁层里 llm-pi-ai 配的路由也列进可选模型
const ROUTE_GROUPS: { group: string; options: { value: string; name: string }[] }[] = (() => {
  try {
    const rows = JSON.parse(readFileSync(patchPath!, 'utf8')) as { id: string; config?: { providers?: Record<string, { models?: { id: string }[] }> } }[]
    const providers = rows.find(r => r.id === 'llm-pi-ai')?.config?.providers ?? {}
    return Object.entries(providers).map(([name, r]) => ({ group: name, options: (r.models ?? []).map(m => ({ value: JSON.stringify([name, m.id]), name: m.id })) }))
  } catch { return [] }
})()
function configOptions(s: Sess) {
  return [
    { id: 'model', name: 'Model', type: 'select', currentValue: s.model, options: [{ group: 'deepseek-official', options: MODELS.map(m => ({ value: JSON.stringify(['deepseek-official', m]), name: m })) }, ...ROUTE_GROUPS] },
    { id: 'reasoning_effort', name: 'Effort', type: 'select', currentValue: s.effort, options: ['off', 'low', 'high', 'max'].map(v => ({ value: v, name: v })) },
  ]
}

let mcpSeq = 1
async function callTool(s: Sess, sessionId: string, name: string, args: object): Promise<string> {
  if (!s.mcp) return 'no mcp'
  const headers: Record<string, string> = { 'content-type': 'application/json' }
  for (const h of s.mcp.headers) headers[h.name] = h.value
  update(sessionId, { sessionUpdate: 'tool_call', toolCallId: `t${mcpSeq}`, title: name, status: 'pending' })
  const res = await fetch(s.mcp.url, { method: 'POST', headers, body: JSON.stringify({ jsonrpc: '2.0', id: mcpSeq++, method: 'tools/call', params: { name, arguments: args } }) })
  const body = await res.json() as any
  const text = String(body?.result?.content?.[0]?.text ?? body?.error?.message ?? `http ${res.status}`)
  log('tool-results.jsonl', { sessionId, name, args, text, isError: body?.result?.isError === true })
  update(sessionId, { sessionUpdate: 'tool_call_update', toolCallId: `t${mcpSeq}`, status: 'completed' })
  return text
}

function directive(text: string, name: string): string[] | null {
  const m = text.match(new RegExp(`!${name}(?![a-z])(?::([^\\s:]+))?(?::([^\\s]+))?`))
  return m ? [m[1] ?? '', m[2] ?? ''] : null
}
/** !name:N:tag 形式：前 N 次生效 */
function firstN(text: string, name: string): boolean {
  const d = directive(text, name)
  if (!d) return false
  const n = Number(d[0] || 1)
  return bump(`${name}:${d[1]}`) <= n
}

function lastUserLine(text: string): string {
  const lines = text.split('\n').filter(l => l.trim() && !l.startsWith('⟦'))
  return (lines[lines.length - 1] ?? '').replace(/![a-z]+(?::\S+)?/g, '').trim()
}

const sleep = (ms: number) => new Promise(r => setTimeout(r, ms))

async function prompt(id: number, sessionId: string, all0: { type: string; text?: string }[]) {
  const s = sessions[sessionId]
  if (!s || !loadedHere.has(sessionId)) return send({ id, error: { code: -32602, message: 'unknown session' } })
  // 图片块：state 目录里有 no-images 时，像看不了图的模型一样整条拒收（不进历史）
  const images = all0.filter(b => b.type === 'image').length
  if (images && existsSync(P('no-images'))) return send({ id, error: { code: -32602, message: 'UNSUPPORTED_CONTENT: route does not accept image input' } })
  const blocks = all0.filter(b => b.type !== 'image')
  const texts = blocks.map(b => b.text ?? '')
  const all = texts.join('\n')
  const last = texts[texts.length - 1] ?? ''
  // 和真 dsh 一样：先把用户消息记进历史，之后无论成败都留着
  s.history.push(all)
  saveSessions()
  log('prompts.jsonl', { sessionId, text: all, blocks: texts.length, images, model: s.model })
  update(sessionId, { sessionUpdate: 'usage_update', used: s.history.join('').length, size: 1_000_000 })

  // 写交接摘要：先试着调一次工具（应该被锁住），再直接输出摘要文字。summary-mode 文件写 empty 时输出空摘要
  if (last.startsWith('⟦系统·整理记忆⟧')) {
    if (s.mcp) await callTool(s, sessionId, 'react', { message_id: 1, emoji: '❤️' })
    const mode = existsSync(P('summary-mode')) ? readFileSync(P('summary-mode'), 'utf8').trim() : ''
    const n = bump('summary')
    const lines = all.split('\n').filter(l => l.includes('对方：') || (!l.startsWith('⟦') && l.trim())).length
    const text = mode === 'empty' ? '' : [
      `【我说过的要紧话】无（假摘要#${n}）`, '【我答应过的事】无', `【对方的情况】材料里大约有 ${lines} 行`, '【正在聊的话题】无', '【我们现在的关系和气氛】平常',
    ].join('\n')
    log('summaries.jsonl', { sessionId, n, mode, withTools: !!s.mcp, chars: text.length })
    if (text) update(sessionId, { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text } })
    return send({ id, result: { stopReason: 'end_turn' } })
  }
  if (firstN(last, 'crash')) { log('lifecycle.jsonl', { event: 'crash' }); process.exit(3) }
  if (directive(last, 'errauth')) return send({ id, error: { code: -32603, message: 'turn failed: MISSING_CREDENTIAL deepseek-official requires DEEPSEEK_API_KEY' } })
  // !err 和真上游一样"粘"在会话上：一旦触发，这个会话接下来的 N 次请求都失败（包括补救提示）
  const errD = directive(last, 'err')
  if (errD && !counters[`err-armed:${errD[1]}`]) { counters[`err-armed:${errD[1]}`] = 1; counters[`err-left:${sessionId}`] = Number(errD[0] || 1); writeJson('counters.json', counters) }
  if ((counters[`err-left:${sessionId}`] ?? 0) > 0) {
    counters[`err-left:${sessionId}`]!--
    writeJson('counters.json', counters)
    return send({ id, error: { code: -32603, message: 'turn failed: upstream returned 500 Internal Server Error' } })
  }
  if (firstN(last, 'hangforever')) { await new Promise(() => {}); return }
  if (firstN(last, 'hang')) {
    await new Promise<void>(r => cancels.set(sessionId, r))
    cancels.delete(sessionId)
    return send({ id, result: { stopReason: 'cancelled' } })
  }
  if (firstN(last, 'noreply')) { update(sessionId, { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: '（自言自语）' } }); return send({ id, result: { stopReason: 'end_turn' } }) }
  const slow = directive(last, 'slow')
  if (slow) await sleep(Number(slow[0]) || 500)
  if (directive(last, 'silent')) {
    await callTool(s, sessionId, 'stay_silent', { reason: 'test' })
    return send({ id, result: { stopReason: 'end_turn' } })
  }
  const dueMode = last.includes('⟦系统·承诺到期⟧') && existsSync(P('due-mode')) ? readFileSync(P('due-mode'), 'utf8') : ''
  if (dueMode.includes('mute')) {
    await callTool(s, sessionId, 'stay_silent', { reason: 'test mute' })
    return send({ id, result: { stopReason: 'end_turn' } })
  }
  if (dueMode.includes('cancel')) { // 到点时觉得不需要了：取消，不回复也不调 stay_silent
    await callTool(s, sessionId, 'commitment_cancel', { id: Number(last.match(/取消 #(\d+)/)?.[1]), reason: 'test cancel' })
    return send({ id, result: { stopReason: 'end_turn' } })
  }
  const cm = last.match(/!commit:([^|\s]+)\|(\S+)/)
  if (cm) await callTool(s, sessionId, 'commitment_create', { content: cm[1]!.replace(/_/g, ' '), when: cm[2]!.replace(/_/g, ' ') })
  const tl = last.match(/!tool:(\w+)\|(\S+)/)
  if (tl) await callTool(s, sessionId, tl[1]!, JSON.parse(tl[2]!))
  const rem = last.match(/!remember:(\S+)/)
  if (rem) await callTool(s, sessionId, 'remember', { text: rem[1]!.replace(/_/g, ' ') })
  const react = directive(last, 'react')
  if (react) await callTool(s, sessionId, 'react', { message_id: Number(react[0]), emoji: '❤️' })
  const parts = Number(directive(last, 'parts')?.[0] || 1)
  if (directive(last, 'voicetext')) {
    await callTool(s, sessionId, 'reply', { text: '中文第一段\n\n中文第二段', as_voice: true, voice_text: 'にほんご いち\n\nにほんご に' })
    return send({ id, result: { stopReason: 'end_turn' } })
  }
  if (directive(last, 'voice')) {
    await callTool(s, sessionId, 'reply', { text: '语音第一段\n\n语音第二段', as_voice: true, voice_emotion: 'HAPPY' })
    return send({ id, result: { stopReason: 'end_turn' } })
  }
  const say = last.match(/!say:(\S+)/)
  const base = say ? say[1]!.replace(/_/g, ' ') : last.startsWith('⟦系统') ? '接着刚才的说' : `收到：${lastUserLine(last)}`
  const text = parts > 1 ? Array.from({ length: parts }, (_, i) => `${base}（第${i + 1}段）`).join('\n\n') : base
  const args: Record<string, unknown> = { text }
  const rt = directive(last, 'replyto')
  if (rt) args.reply_to = Number(rt[0])
  // 文件路径里可能有冒号（Windows 的盘符），不能用通用的指令解析
  const file = last.match(/!file:(\S+)/)
  if (file) args.files = [file[1]]
  await callTool(s, sessionId, 'reply', args)
  if (directive(last, 'dup')) await callTool(s, sessionId, 'reply', args)
  if (directive(last, 'dupsome')) await callTool(s, sessionId, 'reply', { text: `${base}（第1段）\n\n新的一段甲\n\n新的一段乙` })
  if (directive(last, 'silentafter')) await callTool(s, sessionId, 'stay_silent', { reason: 'test' })
  update(sessionId, { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: '好' } })
  send({ id, result: { stopReason: 'end_turn' } })
}

let sessionSeq = Object.keys(sessions).length
async function handle(f: any) {
  if (f.id === undefined) {
    if (f.method === 'session/cancel') { log('cancels.jsonl', { sessionId: f.params?.sessionId }); cancels.get(f.params?.sessionId)?.() }
    return
  }
  const p = f.params ?? {}
  switch (f.method) {
    case 'initialize':
      return send({ id: f.id, result: { protocolVersion: 1, agentInfo: { name: 'fake-acp', version: '0.0.0' }, agentCapabilities: { loadSession: false } } })
    case 'session/new': {
      const sid = `fake-${process.pid}-${++sessionSeq}`
      const http = (p.mcpServers ?? []).find((m: any) => m.type === 'http')
      sessions[sid] = { cwd: p.cwd, history: [], mcp: http ? { url: http.url, headers: http.headers ?? [] } : null, model: JSON.stringify(['deepseek-official', 'deepseek-flash']), effort: 'off' }
      loadedHere.add(sid)
      saveSessions()
      log('sessions.jsonl', { event: 'new', sessionId: sid })
      return send({ id: f.id, result: { sessionId: sid, configOptions: configOptions(sessions[sid]!) } })
    }
    case 'session/resume': {
      const s = sessions[p.sessionId]
      if (!s || s.closed) return send({ id: f.id, error: { code: -32602, message: 'session not found' } })
      if (s.cwd !== p.cwd) return send({ id: f.id, error: { code: -32602, message: 'cwd mismatch' } })
      if (loadedHere.has(p.sessionId)) return send({ id: f.id, error: { code: -32602, message: 'already loaded' } })
      const http = (p.mcpServers ?? []).find((m: any) => m.type === 'http')
      s.mcp = http ? { url: http.url, headers: http.headers ?? [] } : null
      loadedHere.add(p.sessionId)
      saveSessions()
      log('sessions.jsonl', { event: 'resume', sessionId: p.sessionId })
      return send({ id: f.id, result: { configOptions: configOptions(s) } })
    }
    case 'session/set_config_option': {
      const s = sessions[p.sessionId]
      if (!s) return send({ id: f.id, error: { code: -32602, message: 'unknown session' } })
      log('config.jsonl', { sessionId: p.sessionId, configId: p.configId, value: p.value })
      if (p.configId === 'model') s.model = p.value
      if (p.configId === 'reasoning_effort') s.effort = p.value
      saveSessions()
      return send({ id: f.id, result: { configOptions: configOptions(s) } })
    }
    case 'session/list':
      return send({ id: f.id, result: { sessions: [] } })
    case 'session/close':
      if (sessions[p.sessionId]) { sessions[p.sessionId]!.closed = true; saveSessions() }
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
log('lifecycle.jsonl', { event: 'stdin-closed' })
process.exit(0)
