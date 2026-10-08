// 自建供应商共用文件（方案 3.1）：名字/地址/键名规则、读校验、并进路由、文件锁、凭据 refs 行编辑
import { afterEach, expect, test } from 'bun:test'
import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import type { Route } from '../../src/config'
import { Logger, redact } from '../../src/log'
import {
  apiKeyEnvFor, checkBaseURL, checkProviderName, CredentialsUnsupported, CredentialsVerifyFailed, editRefsText, mergeRoutes, parseProviders,
  providersLockPath, ProvidersLockTimeout, ProvidersUnreadable, readProviders, registerCredentialSecrets, setCredentialRef, withProvidersLock,
} from '../../src/providers/store'
import { sleep } from '../../src/util'

const dirs: string[] = []
const tmp = () => { const d = mkdtempSync(join(tmpdir(), 'pstore-')); dirs.push(d); return d }
afterEach(() => { for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true }) })
class CapLog extends Logger {
  events: Record<string, unknown>[] = []
  override log(_l: string, event: string, f: Record<string, unknown> = {}): void { this.events.push({ event, ...f }) }
}
const entry = (o: Record<string, unknown> = {}, models: unknown[] = [{ id: 'm1', contextWindow: 65536 }]) =>
  ({ route: { api: 'openai-completions', baseURL: 'https://a.example.com/v1', apiKeyEnv: 'PROVIDER_A_KEY', models, ...o }, meta: { epoch: 5, keyRev: 2 } })
const file = (providers: Record<string, unknown>, pendingKeyRemovals: unknown[] = []) => JSON.stringify({ version: 1, providers, pendingKeyRemovals })

test('名字：格式、32 个字符上限、保留字按规整名比较', () => {
  for (const ok of ['a', 'My_Proxy', 'x-1', '9z', 'a'.repeat(32)]) expect(checkProviderName(ok)).toBeNull()
  for (const bad of ['', '-a', '_a', 'a b', 'a.b', '中文', 'a'.repeat(33)]) expect(checkProviderName(bad)).toContain('名字只能用英文字母')
  for (const r of ['add', 'HELP', 'Deepseek_Official', 'default']) expect(checkProviderName(r)).toBe(`${r} 是内置、配置文件里的供应商或保留字，不能用，换个名字`)
})

test('地址：https、本机才可 http、不带账号密码和 ? #、300 字上限；规范化去末尾 / ，Anthropic 去末尾 /v1', () => {
  expect(checkBaseURL('  https://a.com/v1/  ', 'openai-completions')).toEqual({ ok: true, url: 'https://a.com/v1', strippedV1: false })
  expect(checkBaseURL('https://a.com/v1/', 'anthropic-messages')).toEqual({ ok: true, url: 'https://a.com', strippedV1: true })
  expect(checkBaseURL('https://v1', 'anthropic-messages')).toEqual({ ok: true, url: 'https://v1', strippedV1: false })
  expect(checkBaseURL('HTTPS://A.com', 'openai-completions').ok).toBe(true)
  for (const local of ['http://127.0.0.1:8080/v1', 'http://localhost', 'http://LOCALHOST:1/x', 'http://[::1]:9/v1']) expect(checkBaseURL(local, 'openai-completions').ok).toBe(true)
  const why = (u: string) => { const r = checkBaseURL(u, 'openai-completions'); return r.ok ? null : r.why }
  for (const remote of ['http://127.0.0.2', 'http://localhost.evil.com', 'http://127.0.0.1.nip.io', 'http://example.com/v1']) expect(why(remote)).toBe('只有本机地址（127.0.0.1、localhost、[::1]）可以用 http://，其它地址请用 https://')
  expect(why('ftp://a.com')).toBe('地址格式不对，要以 https:// 开头')
  expect(why('a.com/v1')).toBe('地址格式不对，要以 https:// 开头')
  expect(why('https://u:p@a.com')).toBe('地址里不能带用户名和密码')
  expect(why('https://a.com/v1?x=1')).toBe('地址里不能带 ? 或 # 后面的部分')
  expect(why('https://a.com/v1#frag')).toBe('地址里不能带 ? 或 # 后面的部分')
  expect(why(`https://a.com/${'x'.repeat(290)}`)).toBe('地址太长（最多 300 个字符）')
})

