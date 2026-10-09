// 验收：引导通用规矩——按钮回调的判定顺序（INTERFACE 3.3.3）、callback_data 形状（3.3.2）、菜单截断
import { describe, expect, test } from 'bun:test'
import { FRIEND, GROUP, OWNER, openProviderMenu, seedCreds, seedProviders, sleep, toKeyStep, toNameStep, until, waitAnswer, waitEvent, waitText, withBot, withModels, wizardCreate, type BotEnv } from './_acc'
import { openaiOk, sleep as fsleep } from './fake-models'

const STALE = '这个菜单已过期，请重新发 /provider 或 /model'
const parse = (data: string) => { const [w, token, act, idx] = data.split('|'); return { w, token: token!, act: act!, idx } }
/** 回调答复或 bot 发出的文字里出现了某句话 */
const answeredOrSaid = (tg: any, cb: string, text: string) =>
  until(() => (tg.answers.find((a: any) => a.id === cb)?.text ?? '').includes(text) || tg.sentTo(OWNER).some((s: any) => (s.text ?? '').includes(text)) || tg.sentTo(GROUP).some((s: any) => (s.text ?? '').includes(text)), `回应「${text}」`)
const seedTwo = (b: BotEnv) => { seedProviders(b.root, [{ name: 'p-one', baseURL: 'https://a.example.com/v1', models: ['a1'] }, { name: 'p-two', baseURL: 'https://b.example.com/v1', models: ['b1'] }]); seedCreds(b.root, { PROVIDER_P_ONE_KEY: 'test-key-7one0000000000', PROVIDER_P_TWO_KEY: 'test-key-7two0000000000' }) }

