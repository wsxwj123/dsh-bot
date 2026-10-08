// 验收：引导的取消、关闭、/cancel、重开、超时、网关重启、Telegram 出错（INTERFACE 3.3.4、3.3.5、3.11）
import { describe, expect, test } from 'bun:test'
import { DS_KEY, OWNER, Gateway, chatLog, evs, fakeKey, seedCreds, inboundHas, inboundRow, openProviderMenu, promptsText, sleep, toKeyStep, toNameStep, until, waitAnswer, waitEvent, waitMenu, waitOffset, waitSentText, waitText, withBot, withModels } from './_acc'
import { openaiOk, sleep as fsleep } from './fake-models'

const BYE = '【系统】已退出引导。之后的消息照常和角色聊天。'
const LATE = '【系统】这条消息像是密钥，没有交给角色，已帮你删除（没删掉的话请手动删）。需要的话重新发 /provider。'

describe('取消与关闭', () => {
  test('「取消」：菜单改为「已取消。」且没有按钮，回调答复「已退出引导」，日志 wizard.cancelled by=button', async () => {
    await withBot({}, async ({ tg, b }) => {
      const m = await toNameStep(tg)
      const cb = tg.clickButton(OWNER, OWNER, m.messageId, '取消')
      await waitText(tg, OWNER, m.messageId, t => t === '已取消。', '已取消')
      expect(tg.buttonsOf(OWNER, m.messageId)).toEqual([])
      expect((await waitAnswer(tg, cb)).text).toBe('已退出引导')
      await waitEvent(b, 'wizard.cancelled', e => e.by === 'button')
    })
  })

  test('「关闭」：菜单改为「已关闭。」且没有按钮', async () => {
    await withBot({}, async ({ tg }) => {
      const m = await openProviderMenu(tg)
      tg.clickButton(OWNER, OWNER, m.messageId, '关闭')
      await waitText(tg, OWNER, m.messageId, t => t === '已关闭。', '已关闭')
      expect(tg.buttonsOf(OWNER, m.messageId)).toEqual([])
    })
  })

  test('/cancel：菜单改为「已取消。」，回「已退出引导。之后的消息照常和角色聊天。」，日志 wizard.cancelled by=command', async () => {
    await withBot({}, async ({ tg, b }) => {
      const m = await toNameStep(tg)
      const k = tg.sentTo(OWNER).length
      tg.pushText(OWNER, '/cancel')
      await waitSentText(tg, OWNER, BYE, k)
      await waitText(tg, OWNER, m.messageId, t => t === '已取消。', '已取消')
      await waitEvent(b, 'wizard.cancelled', e => e.by === 'command')
    })
  })

  test('退出引导后，普通的话照常交给角色', async () => {
    await withBot({}, async ({ tg }) => {
      const m = await toNameStep(tg)
      tg.clickButton(OWNER, OWNER, m.messageId, '取消')
      await waitText(tg, OWNER, m.messageId, t => t === '已取消。', '已取消')
      const k = tg.sentTo(OWNER).length
      tg.pushText(OWNER, '今天天气不错')
      await waitSentText(tg, OWNER, '收到：今天天气不错', k)
    })
  })
})

