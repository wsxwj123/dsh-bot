// 验收：网关本机接口 POST /v1/provider/save（INTERFACE-管理台UI 3.2）。
// 契约要点
// 1. 校验顺序写死 1..8（name -> 重名 -> 格式 -> 地址 -> 密钥 -> 空密钥的 modify 分支 -> 空密钥的 create 分支）；
// 2. mode=modify 且 key 留空时凭据行为三条硬约束（值不变、行不删、其余字节不变）；
// 3. 同名即更新（kind=created/updated/collided），mode 只影响密钥缺省处理；
// 4. 错误码表（busy / providers_unreadable / save_failed / not_found / key_missing / needs_key）。
// 实现未落地时本文件红（多半 404），那是红基线，不是测试坏了。
import { describe, expect, test } from 'bun:test'
import { writeFileSync } from 'fs'
import { defaultKeyEnv, fakeKey, lockPath, openProviderMenu, providersPath, readCreds, readProviders, seedCreds, seedProviders, withBot, withModels, type BotEnv } from './_acc'
import { type FakeModels, json, openaiOk } from './fake-models'

const IDS = ['m1', 'm2']
const SAVE = '/v1/provider/save'
const ROUTES = { myroute: { api: 'openai-completions', baseURL: 'https://r.example.com/v1', apiKeyEnv: 'MYROUTE_KEY', models: [{ id: 'r1', contextWindow: 32768 }] } }

type SeedOpts = { name?: string; key?: string | null; baseURL?: string; api?: string; models?: string[] }
/** 预置一个已存在的自建。key: null 表示不写凭据文件 */
const seedCustom = (o: SeedOpts = {}) => (b: BotEnv, fm: FakeModels) => {
  const name = o.name ?? 'myproxy'
  seedProviders(b.root, [{ name, api: (o.api ?? 'openai-completions') as any, baseURL: o.baseURL ?? `${fm.url}/v1`, models: o.models ?? IDS }])
  if (o.key !== null) seedCreds(b.root, { [defaultKeyEnv(name)]: o.key ?? fm.expectKey })
}

/** save 的请求体；over 里把某个键设成 undefined 就是"缺失"（JSON.stringify 会丢掉它） */
const body = (fm: FakeModels, over: Record<string, unknown> = {}) => ({
  name: 'myproxy', api: 'openai-completions', baseURL: `${fm.url}/v1`, key: fakeKey('save'), mode: 'create', ...over,
})

