// 供应商的一行快捷命令（方案 3.6）：`/provider add <名字> <地址> <密钥> [openai|anthropic]`（含密钥，
// 从不进账本/chat.log/群聊记录/模型）、`/provider refresh <名字>` 与 `/provider remove <名字>`。纯执行层：
// - 识别、删除消息、校验、调 ProviderService、拼回复文字；回复经注入的 reply 走普通发送（kind=system）。
// - `/provider add` 的识别排在引导拦截与群聊"只记录"之前（poller.interceptBeforeGate）：任何聊天、任何人发来都先接走。
import {
  checkBaseURL, checkProviderName, cleanModelId, cleanSecret, DEFAULT_CONTEXT, hostOf, isContextWindow, isProviderName,
  NAME_FORMAT_WHY, normName, providersPath, readProviders, ProvidersUnreadable, RESERVED, type ProviderApi, type ProvidersSnapshot,
} from './store'
import { deleteMessageWithRetry, type TelegramApi, type TgMessage } from '../telegram/api'
import { registerSecret, safeError, type Logger } from '../log'
import { fetchFailText, TRUNCATED_NOTE } from './models'
import type { ProviderService, RefreshResult, SaveResult } from './service'
import { effortLabel, looksLikeSecret } from '../wizard/text'

/** 本机接口 `POST /v1/provider/refresh` 的返回（方案 3.8）：HTTP 状态码 + 机读错误码 + 文案 */
export type ApiRefreshResult = { status: number; body: { ok: boolean; error?: string; reason?: string; count?: number; added?: number; removed?: number; kept_manual?: number; text: string } }

/** 本机接口（INTERFACE-管理台UI 第 3 节）的统一返回：HTTP 状态码 + 契约里的正文 */
export type ApiResult = { status: number; body: unknown }

/** 机读错误码 + 固定文案（文案是给人看的中文，绝不回显输入值） */
export const apiErr = (status: number, error: string, text: string): ApiResult => ({ status, body: { ok: false, error, text } })

/** providers.json 读不了时的统一文案（三个写端点与详情接口共用，含文件位置） */
export const providersUnreadableText = (root: string): string => `共用供应商文件读不了（格式坏了），先修好 ${providersPath(root)}`

/** `/provider add …` 的识别（文字或图片/文件附言，去首尾空白后）：`^/provider(@\S+)?\s+add(\s|$)`，不分大小写 */
export function isProviderAdd(text: string | undefined | null): boolean {
  return !!text && /^\/provider(@\S+)?\s+add(\s|$)/i.test(text.trim())
}

/** 名字栏里填的是密钥时的拒文（本机接口版）。句式沿用 Telegram 的 KEY_AS_ANSWER_BAD
 *  （「这条像是密钥，没有当作…」），去掉【系统】前缀，也不说「已删除」（本机接口没有删
 *  任何东西）。**不回显输入值**。 */
const NAME_IS_SECRET_TEXT = '这条像是密钥，没有当作供应商名字（密钥要填在密钥那一栏），换个名字'

/** sk- / sk_ 打头的密钥前缀（OpenAI、Anthropic、DeepSeek 几家都是这个形状） */
const SECRET_NAME_PREFIX = /^sk[-_]/i

/** 名字栏像不像密钥。主判据是与 Telegram 同源的 looksLikeSecret；再加一条密钥前缀：
 *  审计复现用的 sk-SECRETNAME-abcdefgh 全是字母、不含数字，looksLikeSecret 判不出来
 *  （它要求字母加数字是为了不误删聊天字，名字场景没有这个约束），只搬那一条等于放走
 *  审计给出的复现 payload。 */
const nameLooksLikeSecret = (name: string): boolean =>
  looksLikeSecret(name) || SECRET_NAME_PREFIX.test(name.trim())
const DELETED_NOTE = '（你发的密钥消息已删除。）'
const NOT_DELETED_NOTE = '（你发的密钥消息没能删除，请手动删掉它。）'
const ADD_USAGE = '【系统】用法：/provider add <名字> <地址> <密钥> [openai|anthropic]（不写格式按 Anthropic）。'
const GROUP_ADD_TEXT = '【系统】/provider add 只能在私聊里用。这条消息已尝试删除；没删掉的话请手动删除，并考虑换一个新密钥。'

export type CommandsDeps = {
  service: ProviderService
  api: TelegramApi
  log: Logger
  root: string
  isOwner: (senderId: string | null) => boolean
  botUsername: () => string
  /** 本 bot 配置文件路由的名字（判"与已有供应商同名"用） */
  configRouteNames: () => string[]
  /** 普通发送（计入出站账本 kind=system） */
  reply: (chatId: string, text: string) => void | Promise<void>
  /** 推进 Telegram offset（和"这条已被接走"一致，方案 Q2） */
  recordUpdate: (updateId: number) => void
  /** 有进行中的引导：先结束它（旧菜单去掉按钮、回"已退出引导。"）。返回 true = 该引导正处理中（本条只删不处理） */
  endWizardForAdd?: (chatId: string) => boolean
  /**
   * 引擎门面（Engine.wizardFacade）。删除正在用的那家时读当前覆盖、清覆盖换回配置文件里的模型（与引导「删除」一致，方案 3.4.4）；
   * efforts / setEffort 供本机接口 /v1/effort（3.5）。单元测试只装配用得到的方法，所以后两个可选
   */
  engine?: {
    current: () => { provider: string; model: string; effort?: string | null }
    clearOverride: () => { provider: string; model: string }
    efforts?: () => Promise<string[]>
    setEffort?: (v: string) => Promise<{ ok: boolean }>
    /** 当前覆盖里的档位（/v1/effort 的 current，没有覆盖为 null） */
    overrideEffort?: () => string | null
  }
}

