// 原子写（方案 3.1.2、3.11）：临时文件一创建就是给定权限、失败不留临时文件、文件被占用时短暂重试
import { afterEach, expect, test } from 'bun:test'
import { chmodSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { retryWhenBusy, writeAtomic } from '../../src/util'

const dirs: string[] = []
const tmp = () => { const d = mkdtempSync(join(tmpdir(), 'util-')); dirs.push(d); return d }
afterEach(() => { for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true }) })

test('写入内容、权限 600，目录里不留临时文件', () => {
  const d = tmp()
  const p = join(d, 'sub', 'credentials.yaml')
  writeAtomic(p, 'version: 1\n', 0o600)
  writeAtomic(p, 'version: 2\n', 0o600)
  expect(readFileSync(p, 'utf8')).toBe('version: 2\n')
  if (process.platform !== 'win32') expect(statSync(p).mode & 0o777).toBe(0o600)
  expect(readdirSync(join(d, 'sub'))).toEqual(['credentials.yaml'])
})

test('上次崩溃留下的同名临时文件（权限宽）：先删掉再"不存在才创建"，结果仍是 600', () => {
  const d = tmp()
  const p = join(d, 'providers.json')
  const stale = `${p}.tmp-${process.pid}`
  writeFileSync(stale, 'old leftover')
  if (process.platform !== 'win32') chmodSync(stale, 0o644)
  writeAtomic(p, '{}', 0o600)
  expect(readFileSync(p, 'utf8')).toBe('{}')
  if (process.platform !== 'win32') expect(statSync(p).mode & 0o777).toBe(0o600)
  expect(readdirSync(d)).toEqual(['providers.json'])
})

test('改名失败（目标是个非空目录）：抛错，并删掉含内容的临时文件', () => {
  const d = tmp()
  const p = join(d, 'target')
  mkdirSync(join(p, 'inside'), { recursive: true })
  expect(() => writeAtomic(p, 'SECRET-VALUE-123', 0o600)).toThrow()
  expect(readdirSync(d)).toEqual(['target'])
})

test('retryWhenBusy：EPERM/EBUSY 重试最多 5 次，成功就返回；其它错误不重试', () => {
  const busy = (code: string) => Object.assign(new Error(code), { code })
  let n = 0
  expect(retryWhenBusy(() => { if (++n < 3) throw busy('EBUSY'); return 'ok' }, 5, 1)).toBe('ok')
  expect(n).toBe(3)
  n = 0
  expect(() => retryWhenBusy(() => { n++; throw busy('EPERM') }, 5, 1)).toThrow('EPERM')
  expect(n).toBe(6)
  n = 0
  expect(() => retryWhenBusy(() => { n++; throw busy('ENOENT') }, 5, 1)).toThrow('ENOENT')
  expect(n).toBe(1)
})
