// 验收：/provider 引导的「删除」与延迟删密钥（INTERFACE 3.4.4、3.1.1 pendingKeyRemovals、BRIEF D8）
import { describe, expect, test } from 'bun:test'
import { OWNER, credPath, labels, warmUp, openProviderMenu, override, patchRoutes, providerAdd, readCreds, readProviders, seedCreds, seedProviders, sleep, until, waitEvent, waitSentText, waitText, withModels, type BotEnv } from './_acc'
import { openaiOk } from './fake-models'
import { writeFileSync } from 'fs'

const IDS = ['m1', 'm2']
const KEEP = '# 我的凭据\nversion: 1\nrefs:\n  DEEPSEEK_API_KEY: "sk-test-deepseek-000000"\n'
const seed = (url = 'https://p.example.com/v1') => (b: BotEnv) => {
  seedProviders(b.root, [{ name: 'myproxy', baseURL: url, models: IDS }])
  writeCredsRaw(b, KEEP + '  PROVIDER_MYPROXY_KEY: "test-key-7mine0000000001"\n')
}
function writeCredsRaw(b: BotEnv, body: string) { writeFileSync(credPath(b.root), body, { mode: 0o600 }) }

async function toConfirm(tg: any): Promise<number> {
  const m = await openProviderMenu(tg)
  tg.clickButton(OWNER, OWNER, m.messageId, '删除')
  await waitText(tg, OWNER, m.messageId, t => t.includes('【系统】删除哪个自建供应商？'), '删除哪个')
  tg.clickButton(OWNER, OWNER, m.messageId, 'myproxy')
  await waitText(tg, OWNER, m.messageId, t => t.includes('确认删除「myproxy」'), '确认')
  return m.messageId
}

async function confirmDelete(tg: any): Promise<{ mid: number; text: string }> {
  const mid = await toConfirm(tg)
  tg.clickButton(OWNER, OWNER, mid, '确认删除')
  const text = await waitText(tg, OWNER, mid, t => t.includes('已删除供应商「myproxy」') || t.includes('没删成'), '删除结果', 20_000)
  return { mid, text }
}

