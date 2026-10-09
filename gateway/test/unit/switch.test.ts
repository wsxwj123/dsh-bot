// 双向切换（src/switch.ts）的白盒单测：假的 launchctl / stop-bot.sh / start-bot.sh 记录器，
// 断言两个方向的动作顺序与"先停后起"、--dry-run 不产生动作、互斥中止、幂等，
// 以及 back 方向带回旧侧的三样东西（摘要/记忆/承诺）写对位置、重复跑不重复追加。
import { expect, test } from 'bun:test'
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { dirname, join } from 'path'
import {
  EMPTY_PAYLOAD, ENABLED_FALSE_LINE, editEnabledYml, enabledValueOf, nodeSwitchFs, oldMemoryPath, oldPromisesPath,
  switchBack, switchToDsh,
  type Ctx, type LedgerPayload, type Run, type SwitchDeps, type SwitchFs,
} from '../../src/switch'

const UID = 501
const BOT = 'bot5'
const OLD = 'yasuna'
const NOW = Date.parse('2026-10-09T12:00:00+08:00')
const GW = `com.dsh-bot.${BOT}`
const JOBS = `com.dsh-bot.self-initiate.${BOT}`
const DIRECTOR = 'com.dsh-bot.director'

/** 内存文件系统：够 src/switch.ts 用，顺带能断言"写了哪个路径、写了什么" */
class MapFs implements SwitchFs {
  files = new Map<string, { text: string; mtime: number }>()
  launchAgents: string[] = []
  /** 写过的路径（断言"没写"用） */
  writes: string[] = []
  /** 等于某个路径时 write 抛错（模拟配置写不进去） */
  failWrite = ''
  /** 停用标记的真相在 world.disabled（stop-bot.sh 会翻它），MapFs 现读它 */
  marker = ''
  world: World | null = null
  heartbeatPath = ''
  exists(p: string): boolean { return (this.marker !== '' && p === this.marker) ? !!this.world!.disabled : this.files.has(p) }
  read(p: string): string | null { return this.files.get(p)?.text ?? null }
  write(p: string, text: string, _mode?: number): void {
    if (this.failWrite === p) throw new Error('模拟写不进去')
    this.writes.push(p)
    this.files.set(p, { text, mtime: NOW })
  }
  mkdirp(_p: string): void {}
  mtimeMs(p: string): number | null { return p === this.heartbeatPath ? this.world!.heartbeatAt : this.files.get(p)?.mtime ?? null }
  list(_p: string): string[] { return this.launchAgents }
}

type LaunchdEntry = { installed: boolean; running: boolean; pid?: string }

type World = {
  home: string
  disabled: boolean
  oldSessions: string[]
  oldPortOpen: boolean
  launchd: Record<string, LaunchdEntry>
  heartbeatAt: number | null
  calls: string[][]
  out: string[]
  /** stop-bot.sh 打桩：跑成功但不清运行痕迹（模拟"停不干净"） */
  stopNoEffect: boolean
  /** bootout 打桩：跑成功但 launchd 里还是 running（模拟"停不掉"） */
  bootoutNoEffect: boolean
}

function makeWorld(o: Partial<World> = {}): World {
  return {
    home: '/tmp/fake-home', disabled: false, oldSessions: ['tg-yasuna-dispatcher'], oldPortOpen: true,
    launchd: { [GW]: { installed: true, running: true, pid: '4242' }, [JOBS]: { installed: true, running: false } },
    heartbeatAt: NOW, calls: [], out: [], stopNoEffect: false, bootoutNoEffect: false, ...o,
  }
}

