// scripts/autostart.ts 的 status 增强（搬自上游 bc8727d 的那一项）：⚠️ 标记、"跑过 N 次"、日志提示、非零退出码。
// 只测新加的纯函数；保护 parseLaunchctlPrint 等三个被验收测试锁定的函数不变。
import { expect, test } from 'bun:test'
import { logHint, parseLaunchctlRuns, statusLine } from '../../src/autostart'

const svc = (body: string) => `gui/501/com.dsh-bot.bot5 = {\n${body}\n}\n`

test('parseLaunchctlRuns：取顶层 runs；嵌套的不算；退出码非 0 或没有该行 → null', () => {
  expect(parseLaunchctlRuns(svc('\tstate = running\n\truns = 12'), 0)).toBe('12')
  expect(parseLaunchctlRuns(svc('\tendpoints = {\n\t\truns = 99\n\t}\n\tstate = waiting'), 0)).toBeNull()
  expect(parseLaunchctlRuns(svc('\tstate = running'), 0)).toBeNull()
  expect(parseLaunchctlRuns('Could not find service\n', 113)).toBeNull()
})

test('statusLine：常驻在跑不标 ⚠️；没在跑标 ⚠️ 并给日志提示', () => {
  const run = statusLine('com.dsh-bot.bot5', { state: 'running', pid: '42', lastExit: '0' }, false, null, 'hint')
  expect(run.startsWith('  ')).toBe(true)
  expect(run).toContain('running（pid 42）')
  expect(run).toContain('上次退出码 0')
  expect(run).not.toContain('⚠️')

  const down = statusLine('com.dsh-bot.bot5', { state: 'not running', pid: null, lastExit: '1' }, false, null, '/r/bots/bot5/logs/launchd.log')
  expect(down.startsWith('⚠️')).toBe(true)
  expect(down).toContain('看日志：/r/bots/bot5/logs/launchd.log')
})

test('statusLine：定时任务看跑过几次、上次退出码是否为 0', () => {
  const ok = statusLine('com.dsh-bot.jiwen', { state: 'not running', pid: null, lastExit: '0' }, true, '5', 'hint')
  expect(ok.startsWith('  ')).toBe(true)
  expect(ok).toContain('定时任务，')
  expect(ok).toContain('跑过 5 次')
  expect(ok).toContain('上次退出码 0')

  const never = statusLine('com.dsh-bot.jiwen', { state: 'not running', pid: null, lastExit: null }, true, null, 'hint')
  expect(never).toContain('还没跑过')
  expect(never).toContain('上次退出码 未知')

  const bad = statusLine('com.dsh-bot.jiwen', { state: 'not running', pid: null, lastExit: '78: EX_CONFIG' }, true, '3', 'hint')
  expect(bad.startsWith('⚠️')).toBe(true)
  expect(bad).toContain('78: EX_CONFIG')
})

test('statusLine：launchd 里没有这个任务（退出码非 0）标 ⚠️', () => {
  const gone = statusLine('com.dsh-bot.bot5', { state: '没在运行（launchd 里没有这个任务）', pid: null, lastExit: null }, false, null, 'hint')
  expect(gone.startsWith('⚠️')).toBe(true)
})

test('logHint：网关、定时任务/朋友圈、导演各自的日志位置', () => {
  expect(logHint('director', '/root')).toBe('/root/director/director.log')
  expect(logHint('bot5', '/root')).toBe('/root/bots/bot5/logs/launchd.log')
  expect(logHint('jiwen', '/root')).toBe('/root/logs/jiwen.log 和 jiwen.launchd.log')
  expect(logHint('self-initiate.bot5', '/root')).toBe('/root/logs/self-initiate.bot5.log 和 self-initiate.bot5.launchd.log')
})
