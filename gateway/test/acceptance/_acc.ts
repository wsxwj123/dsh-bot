// 验收测试公共部分（黑盒，依据 .devflow/INTERFACE.md v2）：只通过对外接口操作——
// 用配置文件启动网关子进程、经假 Telegram 发消息/点按钮、读写 INTERFACE 规定的落盘文件、调本机 HTTP 接口。
// 不 import 网关产品模块（src/）；账本用 bun:sqlite 只读打开，只用 INTERFACE 写明的表名和列名。
import type { Subprocess } from 'bun'
import { Database } from 'bun:sqlite'
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join, relative, resolve } from 'path'
import { FakeTelegram, type Menu, type Sent } from '../fakes/fake-telegram'
import { FakeModels, type Handler } from './fake-models'

export const GATEWAY_DIR = resolve(import.meta.dir, '..', '..')
export const REPO_DIR = resolve(GATEWAY_DIR, '..')
export const FAKE_ACP = join(import.meta.dir, '..', 'fakes', 'fake-acp.ts')
export const OWNER = 5550001
export const FRIEND = 5550002
export const GROUP = -1001777
export const IS_WIN = process.platform === 'win32'

export const sleep = (ms: number) => new Promise(r => setTimeout(r, ms))

export async function until<T>(fn: () => T | Promise<T>, what: string, timeoutMs = 15_000, stepMs = 50): Promise<NonNullable<T>> {
  const end = Date.now() + timeoutMs
  while (Date.now() < end) {
    const v = await fn()
    if (v) return v as NonNullable<T>
    await sleep(stepMs)
  }
  throw new Error(`timed out waiting for: ${what}`)
}

/** 每个测试一把独特的假密钥（像密钥：字母+数字、无空白、不以 http 开头） */
export function fakeKey(tag = ''): string {
  return `test-key-7${tag}${crypto.randomUUID().replace(/-/g, '').slice(0, 20)}`
}

/** 内置 DeepSeek 的假密钥：网关切到内置的其它模型前会检查凭据文件里有没有它 */
export const DS_KEY = { DEEPSEEK_API_KEY: 'sk-test-deepseek-000000' }

/** 规整名：转小写、_ 换成 - */
export const norm = (n: string) => n.toLowerCase().replace(/_/g, '-')
/** 默认凭据键名：PROVIDER_<规整名转大写、- 换成 _>_KEY */
export const defaultKeyEnv = (n: string) => `PROVIDER_${norm(n).toUpperCase().replace(/-/g, '_')}_KEY`

export type BotEnv = {
  name: string
  root: string
  configPath: string
  channelDir: string
  botDir: string
  acpState: string
  fakeHome: string
  tg: FakeTelegram
  gw: Record<string, unknown>
  brain: Record<string, unknown>
}

export type BotOpts = { name?: string; root?: string; gw?: Record<string, unknown>; brain?: Record<string, unknown>; access?: Record<string, unknown> }

export function makeBot(tg: FakeTelegram, o: BotOpts = {}): BotEnv {
  const root = o.root ?? mkdtempSync(join(tmpdir(), 'dshacc-'))
  const name = o.name ?? 'testbot'
  const botDir = join(root, 'bots', name)
  const channelDir = join(botDir, 'channel')
  const acpState = join(root, `acp-${name}`)
  const fakeHome = join(root, 'fakehome')
  mkdirSync(channelDir, { recursive: true })
  mkdirSync(join(botDir, 'media'), { recursive: true })
  mkdirSync(fakeHome, { recursive: true })
  writeFileSync(join(channelDir, 'CLAUDE.md'), '# 测试人设\n你是小测，说话简短。\n')
  writeFileSync(join(channelDir, '.env'), `TELEGRAM_BOT_TOKEN=${tg.token}\n`)
  writeFileSync(join(channelDir, 'access.json'), JSON.stringify({ dmPolicy: 'allowlist', allowFrom: [String(OWNER), String(FRIEND)], groups: {}, splitOnParagraph: true, paragraphDelay: 0, ...o.access }))
  const b: BotEnv = {
    name, root, botDir, channelDir, acpState, fakeHome, tg,
    configPath: join(root, `${name}.yml`),
    gw: {
      telegram_api: tg.url,
      dsh_command: ['bun', FAKE_ACP, '--state', acpState],
      burst_window_ms: 150,
      burst_max_ms: 2_000,
      poll_timeout_s: 1,
      retry_backoff_ms: [50, 100],
      turn_stall_cancel_ms: 60_000,
      turn_stall_warn_ms: 30_000,
      max_send_wait_ms: 500,
      config_poll_ms: 100,
      commit_poll_ms: 200,
      situation_ttl_ms: 300,
      situation_cmd: ['bun', join(import.meta.dir, '..', 'fakes', 'fake-situation.ts'), join(acpState, 'situation.json')],
      heartbeat_ms: 500,
      probe_ms: 60_000,
      log_level: 'debug',
      model_fetch_timeout_ms: 3_000,
      ...o.gw,
    },
    brain: { provider: 'deepseek-official', model: 'deepseek-flash', reasoning_effort: 'low', ...o.brain },
  }
  writeConfig(b)
  return b
}

