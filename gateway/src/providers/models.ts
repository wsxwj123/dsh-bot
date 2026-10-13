// 拉模型列表（方案 3.7）：新建、修改、刷新自建供应商时网关发出的唯一一类外发请求。
// - 只发往主人填的地址；不跟随跳转（R1：bun 的 redirect:'manual' 拿到真实 3xx，跳转目标收不到请求）；
//   每个请求有超时，整次最多 3 倍；单个回包超过 4 MiB 不读完、直接算格式不认识。
// - 任何一页、任何一次请求失败 → 整次失败，调用方保持模型列表不变。
// - 结果只有原因枚举和状态码：绝不带密钥、地址、对方回包正文（401 回包里可能原样带着密钥）。
import { cleanModelId, isContextWindow, type ProviderApi } from './store'

export type FetchFailReason = 'network' | 'timeout' | 'unauthorized' | 'not_found' | 'redirect' | 'rate_limited' | 'server_error' | 'http_error' | 'bad_response' | 'empty'
/** contextWindow 为 null = 对方没给（按 DEFAULT_CONTEXT 算，记入 guessedContext，由调用方处理） */
export type FetchedModel = { id: string; contextWindow: number | null }
export type FetchModelsRequest = { api: ProviderApi; baseURL: string; key: string }
export type FetchModelsResult =
  /** baseURL：OpenAI 格式地址只有域名、在 /v1 下才拉到时补成 <地址>/v1（v1Added） */
  | { ok: true; models: FetchedModel[]; truncated: boolean; baseURL: string; v1Added: boolean }
  /** seconds：超时那一次用的时限（秒），只在 timeout 时有 */
  | { ok: false; reason: FetchFailReason; status: number | null; seconds?: number }
/** 装配时绑定好超时设置后注入 ProviderService 的形状 */
export type FetchModelsFn = (req: FetchModelsRequest) => Promise<FetchModelsResult>

/** 测试连接（INTERFACE-新系统bot运维 §12a）：发一次最小对话请求，只看状态与回包是不是 JSON，不解析内容 */
export type ProbeModelRequest = { api: ProviderApi; baseURL: string; model: string; key: string }
export type ProbeModelResult = { ok: true } | { ok: false; reason: FetchFailReason; status: number | null; seconds?: number }
export type ProbeModelFn = (req: ProbeModelRequest) => Promise<ProbeModelResult>

export const MAX_MODEL_IDS = 1000
export const MAX_MODEL_PAGES = 5
export const MAX_LIST_BYTES = 4 * 1024 * 1024
/** 只取了一部分时回复里加的话（超过 1000 个，或 5 页后对方仍说还有） */
export const TRUNCATED_NOTE = '（只取了前 1000 个）'
const CONTEXT_FIELDS = ['max_input_tokens', 'context_length', 'context_window', 'max_context_length', 'max_model_len', 'input_token_limit']

class Fail {
  constructor(readonly reason: FetchFailReason, readonly status: number | null = null, readonly seconds?: number) {}
}

const seconds = (ms: number) => Number((ms / 1000).toFixed(1))

