// 端到端测试的公共部分：准备隔离目录、写配置、以子进程拉起网关（这样才能真的强杀它）、读假 ACP 的落盘记录。
import type { Subprocess } from 'bun'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join, resolve } from 'path'
import { Ledger } from '../src/ledger'
import { FakeTelegram } from './fakes/fake-telegram'

export const GATEWAY_DIR = resolve(import.meta.dir, '..')
export const FAKE_ACP = join(import.meta.dir, 'fakes', 'fake-acp.ts')
export const OWNER = 5550001
export const FRIEND = 5550002

export const sleep = (ms: number) => new Promise(r => setTimeout(r, ms))

export async function until<T>(fn: () => T | Promise<T>, what: string, timeoutMs = 15_000, stepMs = 50): Promise<NonNullable<T>> {
  const end = Date.now() + timeoutMs
  let last: T | undefined
  while (Date.now() < end) {
    last = await fn()
    if (last) return last as NonNullable<T>
    await sleep(stepMs)
  }
  throw new Error(`timed out waiting for: ${what}`)
}

export const SAMPLE_PERSONA = '# 测试人设\n你是小测，说话简短。称呼对方为 {{user}}。\n'

export type BotEnv = {
  name: string
  root: string
  configPath: string
  channelDir: string
  botDir: string
  acpState: string
  tg: FakeTelegram
  gw: Record<string, unknown>
  brain: Record<string, unknown>
}

export function makeBot(tg: FakeTelegram, o: { name?: string; root?: string; gw?: Record<string, unknown>; brain?: Record<string, unknown>; access?: Record<string, unknown> } = {}): BotEnv {
  const root = o.root ?? mkdtempSync(join(tmpdir(), 'dshbot-'))
  const name = o.name ?? 'testbot'
  const botDir = join(root, 'bots', name)
  const channelDir = join(botDir, 'channel')
  const acpState = join(root, `acp-${name}`)
  mkdirSync(channelDir, { recursive: true })
  mkdirSync(join(botDir, 'media'), { recursive: true })
  writeFileSync(join(channelDir, 'CLAUDE.md'), SAMPLE_PERSONA)
  writeFileSync(join(channelDir, '.env'), `TELEGRAM_BOT_TOKEN=${tg.token}\n`)
  writeFileSync(join(channelDir, 'access.json'), JSON.stringify({ dmPolicy: 'allowlist', allowFrom: [String(OWNER), String(FRIEND)], groups: {}, splitOnParagraph: true, paragraphDelay: 0, ...o.access }))
  const env: BotEnv = {
    name, root, botDir, channelDir, acpState, tg,
    configPath: join(root, `${name}.yml`),
    gw: {
      telegram_api: tg.url,
      dsh_command: ['bun', FAKE_ACP, '--state', acpState],
      burst_window_ms: 150,
      burst_max_ms: 2_000,
      poll_timeout_s: 1,
      retry_backoff_ms: [50, 100],
      turn_stall_cancel_ms: 60_000,
      turn_stall_warn_ms: 30_000,
      max_send_wait_ms: 500,
      config_poll_ms: 100,
      heartbeat_ms: 500,
      probe_ms: 60_000,
      log_level: 'debug',
      ...o.gw,
    },
    brain: { provider: 'deepseek-official', model: 'deepseek-flash', reasoning_effort: 'low', ...o.brain },
  }
  writeConfig(env)
  return env
}

export function writeConfig(b: BotEnv): void {
  const y = {
    id: b.name,
    display_name: b.name,
    bot_channel_path: b.channelDir,
    dispatcher_port: 0,
    brain: b.brain,
    gateway: b.gw,
  }
  // JSON 是 YAML 的子集
  writeFileSync(b.configPath, JSON.stringify(y, null, 1))
}

export class Gateway {
  proc: Subprocess | null = null
  apiPort = 0
  stderr = ''
  constructor(readonly b: BotEnv) {}

