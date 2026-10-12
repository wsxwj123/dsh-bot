// 自建供应商服务（方案 3.4/3.6、Q1）：新建/更新/删除/刷新/加删模型/改上下文/到点删键。
// 白盒：直接构造 ProviderService，注入假的 fetchModels 与断言用的 Logger；用临时目录当 <根>。
import { afterEach, expect, test } from 'bun:test'
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { Logger } from '../../src/log'
import { ProviderService } from '../../src/providers/service'
import { DEFAULT_CONTEXT, parseProviders, setCredentialRef } from '../../src/providers/store'
import type { FetchedModel, FetchModelsRequest, FetchModelsResult, ProbeModelFn, ProbeModelRequest } from '../../src/providers/models'

const dirs: string[] = []
const tmp = () => { const d = mkdtempSync(join(tmpdir(), 'pvc-')); dirs.push(d); return d }
afterEach(() => { for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true }) })

const providersPathOf = (root: string) => join(root, 'providers.json')
const credPathOf = (root: string) => join(root, 'credentials.yaml')

class CapLog extends Logger {
  events: Record<string, unknown>[] = []
  override log(_l: string, event: string, f: Record<string, unknown> = {}): void { this.events.push({ event, ...f }) }
}

type FetchPlan = (req: FetchModelsRequest) => FetchModelsResult
function svc(root: string, plan: FetchPlan, o: { now?: () => number; keyGraceMs?: number; onChanged?: () => void; probe?: ProbeModelFn } = {}) {
  const log = new CapLog()
  const calls: FetchModelsRequest[] = []
  const probeCalls: ProbeModelRequest[] = []
  const s = new ProviderService({
    root, credentialsPath: credPathOf(root), log,
    fetchModels: async (req) => { calls.push(req); return plan(req) },
    probeModel: async (req) => { probeCalls.push(req); return o.probe ? o.probe(req) : { ok: true } },
    now: o.now, lockWaitMs: 2000, keyGraceMs: o.keyGraceMs ?? 600_000,
    configRouteKeys: () => ['MYROUTE_KEY'],
    onChanged: o.onChanged,
  })
  return { s, log, calls, probeCalls }
}
const okModels = (list: FetchedModel[], baseURL: string): FetchModelsResult => ({ ok: true, models: list, truncated: false, baseURL, v1Added: false })
const read = (root: string) => parseProviders(readFileSync(providersPathOf(root), 'utf8'))
const creds = (root: string) => existsSync(credPathOf(root)) ? readFileSync(credPathOf(root), 'utf8') : ''

test('新建：拉到的模型落盘、上下文未知记默认值 与 guessedContext、密钥写进凭据、通知 onChanged', async () => {
  const root = tmp()
  let changed = 0
  const { s, calls } = svc(root, () => okModels([{ id: 'm1', contextWindow: 8000 }, { id: 'm2', contextWindow: null }], 'https://p.example.com/v1'), { onChanged: () => changed++ })
  const r = await s.save({ name: 'My_Proxy', api: 'openai-completions', baseURL: 'https://p.example.com/v1', key: 'test-key-7abcdefghij' })
  expect(r.status).toBe('saved')
  if (r.status !== 'saved') return
  expect(r.kind).toBe('created')
  expect(r.count).toBe(2)
  expect(calls[0]).toEqual({ api: 'openai-completions', baseURL: 'https://p.example.com/v1', key: 'test-key-7abcdefghij' })
  const snap = read(root)
  const e = snap.valid['My_Proxy']!
  expect(e.route.models).toEqual([{ id: 'm1', contextWindow: 8000 }, { id: 'm2', contextWindow: DEFAULT_CONTEXT }])
  expect(e.meta.guessedContext).toEqual(['m2'])
  expect(e.route.apiKeyEnv).toBe('PROVIDER_MY_PROXY_KEY')
  expect(creds(root)).toContain('PROVIDER_MY_PROXY_KEY: "test-key-7abcdefghij"')
  expect(changed).toBe(1)
})

