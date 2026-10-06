// dsh 进程管理：单实例、环境变量白名单、逐级停止、意外退出通知。
// 启动方式：node <harness>/node_modules/@deepseek-ai/dsh/lib/bin.js --profile acp --patch <补丁层>
// （不走 .cmd 外壳，Windows 上也一样）。
import type { Subprocess } from 'bun'
import { existsSync, mkdirSync, readFileSync, rmSync } from 'fs'
import { join } from 'path'
import { RotatingFile, redact, safeError, type Logger } from '../log'
import { sleep, writeAtomic } from '../util'
import { AcpConnection } from './acp'

/** 允许传给 dsh 的环境变量（其余一概不传，包括 Telegram 令牌和网关自己的口令）。 */
const PASS_THROUGH = [
  'LANG', 'LC_ALL', 'LC_CTYPE', 'TMPDIR',
  'HTTPS_PROXY', 'HTTP_PROXY', 'NO_PROXY', 'ALL_PROXY', 'https_proxy', 'http_proxy', 'no_proxy', 'all_proxy',
  'NODE_EXTRA_CA_CERTS', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
]
const WINDOWS_PASS = ['SystemRoot', 'SYSTEMROOT', 'windir', 'ComSpec', 'PATHEXT', 'TEMP', 'TMP', 'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE', 'OS', 'ProgramData', 'ProgramFiles']

function getEnvCI(env: Record<string, string | undefined>, name: string): string | undefined {
  if (env[name] !== undefined) return env[name]
  const k = Object.keys(env).find(x => x.toLowerCase() === name.toLowerCase())
  return k ? env[k] : undefined
}

export function buildDshEnv(o: { dshHome: string; homeDir: string; timezone: string }, parent: Record<string, string | undefined> = process.env): Record<string, string> {
  const env: Record<string, string> = {
    PATH: getEnvCI(parent, 'PATH') ?? '/usr/bin:/bin',
    HOME: o.homeDir,
    DSH_HOME: o.dshHome,
    DSH_TELEMETRY_DISABLED: '1',
    TZ: o.timezone,
  }
  for (const k of PASS_THROUGH) { const v = parent[k]; if (v !== undefined) env[k] = v }
  if (!env.LANG) env.LANG = 'C.UTF-8'
  if (process.platform === 'win32') {
    for (const k of WINDOWS_PASS) { const v = getEnvCI(parent, k); if (v !== undefined) env[k] = v }
    env.USERPROFILE = o.homeDir
    env.APPDATA = join(o.homeDir, 'AppData', 'Roaming')
    env.LOCALAPPDATA = join(o.homeDir, 'AppData', 'Local')
  }
  return env
}

/** 读某个进程的完整命令行；拿不到就返回 null。用来确认 pid 文件里记的进程确实是我们的 dsh（防止 pid 被别的进程复用）。 */
export async function processCommandLine(pid: number): Promise<string | null> {
  try {
    if (process.platform === 'linux') {
      const raw = readFileSync(`/proc/${pid}/cmdline`)
      return raw.toString('utf8').split('\0').join(' ').trim() || null
    }
    if (process.platform === 'win32') {
      const p = Bun.spawn(['powershell', '-NoProfile', '-NonInteractive', '-Command', `(Get-CimInstance Win32_Process -Filter "ProcessId=${pid}").CommandLine`], { stdout: 'pipe', stderr: 'ignore' })
      const out = (await new Response(p.stdout).text()).trim()
      await p.exited
      return out || null
    }
    const p = Bun.spawn(['ps', '-p', String(pid), '-o', 'command='], { stdout: 'pipe', stderr: 'ignore' })
    const out = (await new Response(p.stdout).text()).trim()
    await p.exited
    return out || null
  } catch {
    return null
  }
}

export function isAlive(pid: number): boolean {
  try { process.kill(pid, 0); return true } catch (e) { return (e as { code?: string }).code === 'EPERM' }
}

async function killTree(pid: number, force: boolean): Promise<void> {
  if (process.platform === 'win32') {
    const p = Bun.spawn(['taskkill', '/PID', String(pid), '/T', ...(force ? ['/F'] : [])], { stdout: 'ignore', stderr: 'ignore' })
    await p.exited
    return
  }
  try { process.kill(pid, force ? 'SIGKILL' : 'SIGTERM') } catch {}
}

async function waitGone(pid: number, ms: number): Promise<boolean> {
  const end = Date.now() + ms
  while (Date.now() < end) {
    if (!isAlive(pid)) return true
    await sleep(100)
  }
  return !isAlive(pid)
}

export type DshSpec = {
  command: string[]
  patchPath: string
  patchText: string
  cwd: string
  env: Record<string, string>
  pidFile: string
  stderrLog: string
}

export function defaultDshCommand(harnessDir: string, nodeBin?: string): string[] {
  const bin = join(harnessDir, 'node_modules', '@deepseek-ai', 'dsh', 'lib', 'bin.js')
  const node = nodeBin || process.env.DSH_BOT_NODE || Bun.which('node') || 'node'
  return [node, bin]
}

