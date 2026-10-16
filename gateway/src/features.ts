// 功能开关（约定 v1.1 5.19）：每只 bot 九个开关，存在它自己 yml 的 features 块里，管理台经适配服务改。
// 读法与 moments/features.py 一致：存储里没有的键按上线缺省（前八个开；nsfw 这只 bot 已经有 nsfw 技能才开），
// 不是真假的值按关（yes/no/on/off 这类写法照 Python 那边认成真假）。文字聊天、看图、表情回应、长期记忆、聊天命令、听语音、约定提醒常开，不进开关。
import { existsSync, readFileSync } from 'fs'
import { join } from 'path'

export const FEATURE_KEYS = ['voice', 'image', 'stickers', 'moments', 'proactive', 'life', 'relationship', 'groups', 'nsfw'] as const
export type FeatureKey = typeof FEATURE_KEYS[number]
export type Features = Record<FeatureKey, boolean>

/** 九个键全开（测试与没有开关的老调用方用） */
export const ALL_ON: Features = Object.fromEntries(FEATURE_KEYS.map(k => [k, true])) as Features

/** 上线缺省：前八个开；nsfw 看 <技能根>/skill-nsfw/SKILL.md 在不在 */
function launchDefault(key: FeatureKey, skillDir: string): boolean {
  return key === 'nsfw' ? existsSync(join(skillDir, 'skill-nsfw', 'SKILL.md')) : true
}

/** 写成字符串的真假（yes/no/on/off，或带引号的 true/false）：PyYAML（YAML 1.1）把不带引号的 yes、on 认成真假，
 *  Bun.YAML（1.2）认成字符串；两边都按这张表认，适配服务、定时任务和网关看到的开关才一致 */
const WORD_BOOL = new Map<string, boolean>([
  ...['yes', 'Yes', 'YES', 'on', 'On', 'ON', 'true', 'True', 'TRUE'].map(w => [w, true] as [string, boolean]),
  ...['no', 'No', 'NO', 'off', 'Off', 'OFF', 'false', 'False', 'FALSE'].map(w => [w, false] as [string, boolean]),
])

export function readFeatures(raw: unknown, skillDir: string): { features: Features; warnings: string[] } {
  const warnings: string[] = []
  let block: Record<string, unknown> | null = null
  if (raw !== undefined && raw !== null) {
    if (typeof raw === 'object' && !Array.isArray(raw)) block = raw as Record<string, unknown>
    else warnings.push('配置里的 features 不是一组开关，全部按缺省处理')
  }
  const features = {} as Features
  for (const k of FEATURE_KEYS) {
    if (block && k in block) {
      const v = block[k]
      const b = typeof v === 'boolean' ? v : typeof v === 'string' ? WORD_BOOL.get(v) : undefined
      features[k] = b === true
      if (b === undefined) warnings.push(`features.${k} 的值不是真或假，按关处理`)
    } else {
      features[k] = launchDefault(k, skillDir)
    }
  }
  return { features, warnings }
}

/** 从 yml 现读（网关盯着配置文件，变了就重读）。文件读不出、不是一份配置回 null：调用方保留原来的开关，
 *  不能当成"没写开关"全按缺省放开（手改出错、编辑器写到一半，都会读到坏文件） */
export function loadFeatures(configPath: string, skillDir: string): { features: Features; warnings: string[] } | null {
  let doc: unknown
  try { doc = Bun.YAML.parse(readFileSync(configPath, 'utf8')) } catch { return null }
  if (!doc || typeof doc !== 'object' || Array.isArray(doc)) return null
  return readFeatures((doc as { features?: unknown }).features, skillDir)
}
