// 验收：网关本机接口 POST /v1/provider/preview（第 4 稿新增，全新只读端点）
// 与 POST /v1/provider/refresh 的 only 扩展（第 4 稿）。
// 契约要点（INTERFACE-新系统bot运维 §11a/§11b）：
// - preview 只读：不写 providers.json、不更新 lastRefresh、不占 refreshing 去重；
// - 行恒 6 键 {id, contextWindow, in_list, in_fetched, manual, default_checked}；
//   顺序 = 先 in_fetched（上游顺序）后 in_list（当前列表顺序）；count 只数 in_fetched；
// - default_checked = in_fetched or manual；
// - refresh 带 only：落盘 =（拉到的 ∩ only）∪（当前列表 ∩ only）；被勾上而拉取没有的
//   并入 manualModels（粘性）；only: [] 全清；未知 id 静默忽略。
// 实现未落地时本文件红（404），那是红基线，不是测试坏了。
import { describe, expect, test } from 'bun:test'
import { writeFileSync } from 'fs'
import { defaultKeyEnv, providersPath, readProviders, seedCreds, seedProviders, sleep, withModels, type BotEnv } from './_acc'
import { type FakeModels, type Handler, json, openaiOk, sleep as fsleep } from './fake-models'

const PREVIEW = '/v1/provider/preview'
const REFRESH = '/v1/provider/refresh'

/** 预置：旧列表 [a（主人给了 5000）, b, manual c]，凭据齐。 */
const seed = (o: { models?: any[]; manual?: string[]; key?: boolean; name?: string } = {}) => (b: BotEnv, fm: FakeModels) => {
  seedProviders(b.root, [{
    name: o.name ?? 'myproxy', baseURL: `${fm.url}/v1`,
    models: o.models ?? [{ id: 'a', contextWindow: 5000 }, 'b', 'c'],
    manualModels: o.manual ?? ['c'],
  }])
  if (o.key !== false) seedCreds(b.root, { [defaultKeyEnv(o.name ?? 'myproxy')]: fm.expectKey })
}
const FETCHED = [{ id: 'a', context_length: 8000 }, 'b', 'd']
const entry = (b: BotEnv) => readProviders(b.root).providers.myproxy
const ids = (b: BotEnv) => entry(b).route.models.map((m: any) => m.id)
const snapshot = (b: BotEnv) => JSON.stringify(readProviders(b.root))

