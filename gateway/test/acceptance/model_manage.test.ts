// 验收：/model →「管理模型」（INTERFACE 3.5.2、3.5.3、3.3.2 分页与按钮文字）
import { describe, expect, test } from 'bun:test'
import { writeFileSync } from 'fs'
import { OWNER, labels, lockPath, openModelMenu, override, readProviders, seedCreds, seedProviders, until, waitMenu, waitText, withBot, type BotEnv, type SeedModel } from './_acc'

const KEY = 'test-key-7manage00000000001'
const seed = (models: SeedModel[] = ['m1', { id: 'm2', contextWindow: 64000 }], manual: string[] = [], name = 'myproxy') => (b: BotEnv) => {
  seedProviders(b.root, [{ name, baseURL: 'https://p.example.com/v1', models, manualModels: manual }])
  seedCreds(b.root, { [`PROVIDER_${name.toUpperCase().replace(/-/g, '_')}_KEY`]: KEY })
}
const texts = (tg: any) => [...tg.sentTo(OWNER).map((s: any) => s.text ?? ''), ...tg.edits.map((e: any) => e.text ?? '')] as string[]
const said = (tg: any, pred: (t: string) => boolean, what: string) => until(() => texts(tg).find(pred), what)
const entry = (b: BotEnv, name = 'myproxy') => readProviders(b.root).providers[name]

/** /model →「管理模型」→ 选供应商，停在管理页 */
async function toManage(tg: any, name = 'myproxy'): Promise<number> {
  const m = await openModelMenu(tg)
  tg.clickButton(OWNER, OWNER, m.messageId, '管理模型')
  await until(() => labels(tg, m.messageId).includes(name), '选自建供应商')
  tg.clickButton(OWNER, OWNER, m.messageId, name)
  await waitText(tg, OWNER, m.messageId, t => t.startsWith(`【系统】「${name}」的模型（`), '管理页')
  return m.messageId
}

/** 手动加一个模型；ctx = 'skip' 时点「跳过」。返回结果文字 */
async function addModel(tg: any, id: string, ctx: string): Promise<string> {
  const mid = await toManage(tg)
  tg.clickButton(OWNER, OWNER, mid, '手动加一个模型')
  await waitText(tg, OWNER, mid, t => t === '【系统】请输入模型名（对方接口里的模型 id，最多 200 个字符）：', '输入模型名')
  tg.pushText(OWNER, id)
  const m2 = await waitMenu(tg, OWNER, m => m.text === '【系统】上下文长度（token 数，比如 128000、128k、1.5m）；不知道就点「跳过」（按 131072 算）：', '输入上下文')
  if (ctx === 'skip') tg.clickButton(OWNER, OWNER, m2.messageId, '跳过')
  else tg.pushText(OWNER, ctx)
  return said(tg, t => t.startsWith(`【系统】已给「myproxy」加上模型 ${id}`) || t.startsWith('【系统】没保存成功'), '加模型结果')
}

describe('管理页', () => {
  test('没有自建供应商：「还没有自建供应商。」按钮「新建」「取消」', async () => {
    await withBot({}, async ({ tg }) => {
      const m = await openModelMenu(tg)
      tg.clickButton(OWNER, OWNER, m.messageId, '管理模型')
      await waitText(tg, OWNER, m.messageId, t => t === '【系统】还没有自建供应商。', '没有自建')
      expect(labels(tg, m.messageId)).toEqual(['新建', '取消'])
    })
  })

  test('管理页逐行列出模型与上下文（猜的写「未知，按 131072 算」），按钮齐全', async () => {
    await withBot({ before: seed() }, async ({ tg }) => {
      const mid = await toManage(tg)
      expect(tg.textOf(OWNER, mid)).toBe('【系统】「myproxy」的模型（2 个）：\nm1（上下文 131072，未知，按 131072 算）\nm2（上下文 64000）')
      expect(labels(tg, mid)).toEqual(['手动加一个模型', '删一个模型', '改上下文长度', '返回', '取消'])
    })
  })
})

