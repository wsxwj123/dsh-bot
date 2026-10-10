// 验收：网关本机接口 GET / POST /v1/effort（INTERFACE-管理台UI 3.5）。
// 契约要点GET 回 {ok,current,choices}，问不到 dsh 或模型不支持时 choices=[]（仍 200）；
// current 是当前覆盖里的档位；POST 成功 200 带文案，档位不支持 409 unsupported，effort 非字符串 400 bad_body；
// 语义同 Telegram「思考强度」按钮（只改不换段，下一条消息起生效）。
// 实现未落地时本文件红（多半 404），那是红基线，不是测试坏了。
import { describe, expect, test } from 'bun:test'
import { DS_KEY, OWNER, chatPrompts, configCalls, seedCreds, seedProviders, waitSentText, withBot, withModels, type BotEnv } from './_acc'
import { openaiOk, type FakeModels } from './fake-models'

const IDS = ['m1']
const EFFORT = '/v1/effort'
// 不带配置档位，让"没有覆盖时 current"这件事没有歧义
const NOEFF = { provider: 'deepseek-official', model: 'deepseek-flash' }
const ds = (b: BotEnv) => seedCreds(b.root, DS_KEY)

const mineEfforts = (efforts: Record<string, unknown> | false) => (b: BotEnv, fm: FakeModels) => {
  seedProviders(b.root, [{ name: 'myproxy', baseURL: `${fm.url}/v1`, models: [{ id: 'm1', contextWindow: 131_072, reasoningEfforts: efforts }] }])
  seedCreds(b.root, { PROVIDER_MYPROXY_KEY: 'test-key-7effort-0000000001' })
}

describe('GET /v1/effort', () => {
  test('内置模型：200，choices 是 off/low/high/max 四项', async () => {
    await withBot({ brain: NOEFF, before: ds }, async ({ gw }) => {
      const r = await gw.call(EFFORT)
      expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(r.json.ok, 'ok 应为 true').toBe(true)
      const choices = [...(r.json.choices ?? [])].sort()
      expect(choices, `choices 应是四项，实得 ${JSON.stringify(r.json.choices)}`).toEqual(['high', 'low', 'max', 'off'])
    })
  })

  test('没有覆盖：current 是 null（brain 不带配置档位）', async () => {
    await withBot({ brain: NOEFF, before: ds }, async ({ gw }) => {
      const r = await gw.call(EFFORT)
      expect(r.status, `应 200，实得 ${r.status}`).toBe(200)
      expect(r.json.current, `没覆盖时 current 应是 null，实得 ${JSON.stringify(r.json.current)}`).toBeNull()
    })
  })

  test('current 只可能是 null 或 choices 里的取值', async () => {
    await withBot({ brain: NOEFF, before: ds }, async ({ gw }) => {
      const r = await gw.call(EFFORT)
      const cur = r.json.current
      const ok = cur === null || (r.json.choices ?? []).includes(cur)
      expect(ok, `current=${JSON.stringify(cur)} 不在 choices=${JSON.stringify(r.json.choices)} 里`).toBe(true)
    })
  })

  test('自建供应商的模型没声明档位：choices 为空数组，仍 200', async () => {
    await withModels({ brain: { provider: 'myproxy', model: 'm1' }, handler: openaiOk(IDS), before: mineEfforts(false) }, async ({ gw }) => {
      const r = await gw.call(EFFORT)
      expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(r.json.choices, `不声明档位应给空数组，实得 ${JSON.stringify(r.json.choices)}`).toEqual([])
    })
  })

  test('自建供应商的模型声明了 low/high：choices 只列这两个', async () => {
    await withModels({ brain: { provider: 'myproxy', model: 'm1' }, handler: openaiOk(IDS), before: mineEfforts({ low: {}, high: {} }) }, async ({ gw }) => {
      const r = await gw.call(EFFORT)
      expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect([...(r.json.choices ?? [])].sort(), `choices 应只列声明的档位，实得 ${JSON.stringify(r.json.choices)}`).toEqual(['high', 'low'])
    })
  })

  test('带 Origin 头：403；没带口令：401', async () => {
    await withBot({ brain: NOEFF, before: ds }, async ({ gw }) => {
      const withOrigin = await gw.api(EFFORT, { headers: { authorization: `Bearer ${gw.apiToken()}`, origin: 'http://evil.example' } })
      expect(withOrigin.status, `带 Origin 应 403，实得 ${withOrigin.status}`).toBe(403)
      const noAuth = await gw.api(EFFORT)
      expect(noAuth.status, `没带口令应 401，实得 ${noAuth.status}`).toBe(401)
    })
  })
})

