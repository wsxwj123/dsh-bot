// 验收：刷新模型——引导「刷新模型」（INTERFACE 3.4.5）与一行命令 /provider refresh（3.6）、锁（3.1.3）
import { describe, expect, test } from 'bun:test'
import { existsSync, utimesSync, writeFileSync } from 'fs'
import { OWNER, labels, lockPath, openProviderMenu, override, providersPath, readProviders, seedCreds, seedProviders, sleep, until, waitEvent, waitSentText, waitText, withModels, type BotEnv } from './_acc'
import { type FakeModels, json, openaiOk, sleep as fsleep } from './fake-models'

const FETCHED = [{ id: 'm1', context_length: 8000 }, 'm3', 'm4', 'm3']
const DONE = '【系统】「myproxy」拉到 3 个模型（新增 2 个，去掉 1 个；手动加的 1 个保留）。'
/** 预置：m1（主人手填上下文 50000）、m2（拉到的）、man1（手动加的）；凭据里有 fm 期望的密钥 */
const seed = (o: { key?: boolean; name?: string; api?: any } = {}) => (b: BotEnv, fm: FakeModels) => {
  seedProviders(b.root, [{ name: o.name ?? 'myproxy', api: o.api, baseURL: `${fm.url}/v1`, models: [{ id: 'm1', contextWindow: 50000 }, 'm2', 'man1'], manualModels: ['man1'], ownerContext: ['m1'] }])
  if (o.key !== false) seedCreds(b.root, { PROVIDER_MYPROXY_KEY: fm.expectKey })
}

async function refreshViaMenu(tg: any): Promise<{ mid: number; cb: string }> {
  const m = await openProviderMenu(tg)
  tg.clickButton(OWNER, OWNER, m.messageId, '刷新模型')
  await waitText(tg, OWNER, m.messageId, t => t === '【系统】刷新哪个自建供应商的模型？', '刷新哪个')
  const cb = tg.clickButton(OWNER, OWNER, m.messageId, 'myproxy')
  return { mid: m.messageId, cb }
}