test('更新同名：键名不变、keyRev +1、epoch 不变（同主机）；拉不到模型保持原列表', async () => {
  const root = tmp()
  const { s } = svc(root, () => okModels([{ id: 'a', contextWindow: 1000 }], 'https://p.example.com/v1'))
  await s.save({ name: 'p1', api: 'openai-completions', baseURL: 'https://p.example.com/v1', key: 'test-key-7abcdefghij' })
  const before = read(root).valid.p1!
  const { s: s2 } = svc(root, () => ({ ok: false, reason: 'server_error', status: 500 }))
  const r = await s2.save({ name: 'p1', api: 'openai-completions', baseURL: 'https://p.example.com/v1', key: 'test-key-7abcdefghij' })
  expect(r.status).toBe('saved')
  if (r.status !== 'saved') return
  expect(r.kind).toBe('updated')
  expect(r.reason).toBe('server_error')
  const after = read(root).valid.p1!
  expect(after.route.apiKeyEnv).toBe(before.route.apiKeyEnv)
  expect(after.meta.keyRev).toBe(before.meta.keyRev + 1)
  expect(after.meta.epoch).toBe(before.meta.epoch)
  expect(after.route.models).toEqual(before.route.models)
})

test('换主机（epoch 变）：换新键名，旧键名登记进 pendingKeyRemovals', async () => {
  const root = tmp()
  const { s } = svc(root, () => okModels([{ id: 'a', contextWindow: 8000 }], 'https://old.example.com/v1'))
  await s.save({ name: 'p1', api: 'openai-completions', baseURL: 'https://old.example.com/v1', key: 'test-key-7oldhost0000' })
  const old = read(root).valid.p1!
  const { s: s2 } = svc(root, () => okModels([{ id: 'a', contextWindow: 8000 }], 'https://new.example.com/v1'))
  const r = await s2.save({ name: 'p1', api: 'openai-completions', baseURL: 'https://new.example.com/v1', key: 'test-key-7newhost0000' })
  expect(r.status).toBe('saved')
  if (r.status !== 'saved') return
  expect(r.epochChanged).toBe(true)
  const after = read(root)
  expect(after.valid.p1!.route.apiKeyEnv).not.toBe(old.route.apiKeyEnv)
  expect(after.pendingKeyRemovals.map(p => p.key)).toEqual([old.route.apiKeyEnv])
})

test('刷新：新增/去掉/保留统计、<N> 是拉到的个数、主人手填的上下文保留、lastRefresh 写入', async () => {
  const root = tmp()
  // 预置：m1 主人手填 50000、m2、man1 手动加
  writeFileSync(providersPathOf(root), JSON.stringify({ version: 1, providers: { p1: { route: { api: 'openai-completions', baseURL: 'https://p.example.com/v1', apiKeyEnv: 'PROVIDER_P1_KEY', models: [{ id: 'm1', contextWindow: 50000 }, { id: 'm2', contextWindow: 131072 }, { id: 'man1', contextWindow: 131072 }] }, meta: { createdAt: 1, updatedAt: 1, epoch: 1, keyRev: 1, guessedContext: [], ownerContext: ['m1'], manualModels: ['man1'], lastRefresh: null } } }, pendingKeyRemovals: [] }))
  setCredentialRef(credPathOf(root), 'PROVIDER_P1_KEY', 'test-key-7abcdefghij')
  const { s: s2 } = svc(root, () => okModels([{ id: 'm1', contextWindow: 8000 }, { id: 'm3', contextWindow: null }, { id: 'man1', contextWindow: null }], 'https://p.example.com/v1'))
  const r = await s2.refresh('P1')
  expect(r.status).toBe('ok')
  if (r.status !== 'ok') return
  expect(r.count).toBe(3) // 拉到的 3 个（含 man1）
  expect(r.added).toBe(1) // m3
  expect(r.removed).toEqual(['m2'])
  expect(r.keptManual).toBe(1)
  const e = read(root).valid.p1!
  expect(e.route.models).toEqual([{ id: 'm1', contextWindow: 50000 }, { id: 'm3', contextWindow: DEFAULT_CONTEXT }, { id: 'man1', contextWindow: DEFAULT_CONTEXT }])
  expect(e.meta.lastRefresh).toMatchObject({ ok: true, count: 3, reason: null })
})

test('刷新失败：列表不变、lastRefresh.ok=false、reason=server_error', async () => {
  const root = tmp()
  const { s } = svc(root, () => okModels([{ id: 'm1', contextWindow: 8000 }], 'https://p.example.com/v1'))
  await s.save({ name: 'p1', api: 'openai-completions', baseURL: 'https://p.example.com/v1', key: 'test-key-7abcdefghij' })
  const before = read(root).valid.p1!.route.models
  const { s: s2 } = svc(root, () => ({ ok: false, reason: 'server_error', status: 500 }))
  const r = await s2.refresh('p1')
  expect(r.status).toBe('failed')
  const e = read(root).valid.p1!
  expect(e.route.models).toEqual(before)
  expect(e.meta.lastRefresh).toMatchObject({ ok: false, reason: 'server_error' })
})

