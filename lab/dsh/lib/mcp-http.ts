// 最小 MCP 服务端（Streamable HTTP 传输），只实现 initialize / tools/list / tools/call / ping，按 JSON 直接应答。
// 正式实现会把它挂在网关进程里，每个 dsh 会话一个只属于该聊天的地址和口令。
export type McpTool = {
  name: string
  description: string
  inputSchema: object
  call: (args: any, ctx: { headers: Headers; url: URL }) => Promise<{ text: string; isError?: boolean }>
}

export async function handleMcp(req: Request, tools: McpTool[], log?: (m: string) => void): Promise<Response> {
  if (req.method === 'GET') return new Response('SSE stream not offered', { status: 405 })
  if (req.method === 'DELETE') return new Response(null, { status: 204 })
  if (req.method !== 'POST') return new Response('method not allowed', { status: 405 })
  let msg: any
  try { msg = await req.json() } catch {
    return Response.json({ jsonrpc: '2.0', id: null, error: { code: -32700, message: 'parse error' } }, { status: 400 })
  }
  const url = new URL(req.url)
  const batch = Array.isArray(msg) ? msg : [msg]
  const out: any[] = []
  for (const m of batch) {
    log?.(`mcp <- ${m.method ?? 'response'} ${JSON.stringify(m.params ?? {}).slice(0, 300)}`)
    if (m.id === undefined || m.id === null) continue // 通知，不应答
    if (m.method === 'initialize') {
      out.push({ jsonrpc: '2.0', id: m.id, result: {
        protocolVersion: m.params?.protocolVersion ?? '2025-03-26',
        capabilities: { tools: { listChanged: false } },
        serverInfo: { name: 'lab-mcp', version: '0.0.1' },
      } })
    } else if (m.method === 'tools/list') {
      out.push({ jsonrpc: '2.0', id: m.id, result: { tools: tools.map(({ name, description, inputSchema }) => ({ name, description, inputSchema })) } })
    } else if (m.method === 'tools/call') {
      const tool = tools.find(t => t.name === m.params?.name)
      if (!tool) { out.push({ jsonrpc: '2.0', id: m.id, error: { code: -32602, message: `unknown tool ${m.params?.name}` } }); continue }
      try {
        const r = await tool.call(m.params?.arguments ?? {}, { headers: req.headers, url })
        out.push({ jsonrpc: '2.0', id: m.id, result: { content: [{ type: 'text', text: r.text }], isError: r.isError === true } })
      } catch (e: any) {
        out.push({ jsonrpc: '2.0', id: m.id, result: { content: [{ type: 'text', text: `tool failed: ${e.message}` }], isError: true } })
      }
    } else if (m.method === 'ping') {
      out.push({ jsonrpc: '2.0', id: m.id, result: {} })
    } else {
      out.push({ jsonrpc: '2.0', id: m.id, error: { code: -32601, message: `method not found: ${m.method}` } })
    }
  }
  if (out.length === 0) return new Response(null, { status: 202 })
  return Response.json(Array.isArray(msg) ? out : out[0])
}
