// 验收：POST /v1/provider/refresh 带 only 时，上游回的是空的模型目录（接口约定第 8 稿）。
// 依据 INTERFACE-新系统bot运维 §11b「上游目录是空的」一段。
//
// 起因：有的供应商不提供模型目录，只能手动加模型。预览（§11a）把「上游回了一个空目录」当成功，
// 只列本地那几行；带 only 的刷新却把同一件事当失败回 502，于是这种供应商在勾选弹窗里点确定
// 永远失败。两处判据统一成一句：带 only 时，空目录不算失败。
//
// - only 给出、上游回空目录：按「拉到 0 个」落盘。落盘集合 = 当前列表 ∩ only，没勾的移走。200，count 为 0，
//   text 末尾照样追加「（按勾选保留 S 个）」，这次刷新记成成功。
//   手填名单按 INTERFACE-dsh修补 3.2：只去掉"在清单里、这次没被勾上"的名字；勾上而对方没返回的不再因此变成手填
//   （替换了第 8 稿「空目录时勾上的全部记进手填名单」）。
// - only 缺省、上游回空目录：与以前一字不变，仍是 502 fetch_failed，reason 为 empty，列表不动。
// - 上游别的失败：带不带 only 都仍是失败。
import { describe, expect, test } from 'bun:test'
import { defaultKeyEnv, readProviders, seedCreds, seedProviders, withModels, type BotEnv } from './_acc'
import { type FakeModels, json, openaiOk } from './fake-models'

const PREVIEW = '/v1/provider/preview'
const REFRESH = '/v1/provider/refresh'

/** 预置一个自建供应商，凭据齐。缺省是约定里那个例子：旧列表 [手动 c, 手动 e]。 */
const seed = (o: { models?: any[]; manual?: string[] } = {}) => (b: BotEnv, fm: FakeModels) => {
  seedProviders(b.root, [{
    name: 'myproxy', baseURL: `${fm.url}/v1`,
    models: o.models ?? ['c', 'e'],
    manualModels: o.manual ?? ['c', 'e'],
  }])
  seedCreds(b.root, { [defaultKeyEnv('myproxy')]: fm.expectKey })
}
const EMPTY = openaiOk([])
const entry = (b: BotEnv) => readProviders(b.root).providers.myproxy
const ids = (b: BotEnv) => entry(b).route.models.map((m: any) => m.id)
const manual = (b: BotEnv) => [...entry(b).meta.manualModels].sort()
const listing = (b: BotEnv) => JSON.stringify([entry(b).route.models, entry(b).meta.manualModels])

