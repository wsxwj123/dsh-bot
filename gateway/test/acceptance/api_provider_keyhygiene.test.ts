// 验收：/v1/provider/save 的密钥卫生（INTERFACE-管理台UI 第 5 节）。
// 契约要点密钥唯一出现的位置是 <根>/credentials.yaml 的 refs 行与网关发往所填地址的列模型请求头；
// 账本（含 wal/shm）、日志、providers.json、网关进程输出、任何响应里都不该有；
// 契约还点名「收到非空 key 后立刻 registerSecret(key)」是本批最容易漏的一条。
// 这里的密钥形状特意不用 sk- 开头（日志通用脱敏规则会兜住 sk-，那就测不出 registerSecret 有没有被调）。
// 实现未落地时本文件红（多半 404），那是红基线，不是测试坏了。
import { describe, expect, test } from 'bun:test'
import { writeFileSync } from 'fs'
import { fakeKey, findSecret, lockPath, readCreds, seedProviders, sleep, withBot, withModels } from './_acc'
import { json, openaiOk } from './fake-models'

const IDS = ['m1', 'm2']
const SAVE = '/v1/provider/save'
const REQS = [
  // 每一项都是"密钥合法送进来、但请求会被拒"的场景
  { title: 'name 不合法', body: (key: string, url: string) => ({ name: 'bad.name', api: 'openai-completions', baseURL: url, key, mode: 'create' }) },
  { title: 'api 不合法', body: (key: string, url: string) => ({ name: 'myproxy', api: 'grpc', baseURL: url, key, mode: 'create' }) },
  { title: '地址不合法', body: (key: string, url: string) => ({ name: 'myproxy', api: 'openai-completions', baseURL: 'http://example.com/v1', key, mode: 'create' }) },
  { title: '名字是保留字', body: (key: string, url: string) => ({ name: 'default', api: 'openai-completions', baseURL: url, key, mode: 'create' }) },
]

describe('POST /v1/provider/save 的密钥卫生', () => {
  test('新建成功后：密钥只在 credentials.yaml，响应与账本日志文件里都没有', async () => {
    const key = fakeKey('secret')
    // 让假模型服务的期望密钥与请求体里用的一致，否则假服务认不出网关带来的密钥
    await withModels({ handler: openaiOk(IDS), key }, async ({ gw, b, fm }) => {
      const r = await gw.call(SAVE, { name: 'myproxy', api: 'openai-completions', baseURL: `${fm.url}/v1`, key, mode: 'create' })
      expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      await sleep(600)
      const hits = findSecret(b, key, { gw })
      expect(hits, `密钥出现在不该出现的地方（除 credentials.yaml 外应一处都没有）：${JSON.stringify(hits)}`).toEqual([])
      expect(readCreds(b.root), '凭据文件里应有这把密钥').toContain(key)
      expect(r.text.includes(key), '响应正文里不该有密钥').toBe(false)
      expect(fm.requests.length, '应真去拉了列表').toBeGreaterThan(0)
      expect(fm.requests[0]!.authOk, '请求头里应带的是原密钥').toBe(true)
      expect(fm.requests[0]!.authLen, '鉴权头长度应与原密钥一致').toBe(`Bearer ${key}`.length)
    })
  })

  test('被拒的请求（四种）：密钥不许落进任何文件、日志或响应', async () => {
    const key = fakeKey('reject')
    await withModels({ handler: openaiOk(IDS), before: b => seedProviders(b.root, []) }, async ({ gw, b, fm }) => {
      for (const c of REQS) {
        const r = await gw.call(SAVE, c.body(key, `${fm.url}/v1`))
        expect(r.status, `${c.title} 应被拒（400），实得 ${r.status}`).toBe(400)
        expect(r.text.includes(key), `${c.title} 的响应里回显了密钥`).toBe(false)
      }
      await sleep(600)
      const hits = findSecret(b, key, { gw })
      expect(hits, `被拒请求的密钥出现在：${JSON.stringify(hits)}`).toEqual([])
      expect(readCreds(b.root).includes(key), '被拒的密钥不该写进凭据文件').toBe(false)
    })
  })

  test('拉模型失败（对方 401）：密钥不进任何落盘与输出', async () => {
    const key = fakeKey('fail')
    await withModels({ handler: () => json({ error: { message: 'invalid api key' } }, 401) }, async ({ gw, b }) => {
      const r = await gw.call(SAVE, { name: 'myproxy', api: 'openai-completions', baseURL: 'https://api.example.com/v1', key, mode: 'create' })
      expect(r.status, `拉失败仍是保存成功，应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      await sleep(600)
      const hits = findSecret(b, key, { gw })
      expect(hits, `密钥出现在：${JSON.stringify(hits)}`).toEqual([])
      expect(readCreds(b.root), '凭据文件里应有这把密钥').toContain(key)
    })
  })

  test('拿不到锁（409 busy）：密钥不写凭据文件', async () => {
    const key = fakeKey('busy')
    await withBot({ before: b => { seedProviders(b.root, []); writeFileSync(lockPath(b.root), JSON.stringify({ pid: process.pid, at: Date.now() })) } }, async ({ gw }) => {
      const r = await gw.call(SAVE, { name: 'myproxy', api: 'openai-completions', baseURL: 'https://api.example.com/v1', key, mode: 'create' })
      expect(r.status, `锁被占应 409，实得 ${r.status}`).toBe(409)
      expect(readCreds(gw.b.root).includes(key), '没拿到锁时密钥不该写进去').toBe(false)
      expect(r.text.includes(key), '响应里不该有密钥').toBe(false)
    })
  })
})
