// 验收：网关本机接口 GET /v1/provider（INTERFACE-管理台UI 3.1）。
// 契约要点只列自建供应商；校验通过的在前（名字升序），不合格的在后；
// 字段名是 modelList / manualModels / lastRefresh（不是 models）；key 只有 ok/missing/unknown 三态；
// 绝不含密钥；providers.json 读不了回 503 providers_unreadable。
// 实现未落地时本文件红（多半是 404 或字段对不上），那是红基线，不是测试坏了。
import { describe, expect, test } from 'bun:test'
import { writeFileSync } from 'fs'
import { defaultKeyEnv, fakeKey, providersPath, seedCreds, seedProviders, withBot, withModels, type BotEnv } from './_acc'
import { type FakeModels, openaiOk } from './fake-models'

const IDS = ['m1', 'm2']

/** 取某个供应商；取不到时失败消息里带上全部实得项，方便定位（返回 undefined 时点属性会含糊） */
function find(j: any, name: string): any {
  const p = (j?.providers ?? []).find((x: any) => x?.name === name)
  expect(p, `响应里应有「${name}」一项，实得 ${JSON.stringify(j?.providers)}`).toBeTruthy()
  return p
}

/** 一个合法自建：m1 没声明上下文（131072 且 guessed），m2 声明了 64000，man1 是手动加的 */
const seedOne = (key: string) => (b: BotEnv, fm: FakeModels) => {
  seedProviders(b.root, [{ name: 'myproxy', baseURL: `${fm.url}/v1`, models: ['m1', { id: 'm2', contextWindow: 64_000 }], manualModels: ['man1'] }])
  seedCreds(b.root, { [defaultKeyEnv('myproxy')]: key })
}

