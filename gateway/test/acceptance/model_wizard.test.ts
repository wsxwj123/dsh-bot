// 验收：/model 引导（INTERFACE 3.5）——主菜单、思考强度、换回配置文件里的（D5、D7）
import { describe, expect, test } from 'bun:test'
import { DS_KEY, OWNER, chatPrompts, configCalls, inboundHas, labels, openModelMenu, override, prompts, seedCreds, seedProviders, sleep, waitMenu, waitOffset, waitSentText, waitText, withBot, type BotEnv } from './_acc'

const KEY = 'test-key-7modelwiz0000000001'
const mine = (b: BotEnv) => {
  seedProviders(b.root, [{ name: 'myproxy', baseURL: 'https://p.example.com/v1', models: ['m1', { id: 'm2', contextWindow: 64000 }] }])
  seedCreds(b.root, { ...DS_KEY, PROVIDER_MYPROXY_KEY: KEY })
}
const ds = (b: BotEnv) => seedCreds(b.root, DS_KEY)
const noCheck = (l: string) => l.replace(/✅\s*/g, '').replace(/\s*✅/g, '').trim()
async function cmd(tg: any, text: string) {
  const k = tg.sentTo(OWNER).length
  tg.pushText(OWNER, text)
  return (await waitSentText(tg, OWNER, '【系统】', k)).text!
}
async function toEffort(tg: any): Promise<number> {
  const m = await openModelMenu(tg)
  tg.clickButton(OWNER, OWNER, m.messageId, '思考强度')
  await waitText(tg, OWNER, m.messageId, t => t.startsWith('【系统】思考强度（现在：') || t.includes('不支持调思考强度') || t.includes('暂时问不到'), '思考强度页')
  return m.messageId
}

describe('/model 主菜单', () => {
  test('没有覆盖：第一行写现在的模型和思考档，写「来自配置文件。」和一行命令提示', async () => {
    await withBot({ before: ds }, async ({ tg }) => {
      const m = await openModelMenu(tg)
      const lines = m.text.split('\n')
      expect(lines[0]).toBe('【系统】现在用的模型：deepseek-official / deepseek-flash（思考 low）')
      expect(lines).toContain('来自配置文件。')
      expect(lines[lines.length - 1]).toBe('看能换哪些：/model list；换模型：/model <模型名>。')
    })
  })

  test('按钮是「切换模型」「思考强度」「管理自建供应商的模型」「换回配置文件里的」「关闭」', async () => {
    await withBot({ before: ds }, async ({ tg }) => {
      const m = await openModelMenu(tg)
      expect(labels(tg, m.messageId)).toEqual(['切换模型', '思考强度', '管理自建供应商的模型', '换回配置文件里的', '关闭'])
    })
  })

  test('有覆盖：写「这是用命令换的；配置文件里是 deepseek-official / deepseek-flash，/model default 换回去。」', async () => {
    await withBot({ before: mine }, async ({ tg }) => {
      await cmd(tg, '/model myproxy/m2')
      const m = await openModelMenu(tg)
      expect(m.text).toContain('这是用命令换的；配置文件里是 deepseek-official / deepseek-flash，/model default 换回去。')
      expect(m.text).not.toContain('来自配置文件。')
    })
  })

  test('主人私聊的 /model（无参数）不入账、不交给模型', async () => {
    await withBot({ before: ds }, async ({ tg, b }) => {
      const mid = tg.pushText(OWNER, '/model')
      await waitMenu(tg, OWNER, m => m.text.startsWith('【系统】现在用的模型：'), '/model 主菜单')
      await waitOffset(b, tg.lastUpdateId)
      expect(inboundHas(b, OWNER, mid)).toBe(false)
      expect(chatPrompts(b).some(p => p.text.includes('/model'))).toBe(false)
    })
  })

  test('「切换模型」：进入「切到哪家？」', async () => {
    await withBot({ before: ds }, async ({ tg }) => {
      const m = await openModelMenu(tg)
      tg.clickButton(OWNER, OWNER, m.messageId, '切换模型')
      await waitText(tg, OWNER, m.messageId, t => t === '【系统】切到哪家？（✅ 是现在用的）', '切到哪家')
    })
  })
})

