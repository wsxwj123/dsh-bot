// 作息查询：问仓库根目录的 hang_situation.py "她此刻在干什么、接下来几点起床、手上的事几点忙完"。
// 结果缓存 5 分钟；查不到（Python 出错、超时、没配作息）一律返回 null，调用方按"不知道"处理，绝不卡住聊天。
import { dirname, join, resolve } from 'path'
import { safeError, type Logger } from '../log'

export type Situation = {
  name: string
  state: string
  interruptible: boolean
  termLabel: string
  /** 接下来几天每次起床的时刻（毫秒） */
  wakes: number[]
  /** 手上的事（上课、上班）做完的时刻；没在忙为 null */
  freeAt: number | null
  at: number
}

const REPO_ROOT = resolve(import.meta.dir, '..', '..', '..')
const TTL_MS = 5 * 60_000

export function isAsleep(s: Situation | null): boolean {
  return !!s && /睡|sleep/i.test(s.state)
}

export class SituationBridge {
  private cache: Situation | null = null
  private inflight: Promise<Situation | null> | null = null
  private warnedAt = 0

  constructor(
    private readonly botId: string,
    private readonly configPath: string,
    private readonly log: Logger,
    /** 测试用：换掉默认的 python3 hang_situation.py <bot> --plan */
    private readonly cmd?: string[],
  ) {}

  /** 最近一次查到的（不发起新查询）；超过 10 分钟没更新就当不知道 */
  current(now = Date.now()): Situation | null {
    return this.cache && now - this.cache.at < 2 * TTL_MS ? this.cache : null
  }

  async get(now = Date.now()): Promise<Situation | null> {
    if (this.cache && now - this.cache.at < TTL_MS) return this.cache
    return this.refresh()
  }

  refresh(): Promise<Situation | null> {
    this.inflight ??= this.query().finally(() => { this.inflight = null })
    return this.inflight
  }

  nextWake(after: number): number | null {
    return this.current()?.wakes.find(w => w > after) ?? null
  }

  nextFree(after: number): number | null {
    const f = this.current()?.freeAt
    return f && f > after ? f : null
  }

  private async query(): Promise<Situation | null> {
    const py = process.env.DSH_BOT_PYTHON || (process.platform === 'win32' ? 'python' : 'python3')
    const argv = this.cmd ?? [py, join(REPO_ROOT, 'hang_situation.py'), this.botId, '--plan']
    try {
      const p = Bun.spawn(argv, {
        cwd: REPO_ROOT,
        // 只给最少的环境变量：不把网关的环境整体传下去
        env: { PATH: process.env.PATH ?? '', HOME: process.env.HOME ?? '', HUB_CONFIGS_DIR: dirname(this.configPath), PYTHONIOENCODING: 'utf-8', ...(process.platform === 'win32' ? { SystemRoot: process.env.SystemRoot ?? '', USERPROFILE: process.env.USERPROFILE ?? '' } : {}) },
        stdout: 'pipe', stderr: 'ignore', stdin: 'ignore',
      })
      const timer = setTimeout(() => p.kill(), 10_000)
      const out = await new Response(p.stdout).text()
      clearTimeout(timer)
      if ((await p.exited) !== 0) throw new Error('bridge exited')
      const o = JSON.parse(out.trim().split('\n').pop() ?? '')
      if (typeof o?.name !== 'string' || typeof o?.state !== 'string') throw new Error('bad shape')
      this.cache = {
        name: o.name, state: o.state, interruptible: o.interruptible !== false,
        termLabel: typeof o.term_label === 'string' ? o.term_label : '',
        wakes: Array.isArray(o.wakes) ? o.wakes.filter((x: unknown) => typeof x === 'number').map((x: number) => x * 1000) : [],
        freeAt: typeof o.free_at === 'number' ? o.free_at * 1000 : null,
        at: Date.now(),
      }
      return this.cache
    } catch (e) {
      if (Date.now() - this.warnedAt > 10 * 60_000) { this.warnedAt = Date.now(); this.log.warn('situation.unavailable', { err: safeError(e) }) }
      return this.current()
    }
  }
}

/** 情境锚点：学期状态 + 正在做的事（和旧系统 situ_anchor 同一口径） */
export function situLine(s: Situation | null): string {
  if (!s) return ''
  const parts = [s.termLabel.trim(), s.name.trim()].filter(Boolean)
  return parts.length ? `⟦你此刻：${parts.join(' · ')}⟧` : ''
}