describe('引导「刷新模型」', () => {
  test('列表只列本 bot 上启用的自建（配置有误的、被同名遮住的不列），末尾「取消」', async () => {
    const routes = { shadow: { api: 'openai-completions', baseURL: 'https://r.example.com/v1', apiKeyEnv: 'SHADOW_KEY', models: [{ id: 'r1', contextWindow: 65536 }] } }
    await withModels({ brain: { routes }, handler: openaiOk(FETCHED), before: (b, fm) => seedProviders(b.root, [
      { name: 'myproxy', baseURL: `${fm.url}/v1`, models: ['m1'] },
      { name: 'Shadow', baseURL: 'https://s.example.com/v1', models: ['s1'] },
      { name: 'broken', api: 'grpc' as any, baseURL: 'https://x.example.com/v1', models: ['x'] },
    ]) }, async ({ tg }) => {
      const m = await openProviderMenu(tg)
      tg.clickButton(OWNER, OWNER, m.messageId, '刷新模型')
      await waitText(tg, OWNER, m.messageId, t => t === '【系统】刷新哪个自建供应商的模型？', '刷新哪个')
      expect(labels(tg, m.messageId)).toEqual(['myproxy', '取消'])
    })
  })

  test('选好后：回调答复「正在拉取模型列表…」', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: seed() }, async ({ tg }) => {
      const { cb } = await refreshViaMenu(tg)
      await until(() => tg.answers.find(a => a.id === cb), '回调答复')
      expect(tg.answers.find(a => a.id === cb)!.text).toBe('正在拉取模型列表…')
    })
  })

  test('拉取中：菜单改为「正在拉取「myproxy」的模型列表…」，没有按钮', async () => {
    const slow = async (r: any) => { await fsleep(1_500); return openaiOk(FETCHED)(r, 1, null as any) }
    await withModels({ handler: slow, before: seed() }, async ({ tg }) => {
      const { mid } = await refreshViaMenu(tg)
      await waitText(tg, OWNER, mid, t => t === '【系统】正在拉取「myproxy」的模型列表…', '拉取中')
      expect(labels(tg, mid)).toEqual([])
    })
  })

  test('成功：文案写新增/去掉/保留的个数与「去掉了：m2」，按钮「切到这家」「关闭」', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: seed() }, async ({ tg }) => {
      const { mid } = await refreshViaMenu(tg)
      const t = await waitText(tg, OWNER, mid, x => x.includes('拉到 3 个模型'), '刷新结果', 20_000)
      expect(t).toContain(DONE)
      expect(t).toContain('去掉了：m2')
      expect(labels(tg, mid)).toEqual(['切到这家', '关闭'])
    })
  })

  test('成功：新列表 = 拉到的（去重保序）+ 手动加的；主人手填的上下文长度保留', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: seed() }, async ({ tg, b }) => {
      const { mid } = await refreshViaMenu(tg)
      await waitText(tg, OWNER, mid, x => x.includes('拉到 3 个模型'), '刷新结果', 20_000)
      const e = readProviders(b.root).providers.myproxy
      expect(e.route.models).toEqual([{ id: 'm1', contextWindow: 50000 }, { id: 'm3', contextWindow: 131072 }, { id: 'm4', contextWindow: 131072 }, { id: 'man1', contextWindow: 131072 }])
      expect(e.meta.manualModels).toEqual(['man1'])
    })
  })

  test('成功：写 meta.lastRefresh {ok:true, count:3, reason:null}', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: seed() }, async ({ tg, b }) => {
      const t0 = Date.now()
      const { mid } = await refreshViaMenu(tg)
      await waitText(tg, OWNER, mid, x => x.includes('拉到 3 个模型'), '刷新结果', 20_000)
      const lr = readProviders(b.root).providers.myproxy.meta.lastRefresh
      expect(lr.ok).toBe(true)
      expect(lr.count).toBe(3)
      expect(lr.reason).toBeNull()
      expect(lr.at).toBeGreaterThanOrEqual(t0)
    })
  })

  test('本 bot 正在用被去掉的模型：结果加一句，并换回配置文件里的模型', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: seed() }, async ({ tg, b }) => {
      tg.pushText(OWNER, '/model myproxy/m2')
      await waitSentText(tg, OWNER, '已换成 myproxy / m2')
      const { mid } = await refreshViaMenu(tg)
      const t = await waitText(tg, OWNER, mid, x => x.includes('拉到 3 个模型'), '刷新结果', 20_000)
      expect(t).toContain('这个 bot 正在用的 m2 不在新列表里，已换回配置文件里的模型。')
      const o = override(b)
      expect(o === null || !o.provider).toBe(true)
    })
  })

  test('失败（500）：「没拉到模型：对方接口出错（HTTP 500）。模型列表保持不变。」，lastRefresh ok=false', async () => {
    await withModels({ handler: () => json({}, 500), before: seed() }, async ({ tg, b }) => {
      const before = JSON.stringify(readProviders(b.root).providers.myproxy.route.models)
      const { mid } = await refreshViaMenu(tg)
      const t = await waitText(tg, OWNER, mid, x => x.includes('没拉到模型'), '刷新结果', 20_000)
      expect(t).toContain('【系统】「myproxy」没拉到模型：对方接口出错（HTTP 500）。模型列表保持不变。')
      const e = readProviders(b.root).providers.myproxy
      expect(JSON.stringify(e.route.models)).toBe(before)
      expect(e.meta.lastRefresh.ok).toBe(false)
      expect(e.meta.lastRefresh.reason).toBe('server_error')
    })
  })

  test('对方返回 0 个也算失败：列表不变', async () => {
    await withModels({ handler: openaiOk([]), before: seed() }, async ({ tg, b }) => {
      const { mid } = await refreshViaMenu(tg)
      const t = await waitText(tg, OWNER, mid, x => x.includes('没拉到模型'), '刷新结果', 20_000)
      expect(t).toContain('对方返回的模型列表是空的')
      expect(readProviders(b.root).providers.myproxy.route.models.length).toBe(3)
    })
  })

  test('缺密钥：回「「myproxy」缺密钥，先用「修改」→「密钥」补上。」，不发请求', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: seed({ key: false }) }, async ({ tg, fm }) => {
      const k = tg.sentTo(OWNER).length
      const { mid } = await refreshViaMenu(tg)
      await until(() => [tg.textOf(OWNER, mid) ?? '', ...tg.sentTo(OWNER).slice(k).map(s => s.text ?? '')].some(t => t.includes('【系统】「myproxy」缺密钥，先用「修改」→「密钥」补上。')), '缺密钥提示')
      expect(fm.requests.length).toBe(0)
    })
  })

  test('拉取期间密钥被别处改过（keyRev 变）：「刚被别处改过，这次结果没保存，请再刷新一次。」', async () => {
    const slow = async (r: any) => { await fsleep(2_000); return openaiOk(FETCHED)(r, 1, null as any) }
    await withModels({ handler: slow, before: seed() }, async ({ tg, b }) => {
      const { mid } = await refreshViaMenu(tg)
      await sleep(600)
      const p = readProviders(b.root)
      p.providers.myproxy.meta.keyRev = 2
      writeFileSync(providersPath(b.root), JSON.stringify(p))
      await waitText(tg, OWNER, mid, t => t.includes('【系统】「myproxy」刚被别处改过，这次结果没保存，请再刷新一次。'), '被改过', 20_000)
      expect(readProviders(b.root).providers.myproxy.route.models.map((m: any) => m.id)).toEqual(['m1', 'm2', 'man1'])
    })
  })

  test('拉取期间被别处删掉：「已经不在了，这次结果没保存。」且不复活', async () => {
    const slow = async (r: any) => { await fsleep(2_000); return openaiOk(FETCHED)(r, 1, null as any) }
    await withModels({ handler: slow, before: seed() }, async ({ tg, b }) => {
      const { mid } = await refreshViaMenu(tg)
      await sleep(600)
      seedProviders(b.root, [])
      await waitText(tg, OWNER, mid, t => t.includes('【系统】「myproxy」已经不在了，这次结果没保存。'), '已不在', 20_000)
      expect(readProviders(b.root).providers.myproxy).toBeUndefined()
    })
  })
})
/** 发一行命令，等以【系统】开头的回复 */
async function cmd(tg: any, text: string, timeoutMs = 20_000) {
  const k = tg.sentTo(OWNER).length
  tg.pushText(OWNER, text)
  return (await waitSentText(tg, OWNER, '【系统】', k, timeoutMs)).text!
}

