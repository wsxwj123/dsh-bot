// 把日志排成人看的样子：一行一条，本机时间在前。网关在终端里跑（tmux 里）时用，scripts/logs.ts 实时看日志也用。
const SKIP = new Set(['at', 'level', 'event', 'bot'])
const MARK: Record<string, string> = { warn: '⚠ ', error: '✗ ', debug: '· ' }

function clock(at: unknown): string {
  const d = new Date(typeof at === 'string' || typeof at === 'number' ? at : Date.now())
  return Number.isNaN(d.getTime()) ? '--:--:--' : d.toLocaleTimeString('zh-CN', { hour12: false })
}

function val(v: unknown): string {
  const s = typeof v === 'string' ? v : JSON.stringify(v)
  return s.length > 120 ? `${s.slice(0, 120)}…` : s
}

/** gateway.log 的一行（JSON）→ "23:41:05  ⚠ turn.end  turn=60 outcome=ok"。读不懂的行原样返回 */
export function formatEvent(line: string): string {
  let r: Record<string, unknown>
  try { r = JSON.parse(line) } catch { return line }
  if (!r || typeof r !== 'object' || typeof r.event !== 'string') return line
  const fields = Object.entries(r).filter(([k, v]) => !SKIP.has(k) && v !== undefined).map(([k, v]) => `${k}=${val(v)}`)
  return `${clock(r.at)}  ${MARK[String(r.level)] ?? ''}${r.event}${fields.length ? `  ${fields.join(' ')}` : ''}`
}

/** chat.log 的一行（"<ISO 时间> [<聊天>] <谁>: <正文>"）→ "23:41:05  💬 [<聊天>] <谁>: <正文>" */
export function formatChat(line: string): string {
  const m = line.match(/^(\S+) (\[[^\]]*\]) (.*)$/)
  return m ? `${clock(m[1])}  💬 ${m[2]} ${m[3]!.replace(/⏎/g, ' / ')}` : line
}

/** 一行日志的时间（排序用），读不出来返回 0 */
export function lineTime(line: string, chat: boolean): number {
  if (chat) return Date.parse(line.split(' ', 1)[0] ?? '') || 0
  try { return Date.parse(JSON.parse(line).at) || 0 } catch { return 0 }
}