export class DshProcess {
  proc: Subprocess<'pipe', 'pipe', 'pipe'> | null = null
  conn: AcpConnection | null = null
  /** 每启动一次加一。会话是否已在当前进程里加载，按代数判断。 */
  generation = 0
  startedAt = 0
  agentVersion = ''
  private stopping = false
  onUnexpectedExit?: (code: number | null) => void

  constructor(private readonly log: Logger) {}

  get running(): boolean { return this.proc !== null && this.conn !== null && !this.conn.isClosed }
  get pid(): number | null { return this.proc?.pid ?? null }

  /** 清理上次留下的 dsh：pid 文件里的进程还活着、且命令行里有本 bot 的补丁层路径，才认定是我们的，才动它。 */
  async reapStale(spec: DshSpec): Promise<void> {
    if (!existsSync(spec.pidFile)) return
    let rec: { pid?: number; patch?: string } = {}
    try { rec = JSON.parse(readFileSync(spec.pidFile, 'utf8')) } catch {}
    const pid = Number(rec.pid)
    if (pid > 0 && pid === this.proc?.pid) return // 是自己刚拉起来的，不动
    if (pid > 0 && isAlive(pid)) {
      const cmd = await processCommandLine(pid)
      if (cmd && rec.patch && cmd.includes(rec.patch)) {
        this.log.warn('dsh.reap_stale', { pid })
        await killTree(pid, false)
        if (!(await waitGone(pid, 5_000))) { await killTree(pid, true); await waitGone(pid, 5_000) }
      } else {
        this.log.info('dsh.stale_pid_not_ours', { pid })
      }
    }
    rmSync(spec.pidFile, { force: true })
  }

  async start(spec: DshSpec): Promise<void> {
    if (this.proc) throw new Error('dsh already running')
    await this.reapStale(spec)
    mkdirSync(spec.cwd, { recursive: true })
    writeAtomic(spec.patchPath, spec.patchText)
    const argv = [...spec.command, '--profile', 'acp', '--patch', spec.patchPath]
    this.stopping = false
    const proc = Bun.spawn(argv, { cwd: spec.cwd, env: spec.env, stdin: 'pipe', stdout: 'pipe', stderr: 'pipe' })
    this.proc = proc
    this.generation++
    this.startedAt = Date.now()
    writeAtomic(spec.pidFile, JSON.stringify({ pid: proc.pid, startedAt: this.startedAt, patch: spec.patchPath }))
    const errLog = new RotatingFile(spec.stderrLog, 5 * 1024 * 1024, 3)
    void (async () => {
      const dec = new TextDecoder()
      let buf = ''
      try {
        for await (const chunk of proc.stderr) {
          buf += dec.decode(chunk, { stream: true })
          let i: number
          while ((i = buf.indexOf('\n')) >= 0) { errLog.write(redact(buf.slice(0, i))); buf = buf.slice(i + 1) }
        }
      } catch {}
      if (buf) errLog.write(redact(buf))
    })()
    const conn = new AcpConnection(proc.stdin, proc.stdout, this.log)
    this.conn = conn
    const gen = this.generation
    void proc.exited.then(code => {
      if (this.generation !== gen) return
      const unexpected = !this.stopping
      this.log[unexpected ? 'error' : 'info']('dsh.exited', { pid: proc.pid, code, unexpected })
      this.proc = null
      this.conn = null
      rmSync(spec.pidFile, { force: true })
      if (unexpected) this.onUnexpectedExit?.(code)
    })
    try {
      const init = await conn.initialize()
      this.agentVersion = String(init?.agentInfo?.version ?? '')
      this.log.info('dsh.started', { pid: proc.pid, generation: gen, agent: init?.agentInfo?.name, version: this.agentVersion })
    } catch (e) {
      this.log.error('dsh.initialize_failed', { err: safeError(e) })
      await this.stop()
      throw e
    }
  }

  /** 网关要停了：接下来 dsh 退出不算意外（Ctrl+C 时它和网关同时收到信号，会先自己退出） */
  expectExit(): void { this.stopping = true }

  /** 逐级停止：关闭标准输入等它自己退出 → SIGTERM（Windows 用 taskkill）→ 强杀。确认退出后才返回。 */
  async stop(graceMs = 5_000): Promise<void> {
    const proc = this.proc
    if (!proc) return
    this.stopping = true
    try { proc.stdin.end() } catch {}
    const exited = (ms: number) => Promise.race([proc.exited.then(() => true), sleep(ms).then(() => false)])
    if (!(await exited(graceMs))) {
      this.log.warn('dsh.stop_escalate', { pid: proc.pid, step: 'term' })
      await killTree(proc.pid, false)
      if (!(await exited(graceMs))) {
        this.log.warn('dsh.stop_escalate', { pid: proc.pid, step: 'kill' })
        await killTree(proc.pid, true)
        await proc.exited
      }
    }
    this.proc = null
    this.conn = null
  }
}