describe('引导进行中收到的消息', () => {
  test('名字这一步的回答：不入账、不写 chat.log、不交给模型，日志 wizard.input 不含正文', async () => {
    await withBot({}, async ({ tg, b }) => {
      await toNameStep(tg)
      const mid = tg.pushText(OWNER, 'unique-name-7x')
      await waitMenu(tg, OWNER, m => m.text.includes('（第 2/4 步）'), '第 2 步')
      await waitOffset(b, tg.lastUpdateId)
      expect(inboundHas(b, OWNER, mid)).toBe(false)
      expect(chatLog(b)).not.toContain('unique-name-7x')
      expect(promptsText(b)).not.toContain('unique-name-7x')
      const ins = evs(b, 'wizard.input')
      expect(ins.length).toBeGreaterThan(0)
      expect(JSON.stringify(ins)).not.toContain('unique-name-7x')
    })
  })

  test('引导中发 /provider：结束旧引导（旧菜单去掉按钮），发一条新的主菜单', async () => {
    await withBot({}, async ({ tg }) => {
      const m = await toNameStep(tg)
      tg.pushText(OWNER, '/provider')
      const m2 = await waitMenu(tg, OWNER, x => x.text.startsWith('【系统】供应商（✅ 是现在用的）：') && x.messageId !== m.messageId, '新主菜单')
      expect(m2.messageId).not.toBe(m.messageId)
      expect(tg.buttonsOf(OWNER, m.messageId)).toEqual([])
    })
  })

  test('引导中发 /model：结束旧引导，开 /model 主菜单', async () => {
    await withBot({}, async ({ tg }) => {
      const m = await toNameStep(tg)
      tg.pushText(OWNER, '/model')
      await waitMenu(tg, OWNER, x => x.text.startsWith('【系统】现在用的模型：'), '/model 主菜单')
      expect(tg.buttonsOf(OWNER, m.messageId)).toEqual([])
    })
  })

  test('引导中发带参数的 /model deepseek-v4-pro：结束旧引导，这条照常作为命令入账并执行', async () => {
    await withBot({ before: b => seedCreds(b.root, DS_KEY) }, async ({ tg, b }) => {
      const m = await toNameStep(tg)
      const k = tg.sentTo(OWNER).length
      const mid = tg.pushText(OWNER, '/model deepseek-v4-pro')
      await waitSentText(tg, OWNER, '已换成 deepseek-official / deepseek-v4-pro', k)
      expect(tg.buttonsOf(OWNER, m.messageId)).toEqual([])
      expect(inboundRow(b, OWNER, mid)).not.toBeNull()
    })
  })

  test('引导中发别的 / 开头文字（/foo）：当作这一步的输入（名字不合格）', async () => {
    await withBot({}, async ({ tg }) => {
      await toNameStep(tg)
      const k = tg.sentTo(OWNER).length
      tg.pushText(OWNER, '/foo')
      await waitSentText(tg, OWNER, '【系统】名字只能用英文字母、数字、-、_，以字母或数字开头，最多 32 个字符。请重新输入：', k)
    })
  })

  test('引导中发语音（需要文字的步）：「这一步需要文字，请重新输入。」', async () => {
    await withBot({}, async ({ tg }) => {
      await toNameStep(tg)
      const k = tg.sentTo(OWNER).length
      tg.pushVoice(OWNER)
      await waitSentText(tg, OWNER, '【系统】这一步需要文字，请重新输入。', k)
    })
  })

  test('密钥这一步发 /cancel：这条也先删除，然后退出引导', async () => {
    await withModels({ handler: openaiOk(['m1']) }, async ({ tg, fm }) => {
      await toKeyStep(tg, 'myproxy', 'openai', `${fm.url}/v1`)
      const k = tg.sentTo(OWNER).length
      const mid = tg.pushText(OWNER, '/cancel')
      await waitSentText(tg, OWNER, BYE, k)
      expect(tg.isDeleted(OWNER, mid)).toBe(true)
    })
  })
})

describe('超时', () => {
  test('wizard_timeout_ms 内没操作：菜单按钮去掉、文字后加「（已过期）」，发退出提示，日志 wizard.timeout', async () => {
    await withBot({ gw: { wizard_timeout_ms: 1_200 } }, async ({ tg, b }) => {
      const m = await toNameStep(tg)
      await waitSentText(tg, OWNER, '【系统】已退出引导（1 分钟没有操作）。之后的消息照常和角色聊天。', 0, 10_000)
      expect(tg.textOf(OWNER, m.messageId)!.endsWith('（已过期）')).toBe(true)
      expect(tg.buttonsOf(OWNER, m.messageId)).toEqual([])
      await waitEvent(b, 'wizard.timeout')
    })
  })

  test('超时后点旧按钮：回过期提示', async () => {
    await withBot({ gw: { wizard_timeout_ms: 1_200 } }, async ({ tg }) => {
      const m = await toNameStep(tg)
      const data = m.buttons.find(x => x.text === '取消')!.data
      await waitSentText(tg, OWNER, '已退出引导（', 0, 10_000)
      const cb = tg.pushCallback(OWNER, OWNER, m.messageId, data)
      await until(() => (tg.answers.find(a => a.id === cb)?.text ?? '').includes('这个菜单已过期') || tg.sentTo(OWNER).some(s => (s.text ?? '').includes('这个菜单已过期')), '过期提示')
    })
  })

  test('每次有效操作都重新计时：一直在操作就不会超时', async () => {
    await withBot({ gw: { wizard_timeout_ms: 2_500 } }, async ({ tg }) => {
      await toNameStep(tg)
      await sleep(1_500)
      tg.pushText(OWNER, 'myproxy')
      const m2 = await waitMenu(tg, OWNER, x => x.text.includes('（第 2/4 步）'), '第 2 步')
      await sleep(1_500)
      tg.clickButton(OWNER, OWNER, m2.messageId, 'OpenAI 格式')
      await waitText(tg, OWNER, m2.messageId, t => t.includes('（第 3/4 步）'), '第 3 步')
      expect(tg.sentTo(OWNER).some(s => (s.text ?? '').includes('已退出引导（'))).toBe(false)
    })
  })
})