export function writeConfig(b: BotEnv): void {
  // JSON 是 YAML 的子集
  writeFileSync(b.configPath, JSON.stringify({ id: b.name, display_name: b.name, bot_channel_path: b.channelDir, dispatcher_port: 0, brain: b.brain, gateway: b.gw }, null, 1))
}

export class Gateway {
  proc: Subprocess | null = null
  apiPort = 0
  stdout = ''
  stderr = ''
  constructor(readonly b: BotEnv) {}

  private spawn(extraEnv: Record<string, string>): Subprocess {
    const ready = join(this.b.root, `${this.b.name}.ready`)
    rmSync(ready, { force: true })
    const p = Bun.spawn(['bun', join(GATEWAY_DIR, 'src', 'main.ts'), '--config', this.b.configPath], {
      cwd: GATEWAY_DIR,
      env: {
        PATH: process.env.PATH ?? '', HOME: this.b.fakeHome, DSH_BOT_HOME: this.b.root, DSH_BOT_READY_FILE: ready,
        ...(IS_WIN ? { SystemRoot: process.env.SystemRoot ?? '', TEMP: process.env.TEMP ?? '', TMP: process.env.TMP ?? '', USERPROFILE: process.env.USERPROFILE ?? '' } : {}),
        ...extraEnv,
      },
      stdout: 'pipe',
      stderr: 'pipe',
    })
    void (async () => { for await (const c of p.stdout as ReadableStream<Uint8Array>) this.stdout += new TextDecoder().decode(c) })()
    void (async () => { for await (const c of p.stderr as ReadableStream<Uint8Array>) this.stderr += new TextDecoder().decode(c) })()
    return p
  }

  async start(extraEnv: Record<string, string> = {}): Promise<void> {
    const ready = join(this.b.root, `${this.b.name}.ready`)
    const p = this.proc = this.spawn(extraEnv)
    await until(() => existsSync(ready) || p.exitCode !== null, 'gateway ready', 20_000)
    if (!existsSync(ready)) throw new Error(`gateway exited during startup: ${this.stderr}`)
    this.apiPort = JSON.parse(readFileSync(ready, 'utf8')).api_port
  }

  /** 预期启动失败（配置错误）：返回退出码，不留进程 */
  async startExpectExit(timeoutMs = 20_000): Promise<number | null> {
    const ready = join(this.b.root, `${this.b.name}.ready`)
    const p = this.proc = this.spawn({})
    await until(() => existsSync(ready) || p.exitCode !== null, 'gateway exit or ready', timeoutMs)
    if (existsSync(ready)) return null
    this.proc = null
    return p.exitCode
  }

  async kill(): Promise<void> {
    if (!this.proc) return
    this.proc.kill('SIGKILL')
    await this.proc.exited
    this.proc = null
  }

  async stop(): Promise<void> {
    if (!this.proc) return
    this.proc.kill('SIGTERM')
    const t = setTimeout(() => this.proc?.kill('SIGKILL'), 15_000)
    await this.proc.exited
    clearTimeout(t)
    this.proc = null
  }

  apiToken(): string { return readFileSync(join(this.b.botDir, 'state', 'api.key'), 'utf8').trim() }

