// 验收：「切到这家」/「切过去」（INTERFACE 3.4.6）与一行命令 /model <x>、/model default、/provider <名字>（3.6）
import { describe, expect, test } from 'bun:test'
import { DS_KEY, OWNER, Gateway, labels, openProviderMenu, override, seedCreds, seedProviders, waitSentText, waitText, withBot, type BotEnv } from './_acc'

const KEY = 'test-key-7switch000000000001'
const ROUTES = { myroute: { api: 'openai-completions', baseURL: 'https://r.example.com/v1', apiKeyEnv: 'MYROUTE_KEY', models: [{ id: 'r1', contextWindow: 65536 }] } }
const DONE = (p: string, m: string) => `【系统】已换成 ${p} / ${m}，下一条消息起生效。这个 bot 的所有聊天都换，重启后保持；/model default 换回配置文件里的。`
const ROLL = '换到了另一家供应商：下一条消息会先让现在的模型写一份交接摘要，再在新供应商上开新会话。'
const GUESS = '这个模型的上下文长度未知，按 131072 算；如果它实际更小，请在 /model →「管理模型」里改。'
const mine = (o: { models?: any[]; key?: boolean; extra?: any[] } = {}) => (b: BotEnv) => {
  seedProviders(b.root, [{ name: 'myproxy', baseURL: 'https://p.example.com/v1', models: o.models ?? ['m1', { id: 'm2', contextWindow: 64000 }] }, ...(o.extra ?? [])])
  seedCreds(b.root, o.key !== false ? { ...DS_KEY, PROVIDER_MYPROXY_KEY: KEY } : DS_KEY)
}

async function toPick(tg: any): Promise<number> {
  const m = await openProviderMenu(tg)
  tg.clickButton(OWNER, OWNER, m.messageId, '切到这家')
  await waitText(tg, OWNER, m.messageId, t => t === '【系统】切到哪家？（✅ 是现在用的）', '切到哪家')
  return m.messageId
}
const btn = (tg: any, mid: number, name: string) => labels(tg, mid).find((l: string) => l === name || l.endsWith(` ${name}`) || l.startsWith(`${name} `)) ?? name

async function toModels(tg: any, name: string): Promise<number> {
  const mid = await toPick(tg)
  tg.clickButton(OWNER, OWNER, mid, btn(tg, mid, name))
  await waitText(tg, OWNER, mid, t => t === `【系统】「${name}」的模型（✅ 是现在用的）：`, '模型列表')
  return mid
}