describe('按钮回调的判定', () => {
  test('别人点主人的菜单：不带文字的回调答复，菜单不变，日志 wizard.callback_rejected reason=not_owner', async () => {
    await withBot({}, async ({ tg, b }) => {
      const m = await openProviderMenu(tg)
      const data = m.buttons.find(x => x.text === '新建')!.data
      const cb = tg.pushCallback(FRIEND, OWNER, m.messageId, data)
      const a = await waitAnswer(tg, cb)
      expect(a.text ?? '').toBe('')
      await sleep(400)
      expect(tg.textOf(OWNER, m.messageId)).toBe(m.text)
      await waitEvent(b, 'wizard.callback_rejected', e => e.reason === 'not_owner')
    })
  })

  test('别人点了以后，主人自己点同一个按钮仍然有效', async () => {
    await withBot({}, async ({ tg }) => {
      const m = await openProviderMenu(tg)
      const data = m.buttons.find(x => x.text === '新建')!.data
      tg.pushCallback(FRIEND, OWNER, m.messageId, data)
      await sleep(400)
      tg.pushCallback(OWNER, OWNER, m.messageId, data)
      await waitText(tg, OWNER, m.messageId, t => t.includes('（第 1/4 步）'), '第 1 步')
    })
  })

  test('原消息已不可访问（message.date=0）：回过期提示，reason=inaccessible', async () => {
    await withBot({}, async ({ tg, b }) => {
      const m = await openProviderMenu(tg)
      const cb = tg.pushCallback(OWNER, OWNER, m.messageId, m.buttons[0]!.data, { inaccessible: true })
      await answeredOrSaid(tg, cb, STALE)
      await waitEvent(b, 'wizard.callback_rejected', e => e.reason === 'inaccessible')
    })
  })

  test('在群里点（不是私聊）：回过期提示，reason=inaccessible', async () => {
    await withBot({}, async ({ tg, b }) => {
      const m = await openProviderMenu(tg)
      const cb = tg.pushCallback(OWNER, GROUP, m.messageId, m.buttons[0]!.data)
      await answeredOrSaid(tg, cb, STALE)
      await waitEvent(b, 'wizard.callback_rejected', e => e.reason === 'inaccessible')
    })
  })

  for (const data of ['garbage', 'x|12345678|new', '']) {
    test(`data 不是 w|… 格式（${JSON.stringify(data)}）：回过期提示，reason=malformed`, async () => {
      await withBot({}, async ({ tg, b }) => {
        const m = await openProviderMenu(tg)
        const cb = tg.pushCallback(OWNER, OWNER, m.messageId, data)
        await answeredOrSaid(tg, cb, STALE)
        await waitEvent(b, 'wizard.callback_rejected', e => e.reason === 'malformed')
      })
    })
  }

  test('口令不对：回过期提示，reason=stale，菜单不变', async () => {
    await withBot({}, async ({ tg, b }) => {
      const m = await openProviderMenu(tg)
      const p = parse(m.buttons.find(x => x.text === '新建')!.data)
      const wrong = p.token === 'AAAAAAAA' ? 'BBBBBBBB' : 'AAAAAAAA'
      const cb = tg.pushCallback(OWNER, OWNER, m.messageId, `w|${wrong}|${p.act}`)
      await answeredOrSaid(tg, cb, STALE)
      await waitEvent(b, 'wizard.callback_rejected', e => e.reason === 'stale')
      expect(tg.textOf(OWNER, m.messageId)).toBe(m.text)
    })
  })

  test('点已经推进过的旧按钮（口令已作废）：回过期提示，reason=stale，不重复执行', async () => {
    await withBot({}, async ({ tg, b }) => {
      const m = await openProviderMenu(tg)
      const data = m.buttons.find(x => x.text === '新建')!.data
      tg.pushCallback(OWNER, OWNER, m.messageId, data)
      await waitText(tg, OWNER, m.messageId, t => t.includes('（第 1/4 步）'), '第 1 步')
      const edits = tg.edits.length
      const cb = tg.pushCallback(OWNER, OWNER, m.messageId, data)
      await answeredOrSaid(tg, cb, STALE)
      await waitEvent(b, 'wizard.callback_rejected', e => e.reason === 'stale')
      expect(tg.edits.slice(edits).filter(e => (e.text ?? '').includes('（第 1/4 步）')).length).toBe(0)
    })
  })

  test('引导已结束后点旧菜单：回过期提示，并尝试去掉那条旧消息的按钮', async () => {
    await withBot({}, async ({ tg }) => {
      const m1 = await openProviderMenu(tg)
      const old = m1.buttons.find(x => x.text === '修改')!.data
      tg.clickButton(OWNER, OWNER, m1.messageId, '关闭')
      await waitText(tg, OWNER, m1.messageId, t => t === '已关闭。', '已关闭')
      const cb = tg.pushCallback(OWNER, OWNER, m1.messageId, old)
      await answeredOrSaid(tg, cb, STALE)
      expect(tg.buttonsOf(OWNER, m1.messageId)).toEqual([])
    })
  })

  test('序号越界（列表只有 2 项却点第 99 项）：回过期提示，reason=stale', async () => {
    await withBot({ before: seedTwo }, async ({ tg, b }) => {
      const m = await openProviderMenu(tg)
      tg.clickButton(OWNER, OWNER, m.messageId, '修改')
      await waitText(tg, OWNER, m.messageId, t => t.includes('修改哪个自建供应商'), '修改哪个')
      const p = parse(tg.buttonsOf(OWNER, m.messageId).find(x => x.text === 'p-one')!.data)
      expect(p.idx).toBeDefined()
      const cb = tg.pushCallback(OWNER, OWNER, m.messageId, `w|${p.token}|${p.act}|99`)
      await answeredOrSaid(tg, cb, STALE)
      await waitEvent(b, 'wizard.callback_rejected', e => e.reason === 'stale')
    })
  })

  test('列表里的那一项已被别处删掉：回「该项已不存在，请重新选择」并重新列出', async () => {
    await withBot({ before: seedTwo }, async ({ tg, b }) => {
      const m = await openProviderMenu(tg)
      tg.clickButton(OWNER, OWNER, m.messageId, '修改')
      await waitText(tg, OWNER, m.messageId, t => t.includes('修改哪个自建供应商'), '修改哪个')
      seedProviders(b.root, [{ name: 'p-two', baseURL: 'https://b.example.com/v1', models: ['b1'] }])
      await sleep(600)
      const cb = tg.clickButton(OWNER, OWNER, m.messageId, 'p-one')
      await answeredOrSaid(tg, cb, '该项已不存在，请重新选择')
      await until(() => !tg.buttonsOf(OWNER, m.messageId).some(x => x.text === 'p-one') && tg.buttonsOf(OWNER, m.messageId).some(x => x.text === 'p-two'), '重新列出')
    })
  })

  test('正在拉列表时再点按钮：回「正在处理，请稍候」，不重复执行，reason=busy', async () => {
    const slow = async (r: any) => { await fsleep(2_500); return openaiOk(['m1'])(r, 1, null as any) }
    await withModels({ handler: slow }, async ({ tg, b, fm, key }) => {
      const m4 = await toKeyStep(tg, 'myproxy', 'openai', `${fm.url}/v1`)
      tg.pushText(OWNER, key)
      await sleep(500)
      const cb = tg.pushCallback(OWNER, OWNER, m4.messageId, m4.buttons.find(x => x.text === '取消')!.data)
      await answeredOrSaid(tg, cb, '正在处理，请稍候')
      await waitEvent(b, 'wizard.callback_rejected', e => e.reason === 'busy')
      await until(() => tg.sentTo(OWNER).some(s => (s.text ?? '').includes('已添加供应商「myproxy」')), '照常完成', 20_000)
      expect(fm.requests.length).toBe(1)
    })
  })

  test('每次点击只 answerCallbackQuery 一次，且在拉列表请求之前', async () => {
    const slow = async (r: any) => { await fsleep(800); return openaiOk(['m1'])(r, 1, null as any) }
    await withModels({ handler: slow, before: (b, fm) => { seedProviders(b.root, [{ name: 'myproxy', baseURL: `${fm.url}/v1`, models: ['m0'] }]); seedCreds(b.root, { PROVIDER_MYPROXY_KEY: fm.expectKey }) } }, async ({ tg, fm }) => {
      const m = await openProviderMenu(tg)
      tg.clickButton(OWNER, OWNER, m.messageId, '刷新模型')
      await waitText(tg, OWNER, m.messageId, t => t.includes('刷新哪个自建供应商'), '刷新哪个')
      const cb = tg.clickButton(OWNER, OWNER, m.messageId, 'myproxy')
      await waitText(tg, OWNER, m.messageId, t => t.includes('拉到 1 个模型'), '刷新结果', 20_000)
      const answers = tg.calls.filter(c => c.method === 'answerCallbackQuery' && String(c.params.callback_query_id) === cb)
      expect(answers.length).toBe(1)
      expect(answers[0]!.at).toBeLessThanOrEqual(fm.requests[0]!.at)
    })
  })
})