describe('手动加一个模型', () => {
  test('输入模型名和 128k：回「已给「myproxy」加上模型 new-model（上下文 128000）。几秒后就能切过去。」', async () => {
    await withBot({ before: seed() }, async ({ tg }) => {
      expect(await addModel(tg, 'new-model', '128k')).toBe('【系统】已给「myproxy」加上模型 new-model（上下文 128000）。几秒后就能切过去。')
    })
  })

  test('加上后：providers.json 有这个模型，记入 manualModels 与 ownerContext', async () => {
    await withBot({ before: seed() }, async ({ tg, b }) => {
      await addModel(tg, 'new-model', '128k')
      const e = entry(b)
      expect(e.route.models).toContainEqual({ id: 'new-model', contextWindow: 128000 })
      expect(e.meta.manualModels).toContain('new-model')
      expect(e.meta.ownerContext).toContain('new-model')
    })
  })

  test('点「跳过」：按 131072 算，记入 guessedContext', async () => {
    await withBot({ before: seed() }, async ({ tg, b }) => {
      expect(await addModel(tg, 'skip-model', 'skip')).toContain('（上下文 131072）')
      expect(entry(b).meta.guessedContext).toContain('skip-model')
    })
  })

  for (const [inp, out] of [['128000', 128000], ['1.5m', 1500000], ['32K', 32000], ['1M', 1000000], ['1024', 1024], ['100000000', 100000000], ['1.0245k', 1025]] as const) {
    test(`上下文写「${inp}」：存为 ${out}`, async () => {
      await withBot({ before: seed() }, async ({ tg, b }) => {
        await addModel(tg, 'ctx-model', inp)
        expect(entry(b).route.models.find((m: any) => m.id === 'ctx-model')?.contextWindow).toBe(out)
      })
    })
  }

  for (const bad of ['1023', '100000001', 'abc', '-5', '0', '1.5', '128 k']) {
    test(`上下文写「${bad}」不合规：回原因并重问`, async () => {
      await withBot({ before: seed() }, async ({ tg }) => {
        const mid = await toManage(tg)
        tg.clickButton(OWNER, OWNER, mid, '手动加一个模型')
        await waitText(tg, OWNER, mid, t => t.includes('请输入模型名'), '输入模型名')
        tg.pushText(OWNER, 'ctx-model')
        await waitMenu(tg, OWNER, m => m.text.startsWith('【系统】上下文长度'), '输入上下文')
        const k = texts(tg).length
        tg.pushText(OWNER, bad)
        await until(() => texts(tg).slice(k).includes('【系统】上下文长度要是 1024 到 100000000 之间的整数，可以写 128k 或 1.5m。请重新输入：'), '不合规原因')
      })
    })
  }

  for (const bad of ['has space', 'x'.repeat(201), 'bad⟦id']) {   // 非 ASCII 现在合法（聚合商的 id 带中文前缀），改用含程序标记的样例
    test(`模型名「${bad.length > 20 ? bad.slice(0, 6) + '…(' + bad.length + ' 字)' : bad}」不合规：回原因并重问`, async () => {
      await withBot({ before: seed() }, async ({ tg }) => {
        const mid = await toManage(tg)
        tg.clickButton(OWNER, OWNER, mid, '手动加一个模型')
        await waitText(tg, OWNER, mid, t => t.includes('请输入模型名'), '输入模型名')
        const k = texts(tg).length
        tg.pushText(OWNER, bad)
        await until(() => texts(tg).slice(k).includes('【系统】模型名要 1–200 个字符，不能有空白、控制字符，也不能有 ⟦ ⟧。请重新输入：'), '不合规原因')
      })
    })
  }

  test('模型名已存在：「「myproxy」里已经有 m1 了。请重新输入：」', async () => {
    await withBot({ before: seed() }, async ({ tg }) => {
      const mid = await toManage(tg)
      tg.clickButton(OWNER, OWNER, mid, '手动加一个模型')
      await waitText(tg, OWNER, mid, t => t.includes('请输入模型名'), '输入模型名')
      const k = texts(tg).length
      tg.pushText(OWNER, 'm1')
      await until(() => texts(tg).slice(k).includes('【系统】「myproxy」里已经有 m1 了。请重新输入：'), '已存在')
    })
  })

  test('锁被占着：「没保存成功：别的 bot 正在改供应商，请稍后再试。」，文件不变', async () => {
    await withBot({ gw: { provider_lock_wait_ms: 300 }, before: seed() }, async ({ tg, b }) => {
      const before = JSON.stringify(readProviders(b.root))
      const mid = await toManage(tg)
      writeFileSync(lockPath(b.root), JSON.stringify({ pid: process.pid, at: Date.now() }))
      tg.clickButton(OWNER, OWNER, mid, '手动加一个模型')
      await waitText(tg, OWNER, mid, t => t.includes('请输入模型名'), '输入模型名')
      tg.pushText(OWNER, 'locked-model')
      const m2 = await waitMenu(tg, OWNER, m => m.text.startsWith('【系统】上下文长度'), '输入上下文')
      tg.clickButton(OWNER, OWNER, m2.messageId, '跳过')
      await said(tg, t => t.includes('【系统】没保存成功：别的 bot 正在改供应商，请稍后再试。'), '没保存成功')
      expect(JSON.stringify(readProviders(b.root))).toBe(before)
    })
  })
})
/** 管理页点某个操作，再点某个模型 */
async function pickModel(tg: any, action: string, id: string): Promise<number> {
  const mid = await toManage(tg)
  tg.clickButton(OWNER, OWNER, mid, action)
  await until(() => labels(tg, mid).includes(id), `模型按钮 ${id}`)
  tg.clickButton(OWNER, OWNER, mid, id)
  return mid
}

