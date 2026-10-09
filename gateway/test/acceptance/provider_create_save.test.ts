// 验收：新建第 4 步（密钥）——删消息、拉模型、写 providers.json 与凭据文件、结果文案（INTERFACE 3.1、3.4.2、3.7、3.11）
import { describe, expect, test } from 'bun:test'
import { readFileSync, writeFileSync } from 'fs'
import { OWNER, IS_WIN, credPath, dshStarts, fakeKey, fileMode, labels, patchRoutes, patchSeen, warmUp, providersPath, readCreds, readProviders, seedCreds, seedProviders, toKeyStep, toNameStep, until, waitEvent, waitSentText, withModels, wizardCreate } from './_acc'
import { anthropicOk, json, openaiOk } from './fake-models'

const IDS = ['gpt-a', 'gpt-b', 'gpt-c']
const DELETED_NOTE = '（你发的密钥消息已删除。）'
const NOT_DELETED_NOTE = '（你发的密钥消息没能删除，请手动删掉它。）'
const NOT_KEY = '【系统】这条不像密钥（要求 8–512 个可见英文字符、中间不能有空格），已删除。请重新输入密钥：'

describe('新建：收到合格密钥', () => {
  test('那条密钥消息被删除', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, fm, key }) => {
      const { keyMsg } = await wizardCreate(tg, { name: 'myproxy', fmt: 'openai', url: `${fm.url}/v1`, key })
      expect(tg.isDeleted(OWNER, keyMsg)).toBe(true)
    })
  })

  test('拉到模型：新消息「已添加供应商「myproxy」，拉到 3 个模型。」并附已删除说明', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, fm, key }) => {
      const { result } = await wizardCreate(tg, { name: 'myproxy', fmt: 'openai', url: `${fm.url}/v1`, key })
      expect(result.text!).toContain('【系统】已添加供应商「myproxy」，拉到 3 个模型。')
      expect(result.text!).toContain(DELETED_NOTE)
    })
  })

  test('拉到模型：结果消息按钮是「切过去」「关闭」', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, fm, key }) => {
      const { result } = await wizardCreate(tg, { name: 'myproxy', fmt: 'openai', url: `${fm.url}/v1`, key })
      expect(labels(tg, result.messageId)).toEqual(['切过去', '关闭'])
    })
  })

  test('OpenAI 格式拉列表：只发一个 GET <地址>/models，Bearer 密钥正确，Accept: application/json', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, fm, key }) => {
      await wizardCreate(tg, { name: 'myproxy', fmt: 'openai', url: `${fm.url}/v1`, key })
      expect(fm.requests.map(r => `${r.method} ${r.path}`)).toEqual(['GET /v1/models'])
      expect(fm.requests[0]!.authKind).toBe('bearer')
      expect(fm.requests[0]!.authOk).toBe(true)
      expect(fm.requests[0]!.accept).toBe('application/json')
    })
  })

  test('Anthropic 格式拉列表：GET <根>/v1/models?limit=1000，x-api-key 正确，anthropic-version 2023-06-01', async () => {
    await withModels({ handler: anthropicOk([['claude-a', 'claude-b']]) }, async ({ tg, fm, key }) => {
      const { result } = await wizardCreate(tg, { name: 'anth', fmt: 'anthropic', url: fm.url, key })
      expect(result.text!).toContain('拉到 2 个模型')
      const r = fm.requests[0]!
      expect(r.path).toBe('/v1/models')
      expect(new URLSearchParams(r.search).get('limit')).toBe('1000')
      expect(r.authKind).toBe('x-api-key')
      expect(r.authOk).toBe(true)
      expect(r.anthropicVersion).toBe('2023-06-01')
    })
  })

  test('Anthropic 格式填了以 /v1 结尾的地址：存储时去掉 /v1，并在回复里注明', async () => {
    await withModels({ handler: anthropicOk([['claude-a']]) }, async ({ tg, b, fm, key }) => {
      await wizardCreate(tg, { name: 'anth', fmt: 'anthropic', url: `${fm.url}/v1`, key })
      expect(readProviders(b.root).providers.anth.route.baseURL).toBe(fm.url)
      const all = tg.sentTo(OWNER).map(s => s.text ?? '').concat(tg.edits.map(e => e.text ?? '')).join('\n')
      expect(all).toContain('已去掉地址末尾的 /v1（Anthropic 格式会自动加）')
    })
  })

  test('地址末尾的 / 被去掉后存储', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, b, fm, key }) => {
      await wizardCreate(tg, { name: 'myproxy', fmt: 'openai', url: `${fm.url}/v1/`, key })
      expect(readProviders(b.root).providers.myproxy.route.baseURL).toBe(`${fm.url}/v1`)
    })
  })

  test('providers.json：route 写入格式、地址、默认键名、拉到的模型（上下文未知按 131072 并记入 guessedContext）', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, b, fm, key }) => {
      await wizardCreate(tg, { name: 'myproxy', fmt: 'openai', url: `${fm.url}/v1`, key })
      const p = readProviders(b.root)
      expect(p.version).toBe(1)
      const e = p.providers.myproxy
      expect(e.route.api).toBe('openai-completions')
      expect(e.route.apiKeyEnv).toBe('PROVIDER_MYPROXY_KEY')
      expect(e.route.models).toEqual(IDS.map(id => ({ id, contextWindow: 131072 })))
      expect([...e.meta.guessedContext].sort()).toEqual([...IDS].sort())
      expect(e.meta.keyRev).toBe(1)
      expect(typeof e.meta.epoch).toBe('number')
    })
  })

  test('名字带大写和下划线（My_Proxy）：键名 PROVIDER_MY_PROXY_KEY', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, b, fm, key }) => {
      await wizardCreate(tg, { name: 'My_Proxy', fmt: 'openai', url: `${fm.url}/v1`, key })
      const entries = Object.entries(readProviders(b.root).providers) as [string, any][]
      expect(entries.length).toBe(1)
      expect(entries[0]![0].toLowerCase().replace(/_/g, '-')).toBe('my-proxy')
      expect(entries[0]![1].route.apiKeyEnv).toBe('PROVIDER_MY_PROXY_KEY')
    })
  })

  test.skipIf(IS_WIN)('providers.json 与 credentials.yaml 权限都是 600', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, b, fm, key }) => {
      await wizardCreate(tg, { name: 'myproxy', fmt: 'openai', url: `${fm.url}/v1`, key })
      expect(fileMode(providersPath(b.root))).toBe(0o600)
      expect(fileMode(credPath(b.root))).toBe(0o600)
    })
  })

  test('凭据文件不存在时新建，内容正好是 version / refs / 一行密钥', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, b, fm, key }) => {
      await wizardCreate(tg, { name: 'myproxy', fmt: 'openai', url: `${fm.url}/v1`, key })
      expect(readCreds(b.root)).toBe(`version: 1\nrefs:\n  PROVIDER_MYPROXY_KEY: "${key}"\n`)
    })
  })

  test('已有凭据文件：注释和其它键逐字节不变，只多出一行新密钥', async () => {
    const before = '# 我的凭据\nversion: 1\nrefs:\n  DEEPSEEK_API_KEY: "sk-test-deepseek-000000"\n  VOICE_BRIDGE_TOKEN: "vb-test-000000"\n  NOVELAI_BEARER_TOKEN: "nai-test-000000"\n'
    await withModels({ handler: openaiOk(IDS), before: b => writeFileSync(credPath(b.root), before) }, async ({ tg, b, fm, key }) => {
      await wizardCreate(tg, { name: 'myproxy', fmt: 'openai', url: `${fm.url}/v1`, key })
      const line = `  PROVIDER_MYPROXY_KEY: "${key}"\n`
      const c = readCreds(b.root)
      expect(c.split(line).length - 1).toBe(1)
      expect(c.replace(line, '')).toBe(before)
    })
  })

  test('refs 块用 4 个空格缩进：新行也用 4 个空格', async () => {
    const before = 'version: 1\nrefs:\n    DEEPSEEK_API_KEY: "sk-test-deepseek-000000"\n'
    await withModels({ handler: openaiOk(IDS), before: b => writeFileSync(credPath(b.root), before) }, async ({ tg, b, fm, key }) => {
      await wizardCreate(tg, { name: 'myproxy', fmt: 'openai', url: `${fm.url}/v1`, key })
      const line = `    PROVIDER_MYPROXY_KEY: "${key}"\n`
      const c = readCreds(b.root)
      expect(c.split(line).length - 1).toBe(1)
      expect(c.replace(line, '')).toBe(before)
    })
  })

  test('密钥里有双引号和反斜杠：按 JSON 字符串转义写进凭据文件', async () => {
    const key = `test-key-7q"w\\e${fakeKey().slice(-8)}`
    await withModels({ key, handler: openaiOk(IDS) }, async ({ tg, b, fm }) => {
      await wizardCreate(tg, { name: 'myproxy', fmt: 'openai', url: `${fm.url}/v1`, key })
      expect(readCreds(b.root)).toContain(`  PROVIDER_MYPROXY_KEY: ${JSON.stringify(key)}\n`)
    })
  })

  test('凭据文件里已有同名键（不属于任何自建）：新键名加 _2，原来那行不变', async () => {
    await withModels({ handler: openaiOk(IDS), before: b => seedCreds(b.root, { PROVIDER_MYPROXY_KEY: 'test-old-value-1234' }) }, async ({ tg, b, fm, key }) => {
      await wizardCreate(tg, { name: 'myproxy', fmt: 'openai', url: `${fm.url}/v1`, key })
      expect(readProviders(b.root).providers.myproxy.route.apiKeyEnv).toBe('PROVIDER_MYPROXY_KEY_2')
      const c = readCreds(b.root)
      expect(c).toContain('  PROVIDER_MYPROXY_KEY: "test-old-value-1234"\n')
      expect(c).toContain(`  PROVIDER_MYPROXY_KEY_2: "${key}"\n`)
    })
  })

  test('日志：provider.created {name, api}、wizard.secret_deleted ok=true、provider.models_fetched count=3', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, b, fm, key }) => {
      await wizardCreate(tg, { name: 'myproxy', fmt: 'openai', url: `${fm.url}/v1`, key })
      await waitEvent(b, 'provider.created', e => e.name === 'myproxy' && e.api === 'openai-completions')
      await waitEvent(b, 'wizard.secret_deleted', e => e.ok === true)
      await waitEvent(b, 'provider.models_fetched', e => e.name === 'myproxy' && e.count === 3)
    })
  })
})
describe('新建：失败与异常路径', () => {
  test('没拉到模型（401）：照样建好，回「但没拉到模型：密钥不对或没有权限（HTTP 401）」，按钮「刷新模型」「关闭」', async () => {
    await withModels({ handler: () => json({ error: 'bad key' }, 401) }, async ({ tg, b, fm, key }) => {
      const { result } = await wizardCreate(tg, { name: 'myproxy', fmt: 'openai', url: `${fm.url}/v1`, key })
      expect(result.text!).toContain('【系统】已添加供应商「myproxy」，但没拉到模型：密钥不对或没有权限（HTTP 401）。之后可以在 /provider 里点「刷新模型」，或用 /model →「管理模型」手动加。')
      expect(labels(tg, result.messageId)).toEqual(['刷新模型', '关闭'])
      expect(readProviders(b.root).providers.myproxy.route.models).toEqual([])
      expect(readCreds(b.root)).toContain(`PROVIDER_MYPROXY_KEY: "${key}"`)
    })
  })

  for (const [what, bad] of [['太短（7 个字符）', 'abc1234'], ['中间有空格', 'abcd 12345678'], ['以 / 开头', '/slashkey12345678'], ['超过 512 个字符', 'k1'.repeat(257)], ['含中文', '密钥abcd12345678']] as const) {
    test(`密钥${what}：删除这条消息，回「这条不像密钥…」并继续等密钥`, async () => {
      await withModels({ handler: openaiOk(IDS) }, async ({ tg, fm }) => {
        await toKeyStep(tg, 'myproxy', 'openai', `${fm.url}/v1`)
        const k = tg.sentTo(OWNER).length
        const mid = tg.pushText(OWNER, bad)
        await waitSentText(tg, OWNER, NOT_KEY, k)
        expect(tg.isDeleted(OWNER, mid)).toBe(true)
        expect(fm.requests.length).toBe(0)
      })
    })
  }

  test('密钥这一步发来图片：也先删除，回「这条不像密钥…」', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, fm }) => {
      await toKeyStep(tg, 'myproxy', 'openai', `${fm.url}/v1`)
      const k = tg.sentTo(OWNER).length
      const mid = tg.pushPhoto(OWNER)
      await waitSentText(tg, OWNER, NOT_KEY, k)
      expect(tg.isDeleted(OWNER, mid)).toBe(true)
    })
  })

  test('密钥消息删不掉（太旧）：结果附「没能删除，请手动删掉它」，日志 wizard.secret_deleted ok=false', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, b, fm, key }) => {
      await toKeyStep(tg, 'myproxy', 'openai', `${fm.url}/v1`)
      const k = tg.sentTo(OWNER).length
      const mid = tg.pushText(OWNER, key)
      tg.markTooOld(OWNER, mid)
      const r = await waitSentText(tg, OWNER, '已添加供应商「myproxy」', k, 20_000)
      expect(r.text!).toContain(NOT_DELETED_NOTE)
      await waitEvent(b, 'wizard.secret_deleted', e => e.ok === false)
    })
  })

  test('deleteMessage 先遇到 429：按 retry_after 重试后删掉，结果附已删除说明', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, fm, key }) => {
      tg.faults.push({ method: 'deleteMessage', status: 429, description: 'Too Many Requests: retry after 1', retryAfter: 1, times: 1 })
      const { result, keyMsg } = await wizardCreate(tg, { name: 'myproxy', fmt: 'openai', url: `${fm.url}/v1`, key })
      expect(tg.isDeleted(OWNER, keyMsg)).toBe(true)
      expect(result.text!).toContain(DELETED_NOTE)
    })
  })

  test('deleteMessage 一直 429：最多再试 2 次（共 3 次），然后说明没能删除', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, fm, key }) => {
      tg.faults.push({ method: 'deleteMessage', status: 429, description: 'Too Many Requests: retry after 1', retryAfter: 1, times: 10 })
      const { result } = await wizardCreate(tg, { name: 'myproxy', fmt: 'openai', url: `${fm.url}/v1`, key })
      expect(result.text!).toContain(NOT_DELETED_NOTE)
      expect(tg.calls.filter(c => c.method === 'deleteMessage').length).toBe(3)
    })
  })

  for (const [why, refs] of [['不是块状写法', 'refs: {DEEPSEEK_API_KEY: "sk-test-1"}\n'], ['缩进不一致', 'refs:\n  DEEPSEEK_API_KEY: "sk-test-1"\n    VOICE_BRIDGE_TOKEN: "vb-test-1"\n']] as const) {
    test(`凭据文件 refs ${why}：回「已保存，但密钥没写进去（…${why}…）」，凭据文件逐字节不变`, async () => {
      const before = `version: 1\n${refs}`
      await withModels({ handler: openaiOk(IDS), before: b => writeFileSync(credPath(b.root), before) }, async ({ tg, b, fm, key }) => {
        const { result } = await wizardCreate(tg, { name: 'myproxy', fmt: 'openai', url: `${fm.url}/v1`, key })
        expect(result.text!).toContain('【系统】供应商「myproxy」已保存，但密钥没写进去（')
        expect(result.text!).toContain(`凭据文件的 refs 写法不常见（${why}），请手动编辑凭据文件`)
        expect(readFileSync(credPath(b.root), 'utf8')).toBe(before)
        expect(readProviders(b.root).providers.myproxy).toBeTruthy()
        await waitEvent(b, 'provider.credentials_unsupported')
      })
    })
  }

  test('providers.json 格式坏了时点「新建」：回「共用供应商文件读不了（格式坏了），先修好 …」，文件不被改动', async () => {
    await withModels({ handler: openaiOk(IDS), before: b => writeFileSync(providersPath(b.root), '{ broken') }, async ({ tg, b }) => {
      tg.pushText(OWNER, '/provider')
      const m = await until(() => tg.lastMenu(OWNER)?.buttons.some(x => x.text === '新建') ? tg.lastMenu(OWNER) : null, '主菜单')
      tg.clickButton(OWNER, OWNER, m.messageId, '新建')
      await until(() => [...tg.sentTo(OWNER).map(s => s.text ?? ''), ...tg.edits.map(e => e.text ?? '')].some(t => t.includes('共用供应商文件读不了（格式坏了），先修好 ')), '读不了的说明')
      expect(readFileSync(providersPath(b.root), 'utf8')).toBe('{ broken')
    })
  })

  test('与已有自建同名并选「更新它」走完：回「已更新供应商「my-proxy」，拉到 N 个模型。」', async () => {
    await withModels({ handler: openaiOk(IDS), before: b => { seedCreds(b.root, { PROVIDER_MY_PROXY_KEY: 'test-old-key-12345678' }); seedProviders(b.root, [{ name: 'my-proxy', baseURL: 'https://old.example.com/v1', models: ['old-m'] }]) } }, async ({ tg, fm, key }) => {
      await toNameStep(tg)
      tg.pushText(OWNER, 'my-proxy')
      const c = await until(() => tg.lastMenu(OWNER)?.text.includes('已有自建供应商') ? tg.lastMenu(OWNER) : null, '同名确认')
      tg.clickButton(OWNER, OWNER, c.messageId, '更新它')
      const f = await until(() => tg.lastMenu(OWNER)?.text.includes('（第 2/4 步）') ? tg.lastMenu(OWNER) : null, '第 2 步')
      tg.clickButton(OWNER, OWNER, f.messageId, 'OpenAI 格式')
      await until(() => tg.textOf(OWNER, f.messageId)?.includes('（第 3/4 步）'), '第 3 步')
      tg.pushText(OWNER, `${fm.url}/v1`)
      await until(() => tg.lastMenu(OWNER)?.text.includes('（第 4/4 步）'), '第 4 步')
      const k = tg.sentTo(OWNER).length
      tg.pushText(OWNER, key)
      await waitSentText(tg, OWNER, '【系统】已更新供应商「my-proxy」，拉到 3 个模型。', k, 20_000)
    })
  })
})

