// 自建供应商的读写服务（方案 3.4/3.6）：新建、更新、删除、刷新、加删模型、改上下文、处理到点的 pendingKeyRemovals。
// 取舍（方案 3.1.1、Q1、5.1）：联网拉列表在锁外；写 providers.json 与凭据文件在锁内"重读 → 改 → 写"；
// 先写 providers.json 再写凭据（写凭据失败只是"缺密钥"，S7 证实缺键 dsh 照常启动，不会留无主密钥）。
// 每次写完调注入的 onChanged()（Engine.reloadProvidersNow）让本 bot 立刻用上新路由。
import { safeError, type Logger } from '../log'
import {
  apiKeyEnvFor, DEFAULT_CONTEXT, hostOf, isProviderKeyEnv, normName, readCredentialRefs,
  readProviders, setCredentialRef, writeProviders, ProvidersLockTimeout, ProvidersUnreadable, withProvidersLock,
  type ProviderApi, type ProviderEntry, type PendingKeyRemoval, type ProviderModel, type ProvidersSnapshot,
} from './store'
import type { FetchedModel, FetchFailReason, FetchModelsFn, FetchModelsResult } from './models'

export type ServiceDeps = {
  root: string
  credentialsPath: string
  log: Logger
  /** 已绑定好超时与 fetch 的拉列表函数（models.ts） */
  fetchModels: FetchModelsFn
  now?: () => number
  lockWaitMs: number
  keyGraceMs: number
  /** 本 bot 配置文件路由用到的键名（算"已占用的键"时加上，避免撞键） */
  configRouteKeys: () => string[]
  /** 改完 providers.json / 凭据文件后通知 Engine 立刻重算路由 */
  onChanged?: () => void
}

export type SaveResult =
  | { status: 'saved'; kind: 'created' | 'updated' | 'collided'; name: string; api: ProviderApi; baseURL: string; count: number; reason: FetchFailReason | null; status2: number | null; seconds?: number; v1Added: boolean; truncated: boolean; guessed: boolean; epochChanged: boolean }
  | { status: 'save_failed'; why: string }
  | { status: 'cred_failed'; name: string; why: string }
  | { status: 'lock_timeout' }
  | { status: 'unreadable' }
  | { status: 'unsupported'; why: string }
  | { status: 'gone'; name: string }

export type RefreshResult =
  | { status: 'ok'; name: string; count: number; added: number; removed: string[]; keptManual: number }
  | { status: 'failed'; name: string; reason: FetchFailReason; status2: number | null; seconds?: number }
  | { status: 'not_found' }
  | { status: 'disabled' }
  | { status: 'key_missing' }
  | { status: 'gone'; name: string }
  | { status: 'changed'; name: string }
  | { status: 'busy'; name: string }
  | { status: 'lock_timeout' }
  | { status: 'unreadable' }

export type ModelEditResult =
  | { ok: true; ctx?: number }
  | { ok: false; why: 'not_found' | 'exists' | 'gone' | 'lock_timeout' | 'unreadable' }

const crebByEnv = (refs: Record<string, unknown> | null, env: string): string | null => {
  const v = refs?.[env]
  return typeof v === 'string' && v.trim() ? v.trim() : null
}

export class ProviderService {
  private readonly now: () => number
  private readonly refreshing = new Set<string>()

  constructor(private readonly d: ServiceDeps) { this.now = d.now ?? Date.now }

  private providersPath(): string { return `${this.d.root}/providers.json` }

  private read(): ProvidersSnapshot { return readProviders(this.providersPath()) }

  /** 凭据文件里有没有这个键的像样的值（绝不读出、打印值本身） */
  private hasKey(env: string): boolean {
    const refs = this.safeRefs()
    return !!crebByEnv(refs, env)
  }

  private safeRefs(): Record<string, unknown> | null {
    try { return readCredentialRefs(this.d.credentialsPath) } catch { return null }
  }

