// 验收：/provider 引导的「修改」（INTERFACE 3.4.3、3.1.1 的 epoch/keyRev/键名规则、3.2-4 只改密钥不重启 dsh）
import { describe, expect, test } from 'bun:test'
import { OWNER, defaultKeyEnv, dshStarts, findSecret, warmUp, labels, openProviderMenu, readCreds, readProviders, seedCreds, seedProviders, sleep, until, waitSentText, waitText, withModels, type BotEnv } from './_acc'
import { FakeModels, anthropicOk, openaiOk, sleep as fsleep } from './fake-models'

const IDS = ['m1', 'm2']
const OLD_KEY = 'test-key-7oldoldold1234567890'
const seed = (url: string, o: { api?: any; name?: string } = {}) => (b: BotEnv) => {
  seedProviders(b.root, [{ name: o.name ?? 'myproxy', api: o.api ?? 'openai-completions', baseURL: url, models: IDS, epoch: 1_700_000_000_000 }])
  seedCreds(b.root, { [defaultKeyEnv(o.name ?? 'myproxy')]: OLD_KEY })
}

/** 打开「修改」→ 选供应商，停在「改什么？」 */
async function toWhat(tg: any, name = 'myproxy'): Promise<number> {
  const m = await openProviderMenu(tg)
  tg.clickButton(OWNER, OWNER, m.messageId, '修改')
  await waitText(tg, OWNER, m.messageId, t => t.includes('【系统】修改哪个自建供应商？'), '修改哪个')
  tg.clickButton(OWNER, OWNER, m.messageId, name)
  await waitText(tg, OWNER, m.messageId, t => t.includes(`修改「${name}」`) && t.includes('改什么？'), '改什么')
  return m.messageId
}

describe('修改：选择', () => {
  test('没有自建供应商时点「修改」：「还没有自建供应商。」按钮「新建」「取消」', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg }) => {
      const m = await openProviderMenu(tg)
      tg.clickButton(OWNER, OWNER, m.messageId, '修改')
      await waitText(tg, OWNER, m.messageId, t => t === '【系统】还没有自建供应商。', '没有自建')
      expect(labels(tg, m.messageId)).toEqual(['新建', '取消'])
    })
  })

  test('修改列表只列自建（不列内置、配置文件路由），末尾「取消」', async () => {
    const routes = { myroute: { api: 'openai-completions', baseURL: 'https://r.example.com/v1', apiKeyEnv: 'MYROUTE_KEY', models: [{ id: 'r1', contextWindow: 65536 }] } }
    await withModels({ brain: { routes }, handler: openaiOk(IDS), before: b => seedProviders(b.root, [{ name: 'p-one', baseURL: 'https://a.example.com/v1', models: ['x'] }, { name: 'p-two', baseURL: 'https://b.example.com/v1', models: ['y'] }]) }, async ({ tg }) => {
      const m = await openProviderMenu(tg)
      tg.clickButton(OWNER, OWNER, m.messageId, '修改')
      await waitText(tg, OWNER, m.messageId, t => t.includes('修改哪个自建供应商'), '修改哪个')
      expect(labels(tg, m.messageId)).toEqual(['p-one', 'p-two', '取消'])
    })
  })

  test('选好供应商：「修改「myproxy」（OpenAI 格式，<地址>）：改什么？」按钮「地址」「密钥」「接口格式」「取消」', async () => {
    await withModels({ handler: openaiOk(IDS), before: seed('https://p.example.com/v1') }, async ({ tg }) => {
      const mid = await toWhat(tg)
      expect(tg.textOf(OWNER, mid)).toBe('【系统】修改「myproxy」（OpenAI 格式，https://p.example.com/v1）：改什么？')
      expect(labels(tg, mid)).toEqual(['地址', '密钥', '接口格式', '取消'])
    })
  })
})

const SEEDED = (o: { api?: any } = {}) => (b: BotEnv, fm: FakeModels) => seed(`${fm.url}${o.api === 'anthropic-messages' ? '' : '/v1'}`, o)(b)

/** 在「改什么？」点「密钥」，输入新密钥，等结果 */
async function changeKey(tg: any, mid: number, key: string) {
  tg.clickButton(OWNER, OWNER, mid, '密钥')
  await waitText(tg, OWNER, mid, t => t.includes('请输入密钥。收到后会马上删除你发的那条消息。'), '密钥步')
  const k = tg.sentTo(OWNER).length
  const msg = tg.pushText(OWNER, key)
  const r = await waitSentText(tg, OWNER, '【系统】已更新「myproxy」的密钥', k, 20_000)
  return { r, msg }
}

