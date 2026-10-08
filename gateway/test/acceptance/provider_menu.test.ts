// 验收：/provider 主菜单与列表文字、谁能用、在哪用（INTERFACE 3.3.1、3.4.1、3.6）
import { describe, expect, test } from 'bun:test'
import { writeFileSync } from 'fs'
import { FRIEND, GROUP, OWNER, chatLog, promptsText, evs, fakeKey, inboundHas, inboundRow, providersPath, seedCreds, seedProviders, sleep, waitEvent, waitOffset, waitSent, waitSentText, withBot } from './_acc'

const MENU_HEAD = '【系统】供应商（✅ 是现在用的）：'
const MAIN_BUTTONS = ['新建', '修改', '删除', '刷新模型', '切到这家', '关闭']
const lineOf = (t: string, name: string) => t.split('\n').find(l => l.includes(` ${name}：`)) ?? ''
const GROUP_ACCESS = { groups: { [String(GROUP)]: { requireMention: true, allowFrom: [] } } }
const ROUTE = { myroute: { api: 'openai-completions', baseURL: 'https://route.example.com/v1', apiKeyEnv: 'MYROUTE_KEY', models: [{ id: 'r1', contextWindow: 65536 }] } }

async function openMenu(tg: any, b: any): Promise<{ text: string; mid: number; sent: any }> {
  const n0 = tg.sentTo(OWNER).length
  const mid = tg.pushText(OWNER, '/provider')
  const sent = await waitSentText(tg, OWNER, MENU_HEAD, n0)
  return { text: sent.text!, mid, sent }
}