function fakeRun(w: World): Run {
  return (argv) => {
    w.calls.push(argv)
    const s = argv.join(' ')
    const label = (a: string) => a.split('/').pop()!
    if (argv[0]!.endsWith('tmux')) return { code: 0, out: w.oldSessions.join('\n') }
    if (argv[0]!.endsWith('nc')) return { code: w.oldPortOpen ? 0 : 1, out: '' }
    if (argv[0] === 'launchctl' && argv[1] === 'print') {
      const l = label(argv[2]!)
      const e = w.launchd[l]
      if (!e?.installed) return { code: 113, out: `Could not find service "${l}" in domain for user gui: ${UID}` }
      return { code: 0, out: `gui/${UID}/${l} = {\n\tstate = ${e.running ? 'running' : 'not running'}\n\tpid = ${e.pid ?? 0}\n}\n` }
    }
    if (argv[0] === 'launchctl' && argv[1] === 'bootout') {
      const l = label(argv[2]!)
      const e = w.launchd[l]
      if (!e?.installed) return { code: 3, out: `Could not find service "${l}" in domain for user gui: ${UID}` }
      if (!w.bootoutNoEffect) {
        w.launchd[l] = { installed: false, running: false }
        if (l === GW) w.heartbeatAt = null // 网关进程被杀，心跳也就不写了
      }
      return { code: 0, out: '' }
    }
    if (s.endsWith(`stop-bot.sh ${OLD}`)) {
      w.disabled = true
      if (!w.stopNoEffect) { w.oldSessions = []; w.oldPortOpen = false }
      return { code: 0, out: `✓ 已停用 ${OLD}` }
    }
    if (s.endsWith(`start-bot.sh ${OLD}`)) {
      w.disabled = false
      w.oldSessions = [`tg-${OLD}-dispatcher`]
      w.oldPortOpen = true
      return { code: 0, out: '→ 跑 restart-bots.sh 全栈重启...' }
    }
    if (s.includes('autostart.ts install-jobs')) { w.launchd[JOBS] = { installed: true, running: false }; return { code: 0, out: '✅ 已设为开机自启并启动' } }
    if (s.includes('autostart.ts install ')) { w.launchd[GW] = { installed: true, running: true, pid: '4242' }; return { code: 0, out: '✅ 已设为开机自启并启动' } }
    return { code: 127, out: `没打桩：${s}` }
  }
}

function makeCtx(home: string): Ctx {
  return {
    bot: BOT, oldName: OLD, oldChannelDir: join(home, '.claude', 'channels', 'telegram'),
    configPath: join(home, 'configs', `${BOT}.yml`), repo: '/repo', bun: '/bun', bash: '/bin/bash',
    tmux: '/usr/bin/tmux', nc: '/usr/bin/nc', newBotDir: join(home, '.dsh-bot', 'bots', BOT), ports: { [OLD]: 17801 },
  }
}

function makeDeps(w: World, o: { fs?: SwitchFs; ledger?: LedgerPayload; ready?: boolean } = {}): SwitchDeps {
  const fs = o.fs ?? new MapFs()
  if (fs instanceof MapFs) {
    fs.marker = join(w.home, '.claude', 'dispatcher', '.disabled', OLD)
    fs.world = w
    // 心跳文件的新鲜度现读 world.heartbeatAt：probeNew 靠它认出"网关还在写"，bootout 之后它就没了
    fs.heartbeatPath = join(w.home, '.dsh-bot', 'bots', BOT, 'state', 'heartbeat')
  }
  return {
    home: w.home, uid: UID, fs, run: fakeRun(w),
    print: (l) => w.out.push(l),
    now: () => NOW,
    waitFor: (cond) => cond(), // 假世界是同步的：直接看条件成不成立
    waitNewReady: () => ({ ok: o.ready ?? true, note: '心跳新鲜' }),
    readLedger: () => o.ledger ?? EMPTY_PAYLOAD,
  }
}

/** 会改本机状态的那些动作（探活命令不算） */
const mutations = (w: World) => w.calls.map(c => c.join(' '))
  .filter(s => s.includes('stop-bot.sh') || s.includes('start-bot.sh') || s.includes('autostart.ts') || s.includes('launchctl bootout'))
const printed = (w: World) => w.out.join('\n')

/* ── to-dsh ───────────────────────────────────────────── */

