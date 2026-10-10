// 验收：网关本机接口 POST /v1/provider/model（INTERFACE-管理台UI 3.4）。
// 契约要点action 只认 add/remove/set_context；id 走 cleanModelId（1 到 200 字符、不含空白与控制字符、不含 ⟦⟧）；
// contextWindow 给了必须是 1024 到 100000000 的整数，set_context 时必须给，add 可不给（按 131072 记入 guessed）；
// 成功三条 text 与 Telegram「管理模型」同源；错误码 bad_action / bad_id / bad_context / not_found / exists / disabled / busy / save_failed / providers_unreadable。
// 实现未落地时本文件红（多半 404），那是红基线，不是测试坏了。
import { describe, expect, test } from 'bun:test'
import { writeFileSync } from 'fs'
import { defaultKeyEnv, lockPath, override, providersPath, readProviders, seedCreds, seedProviders, waitSentText, withBot, withModels, OWNER, type BotEnv } from './_acc'
import { type FakeModels, openaiOk } from './fake-models'

const IDS = ['m1', 'm2']
const MODEL = '/v1/provider/model'
const ROUTES = { myroute: { api: 'openai-completions', baseURL: 'https://r.example.com/v1', apiKeyEnv: 'MYROUTE_KEY', models: [{ id: 'r1', contextWindow: 32768 }] } }

const seedCustom = (o: { models?: string[]; manual?: string[] } = {}) => (b: BotEnv, fm: FakeModels) => {
  seedProviders(b.root, [{ name: 'myproxy', baseURL: `${fm.url}/v1`, models: o.models ?? IDS, manualModels: o.manual ?? [] }])
  seedCreds(b.root, { [defaultKeyEnv('myproxy')]: fm.expectKey })
}
const entry = (b: BotEnv) => readProviders(b.root).providers.myproxy
const body = (over: Record<string, unknown> = {}) => ({ name: 'myproxy', action: 'add', id: 'new-model', contextWindow: 128_000, ...over })

