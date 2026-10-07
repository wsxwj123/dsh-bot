// 主人在私聊里用 /provider add 新建的模型供应商（几个 bot 共用）。
//   /provider add <名字> <接口地址> <密钥> [openai]     末尾写 openai 是 OpenAI 兼容接口，不写按 Anthropic 接口
// - 登记在 <根>/providers.json：名字、协议、地址、密钥在凭据文件里的键名、模型列表。这个文件里没有密钥
// - 密钥只写进 <根>/credentials.yaml（dsh 每次请求按键名去这里取），权限保持 600
// - 每个网关都把这里的供应商并进自己的 dsh 路由（配置文件里的同名路由优先），文件一变，空闲时重启 dsh 生效
// - 这条消息带着密钥：收到后在写进账本之前拦下（poller），不进账本、chat.log、gateway.log，也不交给模型
import { chmodSync, existsSync, readFileSync, renameSync, writeFileSync } from 'fs'
import type { Route, RouteModel } from './config'

export const API_ANTHROPIC = 'anthropic-messages'
export const API_OPENAI = 'openai-completions'
/** 模型列表太长时只存前这么多个（Telegram 里列出来也看不完） */
export const MAX_MODELS = 300
/** 模型列表里没写窗口大小的，按这个算（dsh 的默认是 262144，很多模型没有这么大，按小的算稳妥） */
export const DEFAULT_WINDOW = 128_000

export type SharedProvider = {
  displayName: string
  api: string
  baseURL: string
  apiKeyEnv: string
  models: RouteModel[]
  /** 上次拉模型列表的时间、没拉到的原因（null = 拉到了） */
  fetchedAt: number
  fetchError: string | null
  addedBy: string
  addedAt: number
}

export type ProvidersFile = { version: 1; providers: Record<string, SharedProvider> }

/** 带密钥的命令：poller 在写进账本之前就要认出来 */
export function isSecretCommand(text: string | undefined): boolean {
  return !!text && /^\/providers?(@[A-Za-z0-9_]{3,64})?\s+add(\s|$)/i.test(text.trim())
}

export type AddArgs = { name: string; displayName: string; baseURL: string; key: string; api: string }

/** 解析 /provider add 的参数。出错时的说明里绝不带密钥 */
export function parseAddArgs(text: string): { ok: AddArgs } | { error: string; key?: string } {
  const parts = text.trim().split(/\s+/).slice(2) // 去掉 "/provider" 和 "add"
  const openai = parts.length === 4 && parts[3]!.toLowerCase() === 'openai'
  const key = parts[2]
  if (parts.length < 3 || parts.length > 4 || (parts.length === 4 && !openai)) {
    return { error: '格式不对。', key }
  }
  const [display, url] = parts as [string, string]
  const name = display.toLowerCase()
  if (!/^[a-z0-9][a-z0-9-]{0,39}$/.test(name)) return { error: '名字只能用英文字母、数字和连字符（-），最长 40 个字符。', key }
  if (name === 'deepseek-official') return { error: 'deepseek-official 是自带的供应商，换个名字。', key }
  let u: URL
  try { u = new URL(url) } catch { return { error: '接口地址不对，要以 http:// 或 https:// 开头。', key } }
  if (!/^https?:$/.test(u.protocol)) return { error: '接口地址不对，要以 http:// 或 https:// 开头。', key }
  if (u.username || u.password) return { error: '接口地址里不要带账号密码。', key }
  if (!/^[\x21-\x7e]{8,512}$/.test(key!)) return { error: '密钥看起来不对（至少 8 个字符，不能有空格和中文）。', key }
  return { ok: { name, displayName: display, baseURL: url.replace(/\/+$/, ''), key: key!, api: openai ? API_OPENAI : API_ANTHROPIC } }
}

/** 密钥在凭据文件里的键名 */
export function envNameFor(name: string): string {
  return `PROVIDER_${name.toUpperCase().replace(/-/g, '_')}_API_KEY`
}

export function loadProviders(path: string): ProvidersFile {
  try {
    const d = JSON.parse(readFileSync(path, 'utf8')) as ProvidersFile
    if (d && typeof d.providers === 'object' && d.providers) return { version: 1, providers: d.providers }
  } catch {}
  return { version: 1, providers: {} }
}

function writePrivate(path: string, text: string): void {
  const tmp = `${path}.tmp-${process.pid}-${Date.now()}`
  writeFileSync(tmp, text, { mode: 0o600 })
  if (process.platform !== 'win32') chmodSync(tmp, 0o600)
  renameSync(tmp, path)
}

export function saveProvider(path: string, name: string, p: SharedProvider): void {
  const d = loadProviders(path)
  d.providers[name] = p
  writePrivate(path, JSON.stringify(d, null, 1) + '\n')
}

export function removeProvider(path: string, name: string): boolean {
  const d = loadProviders(path)
  if (!d.providers[name]) return false
  delete d.providers[name]
  writePrivate(path, JSON.stringify(d, null, 1) + '\n')
  return true
}