describe('/provider refresh <名字>', () => {
  test('成功：回 3.4.5 的成功文案，不带按钮', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: seed() }, async ({ tg }) => {
      const k = tg.sentTo(OWNER).length
      tg.pushText(OWNER, '/provider refresh myproxy')
      const s = await waitSentText(tg, OWNER, DONE, k, 20_000)
      expect((s.replyMarkup?.inline_keyboard ?? []).flat().length).toBe(0)
    })
  })

  test('名字按规整名匹配（MY_PROXY 找到 my-proxy）', async () => {
    await withModels({ handler: openaiOk(['z1']), before: (b, fm) => { seedProviders(b.root, [{ name: 'my-proxy', baseURL: `${fm.url}/v1`, models: ['z0'] }]); seedCreds(b.root, { PROVIDER_MY_PROXY_KEY: fm.expectKey }) } }, async ({ tg }) => {
      expect(await cmd(tg, '/provider refresh MY_PROXY')).toContain('【系统】「my-proxy」拉到 1 个模型')
    })
  })

  for (const [what, text] of [['没有名字', '/provider refresh'], ['两个名字', '/provider refresh a b']] as const) {
    test(`参数${what}：回「用法：/provider refresh <名字>」`, async () => {
      await withModels({ handler: openaiOk(FETCHED) }, async ({ tg }) => {
        expect(await cmd(tg, text)).toContain('用法：/provider refresh <名字>')
      })
    })
  }

  test('不存在：「没有 nosuch 这个供应商。用 /provider 看有哪些。」', async () => {
    await withModels({ handler: openaiOk(FETCHED) }, async ({ tg }) => {
      expect(await cmd(tg, '/provider refresh nosuch')).toContain('没有 nosuch 这个供应商。用 /provider 看有哪些。')
    })
  })

  test('内置：「只有自建供应商能刷新模型；deepseek-official 是内置的。」', async () => {
    await withModels({ handler: openaiOk(FETCHED) }, async ({ tg }) => {
      expect(await cmd(tg, '/provider refresh deepseek-official')).toContain('只有自建供应商能刷新模型；deepseek-official 是内置的。')
    })
  })

  test('配置文件路由：「只有自建供应商能刷新模型；myroute 是配置文件里的。」', async () => {
    const routes = { myroute: { api: 'openai-completions', baseURL: 'https://r.example.com/v1', apiKeyEnv: 'MYROUTE_KEY', models: [{ id: 'r1', contextWindow: 65536 }] } }
    await withModels({ brain: { routes }, handler: openaiOk(FETCHED) }, async ({ tg }) => {
      expect(await cmd(tg, '/provider refresh myroute')).toContain('只有自建供应商能刷新模型；myroute 是配置文件里的。')
    })
  })

  test('条目不合格：「「broken」配置有误，未启用，不能刷新。」，不发请求', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: (b, fm) => seedProviders(b.root, [{ name: 'broken', api: 'grpc' as any, baseURL: `${fm.url}/v1`, models: ['x'] }]) }, async ({ tg, fm }) => {
      expect(await cmd(tg, '/provider refresh broken')).toContain('「broken」配置有误，未启用，不能刷新。')
      expect(fm.requests.length).toBe(0)
    })
  })

  test('providers.json 读不了：「共用供应商文件读不了（格式坏了），先修好 <根>/providers.json」', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: b => writeFileSync(providersPath(b.root), 'not json') }, async ({ tg, b }) => {
      expect(await cmd(tg, '/provider refresh myproxy')).toContain(`共用供应商文件读不了（格式坏了），先修好 ${providersPath(b.root)}`)
    })
  })

  test('缺密钥：「「myproxy」缺密钥，先用 /provider →「修改」→「密钥」补上。」，不发请求', async () => {
    await withModels({ handler: openaiOk(FETCHED), before: seed({ key: false }) }, async ({ tg, fm }) => {
      expect(await cmd(tg, '/provider refresh myproxy')).toContain('「myproxy」缺密钥，先用 /provider →「修改」→「密钥」补上。')
      expect(fm.requests.length).toBe(0)
    })
  })

  test('同一家正在刷新时再发一次：「「myproxy」正在刷新，请稍候。」，只发一次请求', async () => {
    const slow = async (r: any) => { await fsleep(2_000); return openaiOk(FETCHED)(r, 1, null as any) }
    await withModels({ handler: slow, before: seed() }, async ({ tg, fm }) => {
      const k = tg.sentTo(OWNER).length
      tg.pushText(OWNER, '/provider refresh myproxy')
      await sleep(400)
      tg.pushText(OWNER, '/provider refresh myproxy')
      await waitSentText(tg, OWNER, '「myproxy」正在刷新，请稍候。', k)
      await waitSentText(tg, OWNER, DONE, k, 20_000)
      expect(fm.requests.length).toBe(1)
    })
  })
})

