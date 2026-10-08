// 验收：/provider 引导的「新建」——名字、格式、地址三步的提示与校验（INTERFACE 3.4.2、3.3.4）
import { describe, expect, test } from 'bun:test'
import { OWNER, labels, seedProviders, sleep, until, toFormatStep, toKeyStep, toNameStep, toUrlStep, waitMenu, waitSentText, waitText, withBot } from './_acc'

const STEP1 = '【系统】新建供应商（第 1/4 步）请输入名字：只能用英文字母、数字、-、_，以字母或数字开头，最多 32 个字符。'
const BAD_NAME = '【系统】名字只能用英文字母、数字、-、_，以字母或数字开头，最多 32 个字符。请重新输入：'
const taken = (n: string) => `【系统】${n} 是内置、配置文件里的供应商或保留字，不能用，换个名字。请重新输入：`
const ROUTE = { myroute: { api: 'openai-completions', baseURL: 'https://route.example.com/v1', apiKeyEnv: 'MYROUTE_KEY', models: [{ id: 'r1', contextWindow: 65536 }] } }

describe('新建第 1 步：名字', () => {
  test('点「新建」：主菜单被编辑成第 1/4 步提示，只剩「取消」按钮', async () => {
    await withBot({}, async ({ tg }) => {
      const m = await toNameStep(tg)
      expect(m.text).toBe(STEP1)
      expect(labels(tg, m.messageId)).toEqual(['取消'])
    })
  })

  test('输入合法名字：发一条新消息作为第 2/4 步，按钮「Anthropic 格式」「OpenAI 格式」「取消」', async () => {
    await withBot({}, async ({ tg }) => {
      const step1 = await toNameStep(tg)
      tg.pushText(OWNER, 'myproxy')
      const m = await waitMenu(tg, OWNER, x => x.text.includes('（第 2/4 步）'), '第 2/4 步')
      expect(m.messageId).not.toBe(step1.messageId)
      expect(m.text).toBe('【系统】新建供应商「myproxy」（第 2/4 步）选接口格式：')
      expect(labels(tg, m.messageId)).toEqual(['Anthropic 格式', 'OpenAI 格式', '取消'])
    })
  })

  test('输入名字后，第 1 步那条旧菜单的按钮被去掉', async () => {
    await withBot({}, async ({ tg }) => {
      const step1 = await toNameStep(tg)
      tg.pushText(OWNER, 'myproxy')
      await waitMenu(tg, OWNER, x => x.text.includes('（第 2/4 步）'), '第 2/4 步')
      await sleep(300)
      expect(labels(tg, step1.messageId)).toEqual([])
    })
  })

  for (const bad of ['my proxy', '-abc', '_abc', '中文名字', 'a'.repeat(33), 'a.b', 'a/b', 'ab#c']) {
    test(`名字「${bad.length > 20 ? bad.slice(0, 10) + '…(' + bad.length + ' 字)' : bad}」格式不对：回原因并重问这一步`, async () => {
      await withBot({}, async ({ tg }) => {
        await toNameStep(tg)
        const n = tg.sentTo(OWNER).length
        tg.pushText(OWNER, bad)
        await waitSentText(tg, OWNER, BAD_NAME, n)
        tg.pushText(OWNER, 'okname')
        await waitMenu(tg, OWNER, x => x.text.includes('「okname」（第 2/4 步）'), '改正后进入第 2 步')
      })
    })
  }

  for (const ok of ['a', '9lives', 'My_Proxy', 'x'.repeat(31) + '1']) {
    test(`名字「${ok.length > 20 ? ok.slice(0, 6) + '…(' + ok.length + ' 字)' : ok}」合格：进入第 2 步`, async () => {
      await withBot({}, async ({ tg }) => {
        await toFormatStep(tg, ok)
      })
    })
  }

  for (const n of ['deepseek-official', 'DeepSeek_Official', 'add', 'REFRESH', 'list', 'status', 'default', 'help']) {
    test(`名字「${n}」是内置或保留字（按规整名比较）：不能用`, async () => {
      await withBot({}, async ({ tg }) => {
        await toNameStep(tg)
        const k = tg.sentTo(OWNER).length
        tg.pushText(OWNER, n)
        await waitSentText(tg, OWNER, taken(n), k)
      })
    })
  }

  test('名字与本 bot 配置文件路由规整名相同（MyRoute vs myroute）：不能用', async () => {
    await withBot({ brain: { routes: ROUTE } }, async ({ tg }) => {
      await toNameStep(tg)
      const k = tg.sentTo(OWNER).length
      tg.pushText(OWNER, 'MyRoute')
      await waitSentText(tg, OWNER, taken('MyRoute'), k)
    })
  })

  test('不合格且像密钥的名字：删除这条消息，回「这条像是密钥」并重问', async () => {
    await withBot({}, async ({ tg }) => {
      await toNameStep(tg)
      const k = tg.sentTo(OWNER).length
      const mid = tg.pushText(OWNER, 'sk.live.12345678abcdef')
      await waitSentText(tg, OWNER, '【系统】这条像是密钥，已删除，没有当作这一步的答案。', k)
      expect(tg.isDeleted(OWNER, mid)).toBe(true)
    })
  })

  test('形似密钥但合格的名字（qwen25-72b）照收，不删除', async () => {
    await withBot({}, async ({ tg }) => {
      await toNameStep(tg)
      const mid = tg.pushText(OWNER, 'qwen25-72b')
      await waitMenu(tg, OWNER, x => x.text.includes('「qwen25-72b」（第 2/4 步）'), '第 2/4 步')
      expect(tg.isDeleted(OWNER, mid)).toBe(false)
    })
  })

  test('与自建供应商规整名相同（My_Proxy vs my-proxy）：问要更新还是换名字，按钮「更新它」「换个名字」「取消」', async () => {
    await withBot({ before: b => seedProviders(b.root, [{ name: 'my-proxy', baseURL: 'https://p.example.com/v1', models: ['m1'] }]) }, async ({ tg }) => {
      await toNameStep(tg)
      tg.pushText(OWNER, 'My_Proxy')
      const m = await waitMenu(tg, OWNER, x => x.text.includes('已有自建供应商'), '同名确认')
      expect(m.text).toBe('【系统】已有自建供应商「my-proxy」。要更新它（地址、格式、密钥都重新填），还是换个名字？')
      expect(labels(tg, m.messageId)).toEqual(['更新它', '换个名字', '取消'])
    })
  })

  test('同名确认点「换个名字」：回到第 1 步重新问名字', async () => {
    await withBot({ before: b => seedProviders(b.root, [{ name: 'my-proxy', baseURL: 'https://p.example.com/v1', models: ['m1'] }]) }, async ({ tg }) => {
      await toNameStep(tg)
      tg.pushText(OWNER, 'my-proxy')
      const m = await waitMenu(tg, OWNER, x => x.text.includes('已有自建供应商'), '同名确认')
      tg.clickButton(OWNER, OWNER, m.messageId, '换个名字')
      await waitText(tg, OWNER, m.messageId, t => t.includes('（第 1/4 步）'), '回到第 1 步')
    })
  })

  test('同名确认点「更新它」：进入选格式（第 2/4 步）', async () => {
    await withBot({ before: b => seedProviders(b.root, [{ name: 'my-proxy', baseURL: 'https://p.example.com/v1', models: ['m1'] }]) }, async ({ tg }) => {
      await toNameStep(tg)
      tg.pushText(OWNER, 'my-proxy')
      const m = await waitMenu(tg, OWNER, x => x.text.includes('已有自建供应商'), '同名确认')
      tg.clickButton(OWNER, OWNER, m.messageId, '更新它')
      await until(() => tg.lastMenu(OWNER)?.text.includes('（第 2/4 步）'), '进入第 2/4 步')
    })
  })

  test('名字这一步发图片：回「这一步需要文字，请重新输入。」', async () => {
    await withBot({}, async ({ tg }) => {
      await toNameStep(tg)
      const k = tg.sentTo(OWNER).length
      tg.pushPhoto(OWNER)
      await waitSentText(tg, OWNER, '【系统】这一步需要文字，请重新输入。', k)
    })
  })

  test('名字这一步发贴纸：回「这一步需要文字，请重新输入。」', async () => {
    await withBot({}, async ({ tg }) => {
      await toNameStep(tg)
      const k = tg.sentTo(OWNER).length
      tg.pushSticker(OWNER)
      await waitSentText(tg, OWNER, '【系统】这一步需要文字，请重新输入。', k)
    })
  })
})

