// 开机自启（M6，macOS 的 launchd）：给每个 bot 的网关和导演各生成一个 LaunchAgent。纯函数，方便测试；
// 安装、卸载在 scripts/autostart.ts。
// - 进程环境只放必要的几项：PATH、HOME、DSH_BOT_HOME、代理设置（Telegram 要走代理时没有它连不上）、关遥测。
//   不放任何密钥：Telegram 令牌在频道目录的 .env，模型密钥在凭据文件，网关自己读
// - 日志：网关和导演自己写、自己轮转；launchd 的输出文件只会有崩溃时的几行
import { posix } from 'path'

const { dirname, join } = posix // launchd 只在 macOS 上用：一律按斜杠拼路径（测试在 Windows 上也跑）

export type Agent = {
  label: string
  args: string[]
  cwd: string
  env: Record<string, string>
  stdoutPath: string
  /** 定时任务：每隔多少秒跑一次，或者按日历（Weekday/Hour/Minute）跑。都不给就是常驻（退出了自动拉起） */
  interval?: number
  calendar?: Record<string, number>
}

/** 代理设置：从安装时的终端环境里带过去（launchd 启动的进程拿不到终端里的环境变量） */
export const PROXY_VARS = ['http_proxy', 'https_proxy', 'all_proxy', 'no_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY']

export function baseEnv(o: { home: string; dshBotHome?: string; extraPath: string[]; env: Record<string, string | undefined> }): Record<string, string> {
  const path = [...new Set([...o.extraPath, '/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/bin', '/usr/sbin', '/sbin'])].join(':')
  const env: Record<string, string> = { PATH: path, HOME: o.home, LANG: 'en_US.UTF-8', DSH_TELEMETRY_DISABLED: '1', PYTHONIOENCODING: 'utf-8' }
  if (o.dshBotHome) env.DSH_BOT_HOME = o.dshBotHome
  // 地址里带账号密码（user:pass@）的不写进去：plist 是明文文件
  for (const k of PROXY_VARS) if (o.env[k] && !o.env[k]!.includes('@')) env[k] = o.env[k]!
  return env
}

export function gatewayAgent(o: { botId: string; bun: string; repo: string; configPath: string; logsDir: string; env: Record<string, string> }): Agent {
  return {
    label: `com.dsh-bot.${o.botId}`,
    args: [o.bun, join(o.repo, 'gateway', 'src', 'main.ts'), '--config', o.configPath],
    cwd: o.repo,
    env: o.env,
    stdoutPath: join(o.logsDir, 'launchd.log'),
  }
}

export function directorAgent(o: { python: string; repo: string; chatId: string; root: string; env: Record<string, string> }): Agent {
  const dir = join(o.root, 'director')
  return {
    label: 'com.dsh-bot.director',
    args: [o.python, join(o.repo, 'director.py'), '--run'],
    cwd: o.repo,
    env: { ...o.env, HUB_CONFIGS_DIR: join(o.root, 'configs'), DIRECTOR_CHAT_ID: o.chatId, DIRECTOR_LOG_FILE: join(dir, 'director.log') },
    stdoutPath: join(dir, 'launchd.log'),
  }
}

/** 新系统接手的 Python 周边：每个 bot 的主动消息，以及所有 bot 共用的定时任务和服务（全部切完以后装） */
export function pythonAgent(o: { name: string; python: string; repo: string; script: string[]; root: string; env: Record<string, string>; extraEnv?: Record<string, string>; interval?: number; calendar?: Record<string, number> }): Agent {
  return {
    label: `com.dsh-bot.${o.name}`,
    // 经 run_logged.py 跑：输出写进按大小轮转的日志；launchd 的输出文件只会有崩溃时的几行
    args: [o.python, join(o.repo, 'scripts', 'run_logged.py'), join(o.root, 'logs', `${o.name}.log`), ...o.script.map((x, i) => (i === 0 ? join(o.repo, x) : x))],
    cwd: o.repo,
    env: { ...o.env, HUB_CONFIGS_DIR: join(o.root, 'configs'), ...o.extraEnv },
    stdoutPath: join(o.root, 'logs', `${o.name}.launchd.log`),
    ...(o.interval ? { interval: o.interval } : {}),
    ...(o.calendar ? { calendar: o.calendar } : {}),
  }
}