  api(path: string, init: RequestInit = {}): Promise<Response> {
    return fetch(`http://127.0.0.1:${this.apiPort}${path}`, init)
  }

  /** 带口令的本机接口调用；body 给对象时按 JSON 发 POST */
  async call(path: string, body?: unknown): Promise<{ status: number; json: any; text: string }> {
    const headers: Record<string, string> = { authorization: `Bearer ${this.apiToken()}` }
    const init: RequestInit = { headers }
    if (body !== undefined) { init.method = 'POST'; headers['content-type'] = 'application/json'; init.body = JSON.stringify(body) }
    const r = await this.api(path, init)
    const text = await r.text()
    let json: any = null
    try { json = JSON.parse(text) } catch {}
    return { status: r.status, json, text }
  }
}
// ─── 账本（只读；表名/列名都出自 INTERFACE 3.1.4、3.2、3.3） ───

export const ledgerPath = (b: BotEnv) => join(b.botDir, 'state', 'ledger.sqlite')

export function q<T = any>(b: BotEnv, sql: string, ...args: any[]): T[] {
  if (!existsSync(ledgerPath(b))) return []
  const db = new Database(ledgerPath(b), { readonly: true })
  try { return db.query(sql).all(...args) as T[] } finally { db.close() }
}

/** meta 表里某个键的值（不依赖列名：取"第一列等于键"的那一行的第二列） */
export function metaGet(b: BotEnv, k: string): string | null {
  for (const row of q<Record<string, unknown>>(b, 'SELECT * FROM meta')) {
    const v = Object.values(row)
    if (v[0] === k) return v[1] == null ? null : String(v[1])
  }
  return null
}

export function metaKeys(b: BotEnv, prefix: string): string[] {
  return q<Record<string, unknown>>(b, 'SELECT * FROM meta').map(r => String(Object.values(r)[0])).filter(k => k.startsWith(prefix))
}

export function override(b: BotEnv): Record<string, any> | null {
  const v = metaGet(b, 'brain_override')
  if (!v) return null
  try { return JSON.parse(v) } catch { return null }
}

export const inboundHas = (b: BotEnv, chat: number, msgId: number) => q(b, 'SELECT 1 FROM inbound WHERE ukey = ?', `tg:${chat}:${msgId}`).length > 0
export const inboundRow = (b: BotEnv, chat: number, msgId: number) => q<Record<string, any>>(b, 'SELECT * FROM inbound WHERE ukey = ?', `tg:${chat}:${msgId}`)[0] ?? null
export function eventsOf(b: BotEnv, kind: string): { kind: string; data: any }[] {
  return q<Record<string, any>>(b, 'SELECT * FROM events WHERE kind = ?', kind).map(r => {
    let data: any = r.data
    try { data = JSON.parse(String(r.data)) } catch {}
    return { kind: r.kind, data }
  })
}
export const segments = (b: BotEnv) => q<Record<string, any>>(b, 'SELECT * FROM segments')

// ─── 日志（gateway.log 是 JSON 行，字段 event） ───

export const logPath = (b: BotEnv, f = 'gateway.log') => join(b.botDir, 'logs', f)
export function logEvents(b: BotEnv): Record<string, any>[] {
  if (!existsSync(logPath(b))) return []
  const out: Record<string, any>[] = []
  for (const l of readFileSync(logPath(b), 'utf8').split('\n')) { if (!l.trim()) continue; try { out.push(JSON.parse(l)) } catch {} }
  return out
}
export const evs = (b: BotEnv, name: string, pred: (e: Record<string, any>) => boolean = () => true) => logEvents(b).filter(e => e.event === name && pred(e))
// ─── 假 dsh 的落盘记录（交给模型的内容） ───