describe('GET /v1/provider 的正常路径', () => {
  test('一个合法自建：200，字段与契约 3.1 逐字一致', async () => {
    const key = fakeKey('plist')
    await withModels({ handler: openaiOk(IDS), before: seedOne(key) }, async ({ gw, fm }) => {
      const r = await gw.call('/v1/provider')
      expect(r.status, `GET /v1/provider 应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(r.json.ok, `ok 应为 true，实得 ${JSON.stringify(r.json.ok)}`).toBe(true)
      const p = find(r.json, 'myproxy')
      expect(p, '响应里应有 myproxy 一项，实得 ' + JSON.stringify(r.json.providers)).toBeTruthy()
      expect(p, '逐字段应与契约 3.1 样例一致').toEqual({
        name: 'myproxy',
        api: 'openai-completions',
        baseURL: `${fm.url}/v1`,
        modelList: [
          { id: 'm1', contextWindow: 131_072, guessed: true },
          { id: 'm2', contextWindow: 64_000, guessed: false },
        ],
        manualModels: ['man1'],
        key: 'ok',
        enabled: true,
        note: null,
        lastRefresh: null,
      })
    })
  })

  test('只列自建，配置文件里的路由不出现', async () => {
    const ROUTES = { myroute: { api: 'openai-completions', baseURL: 'https://r.example.com/v1', apiKeyEnv: 'MYROUTE_KEY', models: [{ id: 'r1', contextWindow: 32768 }] } }
    await withModels({ handler: openaiOk(IDS), brain: { routes: ROUTES }, before: seedOne(fakeKey()) }, async ({ gw }) => {
      const names = (await gw.call('/v1/provider')).json.providers.map((p: any) => p.name)
      expect(names, `只应列自建，实得 ${JSON.stringify(names)}`).toEqual(['myproxy'])
    })
  })

  test('多个合法自建：按名字升序（不分大小写）', async () => {
    await withModels({ handler: openaiOk(IDS), before: b => {
      seedProviders(b.root, [
        { name: 'zeta', baseURL: 'https://z.example.com/v1', models: ['z1'] },
        { name: 'abet', baseURL: 'https://a.example.com/v1', models: ['a1'] },
      ])
    } }, async ({ gw }) => {
      const names = (await gw.call('/v1/provider')).json.providers.map((p: any) => p.name)
      expect(names, `名字升序应为 abet,zeta，实得 ${JSON.stringify(names)}`).toEqual(['abet', 'zeta'])
    })
  })

  test('不合格的条目排最后，字段按契约 3.1 的破格式降级', async () => {
    await withModels({ handler: openaiOk(IDS), before: b => {
      seedProviders(b.root, [
        { name: 'abet', baseURL: 'https://a.example.com/v1', models: ['a1'] },
        { name: 'broken', api: 'grpc' as any, baseURL: 'https://x.example.com/v1', models: ['x1'] },
      ])
    } }, async ({ gw }) => {
      const r = await gw.call('/v1/provider')
      const names = r.json.providers.map((p: any) => p.name)
      expect(names, `合格在前不合格在后，实得 ${JSON.stringify(names)}`).toEqual(['abet', 'broken'])
      const broken = find(r.json, 'broken')
      expect(broken, '不合格条目字段应降级为 契约 3.1 样例二（note 只保证包含判定）').toMatchObject({
        name: 'broken',
        api: null,
        baseURL: null,
        modelList: [],
        manualModels: [],
        key: 'unknown',
        enabled: false,
        lastRefresh: null,
      })
      expect(String(broken.note), `note 应说明配置有误，实得 ${JSON.stringify(broken.note)}`).toContain('配置有误')
    })
  })

  test('被配置文件路由同名遮住的自建：enabled=false，note 提到同名', async () => {
    const ROUTES = { myroute: { api: 'openai-completions', baseURL: 'https://r.example.com/v1', apiKeyEnv: 'MYROUTE_KEY', models: [{ id: 'r1', contextWindow: 32768 }] } }
    await withModels({ handler: openaiOk(IDS), brain: { routes: ROUTES }, before: b => {
      seedProviders(b.root, [{ name: 'MyRoute', baseURL: 'https://m.example.com/v1', models: ['m1'] }])
      seedCreds(b.root, { [defaultKeyEnv('MyRoute')]: fakeKey() })
    } }, async ({ gw }) => {
      const p = find((await gw.call('/v1/provider')).json, 'myroute')
      expect(p, '被遮的自建仍应出现在列里').toBeTruthy()
      expect(p.enabled, `enabled 应为 false，实得 ${p.enabled}`).toBe(false)
      expect(String(p.note), `note 应提到同名，实得 ${JSON.stringify(p.note)}`).toContain('同名')
    })
  })

  test('key 三态：有凭据 ok、没凭据 missing', async () => {
    await withModels({ handler: openaiOk(IDS), before: b => {
      seedProviders(b.root, [
        { name: 'has', baseURL: 'https://h.example.com/v1', models: ['h1'] },
        { name: 'lacks', baseURL: 'https://l.example.com/v1', models: ['l1'] },
      ])
      seedCreds(b.root, { [defaultKeyEnv('has')]: fakeKey() })
    } }, async ({ gw }) => {
      const r = await gw.call('/v1/provider')
      expect(find(r.json, 'has').key, '有凭据的应 ok').toBe('ok')
      expect(find(r.json, 'lacks').key, '没凭据的应 missing').toBe('missing')
    })
  })

  test('没有自建供应商：200，providers 为空数组', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ gw }) => {
      const r = await gw.call('/v1/provider')
      expect(r.status, `空表也应 200，实得 ${r.status}`).toBe(200)
      expect(r.json.providers, `空表应是 []，实得 ${JSON.stringify(r.json.providers)}`).toEqual([])
    })
  })
})

describe('GET /v1/provider 的密钥与能力边界', () => {
  test('响应文本里绝不含密钥值', async () => {
    const key = fakeKey('plist')
    await withModels({ handler: openaiOk(IDS), before: seedOne(key) }, async ({ gw }) => {
      const r = await gw.call('/v1/provider')
      expect(r.text.includes(key), '密钥出现在了 /v1/provider 的返回里').toBe(false)
      for (const p of r.json.providers) {
        expect(['ok', 'missing', 'unknown'], `key 字段只能是状态词，实得 ${JSON.stringify(p.key)}`).toContain(p.key)
      }
    })
  })

  test('只回状态词：key 字段没有一个值时长的样子', async () => {
    const key = fakeKey('plist')
    await withModels({ handler: openaiOk(IDS), before: seedOne(key) }, async ({ gw }) => {
      const p = find((await gw.call('/v1/provider')).json, 'myproxy')
      expect(String(p.key).length, `key 是一个状态词，不该有 ${String(p.key).length} 个字符`).toBeLessThan(10)
    })
  })

  test('providers.json 读不了（格式坏了）：503 providers_unreadable，text 指向 providers.json', async () => {
    await withModels({ handler: openaiOk(IDS), before: b => { writeFileSync(providersPath(b.root), '[[[') } }, async ({ gw }) => {
      const r = await gw.call('/v1/provider')
      expect(r.status, `坏文件应 503，实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(503)
      expect(r.json, `错误形状应为 {ok:false,error:providers_unreadable,...}`).toMatchObject({ ok: false, error: 'providers_unreadable' })
      expect(String(r.json.text), `text 应指向 providers.json，实得 ${JSON.stringify(r.json.text)}`).toContain('providers.json')
    })
  })

  test('带 Origin 头：403，与既有接口相同', async () => {
    await withBot({ before: b => seedProviders(b.root, []) }, async ({ gw }) => {
      const r = await gw.api('/v1/provider', { headers: { authorization: `Bearer ${gw.apiToken()}`, origin: 'http://evil.example' } })
      expect(r.status, `带 Origin 应 403，实得 ${r.status}`).toBe(403)
    })
  })

  test('口令不对 / 没带口令：401', async () => {
    await withBot({ before: b => seedProviders(b.root, []) }, async ({ gw }) => {
      const noAuth = await gw.api('/v1/provider')
      expect(noAuth.status, `没带口令应 401，实得 ${noAuth.status}`).toBe(401)
      const bad = await gw.api('/v1/provider', { headers: { authorization: 'Bearer wrong-token-xxxx' } })
      expect(bad.status, `口令错应 401，实得 ${bad.status}`).toBe(401)
    })
  })
})