test('键名：PROVIDER_<规整名大写>_KEY，被占用依次加 _2、_3', () => {
  expect(apiKeyEnvFor('My_Proxy', [])).toBe('PROVIDER_MY_PROXY_KEY')
  expect(apiKeyEnvFor('my-proxy', ['PROVIDER_MY_PROXY_KEY'])).toBe('PROVIDER_MY_PROXY_KEY_2')
  expect(apiKeyEnvFor('my-proxy', ['PROVIDER_MY_PROXY_KEY', 'PROVIDER_MY_PROXY_KEY_2'])).toBe('PROVIDER_MY_PROXY_KEY_3')
})

test('读校验：不合格的条目跳过并给原因；重名（规整名）、共用键名只认先出现的', () => {
  const s = parseProviders(file({
    good: entry(),
    'bad name': entry({ apiKeyEnv: 'PROVIDER_B_KEY' }),
    badapi: entry({ api: 'grpc', apiKeyEnv: 'PROVIDER_C_KEY' }),
    badurl: entry({ baseURL: 'http://example.com/v1', apiKeyEnv: 'PROVIDER_D_KEY' }),
    badkey: entry({ apiKeyEnv: 'DEEPSEEK_API_KEY' }),
    Good_: entry({ apiKeyEnv: 'PROVIDER_E_KEY' }),
    GOOD: entry({ apiKeyEnv: 'PROVIDER_F_KEY' }),
    samekey: entry(),
  }))
  expect(Object.keys(s.valid)).toEqual(['good', 'Good_'])
  const why = Object.fromEntries(s.invalid.map(x => [x.name, x.why]))
  expect(Object.keys(why).sort()).toEqual(['GOOD', 'bad name', 'badapi', 'badkey', 'badurl', 'samekey'])
  expect(why.GOOD).toBe('和自建供应商「good」同名')
  expect(why.samekey).toBe('密钥键名和「good」重复')
  expect(s.valid.good!.meta).toMatchObject({ epoch: 5, keyRev: 2, manualModels: [], lastRefresh: null })
})

test('读校验：模型条目只丢不合格的；没写上下文按 131072；思考档位声明不合格只丢这个字段', () => {
  const s = parseProviders(file({ p: entry({}, [
    { id: ' ok-1 ', contextWindow: 32000 }, { id: 'has space' }, { id: 'ok-1' }, { id: 'big', contextWindow: 1e9 }, { id: 'nocw' },
    { id: 'r1', reasoningEfforts: { high: 'high', off: null } }, { id: 'r2', reasoningEfforts: { turbo: 'x' } }, { id: 'r3', reasoningEfforts: false },
  ]) }))
  expect(s.valid.p!.route.models).toEqual([
    { id: 'ok-1', contextWindow: 32000 }, { id: 'nocw', contextWindow: 131072 },
    { id: 'r1', contextWindow: 131072, reasoningEfforts: { high: 'high', off: null } }, { id: 'r2', contextWindow: 131072 }, { id: 'r3', contextWindow: 131072, reasoningEfforts: false },
  ])
})

test('读不了：不是 JSON、顶层不是对象、版本不认识、providers 不是对象；文件不存在 = 空', () => {
  for (const bad of ['{ broken', '[]', JSON.stringify({ version: 2, providers: {} }), JSON.stringify({ version: 1, providers: [] })]) expect(() => parseProviders(bad)).toThrow(ProvidersUnreadable)
  expect(readProviders(join(tmp(), 'providers.json'))).toMatchObject({ valid: {}, invalid: [], pendingKeyRemovals: [] })
  const s = parseProviders(file({}, [{ key: 'PROVIDER_X_KEY', after: 5 }, { key: 'DEEPSEEK_API_KEY', after: 1 }, { key: 'PROVIDER_Y_KEY' }]))
  expect(s.pendingKeyRemovals).toEqual([{ key: 'PROVIDER_X_KEY', after: 5 }])
  expect(s.droppedPending).toBe(2)
})

