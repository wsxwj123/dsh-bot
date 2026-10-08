// 验收：F7-1 开机自启的两处小修（INTERFACE 3.10.1）
// 说明：3.10.1 把 gateway/src/autostart.ts 导出的三个纯函数明文列为对外接口（"三平台可测"），这里只动态加载这三个导出；
// 不在测试里跑真实的 install（未实现时会真的写 plist、调 launchctl），命令行只测非 macOS 的"只支持 macOS"分支。
import { describe, expect, test } from 'bun:test'
import { readFileSync } from 'fs'
import { join } from 'path'
import { GATEWAY_DIR, REPO_DIR } from './_acc'

type Dir = { name: string; path: string }
type Fns = {
  protectedRepoDir: (repo: string, dirs: Dir[]) => string | null
  defaultProtectedDirs: (home: string) => Dir[]
  parseLaunchctlPrint: (out: string, code: number) => { state: string; pid: string | null; lastExit: string | null }
}
async function fns(): Promise<Fns> {
  const m = await import(join(GATEWAY_DIR, 'src', 'autostart.ts')) as Partial<Fns>
  for (const k of ['protectedRepoDir', 'defaultProtectedDirs', 'parseLaunchctlPrint'] as const) {
    if (typeof m[k] !== 'function') throw new Error(`gateway/src/autostart.ts 没有导出 ${k}`)
  }
  return m as Fns
}

const HOME = '/Users/u'
const DIRS: Dir[] = [
  { name: '~/Desktop', path: `${HOME}/Desktop` },
  { name: '~/Documents', path: `${HOME}/Documents` },
  { name: '~/Downloads', path: `${HOME}/Downloads` },
  { name: 'iCloud 云盘', path: `${HOME}/Library/Mobile Documents` },
]

describe('protectedRepoDir', () => {
  const CASES: [string, string | null][] = [
    [`${HOME}/Desktop/dsh-bot`, '~/Desktop'],
    [`${HOME}/Desktop`, '~/Desktop'],
    [`${HOME}/Documents/code/dsh-bot`, '~/Documents'],
    [`${HOME}/Downloads/x`, '~/Downloads'],
    [`${HOME}/Library/Mobile Documents/com~apple~CloudDocs/dsh-bot`, 'iCloud 云盘'],
    ['/users/U/desktop/dsh-bot', '~/Desktop'],
    [`${HOME}/Documents/dsh-bot/`, '~/Documents'],
    [`${HOME}/Desktop2/dsh-bot`, null],
    [`${HOME}/dsh-bot`, null],
    [`${HOME}`, null],
    ['/opt/dsh-bot', null],
  ]
  for (const [repo, want] of CASES) {
    test(`仓库 ${repo} → ${want === null ? 'null（不在受保护目录下）' : want}`, async () => {
      expect((await fns()).protectedRepoDir(repo, DIRS)).toBe(want)
    })
  }

  test('受保护目录写成带末尾 / 的路径也一样判断', async () => {
    const dirs = DIRS.map(d => ({ ...d, path: d.path + '/' }))
    expect((await fns()).protectedRepoDir(`${HOME}/Desktop/repo`, dirs)).toBe('~/Desktop')
  })

  test('受保护目录列表为空：总是 null', async () => {
    expect((await fns()).protectedRepoDir(`${HOME}/Desktop/repo`, [])).toBeNull()
  })
})

describe('defaultProtectedDirs', () => {
  test('返回四项：~/Desktop、~/Documents、~/Downloads、iCloud 云盘（<家目录>/Library/Mobile Documents）', async () => {
    const got = (await fns()).defaultProtectedDirs(HOME)
    expect([...got].sort((a, b) => a.name.localeCompare(b.name))).toEqual([...DIRS].sort((a, b) => a.name.localeCompare(b.name)))
  })

  test('和 protectedRepoDir 搭配：家目录根下的仓库不拦', async () => {
    const f = await fns()
    expect(f.protectedRepoDir(`${HOME}/dsh-bot`, f.defaultProtectedDirs(HOME))).toBeNull()
  })

  test('和 protectedRepoDir 搭配：桌面下的仓库拦下，返回 ~/Desktop', async () => {
    const f = await fns()
    expect(f.protectedRepoDir(`${HOME}/Desktop/dsh-bot`, f.defaultProtectedDirs(HOME))).toBe('~/Desktop')
  })
})