test('to-dsh：旧侧在跑 → 先 stop-bot.sh、确认停了、再装新侧自启（顺序不能反）', () => {
  const w = makeWorld()
  const code = switchToDsh(makeCtx(w.home), makeDeps(w), { dryRun: false, stopOld: true, waitSeconds: 60 })
  expect(code).toBe(0)
  const m = mutations(w)
  expect(m[0]).toContain(`stop-bot.sh ${OLD}`)
  expect(m[1]).toContain('autostart.ts install ')
  expect(m[2]).toContain('autostart.ts install-jobs')
  expect(w.disabled).toBe(true)
  expect(w.launchd[GW]).toMatchObject({ installed: true, running: true })
  expect(printed(w)).toContain('确认旧侧已停')
})

test('to-dsh：stop-bot.sh 跑完旧侧还在跑 → 中止，一格新侧动作都不做', () => {
  const w = makeWorld({ stopNoEffect: true })
  const code = switchToDsh(makeCtx(w.home), makeDeps(w), { dryRun: false, stopOld: true, waitSeconds: 60 })
  expect(code).toBe(1)
  expect(mutations(w).filter(s => s.includes('autostart.ts'))).toEqual([])
  expect(printed(w)).toContain('不敢起新侧')
  expect(printed(w)).toContain(`start-bot.sh ${OLD}`) // 退回去的办法要打出来
})

test('to-dsh：--no-stop-old 且旧侧还在跑 → 默认中止（互斥），一步不动', () => {
  const w = makeWorld()
  const code = switchToDsh(makeCtx(w.home), makeDeps(w), { dryRun: false, stopOld: false, waitSeconds: 60 })
  expect(code).toBe(1)
  expect(mutations(w)).toEqual([])
  expect(w.disabled).toBe(false)
  expect(printed(w)).toContain('什么都没做')
})

test('to-dsh：旧侧没在跑 → 只提示，不调 stop-bot.sh；重复执行不报错（幂等）', () => {
  const w = makeWorld({ disabled: true, oldSessions: [], oldPortOpen: false })
  const ctx = makeCtx(w.home)
  expect(switchToDsh(ctx, makeDeps(w), { dryRun: false, stopOld: true, waitSeconds: 60 })).toBe(0)
  expect(mutations(w).filter(s => s.includes('stop-bot.sh'))).toEqual([])
  const first = mutations(w).length
  expect(switchToDsh(ctx, makeDeps(w), { dryRun: false, stopOld: true, waitSeconds: 60 })).toBe(0)
  expect(mutations(w).length).toBe(first + 2) // 第二遍只有两次 install，没有 stop-bot
  expect(printed(w)).toContain('没在跑，跳过 stop-bot.sh')
})

test('to-dsh：--dry-run 只打印，不产生任何动作', () => {
  const w = makeWorld()
  const code = switchToDsh(makeCtx(w.home), makeDeps(w), { dryRun: true, stopOld: true, waitSeconds: 60 })
  expect(code).toBe(0)
  expect(mutations(w)).toEqual([])
  const text = printed(w)
  expect(text).toContain('--dry-run')
  expect(text).toContain(`stop-bot.sh ${OLD}`)
  expect(text).toContain('autostart.ts install ')
  expect(text).toContain('这次没动任何东西')
})

test('to-dsh：网关迟迟不就绪 → 退出码 1，但自启已经装上了，提示去哪看', () => {
  const w = makeWorld({ disabled: true, oldSessions: [], oldPortOpen: false })
  const code = switchToDsh(makeCtx(w.home), makeDeps(w, { ready: false }), { dryRun: false, stopOld: true, waitSeconds: 30 })
  expect(code).toBe(1)
  expect(printed(w)).toContain('还没确认网关就绪')
  expect(printed(w)).toContain('logs.ts')
})

/* ── back ─────────────────────────────────────────────── */