describe('POST /v1/effort', () => {
  test('设成 low：200，text 说明改了并下一条起生效，GET 的 current 跟着变', async () => {
    await withBot({ brain: NOEFF, before: ds }, async ({ gw }) => {
      const r = await gw.call(EFFORT, { effort: 'low' })
      expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(r.json.ok, 'ok 应为 true').toBe(true)
      expect(String(r.json.text), `text 应说改成「低」，实得 ${JSON.stringify(r.json.text)}`).toContain('已把思考强度改成「低」')
      const g = await gw.call(EFFORT)
      expect(g.json.current, `设完 current 应是 low，实得 ${JSON.stringify(g.json.current)}`).toBe('low')
    })
  })

  test('设成 high 后聊下一句：同一个会话，下一轮用新档位', async () => {
    await withBot({ brain: NOEFF, before: ds }, async ({ tg, gw, b }) => {
      const k0 = tg.sentTo(OWNER).length
      tg.pushText(OWNER, '第一句话')
      await waitSentText(tg, OWNER, '收到：第一句话', k0, 30_000)
      const r = await gw.call(EFFORT, { effort: 'high' })
      expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      const k2 = tg.sentTo(OWNER).length
      tg.pushText(OWNER, '第二句话')
      await waitSentText(tg, OWNER, '收到：第二句话', k2, 30_000)
      const ps = chatPrompts(b)
      const first = ps.find(p => p.text.includes('第一句话'))
      const second = ps.find(p => p.text.includes('第二句话'))
      expect(first && second, `两轮都应交给模型，实得 ${JSON.stringify(ps.map(p => p.text.slice(0, 20)))}`).toBeTruthy()
      expect(second!.sessionId, '改档位不该换新会话').toBe(first!.sessionId)
      expect(configCalls(b).some(c => c.configId === 'reasoning_effort' && c.value === 'high' && c.ok !== false), `下一轮应设上 reasoning_effort=high，实得 ${JSON.stringify(configCalls(b))}`).toBe(true)
    })
  })

  test('档位不在当前模型支持范围：409 unsupported', async () => {
    await withModels({ brain: { provider: 'myproxy', model: 'm1' }, handler: openaiOk(IDS), before: mineEfforts({ low: {}, high: {} }) }, async ({ gw }) => {
      const r = await gw.call(EFFORT, { effort: 'max' })
      expect(r.status, `应 409，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(409)
      expect(r.json, `error 应是 unsupported，实得 ${JSON.stringify(r.json)}`).toMatchObject({ ok: false, error: 'unsupported' })
      expect(String(r.json.text), `text 应说不支持这个档位，实得 ${JSON.stringify(r.json.text)}`).toContain('不支持')
    })
  })

  for (const effort of [123, null, true, undefined] as const) {
    test(`effort 不是字符串（${JSON.stringify(effort)}）：400 bad_body`, async () => {
      await withBot({ brain: NOEFF, before: ds }, async ({ gw }) => {
        const r = await gw.call(EFFORT, { effort })
        expect(r.status, `应 400，实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(400)
        expect(r.json, `error 应是 bad_body，实得 ${JSON.stringify(r.json)}`).toMatchObject({ ok: false, error: 'bad_body' })
        expect(String(r.json.text), `text 应说 effort 必须是字符串，实得 ${JSON.stringify(r.json.text)}`).toContain('effort 必须是字符串')
      })
    })
  }

  test('Content-Type 不是 JSON：415；数组体：400 invalid json', async () => {
    await withBot({ brain: NOEFF, before: ds }, async ({ gw }) => {
      const t = gw.apiToken()
      const badCt = await gw.api(EFFORT, { method: 'POST', headers: { authorization: `Bearer ${t}`, 'content-type': 'text/plain' }, body: '{}' })
      expect(badCt.status, `Content-Type 不对应 415，实得 ${badCt.status}`).toBe(415)
      const arr = await gw.api(EFFORT, { method: 'POST', headers: { authorization: `Bearer ${t}`, 'content-type': 'application/json' }, body: '[1]' })
      expect(arr.status, `数组体应 400，实得 ${arr.status}`).toBe(400)
      const j = await arr.json() as any
      expect(j?.error, `error 应是 invalid json，实得 ${JSON.stringify(j)}`).toBe('invalid json')
    })
  })
})
