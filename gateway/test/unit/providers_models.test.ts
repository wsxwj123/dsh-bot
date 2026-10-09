// 拉模型列表（方案 3.7）：请求形状、/v1 补全、分页、截断、失败分类与文案；结果里不带密钥、地址、回包正文
import { afterEach, expect, test } from 'bun:test'
import { fetchFailText, fetchModels, type FetchModelsRequest } from '../../src/providers/models'
import { sleep } from '../../src/util'

const KEY = 'test-key-models-Zx81Qw'
type Seen = { path: string; auth: string | null; xKey: string | null; version: string | null; accept: string | null }
const servers: { stop: (f?: boolean) => void }[] = []
afterEach(() => { for (const s of servers.splice(0)) s.stop(true) })

function serve(handler: (u: URL, n: number) => Response | Promise<Response>) {
  const seen: Seen[] = []
  const srv = Bun.serve({
    port: 0, hostname: '127.0.0.1',
    fetch: req => {
      const u = new URL(req.url)
      seen.push({ path: u.pathname + u.search, auth: req.headers.get('authorization'), xKey: req.headers.get('x-api-key'), version: req.headers.get('anthropic-version'), accept: req.headers.get('accept') })
      return handler(u, seen.length)
    },
  })
  servers.push(srv)
  return { url: `http://127.0.0.1:${srv.port}`, seen }
}
const json = (b: unknown, status = 200) => Response.json(b, { status })
const run = (r: Partial<FetchModelsRequest> & { baseURL: string }, timeoutMs = 5_000) => fetchModels({ api: 'openai-completions', key: KEY, ...r }, { timeoutMs })

test('OpenAI：GET <地址>/models 带 Bearer 与 Accept；id 去空白、去重保序、不合格跳过、非 ASCII 照收；上下文按字段顺序取第一个合格整数', async () => {
  const s = serve(() => json({ object: 'list', data: [
    { id: ' a ', max_input_tokens: 500, context_length: 32000 }, { id: 'a' }, { id: 'bad id' }, { id: '模型' }, { id: 'x'.repeat(201) },
    { id: 'b', input_token_limit: 2048 }, { id: 'c', context_window: 99999999999 }, { id: 'd', max_model_len: 4096.5 }, { id: '<b>' }, 'not-an-object',
  ] }))
  const r = await run({ baseURL: `${s.url}/v1` })
  expect(r).toEqual({ ok: true, truncated: false, baseURL: `${s.url}/v1`, v1Added: false, models: [
    { id: 'a', contextWindow: 32000 }, { id: '模型', contextWindow: null }, { id: 'b', contextWindow: 2048 }, { id: 'c', contextWindow: null }, { id: 'd', contextWindow: null }, { id: '<b>', contextWindow: null },
  ] })
  expect(s.seen).toEqual([{ path: '/v1/models', auth: `Bearer ${KEY}`, xKey: null, version: null, accept: 'application/json' }])
})

test('OpenAI：地址只有域名、/models 回 404 或 401 → 再试 /v1/models，成功则地址补成 /v1；有路径时不再试', async () => {
  for (const first of [404, 401]) {
    const s = serve((u, n) => (n === 1 ? json({}, first) : json({ models: [{ id: 'm1' }] })))
    expect(await run({ baseURL: s.url })).toMatchObject({ ok: true, baseURL: `${s.url}/v1`, v1Added: true, models: [{ id: 'm1' }] })
    expect(s.seen.map(x => x.path)).toEqual(['/models', '/v1/models'])
  }
  const s = serve(() => json({}, 404))
  expect(await run({ baseURL: `${s.url}/api` })).toEqual({ ok: false, reason: 'not_found', status: 404 })
  expect(s.seen.length).toBe(1)
})

test('Anthropic：GET /v1/models?limit=1000，has_more 时带 after_id 翻页；5 页后仍有就停并标截断', async () => {
  const pages = Array.from({ length: 7 }, (_, i) => [`p${i}-a`, `p${i}-b`])
  const s = serve(u => {
    const after = u.searchParams.get('after_id')
    const i = after ? pages.findIndex(p => p[p.length - 1] === after) + 1 : 0
    return json({ data: pages[i]!.map(id => ({ id, max_input_tokens: 200000 })), has_more: i < pages.length - 1, last_id: pages[i]!.at(-1) })
  })
  const r = await fetchModels({ api: 'anthropic-messages', baseURL: s.url, key: KEY }, { timeoutMs: 5_000 })
  expect(r.ok && r.models.length).toBe(10)
  expect(r.ok && r.truncated).toBe(true)
  expect(s.seen.map(x => x.path)).toEqual(['/v1/models?limit=1000', ...[0, 1, 2, 3].map(i => `/v1/models?limit=1000&after_id=p${i}-b`)])
  expect(s.seen[0]).toMatchObject({ xKey: KEY, version: '2023-06-01', accept: 'application/json', auth: null })
})

