// 假模型服务：同时说 OpenAI Chat Completions（dsh 0.1.5 的 deepseek-official 与 llm-pi-ai 的 openai-completions 路由）
// 和 Anthropic Messages（dsh 0.2.0 的 deepseek-official 路由）两种流式协议。把 dsh 实际发出的每个请求原样记下来，
// 用来在没有密钥的情况下核对“模型到底收到了什么”。
//
// 剧本（script）：响应队列。带 match 的条目只在“最后一条用户文本包含 match”时命中；不带 match 的按顺序取；
// 用完后默认回显。每项字段：
//   text        普通文本回复
//   reasoning   思考内容（OpenAI: reasoning_content；Anthropic: thinking 块）
//   tool        { name, arguments }：调用一个工具
//   status/body 直接返回 HTTP 错误
//   delayMs     先等再回
//   inputTokens 覆盖用量里的输入 token 数
import { appendFileSync } from 'fs'

export type Step = {
  match?: string
  text?: string
  reasoning?: string
  tool?: { name: string; arguments: unknown }
  status?: number
  body?: string
  delayMs?: number
  inputTokens?: number
}

export type FakeLlm = { port: number; requests: any[]; stop: () => void }

/** 粗估 token：ASCII 约 4 字符 1 个，其余字符约 1 字符 1 个。只用于假用量，不代表真实分词。 */
export function approxTokens(s: string): number {
  let ascii = 0, other = 0
  for (const ch of s) (ch.charCodeAt(0) < 128 ? ascii++ : other++)
  return Math.ceil(ascii / 4 + other)
}

function textOf(content: any): string {
  if (typeof content === 'string') return content
  if (Array.isArray(content)) return content.filter((b: any) => b?.type === 'text').map((b: any) => b.text).join('\n')
  return ''
}

function lastUserText(messages: any[]): string {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i]
    if (m.role !== 'user') continue
    const t = textOf(m.content)
    if (t) return t
  }
  return ''
}

function sseResponse(frames: string[]): Response {
  const enc = new TextEncoder()
  const body = new ReadableStream({
    start(c) {
      for (const f of frames) c.enqueue(enc.encode(f))
      c.close()
    },
  })
  return new Response(body, { headers: { 'content-type': 'text/event-stream' } })
}

function openAiStream(step: Step, n: number, model: string, inputTokens: number): Response {
  const base = { id: `chatcmpl-${n}`, object: 'chat.completion.chunk', created: Math.floor(Date.now() / 1000), model }
  const chunks: object[] = [{ ...base, choices: [{ index: 0, delta: { role: 'assistant', content: '' }, finish_reason: null }] }]
  if (step.reasoning) chunks.push({ ...base, choices: [{ index: 0, delta: { reasoning_content: step.reasoning }, finish_reason: null }] })
  let finish = 'stop'
  if (step.tool) {
    finish = 'tool_calls'
    chunks.push({ ...base, choices: [{ index: 0, delta: { tool_calls: [{ index: 0, id: `call_${n}`, type: 'function', function: { name: step.tool.name, arguments: JSON.stringify(step.tool.arguments) } }] }, finish_reason: null }] })
  } else {
    chunks.push({ ...base, choices: [{ index: 0, delta: { content: step.text ?? '' }, finish_reason: null }] })
  }
  chunks.push({ ...base, choices: [{ index: 0, delta: {}, finish_reason: finish }] })
  chunks.push({ ...base, choices: [], usage: { prompt_tokens: inputTokens, completion_tokens: 10, total_tokens: inputTokens + 10, prompt_cache_hit_tokens: 0, prompt_cache_miss_tokens: inputTokens } })
  return sseResponse([...chunks.map(c => `data: ${JSON.stringify(c)}\n\n`), 'data: [DONE]\n\n'])
}

