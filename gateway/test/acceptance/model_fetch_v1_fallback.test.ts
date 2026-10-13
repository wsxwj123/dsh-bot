// 验收：供应商地址只写域名时拉模型清单（INTERFACE-dsh修补 第二节）。
// 起因：有的中转站对 <地址>/models 回的是状态码 200 的网页。改后这种情况也到 <地址>/v1/models 再试一次，成功就把地址补上 /v1。
// 黑盒：只看假模型服务收到的请求（次数、路径、鉴权对不对）、接口的状态码与返回体、Telegram 里的回复、providers.json。
// 假模型服务只在 127.0.0.1 上，密钥是测试自己编的。
import { describe, expect, setDefaultTimeout, test } from 'bun:test'
import { readFileSync } from 'fs'
import { OWNER, findSecret, providerAdd, providersPath, readProviders, seedCreds, seedProviders, waitSentText, withModels, type BotEnv } from './_acc'
import { FakeModels, type Handler, json, openaiBody, openaiOk } from './fake-models'

setDefaultTimeout(60_000)

const PREVIEW = '/v1/provider/preview'
const REFRESH = '/v1/provider/refresh'
const TEST = '/v1/provider/test'
const PAGE_MARK = 'welcome-page-marker-5c1d'

/** 一张状态码 200 的网页（不是模型清单） */
const page: Handler = () => new Response(`<!doctype html><html><body><h1>${PAGE_MARK}</h1></body></html>`, { status: 200, headers: { 'content-type': 'text/html; charset=utf-8' } })
/** /models 交给 first，/v1/models 交给 second，其它路径 404 */
const split = (first: Handler, second: Handler): Handler => (r, n, raw) =>
  r.path === '/models' ? first(r, n, raw) : r.path === '/v1/models' ? second(r, n, raw) : json({ error: 'not found' }, 404)
const paths = (fm: FakeModels) => fm.requests.map(r => `${r.method} ${r.path}`)
const TWO = ['GET /models', 'GET /v1/models']

/** 预置一个自建供应商：地址缺省只写域名（路径为空），清单缺省为空，凭据齐 */
const seed = (o: { base?: (fm: FakeModels) => string; models?: string[]; api?: any } = {}) => (b: BotEnv, fm: FakeModels) => {
  seedProviders(b.root, [{ name: 'myproxy', api: o.api, baseURL: o.base ? o.base(fm) : fm.url, models: o.models ?? [] }])
  seedCreds(b.root, { PROVIDER_MYPROXY_KEY: fm.expectKey })
}
const entry = (b: BotEnv) => readProviders(b.root).providers.myproxy
const fileBytes = (b: BotEnv) => readFileSync(providersPath(b.root), 'utf8')

