// 日志：落盘、按大小轮转、写盘前统一脱敏。
// - gateway.log：每行一条 JSON，给机器看（排查用）
// - chat.log：人话对话流，给人看（tail -f）
// 任何地方都不要把 Error 对象原样写进日志：bun 的网络错误里会带完整请求地址（含 bot 令牌），先过 safeError()。
import { appendFileSync, existsSync, mkdirSync, renameSync, rmSync, statSync } from 'fs'
import { dirname } from 'path'
import { formatEvent } from './logview'

const PATTERNS: RegExp[] = [
  /\b\d{6,12}:[A-Za-z0-9_-]{30,}\b/g,          // Telegram bot 令牌
  /bot\d{6,12}:[A-Za-z0-9_-]{20,}/g,            // 出现在 URL 里的 bot 令牌
  /\bsk-[A-Za-z0-9_-]{12,}\b/g,                 // sk- 开头的密钥
  /(Bearer\s+)[A-Za-z0-9._~+/=-]{12,}/gi,       // Bearer 口令
  /(x-api-key["']?\s*[:=]\s*["']?)[^\s"',]{8,}/gi,
  /(\/\/[^/\s:@]+:)[^@\s/]+@/g,                  // URL 里的 user:password@
]

const secrets = new Set<string>()

/** 登记一个确切的机密值（令牌、密钥、本机口令），之后任何日志里出现都会被替换。 */
export function registerSecret(value: string | undefined | null): void {
  if (value && value.length >= 8) secrets.add(value)
}

export function redact(s: string): string {
  let out = s
  for (const v of secrets) if (out.includes(v)) out = out.split(v).join('***')
  for (const re of PATTERNS) out = out.replace(re, (_m, p1?: string) => (typeof p1 === 'string' && p1.length < 40 && /[:=\s/]$/.test(p1) ? `${p1}***` : '***'))
  return out
}

/** 把任意异常变成可以安全写日志的短描述：只取类名、code、状态码和脱敏后的消息前 200 字。 */
export function safeError(e: unknown): string {
  if (e === null || e === undefined) return String(e)
  if (typeof e !== 'object') return redact(String(e)).slice(0, 200)
  const o = e as { name?: unknown; code?: unknown; message?: unknown; status?: unknown }
  const parts = [typeof o.name === 'string' ? o.name : 'Error']
  if (o.code !== undefined) parts.push(`code=${String(o.code)}`)
  if (o.status !== undefined) parts.push(`status=${String(o.status)}`)
  if (typeof o.message === 'string') parts.push(redact(o.message).slice(0, 200))
  return parts.join(' ')
}

export class RotatingFile {
  constructor(readonly path: string, readonly maxBytes = 10 * 1024 * 1024, readonly keep = 5) {
    mkdirSync(dirname(path), { recursive: true })
  }

  write(line: string): void {
    try {
      if (existsSync(this.path) && statSync(this.path).size >= this.maxBytes) this.rotate()
      appendFileSync(this.path, line.endsWith('\n') ? line : `${line}\n`)
    } catch {
      // 日志写失败不能拖垮主流程
    }
  }

  private rotate(): void {
    try { rmSync(`${this.path}.${this.keep}`, { force: true }) } catch {}
    for (let i = this.keep - 1; i >= 1; i--) {
      const from = `${this.path}.${i}`
      if (existsSync(from)) try { renameSync(from, `${this.path}.${i + 1}`) } catch {}
    }
    try { renameSync(this.path, `${this.path}.1`) } catch {}
  }
}

export type Level = 'debug' | 'info' | 'warn' | 'error'
const ORDER: Record<Level, number> = { debug: 10, info: 20, warn: 30, error: 40 }

export class Logger {
  private readonly file?: RotatingFile
  private readonly chat?: RotatingFile
  constructor(
    /** pretty：终端里打印成一行一条的人话格式（见 logview.ts），文件里照旧是 JSON */
    readonly opts: { dir?: string; level?: Level; console?: boolean; pretty?: boolean; maxBytes?: number; keep?: number; bot?: string } = {},
  ) {
    if (opts.dir) {
      this.file = new RotatingFile(`${opts.dir}/gateway.log`, opts.maxBytes, opts.keep)
      this.chat = new RotatingFile(`${opts.dir}/chat.log`, opts.maxBytes, opts.keep)
    }
  }

  log(level: Level, event: string, fields: Record<string, unknown> = {}): void {
    if (ORDER[level] < ORDER[this.opts.level ?? 'info']) return
    const rec = { at: new Date().toISOString(), level, event, ...(this.opts.bot ? { bot: this.opts.bot } : {}), ...fields }
    let line: string
    try { line = redact(JSON.stringify(rec)) } catch { line = redact(`{"event":"${event}","note":"unserializable"}`) }
    this.file?.write(line)
    if (this.opts.console) process.stderr.write((this.opts.pretty ? formatEvent(line) : line) + '\n')
  }

  debug(event: string, fields?: Record<string, unknown>) { this.log('debug', event, fields) }
  info(event: string, fields?: Record<string, unknown>) { this.log('info', event, fields) }
  warn(event: string, fields?: Record<string, unknown>) { this.log('warn', event, fields) }
  error(event: string, fields?: Record<string, unknown>) { this.log('error', event, fields) }

  /** 人话对话流：谁在什么聊天说了什么。正文截断到 500 字。 */
  chatLine(who: string, chatId: string, text: string): void {
    const t = text.length > 500 ? `${text.slice(0, 500)}…` : text
    this.chat?.write(redact(`${new Date().toISOString()} [${chatId}] ${who}: ${t.replace(/\n/g, '⏎')}`))
  }
}
