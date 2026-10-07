// M6：开机自启的 LaunchAgent 内容。只放必要的环境变量，不放密钥；代理地址带账号密码的不写
import { expect, test } from 'bun:test'
import { baseEnv, directorAgent, gatewayAgent, plist } from '../../src/autostart'

test('网关和导演的 LaunchAgent：命令、工作目录、环境变量（关遥测、带代理、不带密钥）、自动拉起', () => {
  const env = baseEnv({
    home: '/Users/u', dshBotHome: '/Users/u/.dsh-bot', extraPath: ['/Users/u/.bun/bin'],
    env: { https_proxy: 'http://127.0.0.1:7897', ALL_PROXY: 'socks5://user:secret@127.0.0.1:1080', TELEGRAM_BOT_TOKEN: 'x', DEEPSEEK_API_KEY: 'sk-x' },
  })
  expect(env.PATH.startsWith('/Users/u/.bun/bin:')).toBe(true)
  expect(env.https_proxy).toBe('http://127.0.0.1:7897')
  expect(env.ALL_PROXY).toBeUndefined()
  expect(Object.keys(env).sort()).toEqual(['DSH_BOT_HOME', 'DSH_TELEMETRY_DISABLED', 'HOME', 'LANG', 'PATH', 'PYTHONIOENCODING', 'https_proxy'])
  const g = gatewayAgent({ botId: 'bot5', bun: '/Users/u/.bun/bin/bun', repo: '/r', configPath: '/Users/u/.dsh-bot/configs/bot5.yml', logsDir: '/Users/u/.dsh-bot/bots/bot5/logs', env })
  const x = plist(g)
  expect(g.label).toBe('com.dsh-bot.bot5')
  expect(x).toContain('<string>/r/gateway/src/main.ts</string>')
  expect(x).toContain('<key>KeepAlive</key><true/>')
  expect(x).toContain('<key>DSH_TELEMETRY_DISABLED</key><string>1</string>')
  expect(x).not.toContain('sk-x')
  expect(x).not.toContain('secret')
  const d = directorAgent({ python: '/usr/bin/python3', repo: '/r', chatId: '-1001', root: '/Users/u/.dsh-bot', env })
  expect(d.env).toMatchObject({ HUB_CONFIGS_DIR: '/Users/u/.dsh-bot/configs', DIRECTOR_CHAT_ID: '-1001', DIRECTOR_LOG_FILE: '/Users/u/.dsh-bot/director/director.log' })
  expect(plist({ ...d, args: ['a&b<c>'] })).toContain('a&amp;b&lt;c&gt;')
})

test('定时任务：每个 bot 的主动消息每 10 分钟；共用任务按日历；朋友圈网页常驻。都带上新系统的配置目录', () => {
  const { pythonAgent, SHARED } = require('../../src/autostart') as typeof import('../../src/autostart')
  const env = { PATH: '/usr/bin', HOME: '/Users/u' }
  const si = pythonAgent({ name: 'self-initiate.bot5', python: '/usr/bin/python3', repo: '/r', script: ['scripts/self_initiate.py', 'bot5', '42'], root: '/Users/u/.dsh-bot', env, interval: 600 })
  const x = plist(si)
  expect(si.args).toEqual(['/usr/bin/python3', '/r/scripts/run_logged.py', '/Users/u/.dsh-bot/logs/self-initiate.bot5.log', '/r/scripts/self_initiate.py', 'bot5', '42'])
  expect(x).toContain('<key>StartInterval</key><integer>600</integer>')
  expect(x).not.toContain('KeepAlive')
  expect(si.env.HUB_CONFIGS_DIR).toBe('/Users/u/.dsh-bot/configs')
  const cal = SHARED.find(j => j.name === 'memory-compactor')!
  const c = plist(pythonAgent({ name: cal.name, python: 'p', repo: '/r', script: cal.script, root: '/h', env, calendar: cal.calendar, extraEnv: { BOTLIFE_STATE_DB: '/old/state.db' } }))
  expect(c).toContain('<key>StartCalendarInterval</key>')
  expect(c).toContain('<key>Weekday</key><integer>0</integer>')
  expect(c).toContain('<key>BOTLIFE_STATE_DB</key><string>/old/state.db</string>')
  const web = SHARED.find(j => j.name === 'moments-web')!
  expect(plist(pythonAgent({ name: web.name, python: 'p', repo: '/r', script: web.script, root: '/h', env }))).toContain('<key>KeepAlive</key><true/>')
})

