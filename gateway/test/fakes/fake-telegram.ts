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
  /** 'garbled'：先当作已投递记下来，再回一个读不出来的响应（模拟"发出去了但回包丢了"） */
  mode?: 'error' | 'garbled'
}

type Update = { update_id: number; message: any }

export class FakeTelegram {
  readonly token = '123456789:AAFakeTokenForTestsOnly_abcdefghijklmnop'
  readonly bot = { id: 900001, is_bot: true, first_name: 'TestBot', username: 'test_dsh_bot' }
  server: ReturnType<typeof Bun.serve>
  sent: Sent[] = []
  calls: { method: string; at: number; params: Record<string, any> }[] = []
  reactions: { chatId: string; messageId: number; emoji: string | null }[] = []
  faults: Fault[] = []
  private updates: Update[] = []
  private nextUpdate = 1
  private nextMsg = 1000
  private waiters: (() => void)[] = []

  constructor() {
    this.server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch: req => this.handle(req) })
  }

  get url(): string { return `http://127.0.0.1:${this.server.port}` }

  stop(): void { this.server.stop(true) }

  /** 用户发来一条私聊文字 */
  pushText(fromId: number, text: string, o: { chatId?: number; replyTo?: { message_id: number; text?: string; fromBot?: boolean }; firstName?: string } = {}): number {
    const mid = this.nextMsg++
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
    if (fault && fault.mode !== 'garbled') {
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
      case 'sendMessage':
      case 'sendPhoto':
      case 'sendDocument': {
        const rp = params.reply_parameters
        const replyTo = rp?.message_id ? Number(rp.message_id) : undefined
        const s: Sent = { method, chatId: String(params.chat_id), messageId: this.nextMsg++, at: Date.now(), ...(params.text !== undefined ? { text: String(params.text) } : {}), ...(replyTo ? { replyTo } : {}), ...(params.photo ?? params.document ? { file: String(params.photo ?? params.document) } : {}) }
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
