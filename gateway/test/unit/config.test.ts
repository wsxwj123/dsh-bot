import { expect, test } from 'bun:test'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { ConfigError, loadBotConfig, parseBrain, parseDotenv, readTelegramToken } from '../../src/config'

test('brain：默认 deepseek-official / deepseek-flash', () => {
  const b = parseBrain(undefined)
  expect(b.provider).toBe('deepseek-official')
  expect(b.model).toBe('deepseek-flash')
  expect(b.maxInputTokens).toBeUndefined()
  expect(b.emergencyCompaction).toBe(false)
})

test('brain：自定义路由必须写全；provider 要指向存在的路由', () => {
  expect(() => parseBrain({ provider: 'proxy' })).toThrow(ConfigError)
  expect(() => parseBrain({ routes: { proxy: { api: 'anthropic-messages' } } })).toThrow(ConfigError)
  expect(() => parseBrain({ routes: { Bad_Name: { api: 'x', baseURL: 'y', models: [{ id: 'm' }] } } })).toThrow(ConfigError)
  const b = parseBrain({ provider: 'proxy', model: 'gemini', routes: { proxy: { api: 'anthropic-messages', baseURL: 'http://127.0.0.1:1', apiKeyEnv: 'PROXY_KEY', models: [{ id: 'gemini', contextWindow: 1000000 }] } } })
  expect(b.routes.proxy!.models[0]!.contextWindow).toBe(1000000)
  expect(() => parseBrain({ reasoning_effort: 'extreme' })).toThrow(ConfigError)
})

test('配置：目录约定、端口、主人默认取 allowFrom 第一个', () => {
  const root = mkdtempSync(join(tmpdir(), 'cfg-'))
  const ch = join(root, 'ch')
  mkdirSync(ch)
  writeFileSync(join(ch, 'access.json'), JSON.stringify({ allowFrom: ['42', '43'] }))
  const p = join(root, 'b.yml')
  writeFileSync(p, `id: bot5\nbot_channel_path: ${ch}\ndispatcher_port: 17805\nbrain:\n  model: deepseek-v4-pro\ngateway:\n  timezone: Europe/Berlin\n`)
  const c = loadBotConfig(p, { DSH_BOT_HOME: join(root, 'home') })
  expect(c.botDir).toBe(join(root, 'home', 'bots', 'bot5'))
  expect(c.dshHome).toBe(join(root, 'home', 'bots', 'bot5', 'dsh-home'))
  expect(c.credentialsPath).toBe(join(root, 'home', 'credentials.yaml'))
  expect(c.apiPort).toBe(17805)
  expect(c.gw.owners).toEqual(['42'])
  expect(c.gw.timezone).toBe('Europe/Berlin')
  expect(c.brain.model).toBe('deepseek-v4-pro')
  expect(c.lifeId).toBe('bot5')
  // 朋友圈、画风里用旧名字：life_id，没写就取 life_config 的文件名
  writeFileSync(p, `id: bot5\nbot_channel_path: ${ch}\nlife_config: ~/old/configs/chen.yml\n`)
  expect(loadBotConfig(p, { DSH_BOT_HOME: join(root, 'home') }).lifeId).toBe('chen')
  writeFileSync(p, `id: bot5\nbot_channel_path: ${ch}\nlife_config: ~/old/configs/chen.yml\nlife_id: lulu\n`)
  expect(loadBotConfig(p, { DSH_BOT_HOME: join(root, 'home') }).lifeId).toBe('lulu')
  rmSync(root, { recursive: true, force: true })
})

test('令牌：优先读频道目录的 .env，找不到才看环境变量', () => {
  const root = mkdtempSync(join(tmpdir(), 'tok-'))
  expect(() => readTelegramToken(root, {})).toThrow(ConfigError)
  expect(readTelegramToken(root, { TELEGRAM_BOT_TOKEN: 'fromenv' })).toBe('fromenv')
  writeFileSync(join(root, '.env'), '# c\nexport TELEGRAM_BOT_TOKEN="fromfile"\n')
  expect(readTelegramToken(root, { TELEGRAM_BOT_TOKEN: 'fromenv' })).toBe('fromfile')
  expect(parseDotenv("A=1\nB='two'\n bad line\n")).toEqual({ A: '1', B: 'two' })
  rmSync(root, { recursive: true, force: true })
})
