// 最小 ACP v1 客户端：按行分隔的 JSON-RPC 2.0，走子进程的标准输入输出。不依赖任何第三方包。
// 只用于实验与验证脚本；正式实现会在 M1 里另写（带超时、重连、背压与日志）。
import type { Subprocess } from 'bun'

export type Frame = {
  jsonrpc: '2.0'
  id?: number | string
  method?: string
  params?: any
  result?: any
  error?: { code: number; message: string; data?: unknown }
}

export class AcpClient {
  readonly proc: Subprocess<'pipe', 'pipe', 'pipe'>
  readonly exited: Promise<number>
  readonly updates: any[] = []
  stderr = ''
  /** 收到 session/request_permission 时的固定答复。无人值守时一律拒绝。 */
  permissionAnswer: 'allow-once' | 'reject-once' = 'reject-once'
  permissionRequests = 0
  onUpdate?: (params: any) => void
  private nextId = 1
  private pending = new Map<number, { resolve: (v: any) => void; reject: (e: any) => void; method: string }>()

  constructor(cmd: string[], opts: { env: Record<string, string>; cwd: string }) {
    this.proc = Bun.spawn(cmd, { stdin: 'pipe', stdout: 'pipe', stderr: 'pipe', env: opts.env, cwd: opts.cwd })
    this.exited = this.proc.exited
    void this.readLoop()
    void this.readStderr()
  }

  private async readStderr() {
    const dec = new TextDecoder()
    for await (const chunk of this.proc.stderr) this.stderr += dec.decode(chunk)
  }

  private async readLoop() {
    const dec = new TextDecoder()
    let buf = ''
    for await (const chunk of this.proc.stdout) {
      buf += dec.decode(chunk, { stream: true })
      let i: number
      while ((i = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, i).trim()
        buf = buf.slice(i + 1)
        if (!line) continue
        let f: Frame
        try { f = JSON.parse(line) } catch { this.stderr += `[non-json stdout] ${line}\n`; continue }
        this.handle(f)
      }
    }
    for (const p of this.pending.values()) p.reject(new Error(`process exited before response to ${p.method}`))
    this.pending.clear()
  }

  private handle(f: Frame) {
    if (f.method && f.id !== undefined) {
      if (f.method === 'session/request_permission') {
        this.permissionRequests++
        this.write({ jsonrpc: '2.0', id: f.id, result: { outcome: { outcome: 'selected', optionId: this.permissionAnswer } } })
      } else {
        this.write({ jsonrpc: '2.0', id: f.id, error: { code: -32601, message: `client does not support ${f.method}` } })
      }
      return
    }
    if (f.method) {
      if (f.method === 'session/update') { this.updates.push(f.params); this.onUpdate?.(f.params) }
      return
    }
    const p = this.pending.get(Number(f.id))
    if (!p) return
    this.pending.delete(Number(f.id))
    if (f.error) p.reject(Object.assign(new Error(f.error.message), { code: f.error.code, data: f.error.data }))
    else p.resolve(f.result)
  }

  private write(f: Frame) {
    this.proc.stdin.write(JSON.stringify(f) + '\n')
    this.proc.stdin.flush()
  }

  request<T = any>(method: string, params: any, timeoutMs = 120_000): Promise<T> {
    const id = this.nextId++
    return new Promise<T>((resolve, reject) => {
      const t = setTimeout(() => { this.pending.delete(id); reject(new Error(`timeout waiting for ${method}`)) }, timeoutMs)
      this.pending.set(id, {
        resolve: v => { clearTimeout(t); resolve(v) },
        reject: e => { clearTimeout(t); reject(e) },
        method,
      })
      this.write({ jsonrpc: '2.0', id, method, params })
    })
  }

  notify(method: string, params: any) { this.write({ jsonrpc: '2.0', method, params }) }

  initialize() {
    return this.request('initialize', {
      protocolVersion: 1,
      clientCapabilities: { fs: { readTextFile: false, writeTextFile: false }, terminal: false },
    })
  }

  /** 发一条纯文本 prompt，返回 stopReason；出错时抛出带 code 的异常。 */
  prompt(sessionId: string, text: string, timeoutMs = 300_000) {
    return this.request<{ stopReason: string }>('session/prompt', { sessionId, prompt: [{ type: 'text', text }] }, timeoutMs)
  }

  /** 取某个会话在 [from, to) 区间内模型输出的可见文本（agent_message_chunk 拼接）。 */
  messageText(sessionId: string, from = 0, to = this.updates.length): string {
    return this.updates.slice(from, to)
      .filter(u => u.sessionId === sessionId && u.update?.sessionUpdate === 'agent_message_chunk' && u.update.content?.type === 'text')
      .map(u => u.update.content.text).join('')
  }

  async close() {
    try { this.proc.stdin.end() } catch {}
    const t = setTimeout(() => this.proc.kill('SIGKILL'), 15_000)
    const code = await this.exited
    clearTimeout(t)
    return code
  }
}
