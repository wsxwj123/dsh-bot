// 双向切换：旧系统（claudebotlife）↔ 新系统（dsh 网关）。
//   bun gateway/scripts/switch.ts to-dsh <新 bot 名> [--config <配置文件>] [--old <旧 bot 名>] [--old-channel <旧频道目录>] [--no-stop-old] [--wait-seconds N] [--dry-run]
//   bun gateway/scripts/switch.ts back   <新 bot 名> [--config <配置文件>] [--old <旧 bot 名>] [--old-channel <旧频道目录>] [--wait-seconds N] [--dry-run]
//
// 按 docs/dsh-migration/CUTOVER.md 的逐个 bot 切换流程做。同一个 Telegram 令牌同一时刻只能有一个进程收消息：
// 先停一侧、确认停了、再起另一侧。动作前的检查只读；--dry-run 只打印，一步都不执行。
// 每一步失败立刻停，打印"已做/没做"和退回去的命令。Telegram 令牌之类的密钥脚本不读也不打印。
//
// back（新 → 旧）会把 dsh 期间的收获带回旧侧：最近一份摘要 + 最近的原话 + 长期记忆追加进旧 bot 的
// MEMORY.md（auto-memory 路径，只追加一个带日期的交接小节），没到点的承诺并进旧侧的 .promises.json。
import { existsSync, readFileSync } from 'fs'
import { homedir } from 'os'
import { join, resolve } from 'path'
import { Database } from 'bun:sqlite'
import { loadAccess, loadBotConfig, rootDir, type BotConfig } from '../src/config'
import {
  DEFAULT_WATCHDOG_PORTS, EMPTY_PAYLOAD, HEARTBEAT_FRESH_MS, nodeSwitchFs, oldChannelDirFor, parseWatchdogPorts,
  switchBack, switchToDsh, HANDOVER_QUOTES_MAX,
  type Ctx, type LedgerPayload, type Run, type SwitchDeps,
} from '../src/switch'

const HOME = homedir()
const REPO = resolve(import.meta.dir, '..', '..')

const [cmd, ...rest] = process.argv.slice(2)
const opt = (n: string) => { const i = rest.indexOf(`--${n}`); return i >= 0 ? rest[i + 1] : undefined }
const has = (n: string) => rest.includes(`--${n}`)

function usage(): number {
  console.log('用法：bun gateway/scripts/switch.ts to-dsh <新 bot 名> [--config <配置文件>] [--old <旧 bot 名>] [--old-channel <旧频道目录>] [--no-stop-old] [--wait-seconds N] [--dry-run]')
  console.log('      bun gateway/scripts/switch.ts back   <新 bot 名> [--config <配置文件>] [--old <旧 bot 名>] [--old-channel <旧频道目录>] [--wait-seconds N] [--dry-run]')
  console.log('  to-dsh：停旧 bot → 起新 bot 的网关与主动消息自启 → 等网关就绪 → 抽查提示')
  console.log('  back  ：停新 bot（plist 留着）→ 把摘要/记忆/承诺带回旧侧 → 起旧 bot → 抽查提示')
  console.log('  两个方向都能 --dry-run 先看一遍；手册见 docs/dsh-migration/CUTOVER.md')
  return 2
}

/** 外部命令：跑不起来（没有这个可执行文件）按 127 报，不抛 */
function makeRun(): Run {
  return (argv, extraEnv) => {
    try {
      const p = Bun.spawnSync(argv, { stdout: 'pipe', stderr: 'pipe', env: extraEnv ? { ...process.env, ...extraEnv } : process.env })
      return { code: p.exitCode ?? 1, out: (p.stdout.toString() + p.stderr.toString()).trim() }
    } catch (e) {
      return { code: 127, out: `${argv[0]} 跑不起来：${(e as Error).message}` }
    }
  }
}

/** 轮询等条件成立，最多等 seconds 秒（真 sleep；测试里是注入的假实现） */
function waitFor(cond: () => boolean, seconds: number): boolean {
  const deadline = Date.now() + seconds * 1000
  for (;;) {
    if (cond()) return true
    if (Date.now() >= deadline) return false
    Bun.sleepSync(1000)
  }
}