describe('/provider 主菜单（主人私聊）', () => {
  test('主人私聊发 /provider：发出一条新消息，开头是供应商列表标题', async () => {
    await withBot({}, async ({ tg, b }) => {
      const { sent } = await openMenu(tg, b)
      expect(sent.method).toBe('sendMessage')
      expect(sent.text!.startsWith(MENU_HEAD)).toBe(true)
    })
  })

  test('主菜单按钮正好是「新建」「修改」「删除」「刷新模型」「切到这家」「关闭」', async () => {
    await withBot({}, async ({ tg }) => {
      const { sent } = await openMenu(tg, null)
      expect(tg.buttonsOf(OWNER, sent.messageId).map(x => x.text)).toEqual(MAIN_BUTTONS)
    })
  })

  test('内置供应商一行：标 ✅（现在用的）、模型数取 dsh 回报（假 dsh 有 3 个）、DeepSeek 官方、内置', async () => {
    await withBot({}, async ({ tg }) => {
      const { text } = await openMenu(tg, null)
      const l = lineOf(text, 'deepseek-official')
      expect(l.startsWith('✅ deepseek-official：')).toBe(true)
      expect(l).toContain('3 个模型')
      expect(l).toContain('DeepSeek 官方')
      expect(l).toContain('内置')
    })
  })

  test('自建供应商一行：· 名字：N 个模型，密钥已配，OpenAI 格式，自建；密钥值不出现', async () => {
    const key = fakeKey()
    await withBot({ before: b => { seedProviders(b.root, [{ name: 'myproxy', baseURL: 'https://proxy.example.com/v1', models: ['m-a', 'm-b'] }]); seedCreds(b.root, { PROVIDER_MYPROXY_KEY: key }) } }, async ({ tg }) => {
      const { text } = await openMenu(tg, null)
      expect(lineOf(text, 'myproxy')).toBe('· myproxy：2 个模型，密钥已配，OpenAI 格式，自建')
      expect(text).not.toContain(key)
    })
  })

  test('自建供应商 0 个模型：写明「未生效：先「刷新模型」或手动加」', async () => {
    await withBot({ before: b => { seedProviders(b.root, [{ name: 'empty1', baseURL: 'https://e.example.com/v1', models: [] }]); seedCreds(b.root, { PROVIDER_EMPTY1_KEY: fakeKey() }) } }, async ({ tg }) => {
      const { text } = await openMenu(tg, null)
      expect(lineOf(text, 'empty1')).toContain('0 个模型（未生效：先「刷新模型」或手动加）')
    })
  })

  test('自建供应商缺密钥：标「缺密钥（点「修改」→「密钥」补上）」', async () => {
    await withBot({ before: b => { seedProviders(b.root, [{ name: 'nokey', baseURL: 'https://n.example.com/v1', models: ['x1'] }]) } }, async ({ tg }) => {
      const { text } = await openMenu(tg, null)
      expect(lineOf(text, 'nokey')).toContain('缺密钥（点「修改」→「密钥」补上）')
    })
  })

  test('配置文件路由缺密钥：标「缺密钥（凭据文件里填 <键名>）」，来源写配置文件', async () => {
    await withBot({ brain: { routes: ROUTE } }, async ({ tg }) => {
      const { text } = await openMenu(tg, null)
      const l = lineOf(text, 'myroute')
      expect(l).toContain('缺密钥（凭据文件里填 MYROUTE_KEY）')
      expect(l).toContain('配置文件')
      expect(l).toContain('OpenAI 格式')
    })
  })

  test('列表顺序：内置 → 配置文件 → 自建（自建按名字排）', async () => {
    await withBot({ brain: { routes: ROUTE }, before: b => { seedProviders(b.root, [{ name: 'zeta', baseURL: 'https://z.example.com/v1', models: ['z'] }, { name: 'alpha', baseURL: 'https://a.example.com/v1', models: ['a'] }]) } }, async ({ tg }) => {
      const { text } = await openMenu(tg, null)
      const idx = ['deepseek-official', 'myroute', 'alpha', 'zeta'].map(n => text.split('\n').findIndex(l => l.includes(` ${n}：`)))
      expect(idx.every(i => i > 0)).toBe(true)
      expect([...idx].sort((x, y) => x - y)).toEqual(idx)
    })
  })

  test('Anthropic 格式的自建供应商一行写「Anthropic 格式」', async () => {
    await withBot({ before: b => { seedProviders(b.root, [{ name: 'anth', api: 'anthropic-messages', baseURL: 'https://anth.example.com', models: ['claude-x'] }]) } }, async ({ tg }) => {
      const { text } = await openMenu(tg, null)
      expect(lineOf(text, 'anth')).toContain('Anthropic 格式')
    })
  })

  test('自建与本 bot 配置文件路由规整名相同：列表标同名未启用，日志 providers.shadowed', async () => {
    await withBot({ brain: { routes: ROUTE }, before: b => { seedProviders(b.root, [{ name: 'MyRoute', baseURL: 'https://other.example.com/v1', models: ['o1'] }]) } }, async ({ tg, b }) => {
      const { text } = await openMenu(tg, null)
      expect(text).toContain('和本 bot 配置文件里的路由同名，未启用')
      await waitEvent(b, 'providers.shadowed')
    })
  })

  test('providers.json 里一个条目接口格式不认识：列表标「配置有误，未启用（…）」，日志 providers.entry_skipped', async () => {
    await withBot({ before: b => { seedProviders(b.root, [{ name: 'badapi', api: 'grpc' as any, baseURL: 'https://bad.example.com/v1', models: ['m'] }]) } }, async ({ tg, b }) => {
      const { text } = await openMenu(tg, null)
      expect(lineOf(text, 'badapi')).toContain('配置有误，未启用（')
      await waitEvent(b, 'providers.entry_skipped', e => e.name === 'badapi')
    })
  })

  test('providers.json 格式坏了：主菜单第二行起写明共用供应商文件读不了', async () => {
    await withBot({ before: b => writeFileSync(providersPath(b.root), '{ 这不是 JSON') }, async ({ tg, b }) => {
      const { text } = await openMenu(tg, null)
      expect(text).toContain('⚠️ 共用供应商文件读不了（格式坏了），自建供应商暂时都不可用，也不能新建/修改/删除。请修好 ')
      expect(text.split('\n')[0]).toBe(MENU_HEAD)
      expect(evs(b, 'providers.reload_failed').length).toBeGreaterThan(0)
    })
  })

  test('主人私聊的 /provider 在收消息层被接走：inbound 无行、tg_offset 推进', async () => {
    await withBot({}, async ({ tg, b }) => {
      const { mid } = await openMenu(tg, b)
      await waitOffset(b, tg.lastUpdateId)
      expect(inboundHas(b, OWNER, mid)).toBe(false)
    })
  })

  test('主人私聊的 /provider 不写 chat.log、不加 👀、不交给模型', async () => {
    await withBot({}, async ({ tg, b }) => {
      const { mid } = await openMenu(tg, b)
      await sleep(300)
      expect(chatLog(b)).not.toContain('/provider')
      expect(tg.reactions.filter(r => r.messageId === mid)).toEqual([])
      expect(promptsText(b)).not.toContain('/provider')
    })
  })
})
describe('谁能用、在哪用', () => {
  for (const cmd of ['/provider', '/model']) {
    test(`非主人私聊发 ${cmd}：不回复，账本该行 dropped / not owner`, async () => {
      await withBot({}, async ({ tg, b }) => {
        const mid = tg.pushText(FRIEND, cmd)
        await waitOffset(b, tg.lastUpdateId)
        await sleep(800)
        expect(tg.sentTo(FRIEND)).toEqual([])
        const row = inboundRow(b, FRIEND, mid)
        expect(row?.state).toBe('dropped')
        expect(row?.note).toBe('not owner')
      })
    })

    test(`非主人私聊发 ${cmd}：日志 command.not_owner，不交给模型`, async () => {
      await withBot({}, async ({ tg, b }) => {
        tg.pushText(FRIEND, cmd)
        await waitEvent(b, 'command.not_owner')
        await sleep(500)
        expect(promptsText(b)).not.toContain(cmd)
      })
    })
  }

  test('群里主人发 /provider@本bot：回纯文字列表，不带按钮', async () => {
    await withBot({ access: GROUP_ACCESS }, async ({ tg }) => {
      tg.pushText(OWNER, '/provider@test_dsh_bot', { chatId: GROUP })
      const s = await waitSentText(tg, GROUP, MENU_HEAD)
      expect((s.replyMarkup?.inline_keyboard ?? []).flat().length).toBe(0)
    })
  })

  test('群里主人发 /model@本bot：回现在用的模型的文字，不带按钮', async () => {
    await withBot({ access: GROUP_ACCESS }, async ({ tg }) => {
      tg.pushText(OWNER, '/model@test_dsh_bot', { chatId: GROUP })
      const s = await waitSentText(tg, GROUP, '【系统】现在用的模型：deepseek-official / deepseek-flash')
      expect((s.replyMarkup?.inline_keyboard ?? []).flat().length).toBe(0)
    })
  })

  test('群里非主人发 /provider@本bot：不回复，账本 dropped / not owner', async () => {
    await withBot({ access: GROUP_ACCESS }, async ({ tg, b }) => {
      const mid = tg.pushText(FRIEND, '/provider@test_dsh_bot', { chatId: GROUP })
      await waitOffset(b, tg.lastUpdateId)
      await sleep(800)
      expect(tg.sentTo(GROUP)).toEqual([])
      const row = inboundRow(b, GROUP, mid)
      expect(row?.state).toBe('dropped')
      expect(row?.note).toBe('not owner')
    })
  })

  test('群里的 /provider list 去掉「凭据文件里填…」提示（私聊里才写）', async () => {
    await withBot({ access: GROUP_ACCESS, brain: { routes: ROUTE } }, async ({ tg }) => {
      tg.pushText(OWNER, '/provider@test_dsh_bot list', { chatId: GROUP })
      const s = await waitSentText(tg, GROUP, MENU_HEAD)
      expect(lineOf(s.text!, 'myroute')).toContain('缺密钥')
      expect(s.text!).not.toContain('凭据文件里填')
    })
  })

  for (const sub of ['list', 'status']) {
    test(`私聊 /provider ${sub}：回列表文字，不带按钮`, async () => {
      await withBot({}, async ({ tg }) => {
        tg.pushText(OWNER, `/provider ${sub}`)
        const s = await waitSentText(tg, OWNER, MENU_HEAD)
        expect((s.replyMarkup?.inline_keyboard ?? []).flat().length).toBe(0)
      })
    })
  }
})

