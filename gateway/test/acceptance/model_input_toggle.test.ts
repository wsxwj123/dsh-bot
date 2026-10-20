// 验收：POST /v1/provider/model 的 set_input（依据 .devflow/model-input-toggle-接口.md 行为清单 1–7 与错误契约）。
// 只经本机 HTTP 接口操作，读 providers.json 与交给 dsh 的补丁层；不 import 产品模块。
// 当前实现尚未落地时，成功类用例应红在断言（状态码/落盘/清单），错误类用例红在状态码或错误码。
import { describe, expect, test } from 'bun:test'
import { readFileSync } from 'fs'
import {
  defaultKeyEnv, patchRoutes, providersPath, readProviders, seedCreds, seedProviders,
  until, warmUp, withBot, type BotEnv, type SeedModel,
} from './_acc'

const MODEL = '/v1/provider/model'
const KEY = 'test-key-7input00000000001'

const seed = (models: SeedModel[] = ['m1', 'm2']) => (b: BotEnv) => {
  seedProviders(b.root, [{ name: 'myproxy', baseURL: 'https://p.example.com/v1', models }])
  seedCreds(b.root, { [defaultKeyEnv('myproxy')]: KEY })
}
const entry = (b: BotEnv) => readProviders(b.root).providers.myproxy
const modelOf = (b: BotEnv, id = 'm1') => entry(b).route.models.find((m: any) => m.id === id)
/** providers.json 原文，用来断言错误路径"逐字不变" */
const rawProviders = (b: BotEnv) => readFileSync(providersPath(b.root), 'utf8')
const setInput = (over: Record<string, unknown> = {}) =>
  ({ name: 'myproxy', action: 'set_input', id: 'm1', input: ['text', 'image'], ...over })

/** 等 dsh 起来，返回交给 dsh 的 myproxy 路由；dsh 清单里的模型按 id 找 */
async function dshModels(tg: any, b: BotEnv): Promise<any[]> {
  await warmUp(tg)
  const route = await until(() => patchRoutes(b).myproxy, 'myproxy 交给 dsh', 20_000)
  return route.models
}

describe('行为 1：set_input ["text","image"]', () => {
  test('200 且 ok:true、text 是一句非空人话；providers.json 与 dsh 清单里该模型都带 input', async () => {
    await withBot({ before: seed() }, async ({ tg, gw, b }) => {
      const r = await gw.call(MODEL, setInput())
      expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(r.json?.ok, `应回 ok:true，实得 ${JSON.stringify(r.json)}`).toBe(true)
      expect(typeof r.json?.text, 'text 应是一句人话（字符串）').toBe('string')
      expect(String(r.json?.text).length, 'text 不该为空').toBeGreaterThan(0)

      const m = modelOf(b)
      expect(m, 'providers.json 里应有 m1').toBeTruthy()
      expect(m.input, `providers.json 里 m1 应带 input，实得 ${JSON.stringify(m)}`).toEqual(['text', 'image'])

      const dm = (await dshModels(tg, b)).find((x: any) => x.id === 'm1')
      expect(dm, 'dsh 清单里应有 m1').toBeTruthy()
      expect(dm.input, `dsh 清单里 m1 应带 input，实得 ${JSON.stringify(dm)}`).toEqual(['text', 'image'])
    })
  })
})

describe('行为 2：set_input ["image","image","text"]', () => {
  test('200；保存为去重且保序的 ["image","text"]（覆盖原有 input）', async () => {
    await withBot({ before: seed([{ id: 'm1', input: ['text'] }, 'm2']) }, async ({ gw, b }) => {
      const r = await gw.call(MODEL, setInput({ input: ['image', 'image', 'text'] }))
      expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(modelOf(b)?.input, `应保存为 ["image","text"]，实得 ${JSON.stringify(modelOf(b))}`).toEqual(['image', 'text'])
    })
  })
})

describe('行为 3：set_input []', () => {
  test('200；providers.json 与 dsh 清单里该模型都没有 input 键', async () => {
    await withBot({ before: seed([{ id: 'm1', input: ['image'] }, 'm2']) }, async ({ tg, gw, b }) => {
      const r = await gw.call(MODEL, setInput({ input: [] }))
      expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)

      const m = modelOf(b)
      expect(m, 'providers.json 里应还有 m1').toBeTruthy()
      expect(Object.prototype.hasOwnProperty.call(m, 'input'), `清除后不该有 input 键，实得 ${JSON.stringify(m)}`).toBe(false)

      const dm = (await dshModels(tg, b)).find((x: any) => x.id === 'm1')
      expect(dm, 'dsh 清单里应还有 m1').toBeTruthy()
      expect(Object.prototype.hasOwnProperty.call(dm, 'input'), `dsh 清单里不该有 input 键，实得 ${JSON.stringify(dm)}`).toBe(false)
    })
  })
})

