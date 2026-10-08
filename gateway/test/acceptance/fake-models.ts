// 假模型列表接口（INTERFACE 3.13.3）：OpenAI 形 GET …/models、Anthropic 形 GET /v1/models，可按剧本出错。
// 只记收到的方法、路径、查询串，鉴权头只记"是否等于期望值"和长度，不记原文。
export type ModelReq = {
  method: string
  path: string
  search: string
  authOk: boolean
  authLen: number
  authKind: 'bearer' | 'x-api-key' | 'none'
  anthropicVersion: string | null
  accept: string | null
  at: number
}
/** n = 这是第几个请求（从 1 起）；raw 只用来造"回包里原样带着密钥"之类的剧本，不记录 */
export type Handler = (r: ModelReq, n: number, raw: Request) => Response | Promise<Response>

export const sleep = (ms: number) => new Promise(r => setTimeout(r, ms))
export const json = (o: unknown, status = 200) => Response.json(o, { status })

export class FakeModels {
  readonly requests: ModelReq[] = []
  readonly server: ReturnType<typeof Bun.serve>
  constructor(readonly expectKey: string, private handler: Handler) {
    this.server = Bun.serve({ hostname: '127.0.0.1', port: 0, idleTimeout: 0, fetch: req => this.handle(req) })
  }

  get url(): string { return `http://127.0.0.1:${this.server.port}` }
  setHandler(h: Handler): void { this.handler = h }
  stop(): void { this.server.stop(true) }

  private async handle(req: Request): Promise<Response> {
    const u = new URL(req.url)
    const auth = req.headers.get('authorization')
    const xk = req.headers.get('x-api-key')
    const r: ModelReq = {
      method: req.method,
      path: u.pathname,
      search: u.search,
      authOk: auth !== null ? auth === `Bearer ${this.expectKey}` : xk !== null ? xk === this.expectKey : false,
      authLen: (auth ?? xk ?? '').length,
      authKind: auth !== null ? 'bearer' : xk !== null ? 'x-api-key' : 'none',
      anthropicVersion: req.headers.get('anthropic-version'),
      accept: req.headers.get('accept'),
      at: Date.now(),
    }
    this.requests.push(r)
    return this.handler(r, this.requests.length, req)
  }
}

/** OpenAI 形列表正文 */
export function openaiBody(ids: (string | Record<string, unknown>)[]): unknown {
  return { object: 'list', data: ids.map(x => typeof x === 'string' ? { id: x, object: 'model' } : { object: 'model', ...x }) }
}

/** 标准 OpenAI 剧本：GET 路径以 /models 结尾且鉴权对 → 列表；鉴权不对 → 401；其它路径 → 404 */
export function openaiOk(ids: (string | Record<string, unknown>)[]): Handler {
  return r => {
    if (r.method !== 'GET' || !r.path.endsWith('/models')) return json({ error: 'not found' }, 404)
    if (!r.authOk) return json({ error: { message: 'invalid api key' } }, 401)
    return json(openaiBody(ids))
  }
}

/** 标准 Anthropic 剧本：GET /v1/models，按 after_id 翻页；pages 每项是一页的 id（或带上下文字段的对象） */
export function anthropicOk(pages: (string | Record<string, unknown>)[][], o: { forceHasMore?: boolean } = {}): Handler {
  const idOf = (x: string | Record<string, unknown>) => typeof x === 'string' ? x : String(x.id)
  return r => {
    if (r.method !== 'GET' || r.path !== '/v1/models') return json({ type: 'error', error: { type: 'not_found_error' } }, 404)
    if (!r.authOk || r.anthropicVersion !== '2023-06-01') return json({ type: 'error', error: { type: 'authentication_error' } }, 401)
    const after = new URLSearchParams(r.search).get('after_id')
    let i = 0
    if (after !== null) i = pages.findIndex(p => p.length > 0 && idOf(p[p.length - 1]!) === after) + 1
    const page = pages[i] ?? []
    const hasMore = o.forceHasMore ? true : i < pages.length - 1
    return json({
      data: page.map(x => typeof x === 'string' ? { type: 'model', id: x, display_name: x } : { type: 'model', display_name: String(x.id), ...x }),
      has_more: hasMore,
      first_id: page.length ? idOf(page[0]!) : null,
      last_id: page.length ? idOf(page[page.length - 1]!) : null,
    })
  }
}

/** 先按 first 回应前 n 个请求，之后交给 then */
export function firstThen(n: number, first: Handler, then: Handler): Handler {
  return (r, k, raw) => (k <= n ? first(r, k, raw) : then(r, k, raw))
}
