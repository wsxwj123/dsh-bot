// 长期记忆：<频道目录>/memory/MEMORY.md（加上同目录里的主题记忆文件）。
// 每个新段开头整份带上；模型用 remember 工具往里追加；Python 的整理任务每周压缩一次。
// 写入一律原子（先写临时文件再改名），同一进程里的写入排队，不会互相覆盖。
import { existsSync, mkdirSync, readdirSync, readFileSync } from 'fs'
import { join } from 'path'
import { writeAtomic } from './util'

export const MEMORY_FILE = 'MEMORY.md'
const SECTION = '## 随手记'
/** 不当作记忆带进上下文的文件：备份、旧系统留下的对话转存 */
const SKIP = /(\.bak|\.tmp-\d+)$|^recent_conversation\.md$/

export class MemoryStore {
  private chain: Promise<void> = Promise.resolve()

  constructor(readonly dir: string) {}

  get path(): string { return join(this.dir, MEMORY_FILE) }

  /** 新段开头要带的长期记忆：MEMORY.md 在前，其余主题文件按名字排在后面。超过上限从尾部截断并注明。 */
  readForSeed(maxChars: number): { text: string; truncated: boolean } {
    if (!existsSync(this.dir)) return { text: '', truncated: false }
    const parts: string[] = []
    if (existsSync(this.path)) parts.push(readFileSync(this.path, 'utf8').trim())
    const others = readdirSync(this.dir).filter(f => f.endsWith('.md') && f !== MEMORY_FILE && !SKIP.test(f)).sort()
    for (const f of others) {
      const t = readFileSync(join(this.dir, f), 'utf8').trim()
      if (t) parts.push(`（${f.replace(/\.md$/, '')}）\n${t}`)
    }
    const all = parts.filter(Boolean).join('\n\n')
    if (all.length <= maxChars) return { text: all, truncated: false }
    return { text: `${all.slice(0, maxChars)}\n……（长期记忆太长，后面的部分这次没有带上）`, truncated: true }
  }

  /** 追加一条到"随手记"一节（没有这一节就在文末新建）。同样的内容已经有了就不重复记。 */
  append(text: string, date: string): Promise<{ added: boolean }> {
    const run = this.chain.then(() => {
      mkdirSync(this.dir, { recursive: true })
      const cur = existsSync(this.path) ? readFileSync(this.path, 'utf8') : '# Memory\n'
      const line = `- ${date} ${text}`
      if (cur.split('\n').some(l => l.replace(/^- \d{4}-\d{2}-\d{2} /, '- ').trim() === `- ${text}`)) return { added: false }
      let next: string
      const i = cur.indexOf(`\n${SECTION}`)
      if (i < 0) {
        next = `${cur.replace(/\s*$/, '')}\n\n${SECTION}\n${line}\n`
      } else {
        // 插到这一节的末尾（下一个同级或更高级标题之前）
        const start = i + 1 + SECTION.length
        const rest = cur.slice(start)
        const m = rest.match(/\n#{1,2} /)
        const end = m ? start + m.index! : cur.length
        next = `${cur.slice(0, end).replace(/\s*$/, '')}\n${line}\n${cur.slice(end).replace(/^\n*/, m ? '\n' : '')}`
      }
      writeAtomic(this.path, next)
      return { added: true }
    })
    this.chain = run.then(() => {}, () => {})
    return run
  }
}