/** 并进 dsh 路由的部分：只要 dsh 认的字段；没有模型的不并（dsh 要求自定义路由至少有一个模型） */
export function sharedRoutes(path: string): Record<string, Route> {
  const out: Record<string, Route> = {}
  for (const [name, p] of Object.entries(loadProviders(path).providers)) {
    if (!/^[a-z0-9][a-z0-9-]*$/.test(name) || name === 'deepseek-official') continue
    if (![API_ANTHROPIC, API_OPENAI].includes(p?.api) || typeof p.baseURL !== 'string' || !/^https?:\/\//.test(p.baseURL)) continue
    const models = (Array.isArray(p.models) ? p.models : []).filter(m => m && typeof m.id === 'string' && m.id.trim())
    if (models.length === 0) continue
    out[name] = {
      displayName: String(p.displayName || name), api: p.api, baseURL: p.baseURL, apiKeyEnv: p.apiKeyEnv,
      models: models.map(m => ({ id: m.id, ...(m.name ? { name: String(m.name) } : {}), contextWindow: Number(m.contextWindow) > 0 ? Number(m.contextWindow) : DEFAULT_WINDOW })),
    }
  }
  return out
}

/**
 * 把密钥写进凭据文件（refs 下面的一个键）。其他内容原样保留；已有同名键就替换。
 * 写完重新读一遍，确认读出来的就是这个值，否则不写。返回 null 表示成功，否则是原因（不含密钥）
 */
export function setCredential(path: string, envName: string, value: string): string | null {
  let text = existsSync(path) ? readFileSync(path, 'utf8') : 'version: 1\nrefs:\n'
  const line = `  ${envName}: ${JSON.stringify(value)}`
  const re = new RegExp(`^[ \\t]+${envName}[ \\t]*:.*$`, 'm')
  if (re.test(text)) text = text.replace(re, () => line)
  else if (/^refs:[ \t]*$/m.test(text)) text = text.replace(/^refs:[ \t]*$/m, m => `${m}\n${line}`)
  else if (/^refs:/m.test(text)) return '凭据文件的 refs 写法认不出来（不是"refs:"单独一行），请手动加'
  else text = `${text.replace(/\n*$/, '\n')}refs:\n${line}\n`
  let back: unknown
  try { back = (Bun.YAML.parse(text) as { refs?: Record<string, unknown> })?.refs?.[envName] } catch { back = undefined }
  if (back !== value) return '改完以后凭据文件读不出这个密钥，没有写入'
  writePrivate(path, text.endsWith('\n') ? text : text + '\n')
  return null
}

export function removeCredential(path: string, envName: string): void {
  if (!existsSync(path)) return
  const text = readFileSync(path, 'utf8')
  const next = text.replace(new RegExp(`^[ \\t]+${envName}[ \\t]*:.*\\n?`, 'm'), '')
  if (next !== text) writePrivate(path, next)
}

/** 模型列表的地址：OpenAI 兼容是 {地址}/models；Anthropic 是 {根}/v1/models（地址末尾有没有 /v1 都行，和 dsh 一样） */
export function modelsUrl(api: string, baseURL: string): string {
  const b = baseURL.replace(/\/+$/, '')
  return api === API_OPENAI ? `${b}/models` : `${b.replace(/\/v1$/, '')}/v1/models?limit=1000`
}

/** 回包里取模型：标准的 data 数组，或者 models 数组/对象。上下文窗口认几种常见写法 */
export function parseModels(body: unknown): RouteModel[] {
  const b = body as { data?: unknown; models?: unknown }
  let raw: unknown[] = []
  if (Array.isArray(b?.data)) raw = b.data
  else if (Array.isArray(b?.models)) raw = b.models
  else if (b?.models && typeof b.models === 'object') raw = Object.entries(b.models as Record<string, unknown>).map(([id, v]) => ({ ...(v && typeof v === 'object' ? v : {}), id }))
  const out: RouteModel[] = []
  const seen = new Set<string>()
  for (const r of raw) {
    const o = (typeof r === 'string' ? { id: r } : r) as Record<string, unknown>
    const id = typeof o?.id === 'string' ? o.id.trim() : ''
    if (!id || id.length > 200 || seen.has(id)) continue
    seen.add(id)
    const win = [o.context_length, o.context_window, o.contextWindow, o.max_input_tokens, o.max_context_length].map(Number).find(n => Number.isFinite(n) && n > 0)
    const name = typeof o.display_name === 'string' ? o.display_name : typeof o.name === 'string' ? o.name : undefined
    out.push({ id, ...(name && name !== id ? { name } : {}), ...(win ? { contextWindow: Math.floor(win) } : {}) })
    if (out.length >= MAX_MODELS) break
  }
  return out
}

/** 去对方接口拉模型列表。失败的原因只写状态码这类，不带回包内容（有的接口会把密钥原样回显） */
export async function fetchModels(api: string, baseURL: string, key: string, timeoutMs = 20_000): Promise<{ models: RouteModel[] } | { error: string }> {
  const headers: Record<string, string> = api === API_OPENAI
    ? { authorization: `Bearer ${key}` }
    : { 'x-api-key': key, 'anthropic-version': '2023-06-01', authorization: `Bearer ${key}` }
  let res: Response
  try {
    res = await fetch(modelsUrl(api, baseURL), { headers, signal: AbortSignal.timeout(timeoutMs) })
  } catch (e) {
    const err = e as { name?: string; code?: string; cause?: { code?: string } }
    return { error: err?.name === 'TimeoutError' ? `对方接口 ${timeoutMs / 1000} 秒内没有回应` : `连不上对方接口（${String(err?.code ?? err?.cause?.code ?? err?.name ?? '网络错误').slice(0, 40)}）` }
  }
  if (res.status === 401 || res.status === 403) return { error: `对方说密钥不对或没有权限（HTTP ${res.status}）` }
  if (res.status === 404) return { error: '对方接口没有模型列表（HTTP 404）：地址可能不对，或者这家不提供模型列表' }
  if (!res.ok) return { error: `对方接口出错（HTTP ${res.status}）` }
  let body: unknown
  try { body = await res.json() } catch { return { error: '对方回的不是模型列表（不是 JSON）：地址可能不对' } }
  const models = parseModels(body)
  if (models.length === 0) return { error: '对方回的模型列表是空的' }
  return { models }
}
