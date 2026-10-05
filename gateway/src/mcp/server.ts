// 给 dsh 用的工具服务（MCP，Streamable HTTP 传输，直接按 JSON 应答）。
// 只听 127.0.0.1。每个段（= 一个 dsh 会话）一个地址 /mcp/<段号> 和一个口令，工具据此知道是哪个聊天，不存在"上一个聊天"。
import type { Server } from 'bun'
import { safeError, type Logger } from '../log'
import { safeEqual } from '../util'

export type ToolCtx = { segmentId: number; chatId: string }
export type ToolResult = { text: string; isError?: boolean }
export type ToolDef = {
  name: string
  description: string
  inputSchema: object
  call: (args: Record<string, unknown>, ctx: ToolCtx) => Promise<ToolResult>
}

export type SegmentResolver = (segmentId: number) => { chatId: string; token: string } | null

export class McpServer {
  private server: Server<undefined> | null = null

  constructor(
    private readonly tools: ToolDef[],
    private readonly resolve: SegmentResolver,
    private readonly log: Logger,
  ) {}

  get port(): number { return this.server?.port ?? 0 }

  start(port = 0): void {
    this.server = Bun.serve({ hostname: '127.0.0.1', port, idleTimeout: 255, fetch: req => this.handle(req) })
    this.log.info('mcp.listening', { port: this.server.port })
  }

  async stop(): Promise<void> { await this.server?.stop(true) }

  /** 交给 dsh 的 mcpServers 参数 */
  serverSpec(segmentId: number, token: string) {
    return [{ type: 'http', name: 'tg', url: `http://127.0.0.1:${this.port}/mcp/${segmentId}`, headers: [{ name: 'Authorization', value: `Bearer ${token}` }] }]
  }

  private async handle(req: Request): Promise<Response> {
    const url = new URL(req.url)
    const m = url.pathname.match(/^\/mcp\/(\d+)$/)
    if (!m) return new Response('not found', { status: 404 })
    if (req.headers.get('origin')) return new Response('forbidden', { status: 403 })
    const seg = this.resolve(Number(m[1]))
    const auth = req.headers.get('authorization') ?? ''
    if (!seg || !safeEqual(auth, `Bearer ${seg.token}`)) return new Response('unauthorized', { status: 401 })
    if (req.method === 'GET') return new Response('SSE stream not offered', { status: 405 })
    if (req.method === 'DELETE') return new Response(null, { status: 204 })
    if (req.method !== 'POST') return new Response('method not allowed', { status: 405 })
    let msg: any
    try { msg = await req.json() } catch {
      return Response.json({ jsonrpc: '2.0', id: null, error: { code: -32700, message: 'parse error' } }, { status: 400 })
    }
    const ctx: ToolCtx = { segmentId: Number(m[1]), chatId: seg.chatId }
    const batch = Array.isArray(msg) ? msg : [msg]
    const out: any[] = []
    for (const r of batch) {
      if (r?.id === undefined || r?.id === null) continue
      out.push(await this.dispatch(r, ctx))
    }
    if (out.length === 0) return new Response(null, { status: 202 })
    return Response.json(Array.isArray(msg) ? out : out[0])
  }

  private async dispatch(r: any, ctx: ToolCtx): Promise<any> {
    switch (r.method) {
      case 'initialize':
        return { jsonrpc: '2.0', id: r.id, result: {
          protocolVersion: r.params?.protocolVersion ?? '2025-03-26',
          capabilities: { tools: { listChanged: false } },
          serverInfo: { name: 'dsh-bot-gateway', version: '1' },
        } }
      case 'ping':
        return { jsonrpc: '2.0', id: r.id, result: {} }
      case 'tools/list':
        return { jsonrpc: '2.0', id: r.id, result: { tools: this.tools.map(({ name, description, inputSchema }) => ({ name, description, inputSchema })) } }
      case 'tools/call': {
        const tool = this.tools.find(t => t.name === r.params?.name)
        if (!tool) return { jsonrpc: '2.0', id: r.id, error: { code: -32602, message: `unknown tool ${String(r.params?.name).slice(0, 60)}` } }
        let res: ToolResult
        try {
          res = await tool.call((r.params?.arguments ?? {}) as Record<string, unknown>, ctx)
        } catch (e) {
          this.log.error('mcp.tool_threw', { tool: tool.name, err: safeError(e) })
          res = { text: '工具内部出错，这次操作没有完成。', isError: true }
        }
        return { jsonrpc: '2.0', id: r.id, result: { content: [{ type: 'text', text: res.text }], isError: res.isError === true } }
      }
      default:
        return { jsonrpc: '2.0', id: r.id, error: { code: -32601, message: 'method not found' } }
    }
  }
}