function anthropicStream(step: Step, n: number, model: string, inputTokens: number): Response {
  const ev = (type: string, data: object) => `event: ${type}\ndata: ${JSON.stringify({ type, ...data })}\n\n`
  const frames: string[] = [ev('message_start', { message: { id: `msg_${n}`, type: 'message', role: 'assistant', model, content: [], stop_reason: null, stop_sequence: null, usage: { input_tokens: inputTokens, output_tokens: 1, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 } } })]
  let index = 0
  if (step.reasoning) {
    frames.push(ev('content_block_start', { index, content_block: { type: 'thinking', thinking: '', signature: '' } }))
    frames.push(ev('content_block_delta', { index, delta: { type: 'thinking_delta', thinking: step.reasoning } }))
    frames.push(ev('content_block_delta', { index, delta: { type: 'signature_delta', signature: 'fake-signature' } }))
    frames.push(ev('content_block_stop', { index }))
    index++
  }
  let stop = 'end_turn'
  if (step.tool) {
    stop = 'tool_use'
    frames.push(ev('content_block_start', { index, content_block: { type: 'tool_use', id: `toolu_${n}`, name: step.tool.name, input: {} } }))
    frames.push(ev('content_block_delta', { index, delta: { type: 'input_json_delta', partial_json: JSON.stringify(step.tool.arguments) } }))
    frames.push(ev('content_block_stop', { index }))
  } else {
    frames.push(ev('content_block_start', { index, content_block: { type: 'text', text: '' } }))
    frames.push(ev('content_block_delta', { index, delta: { type: 'text_delta', text: step.text ?? '' } }))
    frames.push(ev('content_block_stop', { index }))
  }
  frames.push(ev('message_delta', { delta: { stop_reason: stop, stop_sequence: null }, usage: { output_tokens: 10 } }))
  frames.push(ev('message_stop', {}))
  return sseResponse(frames)
}

export function startFakeLlm(opts: { port: number; script?: Step[]; logPath?: string }): FakeLlm {
  const queue: Step[] = [...(opts.script ?? [])]
  const requests: any[] = []
  let n = 0
  const server = Bun.serve({
    port: opts.port,
    hostname: '127.0.0.1',
    async fetch(req) {
      const url = new URL(req.url)
      if (req.method === 'GET' && url.pathname.endsWith('/models')) {
        return Response.json({ object: 'list', data: [{ id: 'fake-chat', object: 'model' }] })
      }
      if (req.method !== 'POST') return new Response('not found', { status: 404 })
      const raw = await req.text()
      let body: any = {}
      try { body = JSON.parse(raw) } catch {}
      n++
      const headers: Record<string, string> = {}
      req.headers.forEach((v, k) => {
        headers[k] = (k === 'authorization' || k === 'x-api-key') ? `${v.slice(0, 10)}…` : v
      })
      const record = { n, at: new Date().toISOString(), path: url.pathname, headers, body }
      requests.push(record)
      if (opts.logPath) appendFileSync(opts.logPath, JSON.stringify(record) + '\n')

      const anthropic = url.pathname.endsWith('/messages')
      const messages: any[] = body.messages ?? []
      const user = lastUserText(messages)
      let step: Step | undefined
      const idx = queue.findIndex(s => s.match !== undefined && user.includes(s.match))
      if (idx >= 0) step = queue.splice(idx, 1)[0]
      else if (queue.length > 0 && queue[0].match === undefined) step = queue.shift()
      step ??= { text: `收到：${user.slice(-40)}` }
      if (step.delayMs) await Bun.sleep(step.delayMs)
      if (step.status) return new Response(step.body ?? 'error', { status: step.status, headers: { 'content-type': 'application/json' } })
      const inputTokens = step.inputTokens ?? approxTokens(JSON.stringify(body.system ?? '') + JSON.stringify(messages) + JSON.stringify(body.tools ?? []))
      if (body.stream !== true) {
        // 非流式（验证脚本里直接调接口时用）
        return anthropic
          ? Response.json({ id: `msg_${n}`, type: 'message', role: 'assistant', model: body.model, content: [{ type: 'text', text: step.text ?? '' }], stop_reason: 'end_turn', usage: { input_tokens: inputTokens, output_tokens: 10, cache_read_input_tokens: 0 } })
          : Response.json({ id: `chatcmpl-${n}`, object: 'chat.completion', model: body.model, choices: [{ index: 0, message: { role: 'assistant', content: step.text ?? '' }, finish_reason: 'stop' }], usage: { prompt_tokens: inputTokens, completion_tokens: 10, total_tokens: inputTokens + 10 } })
      }
      return anthropic
        ? anthropicStream(step, n, body.model, inputTokens)
        : openAiStream(step, n, body.model, inputTokens)
    },
  })
  return { port: server.port ?? 0, requests, stop: () => server.stop(true) }
}

if (import.meta.main) {
  const port = Number(process.env.FAKE_LLM_PORT ?? 18080)
  const script = process.env.FAKE_LLM_SCRIPT ? await Bun.file(process.env.FAKE_LLM_SCRIPT).json() : []
  startFakeLlm({ port, script, logPath: process.env.FAKE_LLM_LOG })
  console.error(`fake-llm listening on 127.0.0.1:${port}`)
}
