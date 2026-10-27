// 群聊记录（M5）：同一台机器上所有 bot 的网关写同一份 <群聊记录目录>/<群 id>.jsonl（默认 ~/.config/claude-dsh-console/group_transcripts，
// 约定 v1.2 5.12.4 的中立目录）。
// 导演（director.py）读它决定谁说话、把最近的对话交给被点名的 bot；私聊时也从这里取"群里近况"。
// 行格式和旧系统的 group_transcripts 相同，另加 from_name（显示名）。
// - 真人消息：每个网关都会收到一遍，用"抢占标记文件"保证只记一次
// - bot 自己发出的话：Telegram 不把 bot 的消息投给别的 bot，所以由发出它的网关自己记（不再需要旧系统的 bot 互推）；
//   一次回复记一行（导演每行算一次发言，分段记会吃掉额度）
// - 轮转（约定 v1.2 5.12.4）：文件超过阈值，抢到锁的写方把它改名成 <群 id>.jsonl.1（只留一份旧的）；读方读当前 + .1
import { appendFileSync, closeSync, existsSync, mkdirSync, openSync, readdirSync, readSync, renameSync, rmSync, statSync, writeFileSync, writeSync } from 'fs'
import { join } from 'path'
import { safeError, type Logger } from '../log'
import { retryWhenBusy } from '../util'
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
  /** 群记录里的附图（约定 5.12.4）：attachment_kind 缺省视为 photo；document 不读 */
  attachment_kind?: string
  /** 附图的本机绝对路径（另一个程序写入，读侧当作不可信输入校验） */
  image_path?: string
  reply_to_text?: string
  reply_to_from?: string | null
}

const TAIL_BYTES = 128 * 1024
/** 轮转锁超过这么久没删，当作写方崩在半路留下的残留，谁都可以删（约定 v1.2 5.12.4） */
const ROTATE_LOCK_STALE_MS = 60_000

/** 记录行的时间：ISO 字符串（本网关写法）或毫秒数（别的写方/旧记录）。解析不了返回 NaN。 */
export function transcriptTs(v: unknown): number {
  if (typeof v === 'number') return Number.isFinite(v) ? v : NaN
  if (typeof v === 'string') return Date.parse(v)
  return NaN
}

export class GroupTranscript {
  constructor(
    private readonly dir: string,
    private readonly me: () => { id: number; username: string; name: string },
    private readonly log: Logger,
    /** 超过这么多字节（不含等于）就轮转 */
    private readonly rotateBytes: () => number = () => 5 * 1024 * 1024,
  ) {}

  private file(chatId: string): string { return join(this.dir, `${chatId}.jsonl`) }

  private append(line: TranscriptLine): void {
    try {
      mkdirSync(this.dir, { recursive: true, mode: 0o700 })
      this.rotate(line.chat_id)
      // 每行都按路径追加打开、写完就关：别人刚轮转过，下一行自然写进新文件
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

  /** 本 bot 在群里的一次回复（各段已由调用方连成一条；messageId 是第一条发出的段，at 是最后一段发出的时刻）。
   *  photoPath 是这次回复第一张实际送达、且在共享图库内的原图真实路径；没有就不写图片字段。 */
  sent(chatId: string, messageId: number, text: string, at: Date = new Date(), photoPath?: string): void {
    const me = this.me()
    this.append({
      ts: at.toISOString(), chat_id: chatId, message_id: messageId, from_id: String(me.id), from_username: me.username,
      from_name: me.name, is_bot: true, text: text.slice(0, 1000), observed_by: me.username,
      ...(photoPath ? { attachment_kind: 'photo', image_path: photoPath } : {}),
    })
  }

  /** 文件大于阈值就改名成 .1。抢不到锁（别人正在轮转）就跳过这次，调用方照常追加；不整份重写 */
  private rotate(chatId: string): void {
    const path = this.file(chatId)
    const limit = this.rotateBytes()
    try { if (statSync(path).size <= limit) return } catch { return } // 文件还没有
    const lock = join(this.dir, `.rotate-${chatId}`)
    try { if (Date.now() - statSync(lock).mtimeMs > ROTATE_LOCK_STALE_MS) rmSync(lock, { force: true }) } catch {}
    let fd: number
    try { fd = openSync(lock, 'wx', 0o600) } catch { return }
    try {
      try { writeSync(fd, this.me().username) } finally { closeSync(fd) }
      // 抢锁前后别人可能刚轮转完：复查一遍还超才改名
      if (statSync(path).size > limit) {
        const old = `${path}.1`
        // 直接改名覆盖旧的 .1；改名失败（个别 Windows 环境目标已存在时）才删掉 .1 再试一次，不先删免得 .1 空窗
        try {
          retryWhenBusy(() => renameSync(path, old))
        } catch {
          rmSync(old, { force: true })
          retryWhenBusy(() => renameSync(path, old))
        }
        this.log.info('group.transcript_rotated', { chat: chatId })
      }
    } catch (e) {
      this.log.warn('group.rotate_failed', { chat: chatId, err: safeError(e) })
    } finally {
      try { rmSync(lock, { force: true }) } catch {}
    }
  }

  /** 读文件末尾一段（整行开头）；没有或读不了返回空串 */
  private tail(path: string): string {
    if (!existsSync(path)) return ''
    try {
      const size = statSync(path).size
      const start = Math.max(0, size - TAIL_BYTES)
      const buf = Buffer.alloc(size - start)
      const fd = openSync(path, 'r')
      try { readSync(fd, buf, 0, buf.length, start) } finally { closeSync(fd) }
      const text = buf.toString('utf8')
      return start > 0 ? text.slice(text.indexOf('\n') + 1) : text
    } catch { return '' }
  }

  /** 某个时间之后的群消息（按消息编号去重、时间正序，最多 limit 条）。读 .1 和当前文件各自末尾一段（刚轮转过近况也不丢），记录再长也不慢。
   *  只有图片没有 text 的行也要读出来（群里的纯图片消息）；去重只合并**同一写方**的重复行。 */
  since(chatId: string, afterMs: number, limit: number): TranscriptLine[] {
    const path = this.file(chatId)
    // 中间补一个换行：.1 最后一行万一没换行也不会和当前文件第一行粘在一起
    const text = this.tail(`${path}.1`) + '\n' + this.tail(path)
    const byLine = new Map<string, TranscriptLine>()
    for (const l of text.split('\n')) {
      if (!l) continue
      try {
        const o = JSON.parse(l) as TranscriptLine
        const ts = transcriptTs(o.ts)
        if (!(ts > afterMs)) continue
        // 下游（私聊近况、群图水位）都按 ISO 字符串比较：数字时间戳在这里统一归一
        o.ts = new Date(ts).toISOString()
        if (typeof o.text !== 'string') o.text = ''
        // 只合并同一个写方（observed_by）写的重复行：不同写方各看到一次同一消息时保留两行，
        // 否则别人的图片会被并到本 bot 的 observe 行上，随后按"自己写的"被排除掉
        const writer = typeof o.observed_by === 'string' ? o.observed_by : ''
        const key = `${writer}\u0000${o.message_id}`
        const prev = byLine.get(key)
        if (prev) {
          // 同一个 id 被两份文件各读到一次/同一写方各写一行：补齐缺的字段，不重复交给模型
          if (!prev.text && o.text) prev.text = o.text
          if (!prev.attachment_kind && o.attachment_kind) prev.attachment_kind = o.attachment_kind
          if (!prev.image_path && o.image_path) prev.image_path = o.image_path
          continue
        }
        byLine.set(key, o)
      } catch {}
    }
    return [...byLine.values()].sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts)).slice(-limit)
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
