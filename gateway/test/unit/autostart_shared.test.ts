// install-shared 的 --only / --web-port / --call-port（搬自上游 1e48e78 的那一项）：
// 只装列出的服务、认不出的名字报错、换端口翻成对应服务的环境变量。纯函数，不碰 launchctl。
import { expect, test } from 'bun:test'
import { pickShared, plist, pythonAgent, SHARED, sharedPortEnv } from '../../src/autostart'

test('pickShared：不给 --only 全装；给了就只装列出的', () => {
  expect(pickShared(undefined).items.map(j => j.name)).toEqual(SHARED.map(j => j.name))
  expect(pickShared(undefined).unknown).toEqual([])
  const r = pickShared(['jiwen', 'memory-compactor'])
  expect(r.items.map(j => j.name)).toEqual(['jiwen', 'memory-compactor'])
  expect(r.unknown).toEqual([])
})

test('pickShared：认不出的名字单独报出来（给上层报错用）', () => {
  const r = pickShared(['jiwen', 'nope', 'voicecall'])
  expect(r.items.map(j => j.name)).toEqual(['jiwen', 'voicecall'])
  expect(r.unknown).toEqual(['nope'])
})

test('sharedPortEnv：朋友圈网页、电话换端口；没给就是空', () => {
  expect(sharedPortEnv('8800', '8801')).toEqual({ 'moments-web': { MOMENTS_WEB_PORT: '8800' }, voicecall: { VOICECALL_PORT: '8801' } })
  expect(sharedPortEnv()).toEqual({ 'moments-web': {}, voicecall: {} })
  expect(sharedPortEnv('8800')).toEqual({ 'moments-web': { MOMENTS_WEB_PORT: '8800' }, voicecall: {} })
})

test('端口环境变量真的进了 plist：--call-port 换电话端口、--web-port 换朋友圈网页端口', () => {
  const ports = sharedPortEnv('8800', '8801')
  const env = { PATH: '/usr/bin', HOME: '/Users/u' }
  const call = plist(pythonAgent({ name: 'voicecall', python: 'p', repo: '/r', script: ['voicecall/server.py'], root: '/h', env, extraEnv: ports.voicecall }))
  expect(call).toContain('<key>VOICECALL_PORT</key><string>8801</string>')
  const web = plist(pythonAgent({ name: 'moments-web', python: 'p', repo: '/r', script: ['moments/web.py'], root: '/h', env, extraEnv: ports['moments-web'] }))
  expect(web).toContain('<key>MOMENTS_WEB_PORT</key><string>8800</string>')
})
