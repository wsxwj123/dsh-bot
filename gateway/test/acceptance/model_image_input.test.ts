// 验收：供应商文件里模型的可选 input（只依据 .devflow/model-image-input-接口.md 的行为清单与错误契约）。
// 全黑盒：经配置文件启动网关、假 Telegram 触发、本机 HTTP 接口驱动；只读 providers.json / 交给 dsh 的补丁层。
// 行为清单 10 条，对应下面 10 个 test；第 4 条内含三种非法写法。
import { describe, expect, setDefaultTimeout, test } from 'bun:test'
import { defaultKeyEnv, patchRoutes, readProviders, seedCreds, seedProviders, until, warmUp, withBot, withModels, type BotEnv } from './_acc'
import { json, openaiOk } from './fake-models'

setDefaultTimeout(60_000)

const SAVE = '/v1/provider/save'
const REFRESH = '/v1/provider/refresh'

/** 供应商文件（providers.json）里这家供应商的模型清单 */
const fileModels = (b: BotEnv, name = 'myproxy'): any[] | null =>
  readProviders(b.root)?.providers?.[name]?.route?.models ?? null

/** 交给 dsh 的清单（bot.patch.yml 里这家供应商的 models 数组） */
const dshModels = (b: BotEnv, name = 'myproxy'): any[] | null =>
  patchRoutes(b)?.[name]?.models ?? null

/** brain.routes 在补丁层里的键可能被规整大小写，按名字找 */
const routeByName = (routes: Record<string, any>, name: string): any =>
  Object.entries(routes ?? {}).find(([k]) => k.toLowerCase() === name.toLowerCase())?.[1]

/** 等交给 dsh 的清单里出现这个模型 */
const dshModel = (b: BotEnv, id: string) =>
  until(() => dshModels(b)?.find(m => m.id === id), `交给 dsh 的模型 ${id}`, 20_000)

/** 写一个只有一家供应商的 providers.json */
const seedOne = (models: any[], name = 'myproxy') => (b: BotEnv) =>
  seedProviders(b.root, [{ name, baseURL: 'https://p.example.com/v1', models }])

