// 验收：密钥不外泄（INTERFACE 3.12，log_level=debug 下字节级全文搜索）与迟到密钥防护（3.3.5、BRIEF D3）
import { describe, expect, test } from 'bun:test'
import { FRIEND, OWNER, credPath, fakeKey, findSecret, inboundHas, inboundRow, openProviderMenu, promptsText, providerAdd, sleep, toKeyStep, waitEvent, waitOffset, waitSentText, waitText, withModels, wizardCreate } from './_acc'
import { json, openaiOk } from './fake-models'

const IDS = ['gpt-a', 'gpt-b']
const LATE = '【系统】这条消息像是密钥，没有交给角色，已帮你删除（没删掉的话请手动删）。需要的话重新发 /provider。'

describe('密钥只出现在凭据文件里', () => {
  test('引导新建成功后：账本(含 wal/shm)、日志、假 dsh 记录、providers.json、Telegram 发出的请求、网关输出里都没有密钥', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, b, gw, fm, key }) => {
      await wizardCreate(tg, { name: 'myproxy', fmt: 'openai', url: `${fm.url}/v1`, key })
      await sleep(800)
      expect(findSecret(b, key, { gw })).toEqual([])
      expect(await Bun.file(credPath(b.root)).text()).toContain(key)
    })
  })

  test('/provider add 成功后：除凭据文件外任何地方都没有密钥', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, b, gw, fm, key }) => {
      await providerAdd(tg, `myproxy ${fm.url}/v1 ${key} openai`)
      await sleep(800)
      expect(findSecret(b, key, { gw })).toEqual([])
    })
  })

  test('对方 401 回包正文里原样带着密钥：不转发给主人、不写日志、任何落盘文件里都没有', async () => {
    const handler = async (_r: any, _n: number, raw: Request) => json({ error: { message: `Incorrect API key provided: ${(raw.headers.get('authorization') ?? '').replace('Bearer ', '')}` } }, 401)
    await withModels({ handler }, async ({ tg, b, gw, fm, key }) => {
      const { result } = await wizardCreate(tg, { name: 'myproxy', fmt: 'openai', url: `${fm.url}/v1`, key })
      expect(result.text!).not.toContain('Incorrect API key')
      await sleep(800)
      expect(findSecret(b, key, { gw })).toEqual([])
    })
  })

  test('拉列表失败时的结果文字不含接口地址', async () => {
    await withModels({ handler: () => json({}, 500) }, async ({ tg, fm, key }) => {
      const { result } = await wizardCreate(tg, { name: 'myproxy', fmt: 'openai', url: `${fm.url}/v1`, key })
      expect(result.text!).toContain('对方接口出错（HTTP 500）')
      expect(result.text!).not.toContain(fm.url)
    })
  })

  test('新建后「切过去」并聊一轮：交给模型的内容里没有密钥，回复正常', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, b, gw, fm, key }) => {
      const { result } = await wizardCreate(tg, { name: 'myproxy', fmt: 'openai', url: `${fm.url}/v1`, key })
      tg.clickButton(OWNER, OWNER, result.messageId, '切过去')
      await waitText(tg, OWNER, result.messageId, t => t.includes('「myproxy」的模型'), '模型列表')
      tg.clickButton(OWNER, OWNER, result.messageId, 'gpt-a')
      await waitText(tg, OWNER, result.messageId, t => t.includes('已换成 myproxy / gpt-a'), '切换结果')
      const k = tg.sentTo(OWNER).length
      tg.pushText(OWNER, '你好呀')
      await waitSentText(tg, OWNER, '收到：你好呀', k, 30_000)
      expect(promptsText(b)).not.toContain(key)
      expect(findSecret(b, key, { gw })).toEqual([])
    })
  })

  test('GET /v1/model 的 providers 里不含密钥和接口地址', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, gw, fm, key }) => {
      await wizardCreate(tg, { name: 'myproxy', fmt: 'openai', url: `${fm.url}/v1`, key })
      const r = await gw.call('/v1/model')
      expect(r.status).toBe(200)
      expect(r.text).not.toContain(key)
      expect(r.text).not.toContain(fm.url)
      expect(r.text).not.toContain(`127.0.0.1:${fm.server.port}`)
    })
  })

  test('密钥消息没删掉、主人引用回复它：入账的引用文字里密钥换成 ***，任何地方都没有密钥', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, b, gw, fm, key }) => {
      await toKeyStep(tg, 'myproxy', 'openai', `${fm.url}/v1`)
      const k = tg.sentTo(OWNER).length
      const keyMsg = tg.pushText(OWNER, key)
      tg.markTooOld(OWNER, keyMsg)
      await waitSentText(tg, OWNER, '已添加供应商「myproxy」', k, 20_000)
      const k2 = tg.sentTo(OWNER).length
      const mid = tg.pushText(OWNER, '刚才那条你看到了吗', { replyTo: { message_id: keyMsg, text: key } })
      await waitSentText(tg, OWNER, '收到：', k2, 20_000)
      expect(JSON.stringify(inboundRow(b, OWNER, mid))).toContain('***')
      expect(findSecret(b, key, { gw })).toEqual([])
    })
  })
})
/** 开引导、点「新建」、再点「取消」（非正常结束 → 开迟到密钥防护） */
async function startAndCancel(tg: any): Promise<void> {
  const m = await openProviderMenu(tg)
  tg.clickButton(OWNER, OWNER, m.messageId, '新建')
  await waitText(tg, OWNER, m.messageId, t => t.includes('（第 1/4 步）'), '第 1 步')
  tg.clickButton(OWNER, OWNER, m.messageId, '取消')
  await waitText(tg, OWNER, m.messageId, t => t === '已取消。', '已取消')
}

