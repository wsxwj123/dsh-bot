// logs_tmux.sh 的行为（用假 tmux 记录器，不真起会话）。launchd 项本身（tmuxLogsAgent）的测试在 autostart.test.ts。
import { expect, test } from 'bun:test'
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join, resolve } from 'path'

const REPO = resolve(import.meta.dir, '..', '..', '..')
const SCRIPT = join(REPO, 'gateway', 'scripts', 'logs_tmux.sh')

// ─── logs_tmux.sh：假 tmux 记录器 ───

const FAKE_TMUX = `#!/bin/bash
# 假 tmux（单测用）：把每次调用记进 $FAKE_TMUX_LOG；has-session 按 $FAKE_TMUX_SESSIONS（冒号分隔）回答；不真起会话
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

test('会话名与命令拼装：tg-<bot>-dsh，里面跟 logs.ts -f --chat，bun 用绝对路径；已存在的会话跳过', () => {
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
    ['CALL new', '-d', '-s', 'tg-bot7-dsh', `cd "${REPO}" && "/fake/bun" gateway/scripts/logs.ts --config "${join(s.cfgDir, 'bot7.yml')}" -f --chat`],
  ])
  expect(cs[cs.length - 1]).toEqual(['CALL has-session', '-t', '=tg-bot8-dsh'])
})

test('幂等：已存在的会话一个都不动（只 has-session，没有 new，更没有 kill）', () => {
  const s = setup({ 'bot7.yml': 'id: bot7\n', 'bot8.yml': 'id: bot8\n' }, 'tg-bot7-dsh:tg-bot8-dsh')
  const r = run(s, { LOGS_TMUX_VERBOSE: '1' })
  expect(r.exitCode).toBe(0)
  expect(r.stdout.toString()).toContain('共 2 个 bot，建了 0 个、跳过 2 个。')
  expect(calls(s).map(c => c[0])).toEqual(['CALL has-session', 'CALL has-session'])
})

test('launchd 场景（没有终端）：一切正常时完全静默，不刷 launchd 的日志文件', () => {
  const s = setup({ 'bot7.yml': 'id: bot7\n' }, 'tg-bot7-dsh')
  const r = run(s)
  expect(r.exitCode).toBe(0)
  expect(r.stdout.toString()).toBe('')
  expect(r.stderr.toString()).toBe('')
})

test('不碰旧系统的会话：worker / dispatcher 名字相近也照常只看自己的精确会话名，从不 kill', () => {
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

test('配置文件不合格的跳过：没有 id、id 带非法字符的都不建；其余照常', () => {
  const s = setup({ '_global.yml': '# 没有 id\nfoo: 1\n', 'bot7.yml': 'id: bot7\n', 'weird.yml': 'id: "bad id"\n' }, '')
  const r = run(s, { LOGS_TMUX_VERBOSE: '1' })
  expect(r.exitCode).toBe(0)
  const out = r.stdout.toString()
  expect(out).toContain('_global.yml 里没有 id，跳过')
  expect(out).toContain('weird.yml 的 id「bad id」不能用')
  expect(out).toContain('共 1 个 bot，建了 1 个、跳过 0 个。')
  expect(calls(s).filter(c => c[0] === 'CALL new').length).toBe(1)
})

test('没有配置目录：说清楚并正常退出（还没切 bot 的机器）', () => {
  const s = setup({}, '')
  const r = run(s, { DSH_BOT_CONFIGS_DIR: join(s.tmp, '不存在的目录') })
  expect(r.exitCode).toBe(0)
  expect(r.stdout.toString()).toContain('没有配置目录')
})

test('目录里没有 bot 配置：提示一行', () => {
  const s = setup({ 'note.txt': 'x' }, '')
  const r = run(s)
  expect(r.exitCode).toBe(0)
  expect(r.stdout.toString()).toContain('没找到 bot 配置')
})

test('找不到 bun：报错退出（BUN_BIN、~/.bun/bin/bun、PATH 三处都没有）', () => {
  const s = setup({ 'bot7.yml': 'id: bot7\n' }, '')
  const r = run(s, { BUN_BIN: '' })
  expect(r.exitCode).toBe(1)
  expect(r.stderr.toString()).toContain('找不到 bun')
  expect(calls(s).length).toBe(0)
})

test('没有 tmux：报错退出', () => {
  const s = setup({ 'bot7.yml': 'id: bot7\n' }, '')
  const r = Bun.spawnSync(['/bin/bash', SCRIPT], {
    env: { PATH: '/var/empty', HOME: join(s.tmp, 'home'), BUN_BIN: '/fake/bun', DSH_BOT_CONFIGS_DIR: s.cfgDir, FAKE_TMUX_LOG: s.log, FAKE_TMUX_SESSIONS: '' },
  })
  expect(r.exitCode).toBe(1)
  expect(r.stderr.toString()).toContain('找不到 tmux')
})