describe('切到这家（引导）', () => {
  test('第 1 步：列出有模型且启用的供应商（现在用的标 ✅），0 个模型的、配置有误的、被遮住的不列，末尾「取消」', async () => {
    await withBot({ brain: { routes: ROUTES }, before: mine({ extra: [{ name: 'empty0', baseURL: 'https://e.example.com/v1', models: [] }, { name: 'broken', api: 'grpc', baseURL: 'https://x.example.com/v1', models: ['x'] }, { name: 'MyRoute', baseURL: 'https://s.example.com/v1', models: ['s'] }] }) }, async ({ tg }) => {
      const mid = await toPick(tg)
      const ls = labels(tg, mid)
      expect(ls.find(l => l.includes('deepseek-official'))).toContain('✅')
      expect(ls.some(l => l.includes('myroute'))).toBe(true)
      expect(ls.some(l => l.includes('myproxy'))).toBe(true)
      for (const n of ['empty0', 'broken', 'MyRoute']) expect(ls.some(l => l.includes(n))).toBe(false)
      expect(ls[ls.length - 1]).toBe('取消')
    })
  })

  test('第 2 步：该供应商的模型按钮 +「返回」「取消」', async () => {
    await withBot({ before: mine() }, async ({ tg }) => {
      const mid = await toModels(tg, 'myproxy')
      expect(labels(tg, mid).filter(l => l !== '上一页' && l !== '下一页')).toEqual(['m1', 'm2', '返回', '取消'])
    })
  })

  test('第 2 步点「返回」：回到第 1 步', async () => {
    await withBot({ before: mine() }, async ({ tg }) => {
      const mid = await toModels(tg, 'myproxy')
      tg.clickButton(OWNER, OWNER, mid, '返回')
      await waitText(tg, OWNER, mid, t => t === '【系统】切到哪家？（✅ 是现在用的）', '回到第 1 步')
    })
  })

  test('点模型（换到另一家、上下文是猜的）：结果含成功句、换段说明、长度未知提醒，菜单不再有按钮', async () => {
    await withBot({ before: mine() }, async ({ tg }) => {
      const mid = await toModels(tg, 'myproxy')
      tg.clickButton(OWNER, OWNER, mid, 'm1')
      const t = await waitText(tg, OWNER, mid, x => x.startsWith('【系统】已换成'), '切换结果')
      expect(t).toContain(DONE('myproxy', 'm1'))
      expect(t).toContain(ROLL)
      expect(t).toContain(GUESS)
      expect(labels(tg, mid)).toEqual([])
    })
  })

  test('上下文长度已知的模型：结果不带长度未知提醒', async () => {
    await withBot({ before: mine() }, async ({ tg }) => {
      const mid = await toModels(tg, 'myproxy')
      tg.clickButton(OWNER, OWNER, mid, 'm2')
      const t = await waitText(tg, OWNER, mid, x => x.startsWith('【系统】已换成'), '切换结果')
      expect(t).not.toContain('上下文长度未知')
    })
  })

  test('切换后账本 brain_override 记下 provider 与 model', async () => {
    await withBot({ before: mine() }, async ({ tg, b }) => {
      const mid = await toModels(tg, 'myproxy')
      tg.clickButton(OWNER, OWNER, mid, 'm1')
      await waitText(tg, OWNER, mid, x => x.startsWith('【系统】已换成'), '切换结果')
      expect(override(b)).toMatchObject({ provider: 'myproxy', model: 'm1' })
    })
  })

  test('同一家里换模型：结果不带换段说明', async () => {
    await withBot({ before: b => seedCreds(b.root, DS_KEY) }, async ({ tg }) => {
      const mid = await toModels(tg, 'deepseek-official')
      tg.clickButton(OWNER, OWNER, mid, btn(tg, mid, 'deepseek-v4-pro'))
      const t = await waitText(tg, OWNER, mid, x => x.startsWith('【系统】已换成'), '切换结果')
      expect(t).toContain(DONE('deepseek-official', 'deepseek-v4-pro'))
      expect(t).not.toContain(ROLL)
    })
  })

  test('点现在用的模型：「现在用的就是 deepseek-official / deepseek-flash。」', async () => {
    await withBot({ before: b => seedCreds(b.root, DS_KEY) }, async ({ tg }) => {
      const mid = await toModels(tg, 'deepseek-official')
      tg.clickButton(OWNER, OWNER, mid, btn(tg, mid, 'deepseek-flash'))
      await waitText(tg, OWNER, mid, x => x === '【系统】现在用的就是 deepseek-official / deepseek-flash。', '已是当前')
    })
  })

  test('自建缺密钥：「myproxy 还没配密钥，先不换。点「修改」→「密钥」补上」，覆盖不变', async () => {
    await withBot({ before: mine({ key: false }) }, async ({ tg, b }) => {
      const mid = await toModels(tg, 'myproxy')
      tg.clickButton(OWNER, OWNER, mid, 'm1')
      const t = await waitText(tg, OWNER, mid, x => x.includes('还没配密钥'), '缺密钥')
      expect(t).toContain('【系统】myproxy 还没配密钥，先不换。')
      expect(t).toContain('点「修改」→「密钥」补上')
      expect(override(b)?.provider ?? '').not.toBe('myproxy')
    })
  })

  test('配置文件路由缺密钥：提示「凭据文件里填 MYROUTE_KEY」', async () => {
    await withBot({ brain: { routes: ROUTES } }, async ({ tg }) => {
      const mid = await toModels(tg, 'myroute')
      tg.clickButton(OWNER, OWNER, mid, 'r1')
      const t = await waitText(tg, OWNER, mid, x => x.includes('还没配密钥'), '缺密钥')
      expect(t).toContain('【系统】myroute 还没配密钥，先不换。')
      expect(t).toContain('凭据文件里填 MYROUTE_KEY')
    })
  })

  test('点模型前该模型被别处删了：「这个模型已经不在列表里了，请重新选。」并重新列出', async () => {
    await withBot({ before: mine() }, async ({ tg, b }) => {
      const mid = await toModels(tg, 'myproxy')
      seedProviders(b.root, [{ name: 'myproxy', baseURL: 'https://p.example.com/v1', models: ['m2'] }])
      await new Promise(r => setTimeout(r, 800))
      tg.clickButton(OWNER, OWNER, mid, 'm1')
      await waitText(tg, OWNER, mid, x => x.includes('【系统】这个模型已经不在列表里了，请重新选。'), '模型不在')
    })
  })

  test('点模型前整个供应商被删了：「「myproxy」已经不在了，请重新选。」回到第 1 步', async () => {
    await withBot({ before: mine() }, async ({ tg, b }) => {
      const mid = await toModels(tg, 'myproxy')
      seedProviders(b.root, [])
      await new Promise(r => setTimeout(r, 800))
      tg.clickButton(OWNER, OWNER, mid, 'm1')
      await waitText(tg, OWNER, mid, x => x.includes('【系统】「myproxy」已经不在了，请重新选。'), '供应商不在')
    })
  })

  test('切换后重启网关：覆盖保持（GET /v1/model 的 current 仍是 myproxy / m1）', async () => {
    await withBot({ before: mine() }, async ({ tg, b, gw }) => {
      const mid = await toModels(tg, 'myproxy')
      tg.clickButton(OWNER, OWNER, mid, 'm1')
      await waitText(tg, OWNER, mid, x => x.startsWith('【系统】已换成'), '切换结果')
      await gw.stop()
      const gw2 = new Gateway(b)
      await gw2.start()
      try {
        expect((await gw2.call('/v1/model')).json.current).toEqual({ provider: 'myproxy', model: 'm1' })
      } finally { await gw2.stop() }
    })
  })
})
/** 发一行命令，等以【系统】开头的回复文字 */
async function cmd(tg: any, text: string): Promise<string> {
  const k = tg.sentTo(OWNER).length
  tg.pushText(OWNER, text)
  return (await waitSentText(tg, OWNER, '【系统】', k)).text!
}

