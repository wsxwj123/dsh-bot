// 群聊记录（M5）：同一台机器上所有 bot 的网关写同一份 <DSH_BOT_HOME>/groups/<群 id>.jsonl。
// 导演（director.py）读它决定谁说话、把最近的对话交给被点名的 bot；私聊时也从这里取"群里近况"。
// 行格式和旧系统的 group_transcripts 相同，另加 from_name（显示名）。
// - 真人消息：每个网关都会收到一遍，用"抢占标记文件"保证只记一次
// - bot 自己发出的话：Telegram 不把 bot 的消息投给别的 bot，所以由发出它的网关自己记（不再需要旧系统的 bot 互推）
import { appendFileSync, closeSync, existsSync, mkdirSync, openSync, readdirSync, readSync, rmSync, statSync, writeFileSync } from 'fs'
import { join } from 'path'
import { safeError, type Logger } from '../log'
import type { TgMessage } from '../telegram/api'
import { senderName, toInbound } from '../telegram/inbound'

export type TranscriptLine = {
  ts: string
  chat_id: string
  message_id: number
  from_id: string
  from_username: string | null
  from_name: string
  is_bot: boolean
  text: string
  observed_by: string
  reply_to_text?: string
  reply_to_from?: string | null
}

const TAIL_BYTES = 128 * 1024

export class GroupTranscript {
  constructor(
    private readonly dir: string,
    private readonly me: () => { id: number; username: string; name: string },
    private readonly log: Logger,
  ) {}

  private file(chatId: string): string { return join(this.dir, `${chatId}.jsonl`) }

  private append(line: TranscriptLine): void {
    try {
      mkdirSync(this.dir, { recursive: true, mode: 0o700 })
      appendFileSync(this.file(line.chat_id), JSON.stringify(line) + '\n', { mode: 0o600 })
    } catch (e) {
      this.log.warn('group.transcript_failed', { chat: line.chat_id, err: safeError(e) })
    }
  }

  /** 群里真人说的话（每个网关都会收到，谁先抢到标记谁记） */
  observe(msg: TgMessage): void {
    const chatId = String(msg.chat.id)
    const seen = join(this.dir, '.seen')
    try {
      mkdirSync(seen, { recursive: true, mode: 0o700 })
      writeFileSync(join(seen, `${chatId}_${msg.message_id}`), '', { flag: 'wx' })
    } catch { return } // 别的网关已经记了
    if (Math.random() < 0.02) this.sweep(seen)
    const r = msg.reply_to_message
    this.append({
      ts: new Date(msg.date * 1000).toISOString(), chat_id: chatId, message_id: msg.message_id,
      from_id: String(msg.from?.id ?? ''), from_username: msg.from?.username ?? null, from_name: senderName(msg.from),
      is_bot: msg.from?.is_bot === true, text: toInbound(msg, null).text.slice(0, 1000), observed_by: this.me().username,
      ...(r ? { reply_to_text: (r.text ?? r.caption ?? '').slice(0, 200), reply_to_from: r.from?.username ?? null } : {}),
    })
  }

  /** 本 bot 在群里发出的一段 */
  sent(chatId: string, messageId: number, text: string): void {
    const me = this.me()
    this.append({
      ts: new Date().toISOString(), chat_id: chatId, message_id: messageId, from_id: String(me.id), from_username: me.username,
      from_name: me.name, is_bot: true, text: text.slice(0, 1000), observed_by: me.username,
    })
  }

  /** 某个时间之后的群消息（按消息编号去重、时间正序，最多 limit 条）。只读文件末尾一段，记录再长也不慢 */
  since(chatId: string, afterMs: number, limit: number): TranscriptLine[] {
    const path = this.file(chatId)
    if (!existsSync(path)) return []
    let text = ''
    try {
      const size = statSync(path).size
      const start = Math.max(0, size - TAIL_BYTES)
      const buf = Buffer.alloc(size - start)
      const fd = openSync(path, 'r')
      readSync(fd, buf, 0, buf.length, start)
      closeSync(fd)
      text = buf.toString('utf8')
      if (start > 0) text = text.slice(text.indexOf('\n') + 1)
    } catch { return [] }
    const seen = new Set<number>()
    const out: TranscriptLine[] = []
    for (const l of text.split('\n')) {
      if (!l) continue
      try {
        const o = JSON.parse(l) as TranscriptLine
        if (seen.has(o.message_id) || !(Date.parse(o.ts) > afterMs) || !o.text) continue
        seen.add(o.message_id)
        out.push(o)
      } catch {}
    }
    return out.sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts)).slice(-limit)
  }

  private sweep(seen: string): void {
    const cutoff = Date.now() - 24 * 3600_000
    try {
      for (const f of readdirSync(seen)) {
        const p = join(seen, f)
        try { if (statSync(p).mtimeMs < cutoff) rmSync(p, { force: true }) } catch {}
      }
    } catch {}
  }
}
