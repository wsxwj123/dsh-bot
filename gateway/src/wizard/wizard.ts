// 通用引导状态机（方案 3.3、3.4、3.5）：/provider 与 /model 两套引导共用。
// - 拦截：主人在私聊发来的 /provider、/model（无参）、/cancel（引导或防护窗口中）以及引导进行中的任何消息，都在
//   收消息层被接走（intercept），不入账、不写 chat.log、不交给模型；状态与 offset 用同一个账本事务写（meta.wizard:<chat>，D1）。
// - 回调：先把每个回调只 answerCallbackQuery 一次，再按判定顺序（不是主人/不可访问/畸形/过期/处理中/项已不存在/正常）处理。
// - 迟到密钥防护、超时清扫、网关重启中断：方案 3.3.5。
// 引导状态绝不存密钥：密钥只在收到的那一瞬作为局部变量传给 ProviderService。
import type { Ledger } from '../ledger'
import type { Logger } from '../log'
import { registerSecret, safeError } from '../log'
import { deleteMessageWithRetry, isNotModified, type TelegramApi, type TgCallbackQuery, type TgInlineKeyboard, type TgMessage } from '../telegram/api'
import { formatProviders, type ModelChoice, type ProviderView } from '../engine/commands'
import { refreshResultText, saveResultText } from '../providers/commands'
import { checkBaseURL, checkProviderName, cleanModelId, cleanSecret, hostOf, normName, providersPath, type ProviderApi } from '../providers/store'
import type { ProviderService, SaveResult } from '../providers/service'
import { fetchFailText } from '../providers/models'
import { decodeCb, makeNonce, toKeyboard, type Btn } from './keyboard'
import { isProviderAdd } from '../providers/commands'
import * as T from './text'
import * as PF from './provider_flow'
import * as MF from './model_flow'

/** 注入给引导的 Engine 门面（Engine 不 import 引导，避免环） */
export type WizardEngine = {
  views(): Promise<ProviderView[]>
  choices(): Promise<ModelChoice[]>
  efforts(): Promise<string[]>
  switchTo(c: ModelChoice): string
  setEffort(dshValue: string): Promise<{ ok: boolean }>
  revert(): string
  current(): { provider: string; model: string; effort: string | null }
  config(): { provider: string; model: string; effort: string | null }
  clearOverride(): { provider: string; model: string }
  configRoutes(): string[]
  providersReadable(): boolean
  customAll(): { name: string; api: ProviderApi; baseURL: string; keyEnv: string; models: { id: string; ctx: number; guessed: boolean }[]; manualIds: string[]; enabled: boolean }[]
  modelStatusText(chatId: string): string
}

export type WizardDeps = {
  api: TelegramApi
  ledger: Ledger
  log: Logger
  root: string
  isOwner: (senderId: string | null) => boolean
  timeoutMs: number
  lateSecretMs: number
  modelFetchTimeoutMs: number
  lockWaitMs: number
  service: ProviderService
  engine: WizardEngine
  now?: () => number
}

/** 落账本的一步状态（meta.wizard:<chat>）：绝不含密钥 */
type St = {
  flow?: 'provider' | 'model'
  step?: string
  nonce?: string
  menuMessageId?: number | null
  menuText?: string
  name?: string
  api?: ProviderApi
  baseURL?: string
  strippedV1?: boolean
  mode?: 'create' | 'modify'
  list?: string[]
  page?: number
  mField?: '地址' | '密钥' | '接口格式'
  mFromApi?: ProviderApi
  mOrigBase?: string
  keyEnv?: string
  manageName?: string
  addId?: string
  pendingModelId?: string
  addCtxValue?: number | null
  busy?: boolean
  busySince?: number
  op?: string
  savingName?: string
  pendingKeyMessageId?: number | null
  /** 结果步：文字不再被引导接走（照常交给角色），按钮仍可点（不能开防护：正常完成） */
  expiresAt: number
  lateSecretUntil?: number
}

/** 结果步（文字直接交给角色）：引导已正常完成，只是留了几个按钮 */
const FREE_STEPS = new Set<string>([PF.P.result, MF.M.effortResult])

const metaKey = (chatId: string) => `wizard:${chatId}`

export class Wizard {
  private readonly now: () => number
  /** 进行中的引导（含结果步）；key 是 chatId */
  private readonly active = new Map<string, St>()
  /** 迟到密钥防护窗口：chatId → 截止时刻（毫秒） */
  private readonly protection = new Map<string, number>()
  private timer: ReturnType<typeof setInterval> | null = null
  private stopped = false

  constructor(private readonly d: WizardDeps) { this.now = d.now ?? Date.now }

  start(): void {
    // 清扫间隔 = min(15 秒, 超时/4)（方案 3.3.5）
    const period = Math.min(15_000, Math.max(250, Math.floor(this.d.timeoutMs / 4)))
    this.timer = setInterval(() => { void this.sweep() }, period)
  }

  stop(): void { if (this.timer) clearInterval(this.timer); this.timer = null; this.stopped = true }

  /** 启动时先从账本恢复引导与防护窗口（方案 3.3.5；main 里在 poller.start 之前调） */
  recover(): void {
    for (const [k, v] of Object.entries(this.d.ledger.metaByPrefix('wizard:'))) {
      const chatId = k.slice('wizard:'.length)
      let st: St
      try { st = JSON.parse(v) as St } catch { this.d.ledger.deleteMeta(k); continue }
      if (st.busy) {
        // 重启前处在"处理中"：一律结束引导并开防护（方案 3.3.5）
        this.d.log.info('wizard.interrupted', { chat: chatId, op: st.op ?? null })
        if (st.op === 'save') {
          if (st.pendingKeyMessageId) void deleteMessageWithRetry(this.d.api, chatId, st.pendingKeyMessageId).catch(() => {})
          void this.reply(chatId, `【系统】网关重启打断了保存，不能确定「${st.savingName ?? ''}」和它的密钥是否都已写好。请发 /provider 查看（缺密钥会标出来），必要时用「修改」→「密钥」重新输入。`)
        } else {
          void this.reply(chatId, '【系统】网关重启打断了刚才的操作，请重新发 /provider 或 /model。')
        }
        this.openProtection(chatId)
        continue
      }
      if (st.lateSecretUntil && st.lateSecretUntil > this.now()) { this.protection.set(chatId, st.lateSecretUntil); continue }
      if (st.lateSecretUntil) { this.d.ledger.deleteMeta(k); continue }
      if (!st.step || (st.expiresAt && st.expiresAt <= this.now())) { this.d.ledger.deleteMeta(k); continue }
      this.active.set(chatId, st)
    }
  }

  // ─── 底层：状态、消息、回调答复 ───

  private persist(chatId: string, st: St): void { this.d.ledger.setMeta(metaKey(chatId), JSON.stringify(st)) }

  /** 接走一条消息：offset 与引导状态写在同一个事务里（方案 Q2） */
  private takeAtomic(chatId: string, updateId: number, st: St | null): void {
    if (st) {
      this.d.ledger.tx(() => { this.d.ledger.recordUpdate(updateId, null); this.d.ledger.setMeta(metaKey(chatId), JSON.stringify(st)) })
      this.active.set(chatId, st)
    } else {
      this.d.ledger.recordUpdate(updateId, null)
    }
  }

  /** 正常结束引导：清状态，不开防护（「关闭」、切换完成等） */
  private endNormal(chatId: string): void {
    this.active.delete(chatId)
    this.protection.delete(chatId)
    this.d.ledger.deleteMeta(metaKey(chatId))
  }

  /** 非正常结束：清引导、开迟到密钥防护（方案 3.3.5） */
  private openProtection(chatId: string): void {
    const until = this.now() + this.d.lateSecretMs
    this.active.delete(chatId)
    this.protection.set(chatId, until)
    this.d.ledger.setMeta(metaKey(chatId), JSON.stringify({ lateSecretUntil: until, expiresAt: until }))
  }

