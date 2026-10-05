import { randomBytes, timingSafeEqual } from 'crypto'
import { chmodSync, existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'fs'
import { dirname } from 'path'

export const sleep = (ms: number) => new Promise<void>(r => setTimeout(r, ms))

export function randomToken(bytes = 24): string {
  return randomBytes(bytes).toString('base64url')
}

/** 常量时间比较，防止按响应时间猜口令 */
export function safeEqual(a: string, b: string): boolean {
  const x = Buffer.from(a)
  const y = Buffer.from(b)
  if (x.length !== y.length) return false
  return timingSafeEqual(x, y)
}

/** 先写临时文件再改名，半截文件不会出现 */
export function writeAtomic(path: string, data: string, mode?: number): void {
  mkdirSync(dirname(path), { recursive: true })
  const tmp = `${path}.tmp-${process.pid}`
  writeFileSync(tmp, data, mode !== undefined ? { mode } : undefined)
  if (mode !== undefined && process.platform !== 'win32') chmodSync(tmp, mode)
  renameSync(tmp, path)
}

/** 读取或生成一个只有本用户能读的口令文件 */
export function ensureSecretFile(path: string): string {
  if (existsSync(path)) {
    const v = readFileSync(path, 'utf8').trim()
    if (v.length >= 16) {
      if (process.platform !== 'win32') chmodSync(path, 0o600)
      return v
    }
  }
  const v = randomToken(32)
  writeAtomic(path, `${v}\n`, 0o600)
  return v
}

export class Backoff {
  private n = 0
  constructor(readonly baseMs = 1_000, readonly maxMs = 60_000) {}
  next(): number {
    const ms = Math.min(this.maxMs, this.baseMs * 2 ** this.n)
    this.n++
    return Math.round(ms * (0.75 + Math.random() * 0.5))
  }
  reset(): void { this.n = 0 }
  get attempts(): number { return this.n }
}

/** 测试用的崩溃点：设置 DSH_BOT_CRASH_AT=<名字> 时，网关在那一步直接 SIGKILL 自己，模拟断电或被强杀。 */
export function crashPoint(name: string): void {
  const want = process.env.DSH_BOT_CRASH_AT
  if (!want || want !== name) return
  process.stderr.write(`crash point reached: ${name}\n`)
  if (process.platform === 'win32') process.exit(137)
  process.kill(process.pid, 'SIGKILL')
}
