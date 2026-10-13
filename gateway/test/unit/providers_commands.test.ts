// 供应商命令与纯函数（方案 3.6、3.7）：/provider add 的识别、刷新文案、上游错误归类、NO_PROXY 追加、身份键。
import { expect, test } from 'bun:test'
import { classifyTurnError, brainKey, identityOf } from '../../src/engine/engine'
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

/** 没有补地址的刷新结果（多数用例） */
const NO_V1 = { v1Added: false, baseURL: 'https://p.example.com/v1' }

test('刷新结果文案（3.4.5/3.6）：成功带统计与去掉了、失败保持原列表、忙/缺密钥/被改/已不在', () => {
  const c = commands()
  expect(c.refreshText('p1', { status: 'ok', name: 'p1', count: 3, added: 2, removed: ['m2'], keptManual: 1, selected: 4, applied: 'full', ...NO_V1 }))
    .toBe('【系统】「p1」拉到 3 个模型（新增 2 个，去掉 1 个；手动加的 1 个保留）。\n去掉了：m2')
  expect(c.refreshText('p1', { status: 'ok', name: 'p1', count: 2, added: 0, removed: [], keptManual: 0, selected: 1, applied: 'full', ...NO_V1 }))
    .toBe('【系统】「p1」拉到 2 个模型（新增 0 个，去掉 0 个；手动加的 0 个保留）。')
  // 带 only 落盘：末尾追加「（按勾选保留 S 个）」（INTERFACE §11b）
  expect(c.refreshText('p1', { status: 'ok', name: 'p1', count: 3, added: 1, removed: ['m2'], keptManual: 1, selected: 3, applied: 'selection', ...NO_V1 }))
    .toBe('【系统】「p1」拉到 3 个模型（新增 1 个，去掉 1 个；手动加的 1 个保留）。\n去掉了：m2（按勾选保留 3 个）')
  // 这次是在 /v1 下才拉到的：末尾再接「（地址补成了 …）」，排在「（按勾选保留 S 个）」之后
  const V1 = { v1Added: true, baseURL: 'https://p.example.com/v1' }
  expect(c.refreshText('p1', { status: 'ok', name: 'p1', count: 2, added: 1, removed: [], keptManual: 0, selected: 2, applied: 'selection', ...V1 }))
    .toBe('【系统】「p1」拉到 2 个模型（新增 1 个，去掉 0 个；手动加的 0 个保留）。（按勾选保留 2 个）（地址补成了 https://p.example.com/v1）')
  expect(c.refreshText('p1', { status: 'ok', name: 'p1', count: 2, added: 2, removed: [], keptManual: 0, selected: 2, applied: 'full', ...V1 }))
    .toBe('【系统】「p1」拉到 2 个模型（新增 2 个，去掉 0 个；手动加的 0 个保留）。（地址补成了 https://p.example.com/v1）')
  expect(c.refreshText('p1', { status: 'failed', name: 'p1', reason: 'server_error', status2: 500 })).toBe('【系统】「p1」没拉到模型：对方接口出错（HTTP 500）。模型列表保持不变。')
  expect(c.refreshText('p1', { status: 'gone', name: 'p1' })).toContain('已经不在了，这次结果没保存')
  expect(c.refreshText('p1', { status: 'changed', name: 'p1' })).toContain('刚被别处改过，这次结果没保存，请再刷新一次')
  expect(c.refreshText('p1', { status: 'list_changed', name: 'p1' })).toBe('【系统】清单刚被别处改过，这次没保存，请重新刷新模型列表。')
  expect(c.refreshText('p1', { status: 'busy', name: 'p1' })).toBe('【系统】「p1」正在刷新，请稍候。')
  expect(c.refreshText('p1', { status: 'key_missing' })).toContain('缺密钥')
  expect(c.refreshText('p1', { status: 'lock_timeout' })).toBe('【系统】别的 bot 正在改供应商，请稍后再试。')
})