test('仓库在桌面、文稿、下载、iCloud 云盘下：认出来（安装会拒绝）；家目录下别的位置不算', () => {
  const { protectedDir } = require('../../src/autostart') as typeof import('../../src/autostart')
  expect(protectedDir('/Users/u/Desktop/claude/dshbot/dsh-bot-work', '/Users/u')).toBe('Desktop')
  expect(protectedDir('/Users/u/Documents', '/Users/u')).toBe('Documents')
  expect(protectedDir('/Users/u/Downloads/x/', '/Users/u')).toBe('Downloads')
  expect(protectedDir('/Users/u/Library/Mobile Documents/com~apple~CloudDocs/r', '/Users/u')).toBe('Library/Mobile Documents')
  expect(protectedDir('/Users/u/dsh-bot-work', '/Users/u')).toBeNull()
  expect(protectedDir('/Users/u/DesktopStuff/r', '/Users/u')).toBeNull()
  expect(protectedDir('/Users/u/.dsh-bot', '/Users/u')).toBeNull()
})

test('status：定时任务显示全"没在运行"、跑过几次、上次退出码；退出码不是 0 的标 ⚠️ 并指出日志', () => {
  const { parseLaunchctl, statusLine, logHint } = require('../../src/autostart') as typeof import('../../src/autostart')
  const timedOut = `gui/501/com.dsh-bot.self-initiate.bot5 = {\n\tactive count = 0\n\tstate = not running\n\n\truns = 33\n\tlast exit code = 1\n\trun interval = 600 seconds\n}`
  const s = parseLaunchctl(timedOut, 0)
  expect(s).toEqual({ state: '没在运行', pid: undefined, lastExit: '1', runs: '33' })
  const line = statusLine('com.dsh-bot.self-initiate.bot5', s, true, logHint('self-initiate.bot5'))
  expect(line.startsWith('⚠️')).toBe(true)
  expect(line).toContain('定时任务，没在运行，跑过 33 次，上次退出码 1')
  expect(line).toContain('~/.dsh-bot/logs/self-initiate.bot5.log')
  const okTimed = statusLine('com.dsh-bot.self-initiate.bot5', parseLaunchctl('\tstate = not running\n\truns = 2\n\tlast exit code = 0\n', 0), true, 'x')
  expect(okTimed.startsWith('⚠️')).toBe(false)
  expect(okTimed).toContain('上次退出码 0')
  expect(statusLine('l', parseLaunchctl('\tstate = not running\n\tlast exit code = (never exited)\n', 0), true, 'x')).toContain('还没跑过')
  // 常驻的网关：在跑就没问题，被杀过一次又拉起来的不报
  const gw = parseLaunchctl('\tstate = running\n\tpid = 78785\n\tlast terminating signal = Terminated: 15\n', 0)
  const gl = statusLine('com.dsh-bot.bot5', gw, false, logHint('bot5'))
  expect(gl.startsWith('⚠️')).toBe(false)
  expect(gl).toContain('运行中（pid 78785）')
  const down = statusLine('com.dsh-bot.bot5', parseLaunchctl('\tstate = not running\n\tlast exit code = 78\n', 0), false, logHint('bot5'))
  expect(down.startsWith('⚠️')).toBe(true)
  expect(down).toContain('~/.dsh-bot/bots/bot5/logs/launchd.log')
  expect(statusLine('x', parseLaunchctl('Could not find service', 113), false, 'y').startsWith('⚠️')).toBe(true)
  expect(logHint('director')).toBe('~/.dsh-bot/director/director.log')
  expect(logHint('jiwen')).toContain('~/.dsh-bot/logs/jiwen.log')
})
