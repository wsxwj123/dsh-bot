// 验收：刷新和保存默认不改动挑好的清单（INTERFACE-dsh修补 第五节）。
// 起因：在聊天里点「刷新模型」或发 /provider refresh，会把清单重铺成"对方返回的全部加手填的"，冲掉在管理台挑好的子集。
// 改后：清单不为空时，不带勾选的刷新、落到已有供应商上的保存都只报数（对账句），不改清单；要重铺只能显式带 full。
// 黑盒：经假 Telegram 发命令/点按钮、调本机接口，读 providers.json、假 dsh 的启动记录。
// 聊天刷新成功后的文案与落盘（清单 [m1, m2, man1] 的那一组）在 provider_refresh.test.ts，这里是其余各格。
import { describe, expect, setDefaultTimeout, test } from 'bun:test'
import { readFileSync, writeFileSync } from 'fs'
import { DISJOINT_NOTE, OWNER, botTexts, dshStarts, labels, providerAdd, providersPath, readCreds, readProviders, reconLine, seedCreds, seedProviders, sleep, until, waitSentText, warmUp, withModels, wizardCreate, type BotEnv, type SeedModel } from './_acc'
import { type FakeModels, type Handler, json, openaiOk, sleep as fsleep } from './fake-models'

setDefaultTimeout(60_000)

const REFRESH = '/v1/provider/refresh'
const SAVE = '/v1/provider/save'
const OLD_KEY = 'test-key-7keeplistold00000001'
const DELETED_NOTE = '（你发的密钥消息已删除。）'
/** 清单 [m1（主人填的 50000）, m2（上下文是猜的）, man1（手填）]，对方返回 [m1, m3, m4]：N=3，不在清单 2 个，没返回 2 个 */
const LIST: SeedModel[] = [{ id: 'm1', contextWindow: 50000 }, 'm2', 'man1']
const FETCHED = [{ id: 'm1', context_length: 8000 }, 'm3', 'm4', 'm3']

const seed = (o: { models?: SeedModel[]; manual?: string[]; base?: (fm: FakeModels) => string; name?: string; key?: string } = {}) => (b: BotEnv, fm: FakeModels) => {
  const name = o.name ?? 'myproxy'
  seedProviders(b.root, [{ name, baseURL: o.base ? o.base(fm) : `${fm.url}/v1`, models: o.models ?? LIST, manualModels: o.manual ?? ['man1'], ownerContext: ['m1'] }])
  seedCreds(b.root, { [`PROVIDER_${name.toUpperCase().replace(/-/g, '_')}_KEY`]: o.key ?? fm.expectKey })
}
const entry = (b: BotEnv, name = 'myproxy') => readProviders(b.root).providers[name]
const ids = (b: BotEnv) => entry(b).route.models.map((m: any) => m.id)
/** 清单有关的三样：模型（名字、先后、上下文）、手填名单、猜的上下文名单 */
const listing = (b: BotEnv, name = 'myproxy') => { const e = entry(b, name); return JSON.stringify([e.route.models, e.meta.manualModels, e.meta.guessedContext]) }
const fileBytes = (b: BotEnv) => readFileSync(providersPath(b.root), 'utf8')
const edit = (b: BotEnv, fn: (e: any) => void) => { const p = readProviders(b.root); fn(p.providers.myproxy); writeFileSync(providersPath(b.root), JSON.stringify(p, null, 1)) }

/** 发 /provider refresh，返回 bot 的那条回复 */
async function refreshCmd(tg: any, name = 'myproxy') {
  const k = tg.sentTo(OWNER).length
  tg.pushText(OWNER, `/provider refresh ${name}`)
  return waitSentText(tg, OWNER, `【系统】「${name}」`, k, 20_000)
}

