// 开机自启（macOS launchd）。网关、导演各一个 LaunchAgent：开机自动起来，退出了 10 秒后自动拉起。
//   bun gateway/scripts/autostart.ts install <配置文件>              这个 bot 的网关开机自启
//   bun gateway/scripts/autostart.ts install-director --chat <群 id>  导演开机自启
//   bun gateway/scripts/autostart.ts install-jobs <配置文件>         这个 bot 的主动消息（每 10 分钟看一次要不要开口）
//   bun gateway/scripts/autostart.ts install-shared [--botlife-db <朋友圈库>] [--only jiwen,memory-compactor] [--web-port N] [--call-port N]
//                                                                    全部 bot 切完以后：情绪、记忆整理、朋友圈网页、电话
//                                                                    （切换期间可以先 --only jiwen,memory-compactor：只管新系统的 bot）
//   bun gateway/scripts/autostart.ts uninstall <bot 名 | director>   停掉并取消开机自启
//   bun gateway/scripts/autostart.ts status                          列出新系统的开机自启项和状态
// 看实时日志：bun gateway/scripts/logs.ts --config <配置文件> -f（导演：tail -f ~/.dsh-bot/director/director.log）
import { chmodSync, existsSync, mkdirSync, readdirSync, realpathSync, rmSync, writeFileSync } from 'fs'
import { homedir } from 'os'
import { dirname, join, resolve } from 'path'
import { baseEnv, defaultProtectedDirs, directorAgent, gatewayAgent, parseLaunchctlPrint, pickShared, plist, plistPath, protectedRepoDir, PROXY_VARS, pythonAgent, SHARED, sharedPortEnv, type Agent } from '../src/autostart'
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

/**
 * 仓库在桌面、文稿、下载或 iCloud 云盘下时拒绝安装（方案 3.10.1）：macOS 的隐私保护会拦住 launchd 直接拉起的
 * python 任务，装上了也起不来。两边都先解析软链再比，免得经软链绕过或误拦。
 */
function repoBlocked(): boolean {
  const real = (p: string) => { try { return realpathSync(p) } catch { return p } }
  const repo = real(REPO)
  const hit = protectedRepoDir(repo, defaultProtectedDirs(HOME).map(d => ({ name: d.name, path: real(d.path) })))
  if (!hit) return false
  console.log(`  ⛔ 仓库在 ${hit} 下（${repo}）。macOS 的隐私保护会拦住 launchd 直接拉起的 python 任务，开机自启会失败。`)
  console.log('     请把仓库移到家目录根下（例如 ~/dsh-bot），再重新安装。')
  return true
}

function main(): number {
  if (process.platform !== 'darwin') { console.log('开机自启目前只支持 macOS（launchd）。其他系统请用 tmux 或系统自己的服务管理。'); return 2 }
  if (['install', 'install-director', 'install-jobs', 'install-shared'].includes(cmd ?? '') && repoBlocked()) return 1
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
    // --only：只装其中几项。切换期间情绪、记忆整理可以先装（只管新系统的 bot，和旧系统的同名任务互不干扰）
    const only = opt('only')?.split(',').map(x => x.trim()).filter(Boolean)
    const { items, unknown } = pickShared(only)
    if (unknown.length) { console.log(`不认识：${unknown.join('、')}。能装的有：${SHARED.map(j => j.name).join('、')}`); return 2 }
    // --web-port、--call-port：和旧系统的朋友圈网页（8765）、电话（8766）同时跑时换个端口
    const ports = sharedPortEnv(opt('web-port'), opt('call-port'))
    let bad = 0
    for (const j of items) {
      const extraEnv = { ...(db ? { BOTLIFE_STATE_DB: resolve(db.replace(/^~/, HOME)) } : {}), ...ports[j.name] }
      bad += install(pythonAgent({ name: j.name, python: py, repo: REPO, script: j.script, root: rootDir(), env: env(), extraEnv, interval: j.interval, calendar: j.calendar }))
    }
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
    for (const l of labels) {
      const r = launchctl('print', `gui/${uid()}/${l}`)
      const s = parseLaunchctlPrint(r.out, r.code)
      console.log(`  ${l}：${s.state}${s.pid ? `（pid ${s.pid}）` : ''}（上次退出码 ${s.lastExit ?? '未知'}）`)
    }
    return 0
  }
  console.log('用法：bun gateway/scripts/autostart.ts install <配置文件> | install-director --chat <群 id> | install-jobs <配置文件> | install-shared [--botlife-db <路径>] [--only 名字,名字] [--web-port N] [--call-port N] | uninstall <名字> | status')
  return 2
}

process.exit(main())
