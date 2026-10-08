// 主人用的斜杠命令：/model、/provider、/compact、/help（/clear 在 engine 里）。用法参照 OpenClaw：
//   /model                     现在用的模型
//   /model list（/models）      能换哪些
//   /model <模型> | <供应商>/<模型>  换模型（这个 bot 的所有聊天都换，重启后保持）
//   /model default             换回配置文件里的
//   /provider（/providers）     供应商列表、密钥配没配
//   /provider <供应商>          换到这个供应商的默认模型
//   /compact [要特别留意的内容]  把这段对话压缩成摘要，换新会话（带着摘要和最近的原话）
// 这里只放纯函数（解析、格式），副作用在 engine 里。

/** dsh 回报的一个可选模型（session/new 等返回的 configOptions 里 id 为 model 的那一项） */
export type ModelChoice = { provider: string; model: string }

/** 从 configOptions 里取出可选模型。值是 JSON.stringify([供应商, 模型])，按供应商分组。 */
export function parseModelChoices(configOptions: unknown): ModelChoice[] {
  if (!Array.isArray(configOptions)) return []
  const opt = configOptions.find((o: any) => o?.id === 'model') as { options?: unknown[] } | undefined
  const out: ModelChoice[] = []
  const seen = new Set<string>()
  const visit = (o: any) => {
    if (Array.isArray(o?.options)) { for (const x of o.options) visit(x); return }
    if (typeof o?.value !== 'string') return
    try {
      const v = JSON.parse(o.value)
      if (Array.isArray(v) && typeof v[0] === 'string' && typeof v[1] === 'string' && !seen.has(o.value)) {
        seen.add(o.value)
        out.push({ provider: v[0], model: v[1] })
      }
    } catch {}
  }
  for (const o of opt?.options ?? []) visit(o)
  return out
}

/** 按 /model 后面写的内容找模型：先按"供应商/模型"找，再按模型名在所有供应商里找（模型名本身可以带斜杠） */
export function resolveModel(spec: string, choices: ModelChoice[]): { ok: ModelChoice } | { error: string } {
  const s = spec.trim()
  const eq = (a: string, b: string) => a.toLowerCase() === b.toLowerCase()
  const slash = s.indexOf('/')
  if (slash > 0) {
    const p = s.slice(0, slash)
    const m = s.slice(slash + 1)
    const hit = choices.find(c => eq(c.provider, p) && eq(c.model, m))
    if (hit) return { ok: hit }
  }
  const byModel = choices.filter(c => eq(c.model, s))
  if (byModel.length === 1) return { ok: byModel[0]! }
  if (byModel.length > 1) return { error: `有好几个供应商都有 ${s}，请写成"供应商/模型"：${byModel.map(c => `${c.provider}/${c.model}`).join('、')}` }
  return { error: `没有 ${s} 这个模型。用 /model list 看能换哪些。` }
}

export function providersOf(choices: ModelChoice[]): string[] {
  return [...new Set(choices.map(c => c.provider))]
}

export function formatModelList(choices: ModelChoice[], current: ModelChoice): string {
  const lines = ['【系统】能换的模型（✅ 是现在用的）：']
  for (const p of providersOf(choices)) {
    lines.push(`${p}：`)
    for (const c of choices.filter(x => x.provider === p)) {
      const on = c.provider === current.provider && c.model === current.model
      lines.push(`  ${on ? '✅' : '·'} ${c.model}`)
    }
  }
  lines.push('换模型：/model <模型名>，或 /model <供应商>/<模型名>。')
  return lines.join('\n')
}

export type KeyStatus = 'ok' | 'missing' | 'none' | 'unknown'
const KEY_WORD: Record<KeyStatus, string> = { ok: '密钥已配', missing: '缺密钥', none: '不需要密钥', unknown: '密钥情况不明' }

