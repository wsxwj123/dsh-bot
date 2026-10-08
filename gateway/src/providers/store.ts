// 自建供应商的共用文件（方案 3.1）：<根>/providers.json 的读、校验、写（读写同一套规则），并进 dsh 路由，
// 多网关写时的文件锁，凭据文件 refs 行的增删改。只做文件和规则，不联网（拉模型列表在 models.ts），不发消息。
// - providers.json 只存地址、格式、键名、模型，不存密钥；密钥只在 credentials.yaml 的 refs 里。
// - 读到的文件一律当外部输入：坏条目只跳过那一条，文件读坏不抛到调用方之外（由调用方决定"当空"还是"保留旧值"）。
// - 写回时在原样解析出的对象上改，保留不认识的字段和不合格的条目（那是主人的数据，不替他删）。
import { closeSync, existsSync, linkSync, openSync, readFileSync, renameSync, rmSync, statSync, writeSync } from 'fs'
import { randomBytes } from 'crypto'
import { join } from 'path'
import type { Route } from '../config'
import { registerSecrets, type Logger } from '../log'
import { sleep, writeAtomic } from '../util'

export type ProviderApi = 'anthropic-messages' | 'openai-completions' | 'openai-responses'
export const PROVIDER_APIS: readonly ProviderApi[] = ['anthropic-messages', 'openai-completions', 'openai-responses']

/** dsh 认的思考档位声明（S9；dsh-llm-pi-ai 的 THINKING_LEVELS）。写错 dsh 会起不来，所以读的时候就筛掉 */
export type ReasoningEfforts = false | Record<string, string | null>
export type ProviderModel = { id: string; contextWindow: number; reasoningEfforts?: ReasoningEfforts }
export type ProviderRoute = { api: ProviderApi; baseURL: string; apiKeyEnv: string; models: ProviderModel[] }
export type LastRefresh = { at: number; ok: boolean; count: number; reason: string | null }
export type ProviderMeta = {
  createdAt: number; updatedAt: number
  /** 供应商身份的一部分（换段判断，方案 3.2 第 6 条）：创建、改格式、改主机时设为当时的毫秒时间 */
  epoch: number
  /** 改密钥的次数（只计数）。刷新时比对它判断"拉取期间密钥被改过" */
  keyRev: number
  guessedContext: string[]; ownerContext: string[]; manualModels: string[]
  lastRefresh: LastRefresh | null
}
export type ProviderEntry = { route: ProviderRoute; meta: ProviderMeta }
export type PendingKeyRemoval = { key: string; after: number }

/** 读到的一份 providers.json */
export type ProvidersSnapshot = {
  /** 原样解析出的对象：写回时在它上面改（保留不认识的字段、不合格的条目） */
  raw: Record<string, unknown>
  /** 校验通过的自建供应商（键是文件里的名字） */
  valid: Record<string, ProviderEntry>
  /** 不合格的条目：在所有 bot 上都不启用（日志 providers.entry_skipped） */
  invalid: { name: string; why: string }[]
  pendingKeyRemovals: PendingKeyRemoval[]
  /** 键名不合规则、被丢弃的待删登记条数（日志 providers.entry_skipped） */
  droppedPending: number
}

/** providers.json 读不了（不是 JSON、顶层结构不对、版本不认识、读文件出错） */
export class ProvidersUnreadable extends Error {
  override name = 'ProvidersUnreadable'
}

