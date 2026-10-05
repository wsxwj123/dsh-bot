// 生成“只留人设”的 dsh 补丁层，以及每次实验用的隔离目录与最小环境变量。
// 补丁按 JSON 写（YAML 是 JSON 的超集，dsh 能直接读），这样人设里的任何字符都不需要手工转义。
import { mkdirSync, writeFileSync, rmSync, readFileSync } from 'fs'
import { join, resolve } from 'path'

/**
 * 要禁用的出厂行。两个版本的并集：不存在的行 dsh 只打一条警告，不影响启动。
 * - 工具与工具说明：bash/文件/搜索/任务/技能/子代理/工作流/待办/目标/网页等
 * - 自动注入的上下文：agent-instructions（$DSH_HOME/AGENTS.md 与工作目录链上的 AGENTS.md、CLAUDE.md）、技能目录
 * - 隐私：session-log-deepseek（0.2.0 起默认随请求上传会话日志增量）、plugin-package-inventory-deepseek（插件清单字段）、
 *   session-telemetry-otel（遥测）
 * - mcp-resources（0.2.0 新增：挂 MCP 时自动加 3 个资源工具，并往系统提示词末尾追加一段）
 * - session-title-llm（用模型给会话起标题，多一次请求）
 */
export const DISABLE_ROWS = [
  'tool-bash', 'tool-pwsh', 'tool-jobs', 'tool-fs', 'tool-fs-search',
  'agent-instructions', 'skill', 'skill-filesystem', 'tool-skill',
  'commands', 'command-feedback', 'goal', 'goal-round-driver', 'command-goal', 'plan-mode', 'command-compact',
  'tool-subagent-control', 'tool-subagent-list-agents', 'tool-subagent', 'tool-subagent-fork',
  'workflow-worker-thread', 'tool-workflow', 'tool-todo', 'tool-goal', 'tool-ralph', 'repeat-tool-reminder',
  'web', 'web-search-deepseek', 'web-fetch-http', 'tool-web',
  'session-log-deepseek', 'plugin-package-inventory-deepseek', 'session-telemetry-otel',
  'session-title-llm', 'mcp-resources',
]

