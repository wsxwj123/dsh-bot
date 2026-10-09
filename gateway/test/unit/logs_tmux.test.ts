// logs_tmux.sh 的行为（用假 tmux 记录器，不真起会话）。launchd 项本身（tmuxLogsAgent）的测试在 autostart.test.ts。
//
// 整个文件在 Windows 上跳过：被测的 logs_tmux.sh 是一份 POSIX shell 脚本（launchd 登录项专用，
// 产品侧本来就不在 Windows 上跑），用例要用 /bin/bash 起它——Windows 上没有这个路径，
// 14 条会全部挂在 ENOENT 上。按需求书第 75 行的口径（非 macOS 上要能跳过），
// 用统一的 testIf 让整个文件在 Windows 上注册为 skip，不在每个用例里散加判平台。
import { expect, test } from 'bun:test'
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join, resolve } from 'path'

const testIf = process.platform === 'win32' ? test.skip : test

const REPO = resolve(import.meta.dir, '..', '..', '..')
const SCRIPT = join(REPO, 'gateway', 'scripts', 'logs_tmux.sh')

// ─── logs_tmux.sh：假 tmux 记录器 ───

const FAKE_TMUX = `#!/bin/bash
# 假 tmux（单测用）：把每次调用记进 $FAKE_TMUX_LOG；has-session 按 $FAKE_TMUX_SESSIONS（冒号分隔）回答；
# 不真起会话。FAKE_TMUX_RUN=1 时把 new 的命令串（shift 后的 $4）交给 sh 真跑一遍：配合假 bun 验证转义后语义不变
log=\${FAKE_TMUX_LOG:?}
cmd=$1
printf 'CALL %s' "$cmd" >> "$log"
shift
for a in "$@"; do printf '\\t%s' "$a" >> "$log"; done
printf '\\n' >> "$log"
case "$cmd" in
  has-session)
    target=''
    prev=''
    for a in "$@"; do [ "$prev" = "-t" ] && target=$a; prev=$a; done
    target=\${target#=}
    case ":\${FAKE_TMUX_SESSIONS:-}:" in *":$target:"*) exit 0 ;; *) exit 1 ;; esac
    ;;
  new)
    if [ "\${FAKE_TMUX_NEW_FAIL:-}" = 1 ]; then exit 1; fi
    if [ "\${FAKE_TMUX_RUN:-}" = 1 ]; then
      sh -c "$4" >/dev/null 2>&1 || true
    fi
    exit 0
    ;;
esac
exit 0
`

function setup(configs: Record<string, string>, sessions: string) {
  const tmp = mkdtempSync(join(tmpdir(), 'logs-tmux-'))
  const binDir = join(tmp, 'bin')
  const cfgDir = join(tmp, 'configs')
  mkdirSync(binDir, { recursive: true })
  mkdirSync(cfgDir, { recursive: true })
  for (const [name, body] of Object.entries(configs)) writeFileSync(join(cfgDir, name), body)
  writeFileSync(join(binDir, 'tmux'), FAKE_TMUX)
  chmodSync(join(binDir, 'tmux'), 0o755)
  const log = join(tmp, 'tmux.calls')
  writeFileSync(log, '')
  return { tmp, binDir, cfgDir, log, sessions }
}

type Env = ReturnType<typeof setup>

function run(s: Env, extra: Record<string, string> = {}) {
  return Bun.spawnSync(['/bin/bash', SCRIPT], {
    env: {
      PATH: `${s.binDir}:/usr/bin:/bin`, HOME: join(s.tmp, 'home'),
      BUN_BIN: '/fake/bun', TMUX_BIN: join(s.binDir, 'tmux'),
      DSH_BOT_CONFIGS_DIR: s.cfgDir,
      FAKE_TMUX_LOG: s.log, FAKE_TMUX_SESSIONS: s.sessions,
      ...extra,
    },
  })
}