/** 共用的定时任务和服务（对应旧系统的同名 launchd 任务，跑新仓库里的同一批脚本）。
 *  旧系统的 daily-wildcard、cleanup 两个任务指向的脚本不在仓库里，没有接手，切换时照旧留着或停掉由用户定 */
export const SHARED: { name: string; script: string[]; interval?: number; calendar?: Record<string, number> }[] = [
  { name: 'jiwen', script: ['jiwen/tick.py'], interval: 300 },
  { name: 'memory-compactor', script: ['memory/memory_compactor.py'], calendar: { Weekday: 0, Hour: 4, Minute: 0 } },
  { name: 'moments-web', script: ['moments/web.py'] },
  { name: 'voicecall', script: ['voicecall/server.py'] },
]

/** install-shared 要装哪几项：不给 --only 就全装，给了就只装列出的。认不出的名字单独返回（要报错） */
export function pickShared(only: string[] | undefined): { items: typeof SHARED; unknown: string[] } {
  const unknown = (only ?? []).filter(n => !SHARED.some(j => j.name === n))
  return { items: SHARED.filter(x => !only || only.includes(x.name)), unknown }
}

/** 和旧系统的朋友圈网页、电话同时跑时换端口：把 --web-port/--call-port 翻译成对应服务的环境变量 */
export function sharedPortEnv(webPort?: string, callPort?: string): Record<string, Record<string, string>> {
  return {
    'moments-web': webPort ? { MOMENTS_WEB_PORT: webPort } : {},
    voicecall: callPort ? { VOICECALL_PORT: callPort } : {},
  }
}

const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')

export function plist(a: Agent): string {
  const strs = (xs: string[]) => xs.map(x => `    <string>${esc(x)}</string>`).join('\n')
  const env = Object.entries(a.env).map(([k, v]) => `    <key>${esc(k)}</key><string>${esc(v)}</string>`).join('\n')
  return `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${esc(a.label)}</string>
  <key>ProgramArguments</key>
  <array>
${strs(a.args)}
  </array>
  <key>WorkingDirectory</key><string>${esc(a.cwd)}</string>
  <key>EnvironmentVariables</key>
  <dict>
${env}
  </dict>
${a.interval ? `  <key>StartInterval</key><integer>${a.interval}</integer>
  <key>RunAtLoad</key><false/>` : a.calendar ? `  <key>StartCalendarInterval</key>
  <dict>
${Object.entries(a.calendar).map(([k, v]) => `    <key>${esc(k)}</key><integer>${v}</integer>`).join('\n')}
  </dict>
  <key>RunAtLoad</key><false/>` : `  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>`}
  <key>StandardOutPath</key><string>${esc(a.stdoutPath)}</string>
  <key>StandardErrorPath</key><string>${esc(a.stdoutPath)}</string>
</dict>
</plist>
`
}

export const plistPath = (home: string, label: string) => join(home, 'Library', 'LaunchAgents', `${label}.plist`)
export const binDir = (p: string) => dirname(p)

// ─── 安装前检查与状态解析（方案 3.10.1） ───

/**
 * 受保护目录：macOS 的隐私保护（TCC）不让 launchd 直接拉起的 python 读这些目录，
 * 仓库放在这里时开机自启的 python 任务会起不来（M6 真机：仓库在桌面上，jiwen 等任务全部失败）。
 */
export function defaultProtectedDirs(home: string): { name: string; path: string }[] {
  return [
    { name: '~/Desktop', path: join(home, 'Desktop') },
    { name: '~/Documents', path: join(home, 'Documents') },
    { name: '~/Downloads', path: join(home, 'Downloads') },
    { name: 'iCloud 云盘', path: join(home, 'Library', 'Mobile Documents') },
  ]
}