describe('修改：密钥', () => {
  test('改密钥：删除那条消息，用新密钥拉列表，回「已更新「myproxy」的密钥，拉到 2 个模型。」附删除说明，按钮「切过去」「关闭」', async () => {
    await withModels({ handler: openaiOk(IDS), before: SEEDED() }, async ({ tg, fm, key }) => {
      const { r, msg } = await changeKey(tg, await toWhat(tg), key)
      expect(r.text!).toContain('【系统】已更新「myproxy」的密钥，拉到 2 个模型。')
      expect(r.text!).toContain('（你发的密钥消息已删除。）')
      expect(tg.isDeleted(OWNER, msg)).toBe(true)
      expect(fm.requests.length).toBeGreaterThan(0)
      expect(fm.requests.every(q => q.authOk)).toBe(true)
      expect(labels(tg, r.messageId)).toEqual(['切过去', '关闭'])
    })
  })

  test('改密钥：凭据文件同一键名的值换成新密钥、旧密钥消失；keyRev 加 1，epoch 不变', async () => {
    await withModels({ handler: openaiOk(IDS), before: SEEDED() }, async ({ tg, b, key }) => {
      await changeKey(tg, await toWhat(tg), key)
      const c = readCreds(b.root)
      expect(c).toContain(`  PROVIDER_MYPROXY_KEY: "${key}"\n`)
      expect(c).not.toContain(OLD_KEY)
      const e = readProviders(b.root).providers.myproxy
      expect(e.route.apiKeyEnv).toBe('PROVIDER_MYPROXY_KEY')
      expect(e.meta.keyRev).toBe(2)
      expect(e.meta.epoch).toBe(1_700_000_000_000)
    })
  })

  test('只改密钥不重启 dsh（假 dsh 启动次数不变）', async () => {
    await withModels({ handler: openaiOk(IDS), before: SEEDED() }, async ({ tg, b, key }) => {
      await warmUp(tg)
      const starts0 = dshStarts(b)
      await changeKey(tg, await toWhat(tg), key)
      await sleep(1_500)
      await warmUp(tg, '改完密钥再说一句')
      expect(dshStarts(b)).toBe(starts0)
    })
  })

  test('改完密钥：新旧密钥都只在凭据文件里出现（新的）或彻底消失（旧的）', async () => {
    await withModels({ handler: openaiOk(IDS), before: SEEDED() }, async ({ tg, b, gw, key }) => {
      await changeKey(tg, await toWhat(tg), key)
      await sleep(500)
      expect(findSecret(b, key, { gw })).toEqual([])
      expect(findSecret(b, OLD_KEY, { gw })).toEqual([])
    })
  })
})
const ROLL_NOTE = '正在用它的聊天下一条消息会先写交接摘要、再开新会话（相当于换了一家）。'
const REKEY = '【系统】换了主机或格式，需要重新输入密钥（旧密钥不会发给新地址）：'

