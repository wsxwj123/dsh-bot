// 本机接口：给 Python 周边脚本、看门狗和 bot 之间互推用。
// 安全：只听 127.0.0.1；要口令（state/api.key，权限 600，常量时间比较）；带 Origin 头一律拒绝（防浏览器里的网页跨站调用）；
// 只收 application/json；/v1/send 发文件只允许白名单目录。
import type { Server } from 'bun'
import { loadAccess } from '../config'
import type { Ledger } from '../ledger'
import { safeError, type Logger } from '../log'
import { isProviderName } from '../providers/store'
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
  /**
   * 自建供应商（方案 3.8）：`views` 每次请求现读 providers.json；`refresh` 走 ProviderService 的刷新。
   * 由 main.ts 装配注入。views 的返回与 engine 的 ProviderView 结构兼容（多带的字段在这里丢掉）。
   */
  provider?: {
    views: () => Promise<ProviderViewLike[]>
    refresh: (name: string) => Promise<{ status: number; body: unknown }>
  }
  onInbound: (chatId: string) => void
}

/** engine.ProviderView 的结构子集：本接口只投这些字段（绝不含密钥、地址） */
type ProviderViewLike = {
  name: string
  source: string
  api: string | null
  models: number | null
  key: string
  enabled: boolean
  note: string | null
  lastRefresh: { at: number; ok: boolean; count: number; reason: string | null } | null
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
      try { return json(await this.modelResponse()) } catch (e) {
        this.d.log.error('api.failed', { path: url.pathname, err: safeError(e) })
        return json({ error: 'internal error' }, 500)
      }
    }
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
        case '/v1/provider/refresh': return await this.providerRefresh(body)
        default: return json({ error: 'not found' }, 404)
      }
    } catch (e) {
      this.d.log.error('api.failed', { path: url.pathname, err: safeError(e) })
      return json({ error: 'internal error' }, 500)
    }
  }

  /** GET /v1/model：原有 current/config/choices + providers（方案 3.8）；providers 每次请求现读 */
  private async modelResponse(): Promise<Record<string, unknown>> {
    const info = (await this.d.model!.info()) as Record<string, unknown>
    const providers = this.d.provider ? (await this.d.provider.views()).map(providerJson) : []
    return { ...info, providers }
  }

  /** POST /v1/provider/refresh：名字先按 3.1.1 的格式筛掉，再交给 ProviderCommands 的刷新 */
  private async providerRefresh(body: Record<string, unknown>): Promise<Response> {
    const name = body.name
    if (typeof name !== 'string' || !isProviderName(name)) {
      return json({ ok: false, error: 'bad_name', text: '供应商名字不合法' }, 400)
    }
    if (!this.d.provider) return json({ ok: false, error: 'internal error' }, 500)
    const r = await this.d.provider.refresh(name)
    return json(r.body, r.status)
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

/** ProviderView → 3.8 的字段形状：只投这些字段，丢掉内部的 keyEnv（绝不含密钥、地址） */
function providerJson(v: ProviderViewLike): Record<string, unknown> {
  return {
    name: v.name, source: v.source, api: v.api, models: v.models, key: v.key,
    enabled: v.enabled, note: v.note,
    last_refresh: v.lastRefresh ? { at: v.lastRefresh.at, ok: v.lastRefresh.ok, count: v.lastRefresh.count, reason: v.lastRefresh.reason } : null,
  }
}