describe('/models 回了 200 的网页：到 /v1/models 再试一次', () => {
  test('/provider add 只写域名：先后各发一次（同一把密钥），保存的地址补上 /v1，回复里写明「地址补成了 …」', async () => {
    await withModels({ handler: split(page, openaiOk(['gpt-a', 'gpt-b'])) }, async ({ tg, b, fm, key }) => {
      const { reply } = await providerAdd(tg, `myproxy ${fm.url} ${key} openai`)
      expect(paths(fm)).toEqual(TWO)
      expect(fm.requests.map(r => [r.authKind, r.authOk])).toEqual([['bearer', true], ['bearer', true]])
      expect(entry(b).route.baseURL).toBe(`${fm.url}/v1`)
      expect(entry(b).route.models.map((m: any) => m.id)).toEqual(['gpt-a', 'gpt-b'])
      expect(reply.text!).toContain('【系统】已添加供应商「myproxy」，拉到 2 个模型。')
      expect(reply.text!).toContain(`（地址补成了 ${fm.url}/v1）`)
    })
  })

  test('预览：200，count 与 models 按第二次拉到的算；仍是只读，providers.json 逐字节不变（地址没被补）', async () => {
    await withModels({ handler: split(page, openaiOk(['a', 'b'])), before: seed({ models: ['a'] }) }, async ({ gw, b, fm }) => {
      const before = fileBytes(b)
      const r = await gw.call(PREVIEW, { name: 'myproxy' })
      expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect([r.json.count, r.json.models.map((m: any) => m.id)]).toEqual([2, ['a', 'b']])
      expect(paths(fm)).toEqual(TWO)
      expect(fileBytes(b)).toBe(before)
    })
  })

  const NOT_A_LIST: [string, Handler][] = [
    ['是 JSON 但不是对象（数组）', () => json([1, 2])],
    ['是对象但没有 data 也没有 models 数组', () => json({ ok: true, message: 'hello' })],
    ['正文超过 4 MiB', () => json({ ...(openaiBody(['big-1']) as object), pad: 'x'.repeat(4 * 1024 * 1024 + 16) })],
  ]
  test('第一次回 200 但不是清单的其余三种样子（JSON 数组、没有 data 也没有 models 的对象、正文超过 4 MiB）：同样再试一次并成功', async () => {
    for (const [what, first] of NOT_A_LIST) {
      await withModels({ handler: split(first, openaiOk(['a', 'b'])), before: seed() }, async ({ gw, fm }) => {
        const r = await gw.call(PREVIEW, { name: 'myproxy' })
        expect(r.status, `${what}：应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
        expect(r.json.models.map((m: any) => m.id)).toEqual(['a', 'b'])
        expect(paths(fm), what).toEqual(TWO)
      })
    }
  })

  test('带勾选的刷新补了地址：text 末尾依次是「（按勾选保留 2 个）（地址补成了 …）」，地址落盘；同样的请求再发一次只发一个请求，不再有「地址补成了」', async () => {
    await withModels({ handler: split(page, openaiOk(['a', 'b'])), before: seed({ models: ['a'] }) }, async ({ gw, b, fm }) => {
      const r1 = await gw.call(REFRESH, { name: 'myproxy', only: ['a', 'b'] })
      expect(r1.status, `第一次应 200，实得 ${r1.status}，正文 ${r1.text.slice(0, 300)}`).toBe(200)
      expect(String(r1.json.text)).toEndWith(`（按勾选保留 2 个）（地址补成了 ${fm.url}/v1）`)
      expect(entry(b).route.baseURL).toBe(`${fm.url}/v1`)
      const r2 = await gw.call(REFRESH, { name: 'myproxy', only: ['a', 'b'] })
      expect(r2.status).toBe(200)
      expect(paths(fm)).toEqual([...TWO, 'GET /v1/models'])
      expect(String(r2.json.text)).not.toContain('地址补成了')
      expect(entry(b).route.baseURL).toBe(`${fm.url}/v1`)
      expect(entry(b).route.models.map((m: any) => m.id)).toEqual(['a', 'b'])
    })
  })
})

describe('不该再试的情形：只发一次请求', () => {
  test('地址已经带路径（/api）而 /api/models 回了 200 的网页：只发这一次，502 bad_response', async () => {
    const h: Handler = (r, n, raw) => r.path === '/api/models' ? page(r, n, raw) : openaiOk(['should-not-be-fetched'])(r, n, raw)
    await withModels({ handler: h, before: seed({ base: fm => `${fm.url}/api`, models: ['a'] }) }, async ({ gw, b, fm }) => {
      const before = fileBytes(b)
      const r = await gw.call(PREVIEW, { name: 'myproxy' })
      expect(r.status).toBe(502)
      expect(r.json).toMatchObject({ ok: false, error: 'fetch_failed', reason: 'bad_response' })
      expect(paths(fm)).toEqual(['GET /api/models'])
      expect(fileBytes(b)).toBe(before)
    })
  })

  test('Anthropic 格式不变：地址只写域名、对方回 200 的网页，只请求一次 /v1/models，502 bad_response', async () => {
    await withModels({ handler: page, before: seed({ api: 'anthropic-messages', models: ['a'] }) }, async ({ gw, fm }) => {
      const r = await gw.call(PREVIEW, { name: 'myproxy' })
      expect(r.status).toBe(502)
      expect(r.json).toMatchObject({ ok: false, error: 'fetch_failed', reason: 'bad_response' })
      expect(fm.requests.map(q => q.path)).toEqual(['/v1/models'])
    })
  })
})

describe('第二次也失败：报哪一次的原因', () => {
  test('第一次 200 网页、第二次 401：报第二次的（unauthorized），502，文件逐字节不变', async () => {
    await withModels({ handler: split(page, () => json({ error: 'bad key' }, 401)), before: seed({ models: ['a'] }) }, async ({ gw, b, fm }) => {
      const before = fileBytes(b)
      const r = await gw.call(PREVIEW, { name: 'myproxy' })
      expect(r.status, `应 502，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(502)
      expect(Object.keys(r.json).sort()).toEqual(['error', 'ok', 'reason', 'text'])
      expect(r.json).toMatchObject({ ok: false, error: 'fetch_failed', reason: 'unauthorized' })
      expect(String(r.json.text)).toContain('密钥不对或没有权限（HTTP 401）')
      expect(paths(fm)).toEqual(TWO)
      expect(fileBytes(b)).toBe(before)
    })
  })

  test('第一次 200 网页、第二次 404（/provider add）：报第一次的「对方返回的不是模型列表（格式不认识）」，供应商照样保存、地址是填的原样', async () => {
    await withModels({ handler: split(page, () => json({ error: 'nope' }, 404)) }, async ({ tg, b, fm, key }) => {
      const { reply } = await providerAdd(tg, `myproxy ${fm.url} ${key} openai`)
      expect(reply.text!).toContain('【系统】已添加供应商「myproxy」，但没拉到模型：对方返回的不是模型列表（格式不认识）。')
      expect(reply.text!).not.toContain('地址补成了')
      expect(paths(fm)).toEqual(TWO)
      expect(entry(b).route.baseURL).toBe(fm.url)
      expect(entry(b).route.models).toEqual([])
    })
  })

  test('第二次遇到 302 就停：报 redirect，跳转的目标一个请求都收不到', async () => {
    const target = new FakeModels('unused-key-for-redirect-target', openaiOk(['leaked']))
    try {
      const redirect: Handler = () => new Response('', { status: 302, headers: { location: `${target.url}/v1/models` } })
      await withModels({ handler: split(page, redirect), before: seed({ models: ['a'] }) }, async ({ gw, fm }) => {
        const r = await gw.call(PREVIEW, { name: 'myproxy' })
        expect(r.status).toBe(502)
        expect(r.json).toMatchObject({ ok: false, error: 'fetch_failed', reason: 'redirect' })
        expect(paths(fm)).toEqual(TWO)
        expect(target.requests.length).toBe(0)
      })
    } finally { target.stop() }
  })
})

describe('测试连接：只多一句提示', () => {
  test('保存的地址只有域名、对方回 200 但不是 JSON：不到 /v1 下再试，text 末尾多一句「地址只写了域名，可能少写了 /v1」', async () => {
    const h: Handler = () => new Response('hello', { status: 200, headers: { 'content-type': 'text/plain' } })
    await withModels({ handler: h, before: seed({ models: ['m1'] }) }, async ({ gw, fm }) => {
      const r = await gw.call(TEST, { name: 'myproxy' })
      expect(r.status).toBe(502)
      expect(r.json).toMatchObject({ ok: false, error: 'connect_failed', reason: 'bad_response' })
      expect(r.json.text).toBe('对方返回的不是模型列表（格式不认识）；用 m1 测的。地址只写了域名，可能少写了 /v1')
      expect(paths(fm)).toEqual(['POST /chat/completions'])
    })
  })
})

describe('密钥与对方回包不外泄', () => {
  test('/models 回一张把请求头原样印出来的网页：之后在网关日志、接口响应、发出的消息、providers.json 里都搜不到密钥，也搜不到网页里的字', async () => {
    const echo: Handler = (_r, _n, raw) => new Response(
      `<html><body><h1>${PAGE_MARK}</h1><pre>${[...raw.headers].map(([k, v]) => `${k}: ${v}`).join('\n')}</pre></body></html>`,
      { status: 200, headers: { 'content-type': 'text/html' } })
    await withModels({ handler: split(echo, openaiOk(['a', 'b'])), before: seed() }, async ({ tg, gw, b, fm, key }) => {
      const pv = await gw.call(PREVIEW, { name: 'myproxy' })
      expect(pv.status, `前提：预览走完两次请求并成功，实得 ${pv.status}`).toBe(200)
      const k = tg.sentTo(OWNER).length
      tg.pushText(OWNER, '/provider refresh myproxy')
      await waitSentText(tg, OWNER, '【系统】「myproxy」', k, 20_000)
      expect(paths(fm).filter(p => p === 'GET /models').length, '前提：那张网页确实被取回过').toBeGreaterThanOrEqual(1)
      expect(findSecret(b, key, { gw, extra: { '预览的响应': pv.text } })).toEqual([])
      expect(findSecret(b, PAGE_MARK, { gw, extra: { '预览的响应': pv.text } })).toEqual([])
    })
  })
})
