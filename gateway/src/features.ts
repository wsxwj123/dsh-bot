// 功能开关（约定 v1.1 5.19）：每只 bot 九个开关，存在它自己 yml 的 features 块里，管理台经适配服务改。
// 读法与 moments/features.py 一致：存储里没有的键按上线缺省（前八个开；nsfw 这只 bot 已经有 nsfw 技能才开），
// 不是真假的值按关。文字聊天、看图、表情回应、长期记忆、聊天命令、听语音、约定提醒常开，不进开关。
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
      features[k] = v === true
      if (typeof v !== 'boolean') warnings.push(`features.${k} 的值不是真或假，按关处理`)
    } else {
      features[k] = launchDefault(k, skillDir)
    }
  }
  return { features, warnings }
}

/** 从 yml 现读（网关盯着配置文件，变了就重读）；读不出按"块不在"处理，全取缺省 */
export function loadFeatures(configPath: string, skillDir: string): { features: Features; warnings: string[] } {
  let raw: unknown
  try { raw = (Bun.YAML.parse(readFileSync(configPath, 'utf8')) as { features?: unknown } | null)?.features } catch { raw = undefined }
  return readFeatures(raw, skillDir)
}