export async function fetchModels(req: FetchModelsRequest, o: { timeoutMs: number; fetch?: typeof fetch }): Promise<FetchModelsResult> {
  const doFetch = o.fetch ?? fetch
  const overall = new AbortController()
  const overallTimer = setTimeout(() => overall.abort(), o.timeoutMs * 3)

  /** 一次 GET：状态码分类、限长读正文、解析出列表 */
  const getList = async (url: string, headers: Record<string, string>) => {
    const one = AbortSignal.timeout(o.timeoutMs)
    try {
      const res = await doFetch(url, { method: 'GET', headers, redirect: 'manual', signal: AbortSignal.any([one, overall.signal]) })
      const fail = statusFail(res)
      if (fail) { await res.body?.cancel().catch(() => {}); throw fail }
      return parseList(await readLimited(res))
    } catch (e) {
      if (e instanceof Fail) throw e
      if (one.aborted) throw new Fail('timeout', null, seconds(o.timeoutMs))
      if (overall.signal.aborted) throw new Fail('timeout', null, seconds(o.timeoutMs * 3))
      throw new Fail('network') // 连不上、证书错、连接被掐断：不带底层错误（里面有地址）
    }
  }

  try {
    const seen = new Set<string>()
    const models: FetchedModel[] = []
    let items = 0
    let truncated = false
    const add = (list: unknown[]) => {
      for (const it of list) {
        items++
        const id = it && typeof it === 'object' ? cleanModelId((it as Record<string, unknown>).id) : null
        if (!id || seen.has(id)) continue
        if (models.length >= MAX_MODEL_IDS) { truncated = true; continue }
        seen.add(id)
        models.push({ id, contextWindow: contextOf(it as Record<string, unknown>) })
      }
    }
    let baseURL = req.baseURL
    let v1Added = false
    if (req.api === 'anthropic-messages') {
      const headers = { 'x-api-key': req.key, 'anthropic-version': '2023-06-01', Accept: 'application/json' }
      let url = `${baseURL}/v1/models?limit=1000`
      for (let page = 1; ; page++) {
        const list = await getList(url, headers)
        add(list.items)
        if (!list.hasMore) break
        const after = list.lastId ?? lastItemId(list.items)
        if (page >= MAX_MODEL_PAGES || !after) { truncated = true; break }
        url = `${baseURL}/v1/models?limit=1000&after_id=${encodeURIComponent(after)}`
      }
    } else {
      const headers = { Authorization: `Bearer ${req.key}`, Accept: 'application/json' }
      let list
      try {
        list = await getList(`${baseURL}/models`, headers)
      } catch (e) {
        // 地址只有域名时常见的是少写了 /v1：404、401、403，或者回了 2xx 却不是模型清单（有的中转站在域名下放的是网页），
        // 再在 /v1 下试一次，成功就把地址补上。bad_response 在 getList 里只会来自 2xx 的回包（不是 JSON、没有清单数组、超过 4 MiB）
        if (!(e instanceof Fail) || !['unauthorized', 'not_found', 'bad_response'].includes(e.reason) || !isBareHost(baseURL)) throw e
        try {
          list = await getList(`${baseURL}/v1/models`, headers)
        } catch (e2) {
          // 两次都失败默认报第二次的。唯独「第一次是网页、第二次 404」报第一次的：404 的文案叫主人检查 /v1，而网关刚替他试过 /v1
          if (e.reason === 'bad_response' && e2 instanceof Fail && e2.reason === 'not_found') throw e
          throw e2
        }
        baseURL = `${baseURL}/v1`
        v1Added = true
      }
      add(list.items)
    }
    if (items === 0) return { ok: false, reason: 'empty', status: null }
    if (models.length === 0) return { ok: false, reason: 'bad_response', status: null }
    return { ok: true, models, truncated, baseURL, v1Added }
  } catch (e) {
    if (e instanceof Fail) return { ok: false, reason: e.reason, status: e.status, ...(e.seconds !== undefined ? { seconds: e.seconds } : {}) }
    return { ok: false, reason: 'network', status: null }
  } finally {
    clearTimeout(overallTimer)
  }
}

/**
 * POST /v1/provider/test 用：按接口格式发一次最小对话请求（§12a）。
 * - 与 fetchModels 同一把尺：失败只有原因枚举与状态码，绝不带密钥、地址、对方回包正文；
 * - 判据 = HTTP 200 且回包是 JSON（不解析内容）；3xx 按 redirect（redirect:'manual'）；
 * - 超时是单次上限（无翻页，不乘 3）；
 * - openai 系地址 pathname 为 / 且首次 401/404 时在 /v1 下重试一次。比 fetchModels 的回落窄：2xx 但不是 JSON 时不再试
 *   （测试连接是只读的，补不了保存的地址，在 /v1 下测通会出现「测试说通、聊天不通」），由调用方加一句提示。
 */
export async function probeModel(req: ProbeModelRequest, o: { timeoutMs: number; fetch?: typeof fetch }): Promise<ProbeModelResult> {
  const doFetch = o.fetch ?? fetch
  const path = req.api === 'anthropic-messages' ? '/v1/messages' : req.api === 'openai-responses' ? '/responses' : '/chat/completions'
  const headers: Record<string, string> = req.api === 'anthropic-messages'
    ? { 'x-api-key': req.key, 'anthropic-version': '2023-06-01', Accept: 'application/json' }
    : { Authorization: `Bearer ${req.key}`, Accept: 'application/json' }
  const body = req.api === 'openai-responses'
    ? { model: req.model, max_output_tokens: 16, input: 'ping' }
    : { model: req.model, max_tokens: 1, messages: [{ role: 'user', content: 'ping' }] }

  const post = async (url: string): Promise<void> => {
    const one = AbortSignal.timeout(o.timeoutMs)
    try {
      const res = await doFetch(url, { method: 'POST', headers: { ...headers, 'content-type': 'application/json' }, body: JSON.stringify(body), redirect: 'manual', signal: one })
      const fail = probeStatusFail(res)
      if (fail) { await res.body?.cancel().catch(() => {}); throw fail }
      const text = await readLimited(res)
      try { JSON.parse(text) } catch { throw new Fail('bad_response', res.status) }
    } catch (e) {
      if (e instanceof Fail) throw e
      if (one.aborted) throw new Fail('timeout', null, seconds(o.timeoutMs))
      throw new Fail('network') // 连不上、证书错、连接被掐断：不带底层错误（里面有地址）
    }
  }

  const failOf = (e: Fail): ProbeModelResult => ({ ok: false, reason: e.reason, status: e.status, ...(e.seconds !== undefined ? { seconds: e.seconds } : {}) })
  try {
    await post(`${req.baseURL}${path}`)
    return { ok: true }
  } catch (e) {
    // 地址只有域名时常见的是少写了 /v1：401、404 再在 /v1 下试一次（§12a）
    if (req.api !== 'anthropic-messages' && e instanceof Fail && ['unauthorized', 'not_found'].includes(e.reason) && isBareHost(req.baseURL)) {
      try {
        await post(`${req.baseURL}/v1${path}`)
        return { ok: true }
      } catch (e2) {
        return e2 instanceof Fail ? failOf(e2) : { ok: false, reason: 'network', status: null }
      }
    }
    return e instanceof Fail ? failOf(e) : { ok: false, reason: 'network', status: null }
  }
}

