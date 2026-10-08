// 验收：网关本机接口——GET /v1/model 的 providers、POST /v1/provider/refresh（INTERFACE 3.8）
import { describe, expect, test } from 'bun:test'
import { writeFileSync } from 'fs'
import { lockPath, providersPath, readProviders, seedCreds, seedProviders, sleep, withModels, type BotEnv } from './_acc'
import { type FakeModels, json, openaiOk, sleep as fsleep } from './fake-models'

const FETCHED = ['m1', 'm3', 'm4']
const seed = (o: { key?: boolean } = {}) => (b: BotEnv, fm: FakeModels) => {
  seedProviders(b.root, [{ name: 'myproxy', baseURL: `${fm.url}/v1`, models: ['m1', 'm2', 'man1'], manualModels: ['man1'] }])
  if (o.key !== false) seedCreds(b.root, { PROVIDER_MYPROXY_KEY: fm.expectKey })
}
const ROUTES = { myroute: { api: 'openai-completions', baseURL: 'https://r.example.com/v1', apiKeyEnv: 'MYROUTE_KEY', models: [{ id: 'r1', contextWindow: 65536 }] } }
const find = (j: any, name: string) => (j?.providers ?? []).find((p: any) => p.name === name)

describe('GET /v1/model', () => {
  test('原有的 current、config、choices 仍在', async () => {
    await withModels({ handler: openaiOk(FETCHED) }, async ({ gw }) => {
      const r = await gw.call('/v1/model')
      expect(r.status).toBe(200)
      expect(r.json.current).toEqual({ provider: 'deepseek-official', model: 'deepseek-flash' })
      expect(r.json.config).toEqual({ provider: 'deepseek-official', model: 'deepseek-flash' })
      expect(Array.isArray(r.json.choices)).toBe(true)
    })
  })

  test('新增 providers：内置项 source=builtin、models=3、enabled=true、last_refresh=null', async () => {
    await withModels({ handler: openaiOk(FETCHED) }, async ({ gw }) => {
      const p = find((await gw.call('/v1/model')).json, 'deepseek-official')
      expect(p.source).toBe('builtin')
      expect(p.models).toBe(3)
      expect(p.enabled).toBe(true)
      expect(p.last_refresh).toBeNull()
    })
  })

  test('自建项：source=custom、api、models 数、key=ok、enabled=true、note=null，字段齐全', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: seed() }, async ({ gw }) => {
      const p = find((await gw.call('/v1/model')).json, 'myproxy')
      expect(p).toEqual({ name: 'myproxy', source: 'custom', api: 'openai-completions', models: 3, key: 'ok', enabled: true, note: null, last_refresh: null })
    })
  })

  test('自建缺密钥：key=missing', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: seed({ key: false }) }, async ({ gw }) => {
      expect(find((await gw.call('/v1/model')).json, 'myproxy').key).toBe('missing')
    })
  })

  test('配置文件路由：source=config', async () => {
    await withModels({ brain: { routes: ROUTES }, handler: openaiOk(FETCHED) }, async ({ gw }) => {
      expect(find((await gw.call('/v1/model')).json, 'myroute').source).toBe('config')
    })
  })

  test('条目不合格的自建：enabled=false，note 写配置有误', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: b => seedProviders(b.root, [{ name: 'broken', api: 'grpc' as any, baseURL: 'https://x.example.com/v1', models: ['x'] }]) }, async ({ gw }) => {
      const p = find((await gw.call('/v1/model')).json, 'broken')
      expect(p.enabled).toBe(false)
      expect(String(p.note)).toContain('配置有误')
    })
  })

  test('被本 bot 配置文件路由同名遮住的自建：enabled=false，note 写同名', async () => {
    await withModels({ brain: { routes: ROUTES }, handler: openaiOk(FETCHED), before: b => seedProviders(b.root, [{ name: 'MyRoute', baseURL: 'https://x.example.com/v1', models: ['x'] }]) }, async ({ gw }) => {
      const p = (await gw.call('/v1/model')).json.providers.find((x: any) => x.source === 'custom')
      expect(p.enabled).toBe(false)
      expect(String(p.note)).toContain('同名')
    })
  })

  test('自建的每次请求都现读 providers.json：网关运行中新写入的供应商立刻出现', async () => {
    await withModels({ gw: { config_poll_ms: 60_000 }, handler: openaiOk(FETCHED) }, async ({ gw, b }) => {
      seedProviders(b.root, [{ name: 'fresh', baseURL: 'https://f.example.com/v1', models: ['f1', 'f2'] }])
      expect(find((await gw.call('/v1/model')).json, 'fresh')?.models).toBe(2)
    })
  })

  test('providers 的每一项都没有地址字段', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: seed() }, async ({ gw, fm }) => {
      const r = await gw.call('/v1/model')
      for (const p of r.json.providers) expect(Object.keys(p).some(k => /url|base/i.test(k))).toBe(false)
      expect(r.text).not.toContain(`127.0.0.1:${fm.server.port}`)
    })
  })
})