describe('删一个模型', () => {
  test('删拉到的模型：「已从「myproxy」删掉模型 m1。」并说明刷新时可能重新出现', async () => {
    await withBot({ before: seed() }, async ({ tg, b }) => {
      await pickModel(tg, '删一个模型', 'm1')
      const t = await said(tg, x => x.startsWith('【系统】已从「myproxy」删掉模型 m1。'), '删除结果')
      expect(t).toContain('下次「刷新模型」时，对方列表里还有的话它会重新出现。')
      expect(entry(b).route.models.map((m: any) => m.id)).toEqual(['m2'])
    })
  })

  test('删手动加的模型：不说「刷新时会重新出现」', async () => {
    await withBot({ before: seed(['m1', 'man1'], ['man1']) }, async ({ tg, b }) => {
      await pickModel(tg, '删一个模型', 'man1')
      const t = await said(tg, x => x.startsWith('【系统】已从「myproxy」删掉模型 man1。'), '删除结果')
      expect(t).not.toContain('重新出现')
      expect(entry(b).meta.manualModels).not.toContain('man1')
    })
  })

  test('删本 bot 正在用的模型：加「这个 bot 正在用它，已换回配置文件里的模型：deepseek-official / deepseek-flash。」并清覆盖', async () => {
    await withBot({ before: seed() }, async ({ tg, b }) => {
      const k = tg.sentTo(OWNER).length
      tg.pushText(OWNER, '/model myproxy/m2')
      await until(() => tg.sentTo(OWNER).slice(k).some(s => (s.text ?? '').includes('已换成 myproxy / m2')), '切过去')
      await pickModel(tg, '删一个模型', 'm2')
      const t = await said(tg, x => x.startsWith('【系统】已从「myproxy」删掉模型 m2。'), '删除结果')
      expect(t).toContain('这个 bot 正在用它，已换回配置文件里的模型：deepseek-official / deepseek-flash。')
      const o = override(b)
      expect(o === null || !o.provider).toBe(true)
    })
  })

  test('删完没有模型了：加「「solo」没有模型了，暂时不会出现在可选模型里。」', async () => {
    await withBot({ before: seed(['only1'], [], 'solo') }, async ({ tg }) => {
      const mid = await toManage(tg, 'solo')
      tg.clickButton(OWNER, OWNER, mid, '删一个模型')
      await until(() => labels(tg, mid).includes('only1'), '模型按钮')
      tg.clickButton(OWNER, OWNER, mid, 'only1')
      const t = await said(tg, x => x.startsWith('【系统】已从「solo」删掉模型 only1。'), '删除结果')
      expect(t).toContain('「solo」没有模型了，暂时不会出现在可选模型里。')
    })
  })

  test('模型列表分页：每页 8 个，有「下一页」，翻页后看到第 9–16 个', async () => {
    const ids = Array.from({ length: 20 }, (_, i) => `mod-${String(i).padStart(2, '0')}`)
    await withBot({ before: seed(ids) }, async ({ tg }) => {
      const mid = await toManage(tg)
      tg.clickButton(OWNER, OWNER, mid, '删一个模型')
      await until(() => labels(tg, mid).includes('mod-00'), '第一页')
      expect(labels(tg, mid).filter(l => l.startsWith('mod-'))).toEqual(ids.slice(0, 8))
      tg.clickButton(OWNER, OWNER, mid, '下一页')
      await until(() => labels(tg, mid).includes('mod-08'), '第二页')
      expect(labels(tg, mid).filter(l => l.startsWith('mod-'))).toEqual(ids.slice(8, 16))
    })
  })

  test('模型名超过 40 个字符：按钮保留前 18 个和后 18 个，中间写 …', async () => {
    const long = 'abcdefghijklmnopqr' + '-middle-part-that-is-long-' + 'stuvwxyz0123456789'
    await withBot({ before: seed([long, 'm2']) }, async ({ tg }) => {
      const mid = await toManage(tg)
      tg.clickButton(OWNER, OWNER, mid, '删一个模型')
      await until(() => labels(tg, mid).includes('m2'), '模型按钮')
      expect(labels(tg, mid)).toContain(`${long.slice(0, 18)}…${long.slice(-18)}`)
    })
  })

  test('模型名正好 40 个字符：按钮原样显示', async () => {
    const forty = 'x'.repeat(39) + '1'
    await withBot({ before: seed([forty, 'm2']) }, async ({ tg }) => {
      const mid = await toManage(tg)
      tg.clickButton(OWNER, OWNER, mid, '删一个模型')
      await until(() => labels(tg, mid).includes('m2'), '模型按钮')
      expect(labels(tg, mid)).toContain(forty)
    })
  })
})