  private async reply(chatId: string, text: string): Promise<void> {
    await this.d.api.sendMessage(chatId, text).catch(e => this.d.log.warn('wizard.reply_failed', { chat: chatId, err: safeError(e) }))
  }

  private async stripButtons(chatId: string, messageId: number | null | undefined): Promise<void> {
    if (messageId == null) return
    try { await this.d.api.editMessageReplyMarkup(chatId, messageId) } catch {}
  }

  private async deleteMsg(chatId: string, messageId: number): Promise<boolean> {
    const r = await deleteMessageWithRetry(this.d.api, chatId, messageId)
    if (!r.ok) this.d.log.warn('wizard.secret_deleted', { chat: chatId, ok: false, err: r.err })
    else this.d.log.info('wizard.secret_deleted', { chat: chatId, ok: true })
    return r.ok
  }

  private async answer(cq: TgCallbackQuery, text?: string): Promise<void> {
    try { await this.d.api.answerCallbackQuery(cq.id, text) } catch (e) { this.d.log.debug('wizard.answer_failed', { err: safeError(e) }) }
  }

  /** 发一条新消息作为当前菜单；成功则把旧菜单的按钮去掉、换新口令 */
  private async sendMenu(chatId: string, st: St, text: string, buttons: Btn[]): Promise<void> {
    const nonce = makeNonce()
    const prev = st.menuMessageId ?? null
    const r = await this.sendWithRetry(chatId, text, buttons.length ? toKeyboard(nonce, buttons) : undefined)
    if (!r.ok) { await this.sendFailed(chatId, st); return }
    st.nonce = nonce
    st.menuMessageId = r.messageId
    st.menuText = text
    this.persist(chatId, st)
    if (prev && prev !== r.messageId) void this.stripButtons(chatId, prev)
  }

  /** 编辑当前菜单（点按钮推进用）。"message is not modified" 当成功；其它失败改发新消息（方案 3.11） */
  private async editMenu(chatId: string, st: St, text: string, buttons: Btn[]): Promise<void> {
    const nonce = makeNonce()
    const kb = buttons.length ? toKeyboard(nonce, buttons) : undefined
    const id = st.menuMessageId
    if (id == null) { await this.sendMenu(chatId, st, text, buttons); return }
    try {
      await this.d.api.editMessageText(chatId, id, text, kb ? { replyMarkup: kb } : {})
      st.nonce = nonce
      st.menuText = text
      this.persist(chatId, st)
    } catch (e) {
      if (isNotModified(e)) { this.persist(chatId, st); return }
      this.d.log.warn('wizard.edit_failed', { chat: chatId, err: safeError(e) })
      await this.sendMenu(chatId, st, text, buttons)
    }
  }

  /** 发菜单/结果：429 按 retry_after 再试 1 次；仍失败返回 ok:false（调用方结束引导并开防护） */
  private async sendWithRetry(chatId: string, text: string, replyMarkup?: TgInlineKeyboard): Promise<{ ok: true; messageId: number } | { ok: false }> {
    const attempt = async () => await this.d.api.sendMessage(chatId, text, replyMarkup ? { replyMarkup } : {})
    try {
      const m = await attempt()
      return { ok: true, messageId: m.message_id }
    } catch (e) {
      const ra = (e as { status?: number; retryAfter?: number })
      if (ra.status === 429) {
        await new Promise(r => setTimeout(r, Math.min(5, ra.retryAfter ?? 1) * 1000))
        try { const m = await attempt(); return { ok: true, messageId: m.message_id } } catch {}
      }
      return { ok: false }
    }
  }

  private async sendFailed(chatId: string, st: St): Promise<void> {
    this.d.log.warn('wizard.send_failed', { chat: chatId, step: st.step ?? null })
    this.openProtection(chatId)
  }

  // ─── 菜单内容（这一步长什么样） ───

  private async providerMenuText(): Promise<string> {
    const views = await this.d.engine.views()
    const cur = this.d.engine.current()
    let text = formatProviders(views, { provider: cur.provider, model: cur.model })
    if (!this.d.engine.providersReadable()) {
      const warn = `⚠️ 共用供应商文件读不了（格式坏了），自建供应商暂时都不可用，也不能新建/修改/删除。请修好 ${providersPath(this.d.root)}。`
      const lines = text.split('\n')
      lines.splice(1, 0, warn)
      text = lines.join('\n')
    }
    return text
  }

  // ─── 收消息层：拦截（方案 3.3.1、3.3.4、3.3.5） ───

  intercept(msg: TgMessage, updateId: number): boolean {
    if (msg.chat.type !== 'private') return false
    const fromId = msg.from ? String(msg.from.id) : null
    if (!this.d.isOwner(fromId)) return false
    const chatId = String(msg.chat.id)
    const body = (msg.text ?? msg.caption ?? '').trim()
    const st = this.active.get(chatId)

    if (!st) {
      const until = this.protection.get(chatId)
      if (until !== undefined) {
        if (until > this.now()) {
          // 防护窗口里：像密钥的删除并提示；/cancel 接走；其余照常（/provider、/model 会开新菜单）
          if (T.looksLikeLateSecret(body)) { this.takeAtomic(chatId, updateId, null); void this.dropLate(chatId, msg); return true }
          if (isCancel(body)) { this.takeAtomic(chatId, updateId, null); return true }
        } else {
          this.protection.delete(chatId)
          this.d.ledger.deleteMeta(metaKey(chatId))
        }
      }
      if (isProviderNoArg(body)) { this.beginProvider(chatId, updateId); return true }
      if (isModelNoArg(body)) { this.beginModel(chatId, updateId); return true }
      return false
    }

    // 结果步：文字照常交给角色（正常完成，不开防护）；按钮仍由 onCallback 处理
    if (FREE_STEPS.has(st.step ?? '')) return false

    if (st.busy) {
      const del = isProviderAdd(body) || T.looksLikeSecret(body)
      this.takeAtomic(chatId, updateId, st)
      this.d.log.info('wizard.input', { chat: chatId, step: 'busy', deleted: del, chars: body.length })
      void this.busyInput(chatId, msg, del)
      return true
    }

    if (isProviderAdd(body)) { this.takeAtomic(chatId, updateId, st); return true } // 正常在 interceptBeforeGate 处理，这里兜底
    if (isCancel(body)) { this.takeAtomic(chatId, updateId, st); void this.handleCancel(chatId, st, msg); return true }
    if (isProviderNoArg(body)) { this.endOldToProtection(chatId, st); this.beginProvider(chatId, updateId); return true }
    if (isModelNoArg(body)) { this.endOldToProtection(chatId, st); this.beginModel(chatId, updateId); return true }
    if (isModelWithArgs(body) || isProviderWithArgs(body)) { this.endOldToProtection(chatId, st); return false }
    // 密钥步：先校验并把"处理中"连同 offset 一起写掉，密钥只作为局部变量往下传
    const key = isKeyStep(st) ? cleanSecret(body) : null
    if (key) {
      registerSecret(key)
      st.busy = true
      st.busySince = this.now()
      st.op = 'save'
      st.savingName = st.name
      st.pendingKeyMessageId = msg.message_id
    }
    this.touch(st)
    this.takeAtomic(chatId, updateId, st)
    void this.handleInput(chatId, st, msg, body, key)
    return true
  }

  /** 一次有效操作：超时重新计时 */
  private touch(st: St): void { st.expiresAt = this.now() + this.d.timeoutMs }

  /** 被别的东西打断重开：旧引导非正常结束（去按钮 + 开防护），随后开新的 */
  private endOldToProtection(chatId: string, st: St): void {
    void this.stripButtons(chatId, st.menuMessageId)
    this.openProtection(chatId)
  }

  private beginProvider(chatId: string, updateId: number): void {
    const st: St = { flow: 'provider', step: PF.P.main, menuMessageId: null, page: 0, expiresAt: this.now() + this.d.timeoutMs }
    this.takeAtomic(chatId, updateId, st)
    this.protection.delete(chatId)
    void this.showProviderMain(chatId, st)
  }