export class ProviderCommands {
  constructor(private readonly d: CommandsDeps) {}

  /** 消息里的命令文字：文字或图片/文件附言（方案 3.6） */
  private static body(msg: TgMessage): string {
    return (msg.text ?? msg.caption ?? '').trim()
  }

  /**
   * 拦截 `/provider add`（poller.interceptBeforeGate）。返回 true = 已接走：推进 offset、异步删消息与处理。
   * 处理在后台做，不在这个方法里等（不然收消息会被慢操作卡住）。
   */
  interceptBeforeGate(msg: TgMessage, updateId: number): boolean {
    const body = ProviderCommands.body(msg)
    if (!isProviderAdd(body)) return false
    // 先把 offset 推进（和"这条已被接走"一致），再异步删消息、处理
    this.d.recordUpdate(updateId)
    void this.handleAdd(msg, body).catch(e => this.d.log.error('command.provider_add_failed', { err: safeError(e) }))
    return true
  }

  private async handleAdd(msg: TgMessage, body: string): Promise<void> {
    const chatId = String(msg.chat.id)
    // 先把正文里的密钥字段登记为机密（方案 3.11）：这条消息可能因超时等原因删不掉，之后主人引用它时，
    // 入账前替换机密那一步要认得出它。放在最前面，任何分支（非主人、群里、参数不合规）都先登记过。
    this.registerKeyFromBody(body)
    const isPrivate = msg.chat.type === 'private'
    const owner = this.d.isOwner(msg.from ? String(msg.from.id) : null)
    const deleted = await this.deleteMessage(chatId, msg.message_id)

    if (!isPrivate) {
      // 群里：只删，不处理。只有主人且 @ 了本 bot 时回一句提示
      const username = this.d.botUsername()
      const mention = !!username && new RegExp(`^/provider@${escapeRe(username)}\\b`, 'i').test(body)
      this.d.log.info('command.secret_in_group', { chat: chatId, deleted })
      if (owner && mention) await this.d.reply(chatId, GROUP_ADD_TEXT)
      return
    }
    if (!owner) {
      // 非主人私聊：删掉、不回复、不入账
      this.d.log.warn('command.not_owner', { chat: chatId })
      return
    }
    // 主人私聊：先结束进行中的引导（正忙时只删不处理，按 3.3.4 第 1 条）
    if (this.d.endWizardForAdd?.(chatId)) {
      this.d.log.info('command.provider_add', { chat: chatId, ok: false, reason: 'wizard busy' })
      return
    }
    const note = deleted ? DELETED_NOTE : NOT_DELETED_NOTE
    const args = body.replace(/^\/provider(@\S+)?\s+add(\s|$)/i, '').trim()
    const parsed = this.parseAdd(args)
    if ('error' in parsed) {
      await this.d.reply(chatId, `${parsed.error}${note}`)
      this.d.log.info('command.provider_add', { chat: chatId, ok: false, reason: parsed.reason })
      return
    }
    registerSecret(parsed.key) // 收到即登记为机密（方案 3.11）：之后的日志、入账都替换掉它
    const r = await this.d.service.save({ name: parsed.name, api: parsed.api, baseURL: parsed.baseURL, key: parsed.key })
    await this.d.reply(chatId, `${this.saveText(r, parsed.name)}${note}`)
    this.d.log.info('command.provider_add', { chat: chatId, ok: r.status === 'saved', ...(r.status !== 'saved' ? { reason: r.status } : {}) })
  }
  /**
   * 从 `/provider add` 的正文里认出密钥字段（第 3 个参数）并立即登记为机密，不依赖其它参数是否合法、
   * 也不管这条消息删没删掉（方案 3.11）。判定用 cleanSecret 的形态、并排除地址（含 :// 或斜杠），
   * 避免把填错位置的地址当成密钥。含斜杠的真密钥这里不登记（见 3.3.5"像密钥"判定的取舍）。
   */
  private registerKeyFromBody(body: string): void {
    const args = body.replace(/^\/provider(@\S+)?\s+add(\s|$)/i, '').trim()
    const raw = args.split(/\s+/).filter(Boolean)[2]
    if (!raw) return
    const key = cleanSecret(raw)
    if (key && !/^https?:\/\//i.test(key) && !key.includes('/')) registerSecret(key)
  }

  /** 解析 `/provider add` 的参数：`<名字> <地址> <密钥> [openai|anthropic]`（第 4 个不写 = Anthropic） */
  private parseAdd(args: string): { name: string; api: ProviderApi; baseURL: string; key: string } | { error: string; reason: string } {
    const parts = args.split(/\s+/).filter(Boolean)
    if (parts.length < 3 || parts.length > 4) return { error: ADD_USAGE, reason: 'usage' }
    const [name, rawURL, rawKey, fmt] = parts as [string, string, string, string | undefined]
    const nameWhy = checkProviderName(name)
    if (nameWhy) return { error: `【系统】没添加：${nameWhy}。`, reason: 'bad_name' }
    if (this.d.configRouteNames().map(normName).includes(normName(name))) {
      return { error: `【系统】没添加：${name} 是内置、配置文件里的供应商或保留字，不能用，换个名字。`, reason: 'bad_name' }
    }
    let api: ProviderApi = 'anthropic-messages'
    if (fmt !== undefined) {
      const f = fmt.toLowerCase()
      if (f === 'openai') api = 'openai-completions'
      else if (f === 'anthropic') api = 'anthropic-messages'
      else return { error: '【系统】没添加：接口格式只能写 openai 或 anthropic。', reason: 'bad_format' }
    }
    const url = checkBaseURL(rawURL, api)
    if (!url.ok) return { error: `【系统】没添加：${url.why}。`, reason: 'bad_url' }
    const key = cleanSecret(rawKey)
    if (!key) return { error: '【系统】没添加：密钥要是 8–512 个可见英文字符、中间不能有空格。', reason: 'bad_key' }
    return { name, api, baseURL: url.url, key }
  }

  private saveText(r: SaveResult, name: string): string {
    return saveResultText(r, name)
  }

  /**
   * `/provider refresh <名字>`（Engine 命令队列委托进来）。返回要发的文字（含【系统】）；
   * 参数个数不对等即时错误同步返回，真正的刷新在 ProviderService 里跑（锁外拉列表）。
   */
  async runRefresh(args: string): Promise<string> {
    const parts = args.trim().split(/\s+/).filter(Boolean)
    if (parts.length !== 1) return '【系统】用法：/provider refresh <名字>'
    const name = parts[0]!
    let snap
    try {
      snap = readProviders(`${this.root()}/providers.json`)
    } catch (e) {
      if (e instanceof ProvidersUnreadable) return `【系统】共用供应商文件读不了（格式坏了），先修好 ${providersPath(this.root())}`
      throw e
    }
    const found = this.findValid(snap, name)
    if (!found) {
      if (snap.invalid.some(i => normName(i.name) === normName(name))) return `【系统】「${name}」配置有误，未启用，不能刷新。`
      if (normName(name) === 'deepseek-official') return `【系统】只有自建供应商能刷新模型；${name} 是内置的。`
      if (this.d.configRouteNames().map(normName).includes(normName(name))) return `【系统】只有自建供应商能刷新模型；${name} 是配置文件里的。`
      return `【系统】没有 ${name} 这个供应商。用 /provider 看有哪些。`
    }
    if (!this.d.service.credential(found.entry.route.apiKeyEnv)) return `【系统】「${found.name}」缺密钥，先用 /provider →「修改」→「密钥」补上。`
    return this.refreshText(found.name, await this.d.service.refresh(found.name))
  }

  /** 3.4.5 的刷新结果文案（无按钮） */
  refreshText(name: string, r: RefreshResult): string {
    return refreshResultText(this.root(), name, r)
  }

  /**
   * `/provider remove <名字>`（方案 3.6）：只删自建供应商。复用 ProviderService.remove()——密钥进
   * pendingKeyRemovals、按 provider_key_grace_ms 到点才删（D8），不立即删。删正在用的那家时与引导「删除」一致
   * （3.4.4）：立即清覆盖、换回配置文件里的模型，并在文案里说明。
   */
  async runRemove(args: string, chatId = ''): Promise<string> {
    const parts = args.trim().split(/\s+/).filter(Boolean)
    if (parts.length !== 1) return '【系统】用法：/provider remove <名字>'
    return removeOutcomeText(await this.removeCore(parts[0]!, chatId), this.root())
  }

  /**
   * 删除的判定与执行（Telegram 的 runRemove 与本机接口 POST /v1/provider/remove 共用同一套）。
   * 只判定与调 service.remove，不拼文案；清覆盖也在这一层，两条入口行为一致
   */
  private async removeCore(name: string, chatId: string): Promise<RemoveOutcome> {
    const deny = (reason: string) => { this.d.log.info('command.provider_remove', { chat: chatId, ok: false, reason }) }
    let snap
    try {
      snap = readProviders(`${this.root()}/providers.json`)
    } catch (e) {
      if (e instanceof ProvidersUnreadable) return { kind: 'unreadable', where: 'read' }
      throw e
    }
    const found = this.findValid(snap, name)
    if (!found) {
      if (snap.invalid.some(i => normName(i.name) === normName(name))) { deny('disabled'); return { kind: 'disabled', name } }
      if (normName(name) === 'deepseek-official') { deny('builtin'); return { kind: 'builtin', name } }
      if (this.d.configRouteNames().map(normName).includes(normName(name))) { deny('config'); return { kind: 'config', name } }
      deny('not_found')
      return { kind: 'not_found', name }
    }
    const wasInUse = this.d.engine ? normName(this.d.engine.current().provider) === normName(found.name) : false
    const r = await this.d.service.remove(found.name)
    if (r.status === 'not_found') return { kind: 'gone', name: found.name }
    if (r.status === 'lock_timeout') return { kind: 'busy' }
    if (r.status === 'unreadable') return { kind: 'unreadable', where: 'save' }
    if (r.status === 'save_failed') return { kind: 'save_failed', why: r.why }
    const cleared = wasInUse && this.d.engine ? this.d.engine.clearOverride() : null
    this.d.log.info('command.provider_remove', { chat: chatId, ok: true, name: r.name })
    return { kind: 'ok', name: r.name, graceMs: r.graceMs, cleared }
  }

  // ─── 本机接口（INTERFACE-管理台UI 第 3 节）：管理台与 Telegram 走同一套存储与校验 ───

  /**
   * `GET /v1/provider`（3.1）：只列自建。校验通过的在前（按名字升序，输出规整名），不合格的在后（字段降级）。
   * 含地址与模型清单，绝不含密钥（key 只是 ok / missing / unknown 状态词）
   */
  async apiDetail(): Promise<ApiResult> {
    const root = this.root()
    let snap: ProvidersSnapshot
    try {
      snap = readProviders(providersPath(root))
    } catch (e) {
      if (e instanceof ProvidersUnreadable) return apiErr(503, 'providers_unreadable', providersUnreadableText(root))
      throw e
    }
    const configNames = new Set(this.d.configRouteNames().map(normName))
    const good: Record<string, unknown>[] = []
    for (const [name, e] of Object.entries(snap.valid)) {
      const shadowed = configNames.has(normName(name))
      good.push({
        name: normName(name),
        api: e.route.api,
        baseURL: e.route.baseURL,
        modelList: e.route.models.map(m => ({ id: m.id, contextWindow: m.contextWindow, guessed: e.meta.guessedContext.includes(m.id) })),
        manualModels: [...e.meta.manualModels],
        key: this.d.service.credential(e.route.apiKeyEnv) ? 'ok' : 'missing',
        enabled: !shadowed,
        note: shadowed ? '和本 bot 配置文件里的路由同名，未启用' : null,
        lastRefresh: e.meta.lastRefresh,
      })
    }
    good.sort((a, b) => (String(a.name) < String(b.name) ? -1 : String(a.name) > String(b.name) ? 1 : 0))
    const bad = snap.invalid.map(i => ({
      name: normName(i.name), api: null, baseURL: null, modelList: [], manualModels: [],
      key: 'unknown', enabled: false, note: `配置有误，未启用（${i.why}）`, lastRefresh: null,
    }))
    return { status: 200, body: { ok: true, providers: [...good, ...bad] } }
  }

  /**
   * `POST /v1/provider/save`（3.2）：新建 / 更新自建供应商。校验顺序与错误码写死（测试照此断言）：
   * name → 保留与重名 → api → 地址 → 密钥 → 空密钥的 modify 分支 → 空密钥的 create 分支。
   * mode=modify 且密钥留空时用凭据文件里的现有密钥（照抄向导 doSave），绝不把空串传给 service.save
   */
  async apiSave(body: Record<string, unknown>): Promise<ApiResult> {
    // 收到非空密钥第一件事就登记为机密（方案 3.11）：之后任何日志、异常信息里的它都会被替换掉
    const rawKey = typeof body.key === 'string' ? body.key : ''
    if (rawKey) { registerSecret(rawKey); registerSecret(rawKey.trim()) }
    const name = body.name
    // 名字框里塞了密钥：硬拦（nameLooksLikeSecret）。网页这条路原来只有前端一道软拦，
    // 判据与网关不同，直连本机接口就能绕过去，密钥会被写进 providers.json 的键名、
    // 回显进 text、落进 gateway.log。文案与判定都不回显输入值。
    if (typeof name === 'string' && nameLooksLikeSecret(name)) return apiErr(400, 'bad_name', NAME_IS_SECRET_TEXT)
    if (typeof name !== 'string' || !isProviderName(name)) return apiErr(400, 'bad_name', NAME_FORMAT_WHY)
    if (RESERVED.includes(normName(name)) || this.d.configRouteNames().map(normName).includes(normName(name))) {
      return apiErr(400, 'name_taken', `${name} 是内置、配置文件里的供应商或保留字，不能用，换个名字`)
    }
    const apiRaw = body.api
    if (apiRaw !== 'anthropic-messages' && apiRaw !== 'openai-completions') return apiErr(400, 'bad_format', '接口格式只能选 Anthropic 格式或 OpenAI 格式')
    const api: ProviderApi = apiRaw
    const rawURL = body.baseURL
    if (typeof rawURL !== 'string') return apiErr(400, 'bad_url', '地址格式不对，要以 https:// 开头')
    const url = checkBaseURL(rawURL, api)
    if (!url.ok) return apiErr(400, 'bad_url', url.why)
    const clean = rawKey ? cleanSecret(rawKey) : null
    if (rawKey && !clean) return apiErr(400, 'bad_key', '密钥要是 8–512 个可见英文字符、中间不能有空格')
    const mode: 'create' | 'modify' = body.mode === 'modify' ? 'modify' : 'create'
    let key = clean
    if (!key) {
      if (mode === 'create') return apiErr(400, 'needs_key', '新建供应商需要密钥')
      let snap: ProvidersSnapshot
      try {
        snap = readProviders(providersPath(this.root()))
      } catch (e) {
        if (e instanceof ProvidersUnreadable) return apiErr(503, 'providers_unreadable', providersUnreadableText(this.root()))
        throw e
      }
      const found = this.findValid(snap, name)
      if (!found) return { status: 404, body: { ok: false, error: 'not_found', text: `「${name}」已经不在了` } }
      // 换了主机（含端口）或接口格式：旧密钥绝不发往新地址，必须重新输入（硬拦截在网关侧）
      if (found.entry.route.api !== api || hostOf(found.entry.route.baseURL) !== hostOf(url.url)) {
        return apiErr(400, 'needs_key', '换了主机或格式，需要重新输入密钥（旧密钥不会发给新地址）')
      }
      const existing = this.d.service.credential(found.entry.route.apiKeyEnv)
      if (!existing) return apiErr(409, 'key_missing', `「${found.name}」缺密钥，请重新输入密钥`)
      key = existing
    }
    return this.saveApiResult(await this.d.service.save({ name, api, baseURL: url.url, key, mode }), name)
  }

  /** service.save 的结果翻成本机接口的状态码与正文（文案与 Telegram 同源，去掉【系统】前缀） */
  private saveApiResult(r: SaveResult, name: string): ApiResult {
    const noPrefix = (t: string) => t.replace(/^【系统】/, '')
    switch (r.status) {
      case 'saved':
        return { status: 200, body: { ok: true, kind: r.kind, name: r.name, count: r.count, reason: r.reason, text: noPrefix(saveResultText(r, name)), epoch_changed: r.epochChanged } }
      case 'save_failed':
        return { status: 500, body: { ok: false, error: 'save_failed', text: noPrefix(saveResultText(r, name)) } }
      case 'cred_failed':
        return { status: 409, body: { ok: false, error: 'cred_failed', text: noPrefix(saveResultText(r, name)) } }
      case 'lock_timeout':
        return { status: 409, body: { ok: false, error: 'busy', text: noPrefix(saveResultText(r, name)) } }
      case 'unreadable':
        return apiErr(503, 'providers_unreadable', providersUnreadableText(this.root()))
      case 'gone':
        return { status: 404, body: { ok: false, error: 'not_found', text: `「${r.name}」已经不在了，这次没保存` } }
      case 'unsupported':
        return apiErr(400, 'unsupported', `不支持：${r.why}`)
    }
  }

  /** `POST /v1/provider/remove`（3.3）：与 /provider remove 同一套判定与文案（去掉【系统】前缀） */
  async apiRemove(name: unknown): Promise<ApiResult> {
    // 与 apiSave 同一道硬拦：删除端点的名字也会进 text 与日志，像密钥的一律拒
    if (typeof name === 'string' && nameLooksLikeSecret(name)) return apiErr(400, 'bad_name', NAME_IS_SECRET_TEXT)
    if (typeof name !== 'string' || !isProviderName(name)) return apiErr(400, 'bad_name', NAME_FORMAT_WHY)
    const o = await this.removeCore(name, '')
    switch (o.kind) {
      case 'ok': {
        let text = `已删除供应商「${o.name}」。它的密钥会在 ${Math.round(o.graceMs / 60_000)} 分钟后从凭据文件删除（让正在用它的 bot 先切走）。注意：这只是从本机删掉，不会在供应商那边作废这把密钥；如果担心泄露，请到供应商后台作废它。`
        if (o.cleared) text += `这个 bot 已换回配置文件里的模型：${o.cleared.provider} / ${o.cleared.model}。`
        return { status: 200, body: { ok: true, name: o.name, key_grace_ms: o.graceMs, text } }
      }
      case 'unreadable': return apiErr(503, 'providers_unreadable', providersUnreadableText(this.root()))
      case 'disabled': return apiErr(409, 'disabled', `「${o.name}」配置有误，未启用，不能删除`)
      case 'busy': return apiErr(409, 'busy', '没删成：别的 bot 正在改供应商，请稍后再试')
      case 'save_failed': return apiErr(500, 'save_failed', `没删成：${o.why}`)
      case 'gone': return apiErr(404, 'not_found', `「${o.name}」已经不在了`)
      case 'builtin':
      case 'config':
      case 'not_found':
        return apiErr(404, 'not_found', `没有 ${o.name} 这个供应商`)
    }
  }

  /**
   * `POST /v1/provider/model`（3.4）：模型的加、删、改上下文。text 与 Telegram「管理模型」同源三条；
   * add 不给上下文按 131072 算（记入 guessed），set_context 必须给
   */
  async apiModelEdit(body: Record<string, unknown>): Promise<ApiResult> {
    const name = body.name
    if (typeof name !== 'string' || !isProviderName(name)) return apiErr(400, 'bad_name', NAME_FORMAT_WHY)
    let snap: ProvidersSnapshot
    try {
      snap = readProviders(providersPath(this.root()))
    } catch (e) {
      if (e instanceof ProvidersUnreadable) return apiErr(503, 'providers_unreadable', providersUnreadableText(this.root()))
      throw e
    }
    const found = this.findValid(snap, name)
    if (!found) {
      if (snap.invalid.some(i => normName(i.name) === normName(name))) return apiErr(409, 'disabled', `「${name}」配置有误，未启用，不能编辑`)
      return apiErr(404, 'not_found', `没有 ${name} 这个供应商`)
    }
    const action = body.action
    if (action !== 'add' && action !== 'remove' && action !== 'set_context') return apiErr(400, 'bad_action', 'action 只能是 add、remove、set_context')
    const id = cleanModelId(body.id)
    if (!id) return apiErr(400, 'bad_id', '模型名要 1–200 个字符，不能有空白、控制字符，也不能有 ⟦ ⟧')
    const hasCtx = body.contextWindow !== undefined
    if (hasCtx && !isContextWindow(body.contextWindow)) return apiErr(400, 'bad_context', '上下文长度要是 1024 到 100000000 之间的整数')
    if (action === 'set_context' && !hasCtx) return apiErr(400, 'bad_context', '上下文长度要是 1024 到 100000000 之间的整数')
    const ctx = hasCtx ? (body.contextWindow as number) : null
    if (action === 'add') {
      const r = await this.d.service.addModel(found.name, id, ctx)
      if (!r.ok) return this.modelEditFail(r.why, found.name, id)
      return { status: 200, body: { ok: true, text: `已给「${found.name}」加上模型 ${id}（上下文 ${r.ctx ?? DEFAULT_CONTEXT}）。几秒后就能切过去。` } }
    }
    if (action === 'set_context') {
      const r = await this.d.service.setContext(found.name, id, ctx!)
      if (!r.ok) return this.modelEditFail(r.why, found.name, id)
      return { status: 200, body: { ok: true, text: `已把 ${id} 的上下文长度改成 ${ctx}。几秒后生效。` } }
    }
    const manual = found.entry.meta.manualModels.includes(id)
    const before = this.d.engine?.current()
    const r = await this.d.service.removeModel(found.name, id)
    if (!r.ok) return this.modelEditFail(r.why, found.name, id)
    let text = `已从「${found.name}」删掉模型 ${id}。`
    if (!manual) text += '下次「刷新模型」时，对方列表里还有的话它会重新出现。'
    if (found.entry.route.models.filter(m => m.id !== id).length === 0) text += `「${found.name}」没有模型了，暂时不会出现在可选模型里。`
    if (before && this.d.engine && normName(before.provider) === normName(found.name) && before.model === id) {
      const c = this.d.engine.clearOverride()
      text += `这个 bot 正在用它，已换回配置文件里的模型：${c.provider} / ${c.model}。`
    }
    return { status: 200, body: { ok: true, text } }
  }

  /** 模型编辑失败（service 的 ModelEditResult）翻成状态码与文案 */
  private modelEditFail(why: 'not_found' | 'exists' | 'gone' | 'lock_timeout' | 'unreadable' | 'save_failed', name: string, id: string): ApiResult {
    if (why === 'exists') return apiErr(409, 'exists', '这个模型已经有了')
    if (why === 'lock_timeout') return apiErr(409, 'busy', '别的 bot 正在改供应商，请稍后再试')
    if (why === 'unreadable') return apiErr(503, 'providers_unreadable', providersUnreadableText(this.root()))
    if (why === 'save_failed') return apiErr(500, 'save_failed', '没保存成功：写文件失败')
    return apiErr(404, 'not_found', why === 'gone' ? `「${name}」已经不在了` : `「${id}」已经不在了`)
  }

  /**
   * `GET /v1/effort`（3.5）：当前档位与可选档位。问不到 dsh 或模型不支持时 choices 为空数组，仍 200；
   * current 取当前覆盖里的档位，没有覆盖就是 null（与契约一致，配置文件里的默认档位不算数）
   */
  async apiEffortGet(): Promise<ApiResult> {
    let choices: string[] = []
    try {
      if (this.d.engine?.efforts) choices = await this.d.engine.efforts()
    } catch { choices = [] }
    const current = this.d.engine?.overrideEffort ? this.d.engine.overrideEffort() : null
    return { status: 200, body: { ok: true, current, choices } }
  }

  /** `POST /v1/effort`（3.5）：只改思考强度，不换新会话（与 Telegram 的「思考强度」同一套） */
  async apiEffortSet(body: Record<string, unknown>): Promise<ApiResult> {
    const effort = body.effort
    if (typeof effort !== 'string') return apiErr(400, 'bad_body', 'effort 必须是字符串')
    const r = this.d.engine?.setEffort ? await this.d.engine.setEffort(effort) : { ok: false }
    if (!r.ok) return apiErr(409, 'unsupported', '这个模型现在不支持这个档位')
    return { status: 200, body: { ok: true, text: `已把思考强度改成「${effortLabel(effort)}」，下一条消息起生效（不换新会话）。` } }
  }

  /**
   * 本机接口的 `POST /v1/provider/refresh`（方案 3.8）：与 `/provider refresh` 同一套流程，
   * 但把结果翻成 HTTP 状态码 + 机读错误码的契约。text 去掉「【系统】」前缀，其余同 3.6 的回复。
   */
  async apiRefresh(name: string): Promise<ApiRefreshResult> {
    const noPrefix = (t: string) => t.replace(/^【系统】/, '')
    const root = this.root()
    let snap
    try {
      snap = readProviders(`${root}/providers.json`)
    } catch (e) {
      if (e instanceof ProvidersUnreadable) {
        return { status: 503, body: { ok: false, error: 'providers_unreadable', text: noPrefix(`【系统】共用供应商文件读不了（格式坏了），先修好 ${providersPath(root)}`) } }
      }
      throw e
    }
    const found = this.findValid(snap, name)
    if (!found) {
      if (snap.invalid.some(i => normName(i.name) === normName(name))) {
        return { status: 409, body: { ok: false, error: 'disabled', text: noPrefix(`【系统】「${name}」配置有误，未启用，不能刷新。`) } }
      }
      if (normName(name) === 'deepseek-official') {
        return { status: 400, body: { ok: false, error: 'not_custom', text: noPrefix(`【系统】只有自建供应商能刷新模型；${name} 是内置的。`) } }
      }
      if (this.d.configRouteNames().map(normName).includes(normName(name))) {
        return { status: 400, body: { ok: false, error: 'not_custom', text: noPrefix(`【系统】只有自建供应商能刷新模型；${name} 是配置文件里的。`) } }
      }
      return { status: 404, body: { ok: false, error: 'not_found', text: noPrefix(`【系统】没有 ${name} 这个供应商。用 /provider 看有哪些。`) } }
    }
    if (!this.d.service.credential(found.entry.route.apiKeyEnv)) {
      return { status: 409, body: { ok: false, error: 'key_missing', text: noPrefix(`【系统】「${found.name}」缺密钥，先用 /provider →「修改」→「密钥」补上。`) } }
    }
    const r = await this.d.service.refresh(found.name)
    const t = noPrefix(this.refreshText(found.name, r))
    switch (r.status) {
      case 'ok':
        return { status: 200, body: { ok: true, count: r.count, added: r.added, removed: r.removed.length, kept_manual: r.keptManual, text: t } }
      case 'failed':
        return { status: 502, body: { ok: false, error: 'fetch_failed', reason: r.reason, text: t } }
      case 'gone':
      case 'changed':
        return { status: 409, body: { ok: false, error: 'changed', text: t } }
      case 'busy':
      case 'lock_timeout':
        return { status: 409, body: { ok: false, error: 'busy', text: t } }
      case 'key_missing':
        return { status: 409, body: { ok: false, error: 'key_missing', text: t } }
      case 'unreadable':
        return { status: 503, body: { ok: false, error: 'providers_unreadable', text: t } }
      case 'not_found':
        return { status: 404, body: { ok: false, error: 'not_found', text: t } }
      case 'disabled':
        return { status: 409, body: { ok: false, error: 'disabled', text: t } }
    }
  }

  private root(): string { return this.d.root }

  private findValid(snap: ReturnType<typeof readProviders>, name: string) {
    const want = normName(name)
    for (const [n, e] of Object.entries(snap.valid)) if (normName(n) === want) return { name: n, entry: e }
    return null
  }

  /** 删一条消息（带重试，方案 3.11）；返回删没删掉 */
  private async deleteMessage(chatId: string, messageId: number): Promise<boolean> {
    const r = await deleteMessageWithRetry(this.d.api, chatId, messageId)
    if (!r.ok) this.d.log.warn('wizard.secret_deleted', { chat: chatId, ok: false, err: r.err })
    return r.ok
  }
}

function escapeRe(s: string): string { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') }

/** 删除自建供应商的结果（runRemove 与本机接口共用同一套判定，文案各自渲染） */
type RemoveOutcome =
  | { kind: 'ok'; name: string; graceMs: number; cleared: { provider: string; model: string } | null }
  /** where=read：读 providers.json 就失败；where=save：删除过程中服务返回读不了 */
  | { kind: 'unreadable'; where: 'read' | 'save' }
  | { kind: 'disabled'; name: string }
  | { kind: 'builtin'; name: string }
  | { kind: 'config'; name: string }
  | { kind: 'not_found'; name: string }
  | { kind: 'gone'; name: string }
  | { kind: 'busy' }
  | { kind: 'save_failed'; why: string }

/** 删除结果的 Telegram 文案（与既有 /provider remove 回复逐字一致） */
function removeOutcomeText(o: RemoveOutcome, root: string): string {
  switch (o.kind) {
    case 'ok': {
      let text = `【系统】已删除供应商「${o.name}」。它的密钥会在 ${Math.round(o.graceMs / 60_000)} 分钟后从凭据文件删除（让正在用它的 bot 先切走）。注意：这只是从本机删掉，不会在供应商那边作废这把密钥；如果担心泄露，请到供应商后台作废它。`
      if (o.cleared) text += `这个 bot 已换回配置文件里的模型：${o.cleared.provider} / ${o.cleared.model}。`
      return text
    }
    case 'unreadable':
      return o.where === 'read'
        ? `【系统】共用供应商文件读不了（格式坏了），先修好 ${providersPath(root)}`
        : '【系统】没删成：共用供应商文件读不了（格式坏了）。'
    case 'disabled': return `【系统】「${o.name}」配置有误，未启用，不能删除。`
    case 'builtin': return `【系统】只有自建供应商能删除；${o.name} 是内置的。`
    case 'config': return `【系统】只有自建供应商能删除；${o.name} 是配置文件里的。`
    case 'not_found': return `【系统】没有 ${o.name} 这个供应商。用 /provider 看有哪些。`
    case 'gone': return `【系统】「${o.name}」已经不在了。`
    case 'busy': return '【系统】没删成：别的 bot 正在改供应商，请稍后再试。'
    case 'save_failed': return `【系统】没删成：${o.why}。`
  }
}

/** 新建/更新（或 /provider add）的结果文案（方案 3.4.2；不含删除说明与按钮）。引导与快捷命令共用 */
export function saveResultText(r: SaveResult, name: string): string {
  if (r.status === 'saved') {
    const extra = r.v1Added ? `（地址补成了 ${r.baseURL}）` : ''
    const trunc = r.truncated ? TRUNCATED_NOTE : ''
    if (r.reason !== null) {
      const verb = r.kind === 'created' ? '已添加' : '已更新'
      return `【系统】${verb}供应商「${r.name}」，但没拉到模型：${fetchFailText({ reason: r.reason, status: r.status2, seconds: r.seconds })}。之后可以在 /provider 里点「刷新模型」，或用 /model →「管理模型」手动加。${extra}${trunc}`
    }
    if (r.kind === 'collided') return `【系统】「${r.name}」刚被别处新建，已改为更新它，拉到 ${r.count} 个模型。${extra}${trunc}`
    const verb = r.kind === 'created' ? '已添加' : '已更新'
    return `【系统】${verb}供应商「${r.name}」，拉到 ${r.count} 个模型。${extra}${trunc}`
  }
  if (r.status === 'cred_failed') return `【系统】供应商「${r.name}」已保存，但密钥没写进去（${r.why}）。请用 /provider →「修改」→「密钥」重试。`
  if (r.status === 'lock_timeout') return '【系统】没保存成功：别的 bot 正在改供应商，请稍后再试。密钥没有写入。'
  if (r.status === 'unreadable') return '【系统】没保存成功：共用供应商文件读不了（格式坏了）。密钥没有写入。'
  if (r.status === 'save_failed') return `【系统】没保存成功：${r.why}。密钥没有写入。`
  if (r.status === 'gone') return `【系统】「${r.name}」已经不在了，这次没保存。`
  return `【系统】没添加：${name} 没保存成功。`
}

/** 刷新模型的结果文案（方案 3.4.5）；root 用来在"读不了"时指出文件位置 */
export function refreshResultText(root: string, name: string, r: RefreshResult): string {
  switch (r.status) {
    case 'ok': {
      let t = `【系统】「${name}」拉到 ${r.count} 个模型（新增 ${r.added} 个，去掉 ${r.removed.length} 个；手动加的 ${r.keptManual} 个保留）。`
      if (r.removed.length) t += `\n去掉了：${r.removed.slice(0, 10).join('、')}`
      return t
    }
    case 'failed': return `【系统】「${name}」没拉到模型：${fetchFailText({ reason: r.reason, status: r.status2, seconds: r.seconds })}。模型列表保持不变。`
    case 'gone': return `【系统】「${name}」已经不在了，这次结果没保存。`
    case 'changed': return `【系统】「${name}」刚被别处改过，这次结果没保存，请再刷新一次。`
    case 'busy': return `【系统】「${name}」正在刷新，请稍候。`
    case 'key_missing': return `【系统】「${name}」缺密钥，先用 /provider →「修改」→「密钥」补上。`
    case 'lock_timeout': return '【系统】别的 bot 正在改供应商，请稍后再试。'
    case 'unreadable': return `【系统】共用供应商文件读不了（格式坏了），先修好 ${providersPath(root)}`
    case 'disabled': return `【系统】「${name}」配置有误，未启用，不能刷新。`
    case 'not_found': return `【系统】没有 ${name} 这个供应商。用 /provider 看有哪些。`
  }
}
