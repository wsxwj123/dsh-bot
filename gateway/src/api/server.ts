// 本机接口：给 Python 周边脚本、看门狗和 bot 之间互推用。
// 安全：只听 127.0.0.1；要口令（state/api.key，权限 600，常量时间比较）；带 Origin 头一律拒绝（防浏览器里的网页跨站调用）；
// 只收 application/json；/v1/send 发文件只允许白名单目录。
import type { Server } from 'bun'
import { loadAccess } from '../config'
import type { Ledger } from '../ledger'
import { safeError, type Logger } from '../log'
import { allowedFile, describeResults, type Sender } from '../telegram/sender'
import { randomToken, safeEqual } from '../util'

export type ApiDeps = {
  token: string
  port: number
  ledger: Ledger
  log: Logger
  sender: Sender
  channelDir: string
  allowDirs: () => string[]
  /** 电话里 bot 说过的话 → 登记承诺，返回要告诉模型的话 */
  callPromises?: (chatId: string, lines: string[]) => string[]
  health: () => { ok: boolean } & Record<string, unknown>
  /** 管理台看模型、换模型（和 /model 命令同一套逻辑） */
  model?: { info: () => Promise<unknown>; set: (spec: string) => Promise<{ ok: boolean }> }
  /** 管理台：用 /provider add 建的供应商（不含密钥），以及重新拉模型列表 */
  providers?: { list: () => unknown; refresh: (name: string) => Promise<{ ok: boolean }> }
  onInbound: (chatId: string) => void
}

const MAX_BODY = 1024 * 1024

export class ApiServer {
  private server: Server<undefined> | null = null
  constructor(private readonly d: ApiDeps) {}

  get port(): number { return this.server?.port ?? 0 }

  start(): void {
    this.server = Bun.serve({ hostname: '127.0.0.1', port: this.d.port, fetch: req => this.handle(req) })
    this.d.log.info('api.listening', { port: this.server.port })
  }

  async stop(): Promise<void> { await this.server?.stop(true) }

  async handle(req: Request): Promise<Response> {
    const url = new URL(req.url)
    if (req.headers.get('origin') !== null) return json({ error: 'cross-origin requests are not accepted' }, 403)
    const auth = req.headers.get('authorization') ?? ''
    if (!safeEqual(auth, `Bearer ${this.d.token}`)) return json({ error: 'unauthorized' }, 401)
    if (req.method === 'GET' && url.pathname === '/v1/health') {
      const h = this.d.health()
      return json(h, h.ok ? 200 : 503)
    }
    if (req.method === 'GET' && url.pathname === '/v1/model' && this.d.model) {
      try { return json(await this.d.model.info()) } catch (e) {
        this.d.log.error('api.failed', { path: url.pathname, err: safeError(e) })
        return json({ error: 'internal error' }, 500)
      }
    }
    if (req.method === 'GET' && url.pathname === '/v1/providers' && this.d.providers) return json({ providers: this.d.providers.list() })
    if (req.method !== 'POST') return json({ error: 'not found' }, 404)
    const ct = (req.headers.get('content-type') ?? '').split(';')[0]!.trim().toLowerCase()
    if (ct !== 'application/json') return json({ error: 'content-type must be application/json' }, 415)
    const len = Number(req.headers.get('content-length') ?? 0)
    if (len > MAX_BODY) return json({ error: 'body too large' }, 413)
    let body: Record<string, unknown>
    try {
      const raw = await req.text()
      if (raw.length > MAX_BODY) return json({ error: 'body too large' }, 413)
      body = JSON.parse(raw) as Record<string, unknown>
      if (!body || typeof body !== 'object' || Array.isArray(body)) throw new Error('not an object')
    } catch {
      return json({ error: 'invalid json' }, 400)
    }
    try {
      switch (url.pathname) {
        case '/v1/inject': return this.inject(body)
        case '/v1/send': return await this.send(body)
        case '/v1/model': {
          if (!this.d.model) return json({ error: 'not found' }, 404)
          const spec = typeof body.spec === 'string' ? body.spec.slice(0, 200) : ''
          if (!spec.trim()) return json({ error: 'spec is required' }, 400)
          const r = await this.d.model.set(spec)
          return json(r, r.ok ? 200 : 400)
        }
        case '/v1/providers/refresh': {
          if (!this.d.providers) return json({ error: 'not found' }, 404)
          const name = typeof body.name === 'string' ? body.name.slice(0, 64).trim() : ''
          if (!name) return json({ error: 'name is required' }, 400)
          const r = await this.d.providers.refresh(name)
          return json(r, r.ok ? 200 : 400)
        }
        default: return json({ error: 'not found' }, 404)
      }
    } catch (e) {
      this.d.log.error('api.failed', { path: url.pathname, err: safeError(e) })
      return json({ error: 'internal error' }, 500)
    }
  }