test('超过 1000 个 id：只取前 1000 个并标截断', async () => {
  const s = serve(() => json({ data: Array.from({ length: 1005 }, (_, i) => ({ id: `m-${i}` })) }))
  const r = await run({ baseURL: `${s.url}/v1` })
  expect(r.ok && [r.models.length, r.models[999]!.id, r.truncated]).toEqual([1000, 'm-999', true])
})

test('失败分类与文案（方案 3.7 的表）；回包正文、密钥、地址都不出现在结果里', async () => {
  const CASES: [number, string, string][] = [
    [401, 'unauthorized', '密钥不对或没有权限（HTTP 401）'], [403, 'unauthorized', '密钥不对或没有权限（HTTP 403）'],
    [404, 'not_found', '接口地址不对（HTTP 404），检查是否多写或少写了 /v1'], [429, 'rate_limited', '对方限流了（HTTP 429），稍后再刷新'],
    [500, 'server_error', '对方接口出错（HTTP 500）'], [503, 'server_error', '对方接口出错（HTTP 503）'], [418, 'http_error', '对方接口返回 HTTP 418'],
  ]
  for (const [status, reason, text] of CASES) {
    const s = serve(() => json({ error: { message: `upstream-body ${KEY}` } }, status))
    const r = await run({ baseURL: `${s.url}/v1` })
    expect(r).toEqual({ ok: false, reason: reason as never, status })
    expect(fetchFailText(r as never)).toBe(text)
    expect(JSON.stringify(r) + fetchFailText(r as never)).not.toContain(KEY)
  }
})

test('302：按 redirect 失败，不跟随（跳转目标收到 0 个请求）', async () => {
  const target = serve(() => json({ data: [{ id: 'x' }] }))
  const s = serve(() => new Response(null, { status: 302, headers: { location: `${target.url}/v1/models` } }))
  const r = await run({ baseURL: `${s.url}/v1` })
  expect(r).toEqual({ ok: false, reason: 'redirect', status: 302 })
  expect(fetchFailText(r as never)).toBe('接口地址发生了跳转（HTTP 302），请填跳转后的最终地址')
  expect(target.seen.length).toBe(0)
})

test('格式不认识：不是 JSON、没有 data/models 数组、全部 id 不合格、回包超过 4 MiB；成功但 0 个：empty', async () => {
  const big = JSON.stringify({ data: [{ id: 'm', pad: 'x'.repeat(4 * 1024 * 1024) }] })
  for (const body of ['<html>', '{"object":"list"}', JSON.stringify({ data: [{ id: 'bad id' }, { name: 'n' }] }), big]) {
    const s = serve(() => new Response(body, { headers: { 'content-type': 'application/json' } }))
    expect(await run({ baseURL: `${s.url}/v1` })).toEqual({ ok: false, reason: 'bad_response', status: null })
  }
  const s = serve(() => json({ data: [] }))
  const r = await run({ baseURL: `${s.url}/v1` })
  expect(r).toEqual({ ok: false, reason: 'empty', status: null })
  expect(fetchFailText(r as never)).toBe('对方返回的模型列表是空的')
})

test('超时：慢于设定 → timeout「接口 1 秒没有响应」；连不上 → network', async () => {
  const s = serve(async () => { await sleep(2_000); return json({ data: [{ id: 'm' }] }) })
  const r = await run({ baseURL: `${s.url}/v1` }, 1_000)
  expect(r).toEqual({ ok: false, reason: 'timeout', status: null, seconds: 1 })
  expect(fetchFailText(r as never)).toBe('接口 1 秒没有响应')
  const refused = (async () => { throw new TypeError(`Unable to connect https://secret.example/v1/models ${KEY}`) }) as unknown as typeof fetch
  const n = await fetchModels({ api: 'openai-completions', baseURL: 'https://secret.example/v1', key: KEY }, { timeoutMs: 1_000, fetch: refused })
  expect(n).toEqual({ ok: false, reason: 'network', status: null })
  expect(fetchFailText(n as never)).toBe('连不上接口地址（网络不通、地址不对或 HTTPS 证书有问题）')
})

test('Anthropic 多页、第 2 页失败：整次失败', async () => {
  const s = serve((u, n) => (n === 1 ? json({ data: [{ id: 'a1' }], has_more: true, last_id: 'a1' }) : json({}, 500)))
  expect(await fetchModels({ api: 'anthropic-messages', baseURL: s.url, key: KEY }, { timeoutMs: 5_000 })).toEqual({ ok: false, reason: 'server_error', status: 500 })
})
