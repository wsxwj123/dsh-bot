// gateway.image_wait_ms（搬自上游 1e48e78 的那一项）：生图同步等待时长可配。
// 配置键的范围校验（INTERFACE 3.13.4）+ 等待逻辑 raceWait 的行为。
import { expect, test } from 'bun:test'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { ConfigError, loadBotConfig } from '../../src/config'
import { raceWait } from '../../src/life/actions'

function cfgWith(gatewayExtra: string): number {
  const root = mkdtempSync(join(tmpdir(), 'iw-'))
  try {
    const ch = join(root, 'ch')
    mkdirSync(ch)
    writeFileSync(join(ch, 'access.json'), JSON.stringify({ allowFrom: ['42'] }))
    const p = join(root, 'b.yml')
    writeFileSync(p, `id: bot5\nbot_channel_path: ${ch}\n${gatewayExtra}`)
    return loadBotConfig(p, { DSH_BOT_HOME: join(root, 'home') }).gw.imageWaitMs
  } finally { rmSync(root, { recursive: true, force: true }) }
}

test('image_wait_ms：默认 10000；合法值照收（含 0 和上界）', () => {
  expect(cfgWith('')).toBe(10_000)
  expect(cfgWith('gateway:\n  image_wait_ms: 30000\n')).toBe(30_000)
  expect(cfgWith('gateway:\n  image_wait_ms: 0\n')).toBe(0)
  expect(cfgWith('gateway:\n  image_wait_ms: 600000\n')).toBe(600_000)
})

test('image_wait_ms：不是整数或超出 0–600000 之外：启动就报配置错误', () => {
  for (const bad of ['-1', '600001', '1.5', 'abc']) {
    expect(() => cfgWith(`gateway:\n  image_wait_ms: ${bad}\n`)).toThrow(ConfigError)
  }
})

test('raceWait：run 先完成就返回它，否则返回 wait', async () => {
  expect(await raceWait(Promise.resolve('done'), 10_000)).toBe('done')
  expect(await raceWait(new Promise<string>(() => {}), 5)).toBe('wait')
  expect(await raceWait(new Promise<string>(r => setTimeout(() => r('late'), 20)), 5)).toBe('wait')
  expect(await raceWait(new Promise<string>(r => setTimeout(() => r('quick'), 5)), 100)).toBe('quick')
})
