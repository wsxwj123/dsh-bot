// 验收：共用文件 providers.json 的读校验、发现与重启、多 bot 共用与并发写、配置项范围（INTERFACE 3.1、3.2-1/2、3.13.4）
import { describe, expect, test } from 'bun:test'
import { existsSync, rmSync, writeFileSync } from 'fs'
import { FakeTelegram } from '../fakes/fake-telegram'
import { OWNER, Gateway, evs, lifecycle, warmUp, lockPath, makeBot, patchRoutes, providerAdd, readCreds, readProviders, providersPath, seedCreds, seedProviders, sleep, tmpLeftovers, until, waitEvent, waitSentText, withBot, withTwo, type SeedProvider } from './_acc'
import { FakeModels, openaiOk } from './fake-models'

const okRoute = (o: Partial<SeedProvider> = {}): SeedProvider => ({ name: 'goodone', baseURL: 'https://g.example.com/v1', models: ['g1'], ...o })
const call = async (gw: Gateway) => (await gw.call('/v1/model')).json

describe('读文件时也校验：不合格的供应商不启用、不交给 dsh', () => {
  const BAD: [string, SeedProvider][] = [
    ['名字不合规（含空格）', { name: 'bad name', baseURL: 'https://b.example.com/v1', models: ['x'] }],
    ['接口格式不认识', { name: 'badapi', api: 'grpc' as any, baseURL: 'https://b.example.com/v1', models: ['x'] }],
    ['地址是非本机的 http://', { name: 'badurl', baseURL: 'http://example.com/v1', models: ['x'] }],
    ['地址带用户名密码', { name: 'baduser', baseURL: 'https://u:p@example.com/v1', models: ['x'] }],
    ['apiKeyEnv 是内置的 DEEPSEEK_API_KEY', { name: 'badkey1', baseURL: 'https://b.example.com/v1', apiKeyEnv: 'DEEPSEEK_API_KEY', models: ['x'] }],
    ['apiKeyEnv 不是 PROVIDER_…_KEY 形状', { name: 'badkey2', baseURL: 'https://b.example.com/v1', apiKeyEnv: 'MY_SECRET', models: ['x'] }],
  ]
  for (const [what, p] of BAD) {
    test(`${what}：不交给 dsh，日志 providers.entry_skipped，GET /v1/model 里 enabled=false`, async () => {
      await withBot({ before: b => seedProviders(b.root, [okRoute(), p]) }, async ({ tg, b, gw }) => {
        await warmUp(tg)
        await until(() => patchRoutes(b).goodone, '合格的那家照常交给 dsh', 20_000)
        expect(patchRoutes(b)[p.name]).toBeUndefined()
        await waitEvent(b, 'providers.entry_skipped', e => e.name === p.name)
        const item = (await call(gw)).providers.find((x: any) => x.name === p.name)
        expect(item?.enabled).toBe(false)
      })
    })
  }

  test('apiKeyEnv 与本 bot 配置文件路由的键名相同：不启用', async () => {
    const routes = { myroute: { api: 'openai-completions', baseURL: 'https://r.example.com/v1', apiKeyEnv: 'PROVIDER_STEAL_KEY', models: [{ id: 'r1', contextWindow: 65536 }] } }
    await withBot({ brain: { routes }, before: b => seedProviders(b.root, [okRoute(), { name: 'steal', baseURL: 'https://s.example.com/v1', apiKeyEnv: 'PROVIDER_STEAL_KEY', models: ['s1'] }]) }, async ({ tg, b }) => {
      await warmUp(tg)
      await until(() => patchRoutes(b).goodone, '合格的那家照常交给 dsh', 20_000)
      expect(patchRoutes(b).steal).toBeUndefined()
      await waitEvent(b, 'providers.entry_skipped', e => e.name === 'steal')
    })
  })

  test('单个模型条目不合格（id 含空格）：只丢这个模型，供应商照常启用', async () => {
    await withBot({ before: b => seedProviders(b.root, [{ name: 'mixed', baseURL: 'https://m.example.com/v1', models: ['ok-1', 'has space', 'ok-2'] }]) }, async ({ tg, b }) => {
      await warmUp(tg)
      const r = await until(() => patchRoutes(b).mixed, '交给 dsh', 20_000)
      expect(r.models.map((m: any) => m.id)).toEqual(['ok-1', 'ok-2'])
    })
  })

  test('openai-responses 格式读取时认：交给 dsh，列表写「OpenAI Responses 格式」', async () => {
    await withBot({ before: b => seedProviders(b.root, [{ name: 'resp', api: 'openai-responses', baseURL: 'https://r.example.com/v1', models: ['r1'] }]) }, async ({ tg, b }) => {
      await warmUp(tg)
      expect((await until(() => patchRoutes(b).resp, '交给 dsh', 20_000)).api).toBe('openai-responses')
      const k = tg.sentTo(OWNER).length
      tg.pushText(OWNER, '/provider list')
      const s = await waitSentText(tg, OWNER, '【系统】供应商', k)
      expect(s.text!.split('\n').find(l => l.includes(' resp：'))).toContain('OpenAI Responses 格式')
    })
  })
})