/** 日志里最后一条 gateway.ready 的时间戳（没有就 null）。只认网关日志里的这一行，不打印别的 */
function lastReadyLine(logPath: string): string | null {
  try {
    const text = readFileSync(logPath, 'utf8')
    let at: string | null = null
    for (const line of text.split('\n')) {
      if (!line.includes('"event":"gateway.ready"')) continue
      const m = /"at":"([^"]+)"/.exec(line)
      at = m ? m[1]! : '（时间没读出来）'
    }
    return at
  } catch {
    return null
  }
}

function makeWaitNewReady(cfg: BotConfig): (seconds: number) => { ok: boolean; note: string } {
  const hb = join(cfg.stateDir, 'heartbeat')
  const logPath = join(cfg.logsDir, 'gateway.log')
  const fs = nodeSwitchFs()
  return (seconds) => {
    const deadline = Date.now() + seconds * 1000
    for (;;) {
      const at = fs.mtimeMs(hb)
      const ready = lastReadyLine(logPath)
      if (at !== null && Date.now() - at < HEARTBEAT_FRESH_MS) {
        return { ok: true, note: ready ? `心跳新鲜；日志里最后一条 gateway.ready 在 ${ready}` : '心跳新鲜（日志里还没写到 gateway.ready）' }
      }
      if (Date.now() >= deadline) {
        return { ok: false, note: ready ? `心跳一直没新鲜过；日志里最后一条 gateway.ready 在 ${ready}` : '心跳一直没新鲜过，日志里也没有 gateway.ready' }
      }
      Bun.sleepSync(1000)
    }
  }
}

/** 只读账本，取出要带回旧侧的三样东西（什么都不写、什么都不打印内容） */
function makeReadLedger(cfg: BotConfig): () => LedgerPayload {
  return () => {
    const path = join(cfg.stateDir, 'ledger.sqlite')
    if (!existsSync(path)) return EMPTY_PAYLOAD
    let db: Database | null = null
    try {
      db = new Database(path, { readonly: true }) // 网关还在跑时也能读；WAL 下读到的是已提交的那份
      const chat = cfg.gw.owners[0] ?? loadAccess(cfg.channelDir).allowFrom[0] ?? ''
      if (!chat) return EMPTY_PAYLOAD
      const seg = db.query('SELECT summary, created_at, closed_at FROM segments WHERE chat_id = ? AND summary IS NOT NULL AND summary <> \'\' ORDER BY id DESC LIMIT 1').get(chat) as { summary: string } | null
      const range = db.query('SELECT MIN(created_at) AS a, MAX(COALESCE(closed_at, last_used_at, created_at)) AS b FROM segments WHERE chat_id = ?').get(chat) as { a: number | null; b: number | null } | null
      const userRows = db.query('SELECT text, ts FROM inbound WHERE chat_id = ? AND state = \'done\' AND text IS NOT NULL ORDER BY id DESC LIMIT 24').all(chat) as { text: string; ts: number }[]
      // outbound 没有 ts 列：用发出时刻（sent_at，兜底 created_at）
      const botRows = db.query('SELECT text, COALESCE(sent_at, created_at) AS ts FROM outbound WHERE chat_id = ? AND kind = \'text\' AND state = \'sent\' AND text IS NOT NULL ORDER BY id DESC LIMIT 24').all(chat) as { text: string; ts: number }[]
      const memRows = db.query('SELECT text, at FROM memories WHERE chat_id IS NULL OR chat_id = ? ORDER BY id ASC').all(chat) as { text: string; at: number }[]
      const pending = db.query('SELECT chat_id, text, quote, due_at FROM commitments WHERE state = \'pending\' ORDER BY due_at ASC').all() as { chat_id: string; text: string; quote: string | null; due_at: number }[]
      const quotes = [
        ...userRows.map(r => ({ who: 'user' as const, text: r.text, ts: r.ts })),
        ...botRows.map(r => ({ who: 'bot' as const, text: r.text, ts: r.ts })),
      ].sort((a, b) => a.ts - b.ts).slice(-HANDOVER_QUOTES_MAX)
        .map(q => ({ who: q.who, text: q.text.replace(/\s+/g, ' ').trim().slice(0, 160) }))
      return {
        chatId: chat,
        summary: seg?.summary ?? null,
        quotes,
        memories: memRows.map(m => ({ text: m.text, at: m.at })),
        pending: pending.filter(p => p.text?.trim()).map(p => ({ chatId: p.chat_id, text: p.text.trim(), quote: (p.quote ?? p.text).trim(), dueAt: p.due_at })),
        firstAt: range?.a ?? null,
        lastAt: range?.b ?? null,
      }
    } catch (e) {
      console.error(`  读账本失败（${(e as Error).name}），这次不带走摘要/记忆/承诺，其它照常`)
      return EMPTY_PAYLOAD
    } finally {
      if (db) db.close()
    }
  }
}

