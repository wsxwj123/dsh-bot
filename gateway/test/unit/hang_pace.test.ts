// 新网关加的：被晾追问等多久按关系数值调（越亲近追得越快）
import { expect, test } from 'bun:test'
import { mkdtempSync, readFileSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { hangPace } from '../../src/engine/engine'
import { createHangRuntime } from '../../src/life/hang_runtime'

test('关系数值 → 等待倍数：很亲近 0.5，一般 1，生疏 1.5，读不到 1', () => {
  const dir = mkdtempSync(join(tmpdir(), 'pace-'))
  const set = (a: number, t: number) => writeFileSync(join(dir, 'relationship.json'), JSON.stringify({ affection: a, trust: t }))
  expect(hangPace(dir)).toBe(1)
  set(100, 100); expect(hangPace(dir)).toBeCloseTo(0.5)
  set(50, 50); expect(hangPace(dir)).toBeCloseTo(1)
  set(0, 0); expect(hangPace(dir)).toBeCloseTo(1.5)
})

test('第一次追问的等待时间按倍数伸缩', () => {
  const at = (k: number) => {
    const dir = mkdtempSync(join(tmpdir(), 'hang-'))
    const t0 = 1_800_000_000_000
    const rt = createHangRuntime({ channelDir: dir, inject: () => {}, probe: () => null, now: () => t0, rand: () => 0.5, pace: () => k })
    rt.onInbound('1', t0 - 60_000)
    rt.onOutbound('1')
    const s = JSON.parse(readFileSync(join(dir, '.hang-state.json'), 'utf8')).chats['1']
    return (s.nextAt - t0) / 60_000
  }
  const base = at(1)
  expect(base).toBeGreaterThanOrEqual(5)
  expect(base).toBeLessThanOrEqual(9)
  expect(at(0.5)).toBeCloseTo(base * 0.5, 1)
  expect(at(1.5)).toBeCloseTo(base * 1.5, 1)
})