test('上游错误归类（3.2 第 10、11 条）：只看文字、不分大小写；无关错误归成普通失败', () => {
  for (const m of ["400 This model's maximum context length is 8192 tokens", 'context_length_exceeded', 'Prompt Is Too Long: 1', 'TOO MANY TOKENS', 'input exceeds context_window', 'exceeds the context length']) expect(classifyTurnError(m)).toBe('context-overflow')
  for (const m of ['400 Missing `reasoning_content` field', 'Thinking blocks are required']) expect(classifyTurnError(m)).toBe('thinking-history')
  expect(classifyTurnError('upstream returned 502 Bad Gateway')).toBe('other')
  expect(classifyTurnError('')).toBe('other')
})

test('报错归类的先后：上游拒绝排在上下文超长、历史思考被拒前面', () => {
  for (const m of ['MISSING_CREDENTIAL deepseek-official requires DEEPSEEK_API_KEY', 'upstream returned 401 Unauthorized', 'Insufficient Balance', 'invalid api key', 'HTTP 403', 'quota exceeded']) expect(classifyTurnError(m)).toBe('fatal')
  expect(classifyTurnError('401 unauthorized: prompt is too long')).toBe('fatal')
  expect(classifyTurnError('402 insufficient balance while sending thinking blocks')).toBe('fatal')
  // 400、500 这类不在"拒绝"里；网关拼在前面的 -32603 也不会被当成 403
  expect(classifyTurnError('-32603 session/prompt: -32603 turn failed: upstream returned 500 Internal Server Error')).toBe('other')
})

test('空内容报错：turn failed 之后整句是那两句之一才算，前后多一截都不算', () => {
  // 网关实际拿到的样子：<code> <method>: <code> <dsh 的 message>
  const wrap = (m: string) => `-32603 session/prompt: -32603 ${m}`
  const custom = 'Internal error: turn failed: model "gemini-3.6-flash" returned a completed response with no content'
  const builtin = 'Internal error: turn failed: DeepSeek Messages returned no content'
  for (const m of [custom, builtin, wrap(custom), wrap(builtin), 'turn failed: model "" returned a completed response with no content']) expect(classifyTurnError(m)).toBe('empty-response')
  for (const m of [
    'Internal error: turn failed: 502: upstream returned no content',
    'Internal error: turn failed: upstream said: model "m" returned a completed response with no content',
    `${custom}.`,
    `${custom} (retry later)`,
    `${builtin}\nmore`,
    'Internal error: turn failed: deepseek messages returned no content',
    'model "m" returned a completed response with no content',
    'DeepSeek Messages returned no content',
    // turn failed 出现两次：算的是第一个之后的整段
    `Internal error: turn failed: wrapped: turn failed: DeepSeek Messages returned no content`,
    // 名字里带引号：引号对不上，认不出来就按普通失败（退回原来的处理）
    'Internal error: turn failed: model "a"b" returned a completed response with no content',
  ]) expect(classifyTurnError(m), m).toBe('other')
})