test('并进路由：配置文件同名的遮住；键名与配置文件路由相同的不启用；没模型的启用但不交给 dsh；只交白名单字段', () => {
  const cfg: Record<string, Route> = { 'my-proxy': { api: 'openai-completions', baseURL: 'https://c.example.com/v1', apiKeyEnv: 'PROVIDER_STEAL_KEY', models: [{ id: 'c1' }] } }
  const s = parseProviders(file({
    My_Proxy: entry({ apiKeyEnv: 'PROVIDER_MY_PROXY_KEY' }),
    steal: entry({ apiKeyEnv: 'PROVIDER_STEAL_KEY' }),
    empty: entry({ apiKeyEnv: 'PROVIDER_EMPTY_KEY' }, []),
    ok: entry({ apiKeyEnv: 'PROVIDER_OK_KEY', extraField: 'x' }, [{ id: 'm1', contextWindow: 65536, cost: 3 }]),
    'bad name': entry({ apiKeyEnv: 'PROVIDER_Z_KEY' }),
  }))
  const m = mergeRoutes(cfg, s)
  expect(m.shadowed).toEqual(['My_Proxy'])
  expect(m.invalid.map(x => x.name).sort()).toEqual(['bad name', 'steal'])
  expect(Object.keys(m.enabled).sort()).toEqual(['empty', 'ok'])
  expect(Object.keys(m.routes).sort()).toEqual(['my-proxy', 'ok'])
  expect(m.routes.ok).toEqual({ api: 'openai-completions', baseURL: 'https://a.example.com/v1', apiKeyEnv: 'PROVIDER_OK_KEY', models: [{ id: 'm1', contextWindow: 65536 }] })
  expect(mergeRoutes(cfg, null).routes).toEqual(cfg)
})

test('refs 行编辑：加在块里最后一个键后、照现有缩进；替换、删除只动那一行；没有 refs 时补一个块', () => {
  const four = '# 注释\nversion: 1\nrefs:\n    DEEPSEEK_API_KEY: "sk-1"\n\n# 顶格注释\nrecords: {}\n'
  expect(editRefsText(four, 'PROVIDER_A_KEY', 'k"\\1')).toBe('# 注释\nversion: 1\nrefs:\n    DEEPSEEK_API_KEY: "sk-1"\n    PROVIDER_A_KEY: "k\\"\\\\1"\n\n# 顶格注释\nrecords: {}\n')
  const two = 'version: 1\nrefs:\n  PROVIDER_A_KEY: "old"\n  PROVIDER_A_KEY_2: "keep"\n'
  expect(editRefsText(two, 'PROVIDER_A_KEY', 'new')).toBe('version: 1\nrefs:\n  PROVIDER_A_KEY: "new"\n  PROVIDER_A_KEY_2: "keep"\n')
  expect(editRefsText(two, 'PROVIDER_A_KEY', null)).toBe('version: 1\nrefs:\n  PROVIDER_A_KEY_2: "keep"\n')
  expect(editRefsText(two, 'PROVIDER_NOPE_KEY', null)).toBe(two)
  expect(editRefsText('version: 1\nrefs:\n', 'PROVIDER_A_KEY', 'v')).toBe('version: 1\nrefs:\n  PROVIDER_A_KEY: "v"\n')
  expect(editRefsText('version: 1', 'PROVIDER_A_KEY', 'v')).toBe('version: 1\nrefs:\n  PROVIDER_A_KEY: "v"\n')
  expect(editRefsText('version: 1\r\nrefs:\r\n  A: "x"\r\n', 'PROVIDER_A_KEY', 'v')).toBe('version: 1\r\nrefs:\r\n  A: "x"\r\n  PROVIDER_A_KEY: "v"\r\n')
  expect(() => editRefsText('version: 1\nrefs: {A: "x"}\n', 'PROVIDER_A_KEY', 'v')).toThrow(new CredentialsUnsupported('不是块状写法'))
  expect(() => editRefsText('refs:\n  A: "x"\n    B: "y"\n', 'PROVIDER_A_KEY', 'v')).toThrow(new CredentialsUnsupported('缩进不一致'))
})

