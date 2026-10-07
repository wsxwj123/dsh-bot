// 开机自启（macOS launchd）。网关、导演各一个 LaunchAgent：开机自动起来，退出了 10 秒后自动拉起。
//   bun gateway/scripts/autostart.ts install <配置文件>              这个 bot 的网关开机自启
//   bun gateway/scripts/autostart.ts install-director --chat <群 id>  导演开机自启
//   bun gateway/scripts/autostart.ts install-jobs <配置文件>         这个 bot 的主动消息（每 10 分钟看一次要不要开口）
//   bun gateway/scripts/autostart.ts install-shared [--botlife-db <朋友圈库>]
//                                                                    全部 bot 切完以后：情绪、记忆整理、朋友圈网页、电话
//   bun gateway/scripts/autostart.ts uninstall <bot 名 | director>   停掉并取消开机自启
//   bun gateway/scripts/autostart.ts status                          列出新系统的开机自启项、状态、定时任务上次的退出码
// 仓库和 ~/.dsh-bot 都不能放在桌面、文稿、下载、iCloud 云盘下（macOS 的隐私保护会拦住 launchd 拉起的程序），安装时会检查
// 看实时日志：bun gateway/scripts/logs.ts --config <配置文件> -f（导演：tail -f ~/.dsh-bot/director/director.log）
import { chmodSync, existsSync, mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'fs'
import { homedir } from 'os'
import { dirname, join, resolve } from 'path'
import { baseEnv, directorAgent, gatewayAgent, logHint, parseLaunchctl, plist, plistPath, protectedDir, PROXY_VARS, pythonAgent, SHARED, statusLine, type Agent } from '../src/autostart'
import { loadAccess, loadBotConfig, rootDir } from '../src/config'

const [cmd, ...rest] = process.argv.slice(2)
const opt = (n: string) => { const i = rest.indexOf(`--${n}`); return i >= 0 ? rest[i + 1] : undefined }
const REPO = resolve(import.meta.dir, '..', '..')
const HOME = homedir()
const uid = () => process.getuid?.() ?? 0

function launchctl(...args: string[]): { code: number; out: string } {
  const p = Bun.spawnSync(['launchctl', ...args], { stdout: 'pipe', stderr: 'pipe' })
  return { code: p.exitCode ?? 1, out: (p.stdout.toString() + p.stderr.toString()).trim() }
}

function env(): Record<string, string> {
  const bins = [Bun.which('bun'), Bun.which('node'), Bun.which('python3')].filter((x): x is string => !!x).map(p => dirname(p))
  for (const k of PROXY_VARS) if (process.env[k]?.includes('@')) console.log(`  ⚠️  ${k} 里带账号密码，没有写进开机自启的配置；需要的话请手动加`)
  return baseEnv({ home: HOME, dshBotHome: process.env.DSH_BOT_HOME, extraPath: bins, env: process.env })
}

function install(a: Agent): number {
  const p = plistPath(HOME, a.label)
  mkdirSync(dirname(p), { recursive: true })
  mkdirSync(dirname(a.stdoutPath), { recursive: true, mode: 0o700 })
  writeFileSync(p, plist(a))
  chmodSync(p, 0o600)
  launchctl('bootout', `gui/${uid()}/${a.label}`) // 已经装过就先停掉旧的
  const r = launchctl('bootstrap', `gui/${uid()}`, p)
  if (r.code !== 0) { console.log(`  ⚠️  launchctl 没能启动它：${r.out}`); return 1 }
  console.log(`  ✅ 已设为开机自启并启动：${a.label}`)
  return 0
}

/** 仓库或 ~/.dsh-bot 在桌面、文稿、下载、iCloud 云盘下时拒绝安装：macOS 的隐私保护会拦住 launchd 直接拉起的程序 */
function blockedByPrivacy(): boolean {
  for (const [what, p] of [['仓库', REPO], ['新系统目录', rootDir()]] as const) {
    const d = protectedDir(p, HOME)
    if (!d) continue
    console.log(`  ❌ ${what}在 ~/${d} 下面（${p.replace(HOME, '~')}）。macOS 的隐私保护会拦住开机自启拉起的程序读这个目录，`)
    console.log(`     主动消息、导演这类任务会一启动就失败（日志里是 getcwd: Operation not permitted）。`)
    console.log(`     请先把它挪到家目录下（比如 ~/dsh-bot-work），在新位置重新运行这条命令。没有安装任何东西。`)
    return true
  }
  return false
}

function main(): number {
  if (process.platform !== 'darwin') { console.log('开机自启目前只支持 macOS（launchd）。其他系统请用 tmux 或系统自己的服务管理。'); return 2 }
  if (cmd?.startsWith('install') && blockedByPrivacy()) return 1
  if (cmd === 'install') {
    const cfgPath = rest[0]
    if (!cfgPath) { console.log('用法：autostart.ts install <配置文件>'); return 2 }
    const cfg = loadBotConfig(cfgPath)
    const bun = Bun.which('bun') ?? process.execPath
    return install(gatewayAgent({ botId: cfg.id, bun, repo: REPO, configPath: resolve(cfgPath), logsDir: cfg.logsDir, env: env() }))
  }
  if (cmd === 'install-director') {
    const chat = opt('chat')
    if (!chat || !/^-\d+$/.test(chat)) { console.log('用法：autostart.ts install-director --chat <群 id（负数）>'); return 2 }
    const py = Bun.which('python3')
    if (!py) { console.log('没找到 python3'); return 1 }
    return install(directorAgent({ python: py, repo: REPO, chatId: chat, root: rootDir(), env: env() }))
  }
  if (cmd === 'install-jobs') {
    const cfgPath = rest[0]
    if (!cfgPath) { console.log('用法：autostart.ts install-jobs <配置文件>'); return 2 }
    const cfg = loadBotConfig(cfgPath)
    const owner = cfg.gw.owners[0] ?? loadAccess(cfg.channelDir).allowFrom[0]
    const py = Bun.which('python3')
    if (!py || !owner) { console.log(py ? '找不到主人（access.json 的 allowFrom 是空的）' : '没找到 python3'); return 1 }
    return install(pythonAgent({ name: `self-initiate.${cfg.id}`, python: py, repo: REPO, script: ['scripts/self_initiate.py', cfg.id, owner], root: rootDir(), env: env(), interval: 600 }))
  }
  if (cmd === 'install-shared') {
    const py = Bun.which('python3')
    if (!py) { console.log('没找到 python3'); return 1 }
    const db = opt('botlife-db')
    const extraEnv = db ? { BOTLIFE_STATE_DB: resolve(db.replace(/^~/, HOME)) } : undefined
    let bad = 0
    for (const j of SHARED) bad += install(pythonAgent({ name: j.name, python: py, repo: REPO, script: j.script, root: rootDir(), env: env(), extraEnv, interval: j.interval, calendar: j.calendar }))
    return bad ? 1 : 0
  }
  if (cmd === 'uninstall') {
    const name = rest[0]
    if (!name) { console.log('用法：autostart.ts uninstall <bot 名 | director | self-initiate.<bot 名> | jiwen | moments-web …>'); return 2 }
    const label = `com.dsh-bot.${name}`
    launchctl('bootout', `gui/${uid()}/${label}`)
    const p = plistPath(HOME, label)
    if (existsSync(p)) rmSync(p)
    console.log(`  ✅ 已停掉并取消开机自启：${label}`)
    return 0
  }
  if (cmd === 'status') {
    const dir = join(HOME, 'Library', 'LaunchAgents')
    const labels = existsSync(dir) ? readdirSync(dir).filter(f => f.startsWith('com.dsh-bot.') && f.endsWith('.plist')).map(f => f.slice(0, -6)) : []
    if (labels.length === 0) console.log('还没有设开机自启的项。')
    let bad = 0
    for (const l of labels) {
      const r = launchctl('print', `gui/${uid()}/${l}`)
      const timed = /<key>Start(Calendar)?Interval<\/key>/.test(readFileSync(join(dir, `${l}.plist`), 'utf8'))
      const line = statusLine(l, parseLaunchctl(r.out, r.code), timed, logHint(l.slice('com.dsh-bot.'.length)))
      if (line.startsWith('⚠️')) bad++
      console.log(line)
    }
    if (bad) console.log(`\n${bad} 项有问题，见上面标 ⚠️ 的行。`)
    return bad ? 1 : 0
  }
  console.log('用法：bun gateway/scripts/autostart.ts install <配置文件> | install-director --chat <群 id> | install-jobs <配置文件> | install-shared [--botlife-db <路径>] | uninstall <名字> | status')
  return 2
}

process.exit(main())
