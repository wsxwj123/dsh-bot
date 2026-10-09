// 引导按钮的纯函数（方案 3.3.2）：callback_data 编解码、分页、按钮字截断、菜单文字截断。
// callback_data 一律 ASCII、≤40 字节、形如 w|<8 位口令>|<动作>[|<序号>]；名字与模型名不进 data（只放序号）。
import type { TgInlineButton, TgInlineKeyboard } from '../telegram/api'

/** 一个待编码的按钮：label 是按钮上的字，action 是动作标识，idx 指渲染时保存的列表快照的第几项（从 0 起） */
export type Btn = { label: string; action: string; idx?: number }

/** 每页列表项数（方案 3.3.2） */
export const PAGE_SIZE = 8

const NONCE_CHARS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'

/** 新的 8 位口令：只用字母数字（不能含 |，40 字节上限也留足余量） */
export function makeNonce(): string {
  let s = ''
  for (let i = 0; i < 8; i++) s += NONCE_CHARS[Math.floor(Math.random() * NONCE_CHARS.length)]
  return s
}

/** 编码一个回调数据：w|<口令>|<动作>[|<序号>] */
export function encodeCb(nonce: string, action: string, idx?: number): string {
  return `w|${nonce}|${action}${idx === undefined ? '' : `|${idx}`}`
}

/** 解码回调数据；不是 w|… 形状、口令不是 8 位、序号不是整数都算畸形（返回 null，调用方按 malformed 处理） */
export function decodeCb(data: unknown): { nonce: string; action: string; idx: number | null } | null {
  if (typeof data !== 'string') return null
  const parts = data.split('|')
  if (parts.length < 3 || parts.length > 4) return null
  const [w, nonce, action, idxRaw] = parts
  if (w !== 'w' || !nonce || nonce.length !== 8 || !action) return null
  let idx: number | null = null
  if (idxRaw !== undefined) {
    if (!/^\d+$/.test(idxRaw)) return null
    idx = Number(idxRaw)
  }
  return { nonce, action, idx }
}

/** 按钮上的模型名超过 40 个字符时保留前 18 + 后 18，中间写 …（方案 3.3.2）。
 *  按 UTF-16 单元计数：模型 id 允许非 ASCII（聚合商渠道前缀），中文每个 1 个单元照切；按钮只放序号，切坏也不影响回调定位 */
export function truncLabel(name: string): string {
  return name.length > 40 ? `${name.slice(0, 18)}…${name.slice(-18)}` : name
}

/** 把一组按钮排成 Telegram 的内联键盘：每行最多 3 个，其余各自成行（列表项多了也好看） */
export function toKeyboard(nonce: string, buttons: Btn[]): TgInlineKeyboard {
  const rows: TgInlineButton[][] = []
  let row: TgInlineButton[] = []
  for (const b of buttons) {
    row.push({ text: b.label, callback_data: encodeCb(nonce, b.action, b.idx) })
    if (row.length >= 3) { rows.push(row); row = [] }
  }
  if (row.length) rows.push(row)
  return { inline_keyboard: rows }
}

/** 一页列表：返回该页的项与页码信息；page 从 0 起，超出范围夹到有效区间 */
export function pageSlice<T>(items: T[], page: number): { slice: T[]; page: number; pages: number; start: number } {
  const pages = Math.max(1, Math.ceil(items.length / PAGE_SIZE))
  const p = Math.min(Math.max(0, page), pages - 1)
  const start = p * PAGE_SIZE
  return { slice: items.slice(start, start + PAGE_SIZE), page: p, pages, start }
}

/**
 * 菜单文字超过 3500 个字符时截断：保留表头与能放下的列表行，末尾写 `…还有 <n> 个`（方案 3.3.2、3.5.2）。
 * lines 是完整行；headerCount 是表头行数（永远保留）；unit 是量词（一般项、管理页用「个」）。
 */
export function truncateMenu(header: string[], lines: string[], unit = '项'): string {
  const full = [...header, ...lines].join('\n')
  if (full.length <= 3500) return full
  const out = [...header]
  let len = header.join('\n').length
  for (const l of lines) {
    const add = 1 + l.length
    // 给末尾的「…还有 N 个」留位置（约 20 字符）
    if (len + add > 3500 - 24) break
    out.push(l)
    len += add
  }
  const shown = out.length - header.length
  const rest = lines.length - shown
  return `${out.join('\n')}\n…还有 ${rest} ${unit}`
}
