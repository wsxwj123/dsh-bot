// 语音和图片（M4）：
//   收：语音先下载再交给 voice-bridge 转写，图片下载到媒体目录。都放在处理这一轮之前做，不卡收消息；
//       失败也不挡聊天（语音留一句"没转成文字"）。
//   发：reply 带 as_voice 时，文字交给 voice-bridge 合成，网关自己用 sendVoice 发。Telegram 令牌不离开网关。
import { closeSync, constants as fsConstants, copyFileSync, existsSync, fstatSync, lstatSync, mkdirSync, openSync, readFileSync, readSync, readdirSync, readlinkSync, realpathSync, rmSync, statSync, writeFileSync } from 'fs'
import { basename, extname, isAbsolute, join, relative } from 'path'
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

/** 群聊共享图：读写同一上限（写侧超限不记字段，读侧超限当没有图） */
export const MAX_SHARED_IMAGE_BYTES = 8 * 1024 * 1024
/** 群记录里允许转给模型的图片扩展名 */
export const SHARED_IMAGE_EXTS = new Set(['.jpg', '.jpeg', '.png', '.webp'])

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

  /** 合成一段语音，存成 ogg 文件返回路径；失败返回 null（调用方改发文字）。
   *  model 是这只 bot 的 access.json 里的 voiceModel（约定 v0.8 B.3）：非空字符串才带上，没有就由语音桥按全局设置 */
  async synthesize(text: string, voiceId: string, emotion?: string, instruct?: string, model?: string): Promise<string | null> {
    try {
      const res = await fetch(`${this.d.bridgeUrl}/synthesize_voice`, {
        method: 'POST', headers: this.headers(),
        body: JSON.stringify({ text, voice_id: voiceId, emotion: emotion || 'NEUTRAL', instruct: instruct || '', ...(typeof model === 'string' && model ? { model } : {}) }),
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

/** 群记录里允许读的目录：write 只有共享图库，read 另加 claude 侧启动时已存在的一级目录里的 telegram-group */
export type SharedDirs = { write: readonly string[]; read: readonly string[] }

function realDir(p: string): string | null {
  try {
    const real = realpathSync(p)
    return statSync(real).isDirectory() ? real : null
  } catch { return null }
}

/** 只认真正的目录（lstat 判断）：本身是软链就不收，免得白名单被链接扩到别处 */
function realDirNoLink(p: string): string | null {
  try {
    if (!lstatSync(p).isDirectory()) return null
    return realpathSync(p)
  } catch { return null }
}

/** p 是否在 root 之下（不含 root 本身） */
function insideDir(root: string, p: string): boolean {
  const rel = relative(root, p)
  return !!rel && !rel.startsWith('..') && !isAbsolute(rel)
}

/**
 * 启动时把白名单展开并冻结（之后新出现的目录要重启才生效）。
 * 只收 sharedMediaDir，以及 claudeMediaRoot 下已存在的一级目录里的 telegram-group（群图）。
 * 一级目录本身（含其 telegram/ 私聊图）不收，免得一条被手改的群记录把私聊图拉进群回合；
 * telegram-group 必须是真目录（软链不收），且解析后仍落在 claudeMediaRoot 内。
 */
export function collectSharedDirs(o: { sharedMediaDir: string; claudeMediaRoot: string }): SharedDirs {
  const shared = realDir(o.sharedMediaDir)
  const write = shared ? [shared] : []
  const read = [...write]
  let root: string | null = null
  try { root = realpathSync(o.claudeMediaRoot) } catch {}
  let names: string[] = []
  if (root) {
    try {
      names = readdirSync(o.claudeMediaRoot, { withFileTypes: true })
        .filter(e => e.isDirectory()).map(e => e.name).sort()
    } catch {}
  }
  for (const name of names) {
    // 只到 telegram-group 这一层；<bot>/ 本身和 <bot>/telegram/（私聊）都不在群图白名单里
    const group = realDirNoLink(join(o.claudeMediaRoot, name, 'telegram-group'))
    if (group && root && insideDir(root, group)) read.push(group)
  }
  return { write: Object.freeze([...new Set(write)]), read: Object.freeze([...new Set(read)]) }
}

export type SharedImageReason = 'realpath_failed' | 'not_file' | 'outside_allowlist' | 'too_large' | 'read_failed'
export type SharedImageRead =
  | { ok: true; real: string; mimeType: string; data: string }
  | { ok: false; reason: SharedImageReason }

function mimeOf(path: string): string {
  const ext = extname(path).toLowerCase()
  return ext === '.png' ? 'image/png' : ext === '.webp' ? 'image/webp' : 'image/jpeg'
}

function inAllowlist(real: string, dirs: readonly string[]): boolean {
  for (const d of dirs) {
    const rel = relative(d, real)
    if (rel && !rel.startsWith('..') && !isAbsolute(rel)) return true
  }
  return false
}

/** 已打开 fd 的真实路径（打开后再复核一次，防 realpath 与 open 之间被换掉）；拿不到返回 null */
function fdRealPath(fd: number): string | null {
  try {
    if (process.platform === 'darwin') return realpathSync(`/dev/fd/${fd}`)
    if (process.platform === 'linux') {
      const p = readlinkSync(`/proc/self/fd/${fd}`)
      // 打开后被删掉的文件读出来会带这个后缀，不再当作白名单内的文件
      return p.endsWith(' (deleted)') ? null : p
    }
  } catch {}
  return null
}

/**
 * 校验并读出一张群记录图片：realpath → 白名单 → O_NOFOLLOW 打开 → 按 fd 复核真实路径 →
 * 同一个 fd 上 fstat + read（关闭 TOCTOU）。
 * 任何一步不满足都返回原因，调用方静默降级（日志只记原因，不记路径）。
 */
export function readSharedImage(path: string, allowDirs: readonly string[], maxBytes = MAX_SHARED_IMAGE_BYTES): SharedImageRead {
  let real: string
  try { real = realpathSync(path) } catch { return { ok: false, reason: 'realpath_failed' } }
  if (!inAllowlist(real, allowDirs)) return { ok: false, reason: 'outside_allowlist' }
  let fd: number
  try {
    // 最后一段若是 realpath 之后被换成的符号链接，O_NOFOLLOW 会让 open 失败
    const flags = (fsConstants.O_RDONLY ?? 0) | (fsConstants.O_NOFOLLOW ?? 0)
    fd = openSync(real, flags)
  } catch { return { ok: false, reason: 'read_failed' } }
  try {
    // 复核已打开的那个文件到底在哪：中间目录被换掉也逃不出白名单
    const actual = fdRealPath(fd)
    if (actual !== null && !inAllowlist(actual, allowDirs)) return { ok: false, reason: 'outside_allowlist' }
    const st = fstatSync(fd)
    if (!st.isFile()) return { ok: false, reason: 'not_file' }
    // 0 字节会变成空 data 的图片块，可能让上游整条拒收：当作读失败
    if (st.size <= 0) return { ok: false, reason: 'read_failed' }
    if (st.size > maxBytes) return { ok: false, reason: 'too_large' }
    const buf = Buffer.alloc(st.size)
    let off = 0
    while (off < buf.length) {
      const n = readSync(fd, buf, off, buf.length - off, off)
      if (n <= 0) return { ok: false, reason: 'read_failed' }
      off += n
    }
    return { ok: true, real, mimeType: mimeOf(real), data: buf.toString('base64') }
  } catch {
    return { ok: false, reason: 'read_failed' }
  } finally {
    try { closeSync(fd) } catch {}
  }
}
