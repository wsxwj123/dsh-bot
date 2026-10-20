// 自建供应商的读写服务（方案 3.4/3.6）：新建、更新、删除、刷新、加删模型、改上下文、处理到点的 pendingKeyRemovals。
// 取舍（方案 3.1.1、Q1、5.1）：联网拉列表在锁外；写 providers.json 与凭据文件在锁内"重读 → 改 → 写"；
// 先写 providers.json 再写凭据（写凭据失败只是"缺密钥"，S7 证实缺键 dsh 照常启动，不会留无主密钥）。
// 每次写完调注入的 onChanged()（Engine.reloadProvidersNow）让本 bot 立刻用上新路由。
import { createHash } from 'crypto'
import { safeError, type Logger } from '../log'
import {
  apiKeyEnvFor, CredentialsUnsupported, DEFAULT_CONTEXT, hostOf, isProviderKeyEnv, normName, readCredentialRefs,
  readProviders, setCredentialRef, writeProviders, ProvidersLockTimeout, ProvidersUnreadable, withProvidersLock,
  type ModelInput, type ProviderApi, type ProviderEntry, type PendingKeyRemoval, type ProviderMeta, type ProviderModel, type ProviderRoute, type ProvidersSnapshot,
} from './store'
import { isBareHost, type FetchedModel, type FetchFailReason, type FetchModelsFn, type FetchModelsResult, type ProbeModelFn } from './models'

export type ServiceDeps = {
  root: string
  credentialsPath: string
  log: Logger
  /** 已绑定好超时与 fetch 的拉列表函数（models.ts） */
  fetchModels: FetchModelsFn
  /** 已绑定好超时与 fetch 的最小对话请求（INTERFACE §12a 的测试连接，models.ts） */
  probeModel: ProbeModelFn
  now?: () => number
  lockWaitMs: number
  keyGraceMs: number
  /** 本 bot 配置文件路由用到的键名（算"已占用的键"时加上，避免撞键） */
  configRouteKeys: () => string[]
  /** 改完 providers.json / 凭据文件后通知 Engine 立刻重算路由 */
  onChanged?: () => void
}

/**
 * 拉到的模型并进清单的三种做法，是封闭的（INTERFACE-dsh修补 第五节）：
 * selection = 按勾选；kept = 清单不动、只对账；full = 重铺成对方返回的全部加手填的。
 */
export type ListApplied = 'selection' | 'kept' | 'full'

/**
 * 对账数字，按保存那一刻文件里的清单算：notInList = 对方这次返回而不在清单里的个数，notReturned = 清单里对方
 * 这次没返回的个数（手填的也算），disjoint = 对方返回了模型、而清单里的一个都没返回。只在 applied 为 kept 时给主人看。
 */
export type ListRecon = { notInList: number; notReturned: number; disjoint: boolean }

export type SaveResult =
  /** count：applied 为 kept 时是对方这次返回的个数，否则是落盘后清单的个数。保存不按勾选，applied 只有两种 */
  | ({ status: 'saved'; kind: 'created' | 'updated' | 'collided'; name: string; api: ProviderApi; baseURL: string; count: number; reason: FetchFailReason | null; status2: number | null; seconds?: number; v1Added: boolean; truncated: boolean; guessed: boolean; epochChanged: boolean
      applied: Exclude<ListApplied, 'selection'> } & ListRecon)
  | { status: 'save_failed'; why: string }
  | { status: 'cred_failed'; name: string; why: string }
  | { status: 'lock_timeout' }
  | { status: 'unreadable' }
  | { status: 'unsupported'; why: string }
  | { status: 'gone'; name: string }

/** 拉取失败的形状（refresh 与 preview 共用，文案也共用） */
export type RefreshFailure = { status: 'failed'; name: string; reason: FetchFailReason; status2: number | null; seconds?: number }

export type RefreshResult =
  | ({ status: 'ok'; name: string; count: number; added: number; removed: string[]; keptManual: number
      /** 落盘后列表长度（INTERFACE §11b：缺省 only 时也给） */
      selected: number
      /** 这次落盘走的哪种做法（selection 时文案尾部追加「（按勾选保留 S 个）」、响应 removed 给数组；kept 时文案是对账句） */
      applied: ListApplied
      /** 这次是在 /v1 下才拉到的：落盘时把保存的地址一并补成了 baseURL（文案尾部追加「（地址补成了 …）」） */
      v1Added: boolean; baseURL: string } & ListRecon)
  | RefreshFailure
  | { status: 'not_found' }
  | { status: 'disabled' }
  | { status: 'key_missing' }
  | { status: 'gone'; name: string }
  | { status: 'changed'; name: string }
  /** 带了 rev，而保存那一刻清单的指纹对不上（预览之后清单被别处改过）：不保存 */
  | { status: 'list_changed'; name: string }
  | { status: 'busy'; name: string }
  | { status: 'lock_timeout' }
  | { status: 'unreadable' }

