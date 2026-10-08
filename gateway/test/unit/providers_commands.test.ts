// 供应商命令与纯函数（方案 3.6、3.7）：/provider add 的识别、刷新文案、上游错误归类、NO_PROXY 追加、身份键。
import { expect, test } from 'bun:test'
import { classifyUpstreamError, brainKey, identityOf } from '../../src/engine/engine'
import { isProviderAdd, ProviderCommands } from '../../src/providers/commands'
import { Logger } from '../../src/log'
import { addNoProxyHosts } from '../../src/util'
import type { Brain } from '../../src/config'

test('/provider add 的识别：文字/附言、去空白、可选 @bot、不分大小写；别的不认', () => {
  for (const ok of ['/provider add a b c', '  /provider   add x  ', '/PROVIDER ADD x', '/provider@test_dsh_bot add a b c', '/provider add']) expect(isProviderAdd(ok)).toBe(true)
  for (const no of ['/provider', '/provider addx', '/provider refresh a', 'provider add a', '/model add a']) expect(isProviderAdd(no)).toBe(false)
  expect(isProviderAdd(undefined)).toBe(false)
})

function commands(): ProviderCommands {
  return new ProviderCommands({
    service: {} as never, api: {} as never, log: new Logger({}), root: '/root',
    isOwner: () => true, botUsername: () => 'test_dsh_bot', configRouteNames: () => [],
    reply: () => {}, recordUpdate: () => {},
  })
}

test('刷新结果文案（3.4.5/3.6）：成功带统计与去掉了、失败保持原列表、忙/缺密钥/被改/已不在', () => {
  const c = commands()
  expect(c.refreshText('p1', { status: 'ok', name: 'p1', count: 3, added: 2, removed: ['m2'], keptManual: 1 }))
    .toBe('【系统】「p1」拉到 3 个模型（新增 2 个，去掉 1 个；手动加的 1 个保留）。\n去掉了：m2')
  expect(c.refreshText('p1', { status: 'ok', name: 'p1', count: 2, added: 0, removed: [], keptManual: 0 }))
    .toBe('【系统】「p1」拉到 2 个模型（新增 0 个，去掉 0 个；手动加的 0 个保留）。')
  expect(c.refreshText('p1', { status: 'failed', name: 'p1', reason: 'server_error', status2: 500 })).toBe('【系统】「p1」没拉到模型：对方接口出错（HTTP 500）。模型列表保持不变。')
  expect(c.refreshText('p1', { status: 'gone', name: 'p1' })).toContain('已经不在了，这次结果没保存')
  expect(c.refreshText('p1', { status: 'changed', name: 'p1' })).toContain('刚被别处改过，这次结果没保存，请再刷新一次')
  expect(c.refreshText('p1', { status: 'busy', name: 'p1' })).toBe('【系统】「p1」正在刷新，请稍候。')
  expect(c.refreshText('p1', { status: 'key_missing' })).toContain('缺密钥')
  expect(c.refreshText('p1', { status: 'lock_timeout' })).toBe('【系统】别的 bot 正在改供应商，请稍后再试。')
})

test('上游错误归类（3.2 第 10、11 条）：只看文字、不分大小写；无关错误不归类', () => {
  for (const m of ["400 This model's maximum context length is 8192 tokens", 'context_length_exceeded', 'Prompt Is Too Long: 1', 'TOO MANY TOKENS', 'input exceeds context_window', 'exceeds the context length']) expect(classifyUpstreamError(m)).toBe('context-overflow')
  for (const m of ['400 Missing `reasoning_content` field', 'Thinking blocks are required']) expect(classifyUpstreamError(m)).toBe('thinking-history')
  expect(classifyUpstreamError('upstream returned 502 Bad Gateway')).toBe(null)
})

test('供应商身份与键：brainKey 取名字/模型/强度；identityOf 取第一个 / 前', () => {
  const b: Brain = { provider: 'deepseek-official', model: 'deepseek-flash', reasoningEffort: 'low', routes: {}, emergencyCompaction: false }
  expect(brainKey(b)).toBe('deepseek-official/deepseek-flash/low')
  expect(identityOf('myproxy@1700/m1/')).toBe('myproxy@1700')
  expect(identityOf('deepseek-official/deepseek-flash/')).toBe('deepseek-official')
})

test('NO_PROXY 追加：保留原有值、去重、NO_PROXY 与 no_proxy 都写', () => {
  const env: Record<string, string | undefined> = { NO_PROXY: 'example.com', no_proxy: '127.0.0.1' }
  addNoProxyHosts(env)
  expect(env.NO_PROXY!.split(',')).toContain('example.com')
  for (const h of ['127.0.0.1', 'localhost', '::1']) expect(env.NO_PROXY!.split(',')).toContain(h)
  expect(env.no_proxy!.split(',').filter(x => x === '127.0.0.1').length).toBe(1) // 去重
  expect(env.no_proxy!.split(',')).toContain('localhost')
  // 原本没有就新建
  const empty: Record<string, string | undefined> = {}
  addNoProxyHosts(empty)
  expect(empty.NO_PROXY).toBe('127.0.0.1,localhost,::1')
})