  private beginModel(chatId: string, updateId: number): void {
    const st: St = { flow: 'model', step: MF.M.main, menuMessageId: null, expiresAt: this.now() + this.d.timeoutMs }
    this.takeAtomic(chatId, updateId, st)
    this.protection.delete(chatId)
    void this.showModelMain(chatId, st)
  }

  private async showProviderMain(chatId: string, st: St): Promise<void> {
    const text = await this.providerMenuText()
    if (!this.active.has(chatId)) return
    await this.sendMenu(chatId, st, text, PF.MAIN_BUTTONS)
  }

  private async showModelMain(chatId: string, st: St): Promise<void> {
    const text = this.d.engine.modelStatusText(chatId)
    if (!this.active.has(chatId)) return
    await this.sendMenu(chatId, st, text, MF.MAIN_BUTTONS)
  }

  /** "处理中"时主人再发消息（方案 3.3.4 第 1 条） */
  private async busyInput(chatId: string, msg: TgMessage, del: boolean): Promise<void> {
    if (del) await this.deleteMsg(chatId, msg.message_id)
    await this.reply(chatId, del ? T.BUSY_DELETED : T.BUSY_KEEP)
  }

  /** 迟到密钥防护挡下一条消息（方案 3.3.5） */
  private async dropLate(chatId: string, msg: TgMessage): Promise<void> {
    const ok = await this.deleteMsg(chatId, msg.message_id)
    this.d.log.info('wizard.late_secret_dropped', { chat: chatId, deleted: ok })
    await this.reply(chatId, T.LATE_SECRET)
  }

  private async handleCancel(chatId: string, st: St, msg: TgMessage): Promise<void> {
    // 密钥步收到的任何消息都先删除（方案 3.3.4 第 3 条）
    if (isKeyStep(st)) await this.deleteMsg(chatId, msg.message_id)
    this.d.log.info('wizard.cancelled', { chat: chatId, by: 'command' })
    await this.editMenu(chatId, st, T.CANCELLED_MENU, [])
    await this.reply(chatId, T.EXITED_CHAT)
    this.openProtection(chatId)
  }

  // ─── 引导进行中收到的文字答案 ───

  private async handleInput(chatId: string, st: St, msg: TgMessage, body: string, key: string | null): Promise<void> {
    this.d.log.info('wizard.input', { chat: chatId, step: st.step ?? null, chars: body.length })
    const hasText = typeof msg.text === 'string' || typeof msg.caption === 'string'
    if (isKeyStep(st)) { await this.onKeyInput(chatId, st, msg, key); return }
    if (!hasText) { await this.reply(chatId, T.THIS_STEP_NEEDS_TEXT); return }
    switch (st.step) {
      case PF.P.name: return this.onNameInput(chatId, st, msg, body)
      case PF.P.url: case PF.P.modifyOpenAIAsk: return this.onUrlInput(chatId, st, msg, body)
      case MF.M.addId: return this.onAddIdInput(chatId, st, msg, body)
      case MF.M.addCtx: return this.onAddCtxInput(chatId, st, msg, body)
      case MF.M.ctxInput: return this.onCtxInput(chatId, st, msg, body)
      default: await this.reply(chatId, T.THIS_STEP_NEEDS_BUTTON); return
    }
  }

  /** 一步的输入不合格：像密钥就删；否则回原因并重问 */
  private async inputError(chatId: string, msg: TgMessage, why: string, hint: string): Promise<void> {
    if (T.looksLikeSecret(msg.text ?? msg.caption ?? '')) {
      await this.deleteMsg(chatId, msg.message_id)
      await this.reply(chatId, `${T.KEY_AS_ANSWER_BAD}\n${hint}`)
      return
    }
    await this.reply(chatId, `【系统】${why}。请重新输入：`)
  }

  /** 管理页/上下文步的输入不合格：把当前菜单编辑成原因并重问（保持这一步的按钮） */
  private async stepError(chatId: string, st: St, msg: TgMessage, why: string, hint: string, buttons: Btn[]): Promise<void> {
    if (T.looksLikeSecret(msg.text ?? msg.caption ?? '')) {
      await this.deleteMsg(chatId, msg.message_id)
      await this.reply(chatId, `${T.KEY_AS_ANSWER_BAD}\n${hint}`)
      return
    }
    await this.editMenu(chatId, st, `【系统】${why}。请重新输入：`, buttons)
  }

  private async onNameInput(chatId: string, st: St, msg: TgMessage, body: string): Promise<void> {
    const why = checkProviderName(body)
    if (why) { await this.inputError(chatId, msg, why, T.nameStepHint); return }
    const norm = normName(body)
    if (this.d.engine.configRoutes().some(n => normName(n) === norm)) {
      await this.reply(chatId, T.nameTaken(body))
      return
    }
    const existing = this.d.engine.customAll().find(c => normName(c.name) === norm)
    st.name = body
    if (existing) {
      st.step = PF.P.collision
      await this.sendMenu(chatId, st, PF.collisionStep(existing.name).text, PF.collisionStep(existing.name).buttons)
      return
    }
    st.mode = 'create'
    st.step = PF.P.format
    const r = PF.formatStep(body)
    await this.sendMenu(chatId, st, r.text, r.buttons)
  }

  private async onUrlInput(chatId: string, st: St, msg: TgMessage, body: string): Promise<void> {
    const api = st.api ?? 'anthropic-messages'
    const res = checkBaseURL(body, api)
    if (!res.ok) { await this.inputError(chatId, msg, res.why, T.urlStepHint); return }
    st.baseURL = res.url
    st.strippedV1 = res.strippedV1
    if (st.mode === 'create') {
      st.step = PF.P.key
      await this.sendMenu(chatId, st, T.CREATE_STEP4, [PF.CANCEL])
      return
    }
    // 修改：换了主机（含端口）或改了格式 → 要求重新输入密钥
    if (st.mField === '接口格式') { st.step = PF.P.key; await this.sendMenu(chatId, st, T.REKEY, [PF.CANCEL]); return }
    const hostChanged = hostOf(res.url) !== hostOf(st.mOrigBase ?? '')
    if (hostChanged) { st.step = PF.P.key; await this.sendMenu(chatId, st, T.REKEY, [PF.CANCEL]); return }
    await this.doSave(chatId, st, { field: '地址', key: null })
  }


  private async onKeyInput(chatId: string, st: St, msg: TgMessage, key: string | null): Promise<void> {
    // 密钥步：任何消息都先删除（方案 3.3.4 第 3 条）
    const deleted = await this.deleteMsg(chatId, msg.message_id)
    if (!key) { await this.reply(chatId, T.KEY_BAD); return }
    void this.doSave(chatId, st, { field: st.mField ?? null, key, deleted })
  }

  private customBy(name: string): { name: string; api: ProviderApi; baseURL: string; keyEnv: string; models: { id: string; ctx: number; guessed: boolean }[]; manualIds: string[]; enabled: boolean } | null {
    const want = normName(name)
    return this.d.engine.customAll().find(c => normName(c.name) === want) ?? null
  }

  /** 保存（新建或修改）：锁外拉列表在 ProviderService 里发生，这里只管进入"处理中"、出结果、发菜单 */
  private async doSave(chatId: string, st: St, o: { field: '地址' | '密钥' | '接口格式' | null; key: string | null; deleted?: boolean }): Promise<void> {
    const name = st.name ?? ''
    const api = st.api ?? 'anthropic-messages'
    const baseURL = st.baseURL ?? ''
    let key = o.key
    if (!key) {
      // 只改地址：用现有密钥（值只在局部变量里，不进状态、不进日志）
      const entry = this.customBy(name)
      key = entry ? this.d.service.credential(entry.keyEnv) : null
    }
    st.busy = true
    st.busySince = st.busySince ?? this.now()
    st.op = 'save'
    st.savingName = name
    this.persist(chatId, st)
    let r: SaveResult
    try {
      r = key ? await this.d.service.save({ name, api, baseURL, key, mode: o.field ? 'modify' : 'create' }) : { status: 'gone', name }
    } catch (e) {
      this.d.log.error('wizard.failed', { chat: chatId, err: safeError(e) })
      this.finishOp(chatId, st)
      await this.reply(chatId, `【系统】引导出错了，已退出（${(e as Error)?.name ?? 'Error'}）。`)
      this.openProtection(chatId)
      return
    }
    await this.renderSaveResult(chatId, st, r, o)
  }