export const DEFAULT_CONTEXT = 131072
export const CONTEXT_MIN = 1024
export const CONTEXT_MAX = 100_000_000
const RESERVED = ['add', 'refresh', 'list', 'status', 'default', 'help', 'deepseek-official']
const LEVELS = ['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$/
const KEY_ENV_RE = /^PROVIDER_[A-Z0-9_]+_KEY(_[0-9]+)?$/
/** 模型 id、密钥共用的"可见 ASCII、不含空白"（! 到 ~） */
const VISIBLE = /^[\x21-\x7e]+$/

export const providersPath = (root: string) => join(root, 'providers.json')
export const providersLockPath = (root: string) => join(root, 'providers.lock')

// ─── 名字、地址、键名、模型的规则（引导、快捷命令、读文件共用） ───

/** 规整名：小写、_ 换成 -。规整名相同就算同名（方案 3.1.1，D4） */
export const normName = (name: string) => name.toLowerCase().replace(/_/g, '-')

export const NAME_FORMAT_WHY = '名字只能用英文字母、数字、-、_，以字母或数字开头，最多 32 个字符'

/** 名字本身合不合规（格式 + 保留字）。与已有供应商重名要结合场景判断，不在这里 */
export function checkProviderName(name: string): string | null {
  if (!NAME_RE.test(name)) return NAME_FORMAT_WHY
  if (RESERVED.includes(normName(name))) return `${name} 是内置、配置文件里的供应商或保留字，不能用，换个名字`
  return null
}

/** 只判名字**格式**（不含保留字）：本机接口与管理台按 3.1.1 的规则先筛掉不合法的名字（方案 3.8、3.9） */
export const isProviderName = (name: string): boolean => NAME_RE.test(name)

/** 本机地址：URL 解析后的主机名（小写）精确等于这三个之一（http://127.0.0.1.nip.io 之类都不算） */
export const isLocalHost = (hostname: string) => ['127.0.0.1', 'localhost', '[::1]'].includes(hostname.toLowerCase())

/**
 * 接口地址规则（方案 3.4.2 第 3 步）：合格返回规范化后的地址（去首尾空白和末尾 /；Anthropic 再去掉末尾一个 /v1），
 * 不合格返回原因。读 providers.json 时用同一个函数校验。
 */
export function checkBaseURL(input: string, api: ProviderApi): { ok: true; url: string; strippedV1: boolean } | { ok: false; why: string } {
  const s = input.trim()
  if (s.length > 300) return { ok: false, why: '地址太长（最多 300 个字符）' }
  if (!/^https?:\/\//i.test(s)) return { ok: false, why: '地址格式不对，要以 https:// 开头' }
  let u: URL
  try { u = new URL(s) } catch { return { ok: false, why: '地址格式不对，要以 https:// 开头' } }
  if (u.username || u.password) return { ok: false, why: '地址里不能带用户名和密码' }
  if (s.includes('?') || s.includes('#')) return { ok: false, why: '地址里不能带 ? 或 # 后面的部分' }
  if (u.protocol === 'http:' && !isLocalHost(u.hostname)) return { ok: false, why: '只有本机地址（127.0.0.1、localhost、[::1]）可以用 http://，其它地址请用 https://' }
  let url = s.replace(/\/+$/, '')
  let strippedV1 = false
  // 看的是路径末尾（不是整串末尾）：主机名恰好叫 v1 时（https://v1）不能去掉
  if (api === 'anthropic-messages' && new URL(url).pathname.endsWith('/v1')) {
    url = url.slice(0, -3).replace(/\/+$/, '')
    strippedV1 = true
  }
  return { ok: true, url, strippedV1 }
}

/** 主机名（含端口）：改地址时它变了 = 换了一家（epoch 变，方案 3.1.1、D6） */
export function hostOf(url: string): string {
  try { return new URL(url).host.toLowerCase() } catch { return '' }
}

/** 密钥本身合不合规（方案 3.1.2）：去首尾空白后 8–512 个可见 ASCII，不以 / 开头。合格返回去掉空白后的值 */
export function cleanSecret(input: string): string | null {
  const s = input.trim()
  return s.length >= 8 && s.length <= 512 && VISIBLE.test(s) && !s.startsWith('/') ? s : null
}

/** 模型 id：去首尾空白后 1–200 个可见 ASCII、不含空白。合格返回去掉空白后的值 */
export function cleanModelId(v: unknown): string | null {
  if (typeof v !== 'string') return null
  const s = v.trim()
  return s.length >= 1 && s.length <= 200 && VISIBLE.test(s) ? s : null
}

export const isContextWindow = (v: unknown): v is number => typeof v === 'number' && Number.isInteger(v) && v >= CONTEXT_MIN && v <= CONTEXT_MAX

/** 凭据键名：PROVIDER_<规整名转大写、- 换成 _>_KEY；被占用就依次加 _2、_3…（taken = 凭据文件里已有的、自建在用的、待删的、配置文件路由用的） */
export function apiKeyEnvFor(name: string, taken: Iterable<string>): string {
  const used = new Set(taken)
  const base = `PROVIDER_${normName(name).toUpperCase().replace(/-/g, '_')}_KEY`
  if (!used.has(base)) return base
  for (let i = 2; ; i++) if (!used.has(`${base}_${i}`)) return `${base}_${i}`
}

export const isProviderKeyEnv = (k: unknown): k is string => typeof k === 'string' && KEY_ENV_RE.test(k)

function cleanEfforts(v: unknown): ReasoningEfforts | undefined {
  if (v === false) return false
  if (!v || typeof v !== 'object' || Array.isArray(v)) return undefined
  const e = v as Record<string, unknown>
  const keys = Object.keys(e)
  if (!keys.length || keys.some(k => !LEVELS.includes(k)) || !keys.some(k => k !== 'off')) return undefined
  for (const k of keys) {
    const w = e[k]
    if (w === null ? k !== 'off' : typeof w !== 'string' || !w) return undefined
  }
  return { ...e } as Record<string, string | null>
}

/** 模型条目：id 不合格或上下文长度写了但不合格的整条丢弃；没写上下文长度按未知（131072）；思考档位声明不合格只丢这个字段 */
function cleanModels(v: unknown): ProviderModel[] {
  if (!Array.isArray(v)) return []
  const out: ProviderModel[] = []
  const seen = new Set<string>()
  for (const m of v) {
    if (!m || typeof m !== 'object') continue
    const o = m as Record<string, unknown>
    const id = cleanModelId(o.id)
    if (!id || seen.has(id)) continue
    if (o.contextWindow !== undefined && !isContextWindow(o.contextWindow)) continue
    const efforts = cleanEfforts(o.reasoningEfforts)
    seen.add(id)
    out.push({ id, contextWindow: (o.contextWindow as number | undefined) ?? DEFAULT_CONTEXT, ...(efforts !== undefined ? { reasoningEfforts: efforts } : {}) })
  }
  return out
}

function cleanMeta(v: unknown): ProviderMeta {
  const o = (v && typeof v === 'object' && !Array.isArray(v) ? v : {}) as Record<string, unknown>
  const num = (x: unknown) => (typeof x === 'number' && Number.isFinite(x) ? x : 0)
  const strs = (x: unknown) => (Array.isArray(x) ? x.filter((s): s is string => typeof s === 'string') : [])
  const lr = (o.lastRefresh && typeof o.lastRefresh === 'object' ? o.lastRefresh : null) as Record<string, unknown> | null
  return {
    createdAt: num(o.createdAt), updatedAt: num(o.updatedAt), epoch: num(o.epoch), keyRev: num(o.keyRev),
    guessedContext: strs(o.guessedContext), ownerContext: strs(o.ownerContext), manualModels: strs(o.manualModels),
    lastRefresh: lr && typeof lr.at === 'number' && typeof lr.ok === 'boolean' && typeof lr.count === 'number'
      ? { at: lr.at, ok: lr.ok, count: lr.count, reason: typeof lr.reason === 'string' ? lr.reason : null }
      : null,
  }
}

/**
 * 一个条目合不合格（读写同一套规则，方案 3.1.1）：名字、接口格式、地址、键名形状；模型条目只丢不合格的那几个。
 * 合格时返回整理好的条目（地址是规范化后的；meta 缺的字段补默认值）。与别的条目重名、与本 bot 配置冲突不在这里判断。
 */
export function checkEntry(name: string, v: unknown): { ok: true; entry: ProviderEntry } | { ok: false; why: string } {
  const nameWhy = checkProviderName(name)
  if (nameWhy) return { ok: false, why: nameWhy }
  const route = (v && typeof v === 'object' ? (v as Record<string, unknown>).route : null) as Record<string, unknown> | null
  if (!route || typeof route !== 'object' || Array.isArray(route)) return { ok: false, why: '缺少 route' }
  const api = route.api as ProviderApi
  if (!PROVIDER_APIS.includes(api)) return { ok: false, why: '接口格式不认识' }
  const url = typeof route.baseURL === 'string' ? checkBaseURL(route.baseURL, api) : { ok: false as const, why: '缺少地址' }
  if (!url.ok) return { ok: false, why: url.why }
  if (!isProviderKeyEnv(route.apiKeyEnv)) return { ok: false, why: '密钥键名不合规则（要形如 PROVIDER_…_KEY）' }
  return { ok: true, entry: { route: { api, baseURL: url.url, apiKeyEnv: route.apiKeyEnv, models: cleanModels(route.models) }, meta: cleanMeta((v as Record<string, unknown>).meta) } }
}

const emptySnapshot = (): ProvidersSnapshot => ({ raw: { version: 1, providers: {}, pendingKeyRemovals: [] }, valid: {}, invalid: [], pendingKeyRemovals: [], droppedPending: 0 })

/** 解析并校验 providers.json 的内容。整份读不了抛 ProvidersUnreadable（异常消息里不带文件内容） */
export function parseProviders(text: string): ProvidersSnapshot {
  let raw: unknown
  try { raw = JSON.parse(text) } catch { throw new ProvidersUnreadable('不是合法的 JSON') }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new ProvidersUnreadable('顶层不是对象')
  const o = raw as Record<string, unknown>
  if (o.version !== undefined && o.version !== 1) throw new ProvidersUnreadable('版本不认识')
  const providers = o.providers ?? {}
  if (!providers || typeof providers !== 'object' || Array.isArray(providers)) throw new ProvidersUnreadable('providers 不是对象')
  const pending = o.pendingKeyRemovals ?? []
  if (!Array.isArray(pending)) throw new ProvidersUnreadable('pendingKeyRemovals 不是数组')
  const snap: ProvidersSnapshot = { raw: o, valid: {}, invalid: [], pendingKeyRemovals: [], droppedPending: 0 }
  const names = new Map<string, string>()
  const keys = new Map<string, string>()
  for (const [name, v] of Object.entries(providers as Record<string, unknown>)) {
    const r = checkEntry(name, v)
    if (!r.ok) { snap.invalid.push({ name, why: r.why }); continue }
    // 重名、共用一个键名：只认先出现的那个。共用键名等于把一家的密钥发给另一家的地址，绝不能放过
    const sameName = names.get(normName(name))
    if (sameName !== undefined) { snap.invalid.push({ name, why: `和自建供应商「${sameName}」同名` }); continue }
    const sameKey = keys.get(r.entry.route.apiKeyEnv)
    if (sameKey !== undefined) { snap.invalid.push({ name, why: `密钥键名和「${sameKey}」重复` }); continue }
    names.set(normName(name), name)
    keys.set(r.entry.route.apiKeyEnv, name)
    snap.valid[name] = r.entry
  }
  for (const p of pending) {
    const e = (p && typeof p === 'object' ? p : {}) as Record<string, unknown>
    if (isProviderKeyEnv(e.key) && typeof e.after === 'number' && Number.isFinite(e.after)) snap.pendingKeyRemovals.push({ key: e.key, after: e.after })
    else snap.droppedPending++
  }
  return snap
}

/** 读 providers.json：不存在 = 没有自建供应商；读坏抛 ProvidersUnreadable */
export function readProviders(path: string): ProvidersSnapshot {
  let text: string
  try {
    text = readFileSync(path, 'utf8')
  } catch (e) {
    if ((e as { code?: unknown }).code === 'ENOENT') return emptySnapshot()
    throw new ProvidersUnreadable(`读文件失败（${(e as { code?: unknown }).code ?? 'unknown'}）`)
  }
  return parseProviders(text)
}

/** 整份原子替换写回（600）。raw 是在 readProviders 读到的 raw 上改好的对象 */
export function writeProviders(path: string, raw: Record<string, unknown>): void {
  writeAtomic(path, `${JSON.stringify(raw, null, 2)}\n`, 0o600)
}

export type MergedProviders = {
  /** 交给 dsh 的路由：本 bot 配置文件路由 + 启用且有模型的自建（只含白名单字段） */
  routes: Record<string, Route>
  /** 本 bot 上启用的自建（含 0 个模型、暂不交给 dsh 的） */
  enabled: Record<string, ProviderEntry>
  /** 与本 bot 配置文件路由同名、被遮住的自建（日志 providers.shadowed） */
  shadowed: string[]
  /** 本 bot 上不启用的：文件里不合格的 + 键名与本 bot 配置文件路由相同的（日志 providers.entry_skipped） */
  invalid: { name: string; why: string }[]
}

/**
 * 并进 dsh 的路由（方案 3.2 第 1 条、Q1）。同名时配置文件赢；只交白名单字段（api、baseURL、apiKeyEnv、
 * models[].id/contextWindow/reasoningEfforts），其它字段留在文件里但不交给 dsh；没有模型的不交给 dsh（S5）。
 */
export function mergeRoutes(configRoutes: Record<string, Route>, snap: ProvidersSnapshot | null): MergedProviders {
  const out: MergedProviders = { routes: { ...configRoutes }, enabled: {}, shadowed: [], invalid: [...(snap?.invalid ?? [])] }
  const configNames = new Set(Object.keys(configRoutes).map(normName))
  const configKeys = new Set(Object.values(configRoutes).map(r => r.apiKeyEnv).filter((k): k is string => !!k))
  for (const [name, e] of Object.entries(snap?.valid ?? {})) {
    if (configNames.has(normName(name))) { out.shadowed.push(name); continue }
    if (configKeys.has(e.route.apiKeyEnv)) { out.invalid.push({ name, why: '密钥键名和本 bot 配置文件里的路由相同' }); continue }
    out.enabled[name] = e
    if (!e.route.models.length) continue
    out.routes[name] = {
      api: e.route.api, baseURL: e.route.baseURL, apiKeyEnv: e.route.apiKeyEnv,
      models: e.route.models.map(m => ({ id: m.id, contextWindow: m.contextWindow, ...(m.reasoningEfforts !== undefined ? { reasoningEfforts: m.reasoningEfforts } : {}) })),
    }
  }
  return out
}

// ─── 文件锁 <根>/providers.lock（方案 3.1.3） ───

/** 等锁超时（对外：`别的 bot 正在改供应商，请稍后再试`） */
export class ProvidersLockTimeout extends Error {
  override name = 'ProvidersLockTimeout'
}

/** 残留锁的年龄上限：固定值、不可配。多个 bot 共用一把锁，判断必须一致；正常持锁不到 1 秒 */
export const LOCK_STALE_MS = 120_000

function pidAlive(pid: unknown): boolean {
  if (typeof pid !== 'number' || !Number.isInteger(pid) || pid <= 0) return false
  try { process.kill(pid, 0); return true } catch (e) { return (e as { code?: unknown }).code === 'EPERM' } // EPERM = 进程在、只是不归我们
}

/** "不存在才创建"。已存在（Windows 上正在被删时报 EPERM/EBUSY）返回 false */
function tryCreateLock(path: string, content: string): boolean {
  let fd: number
  try {
    fd = openSync(path, 'wx', 0o600)
  } catch (e) {
    const code = (e as { code?: unknown }).code
    if (code === 'EEXIST' || code === 'EPERM' || code === 'EBUSY') return false
    throw e
  }
  try {
    writeSync(fd, content)
  } catch (e) {
    closeSync(fd)
    rmSync(path, { force: true })
    throw e
  }
  closeSync(fd)
  return true
}

/**
 * 残留锁就清掉，返回原持锁的 pid（看不出来时为 null）；不是残留返回 undefined。
 * 判据：持锁进程已不在（主），或锁已存在超过 120 秒（兜底，pid 被复用时靠它）。
 * 清法：先改名成唯一名字，确认还是刚才看的那份再删；两个进程同时清时，不会把对方刚抢到的新锁删掉。
 */
function clearStaleLock(path: string, now: number): { pid: number | null } | undefined {
  let seen: string
  try { seen = readFileSync(path, 'utf8') } catch { return undefined }
  let info: { pid?: unknown; at?: unknown } | null = null
  try { info = JSON.parse(seen) as { pid?: unknown; at?: unknown } } catch {}
  let at = typeof info?.at === 'number' ? info.at : NaN
  // 内容还没写进去（刚创建）或写坏了：按文件修改时间算年龄
  if (!Number.isFinite(at)) { try { at = statSync(path).mtimeMs } catch { return undefined } }
  const known = info !== null && typeof info.pid === 'number'
  if (!(known && !pidAlive(info!.pid)) && now - at <= LOCK_STALE_MS) return undefined
  const aside = `${path}.stale-${process.pid}-${randomBytes(4).toString('hex')}`
  try { renameSync(path, aside) } catch { return undefined }
  let moved = ''
  try { moved = readFileSync(aside, 'utf8') } catch {}
  if (moved === seen) {
    rmSync(aside, { force: true })
    return { pid: known ? (info!.pid as number) : null }
  }
  // 改名时拿走的已是别人的新锁：锁位置空着就放回去（硬链接不会覆盖已有文件），然后丢掉这份
  try { linkSync(aside, path) } catch {}
  rmSync(aside, { force: true })
  return undefined
}

/**
 * 在锁内执行 fn（锁内只做"重读 → 改 → 写"，不联网）。每 100 毫秒重试，最多等 waitMs；
 * 等不到抛 ProvidersLockTimeout（日志 provider.lock_timeout）；清掉残留锁记 provider.lock_stale_cleared {pid}。
 * fn 结束（含抛错）后删锁：只删自己那份。
 */
export async function withProvidersLock<T>(root: string, o: { waitMs: number; log?: Logger; now?: () => number }, fn: () => T | Promise<T>): Promise<T> {
  const path = providersLockPath(root)
  const now = o.now ?? Date.now
  const deadline = now() + o.waitMs
  let mine = ''
  for (;;) {
    mine = JSON.stringify({ pid: process.pid, at: now() })
    if (tryCreateLock(path, mine)) break
    const cleared = clearStaleLock(path, now())
    if (cleared) { o.log?.warn('provider.lock_stale_cleared', { pid: cleared.pid }); continue }
    if (now() >= deadline) {
      o.log?.warn('provider.lock_timeout', {})
      throw new ProvidersLockTimeout('别的 bot 正在改供应商')
    }
    await sleep(100)
  }
  try {
    return await fn()
  } finally {
    try { if (readFileSync(path, 'utf8') === mine) rmSync(path, { force: true }) } catch {}
  }
}

// ─── 凭据文件 <根>/credentials.yaml 的 refs 行（方案 3.1.2） ───

/** refs 的写法不常见，不敢自动改（对外：`凭据文件的 refs 写法不常见（<why>），请手动编辑凭据文件`） */
export class CredentialsUnsupported extends Error {
  override name = 'CredentialsUnsupported'
  constructor(readonly why: '不是块状写法' | '缩进不一致') { super(`凭据文件的 refs 写法不常见（${why}）`) }
}

/** 改前/改后比对对不上：文件本来就不是合法 YAML、改出了目标键以外的变化、或别的程序同时在改。文件不动（或已如实报错） */
export class CredentialsVerifyFailed extends Error {
  override name = 'CredentialsVerifyFailed'
}

/**
 * 在凭据文件文本里改一个 refs 键（value=null 删掉那一行），其它字节一律不动。纯函数。
 * 块 = `refs:` 行到下一个顶格键之前；新行用块里现有的缩进（块里没有键时两个空格），值按 JSON 字符串转义加双引号；
 * 加在块里最后一个键之后。没有 refs 时在文件末尾补一个 refs 块。
 */
export function editRefsText(text: string, key: string, value: string | null): string {
  const crlf = text.includes('\r\n')
  const eol = crlf ? '\r\n' : '\n'
  const lines = text.split('\n')
  const body = (i: number) => lines[i]!.replace(/\r$/, '')
  const line = (indent: string) => `${indent}${key}: ${JSON.stringify(value)}${crlf ? '\r' : ''}`
  const refsAt = lines.findIndex((_, i) => /^refs\s*:/.test(body(i)))
  if (refsAt < 0) {
    if (value === null) return text
    return `${text === '' || text.endsWith('\n') ? text : text + eol}refs:${eol}  ${key}: ${JSON.stringify(value)}${eol}`
  }
  const rest = body(refsAt).replace(/^refs\s*:/, '').trim()
  if (rest !== '' && !rest.startsWith('#')) throw new CredentialsUnsupported('不是块状写法')
  let end = refsAt + 1
  while (end < lines.length && !/^[^\s#]/.test(body(end))) end++ // 顶格的注释、空行不算块的结束
  const entries: number[] = []
  for (let i = refsAt + 1; i < end; i++) if (body(i).trim() !== '' && !body(i).trim().startsWith('#')) entries.push(i)
  const indents = new Set(entries.map(i => /^\s*/.exec(body(i))![0]))
  if (indents.size > 1 || [...indents].some(s => s.includes('\t'))) throw new CredentialsUnsupported('缩进不一致')
  const indent = [...indents][0] ?? '  '
  const hit = entries.find(i => body(i).startsWith(`${indent}${key}`) && /^\s*:/.test(body(i).slice(indent.length + key.length)))
  const out = [...lines]
  if (hit !== undefined) {
    if (value === null) out.splice(hit, 1)
    else out[hit] = line(indent)
  } else {
    if (value === null) return text
    out.splice((entries.length ? entries[entries.length - 1]! : refsAt) + 1, 0, line(indent))
  }
  return out.join('\n')
}

function parseYamlDoc(text: string): Record<string, unknown> {
  let doc: unknown
  try { doc = text.trim() === '' ? {} : Bun.YAML.parse(text) } catch { throw new CredentialsVerifyFailed('凭据文件不是合法的 YAML') }
  if (doc === null || doc === undefined) return {}
  if (typeof doc !== 'object' || Array.isArray(doc)) throw new CredentialsVerifyFailed('凭据文件顶层不是键值对')
  return doc as Record<string, unknown>
}

/** 凭据文件的 refs：文件不存在为 null；读坏抛错。只给网关自己用，值绝不写日志、不回给任何人 */
export function readCredentialRefs(path: string): Record<string, unknown> | null {
  if (!existsSync(path)) return null
  const refs = parseYamlDoc(readFileSync(path, 'utf8')).refs
  return refs && typeof refs === 'object' && !Array.isArray(refs) ? (refs as Record<string, unknown>) : {}
}

/** 把凭据文件 refs 的全部值登记为机密（启动时、每次写凭据文件后，方案 3.11）。读不了就算了 */
export function registerCredentialSecrets(path: string): void {
  try {
    const values = Object.values(readCredentialRefs(path) ?? {})
    registerSecrets([...values, ...values.map(v => (typeof v === 'string' ? v.trim() : v))])
  } catch {}
}

/** 确认 after 相对 before 只有 refs.<key> 变成了 value（null = 删掉）：其它顶层键、其它 refs 键的值都不变 */
function verifyOnlyKey(before: string, after: string, key: string, value: string | null): void {
  const a = parseYamlDoc(before)
  const b = parseYamlDoc(after)
  const same = (x: unknown, y: unknown) => JSON.stringify(x) === JSON.stringify(y)
  for (const k of new Set([...Object.keys(a), ...Object.keys(b)])) if (k !== 'refs' && !same(a[k], b[k])) throw new CredentialsVerifyFailed('改出了 refs 以外的变化')
  const ra = (a.refs && typeof a.refs === 'object' ? a.refs : {}) as Record<string, unknown>
  const rb = (b.refs && typeof b.refs === 'object' ? b.refs : {}) as Record<string, unknown>
  for (const k of new Set([...Object.keys(ra), ...Object.keys(rb)])) if (k !== key && !same(ra[k], rb[k])) throw new CredentialsVerifyFailed('改动波及了别的键')
  if (value === null ? key in rb : rb[key] !== value) throw new CredentialsVerifyFailed('目标键没改对')
}

/**
 * 增、改、删凭据文件里自建供应商自己的一个键（只认 PROVIDER_…_KEY，其它键碰都不碰）。整份原子替换、600；
 * 写前回读确认没被别处改过，写后回读确认只有这个键变了；失败抛 CredentialsUnsupported / CredentialsVerifyFailed / 写文件的错误，
 * 临时文件由 writeAtomic 删掉，盘上不留含密钥的残余。异常消息里不带密钥。
 */
export function setCredentialRef(path: string, key: string, value: string | null): void {
  if (!isProviderKeyEnv(key)) throw new CredentialsVerifyFailed('只改自建供应商自己的键')
  if (value !== null) registerSecrets([value])
  const before = existsSync(path) ? readFileSync(path, 'utf8') : null
  if (before === null && value === null) return
  const after = before === null ? `version: 1\nrefs:\n  ${key}: ${JSON.stringify(value)}\n` : editRefsText(before, key, value)
  if (after === before) return
  // 新建文件时拿空模板当"改前"：除了这个键，只多出 version: 1
  const base = before ?? 'version: 1\nrefs:\n'
  verifyOnlyKey(base, after, key, value)
  if ((existsSync(path) ? readFileSync(path, 'utf8') : null) !== before) throw new CredentialsVerifyFailed('凭据文件刚被别处改过')
  writeAtomic(path, after, 0o600)
  verifyOnlyKey(base, readFileSync(path, 'utf8'), key, value)
  registerCredentialSecrets(path)
}