describe('行为清单', () => {
  test('1. input:["text","image"]：交给 dsh 的条目带同样的 input', async () => {
    await withBot({ before: seedOne([{ id: 'm1', contextWindow: 128000, input: ['text', 'image'] }]) }, async ({ tg, b }) => {
      await warmUp(tg)
      expect(await dshModel(b, 'm1')).toEqual({ id: 'm1', contextWindow: 128000, input: ['text', 'image'] })
    })
  })

  test('2. 模型没写 input：交给 dsh 的条目里没有 input 键', async () => {
    await withBot({ before: seedOne([{ id: 'm2', contextWindow: 65536 }]) }, async ({ tg, b }) => {
      await warmUp(tg)
      const m = await dshModel(b, 'm2')
      expect('input' in m).toBe(false)
      expect(m).toEqual({ id: 'm2', contextWindow: 65536 })
    })
  })

  test('3. input:["text","audio"]：只丢字段，模型照常在清单里，供应商照常启用', async () => {
    await withBot({ before: seedOne([{ id: 'm3', contextWindow: 64000, input: ['text', 'audio'] }]) }, async ({ tg, b, gw }) => {
      await warmUp(tg)
      const m = await dshModel(b, 'm3')
      expect('input' in m).toBe(false)
      expect(m).toEqual({ id: 'm3', contextWindow: 64000 })
      const r = await gw.call('/v1/model')
      expect(r.status, `GET /v1/model 应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      const item = r.json.providers.find((p: any) => p.name === 'myproxy')
      expect(item?.enabled, `myproxy 应照常启用，实得 ${JSON.stringify(item)}`).toBe(true)
    })
  })

  test('4. input 为 []、"image"、[1]（并含错误契约里的对象/数字/null）：都只丢字段，模型照常', async () => {
    const models = [
      { id: 'm-empty', contextWindow: 1024, input: [] },
      { id: 'm-string', contextWindow: 2048, input: 'image' },
      { id: 'm-num', contextWindow: 4096, input: [1] },
      { id: 'm-object', contextWindow: 8192, input: { kind: 'image' } },
      { id: 'm-null', contextWindow: 16384, input: null },
      { id: 'm-numval', contextWindow: 32768, input: 42 },
    ]
    await withBot({ before: seedOne(models) }, async ({ tg, b, gw }) => {
      await warmUp(tg)
      expect(await until(() => dshModels(b), '交给 dsh 的清单', 20_000)).toEqual([
        { id: 'm-empty', contextWindow: 1024 },
        { id: 'm-string', contextWindow: 2048 },
        { id: 'm-num', contextWindow: 4096 },
        { id: 'm-object', contextWindow: 8192 },
        { id: 'm-null', contextWindow: 16384 },
        { id: 'm-numval', contextWindow: 32768 },
      ])
      const item = (await gw.call('/v1/model')).json.providers.find((p: any) => p.name === 'myproxy')
      expect(item?.enabled, `myproxy 应照常启用，实得 ${JSON.stringify(item)}`).toBe(true)
    })
  })

  test('5. input:["image","image","text"]：交给 dsh 的是去重后的 ["image","text"]', async () => {
    await withBot({ before: seedOne([{ id: 'm5', contextWindow: 128000, input: ['image', 'image', 'text'] }]) }, async ({ tg, b }) => {
      await warmUp(tg)
      const m = await dshModel(b, 'm5')
      expect(m.input).toEqual(['image', 'text'])
      expect(m).toEqual({ id: 'm5', contextWindow: 128000, input: ['image', 'text'] })
    })
  })

  test('6. 管理台刷新成功、假供应商返回同一批模型，原有模型的 input 保留', async () => {
    const key = 'test-key-7img-refresh-000001'
    await withModels({
      key,
      handler: openaiOk([{ id: 'm6', context_length: 128000 }]),
      before: (b, fm) => {
        seedProviders(b.root, [{ name: 'myproxy', baseURL: `${fm.url}/v1`, models: [{ id: 'm6', contextWindow: 128000, input: ['text', 'image'] }] }])
        seedCreds(b.root, { [defaultKeyEnv('myproxy')]: key })
      },
    }, async ({ gw, b, tg }) => {
      const r = await gw.call(REFRESH, { name: 'myproxy', full: true })
      expect(r.status, `刷新应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(fileModels(b)).toEqual([{ id: 'm6', contextWindow: 128000, input: ['text', 'image'] }])
      await warmUp(tg)
      expect(await dshModel(b, 'm6')).toEqual({ id: 'm6', contextWindow: 128000, input: ['text', 'image'] })
    })
  })

  test('7. 刷新后新出现的模型：这个新模型不带 input', async () => {
    const key = 'test-key-7img-new-000000001'
    await withModels({
      key,
      handler: openaiOk([{ id: 'm7-old', context_length: 128000 }, { id: 'm7-new', context_length: 32000 }]),
      before: (b, fm) => {
        seedProviders(b.root, [{ name: 'myproxy', baseURL: `${fm.url}/v1`, models: [{ id: 'm7-old', contextWindow: 128000, input: ['text'] }] }])
        seedCreds(b.root, { [defaultKeyEnv('myproxy')]: key })
      },
    }, async ({ gw, b, tg }) => {
      const r = await gw.call(REFRESH, { name: 'myproxy', full: true })
      expect(r.status, `刷新应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      const fresh = fileModels(b)!.find(m => m.id === 'm7-new')
      expect(fresh, '新模型应进入 providers.json 的清单').toBeTruthy()
      expect('input' in fresh!).toBe(false)
      await warmUp(tg)
      const handed = await dshModel(b, 'm7-new')
      expect(handed.id).toBe('m7-new')
      expect('input' in handed).toBe(false)
    })
  })

  test('8. 刷新失败（假供应商报错）：原有列表保持不变，原有 input 保留', async () => {
    const key = 'test-key-7img-fail-00000001'
    await withModels({
      key,
      handler: () => json({ error: { message: 'boom' } }, 500),
      before: (b, fm) => {
        seedProviders(b.root, [{ name: 'myproxy', baseURL: `${fm.url}/v1`, models: [{ id: 'm8', contextWindow: 128000, input: ['image', 'text'] }] }])
        seedCreds(b.root, { [defaultKeyEnv('myproxy')]: key })
      },
    }, async ({ gw, b }) => {
      const before = JSON.stringify(fileModels(b))
      const r = await gw.call(REFRESH, { name: 'myproxy', full: true })
      expect(r.status, `假供应商报错，刷新不应报成功，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).not.toBe(200)
      expect(JSON.stringify(fileModels(b)), '刷新失败后清单应逐字不变').toBe(before)
      expect(fileModels(b)).toEqual([{ id: 'm8', contextWindow: 128000, input: ['image', 'text'] }])
    })
  })

  test('9. 经网关把这家供应商原样保存一次：providers.json 与交给 dsh 的清单里 input 仍在', async () => {
    const key = 'test-key-7img-save-00000001'
    await withModels({
      key,
      handler: openaiOk([{ id: 'm9', context_length: 128000 }]),
      before: (b, fm) => {
        seedProviders(b.root, [{ name: 'myproxy', baseURL: `${fm.url}/v1`, models: [{ id: 'm9', contextWindow: 128000, input: ['text', 'image'] }] }])
        seedCreds(b.root, { [defaultKeyEnv('myproxy')]: key })
      },
    }, async ({ gw, b, tg, fm }) => {
      const r = await gw.call(SAVE, { name: 'myproxy', api: 'openai-completions', baseURL: `${fm.url}/v1`, key: '', mode: 'modify' })
      expect(r.status, `保存应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(fileModels(b)).toEqual([{ id: 'm9', contextWindow: 128000, input: ['text', 'image'] }])
      await warmUp(tg)
      expect(await dshModel(b, 'm9')).toEqual({ id: 'm9', contextWindow: 128000, input: ['text', 'image'] })
    })
  })

  test('10. 配置文件 brain.routes 里模型写了 input:["text","image"]：交给 dsh 的清单里带着它', async () => {
    const routes = {
      myroute: {
        api: 'openai-completions',
        baseURL: 'https://r.example.com/v1',
        apiKeyEnv: 'MYROUTE_KEY',
        models: [{ id: 'r1', contextWindow: 32768, input: ['text', 'image'] }],
      },
    }
    await withBot({ brain: { provider: 'deepseek-official', model: 'deepseek-flash', reasoning_effort: 'low', routes } }, async ({ tg, b }) => {
      await warmUp(tg)
      const route = await until(() => routeByName(patchRoutes(b), 'myroute'), '交给 dsh 的配置路由 myroute', 20_000)
      expect(route.models).toEqual([{ id: 'r1', contextWindow: 32768, input: ['text', 'image'] }])
    })
  })
})
