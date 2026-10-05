// 用量记录代理：在本机监听，把 dsh 发往 DeepSeek 的请求原样转发到真实地址，同时记下
//   - 请求的形状：路径、模型、消息条数、系统提示词字数、工具个数、历史里携带的思考内容字数
//   - 供应商回报的用量：输入 token、缓存命中 / 未命中、输出 token
// 兼容两种协议：OpenAI Chat Completions（dsh 0.1.5）与 Anthropic Messages（dsh 0.2.0）。
// 密钥只在转发时原样带上，从不写进记录；默认也不记录对话正文（LOG_BODIES=1 才记）。

export type TapRecord = {
  n: number
  at: string
  path: string
  status: number
  model?: string
  purpose: 'chat' | 'compaction' | 'other'
  messages: number
  systemChars: number
  tools: number
  toolsJsonChars: number
  historyReasoningChars: number
  lastUserPreview: string
  usage: {
    input?: number
    cacheHit?: number
    cacheMiss?: number
    output?: number
    reasoning?: number
    raw?: any
  }
  /** 只对压缩请求保留模型输出的文本（用来展示出厂压缩写出的摘要长什么样）。 */
  outputText?: string
  body?: any
}

/** 从流式正文里拼出模型输出的可见文本（两种协议都认）。 */
export function parseOutputText(text: string): string {
  let out = ''
  for (const line of text.split('\n')) {
    if (!line.startsWith('data:')) continue
    const data = line.slice(5).trim()
    if (!data || data === '[DONE]') continue
    try {
      const obj = JSON.parse(data)
      const d = obj?.choices?.[0]?.delta?.content
      if (typeof d === 'string') out += d
      if (obj?.type === 'content_block_delta' && obj.delta?.type === 'text_delta') out += obj.delta.text ?? ''
    } catch {}
  }
  return out
}

function textOf(content: any): string {
  if (typeof content === 'string') return content
  if (Array.isArray(content)) return content.filter((b: any) => b?.type === 'text').map((b: any) => b.text ?? '').join('\n')
  return ''
}

function describeRequest(body: any) {
  const messages: any[] = Array.isArray(body?.messages) ? body.messages : []
  let systemChars = 0
  if (typeof body?.system === 'string') systemChars += body.system.length
  else if (Array.isArray(body?.system)) systemChars += textOf(body.system).length
  for (const m of messages) if (m.role === 'system' || m.role === 'developer') systemChars += textOf(m.content).length
  let historyReasoningChars = 0
  for (const m of messages) {
    if (m.role !== 'assistant') continue
    if (typeof m.reasoning_content === 'string') historyReasoningChars += m.reasoning_content.length
    if (Array.isArray(m.content)) for (const b of m.content) if (b?.type === 'thinking' && typeof b.thinking === 'string') historyReasoningChars += b.thinking.length
  }
  let lastUser = ''
  for (let i = messages.length - 1; i >= 0; i--) if (messages[i].role === 'user') { lastUser = textOf(messages[i].content); if (lastUser) break }
  const tools: any[] = Array.isArray(body?.tools) ? body.tools : []
  return {
    model: body?.model,
    messages: messages.filter(m => m.role !== 'system' && m.role !== 'developer').length,
    systemChars,
    tools: tools.length,
    toolsJsonChars: tools.length ? JSON.stringify(tools).length : 0,
    historyReasoningChars,
    purpose: (/compaction engine/i.test(lastUser) ? 'compaction' : 'chat') as TapRecord['purpose'],
    lastUserPreview: lastUser.replace(/\s+/g, ' ').slice(0, 40),
  }
}

