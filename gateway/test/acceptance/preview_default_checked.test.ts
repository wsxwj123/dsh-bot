// 验收：刷新预览的默认勾选、按勾选保存、清单指纹（INTERFACE-dsh修补 第三节 3.1–3.4、3.6）。
// 起因：预览把对方返回的模型全部默认打勾，用户每次都要把想要的重选一遍。改后默认打勾的是"已经在清单里的"，
// 并且必须成立一条性质：打开预览后什么都不动直接确定，清单与确定前完全一样。
// 黑盒：只调本机接口 POST /v1/provider/preview 与 POST /v1/provider/refresh，读 providers.json。
// 照抄约定里的例子（清单 [a, b, 手填 c]、拉到 [a, b, d] 等）放在 model_preview_only.test.ts，这里是其余各格。
import { describe, expect, setDefaultTimeout, test } from 'bun:test'
import { readFileSync, writeFileSync } from 'fs'
import { providersPath, readProviders, seedCreds, seedProviders, withModels, type BotEnv, type SeedModel } from './_acc'
import { type FakeModels, type Handler, openaiOk } from './fake-models'

setDefaultTimeout(60_000)

const PREVIEW = '/v1/provider/preview'
const REFRESH = '/v1/provider/refresh'

const seed = (models: SeedModel[], manual: string[] = []) => (b: BotEnv, fm: FakeModels) => {
  seedProviders(b.root, [{ name: 'myproxy', baseURL: `${fm.url}/v1`, models, manualModels: manual }])
  seedCreds(b.root, { PROVIDER_MYPROXY_KEY: fm.expectKey })
}
const entry = (b: BotEnv) => readProviders(b.root).providers.myproxy
const ids = (b: BotEnv) => entry(b).route.models.map((m: any) => m.id)
const fileBytes = (b: BotEnv) => readFileSync(providersPath(b.root), 'utf8')
/** 像"别处改了文件"那样直接改 providers.json 里这家供应商 */
const edit = (b: BotEnv, fn: (e: any) => void) => { const p = readProviders(b.root); fn(p.providers.myproxy); writeFileSync(providersPath(b.root), JSON.stringify(p, null, 1)) }
const preview = async (gw: any) => { const r = await gw.call(PREVIEW, { name: 'myproxy' }); expect(r.status, `预览应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200); return r.json }
const checkedOf = (pv: any): string[] => pv.models.filter((m: any) => m.default_checked).map((m: any) => m.id)

describe('预览的默认勾选（3.1）', () => {
  test('清单为空：这次拉到的全部默认打勾', async () => {
    await withModels({ handler: openaiOk(['x', 'y']), before: seed([]) }, async ({ gw }) => {
      const pv = await preview(gw)
      expect(pv.models.map((m: any) => [m.id, m.default_checked])).toEqual([['x', true], ['y', true]])
    })
  })

  test('手填名单里有、清单里却没有的名字（文件被手改过），这次拉到了：按"不在清单"算，默认不打勾', async () => {
    await withModels({ handler: openaiOk(['a', 'c']), before: seed(['a'], ['c']) }, async ({ gw }) => {
      const pv = await preview(gw)
      const c = pv.models.find((m: any) => m.id === 'c')
      expect(c).toMatchObject({ in_fetched: true, in_list: false, manual: true, default_checked: false })
    })
  })
})

describe('清单指纹 list_rev（3.3）', () => {
  test('预览的 200 体顶层只多一个键 list_rev（非空字符串，不超过 100 个字符）；清单的模型名与手填名单都没变时它不变：对方返回变了、上下文长度变了、刷新记录和时间戳变了都不影响', async () => {
    await withModels({ handler: openaiOk(['a', 'd']), before: seed(['a', 'b'], ['b']) }, async ({ gw, b, fm }) => {
      const pv0 = await preview(gw)
      const rev0 = pv0.list_rev
      expect(Object.keys(pv0).sort()).toEqual(['count', 'list_rev', 'models', 'ok'])
      expect(typeof rev0 === 'string' && rev0.length > 0 && rev0.length <= 100, `list_rev 实得 ${JSON.stringify(rev0)}`).toBe(true)
      fm.setHandler(openaiOk(['zzz']))
      edit(b, e => { e.route.models[0].contextWindow = 4096; e.meta.updatedAt = 1_800_000_000_000; e.meta.lastRefresh = { at: 1_800_000_000_000, ok: true, count: 9, reason: null } })
      const rev1 = (await preview(gw)).list_rev
      expect(rev1).toBe(rev0)
    })
  })

  test('清单的先后变了、或者手填名单变了：list_rev 都跟着变', async () => {
    await withModels({ handler: openaiOk(['a', 'd']), before: seed(['a', 'b'], ['b']) }, async ({ gw, b }) => {
      const rev0 = (await preview(gw)).list_rev
      edit(b, e => { e.route.models.reverse() })
      const reordered = (await preview(gw)).list_rev
      edit(b, e => { e.route.models.reverse(); e.meta.manualModels = [] })
      const manualChanged = (await preview(gw)).list_rev
      expect(typeof rev0).toBe('string')
      expect([reordered === rev0, manualChanged === rev0]).toEqual([false, false])
    })
  })

  test('预览之后清单被别处改过，再带着旧的 rev 确定：409 list_changed 与固定文案，文件不变', async () => {
    await withModels({ handler: openaiOk(['a', 'b', 'd']), before: seed(['a', 'b']) }, async ({ gw, b }) => {
      const pv = await preview(gw)
      edit(b, e => { e.route.models = e.route.models.filter((m: any) => m.id !== 'b') })
      const before = fileBytes(b)
      const r = await gw.call(REFRESH, { name: 'myproxy', only: checkedOf(pv), rev: pv.list_rev })
      expect(r.status, `应 409，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(409)
      expect(r.json).toEqual({ ok: false, error: 'list_changed', text: '清单刚被别处改过，这次没保存，请重新刷新模型列表。' })
      expect(fileBytes(b)).toBe(before)
    })
  })

  test('rev 形状不对（不是字符串、空串、超过 100 个字符）：400 bad_rev 与固定文案，文件不变；恰好 100 个字符的不算形状错（走到比对，回 409）', async () => {
    await withModels({ handler: openaiOk(['a', 'b']), before: seed(['a', 'b']) }, async ({ gw, b }) => {
      const before = fileBytes(b)
      for (const rev of [123, '', 'r'.repeat(101), ['x']]) {
        const r = await gw.call(REFRESH, { name: 'myproxy', only: ['a'], rev })
        expect(r.status, `rev=${JSON.stringify(rev).slice(0, 30)} 应 400，实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(400)
        expect(r.json).toEqual({ ok: false, error: 'bad_rev', text: 'rev 不合法' })
      }
      const edge = await gw.call(REFRESH, { name: 'myproxy', only: ['a'], rev: 'r'.repeat(100) })
      expect([edge.status, edge.json?.error]).toEqual([409, 'list_changed'])
      expect(fileBytes(b)).toBe(before)
    })
  })

  test('重复提交同一次确定（同一个 rev，这次确定改了清单）：第一次 200，第二次 409，清单停在第一次的结果', async () => {
    await withModels({ handler: openaiOk(['a', 'b']), before: seed(['a', 'b']) }, async ({ gw, b }) => {
      const pv = await preview(gw)
      const body = { name: 'myproxy', only: ['a'], rev: pv.list_rev }
      const r1 = await gw.call(REFRESH, body)
      const r2 = await gw.call(REFRESH, body)
      expect([r1.status, r2.status, r2.json?.error]).toEqual([200, 409, 'list_changed'])
      expect(ids(b)).toEqual(['a'])
    })
  })

})

describe('什么都不动直接确定，清单原样不变（3.4）', () => {
  const CASES: { what: string; list: SeedModel[]; manual: string[]; atPreview: Handler; atConfirm?: Handler }[] = [
    { what: '对方返回的先后与清单不同，清单里有不是手填、对方这次没返回的模型，手填名单里还有一个本来就不在清单的名字', list: ['a', 'b', { id: 'c', contextWindow: 5000 }, 'gone'], manual: ['c', 'ghost'], atPreview: openaiOk(['d', 'b', 'a']) },
    { what: '对方回空目录', list: ['a', { id: 'b', contextWindow: 8000 }], manual: [], atPreview: openaiOk([]) },
    { what: '确定时对方返回的内容与预览时不同，给的上下文长度也与清单里的不同或没给', list: [{ id: 'a', contextWindow: 8000 }, 'b', { id: 'c', contextWindow: 5000 }], manual: ['c'], atPreview: openaiOk([{ id: 'a', context_length: 32000 }, 'b', 'c', 'd']), atConfirm: openaiOk(['d', { id: 'b', context_length: 64000 }, { id: 'a', context_length: 16000 }, 'e']) },
  ]
  for (const c of CASES) {
    test(`${c.what}：200，模型名、先后、上下文长度、手填名单、猜的上下文名单全都不变，added 0、removed 为空`, async () => {
      await withModels({ handler: c.atPreview, before: seed(c.list, c.manual) }, async ({ gw, b, fm }) => {
        const before = entry(b)
        const pv = await preview(gw)
        if (c.atConfirm) fm.setHandler(c.atConfirm)
        const r = await gw.call(REFRESH, { name: 'myproxy', only: checkedOf(pv), rev: pv.list_rev })
        expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
        const after = entry(b)
        expect(after.route.models).toEqual(before.route.models)
        expect(after.meta.manualModels).toEqual(before.meta.manualModels)
        expect(after.meta.guessedContext).toEqual(before.meta.guessedContext)
        expect(r.json).toMatchObject({ ok: true, added: 0, removed: [], selected: before.route.models.length, kept_manual: before.meta.manualModels.length, applied: 'selection' })
      })
    })
  }

  test('清单本身超过 1000 个：默认勾选直接确定回 400 bad_only，文件不变（不会被悄悄裁成对方返回的那几个）', async () => {
    const many = Array.from({ length: 1001 }, (_, i) => `m${i}`)
    await withModels({ handler: openaiOk(['m0', 'new-1']), before: seed(many) }, async ({ gw, b }) => {
      const before = fileBytes(b)
      const pv = await preview(gw)
      const checked = checkedOf(pv)
      expect(checked.length, '在清单里的 1001 个都该默认打勾').toBe(1001)
      const r = await gw.call(REFRESH, { name: 'myproxy', only: checked, rev: pv.list_rev })
      expect(r.status).toBe(400)
      expect(r.json).toEqual({ ok: false, error: 'bad_only', text: 'only 必须是模型名数组' })
      expect(fileBytes(b)).toBe(before)
    })
  })
})

describe('按勾选保存的其余各格（3.2）', () => {
  test('上下文长度与"猜的"名单：原来在清单的保持原值；新加的取对方给的值，没给就按 1048576 并记进名单；被取消勾选的移出名单', async () => {
    const fetched = [{ id: 'a', context_length: 32000 }, { id: 'd', context_length: 64000 }, 'e']
    await withModels({ handler: openaiOk(fetched), before: seed([{ id: 'a', contextWindow: 8000 }, 'g']) }, async ({ gw, b }) => {
      expect(entry(b).meta.guessedContext, '前提：g 的上下文是猜的').toEqual(['g'])
      const r = await gw.call(REFRESH, { name: 'myproxy', only: ['a', 'd', 'e'] })
      expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(entry(b).route.models).toEqual([{ id: 'a', contextWindow: 8000 }, { id: 'd', contextWindow: 64000 }, { id: 'e', contextWindow: 1048576 }])
      expect(entry(b).meta.guessedContext).toEqual(['e'])
      expect(r.json).toMatchObject({ added: 2, removed: ['g'], selected: 3, applied: 'selection' })
    })
  })

})