test('报错归类不看 model、provider 后面引号里的名字；引号外面和别处的引号照旧参与', () => {
  const odd = 'x-thinking-401-quota'
  // 名字本身带着三类关键词：空内容仍是空内容，上游 503 仍是普通失败
  expect(classifyTurnError(`-32603 session/prompt: -32603 Internal error: turn failed: model "${odd}" returned a completed response with no content`)).toBe('empty-response')
  expect(classifyTurnError(`turn failed: model "${odd}" is overloaded, upstream returned 503 Service Unavailable`)).toBe('other')
  expect(classifyTurnError(`no adapter registered for provider "my-balance-403-thinking"`)).toBe('other')
  // 引号外面的文字照旧
  expect(classifyTurnError(`turn failed: model "${odd}" rejected: upstream returned 401 Unauthorized`)).toBe('fatal')
  expect(classifyTurnError(`turn failed: model "plain" says prompt is too long`)).toBe('context-overflow')
  expect(classifyTurnError(`turn failed: model "plain" needs reasoning_content`)).toBe('thinking-history')
  // 上游用 JSON 报的错，关键词在别的引号里：不能被去掉
  expect(classifyTurnError('turn failed: 400 {"error":{"code":"context_length_exceeded","message":"too long"}}')).toBe('context-overflow')
  expect(classifyTurnError('turn failed: 400 {"error":{"message":"Missing `reasoning_content` field","param":"model"}}')).toBe('thinking-history')
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
import type { PreviewResult, RefreshResult, ProviderService, TestConnResult } from '../../src/providers/service'

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

function apiCmd(o: { root: string; refresh?: (name: string, how: { only?: string[]; rev?: string }) => Promise<RefreshResult>; preview?: () => Promise<PreviewResult>; testConn?: () => Promise<TestConnResult>; credential?: (env: string) => string | null; configRoutes?: string[] }): ProviderCommands {
  const service = { credential: o.credential ?? (() => 'sekret'), refresh: o.refresh ?? (async () => ({ status: 'ok', name: 'p1', count: 3, added: 2, removed: ['m2'], keptManual: 1, selected: 4, applied: 'full', ...NO_V1 })), preview: o.preview, testConn: o.testConn } as unknown as ProviderService
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

test('apiRefresh：only 与 rev 原样交给服务层；带 only 时 removed 是数组、applied 为 selection；清单指纹对不上 409 list_changed 与固定文案', async () => {
  await withRoot(async (root, put) => {
    put(snapOf(['p1', entry('p1')]))
    const seen: unknown[] = []
    const c = apiCmd({ root, refresh: async (_n, how) => { seen.push(how); return { status: 'ok', name: 'p1', count: 3, added: 1, removed: ['m2'], keptManual: 0, selected: 2, applied: 'selection', ...NO_V1 } } })
    const r = await c.apiRefresh('p1', { only: ['m1', 'm3'], rev: 'abc' })
    expect(seen).toEqual([{ only: ['m1', 'm3'], rev: 'abc' }])
    expect(r).toMatchObject({ status: 200, body: { ok: true, removed: ['m2'], selected: 2, applied: 'selection' } })
    expect(await apiCmd({ root, refresh: async () => ({ status: 'list_changed', name: 'p1' }) }).apiRefresh('p1', { only: ['m1'], rev: 'old' }))
      .toEqual({ status: 409, body: { ok: false, error: 'list_changed', text: '清单刚被别处改过，这次没保存，请重新刷新模型列表。' } })
  })
})

test('apiPreview：200 体顶层是 ok、count、models、list_rev 四个键', async () => {
  await withRoot(async (root, put) => {
    put(snapOf(['p1', entry('p1')]))
    const row = { id: 'm1', contextWindow: null, in_list: true, in_fetched: true, manual: false, default_checked: true }
    const r = await apiCmd({ root, preview: async () => ({ status: 'ok', name: 'p1', count: 1, models: [row], listRev: 'rev-1' }) }).apiPreview('p1')
    expect(r).toEqual({ status: 200, body: { ok: true, count: 1, models: [row], list_rev: 'rev-1' } })
  })
})

test('apiTest：对方回的不是接口返回、保存的地址又只有域名时，失败文案末尾多一句提示；别的原因或地址带路径时一字不变', async () => {
  await withRoot(async (root, put) => {
    put(snapOf(['p1', entry('p1')]))
    const failed = (reason: 'bad_response' | 'unauthorized', bareHost: boolean) => apiCmd({ root, testConn: async () => ({ status: 'failed', name: 'p1', model: 'm1', reason, status2: reason === 'unauthorized' ? 401 : 200, bareHost }) }).apiTest('p1')
    expect(await failed('bad_response', true)).toEqual({ status: 502, body: { ok: false, error: 'connect_failed', reason: 'bad_response', text: '对方返回的不是模型列表（格式不认识）；用 m1 测的。地址只写了域名，可能少写了 /v1' } })
    expect((await failed('bad_response', false)).body).toMatchObject({ text: '对方返回的不是模型列表（格式不认识）；用 m1 测的' })
    expect((await failed('unauthorized', true)).body).toMatchObject({ text: '密钥不对或没有权限（HTTP 401）；用 m1 测的' })
  })
})

// ─── /provider remove（方案 3.6）：复用 ProviderService.remove，保持我们"密钥延迟删"的语义（D8） ───

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

type RemoveResult = { status: 'ok'; name: string; keyEnv: string | null; graceMs: number } | { status: 'not_found' } | { status: 'lock_timeout' } | { status: 'unreadable' } | { status: 'save_failed'; why: string }

function removeCmd(o: { root: string; remove?: (name: string) => Promise<RemoveResult>; configRoutes?: string[]; engine?: { current: () => { provider: string; model: string }; clearOverride: () => { provider: string; model: string } } }): ProviderCommands {
  const service = { remove: o.remove ?? (async (name: string) => ({ status: 'ok', name, keyEnv: 'PROVIDER_P1_KEY', graceMs: 600_000 })) } as unknown as ProviderService
  return new ProviderCommands({ service, api: {} as never, log: new Logger({}), root: o.root, isOwner: () => true, botUsername: () => 'b', configRouteNames: () => o.configRoutes ?? [], reply: () => {}, recordUpdate: () => {}, engine: o.engine })
}

test('runRemove：参数不对 / 读不了文件 / 内置、配置文件、不合格、不存在', async () => {
  await withRoot(async (root) => {
    const c = removeCmd({ root, configRoutes: ['myroute'] })
    expect(await c.runRemove('')).toBe('【系统】用法：/provider remove <名字>')
    expect(await c.runRemove('a b')).toBe('【系统】用法：/provider remove <名字>')
    expect(await c.runRemove('nosuch')).toBe('【系统】没有 nosuch 这个供应商。用 /provider 看有哪些。')
    expect(await c.runRemove('deepseek-official')).toBe('【系统】只有自建供应商能删除；deepseek-official 是内置的。')
    expect(await c.runRemove('myroute')).toBe('【系统】只有自建供应商能删除；myroute 是配置文件里的。')
  })

  await withRoot(async (root, put) => {
    put(snapOf(['p1', entry('p1')], ['broken', { route: { api: 'grpc', baseURL: 'https://x/v1', apiKeyEnv: 'PROVIDER_B_KEY', models: [] }, meta: {} }]))
    expect(await removeCmd({ root }).runRemove('broken')).toBe('【系统】「broken」配置有误，未启用，不能删除。')
  })

  await withRoot(async (root) => {
    expect(await removeCmd({ root }).runRemove('p1')).toContain('共用供应商文件读不了（格式坏了），先修好')
  }, { raw: '[[[' })
})

test('runRemove：成功走 service.remove（延迟删密钥），名字按规整名匹配', async () => {
  await withRoot(async (root, put) => {
    put(snapOf(['p1', entry('p1')]))
    let removed = ''
    const c = removeCmd({ root, remove: async (name) => { removed = name; return { status: 'ok', name, keyEnv: 'PROVIDER_P1_KEY', graceMs: 600_000 } } })
    const t = await c.runRemove('P1')
    expect(removed).toBe('p1')
    expect(t).toContain('已删除供应商「p1」')
    expect(t).toContain('密钥会在 10 分钟后从凭据文件删除')
    expect(t).toContain('不会在供应商那边作废这把密钥')
  })
})

test('runRemove：删正在用的那家，文案与引导「删除」一致（换回配置文件里的模型）', async () => {
  await withRoot(async (root, put) => {
    put(snapOf(['p1', entry('p1')]))
    let cleared = 0
    const engine = { current: () => ({ provider: 'p1', model: 'm1' }), clearOverride: () => { cleared++; return { provider: 'deepseek-official', model: 'deepseek-flash' } } }
    const t = await removeCmd({ root, engine }).runRemove('p1')
    expect(cleared).toBe(1)
    expect(t).toContain('这个 bot 已换回配置文件里的模型：deepseek-official / deepseek-flash。')
  })
})

test('runRemove：服务端失败文案（忙 / 读不了 / 写失败 / 已不在）', async () => {
  await withRoot(async (root, put) => {
    put(snapOf(['p1', entry('p1')]))
    expect(await removeCmd({ root, remove: async () => ({ status: 'lock_timeout' }) }).runRemove('p1')).toBe('【系统】没删成：别的 bot 正在改供应商，请稍后再试。')
    expect(await removeCmd({ root, remove: async () => ({ status: 'unreadable' }) }).runRemove('p1')).toBe('【系统】没删成：共用供应商文件读不了（格式坏了）。')
    expect(await removeCmd({ root, remove: async () => ({ status: 'save_failed', why: '写文件失败（Error）' }) }).runRemove('p1')).toBe('【系统】没删成：写文件失败（Error）。')
    expect(await removeCmd({ root, remove: async () => ({ status: 'not_found' }) }).runRemove('p1')).toBe('【系统】「p1」已经不在了。')
  })
})