test('back：先停新侧（bootout，plist 不删）→ 带回旧侧 → 最后才起旧侧', () => {
  const w = makeWorld()
  w.launchd[DIRECTOR] = { installed: true, running: true, pid: '9' }
  const ledger: LedgerPayload = {
    chatId: '533', summary: '一段摘要', quotes: [{ who: 'user', text: '在吗' }],
    memories: [{ text: '他喜欢兔子套装', at: NOW }], pending: [], firstAt: NOW - 86400_000, lastAt: NOW,
  }
  const code = switchBack(makeCtx(w.home), makeDeps(w, { ledger }), { dryRun: false, waitSeconds: 30 })
  expect(code).toBe(0)
  const m = mutations(w)
  expect(m[0]).toBe(`launchctl bootout gui/${UID}/${GW}`)
  expect(m[1]).toBe(`launchctl bootout gui/${UID}/${JOBS}`)
  expect(m[2]).toBe(`launchctl bootout gui/${UID}/${DIRECTOR}`)
  expect(m[3]).toContain(`start-bot.sh ${OLD}`)
  expect(w.oldSessions).toEqual([`tg-${OLD}-dispatcher`])
  expect(w.launchd[GW]).toMatchObject({ installed: false, running: false })
  expect(printed(w)).toContain('确认新侧已停')
})

test('back：新系统里还有别的 bot 在跑 → 共用的导演不停，只提示', () => {
  const w = makeWorld()
  w.launchd['com.dsh-bot.bot3'] = { installed: true, running: true, pid: '7' }
  const fs = new MapFs()
  fs.launchAgents = ['com.dsh-bot.bot5.plist', 'com.dsh-bot.bot3.plist', 'com.dsh-bot.jiwen.plist', 'com.dsh-bot.director.plist']
  w.launchd[DIRECTOR] = { installed: true, running: true }
  const code = switchBack(makeCtx(w.home), makeDeps(w, { fs }), { dryRun: false, waitSeconds: 30 })
  expect(code).toBe(0)
  expect(w.launchd[DIRECTOR]).toMatchObject({ installed: true, running: true })
  expect(printed(w)).toContain('导演（所有新系统 bot 共用的）没停')
  expect(mutations(w)).not.toContain(`launchctl bootout gui/${UID}/${DIRECTOR}`)
})

test('back：新侧网关是手动起的（心跳新鲜、launchd 里没有）→ 中止，不停旧侧、不写数据', () => {
  const w = makeWorld()
  delete w.launchd[GW]
  const code = switchBack(makeCtx(w.home), makeDeps(w), { dryRun: false, waitSeconds: 30 })
  expect(code).toBe(1)
  expect(mutations(w)).toEqual([])
  expect(w.oldSessions).toEqual(['tg-yasuna-dispatcher']) // 没动过
  expect(printed(w)).toContain('不是 launchd 起的')
})

test('back：bootout 之后新侧还活着 → 不敢起旧侧，什么都不写', () => {
  const w = makeWorld({ bootoutNoEffect: true })
  const code = switchBack(makeCtx(w.home), makeDeps(w), { dryRun: false, waitSeconds: 30 })
  expect(code).toBe(1)
  expect(mutations(w).some(s => s.includes('start-bot.sh'))).toBe(false)
  expect(w.oldSessions).toEqual(['tg-yasuna-dispatcher'])
  expect(printed(w)).toContain('不敢起旧侧')
})

test('back：--dry-run 只打印（含 bootout 与带回旧侧的目标路径），不产生动作', () => {
  const w = makeWorld()
  const ledger: LedgerPayload = { ...EMPTY_PAYLOAD, chatId: '533', summary: '摘要', pending: [{ chatId: '533', text: '等他回家', quote: '', dueAt: NOW + 3_600_000 }] }
  const code = switchBack(makeCtx(w.home), makeDeps(w, { ledger }), { dryRun: true, waitSeconds: 30 })
  expect(code).toBe(0)
  expect(mutations(w)).toEqual([])
  const text = printed(w)
  expect(text).toContain(`launchctl bootout gui/${UID}/${GW}`)
  expect(text).toContain('MEMORY.md')
  expect(text).toContain('.promises.json')
  expect(text).toContain('这次没动任何东西')
})

/* ── back 的数据搬运：真文件、真路径、跑两遍不重复 ─────────── */

function realWorld(): { root: string; home: string; ch: string; cleanup: () => void } {
  const root = mkdtempSync(join(tmpdir(), 'switch-'))
  const home = join(root, 'home')
  const ch = join(home, '.claude', 'channels', 'telegram')
  return { root, home, ch, cleanup: () => rmSync(root, { recursive: true, force: true }) }
}