describe('POST /v1/provider/preview', () => {
  test('合成：先拉到的（上游顺序）后仅本地的；行恒 6 键；count 只数拉到的', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: seed() }, async ({ gw }) => {
      const r = await gw.call(PREVIEW, { name: 'myproxy' })
      expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(r.json.ok).toBe(true)
      expect(r.json.count).toBe(3)
      expect(r.json.models.map((m: any) => m.id)).toEqual(['a', 'b', 'd', 'c'])
      for (const m of r.json.models) {
        expect(Object.keys(m).sort()).toEqual(['contextWindow', 'default_checked', 'id', 'in_fetched', 'in_list', 'manual'])
      }
      const by = Object.fromEntries(r.json.models.map((m: any) => [m.id, m]))
      expect(by.a).toMatchObject({ in_fetched: true, in_list: true, manual: false, default_checked: true, contextWindow: 8000 })
      expect(by.b).toMatchObject({ in_fetched: true, in_list: true, manual: false, default_checked: true })
      expect(by.d).toMatchObject({ in_fetched: true, in_list: false, manual: false, default_checked: true })
      expect(by.c).toMatchObject({ in_fetched: false, in_list: true, manual: true, default_checked: true })
      expect(by.c.contextWindow, '仅本地的行取列表里的值（恒为整数）').toBe(5000)
      expect(by.b.contextWindow, '上游没给上下文就是 null').toBeNull()
      expect(by.d.contextWindow).toBeNull()
    })
  })

  test('拉到 0 个模型也是 200：models 只含 in_list 的行', async () => {
    await withModels({ handler: openaiOk([]), before: seed() }, async ({ gw }) => {
      const r = await gw.call(PREVIEW, { name: 'myproxy' })
      expect(r.status, `实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(r.json.count).toBe(0)
      expect(r.json.models.map((m: any) => m.id)).toEqual(['a', 'b', 'c'])
      for (const m of r.json.models) expect(m.in_fetched).toBe(false)
      expect(r.json.models.map((m: any) => m.default_checked)).toEqual([false, false, true])
    })
  })

  test('名字按规整名匹配（MY_PROXY 找到 my-proxy）', async () => {
    await withModels({ handler: openaiOk(['z1']), before: seed({ name: 'my-proxy' }) }, async ({ gw }) => {
      const r = await gw.call(PREVIEW, { name: 'MY_PROXY' })
      expect(r.status, `实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(r.json.models.map((m: any) => m.id)).toContain('z1')
    })
  })

  test('只读：先留一份 lastRefresh，preview 后 providers.json 逐字节不变', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: seed() }, async ({ gw, b, fm }) => {
      const r0 = await gw.call(REFRESH, { name: 'myproxy' })
      expect(r0.status).toBe(200)
      const before = snapshot(b)
      fm.setHandler(openaiOk(['zzz']))
      const r = await gw.call(PREVIEW, { name: 'myproxy' })
      expect(r.status).toBe(200)
      expect(snapshot(b), 'preview 不该写 providers.json / 不该动 lastRefresh').toBe(before)
    })
  })

  test('不占 refreshing：两个并发 preview 都真的去拉了', async () => {
    const slow: Handler = async (r) => { await fsleep(600); return openaiOk(FETCHED)(r, 1, null as any) }
    await withModels({ handler: slow, before: seed() }, async ({ gw, fm }) => {
      const [r1, r2] = await Promise.all([gw.call(PREVIEW, { name: 'myproxy' }), gw.call(PREVIEW, { name: 'myproxy' })])
      expect([r1.status, r2.status]).toEqual([200, 200])
      expect(fm.requests.length, '两个 preview 都该发拉取请求').toBe(2)
    })
  })

  test('不占 refreshing：preview 拉取期间 refresh 同一家照常成功', async () => {
    const slow: Handler = async (r) => { await fsleep(700); return openaiOk(FETCHED)(r, 1, null as any) }
    await withModels({ handler: slow, before: seed() }, async ({ gw, fm }) => {
      const pv = gw.call(PREVIEW, { name: 'myproxy' })
      await sleep(120)
      const rf = await gw.call(REFRESH, { name: 'myproxy' })
      expect(rf.status, 'preview 不该占掉 refreshing 去重').toBe(200)
      expect((await pv).status).toBe(200)
      expect(fm.requests.length).toBe(2)
    })
  })

  for (const [what, body] of [['缺 name', {}], ['name 非字符串', { name: 123 }], ['name 空串', { name: '' }], ['name 形状错', { name: 'bad.name' }]] as const) {
    test(`坏名字（${what}）：400 bad_name 与固定文案`, async () => {
      await withModels({ handler: openaiOk(FETCHED) }, async ({ gw }) => {
        const r = await gw.call(PREVIEW, body)
        expect(r.status, `实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(400)
        expect(r.json).toEqual({ ok: false, error: 'bad_name', text: '供应商名字不合法' })
      })
    })
  }

  test('不存在：404 not_found，text 与 refresh 同款', async () => {
    await withModels({ handler: openaiOk(FETCHED) }, async ({ gw }) => {
      const a = await gw.call(REFRESH, { name: 'nosuch' })
      const b = await gw.call(PREVIEW, { name: 'nosuch' })
      expect([a.status, b.status]).toEqual([404, 404])
      expect(b.json.error).toBe('not_found')
      expect(b.json.text).toBe(a.json.text)
    })
  })

  test('条目不合格：409 disabled，text 与 refresh 同款', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: b => seedProviders(b.root, [{ name: 'broken', api: 'grpc' as any, baseURL: 'https://x.example.com/v1', models: ['x'] }]) }, async ({ gw }) => {
      const a = await gw.call(REFRESH, { name: 'broken' })
      const b = await gw.call(PREVIEW, { name: 'broken' })
      expect([a.status, b.status]).toEqual([409, 409])
      expect(b.json.error).toBe('disabled')
      expect(b.json.text).toBe(a.json.text)
    })
  })

  test('缺密钥：409 key_missing，text 与 refresh 同款，不发请求', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: seed({ key: false }) }, async ({ gw, fm }) => {
      const a = await gw.call(REFRESH, { name: 'myproxy' })
      const b = await gw.call(PREVIEW, { name: 'myproxy' })
      expect([a.status, b.status]).toEqual([409, 409])
      expect(b.json.error).toBe('key_missing')
      expect(b.json.text).toBe(a.json.text)
      expect(fm.requests.length).toBe(0)
    })
  })

  test('providers.json 读不了：503 providers_unreadable，text 与 refresh 同款', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: b => writeFileSync(providersPath(b.root), '[[[') }, async ({ gw }) => {
      const a = await gw.call(REFRESH, { name: 'myproxy' })
      const b = await gw.call(PREVIEW, { name: 'myproxy' })
      expect([a.status, b.status]).toEqual([503, 503])
      expect(b.json.error).toBe('providers_unreadable')
      expect(b.json.text).toBe(a.json.text)
    })
  })

  test('拉取失败：502 fetch_failed 带 reason，text 与 refresh 同款，列表不变', async () => {
    await withModels({ handler: () => json({}, 500), before: seed() }, async ({ gw, b }) => {
      const before = snapshot(b)
      const a = await gw.call(REFRESH, { name: 'myproxy' })
      const r = await gw.call(PREVIEW, { name: 'myproxy' })
      expect(r.status, `应 502，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(502)
      expect(r.json.error).toBe('fetch_failed')
      expect(r.json.reason).toBe('server_error')
      expect(r.json.text).toBe(a.json.text)
      expect(snapshot(b)).toBe(before)
    })
  })
})

