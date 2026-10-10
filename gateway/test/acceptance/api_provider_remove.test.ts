// 验收：网关本机接口 POST /v1/provider/remove（INTERFACE-管理台UI 3.3）。
// 契约要点成功 200 {ok,name,key_grace_ms,text}，text 是既有一行命令的删除文案去掉【系统】前缀；
// 正在用时加一句并清掉本 bot 的模型覆盖；不合格条目 409 disabled；找不到 404 not_found；
// 错误码 busy / providers_unreadable / save_failed。
// 实现未落地时本文件红（多半 404），那是红基线，不是测试坏了。
import { describe, expect, test } from 'bun:test'
import { writeFileSync } from 'fs'
import { defaultKeyEnv, lockPath, override, providersPath, readProviders, seedCreds, seedProviders, waitEvent, waitSentText, withBot, withModels, OWNER, type BotEnv } from './_acc'
import { type FakeModels, openaiOk } from './fake-models'

const IDS = ['m1', 'm2']
const REMOVE = '/v1/provider/remove'
// 既有一行命令的删除文案（provider_delete.test.ts 断言过的全文），按契约去掉【系统】前缀
const FULL = '已删除供应商「myproxy」。它的密钥会在 10 分钟后从凭据文件删除（让正在用它的 bot 先切走）。注意：这只是从本机删掉，不会在供应商那边作废这把密钥；如果担心泄露，请到供应商后台作废它。'

const seedCustom = (o: { key?: string | null } = {}) => (b: BotEnv, fm: FakeModels) => {
  seedProviders(b.root, [{ name: 'myproxy', baseURL: `${fm.url}/v1`, models: IDS }])
  if (o.key !== null) seedCreds(b.root, { [defaultKeyEnv('myproxy')]: o.key ?? fm.expectKey })
}

describe('POST /v1/provider/remove 的正常路径', () => {
  test('成功：200 {ok,name,key_grace_ms,text}，text 与既有删除文案一致（去掉【系统】前缀）', async () => {
    await withModels({ handler: openaiOk(IDS), before: seedCustom() }, async ({ gw, b }) => {
      const r = await gw.call(REMOVE, { name: 'myproxy' })
      expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(r.json, '正文应与契约 3.3 的样例一致（text 按全文判）').toEqual({
        ok: true, name: 'myproxy', key_grace_ms: 600_000, text: FULL,
      })
      expect(r.json.text.startsWith('【系统】'), 'text 不该带【系统】前缀').toBe(false)
      expect(readProvidersOf(b).providers.myproxy, 'providers.json 里的条目应没了').toBeUndefined()
    })
  })

  test('删除后凭据登记进 pendingKeyRemovals（宽限后删密钥）', async () => {
    await withModels({ handler: openaiOk(IDS), before: seedCustom() }, async ({ gw, b }) => {
      const r = await gw.call(REMOVE, { name: 'myproxy' })
      expect(r.status, `应 200，实得 ${r.status}`).toBe(200)
      const pending = readProvidersOf(b).pendingKeyRemovals ?? []
      expect(pending.some((x: any) => x.key === defaultKeyEnv('myproxy')), `应登记 ${defaultKeyEnv('myproxy')} 待删，实得 ${JSON.stringify(pending)}`).toBe(true)
    })
  })

  test('本 bot 正在用它：text 加一句已换回配置文件里的模型，并清掉本 bot 的模型覆盖', async () => {
    await withModels({ handler: openaiOk(IDS), before: seedCustom() }, async ({ tg, gw, b }) => {
      tg.pushText(OWNER, '/model myproxy/m1')
      await waitSentText(tg, OWNER, '已换成 myproxy / m1', 0, 20_000)
      expect(override(b)?.provider, '前置条件 覆盖应指向 myproxy').toBe('myproxy')
      const r = await gw.call(REMOVE, { name: 'myproxy' })
      expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(String(r.json.text), `text 应加一句换回配置文件里的模型，实得 ${JSON.stringify(r.json.text)}`).toContain('这个 bot 已换回配置文件里的模型')
      expect(override(b)?.provider ?? null, '本 bot 的模型覆盖应被清掉').toBeNull()
      await waitEvent(b, 'brain.override_cleared', e => e.reason === 'provider removed', 15_000)
    })
  })
})