function calls(s: Env): string[][] {
  const text = readFileSync(s.log, 'utf8').trim()
  return text ? text.split('\n').map(l => l.split('\t')) : []
}

testIf('会话名与命令拼装：tg-<bot>-dsh，里面跟 logs.ts -f --chat，路径用单引号引用；已存在的会话跳过', () => {
  const s = setup({ 'bot7.yml': 'id: bot7\n# 注释\nblue: 1\n', 'bot8.yml': 'id: bot8\n' }, 'tg-bot8-dsh')
  const r = run(s, { LOGS_TMUX_VERBOSE: '1' })
  expect(r.exitCode).toBe(0)
  const out = r.stdout.toString()
  expect(out).toContain('tg-bot7-dsh 新建')
  expect(out).toContain('tg-bot8-dsh 已存在，跳过')
  expect(out).toContain('共 2 个 bot，建了 1 个、跳过 1 个。')

  const cs = calls(s)
  expect(cs[0]).toEqual(['CALL has-session', '-t', '=tg-bot7-dsh'])
  const news = cs.filter(c => c[0] === 'CALL new')
  expect(news).toEqual([
    ['CALL new', '-d', '-s', 'tg-bot7-dsh', `cd '${REPO}' && '/fake/bun' gateway/scripts/logs.ts --config '${join(s.cfgDir, 'bot7.yml')}' -f --chat`],
  ])
  expect(cs[cs.length - 1]).toEqual(['CALL has-session', '-t', '=tg-bot8-dsh'])
})

testIf('幂等：已存在的会话一个都不动（只 has-session，没有 new，更没有 kill）', () => {
  const s = setup({ 'bot7.yml': 'id: bot7\n', 'bot8.yml': 'id: bot8\n' }, 'tg-bot7-dsh:tg-bot8-dsh')
  const r = run(s, { LOGS_TMUX_VERBOSE: '1' })
  expect(r.exitCode).toBe(0)
  expect(r.stdout.toString()).toContain('共 2 个 bot，建了 0 个、跳过 2 个。')
  expect(calls(s).map(c => c[0])).toEqual(['CALL has-session', 'CALL has-session'])
})

testIf('launchd 场景（没有终端）：一切正常时完全静默，不刷 launchd 的日志文件', () => {
  const s = setup({ 'bot7.yml': 'id: bot7\n' }, 'tg-bot7-dsh')
  const r = run(s)
  expect(r.exitCode).toBe(0)
  expect(r.stdout.toString()).toBe('')
  expect(r.stderr.toString()).toBe('')
})

testIf('不碰旧系统的会话：worker / dispatcher 名字相近也照常只看自己的精确会话名，从不 kill', () => {
  const s = setup({ 'bot7.yml': 'id: bot7\n' }, 'tg-bot7-worker:tg-bot7-dispatcher:tg-bot7-dsh-2')
  const r = run(s, { LOGS_TMUX_VERBOSE: '1' })
  expect(r.exitCode).toBe(0)
  expect(r.stdout.toString()).toContain('tg-bot7-dsh 新建')
  const cmds = calls(s)
  expect(new Set(cmds.map(c => c[0]))).toEqual(new Set(['CALL has-session', 'CALL new']))
  expect(cmds.filter(c => c[0] === 'CALL new').every(c => c[3] === 'tg-bot7-dsh')).toBe(true)
  const src = readFileSync(SCRIPT, 'utf8')
  expect(src).not.toMatch(/kill-session|kill-server|tmux attach/)
})

