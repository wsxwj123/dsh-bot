// 假 Telegram：本机 HTTP 服务，实现网关用到的那几个 Bot API 方法，能按剧本制造故障，并记录收到的每一次发送。
// 按钮引导（INTERFACE 3.13.1）另加：按钮回调（callback_query）、编辑消息、删除消息、回应按钮，
// 以及真实 Bot API 的硬性限制与原样报错文案（消息超长、按钮数据超长、没改动、找不到、删不掉、回调过期）。
export type Button = { text: string; data: string }

export type Sent = {
  method: string
  chatId: string
  text?: string
  replyTo?: number
  file?: string
  messageId: number
  at: number
  /** 发送时带的 reply_markup（解析成对象；没带就没有这个字段） */
  replyMarkup?: any
}

export type Edit = { method: string; chatId: string; messageId: number; text?: string; replyMarkup: any; at: number }
export type Answer = { id: string; text?: string; showAlert?: boolean; at: number }
export type Deleted = { chatId: string; messageId: number; at: number }
export type Menu = { messageId: number; text: string; buttons: Button[] }

export type Fault = {
  method: string
  /** 只对满足条件的请求生效 */
  when?: (params: Record<string, any>) => boolean
  /** 生效几次（默认 1） */
  times?: number
  status?: number
  description?: string
  retryAfter?: number
  /** 'garbled'：先当作已投递记下来，再回一个读不出来的响应（模拟"发出去了但回包丢了"）；'slow'：等 delayMs 再正常处理（模拟大文件上传慢） */
  mode?: 'error' | 'garbled' | 'slow'
  delayMs?: number
}

type Update = { update_id: number; message?: any; callback_query?: any }
type Stored = { chatId: string; messageId: number; fromBot: boolean; text: string; markup: any; buttons: Button[]; date: number; deleted: boolean; tooOld: boolean; touched: number }

const key = (chatId: number | string, messageId: number) => `${chatId}:${messageId}`

export class FakeTelegram {
  readonly token = '123456789:AAFakeTokenForTestsOnly_abcdefghijklmnop'
  readonly bot: { id: number; is_bot: true; first_name: string; username: string }
  server: ReturnType<typeof Bun.serve>
  sent: Sent[] = []
  calls: { method: string; at: number; params: Record<string, any> }[] = []
  reactions: { chatId: string; messageId: number; emoji: string | null }[] = []
  faults: Fault[] = []
  /** 成功的 editMessageText / editMessageReplyMarkup */
  edits: Edit[] = []
  /** 成功删除的消息 */
  deleted: Deleted[] = []
  /** 成功的 answerCallbackQuery */
  answers: Answer[] = []
  /** 最近一次 getUpdates 声明的 allowed_updates；从没声明过 = null（真实 Telegram 会记住上一次的声明） */
  lastAllowedUpdates: string[] | null = null
  private updates: Update[] = []
  private nextUpdate = 1
  private nextMsg = 1000
  private nextCallback = 1
  private touch = 0
  private waiters: (() => void)[] = []
  private msgs = new Map<string, Stored>()
  private callbacks = new Map<string, { answered: boolean }>()
  private notAdmin = new Set<string>()

