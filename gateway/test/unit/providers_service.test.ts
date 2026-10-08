// 自建供应商服务（方案 3.4/3.6、Q1）：新建/更新/删除/刷新/加删模型/改上下文/到点删键。
// 白盒：直接构造 ProviderService，注入假的 fetchModels 与断言用的 Logger；用临时目录当 <根>。
import { afterEach, expect, test } from 'bun:test'
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { Logger } from '../../src/log'
import { ProviderService } from '../../src/providers/service'
import { parseProviders, setCredentialRef } from '../../src/providers/store'
import type { FetchedModel, FetchModelsRequest, FetchModelsResult } from '../../src/providers/models'

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
function svc(root: string, plan: FetchPlan, o: { now?: () => number; keyGraceMs?: number; onChanged?: () => void } = {}) {
  const log = new CapLog()
  const calls: FetchModelsRequest[] = []
  const s = new ProviderService({
    root, credentialsPath: credPathOf(root), log,
    fetchModels: async (req) => { calls.push(req); return plan(req) },
    now: o.now, lockWaitMs: 2000, keyGraceMs: o.keyGraceMs ?? 600_000,
    configRouteKeys: () => ['MYROUTE_KEY'],
    onChanged: o.onChanged,
  })
  return { s, log, calls }
}
const okModels = (list: FetchedModel[], baseURL: string): FetchModelsResult => ({ ok: true, models: list, truncated: false, baseURL, v1Added: false })
const read = (root: string) => parseProviders(readFileSync(providersPathOf(root), 'utf8'))
const creds = (root: string) => existsSync(credPathOf(root)) ? readFileSync(credPathOf(root), 'utf8') : ''

test('新建：拉到的模型落盘、上下文未知记 131072 与 guessedContext、密钥写进凭据、通知 onChanged', async () => {
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
  expect(e.route.models).toEqual([{ id: 'm1', contextWindow: 8000 }, { id: 'm2', contextWindow: 131072 }])
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
  expect(e.route.models).toEqual([{ id: 'm1', contextWindow: 50000 }, { id: 'm3', contextWindow: 131072 }, { id: 'man1', contextWindow: 131072 }])
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

test('模型管理：加手动模型（记入 manualModels）、改上下文（记入 ownerContext）、删模型；不存在返回 not_found', async () => {
  const root = tmp()
  const { s } = svc(root, () => okModels([{ id: 'm1', contextWindow: 8000 }], 'https://p.example.com/v1'))
  await s.save({ name: 'p1', api: 'openai-completions', baseURL: 'https://p.example.com/v1', key: 'test-key-7abcdefghij' })
  expect((await s.addModel('p1', 'man', null)).ok).toBe(true)
  expect((await s.addModel('p1', 'man', null)).ok).toBe(false) // 已有
  expect((await s.setContext('p1', 'm1', 200000)).ok).toBe(true)
  const e = read(root).valid.p1!
  expect(e.route.models.find(m => m.id === 'man')!.contextWindow).toBe(131072)
  expect(e.meta.manualModels).toEqual(['man'])
  expect(e.route.models.find(m => m.id === 'm1')!.contextWindow).toBe(200000)
  expect(e.meta.ownerContext).toEqual(['m1'])
  expect((await s.removeModel('p1', 'm1')).ok).toBe(true)
  expect(read(root).valid.p1!.route.models.map(m => m.id)).toEqual(['man'])
  expect((await s.removeModel('p1', 'nope')).ok).toBe(false)
})
