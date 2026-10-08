// 验收：一行命令 /provider add（INTERFACE 3.6 专门约定，含密钥，从不进账本/chat.log/群聊记录/模型）
import { describe, expect, test } from 'bun:test'
import { FRIEND, GROUP, OWNER, evs, fakeKey, findSecret, inboundHas, providerAdd, readCreds, readProviders, seedProviders, sleep, toKeyStep, toNameStep, until, waitEvent, waitOffset, waitSentText, withModels } from './_acc'
import { anthropicOk, json, openaiOk, sleep as fsleep } from './fake-models'

const IDS = ['gpt-a', 'gpt-b', 'gpt-c']
const DELETED_NOTE = '（你发的密钥消息已删除。）'
const USAGE = '【系统】用法：/provider add <名字> <地址> <密钥> [openai|anthropic]（不写格式按 Anthropic）。'
const GROUP_ACCESS = { groups: { [String(GROUP)]: { requireMention: true, allowFrom: [] } } }
const noButtons = (s: any) => (s.replyMarkup?.inline_keyboard ?? []).flat().length === 0

describe('/provider add：主人私聊', () => {
  test('成功：删除这条消息，回「已添加供应商「myproxy」，拉到 3 个模型。」附删除说明，不带按钮', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, fm, key }) => {
      const { reply, msg } = await providerAdd(tg, `myproxy ${fm.url}/v1 ${key} openai`)
      expect(tg.isDeleted(OWNER, msg)).toBe(true)
      expect(reply.text!).toContain('【系统】已添加供应商「myproxy」，拉到 3 个模型。')
      expect(reply.text!).toContain(DELETED_NOTE)
      expect(noButtons(reply)).toBe(true)
    })
  })

  test('这条命令不入账、不写 chat.log、不交给模型', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, b, fm, key }) => {
      const { msg } = await providerAdd(tg, `myproxy ${fm.url}/v1 ${key} openai`)
      await waitOffset(b, tg.lastUpdateId)
      expect(inboundHas(b, OWNER, msg)).toBe(false)
      expect(findSecret(b, key)).toEqual([])
    })
  })

  test('不写第 4 个参数：按 Anthropic 格式保存并按 Anthropic 形拉列表', async () => {
    await withModels({ handler: anthropicOk([['claude-a']]) }, async ({ tg, b, fm, key }) => {
      const { reply } = await providerAdd(tg, `anth ${fm.url} ${key}`)
      expect(reply.text!).toContain('拉到 1 个模型')
      expect(readProviders(b.root).providers.anth.route.api).toBe('anthropic-messages')
      expect(fm.requests[0]!.authKind).toBe('x-api-key')
    })
  })

  for (const f of ['OpenAI', 'OPENAI']) {
    test(`第 4 个参数不分大小写（${f}）：按 OpenAI 格式保存`, async () => {
      await withModels({ handler: openaiOk(IDS) }, async ({ tg, b, fm, key }) => {
        await providerAdd(tg, `myproxy ${fm.url}/v1 ${key} ${f}`)
        expect(readProviders(b.root).providers.myproxy.route.api).toBe('openai-completions')
      })
    })
  }

  test('命令本身不分大小写（/PROVIDER ADD …）也按本条处理', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, b, fm, key }) => {
      const k = tg.sentTo(OWNER).length
      const msg = tg.pushText(OWNER, `/PROVIDER ADD myproxy ${fm.url}/v1 ${key} openai`)
      await waitSentText(tg, OWNER, '已添加供应商「myproxy」', k, 20_000)
      expect(tg.isDeleted(OWNER, msg)).toBe(true)
      expect(findSecret(b, key)).toEqual([])
    })
  })

  test('带 @本bot（/provider@test_dsh_bot add …）也按本条处理', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, fm, key }) => {
      const k = tg.sentTo(OWNER).length
      const msg = tg.pushText(OWNER, `/provider@test_dsh_bot add myproxy ${fm.url}/v1 ${key} openai`)
      await waitSentText(tg, OWNER, '已添加供应商「myproxy」', k, 20_000)
      expect(tg.isDeleted(OWNER, msg)).toBe(true)
    })
  })

  test('参数之间有多个空格：按空白切分照样成功', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, fm, key }) => {
      const { reply } = await providerAdd(tg, ` myproxy   ${fm.url}/v1    ${key}   openai `)
      expect(reply.text!).toContain('已添加供应商「myproxy」')
    })
  })

  test('第 4 个参数非法（grpc）：回「没添加：接口格式只能写 openai 或 anthropic」附删除说明，不发请求', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, b, fm, key }) => {
      const { reply, msg } = await providerAdd(tg, `myproxy ${fm.url}/v1 ${key} grpc`)
      expect(reply.text!).toContain('【系统】没添加：接口格式只能写 openai 或 anthropic。')
      expect(reply.text!).toContain(DELETED_NOTE)
      expect(tg.isDeleted(OWNER, msg)).toBe(true)
      expect(fm.requests.length).toBe(0)
      expect(readProviders(b.root)?.providers?.myproxy).toBeUndefined()
    })
  })

  for (const [what, args] of [['没有参数', ''], ['只有 2 个参数', 'myproxy https://x.example.com/v1'], ['有 5 个参数', 'myproxy https://x.example.com/v1 test-key-7abcdef12345 openai extra']] as const) {
    test(`${what}：回用法说明附删除说明，消息被删除`, async () => {
      await withModels({ handler: openaiOk(IDS) }, async ({ tg }) => {
        const { reply, msg } = await providerAdd(tg, args)
        expect(reply.text!).toContain(USAGE)
        expect(reply.text!).toContain(DELETED_NOTE)
        expect(tg.isDeleted(OWNER, msg)).toBe(true)
      })
    })
  }

  test('名字不合规：回「没添加：名字只能用…」，什么都不写', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, b, fm, key }) => {
      const { reply } = await providerAdd(tg, `bad.name ${fm.url}/v1 ${key} openai`)
      expect(reply.text!).toContain('【系统】没添加：名字只能用英文字母、数字、-、_，以字母或数字开头，最多 32 个字符。')
      expect(readCreds(b.root)).not.toContain(key)
      expect(fm.requests.length).toBe(0)
    })
  })

  test('名字是内置供应商（deepseek-official）：回「没添加：…不能用…」', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, fm, key }) => {
      const { reply } = await providerAdd(tg, `deepseek-official ${fm.url}/v1 ${key} openai`)
      expect(reply.text!).toContain('【系统】没添加：deepseek-official 是内置、配置文件里的供应商或保留字，不能用，换个名字。')
    })
  })

  test('地址是非本机的 http://：回「没添加：只有本机地址…」', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, key }) => {
      const { reply } = await providerAdd(tg, `myproxy http://example.com/v1 ${key} openai`)
      expect(reply.text!).toContain('【系统】没添加：只有本机地址（127.0.0.1、localhost、[::1]）可以用 http://，其它地址请用 https://。')
    })
  })

  test('密钥太短：回「没添加：…」，什么都不写、不发请求', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, b, fm }) => {
      const { reply } = await providerAdd(tg, `myproxy ${fm.url}/v1 ab12 openai`)
      expect(reply.text!.startsWith('【系统】没添加：')).toBe(true)
      expect(fm.requests.length).toBe(0)
      expect(readProviders(b.root)?.providers?.myproxy).toBeUndefined()
    })
  })

  test('与已有自建同名：不弹确认，直接更新，回「已更新供应商「my-proxy」…」', async () => {
    await withModels({ handler: openaiOk(IDS), before: b => seedProviders(b.root, [{ name: 'my-proxy', baseURL: 'https://old.example.com/v1', models: ['old'] }]) }, async ({ tg, b, fm, key }) => {
      const { reply } = await providerAdd(tg, `My_Proxy ${fm.url}/v1 ${key} openai`)
      expect(reply.text!).toContain('【系统】已更新供应商「')
      expect(Object.keys(readProviders(b.root).providers).length).toBe(1)
      expect((Object.values(readProviders(b.root).providers)[0] as any).route.baseURL).toBe(`${fm.url}/v1`)
    })
  })

  test('拉不到模型（401）：照样建好，回「但没拉到模型：密钥不对或没有权限（HTTP 401）」，不带按钮', async () => {
    await withModels({ handler: () => json({}, 401) }, async ({ tg, fm, key }) => {
      const { reply } = await providerAdd(tg, `myproxy ${fm.url}/v1 ${key} openai`)
      expect(reply.text!).toContain('【系统】已添加供应商「myproxy」，但没拉到模型：密钥不对或没有权限（HTTP 401）。')
      expect(noButtons(reply)).toBe(true)
    })
  })

  test('图片附言是 /provider add …：整条删除，附件不下载，不入账', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, b, fm, key }) => {
      const k = tg.sentTo(OWNER).length
      const msg = tg.pushPhoto(OWNER, `/provider add myproxy ${fm.url}/v1 ${key} openai`)
      await waitSentText(tg, OWNER, '已添加供应商「myproxy」', k, 20_000)
      expect(tg.isDeleted(OWNER, msg)).toBe(true)
      expect(tg.calls.some(c => c.method === 'getFile')).toBe(false)
      expect(inboundHas(b, OWNER, msg)).toBe(false)
    })
  })

  test('日志 command.provider_add ok=true', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, b, fm, key }) => {
      await providerAdd(tg, `myproxy ${fm.url}/v1 ${key} openai`)
      await waitEvent(b, 'command.provider_add', e => e.ok === true)
    })
  })
})
describe('/provider add：非主人与群里', () => {
  test('非主人私聊发：尝试删除，不回复，不入账，什么都不写', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, b, fm, key }) => {
      const msg = tg.pushText(FRIEND, `/provider add myproxy ${fm.url}/v1 ${key} openai`)
      await waitOffset(b, tg.lastUpdateId)
      await sleep(1_000)
      expect(tg.isDeleted(FRIEND, msg)).toBe(true)
      expect(tg.sentTo(FRIEND)).toEqual([])
      expect(inboundHas(b, FRIEND, msg)).toBe(false)
      expect(fm.requests.length).toBe(0)
      expect(readProviders(b.root)).toBeNull()
      expect(findSecret(b, key)).toEqual([])
    })
  })

  test('非主人私聊发：日志 command.not_owner', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, b, fm, key }) => {
      tg.pushText(FRIEND, `/provider add myproxy ${fm.url}/v1 ${key} openai`)
      await waitEvent(b, 'command.not_owner')
    })
  })

  test('群里主人 @本bot 发：尝试删除，回「只能在私聊里用…」，不保存', async () => {
    await withModels({ access: GROUP_ACCESS, handler: openaiOk(IDS) }, async ({ tg, b, fm, key }) => {
      const msg = tg.pushText(OWNER, `/provider@test_dsh_bot add myproxy ${fm.url}/v1 ${key} openai`, { chatId: GROUP })
      await waitSentText(tg, GROUP, '【系统】/provider add 只能在私聊里用。这条消息已尝试删除；没删掉的话请手动删除，并考虑换一个新密钥。')
      expect(tg.isDeleted(GROUP, msg)).toBe(true)
      expect(fm.requests.length).toBe(0)
      expect(readProviders(b.root)).toBeNull()
    })
  })

  test('群里的这条消息不入账、不进群聊记录，任何落盘文件里都没有密钥', async () => {
    await withModels({ access: GROUP_ACCESS, handler: openaiOk(IDS) }, async ({ tg, b, gw, fm, key }) => {
      const msg = tg.pushText(OWNER, `/provider@test_dsh_bot add myproxy ${fm.url}/v1 ${key} openai`, { chatId: GROUP })
      await waitSentText(tg, GROUP, '只能在私聊里用')
      await waitOffset(b, tg.lastUpdateId)
      expect(inboundHas(b, GROUP, msg)).toBe(false)
      expect(findSecret(b, key, { gw })).toEqual([])
    })
  })

  test('群里的日志 command.secret_in_group', async () => {
    await withModels({ access: GROUP_ACCESS, handler: openaiOk(IDS) }, async ({ tg, b, fm, key }) => {
      tg.pushText(OWNER, `/provider@test_dsh_bot add myproxy ${fm.url}/v1 ${key} openai`, { chatId: GROUP })
      await waitEvent(b, 'command.secret_in_group')
    })
  })

  test('群里主人没 @ 本 bot：只删不回', async () => {
    await withModels({ access: GROUP_ACCESS, handler: openaiOk(IDS) }, async ({ tg, b, fm, key }) => {
      const msg = tg.pushText(OWNER, `/provider add myproxy ${fm.url}/v1 ${key} openai`, { chatId: GROUP })
      await until(() => tg.isDeleted(GROUP, msg), '群消息被删除')
      await waitOffset(b, tg.lastUpdateId)
      await sleep(800)
      expect(tg.sentTo(GROUP)).toEqual([])
    })
  })

  test('群里非主人 @本bot 发：尝试删除，不回复', async () => {
    await withModels({ access: GROUP_ACCESS, handler: openaiOk(IDS) }, async ({ tg, b, fm, key }) => {
      const msg = tg.pushText(FRIEND, `/provider@test_dsh_bot add myproxy ${fm.url}/v1 ${key} openai`, { chatId: GROUP })
      await until(() => tg.isDeleted(GROUP, msg), '群消息被删除')
      await waitOffset(b, tg.lastUpdateId)
      await sleep(800)
      expect(tg.sentTo(GROUP)).toEqual([])
    })
  })

  test('群里 bot 不是管理员、删不掉：仍回「已尝试删除…」，密钥仍不落盘', async () => {
    await withModels({ access: GROUP_ACCESS, handler: openaiOk(IDS) }, async ({ tg, b, gw, fm, key }) => {
      tg.setNotAdmin(GROUP)
      const msg = tg.pushText(OWNER, `/provider@test_dsh_bot add myproxy ${fm.url}/v1 ${key} openai`, { chatId: GROUP })
      await waitSentText(tg, GROUP, '只能在私聊里用')
      expect(tg.isDeleted(GROUP, msg)).toBe(false)
      expect(findSecret(b, key, { gw })).toEqual([])
    })
  })

  test('群里图片附言是 /provider add …：同样删除，附件不下载', async () => {
    await withModels({ access: GROUP_ACCESS, handler: openaiOk(IDS) }, async ({ tg, b, fm, key }) => {
      const msg = tg.pushPhoto(OWNER, `/provider add myproxy ${fm.url}/v1 ${key} openai`, { chatId: GROUP })
      await until(() => tg.isDeleted(GROUP, msg), '群消息被删除')
      await waitOffset(b, tg.lastUpdateId)
      expect(tg.calls.some(c => c.method === 'getFile')).toBe(false)
      expect(findSecret(b, key)).toEqual([])
    })
  })
})