describe('一行命令：/model <x>', () => {
  test('/model m1（只有自建 myproxy 有）：唯一命中，换过去，带换段说明与长度未知提醒', async () => {
    await withBot({ before: mine() }, async ({ tg, b }) => {
      const t = await cmd(tg, '/model m1')
      expect(t).toContain('已换成 myproxy / m1，下一条消息起生效。这个 bot 的所有聊天都换，重启后保持；/model default 换回配置文件里的。')
      expect(t).toContain(ROLL)
      expect(t).toContain(GUESS)
      expect(override(b)).toMatchObject({ provider: 'myproxy', model: 'm1' })
    })
  })

  test('/model myproxy/m2：按「供应商/模型」换过去', async () => {
    await withBot({ before: mine() }, async ({ tg }) => {
      expect(await cmd(tg, '/model myproxy/m2')).toContain('已换成 myproxy / m2')
    })
  })

  test('/model MY_PROXY/m1：供应商名按规整名匹配', async () => {
    await withBot({ before: b => { seedProviders(b.root, [{ name: 'my-proxy', baseURL: 'https://p.example.com/v1', models: ['m1'] }]); seedCreds(b.root, { ...DS_KEY, PROVIDER_MY_PROXY_KEY: KEY }) } }, async ({ tg }) => {
      expect(await cmd(tg, '/model MY_PROXY/m1')).toContain('已换成 my-proxy / m1')
    })
  })

  test('/model nosuch：「没有 nosuch 这个模型。用 /model list 看能换哪些。」', async () => {
    await withBot({ before: mine() }, async ({ tg }) => {
      expect(await cmd(tg, '/model nosuch')).toContain('没有 nosuch 这个模型。用 /model list 看能换哪些。')
    })
  })

  test('多家都有同名模型：列出「供应商/模型」写法，不切换', async () => {
    await withBot({ before: b => { seedProviders(b.root, [{ name: 'pa', baseURL: 'https://a.example.com/v1', models: ['shared'] }, { name: 'pb', baseURL: 'https://b.example.com/v1', models: ['shared'] }]); seedCreds(b.root, { ...DS_KEY, PROVIDER_PA_KEY: KEY, PROVIDER_PB_KEY: KEY }) } }, async ({ tg, b }) => {
      const t = await cmd(tg, '/model shared')
      expect(t).toContain('有好几个供应商都有 shared，请写成"供应商/模型"：')
      expect(t).toContain('pa/shared')
      expect(t).toContain('pb/shared')
      expect(override(b)?.model ?? '').not.toBe('shared')
    })
  })

  test('/model <现在用的>：「现在用的就是 deepseek-official / deepseek-flash。」', async () => {
    await withBot({ before: b => seedCreds(b.root, DS_KEY) }, async ({ tg }) => {
      expect(await cmd(tg, '/model deepseek-flash')).toContain('现在用的就是 deepseek-official / deepseek-flash。')
    })
  })

  test('/model 到缺密钥的自建：不换，提示补密钥', async () => {
    await withBot({ before: mine({ key: false }) }, async ({ tg, b }) => {
      const t = await cmd(tg, '/model myproxy/m1')
      expect(t).toContain('myproxy 还没配密钥，先不换。')
      expect(override(b)?.provider ?? '').not.toBe('myproxy')
    })
  })

  test('/model list：列出自建供应商和它的模型；0 个模型的不列', async () => {
    await withBot({ before: mine({ extra: [{ name: 'empty0', baseURL: 'https://e.example.com/v1', models: [] }] }) }, async ({ tg }) => {
      const t = await cmd(tg, '/model list')
      expect(t).toContain('myproxy：\n  · m1\n  · m2')
      expect(t).not.toContain('empty0')
    })
  })

  test('/model default（有覆盖）：「已换回配置文件里的模型：deepseek-official / deepseek-flash，下一条消息起生效。」并清掉覆盖', async () => {
    await withBot({ before: mine() }, async ({ tg, b }) => {
      await cmd(tg, '/model myproxy/m1')
      expect(await cmd(tg, '/model default')).toContain('已换回配置文件里的模型：deepseek-official / deepseek-flash，下一条消息起生效。')
      const o = override(b)
      expect(o === null || !o.provider).toBe(true)
    })
  })

  test('/provider default（没有覆盖）：「现在用的就是配置文件里的模型：deepseek-official / deepseek-flash。」', async () => {
    await withBot({ before: b => seedCreds(b.root, DS_KEY) }, async ({ tg }) => {
      expect(await cmd(tg, '/provider default')).toContain('现在用的就是配置文件里的模型：deepseek-official / deepseek-flash。')
    })
  })
})