describe('读坏、发现、重启', () => {
  test('启动时 providers.json 读坏：照常启动（当作没有自建），日志 providers.reload_failed', async () => {
    await withBot({ before: b => writeFileSync(providersPath(b.root), '{ not json') }, async ({ tg, b }) => {
      await waitEvent(b, 'providers.reload_failed')
      const k = tg.sentTo(OWNER).length
      tg.pushText(OWNER, '你好')
      await waitSentText(tg, OWNER, '收到：你好', k)
    })
  })

  test('运行中读坏：保留上一次读到的内容（自建供应商仍能切过去）', async () => {
    await withBot({ before: b => { seedProviders(b.root, [okRoute()]); seedCreds(b.root, { PROVIDER_GOODONE_KEY: 'test-key-7good00000000' }) } }, async ({ tg, b }) => {
      await warmUp(tg)
      await until(() => patchRoutes(b).goodone, '交给 dsh', 20_000)
      writeFileSync(providersPath(b.root), '{ broken')
      await waitEvent(b, 'providers.reload_failed')
      const k = tg.sentTo(OWNER).length
      tg.pushText(OWNER, '/model goodone/g1')
      await waitSentText(tg, OWNER, '已换成 goodone / g1', k)
    })
  })

  test('网关运行中写入新供应商：在 config_poll_ms 内发现（providers.reloaded），空闲时重启 dsh，路由出现在补丁层', async () => {
    await withBot({}, async ({ tg, b }) => {
      await warmUp(tg)
      const starts = lifecycle(b).filter(l => l.event === 'start').length
      seedProviders(b.root, [okRoute()])
      await waitEvent(b, 'providers.reloaded', () => true, 5_000)
      await waitEvent(b, 'config.restart_needed', () => true, 5_000)
      await waitEvent(b, 'dsh.restart_idle', () => true, 15_000)
      await until(() => lifecycle(b).filter(l => l.event === 'start').length > starts && patchRoutes(b).goodone, 'dsh 带新路由重启', 20_000)
    })
  })

  test('只改了不交给 dsh 的字段（meta.lastRefresh）：不重启 dsh', async () => {
    await withBot({ before: b => seedProviders(b.root, [okRoute()]) }, async ({ tg, b }) => {
      await warmUp(tg)
      await until(() => patchRoutes(b).goodone, '交给 dsh', 20_000)
      await sleep(1_000)
      const starts = lifecycle(b).filter(l => l.event === 'start').length
      const p = readProviders(b.root)
      p.providers.goodone.meta.lastRefresh = { at: Date.now(), ok: false, count: 0, reason: 'network' }
      const n = evs(b, 'providers.reloaded').length
      writeFileSync(providersPath(b.root), JSON.stringify(p))
      await until(() => evs(b, 'providers.reloaded').length > n, '读到改动')
      await sleep(1_500)
      expect(lifecycle(b).filter(l => l.event === 'start').length).toBe(starts)
    })
  })
})