/** 地址的路径部分是不是空的（只有协议、主机、可选端口）。到 /v1 下再试、提示「可能少写了 /v1」都只对这种地址 */
export function isBareHost(url: string): boolean {
  try { return new URL(url).pathname === '/' } catch { return false }
}

/**
 * 测试连接的状态分类（§12a）。与 fetchModels 的 statusFail 有一处刻意不同：403 不是
 * unauthorized 而是 http_error（拉名单时 403 = 没权限看目录；发对话时 403 = 对方的其它拒绝）。
 */
function probeStatusFail(res: Response): Fail | null {
  const s = res.status
  if (res.type === 'opaqueredirect' || (s >= 300 && s < 400)) return new Fail('redirect', s >= 300 ? s : null)
  if (s >= 200 && s < 300) return null
  if (s === 401) return new Fail('unauthorized', s)
  if (s === 404) return new Fail('not_found', s)
  if (s === 429) return new Fail('rate_limited', s)
  if (s >= 500) return new Fail('server_error', s)
  return new Fail('http_error', s)
}

/** 失败原因给主人看的话（方案 3.7 的表）；不含密钥、地址、对方回包正文 */
export function fetchFailText(r: { reason: FetchFailReason; status: number | null; seconds?: number }): string {
  switch (r.reason) {
    case 'network': return '连不上接口地址（网络不通、地址不对或 HTTPS 证书有问题）'
    case 'timeout': return `接口 ${r.seconds ?? '?'} 秒没有响应`
    case 'unauthorized': return `密钥不对或没有权限（HTTP ${r.status ?? 401}）`
    case 'not_found': return '接口地址不对（HTTP 404），检查是否多写或少写了 /v1'
    case 'redirect': return `接口地址发生了跳转（HTTP ${r.status ?? '3xx'}），请填跳转后的最终地址`
    case 'rate_limited': return '对方限流了（HTTP 429），稍后再刷新'
    case 'server_error': return `对方接口出错（HTTP ${r.status ?? '5xx'}）`
    case 'http_error': return `对方接口返回 HTTP ${r.status ?? '?'}`
    case 'bad_response': return '对方返回的不是模型列表（格式不认识）'
    case 'empty': return '对方返回的模型列表是空的'
  }
}

function statusFail(res: Response): Fail | null {
  const s = res.status
  if (res.type === 'opaqueredirect' || (s >= 300 && s < 400)) return new Fail('redirect', s >= 300 ? s : null)
  if (s >= 200 && s < 300) return null
  if (s === 401 || s === 403) return new Fail('unauthorized', s)
  if (s === 404) return new Fail('not_found', s)
  if (s === 429) return new Fail('rate_limited', s)
  if (s >= 500) return new Fail('server_error', s)
  return new Fail('http_error', s)
}

/** 读正文，超过 4 MiB 就停（不把大回包整个读进内存） */
async function readLimited(res: Response): Promise<string> {
  const declared = Number(res.headers.get('content-length'))
  if (Number.isFinite(declared) && declared > MAX_LIST_BYTES) { await res.body?.cancel().catch(() => {}); throw new Fail('bad_response') }
  if (!res.body) return ''
  const reader = res.body.getReader()
  const chunks: Uint8Array[] = []
  let total = 0
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    total += value.byteLength
    if (total > MAX_LIST_BYTES) { await reader.cancel().catch(() => {}); throw new Fail('bad_response') }
    chunks.push(value)
  }
  return Buffer.concat(chunks).toString('utf8')
}

/** 顶层 data 数组（或 models 数组）；Anthropic 的分页字段 has_more / last_id */
function parseList(text: string): { items: unknown[]; hasMore: boolean; lastId: string | null } {
  let j: unknown
  try { j = JSON.parse(text) } catch { throw new Fail('bad_response') }
  if (!j || typeof j !== 'object' || Array.isArray(j)) throw new Fail('bad_response')
  const o = j as Record<string, unknown>
  const items = Array.isArray(o.data) ? o.data : Array.isArray(o.models) ? o.models : null
  if (!items) throw new Fail('bad_response')
  return { items, hasMore: o.has_more === true, lastId: typeof o.last_id === 'string' && o.last_id ? o.last_id : null }
}

/** 上下文长度：按字段顺序取第一个 1024–100000000 的整数；都没有 = 未知 */
function contextOf(it: Record<string, unknown>): number | null {
  for (const f of CONTEXT_FIELDS) if (isContextWindow(it[f])) return it[f] as number
  return null
}

function lastItemId(items: unknown[]): string | null {
  const last = items[items.length - 1]
  const id = last && typeof last === 'object' ? (last as Record<string, unknown>).id : null
  return typeof id === 'string' && id ? id : null
}
