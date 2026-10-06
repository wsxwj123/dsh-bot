// 配置加载：configs/<bot>.yml（沿用旧格式，新增 brain 与 gateway 两段）+ 频道目录里的 access.json、.env。
// 目录约定（和用户日常用的 ~/.dsh、旧系统的 ~/.claude 完全分开）：
//   <根>/credentials.yaml              模型密钥（dsh 的凭据文件格式，权限 600）
//   <根>/harness/                      钉死版本的 dsh
//   <根>/bots/<bot>/{state,dsh-home,work,home,logs,media}
// 根目录默认 ~/.dsh-bot，可用环境变量 DSH_BOT_HOME 改。
import { existsSync, readFileSync, statSync } from 'fs'
import { homedir } from 'os'
import { basename, isAbsolute, join, resolve } from 'path'

export type Effort = 'off' | 'low' | 'high' | 'max'

export type RouteModel = { id: string; contextWindow?: number; [k: string]: unknown }
export type Route = {
  api: string
  baseURL: string
  apiKeyEnv?: string
  models: RouteModel[]
  [k: string]: unknown
}

export type Brain = {
  provider: string
  model: string
  reasoningEffort?: Effort
  /** 可选的硬上限；不设就按模型窗口 × 80%（M2 起用） */
  maxInputTokens?: number
  /** 自定义 llm-pi-ai 路由（比如本机 provider-proxy）。键是路由名，brain.provider 填它即可使用。 */
  routes: Record<string, Route>
  /** dsh 自带压缩，只作应急开关，默认关 */
  emergencyCompaction: boolean
}

export type GatewayOpts = {
  timezone: string
  owners: string[]
  burstWindowMs: number
  burstMaxMs: number
  maxConcurrentTurns: number
  /** 一轮多久没有任何进展就取消 */
  stallCancelMs: number
  /** 一轮多久没有进展时，健康检查就报"活着但不干活" */
  stallWarnMs: number
  /** 模型报错时最多补救几次 */
  maxTurnRetries: number
  /** 换段或崩溃恢复时，新会话开头最多补回多少字的最近原话 */
  seedRecentChars: number
  /** 用到模型上下文窗口的多少比例就换段（方案 10.2：80%） */
  rollRatio: number
  /** 新段开头最多带多少字的长期记忆 */
  memoryMaxChars: number
  /** 写一份摘要最多等多久 */
  summaryTimeoutMs: number
  /** 用账本流水补写摘要时，最多喂给模型多少字 */
  summarySourceMaxChars: number
  /** 两次重试之间的等待（毫秒），按次数取，超出取最后一个 */
  retryBackoffMs: number[]
  /** Telegram 429 时最多原地等多久，超过就如实告诉模型 */
  maxSendWaitMs: number
  telegramApi: string
  pollTimeoutS: number
  /** 高级：自定义 dsh 的启动命令（测试用假 ACP 服务） */
  dshCommand?: string[]
  /** 承诺到点检查的间隔（默认 30 秒） */
  commitPollMs: number
  /** 承诺到点没兑现时，第 1、2 次重试前等多久（默认 5、15 分钟） */
  commitRetryMs: number[]
  /** 被晾追问的检查间隔（默认 60 秒） */
  hangTickMs: number
  /** 测试用：换掉作息查询命令（默认 python3 hang_situation.py <bot> --plan） */
  situationCmd?: string[]
  /** 作息查询结果（含"查不到"）缓存多久（默认 5 分钟） */
  situationTtlMs: number
  /** 语音服务（仓库里的 voice-bridge）地址，只用本机 */
  voiceBridgeUrl: string
  /** 生图：novelai | comfyui | off */
  imageProvider: string
  /** 生图技能目录（SKILL.md 是写法说明）；默认仓库里的 skills/<provider>-skill */
  imageSkillDir?: string
  /** 生图结果所在目录（允许 reply 发出去），默认 ~/resource/media */
  imageDirs: string[]
  /** 朋友圈数据库；不设就用仓库里的 state.db */
  botlifeDb?: string
  logLevel: 'debug' | 'info' | 'warn' | 'error'
  logMaxBytes: number
  logKeep: number
  /** 配置文件多久检查一次是否改动 */
  configPollMs: number
  heartbeatMs: number
  probeMs: number
}