describe('POST /v1/provider/save 的正常路径', () => {
  test('新建：200，kind=created，text 说已添加并带模型数，落盘与凭据文件都对', async () => {
    const key = fakeKey('save')
    // 让假模型服务的期望密钥与请求体里用的一致，否则假服务认不出网关带来的密钥
    await withModels({ handler: openaiOk(IDS), key }, async ({ gw, b, fm }) => {
      const r = await gw.call(SAVE, body(fm, { key }))
      expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(r.json, '正文应与契约 3.2 成功样例一致（text 只判包含）').toMatchObject({
        ok: true, kind: 'created', name: 'myproxy', count: 2, reason: null, epoch_changed: false,
      })
      expect(String(r.json.text), `text 应说已添加，实得 ${JSON.stringify(r.json.text)}`).toContain('已添加供应商「myproxy」')
      const entry = readProviders(b.root)?.providers?.myproxy
      expect(entry, 'providers.json 里应出现 myproxy').toBeTruthy()
      expect(entry.route.baseURL, '地址应落盘').toBe(`${fm.url}/v1`)
      expect(entry.route.models.map((m: any) => m.id), '拉到的模型应落盘').toEqual(IDS)
      expect(readCreds(b.root), '凭据文件里应有新密钥').toContain(key)
      expect(fm.requests.length, '应真去拉了模型列表').toBeGreaterThan(0)
      expect(fm.requests[0]!.authOk, '拉列表时应带着新模式').toBe(true)
    })
  })

  test('同名新建：不报错，走更新，kind=updated', async () => {
    await withModels({ handler: openaiOk(IDS), before: seedCustom() }, async ({ gw, fm }) => {
      const r = await gw.call(SAVE, body(fm))
      expect(r.status, `同名更新应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(r.json.kind, `同名已有条目时应 updated，实得 ${JSON.stringify(r.json.kind)}`).toBe('updated')
      expect(String(r.json.text), `text 应说已更新，实得 ${JSON.stringify(r.json.text)}`).toContain('已更新供应商「myproxy」')
    })
  })

  test('mode 可省，默认按新建：key 空时报 needs_key', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ gw, fm }) => {
      const r = await gw.call(SAVE, body(fm, { key: '', mode: undefined }))
      expect(r.status, `默认 create 且没密钥应 400，实得 ${r.status}`).toBe(400)
      expect(r.json, 'error 应是 needs_key').toMatchObject({ ok: false, error: 'needs_key' })
    })
  })

  test('modify 且 key 留空、地址与格式都没变：用凭据文件里的旧密钥去拉列表，200', async () => {
    await withModels({ handler: openaiOk(IDS), before: seedCustom() }, async ({ gw, b, fm }) => {
      const before = readCreds(b.root)
      const r = await gw.call(SAVE, body(fm, { key: '', mode: 'modify' }))
      expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(fm.requests.length, '应用现有密钥去拉列表').toBeGreaterThan(0)
      expect(fm.requests[0]!.authOk, '拉列表必须带凭据文件里的密钥，不能是空串').toBe(true)
      expect(readCreds(b.root), '凭据文件应逐字节不变').toBe(before)
    })
  })

  test('modify 带新密钥：凭据文件里换成新密钥', async () => {
    const OLD = 'test-key-7old-old-old-old-old-0000'
    const fresh = fakeKey('fresh')
    await withModels({ handler: openaiOk(IDS), key: fresh, before: seedCustom({ key: OLD }) }, async ({ gw, b, fm }) => {
      const r = await gw.call(SAVE, body(fm, { key: fresh, mode: 'modify' }))
      expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      const after = readCreds(b.root)
      expect(after, `凭据文件里应是新密钥，实得：\n${after}`).toContain(fresh)
      expect(after.includes(OLD), '旧密钥应被换掉').toBe(false)
    })
  })

  test('modify 换了主机：epoch_changed=true，新地址落盘', async () => {
    await withModels({ handler: openaiOk(IDS), before: seedCustom({ baseURL: 'https://old.example.com/v1' }) }, async ({ gw, b, fm }) => {
      const r = await gw.call(SAVE, body(fm, { mode: 'modify' }))
      expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(r.json.epoch_changed, '换了主机应 epoch_changed=true').toBe(true)
      expect(readProviders(b.root).providers.myproxy.route.baseURL, '新地址应落盘').toBe(`${fm.url}/v1`)
    })
  })

  test('拉模型失败：仍 200，ok=true，reason 非空，text 有话说', async () => {
    await withModels({ handler: () => json({}, 500) }, async ({ gw, fm }) => {
      const r = await gw.call(SAVE, body(fm))
      expect(r.status, `拉失败也是保存成功，应 200，实得 ${r.status}`).toBe(200)
      expect(r.json.ok, 'ok 应为 true').toBe(true)
      expect(r.json.reason, `reason 应非空（如 server_error），实得 ${JSON.stringify(r.json.reason)}`).toBe('server_error')
      expect(String(r.json.text).length, 'text 应说明没拉到与后续办法').toBeGreaterThan(0)
    })
  })
})

describe('POST /v1/provider/save 的校验顺序', () => {
  test('顺序 1：name 缺失', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ gw, fm }) => {
      const r = await gw.call(SAVE, body(fm, { name: undefined }))
      expect(r.status, `应 400，实得 ${r.status}`).toBe(400)
      expect(r.json, 'error 应是 bad_name').toMatchObject({ ok: false, error: 'bad_name' })
    })
  })

  for (const name of ['-abc', '_abc', 'bad.name', 'a b', '中文名', 'a'.repeat(33), '', 123] as const) {
    test(`顺序 1：name 不合规则（${JSON.stringify(name)}）400 bad_name`, async () => {
      await withModels({ handler: openaiOk(IDS) }, async ({ gw, fm }) => {
        const r = await gw.call(SAVE, body(fm, { name }))
        expect(r.status, `应 400，实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(400)
        expect(r.json, `error 应是 bad_name，实得 ${JSON.stringify(r.json)}`).toMatchObject({ ok: false, error: 'bad_name' })
        expect(String(r.json.text), 'text 应说明名字规则').toContain('名字只能用')
      })
    })
  }

  test('顺序 2：内置名（规整后同名）400 name_taken', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ gw, fm }) => {
      const r = await gw.call(SAVE, body(fm, { name: 'DeepSeek_Official' }))
      expect(r.status, `应 400，实得 ${r.status}`).toBe(400)
      expect(r.json, `error 应是 name_taken，实得 ${JSON.stringify(r.json)}`).toMatchObject({ ok: false, error: 'name_taken' })
    })
  })

  for (const name of ['add', 'refresh', 'list', 'status', 'default', 'help'] as const) {
    test(`顺序 2：保留字（${name}）400 name_taken`, async () => {
      await withModels({ handler: openaiOk(IDS) }, async ({ gw, fm }) => {
        const r = await gw.call(SAVE, body(fm, { name }))
        expect(r.status, `应 400，实得 ${r.status}`).toBe(400)
        expect(r.json, `error 应是 name_taken，实得 ${JSON.stringify(r.json)}`).toMatchObject({ ok: false, error: 'name_taken' })
      })
    })
  }

  test('顺序 2：与本 bot 配置文件里的路由重名 400 name_taken', async () => {
    await withModels({ handler: openaiOk(IDS), brain: { routes: ROUTES } }, async ({ gw, fm }) => {
      const r = await gw.call(SAVE, body(fm, { name: 'MyRoute' }))
      expect(r.status, `应 400，实得 ${r.status}`).toBe(400)
      expect(r.json, `error 应是 name_taken，实得 ${JSON.stringify(r.json)}`).toMatchObject({ ok: false, error: 'name_taken' })
    })
  })

  for (const api of ['openai-responses', 'grpc', '', 123, undefined] as const) {
    test(`顺序 3：api 不是两种之一（${JSON.stringify(api)}）400 bad_format`, async () => {
      await withModels({ handler: openaiOk(IDS) }, async ({ gw, fm }) => {
        const r = await gw.call(SAVE, body(fm, { api }))
        expect(r.status, `应 400，实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(400)
        expect(r.json, `error 应是 bad_format，实得 ${JSON.stringify(r.json)}`).toMatchObject({ ok: false, error: 'bad_format' })
      })
    })
  }

  const BAD_URLS: [string, string][] = [
    ['ftp://files.example.com/v1', 'https'],
    ['example.com/v1', 'https'],
    ['http://example.com/v1', '本机'],
    ['http://127.0.0.2/v1', '本机'],
    ['https://user:pass@example.com/v1', '用户名'],
    ['https://example.com/v1?x=1', '?'],
    ['https://example.com/v1#frag', '?'],
    ['https://example.com/' + 'a'.repeat(281), '太长'],
  ]
  for (const [url, why] of BAD_URLS) {
    test(`顺序 4：地址不合规（${url.slice(0, 40)}…）400 bad_url 且带原因`, async () => {
      await withModels({ handler: openaiOk(IDS) }, async ({ gw, fm }) => {
        const r = await gw.call(SAVE, body(fm, { baseURL: url }))
        expect(r.status, `应 400，实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(400)
        expect(r.json, `error 应是 bad_url，实得 ${JSON.stringify(r.json)}`).toMatchObject({ ok: false, error: 'bad_url' })
        expect(String(r.json.text), `地址原因应提到「${why}」，实得 ${JSON.stringify(r.json.text)}`).toContain(why)
      })
    })
  }

  const BAD_KEYS = ['short7c', 'abc def ghijklmnop', '中文密钥abcdefgh', 'a'.repeat(513)]
  for (const key of BAD_KEYS) {
    test(`顺序 5：密钥不合法（${key.length} 字符）400 bad_key 且不回显输入值`, async () => {
      await withModels({ handler: openaiOk(IDS) }, async ({ gw, fm }) => {
        const r = await gw.call(SAVE, body(fm, { key }))
        expect(r.status, `应 400，实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(400)
        expect(r.json, `error 应是 bad_key，实得 ${JSON.stringify(r.json)}`).toMatchObject({ ok: false, error: 'bad_key' })
        expect(String(r.text).includes(key), '响应里回显了不合法密钥的输入值').toBe(false)
      })
    })
  }

  test('顺序 4 先于 5：地址与密钥都不合法，报 bad_url', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ gw, fm }) => {
      const r = await gw.call(SAVE, body(fm, { baseURL: 'http://example.com/v1', key: 'short7c' }))
      expect(r.json.error, `应先报 bad_url，实得 ${JSON.stringify(r.json.error)}`).toBe('bad_url')
    })
  })

  test('顺序 3 先于 4、5：格式、地址、密钥都不合法，报 bad_format', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ gw, fm }) => {
      const r = await gw.call(SAVE, body(fm, { api: 'grpc', baseURL: 'http://example.com/v1', key: 'short7c' }))
      expect(r.json.error, `应先报 bad_format，实得 ${JSON.stringify(r.json.error)}`).toBe('bad_format')
    })
  })

  test('顺序 1 先于 3、4、5：name、格式、地址、密钥都不合法，报 bad_name', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ gw, fm }) => {
      const r = await gw.call(SAVE, body(fm, { name: 'bad.name', api: 'grpc', baseURL: 'http://example.com/v1', key: 'short7c' }))
      expect(r.json.error, `应先报 bad_name，实得 ${JSON.stringify(r.json.error)}`).toBe('bad_name')
    })
  })
})

describe('POST /v1/provider/save 的密钥缺省分支（顺序 6、7）', () => {
  test('顺序 7：新建但 key 空串，400 needs_key', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ gw, fm }) => {
      const r = await gw.call(SAVE, body(fm, { key: '' }))
      expect(r.status, `应 400，实得 ${r.status}`).toBe(400)
      expect(r.json, `error 应是 needs_key，实得 ${JSON.stringify(r.json)}`).toMatchObject({ ok: false, error: 'needs_key' })
      expect(String(r.json.text), 'text 应说新建需要密钥').toContain('需要密钥')
    })
  })

  test('顺序 7：新建但 key 缺失，400 needs_key', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ gw, fm }) => {
      const r = await gw.call(SAVE, body(fm, { key: undefined }))
      expect(r.status, `应 400，实得 ${r.status}`).toBe(400)
      expect(r.json.error).toBe('needs_key')
    })
  })

  test('顺序 6：modify 但条目已被别处删掉，404 not_found', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ gw, fm }) => {
      const r = await gw.call(SAVE, body(fm, { key: '', mode: 'modify' }))
      expect(r.status, `应 404，实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(404)
      expect(r.json, `error 应是 not_found，实得 ${JSON.stringify(r.json)}`).toMatchObject({ ok: false, error: 'not_found' })
      expect(String(r.json.text), 'text 应说已经不在了').toContain('已经不在了')
    })
  })

  test('顺序 6：modify 换了主机但 key 空，400 needs_key（旧密钥不会发给新地址）', async () => {
    await withModels({ handler: openaiOk(IDS), before: seedCustom({ baseURL: 'https://old.example.com/v1' }) }, async ({ gw, fm }) => {
      const r = await gw.call(SAVE, body(fm, { key: '', mode: 'modify' }))
      expect(r.status, `应 400，实得 ${r.status}`).toBe(400)
      expect(r.json, `error 应是 needs_key，实得 ${JSON.stringify(r.json)}`).toMatchObject({ ok: false, error: 'needs_key' })
    })
  })

  test('顺序 6：modify 但凭据文件里读不到现有密钥，409 key_missing，不发拉列表请求', async () => {
    await withModels({ handler: openaiOk(IDS), before: seedCustom({ key: null }) }, async ({ gw, fm }) => {
      const r = await gw.call(SAVE, body(fm, { key: '', mode: 'modify' }))
      expect(r.status, `应 409，实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(409)
      expect(r.json, `error 应是 key_missing，实得 ${JSON.stringify(r.json)}`).toMatchObject({ ok: false, error: 'key_missing' })
      expect(fm.requests.length, '缺密钥时不该去拉列表').toBe(0)
    })
  })
})

describe('POST /v1/provider/save 的错误码', () => {
  test('拿不到锁：409 busy', async () => {
    await withModels({ gw: { provider_lock_wait_ms: 300 }, handler: openaiOk(IDS), before: b => {
      seedProviders(b.root, [])
      writeFileSync(lockPath(b.root), JSON.stringify({ pid: process.pid, at: Date.now() }))
    } }, async ({ gw, fm }) => {
      const r = await gw.call(SAVE, body(fm))
      expect(r.status, `锁被占应 409，实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(409)
      expect(r.json, `error 应是 busy，实得 ${JSON.stringify(r.json)}`).toMatchObject({ ok: false, error: 'busy' })
      expect(String(r.json.text), 'text 应说别的 bot 正在改').toContain('正在改')
    })
  })

  test('providers.json 读不了：503 providers_unreadable', async () => {
    await withModels({ handler: openaiOk(IDS), before: b => writeFileSync(providersPath(b.root), '[[[') }, async ({ gw, fm }) => {
      const r = await gw.call(SAVE, body(fm))
      expect(r.status, `应 503，实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(503)
      expect(r.json, `error 应是 providers_unreadable，实得 ${JSON.stringify(r.json)}`).toMatchObject({ ok: false, error: 'providers_unreadable' })
      expect(String(r.json.text), 'text 应指向 providers.json').toContain('providers.json')
    })
  })

  test('同名不合格条目挡路：500 save_failed，密钥不写入', async () => {
    const key = fakeKey('blocked')
    await withModels({ handler: openaiOk(IDS), before: b => {
      seedProviders(b.root, [{ name: 'broken', api: 'grpc' as any, baseURL: 'https://x.example.com/v1', models: ['x1'] }])
      seedCreds(b.root, { OTHER_KEY: 'other-value-123456' })
    } }, async ({ gw, b, fm }) => {
      const r = await gw.call(SAVE, body(fm, { name: 'broken', key }))
      expect(r.status, `应 500，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(500)
      expect(r.json, `error 应是 save_failed，实得 ${JSON.stringify(r.json)}`).toMatchObject({ ok: false, error: 'save_failed' })
      expect(String(r.json.text), 'text 应说没保存成功').toContain('没保存成功')
      expect(readCreds(b.root).includes(key), '失败时密钥不该写进凭据文件').toBe(false)
    })
  })
})

describe('POST /v1/provider/save 的凭据行硬约束（mode=modify 且 key 留空）', () => {
  test('双引号写法：成功后凭据文件逐字节不变，值非空，行还在', async () => {
    await withModels({ handler: openaiOk(IDS), before: seedCustom() }, async ({ gw, b, fm }) => {
      const before = readCreds(b.root)
      const key = defaultKeyEnv('myproxy')
      expect(before.split('\n').find(l => l.includes(key)), '前置条件 凭据文件里应有这一行').toBeTruthy()
      const r = await gw.call(SAVE, body(fm, { key: '', mode: 'modify' }))
      expect(r.status, `应 200，实得 ${r.status}`).toBe(200)
      const after = readCreds(b.root)
      expect(after, '双引号写法时整份文件应逐字节不变').toBe(before)
      const line = after.split('\n').find(l => l.includes(key))
      expect(line, `这条凭据行不许消失：\n${after}`).toBeTruthy()
      expect(line!, `这条凭据行的值被写空了：${line}`).not.toMatch(/:\s*(""|'')\s*$/)
    })
  })

  test('用户手写写法（单引号）：值必须不变，其它行逐字节不变', async () => {
    const HAND = fakeKey('hand')
    const head = '# 手写凭据\nversion: 1\nrefs:\n' + `  ${defaultKeyEnv('myproxy')}: '${HAND}'\n`
    await withModels({ handler: openaiOk(IDS), key: HAND, before: (b, fm) => {
      seedProviders(b.root, [{ name: 'myproxy', baseURL: `${fm.url}/v1`, models: IDS }])
      seedCreds(b.root, { OTHER_KEY: 'other-value-123456' }, head)
    } }, async ({ gw, b, fm }) => {
      const r = await gw.call(SAVE, body(fm, { key: '', mode: 'modify' }))
      expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      const after = readCreds(b.root)
      expect(after.includes(HAND), `手写行的值被改掉了：\n${after}`).toBe(true)
      expect(after, '别的行不许动').toContain('OTHER_KEY: "other-value-123456"')
      const keyLine = after.split('\n').find(l => l.includes(defaultKeyEnv('myproxy')))
      expect(keyLine, `这条凭据行不许消失：\n${after}`).toBeTruthy()
    })
  })

  test('反向：成功后该行的值仍能被拉到列表的请求验证（不是空串占位）', async () => {
    await withModels({ handler: openaiOk(IDS), before: seedCustom() }, async ({ gw, fm }) => {
      await gw.call(SAVE, body(fm, { key: '', mode: 'modify' }))
      expect(fm.requests.length, '必须真去拉了列表').toBeGreaterThan(0)
      expect(fm.requests[0]!.authOk, '请求里带的必须是原密钥，不是空串').toBe(true)
      expect(fm.requests[0]!.authLen, '鉴权头长度应与原密钥一致（空串会短一截）').toBe(`Bearer ${fm.expectKey}`.length)
    })
  })
})

describe('POST /v1/provider/save 与 Telegram 侧一致（同一套存储）', () => {
  test('本机 save 建的供应商，Telegram /provider 菜单里能看到', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, gw, fm }) => {
      const r = await gw.call(SAVE, body(fm, { name: 'crosspath' }))
      expect(r.status, `应 200，实得 ${r.status}`).toBe(200)
      const m = await openProviderMenu(tg)
      expect(m.text, `Telegram 菜单里应列出 crosspath，实得菜单：\n${m.text}`).toContain('crosspath')
    })
  })
})