/** POST /v1/provider/preview 的行（INTERFACE §11a，恒 6 键） */
export type PreviewRow = { id: string; contextWindow: number | null; in_list: boolean; in_fetched: boolean; manual: boolean; default_checked: boolean }
export type PreviewResult =
  /** listRev：拉取前那份清单的指纹，确定时带回来核对 */
  | { status: 'ok'; name: string; count: number; models: PreviewRow[]; listRev: string }
  | RefreshFailure
  | { status: 'not_found' }
  | { status: 'disabled' }
  | { status: 'key_missing' }
  | { status: 'unreadable' }

/** POST /v1/provider/test 的结果（INTERFACE §12a） */
export type TestConnResult =
  | { status: 'ok'; name: string; model: string; latencyMs: number }
  /** bareHost：OpenAI 格式、保存的地址只有域名（回的不是接口返回时，文案提示可能少写了 /v1） */
  | { status: 'failed'; name: string; model: string; reason: FetchFailReason; status2: number | null; seconds?: number; bareHost: boolean }
  | { status: 'not_found' }
  | { status: 'disabled' }
  | { status: 'key_missing' }
  | { status: 'no_models' }
  | { status: 'unreadable' }

export type ModelEditResult =
  | { ok: true; ctx?: number }
  /** save_failed：写盘那一步出错（provider.write_failed），与"条目不存在"分开，本机接口按 500 回 */
  | { ok: false; why: 'not_found' | 'exists' | 'gone' | 'lock_timeout' | 'unreadable' | 'save_failed' }

const crebByEnv = (refs: Record<string, unknown> | null, env: string): string | null => {
  const v = refs?.[env]
  return typeof v === 'string' && v.trim() ? v.trim() : null
}

/**
 * 「上游回了空目录算不算失败」的唯一判据（INTERFACE §11a、§11b），预览与刷新共用这一处。
 * emptyIsOk 为真（预览、带 only 的刷新）时空目录按「拉到 0 个」往下走：有的供应商不提供模型目录、
 * 只能手动加模型，预览放行而落盘拒绝的话，勾选弹窗里点确定永远失败。别的失败照旧算失败。
 */
function isFetchFailure(res: FetchModelsResult, emptyIsOk: boolean): res is Extract<FetchModelsResult, { ok: false }> {
  return !res.ok && !(emptyIsOk && res.reason === 'empty')
}

/** 文件里的原样内容当对象用：不是对象（没写、写坏了）就当空对象 */
const asObject = (v: unknown): Record<string, unknown> => (v && typeof v === 'object' && !Array.isArray(v) ? v : {}) as Record<string, unknown>

/**
 * 清单指纹（INTERFACE-dsh修补 3.3）：预览给出，确定时带回，保存那一刻对不上就不保存。
 * 只看模型名序列和手填名单。上下文长度、时间戳、刷新记录不算进去，所以聊天里的一次对账不会让已经打开的弹窗作废。
 */
export function listRev(entry: ProviderEntry): string {
  return createHash('sha256').update(JSON.stringify([entry.route.models.map(m => m.id), entry.meta.manualModels])).digest('hex').slice(0, 32)
}

export class ProviderService {
  private readonly now: () => number
  private readonly refreshing = new Set<string>()

  constructor(private readonly d: ServiceDeps) { this.now = d.now ?? Date.now }

  private providersPath(): string { return `${this.d.root}/providers.json` }

  /**
   * 写回一个条目时保留文件里原来不认识的字段（条目级与 route 级，如主人手写的 note、route.timeoutMs），
   * 只覆盖本次真正要改的 route 已知字段与 meta（方案 3.1.1「其它字段留在文件里」、store.ts 头注释）。
   * 不这么做会把主人手写字段静默丢掉。
   */
  private withKnownEntry(prev: unknown, known: { route: Omit<ProviderRoute, 'models'> & { models?: ProviderModel[] }; meta: ProviderMeta }): Record<string, unknown> {
    const p = asObject(prev)
    return { ...p, route: { ...asObject(p.route), ...known.route }, meta: known.meta }
  }

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

