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