  private finishOp(chatId: string, st: St): void {
    st.busy = false
    delete st.busySince
    delete st.op
    delete st.savingName
    delete st.pendingKeyMessageId
    this.persist(chatId, st)
  }

  private async renderSaveResult(chatId: string, st: St, r: SaveResult, o: { field: '地址' | '密钥' | '接口格式' | null; deleted?: boolean }): Promise<void> {
    this.finishOp(chatId, st)
    const name = st.name ?? ''
    const note = o.deleted === false ? T.NOT_DELETED_NOTE : T.DELETED_NOTE
    // 失败类：没有按钮，正常结束（不再接走之后的消息）
    const failText = (): string | null => {
      switch (r.status) {
        case 'save_failed': return `【系统】没保存成功：${r.why}。密钥没有写入。`
        case 'cred_failed': return `【系统】供应商「${r.name}」已保存，但密钥没写进去（${r.why}）。请用 /provider →「修改」→「密钥」重试。`
        case 'lock_timeout': return '【系统】没保存成功：别的 bot 正在改供应商，请稍后再试。密钥没有写入。'
        case 'unreadable': return '【系统】没保存成功：共用供应商文件读不了（格式坏了）。密钥没有写入。'
        case 'gone': return o.field ? `【系统】「${name}」已经不在了，这次修改没保存。` : `【系统】「${name}」已经不在了，这次没保存。`
        default: return null
      }
    }
    if (r.status !== 'saved') {
      const t = failText()
      if (t) { await this.sendMenu(chatId, st, t, []); this.endNormal(chatId) }
      return
    }
    if (o.field) {
      const verb = `【系统】已更新「${r.name}」的${o.field}`
      let text: string
      if (r.reason === null) {
        text = `${verb}，拉到 ${r.count} 个模型。`
      } else {
        text = `${verb}，但没拉到模型：${fetchFailText({ reason: r.reason, status: r.status2, seconds: r.seconds })}。模型列表保持不变。`
      }
      if (o.field === '密钥') text += note
      if (o.field === '接口格式') text += `地址改成了 ${st.baseURL}`
      if (r.epochChanged) text += `\n${T.ROLL_NOTE}`
      st.step = PF.P.result
      await this.sendMenu(chatId, st, text, [PF.SWITCH_ONE_BTN, PF.CLOSE])
      return
    }
    // 新建
    const base = saveResultText(r, name)
    let text = `${base}${note}`
    if (st.strippedV1) text += '\n已去掉地址末尾的 /v1（Anthropic 格式会自动加）'
    st.step = PF.P.result
    const buttons: Btn[] = r.reason === null ? [PF.SWITCH_ONE_BTN, PF.CLOSE] : [PF.REFRESH_ONE_BTN, PF.CLOSE]
    await this.sendMenu(chatId, st, text, buttons)
  }

  // ─── /model：管理模型（文字答案） ───

  private async onAddIdInput(chatId: string, st: St, msg: TgMessage, body: string): Promise<void> {
    const id = cleanModelId(body)
    if (!id) { await this.stepError(chatId, st, msg, T.MODEL_NAME_BAD, T.modelNameHint, MF.addIdStep().buttons); return }
    const entry = this.customBy(st.manageName ?? '')
    if (!entry) { await this.editMenu(chatId, st, `【系统】「${st.manageName ?? ''}」已经不在了。`, []); return }
    if (entry.models.some(m => m.id === id)) { await this.editMenu(chatId, st, `【系统】「${entry.name}」里已经有 ${id} 了。请重新输入：`, MF.addIdStep().buttons); return }
    st.addId = id
    st.step = MF.M.addCtx
    await this.sendMenu(chatId, st, MF.addCtxStep().text, MF.addCtxStep().buttons)
  }

  private async onAddCtxInput(chatId: string, st: St, msg: TgMessage, body: string): Promise<void> {
    const ctx = parseCtxOrError(body)
    if (ctx === 'bad') { await this.stepError(chatId, st, msg, T.CONTEXT_BAD, T.ctxStepHint, MF.addCtxStep().buttons); return }
    await this.doAddModel(chatId, st, ctx)
  }

  private async actAddSkip(chatId: string, st: St): Promise<void> {
    await this.doAddModel(chatId, st, null)
  }

  private async doAddModel(chatId: string, st: St, ctx: number | null): Promise<void> {
    const name = st.manageName ?? ''
    const id = st.addId ?? ''
    st.busy = true; st.busySince = this.now(); st.op = 'modeledit'
    this.persist(chatId, st)
    const r = await this.d.service.addModel(name, id, ctx)
    this.finishOp(chatId, st)
    if (!r.ok) { await this.editMenu(chatId, st, T.modelEditError(r.why, name), []); st.step = MF.M.done; return }
    st.step = MF.M.done
    await this.editMenu(chatId, st, `【系统】已给「${name}」加上模型 ${id}（上下文 ${r.ctx ?? ctx ?? 0}）。几秒后就能切过去。`, [PF.BACK, PF.CLOSE])
  }

  private async onCtxInput(chatId: string, st: St, msg: TgMessage, body: string): Promise<void> {
    const ctx = parseCtxOrError(body)
    if (ctx === 'bad') { await this.stepError(chatId, st, msg, T.CONTEXT_BAD, T.ctxStepHint, MF.ctxInputStep(st.pendingModelId ?? '', 0, false).buttons); return }
    await this.doSetContext(chatId, st, st.pendingModelId ?? '', ctx)
  }

  private async doSetContext(chatId: string, st: St, id: string, ctx: number): Promise<void> {
    const name = st.manageName ?? ''
    st.busy = true; st.busySince = this.now(); st.op = 'modeledit'
    this.persist(chatId, st)
    const r = await this.d.service.setContext(name, id, ctx)
    this.finishOp(chatId, st)
    st.step = MF.M.done
    if (!r.ok) { await this.editMenu(chatId, st, T.modelEditError(r.why, r.why === 'gone' ? name : id), []); return }
    await this.editMenu(chatId, st, `【系统】已把 ${id} 的上下文长度改成 ${ctx}。几秒后生效。`, [PF.BACK, PF.CLOSE])
  }

  private async doRemoveModel(chatId: string, st: St, id: string): Promise<void> {
    const name = st.manageName ?? ''
    const manual = this.customBy(name)?.manualIds.includes(id) ?? false
    const before = this.d.engine.current() // 删掉正在用的模型会让覆盖失效，先记下删除前在用的
    st.busy = true; st.busySince = this.now(); st.op = 'modeledit'
    this.persist(chatId, st)
    const r = await this.d.service.removeModel(name, id)
    this.finishOp(chatId, st)
    st.step = MF.M.done
    if (!r.ok) { await this.editMenu(chatId, st, T.modelEditError(r.why, r.why === 'gone' ? name : id), []); return }
    let text = `【系统】已从「${name}」删掉模型 ${id}。`
    if (!manual) text += '下次「刷新模型」时，对方列表里还有的话它会重新出现。'
    const after = this.customBy(name)
    if (!after || after.models.length === 0) text += `「${name}」没有模型了，暂时不会出现在可选模型里。`
    if (before.provider === name && before.model === id) {
      const c = this.d.engine.clearOverride()
      text += `这个 bot 正在用它，已换回配置文件里的模型：${c.provider} / ${c.model}。`
    }
    await this.editMenu(chatId, st, text, [PF.BACK, PF.CLOSE])
  }

  // ─── 按钮回调（方案 3.3.3） ───

