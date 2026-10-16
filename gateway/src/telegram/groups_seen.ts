// 刚发现的群（约定 v1.1 5.20）：bot 被拉进群、被设成管理员、被移出群（my_chat_member），以及没加入的群里
// 有人说话被挡下，都记进 <bot>/state/groups-seen.json，适配服务据此把"刚发现的群"列给管理台。
// 只记群的身份信息（群名、类型、bot 的身份、是不是主人拉的、时间），不记任何消息内容，也不记拉人者的编号和名字。
import { readFileSync } from 'fs'
import { safeError, type Logger } from '../log'
import { writeAtomic } from '../util'
import type { TgChat, TgChatMember, TgChatMemberUpdated, TgMessage } from './api'

export type GroupStatus = 'member' | 'administrator' | 'restricted' | 'left' | 'kicked'
export type SeenGroup = {
  title: string
  type: 'group' | 'supergroup'
  status: GroupStatus
  /** 拉它进群的人是不是主人（access.json 的 allowFrom 里）；只从群消息里见过的为 null */
  added_by_owner: boolean | null
  first_seen: string
  last_seen: string
  /** 群升级成超级群后换了新号：记原来的群号 */
  migrated_from: string | null
}

/** 最多留这么多个群 */
export const MAX_GROUPS = 50
/** 同一个群"又看到一次"最多这么久写一次（身份变化随时写） */
const REFRESH_MS = 60_000

const OUT = new Set<GroupStatus>(['left', 'kicked'])

/** bot 在群里的身份。restricted 而 is_member 为假时其实已经不在群里，记作 left；creator 不会出现，按 administrator 算 */
export function memberStatus(m: TgChatMember): GroupStatus {
  if (m.status === 'restricted') return m.is_member === false ? 'left' : 'restricted'
  if (m.status === 'creator') return 'administrator'
  return m.status === 'administrator' || m.status === 'left' || m.status === 'kicked' ? m.status : 'member'
}

const isGroup = (c: TgChat) => c.type === 'group' || c.type === 'supergroup'
const utc = (ms: number) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z')

export class GroupsSeen {
  private lastWrite = new Map<string, number>()

  constructor(
    private readonly path: string,
    /** 主人的编号（access.json 的 allowFrom），每次现读 */
    private readonly owners: () => string[],
    private readonly log: Logger,
    private readonly now: () => number = Date.now,
  ) {}

  /** my_chat_member：身份变化一律记下；原来不在群里、这次进来了，记下是不是主人拉的 */
  onMember(u: TgChatMemberUpdated): void {
    if (!isGroup(u.chat)) return
    const status = memberStatus(u.new_chat_member)
    const joined = OUT.has(memberStatus(u.old_chat_member)) && !OUT.has(status)
    this.write(String(u.chat.id), u.chat, true, g => {
      g.status = status
      if (joined) g.added_by_owner = this.owners().includes(String(u.from.id))
    })
  }

  /** 没加入的群里有人说话被挡下：记下见过这个群；新群里第一条带原群号时记下 migrated_from，原群号记作 left */
  onDropped(msg: TgMessage): void {
    if (!isGroup(msg.chat)) return
    const from = msg.migrate_from_chat_id !== undefined ? String(msg.migrate_from_chat_id) : null
    this.write(String(msg.chat.id), msg.chat, from !== null, g => { if (from) g.migrated_from = from }, from)
  }

  private write(id: string, chat: TgChat, change: boolean, apply: (g: SeenGroup) => void, retire: string | null = null): void {
    const now = this.now()
    const all = this.read()
    const had = all[id]
    // 只是"又看到一次"：一分钟内写过就不写（群里一直有人说话时不至于每条都写盘）
    if (had && !change && now - (this.lastWrite.get(id) ?? 0) < REFRESH_MS) return
    const at = utc(now)
    const g: SeenGroup = had ?? { title: '', type: 'group', status: 'member', added_by_owner: null, first_seen: at, last_seen: at, migrated_from: null }
    g.title = chat.title ?? g.title
    g.type = chat.type === 'supergroup' ? 'supergroup' : 'group'
    g.last_seen = at
    apply(g)
    all[id] = g
    if (retire && all[retire]) all[retire]!.status = 'left'
    evict(all)
    try {
      writeAtomic(this.path, JSON.stringify(all, null, 1), 0o600)
      this.lastWrite.set(id, now)
    } catch (e) {
      this.log.warn('groups.seen_write_failed', { err: safeError(e) })
    }
  }

  private read(): Record<string, SeenGroup> {
    let raw: string
    try { raw = readFileSync(this.path, 'utf8') } catch { return {} }
    try {
      const v = JSON.parse(raw)
      if (v && typeof v === 'object' && !Array.isArray(v)) return v as Record<string, SeenGroup>
    } catch {}
    this.log.warn('groups.seen_unreadable', { action: 'start over' })
    return {}
  }
}

/** 超过 MAX_GROUPS：先挤不是主人拉的里面 last_seen 最旧的；都是主人拉的就挤最旧的 */
function evict(all: Record<string, SeenGroup>): void {
  while (Object.keys(all).length > MAX_GROUPS) {
    const rows = Object.entries(all)
    const pool = rows.filter(([, g]) => g.added_by_owner !== true)
    const [id] = (pool.length ? pool : rows).sort((a, b) => a[1].last_seen.localeCompare(b[1].last_seen))[0]!
    delete all[id]
  }
}