export function readJsonl<T = any>(file: string): T[] {
  if (!existsSync(file)) return []
  return readFileSync(file, 'utf8').split('\n').filter(Boolean).map(l => JSON.parse(l) as T)
}
export type PromptRec = { pid: number; at: number; sessionId: string; text: string; blocks: number; model: string }
export const prompts = (b: BotEnv) => readJsonl<PromptRec>(join(b.acpState, 'prompts.jsonl'))
/** 交给模型的全部文字（含写摘要那一轮） */
export const promptsText = (b: BotEnv) => prompts(b).map(p => p.text).join('\n')
/** 不含写摘要的普通对话轮 */
export const chatPrompts = (b: BotEnv) => prompts(b).filter(p => !p.text.includes('⟦系统·整理记忆⟧'))
export const configCalls = (b: BotEnv) => readJsonl<{ pid: number; sessionId: string; configId: string; value: string; ok?: boolean }>(join(b.acpState, 'config.jsonl'))
export const lifecycle = (b: BotEnv) => readJsonl<{ pid: number; event: string; argv?: string[] }>(join(b.acpState, 'lifecycle.jsonl'))
/** 假 dsh 最近一次启动时看到的补丁层 */
export function patchSeen(b: BotEnv): any[] {
  const f = join(b.acpState, 'patch-seen.json')
  return existsSync(f) ? JSON.parse(readFileSync(f, 'utf8')) : []
}
/** 补丁层里交给 dsh 的路由（llm-pi-ai 行） */
export const patchRoutes = (b: BotEnv): Record<string, any> => patchSeen(b).find((r: any) => r?.id === 'llm-pi-ai')?.config?.providers ?? {}

export const chatLog = (b: BotEnv) => existsSync(logPath(b, 'chat.log')) ? readFileSync(logPath(b, 'chat.log'), 'utf8') : ''

// ─── 共用落盘文件（INTERFACE 3.1） ───

export const providersPath = (root: string) => join(root, 'providers.json')
export const credPath = (root: string) => join(root, 'credentials.yaml')
export const lockPath = (root: string) => join(root, 'providers.lock')
export function readProviders(root: string): any | null {
  return existsSync(providersPath(root)) ? JSON.parse(readFileSync(providersPath(root), 'utf8')) : null
}
export const fileMode = (f: string) => statSync(f).mode & 0o777

export type SeedModel = string | { id: string; contextWindow?: number; reasoningEfforts?: Record<string, unknown> }
export type SeedProvider = {
  name: string
  api?: 'openai-completions' | 'anthropic-messages' | 'openai-responses'
  baseURL: string
  apiKeyEnv?: string
  models?: SeedModel[]
  epoch?: number
  keyRev?: number
  manualModels?: string[]
  ownerContext?: string[]
}

/** 直接写一份 providers.json（前置条件用；格式按 INTERFACE 3.1.1） */
export function seedProviders(root: string, list: SeedProvider[], pending: { key: string; after: number }[] = []): void {
  const providers: Record<string, any> = {}
  for (const p of list) {
    const models = (p.models ?? []).map(m => typeof m === 'string' ? { id: m, contextWindow: 131072 } : { contextWindow: 131072, ...m })
    providers[p.name] = {
      route: { api: p.api ?? 'openai-completions', baseURL: p.baseURL, apiKeyEnv: p.apiKeyEnv ?? defaultKeyEnv(p.name), models },
      meta: {
        createdAt: 1_700_000_000_000, updatedAt: 1_700_000_000_000, epoch: p.epoch ?? 1_700_000_000_000, keyRev: p.keyRev ?? 1,
        guessedContext: (p.models ?? []).filter(m => typeof m === 'string' || m.contextWindow === undefined).map(m => typeof m === 'string' ? m : m.id),
        ownerContext: p.ownerContext ?? [], manualModels: p.manualModels ?? [],
        lastRefresh: null,
      },
    }
  }
  writeFileSync(providersPath(root), JSON.stringify({ version: 1, providers, pendingKeyRemovals: pending }, null, 1))
  if (!IS_WIN) chmodSync(providersPath(root), 0o600)
}

/** 写凭据文件（dsh 格式：version + refs 块，双引号值） */
export function seedCreds(root: string, refs: Record<string, string>, head = '# 测试用凭据文件\nversion: 1\nrefs:\n'): string {
  const body = head + Object.entries(refs).map(([k, v]) => `  ${k}: ${JSON.stringify(v)}\n`).join('')
  writeFileSync(credPath(root), body)
  if (!IS_WIN) chmodSync(credPath(root), 0o600)
  return body
}

export const readCreds = (root: string) => existsSync(credPath(root)) ? readFileSync(credPath(root), 'utf8') : ''