export type BotConfig = {
  id: string
  /** 在朋友圈、画风里用的名字（旧系统里的 bot 名）：life_id，没写就取 life_config 的文件名，再没有就是 id */
  lifeId: string
  displayName: string
  configPath: string
  root: string
  botDir: string
  channelDir: string
  /** 长期记忆目录：<频道目录>/memory（MEMORY.md 和主题记忆文件） */
  memoryDir: string
  stateDir: string
  dshHome: string
  workDir: string
  homeDir: string
  logsDir: string
  mediaDir: string
  harnessDir: string
  credentialsPath: string
  apiPort: number
  brain: Brain
  gw: GatewayOpts
}

export class ConfigError extends Error {
  override name = 'ConfigError'
}

export function expandHome(p: string): string {
  if (p === '~') return homedir()
  if (p.startsWith('~/') || p.startsWith('~\\')) return join(homedir(), p.slice(2))
  return p
}

export function rootDir(env: Record<string, string | undefined> = process.env): string {
  return resolve(expandHome(env.DSH_BOT_HOME || join(homedir(), '.dsh-bot')))
}

function num(v: unknown, def: number, name: string): number {
  if (v === undefined || v === null || v === '') return def
  const n = Number(v)
  if (!Number.isFinite(n) || n < 0) throw new ConfigError(`${name} 必须是非负数字`)
  return n
}

function str(v: unknown, def: string): string {
  return typeof v === 'string' && v.trim() ? v.trim() : def
}

const EFFORTS: Effort[] = ['off', 'low', 'high', 'max']

export function parseBrain(raw: unknown): Brain {
  const b = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>
  const routesRaw = (b.routes && typeof b.routes === 'object' ? b.routes : {}) as Record<string, unknown>
  const routes: Record<string, Route> = {}
  for (const [name, r] of Object.entries(routesRaw)) {
    if (!/^[a-z0-9][a-z0-9-]*$/.test(name)) throw new ConfigError(`brain.routes 的路由名只能用小写字母、数字和连字符：${name}`)
    if (name === 'deepseek-official') throw new ConfigError('brain.routes 不能叫 deepseek-official（这是 dsh 自带的路由名）')
    const o = (r ?? {}) as Record<string, unknown>
    const models = Array.isArray(o.models) ? o.models : []
    if (typeof o.api !== 'string' || typeof o.baseURL !== 'string' || models.length === 0) {
      throw new ConfigError(`brain.routes.${name} 需要 api、baseURL 和至少一个 models 条目`)
    }
    for (const m of models) if (!m || typeof (m as RouteModel).id !== 'string') throw new ConfigError(`brain.routes.${name}.models 每项都要有 id`)
    routes[name] = { ...(o as Route), models: models as RouteModel[] }
  }
  const provider = str(b.provider, 'deepseek-official')
  if (provider !== 'deepseek-official' && !routes[provider]) {
    throw new ConfigError(`brain.provider 是 ${provider}，但 brain.routes 里没有这条路由`)
  }
  const effortRaw = b.reasoning_effort
  let reasoningEffort: Effort | undefined
  if (effortRaw !== undefined && effortRaw !== null) {
    const e = String(effortRaw) as Effort
    if (!EFFORTS.includes(e)) throw new ConfigError('brain.reasoning_effort 只能是 off、low、high、max')
    reasoningEffort = e
  }
  const max = num(b.max_input_tokens, 0, 'brain.max_input_tokens')
  return {
    provider,
    model: str(b.model, 'deepseek-flash'),
    reasoningEffort,
    maxInputTokens: max > 0 ? max : undefined,
    routes,
    emergencyCompaction: b.emergency_compaction === true,
  }
}

