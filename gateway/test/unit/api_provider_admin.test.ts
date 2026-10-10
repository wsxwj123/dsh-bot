// 白盒：本机接口（INTERFACE-管理台UI 3.1 到 3.5）里验收测试够不到的判定分支。
// 用假 service 与临时目录，只测判定与错误码映射，不碰真存储、不联网。
import { expect, test } from 'bun:test'
import { mkdtempSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { Logger } from '../../src/log'
import { ProviderCommands, type ApiResult } from '../../src/providers/commands'
import type { ProviderService } from '../../src/providers/service'

const entry = (name: string, api = 'openai-completions') => ({
  route: { api, baseURL: 'https://x.example.com/v1', apiKeyEnv: `PROVIDER_${name.toUpperCase().replace(/-/g, '_')}_KEY`, models: [{ id: 'm1', contextWindow: 131_072 }] },
  meta: {
    createdAt: 1, updatedAt: 1, epoch: 1, keyRev: 1, guessedContext: [], ownerContext: [], manualModels: [],
    lastRefresh: { at: 1_700_000_000_000, ok: true, count: 1, reason: null },
  },
})

async function withRoot(providers: Record<string, unknown>, fn: (c: ProviderCommands) => Promise<void>): Promise<void> {
  const root = mkdtempSync(join(tmpdir(), 'dshapi-'))
  try {
    writeFileSync(join(root, 'providers.json'), JSON.stringify({ version: 1, providers }, null, 1))
    const service = { credential: () => 'test-key-7x-000000000001' } as unknown as ProviderService
    const c = new ProviderCommands({
      service, api: {} as never, log: new Logger({}), root,
      isOwner: () => true, botUsername: () => 'b', configRouteNames: () => ['myroute'], reply: () => {}, recordUpdate: () => {},
    })
    await fn(c)
  } finally { rmSync(root, { recursive: true, force: true }) }
}

const find = (body: any, name: string) => (body.providers as any[]).find(p => p.name === name)

test('apiDetail：lastRefresh 原样透传；被配置文件路由同名遮住的 enabled=false 且 note 说同名', async () => {
  await withRoot({ myproxy: entry('myproxy'), MyRoute: entry('MyRoute') }, async c => {
    const r = await c.apiDetail() as ApiResult
    expect(r.status).toBe(200)
    const body = r.body as any
    expect(find(body, 'myproxy'), '正常的自建：key 是状态词，lastRefresh 原样').toMatchObject({
      enabled: true, note: null, key: 'ok',
      lastRefresh: { at: 1_700_000_000_000, ok: true, count: 1, reason: null },
    })
    const shadowed = find(body, 'myroute')
    expect(shadowed, '被遮住的自建用规整名列出').toBeTruthy()
    expect(shadowed.enabled).toBe(false)
    expect(String(shadowed.note)).toContain('同名')
  })
})

test('apiRemove：内置名与配置文件里的路由名都回 404（它们不在自建列表里）', async () => {
  await withRoot({ myproxy: entry('myproxy') }, async c => {
    for (const name of ['deepseek-official', 'myroute', 'nosuch']) {
      const r = await c.apiRemove(name) as ApiResult
      expect(r.status, `${name} 应 404`).toBe(404)
      expect(r.body).toMatchObject({ ok: false, error: 'not_found' })
      expect(String((r.body as any).text)).toContain('没有')
    }
  })
})

test('apiRemove / apiModelEdit：名字不合规先回 400 bad_name', async () => {
  await withRoot({ myproxy: entry('myproxy') }, async c => {
    for (const name of ['bad.name', '', 123, undefined]) {
      expect((await c.apiRemove(name) as ApiResult).status, `${JSON.stringify(name)} 删应 400`).toBe(400)
      expect(((await c.apiModelEdit({ name, action: 'add', id: 'x' }) as ApiResult).body as any).error).toBe('bad_name')
    }
  })
})

test('apiModelEdit：供应商找不到 / action 与 id 与上下文的校验顺序', async () => {
  await withRoot({ myproxy: entry('myproxy') }, async c => {
    const notFound = await c.apiModelEdit({ name: 'nosuch', action: 'add', id: 'x' }) as ApiResult
    expect(notFound).toMatchObject({ status: 404, body: { ok: false, error: 'not_found' } })
    expect(String((notFound.body as any).text)).toContain('nosuch')
    const bad = await c.apiModelEdit({ name: 'myproxy', action: 'add2', id: 'x' }) as ApiResult
    expect(bad).toMatchObject({ status: 400, body: { error: 'bad_action' } })
    const badId = await c.apiModelEdit({ name: 'myproxy', action: 'add', id: 'a b' }) as ApiResult
    expect(badId).toMatchObject({ status: 400, body: { error: 'bad_id' } })
    const noCtx = await c.apiModelEdit({ name: 'myproxy', action: 'set_context', id: 'm1' }) as ApiResult
    expect(noCtx).toMatchObject({ status: 400, body: { error: 'bad_context' } })
  })
})
