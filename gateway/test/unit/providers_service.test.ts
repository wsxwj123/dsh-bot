// 自建供应商服务（方案 3.4/3.6、Q1）：新建/更新/删除/刷新/加删模型/改上下文/到点删键。
// 白盒：直接构造 ProviderService，注入假的 fetchModels 与断言用的 Logger；用临时目录当 <根>。
import { afterEach, expect, test } from 'bun:test'
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { Logger } from '../../src/log'
import { listRev, ProviderService } from '../../src/providers/service'
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

test('全量刷新（显式带 full）：新增/去掉/保留统计、<N> 是拉到的个数、主人手填的上下文保留、lastRefresh 写入', async () => {
  const root = tmp()
  // 预置：m1 主人手填 50000、m2、man1 手动加
  writeFileSync(providersPathOf(root), JSON.stringify({ version: 1, providers: { p1: { route: { api: 'openai-completions', baseURL: 'https://p.example.com/v1', apiKeyEnv: 'PROVIDER_P1_KEY', models: [{ id: 'm1', contextWindow: 50000 }, { id: 'm2', contextWindow: 131072 }, { id: 'man1', contextWindow: 131072 }] }, meta: { createdAt: 1, updatedAt: 1, epoch: 1, keyRev: 1, guessedContext: [], ownerContext: ['m1'], manualModels: ['man1'], lastRefresh: null } } }, pendingKeyRemovals: [] }))
  setCredentialRef(credPathOf(root), 'PROVIDER_P1_KEY', 'test-key-7abcdefghij')
  const { s: s2 } = svc(root, () => okModels([{ id: 'm1', contextWindow: 8000 }, { id: 'm3', contextWindow: null }, { id: 'man1', contextWindow: null }], 'https://p.example.com/v1'))
  const r = await s2.refresh('P1', { full: true })
  expect(r.status).toBe('ok')
  if (r.status !== 'ok') return
  expect(r.applied).toBe('full')
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

// ─── INTERFACE-新系统bot运维 §11a/§11b/§12a：preview、refresh 的 only、testConn；默认勾选与按勾选保存按 INTERFACE-dsh修补 第三节 ───

/** 预置一份 [a（owner 手填 5000）、b、manual c] 的自建与凭据 */
function seedPreview(root: string) {
  writeFileSync(providersPathOf(root), JSON.stringify({ version: 1, providers: { myproxy: {
    route: { api: 'openai-completions', baseURL: 'https://p.example.com/v1', apiKeyEnv: 'PROVIDER_MYPROXY_KEY', models: [{ id: 'a', contextWindow: 5000 }, { id: 'b', contextWindow: 131072 }, { id: 'c', contextWindow: 1048576 }] },
    meta: { createdAt: 1, updatedAt: 1, epoch: 1, keyRev: 1, guessedContext: ['b', 'c'], ownerContext: ['a'], manualModels: ['c'], lastRefresh: null },
  } }, pendingKeyRemovals: [] }))
  setCredentialRef(credPathOf(root), 'PROVIDER_MYPROXY_KEY', 'test-key-7abcdefghij')
}
const previewFetched = () => okModels([{ id: 'a', contextWindow: 8000 }, { id: 'b', contextWindow: null }, { id: 'd', contextWindow: null }], 'https://p.example.com/v1')

test('preview（§11a）：先拉到的（上游顺序）后仅本地的（列表顺序）；行恒 6 键；count 只数拉到的；在清单里的默认勾、新出现的不勾；只读', async () => {
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
    { id: 'd', contextWindow: null, in_list: false, in_fetched: true, manual: false, default_checked: false },
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
  expect(r.models.map(m => m.default_checked), '在清单里的都默认勾，不分是不是手填').toEqual([true, true, true])

  const { s: s1, calls } = svc(root, previewFetched)
  setCredentialRef(credPathOf(root), 'PROVIDER_MYPROXY_KEY', null)
  expect((await s1.preview('myproxy')).status).toBe('key_missing')
  expect(calls.length).toBe(0)
  expect((await s1.preview('nosuch')).status).toBe('not_found')
})

test('refresh 带 only：原来在清单的在前（原先后、原上下文），新勾上的在后（对方返回的先后）；重复的名字当成一个', async () => {
  const root = tmp()
  seedPreview(root)
  const { s } = svc(root, previewFetched)
  const r = await s.refresh('myproxy', { only: ['d', 'a', 'd', 'c', 'a'] })
  expect(r.status).toBe('ok')
  if (r.status !== 'ok') return
  expect(r).toMatchObject({ count: 3, added: 1, removed: ['b'], keptManual: 1, selected: 3, applied: 'selection' })
  const e = read(root).valid.myproxy!
  expect(e.route.models, 'a 对方这次给了 8000，仍保持原来的 5000').toEqual([{ id: 'a', contextWindow: 5000 }, { id: 'c', contextWindow: 1048576 }, { id: 'd', contextWindow: DEFAULT_CONTEXT }])
  expect(e.meta.manualModels).toEqual(['c'])
  expect(e.meta.guessedContext, 'b 被取消勾选移出，c 不变，d 对方没给上下文加进去').toEqual(['c', 'd'])
})

test('refresh 带 only：未知 id 静默忽略、旧 manual 被弃选即移除；only: [] 清空列表与 manual', async () => {
  const root = tmp()
  seedPreview(root)
  const { s } = svc(root, previewFetched)
  const r = await s.refresh('myproxy', { only: ['a', 'zzz-never-seen'] })
  expect(r.status).toBe('ok')
  expect(read(root).valid.myproxy!.route.models.map(m => m.id)).toEqual(['a'])
  expect(read(root).valid.myproxy!.meta.manualModels).toEqual([])

  const root2 = tmp()
  seedPreview(root2)
  const { s: s2 } = svc(root2, previewFetched)
  const r2 = await s2.refresh('myproxy', { only: [] })
  expect(r2.status).toBe('ok')
  if (r2.status !== 'ok') return
  expect(r2).toMatchObject({ selected: 0, keptManual: 0, removed: ['a', 'b', 'c'] })
  expect(read(root2).valid.myproxy!.route.models).toEqual([])
  expect(read(root2).valid.myproxy!.meta.manualModels).toEqual([])
})

test('refresh 带 full：清单重铺成对方返回的全部加手填的，manual 不动', async () => {
  const root = tmp()
  seedPreview(root)
  const { s } = svc(root, previewFetched)
  const r = await s.refresh('myproxy', { full: true })
  expect(r.status).toBe('ok')
  if (r.status !== 'ok') return
  expect(r).toMatchObject({ count: 3, added: 1, removed: [], keptManual: 1, selected: 4, applied: 'full' })
  expect(read(root).valid.myproxy!.route.models.map(m => m.id)).toEqual(['a', 'b', 'd', 'c'])
})

// ─── INTERFACE-dsh修补 第五节：刷新和保存默认不改动挑好的清单 ───

test('refresh 不带 only 也不带 full、清单不为空：清单、手填名单、猜的上下文名单都不动，只写刷新记录与更新时间；结果是对账数字', async () => {
  const root = tmp()
  seedPreview(root)
  const strip = () => { const j = JSON.parse(readFileSync(providersPathOf(root), 'utf8')); delete j.providers.myproxy.meta.lastRefresh; delete j.providers.myproxy.meta.updatedAt; return JSON.stringify(j) }
  const before = strip()
  let changed = 0
  // 对方返回 [a(8000), b, d]；清单 [a, b, 手填 c]：N=3，不在清单 1 个（d），没返回 1 个（c）
  const { s } = svc(root, previewFetched, { now: () => 4242, onChanged: () => changed++ })
  const r = await s.refresh('myproxy')
  expect(r).toMatchObject({ status: 'ok', applied: 'kept', count: 3, added: 0, removed: [], keptManual: 1, selected: 3, notInList: 1, notReturned: 1, disjoint: false })
  expect(strip(), '除了刷新记录和更新时间，文件一个字都不变').toBe(before)
  const e = read(root).valid.myproxy!
  expect(e.meta.lastRefresh).toEqual({ at: 4242, ok: true, count: 3, reason: null })
  expect(e.meta.updatedAt).toBe(4242)
  expect(changed, '文件写过了，照常通知重算（清单没变，路由指纹不变，不会因此重启）').toBe(1)
  // full 为 false 等同没给
  expect(await s.refresh('myproxy', { full: false })).toMatchObject({ status: 'ok', applied: 'kept' })
  expect(strip()).toBe(before)
})

test('只对账时的数字：对方返回的去重后计数；清单与对方完全一样时两个数是 0；清单里的一个都没返回时 disjoint 为真', async () => {
  const root = tmp()
  seedList(root, [{ id: 'a', contextWindow: 8000 }, { id: 'b', contextWindow: 8000 }])
  const same = await svc(root, fetchedIds('b', 'a')).s.refresh('myproxy')
  expect(same).toMatchObject({ applied: 'kept', count: 2, notInList: 0, notReturned: 0, disjoint: false })
  const other = await svc(root, fetchedIds('n1', 'n2', 'n3')).s.refresh('myproxy')
  expect(other).toMatchObject({ applied: 'kept', count: 3, notInList: 3, notReturned: 2, disjoint: true })
  const partly = await svc(root, fetchedIds('a', 'n1')).s.refresh('myproxy')
  expect(partly).toMatchObject({ applied: 'kept', notInList: 1, notReturned: 1, disjoint: false })
  expect(read(root).valid.myproxy!.route.models.map(m => m.id)).toEqual(['a', 'b'])
})

test('只对账不重写条目：手写的字段、键的先后、读取时会被丢掉的重复或不合格模型条目都原样留在文件里', async () => {
  const root = tmp()
  // route 里键的先后与众不同、带手写字段；模型里有一条重复的 a、一条名字不合格的、一条上下文不合格的
  const rawModels = [{ contextWindow: 8000, id: 'a', note: '手写' }, { id: 'a', contextWindow: 9000 }, { id: 'bad\tid', contextWindow: 8000 }, { id: 'c', contextWindow: 5 }, { id: 'b', contextWindow: 8000 }]
  writeFileSync(providersPathOf(root), JSON.stringify({ version: 1, providers: { myproxy: {
    note: '条目上的手写字段',
    route: { models: rawModels, timeoutMs: 9000, baseURL: 'https://p.example.com/v1', api: 'openai-completions', apiKeyEnv: 'PROVIDER_MYPROXY_KEY' },
    meta: { manualModels: [], keyRev: 1, epoch: 1, extra: 'x', createdAt: 1, updatedAt: 1, guessedContext: [], ownerContext: [], lastRefresh: null },
  } }, pendingKeyRemovals: [] }, null, 1))
  setCredentialRef(credPathOf(root), 'PROVIDER_MYPROXY_KEY', 'test-key-7abcdefghij')
  expect(read(root).valid.myproxy!.route.models.map(m => m.id), '前提：读取时看到的清单是丢掉重复和不合格之后的').toEqual(['a', 'b'])
  const text = () => readFileSync(providersPathOf(root), 'utf8')
  const before = text()
  const r = await svc(root, fetchedIds('a', 'b', 'z'), { now: () => 1 }).s.refresh('myproxy')
  expect(r).toMatchObject({ applied: 'kept', selected: 2, notInList: 1, notReturned: 0 })
  // now 取 1，与文件里的 updatedAt 相同；整份文件只多出一条刷新记录
  expect(JSON.parse(text())).toEqual((() => { const j = JSON.parse(before); j.providers.myproxy.meta.lastRefresh = { at: 1, ok: true, count: 3, reason: null }; return j })())
  expect(Object.keys(JSON.parse(text()).providers.myproxy.route), '键的先后没被重排').toEqual(['models', 'timeoutMs', 'baseURL', 'api', 'apiKeyEnv'])
  // 对照：按勾选保存是重写清单，写回去的是整理后的条目
  await svc(root, fetchedIds('a', 'b', 'z')).s.refresh('myproxy', { only: ['a', 'b'] })
  expect(JSON.parse(text()).providers.myproxy.route.models).toEqual([{ id: 'a', contextWindow: 8000 }, { id: 'b', contextWindow: 8000 }])
})

test('只对账而这次是在 /v1 下才拉到的：地址补上，清单仍不动', async () => {
  const root = tmp()
  seedBareHost(root)
  const r = await svc(root, () => viaV1(['a', 'b'])).s.refresh('myproxy')
  expect(r).toMatchObject({ status: 'ok', applied: 'kept', v1Added: true, baseURL: 'https://p.example.com/v1', notInList: 1 })
  const j = JSON.parse(readFileSync(providersPathOf(root), 'utf8')).providers.myproxy.route
  expect(j).toEqual({ api: 'openai-completions', baseURL: 'https://p.example.com/v1', apiKeyEnv: 'PROVIDER_MYPROXY_KEY', models: [{ id: 'a', contextWindow: 5000 }], timeoutMs: 9000 })
})

test('清单是空的、不带任何选项：拉到的全部收下（applied 为 full）', async () => {
  const root = tmp()
  seedList(root, [])
  const r = await svc(root, fetchedIds('x', 'y')).s.refresh('myproxy')
  expect(r).toMatchObject({ status: 'ok', applied: 'full', count: 2, added: 2, selected: 2 })
  expect(read(root).valid.myproxy!.route.models.map(m => m.id)).toEqual(['x', 'y'])
})

test('清单空不空按保存那一刻的文件算：拉取期间被别处填上就不动，被别处清空就全部收下', async () => {
  /** 拉取进行到一半时，像别处那样改写文件里这家的清单 */
  const during = (root: string, models: unknown[]) => () => {
    const j = JSON.parse(readFileSync(providersPathOf(root), 'utf8'))
    j.providers.myproxy.route.models = models
    writeFileSync(providersPathOf(root), JSON.stringify(j))
    return fetchedIds('x', 'y')()
  }
  const filled = tmp()
  seedList(filled, [])
  expect(await svc(filled, during(filled, [{ id: 'picked', contextWindow: 8000 }])).s.refresh('myproxy')).toMatchObject({ applied: 'kept', count: 2, notInList: 2, notReturned: 1 })
  expect(read(filled).valid.myproxy!.route.models.map(m => m.id)).toEqual(['picked'])

  const emptied = tmp()
  seedList(emptied, [{ id: 'old', contextWindow: 8000 }])
  expect(await svc(emptied, during(emptied, [])).s.refresh('myproxy')).toMatchObject({ applied: 'full', added: 2 })
  expect(read(emptied).valid.myproxy!.route.models.map(m => m.id)).toEqual(['x', 'y'])
})

test('保存到已有的同一家、清单不为空、拉取成功：清单与两份名单不动，地址和密钥照常更新，刷新记录保持原值，count 是对方返回的个数', async () => {
  const root = tmp()
  const mark = { at: 99, ok: true, count: 7, reason: null }
  // 模型条目里键的先后与整理后的不同，用来确认写回去的是文件里的原样
  writeFileSync(providersPathOf(root), JSON.stringify({ version: 1, providers: { p1: {
    route: { api: 'openai-completions', baseURL: 'https://p.example.com/v1', apiKeyEnv: 'PROVIDER_P1_KEY', models: [{ contextWindow: 50000, id: 'm1' }, { contextWindow: 131072, id: 'm2' }, { contextWindow: 131072, id: 'man1' }] },
    meta: { createdAt: 1, updatedAt: 1, epoch: 1, keyRev: 1, guessedContext: ['m2'], ownerContext: ['m1'], manualModels: ['man1'], lastRefresh: mark },
  } }, pendingKeyRemovals: [] }))
  setCredentialRef(credPathOf(root), 'PROVIDER_P1_KEY', 'test-key-7oldoldold00')
  const listing = () => { const e = JSON.parse(readFileSync(providersPathOf(root), 'utf8')).providers.p1; return JSON.stringify([e.route.models, e.meta.manualModels, e.meta.guessedContext]) }
  const before = listing()
  const { s } = svc(root, () => okModels([{ id: 'm1', contextWindow: 8000 }, { id: 'm3', contextWindow: null }, { id: 'm4', contextWindow: null }], 'https://p.example.com/api/v1'), { now: () => 500 })
  const r = await s.save({ name: 'P1', api: 'openai-completions', baseURL: 'https://p.example.com/api/v1', key: 'test-key-7newnewnew00', mode: 'modify' })
  expect(r).toMatchObject({ status: 'saved', kind: 'updated', name: 'p1', applied: 'kept', count: 3, notInList: 2, notReturned: 2, disjoint: false, epochChanged: false, reason: null, baseURL: 'https://p.example.com/api/v1' })
  expect(listing(), '模型条目连键的先后都没变').toBe(before)
  const e = read(root).valid.p1!
  expect(e.route.baseURL).toBe('https://p.example.com/api/v1')
  expect(e.meta).toMatchObject({ keyRev: 2, epoch: 1, updatedAt: 500, lastRefresh: mark })
  expect(creds(root)).toContain('PROVIDER_P1_KEY: "test-key-7newnewnew00"')
})

test('保存时四条有一条不成立就照旧重铺（applied 为 full）：换了主机、换了接口格式、清单是空的、新建；拉取失败则保持原清单', async () => {
  const seedOne = (root: string, models: unknown[]) => {
    writeFileSync(providersPathOf(root), JSON.stringify({ version: 1, providers: { p1: {
      route: { api: 'openai-completions', baseURL: 'https://p.example.com/v1', apiKeyEnv: 'PROVIDER_P1_KEY', models },
      meta: { createdAt: 1, updatedAt: 1, epoch: 1, keyRev: 1, guessedContext: [], ownerContext: [], manualModels: [], lastRefresh: null },
    } }, pendingKeyRemovals: [] }))
    setCredentialRef(credPathOf(root), 'PROVIDER_P1_KEY', 'test-key-7abcdefghij')
  }
  const OLD = [{ id: 'old1', contextWindow: 8000 }, { id: 'old2', contextWindow: 8000 }]
  const CASES: [string, unknown[], { api: 'openai-completions' | 'anthropic-messages'; baseURL: string }][] = [
    ['换了主机', OLD, { api: 'openai-completions', baseURL: 'https://other.example.com/v1' }],
    ['换了端口', OLD, { api: 'openai-completions', baseURL: 'https://p.example.com:8443/v1' }],
    ['换了接口格式', OLD, { api: 'anthropic-messages', baseURL: 'https://p.example.com' }],
    ['清单是空的', [], { api: 'openai-completions', baseURL: 'https://p.example.com/v1' }],
  ]
  for (const [what, models, to] of CASES) {
    const root = tmp()
    seedOne(root, models)
    const r = await svc(root, req => okModels([{ id: 'n1', contextWindow: null }, { id: 'n2', contextWindow: null }], req.baseURL)).s.save({ name: 'p1', key: 'test-key-7abcdefghij', ...to })
    expect(r, what).toMatchObject({ status: 'saved', applied: 'full', count: 2 })
    expect(read(root).valid.p1!.route.models.map(m => m.id), what).toEqual(['n1', 'n2'])
  }
  const fresh = tmp()
  expect(await svc(fresh, req => okModels([{ id: 'n1', contextWindow: null }], req.baseURL)).s.save({ name: 'p1', api: 'openai-completions', baseURL: 'https://p.example.com/v1', key: 'test-key-7abcdefghij' }))
    .toMatchObject({ status: 'saved', kind: 'created', applied: 'full', count: 1 })
  const failed = tmp()
  seedOne(failed, OLD)
  expect(await svc(failed, () => ({ ok: false, reason: 'server_error', status: 500 })).s.save({ name: 'p1', api: 'openai-completions', baseURL: 'https://p.example.com/v1', key: 'test-key-7abcdefghij' }))
    .toMatchObject({ status: 'saved', applied: 'full', reason: 'server_error', count: 2 })
  expect(read(failed).valid.p1!.route.models.map(m => m.id)).toEqual(['old1', 'old2'])
})

// ─── §11b 第 8 稿：上游回空目录。带 only 不算失败（判据与 preview 同一个），only 缺省仍是失败 ───

const emptyCatalog = (): FetchModelsResult => ({ ok: false, reason: 'empty', status: null })

test('refresh 带 only、上游回空目录：按拉到 0 个照勾选落盘，勾上的不会因此变成手填，记成成功', async () => {
  const root = tmp()
  seedPreview(root)
  let changed = 0
  const { s, log } = svc(root, emptyCatalog, { now: () => 4242, onChanged: () => changed++ })
  const r = await s.refresh('myproxy', { only: ['a', 'c', 'zzz-never-seen'] })
  expect(r.status).toBe('ok')
  if (r.status !== 'ok') return
  expect(r).toMatchObject({ count: 0, added: 0, removed: ['b'], keptManual: 1, selected: 2, applied: 'selection' })
  const e = read(root).valid.myproxy!
  expect(e.route.models, '只留勾上的本地模型；owner 手填的上下文原样保留').toEqual([{ id: 'a', contextWindow: 5000 }, { id: 'c', contextWindow: 1048576 }])
  expect(e.meta.manualModels, 'a 原先不是手动的，勾上而对方这次没返回：仍然不是').toEqual(['c'])
  expect(e.meta.guessedContext, 'b 被取消勾选移出，c 原来在名单里、仍然在').toEqual(['c'])
  expect(e.meta.lastRefresh).toEqual({ at: 4242, ok: true, count: 0, reason: null })
  expect(changed).toBe(1)
  expect(log.events.filter(ev => ev.event === 'provider.models_failed'), '这一路不是失败，不记失败日志').toEqual([])
  expect(log.events).toContainEqual({ event: 'provider.models_fetched', name: 'myproxy', count: 0 })
})

test('refresh 带 only: []、上游回空目录：列表与 manualModels 都清空，仍是成功', async () => {
  const root = tmp()
  seedPreview(root)
  const { s } = svc(root, emptyCatalog)
  const r = await s.refresh('myproxy', { only: [] })
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
    const r = await s.refresh('myproxy', { only: ['a'] })
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
  expect(checked).toEqual(['a', 'b', 'c'])
  const r = await s.refresh('myproxy', { only: checked, rev: pv.listRev })
  expect(r.status).toBe('ok')
  expect(read(root).valid.myproxy!.route.models.map(m => m.id)).toEqual(['a', 'b', 'c'])
})

// ─── INTERFACE-dsh修补 第三节：默认勾选的取值表、清单指纹、直接确定清单不变 ───

/** 写一家自建供应商（模型可带 reasoningEfforts），凭据齐 */
function seedList(root: string, models: { id: string; contextWindow: number; reasoningEfforts?: unknown }[], meta: { manual?: string[]; guessed?: string[]; owner?: string[] } = {}) {
  writeFileSync(providersPathOf(root), JSON.stringify({ version: 1, providers: { myproxy: {
    route: { api: 'openai-completions', baseURL: 'https://p.example.com/v1', apiKeyEnv: 'PROVIDER_MYPROXY_KEY', models },
    meta: { createdAt: 1, updatedAt: 1, epoch: 1, keyRev: 1, guessedContext: meta.guessed ?? [], ownerContext: meta.owner ?? [], manualModels: meta.manual ?? [], lastRefresh: null },
  } }, pendingKeyRemovals: [] }))
  setCredentialRef(credPathOf(root), 'PROVIDER_MYPROXY_KEY', 'test-key-7abcdefghij')
}
const fetchedIds = (...ids: string[]) => () => okModels(ids.map(id => ({ id, contextWindow: null })), 'https://p.example.com/v1')

test('默认勾选的取值表（3.1 的八行）：default_checked = in_list or（清单为空 and in_fetched）', async () => {
  // 清单不为空：[inboth, inboth-m（手填）, gone-m（手填）, gone]，手填名单里另有一个不在清单的 ghost
  const root = tmp()
  seedList(root, ['inboth', 'inboth-m', 'gone-m', 'gone'].map(id => ({ id, contextWindow: 8000 })), { manual: ['inboth-m', 'gone-m', 'ghost'] })
  const pv = await svc(root, fetchedIds('inboth', 'inboth-m', 'new', 'ghost')).s.preview('myproxy')
  if (pv.status !== 'ok') throw new Error(pv.status)
  expect(pv.models.map(m => [m.id, m.in_fetched, m.in_list, m.manual, m.default_checked])).toEqual([
    ['inboth', true, true, false, true],
    ['inboth-m', true, true, true, true],
    ['new', true, false, false, false],
    ['ghost', true, false, true, false],
    ['gone-m', false, true, true, true],
    ['gone', false, true, false, true],
  ])
  // 清单为空：拉到的全勾，哪怕手填名单里有它；对方也回空目录时一行都没有
  const empty = tmp()
  seedList(empty, [], { manual: ['ghost'] })
  const pv2 = await svc(empty, fetchedIds('x', 'ghost')).s.preview('myproxy')
  if (pv2.status !== 'ok') throw new Error(pv2.status)
  expect(pv2.models.map(m => [m.id, m.in_fetched, m.in_list, m.manual, m.default_checked])).toEqual([['x', true, false, false, true], ['ghost', true, false, true, true]])
  expect(await svc(empty, () => ({ ok: false, reason: 'empty', status: null })).s.preview('myproxy')).toMatchObject({ status: 'ok', count: 0, models: [] })
})

test('清单指纹：只看模型名序列和手填名单；上下文长度、时间戳、刷新记录、别的名单不影响；先后或手填名单一变就变', () => {
  const entry = (ids: string[], manual: string[], extra: Record<string, unknown> = {}) => parseProviders(JSON.stringify({ version: 1, providers: { p: {
    route: { api: 'openai-completions', baseURL: 'https://p.example.com/v1', apiKeyEnv: 'PROVIDER_P_KEY', models: ids.map(id => ({ id, contextWindow: (extra.ctx as number) ?? 8000 })) },
    meta: { createdAt: 1, updatedAt: extra.updatedAt ?? 1, epoch: 1, keyRev: 1, guessedContext: extra.guessed ?? [], ownerContext: [], manualModels: manual, lastRefresh: extra.lastRefresh ?? null },
  } }, pendingKeyRemovals: [] })).valid.p!
  const rev = listRev(entry(['a', 'b'], ['b']))
  expect(rev).toMatch(/^[0-9a-f]{32}$/)
  expect(listRev(entry(['a', 'b'], ['b'], { ctx: 4096, updatedAt: 99, guessed: ['a'], lastRefresh: { at: 9, ok: true, count: 9, reason: null } }))).toBe(rev)
  for (const other of [entry(['b', 'a'], ['b']), entry(['a', 'b'], []), entry(['a', 'b'], ['b', 'ghost']), entry(['a'], ['b']), entry(['a', 'b', 'c'], ['b'])]) expect(listRev(other)).not.toBe(rev)
  // 模型名和手填名单分开算：不会因为拼在一起恰好相同而撞上
  expect(listRev(entry(['a', 'b'], []))).not.toBe(listRev(entry(['a'], ['b'])))
})

test('什么都不动直接确定：清单、先后、上下文长度、思考档位声明、手填名单、猜的上下文名单全都不变（对方返回的先后、内容、上下文都不同）', async () => {
  const root = tmp()
  const models = [{ id: 'a', contextWindow: 8000 }, { id: 'b', contextWindow: 1048576, reasoningEfforts: { high: 'high' } }, { id: 'c', contextWindow: 5000 }, { id: 'gone', contextWindow: 1048576 }]
  seedList(root, models, { manual: ['c', 'ghost'], guessed: ['b', 'gone'], owner: ['a'] })
  const before = read(root).valid.myproxy!
  const pv = await svc(root, () => okModels([{ id: 'd', contextWindow: null }, { id: 'b', contextWindow: 64000 }, { id: 'a', contextWindow: 32000 }], 'https://p.example.com/v1')).s.preview('myproxy')
  if (pv.status !== 'ok') throw new Error(pv.status)
  const only = pv.models.filter(m => m.default_checked).map(m => m.id)
  // 确定时对方返回的又变了
  const r = await svc(root, () => okModels([{ id: 'e', contextWindow: null }, { id: 'a', contextWindow: 16000 }, { id: 'c', contextWindow: null }], 'https://p.example.com/v1'), { now: () => 777 }).s.refresh('myproxy', { only, rev: pv.listRev })
  expect(r).toMatchObject({ status: 'ok', added: 0, removed: [], selected: 4, keptManual: 2, applied: 'selection' })
  const after = read(root).valid.myproxy!
  expect(after.route.models).toEqual(before.route.models)
  expect(after.meta.manualModels).toEqual(['c', 'ghost'])
  expect(after.meta.guessedContext).toEqual(['b', 'gone'])
  expect(after.meta.ownerContext).toEqual(['a'])
  expect(after.meta, '允许变的只有刷新记录和更新时间').toEqual({ ...before.meta, updatedAt: 777, lastRefresh: { at: 777, ok: true, count: 3, reason: null } })
})

test('手填名单里本来就不在清单的名字：按勾选保存时原样留着；它这次被新勾上就成了清单里的手填模型', async () => {
  const root = tmp()
  seedList(root, [{ id: 'a', contextWindow: 8000 }], { manual: ['c'] })
  expect(await svc(root, fetchedIds('a', 'c')).s.refresh('myproxy', { only: ['a'] })).toMatchObject({ status: 'ok', keptManual: 1 })
  expect(read(root).valid.myproxy!.meta.manualModels).toEqual(['c'])
  expect(await svc(root, fetchedIds('a', 'c')).s.refresh('myproxy', { only: ['a', 'c'] })).toMatchObject({ status: 'ok', keptManual: 1, added: 1 })
  expect(read(root).valid.myproxy!.route.models.map(m => m.id)).toEqual(['a', 'c'])
  expect(read(root).valid.myproxy!.meta.manualModels).toEqual(['c'])
})

test('带 rev 确定：与保存那一刻的清单一致才保存；预览之后清单被别处改过回 list_changed，文件不变；不带 rev 不检查', async () => {
  const root = tmp()
  seedPreview(root)
  const pv = await svc(root, previewFetched).s.preview('myproxy')
  if (pv.status !== 'ok') throw new Error(pv.status)
  // 别处删掉了 b
  const raw = JSON.parse(readFileSync(providersPathOf(root), 'utf8'))
  raw.providers.myproxy.route.models = raw.providers.myproxy.route.models.filter((m: { id: string }) => m.id !== 'b')
  writeFileSync(providersPathOf(root), JSON.stringify(raw))
  const before = readFileSync(providersPathOf(root), 'utf8')
  let changed = 0
  const { s } = svc(root, previewFetched, { onChanged: () => changed++ })
  expect(await s.refresh('myproxy', { only: ['a', 'b', 'c'], rev: pv.listRev })).toEqual({ status: 'list_changed', name: 'myproxy' })
  expect(readFileSync(providersPathOf(root), 'utf8')).toBe(before)
  expect(changed).toBe(0)
  // 重新预览拿新指纹就能保存；同一个指纹第二次确定（清单已被第一次改掉）被挡住
  const pv2 = await s.preview('myproxy')
  if (pv2.status !== 'ok') throw new Error(pv2.status)
  expect((await s.refresh('myproxy', { only: ['a'], rev: pv2.listRev })).status).toBe('ok')
  expect((await s.refresh('myproxy', { only: ['a'], rev: pv2.listRev })).status).toBe('list_changed')
  expect((await s.refresh('myproxy', { only: ['a'] })).status).toBe('ok')
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

test('testConn 失败：OpenAI 格式且保存的地址只有域名时 bareHost 为真；地址带路径或 Anthropic 格式为假', async () => {
  const CASES: [string, string, boolean][] = [
    ['openai-completions', 'https://p.example.com', true], ['openai-responses', 'https://p.example.com', true],
    ['openai-completions', 'https://p.example.com/v1', false], ['anthropic-messages', 'https://p.example.com', false],
  ]
  for (const [api, baseURL, bareHost] of CASES) {
    const root = tmp()
    writeFileSync(providersPathOf(root), JSON.stringify({ version: 1, providers: { p1: {
      route: { api, baseURL, apiKeyEnv: 'PROVIDER_P1_KEY', models: [{ id: 'm1', contextWindow: 131072 }] },
      meta: { createdAt: 1, updatedAt: 1, epoch: 1, keyRev: 1, guessedContext: [], ownerContext: [], manualModels: [], lastRefresh: null },
    } }, pendingKeyRemovals: [] }))
    setCredentialRef(credPathOf(root), 'PROVIDER_P1_KEY', 'test-key-7abcdefghij')
    const { s } = svc(root, () => okModels([], baseURL), { probe: async () => ({ ok: false, reason: 'bad_response', status: 200 }) })
    expect(await s.testConn('p1'), `${api} ${baseURL}`).toMatchObject({ status: 'failed', reason: 'bad_response', bareHost })
  }
})

// ─── INTERFACE-dsh修补 第二节：刷新时这次是在 /v1 下才拉到的，把保存的地址一并补上 ───

/** 预置一家地址只有域名的自建 [a]，凭据齐 */
function seedBareHost(root: string) {
  writeFileSync(providersPathOf(root), JSON.stringify({ version: 1, providers: { myproxy: {
    route: { api: 'openai-completions', baseURL: 'https://p.example.com', apiKeyEnv: 'PROVIDER_MYPROXY_KEY', models: [{ id: 'a', contextWindow: 5000 }], timeoutMs: 9000 },
    meta: { createdAt: 1, updatedAt: 1, epoch: 1, keyRev: 1, guessedContext: [], ownerContext: [], manualModels: [], lastRefresh: null },
  } }, pendingKeyRemovals: [] }))
  setCredentialRef(credPathOf(root), 'PROVIDER_MYPROXY_KEY', 'test-key-7abcdefghij')
}
const viaV1 = (ids: string[]): FetchModelsResult => ({ ok: true, models: ids.map(id => ({ id, contextWindow: null })), truncated: false, baseURL: 'https://p.example.com/v1', v1Added: true })

test('刷新时在 /v1 下才拉到：保存的地址补成 <地址>/v1，结果里 v1Added 为真并带补后的地址；文件里不认识的字段不丢', async () => {
  const root = tmp()
  seedBareHost(root)
  const { s, calls } = svc(root, () => viaV1(['a', 'b']))
  const r = await s.refresh('myproxy', { only: ['a', 'b'] })
  expect(r).toMatchObject({ status: 'ok', v1Added: true, baseURL: 'https://p.example.com/v1' })
  expect(calls[0]!.baseURL, '拉取用的是保存的原地址').toBe('https://p.example.com')
  expect(read(root).valid.myproxy!.route.baseURL).toBe('https://p.example.com/v1')
  expect(JSON.parse(readFileSync(providersPathOf(root), 'utf8')).providers.myproxy.route.timeoutMs).toBe(9000)
  // 地址已经带 /v1：下一次不再补，也不再说
  const again = svc(root, req => okModels([{ id: 'a', contextWindow: null }], req.baseURL))
  expect(await again.s.refresh('myproxy', { only: ['a'] })).toMatchObject({ status: 'ok', v1Added: false, baseURL: 'https://p.example.com/v1' })
  expect(again.calls[0]!.baseURL).toBe('https://p.example.com/v1')
})

test('不补地址的三种：预览只读；带勾选时对方在 /v1 下回的是空目录；刷新失败', async () => {
  const root = tmp()
  seedBareHost(root)
  const before = readFileSync(providersPathOf(root), 'utf8')
  expect((await svc(root, () => viaV1(['a', 'b'])).s.preview('myproxy')).status).toBe('ok')
  expect(readFileSync(providersPathOf(root), 'utf8'), '预览不写文件').toBe(before)
  expect(await svc(root, () => ({ ok: false, reason: 'empty', status: null })).s.refresh('myproxy', { only: ['a'] })).toMatchObject({ status: 'ok', v1Added: false, baseURL: 'https://p.example.com' })
  expect(read(root).valid.myproxy!.route.baseURL).toBe('https://p.example.com')
  expect((await svc(root, () => ({ ok: false, reason: 'bad_response', status: null })).s.refresh('myproxy')).status).toBe('failed')
  expect(read(root).valid.myproxy!.route.baseURL).toBe('https://p.example.com')
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