describe('新建后交给 dsh', () => {
  test('dsh 已在跑时新建：网关发现变化（providers.reloaded），空闲时重启 dsh，补丁层里有它', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, b, fm, key }) => {
      await warmUp(tg)
      const starts = dshStarts(b)
      await wizardCreate(tg, { name: 'myproxy', fmt: 'openai', url: `${fm.url}/v1`, key })
      await waitEvent(b, 'providers.reloaded')
      const r = await until(() => dshStarts(b) > starts && patchRoutes(b).myproxy, 'dsh 带新路由重启', 20_000)
      expect(r.api).toBe('openai-completions')
      expect(r.baseURL).toBe(`${fm.url}/v1`)
      expect(r.apiKeyEnv).toBe('PROVIDER_MYPROXY_KEY')
      expect(r.models.map((m: any) => m.id)).toEqual(IDS)
    })
  })

  test('交给 dsh 的路由只有白名单字段（没有 meta，没有密钥）', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, b, fm, key }) => {
      await warmUp(tg)
      const starts = dshStarts(b)
      await wizardCreate(tg, { name: 'myproxy', fmt: 'openai', url: `${fm.url}/v1`, key })
      const r = await until(() => dshStarts(b) > starts && patchRoutes(b).myproxy, 'dsh 带新路由重启', 20_000)
      expect(Object.keys(r).sort()).toEqual(['api', 'apiKeyEnv', 'baseURL', 'models'])
      for (const m of r.models) expect(Object.keys(m).every((k: string) => ['id', 'contextWindow', 'reasoningEfforts'].includes(k))).toBe(true)
      expect(JSON.stringify(r)).not.toContain(key)
    })
  })

  test('没拉到模型（models 为空）的自建供应商不交给 dsh', async () => {
    await withModels({ handler: () => json({ error: 'x' }, 500) }, async ({ tg, b, fm, key }) => {
      await wizardCreate(tg, { name: 'myproxy', fmt: 'openai', url: `${fm.url}/v1`, key })
      await waitEvent(b, 'providers.reloaded')
      await warmUp(tg, '建完再说一句')
      expect(patchSeen(b).length).toBeGreaterThan(0)
      expect(patchRoutes(b).myproxy).toBeUndefined()
    })
  })
})
