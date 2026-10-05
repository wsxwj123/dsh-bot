// 报告：同时打印到终端并写进 report.md。只写数字和测试用的合成对话，不写密钥。
import { appendFileSync, writeFileSync } from 'fs'
import { join } from 'path'

export class Report {
  readonly path: string
  constructor(dir: string, title: string) {
    this.path = join(dir, 'report.md')
    writeFileSync(this.path, '')
    this.h(1, title)
    this.line(`生成时间：${new Date().toISOString()}`)
  }
  line(s = '') { console.log(s); appendFileSync(this.path, s + '\n') }
  h(level: number, s: string) { this.line(''); this.line(`${'#'.repeat(level)} ${s}`); this.line('') }
  table(head: string[], rows: (string | number | undefined | null)[][]) {
    const cell = (v: unknown) => (v === undefined || v === null ? '' : String(v)).replace(/\|/g, '\\|').replace(/\n/g, ' ')
    this.line(`| ${head.join(' | ')} |`)
    this.line(`|${head.map(() => '---').join('|')}|`)
    for (const r of rows) this.line(`| ${r.map(cell).join(' | ')} |`)
    this.line()
  }
  block(s: string) { this.line('```'); this.line(s); this.line('```') }
}

export function pct(part?: number, whole?: number): string {
  if (part === undefined || whole === undefined || whole === 0) return ''
  return `${Math.round((part / whole) * 100)}%`
}