describe('行为 4：input 含 "text"/"image" 以外的值', () => {
  test('400 bad_input；providers.json 逐字不变', async () => {
    await withBot({ before: seed() }, async ({ gw, b }) => {
      const before = rawProviders(b)
      const r = await gw.call(MODEL, setInput({ input: ['text', 'audio'] }))
      expect(r.status, `应 400，实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(400)
      expect(r.json, `error 应是 bad_input，实得 ${JSON.stringify(r.json)}`).toMatchObject({ ok: false, error: 'bad_input' })
      expect(rawProviders(b), '错误请求不该改 providers.json').toBe(before)
    })
  })
})

describe('行为 5：input 不是数组', () => {
  test('400 bad_input；providers.json 逐字不变', async () => {
    await withBot({ before: seed() }, async ({ gw, b }) => {
      const before = rawProviders(b)
      const r = await gw.call(MODEL, setInput({ input: 'image' }))
      expect(r.status, `应 400，实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(400)
      expect(r.json, `error 应是 bad_input，实得 ${JSON.stringify(r.json)}`).toMatchObject({ ok: false, error: 'bad_input' })
      expect(rawProviders(b), '错误请求不该改 providers.json').toBe(before)
    })
  })
})

describe('行为 6：id 是不存在的模型', () => {
  test('404 not_found；providers.json 逐字不变', async () => {
    await withBot({ before: seed() }, async ({ gw, b }) => {
      const before = rawProviders(b)
      const r = await gw.call(MODEL, setInput({ id: 'nosuch-model' }))
      expect(r.status, `应 404，实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(404)
      expect(r.json, `error 应是 not_found，实得 ${JSON.stringify(r.json)}`).toMatchObject({ ok: false, error: 'not_found' })
      expect(rawProviders(b), '错误请求不该改 providers.json').toBe(before)
    })
  })
})

describe('行为 7：set_input 但没给 input', () => {
  test('400 bad_input；providers.json 逐字不变', async () => {
    await withBot({ before: seed() }, async ({ gw, b }) => {
      const before = rawProviders(b)
      const { input, ...withoutInput } = setInput()
      expect(Object.prototype.hasOwnProperty.call(withoutInput, 'input'), '前置条件：请求体确实没有 input').toBe(false)
      const r = await gw.call(MODEL, withoutInput)
      expect(r.status, `应 400，实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(400)
      expect(r.json, `error 应是 bad_input，实得 ${JSON.stringify(r.json)}`).toMatchObject({ ok: false, error: 'bad_input' })
      expect(rawProviders(b), '错误请求不该改 providers.json').toBe(before)
    })
  })
})

// 以下三条是错误契约里行为清单没单列的行，照同一份契约补上。
describe('错误契约：供应商不存在', () => {
  test('404 not_found；providers.json 逐字不变', async () => {
    await withBot({ before: seed() }, async ({ gw, b }) => {
      const before = rawProviders(b)
      const r = await gw.call(MODEL, setInput({ name: 'nosuch-provider' }))
      expect(r.status, `应 404，实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(404)
      expect(r.json, `error 应是 not_found，实得 ${JSON.stringify(r.json)}`).toMatchObject({ ok: false, error: 'not_found' })
      expect(rawProviders(b), '错误请求不该改 providers.json').toBe(before)
    })
  })
})

describe('错误契约：供应商配置有误未启用', () => {
  test('409 disabled；providers.json 逐字不变', async () => {
    await withBot({ before: b => {
      seedProviders(b.root, [{ name: 'broken', api: 'grpc' as any, baseURL: 'https://x.example.com/v1', models: ['x1'] }])
    } }, async ({ gw, b }) => {
      const before = rawProviders(b)
      const r = await gw.call(MODEL, { name: 'broken', action: 'set_input', id: 'x1', input: ['text', 'image'] })
      expect(r.status, `应 409，实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(409)
      expect(r.json, `error 应是 disabled，实得 ${JSON.stringify(r.json)}`).toMatchObject({ ok: false, error: 'disabled' })
      expect(rawProviders(b), '错误请求不该改 providers.json').toBe(before)
    })
  })
})

describe('错误契约：input 元素不是字符串', () => {
  test('400 bad_input；providers.json 逐字不变', async () => {
    await withBot({ before: seed() }, async ({ gw, b }) => {
      const before = rawProviders(b)
      const r = await gw.call(MODEL, setInput({ input: ['text', 1] }))
      expect(r.status, `应 400，实得 ${r.status}，正文 ${r.text.slice(0, 200)}`).toBe(400)
      expect(r.json, `error 应是 bad_input，实得 ${JSON.stringify(r.json)}`).toMatchObject({ ok: false, error: 'bad_input' })
      expect(rawProviders(b), '错误请求不该改 providers.json').toBe(before)
    })
  })
})

describe('行为 9：GET /v1/provider 的 modelList 每项带 input', () => {
  test('有声明的是原数组 ["text","image"]，没声明的是 []（键必须存在）', async () => {
    await withBot({ before: b => {
      seedProviders(b.root, [{ name: 'myproxy', baseURL: 'https://p.example.com/v1', models: [{ id: 'm1', input: ['text', 'image'] }, 'm2'] }])
      seedCreds(b.root, { [defaultKeyEnv('myproxy')]: KEY })
    } }, async ({ gw }) => {
      const r = await gw.call('/v1/provider')
      expect(r.status, `GET /v1/provider 应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      const p = (r.json?.providers ?? []).find((x: any) => x?.name === 'myproxy')
      expect(p, `响应里应有 myproxy，实得 ${JSON.stringify(r.json?.providers)}`).toBeTruthy()
      const listed = (id: string) => p.modelList.find((m: any) => m.id === id)

      const declared = listed('m1')
      expect(declared, `modelList 里应有 m1，实得 ${JSON.stringify(p.modelList)}`).toBeTruthy()
      expect(declared.input, `m1 的 input 应是 ["text","image"]，实得 ${JSON.stringify(declared)}`).toEqual(['text', 'image'])

      const undeclared = listed('m2')
      expect(undeclared, `modelList 里应有 m2，实得 ${JSON.stringify(p.modelList)}`).toBeTruthy()
      expect(Object.prototype.hasOwnProperty.call(undeclared, 'input'), `m2 的 input 键必须存在（哪怕是空数组），实得 ${JSON.stringify(undeclared)}`).toBe(true)
      expect(undeclared.input, `m2 的 input 应是 []，实得 ${JSON.stringify(undeclared)}`).toEqual([])
    })
  })
})