  private chatAllowed(chatId: string): boolean {
    const a = loadAccess(this.d.channelDir)
    return a.allowFrom.includes(chatId) || Object.prototype.hasOwnProperty.call(a.groups, chatId)
  }

  /** 写一条合成消息进账本（主动消息、承诺到期等），由模型在下一轮处理。key 用来防重复投递。 */
  private inject(b: Record<string, unknown>): Response {
    const chatId = String(b.chat_id ?? '')
    const text = typeof b.text === 'string' ? b.text : ''
    const source = typeof b.source === 'string' && /^[a-z0-9_.-]{1,40}$/i.test(b.source) ? b.source : 'api'
    if (!chatId || !text.trim()) return json({ error: 'chat_id and text are required' }, 400)
    if (!this.chatAllowed(chatId)) return json({ error: 'chat not allowed' }, 403)
    const key = typeof b.key === 'string' && b.key ? b.key.slice(0, 120) : randomToken(12)
    // bot 自己说过的话（电话里）：找许诺登记成承诺，登记结果附在这条消息后面告诉模型
    const lines = Array.isArray(b.bot_lines) ? b.bot_lines.filter((x): x is string => typeof x === 'string').slice(0, 200) : []
    const dup = this.d.ledger.inboundByKey(`ext:${source}:${key}`)
    const hints = lines.length && !dup && this.d.callPromises ? this.d.callPromises(chatId, lines) : []
    const r = this.d.ledger.insertInbound({ ukey: `ext:${source}:${key}`, chatId, kind: 'synthetic', text: hints.length ? `${text}\n${hints.join('\n')}` : text, ts: Date.now(), meta: { source } })
    if (r.inserted) this.d.onInbound(chatId)
    return json({ ok: true, id: r.id, duplicate: !r.inserted })
  }

  /** 直接发消息（不经过模型），比如朋友圈通知。逐段结果照实返回。 */
  private async send(b: Record<string, unknown>): Promise<Response> {
    const chatId = String(b.chat_id ?? '')
    const text = typeof b.text === 'string' ? b.text : ''
    const files = Array.isArray(b.files) ? b.files.filter((f): f is string => typeof f === 'string') : []
    if (!chatId || (!text.trim() && files.length === 0)) return json({ error: 'chat_id and text or files are required' }, 400)
    if (!this.chatAllowed(chatId)) return json({ error: 'chat not allowed' }, 403)
    for (const f of files) if (!allowedFile(f, this.d.allowDirs())) return json({ error: 'file not in an allowed directory' }, 403)
    const results = await this.d.sender.send({ chatId, turnId: null, callSeq: 0, text, files, kind: 'api' })
    const d = describeResults(results)
    return json({ ok: !d.isError, delivered: d.delivered, parts: results })
  }
}

function json(o: unknown, status = 200): Response {
  return new Response(JSON.stringify(o), { status, headers: { 'content-type': 'application/json' } })
}