describe('POST /v1/provider/refresh', () => {
  test('成功：200 {ok:true, count:3, added:2, removed:1, kept_manual:1, text}，text 不带【系统】', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: seed() }, async ({ gw }) => {
      const r = await gw.call('/v1/provider/refresh', { name: 'myproxy' })
      expect(r.status).toBe(200)
      expect(r.json).toMatchObject({ ok: true, count: 3, added: 2, removed: 1, kept_manual: 1 })
      expect(r.json.text).toContain('「myproxy」拉到 3 个模型（新增 2 个，去掉 1 个；手动加的 1 个保留）。')
      expect(r.json.text.startsWith('【系统】')).toBe(false)
    })
  })

  for (const [what, body] of [['缺 name', {}], ['name 不是字符串', { name: 123 }], ['name 为空', { name: '' }], ['name 不合规则', { name: 'bad.name' }]] as const) {
    test(`${what}：400 {ok:false, error:'bad_name'}`, async () => {
      await withModels({ handler: openaiOk(FETCHED) }, async ({ gw }) => {
        const r = await gw.call('/v1/provider/refresh', body)
        expect(r.status).toBe(400)
        expect(r.json).toMatchObject({ ok: false, error: 'bad_name' })
        expect(typeof r.json.text).toBe('string')
      })
    })
  }

  test('不是自建（deepseek-official）：400 not_custom', async () => {
    await withModels({ handler: openaiOk(FETCHED) }, async ({ gw }) => {
      const r = await gw.call('/v1/provider/refresh', { name: 'deepseek-official' })
      expect(r.status).toBe(400)
      expect(r.json).toMatchObject({ ok: false, error: 'not_custom' })
    })
  })

  test('不存在：404 not_found', async () => {
    await withModels({ handler: openaiOk(FETCHED) }, async ({ gw }) => {
      const r = await gw.call('/v1/provider/refresh', { name: 'nosuch' })
      expect(r.status).toBe(404)
      expect(r.json).toMatchObject({ ok: false, error: 'not_found' })
    })
  })

  test('条目不合格：409 disabled', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: b => seedProviders(b.root, [{ name: 'broken', api: 'grpc' as any, baseURL: 'https://x.example.com/v1', models: ['x'] }]) }, async ({ gw }) => {
      const r = await gw.call('/v1/provider/refresh', { name: 'broken' })
      expect(r.status).toBe(409)
      expect(r.json).toMatchObject({ ok: false, error: 'disabled' })
    })
  })

  test('providers.json 读不了：503 providers_unreadable', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: b => writeFileSync(providersPath(b.root), '[[[') }, async ({ gw }) => {
      const r = await gw.call('/v1/provider/refresh', { name: 'myproxy' })
      expect(r.status).toBe(503)
      expect(r.json).toMatchObject({ ok: false, error: 'providers_unreadable' })
    })
  })

  test('缺密钥：409 key_missing，不发请求', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: seed({ key: false }) }, async ({ gw, fm }) => {
      const r = await gw.call('/v1/provider/refresh', { name: 'myproxy' })
      expect(r.status).toBe(409)
      expect(r.json).toMatchObject({ ok: false, error: 'key_missing' })
      expect(fm.requests.length).toBe(0)
    })
  })

  test('拉取失败：502 fetch_failed，reason=server_error，列表不变', async () => {
    await withModels({ handler: () => json({}, 500), before: seed() }, async ({ gw, b }) => {
      const r = await gw.call('/v1/provider/refresh', { name: 'myproxy' })
      expect(r.status).toBe(502)
      expect(r.json).toMatchObject({ ok: false, error: 'fetch_failed', reason: 'server_error' })
      expect(readProviders(b.root).providers.myproxy.route.models.length).toBe(3)
    })
  })

  test('拉取期间被别处删掉：409 changed', async () => {
    const slow = async (r: any) => { await fsleep(1_500); return openaiOk(FETCHED)(r, 1, null as any) }
    await withModels({ handler: slow, before: seed() }, async ({ gw, b }) => {
      const p = gw.call('/v1/provider/refresh', { name: 'myproxy' })
      await sleep(500)
      seedProviders(b.root, [])
      const r = await p
      expect(r.status).toBe(409)
      expect(r.json).toMatchObject({ ok: false, error: 'changed' })
    })
  })

  test('拿不到锁：409 busy', async () => {
    await withModels({ gw: { provider_lock_wait_ms: 300 }, handler: openaiOk(FETCHED), before: seed() }, async ({ gw, b }) => {
      writeFileSync(lockPath(b.root), JSON.stringify({ pid: process.pid, at: Date.now() }))
      const r = await gw.call('/v1/provider/refresh', { name: 'myproxy' })
      expect(r.status).toBe(409)
      expect(r.json).toMatchObject({ ok: false, error: 'busy' })
    })
  })

  const REQS: [string, number, (tok: string) => RequestInit][] = [
    ['没带口令', 401, () => ({ method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ name: 'myproxy' }) })],
    ['带 Origin 头', 403, t => ({ method: 'POST', headers: { authorization: `Bearer ${t}`, 'content-type': 'application/json', origin: 'http://evil.example' }, body: JSON.stringify({ name: 'myproxy' }) })],
    ['Content-Type 不是 JSON', 415, t => ({ method: 'POST', headers: { authorization: `Bearer ${t}`, 'content-type': 'text/plain' }, body: JSON.stringify({ name: 'myproxy' }) })],
    ['请求体是 JSON 数组', 400, t => ({ method: 'POST', headers: { authorization: `Bearer ${t}`, 'content-type': 'application/json' }, body: '[1]' })],
  ]
  for (const [what, status, init] of REQS) {
    test(`${what}：${status}（与现有接口相同），不发拉列表请求`, async () => {
      await withModels({ handler: openaiOk(FETCHED), before: seed() }, async ({ gw, fm }) => {
        expect((await gw.api('/v1/provider/refresh', init(gw.apiToken()))).status).toBe(status)
        expect(fm.requests.length).toBe(0)
      })
    })
  }
})