// ─── 机密扫描（INTERFACE 3.12：字节级全文搜索） ───

function walk(dir: string, fn: (f: string) => void): void {
  let names: string[] = []
  try { names = readdirSync(dir) } catch { return }
  for (const n of names) {
    const f = join(dir, n)
    let st
    try { st = statSync(f) } catch { continue }
    if (st.isDirectory()) walk(f, fn)
    else fn(f)
  }
}

/** 返回密钥出现过的地方（除 <根>/credentials.yaml 外应当一个都没有） */
export function findSecret(b: BotEnv, secret: string, o: { gw?: Gateway; tg?: FakeTelegram; extra?: Record<string, string> } = {}): string[] {
  const hits: string[] = []
  const needle = Buffer.from(secret, 'utf8')
  walk(b.root, f => {
    if (resolve(f) === resolve(credPath(b.root))) return
    try { if (readFileSync(f).includes(needle)) hits.push(relative(b.root, f)) } catch {}
  })
  if (o.gw && (o.gw.stdout.includes(secret) || o.gw.stderr.includes(secret))) hits.push('<gateway stdout/stderr>')
  const tg = o.tg ?? b.tg
  if (JSON.stringify(tg.calls).includes(secret)) hits.push('<telegram: bot 发出的请求>')
  for (const [k, v] of Object.entries(o.extra ?? {})) if (v.includes(secret)) hits.push(k)
  return hits
}

// ─── 等待与查看 bot 的输出 ───

/** 这个聊天里 bot 发过、编辑过的全部文字 + 按钮回调答复 */
export function botTexts(tg: FakeTelegram, chat: number): string[] {
  const out: string[] = []
  for (const s of tg.sentTo(chat)) if (s.text) out.push(s.text)
  for (const e of tg.edits) if (e.chatId === String(chat) && e.text) out.push(e.text)
  return out
}

export async function waitSent(tg: FakeTelegram, chat: number, pred: (s: Sent) => boolean, what: string, since = 0, timeoutMs = 15_000): Promise<Sent> {
  return until(() => tg.sentTo(chat).slice(since).find(s => pred(s)), what, timeoutMs)
}

export async function waitSentText(tg: FakeTelegram, chat: number, needle: string, since = 0, timeoutMs = 15_000): Promise<Sent> {
  return waitSent(tg, chat, s => (s.text ?? '').includes(needle), `发出含「${needle}」的消息`, since, timeoutMs)
}

export async function waitMenu(tg: FakeTelegram, chat: number, pred: (m: Menu) => boolean, what: string, timeoutMs = 15_000): Promise<Menu> {
  return until(() => { const m = tg.lastMenu(chat); return m && pred(m) ? m : null }, what, timeoutMs)
}

/** 某条 bot 消息（编辑后）的文字满足条件 */
export async function waitText(tg: FakeTelegram, chat: number, messageId: number, pred: (t: string) => boolean, what: string, timeoutMs = 15_000): Promise<string> {
  return until(() => { const t = tg.textOf(chat, messageId); return t !== undefined && pred(t) ? t : null }, what, timeoutMs)
}

export async function waitAnswer(tg: FakeTelegram, cbId: string, what = '回调答复', timeoutMs = 15_000) {
  return until(() => tg.answers.find(a => a.id === cbId), what, timeoutMs)
}

export async function waitEvent(b: BotEnv, name: string, pred: (e: Record<string, any>) => boolean = () => true, timeoutMs = 15_000) {
  return until(() => evs(b, name, pred)[0], `日志 ${name}`, timeoutMs)
}

/** 网关已经消费到第 updateId 个更新（账本 meta.tg_offset ≥ updateId + 1，INTERFACE 3.1.4） */
export async function waitOffset(b: BotEnv, updateId: number, timeoutMs = 15_000): Promise<void> {
  await until(() => Number(metaGet(b, 'tg_offset') ?? 0) >= updateId + 1, `tg_offset 推进到 ${updateId + 1}`, timeoutMs)
}

// ─── 一套隔离环境：假 Telegram + 网关；测试结束一定清理 ───

export type Ctx = { tg: FakeTelegram; b: BotEnv; gw: Gateway }