describe('/help 与 /cancel', () => {
  test('/help 同时写按钮引导和一行命令，含 /provider add、/provider refresh、/cancel', async () => {
    await withBot({}, async ({ tg }) => {
      tg.pushText(OWNER, '/help')
      const s = await waitSent(tg, OWNER, x => (x.text ?? '').includes('/provider add'), '/help 回复')
      expect(s.text!).toContain('/provider add <名字> <地址> <密钥> [openai|anthropic]')
      expect(s.text!).toContain('/provider refresh <名字>')
      expect(s.text!).toContain('/cancel')
      expect(s.text!).toContain('按钮')
    })
  })

  test('/provider help 的回复和 /help 完全相同', async () => {
    await withBot({}, async ({ tg }) => {
      tg.pushText(OWNER, '/help')
      const a = await waitSent(tg, OWNER, x => (x.text ?? '').includes('/provider add'), '/help 回复')
      const n = tg.sentTo(OWNER).length
      tg.pushText(OWNER, '/provider help')
      const c = await waitSent(tg, OWNER, x => (x.text ?? '').includes('/provider add'), '/provider help 回复', n)
      expect(c.text).toBe(a.text)
    })
  })

  test('/commands 也写了 /provider add 的用法', async () => {
    await withBot({}, async ({ tg }) => {
      tg.pushText(OWNER, '/commands')
      await waitSent(tg, OWNER, x => (x.text ?? '').includes('/provider add <名字> <地址> <密钥>'), '/commands 回复')
    })
  })

  test('主人在没有引导、没有防护窗口时发 /cancel：当普通文字交给模型', async () => {
    await withBot({}, async ({ tg, b }) => {
      tg.pushText(OWNER, '/cancel')
      await waitSentText(tg, OWNER, '收到：/cancel')
      expect(promptsText(b)).toContain('/cancel')
    })
  })

  test('非主人发 /cancel：当普通文字交给模型（不是网关命令）', async () => {
    await withBot({}, async ({ tg, b }) => {
      tg.pushText(FRIEND, '/cancel')
      await waitSentText(tg, FRIEND, '收到：/cancel')
      expect(promptsText(b)).toContain('/cancel')
    })
  })
})