describe('POST /v1/provider/refresh 的 only 扩展', () => {
  test('示例一（照抄契约）：拉 [a,b,d]、勾 [a,d,c] → 落盘 [a,d,c]，c 留在 manual', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: seed() }, async ({ gw, b }) => {
      const r = await gw.call(REFRESH, { name: 'myproxy', only: ['a', 'd', 'c'] })
      expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(r.json).toMatchObject({ ok: true, count: 3, added: 1, removed: ['b'], kept_manual: 1, selected: 3 })
      expect(String(r.json.text)).toContain('（按勾选保留 3 个）')
      expect(ids(b)).toEqual(['a', 'd', 'c'])
      expect(entry(b).meta.manualModels).toEqual(['c'])
    })
  })

  test('示例二（照抄契约）：only [a] → 落盘 [a]，manual 清空（c 被弃选）', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: seed() }, async ({ gw, b }) => {
      const r = await gw.call(REFRESH, { name: 'myproxy', only: ['a'] })
      expect(r.status).toBe(200)
      expect(r.json).toMatchObject({ ok: true, count: 3, added: 0, kept_manual: 0, selected: 1 })
      expect([...r.json.removed].sort()).toEqual(['b', 'c'])
      expect(String(r.json.text)).toContain('（按勾选保留 1 个）')
      expect(ids(b)).toEqual(['a'])
      expect(entry(b).meta.manualModels).toEqual([])
    })
  })

  test('only 空数组：列表与 manual 都清空，count 不变仍是上游个数', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: seed() }, async ({ gw, b }) => {
      const r = await gw.call(REFRESH, { name: 'myproxy', only: [] })
      expect(r.status).toBe(200)
      expect(r.json).toMatchObject({ ok: true, count: 3, added: 0, kept_manual: 0, selected: 0 })
      expect([...r.json.removed].sort()).toEqual(['a', 'b', 'c'])
      expect(String(r.json.text)).toContain('（按勾选保留 0 个）')
      expect(ids(b)).toEqual([])
      expect(entry(b).meta.manualModels).toEqual([])
    })
  })

  test('only 里既不在拉取也不在列表的 id：静默忽略', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: seed() }, async ({ gw, b }) => {
      const r = await gw.call(REFRESH, { name: 'myproxy', only: ['a', 'zzz-never-seen'] })
      expect(r.status, `不该因未知 id 报错，实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(200)
      expect(ids(b)).toEqual(['a'])
      expect(entry(b).meta.manualModels).toEqual([])
    })
  })

  test('粘性：列表里被勾上而上游没有的，并入 manualModels', async () => {
    await withModels({ handler: openaiOk(['a']), before: seed({ models: ['a', 'b'], manual: [] }) }, async ({ gw, b }) => {
      const r = await gw.call(REFRESH, { name: 'myproxy', only: ['a', 'b'] })
      expect(r.status).toBe(200)
      expect([...ids(b)].sort()).toEqual(['a', 'b'])
      expect(entry(b).meta.manualModels, 'b 被勾上、拉取没有 → 粘成手动').toContain('b')
      expect(r.json.selected).toBe(2)
    })
  })

  test('缺省 only：行为与响应不退化，selected 照给、text 不带「按勾选保留」', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: seed() }, async ({ gw, b }) => {
      const r = await gw.call(REFRESH, { name: 'myproxy' })
      expect(r.status).toBe(200)
      expect(r.json).toMatchObject({ ok: true, count: 3, added: 1, kept_manual: 1 })
      expect(r.json.selected, '缺省 only 时 selected 也给，等于落盘后列表长度').toBe(ids(b).length)
      expect(String(r.json.text)).not.toContain('按勾选保留')
    })
  })

  test('确定（提交 only）时重新拉上游：落盘按本次结果，不复用 preview 的缓存', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: seed() }, async ({ gw, b, fm }) => {
      const pv = await gw.call(PREVIEW, { name: 'myproxy' })
      expect(pv.status, `实得 ${pv.status}，正文 ${pv.text.slice(0, 200)}`).toBe(200)
      fm.setHandler(openaiOk([{ id: 'a', context_length: 8000 }, 'b', 'n1']))
      const r = await gw.call(REFRESH, { name: 'myproxy', only: ['a', 'b', 'n1'] })
      expect(r.status).toBe(200)
      expect(ids(b), 'n1 只存在于第二次拉取，落盘有它才说明提交时重拉了').toEqual(['a', 'b', 'n1'])
    })
  })

  test('only 合法的 200 字符边界 id：能用', async () => {
    const longId = 'm' + 'x'.repeat(199)
    await withModels({ handler: openaiOk([longId]), before: seed({ models: ['a', longId], manual: [] }) }, async ({ gw, b }) => {
      const r = await gw.call(REFRESH, { name: 'myproxy', only: [longId] })
      expect(r.status, `200 字符的 id 该能用，实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(200)
      expect(ids(b)).toEqual([longId])
    })
  })

  for (const [what, only] of [
    ['不是数组（字符串）', 'm1'],
    ['不是数组（数字）', 7],
    ['不是数组（对象）', { a: 1 }],
    ['含非字符串项', ['a', 2]],
    ['项是空串', ['']],
    ['项含换行', ['a\nb']],
    ['项含制表符', ['a\tb']],
    ['项含 ⟦⟧', ['a⟦b⟧']],
    ['项超过 200 字符', ['x'.repeat(201)]],
    ['超过 1000 项', Array.from({ length: 1001 }, (_, i) => `m${i}`)],
  ] as const) {
    test(`only 形状错（${what}）：400 bad_only 与固定文案，文件不变`, async () => {
      await withModels({ handler: openaiOk(FETCHED), before: seed() }, async ({ gw, b }) => {
        const before = snapshot(b)
        const r = await gw.call(REFRESH, { name: 'myproxy', only })
        expect(r.status, `实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(400)
        expect(r.json).toEqual({ ok: false, error: 'bad_only', text: 'only 必须是模型名数组' })
        expect(snapshot(b)).toBe(before)
      })
    })
  }

  test('only 给出但拉取失败：502 fetch_failed，模型列表不发生改动', async () => {
    await withModels({ handler: () => json({}, 500), before: seed() }, async ({ gw, b }) => {
      const before = JSON.stringify(entry(b).route.models)
      const r = await gw.call(REFRESH, { name: 'myproxy', only: ['a'] })
      expect(r.status).toBe(502)
      expect(r.json.error).toBe('fetch_failed')
      expect(JSON.stringify(entry(b).route.models), '拉取失败时列表不该被 only 动过').toBe(before)
    })
  })
})