function parseGateway(raw: unknown, access: Access | null): GatewayOpts {
  const g = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>
  const owners = Array.isArray(g.owners) ? g.owners.map(String).filter(Boolean)
    : access?.allowFrom?.[0] ? [String(access.allowFrom[0])] : []
  const stallCancelMs = num(g.turn_stall_cancel_ms, 180_000, 'gateway.turn_stall_cancel_ms')
  const backoff = Array.isArray(g.retry_backoff_ms) ? g.retry_backoff_ms.map((v, i) => num(v, 0, `gateway.retry_backoff_ms[${i}]`)) : [5_000, 20_000]
  const cmd = Array.isArray(g.dsh_command) && g.dsh_command.length > 0 ? g.dsh_command.map(String) : undefined
  const level = str(g.log_level, 'info')
  if (!['debug', 'info', 'warn', 'error'].includes(level)) throw new ConfigError('gateway.log_level 只能是 debug、info、warn、error')
  return {
    timezone: str(g.timezone, 'Asia/Shanghai'),
    owners,
    burstWindowMs: num(g.burst_window_ms, 5_000, 'gateway.burst_window_ms'),
    burstMaxMs: num(g.burst_max_ms, 12_000, 'gateway.burst_max_ms'),
    maxConcurrentTurns: Math.max(1, num(g.max_concurrent_turns, 2, 'gateway.max_concurrent_turns')),
    stallCancelMs,
    stallWarnMs: num(g.turn_stall_warn_ms, Math.min(60_000, stallCancelMs), 'gateway.turn_stall_warn_ms'),
    maxTurnRetries: num(g.max_turn_retries, 2, 'gateway.max_turn_retries'),
    seedRecentChars: num(g.seed_recent_chars, 12_000, 'gateway.seed_recent_chars'),
    rollRatio: Math.min(0.95, Math.max(0.1, num(g.roll_ratio, 0.8, 'gateway.roll_ratio'))),
    memoryMaxChars: num(g.memory_max_chars, 20_000, 'gateway.memory_max_chars'),
    summaryTimeoutMs: num(g.summary_timeout_ms, 180_000, 'gateway.summary_timeout_ms'),
    summarySourceMaxChars: num(g.summary_source_max_chars, 60_000, 'gateway.summary_source_max_chars'),
    retryBackoffMs: backoff.length ? backoff : [0],
    maxSendWaitMs: num(g.max_send_wait_ms, 10_000, 'gateway.max_send_wait_ms'),
    telegramApi: str(g.telegram_api, 'https://api.telegram.org').replace(/\/+$/, ''),
    pollTimeoutS: num(g.poll_timeout_s, 25, 'gateway.poll_timeout_s'),
    dshCommand: cmd,
    commitPollMs: num(g.commit_poll_ms, 30_000, 'gateway.commit_poll_ms'),
    commitRetryMs: Array.isArray(g.commit_retry_ms) ? g.commit_retry_ms.map((v, i) => num(v, 0, `gateway.commit_retry_ms[${i}]`)) : [5 * 60_000, 15 * 60_000],
    hangTickMs: num(g.hang_tick_ms, 60_000, 'gateway.hang_tick_ms'),
    voiceBridgeUrl: str(g.voice_bridge_url, 'http://127.0.0.1:7788'),
    imageProvider: str(g.image_provider, 'novelai'),
    imageSkillDir: typeof g.image_skill_dir === 'string' && g.image_skill_dir ? g.image_skill_dir : undefined,
    imageDirs: Array.isArray(g.image_dirs) ? g.image_dirs.map(String) : ['~/resource/media'],
    botlifeDb: typeof g.botlife_db === 'string' && g.botlife_db ? g.botlife_db : undefined,
    situationTtlMs: num(g.situation_ttl_ms, 5 * 60_000, 'gateway.situation_ttl_ms'),
    situationCmd: Array.isArray(g.situation_cmd) && g.situation_cmd.length > 0 ? g.situation_cmd.map(String) : undefined,
    logLevel: level as GatewayOpts['logLevel'],
    logMaxBytes: num(g.log_max_bytes, 10 * 1024 * 1024, 'gateway.log_max_bytes'),
    logKeep: num(g.log_keep, 5, 'gateway.log_keep'),
    configPollMs: num(g.config_poll_ms, 5_000, 'gateway.config_poll_ms'),
    heartbeatMs: num(g.heartbeat_ms, 10_000, 'gateway.heartbeat_ms'),
    probeMs: num(g.probe_ms, 60_000, 'gateway.probe_ms'),
  }
}

export function readYaml(path: string): Record<string, unknown> {
  let text: string
  try { text = readFileSync(path, 'utf8') } catch { throw new ConfigError(`读不了配置文件 ${path}`) }
  let data: unknown
  try { data = Bun.YAML.parse(text) } catch (e) { throw new ConfigError(`配置文件 ${path} 不是合法的 YAML：${(e as Error).message?.slice(0, 120)}`) }
  if (!data || typeof data !== 'object' || Array.isArray(data)) throw new ConfigError(`配置文件 ${path} 的顶层必须是键值表`)
  return data as Record<string, unknown>
}