test('back：摘要/记忆追加进旧 MEMORY.md（只加一个小节、不动原段落），承诺并进旧 .promises.json', () => {
  const r = realWorld()
  try {
    const w = makeWorld({ home: r.home, disabled: false, oldSessions: ['tg-yasuna-dispatcher'], oldPortOpen: true })
    const memPath = oldMemoryPath(r.home, r.ch)
    mkdirSync(dirname(memPath), { recursive: true })
    writeFileSync(memPath, '# Memory\n\n## 关于用户\n- 名字：他\n')
    const prPath = oldPromisesPath(r.ch)
    mkdirSync(r.ch, { recursive: true })
    writeFileSync(prPath, JSON.stringify({ 'p-old': { chat_id: '533', due_at: 1, gist: '旧事', state: 'pending' } }))
    const ledger: LedgerPayload = {
      chatId: '533', summary: '这段时间聊了兔子套装和例假', quotes: [{ who: 'user', text: '在吗' }, { who: 'bot', text: '在的' }],
      memories: [{ text: '他喜欢兔子套装', at: NOW }], pending: [{ chatId: '533', text: '等他回家', quote: 'q', dueAt: NOW + 3_600_000 }],
      firstAt: NOW - 86_400_000, lastAt: NOW,
    }
    const ctx = { ...makeCtx(r.home), oldChannelDir: r.ch }
    const deps = { ...makeDeps(w, { ledger }), fs: nodeSwitchFs() }
    expect(switchBack(ctx, deps, { dryRun: false, waitSeconds: 30 })).toBe(0)

    const mem = readFileSync(memPath, 'utf8')
    expect(mem).toContain('## 关于用户')            // 原来的段落没动
    expect(mem).toContain('## dsh 交接（2026-10-09）')
    expect(mem).toContain('这段时间聊了兔子套装和例假')
    expect(mem).toContain('- 他：在吗')
    expect(mem).toContain('他喜欢兔子套装')
    expect(mem.split('## dsh 交接').length).toBe(2)  // 只有一个小节

    const store = JSON.parse(readFileSync(prPath, 'utf8')) as Record<string, Record<string, unknown>>
    expect(store['p-old']).toMatchObject({ gist: '旧事', state: 'pending' })  // 老条目原样
    const added = Object.entries(store).filter(([id]) => id !== 'p-old')
    expect(added.length).toBe(1)
    expect(added[0]![1]).toMatchObject({ chat_id: '533', due_at: NOW + 3_600_000, gist: '等他回家', text: '等他回家', state: 'pending', attempts: 0, fired_at: null })

    // 第二遍：同一个日期的交接小节、同一句承诺都不重复写，文件一个字节都不变
    const memBefore = readFileSync(memPath, 'utf8')
    const prBefore = readFileSync(prPath, 'utf8')
    const w2 = makeWorld({ home: r.home, disabled: false, oldSessions: [], oldPortOpen: false })
    const deps2 = { ...makeDeps(w2, { ledger }), fs: nodeSwitchFs() }
    expect(switchBack(ctx, deps2, { dryRun: false, waitSeconds: 30 })).toBe(0)
    expect(readFileSync(memPath, 'utf8')).toBe(memBefore)
    expect(readFileSync(prPath, 'utf8')).toBe(prBefore)
    expect(printed(w2)).toContain('没重复写')
  } finally { r.cleanup() }
})

