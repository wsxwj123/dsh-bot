// 验收：拉模型列表（INTERFACE 3.7）——请求形状、解析规则、各种失败的分类与文案。用 /provider add 驱动。
import { describe, expect, test } from 'bun:test'
import { logEvents, providerAdd, readProviders, waitEvent, withModels } from './_acc'
import { FakeModels, anthropicOk, firstThen, json, openaiBody, openaiOk, sleep } from './fake-models'

const add = (tg: any, fm: FakeModels, key: string, fmt = 'openai', path = '/v1') => providerAdd(tg, `fp ${fm.url}${path} ${key} ${fmt}`)
const models = (b: any) => readProviders(b.root)?.providers?.fp?.route?.models ?? null
const failText = (why: string) => `【系统】已添加供应商「fp」，但没拉到模型：${why}。`

describe('拉列表：解析', () => {
  test('上下文长度按字段优先级取第一个 1024–100000000 的整数，取不到按 131072 并记入 guessedContext', async () => {
    const ids = [
      { id: 'a', max_input_tokens: 500, context_length: 32000 },
      { id: 'b', input_token_limit: 2048 },
      { id: 'c', context_window: 99999999999 },
      { id: 'd', max_model_len: 4096.5 },
      { id: 'e', max_context_length: 100000000 },
      { id: 'f', context_window: 1024 },
    ]
    await withModels({ handler: openaiOk(ids) }, async ({ tg, b, fm, key }) => {
      await add(tg, fm, key)
      expect(models(b)).toEqual([
        { id: 'a', contextWindow: 32000 }, { id: 'b', contextWindow: 2048 }, { id: 'c', contextWindow: 131072 },
        { id: 'd', contextWindow: 131072 }, { id: 'e', contextWindow: 100000000 }, { id: 'f', contextWindow: 1024 },
      ])
      expect([...readProviders(b.root).providers.fp.meta.guessedContext].sort()).toEqual(['c', 'd'])
    })
  })

  test('Anthropic 回包的 max_input_tokens 当上下文长度', async () => {
    await withModels({ handler: anthropicOk([[{ id: 'claude-x', max_input_tokens: 200000 }]]) }, async ({ tg, b, fm, key }) => {
      await add(tg, fm, key, 'anthropic', '')
      expect(models(b)).toEqual([{ id: 'claude-x', contextWindow: 200000 }])
    })
  })

  test('id 去首尾空白、去重保序；含空格/换行/控制字符/超过 200 字的跳过；非 ASCII（聚合商的中文渠道前缀）与 <b> 这类字符照收', async () => {
    const ids = ['good-1', ' padded ', 'bad id', 'bad\nid', 'bad\u0001id', '模型一', 'x'.repeat(201), 'y'.repeat(200), '<b>', 'good-1']
    await withModels({ handler: openaiOk(ids) }, async ({ tg, b, fm, key }) => {
      const { reply } = await add(tg, fm, key)
      expect(models(b).map((m: any) => m.id)).toEqual(['good-1', 'padded', '模型一', 'y'.repeat(200), '<b>'])
      expect(reply.text!).toContain('拉到 5 个模型')
    })
  })

  test('顶层是 models 数组（不是 data）也认', async () => {
    await withModels({ handler: r => (r.authOk ? json({ models: [{ id: 'm1' }, { id: 'm2' }] }) : json({}, 401)) }, async ({ tg, b, fm, key }) => {
      await add(tg, fm, key)
      expect(models(b).map((m: any) => m.id)).toEqual(['m1', 'm2'])
    })
  })

  test('超过 1000 个 id：只取前 1000 个，回复加「（只取了前 1000 个）」', async () => {
    const ids = Array.from({ length: 1100 }, (_, i) => `m-${i}`)
    await withModels({ handler: openaiOk(ids) }, async ({ tg, b, fm, key }) => {
      const { reply } = await add(tg, fm, key)
      expect(models(b).length).toBe(1000)
      expect(models(b)[999].id).toBe('m-999')
      expect(reply.text!).toContain('（只取了前 1000 个）')
    })
  })

  test('Anthropic 分页：has_more 时带 after_id=<last_id> 继续，三页合并', async () => {
    await withModels({ handler: anthropicOk([['a1', 'a2'], ['b1'], ['c1', 'c2']]) }, async ({ tg, b, fm, key }) => {
      const { reply } = await add(tg, fm, key, 'anthropic', '')
      expect(models(b).map((m: any) => m.id)).toEqual(['a1', 'a2', 'b1', 'c1', 'c2'])
      expect(fm.requests.map(r => new URLSearchParams(r.search).get('after_id'))).toEqual([null, 'a2', 'b1'])
      expect(reply.text!).toContain('拉到 5 个模型')
    })
  })

  test('Anthropic 拉满 5 页仍 has_more：只发 5 个请求，回复加「（只取了前 1000 个）」', async () => {
    const pages = Array.from({ length: 7 }, (_, i) => [`p${i}-a`, `p${i}-b`])
    await withModels({ handler: anthropicOk(pages) }, async ({ tg, b, fm, key }) => {
      const { reply } = await add(tg, fm, key, 'anthropic', '')
      expect(fm.requests.length).toBe(5)
      expect(models(b).length).toBe(10)
      expect(reply.text!).toContain('（只取了前 1000 个）')
    })
  })

  test('地址只有域名、GET /models 返回 404：再试 /v1/models 成功，地址补成 <地址>/v1 并说明', async () => {
    const h = (r: any) => (r.path === '/v1/models' && r.authOk ? json(openaiBody(['m1'])) : json({}, 404))
    await withModels({ handler: h }, async ({ tg, b, fm, key }) => {
      const { reply } = await add(tg, fm, key, 'openai', '')
      expect(fm.requests.map(r => r.path)).toEqual(['/models', '/v1/models'])
      expect(readProviders(b.root).providers.fp.route.baseURL).toBe(`${fm.url}/v1`)
      expect(reply.text!).toContain(`地址补成了 ${fm.url}/v1`)
    })
  })

  test('地址只有域名、GET /models 返回 401：同样再试 /v1/models', async () => {
    const h = (r: any) => (r.path === '/v1/models' && r.authOk ? json(openaiBody(['m1'])) : json({}, 401))
    await withModels({ handler: h }, async ({ tg, b, fm, key }) => {
      await add(tg, fm, key, 'openai', '')
      expect(fm.requests.map(r => r.path)).toEqual(['/models', '/v1/models'])
      expect(models(b).map((m: any) => m.id)).toEqual(['m1'])
    })
  })

  test('地址有路径（/api）时 404：不再试 /v1，按 not_found 失败', async () => {
    await withModels({ handler: () => json({}, 404) }, async ({ tg, fm, key }) => {
      const { reply } = await add(tg, fm, key, 'openai', '/api')
      expect(fm.requests.length).toBe(1)
      expect(reply.text!).toContain(failText('接口地址不对（HTTP 404），检查是否多写或少写了 /v1'))
    })
  })
})
describe('拉列表：失败分类（供应商照样建好，模型列表为空）', () => {
  const CASES: [string, number, string][] = [
    ['401', 401, '密钥不对或没有权限（HTTP 401）'],
    ['403', 403, '密钥不对或没有权限（HTTP 403）'],
    ['404', 404, '接口地址不对（HTTP 404），检查是否多写或少写了 /v1'],
    ['429', 429, '对方限流了（HTTP 429），稍后再刷新'],
    ['500', 500, '对方接口出错（HTTP 500）'],
    ['503', 503, '对方接口出错（HTTP 503）'],
    ['418', 418, '对方接口返回 HTTP 418'],
  ]
  for (const [name, status, why] of CASES) {
    test(`对方回 HTTP ${name}：文案「${why}」`, async () => {
      await withModels({ handler: () => json({ error: { message: 'upstream-body-text-xyz' } }, status) }, async ({ tg, b, fm, key }) => {
        const { reply } = await add(tg, fm, key)
        expect(reply.text!).toContain(failText(why))
        expect(reply.text!).not.toContain('upstream-body-text-xyz')
        expect(models(b)).toEqual([])
      })
    })
  }

  test('对方回 302：按 redirect 失败，不跟随跳转（第二个服务收到 0 个请求）', async () => {
    const second = new FakeModels('unused', openaiOk(['should-not-see']))
    try {
      await withModels({ handler: () => new Response(null, { status: 302, headers: { location: `${second.url}/v1/models` } }) }, async ({ tg, b, fm, key }) => {
        const { reply } = await add(tg, fm, key)
        expect(reply.text!).toContain(failText('接口地址发生了跳转（HTTP 302），请填跳转后的最终地址'))
        expect(second.requests.length).toBe(0)
        expect(fm.requests.length).toBe(1)
        expect(models(b)).toEqual([])
      })
    } finally { second.stop() }
  })

  test('回包不是 JSON：bad_response「对方返回的不是模型列表（格式不认识）」', async () => {
    await withModels({ handler: () => new Response('<html>hello</html>', { status: 200, headers: { 'content-type': 'text/html' } }) }, async ({ tg, fm, key }) => {
      const { reply } = await add(tg, fm, key)
      expect(reply.text!).toContain(failText('对方返回的不是模型列表（格式不认识）'))
    })
  })

  test('所有 id 都不合格：bad_response', async () => {
    await withModels({ handler: openaiOk(['has space', 'x'.repeat(300), '']) }, async ({ tg, fm, key }) => {
      const { reply } = await add(tg, fm, key)
      expect(reply.text!).toContain(failText('对方返回的不是模型列表（格式不认识）'))
    })
  })

  test('回包超过 4 MiB：bad_response', async () => {
    const big = JSON.stringify({ object: 'list', data: [{ id: 'm1' }], pad: 'x'.repeat(4 * 1024 * 1024 + 100) })
    await withModels({ handler: () => new Response(big, { status: 200, headers: { 'content-type': 'application/json' } }) }, async ({ tg, fm, key }) => {
      const { reply } = await add(tg, fm, key)
      expect(reply.text!).toContain(failText('对方返回的不是模型列表（格式不认识）'))
    })
  })

  test('成功但 0 个：empty「对方返回的模型列表是空的」', async () => {
    await withModels({ handler: openaiOk([]) }, async ({ tg, fm, key }) => {
      const { reply } = await add(tg, fm, key)
      expect(reply.text!).toContain(failText('对方返回的模型列表是空的'))
    })
  })

  test('慢于 model_fetch_timeout_ms（1 秒）：timeout「接口 1 秒没有响应」', async () => {
    await withModels({ gw: { model_fetch_timeout_ms: 1_000 }, handler: async r => { await sleep(4_000); return openaiOk(['m1'])(r, 1, null as any) } }, async ({ tg, fm, key }) => {
      const { reply } = await add(tg, fm, key)
      expect(reply.text!).toContain(failText('接口 1 秒没有响应'))
    })
  })

  test('连不上（端口没人监听）：network「连不上接口地址（网络不通、地址不对或 HTTPS 证书有问题）」', async () => {
    const dead = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch: () => new Response('x') })
    const port = dead.port
    dead.stop(true)
    await withModels({ handler: openaiOk(['m1']) }, async ({ tg, key }) => {
      const { reply } = await providerAdd(tg, `fp http://127.0.0.1:${port}/v1 ${key} openai`)
      expect(reply.text!).toContain(failText('连不上接口地址（网络不通、地址不对或 HTTPS 证书有问题）'))
    })
  })

  test('Anthropic 多页、第 2 页失败：整次失败，一个模型都不存', async () => {
    const h = firstThen(1, anthropicOk([['a1', 'a2'], ['b1']]), () => json({}, 500))
    await withModels({ handler: h }, async ({ tg, b, fm, key }) => {
      const { reply } = await add(tg, fm, key, 'anthropic', '')
      expect(reply.text!).toContain(failText('对方接口出错（HTTP 500）'))
      expect(models(b)).toEqual([])
    })
  })

  test('失败日志 provider.models_failed 带 reason 与 status（401 → unauthorized）', async () => {
    await withModels({ handler: () => json({}, 401) }, async ({ tg, b, fm, key }) => {
      await add(tg, fm, key)
      await waitEvent(b, 'provider.models_failed', e => e.name === 'fp' && e.reason === 'unauthorized' && Number(e.status) === 401)
    })
  })

  test('拉列表失败时的文案和日志里都没有接口地址', async () => {
    await withModels({ handler: () => json({}, 500) }, async ({ tg, b, fm, key }) => {
      const { reply } = await add(tg, fm, key)
      expect(reply.text!).not.toContain(`127.0.0.1:${fm.server.port}`)
      await waitEvent(b, 'provider.models_failed')
      expect(JSON.stringify(logEvents(b).filter(e => String(e.event).startsWith('provider.')))).not.toContain(`127.0.0.1:${fm.server.port}`)
    })
  })
})