describe('POST /v1/provider/save 的全局约束', () => {
  test('带 Origin 头：403', async () => {
    await withBot({ before: b => seedProviders(b.root, []) }, async ({ gw }) => {
      const r = await gw.api(SAVE, { method: 'POST', headers: { authorization: `Bearer ${gw.apiToken()}`, origin: 'http://evil.example', 'content-type': 'application/json' }, body: '{}' })
      expect(r.status, `带 Origin 应 403，实得 ${r.status}`).toBe(403)
    })
  })

  test('没带口令：401', async () => {
    await withBot({ before: b => seedProviders(b.root, []) }, async ({ gw }) => {
      const r = await gw.api(SAVE, { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' })
      expect(r.status, `没带口令应 401，实得 ${r.status}`).toBe(401)
    })
  })

  test('Content-Type 不是 JSON：415', async () => {
    await withBot({ before: b => seedProviders(b.root, []) }, async ({ gw }) => {
      const r = await gw.api(SAVE, { method: 'POST', headers: { authorization: `Bearer ${gw.apiToken()}`, 'content-type': 'text/plain' }, body: '{}' })
      expect(r.status, `Content-Type 不对应 415，实得 ${r.status}`).toBe(415)
    })
  })

  test('请求体不是 JSON 对象：400 invalid json', async () => {
    await withBot({ before: b => seedProviders(b.root, []) }, async ({ gw }) => {
      const r = await gw.api(SAVE, { method: 'POST', headers: { authorization: `Bearer ${gw.apiToken()}`, 'content-type': 'application/json' }, body: '[1, 2]' })
      expect(r.status, `数组体应 400，实得 ${r.status}`).toBe(400)
      const j = await r.json() as any
      expect(j?.error, `error 应是 invalid json，实得 ${JSON.stringify(j)}`).toBe('invalid json')
    })
  })

  test('请求体超 1MB：413', async () => {
    await withBot({ before: b => seedProviders(b.root, []) }, async ({ gw }) => {
      const big = JSON.stringify({ name: 'x'.repeat(1_100_000) })
      const r = await gw.api(SAVE, { method: 'POST', headers: { authorization: `Bearer ${gw.apiToken()}`, 'content-type': 'application/json' }, body: big })
      expect(r.status, `超 1MB 应 413，实得 ${r.status}`).toBe(413)
    })
  })
})