describe('思考强度', () => {
  test('内置模型：列出「关」「低」「高」「最高」（按 dsh 报出的顺序，去掉空档），现在的「低」标 ✅，加「返回」「取消」', async () => {
    await withBot({ before: ds }, async ({ tg }) => {
      const mid = await toEffort(tg)
      expect(tg.textOf(OWNER, mid)).toBe('【系统】思考强度（现在：低）：')
      const ls = labels(tg, mid)
      expect(ls.map(noCheck)).toEqual(['关', '低', '高', '最高', '返回', '取消'])
      expect(ls.find(l => noCheck(l) === '低')).toContain('✅')
      expect(ls.find(l => noCheck(l) === '高')).not.toContain('✅')
    })
  })

  test('点「高」：回改好了（不换新会话），覆盖记下 effort=high', async () => {
    await withBot({ before: ds }, async ({ tg, b }) => {
      const mid = await toEffort(tg)
      tg.clickButton(OWNER, OWNER, mid, labels(tg, mid).find(l => noCheck(l) === '高')!)
      await waitText(tg, OWNER, mid, t => t === '【系统】思考强度已改成「高」，下一条消息起生效（不换新会话）。这个 bot 的所有聊天都改，重启后保持。', '改好了')
      expect(override(b)).toMatchObject({ provider: 'deepseek-official', model: 'deepseek-flash', effort: 'high' })
    })
  })

  test('改强度后下一轮：同一个会话，设了 reasoning_effort=high', async () => {
    await withBot({ before: ds }, async ({ tg, b }) => {
      const k = tg.sentTo(OWNER).length
      tg.pushText(OWNER, '第一句话')
      await waitSentText(tg, OWNER, '收到：第一句话', k)
      const mid = await toEffort(tg)
      tg.clickButton(OWNER, OWNER, mid, labels(tg, mid).find(l => noCheck(l) === '高')!)
      await waitText(tg, OWNER, mid, t => t.includes('思考强度已改成「高」'), '改好了')
      const k2 = tg.sentTo(OWNER).length
      tg.pushText(OWNER, '第二句话')
      await waitSentText(tg, OWNER, '收到：第二句话', k2)
      const ps = chatPrompts(b)
      expect(ps.find(p => p.text.includes('第二句话'))!.sessionId).toBe(ps.find(p => p.text.includes('第一句话'))!.sessionId)
      expect(configCalls(b).some(c => c.configId === 'reasoning_effort' && c.value === 'high' && c.ok !== false)).toBe(true)
    })
  })

  test('点现在的档：「现在就是「低」。」', async () => {
    await withBot({ before: ds }, async ({ tg }) => {
      const mid = await toEffort(tg)
      tg.clickButton(OWNER, OWNER, mid, labels(tg, mid).find(l => noCheck(l) === '低')!)
      await waitText(tg, OWNER, mid, t => t === '【系统】现在就是「低」。', '已是当前')
    })
  })

  test('自建模型（没声明档位）：「现在的模型（myproxy / m1）不支持调思考强度。自建供应商的模型默认不声明思考档位。」按钮「返回」「关闭」', async () => {
    await withBot({ before: mine }, async ({ tg }) => {
      await cmd(tg, '/model myproxy/m1')
      const mid = await toEffort(tg)
      expect(tg.textOf(OWNER, mid)).toContain('【系统】现在的模型（myproxy / m1）不支持调思考强度。')
      expect(tg.textOf(OWNER, mid)).toContain('自建供应商的模型默认不声明思考档位。')
      expect(labels(tg, mid)).toEqual(['返回', '关闭'])
    })
  })

  test('问档位不发模型请求（假 dsh 没收到新的一轮）', async () => {
    await withBot({ before: ds }, async ({ tg, b }) => {
      const n = prompts(b).length
      await toEffort(tg)
      await sleep(500)
      expect(prompts(b).length).toBe(n)
    })
  })

  test('改过强度后再换模型：覆盖里不再有 effort（回到配置文件里的强度，D7）', async () => {
    await withBot({ before: ds }, async ({ tg, b }) => {
      const mid = await toEffort(tg)
      tg.clickButton(OWNER, OWNER, mid, labels(tg, mid).find(l => noCheck(l) === '最高')!)
      await waitText(tg, OWNER, mid, t => t.includes('思考强度已改成「最高」'), '改好了')
      await cmd(tg, '/model deepseek-v4-pro')
      const o = override(b)
      expect(o).toMatchObject({ provider: 'deepseek-official', model: 'deepseek-v4-pro' })
      expect(o?.effort ?? '').toBe('')
    })
  })
})

describe('换回配置文件里的', () => {
  test('有覆盖：「已换回配置文件里的模型：deepseek-official / deepseek-flash（思考 low），下一条消息起生效。」并清掉覆盖', async () => {
    await withBot({ before: ds }, async ({ tg, b }) => {
      await cmd(tg, '/model deepseek-v4-pro')
      const m = await openModelMenu(tg)
      tg.clickButton(OWNER, OWNER, m.messageId, '换回配置文件里的')
      await waitText(tg, OWNER, m.messageId, t => t.startsWith('【系统】已换回配置文件里的模型：deepseek-official / deepseek-flash（思考 low），下一条消息起生效。'), '已换回')
      const o = override(b)
      expect(o === null || !o.provider).toBe(true)
    })
  })

  test('覆盖的是另一家：结果加换段说明', async () => {
    await withBot({ before: mine }, async ({ tg }) => {
      await cmd(tg, '/model myproxy/m2')
      const m = await openModelMenu(tg)
      tg.clickButton(OWNER, OWNER, m.messageId, '换回配置文件里的')
      const t = await waitText(tg, OWNER, m.messageId, x => x.startsWith('【系统】已换回配置文件里的模型'), '已换回')
      expect(t).toContain('换到了另一家供应商：下一条消息会先让现在的模型写一份交接摘要，再在新供应商上开新会话。')
    })
  })

  test('没有覆盖：「现在用的就是配置文件里的模型：deepseek-official / deepseek-flash。」', async () => {
    await withBot({ before: ds }, async ({ tg }) => {
      const m = await openModelMenu(tg)
      tg.clickButton(OWNER, OWNER, m.messageId, '换回配置文件里的')
      await waitText(tg, OWNER, m.messageId, t => t === '【系统】现在用的就是配置文件里的模型：deepseek-official / deepseek-flash。', '已是配置文件的')
    })
  })
})