describe('网关重启', () => {
  test('没在处理中的引导：重启后照常续（接着输入名字进入第 2 步）', async () => {
    await withBot({}, async ({ tg, b, gw }) => {
      await toNameStep(tg)
      await gw.stop()
      const gw2 = new Gateway(b)
      await gw2.start()
      try {
        tg.pushText(OWNER, 'after-restart')
        await waitMenu(tg, OWNER, x => x.text.includes('「after-restart」（第 2/4 步）'), '重启后进入第 2 步')
      } finally { await gw2.stop() }
    })
  })

  test('重启后主人接着发的回答仍被拦下，不交给模型', async () => {
    await withBot({}, async ({ tg, b, gw }) => {
      await toNameStep(tg)
      await gw.stop()
      const gw2 = new Gateway(b)
      await gw2.start()
      try {
        tg.pushText(OWNER, 'after-restart')
        await waitMenu(tg, OWNER, x => x.text.includes('（第 2/4 步）'), '第 2 步')
        expect(promptsText(b)).not.toContain('after-restart')
      } finally { await gw2.stop() }
    })
  })

  test('保存途中网关被强杀：重启后回「网关重启打断了保存…」，开防护（随后像密钥的消息被删）', async () => {
    const slow = async (r: any) => { await fsleep(6_000); return openaiOk(['m1'])(r, 1, null as any) }
    await withModels({ handler: slow }, async ({ tg, b, gw, fm, key }) => {
      await toKeyStep(tg, 'myproxy', 'openai', `${fm.url}/v1`)
      tg.pushText(OWNER, key)
      await until(() => fm.requests.length > 0, '开始拉列表')
      await gw.kill()
      const gw2 = new Gateway(b)
      await gw2.start()
      try {
        await waitSentText(tg, OWNER, '【系统】网关重启打断了保存，不能确定「myproxy」和它的密钥是否都已写好。请发 /provider 查看（缺密钥会标出来），必要时用「修改」→「密钥」重新输入。', 0, 20_000)
        await waitEvent(b, 'wizard.interrupted')
        const k = tg.sentTo(OWNER).length
        tg.pushText(OWNER, fakeKey())
        await waitSentText(tg, OWNER, LATE, k)
      } finally { await gw2.stop() }
    })
  })
})

describe('Telegram 出错时', () => {
  test('editMessageText 报 message is not modified：当作成功，不另发新菜单', async () => {
    await withBot({}, async ({ tg, b }) => {
      const m = await openProviderMenu(tg)
      tg.faults.push({ method: 'editMessageText', description: 'Bad Request: message is not modified: specified new message content and reply markup are exactly the same as a current content and reply markup of the message' })
      const n = tg.sentTo(OWNER).length
      tg.clickButton(OWNER, OWNER, m.messageId, '新建')
      await sleep(1_500)
      expect(tg.sentTo(OWNER).length).toBe(n)
      expect(evs(b, 'wizard.edit_failed')).toEqual([])
    })
  })

  test('editMessageText 其它失败：改发一条新消息（同样文字和按钮）作为当前菜单，日志 wizard.edit_failed', async () => {
    await withBot({}, async ({ tg, b }) => {
      const m = await openProviderMenu(tg)
      tg.faults.push({ method: 'editMessageText', description: 'Bad Request: message to edit not found' })
      tg.clickButton(OWNER, OWNER, m.messageId, '新建')
      const m2 = await waitMenu(tg, OWNER, x => x.text.includes('（第 1/4 步）'), '新消息作为第 1 步')
      expect(m2.messageId).not.toBe(m.messageId)
      expect(m2.buttons.map(x => x.text)).toEqual(['取消'])
      await waitEvent(b, 'wizard.edit_failed')
    })
  })

  test('发主菜单遇到 429：按 retry_after 再试一次后发出', async () => {
    await withBot({}, async ({ tg }) => {
      tg.faults.push({ method: 'sendMessage', status: 429, description: 'Too Many Requests: retry after 1', retryAfter: 1, when: p => String(p.text ?? '').startsWith('【系统】供应商') })
      await openProviderMenu(tg)
    })
  })

  test('发菜单一直失败：引导结束（日志 wizard.send_failed）并开防护', async () => {
    await withBot({}, async ({ tg, b }) => {
      const m = await openProviderMenu(tg)
      tg.faults.push({ method: 'editMessageText', status: 500, description: 'Internal Server Error', times: 10 })
      tg.faults.push({ method: 'sendMessage', status: 500, description: 'Internal Server Error', times: 10, when: p => String(p.text ?? '').includes('第 1/4 步') })
      tg.clickButton(OWNER, OWNER, m.messageId, '新建')
      await waitEvent(b, 'wizard.send_failed', () => true, 20_000)
      tg.faults.length = 0
      const k = tg.sentTo(OWNER).length
      tg.pushText(OWNER, fakeKey())
      await waitSentText(tg, OWNER, LATE, k)
    })
  })

  test('answerCallbackQuery 失败：忽略，照常推进', async () => {
    await withBot({}, async ({ tg }) => {
      const m = await openProviderMenu(tg)
      tg.faults.push({ method: 'answerCallbackQuery', status: 400, description: 'Bad Request: query is too old and response timeout expired or query ID is invalid' })
      tg.clickButton(OWNER, OWNER, m.messageId, '新建')
      await waitText(tg, OWNER, m.messageId, t => t.includes('（第 1/4 步）'), '照常进入第 1 步')
    })
  })
})