describe('多个 bot 共用', () => {
  test('bot A 新建的供应商，bot B 几秒内也能看到并切过去', async () => {
    const key = 'test-key-7shared0000000001'
    const fm = new FakeModels(key, openaiOk(['s1', 's2']))
    try {
      await withTwo({}, async (a, b) => {
        await providerAdd(a.tg, `shared ${fm.url}/v1 ${key} openai`)
        await until(async () => (await b.gw.call('/v1/model')).json.providers?.some((p: any) => p.name === 'shared' && p.enabled), 'bot B 看到 shared', 15_000)
        const k = b.tg.sentTo(OWNER).length
        b.tg.pushText(OWNER, '/model shared/s1')
        await waitSentText(b.tg, OWNER, '已换成 shared / s1', k)
      })
    } finally { fm.stop() }
  })

  test('与 bot A 配置文件路由同名的自建：在 A 上被遮住，在 B 上照常启用', async () => {
    const routes = { dup: { api: 'openai-completions', baseURL: 'https://r.example.com/v1', apiKeyEnv: 'DUP_ROUTE_KEY', models: [{ id: 'r1', contextWindow: 65536 }] } }
    await withTwo({ brainA: { provider: 'deepseek-official', model: 'deepseek-flash', reasoning_effort: 'low', routes }, before: root => seedProviders(root, [{ name: 'Dup', baseURL: 'https://d.example.com/v1', models: ['d1'] }]) }, async (a, b) => {
      await warmUp(b.tg)
      await until(() => Object.keys(patchRoutes(b.b)).some(k => k.toLowerCase() === 'dup'), 'B 上启用', 20_000)
      await waitEvent(a.b, 'providers.shadowed')
      const pa = (await a.gw.call('/v1/model')).json.providers.find((p: any) => p.source === 'custom')
      expect(pa.enabled).toBe(false)
    })
  })

  test('两个 bot 同时 /provider add 不同的名字：两个都保存、两行密钥都在、不留锁文件和临时文件', async () => {
    const k1 = 'test-key-7concurrent000001', k2 = 'test-key-7concurrent000002'
    const f1 = new FakeModels(k1, openaiOk(['a1'])), f2 = new FakeModels(k2, openaiOk(['b1']))
    try {
      await withTwo({}, async (a, b) => {
        await Promise.all([providerAdd(a.tg, `alpha ${f1.url}/v1 ${k1} openai`), providerAdd(b.tg, `beta ${f2.url}/v1 ${k2} openai`)])
        const p = readProviders(a.b.root)
        expect(Object.keys(p.providers).sort()).toEqual(['alpha', 'beta'])
        const c = readCreds(a.b.root)
        expect(c).toContain(`PROVIDER_ALPHA_KEY: "${k1}"`)
        expect(c).toContain(`PROVIDER_BETA_KEY: "${k2}"`)
        expect(existsSync(lockPath(a.b.root))).toBe(false)
        expect(tmpLeftovers(a.b.root)).toEqual([])
      })
    } finally { f1.stop(); f2.stop() }
  })
})

describe('新配置项的范围校验（3.13.4）', () => {
  const BAD: [string, unknown][] = [
    ['wizard_timeout_ms', 999], ['wizard_timeout_ms', 86_400_001], ['late_secret_window_ms', 'abc'], ['late_secret_window_ms', 0],
    ['provider_lock_wait_ms', 99], ['provider_lock_wait_ms', 60_001], ['model_fetch_timeout_ms', 999], ['model_fetch_timeout_ms', 120_001],
    ['provider_key_grace_ms', -1], ['provider_switch_summary_ms', 1.5], ['provider_switch_summary_ms', 600_001],
  ]
  for (const [k, v] of BAD) {
    test(`gateway.${k}=${JSON.stringify(v)}：启动时报配置错误，网关退出`, async () => {
      const tg = new FakeTelegram()
      const b = makeBot(tg, { gw: { [k]: v } })
      try {
        const code = await new Gateway(b).startExpectExit()
        expect(code).not.toBeNull()
        expect(code).not.toBe(0)
      } finally { tg.stop(); rmSync(b.root, { recursive: true, force: true }) }
    })
  }

  for (const [k, v] of [['provider_key_grace_ms', 0], ['wizard_timeout_ms', 86_400_000], ['late_secret_window_ms', 1_000], ['model_fetch_timeout_ms', 120_000]] as const) {
    test(`gateway.${k}=${v}（边界合格值）：正常启动`, async () => {
      await withBot({ gw: { [k]: v } }, async ({ gw }) => {
        expect((await gw.call('/v1/model')).status).toBe(200)
      })
    })
  }
})

