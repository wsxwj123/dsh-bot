// 模型「看不见图」的判定与状态（.devflow/noimages-permanent-disable 接口）。
// 两类错误分开：A 档是图片本身有问题（太大/格式/加载失败），本轮去图重发但不记到模型头上；
// B 档是模型明确不接受图片输入，连续 2 次才禁用 30 分钟。换模型（key 变化）立即清空旧判断。
import { safeError, type Logger } from '../log'

/** 禁用时长：到期后自动恢复带图尝试（接口约定 30 分钟） */
const DISABLE_MS = 30 * 60 * 1000
/** 连续几次 B 档失败后禁用 */
const DISABLE_AFTER = 2

/** 「不支持 / 未支持」语义。`not\s+support` 同时覆盖 "not supported" 与 "does not support" */
const NOT_SUPPORTED = /unsupported|not\s+support|doesn'?t\s+support|不支持/i
/** 「图片」语义：必须和不支持语义同时出现才算 B 档 */
const IMAGE_WORD = /images?|图片|图像|vision/i
/** A 档判据：消息提到图片/附件，但没有"不支持"语义 */
const MENTIONS_IMAGE = /image|attachment|图片|图像/i

/**
 * 是否「模型明确不接受图片输入」（B 档）。
 * 只有不支持语义与图片语义同时出现才算；UNSUPPORTED_CONTENT 是 ACP 的明确错误码，单独出现也算。
 */
export function isImagesUnsupportedError(message: string): boolean {
  if (!message) return false
  if (/UNSUPPORTED_CONTENT/i.test(message)) return true
  return NOT_SUPPORTED.test(message) && IMAGE_WORD.test(message)
}

type FailureVerdict = {
  /** 本轮要不要去掉图片重发：A 档与 B 档都为 true */
  unsupported: boolean
  /** 是否刚进入「该模型不再附图」状态 */
  disabled: boolean
}

export class ImagesUnsupportedTracker {
  private readonly now: () => number
  private readonly logger?: Pick<Logger, 'info' | 'warn'>
  /** 当前生效的模型标识；变了就清空旧状态（换回原模型也重新计数） */
  private key: string | null = null
  /** 连续 B 档失败次数 */
  private failures = 0
  /** 「首次偶发失败」的 info 是否已经打过，成功/过期/换模型后重置 */
  private infoLogged = false
  /** 禁用截止时间戳；null = 未禁用 */
  private disabledUntil: number | null = null

  constructor(opts: { now?: () => number; logger?: Pick<Logger, 'info' | 'warn'> } = {}) {
    this.now = opts.now ?? Date.now
    this.logger = opts.logger
  }

  /** 换模型（key 变化）立即清空禁用与计数 */
  private use(modelKey: string): void {
    if (this.key === modelKey) return
    this.key = modelKey
    this.failures = 0
    this.infoLogged = false
    this.disabledUntil = null
  }

  shouldAttachImages(modelKey: string): boolean {
    this.use(modelKey)
    if (this.disabledUntil === null) return true
    if (this.now() < this.disabledUntil) return false
    // 到期：恢复尝试，重新计数（不能一次失败就又禁用）
    this.disabledUntil = null
    this.failures = 0
    this.infoLogged = false
    return true
  }

  onImageFailure(modelKey: string, errorMessage: string): FailureVerdict {
    this.use(modelKey)
    if (!isImagesUnsupportedError(errorMessage)) {
      if (!MENTIONS_IMAGE.test(errorMessage)) return { unsupported: false, disabled: false }
      // A 档：图片太大/格式不对/加载失败之类，本轮去图重发，不计入模型禁用
      this.logFirstRetry(errorMessage)
      return { unsupported: true, disabled: false }
    }
    // B 档：模型不接受图片输入，连续计数
    this.failures += 1
    if (this.failures >= DISABLE_AFTER) {
      this.disabledUntil = this.now() + DISABLE_MS
      this.logger?.warn('prompt.images_unsupported', { model: modelKey })
      return { unsupported: true, disabled: true }
    }
    this.logFirstRetry(errorMessage)
    return { unsupported: true, disabled: false }
  }

  onImageSuccess(modelKey: string): void {
    this.use(modelKey)
    this.failures = 0
    this.infoLogged = false
    this.disabledUntil = null
  }

  private logFirstRetry(err: string): void {
    if (this.infoLogged) return
    this.infoLogged = true
    this.logger?.info('prompt.images_retry', { model: this.key, err: safeError(err) })
  }
}
