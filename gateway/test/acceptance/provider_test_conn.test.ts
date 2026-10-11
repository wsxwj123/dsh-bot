// 验收：网关本机接口 POST /v1/provider/test（第 5 稿新增，只读端点族）。
// 契约要点（INTERFACE-新系统bot运维 §12a）：
// - 只读：不写 providers.json、不动 lastRefresh、不调 onChanged、不占 refreshing、不写凭据；
//   响应与日志不含密钥、地址、上游响应正文；
// - 测的模型 = route.models[0].id；成功 text「连接正常：用 <model> 测通，<N> 毫秒」；
// - 请求构造按 route.api 三型；openai 系在 pathname 为 / 且首次 401/404 时回落 /v1 重试一次；
// - 失败 502 connect_failed，reason ∈ FetchFailReason；超时 15 秒（无翻页不乘 3）；
// - 日志 provider.tested / provider.test_failed {name, reason, status}；不参与互斥。
// 实现未落地时本文件红（404），那是红基线，不是测试坏了。
import { describe, expect, test } from 'bun:test'
import { readFileSync, writeFileSync } from 'fs'
import { defaultKeyEnv, evs, logPath, providersPath, readCreds, readProviders, seedCreds, seedProviders, until, withModels, type BotEnv } from './_acc'
import { type FakeModels, type Handler, firstThen, json, sleep as fsleep } from './fake-models'

const TEST = '/v1/provider/test'

/** 预置：自建 myproxy，models 默认 [m1, m2]，凭据齐。base 可覆盖（做回落用例）。 */
const seed = (o: { api?: any; models?: any[]; key?: boolean; base?: (fm: FakeModels) => string; name?: string } = {}) => (b: BotEnv, fm: FakeModels) => {
  const name = o.name ?? 'myproxy'
  seedProviders(b.root, [{
    name, api: o.api, baseURL: o.base ? o.base(fm) : `${fm.url}/v1`,
    models: o.models ?? ['m1', 'm2'],
  }])
  if (o.key !== false) seedCreds(b.root, { [defaultKeyEnv(name)]: fm.expectKey })
}
const snapshot = (b: BotEnv) => JSON.stringify(readProviders(b.root))

type Seen = { method: string; path: string; authKind: string; authOk: boolean; version: string | null; body: any }
/** 包一层 handler，把每个上游请求的 body 与头也记下来（FakeModels 只记头不记 body）。 */
function capture(inner: Handler) {
  const seen: Seen[] = []
  const h: Handler = async (r, n, raw) => {
    let body: any = null
    try { body = JSON.parse(await raw.text()) } catch {}
    seen.push({ method: r.method, path: r.path, authKind: r.authKind, authOk: r.authOk, version: r.anthropicVersion, body })
    return inner(r, n, raw)
  }
  return { h, seen }
}

function deadBase(): string {
  const s = Bun.serve({ port: 0, fetch: () => new Response('x') })
  const port = s.port
  s.stop(true)
  return `http://127.0.0.1:${port}/v1`
}

