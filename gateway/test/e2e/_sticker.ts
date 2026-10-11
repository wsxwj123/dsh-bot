// 表情包验收测试的公共件：假 HOME 造库（与生产同构）、调 sticker 工具、读日志与发送记录。
// 契约来源：.devflow/INTERFACE-表情包接线.md（只看契约、不看实现）。
import { mkdirSync, realpathSync, writeFileSync } from 'fs'
import { dirname, join } from 'path'
import { FakeTelegram } from '../fakes/fake-telegram'
import { Gateway, OWNER, readJsonl, toolResults, until, type BotEnv } from '../harness'

/** 在 root（测试临时目录）下建一个 fakehome 并返回规范化绝对路径。
 *  realpath：macOS 的 /var → /private/var 这类符号链接统一掉，断言绝对路径时不会两头差一截。 */
export function makeStickerHome(root: string): string {
  const raw = join(root, 'fakehome')
  mkdirSync(join(raw, '.claude', 'channels', '_shared'), { recursive: true })
  mkdirSync(join(raw, 'resource', 'stickers'), { recursive: true })
  return realpathSync(raw)
}

/** 文件实体：rel 相对 <home>/resource/stickers（生产里库的允许根） */
export function putStickerFiles(home: string, files: Record<string, string>): void {
  for (const [rel, content] of Object.entries(files)) {
    const p = join(home, 'resource', 'stickers', rel)
    mkdirSync(dirname(p), { recursive: true })
    writeFileSync(p, content)
  }
}

/** 库文件 <home>/.claude/channels/_shared/stickers.json；给字符串就原样写（坏 JSON 用例） */
export function writeStickerLib(home: string, data: unknown): void {
  writeFileSync(join(home, '.claude', 'channels', '_shared', 'stickers.json'), typeof data === 'string' ? data : JSON.stringify(data))
}

/** 库条目/文件实体的规范化绝对路径 */
export const stickerPath = (home: string, rel: string): string => join(home, 'resource', 'stickers', rel)

export type ToolRes = { sessionId: string; name: string; text: string; isError: boolean; args?: unknown }

/** 假 ACP 记录下来的 sticker 工具调用结果（按发生顺序） */
export const stickerCalls = (b: BotEnv): ToolRes[] => toolResults(b).filter(r => r.name === 'sticker') as ToolRes[]

/** 推一条消息让假 ACP 调一次 sticker（参数原样 JSON），返回这一次调用的工具结果 */
export async function callSticker(tg: FakeTelegram, b: BotEnv, args: unknown): Promise<ToolRes> {
  const n = stickerCalls(b).length
  tg.pushText(OWNER, `!tool:sticker|${JSON.stringify(args)}`)
  await until(() => stickerCalls(b).length > n, `sticker result for ${JSON.stringify(args)}`)
  return stickerCalls(b)[n]!
}

/** 网关日志按 event 过滤（gateway.log 是 JSON 行） */
export const logEvents = (b: BotEnv, event: string): Record<string, any>[] =>
  readJsonl<Record<string, any>>(join(b.botDir, 'logs', 'gateway.log')).filter(r => r.event === event)

/** 假 Telegram 收到的「发文件类」请求（表情包走的通道都在里面） */
export const FILE_METHODS = ['sendAnimation', 'sendSticker', 'sendPhoto', 'sendDocument']
export const fileSends = (tg: FakeTelegram) => tg.sent.filter(s => FILE_METHODS.includes(s.method))
export const fileSendsTo = (tg: FakeTelegram, chat: number | string) => tg.sentTo(chat).filter(s => FILE_METHODS.includes(s.method))

/** 等这一轮结束（inbound done），返回 turn id */
export async function settledTurn(gw: Gateway, b: BotEnv, mid: number): Promise<number> {
  await until(() => { const l = gw.ledger(); const st = l.inboundByKey(`tg:${OWNER}:${mid}`)?.state; l.close(); return st === 'done' }, 'turn settled')
  const l = gw.ledger()
  const turnId = l.inboundByKey(`tg:${OWNER}:${mid}`)!.turn_id!
  l.close()
  return turnId
}

// ───────────────────────── 标准库（两个测试文件共用） ─────────────────────────
// sfw:1 Happy 1 (a.gif) · sfw:2 Happy 2 (b.GIF 大写扩展名) · sfw:3 Happy (h.gif) · sfw:4 Still 1 (c.jpg)
// sfw:5 Sticky 1 (d.webp) · sfw:6-7 Same 1 (e.gif / f.gif，同名两条) · nsfw:1 露奶勾引 (g.gif)
export const STD_FILES: Record<string, string> = {
  'sfw/a.gif': 'GIF89a',
  'sfw/b.GIF': 'GIF89a',
  'sfw/h.gif': 'GIF89a',
  'sfw/c.jpg': 'JPEGDATA',
  'sfw/d.webp': 'WEBPDATA',
  'sfw/e.gif': 'GIF89a',
  'sfw/f.gif': 'GIF89a',
  'nsfw/g.gif': 'GIF89a',
}
export const stdLib = (home: string) => ({
  sfw: [
    { label: 'Happy 1', path: stickerPath(home, 'sfw/a.gif') },
    { label: 'Happy 2', path: stickerPath(home, 'sfw/b.GIF') },
    { label: 'Happy', path: stickerPath(home, 'sfw/h.gif') },
    { label: 'Still 1', path: stickerPath(home, 'sfw/c.jpg') },
    { label: 'Sticky 1', path: stickerPath(home, 'sfw/d.webp') },
    { label: 'Same 1', path: stickerPath(home, 'sfw/e.gif') },
    { label: 'Same 1', path: stickerPath(home, 'sfw/f.gif') },
  ],
  nsfw: [{ label: '露奶勾引', category: 'Forest', path: stickerPath(home, 'nsfw/g.gif') }],
})