testIf('配置文件不合格的跳过：没有 id、id 带非法字符的都不建；其余照常', () => {
  const s = setup({ '_global.yml': '# 没有 id\nfoo: 1\n', 'bot7.yml': 'id: bot7\n', 'weird.yml': 'id: "bad id"\n' }, '')
  const r = run(s, { LOGS_TMUX_VERBOSE: '1', LANG: 'en_US.UTF-8' }) // 这一条会打到「$id」那句
  expect(r.exitCode).toBe(0)
  const out = r.stdout.toString()
  expect(out).toContain('_global.yml 里没有 id，跳过')
  expect(out).toContain('weird.yml 的 id「bad id」不能用')
  expect(out).toContain('共 1 个 bot，建了 1 个、跳过 0 个。')
  expect(calls(s).filter(c => c[0] === 'CALL new').length).toBe(1)
})

testIf('文件名不在白名单里的跳过（没有终端也提示）：带 ;、$(...)、双引号的名字不会进 tmux 命令串', () => {
  const s = setup({
    'bot7.yml': 'id: bot7\n',
    'evil;touch pwned.yml': 'id: evil\n',
    'evil$(whoami).yml': 'id: evil\n',
    'evil"quote.yml': 'id: evil\n',
  }, '')
  const r = run(s) // 不设 LOGS_TMUX_VERBOSE：非法文件名的提示必须始终打出来
  expect(r.exitCode).toBe(0)
  const out = r.stdout.toString()
  for (const base of ['evil;touch pwned.yml', 'evil$(whoami).yml', 'evil"quote.yml']) {
    expect(out).toContain(`${base} 的文件名不能用`)
  }
  expect(out).toContain('共 1 个 bot，建了 1 个、跳过 0 个。')
  const news = calls(s).filter(c => c[0] === 'CALL new')
  expect(news.length).toBe(1)
  expect(news[0]![3]).toBe('tg-bot7-dsh')
  expect(news[0]![4]).not.toContain('evil')
})

testIf('路径含空格和单引号时按单引号转义，交给 sh 执行后语义不变（假 bun 收到原样的 --config 路径）', () => {
  const s = setup({}, '')
  const weirdCfg = join(s.tmp, "cfg dir 'x")
  mkdirSync(weirdCfg, { recursive: true })
  writeFileSync(join(weirdCfg, 'bot7.yml'), 'id: bot7\n')
  const weirdBinDir = join(s.tmp, "bin dir 'y")
  mkdirSync(weirdBinDir, { recursive: true })
  const fakeBun = join(weirdBinDir, 'fake bun')
  writeFileSync(fakeBun, `#!/bin/bash\nprintf '%s\\n' "$@" > "$FAKE_BUN_LOG"\n`)
  chmodSync(fakeBun, 0o755)
  const bunLog = join(s.tmp, 'bun.args')
  const r = run(s, { DSH_BOT_CONFIGS_DIR: weirdCfg, BUN_BIN: fakeBun, FAKE_TMUX_RUN: '1', FAKE_BUN_LOG: bunLog })
  expect(r.exitCode).toBe(0)
  expect(r.stdout.toString()).toContain('tg-bot7-dsh 新建')
  expect(readFileSync(bunLog, 'utf8')).toBe(['gateway/scripts/logs.ts', '--config', join(weirdCfg, 'bot7.yml'), '-f', '--chat'].join('\n') + '\n')
})

testIf('没有配置目录：说清楚并正常退出（还没切 bot 的机器）', () => {
  const s = setup({}, '')
  const r = run(s, { DSH_BOT_CONFIGS_DIR: join(s.tmp, '不存在的目录'), LANG: 'en_US.UTF-8' })
  expect(r.exitCode).toBe(0)
  expect(r.stdout.toString()).toContain('没有配置目录')
})

testIf('目录里没有（启用中的）bot 配置：提示一行', () => {
  const s = setup({ 'note.txt': 'x', 'bot3.yml': 'id: bot3\nenabled: false\n' }, '')
  const r = run(s)
  expect(r.exitCode).toBe(0)
  expect(r.stdout.toString()).toContain('没有启用中的 bot 配置')
  expect(calls(s).length).toBe(0) // 停用的连 has-session 都不查
})