  async start(extraEnv: Record<string, string> = {}): Promise<void> {
    const ready = join(this.b.root, `${this.b.name}.ready`)
    rmSync(ready, { force: true })
    this.stderr = ''
    this.proc = Bun.spawn(['bun', join(GATEWAY_DIR, 'src', 'main.ts'), '--config', this.b.configPath], {
      cwd: GATEWAY_DIR,
      env: { PATH: process.env.PATH ?? '', HOME: process.env.HOME ?? this.b.root, DSH_BOT_HOME: this.b.root, DSH_BOT_READY_FILE: ready, ...(process.platform === 'win32' ? { SystemRoot: process.env.SystemRoot ?? '', TEMP: process.env.TEMP ?? '', TMP: process.env.TMP ?? '', USERPROFILE: process.env.USERPROFILE ?? '' } : {}), ...extraEnv },
      stdout: 'ignore',
      stderr: 'pipe',
    })
    const p = this.proc
    void (async () => { for await (const c of p.stderr as ReadableStream<Uint8Array>) this.stderr += new TextDecoder().decode(c) })()
    await until(() => existsSync(ready) || p.exitCode !== null, 'gateway ready', 20_000)
    if (!existsSync(ready)) throw new Error(`gateway exited during startup: ${this.stderr}`)
    this.apiPort = JSON.parse(readFileSync(ready, 'utf8')).api_port
  }

  /** 强杀（模拟断电），等它真的退出 */
  async kill(): Promise<void> {
    if (!this.proc) return
    this.proc.kill('SIGKILL')
    await this.proc.exited
    this.proc = null
  }

  async stop(): Promise<void> {
    if (!this.proc) return
    this.proc.kill('SIGTERM')
    const t = setTimeout(() => this.proc?.kill('SIGKILL'), 15_000)
    await this.proc.exited
    clearTimeout(t)
    this.proc = null
  }

  async waitExit(timeoutMs = 15_000): Promise<number | null> {
    if (!this.proc) return null
    const p = this.proc
    const code = await Promise.race([p.exited, sleep(timeoutMs).then(() => null)])
    if (code !== null) this.proc = null
    return code as number | null
  }

  apiToken(): string { return readFileSync(join(this.b.botDir, 'state', 'api.key'), 'utf8').trim() }

  api(path: string, init: RequestInit = {}): Promise<Response> {
    return fetch(`http://127.0.0.1:${this.apiPort}${path}`, init)
  }

  /** 只读打开账本（网关在跑也能读，WAL 模式） */
  ledger(): Ledger { return new Ledger(join(this.b.botDir, 'state', 'ledger.sqlite')) }
}

// ─── 读假 ACP 的落盘记录 ───

export function readJsonl<T = any>(file: string): T[] {
  if (!existsSync(file)) return []
  return readFileSync(file, 'utf8').split('\n').filter(Boolean).map(l => JSON.parse(l) as T)
}

export type PromptRec = { pid: number; at: number; sessionId: string; text: string; blocks: number; model: string }
export const prompts = (b: BotEnv) => readJsonl<PromptRec>(join(b.acpState, 'prompts.jsonl'))
export const toolResults = (b: BotEnv) => readJsonl<{ sessionId: string; name: string; text: string; isError: boolean }>(join(b.acpState, 'tool-results.jsonl'))
export const configCalls = (b: BotEnv) => readJsonl<{ pid: number; sessionId: string; configId: string; value: string }>(join(b.acpState, 'config.jsonl'))
export const lifecycle = (b: BotEnv) => readJsonl<{ pid: number; event: string }>(join(b.acpState, 'lifecycle.jsonl'))
export function sessionsState(b: BotEnv): Record<string, { history: string[] }> {
  const f = join(b.acpState, 'sessions.json')
  return existsSync(f) ? JSON.parse(readFileSync(f, 'utf8')) : {}
}
export function envDumps(b: BotEnv): Record<string, string>[] {
  if (!existsSync(b.acpState)) return []
  return readdirSync(b.acpState).filter(f => f.startsWith('env-')).map(f => JSON.parse(readFileSync(join(b.acpState, f), 'utf8')))
}

/** 每个会话里，某段文字在模型看到的历史中出现了几次（取最大值） */
export function maxTimesSeen(b: BotEnv, needle: string): number {
  let max = 0
  for (const s of Object.values(sessionsState(b))) {
    const n = s.history.join('\n').split(needle).length - 1
    if (n > max) max = n
  }
  return max
}

export function cleanup(b: BotEnv): void {
  try { rmSync(b.root, { recursive: true, force: true }) } catch {}
}