export function loadBotConfig(configPath: string, env: Record<string, string | undefined> = process.env): BotConfig {
  const abs = resolve(configPath)
  const y = readYaml(abs)
  const id = str(y.id, '')
  if (!/^[A-Za-z0-9_-]+$/.test(id)) throw new ConfigError('配置里的 id 只能用字母、数字、下划线和连字符')
  const root = rootDir(env)
  const botDir = join(root, 'bots', id)
  const channelRaw = str(y.bot_channel_path, join(botDir, 'channel'))
  const channelDir = resolve(expandHome(channelRaw))
  const access = readAccess(channelDir)
  const harnessRaw = env.DSH_BOT_HARNESS || join(root, 'harness')
  const cfg: BotConfig = {
    id,
    lifeId: str(y.life_id, '') || (typeof y.life_config === 'string' && y.life_config.trim() ? basename(y.life_config.trim()).replace(/\.ya?ml$/i, '') : id),
    displayName: str(y.display_name, id),
    configPath: abs,
    root,
    botDir,
    channelDir,
    memoryDir: join(channelDir, 'memory'),
    stateDir: join(botDir, 'state'),
    dshHome: join(botDir, 'dsh-home'),
    workDir: join(botDir, 'work'),
    homeDir: join(botDir, 'home'),
    logsDir: join(botDir, 'logs'),
    mediaDir: join(botDir, 'media'),
    harnessDir: resolve(expandHome(harnessRaw)),
    credentialsPath: join(root, 'credentials.yaml'),
    apiPort: num(y.dispatcher_port, 17801, 'dispatcher_port'),
    brain: parseBrain(y.brain),
    gw: parseGateway(y.gateway, access),
  }
  if (!isAbsolute(cfg.channelDir)) throw new ConfigError('bot_channel_path 必须是绝对路径或以 ~ 开头')
  return cfg
}

/** 只重读 brain 段（换模型用）。读失败返回 null，由调用方保留旧值并记警告。 */
export function reloadBrain(configPath: string): Brain | null {
  try { return parseBrain(readYaml(configPath).brain) } catch { return null }
}

// ─── access.json（格式与旧系统完全相同） ───

export type GroupPolicy = {
  requireMention?: boolean
  allowFrom?: string[]
  selfAliases?: string[]
  otherBotUsernames?: string[]
  otherBotAliases?: string[]
}

export type Access = {
  dmPolicy: 'pairing' | 'allowlist' | 'disabled'
  allowFrom: string[]
  groups: Record<string, GroupPolicy>
  mentionPatterns?: string[]
  splitOnParagraph?: boolean
  paragraphDelay?: number
  textChunkLimit?: number
  chunkMode?: 'length' | 'newline'
  replyToMode?: 'first' | 'all' | 'off'
  /** Fish Audio 的音色 id；没有就不能发语音（降级成文字） */
  voiceId?: string
}

export function readAccess(channelDir: string): Access | null {
  const p = join(channelDir, 'access.json')
  if (!existsSync(p)) return null
  try {
    const o = JSON.parse(readFileSync(p, 'utf8')) as Partial<Access>
    return {
      ...o,
      dmPolicy: o.dmPolicy ?? 'allowlist',
      allowFrom: (o.allowFrom ?? []).map(String),
      groups: o.groups ?? {},
    }
  } catch {
    return null
  }
}

/** 每次用时重读（和旧系统一样支持热改）；读坏了按"全部拒绝"处理，不会放陌生人进来。 */
export function loadAccess(channelDir: string): Access {
  return readAccess(channelDir) ?? { dmPolicy: 'disabled', allowFrom: [], groups: {} }
}

// ─── Telegram 令牌：频道目录的 .env（与旧系统相同），找不到才看环境变量 ───

export function parseDotenv(text: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim()
    if (!line || line.startsWith('#')) continue
    const m = line.replace(/^export\s+/, '').match(/^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/)
    if (!m) continue
    let v = m[2]!.trim()
    if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) v = v.slice(1, -1)
    out[m[1]!] = v
  }
  return out
}

export function readTelegramToken(channelDir: string, env: Record<string, string | undefined> = process.env): string {
  const p = join(channelDir, '.env')
  if (existsSync(p)) {
    const v = parseDotenv(readFileSync(p, 'utf8')).TELEGRAM_BOT_TOKEN
    if (v) return v.trim()
  }
  const v = env.TELEGRAM_BOT_TOKEN?.trim()
  if (v) return v
  throw new ConfigError(`找不到 Telegram 令牌：请在 ${p} 里写 TELEGRAM_BOT_TOKEN=...`)
}

/** 凭据文件权限检查（dsh 自己也会拒绝 600 以外的权限，这里提前给一句看得懂的提示）。Windows 没有可检查的权限位，跳过。 */
export function checkCredentialsFile(path: string): string | null {
  if (!existsSync(path)) return `没有凭据文件 ${path}。请按安装说明创建它（权限 600），在 refs 下写 DEEPSEEK_API_KEY`
  if (process.platform === 'win32') return null
  const mode = statSync(path).mode & 0o777
  if (mode & 0o077) return `凭据文件 ${path} 的权限是 ${mode.toString(8)}，其他用户能读。请运行：chmod 600 ${path}`
  return null
}