describe('删除', () => {
  test('点「删除」：列出自建供应商 +「取消」', async () => {
    await withModels({ handler: openaiOk(IDS), before: seed() }, async ({ tg }) => {
      const m = await openProviderMenu(tg)
      tg.clickButton(OWNER, OWNER, m.messageId, '删除')
      await waitText(tg, OWNER, m.messageId, t => t === '【系统】删除哪个自建供应商？', '删除哪个')
      expect(labels(tg, m.messageId)).toEqual(['myproxy', '取消'])
    })
  })

  test('没有自建时点「删除」：「还没有自建供应商。」按钮「新建」「取消」', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg }) => {
      const m = await openProviderMenu(tg)
      tg.clickButton(OWNER, OWNER, m.messageId, '删除')
      await waitText(tg, OWNER, m.messageId, t => t === '【系统】还没有自建供应商。', '没有自建')
      expect(labels(tg, m.messageId)).toEqual(['新建', '取消'])
    })
  })

  test('确认页全文与按钮「确认删除」「取消」（本 bot 没在用它）', async () => {
    await withModels({ handler: openaiOk(IDS), before: seed() }, async ({ tg }) => {
      const mid = await toConfirm(tg)
      expect(tg.textOf(OWNER, mid)).toBe('【系统】确认删除「myproxy」？会同时删除它的密钥。正在用它的 bot 会自动换回各自配置文件里的模型。')
      expect(labels(tg, mid)).toEqual(['确认删除', '取消'])
    })
  })

  test('确认后：菜单写明已删除、密钥 10 分钟后从凭据文件删、不会在供应商那边作废', async () => {
    await withModels({ handler: openaiOk(IDS), before: seed() }, async ({ tg }) => {
      const { text } = await confirmDelete(tg)
      expect(text).toContain('【系统】已删除供应商「myproxy」。它的密钥会在 10 分钟后从凭据文件删除（让正在用它的 bot 先切走）。注意：这只是从本机删掉，不会在供应商那边作废这把密钥；如果担心泄露，请到供应商后台作废它。')
    })
  })

  test('确认后：providers.json 里没有它，键名登记进 pendingKeyRemovals（after ≈ 删除时刻 + 600000），凭据文件里暂时还在', async () => {
    await withModels({ handler: openaiOk(IDS), before: seed() }, async ({ tg, b }) => {
      const t0 = Date.now()
      await confirmDelete(tg)
      const p = readProviders(b.root)
      expect(p.providers.myproxy).toBeUndefined()
      expect(p.pendingKeyRemovals.length).toBe(1)
      expect(p.pendingKeyRemovals[0].key).toBe('PROVIDER_MYPROXY_KEY')
      expect(Math.abs(p.pendingKeyRemovals[0].after - (t0 + 600_000))).toBeLessThan(15_000)
      expect(readCreds(b.root)).toContain('PROVIDER_MYPROXY_KEY')
    })
  })

  test('确认后：日志 provider.deleted {name}', async () => {
    await withModels({ handler: openaiOk(IDS), before: seed() }, async ({ tg, b }) => {
      await confirmDelete(tg)
      await waitEvent(b, 'provider.deleted', e => e.name === 'myproxy')
    })
  })

  test('provider_key_grace_ms=0：下一次轮询就从凭据文件删掉键，其它行逐字节不变，登记移除，日志 provider.key_removed', async () => {
    await withModels({ gw: { provider_key_grace_ms: 0 }, handler: openaiOk(IDS), before: seed() }, async ({ tg, b }) => {
      await confirmDelete(tg)
      await until(() => !readCreds(b.root).includes('PROVIDER_MYPROXY_KEY'), '键被删', 15_000)
      expect(readCreds(b.root)).toBe(KEEP)
      expect(readProviders(b.root).pendingKeyRemovals).toEqual([])
      await waitEvent(b, 'provider.key_removed', e => e.key === 'PROVIDER_MYPROXY_KEY')
    })
  })

  test('本 bot 正在用它：确认页加一句；删除后清掉覆盖并在结果里说换回了配置文件里的模型', async () => {
    await withModels({ handler: openaiOk(IDS), before: seed() }, async ({ tg, b }) => {
      tg.pushText(OWNER, '/model myproxy/m1')
      await waitSentText(tg, OWNER, '已换成 myproxy / m1')
      const mid = await toConfirm(tg)
      expect(tg.textOf(OWNER, mid)).toContain('这个 bot 正在用它，删除后换回 deepseek-official / deepseek-flash。')
      tg.clickButton(OWNER, OWNER, mid, '确认删除')
      const t = await waitText(tg, OWNER, mid, x => x.includes('已删除供应商「myproxy」'), '删除结果', 20_000)
      expect(t).toContain('这个 bot 已换回配置文件里的模型：deepseek-official / deepseek-flash。')
      const o = override(b)
      expect(o === null || !o.provider).toBe(true)
    })
  })

  test('确认页点「取消」：菜单「已取消。」，providers.json 不变', async () => {
    await withModels({ handler: openaiOk(IDS), before: seed() }, async ({ tg, b }) => {
      const before = JSON.stringify(readProviders(b.root))
      const mid = await toConfirm(tg)
      tg.clickButton(OWNER, OWNER, mid, '取消')
      await waitText(tg, OWNER, mid, t => t === '已取消。', '已取消')
      expect(JSON.stringify(readProviders(b.root))).toBe(before)
    })
  })

  test('选好后、确认前被别处删掉：确认时回「「myproxy」已经不在了。」', async () => {
    await withModels({ handler: openaiOk(IDS), before: seed() }, async ({ tg, b }) => {
      const mid = await toConfirm(tg)
      seedProviders(b.root, [])
      await sleep(500)
      tg.clickButton(OWNER, OWNER, mid, '确认删除')
      await waitText(tg, OWNER, mid, t => t.includes('【系统】「myproxy」已经不在了。'), '已经不在了')
    })
  })

  test('删除后同名重建：键名不复用（PROVIDER_MYPROXY_KEY_2），epoch 是新的', async () => {
    await withModels({ handler: openaiOk(IDS), before: (b, fm) => seed(`${fm.url}/v1`)(b) }, async ({ tg, b, fm, key }) => {
      const oldEpoch = readProviders(b.root).providers.myproxy.meta.epoch
      await confirmDelete(tg)
      await providerAdd(tg, `myproxy ${fm.url}/v1 ${key} openai`)
      const e = readProviders(b.root).providers.myproxy
      expect(e.route.apiKeyEnv).toBe('PROVIDER_MYPROXY_KEY_2')
      expect(e.meta.epoch).not.toBe(oldEpoch)
    })
  })

  test('删除后 dsh 按新配置重启，交给 dsh 的路由里不再有它', async () => {
    await withModels({ handler: openaiOk(IDS), before: seed() }, async ({ tg, b }) => {
      await warmUp(tg)
      await until(() => patchRoutes(b).myproxy, '补丁层有 myproxy', 20_000)
      await confirmDelete(tg)
      await until(() => !patchRoutes(b).myproxy, '补丁层去掉 myproxy', 20_000)
    })
  })
})

describe('pendingKeyRemovals 的安全处理', () => {
  test('登记里的键名不合规则（DEEPSEEK_API_KEY）：直接丢弃、绝不删它，日志 providers.entry_skipped', async () => {
    await withModels({ gw: { provider_key_grace_ms: 0 }, handler: openaiOk(IDS), before: b => { seedProviders(b.root, [], [{ key: 'DEEPSEEK_API_KEY', after: 0 }]); seedCreds(b.root, { DEEPSEEK_API_KEY: 'sk-test-deepseek-000000' }) } }, async ({ b }) => {
      const before = readCreds(b.root)
      await waitEvent(b, 'providers.entry_skipped')
      await sleep(1_500)
      expect(readCreds(b.root)).toBe(before)
    })
  })

  test('启动时已有到点的合规登记：由网关在锁内从凭据文件删掉该键并移除登记', async () => {
    await withModels({ handler: openaiOk(IDS), before: b => { seedProviders(b.root, [], [{ key: 'PROVIDER_GONE_KEY', after: 1 }]); seedCreds(b.root, { DEEPSEEK_API_KEY: 'sk-test-deepseek-000000', PROVIDER_GONE_KEY: 'test-key-7gone000000000' }) } }, async ({ b }) => {
      await until(() => !readCreds(b.root).includes('PROVIDER_GONE_KEY'), '到点的键被删', 15_000)
      expect(readCreds(b.root)).toContain('DEEPSEEK_API_KEY: "sk-test-deepseek-000000"')
      expect(readProviders(b.root).pendingKeyRemovals).toEqual([])
      await waitEvent(b, 'provider.key_removed', e => e.key === 'PROVIDER_GONE_KEY')
    })
  })
})