  onCallback(cq: TgCallbackQuery): void { void this.handleCallback(cq) }

  /** ProviderCommands 用（`/provider add` 打断引导，方案 3.3.4 第 1、2 条）：返回 true = 正处理中（只删、不处理 add） */
  endForAdd(chatId: string): boolean {
    const st = this.active.get(chatId)
    if (!st) return false
    if (st.busy) {
      this.d.log.info('wizard.input', { chat: chatId, step: 'busy', deleted: true, chars: 0 })
      void this.reply(chatId, T.BUSY_DELETED)
      return true
    }
    void this.stripButtons(chatId, st.menuMessageId)
    void this.reply(chatId, T.EXITED)
    this.openProtection(chatId)
    return false
  }

  private async handleCallback(cq: TgCallbackQuery): Promise<void> {
    const msg = cq.message
    const fromId = cq.from ? String(cq.from.id) : null
    if (!this.d.isOwner(fromId)) { await this.answer(cq); this.d.log.info('wizard.callback_rejected', { reason: 'not_owner' }); return }
    const isPrivate = !!msg && msg.chat.type === 'private'
    if (!msg || !isPrivate || msg.date === 0) { await this.answer(cq, T.STALE_TEXT); this.d.log.info('wizard.callback_rejected', { reason: 'inaccessible' }); return }
    const chatId = String(msg.chat.id)
    const cb = decodeCb(cq.data)
    if (!cb) { await this.answer(cq, T.STALE_TEXT); this.d.log.info('wizard.callback_rejected', { reason: 'malformed' }); return }
    const st = this.active.get(chatId)
    const actionOk = !!st && (ALLOW[st.step ?? ''] ?? []).includes(cb.action) && idxOk(st, cb.action, cb.idx)
    const stale = !st || st.menuMessageId !== msg.message_id || st.nonce !== cb.nonce || !actionOk
    if (stale) {
      await this.answer(cq, T.STALE_TEXT)
      this.d.log.info('wizard.callback_rejected', { reason: 'stale' })
      if (!st || st.menuMessageId !== msg.message_id) void this.stripButtons(chatId, msg.message_id)
      return
    }
    if (st!.busy) { await this.answer(cq, T.BUSY_TEXT); this.d.log.info('wizard.callback_rejected', { reason: 'busy' }); return }
    this.touch(st!)
    const ans = cb.action === 'cancel' ? '已退出引导'
      : (cb.action === 'refone' || (st!.step === PF.P.refreshPick && cb.action === 'pv')) ? '正在拉取模型列表…'
      : undefined
    await this.answer(cq, ans)
    this.d.log.info('wizard.step', { chat: chatId, flow: st!.flow ?? null, step: st!.step ?? null })
    try {
      await this.dispatch(chatId, st!, cb.action, cb.idx)
    } catch (e) {
      this.d.log.error('wizard.failed', { chat: chatId, err: safeError(e) })
      await this.reply(chatId, `【系统】引导出错了，已退出（${(e as Error)?.name ?? 'Error'}）。`)
      this.openProtection(chatId)
    }
  }

  private async dispatch(chatId: string, st: St, action: string, idx: number | null): Promise<void> {
    switch (action) {
      case 'cancel': return this.actCancel(chatId, st)
      case 'close': return this.actClose(chatId, st)
      case 'new': return this.actNew(chatId, st)
      case 'mod': return this.actModifyPick(chatId, st)
      case 'del': return this.actDeletePick(chatId, st)
      case 'ref': return this.actRefreshPick(chatId, st)
      case 'sw': case 'swmodel': return this.actSwitchPick(chatId, st)
      case 'swone': return this.actSwitchOne(chatId, st)
      case 'refone': return this.actRefreshOne(chatId, st)
      case 'pv': return this.actPick(chatId, st, idx ?? 0)
      case 'm': return this.actPickModel(chatId, st, idx ?? 0)
      case 'mgr': return this.actPickManage(chatId, st, idx ?? 0)
      case 'pg': return this.actPage(chatId, st, idx ?? 0)
      case 'back': return this.actBack(chatId, st)
      case 'cu': return this.actCollisionUpdate(chatId, st)
      case 'crn': return this.actCollisionRename(chatId, st)
      case 'fmt': return this.actFormat(chatId, st, idx ?? 0)
      case 'murl': case 'mkey': case 'mfmt': return this.actModifyField(chatId, st, action)
      case 'u1': return this.actUseV1(chatId, st)
      case 'dc': return this.actDeleteConfirm(chatId, st)
      case 'mgadd': return this.actManageAdd(chatId, st)
      case 'mgdel': return this.actManagePickModel(chatId, st, 'del')
      case 'mgctx': return this.actManagePickModel(chatId, st, 'ctx')
      case 'mg': return this.actManageStart(chatId, st)
      case 'skip': return this.actAddSkip(chatId, st)
      case 'eff': return this.actEffort(chatId, st, idx ?? 0)
      case 'rev': return this.actRevert(chatId, st)
      default: return
    }
  }

  private async actCancel(chatId: string, st: St): Promise<void> {
    this.d.log.info('wizard.cancelled', { chat: chatId, by: 'button' })
    await this.editMenu(chatId, st, T.CANCELLED_MENU, [])
    this.openProtection(chatId)
  }

  private async actClose(chatId: string, st: St): Promise<void> {
    await this.editMenu(chatId, st, T.CLOSED_MENU, [])
    this.endNormal(chatId)
  }

  private async actNew(chatId: string, st: St): Promise<void> {
    if (await this.unreadableGuard(chatId, st)) return
    st.flow = 'provider'
    st.mode = 'create'
    st.step = PF.P.name
    delete st.name; delete st.api; delete st.baseURL; delete st.strippedV1; delete st.mField; delete st.mOrigBase; delete st.keyEnv
    const r = PF.nameStep()
    await this.editMenu(chatId, st, r.text, r.buttons)
  }

  private enabledCustomNames(): string[] {
    return this.d.engine.customAll().filter(c => c.enabled).map(c => c.name)
  }

  private async unreadableGuard(chatId: string, st: St): Promise<boolean> {
    if (this.d.engine.providersReadable()) return false
    await this.sendMenu(chatId, st, `【系统】共用供应商文件读不了（格式坏了），先修好 ${providersPath(this.d.root)}`, [])
    this.endNormal(chatId)
    return true
  }

  private async actModifyPick(chatId: string, st: St): Promise<void> {
    if (await this.unreadableGuard(chatId, st)) return
    const names = this.enabledCustomNames()
    st.flow = 'provider'
    st.list = names
    st.page = 0
    st.step = PF.P.modifyPick
    const r = PF.modifyPickStep(names, 0)
    await this.editMenu(chatId, st, r.text, r.buttons)
  }

  private async actDeletePick(chatId: string, st: St): Promise<void> {
    if (await this.unreadableGuard(chatId, st)) return
    const names = this.enabledCustomNames()
    st.flow = 'provider'
    st.list = names
    st.page = 0
    st.step = PF.P.deletePick
    const r = PF.deletePickStep(names, 0)
    await this.editMenu(chatId, st, r.text, r.buttons)
  }

  private async actRefreshPick(chatId: string, st: St): Promise<void> {
    if (await this.unreadableGuard(chatId, st)) return
    const names = this.enabledCustomNames()
    st.flow = 'provider'
    st.list = names
    st.page = 0
    st.step = PF.P.refreshPick
    if (names.length === 0) { const r = PF.deletePickStep([], 0); await this.editMenu(chatId, st, r.text, r.buttons); return }
    const r = PF.refreshPickStep(names, 0)
    await this.editMenu(chatId, st, r.text, r.buttons)
  }

  private async actSwitchPick(chatId: string, st: St): Promise<void> {
    const list = await this.switchList()
    const names = list.map(p => p.name)
    st.flow = st.flow === 'model' ? 'model' : 'provider'
    st.list = names
    st.page = 0
    st.step = PF.P.switchPick
    const r = PF.switchPickStep(names, this.d.engine.current().provider, 0)
    await this.editMenu(chatId, st, r.text, r.buttons)
  }

