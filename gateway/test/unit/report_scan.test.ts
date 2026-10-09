// report.ts 的机密扫描（搬自上游 bc8727d 的那一项）：凭据文件按 YAML 读（带引号的密钥也要认出来），
// 扫描范围要覆盖状态目录（账本和 WAL）、bot.patch.yml、providers.json，不只是日志目录。
// 用子进程跑真脚本，避免 import 顶层就把它整套跑起来。
import { expect, test } from 'bun:test'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join, resolve } from 'path'

const GATEWAY_DIR = resolve(import.meta.dir, '..', '..')
const SECRET = 'quoted-secret-0123456789'

/** 造一个最小可跑的根 + 配置，返回 { p（配置文件）, home（DSH_BOT_HOME）, run()（跑 report.ts） } */
function makeEnv() {
  const root = mkdtempSync(join(tmpdir(), 'report-'))
  const home = join(root, 'home')
  const ch = join(root, 'ch')
  mkdirSync(ch, { recursive: true })
  writeFileSync(join(ch, 'access.json'), JSON.stringify({ allowFrom: ['42'] }))
  const p = join(root, 'b.yml')
  writeFileSync(p, `id: bot5\nbot_channel_path: ${ch}\n`)
  // 凭据文件：值带引号（按行正则读会漏扫，按 YAML 读才认得出）
  mkdirSync(home, { recursive: true })
  writeFileSync(join(home, 'credentials.yaml'), `version: 1\nrefs:\n  PROVIDER_X_KEY: "${SECRET}"\n`)
  const run = () => {
    const r = Bun.spawnSync(['bun', join(GATEWAY_DIR, 'scripts', 'report.ts'), '--config', p], {
      cwd: GATEWAY_DIR, env: { PATH: process.env.PATH ?? '', HOME: process.env.HOME ?? '', DSH_BOT_HOME: home },
    })
    return r.stdout.toString() + r.stderr.toString()
  }
  const cleanup = () => rmSync(root, { recursive: true, force: true })
  return { root, home, p, run, cleanup }
}

test('日志里有带引号密钥的原文：报出来（发现 1 处）', () => {
  const e = makeEnv()
  try {
    const logs = join(e.home, 'bots', 'bot5', 'logs')
    mkdirSync(logs, { recursive: true })
    writeFileSync(join(logs, 'gateway.log'), `some line with ${SECRET} inside\n`)
    const out = e.run()
    expect(out).toContain('日志、账本里有没有机密')
    expect(out).toContain('发现 1 处')
    expect(out).toContain('gateway.log')
  } finally { e.cleanup() }
})

test('机密出现在状态目录里的文件也要查到（不只日志目录）', () => {
  const e = makeEnv()
  try {
    const state = join(e.home, 'bots', 'bot5', 'state')
    mkdirSync(state, { recursive: true })
    writeFileSync(join(state, 'stray.txt'), `blob ${SECRET}`)
    const out = e.run()
    expect(out).toContain('发现 1 处')
    expect(out).toContain('stray.txt')
  } finally { e.cleanup() }
})

test('干净环境：没有发现', () => {
  const e = makeEnv()
  try {
    const out = e.run()
    expect(out).toContain('没有发现')
  } finally { e.cleanup() }
})