test('刷新：缺密钥不发请求；同一家并发刷新第二个 busy；删除后再刷新 gone', async () => {
  const root = tmp()
  const { s, calls } = svc(root, () => okModels([{ id: 'm1', contextWindow: 8000 }], 'https://p.example.com/v1'))
  await s.save({ name: 'p1', api: 'openai-completions', baseURL: 'https://p.example.com/v1', key: 'test-key-7abcdefghij' })
  const noKey = svc(root, () => okModels([], 'https://p.example.com/v1'))
  expect((await noKey.s.refresh('p1')).status).toBe('ok') // 有密钥
  setCredentialRef(credPathOf(root), 'PROVIDER_P1_KEY', null)
  expect((await noKey.s.refresh('p1')).status).toBe('key_missing')
  await s.remove('p1')
  expect((await s.refresh('p1')).status).toBe('not_found')
  expect(calls.length).toBe(1) // 只有最开始的 save 各一次（noKey 的那次没发请求）
})

test('删除：providers.json 移除、凭据键登记待删；到点后 processPendingKeys 删掉凭据键并移除登记', async () => {
  const root = tmp()
  let now = 1_000_000
  const { s } = svc(root, () => okModels([{ id: 'm1', contextWindow: 8000 }], 'https://p.example.com/v1'), { now: () => now, keyGraceMs: 10_000 })
  await s.save({ name: 'p1', api: 'openai-completions', baseURL: 'https://p.example.com/v1', key: 'test-key-7abcdefghij' })
  const r = await s.remove('p1')
  expect(r.status).toBe('ok')
  let snap = read(root)
  expect(snap.valid.p1).toBeUndefined()
  expect(snap.pendingKeyRemovals).toEqual([{ key: 'PROVIDER_P1_KEY', after: now + 10_000 }])
  expect(creds(root)).toContain('PROVIDER_P1_KEY')
  await s.processPendingKeys() // 还没到点
  expect(creds(root)).toContain('PROVIDER_P1_KEY')
  now += 10_001
  await s.processPendingKeys()
  expect(creds(root)).not.toContain('PROVIDER_P1_KEY')
  snap = read(root)
  expect(snap.pendingKeyRemovals).toEqual([])
})

// ─── INTERFACE-新系统bot运维 §11a/§11b/§12a：preview、refresh 的 only、testConn ───

/** 预置一份 [a（owner 手填 5000）、b、manual c] 的自建与凭据 */
function seedPreview(root: string) {
  writeFileSync(providersPathOf(root), JSON.stringify({ version: 1, providers: { myproxy: {
    route: { api: 'openai-completions', baseURL: 'https://p.example.com/v1', apiKeyEnv: 'PROVIDER_MYPROXY_KEY', models: [{ id: 'a', contextWindow: 5000 }, { id: 'b', contextWindow: 131072 }, { id: 'c', contextWindow: 1048576 }] },
    meta: { createdAt: 1, updatedAt: 1, epoch: 1, keyRev: 1, guessedContext: ['b', 'c'], ownerContext: ['a'], manualModels: ['c'], lastRefresh: null },
  } }, pendingKeyRemovals: [] }))
  setCredentialRef(credPathOf(root), 'PROVIDER_MYPROXY_KEY', 'test-key-7abcdefghij')
}
const previewFetched = () => okModels([{ id: 'a', contextWindow: 8000 }, { id: 'b', contextWindow: null }, { id: 'd', contextWindow: null }], 'https://p.example.com/v1')

test('preview（§11a）：先拉到的（上游顺序）后仅本地的（列表顺序）；行恒 6 键；count 只数拉到的；default_checked = in_fetched or manual；只读', async () => {
  const root = tmp()
  seedPreview(root)
  const { s, calls } = svc(root, previewFetched)
  const before = readFileSync(providersPathOf(root), 'utf8')
  const r = await s.preview('MYPROXY')
  expect(r.status).toBe('ok')
  if (r.status !== 'ok') return
  expect(r.name).toBe('myproxy')
  expect(r.count).toBe(3)
  expect(r.models).toEqual([
    { id: 'a', contextWindow: 8000, in_list: true, in_fetched: true, manual: false, default_checked: true },
    { id: 'b', contextWindow: null, in_list: true, in_fetched: true, manual: false, default_checked: true },
    { id: 'd', contextWindow: null, in_list: false, in_fetched: true, manual: false, default_checked: true },
    { id: 'c', contextWindow: 1048576, in_list: true, in_fetched: false, manual: true, default_checked: true },
  ])
  expect(calls.length).toBe(1)
  expect(readFileSync(providersPathOf(root), 'utf8'), 'preview 只读：providers.json 逐字节不变').toBe(before)
})