/** dsh 把成对的 {{x}} 当模板变量，未注册就报错。在两个左花括号之间插入 U+2060（不可见的字连接符），模型看到的字不变。 */
export function escapePersona(text: string): string {
  return text.replace(/\{\{/g, '{⁠{')
}

export type Route =
  | { kind: 'deepseek'; model: string }                                   // llm-deepseek 的 deepseek-official 路由
  | { kind: 'openai-compatible'; name: string; baseURL: string; apiKeyEnv: string; model: string; contextWindow?: number }

/** 只留人设：人设当系统提示词，去掉身份句、运行时上下文、全部内置工具与自动注入，关掉上传类功能。 */
export function personaOnlyRows(opts: {
  persona: string
  suffix?: string
  route: Route
  sessionsRoot?: string
  compaction?: object | 'off'
  extra?: object[]
}): object[] {
  const rows: object[] = []
  if (opts.route.kind === 'openai-compatible') {
    rows.push({ id: 'llm-pi-ai', config: { providers: { [opts.route.name]: {
      api: 'openai-completions', baseURL: opts.route.baseURL, apiKeyEnv: opts.route.apiKeyEnv,
      models: [{ id: opts.route.model, ...(opts.route.contextWindow ? { contextWindow: opts.route.contextWindow } : {}) }],
    } } } })
    rows.push({ id: 'acp', config: { provider: opts.route.name, model: opts.route.model } })
    rows.push({ id: 'agent-default-model', config: { provider: opts.route.name, model: opts.route.model } })
  } else {
    rows.push({ id: 'acp', config: { provider: 'deepseek-official', model: opts.route.model } })
    rows.push({ id: 'agent-default-model', config: { provider: 'deepseek-official', model: opts.route.model } })
  }
  rows.push({ id: 'system-prompt', config: {
    includeHarnessIdentity: false,
    includeRuntimeContext: false,
    personaPrefix: escapePersona(opts.persona),
    ...(opts.suffix ? { personaSuffix: escapePersona(opts.suffix) } : {}),
  } })
  if (opts.sessionsRoot) rows.push({ id: 'session-persistence-jsonl', config: { root: opts.sessionsRoot, compression: 'none' } })
  if (opts.compaction === 'off') rows.push({ id: 'compaction-basic', config: { auto: false } })
  else if (opts.compaction) rows.push({ id: 'compaction-basic', config: opts.compaction })
  for (const id of DISABLE_ROWS) rows.push({ id, disabled: true })
  rows.push(...(opts.extra ?? []))
  return rows
}

/** 出厂 acp 配置，只把路由指到给定模型（可选把人设塞进 personaPrefix，其余一概不动）。用于对照。 */
export function shippedRows(opts: { route: Route; persona?: string }): object[] {
  const rows: object[] = []
  if (opts.route.kind === 'openai-compatible') {
    rows.push({ id: 'llm-pi-ai', config: { providers: { [opts.route.name]: {
      api: 'openai-completions', baseURL: opts.route.baseURL, apiKeyEnv: opts.route.apiKeyEnv,
      models: [{ id: opts.route.model, ...(opts.route.contextWindow ? { contextWindow: opts.route.contextWindow } : {}) }],
    } } } })
    rows.push({ id: 'acp', config: { provider: opts.route.name, model: opts.route.model } })
  } else {
    rows.push({ id: 'acp', config: { provider: 'deepseek-official', model: opts.route.model } })
  }
  if (opts.persona) {
    // 出厂 acp 的 system-prompt 行带后缀“Your working directory is {{cwd}}.”，整块替换时要照抄回去
    rows.push({ id: 'system-prompt', config: { personaPrefix: escapePersona(opts.persona), personaSuffix: 'Your working directory is {{cwd}}.' } })
  }
  return rows
}

export type Sandbox = { root: string; home: string; dshHome: string; work: string; patch: string }

/** 每次实验一个全新的隔离目录：独立 HOME、独立 DSH_HOME、独立工作目录，不碰用户自己的 ~/.dsh。 */
export function makeSandbox(root: string, rows: object[]): Sandbox {
  rmSync(root, { recursive: true, force: true })
  const sb = { root, home: join(root, 'home'), dshHome: join(root, 'dshhome'), work: join(root, 'work'), patch: join(root, 'bot.patch.yml') }
  for (const d of [sb.home, sb.dshHome, sb.work]) mkdirSync(d, { recursive: true })
  writeFileSync(sb.patch, JSON.stringify(rows, null, 1))
  return sb
}

/** 传给 dsh 的环境变量白名单：不把当前进程的环境整体传下去。 */
export function dshEnv(sb: Sandbox, extra: Record<string, string | undefined> = {}): Record<string, string> {
  const env: Record<string, string> = {
    PATH: process.env.PATH ?? '/usr/bin:/bin',
    HOME: sb.home,
    DSH_HOME: sb.dshHome,
    DSH_TELEMETRY_DISABLED: '1',
    LANG: process.env.LANG ?? 'C.UTF-8',
  }
  for (const [k, v] of Object.entries(extra)) if (v !== undefined) env[k] = v
  return env
}

export function dshBin(): string {
  return process.env.DSH ?? 'dsh'
}

export function dshCmd(sb: Sandbox, profile = 'acp'): string[] {
  return [dshBin(), '--profile', profile, '--patch', sb.patch]
}

export const REPO_ROOT = resolve(import.meta.dir, '..', '..', '..')

export function samplePersona(): string {
  return readFileSync(join(REPO_ROOT, 'channels', 'chenlulu', 'CLAUDE.md'), 'utf8')
}

export function runsDir(name: string): string {
  const base = process.env.LAB_OUT ?? join(REPO_ROOT, 'lab', 'dsh', '.runs')
  const dir = join(base, name)
  mkdirSync(dir, { recursive: true })
  return dir
}

export async function dshVersion(): Promise<string> {
  const p = Bun.spawn([dshBin(), '--version'], { stdout: 'pipe', stderr: 'pipe', env: { PATH: process.env.PATH ?? '' } })
  const out = (await new Response(p.stdout).text()).trim()
  await p.exited
  return out
}
