// 主人用的斜杠命令：/model、/provider、/compact、/help（/clear 在 engine 里）。用法参照 OpenClaw：
//   /model                     现在用的模型
//   /model list（/models）      能换哪些
//   /model <模型> | <供应商>/<模型>  换模型（这个 bot 的所有聊天都换，重启后保持）
//   /model default             换回配置文件里的
//   /provider（/providers）     供应商列表、密钥配没配
//   /provider <供应商>          换到这个供应商的默认模型
//   /provider add / refresh / remove <名字>  一行新建、刷新、删除自建供应商
//   /compact [要特别留意的内容]  把这段对话压缩成摘要，换新会话（带着摘要和最近的原话）
//   /summary                   看最近一份摘要原文
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

/** 按 /model 后面写的内容找模型：供应商名按规整名匹配，再按模型名在所有供应商里找（模型名本身可以带斜杠） */
export function resolveModel(spec: string, choices: ModelChoice[]): { ok: ModelChoice } | { error: string } {
  const s = spec.trim()
  const eq = (a: string, b: string) => a.toLowerCase() === b.toLowerCase()
  const reg = (a: string) => a.toLowerCase().replace(/_/g, '-')
  const slash = s.indexOf('/')
  if (slash > 0) {
    const p = s.slice(0, slash)
    const m = s.slice(slash + 1)
    const hit = choices.find(c => reg(c.provider) === reg(p) && eq(c.model, m))
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
  '/provider remove <名字> —— 删掉这个自建供应商（它的密钥过一会儿才从凭据文件删掉）',
  '/cancel —— 退出正在进行的按钮引导',
  '/compact [要特别留意的事] —— 把这段对话压缩成摘要、换新会话，带着摘要和最近的原话',
  '/clear —— 清空这段对话的上下文，只留一份摘要',
  '/summary —— 看最近一份对话摘要的原文（换段时写的那份）',
  '/help —— 显示这份说明',
].join('\n')

// ─── 摘要查看（.devflow/INTERFACE-摘要查看.md）───

/** Telegram 单条消息的字数上限 */
export const TG_TEXT_LIMIT = 4096

/** 换段原因的说法：常见的两种给中文，其他原因照原词写 */
const ROLL_REASON_TEXT: Record<string, string> = { compact: '手动压缩', 'prompt-changed': '人设或技能改了' }

export type SummarySegment = { created_at: number; closed_at: number | null; close_reason: string | null; summary: string | null }

/** 账本里的毫秒时刻 → 「2026-10-15 08:05」，按 bot 的时区（时区写错就按本机时区） */
function stampText(ms: number | null, timeZone?: string): string {
  if (ms === null || !Number.isFinite(ms)) return '?'
  const opts: Intl.DateTimeFormatOptions = { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }
  let f: Intl.DateTimeFormat
  try { f = new Intl.DateTimeFormat('zh-CN', timeZone ? { ...opts, timeZone } : opts) } catch { f = new Intl.DateTimeFormat('zh-CN', opts) }
  const parts = f.formatToParts(new Date(ms))
  const part = (type: Intl.DateTimeFormatPartTypes) => parts.find(p => p.type === type)?.value ?? ''
  return `${part('year')}-${part('month')}-${part('day')} ${part('hour')}:${part('minute')}`
}

/** /summary 的回复：第一行写这段的起止时间与换段原因，空一行接摘要原文；没有任何摘要时只回一句 */
export function summaryReply(seg: SummarySegment | null, timeZone?: string): string {
  const summary = seg?.summary?.trim()
  if (!seg || !summary) return '【系统】还没有摘要'
  const reason = seg.close_reason ? (ROLL_REASON_TEXT[seg.close_reason] ?? seg.close_reason) : '没有记原因'
  return `【系统】最近一份摘要（${stampText(seg.created_at, timeZone)} 到 ${stampText(seg.closed_at, timeZone)}，换段原因：${reason}）\n\n${summary}`
}

/** /compact 的回复；成功时在原来那句之后另起一段附上这次写的摘要原文（没写成、进程出问题时没有可附的） */
export function compactReply(r: { how: 'in-session' | 'ledger' | 'none' | 'crashed'; chars: number; summary: string | null }, usedTokens: number | null): string {
  if (r.how === 'crashed') return '【系统】压缩时模型进程出了问题。下一条消息会用聊天记录补写摘要，再开新会话。'
  if (r.how === 'none') return '【系统】已换新会话，但这次摘要没写成，沿用上一份摘要；最近的原话照常带上。'
  const head = `【系统】已把这段对话压缩成一份摘要（${r.chars} 字，压缩前约 ${usedTokens ?? '?'} token）。下一条消息从新会话开始，带着摘要和最近的原话。`
  const summary = r.summary?.trim()
  return summary ? `${head}\n\n${summary}` : head
}

/**
 * 回复超过 Telegram 单条上限时拆成几条：先在段与段之间（空行）切，一段本身超长再按行切，一行还超长才硬切。
 * 一个字都不丢：切口上只少了作分隔的那个空行或换行。
 */
export function splitReply(text: string, limit = TG_TEXT_LIMIT): string[] {
  return packBy(text, limit, ['\n\n', '\n'])
}

function packBy(text: string, limit: number, seps: string[]): string[] {
  if (text.length <= limit) return [text]
  const [sep, ...rest] = seps
  if (sep === undefined) return hardSplit(text, limit)
  const out: string[] = []
  let cur: string | null = null
  for (const piece of text.split(sep)) {
    if (cur !== null && cur.length + sep.length + piece.length <= limit) { cur += sep + piece; continue }
    if (cur !== null) out.push(cur)
    cur = null
    if (piece.length <= limit) cur = piece
    else out.push(...packBy(piece, limit, rest))
  }
  if (cur !== null) out.push(cur)
  return out
}

function hardSplit(text: string, limit: number): string[] {
  const out: string[] = []
  for (let i = 0; i < text.length;) {
    let end = Math.min(i + limit, text.length)
    const c = text.charCodeAt(end - 1)
    if (end < text.length && end - 1 > i && c >= 0xd800 && c <= 0xdbff) end -= 1   // 不把一个表情符号切成两半
    out.push(text.slice(i, end))
    i = end
  }
  return out
}