describe('/provider add：撞上进行中的引导', () => {
  test('引导第 1 步时发 /provider add：先结束引导（旧菜单去掉按钮，回「已退出引导。」），再照常添加', async () => {
    await withModels({ handler: openaiOk(IDS) }, async ({ tg, fm, key }) => {
      const m = await toNameStep(tg)
      const k = tg.sentTo(OWNER).length
      tg.pushText(OWNER, `/provider add myproxy ${fm.url}/v1 ${key} openai`)
      await waitSentText(tg, OWNER, '【系统】已退出引导。', k)
      await waitSentText(tg, OWNER, '已添加供应商「myproxy」', k, 20_000)
      expect(tg.buttonsOf(OWNER, m.messageId)).toEqual([])
    })
  })

  test('引导正在拉列表（处理中）时发 /provider add：只删除，回「正在处理，请稍候（你的消息已删除…）」，不保存它', async () => {
    const slow = async (r: any) => { await fsleep(2_500); return openaiOk(IDS)(r, 1, null as any) }
    await withModels({ handler: slow }, async ({ tg, b, fm, key }) => {
      await toKeyStep(tg, 'first', 'openai', `${fm.url}/v1`)
      tg.pushText(OWNER, key)
      await sleep(400)
      const k = tg.sentTo(OWNER).length
      const other = fakeKey()
      const msg = tg.pushText(OWNER, `/provider add second ${fm.url}/v1 ${other} openai`)
      await waitSentText(tg, OWNER, '【系统】正在处理，请稍候（你的消息已删除，其中的内容没有保存）。', k)
      expect(tg.isDeleted(OWNER, msg)).toBe(true)
      await waitSentText(tg, OWNER, '已添加供应商「first」', k, 20_000)
      await sleep(500)
      expect(readProviders(b.root).providers.second).toBeUndefined()
      expect(findSecret(b, other)).toEqual([])
      expect(readCreds(b.root)).not.toContain(other)
      expect(fm.requests.length).toBe(1)
      expect(evs(b, 'wizard.input', e => e.step === 'busy').length).toBeGreaterThan(0)
    })
  })
})