function main(): number {
  if (process.platform !== 'darwin') { console.log('切换脚本目前只支持 macOS（launchd 管新侧的自启）。其他系统请手动停/起两边。'); return 2 }
  if (cmd !== 'to-dsh' && cmd !== 'back') return usage()
  const bot = rest[0] // bot 名写在最前面，和 autostart.ts 的 <配置文件> 同一个位置约定
  if (!bot || bot.startsWith('--')) return usage()
  if (!/^[A-Za-z0-9_-]+$/.test(bot)) { console.log(`bot 名只能用字母、数字、下划线和连字符：${bot}`); return 2 }

  const cfgPath = resolve((opt('config') ?? join(rootDir(), 'configs', `${bot}.yml`)).replace(/^~(?=\/)/, HOME))
  if (!existsSync(cfgPath)) { console.log(`没有配置文件 ${cfgPath}。先按 CUTOVER.md 准备好这个 bot（setup.ts bot / setup.ts check），或显式 --config 指路。`); return 2 }
  let cfg: BotConfig
  try { cfg = loadBotConfig(cfgPath) } catch (e) { console.log(`配置读不了：${(e as Error).message}`); return 2 }

  const fs = nodeSwitchFs()
  const oldName = opt('old') ?? cfg.lifeId
  if (!/^[A-Za-z0-9_-]+$/.test(oldName)) { console.log(`旧 bot 名只能用字母、数字、下划线和连字符：${oldName}（用 --old 指定）`); return 2 }
  const oldChannelDir = resolve((opt('old-channel') ?? oldChannelDirFor(HOME, oldName, fs)).replace(/^~(?=\/)/, HOME))
  const waitSeconds = Number(opt('wait-seconds') ?? '90')
  if (!Number.isInteger(waitSeconds) || waitSeconds < 5 || waitSeconds > 1800) { console.log('--wait-seconds 要在 5 到 1800 之间'); return 2 }

  const ctx: Ctx = {
    bot: cfg.id,
    oldName,
    oldChannelDir,
    configPath: cfgPath,
    repo: REPO,
    bun: Bun.which('bun') ?? process.execPath,
    bash: Bun.which('bash') ?? '/bin/bash',
    tmux: Bun.which('tmux'),
    nc: Bun.which('nc'),
    newBotDir: join(rootDir(), 'bots', cfg.id),
    ports: parseWatchdogPorts(process.env.WATCHDOG_PORTS ?? DEFAULT_WATCHDOG_PORTS),
  }
  const deps: SwitchDeps = {
    home: HOME,
    uid: process.getuid?.() ?? 0,
    fs,
    run: makeRun(),
    print: (line) => console.log(line),
    now: () => Date.now(),
    waitFor,
    waitNewReady: makeWaitNewReady(cfg),
    readLedger: makeReadLedger(cfg),
  }
  if (!fs.exists(oldChannelDir)) console.log(`  ⚠️  旧侧频道目录不在：${oldChannelDir}（--old-channel 可以指定）`)

  const dryRun = has('dry-run')
  return cmd === 'to-dsh'
    ? switchToDsh(ctx, deps, { dryRun, stopOld: !has('no-stop-old'), waitSeconds })
    : switchBack(ctx, deps, { dryRun, waitSeconds })
}

process.exit(main())
