import { randomBytes, timingSafeEqual } from 'crypto'
import { chmodSync, closeSync, existsSync, mkdirSync, openSync, readFileSync, renameSync, rmSync, writeFileSync } from 'fs'
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

/**
 * 先写临时文件再改名，半截文件不会出现。
 * - 临时文件用"不存在才创建"打开，一创建就是给定权限（凭据文件 600，方案 3.1.2）；同名的旧临时文件（上次崩溃留下、pid 复用）先删掉。
 * - 任何一步失败都删掉临时文件再抛：盘上不留含密钥的残余。
 * - Windows 上目标文件正被别的进程读着时改名会报 EPERM/EBUSY，短暂重试（方案 3.11）。
 */
export function writeAtomic(path: string, data: string, mode?: number): void {
  mkdirSync(dirname(path), { recursive: true })
  const tmp = `${path}.tmp-${process.pid}`
  try {
    rmSync(tmp, { force: true })
    const fd = openSync(tmp, 'wx', mode ?? 0o666)
    try { writeFileSync(fd, data) } finally { closeSync(fd) }
    if (mode !== undefined && process.platform !== 'win32') chmodSync(tmp, mode)
    retryWhenBusy(() => renameSync(tmp, path))
  } catch (e) {
    try { rmSync(tmp, { force: true }) } catch {}
    throw e
  }
}

/** 文件被占用（EPERM/EBUSY，Windows 上常见且是暂时的）时，每隔 waitMs 再试，最多再试 retries 次；其它错误直接抛 */
export function retryWhenBusy<T>(fn: () => T, retries = 5, waitMs = 50): T {
  for (let i = 0; ; i++) {
    try {
      return fn()
    } catch (e) {
      const code = (e as { code?: unknown })?.code
      if (i >= retries || (code !== 'EPERM' && code !== 'EBUSY')) throw e
      Bun.sleepSync(waitMs)
    }
  }
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

/**
 * 把本机地址加进 NO_PROXY / no_proxy（方案 D16）：保留原有值、追加而不是覆盖。
 * 这样拉模型列表时 127.0.0.1 / localhost / ::1 不绕代理，本机服务没开就如实报"连不上"；外网照旧走代理。
 * 就地改 env；返回 env 便于测试。
 */
export function addNoProxyHosts(env: Record<string, string | undefined>, hosts: string[] = ['127.0.0.1', 'localhost', '::1']): Record<string, string | undefined> {
  for (const key of ['NO_PROXY', 'no_proxy']) {
    const set = new Set((env[key] ?? '').split(',').map(s => s.trim()).filter(Boolean))
    for (const h of hosts) set.add(h)
    env[key] = [...set].join(',')
  }
  return env
}