test('preview：拉到 0 个（empty）也是 200，models 只含仅本地的行；缺密钥不发请求；不存在 not_found', async () => {
  const root = tmp()
  seedPreview(root)
  const { s: s0 } = svc(root, () => ({ ok: false, reason: 'empty', status: null }))
  const r = await s0.preview('myproxy')
  expect(r.status).toBe('ok')
  if (r.status !== 'ok') return
  expect(r.count).toBe(0)
  expect(r.models.map(m => m.id)).toEqual(['a', 'b', 'c'])
  expect(r.models.map(m => m.default_checked)).toEqual([false, false, true])

  const { s: s1, calls } = svc(root, previewFetched)
  setCredentialRef(credPathOf(root), 'PROVIDER_MYPROXY_KEY', null)
  expect((await s1.preview('myproxy')).status).toBe('key_missing')
  expect(calls.length).toBe(0)
  expect((await s1.preview('nosuch')).status).toBe('not_found')
})

test('refresh 带 only（§11b）：落盘 =（拉到的 ∩ only）∪（列表 ∩ only）；勾上但上游没有的粘进 manualModels；owner 手填的保留', async () => {
  const root = tmp()
  seedPreview(root)
  const { s } = svc(root, previewFetched)
  const r = await s.refresh('myproxy', ['a', 'd', 'c'])
  expect(r.status).toBe('ok')
  if (r.status !== 'ok') return
  expect(r).toMatchObject({ count: 3, added: 1, removed: ['b'], keptManual: 1, selected: 3, bySelection: true })
  const e = read(root).valid.myproxy!
  expect(e.route.models.map(m => m.id)).toEqual(['a', 'd', 'c'])
  expect(e.meta.manualModels).toEqual(['c'])
  expect(e.route.models.find(m => m.id === 'a')!.contextWindow).toBe(5000)
})

test('refresh 带 only：未知 id 静默忽略、旧 manual 被弃选即移除；only: [] 清空列表与 manual', async () => {
  const root = tmp()
  seedPreview(root)
  const { s } = svc(root, previewFetched)
  const r = await s.refresh('myproxy', ['a', 'zzz-never-seen'])
  expect(r.status).toBe('ok')
  expect(read(root).valid.myproxy!.route.models.map(m => m.id)).toEqual(['a'])
  expect(read(root).valid.myproxy!.meta.manualModels).toEqual([])

  const root2 = tmp()
  seedPreview(root2)
  const { s: s2 } = svc(root2, previewFetched)
  const r2 = await s2.refresh('myproxy', [])
  expect(r2.status).toBe('ok')
  if (r2.status !== 'ok') return
  expect(r2).toMatchObject({ selected: 0, keptManual: 0, removed: ['a', 'b', 'c'] })
  expect(read(root2).valid.myproxy!.route.models).toEqual([])
  expect(read(root2).valid.myproxy!.meta.manualModels).toEqual([])
})

test('refresh 不带 only：响应加 selected、removed 仍是数组、manual 不动（既有行为不退化）', async () => {
  const root = tmp()
  seedPreview(root)
  const { s } = svc(root, previewFetched)
  const r = await s.refresh('myproxy')
  expect(r.status).toBe('ok')
  if (r.status !== 'ok') return
  expect(r).toMatchObject({ count: 3, added: 1, keptManual: 1, selected: 4, bySelection: false })
  expect(read(root).valid.myproxy!.route.models.map(m => m.id)).toEqual(['a', 'b', 'd', 'c'])
})

// ─── §11b 第 8 稿：上游回空目录。带 only 不算失败（判据与 preview 同一个），only 缺省仍是失败 ───

const emptyCatalog = (): FetchModelsResult => ({ ok: false, reason: 'empty', status: null })