  /** 算"已占用的键名"（方案 3.1.1）：凭据文件里已有的、自建在用的（含不合格条目的）、待删的、本 bot 配置文件路由用的 */
  private takenKeys(snap: ProvidersSnapshot | null): Set<string> {
    const out = new Set<string>(this.d.configRouteKeys())
    const refs = this.safeRefs()
    for (const k of Object.keys(refs ?? {})) out.add(k)
    const providers = (snap?.raw?.providers ?? {}) as Record<string, unknown>
    for (const v of Object.values(providers)) {
      const env = (v && typeof v === 'object' ? (v as Record<string, unknown>).route : null)
      const k = env && typeof env === 'object' ? (env as Record<string, unknown>).apiKeyEnv : null
      if (typeof k === 'string') out.add(k)
    }
    for (const p of snap?.pendingKeyRemovals ?? []) out.add(p.key)
    return out
  }

  private byNorm(snap: ProvidersSnapshot, name: string): { name: string; entry: ProviderEntry } | null {
    const want = normName(name)
    for (const [n, e] of Object.entries(snap.valid)) if (normName(n) === want) return { name: n, entry: e }
    return null
  }

  private isInvalid(snap: ProvidersSnapshot, name: string): boolean {
    const want = normName(name)
    return snap.invalid.some(i => normName(i.name) === want)
  }

  /** 新建/更新：锁外拉列表 → 锁内重读判断新建还是更新 → 先写 providers.json 再写凭据 */
  async save(o: { name: string; api: ProviderApi; baseURL: string; key: string }): Promise<SaveResult> {
    const fetched = await this.d.fetchModels({ api: o.api, baseURL: o.baseURL, key: o.key })
    const fetchOk = fetched.ok
    const storedURL = fetched.ok ? fetched.baseURL : o.baseURL
    const v1Added = fetched.ok ? fetched.v1Added : false
    const truncated = fetched.ok ? fetched.truncated : false
    const failStatus = fetched.ok ? null : fetched.status
    const failSeconds = fetched.ok ? undefined : fetched.seconds
    const models = fetched.ok ? fetched.models : []
    const failReason = fetched.ok ? null : fetched.reason
    this.d.log[fetchOk ? 'info' : 'warn'](fetchOk ? 'provider.models_fetched' : 'provider.models_failed', fetchOk
      ? { name: o.name, count: models.length }
      : { name: o.name, reason: fetched.reason, status: fetched.status })
    let seenBefore = false
    try { seenBefore = !!this.byNorm(this.read(), o.name) } catch { seenBefore = false }

    try {
      return await withProvidersLock(this.d.root, { waitMs: this.d.lockWaitMs, log: this.d.log, now: this.now }, () =>
        this.writeEntry(this.read(), { name: o.name, api: o.api, baseURL: storedURL, key: o.key, models, failReason, failStatus, failSeconds, v1Added, truncated, seenBefore }))
    } catch (e) {
      if (e instanceof ProvidersLockTimeout) return { status: 'lock_timeout' }
      if (e instanceof ProvidersUnreadable) return { status: 'unreadable' }
      this.d.log.error('provider.write_failed', { file: 'providers', err: safeError(e) })
      return { status: 'save_failed', why: `写文件失败（${(e as Error)?.name ?? 'Error'}）` }
    }
  }

