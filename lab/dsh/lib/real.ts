// 需要真实密钥的验证脚本共用的小工具：读参数、检查密钥、起用量记录代理、跑一个 dsh 会话。
import { readFileSync } from 'fs'
import { AcpClient } from './acp-client'
import { dshCmd, dshEnv, makeSandbox, samplePersona, type Sandbox } from './profile'
import { deepseekUpstream, startUsageTap, type Tap, type TapRecord } from './usage-tap'
import type { Report } from './report'

export type RealArgs = {
  persona: string
  personaLabel: string
  model: string
  effort: 'off' | 'low' | 'high' | 'max'
  tapPort: number
  /** 只出数字：报告里不写模型按人设说出的任何文字（回复、摘要、回答）。用自己的人设时默认打开。 */
  numbersOnly: boolean
}

export function parseArgs(argv = process.argv.slice(2)): RealArgs {
  const get = (name: string) => {
    const i = argv.indexOf(`--${name}`)
    return i >= 0 ? argv[i + 1] : undefined
  }
  const personaPath = get('persona')
  const effort = (get('effort') ?? 'low') as RealArgs['effort']
  if (!['off', 'low', 'high', 'max'].includes(effort)) throw new Error(`--effort 只能是 off/low/high/max`)
  // 用自己的人设时默认只出数字；确实想看文字（只给自己看、不上传）时加 --with-text
  const numbersOnly = argv.includes('--numbers-only') || (personaPath !== undefined && !argv.includes('--with-text'))
  return {
    persona: personaPath ? readFileSync(personaPath, 'utf8') : samplePersona(),
    personaLabel: personaPath ? '自定义人设（路径不记录）' : '仓库示例人设 channels/chenlulu/CLAUDE.md',
    model: get('model') ?? 'deepseek-v4-flash',
    effort,
    tapPort: Number(get('tap-port') ?? 18190),
    numbersOnly,
  }
}

export function requireKey(): string {
  const key = process.env.DEEPSEEK_API_KEY
  if (!key) {
    console.error('缺少 DEEPSEEK_API_KEY。用法：DEEPSEEK_API_KEY=sk-... DSH=<dsh 可执行文件> bun <脚本>')
    process.exit(2)
  }
  return key
}

export function startDeepseekTap(port: number): Tap {
  return startUsageTap({ port, upstream: deepseekUpstream })
}

/** 用给定补丁层起一个 dsh acp 进程，DeepSeek 请求经本机代理转发（代理负责记账，不碰密钥内容）。 */
export async function startBot(sb: Sandbox, tap: Tap): Promise<AcpClient> {
  const env = dshEnv(sb, {
    DEEPSEEK_API_KEY: process.env.DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL: `http://127.0.0.1:${tap.port}`,
  })
  const c = new AcpClient(dshCmd(sb), { env, cwd: sb.work })
  await c.initialize()
  return c
}

export async function newSession(c: AcpClient, sb: Sandbox, opts: { effort: string; mcpServers?: any[] }) {
  const s = await c.request('session/new', { cwd: sb.work, mcpServers: opts.mcpServers ?? [] })
  await c.request('session/set_config_option', { sessionId: s.sessionId, configId: 'reasoning_effort', value: opts.effort })
  return s.sessionId as string
}

export function usageTable(report: Report, records: TapRecord[], label: (r: TapRecord) => string) {
  report.table(
    ['#', '场景', '用途', 'HTTP', '输入 token', '缓存命中', '未命中', '输出 token', '系统提示词字数', '工具数', '历史里思考字数', '最后一条用户消息（前 40 字）'],
    records.map(r => [r.n, label(r), r.purpose, r.status, r.usage.input, r.usage.cacheHit, r.usage.cacheMiss, r.usage.output, r.systemChars, r.tools, r.historyReasoningChars, r.lastUserPreview]),
  )
}

export function sandboxFor(dir: string, name: string, rows: object[]): Sandbox {
  return makeSandbox(`${dir}/${name}`, rows)
}
