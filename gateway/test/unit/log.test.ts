import { expect, test } from 'bun:test'
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { Logger, redact, registerSecret, RotatingFile, safeError } from '../../src/log'

test('脱敏：Telegram 令牌、sk- 密钥、Bearer 口令、x-api-key、URL 里的口令', () => {
  const tok = '123456789:TESTONLY_not_a_real_token_0000000000'
  expect(redact(`https://api.telegram.org/bot${tok}/getUpdates`)).not.toContain('TESTONLY')
  expect(redact(`token=${tok}`)).not.toContain('TESTONLY')
  expect(redact('key sk-abcdefghijklmnop1234')).toBe('key ***')
  expect(redact('Authorization: Bearer abcdefghijklmnopqrstuvwxyz')).toBe('Authorization: Bearer ***')
  expect(redact('{"x-api-key": "secretsecret123"}')).not.toContain('secretsecret123')
  expect(redact('http://user:pa55word@proxy:8080')).not.toContain('pa55word')
})

test('脱敏：登记过的确切机密值，无论什么格式都会被替换', () => {
  registerSecret('my-very-own-secret-value')
  expect(redact('xx my-very-own-secret-value yy')).toBe('xx *** yy')
})

test('safeError 不带出请求地址里的令牌', () => {
  const e = Object.assign(new Error('fetch failed for https://api.telegram.org/bot123456789:TESTONLY_not_a_real_token_0000000000/sendMessage'), { code: 'ECONNRESET' })
  const s = safeError(e)
  expect(s).toContain('code=ECONNRESET')
  expect(s).not.toContain('TESTONLY')
})

test('日志按大小轮转，最多保留 keep 份', () => {
  const dir = mkdtempSync(join(tmpdir(), 'logrot-'))
  const f = new RotatingFile(join(dir, 'a.log'), 200, 2)
  for (let i = 0; i < 40; i++) f.write(`line ${i} ${'x'.repeat(30)}`)
  const files = readdirSync(dir).sort()
  expect(files).toEqual(['a.log', 'a.log.1', 'a.log.2'])
  expect(readFileSync(join(dir, 'a.log'), 'utf8').length).toBeLessThan(400)
  rmSync(dir, { recursive: true, force: true })
})

test('Logger 写盘前脱敏', () => {
  const dir = mkdtempSync(join(tmpdir(), 'logger-'))
  const log = new Logger({ dir })
  log.info('x', { url: 'https://api.telegram.org/bot123456789:TESTONLY_not_a_real_token_0000000000/x' })
  log.chatLine('bot', '1', 'hello sk-abcdefghijklmnop1234')
  expect(readFileSync(join(dir, 'gateway.log'), 'utf8')).not.toContain('TESTONLY')
  expect(readFileSync(join(dir, 'chat.log'), 'utf8')).not.toContain('sk-abcdef')
  expect(existsSync(join(dir, 'chat.log'))).toBe(true)
  rmSync(dir, { recursive: true, force: true })
})

test('看日志：一行一条，时间在前，告警和错误有标记；对话行换成一行；读不懂的行原样给', () => {
  const { formatEvent, formatChat } = require('../../src/logview') as typeof import('../../src/logview')
  const at = '2026-10-06T15:41:05.000Z'
  const ev = formatEvent(JSON.stringify({ at, level: 'warn', event: 'tool.reply_slow', bot: 'bot5', turn: 60, detail: 'a b' }))
  expect(ev).toMatch(/^\d{2}:\d{2}:\d{2} {2}⚠ tool\.reply_slow {2}turn=60 detail=a b$/)
  expect(formatEvent('not json')).toBe('not json')
  expect(formatChat(`${at} [42] bot: 第一行⏎第二行`)).toMatch(/^\d{2}:\d{2}:\d{2} {2}💬 \[42\] bot: 第一行 \/ 第二行$/)
})
