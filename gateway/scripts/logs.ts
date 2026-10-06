// 看 bot 的日志（像 tmux attach 那样实时滚动）。只读日志文件，不连网关，网关是终端里跑的还是开机自启的都能看。
//   bun gateway/scripts/logs.ts --config <配置文件>            最近 50 条事件
//   bun gateway/scripts/logs.ts --config <配置文件> -f         接着实时看（Ctrl+C 退出）
//   加 -n 200 看更多；加 --chat 把对话正文也穿插进来（正文是私密内容，只在自己的终端里看）
import { closeSync, existsSync, openSync, readFileSync, readSync, statSync } from 'fs'
import { join } from 'path'
import { loadBotConfig } from '../src/config'
import { formatChat, formatEvent, lineTime } from '../src/logview'

const argv = process.argv.slice(2)
const opt = (n: string) => { const i = argv.indexOf(n); return i >= 0 ? argv[i + 1] : undefined }
const cfgPath = opt('--config')
if (!cfgPath) { console.log('用法：bun gateway/scripts/logs.ts --config <配置文件> [-f] [-n 条数] [--chat]'); process.exit(2) }
const follow = argv.includes('-f') || argv.includes('--follow')
const n = Math.max(0, Number(opt('-n') ?? 50) || 50)
const cfg = loadBotConfig(cfgPath)
const files = [{ path: join(cfg.logsDir, 'gateway.log'), chat: false }, ...(argv.includes('--chat') ? [{ path: join(cfg.logsDir, 'chat.log'), chat: true }] : [])]
const show = (line: string, chat: boolean) => console.log(chat ? formatChat(line) : formatEvent(line))

// 先打最近 n 条（几个文件按时间合在一起）
const backlog: { t: number; line: string; chat: boolean }[] = []
for (const f of files) {
  if (!existsSync(f.path)) continue
  for (const line of readFileSync(f.path, 'utf8').split('\n').filter(Boolean).slice(-n)) backlog.push({ t: lineTime(line, f.chat), line, chat: f.chat })
}
backlog.sort((a, b) => a.t - b.t)
for (const r of backlog.slice(-n)) show(r.line, r.chat)
if (backlog.length === 0) console.log(`（还没有日志：${files[0]!.path}）`)
if (!follow) process.exit(0)

// 接着实时看：每半秒看一下文件长了没有；文件变短或换了（日志轮转）就从头读新文件
const pos = files.map(f => { try { const s = statSync(f.path); return { size: s.size, ino: s.ino } } catch { return { size: 0, ino: 0 } } })
const rest = files.map(() => '')
setInterval(() => {
  files.forEach((f, i) => {
    let s
    try { s = statSync(f.path) } catch { return }
    if (s.ino !== pos[i]!.ino || s.size < pos[i]!.size) { pos[i] = { size: 0, ino: s.ino }; rest[i] = '' }
    if (s.size === pos[i]!.size) return
    const fd = openSync(f.path, 'r')
    const buf = Buffer.alloc(s.size - pos[i]!.size)
    readSync(fd, buf, 0, buf.length, pos[i]!.size)
    closeSync(fd)
    pos[i]!.size = s.size
    const text = rest[i] + buf.toString('utf8')
    const lines = text.split('\n')
    rest[i] = lines.pop() ?? ''
    for (const line of lines) if (line) show(line, f.chat)
  })
}, 500)