/** 从流式或非流式响应正文里取用量。两种协议都认。 */
export function parseUsage(text: string): TapRecord['usage'] {
  const u: TapRecord['usage'] = {}
  const take = (obj: any) => {
    if (!obj || typeof obj !== 'object') return
    // OpenAI / DeepSeek Chat Completions
    if (obj.usage && typeof obj.usage.prompt_tokens === 'number') {
      u.input = obj.usage.prompt_tokens
      u.output = obj.usage.completion_tokens
      if (typeof obj.usage.prompt_cache_hit_tokens === 'number') u.cacheHit = obj.usage.prompt_cache_hit_tokens
      if (typeof obj.usage.prompt_cache_miss_tokens === 'number') u.cacheMiss = obj.usage.prompt_cache_miss_tokens
      const r = obj.usage.completion_tokens_details?.reasoning_tokens
      if (typeof r === 'number') u.reasoning = r
      u.raw = obj.usage
    }
    // Anthropic Messages：message_start 带输入用量，message_delta 带输出用量
    if (obj.type === 'message_start' && obj.message?.usage) {
      const mu = obj.message.usage
      u.input = (mu.input_tokens ?? 0) + (mu.cache_read_input_tokens ?? 0) + (mu.cache_creation_input_tokens ?? 0)
      if (typeof mu.cache_read_input_tokens === 'number') u.cacheHit = mu.cache_read_input_tokens
      if (typeof mu.input_tokens === 'number') u.cacheMiss = mu.input_tokens
      u.raw = { ...(u.raw ?? {}), message_start: mu }
    }
    if (obj.type === 'message_delta' && obj.usage) {
      if (typeof obj.usage.output_tokens === 'number') u.output = obj.usage.output_tokens
      u.raw = { ...(u.raw ?? {}), message_delta: obj.usage }
    }
    if (obj.type === 'message' && obj.usage) { // 非流式 Anthropic
      const mu = obj.usage
      u.input = (mu.input_tokens ?? 0) + (mu.cache_read_input_tokens ?? 0)
      u.cacheHit = mu.cache_read_input_tokens
      u.output = mu.output_tokens
      u.raw = mu
    }
  }
  const trimmed = text.trim()
  if (trimmed.startsWith('{')) { try { take(JSON.parse(trimmed)) } catch {} return u }
  for (const line of text.split('\n')) {
    if (!line.startsWith('data:')) continue
    const data = line.slice(5).trim()
    if (!data || data === '[DONE]') continue
    try { take(JSON.parse(data)) } catch {}
  }
  return u
}

export type Tap = { port: number; records: TapRecord[]; stop: () => void; settled: () => Promise<void> }

/**
 * DeepSeek 官方地址：dsh 0.1.5 走 Chat Completions（https://api.deepseek.com/chat/completions），
 * dsh 0.2.0 走 Anthropic Messages（https://api.deepseek.com/anthropic/v1/messages 与 /v1/files）。
 * 把 DEEPSEEK_BASE_URL 指到本代理后，按路径把请求送回对应的官方地址。
 */
export function deepseekUpstream(path: string): string {
  const root = (process.env.DEEPSEEK_UPSTREAM ?? 'https://api.deepseek.com').replace(/\/+$/, '')
  if (/^\/v1\/(messages|files)/.test(path)) return `${root}/anthropic${path}`
  return `${root}${path}`
}

export function startUsageTap(opts: { port: number; upstream: string | ((path: string) => string); logBodies?: boolean }): Tap {
  const records: TapRecord[] = []
  const inflight = new Set<Promise<void>>()
  let n = 0
  const resolveTarget = typeof opts.upstream === 'function'
    ? opts.upstream
    : ((root: string) => (path: string) => root + path)(opts.upstream.replace(/\/+$/, ''))
  const server = Bun.serve({
    port: opts.port,
    hostname: '127.0.0.1',
    idleTimeout: 255,
    async fetch(req) {
      const url = new URL(req.url)
      const target = resolveTarget(url.pathname) + url.search
      const reqBody = req.method === 'GET' || req.method === 'HEAD' ? undefined : await req.arrayBuffer()
      const headers = new Headers(req.headers)
      headers.delete('host')
      headers.delete('content-length')
      headers.delete('accept-encoding') // 让上游回明文，便于边转发边解析
      let parsed: any = undefined
      if (reqBody && reqBody.byteLength > 0) { try { parsed = JSON.parse(new TextDecoder().decode(reqBody)) } catch {} }
      const id = ++n
      let resp: Response
      try {
        resp = await fetch(target, { method: req.method, headers, body: reqBody && reqBody.byteLength > 0 ? reqBody : undefined })
      } catch (e: any) {
        records.push({ n: id, at: new Date().toISOString(), path: url.pathname, status: 0, ...describeRequest(parsed), usage: {}, ...(opts.logBodies ? { body: parsed } : {}) })
        return new Response(`usage-tap: upstream fetch failed: ${e.message}`, { status: 502 })
      }
      const outHeaders = new Headers(resp.headers)
      outHeaders.delete('content-encoding')
      outHeaders.delete('content-length')
      const rec: TapRecord = { n: id, at: new Date().toISOString(), path: url.pathname, status: resp.status, ...describeRequest(parsed), usage: {}, ...(opts.logBodies ? { body: parsed } : {}) }
      records.push(rec)
      if (!resp.body) return new Response(null, { status: resp.status, headers: outHeaders })
      const [toClient, toParse] = resp.body.tee()
      const p = (async () => {
        const text = await new Response(toParse).text()
        rec.usage = parseUsage(text)
        if (rec.purpose === 'compaction') rec.outputText = parseOutputText(text)
      })().catch(() => {}).finally(() => inflight.delete(p))
      inflight.add(p)
      return new Response(toClient, { status: resp.status, headers: outHeaders })
    },
  })
  return {
    port: server.port,
    records,
    stop: () => server.stop(true),
    settled: async () => { await Promise.all([...inflight]) },
  }
}