describe('一行命令：/provider <名字>', () => {
  test('/provider myproxy：换到它的第一个模型', async () => {
    await withBot({ before: mine() }, async ({ tg }) => {
      expect(await cmd(tg, '/provider myproxy')).toContain('已换成 myproxy / m1')
    })
  })

  test('/provider nosuch：「没有 nosuch 这个可用的供应商。用 /provider 看有哪些。」', async () => {
    await withBot({ before: b => seedCreds(b.root, DS_KEY) }, async ({ tg }) => {
      expect(await cmd(tg, '/provider nosuch')).toContain('没有 nosuch 这个可用的供应商。用 /provider 看有哪些。')
    })
  })

  test('/provider MyRoute（自建与配置文件路由同名）：按规整名命中配置文件路由 myroute，被遮住的自建不参与', async () => {
    await withBot({ brain: { routes: ROUTES }, before: b => { seedProviders(b.root, [{ name: 'MyRoute', baseURL: 'https://s.example.com/v1', models: ['s1'] }]); seedCreds(b.root, { ...DS_KEY, MYROUTE_KEY: KEY, PROVIDER_MYROUTE_KEY: KEY }) } }, async ({ tg }) => {
      const t = await cmd(tg, '/provider MyRoute')
      expect(t).toContain('已换成 myroute / r1')
      expect(t).not.toContain('s1')
    })
  })

  test('/provider <0 个模型的自建>：「没有 empty0 这个可用的供应商…」', async () => {
    await withBot({ before: b => seedProviders(b.root, [{ name: 'empty0', baseURL: 'https://e.example.com/v1', models: [] }]) }, async ({ tg }) => {
      expect(await cmd(tg, '/provider empty0')).toContain('没有 empty0 这个可用的供应商。用 /provider 看有哪些。')
    })
  })

  test('/provider deepseek-official（已是当前）：「现在用的就是 deepseek-official（模型 deepseek-flash）。」', async () => {
    await withBot({ before: b => seedCreds(b.root, DS_KEY) }, async ({ tg }) => {
      expect(await cmd(tg, '/provider deepseek-official')).toContain('现在用的就是 deepseek-official（模型 deepseek-flash）。')
    })
  })

  test('当前是自建时 /provider deepseek-official：配置文件用的就是它 → 换回配置文件的模型', async () => {
    await withBot({ before: mine() }, async ({ tg }) => {
      await cmd(tg, '/model myproxy/m1')
      expect(await cmd(tg, '/provider deepseek-official')).toContain('已换成 deepseek-official / deepseek-flash')
    })
  })
})