  /** 新建/更新：锁外拉列表 → 锁内重读判断新建还是更新 → 先写 providers.json 再写凭据。mode='modify' 时供应商已不在就返回 gone，不复活 */
  async save(o: { name: string; api: ProviderApi; baseURL: string; key: string; mode?: 'create' | 'modify' }): Promise<SaveResult> {
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
        this.writeEntry(this.read(), { name: o.name, api: o.api, baseURL: storedURL, key: o.key, models, failReason, failStatus, failSeconds, v1Added, truncated, seenBefore, mode: o.mode ?? 'create' }))
    } catch (e) {
      if (e instanceof ProvidersLockTimeout) return { status: 'lock_timeout' }
      if (e instanceof ProvidersUnreadable) return { status: 'unreadable' }
      this.d.log.error('provider.write_failed', { file: 'providers', err: safeError(e) })
      return { status: 'save_failed', why: `写文件失败（${(e as Error)?.name ?? 'Error'}）` }
    }
  }

  /** 锁内：在锁里重读出的文件上写这个供应商（新建或更新），再写凭据。任何失败返回对应状态 */
  private writeEntry(snap: ProvidersSnapshot, o: { name: string; api: ProviderApi; baseURL: string; key: string; models: FetchedModel[]; failReason: FetchFailReason | null; failStatus: number | null; failSeconds?: number; v1Added: boolean; truncated: boolean; seenBefore: boolean; mode: 'create' | 'modify' }): SaveResult {
    const providers = snap.raw.providers as Record<string, unknown>
    const existing = this.byNorm(snap, o.name)

    // 修改：拉列表期间被别处删掉了 → 这次修改没保存，不复活（方案 3.4.3 第 5 条）
    if (o.mode === 'modify' && !existing) return { status: 'gone', name: o.name }

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

    // 换了一家（或新建）才把清单按对方返回的重铺；落到同一家上时由合并函数决定，清单不为空就不动
    const built = this.mergeList(entry, o.models, { full: epochChanged, failReason: o.failReason })
    const kept = built.applied === 'kept'
    const meta: ProviderEntry['meta'] = entry
      ? { ...entry.meta, updatedAt: t, epoch: epochChanged ? t : entry.meta.epoch, keyRev: (entry.meta.keyRev ?? 0) + 1, guessedContext: built.guessed }
      : { createdAt: t, updatedAt: t, epoch: t, keyRev: 1, guessedContext: built.guessed, ownerContext: [], manualModels: [], lastRefresh: null }
    // 清单不动时不写 models 这个键：文件里原来的模型条目原样留着，地址和键名照常更新
    const route = { api: o.api, baseURL: o.baseURL, apiKeyEnv: keyEnv, ...(kept ? {} : { models: built.models }) }
    const pending: PendingKeyRemoval[] = [...snap.pendingKeyRemovals]
    if (entry && epochChanged && isProviderKeyEnv(entry.route.apiKeyEnv)) pending.push({ key: entry.route.apiKeyEnv, after: t + this.d.keyGraceMs })
    writeProviders(this.providersPath(), { ...snap.raw, providers: { ...providers, [entryName]: this.withKnownEntry(providers[entryName], { route, meta }) }, pendingKeyRemovals: pending })
    this.d.log.info(kind === 'created' ? 'provider.created' : 'provider.updated', { name: entryName, api: o.api })
    this.d.onChanged?.() // providers.json 已写成：立刻让本 bot 重算路由

    // 先写 providers.json 成功，再写凭据（写凭据失败只是缺密钥，下次可以只重试凭据）
    try {
      setCredentialRef(this.d.credentialsPath, keyEnv, o.key)
    } catch (e) {
      if (e instanceof CredentialsUnsupported) {
        this.d.log.warn('provider.credentials_unsupported', { why: e.why })
        return { status: 'cred_failed', name: entryName, why: `${e.message}，请手动编辑凭据文件` }
      }
      this.d.log.error('provider.write_failed', { file: 'credentials', err: safeError(e) })
      return { status: 'cred_failed', name: entryName, why: `写文件失败（${(e as Error)?.name ?? 'Error'}）` }
    }
    return {
      status: 'saved', kind, name: entryName, api: o.api, baseURL: o.baseURL, count: kept ? built.fetched : built.models.length,
      reason: o.failReason, status2: o.failStatus, ...(o.failSeconds !== undefined ? { seconds: o.failSeconds } : {}), v1Added: o.v1Added, truncated: o.truncated, guessed: built.guessed.length > 0, epochChanged,
      applied: kept ? 'kept' : 'full', notInList: built.notInList, notReturned: built.notReturned, disjoint: built.disjoint,
    }
  }

  /**
   * 拉到的模型怎么并进清单——刷新与保存共用的唯一一处（INTERFACE-dsh修补 第五节）。三种做法是封闭的：
   * - selection：给了 only，按勾选（buildSelected）；
   * - kept：没给 only、没要求全量、清单不为空、这次拉成功了——清单、手填名单、猜的上下文名单一个字不动，只算对账数字；
   * - full：其余——重铺成对方返回的全部加手填的（buildModels；拉失败时保持原清单也是它的规则）。
   * 「清单不为空就不动」只在这里判断：调用方只说明要不要全量（刷新是显式带 full，保存是换了一家），不各自去看清单空不空。
   * entry 要取锁里重读的那一份，这样拉取期间清单被别处清空或填上，都按保存那一刻算。
   */
  private mergeList(entry: ProviderEntry | undefined, fetched: FetchedModel[], how: { only?: string[]; full?: boolean; failReason?: FetchFailReason | null }):
    { applied: ListApplied; models: ProviderModel[]; guessed: string[]; manual: string[]; fetched: number } & ListRecon {
    const list = entry?.route.models ?? []
    const fetchedIds = new Set(fetched.map(f => f.id))
    const listIds = new Set(list.map(m => m.id))
    const notReturned = list.filter(m => !fetchedIds.has(m.id)).length
    const recon = {
      fetched: fetchedIds.size, notInList: [...fetchedIds].filter(id => !listIds.has(id)).length, notReturned,
      disjoint: fetchedIds.size > 0 && list.length > 0 && notReturned === list.length,
    }
    const failReason = how.failReason ?? null
    if (entry && how.only !== undefined) return { applied: 'selection', ...this.buildSelected(fetched, entry, how.only), ...recon }
    if (entry && !how.full && list.length > 0 && failReason === null) {
      return { applied: 'kept', models: list, guessed: entry.meta.guessedContext, manual: entry.meta.manualModels, ...recon }
    }
    return { applied: 'full', ...this.buildModels(fetched, entry, failReason), manual: entry?.meta.manualModels ?? [], ...recon }
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
      // input 与上下文长度并列：已有条目有就保留（拉列表不会给出 input）；新出现的模型不带
      out.push({ id: f.id, contextWindow: ctx, ...(keep?.input !== undefined ? { input: [...keep.input] } : {}) })
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

  /**
   * 按勾选落盘（INTERFACE-dsh修补 3.2）。四条合起来保证「预览后什么都不动直接确定，清单原样不变」：
   * - 先后：原来就在清单里、这次被勾上的在前（保持原来的先后），新勾上的（对方返回、原来不在清单）在后（按对方返回的先后）；
   * - 上下文长度：原来就在清单里的整条原样留下，不管对方这次给没给、给的是多少；只有新加进来的才取对方给的值或默认值；
   * - 手填名单：只去掉「在清单里、这次没被勾上」的名字，其余原样（含本来就不在清单的），不因为「勾上而对方没返回」新增；
   * - 猜的上下文名单：被取消勾选的移出，新加进来而对方没给上下文的加进去，其余不变。
   * only 里两边都没有的名字静默忽略。
   */
  private buildSelected(fetched: FetchedModel[], entry: ProviderEntry, only: string[]): { models: ProviderModel[]; guessed: string[]; manual: string[] } {
    const onlySet = new Set(only)
    const inList = new Set(entry.route.models.map(m => m.id))
    const kept = entry.route.models.filter(m => onlySet.has(m.id))
    const dropped = new Set(entry.route.models.filter(m => !onlySet.has(m.id)).map(m => m.id))
    const added: ProviderModel[] = []
    const addedGuessed: string[] = []
    const addedIds = new Set<string>()
    for (const f of fetched) {
      if (!onlySet.has(f.id) || inList.has(f.id) || addedIds.has(f.id)) continue
      addedIds.add(f.id)
      added.push({ id: f.id, contextWindow: f.contextWindow ?? DEFAULT_CONTEXT })
      if (f.contextWindow === null) addedGuessed.push(f.id)
    }
    return {
      models: [...kept, ...added],
      guessed: [...entry.meta.guessedContext.filter(id => !dropped.has(id) && !addedIds.has(id)), ...addedGuessed],
      manual: entry.meta.manualModels.filter(id => !dropped.has(id)),
    }
  }

  /**
   * POST /v1/provider/preview（INTERFACE §11a）：只拉不写——不写 providers.json、不动 lastRefresh、
   * 不调 onChanged、不占 refreshing 去重。拉到 0 个模型也算成功（models 只含仅本地的行）。
   * 行的默认勾选与清单指纹都按拉取之前读到的那份清单算。
   */
  async preview(name: string): Promise<PreviewResult> {
    let snap: ProvidersSnapshot
    try { snap = this.read() } catch { return { status: 'unreadable' } }
    const found = this.byNorm(snap, name)
    if (!found) return this.isInvalid(snap, name) ? { status: 'disabled' } : { status: 'not_found' }
    const secret = crebByEnv(this.safeRefs(), found.entry.route.apiKeyEnv)
    if (!secret) return { status: 'key_missing' }
    const res: FetchModelsResult = await this.d.fetchModels({ api: found.entry.route.api, baseURL: found.entry.route.baseURL, key: secret })
    // 拉到 0 个（empty）在 preview 里不是失败：200 + 只含仅本地的行（§11a）
    if (isFetchFailure(res, true)) {
      this.d.log.warn('provider.models_failed', { name: found.name, reason: res.reason, status: res.status })
      return { status: 'failed', name: found.name, reason: res.reason, status2: res.status, ...(res.seconds !== undefined ? { seconds: res.seconds } : {}) }
    }
    const fetched = res.ok ? res.models : []
    this.d.log.info('provider.models_fetched', { name: found.name, count: fetched.length })
    const listById = new Map(found.entry.route.models.map(m => [m.id, m]))
    const manual = new Set(found.entry.meta.manualModels)
    // 默认勾选（INTERFACE-dsh修补 3.1）：in_list or（清单为空 and in_fetched）。清单不为空时默认勾上的恰好是清单的全部，
    // 新出现的不勾（手填名单里有而清单里没有的也按不在清单算）；清单为空时拉到的全勾，免得第一次要一个个点
    const listEmpty = found.entry.route.models.length === 0
    const rows: PreviewRow[] = []
    const seen = new Set<string>()
    for (const f of fetched) {
      if (seen.has(f.id)) continue
      seen.add(f.id)
      const inList = listById.has(f.id)
      rows.push({ id: f.id, contextWindow: f.contextWindow, in_list: inList, in_fetched: true, manual: manual.has(f.id), default_checked: inList || listEmpty })
    }
    for (const m of found.entry.route.models) {
      if (seen.has(m.id)) continue
      seen.add(m.id)
      rows.push({ id: m.id, contextWindow: m.contextWindow, in_list: true, in_fetched: false, manual: manual.has(m.id), default_checked: true })
    }
    return { status: 'ok', name: found.name, count: fetched.length, models: rows, listRev: listRev(found.entry) }
  }

  /**
   * POST /v1/provider/test（INTERFACE §12a）：真发一次最小对话请求（route.models[0]），只读——不写
   * providers.json、不动 lastRefresh、不调 onChanged、不占 refreshing、不写凭据；日志不带密钥、地址、上游正文。
   */
  async testConn(name: string): Promise<TestConnResult> {
    let snap: ProvidersSnapshot
    try { snap = this.read() } catch { return { status: 'unreadable' } }
    const found = this.byNorm(snap, name)
    if (!found) return this.isInvalid(snap, name) ? { status: 'disabled' } : { status: 'not_found' }
    const secret = crebByEnv(this.safeRefs(), found.entry.route.apiKeyEnv)
    if (!secret) return { status: 'key_missing' }
    const first = found.entry.route.models[0]
    if (!first) return { status: 'no_models' }
    const started = this.now()
    const res = await this.d.probeModel({ api: found.entry.route.api, baseURL: found.entry.route.baseURL, model: first.id, key: secret })
    const latencyMs = Math.max(0, Math.round(this.now() - started))
    if (!res.ok) {
      this.d.log.warn('provider.test_failed', { name: found.name, reason: res.reason, status: res.status })
      const bareHost = found.entry.route.api !== 'anthropic-messages' && isBareHost(found.entry.route.baseURL)
      return { status: 'failed', name: found.name, model: first.id, reason: res.reason, status2: res.status, ...(res.seconds !== undefined ? { seconds: res.seconds } : {}), bareHost }
    }
    this.d.log.info('provider.tested', { name: found.name })
    return { status: 'ok', name: found.name, model: first.id, latencyMs }
  }

  /**
   * 刷新模型列表：锁外拉（key 从凭据现读，读不到 = key_missing）→ 锁内比对 baseURL/api/keyRev 后写。
   * 落盘走哪种做法由 mergeList 定：only 给出时按勾选；否则清单不为空就不动、只对账；要把清单重铺成
   * 对方返回的全部加手填的，必须显式带 full（清单为空时不带也是全部收下）。
   * rev 给出时（预览拿到的清单指纹）在锁里和保存那一刻的清单比，对不上就不保存（list_changed）。
   * 上游回空目录：带 only 时按「拉到 0 个」照勾选落盘、记成成功；only 缺省时仍是失败（reason 为 empty，带不带 full 都一样）。
   */
  async refresh(name: string, o: { only?: string[]; rev?: string; full?: boolean } = {}): Promise<RefreshResult> {
    const { only } = o
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
      // 带 only 时空目录不算失败（§11b 第 8 稿，判据与 preview 同一个）；only 缺省时这一行等价于原来的 !res.ok
      if (isFetchFailure(res, only !== undefined)) {
        this.d.log.warn('provider.models_failed', { name: found.name, reason: res.reason, status: res.status })
        try {
          await withProvidersLock(this.d.root, { waitMs: this.d.lockWaitMs, log: this.d.log, now: this.now }, () => {
            const cur = this.read()
            const e = this.byNorm(cur, found.name)
            if (!e) return
            const meta = { ...e.entry.meta, lastRefresh: { at: this.now(), ok: false, count: e.entry.route.models.length, reason: res.reason } }
            // 刷新失败：模型列表保持不变。原样保留文件里的 route（键序、不认识的字段都不动），只换 meta
            const rawList = cur.raw.providers as Record<string, unknown>
            const rawEntry = (rawList[e.name] && typeof rawList[e.name] === 'object' ? rawList[e.name] : {}) as Record<string, unknown>
            const raw = { ...cur.raw, providers: { ...rawList, [e.name]: { ...rawEntry, route: rawEntry.route ?? e.entry.route, meta } } }
            writeProviders(this.providersPath(), raw)
            this.d.onChanged?.()
          })
        } catch (e) { if (!(e instanceof ProvidersLockTimeout || e instanceof ProvidersUnreadable)) this.d.log.warn('provider.write_failed', { file: 'providers', err: safeError(e) }) }
        this.d.log.info('provider.models_failed', { name: found.name, reason: res.reason, status: res.status })
        return { status: 'failed', name: found.name, reason: res.reason, status2: res.status, ...(res.seconds !== undefined ? { seconds: res.seconds } : {}) }
      }
      // 走到这里只有两种：拉成功；或带 only 而上游回空目录（按拉到 0 个落盘）
      const fetched = res.ok ? res.models : []
      // 这次是在 /v1 下才拉到的：落盘时把保存的地址一并补上，不然聊天时 dsh 仍去请求没有 /v1 的地址（空目录不算拉到，不补）
      const v1Added = res.ok && res.v1Added
      const storedURL = res.ok ? res.baseURL : before.route.baseURL
      this.d.log.info('provider.models_fetched', { name: found.name, count: fetched.length })
      return await withProvidersLock(this.d.root, { waitMs: this.d.lockWaitMs, log: this.d.log, now: this.now }, () => {
        const cur = this.read()
        const e = this.byNorm(cur, found.name)
        if (!e) return { status: 'gone', name: found.name } as RefreshResult
        if (e.entry.route.baseURL !== before.route.baseURL || e.entry.route.api !== before.route.api || e.entry.meta.keyRev !== before.meta.keyRev) {
          return { status: 'changed', name: found.name } as RefreshResult
        }
        // 预览之后清单被别处改过：这次的勾选是照旧清单挑的，不保存
        if (o.rev !== undefined && o.rev !== listRev(e.entry)) return { status: 'list_changed', name: found.name } as RefreshResult
        const built = this.mergeList(e.entry, fetched, { only, full: o.full })
        const beforeIds = new Set(e.entry.route.models.map(m => m.id))
        const afterIds = new Set(built.models.map(m => m.id))
        // <N> 和 lastRefresh.count 都指"拉到的个数"（手动加的另算保留），新增/去掉按落盘集合与旧列表比
        const fetchedCount = fetched.length
        const added = built.models.filter(m => !beforeIds.has(m.id)).length
        const removed = e.entry.route.models.filter(m => !afterIds.has(m.id)).map(m => m.id)
        const stamp = { updatedAt: this.now(), lastRefresh: { at: this.now(), ok: true, count: fetchedCount, reason: null } }
        const rawList = cur.raw.providers as Record<string, unknown>
        const rawEntry = asObject(rawList[e.name])
        // 只对账：在文件里的原条目上只换刷新记录和更新时间（补地址时另换地址），别的一个字不动——不拿整理后的条目去重写，
        // 免得改掉键的先后、手写的字段、读取时被丢掉的模型条目。其余两种做法照常重写清单与两份名单
        const written = built.applied === 'kept'
          ? { ...rawEntry, route: v1Added ? { ...asObject(rawEntry.route), baseURL: storedURL } : rawEntry.route ?? e.entry.route, meta: { ...asObject(rawEntry.meta), ...stamp } }
          : this.withKnownEntry(rawEntry, {
              route: { ...e.entry.route, baseURL: storedURL, models: built.models },
              meta: { ...e.entry.meta, ...stamp, guessedContext: built.guessed, manualModels: built.manual },
            })
        writeProviders(this.providersPath(), { ...cur.raw, providers: { ...rawList, [e.name]: written } })
        this.d.onChanged?.()
        return {
          status: 'ok', name: e.name, count: fetchedCount, added, removed,
          keptManual: built.manual.length, selected: built.models.length, applied: built.applied,
          notInList: built.notInList, notReturned: built.notReturned, disjoint: built.disjoint,
          v1Added, baseURL: storedURL,
        } as RefreshResult
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

  /** 加一个手动模型（id 已校验过；ctx 为 null = 未知按 DEFAULT_CONTEXT 算并计入 manualModels + guessedContext，否则计入 ownerContext） */
  async addModel(name: string, id: string, ctx: number | null): Promise<ModelEditResult> {
    return this.edit(name, (e) => {
      if (e.route.models.some(m => m.id === id)) return { why: 'exists' as const }
      const models = [...e.route.models, { id, contextWindow: ctx ?? DEFAULT_CONTEXT }]
      const meta = {
        ...e.meta, updatedAt: this.now(),
        manualModels: e.meta.manualModels.includes(id) ? e.meta.manualModels : [...e.meta.manualModels, id],
        ownerContext: ctx !== null && !e.meta.ownerContext.includes(id) ? [...e.meta.ownerContext, id] : e.meta.ownerContext,
        guessedContext: ctx === null && !e.meta.guessedContext.includes(id) ? [...e.meta.guessedContext, id] : e.meta.guessedContext,
      }
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

  /** 改模型能收的内容类型：空数组＝删掉 input 键（回到默认只看文字）；不动 meta（不算主人手填的上下文） */
  async setInput(name: string, id: string, input: ModelInput[]): Promise<ModelEditResult> {
    return this.edit(name, (e) => {
      if (!e.route.models.some(m => m.id === id)) return { why: 'not_found' as const }
      const models = e.route.models.map(m => {
        if (m.id !== id) return m
        const next: ProviderModel = { ...m }
        if (input.length === 0) delete next.input
        else next.input = [...input]
        return next
      })
      return { entry: { route: { ...e.route, models }, meta: e.meta }, result: { ok: true as const } }
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
        const rawList = snap.raw.providers as Record<string, unknown>
        const raw = { ...snap.raw, providers: { ...rawList, [found.name]: this.withKnownEntry(rawList[found.name], r.entry!) } }
        writeProviders(this.providersPath(), raw)
        this.d.onChanged?.()
        return r.result ?? { ok: true as const }
      })
    } catch (e) {
      if (e instanceof ProvidersLockTimeout) return { ok: false, why: 'lock_timeout' }
      if (e instanceof ProvidersUnreadable) return { ok: false, why: 'unreadable' }
      this.d.log.error('provider.write_failed', { file: 'providers', err: safeError(e) })
      return { ok: false, why: 'save_failed' }
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