test('写凭据：新建文件内容固定、600；已有文件其它字节不变；只认 PROVIDER_…_KEY；写后登记全部值', () => {
  const d = tmp()
  const p = join(d, 'credentials.yaml')
  setCredentialRef(p, 'PROVIDER_A_KEY', 'test-key-AAAA1111')
  expect(readFileSync(p, 'utf8')).toBe('version: 1\nrefs:\n  PROVIDER_A_KEY: "test-key-AAAA1111"\n')
  if (process.platform !== 'win32') expect(statSync(p).mode & 0o777).toBe(0o600)
  const before = '# 我的\nversion: 1\nrefs:\n  DEEPSEEK_API_KEY: "sk-test-deep-0000"\n'
  writeFileSync(p, before)
  setCredentialRef(p, 'PROVIDER_B_KEY', 'test-key-BBBB2222')
  setCredentialRef(p, 'PROVIDER_B_KEY', null)
  expect(readFileSync(p, 'utf8')).toBe(before)
  expect(() => setCredentialRef(p, 'DEEPSEEK_API_KEY', 'x'.repeat(10))).toThrow(CredentialsVerifyFailed)
  writeFileSync(p, 'version: 1\nrefs:\n  OTHER_TOKEN: "other-secret-value-77"\n')
  registerCredentialSecrets(p)
  expect(redact('leak other-secret-value-77 here')).toBe('leak *** here')
  expect(readdirSync(d)).toEqual(['credentials.yaml'])
})

test('锁：用完删掉；同时两个只能一个在锁里', async () => {
  const root = tmp()
  let inside = 0
  let maxInside = 0
  const run = () => withProvidersLock(root, { waitMs: 5_000 }, async () => { inside++; maxInside = Math.max(maxInside, inside); await sleep(150); inside-- })
  await Promise.all([run(), run(), run()])
  expect(maxInside).toBe(1)
  expect(existsSync(providersLockPath(root))).toBe(false)
})

test('锁：持锁进程已不在 → 清掉残留（provider.lock_stale_cleared {pid}）再拿锁', async () => {
  const root = tmp()
  const p = Bun.spawn(['bun', '-e', '0'])
  await p.exited
  writeFileSync(providersLockPath(root), JSON.stringify({ pid: p.pid, at: Date.now() }))
  const log = new CapLog()
  expect(await withProvidersLock(root, { waitMs: 1_000, log }, () => 'done')).toBe('done')
  expect(log.events).toContainEqual({ event: 'provider.lock_stale_cleared', pid: p.pid })
  expect(readdirSync(root)).toEqual([])
})

test('锁：持锁进程还在但锁超过 120 秒 → 当残留；持锁进程还在且没超时 → 等到 waitMs 报 ProvidersLockTimeout', async () => {
  const root = tmp()
  const log = new CapLog()
  writeFileSync(providersLockPath(root), JSON.stringify({ pid: process.pid, at: Date.now() - 121_000 }))
  await withProvidersLock(root, { waitMs: 1_000, log }, () => {})
  expect(log.events.some(e => e.event === 'provider.lock_stale_cleared')).toBe(true)
  writeFileSync(providersLockPath(root), JSON.stringify({ pid: process.pid, at: Date.now() }))
  const t = Date.now()
  await expect(withProvidersLock(root, { waitMs: 300, log }, () => {})).rejects.toBeInstanceOf(ProvidersLockTimeout)
  expect(Date.now() - t).toBeGreaterThanOrEqual(250)
  expect(log.events.some(e => e.event === 'provider.lock_timeout')).toBe(true)
  expect(existsSync(providersLockPath(root))).toBe(true) // 别人的锁不动
})