  /** 锁内：在锁里重读出的文件上写这个供应商（新建或更新），再写凭据。任何失败返回对应状态 */
  private writeEntry(snap: ProvidersSnapshot, o: { name: string; api: ProviderApi; baseURL: string; key: string; models: FetchedModel[]; failReason: FetchFailReason | null; failStatus: number | null; failSeconds?: number; v1Added: boolean; truncated: boolean; seenBefore: boolean }): SaveResult {
    const providers = snap.raw.providers as Record<string, unknown>
    const existing = this.byNorm(snap, o.name)

    let entryName = existing?.name ?? o.name
    let entry: ProviderEntry | undefined = existing?.entry
    let kind: 'created' | 'updated' | 'collided' = existing ? (o.seenBefore ? 'updated' : 'collided') : 'created'
    if (!existing && this.isInvalid(snap, o.name)) {
      // 文件里已有同名但不合格（更像是主人手改坏了）：不当新建覆盖它，按"保存失败"如实回报
      return { status: 'save_failed', why: '共用供应商文件里有同名但不合格的条目，请先修好或改名' }
    }

    // 供应商身份变了（改接口格式或主机名）= 换了一家（D6）：epoch 设为当前毫秒、换一个新键名，旧键名登记待删
    let epochChanged = false
    const t = this.now()
    const taken = this.takenKeys(snap)
    let keyEnv: string
    if (entry) {
      epochChanged = entry.route.api !== o.api || hostOf(entry.route.baseURL) !== hostOf(o.baseURL)
      keyEnv = epochChanged ? apiKeyEnvFor(entryName, taken) : entry.route.apiKeyEnv
    } else {
      keyEnv = apiKeyEnvFor(entryName, taken)
    }

    const built = this.buildModels(o.models, entry, o.failReason)
    const meta: ProviderEntry['meta'] = entry
      ? { ...entry.meta, updatedAt: t, epoch: epochChanged ? t : entry.meta.epoch, keyRev: (entry.meta.keyRev ?? 0) + 1, guessedContext: built.guessed }
      : { createdAt: t, updatedAt: t, epoch: t, keyRev: 1, guessedContext: built.guessed, ownerContext: [], manualModels: [], lastRefresh: null }
    const route = { api: o.api, baseURL: o.baseURL, apiKeyEnv: keyEnv, models: built.models }
    const pending: PendingKeyRemoval[] = [...snap.pendingKeyRemovals]
    if (entry && epochChanged && isProviderKeyEnv(entry.route.apiKeyEnv)) pending.push({ key: entry.route.apiKeyEnv, after: t + this.d.keyGraceMs })
    writeProviders(this.providersPath(), { ...snap.raw, providers: { ...providers, [entryName]: { route, meta } }, pendingKeyRemovals: pending })
    this.d.log.info(kind === 'created' ? 'provider.created' : 'provider.updated', { name: entryName, api: o.api })
    this.d.onChanged?.() // providers.json 已写成：立刻让本 bot 重算路由

    // 先写 providers.json 成功，再写凭据（写凭据失败只是缺密钥，下次可以只重试凭据）
    try {
      setCredentialRef(this.d.credentialsPath, keyEnv, o.key)
    } catch (e) {
      this.d.log.error('provider.write_failed', { file: 'credentials', err: safeError(e) })
      const why = e instanceof Error && /refs 写法不常见/.test(e.message) ? e.message : `写文件失败（${(e as Error)?.name ?? 'Error'}）`
      return { status: 'cred_failed', name: entryName, why }
    }
    return {
      status: 'saved', kind, name: entryName, api: o.api, baseURL: route.baseURL, count: built.models.length,
      reason: o.failReason, status2: o.failStatus, ...(o.failSeconds !== undefined ? { seconds: o.failSeconds } : {}), v1Added: o.v1Added, truncated: o.truncated, guessed: built.guessed.length > 0, epochChanged,
    }
  }

  /** 由拉列表结果 + 已有条目的手工/主人上下文，算出要落盘的模型列表 */
  private buildModels(fetched: FetchedModel[], entry: ProviderEntry | undefined, failReason: FetchFailReason | null): { models: ProviderModel[]; guessed: string[] } {
    const owner = new Set(entry?.meta.ownerContext ?? [])
    const existingById = new Map((entry?.route.models ?? []).map(m => [m.id, m]))
    const out: ProviderModel[] = []
    const seen = new Set<string>()
    for (const f of fetched) {
      if (seen.has(f.id)) continue
      seen.add(f.id)
      const keep = existingById.get(f.id)
      const ctx = owner.has(f.id) && keep ? keep.contextWindow : (f.contextWindow ?? DEFAULT_CONTEXT)
      out.push({ id: f.id, contextWindow: ctx })
    }
    // 更新时（拉成功或失败都）保留手动加的模型；新建时没有
    for (const id of entry?.meta.manualModels ?? []) {
      if (seen.has(id)) continue
      seen.add(id)
      out.push(existingById.get(id) ?? { id, contextWindow: DEFAULT_CONTEXT })
    }
    if (failReason !== null && entry && fetched.length === 0) {
      // 拉失败：保持原有列表不变（方案 3.4.3）
      const kept = [...entry.route.models]
      return { models: kept, guessed: entry.meta.guessedContext }
    }
    const guessed = out.filter(m => !owner.has(m.id) && fetched.some(f => f.id === m.id && f.contextWindow === null)).map(m => m.id)
    return { models: out, guessed }
  }