describe('callback_data 与菜单形状', () => {
  test('走一遍新建：bot 发出的每个按钮 data 都是 ASCII、≤40 字节、形如 w|<8 位口令>|<动作>[|<序号>]', async () => {
    await withModels({ handler: openaiOk(['m1']) }, async ({ tg, fm, key }) => {
      await wizardCreate(tg, { name: 'myproxy', fmt: 'openai', url: `${fm.url}/v1`, key })
      const all = [...tg.sent.map(s => s.replyMarkup), ...tg.edits.map(e => e.replyMarkup)].flatMap(mk => (mk?.inline_keyboard ?? []).flat()).map((x: any) => String(x.callback_data))
      expect(all.length).toBeGreaterThan(5)
      for (const d of all) {
        expect(d).toMatch(/^w\|[!-{}~]{8}\|[!-{}~]+(\|\d+)?$/)
        expect(new TextEncoder().encode(d).length).toBeLessThanOrEqual(40)
      }
    })
  })

  test('每次渲染换新口令：主菜单两次打开，口令不同', async () => {
    await withBot({}, async ({ tg }) => {
      const a = await openProviderMenu(tg)
      tg.clickButton(OWNER, OWNER, a.messageId, '关闭')
      await waitText(tg, OWNER, a.messageId, t => t === '已关闭。', '已关闭')
      const b2 = await openProviderMenu(tg)
      expect(parse(b2.buttons[0]!.data).token).not.toBe(parse(a.buttons[0]!.data).token)
    })
  })

  test('名字和模型名不进 callback_data', async () => {
    const long = 'very-long-model-name-' + 'z'.repeat(60)
    await withBot({ before: b => { seedProviders(b.root, [{ name: 'Secret_Name_X', baseURL: 'https://a.example.com/v1', models: [long] }]); seedCreds(b.root, { PROVIDER_SECRET_NAME_X_KEY: 'test-key-7abc000000000' }) } }, async ({ tg }) => {
      const m = await openProviderMenu(tg)
      tg.clickButton(OWNER, OWNER, m.messageId, '切到这家')
      await until(() => tg.buttonsOf(OWNER, m.messageId).some(x => x.text.includes('Secret_Name_X')), '供应商按钮')
      tg.clickButton(OWNER, OWNER, m.messageId, tg.buttonsOf(OWNER, m.messageId).find(x => x.text.includes('Secret_Name_X'))!.text)
      await until(() => tg.buttonsOf(OWNER, m.messageId).some(x => x.text.startsWith('very-long-model')), '模型按钮')
      const datas = [...tg.sent.map(s => s.replyMarkup), ...tg.edits.map(e => e.replyMarkup)].flatMap(mk => (mk?.inline_keyboard ?? []).flat()).map((x: any) => String(x.callback_data))
      for (const d of datas) { expect(d).not.toContain('Secret_Name_X'); expect(d).not.toContain('very-long-model') }
    })
  })

  test('管理页模型很多（文字超过 3500 字）：截断并在末尾写「…还有 <k> 个」，消息不超过 Telegram 上限', async () => {
    const ids = Array.from({ length: 60 }, (_, i) => `model-${String(i).padStart(2, '0')}-` + 'q'.repeat(120))
    await withBot({ before: b => { seedProviders(b.root, [{ name: 'many', baseURL: 'https://m.example.com/v1', models: ids }]); seedCreds(b.root, { PROVIDER_MANY_KEY: 'test-key-7many00000000' }) } }, async ({ tg }) => {
      tg.pushText(OWNER, '/model')
      const m = await until(() => tg.lastMenu(OWNER)?.buttons.some(x => x.text === '管理模型') ? tg.lastMenu(OWNER) : null, '/model 主菜单')
      tg.clickButton(OWNER, OWNER, m.messageId, '管理模型')
      await until(() => tg.buttonsOf(OWNER, m.messageId).some(x => x.text === 'many'), '选供应商')
      tg.clickButton(OWNER, OWNER, m.messageId, 'many')
      const t = await waitText(tg, OWNER, m.messageId, x => x.startsWith('【系统】「many」的模型（60 个）：'), '管理页')
      expect(t.length).toBeLessThanOrEqual(3600)
      expect(t).toMatch(/…还有 \d+ 个$/)
    })
  })

  test('网关拉 Telegram 更新时声明了 callback_query（否则按钮点击收不到）', async () => {
    await withBot({}, async ({ tg }) => {
      await toNameStep(tg)
      expect(tg.lastAllowedUpdates).toContain('callback_query')
      expect(tg.lastAllowedUpdates).toContain('message')
    })
  })
})