/** 供应商来源（INTERFACE 3.4.1）：内置、本 bot 配置文件路由、自建 */
export type ProviderSource = 'builtin' | 'config' | 'custom'
/** 一个供应商的对外视图（/provider 列表、GET /v1/model 的 providers 都用它；绝不含密钥、地址） */
export type ProviderView = {
  name: string
  source: ProviderSource
  api: string | null
  /** 内置/配置文件：dsh 回报的模型数，拿不到为 null；自建：providers.json 的模型数 */
  models: number | null
  key: KeyStatus
  /** 缺密钥时提示要填的键名（配置文件来源才有），其它情况 null。绝不出现密钥值 */
  keyEnv: string | null
  enabled: boolean
  note: string | null
  lastRefresh: { at: number; ok: boolean; count: number; reason: string | null } | null
}

/** 格式名（INTERFACE 3.4.1） */
export function apiLabel(api: string | null | undefined): string {
  switch (api) {
    case 'anthropic-messages': return 'Anthropic 格式'
    case 'openai-completions': return 'OpenAI 格式'
    case 'openai-responses': return 'OpenAI Responses 格式'
    case 'deepseek-official': return 'DeepSeek 官方'
    default: return api ? String(api) : '?'
  }
}

const SOURCE_WORD: Record<ProviderSource, string> = { builtin: '内置', config: '配置文件', custom: '自建' }

/**
 * /provider 列表（INTERFACE 3.4.1）。顺序：内置 → 配置文件 → 自建（按名字）。
 * 群里去掉"凭据文件里填…"提示（3.6）。
 */
export function formatProviders(views: ProviderView[], current: ModelChoice, o: { inGroup?: boolean } = {}): string {
  const lines = ['【系统】供应商（✅ 是现在用的）：']
  const order: Record<ProviderSource, number> = { builtin: 0, config: 1, custom: 2 }
  const sorted = [...views].sort((a, b) => order[a.source] - order[b.source] || a.name.localeCompare(b.name))
  for (const v of sorted) {
    const on = v.name === current.provider
    const n = v.source === 'custom'
      ? (v.models === 0 ? '0 个模型（未生效：先「刷新模型」或手动加）' : `${v.models ?? 0} 个模型`)
      : `${v.models ?? '?'} 个模型`
    const keyHint = v.key !== 'missing' || o.inGroup ? '' : (v.source === 'custom' ? '（点「修改」→「密钥」补上）' : v.keyEnv ? `（凭据文件里填 ${v.keyEnv}）` : '')
    const note = v.note ? `，${v.note}` : ''
    lines.push(`${on ? '✅' : '·'} ${v.name}：${n}，${KEY_WORD[v.key]}${keyHint}，${apiLabel(v.api)}，${SOURCE_WORD[v.source]}${note}`)
  }
  lines.push('换供应商：/provider <名字>（换到它的第一个模型）；看模型：/model list。')
  return lines.join('\n')
}

export const HELP_TEXT = [
  '【系统】可用的命令（只有主人能用）：',
  '/provider、/model —— 不带参数是按钮引导（点按钮新建供应商、换模型、改思考强度）',
  '/model —— 看现在用的模型',
  '/model list —— 列出能换的模型',
  '/model <模型> —— 换模型，也可以写 <供应商>/<模型>。这个 bot 的所有聊天都换，重启后保持',
  '/model default —— 换回配置文件里的模型',
  '/provider —— 列出供应商，以及密钥配没配',
  '/provider <供应商> —— 换到这个供应商的第一个模型',
  '/provider add <名字> <地址> <密钥> [openai|anthropic] —— 一行新建/更新自建供应商（不写格式按 Anthropic）',
  '/provider refresh <名字> —— 重新拉这个自建供应商的模型列表',
  '/cancel —— 退出正在进行的按钮引导',
  '/compact [要特别留意的事] —— 把这段对话压缩成摘要、换新会话，带着摘要和最近的原话',
  '/clear —— 清空这段对话的上下文，只留一份摘要',
  '/help —— 显示这份说明',
].join('\n')