  /** 刷新模型列表：锁外拉（key 从凭据现读，读不到 = key_missing）→ 锁内比对 baseURL/api/keyRev 后写 */
  async refresh(name: string): Promise<RefreshResult> {
    const key = normName(name)
    let snap: ProvidersSnapshot
    try { snap = this.read() } catch { return { status: 'unreadable' } }
    const found = this.byNorm(snap, name)
    if (!found) return this.isInvalid(snap, name) ? { status: 'disabled' } : { status: 'not_found' }
    if (!this.hasKey(found.entry.route.apiKeyEnv)) return { status: 'key_missing' }
    if (this.refreshing.has(key)) return { status: 'busy', name: found.name }
    this.refreshing.add(key)
    try {
      const secret = crebByEnv(this.safeRefs(), found.entry.route.apiKeyEnv)
      if (!secret) return { status: 'key_missing' }
      const before = found.entry
      const res: FetchModelsResult = await this.d.fetchModels({ api: before.route.api, baseURL: before.route.baseURL, key: secret })
      if (!res.ok) {
        this.d.log.warn('provider.models_failed', { name: found.name, reason: res.reason, status: res.status })
        try {
          await withProvidersLock(this.d.root, { waitMs: this.d.lockWaitMs, log: this.d.log, now: this.now }, () => {
            const cur = this.read()
            const e = this.byNorm(cur, found.name)
            if (!e) return
            const meta = { ...e.entry.meta, lastRefresh: { at: this.now(), ok: false, count: e.entry.route.models.length, reason: res.reason } }
            const raw = { ...cur.raw, providers: { ...(cur.raw.providers as Record<string, unknown>), [e.name]: { route: e.entry.route, meta } } }
            writeProviders(this.providersPath(), raw)
            this.d.onChanged?.()
          })
        } catch (e) { if (!(e instanceof ProvidersLockTimeout || e instanceof ProvidersUnreadable)) this.d.log.warn('provider.write_failed', { file: 'providers', err: safeError(e) }) }
        this.d.log.info('provider.models_failed', { name: found.name, reason: res.reason, status: res.status })
        return { status: 'failed', name: found.name, reason: res.reason, status2: res.status, ...(res.seconds !== undefined ? { seconds: res.seconds } : {}) }
      }
      this.d.log.info('provider.models_fetched', { name: found.name, count: res.models.length })
      return await withProvidersLock(this.d.root, { waitMs: this.d.lockWaitMs, log: this.d.log, now: this.now }, () => {
        const cur = this.read()
        const e = this.byNorm(cur, found.name)
        if (!e) return { status: 'gone', name: found.name } as RefreshResult
        if (e.entry.route.baseURL !== before.route.baseURL || e.entry.route.api !== before.route.api || e.entry.meta.keyRev !== before.meta.keyRev) {
          return { status: 'changed', name: found.name } as RefreshResult
        }
        const built = this.buildModels(res.models, e.entry, null)
        const beforeIds = new Set(e.entry.route.models.map(m => m.id))
        const afterIds = new Set(built.models.map(m => m.id))
        // <N> 和 lastRefresh.count 都指"拉到的个数"（手动加的另算保留），新增/去掉按拉到的与旧列表比
        const fetchedCount = res.models.length
        const added = res.models.filter(m => !beforeIds.has(m.id)).length
        const removed = e.entry.route.models.filter(m => !afterIds.has(m.id)).map(m => m.id)
        const meta = { ...e.entry.meta, updatedAt: this.now(), guessedContext: built.guessed, lastRefresh: { at: this.now(), ok: true, count: fetchedCount, reason: null } }
        const route = { ...e.entry.route, models: built.models }
        const raw = { ...cur.raw, providers: { ...(cur.raw.providers as Record<string, unknown>), [e.name]: { route, meta } } }
        writeProviders(this.providersPath(), raw)
        this.d.onChanged?.()
        return { status: 'ok', name: e.name, count: fetchedCount, added, removed, keptManual: e.entry.meta.manualModels.length } as RefreshResult
      })
    } catch (e) {
      if (e instanceof ProvidersLockTimeout) return { status: 'lock_timeout' }
      if (e instanceof ProvidersUnreadable) return { status: 'unreadable' }
      this.d.log.error('provider.write_failed', { file: 'providers', err: safeError(e) })
      return { status: 'failed', name: found.name, reason: 'bad_response', status2: null }
    } finally {
      this.refreshing.delete(key)
    }
  }

