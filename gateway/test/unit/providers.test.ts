// /provider add 的纯函数：认出带密钥的命令、解析参数（出错说明里不带密钥）、写凭据文件、解析模型列表
import { expect, test } from 'bun:test'
import { mkdtempSync, readFileSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { envNameFor, isSecretCommand, modelsUrl, parseAddArgs, parseModels, removeCredential, setCredential, sharedRoutes } from '../../src/providers'

test('认出 /provider add（带 @bot、大小写、/providers 都算）；别的 /provider 命令不算', () => {
  expect(isSecretCommand('/provider add a http://x k')).toBe(true)
  expect(isSecretCommand('  /Provider@my_bot ADD a b c')).toBe(true)
  expect(isSecretCommand('/providers add')).toBe(true)
  expect(isSecretCommand('/provider')).toBe(false)
  expect(isSecretCommand('/provider address')).toBe(false)
  expect(isSecretCommand('/provider openrouter')).toBe(false)
  expect(isSecretCommand('帮我 /provider add')).toBe(false)
})

test('解析参数：默认 Anthropic，末尾 openai 是 OpenAI 兼容；名字转小写；出错的说明里没有密钥', () => {
  expect(parseAddArgs('/provider add OpenRouter https://openrouter.ai/api/v1/ sk-or-12345678 openai')).toEqual({ ok: { name: 'openrouter', displayName: 'OpenRouter', baseURL: 'https://openrouter.ai/api/v1', key: 'sk-or-12345678', api: 'openai-completions' } })
  expect(parseAddArgs('/provider add proxy http://127.0.0.1:8080 sk-12345678')).toMatchObject({ ok: { api: 'anthropic-messages' } })
  for (const bad of ['/provider add', '/provider add a b', '/provider add a http://x sk-12345678 claude', '/provider add a_b http://x sk-12345678', '/provider add deepseek-official http://x sk-12345678', '/provider add a ftp://x sk-12345678', '/provider add a http://u:p@x sk-12345678', '/provider add a http://x short']) {
    const r = parseAddArgs(bad) as { error: string }
    expect(r.error).toBeTruthy()
    expect(r.error).not.toContain('sk-')
    expect(r.error).not.toContain('short')
  }
  expect(envNameFor('my-proxy')).toBe('PROVIDER_MY_PROXY_API_KEY')
})

test('写凭据文件：别的内容和注释原样留着，同名键替换，权限 600；删除只删这一行', () => {
  const d = mkdtempSync(join(tmpdir(), 'cred-'))
  const p = join(d, 'credentials.yaml')
  writeFileSync(p, '# 注释\nversion: 1\nrefs:\n  DEEPSEEK_API_KEY: sk-aaa\n')
  expect(setCredential(p, 'PROVIDER_X_API_KEY', 'k"e\\y#1234')).toBeNull()
  expect(setCredential(p, 'PROVIDER_X_API_KEY', 'second-key-123')).toBeNull()
  const t = readFileSync(p, 'utf8')
  expect(t).toBe('# 注释\nversion: 1\nrefs:\n  PROVIDER_X_API_KEY: "second-key-123"\n  DEEPSEEK_API_KEY: sk-aaa\n')
  if (process.platform !== 'win32') expect(require('fs').statSync(p).mode & 0o777).toBe(0o600)
  removeCredential(p, 'PROVIDER_X_API_KEY')
  expect(readFileSync(p, 'utf8')).toBe('# 注释\nversion: 1\nrefs:\n  DEEPSEEK_API_KEY: sk-aaa\n')
  // 特殊字符能原样读回
  expect(setCredential(p, 'PROVIDER_Y_API_KEY', 'a"b\\c:d#e')).toBeNull()
  expect((Bun.YAML.parse(readFileSync(p, 'utf8')) as any).refs.PROVIDER_Y_API_KEY).toBe('a"b\\c:d#e')
  // 没有文件：新建
  const q = join(d, 'new.yaml')
  expect(setCredential(q, 'PROVIDER_Z_API_KEY', 'zzzzzzzz')).toBeNull()
  expect((Bun.YAML.parse(readFileSync(q, 'utf8')) as any)).toEqual({ version: 1, refs: { PROVIDER_Z_API_KEY: 'zzzzzzzz' } })
})

test('模型列表：OpenAI 的 data、Anthropic 的 data、models 对象都认；窗口取常见字段；地址末尾有没有 /v1 都行', () => {
  expect(parseModels({ data: [{ id: 'a', context_length: 64000 }, { id: 'a' }, { id: '' }, 'b'] })).toEqual([{ id: 'a', contextWindow: 64000 }, { id: 'b' }])
  expect(parseModels({ data: [{ id: 'claude-x', display_name: 'Claude X', max_input_tokens: 200000 }] })).toEqual([{ id: 'claude-x', name: 'Claude X', contextWindow: 200000 }])
  expect(parseModels({ models: { m1: { context_window: 1000 } } })).toEqual([{ id: 'm1', contextWindow: 1000 }])
  expect(parseModels({ nothing: 1 })).toEqual([])
  expect(modelsUrl('openai-completions', 'https://x/v1/')).toBe('https://x/v1/models')
  expect(modelsUrl('anthropic-messages', 'https://x/anthropic')).toBe('https://x/anthropic/v1/models?limit=1000')
  expect(modelsUrl('anthropic-messages', 'https://x/v1')).toBe('https://x/v1/models?limit=1000')
})

test('并进 dsh 路由的：只有 dsh 认的字段；没有模型、协议不对、名字不合规的不并', () => {
  const d = mkdtempSync(join(tmpdir(), 'prov-'))
  const p = join(d, 'providers.json')
  const base = { displayName: 'A', api: 'openai-completions', baseURL: 'https://a/v1', apiKeyEnv: 'PROVIDER_A_API_KEY', fetchedAt: 1, fetchError: null, addedBy: 'bot5', addedAt: 1 }
  writeFileSync(p, JSON.stringify({ version: 1, providers: {
    a: { ...base, models: [{ id: 'm', contextWindow: 5000 }, { id: 'n' }] },
    empty: { ...base, models: [] },
    weird: { ...base, api: 'something', models: [{ id: 'm' }] },
    'Bad_Name': { ...base, models: [{ id: 'm' }] },
  } }))
  expect(sharedRoutes(p)).toEqual({ a: { displayName: 'A', api: 'openai-completions', baseURL: 'https://a/v1', apiKeyEnv: 'PROVIDER_A_API_KEY', models: [{ id: 'm', contextWindow: 5000 }, { id: 'n', contextWindow: 128000 }] } })
  expect(sharedRoutes(join(d, 'missing.json'))).toEqual({})
})

test('配置文件里的同名路由优先；共用的供应商可以写进配置的 brain.provider', () => {
  const { parseBrain } = require('../../src/config') as typeof import('../../src/config')
  const shared = { a: { api: 'openai-completions', baseURL: 'https://shared/v1', apiKeyEnv: 'S', models: [{ id: 'm' }] } }
  const own = { a: { api: 'openai-completions', baseURL: 'https://own/v1', models: [{ id: 'x' }] } }
  expect(parseBrain({ routes: own }, shared).routes.a!.baseURL).toBe('https://own/v1')
  expect(parseBrain({ provider: 'a', model: 'm' }, shared).routes.a!.baseURL).toBe('https://shared/v1')
})