/**
 * 仓库在哪个受保护目录之下（含目录本身），返回那一项的 name；都不在返回 null。
 * 参数都是已解析好软链的 POSIX 绝对路径。macOS 默认文件系统不分大小写，所以比较也不分；
 * 按"整段目录"比较，~/Desktop2 不算 ~/Desktop 之下。
 */
export function protectedRepoDir(repo: string, protectedDirs: { name: string; path: string }[]): string | null {
  const norm = (p: string) => p.toLowerCase().replace(/\/+$/, '')
  const r = norm(repo)
  for (const d of protectedDirs) {
    const p = norm(d.path)
    if (r === p || r.startsWith(`${p}/`)) return d.name
  }
  return null
}

/**
 * 解析 `launchctl print gui/<uid>/<label>` 的输出。只看顶层（行首恰好一个制表符）的第一条
 * state / pid / last exit code：嵌套块（如 endpoints）里也有 state、pid，旧写法会误取。
 * 取等号后的全文：旧写法 `(\S+)` 会把 `not running` 截成 `not`。
 */
export function parseLaunchctlPrint(out: string, code: number): { state: string; pid: string | null; lastExit: string | null } {
  if (code !== 0) return { state: '没在运行（launchd 里没有这个任务）', pid: null, lastExit: null }
  const top: Record<string, string> = {}
  for (const line of out.split(/\r?\n/)) {
    const m = /^\t(state|pid|last exit code) =(.*)$/.exec(line)
    if (m && !(m[1]! in top)) top[m[1]!] = m[2]!.trim()
  }
  const last = top['last exit code']
  return {
    state: top.state ?? '?',
    pid: top.pid ?? null,
    lastExit: last === undefined ? null : last === '(never exited)' ? '从未退出' : last,
  }
}

/**
 * status 用：在 parseLaunchctlPrint 的基础上多取一个顶层 `runs = N`（"跑过几次"，定时任务看它）。
 * 单独一个函数，不改 parseLaunchctlPrint 的返回（验收测试锁定它的三个字段）。
 */
export function parseLaunchctlRuns(out: string, code: number): string | null {
  if (code !== 0) return null
  for (const line of out.split(/\r?\n/)) {
    const m = /^\truns =(.*)$/.exec(line)
    if (m) return m[1]!.trim()
  }
  return null
}

/** status 的一行：常驻的看是不是在跑；定时任务看跑过几次、上次退出码。有问题的标 ⚠️ 并给出看哪个日志 */
export function statusLine(
  label: string,
  s: { state: string; pid: string | null; lastExit: string | null },
  timed: boolean,
  runs: string | null,
  logHint: string,
): string {
  const gone = s.state.startsWith('没在运行（launchd 里没有这个任务）')
  const bad = s.lastExit !== null && s.lastExit !== '0'
  const parts = [`${s.state}${s.pid ? `（pid ${s.pid}）` : ''}`]
  if (timed) parts.push(runs ? `跑过 ${runs} 次` : '还没跑过')
  parts.push(`上次退出码 ${s.lastExit ?? '未知'}`)
  // 常驻的：在跑就行（被杀过一次又被拉起来的不算问题）；定时任务：上次没正常退出就要看
  const warn = gone || (timed ? bad : s.state !== 'running')
  return `${warn ? '⚠️' : '  '} ${label}：${timed ? '定时任务，' : ''}${parts.join('，')}${warn ? `。看日志：${logHint}` : ''}`
}

/** 某个自启项的日志在哪（status 出问题时提示） */
export function logHint(name: string, root = '~/.dsh-bot'): string {
  if (name === 'director') return `${root}/director/director.log`
  if (name.includes('.') || SHARED.some(j => j.name === name)) return `${root}/logs/${name}.log 和 ${name}.launchd.log`
  return `${root}/bots/${name}/logs/launchd.log`
}