describe('POST /v1/provider/model 的 add', () => {
  test('加上新模型：200，text 与 Telegram 同源，落盘带上下文', async () => {
    await withModels({ handler: openaiOk(IDS), before: seedCustom() }, async ({ gw, b }) => {
      const r = await gw.call(MODEL, body())
      expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(r.json.ok, 'ok 应为 true').toBe(true)
      expect(r.json.text, `text 应是同源三条之一，实得 ${JSON.stringify(r.json.text)}`).toBe('已给「myproxy」加上模型 new-model（上下文 128000）。几秒后就能切过去。')
      const m = entry(b).route.models.find((x: any) => x.id === 'new-model')
      expect(m, '落盘应有 new-model').toBeTruthy()
      expect(m.contextWindow, '上下文应记 128000').toBe(128_000)
      expect(entry(b).meta.guessedContext, '给了上下文的不该记入 guessed').not.toContain('new-model')
    })
  })

  test('add 不给 contextWindow：按 131072 算，记入 guessed', async () => {
    await withModels({ handler: openaiOk(IDS), before: seedCustom() }, async ({ gw, b }) => {
      const r = await gw.call(MODEL, body({ contextWindow: undefined }))
      expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      const m = entry(b).route.models.find((x: any) => x.id === 'new-model')
      expect(m.contextWindow, '不给上下文按 131072 算').toBe(131_072)
      expect(entry(b).meta.guessedContext, '未声明上下文的应记入 guessed').toContain('new-model')
    })
  })

  test('add 已经有的模型：409 exists，text 说这个模型已经有了', async () => {
    await withModels({ handler: openaiOk(IDS), before: seedCustom() }, async ({ gw }) => {
      const r = await gw.call(MODEL, body({ id: 'm1' }))
      expect(r.status, `应 409，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(409)
      expect(r.json, `error 应是 exists，实得 ${JSON.stringify(r.json)}`).toMatchObject({ ok: false, error: 'exists' })
      expect(String(r.json.text), 'text 应说已经有了').toContain('已经有了')
    })
  })

  test('add 上下文边界值 1024 与 100000000 都可用', async () => {
    await withModels({ handler: openaiOk(IDS), before: seedCustom() }, async ({ gw }) => {
      const lo = await gw.call(MODEL, body({ id: 'low', contextWindow: 1024 }))
      expect(lo.status, `1024 应可用，实得 ${lo.status}，正文 ${lo.text.slice(0, 200)}`).toBe(200)
      const hi = await gw.call(MODEL, body({ id: 'high', contextWindow: 100_000_000 }))
      expect(hi.status, `100000000 应可用，实得 ${hi.status}，正文 ${hi.text.slice(0, 200)}`).toBe(200)
    })
  })
})

describe('POST /v1/provider/model 的 remove', () => {
  test('删拉到的模型：text 带「下次刷新会重新出现」的意思', async () => {
    await withModels({ handler: openaiOk(IDS), before: seedCustom() }, async ({ gw, b }) => {
      const r = await gw.call(MODEL, { name: 'myproxy', action: 'remove', id: 'm1' })
      expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(String(r.json.text), `text 应说已删掉，实得 ${JSON.stringify(r.json.text)}`).toContain('已从「myproxy」删掉模型 m1。')
      expect(String(r.json.text), `拉到的模型应提醒会重新出现，实得 ${JSON.stringify(r.json.text)}`).toContain('重新出现')
      expect(entry(b).route.models.map((m: any) => m.id), '落盘应只剩 m2').toEqual(['m2'])
    })
  })

  test('删手动加的模型：不提重新出现', async () => {
    await withModels({ handler: openaiOk(IDS), before: seedCustom({ models: [...IDS, 'man1'], manual: ['man1'] }) }, async ({ gw, b }) => {
      const r = await gw.call(MODEL, { name: 'myproxy', action: 'remove', id: 'man1' })
      expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(String(r.json.text), `手动删的不该提重新出现，实得 ${JSON.stringify(r.json.text)}`).not.toContain('重新出现')
      expect(entry(b).meta.manualModels, 'manualModels 里应没了').not.toContain('man1')
    })
  })

  test('删完没有模型了：加「没有模型了，暂时不会出现在可选模型里」', async () => {
    await withModels({ handler: openaiOk(IDS), before: (b, fm) => {
      seedProviders(b.root, [{ name: 'solo', baseURL: `${fm.url}/v1`, models: ['only1'] }])
      seedCreds(b.root, { [defaultKeyEnv('solo')]: fm.expectKey })
    } }, async ({ gw }) => {
      const r = await gw.call(MODEL, { name: 'solo', action: 'remove', id: 'only1' })
      expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(String(r.json.text), `应提醒没模型了，实得 ${JSON.stringify(r.json.text)}`).toContain('「solo」没有模型了，暂时不会出现在可选模型里。')
    })
  })

  test('删本 bot 正在用的模型：加一句并清掉覆盖', async () => {
    await withModels({ handler: openaiOk(IDS), before: seedCustom() }, async ({ tg, gw, b }) => {
      tg.pushText(OWNER, '/model myproxy/m2')
      await waitSentText(tg, OWNER, '已换成 myproxy / m2', 0, 20_000)
      const r = await gw.call(MODEL, { name: 'myproxy', action: 'remove', id: 'm2' })
      expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(String(r.json.text), `text 应加一句换回配置文件里的模型，实得 ${JSON.stringify(r.json.text)}`).toContain('这个 bot 正在用它，已换回配置文件里的模型')
      const o = override(b)
      expect(o === null || !o.provider, `覆盖应被清掉，实得 ${JSON.stringify(o)}`).toBe(true)
    })
  })

  test('删不存在的模型：404 not_found，text 说已经不在了', async () => {
    await withModels({ handler: openaiOk(IDS), before: seedCustom() }, async ({ gw }) => {
      const r = await gw.call(MODEL, { name: 'myproxy', action: 'remove', id: 'nosuch' })
      expect(r.status, `应 404，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(404)
      expect(r.json, `error 应是 not_found，实得 ${JSON.stringify(r.json)}`).toMatchObject({ ok: false, error: 'not_found' })
      expect(String(r.json.text), 'text 应说已经不在了').toContain('已经不在了')
    })
  })
})

describe('POST /v1/provider/model 的 set_context', () => {
  test('改成 64000：200，text 同源，落盘上下文与 guessed 都更新', async () => {
    await withModels({ handler: openaiOk(IDS), before: seedCustom() }, async ({ gw, b }) => {
      expect(entry(b).meta.guessedContext, '前置条件 m1 的上下文是猜的').toContain('m1')
      const r = await gw.call(MODEL, { name: 'myproxy', action: 'set_context', id: 'm1', contextWindow: 64_000 })
      expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(r.json.text, `text 应是同源三条之一，实得 ${JSON.stringify(r.json.text)}`).toBe('已把 m1 的上下文长度改成 64000。几秒后生效。')
      const m = entry(b).route.models.find((x: any) => x.id === 'm1')
      expect(m.contextWindow, '落盘上下文应改成 64000').toBe(64_000)
      expect(entry(b).meta.guessedContext, '改过的应移出 guessed').not.toContain('m1')
    })
  })

  test('set_context 不给 contextWindow：400 bad_context', async () => {
    await withModels({ handler: openaiOk(IDS), before: seedCustom() }, async ({ gw }) => {
      const r = await gw.call(MODEL, { name: 'myproxy', action: 'set_context', id: 'm1' })
      expect(r.status, `应 400，实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(400)
      expect(r.json, `error 应是 bad_context，实得 ${JSON.stringify(r.json)}`).toMatchObject({ ok: false, error: 'bad_context' })
      expect(String(r.json.text), 'text 应说明范围').toContain('1024 到 100000000')
    })
  })
})

describe('POST /v1/provider/model 的校验', () => {
  for (const action of ['add2', '', 123, undefined] as const) {
    test(`action 不是三个之一（${JSON.stringify(action)}）：400 bad_action`, async () => {
      await withModels({ handler: openaiOk(IDS), before: seedCustom() }, async ({ gw }) => {
        const r = await gw.call(MODEL, body({ action }))
        expect(r.status, `应 400，实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(400)
        expect(r.json, `error 应是 bad_action，实得 ${JSON.stringify(r.json)}`).toMatchObject({ ok: false, error: 'bad_action' })
        expect(String(r.json.text), 'text 应列出三个动作').toContain('add、remove、set_context')
      })
    })
  }

  const BAD_IDS: [string, string][] = [
    ['', '空串'],
    ['a b', '含空格'],
    ['a\tb', '含制表符'],
    ['⟦x⟧', '含 ⟦⟧'],
    ['a'.repeat(201), '超 200 字符'],
  ]
  for (const [id, why] of BAD_IDS) {
    test(`id 不合法（${why}）：400 bad_id`, async () => {
      await withModels({ handler: openaiOk(IDS), before: seedCustom() }, async ({ gw }) => {
        const r = await gw.call(MODEL, body({ id }))
        expect(r.status, `应 400，实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(400)
        expect(r.json, `error 应是 bad_id，实得 ${JSON.stringify(r.json)}`).toMatchObject({ ok: false, error: 'bad_id' })
      })
    })
  }

  test('id 非字符串：400 bad_id', async () => {
    await withModels({ handler: openaiOk(IDS), before: seedCustom() }, async ({ gw }) => {
      const r = await gw.call(MODEL, body({ id: 123 }))
      expect(r.status, `应 400，实得 ${r.status}`).toBe(400)
      expect(r.json.error, `error 应是 bad_id，实得 ${JSON.stringify(r.json.error)}`).toBe('bad_id')
    })
  })

  test('id 边界：1 个字符与 200 个字符都可用', async () => {
    await withModels({ handler: openaiOk(IDS), before: seedCustom() }, async ({ gw }) => {
      const one = await gw.call(MODEL, body({ id: 'a' }))
      expect(one.status, `1 字符应可用，实得 ${one.status}，正文 ${one.text.slice(0, 200)}`).toBe(200)
      const max = await gw.call(MODEL, body({ id: 'b'.repeat(200) }))
      expect(max.status, `200 字符应可用，实得 ${max.status}，正文 ${max.text.slice(0, 200)}`).toBe(200)
    })
  })

  const BAD_CTX = [1023, 100_000_001, 0, -5, 1.5, '128000', null] as const
  for (const cw of BAD_CTX) {
    test(`add 给了不合规的 contextWindow（${JSON.stringify(cw)}）：400 bad_context`, async () => {
      await withModels({ handler: openaiOk(IDS), before: seedCustom() }, async ({ gw }) => {
        const r = await gw.call(MODEL, body({ contextWindow: cw }))
        expect(r.status, `应 400，实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(400)
        expect(r.json, `error 应是 bad_context，实得 ${JSON.stringify(r.json)}`).toMatchObject({ ok: false, error: 'bad_context' })
      })
    })
  }

  test('name 不合规则：400 bad_name', async () => {
    await withModels({ handler: openaiOk(IDS), before: seedCustom() }, async ({ gw }) => {
      const r = await gw.call(MODEL, body({ name: 'bad.name' }))
      expect(r.status, `应 400，实得 ${r.status}`).toBe(400)
      expect(r.json.error, `error 应是 bad_name，实得 ${JSON.stringify(r.json.error)}`).toBe('bad_name')
    })
  })

  test('找不到这个供应商：404 not_found，text 说没有这个供应商', async () => {
    await withModels({ handler: openaiOk(IDS), before: seedCustom() }, async ({ gw }) => {
      const r = await gw.call(MODEL, body({ name: 'nosuch' }))
      expect(r.status, `应 404，实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(404)
      expect(r.json.error, `error 应是 not_found，实得 ${JSON.stringify(r.json.error)}`).toBe('not_found')
      expect(String(r.json.text), 'text 应说没有这个供应商').toContain('没有')
      expect(String(r.json.text), 'text 应带上名字').toContain('nosuch')
    })
  })

  test('名字是不合格条目：409 disabled，text 说不能编辑', async () => {
    await withModels({ handler: openaiOk(IDS), before: b => {
      seedProviders(b.root, [{ name: 'broken', api: 'grpc' as any, baseURL: 'https://x.example.com/v1', models: ['x1'] }])
    } }, async ({ gw }) => {
      const r = await gw.call(MODEL, body({ name: 'broken' }))
      expect(r.status, `应 409，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(409)
      expect(r.json.error, `error 应是 disabled，实得 ${JSON.stringify(r.json.error)}`).toBe('disabled')
      expect(String(r.json.text), 'text 应说不能编辑').toContain('不能编辑')
    })
  })

})

describe('POST /v1/provider/model 的错误码', () => {
  test('拿不到锁：409 busy', async () => {
    await withModels({ gw: { provider_lock_wait_ms: 300 }, handler: openaiOk(IDS), before: b => {
      seedProviders(b.root, [{ name: 'myproxy', baseURL: 'https://x.example.com/v1', models: IDS }])
      writeFileSync(lockPath(b.root), JSON.stringify({ pid: process.pid, at: Date.now() }))
    } }, async ({ gw }) => {
      const r = await gw.call(MODEL, body())
      expect(r.status, `锁被占应 409，实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(409)
      expect(r.json.error, `error 应是 busy，实得 ${JSON.stringify(r.json.error)}`).toBe('busy')
    })
  })

  test('providers.json 读不了：503 providers_unreadable', async () => {
    await withModels({ handler: openaiOk(IDS), before: b => writeFileSync(providersPath(b.root), '[[[') }, async ({ gw }) => {
      const r = await gw.call(MODEL, body())
      expect(r.status, `应 503，实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(503)
      expect(r.json.error, `error 应是 providers_unreadable，实得 ${JSON.stringify(r.json.error)}`).toBe('providers_unreadable')
    })
  })

  test('带 Origin 头：403；没带口令：401；Content-Type 不对：415；数组体：400 invalid json', async () => {
    await withBot({ before: b => seedProviders(b.root, []) }, async ({ gw }) => {
      const t = gw.apiToken()
      const cases: [string, RequestInit, number][] = [
        ['Origin 头', { method: 'POST', headers: { authorization: `Bearer ${t}`, origin: 'http://evil.example', 'content-type': 'application/json' }, body: '{}' }, 403],
        ['没带口令', { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' }, 401],
        ['Content-Type 不对', { method: 'POST', headers: { authorization: `Bearer ${t}`, 'content-type': 'text/plain' }, body: '{}' }, 415],
        ['数组体', { method: 'POST', headers: { authorization: `Bearer ${t}`, 'content-type': 'application/json' }, body: '[1]' }, 400],
      ]
      for (const [what, init, want] of cases) {
        const r = await gw.api(MODEL, init)
        expect(r.status, `${what} 应 ${want}，实得 ${r.status}`).toBe(want)
      }
    })
  })
})