export async function withBot(o: BotOpts & { before?: (b: BotEnv) => void; tgOpts?: { botId?: number; username?: string } }, fn: (c: Ctx) => Promise<void>): Promise<void> {
  const tg = new FakeTelegram(o.tgOpts)
  const b = makeBot(tg, o)
  const gw = new Gateway(b)
  try {
    o.before?.(b)
    await gw.start()
    await fn({ tg, b, gw })
  } finally {
    await gw.stop().catch(() => {})
    tg.stop()
    try { rmSync(b.root, { recursive: true, force: true }) } catch {}
  }
}

// ─── 引导操作（只按 INTERFACE 写明的按钮文字与提示文字推进） ───

export const PROVIDER_HEAD = '【系统】供应商（✅ 是现在用的）：'
export const RESULT_RE = /已添加供应商|已更新供应商|刚被别处新建|没保存成功|已保存，但密钥没写进去/

/** 主人私聊发 /provider，等主菜单（带「新建」按钮） */
export async function openProviderMenu(tg: FakeTelegram): Promise<Menu> {
  tg.pushText(OWNER, '/provider')
  return waitMenu(tg, OWNER, m => m.text.startsWith(PROVIDER_HEAD) && m.buttons.some(x => x.text === '新建'), '/provider 主菜单')
}

/** 主人私聊发 /model，等主菜单（带「切换模型」按钮） */
export async function openModelMenu(tg: FakeTelegram): Promise<Menu> {
  tg.pushText(OWNER, '/model')
  return waitMenu(tg, OWNER, m => m.text.startsWith('【系统】现在用的模型：') && m.buttons.some(x => x.text === '切换模型'), '/model 主菜单')
}

/** 点当前菜单上的按钮，等这条菜单被编辑成满足 pred 的文字（编辑类步骤用） */
export async function clickAndWait(tg: FakeTelegram, menu: Menu, label: string, pred: (t: string) => boolean, what: string): Promise<string> {
  tg.clickButton(OWNER, OWNER, menu.messageId, label)
  return waitText(tg, OWNER, menu.messageId, pred, what)
}

/** 走到新建第 1 步（输入名字），返回那条菜单 */
export async function toNameStep(tg: FakeTelegram): Promise<Menu> {
  const m = await openProviderMenu(tg)
  await clickAndWait(tg, m, '新建', t => t.includes('（第 1/4 步）'), '新建第 1/4 步')
  return { ...m, text: tg.textOf(OWNER, m.messageId)!, buttons: tg.buttonsOf(OWNER, m.messageId) }
}

/** 走到第 2 步（选格式；输入名字后是新消息） */
export async function toFormatStep(tg: FakeTelegram, name: string): Promise<Menu> {
  await toNameStep(tg)
  tg.pushText(OWNER, name)
  return waitMenu(tg, OWNER, m => m.text.includes('（第 2/4 步）'), '新建第 2/4 步')
}

export type Fmt = 'openai' | 'anthropic'
export const fmtLabel = (f: Fmt) => (f === 'openai' ? 'OpenAI 格式' : 'Anthropic 格式')

/** 走到第 3 步（输入地址；点按钮后编辑同一条菜单） */
export async function toUrlStep(tg: FakeTelegram, name: string, fmt: Fmt): Promise<Menu> {
  const m = await toFormatStep(tg, name)
  await clickAndWait(tg, m, fmtLabel(fmt), t => t.includes('（第 3/4 步）'), '新建第 3/4 步')
  return { ...m, text: tg.textOf(OWNER, m.messageId)!, buttons: tg.buttonsOf(OWNER, m.messageId) }
}

/** 走到第 4 步（输入密钥；输入地址后是新消息） */
export async function toKeyStep(tg: FakeTelegram, name: string, fmt: Fmt, url: string): Promise<Menu> {
  await toUrlStep(tg, name, fmt)
  tg.pushText(OWNER, url)
  return waitMenu(tg, OWNER, m => m.text.includes('（第 4/4 步）'), '新建第 4/4 步')
}

