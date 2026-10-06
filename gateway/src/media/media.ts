// 语音和图片（M4）：
//   收：语音先下载再交给 voice-bridge 转写，图片下载到媒体目录。都放在处理这一轮之前做，不卡收消息；
//       失败也不挡聊天（语音留一句"没转成文字"）。
//   发：reply 带 as_voice 时，文字交给 voice-bridge 合成，网关自己用 sendVoice 发。Telegram 令牌不离开网关。
import { copyFileSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'fs'
import { basename, join } from 'path'
import type { InboundRow, Ledger } from '../ledger'
import { safeError, type Logger } from '../log'
import type { TelegramApi } from '../telegram/api'

export type MediaMeta = { kind: 'voice' | 'photo'; file_id: string; path?: string; done?: boolean; transcript?: string | null }

export type MediaDeps = {
  api: TelegramApi
  ledger: Ledger
  log: Logger
  mediaDir: string
  bridgeUrl: string
  /** voice-bridge 设了 VOICE_BRIDGE_TOKEN 时要带上（从凭据文件读，可以没有） */
  bridgeToken: () => string | null
  /** voice-bridge 只读白名单里的目录。切换期跑的还是旧的 voice-bridge，它不认 ~/.dsh-bot（真机 M4 问题 1）：
   *  返回 403 时把语音临时复制到这些共用目录（生图目录，旧白名单里有）再试一次，转完就删 */
  sharedDirs: () => string[]
}

/** 转写失败时给模型看的：让它照实说没听清，别装作听懂了（真机 M4：转写失败 bot 照常回复，用户以为它听懂了） */
export const VOICE_UNHEARD = '[语音消息，没转成文字：你没听清这条语音，照实告诉对方]'

const TRANSCRIBE_TIMEOUT_MS = 60_000

export function rowMedia(r: InboundRow): MediaMeta | null {
  try {
    const m = r.meta ? JSON.parse(r.meta)?.media : null
    return m && (m.kind === 'voice' || m.kind === 'photo') && typeof m.file_id === 'string' ? m as MediaMeta : null
  } catch { return null }
}

export class Media {
  constructor(private readonly d: MediaDeps) {}

  private headers(): Record<string, string> {
    const t = this.d.bridgeToken()
    return { 'content-type': 'application/json', ...(t ? { authorization: `Bearer ${t}` } : {}) }
  }

  /** 这一批里还没处理的语音、图片：下载（语音再转写），更新账本里的文字。返回更新后的行。 */
  async prepare(rows: InboundRow[]): Promise<InboundRow[]> {
    const out: InboundRow[] = []
    for (const r of rows) {
      const m = rowMedia(r)
      if (!m || m.done) { out.push(r); continue }
      const meta = JSON.parse(r.meta!) as Record<string, unknown>
      let text = r.text
      try {
        const f = await this.d.api.getFile(m.file_id)
        if (!f.file_path) throw new Error('no file_path')
        const bytes = await this.d.api.download(f.file_path)
        const ext = m.kind === 'voice' ? 'oga' : (f.file_path.split('.').pop() || 'jpg').toLowerCase()
        const path = join(this.d.mediaDir, `${m.kind}-${r.id}.${ext}`)
        writeFileSync(path, bytes, { mode: 0o600 })
        m.path = path
        if (m.kind === 'voice') {
          m.transcript = await this.transcribe(path)
          text = text.replace('[语音消息]', m.transcript ? `[语音] ${m.transcript}` : VOICE_UNHEARD)
        }
        this.d.log.info('media.prepared', { kind: m.kind, inbound: r.id, transcribed: m.kind === 'voice' ? !!m.transcript : undefined })
      } catch (e) {
        this.d.log.warn('media.failed', { kind: m.kind, inbound: r.id, err: safeError(e) })
        if (m.kind === 'voice') text = text.replace('[语音消息]', VOICE_UNHEARD)
      }
      m.done = true
      meta.media = m
      this.d.ledger.updateInbound(r.id, text, meta)
      out.push(this.d.ledger.inbound(r.id) ?? r)
    }
    return out
  }

  private async transcribe(path: string): Promise<string | null> {
    try {
      let res = await this.postTranscribe(path)
      if (res.status === 403) {
        const dir = this.d.sharedDirs().find(d => existsSync(d))
        if (dir) {
          const tmpDir = join(dir, 'dsh-bot-voice')
          const copy = join(tmpDir, basename(path))
          try {
            mkdirSync(tmpDir, { recursive: true, mode: 0o700 })
            copyFileSync(path, copy)
            this.d.log.info('media.transcribe_retry', { via: 'shared_dir' })
            res = await this.postTranscribe(copy)
          } finally { rmSync(copy, { force: true }) }
        }
      }
      if (!res.ok) throw new Error(`voice-bridge ${res.status}`)
      const j = await res.json() as { text?: string }
      return typeof j.text === 'string' && j.text.trim() ? j.text.trim() : null
    } catch (e) {
      this.d.log.warn('media.transcribe_failed', { err: safeError(e) })
      return null
    }
  }

  private postTranscribe(path: string): Promise<Response> {
    return fetch(`${this.d.bridgeUrl}/transcribe_file`, { method: 'POST', headers: this.headers(), body: JSON.stringify({ path }), signal: AbortSignal.timeout(TRANSCRIBE_TIMEOUT_MS) })
  }

  /** 合成一段语音，存成 ogg 文件返回路径；失败返回 null（调用方改发文字） */
  async synthesize(text: string, voiceId: string, emotion?: string, instruct?: string): Promise<string | null> {
    try {
      const res = await fetch(`${this.d.bridgeUrl}/synthesize_voice`, {
        method: 'POST', headers: this.headers(),
        body: JSON.stringify({ text, voice_id: voiceId, emotion: emotion || 'NEUTRAL', instruct: instruct || '' }),
        signal: AbortSignal.timeout(90_000),
      })
      if (!res.ok) throw new Error(`voice-bridge ${res.status}`)
      const path = join(this.d.mediaDir, `tts-${Date.now()}-${Math.random().toString(16).slice(2, 8)}.ogg`)
      writeFileSync(path, new Uint8Array(await res.arrayBuffer()), { mode: 0o600 })
      return path
    } catch (e) {
      this.d.log.warn('media.synthesize_failed', { err: safeError(e) })
      return null
    }
  }
}

/** 给模型的图片内容块（ACP 的 image 块）。读不到文件返回 null。 */
export function imageBlock(path: string): { type: 'image'; mimeType: string; data: string } | null {
  try {
    const ext = path.split('.').pop()?.toLowerCase()
    const mimeType = ext === 'png' ? 'image/png' : ext === 'webp' ? 'image/webp' : ext === 'gif' ? 'image/gif' : 'image/jpeg'
    return { type: 'image', mimeType, data: readFileSync(path).toString('base64') }
  } catch { return null }
}