test('back：旧 .promises.json 是坏 JSON → 一个字都不覆盖，旧侧照常起回来（不拦路）', () => {
  const r = realWorld()
  try {
    const w = makeWorld({ home: r.home })
    const memPath = oldMemoryPath(r.home, r.ch)
    mkdirSync(dirname(memPath), { recursive: true })
    writeFileSync(memPath, '# Memory\n')
    const prPath = oldPromisesPath(r.ch)
    mkdirSync(r.ch, { recursive: true })
    writeFileSync(prPath, '{ 这不是 JSON')
    const ledger: LedgerPayload = { ...EMPTY_PAYLOAD, chatId: '533', summary: '摘要', pending: [{ chatId: '533', text: '等他回家', quote: '', dueAt: NOW + 1000 }] }
    const ctx = { ...makeCtx(r.home), oldChannelDir: r.ch }
    const deps = { ...makeDeps(w, { ledger }), fs: nodeSwitchFs() }
    expect(switchBack(ctx, deps, { dryRun: false, waitSeconds: 30 })).toBe(0)
    expect(readFileSync(prPath, 'utf8')).toBe('{ 这不是 JSON')
    expect(readFileSync(memPath, 'utf8')).toContain('## dsh 交接（2026-10-09）')
    expect(w.oldSessions).toEqual([`tg-${OLD}-dispatcher`])
    expect(printed(w)).toContain('不是能读的 JSON')
  } finally { r.cleanup() }
})

/* ── 配置里的 enabled：切回旧栈标停用、切回新系统清掉 ─── */

const CFG_PORT = 'dispatcher_port: 17951   # 本机接口端口，只听 127.0.0.1；每个 bot 不一样'
const CFG_BODY = [
  'id: bot5',
  'bot_channel_path: "/tmp/x"',
  CFG_PORT,
  'life_config: /Users/x/claudebotlife/configs/bot5.yml',
  '',
  'brain:',
  '  provider: deepseek-official',
  '',
].join('\n')
/** CFG_BODY 加上停用行（手工切回旧栈后的样子） */
const CFG_STOPPED = CFG_BODY.replace(`${CFG_PORT}\n`, `${CFG_PORT}\n${ENABLED_FALSE_LINE}\n`)

function withCfg(w: World, body: string): { fs: MapFs; path: string } {
  const fs = new MapFs()
  const path = makeCtx(w.home).configPath
  fs.files.set(path, { text: body, mtime: NOW })
  return { fs, path }
}

test('back：新侧停住后把配置标成 enabled: false（插在 dispatcher_port 后面，其它字节不动）', () => {
  const w = makeWorld()
  const { fs, path } = withCfg(w, CFG_BODY)
  expect(switchBack(makeCtx(w.home), makeDeps(w, { fs }), { dryRun: false, waitSeconds: 30 })).toBe(0)
  expect(fs.files.get(path)!.text).toBe(CFG_STOPPED)
  const text = printed(w)
  expect(text.indexOf('确认新侧已停')).toBeLessThan(text.indexOf('插入 enabled: false'))
  expect(text.indexOf('插入 enabled: false')).toBeLessThan(text.indexOf('已拉起'))
})

test('back：已经是 enabled: false → 一个字节都不动，连写都不写', () => {
  const w = makeWorld()
  const { fs, path } = withCfg(w, CFG_STOPPED)
  expect(switchBack(makeCtx(w.home), makeDeps(w, { fs }), { dryRun: false, waitSeconds: 30 })).toBe(0)
  expect(fs.files.get(path)!.text).toBe(CFG_STOPPED)
  expect(fs.writes).not.toContain(path)
  expect(printed(w)).toContain('已经是停用')
})

test('back：enabled: true 改成 false，行尾注释照旧', () => {
  const w = makeWorld()
  const { fs, path } = withCfg(w, 'id: bot5\nenabled: true   # 临时开着\nbrain:\n  provider: x\n')
  expect(switchBack(makeCtx(w.home), makeDeps(w, { fs }), { dryRun: false, waitSeconds: 30 })).toBe(0)
  expect(fs.files.get(path)!.text).toBe('id: bot5\nenabled: false   # 临时开着\nbrain:\n  provider: x\n')
})

test('to-dsh：旧侧停住后把配置里的 enabled 行删掉', () => {
  const w = makeWorld()
  const { fs, path } = withCfg(w, CFG_STOPPED)
  expect(switchToDsh(makeCtx(w.home), makeDeps(w, { fs }), { dryRun: false, stopOld: true, waitSeconds: 30 })).toBe(0)
  expect(fs.files.get(path)!.text).toBe(CFG_BODY)
})