describe('共用文件的锁', () => {
  test('锁被一个活着的进程一直占着：等满 provider_lock_wait_ms 后回「别的 bot 正在改供应商，请稍后再试。」，日志 provider.lock_timeout，文件不变', async () => {
    await withModels({ gw: { provider_lock_wait_ms: 300 }, handler: openaiOk(FETCHED), before: seed() }, async ({ tg, b }) => {
      const before = JSON.stringify(readProviders(b.root))
      writeFileSync(lockPath(b.root), JSON.stringify({ pid: process.pid, at: Date.now() }))
      expect(await cmd(tg, '/provider refresh myproxy')).toContain('别的 bot 正在改供应商，请稍后再试。')
      await waitEvent(b, 'provider.lock_timeout')
      expect(JSON.stringify(readProviders(b.root))).toBe(before)
    })
  })

  test('残留锁（持锁进程已不存在）：清掉并继续，日志 provider.lock_stale_cleared，刷新成功、锁文件不留', async () => {
    await withModels({ gw: { provider_lock_wait_ms: 2_000 }, handler: openaiOk(FETCHED), before: seed() }, async ({ tg, b }) => {
      const dead = Bun.spawnSync(['bun', '-e', 'console.log(process.pid)']).stdout.toString().trim()
      writeFileSync(lockPath(b.root), JSON.stringify({ pid: Number(dead), at: Date.now() }))
      expect(await cmd(tg, '/provider refresh myproxy')).toContain(DONE)
      await waitEvent(b, 'provider.lock_stale_cleared', e => Number(e.pid) === Number(dead))
      await until(() => !existsSync(lockPath(b.root)), '锁文件被删')
    })
  })

  test('锁已存在超过 120 秒（即使写的是活着的进程号）：按残留处理并继续', async () => {
    await withModels({ gw: { provider_lock_wait_ms: 2_000 }, handler: openaiOk(FETCHED), before: seed() }, async ({ tg, b }) => {
      writeFileSync(lockPath(b.root), JSON.stringify({ pid: process.pid, at: Date.now() - 121_000 }))
      const old = new Date(Date.now() - 121_000)
      utimesSync(lockPath(b.root), old, old)
      expect(await cmd(tg, '/provider refresh myproxy')).toContain(DONE)
      await waitEvent(b, 'provider.lock_stale_cleared')
    })
  })
})
