// 跨聊天发送的纯函数：登记表查找、群名状态文件、账本按目标过滤。不启网关。
import { expect, test } from 'bun:test'
import { mkdtempSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { registeredChatTarget } from '../../src/config'

const access = {
  allowFrom: ['5550001', '5550002'],
  groups: { '-1001777': { allowFrom: [] }, '-1002666': { allowFrom: [] } },
}

test('登记表：群按原始键命中，私聊按 allowFrom 命中', () => {
  expect(registeredChatTarget(access, '-1001777')).toEqual({ chatId: '-1001777', kind: 'group' })
  expect(registeredChatTarget(access, '5550002')).toEqual({ chatId: '5550002', kind: 'dm' })
})

test('登记表：首尾空白先 trim；未登记、空串、前缀/数值形态都不算', () => {
  expect(registeredChatTarget(access, ' -1001777 ')).toEqual({ chatId: '-1001777', kind: 'group' })
  expect(registeredChatTarget(access, ' 5550001 ')).toEqual({ chatId: '5550001', kind: 'dm' })
  expect(registeredChatTarget(access, '-1009999')).toBeNull()
  expect(registeredChatTarget(access, '7777777')).toBeNull()
  expect(registeredChatTarget(access, '')).toBeNull()
  expect(registeredChatTarget(access, '-100177')).toBeNull()
  // 前导 0 不归一：0123 与 123 不是同一个聊天
  expect(registeredChatTarget({ allowFrom: ['123'], groups: {} }, '0123')).toBeNull()
  // 大小写不折叠（虽然合法编号里不会出现，但规则如此）
  expect(registeredChatTarget({ allowFrom: ['ABC'], groups: {} }, 'abc')).toBeNull()
})

test('登记表：群里登记的是原始键，发送与群记录都用它', () => {
  const odd = { allowFrom: [], groups: { ' -1001777 ': { allowFrom: [] } } }
  const got = registeredChatTarget(odd, '-1001777')
  expect(got).toEqual({ chatId: ' -1001777 ', kind: 'group' })
})

test('登记表：同一编号同时出现在私聊和群里时按形状（负号）选群', () => {
  const both = { allowFrom: ['-1001777'], groups: { '-1001777': { allowFrom: [] } } }
  expect(registeredChatTarget(both, '-1001777')).toEqual({ chatId: '-1001777', kind: 'group' })
})

test('群名状态文件：读不出、坏 JSON、形状不对都返回空表，不抛异常', () => {
  const { readSeenGroups } = require('../../src/telegram/groups_seen') as typeof import('../../src/telegram/groups_seen')
  const dir = mkdtempSync(join(tmpdir(), 'seen-groups-'))
  try {
    expect(readSeenGroups(join(dir, 'missing.json'))).toEqual({})
    const bad = join(dir, 'bad.json')
    writeFileSync(bad, '{ broken')
    expect(readSeenGroups(bad)).toEqual({})
    const arr = join(dir, 'arr.json')
    writeFileSync(arr, '[]')
    expect(readSeenGroups(arr)).toEqual({})
    const ok = join(dir, 'ok.json')
    writeFileSync(ok, JSON.stringify({
      '-1001777': { title: '测试群', type: 'supergroup', status: 'member', added_by_owner: null, first_seen: 'x', last_seen: 'y', migrated_from: null },
      'oops': { title: 'bad id', type: 'group', status: 'member', added_by_owner: null, first_seen: 'x', last_seen: 'y', migrated_from: null },
      '-1002666': { title: '', type: 'group', status: 'left', added_by_owner: null, first_seen: 'x', last_seen: 'y', migrated_from: null },
    }))
    const got = readSeenGroups(ok)
    expect(Object.keys(got).sort()).toEqual(['-1001777', '-1002666'])
    expect(got['-1001777']!.title).toBe('测试群')
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})