describe('聊天里的刷新（/provider refresh）', () => {
  test('清单不为空：写进文件的只有刷新记录和更新时间，别的一个字都不变（模型、先后、上下文、手填名单、猜的上下文名单）', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: seed() }, async ({ tg, b }) => {
      const strip = () => { const p = readProviders(b.root); delete p.providers.myproxy.meta.lastRefresh; delete p.providers.myproxy.meta.updatedAt; return JSON.stringify(p) }
      const before = strip()
      const s = await refreshCmd(tg)
      expect(s.text).toBe(`【系统】「myproxy」${reconLine(3, 2, 2)}`)
      expect(strip()).toBe(before)
      expect(entry(b).meta.lastRefresh).toMatchObject({ ok: true, count: 3, reason: null })
    })
  })

  test('清单里的模型对方一个都没返回：对账句后面再接一句「…这个地址上的内容可能换了，建议到管理台重新挑。」，清单仍不动', async () => {
    await withModels({ handler: openaiOk(['n1', 'n2', 'n3']), before: seed({ models: ['old1', 'old2'], manual: [] }) }, async ({ tg, b }) => {
      const s = await refreshCmd(tg)
      expect(s.text).toBe(`【系统】「myproxy」${reconLine(3, 3, 2)}${DISJOINT_NOTE}`)
      expect(ids(b)).toEqual(['old1', 'old2'])
    })
  })

  test('清单与对方返回的完全一样（只是先后不同）：两个数照写 0，没有「一个都没返回」那一句', async () => {
    await withModels({ handler: openaiOk(['b', 'a']), before: seed({ models: ['a', 'b'], manual: [] }) }, async ({ tg, b }) => {
      const s = await refreshCmd(tg)
      expect(s.text).toBe(`【系统】「myproxy」${reconLine(2, 0, 0)}`)
      expect(ids(b)).toEqual(['a', 'b'])
    })
  })

  test('清单不为空：这次刷新不会让 dsh 重启', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: seed() }, async ({ tg, b }) => {
      await warmUp(tg)
      const starts = dshStarts(b)
      await refreshCmd(tg)
      await sleep(1_500)
      await warmUp(tg, '刷新完再说一句')
      expect(dshStarts(b)).toBe(starts)
    })
  })

  test('地址只写了域名、这次是在 /v1 下才拉到的：对账句末尾再接「（地址补成了 …）」，地址补上，清单仍不动', async () => {
    const page: Handler = () => new Response('<html><body>hello</body></html>', { status: 200, headers: { 'content-type': 'text/html' } })
    const h: Handler = (r, n, raw) => r.path === '/models' ? page(r, n, raw) : openaiOk(['a', 'b'])(r, n, raw)
    await withModels({ handler: h, before: seed({ models: ['a'], manual: [], base: fm => fm.url }) }, async ({ tg, b, fm }) => {
      const s = await refreshCmd(tg)
      expect(s.text).toBe(`【系统】「myproxy」${reconLine(2, 1, 0)}（地址补成了 ${fm.url}/v1）`)
      expect(entry(b).route.baseURL).toBe(`${fm.url}/v1`)
      expect(ids(b)).toEqual(['a'])
    })
  })

  test('拉取期间清单被别处填上了：按保存那一刻"不为空"处理，不改清单', async () => {
    const slow: Handler = async (r, n, raw) => { await fsleep(1_500); return openaiOk(FETCHED)(r, n, raw) }
    await withModels({ handler: slow, before: seed({ models: [], manual: [] }) }, async ({ gw, b }) => {
      const pending = gw.call(REFRESH, { name: 'myproxy' })
      await sleep(500)
      edit(b, e => { e.route.models = [{ id: 'picked-elsewhere', contextWindow: 1048576 }] })
      const r = await pending
      expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(r.json).toMatchObject({ applied: 'kept', count: 3, not_in_list: 3, not_returned: 1 })
      expect(ids(b)).toEqual(['picked-elsewhere'])
    })
  })
})

describe('新建结果里的「刷新模型」按钮', () => {
  test('新建时没拉到模型（清单是空的），之后点这个按钮：拉到的全部收下', async () => {
    await withModels({ handler: () => json({ error: 'bad key' }, 401) }, async ({ tg, b, fm, key }) => {
      const { result } = await wizardCreate(tg, { name: 'myproxy', fmt: 'openai', url: `${fm.url}/v1`, key })
      expect(labels(tg, result.messageId)).toEqual(['刷新模型', '关闭'])
      fm.setHandler(openaiOk(['gpt-a', 'gpt-b', 'gpt-c']))
      tg.clickButton(OWNER, OWNER, result.messageId, '刷新模型')
      await until(() => botTexts(tg, OWNER).some(t => t.includes('【系统】「myproxy」拉到 3 个模型（新增 3 个，去掉 0 个；手动加的 0 个保留）。')), '刷新结果', 20_000)
      expect(ids(b)).toEqual(['gpt-a', 'gpt-b', 'gpt-c'])
    })
  })
})