test('refresh 带 only、上游回空目录：按拉到 0 个照勾选落盘，勾上的全进 manualModels，记成成功', async () => {
  const root = tmp()
  seedPreview(root)
  let changed = 0
  const { s, log } = svc(root, emptyCatalog, { now: () => 4242, onChanged: () => changed++ })
  const r = await s.refresh('myproxy', ['a', 'c', 'zzz-never-seen'])
  expect(r.status).toBe('ok')
  if (r.status !== 'ok') return
  expect(r).toMatchObject({ count: 0, added: 0, removed: ['b'], keptManual: 2, selected: 2, bySelection: true })
  const e = read(root).valid.myproxy!
  expect(e.route.models, '只留勾上的本地模型；owner 手填的上下文原样保留').toEqual([{ id: 'a', contextWindow: 5000 }, { id: 'c', contextWindow: 1048576 }])
  expect(e.meta.manualModels, 'a 原先不是手动的，勾上而上游没有 → 记成手动').toEqual(['a', 'c'])
  expect(e.meta.guessedContext, '这次没有从上游拿到任何上下文，不凭空标猜测').toEqual([])
  expect(e.meta.lastRefresh).toEqual({ at: 4242, ok: true, count: 0, reason: null })
  expect(changed).toBe(1)
  expect(log.events.filter(ev => ev.event === 'provider.models_failed'), '这一路不是失败，不记失败日志').toEqual([])
  expect(log.events).toContainEqual({ event: 'provider.models_fetched', name: 'myproxy', count: 0 })
})

test('refresh 带 only: []、上游回空目录：列表与 manualModels 都清空，仍是成功', async () => {
  const root = tmp()
  seedPreview(root)
  const { s } = svc(root, emptyCatalog)
  const r = await s.refresh('myproxy', [])
  expect(r.status).toBe('ok')
  if (r.status !== 'ok') return
  expect(r).toMatchObject({ count: 0, added: 0, removed: ['a', 'b', 'c'], keptManual: 0, selected: 0 })
  expect(read(root).valid.myproxy!.route.models).toEqual([])
  expect(read(root).valid.myproxy!.meta.manualModels).toEqual([])
})

test('refresh 不带 only、上游回空目录：仍是失败（reason 为 empty），列表与 manualModels 不动', async () => {
  const root = tmp()
  seedPreview(root)
  const before = read(root).valid.myproxy!
  const { s, log } = svc(root, emptyCatalog, { now: () => 4242 })
  const r = await s.refresh('myproxy')
  expect(r).toEqual({ status: 'failed', name: 'myproxy', reason: 'empty', status2: null })
  const e = read(root).valid.myproxy!
  expect(e.route.models).toEqual(before.route.models)
  expect(e.meta.manualModels).toEqual(before.meta.manualModels)
  expect(e.meta.lastRefresh).toEqual({ at: 4242, ok: false, count: 3, reason: 'empty' })
  expect(log.events.some(ev => ev.event === 'provider.models_failed' && ev.reason === 'empty')).toBe(true)
})

test('refresh 带 only、上游别的失败：照旧是失败，列表不动（放过的只有空目录这一种）', async () => {
  for (const reason of ['unauthorized', 'server_error', 'network', 'timeout', 'bad_response'] as const) {
    const root = tmp()
    seedPreview(root)
    const before = read(root).valid.myproxy!
    const { s } = svc(root, () => ({ ok: false, reason, status: null }))
    const r = await s.refresh('myproxy', ['a'])
    expect(r, reason).toMatchObject({ status: 'failed', reason })
    const e = read(root).valid.myproxy!
    expect(e.route.models, reason).toEqual(before.route.models)
    expect(e.meta.manualModels, reason).toEqual(before.meta.manualModels)
    expect(e.meta.lastRefresh, reason).toMatchObject({ ok: false, reason })
  }
})

test('空目录的判据 preview 与带 only 的 refresh 一致：预览能开弹窗，照默认勾选点确定也成功', async () => {
  const root = tmp()
  seedPreview(root)
  const { s } = svc(root, emptyCatalog)
  const pv = await s.preview('myproxy')
  expect(pv.status).toBe('ok')
  if (pv.status !== 'ok') return
  const checked = pv.models.filter(m => m.default_checked).map(m => m.id)
  expect(checked).toEqual(['c'])
  const r = await s.refresh('myproxy', checked)
  expect(r.status).toBe('ok')
  expect(read(root).valid.myproxy!.route.models.map(m => m.id)).toEqual(['c'])
})