describe('新建第 2、3 步：格式与地址', () => {
  test('选格式这一步打字：回「这一步请点上面的按钮…」', async () => {
    await withBot({}, async ({ tg }) => {
      await toFormatStep(tg, 'myproxy')
      const k = tg.sentTo(OWNER).length
      tg.pushText(OWNER, 'openai')
      await waitSentText(tg, OWNER, '【系统】这一步请点上面的按钮；不想继续就点「取消」或发 /cancel。', k)
    })
  })

  test('点「OpenAI 格式」：同一条菜单被编辑成第 3/4 步地址提示', async () => {
    await withBot({}, async ({ tg }) => {
      const m = await toUrlStep(tg, 'myproxy', 'openai')
      expect(m.text.startsWith('【系统】')).toBe(true)
      expect(m.text).toContain('（第 3/4 步）请输入接口地址：https:// 开头；本机地址（127.0.0.1、localhost、[::1]）可以用 http://。OpenAI 格式一般以 /v1 结尾；Anthropic 格式填根地址，末尾的 /v1 会自动去掉。')
      expect(labels(tg, m.messageId)).toEqual(['取消'])
    })
  })

  const BAD_URLS: [string, string][] = [
    ['ftp://files.example.com/v1', '地址格式不对，要以 https:// 开头'],
    ['example.com/v1', '地址格式不对，要以 https:// 开头'],
    ['http://example.com/v1', '只有本机地址（127.0.0.1、localhost、[::1]）可以用 http://，其它地址请用 https://'],
    ['http://127.0.0.2/v1', '只有本机地址（127.0.0.1、localhost、[::1]）可以用 http://，其它地址请用 https://'],
    ['http://localhost.evil.com/v1', '只有本机地址（127.0.0.1、localhost、[::1]）可以用 http://，其它地址请用 https://'],
    ['http://127.0.0.1.nip.io/v1', '只有本机地址（127.0.0.1、localhost、[::1]）可以用 http://，其它地址请用 https://'],
    ['https://user:pass@example.com/v1', '地址里不能带用户名和密码'],
    ['https://example.com/v1?x=1', '地址里不能带 ? 或 # 后面的部分'],
    ['https://example.com/v1#frag', '地址里不能带 ? 或 # 后面的部分'],
    ['https://example.com/' + 'a'.repeat(281), '地址太长（最多 300 个字符）'],
  ]
  for (const [url, why] of BAD_URLS) {
    test(`地址「${url.length > 40 ? url.slice(0, 24) + '…(' + url.length + ' 字)' : url}」不合格：回「${why}」并重问`, async () => {
      await withBot({}, async ({ tg }) => {
        await toUrlStep(tg, 'myproxy', 'openai')
        const k = tg.sentTo(OWNER).length
        tg.pushText(OWNER, url)
        await waitSentText(tg, OWNER, `【系统】${why}。请重新输入：`, k)
        expect(tg.lastMenu(OWNER)?.text ?? '').not.toContain('（第 4/4 步）')
      })
    })
  }

  for (const url of ['https://example.com/' + 'a'.repeat(280), 'HTTPS://api.example.com/v1', 'http://127.0.0.1:9/v1', 'http://LOCALHOST:9/v1', 'http://[::1]:9/v1']) {
    test(`地址「${url.length > 40 ? url.slice(0, 24) + '…(' + url.length + ' 字)' : url}」合格：进入第 4/4 步（新消息）`, async () => {
      await withBot({}, async ({ tg }) => {
        const m3 = await toUrlStep(tg, 'myproxy', 'openai')
        tg.pushText(OWNER, url)
        const m4 = await waitMenu(tg, OWNER, x => x.text.includes('（第 4/4 步）'), '第 4/4 步')
        expect(m4.messageId).not.toBe(m3.messageId)
        expect(m4.text).toContain('（第 4/4 步）请输入密钥。收到后会马上删除你发的那条消息。')
      })
    })
  }

  test('进入第 4 步后，第 3 步那条菜单的按钮被去掉', async () => {
    await withBot({}, async ({ tg }) => {
      const m3 = await toUrlStep(tg, 'myproxy', 'openai')
      await toKeyStepFrom(tg, 'http://127.0.0.1:9/v1')
      await sleep(300)
      expect(labels(tg, m3.messageId)).toEqual([])
    })
  })

  test('地址这一步发来不合格且像密钥的文字：删除，回「这条像是密钥…」', async () => {
    await withBot({}, async ({ tg }) => {
      await toUrlStep(tg, 'myproxy', 'openai')
      const k = tg.sentTo(OWNER).length
      const mid = tg.pushText(OWNER, 'abcd1234efgh5678')
      await waitSentText(tg, OWNER, '【系统】这条像是密钥，已删除，没有当作这一步的答案。', k)
      expect(tg.isDeleted(OWNER, mid)).toBe(true)
    })
  })

  test('「取消」按钮在第 4 步也有', async () => {
    await withBot({}, async ({ tg }) => {
      const m = await toKeyStep(tg, 'myproxy', 'openai', 'http://127.0.0.1:9/v1')
      expect(labels(tg, m.messageId)).toContain('取消')
    })
  })
})

async function toKeyStepFrom(tg: any, url: string) {
  tg.pushText(OWNER, url)
  return waitMenu(tg, OWNER, (x: any) => x.text.includes('（第 4/4 步）'), '第 4/4 步')
}