describe('refresh 带 only，上游回空目录：不算失败', () => {
  test('例子（照抄契约）：旧 [手动 c, 手动 e]、勾 [c] → 200', async () => {
    await withModels({ handler: EMPTY, before: seed() }, async ({ gw }) => {
      const r = await gw.call(REFRESH, { name: 'myproxy', only: ['c'] })
      expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(r.json.ok).toBe(true)
    })
  })

  test('例子（照抄契约）：响应里 count 0、added 0、removed 含 e、kept_manual 1、selected 1', async () => {
    await withModels({ handler: EMPTY, before: seed() }, async ({ gw }) => {
      const r = await gw.call(REFRESH, { name: 'myproxy', only: ['c'] })
      expect(r.json, `正文 ${r.text.slice(0, 300)}`).toMatchObject({ ok: true, count: 0, added: 0, kept_manual: 1, selected: 1 })
      expect(r.json.removed).toContain('e')
    })
  })

  test('例子（照抄契约）：落盘 [c]，c 仍在手动名单里', async () => {
    await withModels({ handler: EMPTY, before: seed() }, async ({ gw, b }) => {
      await gw.call(REFRESH, { name: 'myproxy', only: ['c'] })
      expect(ids(b)).toEqual(['c'])
      expect(manual(b)).toEqual(['c'])
    })
  })

  test('text 末尾照样追加「（按勾选保留 S 个）」', async () => {
    await withModels({ handler: EMPTY, before: seed() }, async ({ gw }) => {
      const r = await gw.call(REFRESH, { name: 'myproxy', only: ['c'] })
      expect(String(r.json.text), `正文 ${r.text.slice(0, 300)}`).toEndWith('（按勾选保留 1 个）')
    })
  })

  test('勾上而对方没返回的不会变成手填：原先不是手填的仍不是，没勾的移走', async () => {
    await withModels({ handler: EMPTY, before: seed({ models: ['a', 'b', 'c'], manual: ['c'] }) }, async ({ gw, b }) => {
      const r = await gw.call(REFRESH, { name: 'myproxy', only: ['a', 'c'] })
      expect(r.status, `实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(ids(b)).toEqual(['a', 'c'])
      expect(manual(b), 'a 被勾上而对方目录里没有：手填名单仍只有 c').toEqual(['c'])
      expect(r.json).toMatchObject({ count: 0, added: 0, removed: ['b'], kept_manual: 1, selected: 2 })
    })
  })

  test('only 空数组：列表与手动名单都清空，仍是 200', async () => {
    await withModels({ handler: EMPTY, before: seed() }, async ({ gw, b }) => {
      const r = await gw.call(REFRESH, { name: 'myproxy', only: [] })
      expect(r.status, `实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(r.json).toMatchObject({ ok: true, count: 0, added: 0, kept_manual: 0, selected: 0 })
      expect(ids(b)).toEqual([])
      expect(manual(b)).toEqual([])
    })
  })

  test('only 里不在列表的 id：静默忽略，不会凭空加进来', async () => {
    await withModels({ handler: EMPTY, before: seed() }, async ({ gw, b }) => {
      const r = await gw.call(REFRESH, { name: 'myproxy', only: ['c', 'zzz-never-seen'] })
      expect(r.status, `实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(ids(b)).toEqual(['c'])
      expect(r.json.added).toBe(0)
    })
  })

  test('这次刷新记成成功：lastRefresh.ok 为 true、reason 为 null', async () => {
    await withModels({ handler: EMPTY, before: seed() }, async ({ gw, b }) => {
      await gw.call(REFRESH, { name: 'myproxy', only: ['c'] })
      const lr = entry(b).meta.lastRefresh
      expect(lr?.ok, `lastRefresh 实得 ${JSON.stringify(lr)}`).toBe(true)
      expect(lr?.reason).toBeNull()
    })
  })

  test('与预览判据一致：同一个空目录，预览成功之后带勾选的确定也成功', async () => {
    await withModels({ handler: EMPTY, before: seed() }, async ({ gw }) => {
      const pv = await gw.call(PREVIEW, { name: 'myproxy' })
      expect(pv.status, `预览实得 ${pv.status}`).toBe(200)
      const checked = pv.json.models.filter((m: any) => m.default_checked).map((m: any) => m.id)
      const r = await gw.call(REFRESH, { name: 'myproxy', only: checked })
      expect(r.status, `预览能开弹窗，点确定却失败：实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
    })
  })
})

describe('对照：only 缺省时空目录仍是失败（没被改掉）', () => {
  test('only 缺省、上游回空目录：502 fetch_failed，reason 为 empty', async () => {
    await withModels({ handler: EMPTY, before: seed() }, async ({ gw }) => {
      const r = await gw.call(REFRESH, { name: 'myproxy' })
      expect(r.status, `实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(502)
      expect(r.json).toMatchObject({ ok: false, error: 'fetch_failed', reason: 'empty' })
    })
  })

  test('only 缺省、上游回空目录：列表与手动名单都不动', async () => {
    await withModels({ handler: EMPTY, before: seed() }, async ({ gw, b }) => {
      const before = listing(b)
      await gw.call(REFRESH, { name: 'myproxy' })
      expect(listing(b)).toBe(before)
    })
  })

  test('only 缺省、上游回空目录：这次刷新记成失败（lastRefresh.ok 为 false、reason 为 empty）', async () => {
    await withModels({ handler: EMPTY, before: seed() }, async ({ gw, b }) => {
      await gw.call(REFRESH, { name: 'myproxy' })
      const lr = entry(b).meta.lastRefresh
      expect(lr?.ok, `lastRefresh 实得 ${JSON.stringify(lr)}`).toBe(false)
      expect(lr?.reason).toBe('empty')
    })
  })
})

describe('对照：上游别的失败，带 only 也仍是失败', () => {
  for (const [what, status, reason] of [['鉴权失败（401）', 401, 'unauthorized'], ['对方接口出错（500）', 500, 'server_error']] as const) {
    test(`带 only、${what}：502 fetch_failed，reason 为 ${reason}，列表不动`, async () => {
      await withModels({ handler: () => json({ error: 'x' }, status), before: seed() }, async ({ gw, b }) => {
        const before = listing(b)
        const r = await gw.call(REFRESH, { name: 'myproxy', only: ['c'] })
        expect(r.status, `实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(502)
        expect(r.json).toMatchObject({ ok: false, error: 'fetch_failed', reason })
        expect(listing(b)).toBe(before)
      })
    })
  }
})