describe('改上下文长度', () => {
  test('选上下文未知的模型：「m1 现在的上下文长度是 131072（未知，按 131072 算）。请输入新的…」', async () => {
    await withBot({ before: seed() }, async ({ tg }) => {
      const mid = await pickModel(tg, '改上下文长度', 'm1')
      await waitText(tg, OWNER, mid, t => t === '【系统】m1 现在的上下文长度是 131072（未知，按 131072 算）。请输入新的（比如 128000、128k、1.5m）：', '问新长度')
    })
  })

  test('选上下文已知的模型：提示里不写「未知」', async () => {
    await withBot({ before: seed() }, async ({ tg }) => {
      const mid = await pickModel(tg, '改上下文长度', 'm2')
      await waitText(tg, OWNER, mid, t => t === '【系统】m2 现在的上下文长度是 64000。请输入新的（比如 128000、128k、1.5m）：', '问新长度')
    })
  })

  test('输入 64k：「已把 m1 的上下文长度改成 64000。几秒后生效。」，记入 ownerContext、移出 guessedContext', async () => {
    await withBot({ before: seed() }, async ({ tg, b }) => {
      const mid = await pickModel(tg, '改上下文长度', 'm1')
      await waitText(tg, OWNER, mid, t => t.includes('请输入新的'), '问新长度')
      tg.pushText(OWNER, '64k')
      await said(tg, t => t === '【系统】已把 m1 的上下文长度改成 64000。几秒后生效。', '改好了')
      const e = entry(b)
      expect(e.route.models.find((m: any) => m.id === 'm1').contextWindow).toBe(64000)
      expect(e.meta.ownerContext).toContain('m1')
      expect(e.meta.guessedContext).not.toContain('m1')
    })
  })

  test('选好模型后它被别处删了：「「m1」已经不在了。」', async () => {
    await withBot({ before: seed() }, async ({ tg, b }) => {
      const mid = await pickModel(tg, '改上下文长度', 'm1')
      await waitText(tg, OWNER, mid, t => t.includes('请输入新的'), '问新长度')
      seedProviders(b.root, [{ name: 'myproxy', baseURL: 'https://p.example.com/v1', models: [{ id: 'm2', contextWindow: 64000 }] }])
      await new Promise(r => setTimeout(r, 600))
      tg.pushText(OWNER, '64k')
      await said(tg, t => t.includes('【系统】「m1」已经不在了。'), '已不在')
    })
  })
})
