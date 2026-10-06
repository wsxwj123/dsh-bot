// 假 Telegram：本机 HTTP 服务，实现网关用到的那几个 Bot API 方法，能按剧本制造故障，并记录收到的每一次发送。
export type Sent = {
  method: string
  chatId: string
  text?: string
  replyTo?: number
  file?: string
  messageId: number
  at: number
}

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

type Update = { update_id: number; message: any }

export class FakeTelegram {
  readonly token = '123456789:AAFakeTokenForTestsOnly_abcdefghijklmnop'
  readonly bot: { id: number; is_bot: true; first_name: string; username: string }
  server: ReturnType<typeof Bun.serve>
  sent: Sent[] = []
  calls: { method: string; at: number; params: Record<string, any> }[] = []
  reactions: { chatId: string; messageId: number; emoji: string | null }[] = []
  faults: Fault[] = []
  private updates: Update[] = []
  private nextUpdate = 1
  private nextMsg = 1000
  private waiters: (() => void)[] = []

  /** 多个 bot（群聊测试）：每个假 Telegram 当一个不同的 bot */
  constructor(o: { botId?: number; username?: string } = {}) {
    this.bot = { id: o.botId ?? 900001, is_bot: true, first_name: o.username ?? 'TestBot', username: o.username ?? 'test_dsh_bot' }
    this.server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch: req => this.handle(req) })
  }

  get url(): string { return `http://127.0.0.1:${this.server.port}` }

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
    this.updates.push({ update_id: this.nextUpdate++, message })
    for (const w of this.waiters.splice(0)) w()
    return mid
  }

  /** 用户发来一张图片（可带说明文字） */
  pushPhoto(fromId: number, caption?: string): number {
    return this.pushMedia(fromId, { photo: [{ file_id: 'photo-small', width: 90, height: 90 }, { file_id: 'photo-big', width: 800, height: 600 }], ...(caption ? { caption } : {}) })
  }

  /** 用户发来一条语音 */
  pushVoice(fromId: number): number {
    return this.pushMedia(fromId, { voice: { file_id: 'voice-1', duration: 3, mime_type: 'audio/ogg' } })
  }

  private pushMedia(fromId: number, extra: object): number {
    const mid = this.nextMsg++
    const message = { message_id: mid, date: Math.floor(Date.now() / 1000), chat: { id: fromId, type: 'private' }, from: { id: fromId, is_bot: false, first_name: `user${fromId}` }, ...extra }
    this.updates.push({ update_id: this.nextUpdate++, message })
    for (const w of this.waiters.splice(0)) w()
    return mid
  }

  sentTo(chatId: number | string): Sent[] {
    return this.sent.filter(s => s.chatId === String(chatId))
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
    switch (method) {
      case 'getMe': return ok(this.bot)
      case 'getUpdates': return ok(await this.getUpdates(Number(params.offset ?? 0), Number(params.timeout ?? 0)))
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
        const s: Sent = { method, chatId: String(params.chat_id), messageId: this.nextMsg++, at: Date.now(), ...(params.text !== undefined ? { text: String(params.text) } : {}), ...(replyTo ? { replyTo } : {}), ...(params.photo ?? params.document ?? params.voice ? { file: String(params.photo ?? params.document ?? params.voice) } : {}) }
        this.sent.push(s)
        if (fault?.mode === 'garbled') return new Response('<html>bad gateway', { status: 200 })
        return ok({ message_id: s.messageId, date: Math.floor(Date.now() / 1000), chat: { id: Number(params.chat_id), type: 'private' }, text: params.text })
      }
      default:
        return Response.json({ ok: false, error_code: 404, description: `Not Found: method ${method}` }, { status: 404 })
    }
  }

  private async getUpdates(offset: number, timeoutS: number): Promise<Update[]> {
    this.updates = this.updates.filter(u => u.update_id >= offset)
    if (this.updates.length === 0 && timeoutS > 0) {
      await new Promise<void>(r => {
        const t = setTimeout(r, Math.min(timeoutS, 2) * 1000)
        this.waiters.push(() => { clearTimeout(t); r() })
      })
    }
    return this.updates.filter(u => u.update_id >= offset)
  }
}

function ok(result: unknown): Response {
  return Response.json({ ok: true, result })
}
