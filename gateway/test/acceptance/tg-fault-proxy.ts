// outage-delivery 验收用的 TCP 转发层：放在网关与假 Telegram 之间，随时可切换三种模式——
//   forward：原样转发；reset：读完一个完整请求就掐断连接（网关拿到 ECONNRESET / 连接被关）；hang：收下请求不回。
// 也能只对满足条件的某一个请求出故障（rules），并可在出故障的同时把整体模式切走（thenMode，用来演「这一段失败、随后探测也失败」）。
// 按 HTTP/1.1 请求边界切分（Content-Length 或 chunked），记下网关发出的每一个请求和对它采取的动作，
// 这样「Telegram 收不到」可以分成两层核对：假 Telegram 真收到了什么（tg.sent）、网关到底有没有发（seen）。
// 只用 node:net，不碰网关代码，不改共享假件。
import net from 'node:net'

export type ProxyMode = 'forward' | 'reset' | 'hang'

export type SeenRequest = {
  at: number
  /** HTTP 方法 */
  verb: string
  path: string
  /** Bot API 方法名（/bot<令牌>/<方法>），对不上就是原始路径 */
  tgMethod: string
  /** 请求体按 UTF-8 解出的文字（multipart 也照样解，用来搜段落文字、chat_id） */
  body: string
  action: ProxyMode
}

export type ProxyRule = {
  match: (r: { tgMethod: string; path: string; body: string }) => boolean
  action: 'reset' | 'hang'
  /** 生效几次（默认 1） */
  times?: number
  /** 命中后把整体模式切成这个（不写 = 不动） */
  thenMode?: ProxyMode
}

type Parsed = { size: number; raw: Buffer; verb: string; path: string; tgMethod: string; body: string }

export class TgFaultProxy {
  mode: ProxyMode = 'forward'
  rules: ProxyRule[] = []
  seen: SeenRequest[] = []
  port = 0
  private readonly server: net.Server
  private readonly conns = new Set<net.Socket>()

  constructor(private readonly upstreamPort: number) {
    this.server = net.createServer(c => this.onClient(c))
  }

  async start(): Promise<void> {
    await new Promise<void>((resolve, reject) => {
      this.server.once('error', reject)
      this.server.listen(0, '127.0.0.1', () => resolve())
    })
    this.port = (this.server.address() as net.AddressInfo).port
  }

  get url(): string { return `http://127.0.0.1:${this.port}` }

  stop(): void {
    for (const c of this.conns) c.destroy()
    this.server.close()
  }

  /** since 之后网关发出的请求（可按 Bot API 方法名过滤） */
  requests(tgMethod?: string, since = 0): SeenRequest[] {
    return this.seen.filter(r => r.at >= since && (!tgMethod || r.tgMethod === tgMethod))
  }

  private decide(req: Parsed): ProxyMode {
    if (this.mode !== 'forward') return this.mode
    const i = this.rules.findIndex(r => r.match(req))
    if (i < 0) return 'forward'
    const rule = this.rules[i]!
    rule.times = (rule.times ?? 1) - 1
    if (rule.times <= 0) this.rules.splice(i, 1)
    if (rule.thenMode) this.mode = rule.thenMode
    return rule.action
  }

  private onClient(client: net.Socket): void {
    this.conns.add(client)
    let upstream: net.Socket | null = null
    let buf = Buffer.alloc(0)
    let dead = false
    const kill = (reset: boolean) => {
      if (dead) return
      dead = true
      const c = client as net.Socket & { resetAndDestroy?: () => void }
      try {
        if (reset && typeof c.resetAndDestroy === 'function') c.resetAndDestroy()
        else client.destroy()
      } catch { client.destroy() }
      upstream?.destroy()
    }
    client.on('error', () => kill(false))
    client.on('close', () => { this.conns.delete(client); upstream?.destroy() })
    const toUpstream = (): net.Socket => {
      if (upstream) return upstream
      const up = net.connect(this.upstreamPort, '127.0.0.1')
      up.on('data', d => {
        // 切到故障模式时，已经在路上的回包（比如长轮询）也不再交给网关
        if (this.mode === 'reset') return kill(true)
        if (this.mode === 'hang') return
        client.write(d)
      })
      up.on('error', () => kill(false))
      up.on('close', () => { if (!dead) { dead = true; client.end() } })
      upstream = up
      return up
    }
    client.on('data', d => {
      if (dead) return
      buf = Buffer.concat([buf, d])
      for (;;) {
        const req = takeRequest(buf)
        if (!req) break
        buf = buf.subarray(req.size)
        const action = this.decide(req)
        this.seen.push({ at: Date.now(), verb: req.verb, path: req.path, tgMethod: req.tgMethod, body: req.body, action })
        if (action === 'reset') { kill(true); return }
        if (action === 'hang') continue
        toUpstream().write(req.raw)
      }
    })
  }
}

/** 从缓冲区头上切出一个完整的 HTTP 请求；还不完整就返回 null */
function takeRequest(buf: Buffer): Parsed | null {
  const headEnd = buf.indexOf('\r\n\r\n')
  if (headEnd < 0) return null
  const head = buf.subarray(0, headEnd).toString('latin1')
  const [line = ''] = head.split('\r\n')
  const [verb = '', path = ''] = line.split(' ')
  const bodyStart = headEnd + 4
  let size: number
  const cl = /\r\ncontent-length:\s*(\d+)/i.exec(head)
  if (cl) {
    size = bodyStart + Number(cl[1])
    if (buf.length < size) return null
  } else if (/\r\ntransfer-encoding:\s*chunked/i.test(head)) {
    let p = bodyStart
    for (;;) {
      const lineEnd = buf.indexOf('\r\n', p)
      if (lineEnd < 0) return null
      const n = parseInt(buf.subarray(p, lineEnd).toString('latin1'), 16)
      if (Number.isNaN(n)) { p = buf.length; break }
      p = lineEnd + 2 + n + 2
      if (buf.length < p) return null
      if (n === 0) break
    }
    size = p
  } else {
    size = bodyStart
  }
  const tgMethod = /\/bot[^/]+\/(\w+)/.exec(path)?.[1] ?? path
  return { size, raw: buf.subarray(0, size), verb, path, tgMethod, body: buf.subarray(bodyStart, size).toString('utf8') }
}
