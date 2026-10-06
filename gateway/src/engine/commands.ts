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

export function formatProviders(choices: ModelChoice[], current: ModelChoice, keyStatus: (provider: string) => { status: KeyStatus; env?: string }): string {
  const lines = ['【系统】供应商（✅ 是现在用的）：']
  for (const p of providersOf(choices)) {
    const n = choices.filter(c => c.provider === p).length
    const k = keyStatus(p)
    lines.push(`${p === current.provider ? '✅' : '·'} ${p}：${n} 个模型，${KEY_WORD[k.status]}${k.status === 'missing' && k.env ? `（凭据文件里填 ${k.env}）` : ''}`)
  }
  lines.push('换供应商：/provider <名字>（换到它的第一个模型）；看模型：/model list。')
  return lines.join('\n')
}

export const HELP_TEXT = [
  '【系统】可用的命令（只有主人能用）：',
  '/model —— 看现在用的模型',
  '/model list —— 列出能换的模型',
  '/model <模型> —— 换模型，也可以写 <供应商>/<模型>。这个 bot 的所有聊天都换，重启后保持',
  '/model default —— 换回配置文件里的模型',
  '/provider —— 列出供应商，以及密钥配没配',
  '/provider <供应商> —— 换到这个供应商的第一个模型',
  '/compact [要特别留意的事] —— 把这段对话压缩成摘要、换新会话，带着摘要和最近的原话',
  '/clear —— 清空这段对话的上下文，只留一份摘要',
  '/help —— 显示这份说明',
].join('\n')
