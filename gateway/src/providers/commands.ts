// 供应商的一行快捷命令（方案 3.6）：`/provider add <名字> <地址> <密钥> [openai|anthropic]`（含密钥，
// 从不进账本/chat.log/群聊记录/模型）、`/provider refresh <名字>` 与 `/provider remove <名字>`。纯执行层：
// - 识别、删除消息、校验、调 ProviderService、拼回复文字；回复经注入的 reply 走普通发送（kind=system）。
// - `/provider add` 的识别排在引导拦截与群聊"只记录"之前（poller.interceptBeforeGate）：任何聊天、任何人发来都先接走。
import { checkProviderName, checkBaseURL, cleanSecret, normName, providersPath, readProviders, ProvidersUnreadable, type ProviderApi } from './store'
import { deleteMessageWithRetry, type TelegramApi, type TgMessage } from '../telegram/api'
import { registerSecret, safeError, type Logger } from '../log'
import { fetchFailText, TRUNCATED_NOTE } from './models'
import type { ProviderService, RefreshResult, SaveResult } from './service'

/** 本机接口 `POST /v1/provider/refresh` 的返回（方案 3.8）：HTTP 状态码 + 机读错误码 + 文案 */
export type ApiRefreshResult = { status: number; body: { ok: boolean; error?: string; reason?: string; count?: number; added?: number; removed?: number; kept_manual?: number; text: string } }

/** `/provider add …` 的识别（文字或图片/文件附言，去首尾空白后）：`^/provider(@\S+)?\s+add(\s|$)`，不分大小写 */
export function isProviderAdd(text: string | undefined | null): boolean {
  return !!text && /^\/provider(@\S+)?\s+add(\s|$)/i.test(text.trim())
}

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
  /** 删除正在用的那家时要用：读当前覆盖、清覆盖换回配置文件里的模型（与引导「删除」一致，方案 3.4.4） */
  engine?: { current: () => { provider: string; model: string }; clearOverride: () => { provider: string; model: string } }
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
      const deny = (reason: string, text: string) => { this.d.log.info('command.provider_remove', { chat: chatId, ok: false, reason }); return text }
      if (snap.invalid.some(i => normName(i.name) === normName(name))) return deny('disabled', `【系统】「${name}」配置有误，未启用，不能删除。`)
      if (normName(name) === 'deepseek-official') return deny('builtin', `【系统】只有自建供应商能删除；${name} 是内置的。`)
      if (this.d.configRouteNames().map(normName).includes(normName(name))) return deny('config', `【系统】只有自建供应商能删除；${name} 是配置文件里的。`)
      return deny('not_found', `【系统】没有 ${name} 这个供应商。用 /provider 看有哪些。`)
    }
    const wasInUse = this.d.engine ? normName(this.d.engine.current().provider) === normName(found.name) : false
    const r = await this.d.service.remove(found.name)
    if (r.status === 'not_found') return `【系统】「${found.name}」已经不在了。`
    if (r.status === 'lock_timeout') return '【系统】没删成：别的 bot 正在改供应商，请稍后再试。'
    if (r.status === 'unreadable') return '【系统】没删成：共用供应商文件读不了（格式坏了）。'
    if (r.status === 'save_failed') return `【系统】没删成：${r.why}。`
    let text = `【系统】已删除供应商「${r.name}」。它的密钥会在 ${Math.round(r.graceMs / 60_000)} 分钟后从凭据文件删除（让正在用它的 bot 先切走）。注意：这只是从本机删掉，不会在供应商那边作废这把密钥；如果担心泄露，请到供应商后台作废它。`
    if (wasInUse && this.d.engine) { const c = this.d.engine.clearOverride(); text += `这个 bot 已换回配置文件里的模型：${c.provider} / ${c.model}。` }
    this.d.log.info('command.provider_remove', { chat: chatId, ok: true, name: r.name })
    return text
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

/** 新建/更新（或 /provider add）的结果文案（方案 3.4.2；不含删除说明与按钮）。引导与快捷命令共用 */
export function saveResultText(r: SaveResult, name: string): string {
  if (r.status === 'saved') {
    const extra = r.v1Added ? `（地址补成了 ${r.baseURL}）` : ''
    const trunc = r.truncated ? TRUNCATED_NOTE : ''
    if (r.reason !== null) {
      const verb = r.kind === 'created' ? '已添加' : '已更新'
      return `【系统】${verb}供应商「${r.name}」，但没拉到模型：${fetchFailText({ reason: r.reason, status: r.status2, seconds: r.seconds })}。之后可以在 /provider 里点「刷新模型」，或用 /model →「管理自建供应商的模型」手动加。${extra}${trunc}`
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