test('to-dsh：配置里没有 enabled 行 → 一个字节都不动', () => {
  const w = makeWorld()
  const { fs, path } = withCfg(w, CFG_BODY)
  expect(switchToDsh(makeCtx(w.home), makeDeps(w, { fs }), { dryRun: false, stopOld: true, waitSeconds: 30 })).toBe(0)
  expect(fs.files.get(path)!.text).toBe(CFG_BODY)
  expect(fs.writes).not.toContain(path)
  expect(printed(w)).toContain('不用改')
})

test('两个方向 --dry-run：只打印将改哪一行，配置文件一个字节都不动', () => {
  const w = makeWorld()
  const back = withCfg(w, CFG_BODY)
  expect(switchBack(makeCtx(w.home), makeDeps(w, { fs: back.fs }), { dryRun: true, waitSeconds: 30 })).toBe(0)
  expect(back.fs.files.get(back.path)!.text).toBe(CFG_BODY)
  expect(back.fs.writes).toEqual([])
  expect(printed(w)).toContain('在第 4 行插入 enabled: false')
  expect(printed(w)).toContain('这次没动任何东西')

  const w2 = makeWorld()
  const to = withCfg(w2, CFG_STOPPED)
  expect(switchToDsh(makeCtx(w2.home), makeDeps(w2, { fs: to.fs }), { dryRun: true, stopOld: true, waitSeconds: 30 })).toBe(0)
  expect(to.fs.files.get(to.path)!.text).toBe(CFG_STOPPED)
  expect(to.fs.writes).toEqual([])
  expect(printed(w2)).toContain('删掉第 4 行')
})

test('back：配置写不进去 → 只警告，旧侧照常起回来', () => {
  const w = makeWorld()
  const { fs, path } = withCfg(w, CFG_BODY)
  fs.failWrite = path
  expect(switchBack(makeCtx(w.home), makeDeps(w, { fs }), { dryRun: false, waitSeconds: 30 })).toBe(0)
  expect(printed(w)).toContain('配置写不进去')
  expect(w.oldSessions).toEqual([`tg-${OLD}-dispatcher`])
})

test('back：真文件系统上写 enabled —— 其它字节与文件权限都不动', () => {
  const r = realWorld()
  try {
    const w = makeWorld({ home: r.home })
    const cfgPath = join(r.home, 'configs', 'bot5.yml')
    mkdirSync(dirname(cfgPath), { recursive: true })
    writeFileSync(cfgPath, CFG_BODY)
    chmodSync(cfgPath, 0o600)
    const ctx = { ...makeCtx(r.home), configPath: cfgPath }
    const deps = { ...makeDeps(w), fs: nodeSwitchFs() }
    expect(switchBack(ctx, deps, { dryRun: false, waitSeconds: 30 })).toBe(0)
    expect(readFileSync(cfgPath, 'utf8')).toBe(CFG_STOPPED)
    if (process.platform !== 'win32') expect(statSync(cfgPath).mode & 0o777).toBe(0o600)
  } finally { r.cleanup() }
})

test('enabled 行的判定与改写：引号、注释、缩进都不误伤（口径与 list_enabled_bots 一致）', () => {
  const cases: [string, boolean][] = [
    ['enabled: false', false],
    ['enabled: False', false],
    ['enabled: no', false],
    ['enabled: OFF', false],
    ['enabled: 0', false],
    ['enabled: false   # 切回新系统时删掉这行', false],
    ['enabled: true', true],
    ['enabled:', true],
    ['enabled: "false"', true],
    ['enabled: ""', false],
  ]
  for (const [line, on] of cases) expect(`${line} → ${enabledValueOf(line)}`).toBe(`${line} → ${on}`)
  // 缩进的是别的段里的键，不算顶层：to-dsh 不动它；back 另在文件开头补一行顶层的
  const indented = 'gateway:\n  enabled: false\n'
  expect(editEnabledYml(indented, true)).toEqual({ text: indented, changed: false, what: expect.any(String) })
  expect(editEnabledYml(indented, false).text).toBe(`${ENABLED_FALSE_LINE}\n${indented}`)
})
