// M6：开机自启的 LaunchAgent 内容。只放必要的环境变量，不放密钥；代理地址带账号密码的不写
import { expect, test } from 'bun:test'
import { baseEnv, directorAgent, gatewayAgent, logHint, plist, pythonAgent, tmuxLogsAgent } from '../../src/autostart'

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

test('tmux 日志看板（install-logs）：开机就建立、之后每 5 分钟一次；bun/tmux 的绝对路径写进环境', () => {
  const a = tmuxLogsAgent({ repo: '/r', root: '/h', env: { PATH: '/usr/bin', HOME: '/u' }, extraEnv: { BUN_BIN: '/u/.bun/bin/bun', TMUX_BIN: '/opt/homebrew/bin/tmux' } })
  expect(a.label).toBe('com.dsh-bot.tmux-logs')
  expect(a.args).toEqual(['/bin/bash', '/r/gateway/scripts/logs_tmux.sh'])
  expect(a.interval).toBe(300)
  expect(a.runAtLoad).toBe(true)
  expect(a.stdoutPath).toBe('/h/logs/tmux-logs.launchd.log')
  const x = plist(a)
  expect(x).toContain('<key>StartInterval</key><integer>300</integer>')
  expect(x).toContain('<key>RunAtLoad</key><true/>')
  expect(x).toContain('<key>BUN_BIN</key><string>/u/.bun/bin/bun</string>')
  expect(x).toContain('<key>TMUX_BIN</key><string>/opt/homebrew/bin/tmux</string>')
  expect(x).not.toContain('KeepAlive')
})

test('runAtLoad 只影响显式传它的任务：现有的主动消息、日历任务还是 RunAtLoad false', () => {
  const si = pythonAgent({ name: 'self-initiate.bot5', python: 'p', repo: '/r', script: ['scripts/self_initiate.py', 'bot5', '42'], root: '/h', env: {}, interval: 600 })
  expect(plist(si)).toContain('<key>RunAtLoad</key><false/>')
  const cal = pythonAgent({ name: 'jiwen', python: 'p', repo: '/r', script: ['jiwen/tick.py'], root: '/h', env: {}, calendar: { Weekday: 0, Hour: 4, Minute: 0 } })
  expect(plist(cal)).toContain('<key>RunAtLoad</key><false/>')
})

test('logHint：tmux-logs 指向自己的 launchd 日志', () => {
  expect(logHint('tmux-logs', '/root')).toBe('/root/logs/tmux-logs.launchd.log')
})
