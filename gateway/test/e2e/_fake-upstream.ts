// 本机假模型服务（只绑 127.0.0.1），给真 dsh 的"空完成"用例用（INTERFACE-dsh修补 1.3）。
// 三种接口格式都认：…/chat/completions（OpenAI 对话）、…/responses（OpenAI Responses）、…/messages（Anthropic）。
// "空完成" = 上游正常停止、没有任何内容块。两种形状：empty 完全不带 content 字段；empty-string 的 content 是空字符串。
// reply-then-empty（只支持 OpenAI 对话格式）：还没有工具结果时先调一次 mcp__tg__reply，工具结果回来之后回空完成。
// 只记收到的路径与请求体，不记请求头。
export type UpstreamMode = 'empty' | 'empty-string' | 'reply-then-empty'
export type UpstreamReq = { n: number; path: string; body: any }

const sse = (events: { event?: string; data: unknown }[]) => new Response(
  events.map(e => `${e.event ? `event: ${e.event}\n` : ''}data: ${typeof e.data === 'string' ? e.data : JSON.stringify(e.data)}\n\n`).join(''),
  { headers: { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' } })

function completions(body: any, mode: UpstreamMode, replyText: string): Response {
  const model = String(body?.model ?? 'm')
  const hasToolResult = (body?.messages ?? []).some((m: any) => m?.role === 'tool')
  const callTool = mode === 'reply-then-empty' && !hasToolResult
  const emptyMsg: Record<string, unknown> = mode === 'empty' ? { role: 'assistant' } : { role: 'assistant', content: '' }
  const toolCall = { id: 'call_1', type: 'function', function: { name: 'mcp__tg__reply', arguments: JSON.stringify({ text: replyText }) } }
  const usage = { prompt_tokens: 10, completion_tokens: callTool ? 5 : 0, total_tokens: callTool ? 15 : 10 }
  if (!body?.stream) {
    return Response.json({ id: 'c1', object: 'chat.completion', created: 1, model, usage,
      choices: [{ index: 0, finish_reason: callTool ? 'tool_calls' : 'stop', message: callTool ? { role: 'assistant', content: null, tool_calls: [toolCall] } : emptyMsg }] })
  }
  const chunk = (delta: unknown, finish: string | null) => ({ id: 'c1', object: 'chat.completion.chunk', created: 1, model, choices: [{ index: 0, delta, finish_reason: finish }] })
  return sse([
    { data: chunk(callTool ? { role: 'assistant', content: null, tool_calls: [{ index: 0, ...toolCall }] } : emptyMsg, null) },
    { data: chunk({}, callTool ? 'tool_calls' : 'stop') },
    { data: { id: 'c1', object: 'chat.completion.chunk', created: 1, model, choices: [], usage } },
    { data: '[DONE]' },
  ])
}

function responses(body: any): Response {
  const base = { id: 'resp_1', object: 'response', created_at: 1, model: String(body?.model ?? 'm'), output: [] as unknown[] }
  const done = { ...base, status: 'completed', usage: { input_tokens: 10, output_tokens: 0, total_tokens: 10 } }
  if (!body?.stream) return Response.json(done)
  return sse([
    { event: 'response.created', data: { type: 'response.created', sequence_number: 0, response: { ...base, status: 'in_progress' } } },
    { event: 'response.completed', data: { type: 'response.completed', sequence_number: 1, response: done } },
  ])
}

function messages(body: any): Response {
  const msg = { id: 'msg_1', type: 'message', role: 'assistant', model: String(body?.model ?? 'm'), content: [] as unknown[], stop_sequence: null }
  if (!body?.stream) return Response.json({ ...msg, stop_reason: 'end_turn', usage: { input_tokens: 10, output_tokens: 0 } })
  return sse([
    { event: 'message_start', data: { type: 'message_start', message: { ...msg, stop_reason: null, usage: { input_tokens: 10, output_tokens: 0 } } } },
    { event: 'message_delta', data: { type: 'message_delta', delta: { stop_reason: 'end_turn', stop_sequence: null }, usage: { output_tokens: 0 } } },
    { event: 'message_stop', data: { type: 'message_stop' } },
  ])
}

export function startFakeUpstream(mode: UpstreamMode, replyText = '真dsh发出的回复') {
  const requests: UpstreamReq[] = []
  const server = Bun.serve({
    hostname: '127.0.0.1', port: 0, idleTimeout: 0,
    async fetch(req) {
      const path = new URL(req.url).pathname
      let body: any = null
      try { body = await req.json() } catch {}
      requests.push({ n: requests.length + 1, path, body })
      if (path.endsWith('/chat/completions')) return completions(body, mode, replyText)
      if (path.endsWith('/responses')) return responses(body)
      if (path.endsWith('/messages')) return messages(body)
      return Response.json({ error: 'not found' }, { status: 404 })
    },
  })
  /** 只数"让模型生成"的请求 */
  const generations = () => requests.filter(r => /\/(chat\/completions|responses|messages)$/.test(r.path))
  return { port: server.port as number, requests, generations, replyText, stop: () => server.stop(true) }
}