  /** 删除供应商：从 providers.json 删掉，凭据键按 keyGraceMs 登记待删 */
  async remove(name: string): Promise<{ status: 'ok'; name: string; keyEnv: string | null; graceMs: number } | { status: 'not_found' } | { status: 'lock_timeout' } | { status: 'unreadable' } | { status: 'save_failed'; why: string }> {
    try {
      return await withProvidersLock(this.d.root, { waitMs: this.d.lockWaitMs, log: this.d.log, now: this.now }, () => {
        const snap = this.read()
        const found = this.byNorm(snap, name)
        if (!found) return { status: 'not_found' as const }
        const providers = { ...(snap.raw.providers as Record<string, unknown>) }
        delete providers[found.name]
        const pending: PendingKeyRemoval[] = [...snap.pendingKeyRemovals]
        if (isProviderKeyEnv(found.entry.route.apiKeyEnv)) pending.push({ key: found.entry.route.apiKeyEnv, after: this.now() + this.d.keyGraceMs })
        writeProviders(this.providersPath(), { ...snap.raw, providers, pendingKeyRemovals: pending })
        this.d.log.info('provider.deleted', { name: found.name })
        this.d.onChanged?.()
        return { status: 'ok' as const, name: found.name, keyEnv: isProviderKeyEnv(found.entry.route.apiKeyEnv) ? found.entry.route.apiKeyEnv : null, graceMs: this.d.keyGraceMs }
      })
    } catch (e) {
      if (e instanceof ProvidersLockTimeout) return { status: 'lock_timeout' }
      if (e instanceof ProvidersUnreadable) return { status: 'unreadable' }
      this.d.log.error('provider.write_failed', { file: 'providers', err: safeError(e) })
      return { status: 'save_failed', why: `写文件失败（${(e as Error)?.name ?? 'Error'}）` }
    }
  }

  /** 加一个手动模型（id 已校验过；ctx 为 null = 未知按 131072 算并计入 manualModels） */
  async addModel(name: string, id: string, ctx: number | null): Promise<ModelEditResult> {
    return this.edit(name, (e) => {
      if (e.route.models.some(m => m.id === id)) return { why: 'exists' as const }
      const models = [...e.route.models, { id, contextWindow: ctx ?? DEFAULT_CONTEXT }]
      const meta = { ...e.meta, updatedAt: this.now(), manualModels: e.meta.manualModels.includes(id) ? e.meta.manualModels : [...e.meta.manualModels, id] }
      return { entry: { route: { ...e.route, models }, meta }, result: { ok: true as const, ctx: ctx ?? DEFAULT_CONTEXT } }
    })
  }

  async removeModel(name: string, id: string): Promise<ModelEditResult> {
    return this.edit(name, (e) => {
      if (!e.route.models.some(m => m.id === id)) return { why: 'not_found' as const }
      const models = e.route.models.filter(m => m.id !== id)
      const meta = {
        ...e.meta, updatedAt: this.now(),
        manualModels: e.meta.manualModels.filter(x => x !== id),
        ownerContext: e.meta.ownerContext.filter(x => x !== id),
        guessedContext: e.meta.guessedContext.filter(x => x !== id),
      }
      return { entry: { route: { ...e.route, models }, meta }, result: { ok: true as const } }
    })
  }