  private async itemGone(chatId: string, st: St): Promise<void> {
    await this.reply(chatId, `【系统】${T.ITEM_GONE}。`)
    await this.rerenderPick(chatId, st)
  }

  private async rerenderPick(chatId: string, st: St): Promise<void> {
    switch (st.step) {
      case PF.P.modifyPick: { const names = this.enabledCustomNames(); st.list = names; st.page = 0; const r = PF.modifyPickStep(names, 0); await this.editMenu(chatId, st, r.text, r.buttons); return }
      case PF.P.deletePick: { const names = this.enabledCustomNames(); st.list = names; st.page = 0; const r = PF.deletePickStep(names, 0); await this.editMenu(chatId, st, r.text, r.buttons); return }
      case PF.P.refreshPick: { const names = this.enabledCustomNames(); st.list = names; st.page = 0; const r = PF.refreshPickStep(names, 0); await this.editMenu(chatId, st, r.text, r.buttons); return }
      case PF.P.switchPick: return this.rerenderSwitchPick(chatId, st)
      case MF.M.managePick: { const names = this.enabledCustomNames(); st.list = names; st.page = 0; const r = MF.managePickStep(names, 0); await this.editMenu(chatId, st, r.text, r.buttons); return }
      case MF.M.delPick: case MF.M.ctxPick: return this.actManagePickModel(chatId, st, st.step === MF.M.delPick ? 'del' : 'ctx')
      default: return
    }
  }

  private async rerenderSwitchPick(chatId: string, st: St): Promise<void> {
    const list = await this.switchList()
    const names = list.map(p => p.name)
    st.list = names; st.page = 0; st.step = PF.P.switchPick
    const r = PF.switchPickStep(names, this.d.engine.current().provider, 0)
    await this.editMenu(chatId, st, r.text, r.buttons)
  }

  private async actPick(chatId: string, st: St, idx: number): Promise<void> {
    const name = st.list?.[idx]
    if (!name) return
    if (st.step === PF.P.modifyPick) {
      const e = this.customBy(name)
      if (!e || !e.enabled) return this.itemGone(chatId, st)
      st.name = e.name; st.api = e.api; st.baseURL = e.baseURL; st.mOrigBase = e.baseURL; st.keyEnv = e.keyEnv; st.mode = 'modify'; st.step = PF.P.modifyWhat
      const r = PF.modifyWhatStep(e.name, e.api, e.baseURL)
      await this.editMenu(chatId, st, r.text, r.buttons)
      return
    }
    if (st.step === PF.P.deletePick) {
      const e = this.customBy(name)
      if (!e || !e.enabled) return this.itemGone(chatId, st)
      st.name = e.name; st.step = PF.P.deleteConfirm
      const inUse = this.d.engine.current().provider === e.name ? `${this.d.engine.config().provider} / ${this.d.engine.config().model}` : null
      const r = PF.deleteConfirmStep(e.name, inUse)
      await this.editMenu(chatId, st, r.text, r.buttons)
      return
    }
    if (st.step === PF.P.refreshPick) {
      const e = this.customBy(name)
      if (!e || !e.enabled) return this.itemGone(chatId, st)
      await this.doRefresh(chatId, st, e.name)
      return
    }
    if (st.step === PF.P.switchPick) {
      const list = await this.switchList()
      const p = list.find(x => x.name === name)
      if (!p) { await this.reply(chatId, `【系统】「${name}」已经不在了，请重新选。`); return this.rerenderSwitchPick(chatId, st) }
      st.name = p.name; st.list = p.models; st.page = 0; st.step = PF.P.switchModels
      const r = PF.switchModelsStep(p.name, p.models, this.currentModelFor(p.name), 0)
      await this.editMenu(chatId, st, r.text, r.buttons)
    }
  }

  private async actPickManage(chatId: string, st: St, idx: number): Promise<void> {
    const name = st.list?.[idx]
    if (!name) return
    const e = this.customBy(name)
    if (!e || !e.enabled) return this.itemGone(chatId, st)
    st.manageName = e.name
    await this.showManage(chatId, st)
  }

  private async actPickModel(chatId: string, st: St, idx: number): Promise<void> {
    const id = st.list?.[idx]
    if (!id) return
    if (st.step === PF.P.switchModels) {
      const list = await this.switchList()
      const p = list.find(x => x.name === st.name)
      if (!p) {
        // 整个供应商已不在：回到第 1 步，菜单编辑为结果文案
        const names = list.map(x => x.name)
        st.list = names; st.page = 0; st.step = PF.P.switchPick
        const r = PF.switchPickStep(names, this.d.engine.current().provider, 0)
        await this.editMenu(chatId, st, `【系统】「${st.name ?? ''}」已经不在了，请重新选。`, r.buttons)
        return
      }
      if (!p.models.includes(id)) {
        st.list = p.models
        const r = PF.switchModelsStep(p.name, p.models, this.currentModelFor(p.name), st.page ?? 0)
        await this.editMenu(chatId, st, '【系统】这个模型已经不在列表里了，请重新选。', r.buttons)
        return
      }
      const text = this.d.engine.switchTo({ provider: p.name, model: id })
      await this.editMenu(chatId, st, text, [])
      this.endNormal(chatId)
      return
    }
    const e = this.customBy(st.manageName ?? '')
    if (!e || !e.models.some(m => m.id === id)) { await this.reply(chatId, `【系统】「${id}」已经不在了。`); return this.rerenderPick(chatId, st) }
    if (st.step === MF.M.delPick) { await this.doRemoveModel(chatId, st, id); return }
    if (st.step === MF.M.ctxPick) {
      const m = e.models.find(x => x.id === id)!
      st.pendingModelId = id; st.step = MF.M.ctxInput
      const r = MF.ctxInputStep(id, m.ctx, m.guessed)
      await this.editMenu(chatId, st, r.text, r.buttons)
    }
  }

  private async actPage(chatId: string, st: St, page: number): Promise<void> {
    st.page = page
    switch (st.step) {
      case PF.P.modifyPick: { const r = PF.modifyPickStep(st.list ?? [], page); await this.editMenu(chatId, st, r.text, r.buttons); return }
      case PF.P.deletePick: { const r = PF.deletePickStep(st.list ?? [], page); await this.editMenu(chatId, st, r.text, r.buttons); return }
      case PF.P.refreshPick: { const r = PF.refreshPickStep(st.list ?? [], page); await this.editMenu(chatId, st, r.text, r.buttons); return }
      case PF.P.switchPick: { const r = PF.switchPickStep(st.list ?? [], this.d.engine.current().provider, page); await this.editMenu(chatId, st, r.text, r.buttons); return }
      case PF.P.switchModels: { const r = PF.switchModelsStep(st.name ?? '', st.list ?? [], this.currentModelFor(st.name ?? ''), page); await this.editMenu(chatId, st, r.text, r.buttons); return }
      case MF.M.managePick: { const r = MF.managePickStep(st.list ?? [], page); await this.editMenu(chatId, st, r.text, r.buttons); return }
      case MF.M.delPick: case MF.M.ctxPick: { const r = MF.modelPickStep(st.step === MF.M.delPick ? 'del' : 'ctx', st.list ?? [], page); await this.editMenu(chatId, st, r.text, r.buttons); return }
      default: return
    }
  }

  private async actBack(chatId: string, st: St): Promise<void> {
    switch (st.step) {
      case PF.P.switchModels: return this.rerenderSwitchPick(chatId, st)
      case MF.M.effort: case MF.M.effortResult: case MF.M.manage: return this.showModelMainEdit(chatId, st)
      case MF.M.delPick: case MF.M.ctxPick: case MF.M.done: return this.showManage(chatId, st)
      default: return
    }
  }

  private async showModelMainEdit(chatId: string, st: St): Promise<void> {
    st.step = MF.M.main
    st.flow = 'model'
    await this.editMenu(chatId, st, this.d.engine.modelStatusText(chatId), MF.MAIN_BUTTONS)
  }

