// ACP v1 客户端：按行分隔的 JSON-RPC 2.0，走 dsh 子进程的标准输入输出。
// 服务端发来的请求一律拒绝（权限请求、读写文件、终端），因为机器人不需要它们；收到权限请求会记一条警告。
import type { FileSink } from 'bun'
import { redact, type Logger } from '../log'

export type Frame = {
  jsonrpc: '2.0'
  id?: number | string | null
  method?: string
  params?: any
  result?: any
  error?: { code: number; message: string; data?: unknown }
}

export class AcpError extends Error {
  override name = 'AcpError'
  constructor(readonly method: string, readonly code: number, message: string, readonly data?: unknown) {
    super(`${method}: ${code} ${redact(message).slice(0, 300)}`)
  }
}

export class AcpExited extends Error {
  override name = 'AcpExited'
  constructor(readonly method: string) { super(`dsh exited before answering ${method}`) }
}

export class AcpTimeout extends Error {
  override name = 'AcpTimeout'
  constructor(readonly method: string, readonly ms: number) { super(`${method} timed out after ${ms}ms`) }
}

export type SessionUpdate = { sessionId: string; update: { sessionUpdate: string; [k: string]: any } }

export class AcpConnection {
  private nextId = 1
  private pending = new Map<number, { resolve: (v: any) => void; reject: (e: Error) => void; method: string; timer?: ReturnType<typeof setTimeout> }>()
  private listeners = new Map<string, (u: SessionUpdate) => void>()
  private closed = false
  permissionRequests = 0

  constructor(
    private readonly stdin: FileSink,
    stdout: ReadableStream<Uint8Array>,
    private readonly log: Logger,
  ) {
    void this.readLoop(stdout)
  }

  get isClosed(): boolean { return this.closed }

  private async readLoop(stdout: ReadableStream<Uint8Array>): Promise<void> {
    const dec = new TextDecoder()
    let buf = ''
    try {
      for await (const chunk of stdout) {
        buf += dec.decode(chunk, { stream: true })
        let i: number
        while ((i = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, i).trim()
          buf = buf.slice(i + 1)
          if (!line) continue
          let f: Frame
          try { f = JSON.parse(line) } catch { this.log.debug('acp.non_json_stdout', { chars: line.length }); continue }
          try { this.handle(f) } catch (e) { this.log.warn('acp.handle_failed', { err: String((e as Error)?.message ?? e).slice(0, 200) }) }
        }
      }
    } catch {}
    this.closed = true
    for (const p of this.pending.values()) { if (p.timer) clearTimeout(p.timer); p.reject(new AcpExited(p.method)) }
    this.pending.clear()
  }

  private handle(f: Frame): void {
    if (f.method && f.id !== undefined && f.id !== null) {
      if (f.method === 'session/request_permission') {
        this.permissionRequests++
        this.log.warn('acp.permission_request_rejected', { tool: String(f.params?.toolCall?.title ?? f.params?.toolCall?.kind ?? '').slice(0, 80) })
        const opts: any[] = Array.isArray(f.params?.options) ? f.params.options : []
        const reject = opts.find(o => o?.kind === 'reject_once') ?? opts.find(o => String(o?.kind ?? '').startsWith('reject'))
        this.write(reject
          ? { jsonrpc: '2.0', id: f.id, result: { outcome: { outcome: 'selected', optionId: reject.optionId } } }
          : { jsonrpc: '2.0', id: f.id, result: { outcome: { outcome: 'cancelled' } } })
      } else {
        this.write({ jsonrpc: '2.0', id: f.id, error: { code: -32601, message: `client does not support ${f.method}` } })
      }
      return
    }
    if (f.method) {
      if (f.method === 'session/update' && f.params?.sessionId) {
        this.listeners.get(String(f.params.sessionId))?.(f.params as SessionUpdate)
      }
      return
    }
    const id = Number(f.id)
    const p = this.pending.get(id)
    if (!p) return
    this.pending.delete(id)
    if (p.timer) clearTimeout(p.timer)
    if (f.error) p.reject(new AcpError(p.method, f.error.code, f.error.message ?? '', f.error.data))
    else p.resolve(f.result)
  }

  private write(f: Frame): void {
    if (this.closed) return
    try {
      this.stdin.write(JSON.stringify(f) + '\n')
      this.stdin.flush()
    } catch {
      this.closed = true
    }
  }

  request<T = any>(method: string, params: any, timeoutMs = 60_000): Promise<T> {
    if (this.closed) return Promise.reject(new AcpExited(method))
    const id = this.nextId++
    return new Promise<T>((resolve, reject) => {
      const timer = timeoutMs > 0 ? setTimeout(() => { this.pending.delete(id); reject(new AcpTimeout(method, timeoutMs)) }, timeoutMs) : undefined
      this.pending.set(id, { resolve, reject, method, timer })
      this.write({ jsonrpc: '2.0', id, method, params })
    })
  }

  notify(method: string, params: any): void { this.write({ jsonrpc: '2.0', method, params }) }

  onSession(sessionId: string, fn: ((u: SessionUpdate) => void) | null): void {
    if (fn) this.listeners.set(sessionId, fn)
    else this.listeners.delete(sessionId)
  }

  initialize() {
    return this.request<{ protocolVersion: number; agentInfo?: { name?: string; version?: string }; agentCapabilities?: any }>('initialize', {
      protocolVersion: 1,
      clientCapabilities: { fs: { readTextFile: false, writeTextFile: false }, terminal: false },
    }, 60_000)
  }
}