  /** 改上下文长度：记入 ownerContext（刷新时保留主人手填值），移出 guessedContext */
  async setContext(name: string, id: string, ctx: number): Promise<ModelEditResult> {
    return this.edit(name, (e) => {
      if (!e.route.models.some(m => m.id === id)) return { why: 'not_found' as const }
      const models = e.route.models.map(m => (m.id === id ? { ...m, contextWindow: ctx } : m))
      const meta = {
        ...e.meta, updatedAt: this.now(),
        ownerContext: e.meta.ownerContext.includes(id) ? e.meta.ownerContext : [...e.meta.ownerContext, id],
        guessedContext: e.meta.guessedContext.filter(x => x !== id),
      }
      return { entry: { route: { ...e.route, models }, meta }, result: { ok: true as const, ctx } }
    })
  }

  private async edit(name: string, fn: (e: ProviderEntry) => { why?: 'not_found' | 'exists'; entry?: ProviderEntry; result?: { ok: true; ctx?: number } }): Promise<ModelEditResult> {
    try {
      return await withProvidersLock(this.d.root, { waitMs: this.d.lockWaitMs, log: this.d.log, now: this.now }, () => {
        const snap = this.read()
        const found = this.byNorm(snap, name)
        if (!found) return { ok: false as const, why: 'gone' as const }
        const r = fn(found.entry)
        if (r.why) return { ok: false as const, why: r.why === 'exists' ? 'exists' : 'not_found' }
        const raw = { ...snap.raw, providers: { ...(snap.raw.providers as Record<string, unknown>), [found.name]: r.entry } }
        writeProviders(this.providersPath(), raw)
        this.d.onChanged?.()
        return r.result ?? { ok: true as const }
      })
    } catch (e) {
      if (e instanceof ProvidersLockTimeout) return { ok: false, why: 'lock_timeout' }
      if (e instanceof ProvidersUnreadable) return { ok: false, why: 'unreadable' }
      this.d.log.error('provider.write_failed', { file: 'providers', err: safeError(e) })
      return { ok: false, why: 'gone' }
    }
  }

  /** 到点的待删密钥：从凭据文件删掉，并移出登记。由 Engine 的轮询回调调用（方案 3.1.1） */
  async processPendingKeys(): Promise<void> {
    let snap: ProvidersSnapshot
    try { snap = this.read() } catch { return } // 文件读不了：暂停处理
    const due = snap.pendingKeyRemovals.filter(p => p.after <= this.now())
    if (due.length === 0) return
    try {
      await withProvidersLock(this.d.root, { waitMs: this.d.lockWaitMs, log: this.d.log, now: this.now }, () => {
        const cur = this.read()
        const now = this.now()
        const still = cur.pendingKeyRemovals.filter(p => p.after <= now)
        if (still.length === 0) return
        const done: string[] = []
        for (const p of still) {
          try { setCredentialRef(this.d.credentialsPath, p.key, null) } catch (e) {
            // 凭据文件 refs 写法不常见或验证失败：不删登记，等下次；不阻断别的键
            this.d.log.warn('provider.write_failed', { file: 'credentials', err: safeError(e) })
            continue
          }
          done.push(p.key)
          this.d.log.info('provider.key_removed', { key: p.key })
        }
        if (done.length === 0) return
        const left = cur.pendingKeyRemovals.filter(p => !done.includes(p.key))
        writeProviders(this.providersPath(), { ...cur.raw, pendingKeyRemovals: left })
        this.d.onChanged?.()
      })
    } catch (e) {
      if (!(e instanceof ProvidersLockTimeout || e instanceof ProvidersUnreadable)) this.d.log.warn('provider.write_failed', { file: 'providers', err: safeError(e) })
    }
  }

  /** 凭据文件里某个键的密钥（像样的才返回），只给网关自己用 */
  credential(env: string): string | null { return crebByEnv(this.safeRefs(), env) }
}