  private async showManage(chatId: string, st: St): Promise<void> {
    const e = this.customBy(st.manageName ?? '')
    if (!e) { await this.reply(chatId, `【系统】「${st.manageName ?? ''}」已经不在了。`); return this.actManageStart(chatId, st) }
    st.step = MF.M.manage
    const r = MF.manageStep(e.name, e.models, 0)
    await this.editMenu(chatId, st, r.text, r.buttons)
  }

  private async actManageStart(chatId: string, st: St): Promise<void> {
    const names = this.enabledCustomNames()
    st.flow = 'model'; st.list = names; st.page = 0; st.step = MF.M.managePick
    const r = MF.managePickStep(names, 0)
    await this.editMenu(chatId, st, r.text, r.buttons)
  }

  private async actManageAdd(chatId: string, st: St): Promise<void> {
    st.step = MF.M.addId
    const r = MF.addIdStep()
    await this.editMenu(chatId, st, r.text, r.buttons)
  }

  private async actManagePickModel(chatId: string, st: St, kind: 'del' | 'ctx'): Promise<void> {
    const e = this.customBy(st.manageName ?? '')
    if (!e) { await this.reply(chatId, `【系统】「${st.manageName ?? ''}」已经不在了。`); return this.actManageStart(chatId, st) }
    const models = e.models.map(m => m.id)
    st.list = models; st.page = 0; st.step = kind === 'del' ? MF.M.delPick : MF.M.ctxPick
    const r = MF.modelPickStep(kind, models, 0)
    await this.editMenu(chatId, st, r.text, r.buttons)
  }

  private async actEffort(chatId: string, st: St, idx: number): Promise<void> {
    if (st.step === MF.M.main) {
      const options = await this.d.engine.efforts()
      const cur = this.d.engine.current()
      st.flow = 'model'; st.list = options
      st.step = options.length === 0 ? MF.M.effortResult : MF.M.effort
      const r = MF.effortStep(options, cur.effort, { provider: cur.provider, model: cur.model, custom: this.customBy(cur.provider) !== null })
      await this.editMenu(chatId, st, r.text, r.buttons)
      return
    }
    const options = st.list ?? []
    const value = options[idx]
    if (!value) return
    const cur = this.d.engine.current()
    st.step = MF.M.effortResult
    if (value === cur.effort) { await this.editMenu(chatId, st, `【系统】现在就是「${T.effortLabel(value)}」。`, [PF.BACK, PF.CLOSE]); return }
    const res = await this.d.engine.setEffort(value)
    if (!res.ok) {
      st.step = MF.M.effort
      await this.reply(chatId, `【系统】这个模型现在不支持「${T.effortLabel(value)}」了，请重新选。`)
      const r = MF.effortStep(options, cur.effort, { provider: cur.provider, model: cur.model, custom: this.customBy(cur.provider) !== null })
      await this.editMenu(chatId, st, r.text, r.buttons)
      return
    }
    await this.editMenu(chatId, st, `【系统】思考强度已改成「${T.effortLabel(value)}」，下一条消息起生效（不换新会话）。这个 bot 的所有聊天都改，重启后保持。`, [PF.BACK, PF.CLOSE])
  }

  private async actRevert(chatId: string, st: St): Promise<void> {
    const text = this.d.engine.revert()
    await this.editMenu(chatId, st, text, [])
    this.endNormal(chatId)
  }

  private async actCollisionUpdate(chatId: string, st: St): Promise<void> {
    st.mode = 'create'; st.step = PF.P.format
    const r = PF.formatStep(st.name ?? '')
    await this.editMenu(chatId, st, r.text, r.buttons)
  }

  private async actCollisionRename(chatId: string, st: St): Promise<void> {
    delete st.name; st.step = PF.P.name
    const r = PF.nameStep()
    await this.editMenu(chatId, st, r.text, r.buttons)
  }

  private async actFormat(chatId: string, st: St, idx: number): Promise<void> {
    if (st.step === PF.P.format) {
      st.api = idx === 0 ? 'anthropic-messages' : 'openai-completions'
      st.step = PF.P.url
      const r = PF.urlStep(T.CREATE_STEP3)
      await this.editMenu(chatId, st, r.text, r.buttons)
      return
    }
    const newApi: ProviderApi = idx === 0 ? 'anthropic-messages' : 'openai-completions'
    if (newApi === st.api) {
      st.step = PF.P.modifyWhat
      const r = PF.modifyWhatStep(st.name ?? '', st.api ?? 'openai-completions', st.baseURL ?? '', '【系统】格式没变。\n')
      await this.editMenu(chatId, st, r.text, r.buttons)
      return
    }
    if (newApi === 'anthropic-messages') {
      const res = checkBaseURL(st.baseURL ?? '', 'anthropic-messages')
      st.api = newApi
      if (res.ok) st.baseURL = res.url
      st.mField = '接口格式'; st.step = PF.P.key
      await this.editMenu(chatId, st, T.REKEY, [PF.CANCEL])
      return
    }
    st.mFromApi = st.api; st.api = newApi; st.mField = '接口格式'; st.step = PF.P.modifyOpenAIAsk
    const r = PF.modifyOpenAIAsk(st.baseURL ?? '')
    await this.editMenu(chatId, st, r.text, r.buttons)
  }

  private async actModifyField(chatId: string, st: St, action: string): Promise<void> {
    const e = this.customBy(st.name ?? '')
    if (!e) { await this.reply(chatId, `【系统】「${st.name ?? ''}」已经不在了。`); return this.actModifyPick(chatId, st) }
    st.api = e.api; st.baseURL = e.baseURL; st.mOrigBase = e.baseURL; st.keyEnv = e.keyEnv; st.mode = 'modify'
    if (action === 'murl') {
      st.mField = '地址'; st.step = PF.P.url
      const r = PF.urlStep(T.MODIFY_URL_STEP)
      await this.editMenu(chatId, st, r.text, r.buttons)
      return
    }
    if (action === 'mkey') {
      st.mField = '密钥'; st.step = PF.P.key
      const r = PF.keyStep(T.MODIFY_KEY_STEP)
      await this.editMenu(chatId, st, r.text, r.buttons)
      return
    }
    st.mField = '接口格式'; st.step = PF.P.modifyFormat
    const r = PF.modifyFormatStep(e.name, e.api)
    await this.editMenu(chatId, st, r.text, r.buttons)
  }

  private async actUseV1(chatId: string, st: St): Promise<void> {
    st.baseURL = `${st.baseURL ?? ''}/v1`
    st.mField = '接口格式'; st.step = PF.P.key
    await this.editMenu(chatId, st, T.REKEY, [PF.CANCEL])
  }

  private async actDeleteConfirm(chatId: string, st: St): Promise<void> {
    const e = this.customBy(st.name ?? '')
    if (!e) { await this.editMenu(chatId, st, `【系统】「${st.name ?? ''}」已经不在了。`, []); this.endNormal(chatId); return }
    const wasInUse = this.d.engine.current().provider === e.name
    st.busy = true; st.busySince = this.now(); st.op = 'delete'
    this.persist(chatId, st)
    const r = await this.d.service.remove(e.name)
    this.finishOp(chatId, st)
    if (r.status !== 'ok') { await this.editMenu(chatId, st, T.deleteFailText(r), []); this.endNormal(chatId); return }
    let text = `【系统】已删除供应商「${e.name}」。它的密钥会在 ${Math.round(r.graceMs / 60_000)} 分钟后从凭据文件删除（让正在用它的 bot 先切走）。注意：这只是从本机删掉，不会在供应商那边作废这把密钥；如果担心泄露，请到供应商后台作废它。`
    if (wasInUse) { const c = this.d.engine.clearOverride(); text += `这个 bot 已换回配置文件里的模型：${c.provider} / ${c.model}。` }
    await this.editMenu(chatId, st, text, [])
    this.endNormal(chatId)
  }