describe('POST /v1/provider/test 的成功路径与请求构造', () => {
  test('anthropic-messages：POST /v1/messages，x-api-key 头 + anthropic-version，body 三键', async () => {
    const cap = capture(() => json({ content: [{ type: 'text', text: 'pong' }] }))
    await withModels({ handler: cap.h, before: seed({ api: 'anthropic-messages' }) }, async ({ gw }) => {
      const r = await gw.call(TEST, { name: 'myproxy' })
      expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(r.json).toMatchObject({ ok: true, model: 'm1', status: 200 })
      expect(Number.isInteger(r.json.latency_ms), 'latency_ms 该是整数').toBe(true)
      expect(String(r.json.text)).toMatch(/^连接正常：用 m1 测通，\d+ 毫秒$/)
      expect(cap.seen.length).toBe(1)
      expect(cap.seen[0]).toMatchObject({ method: 'POST', path: '/v1/messages', authKind: 'x-api-key', authOk: true, version: '2023-06-01' })
      expect(cap.seen[0].body).toEqual({ model: 'm1', max_tokens: 1, messages: [{ role: 'user', content: 'ping' }] })
    })
  })

  test('openai-completions：POST <base>/chat/completions，Bearer 头，body 与 anthropic 同款', async () => {
    const cap = capture(() => json({ choices: [] }))
    await withModels({ handler: cap.h, before: seed({ api: 'openai-completions' }) }, async ({ gw }) => {
      const r = await gw.call(TEST, { name: 'myproxy' })
      expect(r.status).toBe(200)
      expect(cap.seen.length).toBe(1)
      expect(cap.seen[0]).toMatchObject({ method: 'POST', path: '/v1/chat/completions', authKind: 'bearer', authOk: true })
      expect(cap.seen[0].body).toEqual({ model: 'm1', max_tokens: 1, messages: [{ role: 'user', content: 'ping' }] })
    })
  })

  test('openai-responses：POST <base>/responses，body 走 max_output_tokens/input', async () => {
    const cap = capture(() => json({ output: [] }))
    await withModels({ handler: cap.h, before: seed({ api: 'openai-responses' }) }, async ({ gw }) => {
      const r = await gw.call(TEST, { name: 'myproxy' })
      expect(r.status).toBe(200)
      expect(cap.seen[0].path).toBe('/v1/responses')
      expect(cap.seen[0].body).toEqual({ model: 'm1', max_output_tokens: 16, input: 'ping' })
    })
  })

  test('openai 回落：地址 pathname 为 /，首次 401 时在 /v1 下重试一次并成功', async () => {
    const cap = capture(firstThen(1, () => json({ error: 'nope' }, 401), () => json({ choices: [] })))
    await withModels({ handler: cap.h, before: seed({ api: 'openai-completions', base: fm => fm.url }) }, async ({ gw }) => {
      const r = await gw.call(TEST, { name: 'myproxy' })
      expect(r.status, `回落重试该成功，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(cap.seen.map(s => s.path)).toEqual(['/chat/completions', '/v1/chat/completions'])
    })
  })

  test('名字按规整名匹配（MY_PROXY 找到 my-proxy）', async () => {
    await withModels({ handler: () => json({ choices: [] }), before: seed({ name: 'my-proxy' }) }, async ({ gw }) => {
      const r = await gw.call(TEST, { name: 'MY_PROXY' })
      expect(r.status, `实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(r.json.model).toBe('m1')
    })
  })

  test('型号是列表里的第一个（models[0]）', async () => {
    await withModels({ handler: () => json({ choices: [] }), before: seed({ models: ['zed', 'aaa'] }) }, async ({ gw }) => {
      const r = await gw.call(TEST, { name: 'myproxy' })
      expect(r.status).toBe(200)
      expect(r.json.model).toBe('zed')
      expect(String(r.json.text)).toContain('用 zed 测通')
    })
  })
})

describe('POST /v1/provider/test 的失败分型', () => {
  const CASES: [string, number, string, Handler][] = [
    ['401 密钥不认', 401, 'unauthorized', () => json({}, 401)],
    ['404 地址不对', 404, 'not_found', () => json({}, 404)],
    ['429 限流', 429, 'rate_limited', () => json({}, 429)],
    ['500 上游炸了', 500, 'server_error', () => json({}, 500)],
    ['403 其它 4xx', 403, 'http_error', () => json({}, 403)],
    ['302 跳转（manual 不跟随）', 302, 'redirect', () => new Response('', { status: 302, headers: { location: '/elsewhere' } })],
    ['200 但正文不是 JSON', 200, 'bad_response', () => new Response('hello', { status: 200, headers: { 'content-type': 'text/plain' } })],
  ]
  for (const [what, code, reason, handler] of CASES) {
    test(`${what}：502 connect_failed，reason=${reason}，text 带「用 m1 测的」`, async () => {
      await withModels({ handler, before: seed({ api: 'openai-completions' }) }, async ({ gw }) => {
        const r = await gw.call(TEST, { name: 'myproxy' })
        expect(r.status, `应 502，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(502)
        expect(r.json).toMatchObject({ ok: false, error: 'connect_failed', reason })
        const t = String(r.json.text)
        expect(t.endsWith('；用 m1 测的'), `text 实得：${t}`).toBe(true)
        expect(t.length).toBeGreaterThan('；用 m1 测的'.length)
      })
    })
  }

  test('连不上：reason=network', async () => {
    await withModels({ handler: () => json({}), before: seed({ api: 'openai-completions', base: () => deadBase() }) }, async ({ gw }) => {
      const r = await gw.call(TEST, { name: 'myproxy' })
      expect(r.status).toBe(502)
      expect(r.json).toMatchObject({ ok: false, error: 'connect_failed', reason: 'network' })
    })
  })

  test('太慢：reason=timeout，且无翻页不乘 3', async () => {
    // 超时 1000ms（配置下限）、上游 1600ms 才回：乘 3 成 3000ms 就收不到 timeout 了。
    const slow: Handler = async () => { await fsleep(1600); return json({ choices: [] }) }
    await withModels({ gw: { model_fetch_timeout_ms: 1000 }, handler: slow, before: seed({ api: 'openai-completions' }) }, async ({ gw }) => {
      const r = await gw.call(TEST, { name: 'myproxy' })
      expect(r.status, `上游 1600ms 才回，超时该在 1000ms 生效（乘 3 就不超时了）——实得 ${r.status}`).toBe(502)
      expect(r.json).toMatchObject({ ok: false, error: 'connect_failed', reason: 'timeout' })
    })
  })
})

describe('POST /v1/provider/test 的错误码', () => {
  for (const [what, body] of [['缺 name', {}], ['name 非字符串', { name: 123 }], ['name 空串', { name: '' }], ['name 形状错', { name: 'bad.name' }]] as const) {
    test(`坏名字（${what}）：400 bad_name 与固定文案`, async () => {
      await withModels({ handler: () => json({}) }, async ({ gw }) => {
        const r = await gw.call(TEST, body)
        expect(r.status).toBe(400)
        expect(r.json).toEqual({ ok: false, error: 'bad_name', text: '供应商名字不合法' })
      })
    })
  }

  test('模型列表为空：409 no_models 与固定文案，不发请求', async () => {
    await withModels({ handler: () => json({}), before: seed({ models: [] }) }, async ({ gw, fm }) => {
      const r = await gw.call(TEST, { name: 'myproxy' })
      expect(r.status, `实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(409)
      expect(r.json).toEqual({ ok: false, error: 'no_models', text: '这个供应商还没有模型，先刷新模型列表拿到模型再测' })
      expect(fm.requests.length).toBe(0)
    })
  })

  test('不存在：404 not_found，text 与 refresh 同款', async () => {
    await withModels({ handler: () => json({}) }, async ({ gw }) => {
      const a = await gw.call('/v1/provider/refresh', { name: 'nosuch' })
      const b = await gw.call(TEST, { name: 'nosuch' })
      expect([a.status, b.status]).toEqual([404, 404])
      expect(b.json.error).toBe('not_found')
      expect(b.json.text).toBe(a.json.text)
    })
  })

  test('内置（deepseek-official）：not_custom，text 与 refresh 同款', async () => {
    await withModels({ handler: () => json({}) }, async ({ gw }) => {
      const a = await gw.call('/v1/provider/refresh', { name: 'deepseek-official' })
      const b = await gw.call(TEST, { name: 'deepseek-official' })
      expect([a.status, b.status]).toEqual([400, 400])
      expect(b.json.error).toBe('not_custom')
      expect(b.json.text).toBe(a.json.text)
    })
  })

  test('条目不合格：disabled，text 与 refresh 同款', async () => {
    await withModels({ handler: () => json({}), before: b => seedProviders(b.root, [{ name: 'broken', api: 'grpc' as any, baseURL: 'https://x.example.com/v1', models: ['x'] }]) }, async ({ gw }) => {
      const a = await gw.call('/v1/provider/refresh', { name: 'broken' })
      const b = await gw.call(TEST, { name: 'broken' })
      expect([a.status, b.status]).toEqual([409, 409])
      expect(b.json.error).toBe('disabled')
      expect(b.json.text).toBe(a.json.text)
    })
  })

  test('缺密钥：409 key_missing，text 与 refresh 同款，不发请求', async () => {
    await withModels({ handler: () => json({}), before: seed({ key: false }) }, async ({ gw, fm }) => {
      const a = await gw.call('/v1/provider/refresh', { name: 'myproxy' })
      const b = await gw.call(TEST, { name: 'myproxy' })
      expect([a.status, b.status]).toEqual([409, 409])
      expect(b.json.error).toBe('key_missing')
      expect(b.json.text).toBe(a.json.text)
      expect(fm.requests.length).toBe(0)
    })
  })

  test('providers.json 读不了：503 providers_unreadable，text 与 refresh 同款', async () => {
    await withModels({ handler: () => json({}), before: b => writeFileSync(providersPath(b.root), '[[[') }, async ({ gw }) => {
      const a = await gw.call('/v1/provider/refresh', { name: 'myproxy' })
      const b = await gw.call(TEST, { name: 'myproxy' })
      expect([a.status, b.status]).toEqual([503, 503])
      expect(b.json.error).toBe('providers_unreadable')
      expect(b.json.text).toBe(a.json.text)
    })
  })
})

describe('POST /v1/provider/test 的只读与保密', () => {
  test('只读：providers.json 与 lastRefresh 不动、凭据文件不改', async () => {
    // refresh 要先走一次（留一份 lastRefresh 当对照），GET /models 与 POST /chat/completions 都得答。
    const both: Handler = r => r.method === 'GET' && r.path.endsWith('/models')
      ? json({ object: 'list', data: [{ id: 'm1', object: 'model' }] })
      : json({ choices: [] })
    await withModels({ handler: both, before: seed({ api: 'openai-completions' }) }, async ({ gw, b }) => {
      const r0 = await gw.call('/v1/provider/refresh', { name: 'myproxy' })
      expect(r0.status, `refresh 该成功（实得 ${r0.status}，正文 ${r0.text.slice(0, 200)}）`).toBe(200)
      const snap = snapshot(b)
      const creds = readCreds(b.root)
      const r = await gw.call(TEST, { name: 'myproxy' })
      expect(r.status).toBe(200)
      expect(snapshot(b), 'test 是只读端点，不许写 providers.json / 不许动 lastRefresh').toBe(snap)
      expect(readCreds(b.root), 'test 不该写凭据文件').toBe(creds)
    })
  })

  test('保密：key、地址、上游正文都不进响应与日志', async () => {
    const MARK = 'sup-body-marker-7f3a'
    await withModels({ handler: () => json({ error: { message: MARK } }, 500), before: seed({ api: 'openai-completions' }) }, async ({ gw, b, fm }) => {
      const r = await gw.call(TEST, { name: 'myproxy' })
      expect(r.status).toBe(502)
      expect(r.text).not.toContain(fm.expectKey)
      expect(r.text).not.toContain(fm.url)
      expect(r.text).not.toContain(MARK)
      await until(() => evs(b, 'provider.test_failed')[0], '日志 provider.test_failed')
      const log = readFileSync(logPath(b), 'utf8')
      expect(log).not.toContain(fm.expectKey)
      expect(log).not.toContain(MARK)
      expect(log).not.toContain(fm.url)
    })
  })

  test('成功日志 provider.tested 带名字；失败日志 provider.test_failed 带 name/reason/status', async () => {
    await withModels({ handler: () => json({ choices: [] }), before: seed({ api: 'openai-completions' }) }, async ({ gw, b }) => {
      const r = await gw.call(TEST, { name: 'myproxy' })
      expect(r.status).toBe(200)
      const e = await until(() => evs(b, 'provider.tested')[0], '日志 provider.tested')
      expect(e.name).toBe('myproxy')
    })
    await withModels({ handler: () => json({}, 401), before: seed({ api: 'openai-completions' }) }, async ({ gw, b }) => {
      const r = await gw.call(TEST, { name: 'myproxy' })
      expect(r.status).toBe(502)
      const e = await until(() => evs(b, 'provider.test_failed')[0], '日志 provider.test_failed')
      expect(e).toMatchObject({ name: 'myproxy', reason: 'unauthorized', status: 401 })
    })
  })

  test('不参与互斥：连点两次两个请求都发、都成功', async () => {
    const slow: Handler = async () => { await fsleep(500); return json({ choices: [] }) }
    await withModels({ handler: slow, before: seed({ api: 'openai-completions' }) }, async ({ gw, fm }) => {
      const [r1, r2] = await Promise.all([gw.call(TEST, { name: 'myproxy' }), gw.call(TEST, { name: 'myproxy' })])
      expect([r1.status, r2.status]).toEqual([200, 200])
      expect(fm.requests.length, '两个请求都该发出去（不互斥）').toBe(2)
    })
  })
})