describe('parseLaunchctlPrint', () => {
  const svc = (body: string) => `gui/501/com.dsh-bot.bot5 = {\n${body}\n}\n`

  test('顶层 state / pid / last exit code 取全文', async () => {
    const out = svc('\tactive count = 1\n\tstate = running\n\tpid = 4242\n\tlast exit code = 0')
    expect((await fns()).parseLaunchctlPrint(out, 0)).toEqual({ state: 'running', pid: '4242', lastExit: '0' })
  })

  test('state = not running：保留全文（不再截成 not）', async () => {
    expect((await fns()).parseLaunchctlPrint(svc('\tstate = not running\n\tlast exit code = 1'), 0).state).toBe('not running')
  })

  test('last exit code = 78: EX_CONFIG：取全文', async () => {
    expect((await fns()).parseLaunchctlPrint(svc('\tstate = not running\n\tlast exit code = 78: EX_CONFIG'), 0).lastExit).toBe('78: EX_CONFIG')
  })

  test('last exit code = (never exited)：显示「从未退出」', async () => {
    expect((await fns()).parseLaunchctlPrint(svc('\tstate = running\n\tpid = 7\n\tlast exit code = (never exited)'), 0).lastExit).toBe('从未退出')
  })

  test('没有 last exit code 行：lastExit 为 null；没有 pid 行：pid 为 null', async () => {
    const r = (await fns()).parseLaunchctlPrint(svc('\tstate = waiting'), 0)
    expect(r.lastExit).toBeNull()
    expect(r.pid).toBeNull()
  })

  test('只看顶层：两层缩进的 state / pid 不算', async () => {
    const out = svc('\tendpoints = {\n\t\tstate = active\n\t\tpid = 99\n\t}\n\tstate = waiting\n\tlast exit code = 0')
    const r = (await fns()).parseLaunchctlPrint(out, 0)
    expect(r.state).toBe('waiting')
    expect(r.pid).toBeNull()
  })

  test('有多条顶层 state：取第一条', async () => {
    expect((await fns()).parseLaunchctlPrint(svc('\tstate = running\n\tstate = exited'), 0).state).toBe('running')
  })

  test('等号后的首尾空白去掉', async () => {
    expect((await fns()).parseLaunchctlPrint(svc('\tstate =    spawn scheduled   '), 0).state).toBe('spawn scheduled')
  })

  test('退出码不是 0：state 为「没在运行（launchd 里没有这个任务）」，pid、lastExit 为 null', async () => {
    expect((await fns()).parseLaunchctlPrint('Could not find service "com.dsh-bot.bot5" in domain for port\n', 113)).toEqual({ state: '没在运行（launchd 里没有这个任务）', pid: null, lastExit: null })
  })

  test('退出码 0 但没有顶层 state 行：state 为 ?', async () => {
    expect((await fns()).parseLaunchctlPrint(svc('\tpid = 1'), 0).state).toBe('?')
  })
})

describe('命令行与文档', () => {
  test.skipIf(process.platform === 'darwin')('非 macOS 上 install：行为不变，打印「只支持 macOS」，退出码 2', () => {
    const r = Bun.spawnSync(['bun', join(GATEWAY_DIR, 'scripts', 'autostart.ts'), 'install'], { cwd: REPO_DIR, env: { PATH: process.env.PATH ?? '', HOME: process.env.HOME ?? '' } })
    expect(r.exitCode).toBe(2)
    expect(r.stdout.toString() + r.stderr.toString()).toContain('只支持 macOS')
  })

  test('docs/dsh-migration/CUTOVER.md 写明「仓库要放在家目录根下」', () => {
    expect(readFileSync(join(REPO_DIR, 'docs', 'dsh-migration', 'CUTOVER.md'), 'utf8')).toContain('仓库要放在家目录根下')
  })
})