describe('修改：地址', () => {
  test('同一主机只改路径：不要求重新输入密钥，回「已更新「myproxy」的地址…」，epoch 与键名不变', async () => {
    await withModels({ key: OLD_KEY, handler: openaiOk(IDS), before: SEEDED() }, async ({ tg, b, fm }) => {
      const mid = await toWhat(tg)
      tg.clickButton(OWNER, OWNER, mid, '地址')
      await waitText(tg, OWNER, mid, t => t.includes('请输入接口地址'), '地址步')
      const k = tg.sentTo(OWNER).length
      tg.pushText(OWNER, `${fm.url}/api/v1`)
      const r = await waitSentText(tg, OWNER, '【系统】已更新「myproxy」的地址，拉到 2 个模型。', k, 20_000)
      expect(r.text!).not.toContain(ROLL_NOTE)
      const all = tg.sentTo(OWNER).slice(k).map(s => s.text ?? '').join('\n')
      expect(all).not.toContain('需要重新输入密钥')
      const e = readProviders(b.root).providers.myproxy
      expect(e.route.baseURL).toBe(`${fm.url}/api/v1`)
      expect(e.meta.epoch).toBe(1_700_000_000_000)
      expect(e.route.apiKeyEnv).toBe('PROVIDER_MYPROXY_KEY')
    })
  })

  test('换了主机（端口不同）：先要求重新输入密钥，旧密钥一次都没发往新地址', async () => {
    await withModels({ handler: openaiOk(IDS), before: SEEDED() }, async ({ tg, key }) => {
      const fm2 = new FakeModels(key, openaiOk(['n1', 'n2', 'n3']))
      try {
        const mid = await toWhat(tg)
        tg.clickButton(OWNER, OWNER, mid, '地址')
        await waitText(tg, OWNER, mid, t => t.includes('请输入接口地址'), '地址步')
        const k = tg.sentTo(OWNER).length
        tg.pushText(OWNER, `${fm2.url}/v1`)
        await until(() => [...tg.sentTo(OWNER).slice(k).map(s => s.text ?? ''), ...tg.edits.map(e => e.text ?? '')].some(t => t.includes(REKEY)), '要求重新输入密钥')
        expect(fm2.requests.length).toBe(0)
        tg.pushText(OWNER, key)
        const r = await waitSentText(tg, OWNER, '【系统】已更新「myproxy」的地址，拉到 3 个模型。', k, 20_000)
        expect(r.text!).toContain(ROLL_NOTE)
        expect(fm2.requests.length).toBeGreaterThan(0)
        expect(fm2.requests.every(q => q.authOk)).toBe(true)
      } finally { fm2.stop() }
    })
  })

  test('换了主机：新密钥写进新键名（_2），旧键名登记进 pendingKeyRemovals，epoch 变了', async () => {
    await withModels({ handler: openaiOk(IDS), before: SEEDED() }, async ({ tg, b, key }) => {
      const fm2 = new FakeModels(key, openaiOk(['n1']))
      try {
        const mid = await toWhat(tg)
        tg.clickButton(OWNER, OWNER, mid, '地址')
        await waitText(tg, OWNER, mid, t => t.includes('请输入接口地址'), '地址步')
        const k = tg.sentTo(OWNER).length
        tg.pushText(OWNER, `${fm2.url}/v1`)
        await until(() => tg.sentTo(OWNER).slice(k).some(s => (s.text ?? '').includes('需要重新输入密钥')) || tg.edits.some(e => (e.text ?? '').includes('需要重新输入密钥')), '要求重新输入密钥')
        tg.pushText(OWNER, key)
        await waitSentText(tg, OWNER, '已更新「myproxy」的地址', k, 20_000)
        const p = readProviders(b.root)
        expect(p.providers.myproxy.route.apiKeyEnv).toBe('PROVIDER_MYPROXY_KEY_2')
        expect(p.providers.myproxy.meta.epoch).toBeGreaterThan(1_700_000_000_000)
        expect(p.pendingKeyRemovals.map((x: any) => x.key)).toEqual(['PROVIDER_MYPROXY_KEY'])
        expect(readCreds(b.root)).toContain(`  PROVIDER_MYPROXY_KEY_2: "${key}"\n`)
      } finally { fm2.stop() }
    })
  })

  test('改地址的校验同新建（非本机 http:// 被拒）', async () => {
    await withModels({ handler: openaiOk(IDS), before: SEEDED() }, async ({ tg }) => {
      const mid = await toWhat(tg)
      tg.clickButton(OWNER, OWNER, mid, '地址')
      await waitText(tg, OWNER, mid, t => t.includes('请输入接口地址'), '地址步')
      const k = tg.sentTo(OWNER).length
      tg.pushText(OWNER, 'http://example.com/v1')
      await waitSentText(tg, OWNER, '【系统】只有本机地址（127.0.0.1、localhost、[::1]）可以用 http://，其它地址请用 https://。请重新输入：', k)
    })
  })
})

