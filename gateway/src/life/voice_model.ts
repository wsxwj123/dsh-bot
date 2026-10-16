// 语音模型的生效判定（约定 v1.1 5.14.5 第 7 条，两边一致）：这只 bot 自己设了模型（access.json 的 voiceModel）
// 就用它；没设看壳的全局配置里的 voice.tts_model（不在白名单按没有）；再没有按 drama-3。
// 语音桥的环境变量 FISH_AUDIO_MODEL 不算在内。生效模型是 s1 时语音稿不写方括号标签（s1 会把它念出来）。

/** 语音桥认的模型名（与语音桥的白名单一致） */
export const FISH_MODELS: readonly string[] = ['s1', 's2-pro', 's2.1-pro', 's2.1-pro-free', 'drama-3']
/** 旧名 → 现名（与语音桥一致） */
const ALIASES: Record<string, string> = { 'drama-3-preview': 'drama-3' }

const canon = (v: unknown): string => (typeof v === 'string' ? (ALIASES[v.trim()] ?? v.trim()) : '')

export function effectiveVoiceModel(own: unknown, globalDoc: unknown): string {
  const mine = canon(own)
  if (mine) return mine
  const g = canon((globalDoc as { voice?: { tts_model?: unknown } } | null)?.voice?.tts_model)
  return FISH_MODELS.includes(g) ? g : 'drama-3'
}
