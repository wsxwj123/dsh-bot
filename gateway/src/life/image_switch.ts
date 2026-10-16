// 生图引擎的共用开关（约定 v1.1 5.21）：壳的全局配置文件里 moments.image_generation 下的
// provider、provider_moment、provider_telegram。写入方只有管理台朋友圈页的引擎按钮，这里只读。
import { readFileSync, statSync } from 'fs'
import type { Logger } from '../log'

export type ImageEngine = 'novelai' | 'comfyui'
export type ImageScope = 'telegram' | 'moment'

/** 读取规则（5.21 第 2 条）：聊天发图依次看 provider_telegram、provider；朋友圈配图依次看 provider_moment、provider；
 *  都没有就用 novelai。值不认识的按没有处理 */
export function pickEngine(doc: unknown, scope: ImageScope): ImageEngine {
  const img = (doc as { moments?: { image_generation?: Record<string, unknown> } } | null)?.moments?.image_generation
  for (const k of [scope === 'telegram' ? 'provider_telegram' : 'provider_moment', 'provider']) {
    const v = img?.[k]
    if (v === 'novelai' || v === 'comfyui') return v
  }
  return 'novelai'
}

/** 本 bot 配置写 off 就不生图；否则开关读得到用开关，读不到退回本 bot 配置里的 image_provider */
export function effectiveEngine(own: string, sw: { engine(scope: ImageScope): ImageEngine | null } | null, scope: ImageScope = 'telegram'): string {
  if (own === 'off') return 'off'
  return sw?.engine(scope) ?? own
}

/** 按修改时间缓存地读开关文件。文件不在、不是合法 YAML 时返回 null；同一种读不到只记一次日志 */
export class ImageSwitch {
  private stamp = ''
  private parsed: unknown = null
  private ok = false
  private noted = ''

  constructor(readonly path: string, private readonly log: Logger) {}

  engine(scope: ImageScope): ImageEngine | null {
    const doc = this.doc()
    return doc === null ? null : pickEngine(doc, scope)
  }

  /** 整份全局配置（按修改时间缓存）；读不到为 null。语音模型的生效判定也从这里取 voice.tts_model */
  doc(): unknown | null {
    let stamp: string
    try {
      const st = statSync(this.path)
      stamp = `${st.mtimeMs}:${st.size}`
    } catch {
      this.note('missing')
      return null
    }
    if (stamp !== this.stamp) {
      this.stamp = stamp
      try {
        this.parsed = Bun.YAML.parse(readFileSync(this.path, 'utf8'))
        this.ok = true
      } catch {
        this.ok = false
      }
    }
    if (!this.ok) {
      this.note('invalid')
      return null
    }
    this.noted = ''
    return this.parsed
  }

  private note(why: string): void {
    if (this.noted === why) return
    this.noted = why
    this.log.warn('image_switch.unreadable', { why })
  }
}