testIf('只给启用中的 bot 建会话：enabled 假值的跳过，没写/真值/带引号的照常（口径与 list_enabled_bots 一致）', () => {
  const s = setup({
    'bot2.yml': 'id: bot2\n',
    'bot3.yml': 'id: bot3\ndispatcher_port: 17953\nenabled: false   # 现在跑在旧栈\n',
    'bot4.yml': 'id: bot4\nenabled: true\n',
    'bot5.yml': 'id: bot5\nenabled: 0\n',
    'bot6.yml': 'id: bot6\nenabled:\n',
    'bot7.yml': 'id: bot7\ngateway:\n  enabled: false\n', // 缩进的不是顶层键
    'bot8.yml': 'id: bot8\nenabled: "false"\n', // 带引号是字符串，非空即启用
  }, '')
  const r = run(s, { LOGS_TMUX_VERBOSE: '1' })
  expect(r.exitCode).toBe(0)
  const out = r.stdout.toString()
  expect(out).toContain('bot3.yml 标记为停用（enabled 写了假值），跳过')
  expect(out).toContain('bot5.yml 标记为停用（enabled 写了假值），跳过')
  const news = calls(s).filter(c => c[0] === 'CALL new').map(c => c[3])
  expect(news.sort()).toEqual(['tg-bot2-dsh', 'tg-bot4-dsh', 'tg-bot6-dsh', 'tg-bot7-dsh', 'tg-bot8-dsh'])
  expect(out).toContain('共 5 个 bot，建了 5 个、跳过 0 个。')
})

// launchd 的 plist 设了 LANG=en_US.UTF-8：bash 3.2 会把中文标点的首字节吃进变量名，
// 手工跑（shell 的 locale 是 C）不复现。下面两条把 summary 的两个分支都过一遍。
testIf('UTF-8 locale 下建会话：summary 行正常，没有 unbound variable', () => {
  const s = setup({ 'bot7.yml': 'id: bot7\n' }, '')
  const r = run(s, { LANG: 'en_US.UTF-8', LOGS_TMUX_VERBOSE: '1' })
  expect(r.exitCode).toBe(0)
  expect(r.stderr.toString()).toBe('')
  expect(r.stdout.toString()).toContain('共 1 个 bot，建了 1 个、跳过 0 个。')
})

testIf('UTF-8 locale 下建会话失败：失败数写进 summary、退出码 1，也不炸', () => {
  const s = setup({ 'bot7.yml': 'id: bot7\n' }, '')
  const r = run(s, { LANG: 'en_US.UTF-8', LOGS_TMUX_VERBOSE: '1', FAKE_TMUX_NEW_FAIL: '1' })
  expect(r.exitCode).toBe(1)
  expect(r.stderr.toString()).toContain('没建起来')
  expect(r.stdout.toString()).toContain('共 1 个 bot，建了 0 个、跳过 0 个、失败 1 个。')
})

testIf('找不到 bun：报错退出（BUN_BIN、~/.bun/bin/bun、PATH 三处都没有）', () => {
  const s = setup({ 'bot7.yml': 'id: bot7\n' }, '')
  const r = run(s, { BUN_BIN: '' })
  expect(r.exitCode).toBe(1)
  expect(r.stderr.toString()).toContain('找不到 bun')
  expect(calls(s).length).toBe(0)
})

testIf('没有 tmux：报错退出', () => {
  const s = setup({ 'bot7.yml': 'id: bot7\n' }, '')
  const r = Bun.spawnSync(['/bin/bash', SCRIPT], {
    env: { PATH: '/var/empty', HOME: join(s.tmp, 'home'), BUN_BIN: '/fake/bun', DSH_BOT_CONFIGS_DIR: s.cfgDir, FAKE_TMUX_LOG: s.log, FAKE_TMUX_SESSIONS: '' },
  })
  expect(r.exitCode).toBe(1)
  expect(r.stderr.toString()).toContain('找不到 tmux')
})
