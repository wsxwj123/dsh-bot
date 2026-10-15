// 13.2：bot 已迁到另一种底层（<bot 目录>/.moved 在）时，命令行装开机自启要拒绝并说明去哪里切换
import { expect, test } from 'bun:test'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { movedRefusal } from '../../src/autostart'

test('有 .moved 标记：拒绝安装，说明「已迁到另一种底层，请在管理台用切换底层」；标记读不懂也拒绝', () => {
  const dir = mkdtempSync(join(tmpdir(), 'autostart-moved-'))
  try {
    writeFileSync(join(dir, '.moved'), 'to=claude op=- at=20261015T051700Z\n')
    expect(movedRefusal(dir)).toContain('已迁到另一种底层，请在管理台用切换底层')
    writeFileSync(join(dir, '.moved'), 'garbage')
    expect(movedRefusal(dir)).toContain('已迁到另一种底层')
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('没有标记（bot 目录在或不在）：不拦', () => {
  const dir = mkdtempSync(join(tmpdir(), 'autostart-moved-'))
  try {
    mkdirSync(join(dir, 'bot'))
    expect(movedRefusal(join(dir, 'bot'))).toBeNull()
    expect(movedRefusal(join(dir, 'missing'))).toBeNull()
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})