  /** 多个 bot（群聊测试）：每个假 Telegram 当一个不同的 bot */
  constructor(o: { botId?: number; username?: string } = {}) {
    this.bot = { id: o.botId ?? 900001, is_bot: true, first_name: o.username ?? 'TestBot', username: o.username ?? 'test_dsh_bot' }
    this.server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch: req => this.handle(req) })
  }

  get url(): string { return `http://127.0.0.1:${this.server.port}` }

  /** 最近一次造出的更新的 update_id（还没造过 = 0） */
  get lastUpdateId(): number { return this.nextUpdate - 1 }

  stop(): void { this.server.stop(true) }

  /** 用户发来一条私聊文字 */
  pushText(fromId: number, text: string, o: { chatId?: number; replyTo?: { message_id: number; text?: string; fromBot?: boolean }; firstName?: string; messageId?: number } = {}): number {
    // messageId：同一条群消息投给多个 bot 时，消息编号要相同
    const mid = o.messageId ?? this.nextMsg++
    if (o.messageId) this.nextMsg = Math.max(this.nextMsg, o.messageId + 1)
    const chatId = o.chatId ?? fromId
    const message: any = {
      message_id: mid,
      date: Math.floor(Date.now() / 1000),
      chat: { id: chatId, type: chatId === fromId ? 'private' : 'group' },
      from: { id: fromId, is_bot: false, first_name: o.firstName ?? `user${fromId}` },
      text,
    }
    if (text.startsWith('/')) message.entities = [{ type: 'bot_command', offset: 0, length: text.split(/\s/)[0]!.length }]
    if (o.replyTo) message.reply_to_message = { message_id: o.replyTo.message_id, date: message.date, chat: message.chat, text: o.replyTo.text ?? '', from: o.replyTo.fromBot ? this.bot : message.from }
    this.remember(chatId, mid, false, text, null, message.date)
    this.updates.push({ update_id: this.nextUpdate++, message })
    for (const w of this.waiters.splice(0)) w()
    return mid
  }

  /** 用户发来一张图片（可带说明文字；chatId 不写 = 私聊） */
  pushPhoto(fromId: number, caption?: string, o: { chatId?: number } = {}): number {
    return this.pushMedia(fromId, { photo: [{ file_id: 'photo-small', width: 90, height: 90 }, { file_id: 'photo-big', width: 800, height: 600 }], ...(caption ? { caption } : {}) }, o.chatId)
  }

  /** 用户发来一条语音 */
  pushVoice(fromId: number): number {
    return this.pushMedia(fromId, { voice: { file_id: 'voice-1', duration: 3, mime_type: 'audio/ogg' } })
  }

  /** 用户发来一张贴纸 */
  pushSticker(fromId: number, o: { chatId?: number } = {}): number {
    return this.pushMedia(fromId, { sticker: { file_id: 'sticker-1', width: 512, height: 512, type: 'regular', is_animated: false, is_video: false, emoji: '😀' } }, o.chatId)
  }

  /** 用户发来一个文件（可带说明文字） */
  pushDocument(fromId: number, caption?: string, o: { chatId?: number } = {}): number {
    return this.pushMedia(fromId, { document: { file_id: 'doc-1', file_name: 'a.txt', mime_type: 'text/plain', file_size: 12 }, ...(caption ? { caption } : {}) }, o.chatId)
  }

  private pushMedia(fromId: number, extra: Record<string, any>, chatId0?: number): number {
    const mid = this.nextMsg++
    const chatId = chatId0 ?? fromId
    const message = { message_id: mid, date: Math.floor(Date.now() / 1000), chat: { id: chatId, type: chatId === fromId ? 'private' : 'group' }, from: { id: fromId, is_bot: false, first_name: `user${fromId}` }, ...extra }
    this.remember(chatId, mid, false, String(extra.caption ?? ''), null, message.date)
    this.updates.push({ update_id: this.nextUpdate++, message })
    for (const w of this.waiters.splice(0)) w()
    return mid
  }

  /** 造一次按钮点击；inaccessible = 原消息已不可访问（Telegram 给的 message.date 为 0）。返回回调 id */
  pushCallback(fromId: number, chatId: number, messageId: number, data: string, o: { inaccessible?: boolean } = {}): string {
    const id = `cbq-${this.nextCallback++}`
    this.callbacks.set(id, { answered: false })
    const m = this.msgs.get(key(chatId, messageId))
    const chat = { id: chatId, type: chatId === fromId ? 'private' : 'group' }
    const message = o.inaccessible
      ? { chat, message_id: messageId, date: 0 }
      : { message_id: messageId, date: m?.date ?? Math.floor(Date.now() / 1000), chat, from: this.bot, text: m?.text ?? '', ...(m && m.buttons.length ? { reply_markup: m.markup } : {}) }
    this.updates.push({ update_id: this.nextUpdate++, callback_query: { id, from: { id: fromId, is_bot: false, first_name: `user${fromId}` }, message, chat_instance: `ci-${chatId}`, data } })
    for (const w of this.waiters.splice(0)) w()
    return id
  }

  /** 按按钮上的字点一下（这条消息上没有这个按钮就抛错）。返回回调 id */
  clickButton(fromId: number, chatId: number, messageId: number, label: string): string {
    const bs = this.buttonsOf(chatId, messageId)
    const b = bs.find(x => x.text === label)
    if (!b) throw new Error(`消息 ${messageId} 上没有「${label}」按钮（现有：${bs.map(x => x.text).join('、') || '无'}）`)
    return this.pushCallback(fromId, chatId, messageId, b.data)
  }

  /** 某条 bot 消息现在的按钮（扁平；没有或已删 = []） */
  buttonsOf(chatId: number | string, messageId: number): Button[] {
    const m = this.msgs.get(key(chatId, messageId))
    return m && !m.deleted ? m.buttons : []
  }

  /** 这个聊天里最近一次发出或编辑过、现在仍带按钮的 bot 消息 */
  lastMenu(chatId: number | string): Menu | null {
    let best: Stored | null = null
    for (const m of this.msgs.values()) {
      if (m.chatId === String(chatId) && m.fromBot && !m.deleted && m.buttons.length && (!best || m.touched > best.touched)) best = m
    }
    return best ? { messageId: best.messageId, text: best.text, buttons: best.buttons } : null
  }

  /** 某条消息（bot 的或用户的）现在的文字；不存在 = undefined */
  textOf(chatId: number | string, messageId: number): string | undefined {
    return this.msgs.get(key(chatId, messageId))?.text
  }

  isDeleted(chatId: number | string, messageId: number): boolean {
    return this.msgs.get(key(chatId, messageId))?.deleted === true
  }

  /** 把某条消息标成"太旧"：之后删它会得到 message can't be deleted for everyone */
  markTooOld(chatId: number | string, messageId: number): void {
    const m = this.msgs.get(key(chatId, messageId))
    if (m) m.tooOld = true
  }

  /** 让 bot 在这个群里不是管理员：删别人的消息会得到 message can't be deleted */
  setNotAdmin(chatId: number | string): void { this.notAdmin.add(String(chatId)) }

  sentTo(chatId: number | string): Sent[] {
    return this.sent.filter(s => s.chatId === String(chatId))
  }

  private remember(chatId: number | string, messageId: number, fromBot: boolean, text: string, markup: any, date: number): void {
    const k = key(chatId, messageId)
    this.msgs.set(k, { chatId: String(chatId), messageId, fromBot, text, markup, buttons: flatten(markup), date, deleted: false, tooOld: this.msgs.get(k)?.tooOld ?? false, touched: ++this.touch })
  }
  private takeFault(method: string, params: Record<string, any>): Fault | null {
    const i = this.faults.findIndex(f => f.method === method && (!f.when || f.when(params)))
    if (i < 0) return null
    const f = this.faults[i]!
    f.times = (f.times ?? 1) - 1
    if (f.times <= 0) this.faults.splice(i, 1)
    return f
  }

  private async handle(req: Request): Promise<Response> {
    const url = new URL(req.url)
    // 文件下载：/file/bot<令牌>/<路径>
    const fm = url.pathname.match(/^\/file\/bot([^/]+)\/(.+)$/)
    if (fm) return fm[1] === this.token ? new Response(new TextEncoder().encode(`fake file ${fm[2]}`)) : new Response('no', { status: 401 })
    const m = url.pathname.match(/^\/bot([^/]+)\/(\w+)$/)
    if (!m || m[1] !== this.token) return Response.json({ ok: false, error_code: 401, description: 'Unauthorized' }, { status: 401 })
    const method = m[2]!
    let params: Record<string, any> = {}
    const ct = req.headers.get('content-type') ?? ''
    if (ct.includes('multipart/form-data')) {
      const fd = await req.formData()
      for (const [k, v] of fd.entries()) params[k] = typeof v === 'string' ? v : `<file ${(v as File).name} ${(v as File).size}B>`
      if (typeof params.reply_parameters === 'string') params.reply_parameters = JSON.parse(params.reply_parameters)
    } else if (req.method === 'POST') {
      try { params = await req.json() as Record<string, any> } catch { params = {} }
    }
    this.calls.push({ method, at: Date.now(), params })
    const fault = this.takeFault(method, params)
    if (fault?.mode === 'slow') await new Promise(r => setTimeout(r, fault.delayMs ?? 1000))
    else if (fault && fault.mode !== 'garbled') {
      return Response.json({ ok: false, error_code: fault.status ?? 400, description: fault.description ?? 'Bad Request: injected fault', ...(fault.retryAfter ? { parameters: { retry_after: fault.retryAfter } } : {}) }, { status: fault.status ?? 400 })
    }
    // 真实 Bot API 的硬性限制
    const markup = parseMarkup(params.reply_markup)
    if ((method === 'sendMessage' || method === 'editMessageText') && String(params.text ?? '').length > 4096) return bad('Bad Request: message is too long')
    if (flatten(markup).some(b => new TextEncoder().encode(b.data).length > 64)) return bad('Bad Request: BUTTON_DATA_INVALID')
    switch (method) {
      case 'getMe': return ok(this.bot)
      case 'getUpdates': {
        if (params.allowed_updates !== undefined) this.lastAllowedUpdates = parseList(params.allowed_updates)
        return ok(await this.getUpdates(Number(params.offset ?? 0), Number(params.timeout ?? 0)))
      }
      case 'sendChatAction': return ok(true)
      case 'setMessageReaction': {
        const r = params.reaction?.[0]?.emoji ?? null
        this.reactions.push({ chatId: String(params.chat_id), messageId: Number(params.message_id), emoji: r })
        return ok(true)
      }
      case 'getFile': return ok({ file_id: params.file_id, file_path: String(params.file_id).startsWith('voice') ? 'voice/file_1.oga' : 'photos/file_2.jpg' })
      case 'sendMessage':
      case 'sendPhoto':
      case 'sendVoice':
      case 'sendDocument': {
        const rp = params.reply_parameters
        const replyTo = rp?.message_id ? Number(rp.message_id) : undefined
        const s: Sent = { method, chatId: String(params.chat_id), messageId: this.nextMsg++, at: Date.now(), ...(params.text !== undefined ? { text: String(params.text) } : {}), ...(replyTo ? { replyTo } : {}), ...(params.photo ?? params.document ?? params.voice ? { file: String(params.photo ?? params.document ?? params.voice) } : {}), ...(markup ? { replyMarkup: markup } : {}) }
        this.sent.push(s)
        const date = Math.floor(Date.now() / 1000)
        this.remember(params.chat_id, s.messageId, true, String(params.text ?? params.caption ?? ''), markup, date)
        if (fault?.mode === 'garbled') return new Response('<html>bad gateway', { status: 200 })
        return ok({ message_id: s.messageId, date, chat: { id: Number(params.chat_id), type: 'private' }, text: params.text, ...(markup ? { reply_markup: markup } : {}) })
      }
      case 'editMessageText':
      case 'editMessageReplyMarkup': {
        const st = this.msgs.get(key(params.chat_id, Number(params.message_id)))
        if (!st || st.deleted) return bad('Bad Request: message to edit not found')
        if (!st.fromBot) return bad("Bad Request: message can't be edited")
        const text = method === 'editMessageText' ? String(params.text ?? '') : st.text
        const buttons = flatten(markup)
        if (text === st.text && sameButtons(buttons, st.buttons)) return bad('Bad Request: message is not modified: specified new message content and reply markup are exactly the same as a current content and reply markup of the message')
        st.text = text
        st.markup = markup
        st.buttons = buttons
        st.touched = ++this.touch
        this.edits.push({ method, chatId: st.chatId, messageId: st.messageId, ...(method === 'editMessageText' ? { text } : {}), replyMarkup: markup, at: Date.now() })
        return ok({ message_id: st.messageId, date: st.date, chat: { id: Number(params.chat_id), type: 'private' }, text: st.text, ...(markup ? { reply_markup: markup } : {}) })
      }
      case 'deleteMessage': {
        const st = this.msgs.get(key(params.chat_id, Number(params.message_id)))
        if (!st || st.deleted) return bad('Bad Request: message to delete not found')
        if (st.tooOld) return bad("Bad Request: message can't be deleted for everyone")
        if (!st.fromBot && this.notAdmin.has(st.chatId)) return bad("Bad Request: message can't be deleted")
        st.deleted = true
        this.deleted.push({ chatId: st.chatId, messageId: st.messageId, at: Date.now() })
        return ok(true)
      }
      case 'answerCallbackQuery': {
        const id = String(params.callback_query_id ?? '')
        const c = this.callbacks.get(id)
        if (!c || c.answered) return bad('Bad Request: query is too old and response timeout expired or query ID is invalid')
        c.answered = true
        this.answers.push({ id, ...(params.text !== undefined ? { text: String(params.text) } : {}), ...(params.show_alert ? { showAlert: true } : {}), at: Date.now() })
        return ok(true)
      }
      default:
        return Response.json({ ok: false, error_code: 404, description: `Not Found: method ${method}` }, { status: 404 })
    }
  }

  /** 按最近声明的 allowed_updates 过滤（没声明 callback_query 时点击更新被丢弃，和真实 Telegram 一样不再投递） */
  private allowed(u: Update): boolean {
    const l = this.lastAllowedUpdates
    if (!l || l.length === 0) return true
    return u.callback_query ? l.includes('callback_query') : l.includes('message')
  }

  private async getUpdates(offset: number, timeoutS: number): Promise<Update[]> {
    this.updates = this.updates.filter(u => u.update_id >= offset && this.allowed(u))
    if (this.updates.length === 0 && timeoutS > 0) {
      await new Promise<void>(r => {
        const t = setTimeout(r, Math.min(timeoutS, 2) * 1000)
        this.waiters.push(() => { clearTimeout(t); r() })
      })
    }
    this.updates = this.updates.filter(u => u.update_id >= offset && this.allowed(u))
    return this.updates.slice()
  }
}

function ok(result: unknown): Response {
  return Response.json({ ok: true, result })
}

function bad(description: string): Response {
  return Response.json({ ok: false, error_code: 400, description }, { status: 400 })
}

function parseMarkup(v: unknown): any {
  if (v === undefined || v === null || v === '') return null
  if (typeof v !== 'string') return v
  try { return JSON.parse(v) } catch { return null }
}

function parseList(v: unknown): string[] {
  if (Array.isArray(v)) return v.map(String)
  if (typeof v === 'string') { try { const a = JSON.parse(v); return Array.isArray(a) ? a.map(String) : [] } catch { return [] } }
  return []
}

function flatten(markup: any): Button[] {
  const rows = markup?.inline_keyboard
  if (!Array.isArray(rows)) return []
  return rows.flat().filter(Boolean).map((b: any) => ({ text: String(b.text ?? ''), data: String(b.callback_data ?? '') }))
}

function sameButtons(a: Button[], b: Button[]): boolean {
  return a.length === b.length && a.every((x, i) => x.text === b[i]!.text && x.data === b[i]!.data)
}