describe('保存到已有的供应商', () => {
  test('/provider add 用了已有的名字（同一主机、同一格式，清单不为空）：密钥照常更新，清单不动，回「已更新供应商「myproxy」。」接对账句', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: seed({ key: OLD_KEY }) }, async ({ tg, b, fm, key }) => {
      const before = listing(b)
      const { reply } = await providerAdd(tg, `myproxy ${fm.url}/v1 ${key} openai`)
      expect(reply.text!).toContain(`【系统】已更新供应商「myproxy」。${reconLine(3, 2, 2)}`)
      expect(reply.text!).toContain(DELETED_NOTE)
      expect(reply.text!).not.toContain('拉到 3 个模型')
      expect(listing(b)).toBe(before)
      expect(readCreds(b.root)).toContain(key)
      expect(readCreds(b.root)).not.toContain(OLD_KEY)
    })
  })

  test('本机接口保存到已有的（四条都成立）：200，applied 为 kept，count/not_in_list/not_returned 是对账的三个数，清单与原来的刷新记录都不变', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: seed() }, async ({ gw, b, fm }) => {
      const mark = { at: 1_700_000_000_123, ok: true, count: 7, reason: null }
      edit(b, e => { e.meta.lastRefresh = mark })
      const before = listing(b)
      const r = await gw.call(SAVE, { name: 'myproxy', api: 'openai-completions', baseURL: `${fm.url}/v1`, key: '', mode: 'modify' })
      expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(r.json).toMatchObject({ ok: true, kind: 'updated', name: 'myproxy', reason: null, epoch_changed: false, applied: 'kept', count: 3, not_in_list: 2, not_returned: 2 })
      expect(String(r.json.text).startsWith(`已更新供应商「myproxy」。${reconLine(3, 2, 2)}`), `text 实得 ${JSON.stringify(r.json.text)}`).toBe(true)
      expect(listing(b)).toBe(before)
      expect(entry(b).meta.lastRefresh).toEqual(mark)
    })
  })

})

describe('本机接口 POST /v1/provider/refresh 的 full', () => {
  test('full 为 true：与原来不带勾选的刷新一样把清单重铺成"对方返回的全部加手填的"，体里多一个 applied 为 full', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: seed() }, async ({ gw, b }) => {
      const r = await gw.call(REFRESH, { name: 'myproxy', full: true })
      expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(r.json).toMatchObject({ ok: true, count: 3, added: 2, removed: 1, kept_manual: 1, applied: 'full' })
      expect(String(r.json.text)).toContain('「myproxy」拉到 3 个模型（新增 2 个，去掉 1 个；手动加的 1 个保留）。')
      expect(entry(b).route.models).toEqual([{ id: 'm1', contextWindow: 50000 }, { id: 'm3', contextWindow: 1048576 }, { id: 'm4', contextWindow: 1048576 }, { id: 'man1', contextWindow: 1048576 }])
    })
  })

  test('没有 only、没有 full、清单为空：照旧全部收下，体里多一个 applied 为 full', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: seed({ models: [], manual: [] }) }, async ({ gw, b }) => {
      const r = await gw.call(REFRESH, { name: 'myproxy' })
      expect(r.status).toBe(200)
      expect(r.json).toMatchObject({ ok: true, count: 3, added: 3, applied: 'full' })
      expect(ids(b)).toEqual(['m1', 'm3', 'm4'])
    })
  })

  test('full 和 only 同时给：400 bad_body「only 和 full 不能同时给」，文件不变，也不去拉模型', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: seed() }, async ({ gw, b, fm }) => {
      const before = fileBytes(b)
      const r = await gw.call(REFRESH, { name: 'myproxy', only: ['m1'], full: true })
      expect(r.status).toBe(400)
      expect(r.json).toEqual({ ok: false, error: 'bad_body', text: 'only 和 full 不能同时给' })
      expect(fileBytes(b)).toBe(before)
      expect(fm.requests.length).toBe(0)
    })
  })

  test('full 不是布尔值（"true"、1、"yes"）：400 bad_body「full 必须是 true 或 false」，文件不变；full 为 false 合法，等同没给', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: seed() }, async ({ gw, b }) => {
      const before = listing(b)
      for (const full of ['true', 1, 'yes']) {
        const r = await gw.call(REFRESH, { name: 'myproxy', full })
        expect(r.status, `full=${JSON.stringify(full)} 应 400，实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(400)
        expect(r.json).toEqual({ ok: false, error: 'bad_body', text: 'full 必须是 true 或 false' })
      }
      expect(listing(b)).toBe(before)
      const ok = await gw.call(REFRESH, { name: 'myproxy', full: false })
      expect([ok.status, ok.json?.applied]).toEqual([200, 'kept'])
      expect(listing(b)).toBe(before)
    })
  })
})