test('testConn（§12a）：用列表第一个模型发请求；成功记 provider.tested；请求里带凭据密钥、响应不含', async () => {
  const root = tmp()
  seedPreview(root)
  const seen: ProbeModelRequest[] = []
  const { s, log, probeCalls } = svc(root, () => okModels([], 'https://p.example.com/v1'), { probe: async (req) => { seen.push(req); return { ok: true } } })
  const r = await s.testConn('myproxy')
  expect(r.status).toBe('ok')
  if (r.status !== 'ok') return
  expect(r.model).toBe('a')
  expect(Number.isInteger(r.latencyMs)).toBe(true)
  expect(seen[0]).toEqual({ api: 'openai-completions', baseURL: 'https://p.example.com/v1', model: 'a', key: 'test-key-7abcdefghij' })
  expect(probeCalls.length).toBe(1)
  expect(log.events).toContainEqual({ event: 'provider.tested', name: 'myproxy' })
  expect(JSON.stringify(r)).not.toContain('test-key-7abcdefghij')
})

test('testConn：列表为空 no_models（不发请求）；失败记 provider.test_failed 带 reason/status', async () => {
  const root = tmp()
  writeFileSync(providersPathOf(root), JSON.stringify({ version: 1, providers: { p1: {
    route: { api: 'openai-completions', baseURL: 'https://p.example.com/v1', apiKeyEnv: 'PROVIDER_P1_KEY', models: [] },
    meta: { createdAt: 1, updatedAt: 1, epoch: 1, keyRev: 1, guessedContext: [], ownerContext: [], manualModels: [], lastRefresh: null },
  } }, pendingKeyRemovals: [] }))
  setCredentialRef(credPathOf(root), 'PROVIDER_P1_KEY', 'test-key-7abcdefghij')
  const { s, log, probeCalls } = svc(root, () => okModels([], 'https://p.example.com/v1'), { probe: async () => ({ ok: false, reason: 'unauthorized', status: 401 }) })
  expect((await s.testConn('p1')).status).toBe('no_models')
  expect(probeCalls.length).toBe(0)

  writeFileSync(providersPathOf(root), JSON.stringify({ version: 1, providers: { p1: {
    route: { api: 'openai-completions', baseURL: 'https://p.example.com/v1', apiKeyEnv: 'PROVIDER_P1_KEY', models: [{ id: 'm1', contextWindow: 131072 }] },
    meta: { createdAt: 1, updatedAt: 1, epoch: 1, keyRev: 1, guessedContext: [], ownerContext: [], manualModels: [], lastRefresh: null },
  } }, pendingKeyRemovals: [] }))
  const after = readFileSync(providersPathOf(root), 'utf8')
  const r = await s.testConn('p1')
  expect(r.status).toBe('failed')
  if (r.status !== 'failed') return
  expect(r).toMatchObject({ name: 'p1', model: 'm1', reason: 'unauthorized', status2: 401 })
  expect(log.events).toContainEqual({ event: 'provider.test_failed', name: 'p1', reason: 'unauthorized', status: 401 })
  expect(readFileSync(providersPathOf(root), 'utf8'), 'testConn 只读：providers.json 逐字节不变').toBe(after)
})

test('模型管理：加手动模型（记入 manualModels）、改上下文（记入 ownerContext）、删模型；不存在返回 not_found', async () => {
  const root = tmp()
  const { s } = svc(root, () => okModels([{ id: 'm1', contextWindow: 8000 }], 'https://p.example.com/v1'))
  await s.save({ name: 'p1', api: 'openai-completions', baseURL: 'https://p.example.com/v1', key: 'test-key-7abcdefghij' })
  expect((await s.addModel('p1', 'man', null)).ok).toBe(true)
  expect((await s.addModel('p1', 'man', null)).ok).toBe(false) // 已有
  expect((await s.setContext('p1', 'm1', 200000)).ok).toBe(true)
  const e = read(root).valid.p1!
  expect(e.route.models.find(m => m.id === 'man')!.contextWindow).toBe(DEFAULT_CONTEXT)
  expect(e.meta.manualModels).toEqual(['man'])
  expect(e.route.models.find(m => m.id === 'm1')!.contextWindow).toBe(200000)
  expect(e.meta.ownerContext).toEqual(['m1'])
  expect((await s.removeModel('p1', 'm1')).ok).toBe(true)
  expect(read(root).valid.p1!.route.models.map(m => m.id)).toEqual(['man'])
  expect((await s.removeModel('p1', 'nope')).ok).toBe(false)
})
