// 收到的 Telegram 消息 → 白名单闸门 → 账本记录。纯函数，方便测试。
import type { Access } from '../config'
import type { NewInbound } from '../ledger'
import { redactSecrets } from '../log'
import type { TgMessage } from './api'

/** deliver: 'observe' = 群消息，只记进群聊记录（谁说话由导演决定，见 director.py），不直接触发回复 */
export type GateResult = { deliver: true } | { deliver: 'observe' } | { deliver: false; reason: string }

export const isGroupChat = (chatId: string | number) => String(chatId).startsWith('-')

export function gate(msg: TgMessage, access: Access): GateResult {
  if (access.dmPolicy === 'disabled') return { deliver: false, reason: 'dm disabled' }
  if (!msg.from) return { deliver: false, reason: 'no sender' }
  if (msg.chat.type === 'group' || msg.chat.type === 'supergroup') {
    const policy = access.groups[String(msg.chat.id)]
    if (!policy) return { deliver: false, reason: 'group not in access.json' }
    const allow = (policy.allowFrom ?? []).map(String)
    if (allow.length > 0 && !allow.includes(String(msg.from.id))) return { deliver: false, reason: 'sender not in group allowFrom' }
    // 群里只有点名本 bot 的命令（/clear@本bot 等）直接交给网关，其余都只记录
    const cmd = parseCommand(msg.text)
    if (cmd && cmd.target && GATEWAY_COMMANDS.has(cmd.name)) return { deliver: true }
    return { deliver: 'observe' }
  }
  if (msg.chat.type !== 'private') return { deliver: false, reason: `chat type ${msg.chat.type} not handled` }
  if (!access.allowFrom.includes(String(msg.from.id))) return { deliver: false, reason: 'sender not in allowFrom' }
  return { deliver: true }
}

/** 网关自己处理的命令；其它以 / 开头的文字照常交给模型 */
export const GATEWAY_COMMANDS = new Set(['clear', 'start', 'compact', 'model', 'models', 'provider', 'providers', 'help', 'commands'])

export type Command = { name: string; target: string | null; args: string }

/** 解析 /cmd@bot 参数。不是命令返回 null。 */
export function parseCommand(text: string | undefined): Command | null {
  if (!text) return null
  const m = text.trim().match(/^\/([A-Za-z0-9_]{1,32})(?:@([A-Za-z0-9_]{3,64}))?(?:\s+([\s\S]*))?$/)
  if (!m) return null
  return { name: m[1]!.toLowerCase(), target: m[2] ?? null, args: (m[3] ?? '').trim() }
}

function placeholder(msg: TgMessage): string | null {
  if (msg.photo) return '[图片]'
  if (msg.voice) return '[语音消息]'
  if (msg.audio) return '[音频]'
  if (msg.video || msg.animation) return '[视频]'
  if (msg.video_note) return '[视频消息]'
  if (msg.sticker) return `[贴纸${msg.sticker.emoji ? ` ${msg.sticker.emoji}` : ''}]`
  if (msg.document) return `[文件${msg.document.file_name ? ` ${msg.document.file_name}` : ''}]`
  if (msg.location) return '[位置]'
  if (msg.contact) return '[联系人]'
  return null
}

export function senderName(u: TgMessage['from']): string {
  if (!u) return ''
  return [u.first_name, u.last_name].filter(Boolean).join(' ') || u.username || String(u.id)
}

/**
 * 入账前把登记过的机密值换成 ***（方案 3.12）：主人引用回复一条含密钥的消息（删除失败或还没来得及删）时，
 * 引用文字里也带着密钥。要在 toInbound 截断引用文字之前换，否则截出的半截密钥就认不出来了。
 */
export function redactMessage(msg: TgMessage): TgMessage {
  const clean = (m: TgMessage): TgMessage => ({
    ...m,
    ...(m.text !== undefined ? { text: redactSecrets(m.text) } : {}),
    ...(m.caption !== undefined ? { caption: redactSecrets(m.caption) } : {}),
  })
  const r = msg.reply_to_message
  return { ...clean(msg), ...(r ? { reply_to_message: clean(r) } : {}) }
}

/**
 * 把一条 Telegram 消息变成账本记录。图片、语音等第一期只记一个占位说明（M4 接入真正的内容）。
 * 唯一键 = tg:<聊天 id>:<消息 id>：Telegram 重发同一更新时靠它去重。
 */
export function toInbound(msg: TgMessage, botId: number | null): NewInbound {
  const body = msg.text ?? msg.caption ?? ''
  const ph = placeholder(msg)
  const text = ph ? (body ? `${ph} ${body}` : ph) : body
  const meta: Record<string, unknown> = {}
  // 语音、图片：先记一个占位，下载和转写放到处理这一轮之前（不卡收消息）
  if (msg.voice?.file_id) meta.media = { kind: 'voice', file_id: msg.voice.file_id, duration: msg.voice.duration ?? null }
  else if (msg.photo?.length) meta.media = { kind: 'photo', file_id: msg.photo[msg.photo.length - 1]!.file_id }
  const r = msg.reply_to_message
  if (r) {
    meta.reply_to = {
      message_id: r.message_id,
      from_me: botId !== null && r.from?.id === botId,
      text: (r.text ?? r.caption ?? placeholder(r) ?? '').slice(0, 120),
    }
  }
  const parsed = parseCommand(msg.text)
  const cmd = parsed && GATEWAY_COMMANDS.has(parsed.name) ? parsed : null
  return {
    ukey: `tg:${msg.chat.id}:${msg.message_id}`,
    chatId: String(msg.chat.id),
    kind: cmd ? 'command' : 'user',
    tgMessageId: msg.message_id,
    senderId: msg.from ? String(msg.from.id) : null,
    senderName: senderName(msg.from),
    text,
    meta: Object.keys(meta).length ? meta : null,
    ts: msg.date * 1000,
  }
}