describe('POST /v1/provider/remove 的错误', () => {
  for (const name of ['-abc', 'bad.name', '', 123, undefined] as const) {
    test(`name 不合规则（${JSON.stringify(name)}）：400 bad_name`, async () => {
      await withModels({ handler: openaiOk(IDS), before: seedCustom() }, async ({ gw }) => {
        const r = await gw.call(REMOVE, { name })
        expect(r.status, `应 400，实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(400)
        expect(r.json, `error 应是 bad_name，实得 ${JSON.stringify(r.json)}`).toMatchObject({ ok: false, error: 'bad_name' })
      })
    })
  }

  test('名字是不合格条目：409 disabled，text 说明配置有误不能删除', async () => {
    await withModels({ handler: openaiOk(IDS), before: b => {
      seedProviders(b.root, [{ name: 'broken', api: 'grpc' as any, baseURL: 'https://x.example.com/v1', models: ['x1'] }])
    } }, async ({ gw }) => {
      const r = await gw.call(REMOVE, { name: 'broken' })
      expect(r.status, `应 409，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(409)
      expect(r.json, `error 应是 disabled，实得 ${JSON.stringify(r.json)}`).toMatchObject({ ok: false, error: 'disabled' })
      expect(String(r.json.text), 'text 应说配置有误、未启用、不能删除').toContain('不能删除')
    })
  })

  test('找不到：404 not_found，text 说没有这个名字', async () => {
    await withModels({ handler: openaiOk(IDS), before: seedCustom() }, async ({ gw }) => {
      const r = await gw.call(REMOVE, { name: 'nosuch' })
      expect(r.status, `应 404，实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(404)
      expect(r.json, `error 应是 not_found，实得 ${JSON.stringify(r.json)}`).toMatchObject({ ok: false, error: 'not_found' })
      expect(String(r.json.text), 'text 应带上名字').toContain('nosuch')
    })
  })

  test('拿不到锁：409 busy', async () => {
    await withModels({ gw: { provider_lock_wait_ms: 300 }, handler: openaiOk(IDS), before: b => {
      seedProviders(b.root, [{ name: 'myproxy', baseURL: 'https://x.example.com/v1', models: IDS }])
      writeFileSync(lockPath(b.root), JSON.stringify({ pid: process.pid, at: Date.now() }))
    } }, async ({ gw }) => {
      const r = await gw.call(REMOVE, { name: 'myproxy' })
      expect(r.status, `锁被占应 409，实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(409)
      expect(r.json.error, `error 应是 busy，实得 ${JSON.stringify(r.json.error)}`).toBe('busy')
    })
  })

  test('providers.json 读不了：503 providers_unreadable', async () => {
    await withModels({ handler: openaiOk(IDS), before: b => writeFileSync(providersPath(b.root), '[[[') }, async ({ gw }) => {
      const r = await gw.call(REMOVE, { name: 'myproxy' })
      expect(r.status, `应 503，实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(503)
      expect(r.json.error, `error 应是 providers_unreadable，实得 ${JSON.stringify(r.json.error)}`).toBe('providers_unreadable')
    })
  })

  test('请求体不是 JSON 对象：400 invalid json', async () => {
    await withBot({ before: b => seedProviders(b.root, []) }, async ({ gw }) => {
      const r = await gw.api(REMOVE, { method: 'POST', headers: { authorization: `Bearer ${gw.apiToken()}`, 'content-type': 'application/json' }, body: '"myproxy"' })
      expect(r.status, `应 400，实得 ${r.status}`).toBe(400)
      const j = await r.json() as any
      expect(j?.error, `error 应是 invalid json，实得 ${JSON.stringify(j)}`).toBe('invalid json')
    })
  })

  test('带 Origin 头：403；Content-Type 不对：415', async () => {
    await withBot({ before: b => seedProviders(b.root, []) }, async ({ gw }) => {
      const withOrigin = await gw.api(REMOVE, { method: 'POST', headers: { authorization: `Bearer ${gw.apiToken()}`, origin: 'http://evil.example', 'content-type': 'application/json' }, body: '{"name":"myproxy"}' })
      expect(withOrigin.status, `带 Origin 应 403，实得 ${withOrigin.status}`).toBe(403)
      const badCt = await gw.api(REMOVE, { method: 'POST', headers: { authorization: `Bearer ${gw.apiToken()}`, 'content-type': 'text/plain' }, body: '{"name":"myproxy"}' })
      expect(badCt.status, `Content-Type 不对应 415，实得 ${badCt.status}`).toBe(415)
    })
  })

  test('没带口令：401', async () => {
    await withBot({ before: b => seedProviders(b.root, []) }, async ({ gw }) => {
      const r = await gw.api(REMOVE, { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{"name":"myproxy"}' })
      expect(r.status, `应 401，实得 ${r.status}`).toBe(401)
    })
  })
})

const readProvidersOf = (b: BotEnv): any => readProviders(b.root)