  private async actSwitchOne(chatId: string, st: St): Promise<void> {
    const e = this.customBy(st.name ?? '')
    if (!e) { await this.reply(chatId, `【系统】「${st.name ?? ''}」已经不在了。`); return this.actSwitchPick(chatId, st) }
    if (e.models.length === 0) { await this.reply(chatId, `【系统】「${e.name}」还没有模型。`); return }
    st.list = e.models.map(m => m.id); st.page = 0; st.step = PF.P.switchModels
    const r = PF.switchModelsStep(e.name, st.list, this.currentModelFor(e.name), 0)
    await this.editMenu(chatId, st, r.text, r.buttons)
  }

  private async actRefreshOne(chatId: string, st: St): Promise<void> {
    const e = this.customBy(st.name ?? '')
    if (!e) { await this.reply(chatId, `【系统】「${st.name ?? ''}」已经不在了。`); return }
    await this.doRefresh(chatId, st, e.name)
  }

  private async doRefresh(chatId: string, st: St, name: string): Promise<void> {
    const e = this.customBy(name)
    if (!e) { await this.reply(chatId, `【系统】「${name}」已经不在了。`); return }
    if (!this.d.service.credential(e.keyEnv)) {
      await this.editMenu(chatId, st, `【系统】「${e.name}」缺密钥，先用「修改」→「密钥」补上。`, [])
      this.endNormal(chatId)
      return
    }
    st.name = e.name; st.busy = true; st.busySince = this.now(); st.op = 'refresh'
    const before = this.d.engine.current() // 刷新会让"被去掉的模型"覆盖失效，先记下刷新前在用的
    await this.editMenu(chatId, st, `【系统】正在拉取「${e.name}」的模型列表…`, [])
    const r = await this.d.service.refresh(e.name)
    this.finishOp(chatId, st)
    if (r.status === 'ok') {
      let text = refreshResultText(this.d.root, e.name, r)
      if (before.provider === e.name && r.removed.includes(before.model)) {
        this.d.engine.clearOverride()
        text += `这个 bot 正在用的 ${before.model} 不在新列表里，已换回配置文件里的模型。`
      }
      st.step = PF.P.result
      await this.editMenu(chatId, st, text, [PF.SWITCH_BTN, PF.CLOSE])
      return
    }
    await this.editMenu(chatId, st, refreshResultText(this.d.root, e.name, r), [])
    this.endNormal(chatId)
  }

  private currentModelFor(provider: string): string {
    const c = this.d.engine.current()
    return c.provider === provider ? c.model : ''
  }

  private async switchList(): Promise<{ name: string; models: string[] }[]> {
    let choices: ModelChoice[] = []
    try { choices = await this.d.engine.choices() } catch { choices = [] }
    const out: { name: string; models: string[] }[] = []
    out.push({ name: 'deepseek-official', models: choices.filter(c => c.provider === 'deepseek-official').map(c => c.model) })
    for (const r of this.d.engine.configRoutes()) out.push({ name: r, models: choices.filter(c => c.provider === r).map(c => c.model) })
    for (const c of this.d.engine.customAll()) if (c.enabled && c.models.length > 0) out.push({ name: c.name, models: c.models.map(m => m.id) })
    return out.filter(p => p.models.length > 0)
  }

  /** 超时清扫（方案 3.3.5）：处理中上限、引导超时、防护窗口过期 */
  private async sweep(): Promise<void> {
    if (this.stopped) return
    const now = this.now()
    for (const [chatId, until] of [...this.protection]) {
      if (until <= now) { this.protection.delete(chatId); this.d.ledger.deleteMeta(metaKey(chatId)) }
    }
    const maxOp = 3 * this.d.modelFetchTimeoutMs + this.d.lockWaitMs + 30_000
    for (const [chatId, st] of [...this.active]) {
      if (st.busy) {
        if (st.busySince !== undefined && now - st.busySince > maxOp) {
          this.d.log.warn('wizard.failed', { chat: chatId, err: 'processing timeout' })
          await this.reply(chatId, '【系统】引导出错了，已退出（处理超时）。')
          this.openProtection(chatId)
        }
        continue
      }
      if (st.expiresAt && st.expiresAt <= now) {
        this.d.log.info('wizard.timeout', { chat: chatId, step: st.step ?? null })
        const minutes = Math.max(1, Math.round((now - (st.expiresAt - this.d.timeoutMs)) / 60_000))
        if (st.menuMessageId) { try { await this.d.api.editMessageText(chatId, st.menuMessageId, `${st.menuText ?? ''}（已过期）`) } catch {} }
        await this.reply(chatId, `【系统】已退出引导（${minutes} 分钟没有操作）。之后的消息照常和角色聊天。`)
        this.openProtection(chatId)
      }
    }
  }
}

// ─── 步骤的允许动作（方案 3.3.3：动作不属于当前步 → stale） ───
const ALLOW: Record<string, string[]> = {
  [PF.P.main]: ['new', 'mod', 'del', 'ref', 'sw', 'close'],
  [PF.P.name]: ['cancel'],
  [PF.P.collision]: ['cu', 'crn', 'cancel'],
  [PF.P.format]: ['fmt', 'cancel'],
  [PF.P.url]: ['cancel'],
  [PF.P.key]: ['cancel'],
  [PF.P.modifyPick]: ['pv', 'pg', 'cancel', 'new'],
  [PF.P.modifyWhat]: ['murl', 'mkey', 'mfmt', 'cancel'],
  [PF.P.modifyFormat]: ['fmt', 'cancel'],
  [PF.P.modifyOpenAIAsk]: ['u1', 'cancel'],
  [PF.P.deletePick]: ['pv', 'pg', 'cancel', 'new'],
  [PF.P.deleteConfirm]: ['dc', 'cancel'],
  [PF.P.refreshPick]: ['pv', 'pg', 'cancel', 'new'],
  [PF.P.switchPick]: ['pv', 'pg', 'cancel'],
  [PF.P.switchModels]: ['m', 'back', 'cancel', 'pg'],
  [PF.P.result]: ['swone', 'refone', 'close'],
  [MF.M.main]: ['swmodel', 'eff', 'mg', 'rev', 'close'],
  [MF.M.effort]: ['eff', 'back', 'cancel', 'close'],
  [MF.M.effortResult]: ['back', 'close'],
  [MF.M.managePick]: ['mgr', 'pg', 'cancel', 'new'],
  [MF.M.manage]: ['mgadd', 'mgdel', 'mgctx', 'back', 'cancel'],
  [MF.M.addId]: ['cancel'],
  [MF.M.addCtx]: ['skip', 'cancel'],
  [MF.M.delPick]: ['m', 'back', 'cancel', 'pg'],
  [MF.M.ctxPick]: ['m', 'back', 'cancel', 'pg'],
  [MF.M.ctxInput]: ['cancel'],
  [MF.M.done]: ['back', 'close'],
}

function idxOk(st: St, action: string, idx: number | null): boolean {
  if (action === 'fmt') return idx === 0 || idx === 1
  if (action === 'pg') return true
  if (action === 'eff' && st.step === MF.M.main) return true // 打开思考强度页，没有序号
  if (action === 'pv' || action === 'm' || action === 'mgr' || action === 'eff') return idx !== null && idx >= 0 && idx < (st.list?.length ?? 0)
  return true
}

const isCancel = (s: string) => /^\/cancel(@\S+)?\s*$/i.test(s)
const isProviderNoArg = (s: string) => /^\/provider(@\S+)?\s*$/i.test(s)
const isModelNoArg = (s: string) => /^\/model(@\S+)?\s*$/i.test(s)
const isModelWithArgs = (s: string) => /^\/model(@\S+)?\s+\S+/i.test(s)
const isProviderWithArgs = (s: string) => /^\/provider(@\S+)?\s+\S+/i.test(s)
const isKeyStep = (st: St) => st.step === PF.P.key
function parseCtxOrError(s: string): number | 'bad' { const v = T.parseContext(s); return v === null ? 'bad' : v }