const GW = (o: Record<string, unknown> = {}) => ({ late_secret_window_ms: 600_000, ...o })

describe('迟到密钥防护（引导非正常结束后 10 分钟内）', () => {
  test('点「取消」后，主人单独发来像密钥的一个词：删除，回提示', async () => {
    await withModels({ gw: GW(), handler: openaiOk(IDS) }, async ({ tg }) => {
      await startAndCancel(tg)
      const k = tg.sentTo(OWNER).length
      const mid = tg.pushText(OWNER, fakeKey())
      await waitSentText(tg, OWNER, LATE, k)
      expect(tg.isDeleted(OWNER, mid)).toBe(true)
    })
  })

  test('防护拦下的消息：不入账、不交给模型、不写 chat.log', async () => {
    await withModels({ gw: GW(), handler: openaiOk(IDS) }, async ({ tg, b }) => {
      await startAndCancel(tg)
      const s = fakeKey()
      const k = tg.sentTo(OWNER).length
      const mid = tg.pushText(OWNER, s)
      await waitSentText(tg, OWNER, LATE, k)
      await waitOffset(b, tg.lastUpdateId)
      expect(inboundHas(b, OWNER, mid)).toBe(false)
      expect(promptsText(b)).not.toContain(s)
      expect(findSecret(b, s)).toEqual([])
    })
  })

  test('防护拦下时日志 wizard.late_secret_dropped deleted=true', async () => {
    await withModels({ gw: GW(), handler: openaiOk(IDS) }, async ({ tg, b }) => {
      await startAndCancel(tg)
      tg.pushText(OWNER, fakeKey())
      await waitEvent(b, 'wizard.late_secret_dropped', e => e.deleted === true)
    })
  })

  test('发 /cancel 退出后同样有防护', async () => {
    await withModels({ gw: GW(), handler: openaiOk(IDS) }, async ({ tg }) => {
      const m = await openProviderMenu(tg)
      tg.clickButton(OWNER, OWNER, m.messageId, '新建')
      await waitText(tg, OWNER, m.messageId, t => t.includes('（第 1/4 步）'), '第 1 步')
      const k0 = tg.sentTo(OWNER).length
      tg.pushText(OWNER, '/cancel')
      await waitSentText(tg, OWNER, '【系统】已退出引导。之后的消息照常和角色聊天。', k0)
      const k = tg.sentTo(OWNER).length
      tg.pushText(OWNER, fakeKey())
      await waitSentText(tg, OWNER, LATE, k)
    })
  })

  test('超时退出后同样有防护', async () => {
    await withModels({ gw: GW({ wizard_timeout_ms: 1_200 }), handler: openaiOk(IDS) }, async ({ tg }) => {
      const m = await openProviderMenu(tg)
      tg.clickButton(OWNER, OWNER, m.messageId, '新建')
      await waitText(tg, OWNER, m.messageId, t => t.includes('（第 1/4 步）'), '第 1 步')
      await waitSentText(tg, OWNER, '【系统】已退出引导（1 分钟没有操作）。之后的消息照常和角色聊天。', 0, 10_000)
      const k = tg.sentTo(OWNER).length
      tg.pushText(OWNER, fakeKey())
      await waitSentText(tg, OWNER, LATE, k)
    })
  })

  test('8 个字符、字母加数字（abcd1234）：算像密钥，被拦下（下限边界）', async () => {
    await withModels({ gw: GW(), handler: openaiOk(IDS) }, async ({ tg }) => {
      await startAndCancel(tg)
      const k = tg.sentTo(OWNER).length
      tg.pushText(OWNER, 'abcd1234')
      await waitSentText(tg, OWNER, LATE, k)
    })
  })

  for (const [what, text] of [['7 个字符', 'abc1234'], ['没有数字', 'hahahahahaha'], ['没有字母', '1234567890123'], ['是网址', 'https://example.com/abc123def'], ['不止一个词', 'abc12345 def67890'], ['有中文', '我的密码abc12345']] as const) {
    test(`防护窗口内发来${what}的文字（${text}）：照常交给角色`, async () => {
      await withModels({ gw: GW(), handler: openaiOk(IDS) }, async ({ tg }) => {
        await startAndCancel(tg)
        const k = tg.sentTo(OWNER).length
        const mid = tg.pushText(OWNER, text)
        await waitSentText(tg, OWNER, `收到：${text}`, k)
        expect(tg.isDeleted(OWNER, mid)).toBe(false)
      })
    })
  }

  test('防护窗口过了以后（late_secret_window_ms=1500）：像密钥的词照常交给角色', async () => {
    await withModels({ gw: GW({ late_secret_window_ms: 1_500 }), handler: openaiOk(IDS) }, async ({ tg }) => {
      await startAndCancel(tg)
      await sleep(2_500)
      const s = fakeKey()
      const k = tg.sentTo(OWNER).length
      tg.pushText(OWNER, s)
      await waitSentText(tg, OWNER, `收到：${s}`, k)
    })
  })

  test('点「关闭」正常结束：不开防护，像密钥的词照常交给角色', async () => {
    await withModels({ gw: GW(), handler: openaiOk(IDS) }, async ({ tg }) => {
      const m = await openProviderMenu(tg)
      tg.clickButton(OWNER, OWNER, m.messageId, '关闭')
      await waitText(tg, OWNER, m.messageId, t => t === '已关闭。', '已关闭')
      const s = fakeKey()
      const k = tg.sentTo(OWNER).length
      tg.pushText(OWNER, s)
      await waitSentText(tg, OWNER, `收到：${s}`, k)
    })
  })

  test('新建正常完成：不开防护', async () => {
    await withModels({ gw: GW(), handler: openaiOk(IDS) }, async ({ tg, fm, key }) => {
      await wizardCreate(tg, { name: 'myproxy', fmt: 'openai', url: `${fm.url}/v1`, key })
      const s = fakeKey()
      const k = tg.sentTo(OWNER).length
      tg.pushText(OWNER, s)
      await waitSentText(tg, OWNER, `收到：${s}`, k, 20_000)
    })
  })

  test('防护只管主人的私聊：别人私聊发像密钥的词照常交给角色', async () => {
    await withModels({ gw: GW(), handler: openaiOk(IDS) }, async ({ tg }) => {
      await startAndCancel(tg)
      const s = fakeKey()
      tg.pushText(FRIEND, s)
      await waitSentText(tg, FRIEND, `收到：${s}`)
    })
  })

  test('防护窗口内主人发 /cancel：被接走，不交给模型、不入账', async () => {
    await withModels({ gw: GW(), handler: openaiOk(IDS) }, async ({ tg, b }) => {
      await startAndCancel(tg)
      const mid = tg.pushText(OWNER, '/cancel')
      await waitOffset(b, tg.lastUpdateId)
      await sleep(800)
      expect(inboundHas(b, OWNER, mid)).toBe(false)
      expect(promptsText(b)).not.toContain('/cancel')
    })
  })
})