/** 完整走一遍新建；返回结果消息和那条密钥消息的编号 */
export async function wizardCreate(tg: FakeTelegram, o: { name: string; fmt: Fmt; url: string; key: string }): Promise<{ result: Sent; keyMsg: number }> {
  await toKeyStep(tg, o.name, o.fmt, o.url)
  const n0 = tg.sentTo(OWNER).length
  const keyMsg = tg.pushText(OWNER, o.key)
  const result = await waitSent(tg, OWNER, s => RESULT_RE.test(s.text ?? ''), '新建结果', n0, 20_000)
  return { result, keyMsg }
}

/** 一行命令新建；返回回复 */
export async function providerAdd(tg: FakeTelegram, args: string): Promise<{ reply: Sent; msg: number }> {
  const n0 = tg.sentTo(OWNER).length
  const msg = tg.pushText(OWNER, `/provider add ${args}`)
  const reply = await waitSent(tg, OWNER, s => (s.text ?? '').startsWith('【系统】'), '/provider add 回复', n0, 20_000)
  return { reply, msg }
}

/** 某条消息上的按钮文字 */
export const labels = (tg: FakeTelegram, messageId: number, chat = OWNER) => tg.buttonsOf(chat, messageId).map(x => x.text)

/** 假 Telegram + 网关 + 假模型列表接口（期望密钥 = key）；测试结束一并清理 */
export async function withModels(o: BotOpts & { before?: (b: BotEnv, fm: FakeModels) => void; key?: string; handler: Handler }, fn: (c: Ctx & { fm: FakeModels; key: string }) => Promise<void>): Promise<void> {
  const key = o.key ?? fakeKey()
  const fm = new FakeModels(key, o.handler)
  const before = o.before
  try { await withBot({ ...o, before: before ? (b => before(b, fm)) : undefined }, c => fn({ ...c, fm, key })) } finally { fm.stop() }
}

/** 两个新系统 bot 共用同一个根目录（共用 providers.json 与凭据文件），各有自己的假 Telegram 与网关 */
export async function withTwo(o: { brainA?: Record<string, unknown>; brainB?: Record<string, unknown>; gw?: Record<string, unknown>; before?: (root: string) => void }, fn: (a: Ctx, b: Ctx) => Promise<void>): Promise<void> {
  const root = mkdtempSync(join(tmpdir(), 'dshacc2-'))
  const tgA = new FakeTelegram({ botId: 900001, username: 'bot_a_dsh_bot' })
  const tgB = new FakeTelegram({ botId: 900002, username: 'bot_b_dsh_bot' })
  const ba = makeBot(tgA, { root, name: 'bota', brain: o.brainA, gw: o.gw })
  const bb = makeBot(tgB, { root, name: 'botb', brain: o.brainB, gw: o.gw })
  const ga = new Gateway(ba)
  const gb = new Gateway(bb)
  try {
    o.before?.(root)
    await ga.start()
    await gb.start()
    await fn({ tg: tgA, b: ba, gw: ga }, { tg: tgB, b: bb, gw: gb })
  } finally {
    await ga.stop().catch(() => {})
    await gb.stop().catch(() => {})
    tgA.stop()
    tgB.stop()
    try { rmSync(root, { recursive: true, force: true }) } catch {}
  }
}

/** 根目录下残留的临时文件（*.tmp-*） */
export function tmpLeftovers(root: string): string[] {
  const out: string[] = []
  const visit = (d: string) => {
    let names: string[] = []
    try { names = readdirSync(d) } catch { return }
    for (const n of names) {
      const f = join(d, n)
      let st
      try { st = statSync(f) } catch { continue }
      if (st.isDirectory()) visit(f)
      else if (/\.tmp-/.test(n)) out.push(relative(root, f))
    }
  }
  visit(root)
  return out
}

/** 网关是在第一轮对话时才拉起 dsh 的：要看"交给 dsh 的补丁层"，先让主人说一句、等假模型回话 */
export async function warmUp(tg: FakeTelegram, text = '预热一下'): Promise<void> {
  const k = tg.sentTo(OWNER).length
  tg.pushText(OWNER, text)
  await waitSentText(tg, OWNER, `收到：${text}`, k, 30_000)
}

export const dshStarts = (b: BotEnv) => lifecycle(b).filter(l => l.event === 'start').length
