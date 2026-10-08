// 供应商命令与纯函数（方案 3.6、3.7）：/provider add 的识别、刷新文案、上游错误归类、NO_PROXY 追加、身份键。
import { expect, test } from 'bun:test'
import { classifyUpstreamError, brainKey, identityOf } from '../../src/engine/engine'
import { isProviderAdd, ProviderCommands } from '../../src/providers/commands'
import { Logger } from '../../src/log'
import { addNoProxyHosts } from '../../src/util'
import type { Brain } from '../../src/config'

test('/provider add 的识别：文字/附言、去空白、可选 @bot、不分大小写；别的不认', () => {
  for (const ok of ['/provider add a b c', '  /provider   add x  ', '/PROVIDER ADD x', '/provider@test_dsh_bot add a b c', '/provider add']) expect(isProviderAdd(ok)).toBe(true)
  for (const no of ['/provider', '/provider addx', '/provider refresh a', 'provider add a', '/model add a']) expect(isProviderAdd(no)).toBe(false)
  expect(isProviderAdd(undefined)).toBe(false)
})

function commands(): ProviderCommands {
  return new ProviderCommands({
    service: {} as never, api: {} as never, log: new Logger({}), root: '/root',
    isOwner: () => true, botUsername: () => 'test_dsh_bot', configRouteNames: () => [],
    reply: () => {}, recordUpdate: () => {},
  })
}

test('刷新结果文案（3.4.5/3.6）：成功带统计与去掉了、失败保持原列表、忙/缺密钥/被改/已不在', () => {
  const c = commands()
  expect(c.refreshText('p1', { status: 'ok', name: 'p1', count: 3, added: 2, removed: ['m2'], keptManual: 1 }))
    .toBe('【系统】「p1」拉到 3 个模型（新增 2 个，去掉 1 个；手动加的 1 个保留）。\n去掉了：m2')
  expect(c.refreshText('p1', { status: 'ok', name: 'p1', count: 2, added: 0, removed: [], keptManual: 0 }))
    .toBe('【系统】「p1」拉到 2 个模型（新增 0 个，去掉 0 个；手动加的 0 个保留）。')
  expect(c.refreshText('p1', { status: 'failed', name: 'p1', reason: 'server_error', status2: 500 })).toBe('【系统】「p1」没拉到模型：对方接口出错（HTTP 500）。模型列表保持不变。')
  expect(c.refreshText('p1', { status: 'gone', name: 'p1' })).toContain('已经不在了，这次结果没保存')
  expect(c.refreshText('p1', { status: 'changed', name: 'p1' })).toContain('刚被别处改过，这次结果没保存，请再刷新一次')
  expect(c.refreshText('p1', { status: 'busy', name: 'p1' })).toBe('【系统】「p1」正在刷新，请稍候。')
  expect(c.refreshText('p1', { status: 'key_missing' })).toContain('缺密钥')
  expect(c.refreshText('p1', { status: 'lock_timeout' })).toBe('【系统】别的 bot 正在改供应商，请稍后再试。')
})

test('上游错误归类（3.2 第 10、11 条）：只看文字、不分大小写；无关错误不归类', () => {
  for (const m of ["400 This model's maximum context length is 8192 tokens", 'context_length_exceeded', 'Prompt Is Too Long: 1', 'TOO MANY TOKENS', 'input exceeds context_window', 'exceeds the context length']) expect(classifyUpstreamError(m)).toBe('context-overflow')
  for (const m of ['400 Missing `reasoning_content` field', 'Thinking blocks are required']) expect(classifyUpstreamError(m)).toBe('thinking-history')
  expect(classifyUpstreamError('upstream returned 502 Bad Gateway')).toBe(null)
})

test('供应商身份与键：brainKey 取名字/模型/强度；identityOf 取第一个 / 前', () => {
  const b: Brain = { provider: 'deepseek-official', model: 'deepseek-flash', reasoningEffort: 'low', routes: {}, emergencyCompaction: false }
  expect(brainKey(b)).toBe('deepseek-official/deepseek-flash/low')
  expect(identityOf('myproxy@1700/m1/')).toBe('myproxy@1700')
  expect(identityOf('deepseek-official/deepseek-flash/')).toBe('deepseek-official')
})

test('NO_PROXY 追加：保留原有值、去重、NO_PROXY 与 no_proxy 都写', () => {
  const env: Record<string, string | undefined> = { NO_PROXY: 'example.com', no_proxy: '127.0.0.1' }
  addNoProxyHosts(env)
  expect(env.NO_PROXY!.split(',')).toContain('example.com')
  for (const h of ['127.0.0.1', 'localhost', '::1']) expect(env.NO_PROXY!.split(',')).toContain(h)
  expect(env.no_proxy!.split(',').filter(x => x === '127.0.0.1').length).toBe(1) // 去重
  expect(env.no_proxy!.split(',')).toContain('localhost')
  // 原本没有就新建
  const empty: Record<string, string | undefined> = {}
  addNoProxyHosts(empty)
  expect(empty.NO_PROXY).toBe('127.0.0.1,localhost,::1')
})

// ─── POST /v1/provider/refresh 的状态码契约（方案 3.8） ───

import { mkdtempSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import type { RefreshResult, ProviderService } from '../../src/providers/service'

/** 一个带 providers.json 的临时根，测完删掉 */
function withRoot(fn: (root: string, put: (providers: unknown) => void) => Promise<void>, opts: { raw?: string } = {}): Promise<void> {
  const root = mkdtempSync(join(tmpdir(), 'dshunit-'))
  const put = (providers: unknown) => writeFileSync(join(root, 'providers.json'), typeof providers === 'string' ? providers : JSON.stringify(providers))
  if (opts.raw !== undefined) writeFileSync(join(root, 'providers.json'), opts.raw)
  return fn(root, put).finally(() => rmSync(root, { recursive: true, force: true }))
}

const entry = (name: string, apiKeyEnv = 'PROVIDER_P1_KEY') => ({
  route: { api: 'openai-completions', baseURL: 'https://p.example.com/v1', apiKeyEnv, models: [{ id: 'm1', contextWindow: 131072 }] },
  meta: { createdAt: 1, updatedAt: 1, epoch: 1, keyRev: 1, guessedContext: [], ownerContext: [], manualModels: [], lastRefresh: null },
})
const snapOf = (...entries: [string, unknown][]) => ({ version: 1, providers: Object.fromEntries(entries), pendingKeyRemovals: [] })

function apiCmd(o: { root: string; refresh?: () => Promise<RefreshResult>; credential?: (env: string) => string | null; configRoutes?: string[] }): ProviderCommands {
  const service = { credential: o.credential ?? (() => 'sekret'), refresh: o.refresh ?? (async () => ({ status: 'ok', name: 'p1', count: 3, added: 2, removed: ['m2'], keptManual: 1 })) } as unknown as ProviderService
  return new ProviderCommands({ service, api: {} as never, log: new Logger({}), root: o.root, isOwner: () => true, botUsername: () => 'b', configRouteNames: () => o.configRoutes ?? [], reply: () => {}, recordUpdate: () => {} })
}

test('apiRefresh：成功 200，去掉【系统】，带统计', async () => {
  await withRoot(async (root, put) => {
    put(snapOf(['p1', entry('p1')]))
    const r = await apiCmd({ root }).apiRefresh('P1') // 规整名匹配
    expect(r.status).toBe(200)
    expect(r.body).toMatchObject({ ok: true, count: 3, added: 2, removed: 1, kept_manual: 1 })
    expect(r.body.text).toContain('「p1」拉到 3 个模型（新增 2 个，去掉 1 个；手动加的 1 个保留）。')
    expect(r.body.text.startsWith('【系统】')).toBe(false)
  })
})

test('apiRefresh：读不了 503、内置/配置文件 400 not_custom、不存在 404、不合格 409 disabled', async () => {
  await withRoot(async (root) => {
    expect((await apiCmd({ root }).apiRefresh('p1')).body.error).toBe('providers_unreadable')
    expect((await apiCmd({ root }).apiRefresh('p1')).status).toBe(503)
  }, { raw: '[[[' })

  await withRoot(async (root, put) => {
    put(snapOf(['p1', entry('p1')], ['broken', { route: { api: 'grpc', baseURL: 'https://x/v1', apiKeyEnv: 'PROVIDER_B_KEY', models: [] }, meta: {} }]))
    const c = apiCmd({ root, configRoutes: ['myroute'] })
    expect(await c.apiRefresh('deepseek-official')).toMatchObject({ status: 400, body: { error: 'not_custom' } })
    expect(await c.apiRefresh('myroute')).toMatchObject({ status: 400, body: { error: 'not_custom' } })
    expect(await c.apiRefresh('nosuch')).toMatchObject({ status: 404, body: { error: 'not_found' } })
    expect(await c.apiRefresh('broken')).toMatchObject({ status: 409, body: { error: 'disabled' } })
  })
})

test('apiRefresh：缺密钥 409 key_missing；刷新失败 502 fetch_failed 带 reason；被删/被改 409 changed；忙 409 busy', async () => {
  await withRoot(async (root, put) => {
    put(snapOf(['p1', entry('p1')]))
    expect(await apiCmd({ root, credential: () => null }).apiRefresh('p1')).toMatchObject({ status: 409, body: { error: 'key_missing' } })
    expect(await apiCmd({ root, refresh: async () => ({ status: 'failed', name: 'p1', reason: 'server_error', status2: 500 }) }).apiRefresh('p1'))
      .toMatchObject({ status: 502, body: { ok: false, error: 'fetch_failed', reason: 'server_error' } })
    expect(await apiCmd({ root, refresh: async () => ({ status: 'gone', name: 'p1' }) }).apiRefresh('p1')).toMatchObject({ status: 409, body: { error: 'changed' } })
    expect(await apiCmd({ root, refresh: async () => ({ status: 'changed', name: 'p1' }) }).apiRefresh('p1')).toMatchObject({ status: 409, body: { error: 'changed' } })
    expect(await apiCmd({ root, refresh: async () => ({ status: 'busy', name: 'p1' }) }).apiRefresh('p1')).toMatchObject({ status: 409, body: { error: 'busy' } })
    expect(await apiCmd({ root, refresh: async () => ({ status: 'lock_timeout' }) }).apiRefresh('p1')).toMatchObject({ status: 409, body: { error: 'busy' } })
  })
})