describe('修改：接口格式', () => {
  test('点「接口格式」：按钮里现在的格式标 ✅', async () => {
    await withModels({ handler: openaiOk(IDS), before: SEEDED() }, async ({ tg }) => {
      const mid = await toWhat(tg)
      tg.clickButton(OWNER, OWNER, mid, '接口格式')
      await until(() => labels(tg, mid).some(l => l.includes('Anthropic 格式')), '格式按钮')
      const ls = labels(tg, mid)
      expect(ls.find(l => l.includes('OpenAI 格式'))).toContain('✅')
      expect(ls.find(l => l.includes('Anthropic 格式'))).not.toContain('✅')
    })
  })

  test('点现在的格式：说「格式没变。」并回到「改什么？」', async () => {
    await withModels({ handler: openaiOk(IDS), before: SEEDED() }, async ({ tg }) => {
      const mid = await toWhat(tg)
      tg.clickButton(OWNER, OWNER, mid, '接口格式')
      const cur = await until(() => labels(tg, mid).find(l => l.includes('OpenAI 格式') && l.includes('✅')), '当前格式按钮')
      tg.clickButton(OWNER, OWNER, mid, cur)
      await waitText(tg, OWNER, mid, t => t.includes('改什么？'), '回到改什么')
      const said = [...tg.answers.map(a => a.text ?? ''), ...tg.edits.map(e => e.text ?? ''), ...tg.sentTo(OWNER).map(s => s.text ?? '')]
      expect(said.some(t => t.includes('格式没变。'))).toBe(true)
    })
  })

  test('OpenAI 改成 Anthropic：要求重新输入密钥，地址去掉末尾 /v1 并在结果里写「地址改成了 …」，epoch 变', async () => {
    await withModels({ handler: anthropicOk([['claude-a', 'claude-b']]), before: SEEDED() }, async ({ tg, b, fm, key }) => {
      const mid = await toWhat(tg)
      tg.clickButton(OWNER, OWNER, mid, '接口格式')
      await until(() => labels(tg, mid).includes('Anthropic 格式'), '格式按钮')
      const k = tg.sentTo(OWNER).length
      tg.clickButton(OWNER, OWNER, mid, 'Anthropic 格式')
      await until(() => [...tg.sentTo(OWNER).slice(k).map(s => s.text ?? ''), tg.textOf(OWNER, mid) ?? ''].some(t => t.includes(REKEY)), '要求重新输入密钥')
      tg.pushText(OWNER, key)
      const r = await waitSentText(tg, OWNER, '【系统】已更新「myproxy」的接口格式，拉到 2 个模型。', k, 20_000)
      expect(r.text!).toContain(`地址改成了 ${fm.url}`)
      expect(r.text!).toContain(ROLL_NOTE)
      const e = readProviders(b.root).providers.myproxy
      expect(e.route.api).toBe('anthropic-messages')
      expect(e.route.baseURL).toBe(fm.url)
      expect(e.meta.epoch).toBeGreaterThan(1_700_000_000_000)
    })
  })

  test('Anthropic 改成 OpenAI：问用「<原地址>/v1」还是重新输入，按钮「用 <原地址>/v1」「取消」', async () => {
    await withModels({ handler: openaiOk(IDS), before: SEEDED({ api: 'anthropic-messages' }) }, async ({ tg, fm }) => {
      const mid = await toWhat(tg)
      tg.clickButton(OWNER, OWNER, mid, '接口格式')
      await until(() => labels(tg, mid).includes('OpenAI 格式'), '格式按钮')
      tg.clickButton(OWNER, OWNER, mid, 'OpenAI 格式')
      const ask = `【系统】OpenAI 格式的地址一般以 /v1 结尾。用「${fm.url}/v1」，还是重新输入？`
      await until(() => tg.lastMenu(OWNER)?.text === ask, '问地址')
      expect(tg.lastMenu(OWNER)!.buttons.map(x => x.text)).toEqual([`用 ${fm.url}/v1`, '取消'])
    })
  })
})

describe('修改：并发', () => {
  test('拉列表期间供应商被别处删掉：回「「myproxy」已经不在了，这次修改没保存。」，不复活', async () => {
    const slow = async (r: any) => { await fsleep(2_000); return openaiOk(IDS)(r, 1, null as any) }
    await withModels({ handler: slow, before: SEEDED() }, async ({ tg, b, key }) => {
      const mid = await toWhat(tg)
      tg.clickButton(OWNER, OWNER, mid, '密钥')
      await waitText(tg, OWNER, mid, t => t.includes('请输入密钥'), '密钥步')
      const k = tg.sentTo(OWNER).length
      tg.pushText(OWNER, key)
      await sleep(600)
      seedProviders(b.root, [])
      await waitSentText(tg, OWNER, '【系统】「myproxy」已经不在了，这次修改没保存。', k, 20_000)
      expect(readProviders(b.root).providers.myproxy).toBeUndefined()
    })
  })
})
