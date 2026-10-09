// 回程补料（scripts/import_history.py --since）的验收式用例：真网关 + 假 dsh。
// 场景：bot 从旧系统切回新系统，把"切到旧系统那段时间"的记录增量导进来。
// 要验证的不只是"导入成功"，而是"她接得上"：导入后下一轮请求里要出现旧系统那段里的某句话
// （增量导入会把该聊天还活着的段标成 needs_seed=1，网关下一轮重新构建前情，把刚导的原话带进去）。
import { afterEach, expect, test } from 'bun:test'
import { existsSync, mkdirSync, utimesSync, writeFileSync } from 'fs'
import { join } from 'path'
import { Database } from 'bun:sqlite'
import { FakeTelegram } from '../fakes/fake-telegram'
import { cleanup, Gateway, makeBot, OWNER, prompts, until, type BotEnv } from '../harness'

const REPO = join(import.meta.dir, '..', '..', '..')
const PY = process.env.DSH_BOT_PYTHON || (process.platform === 'win32' ? 'python' : 'python3')
let tg: FakeTelegram | null = null
let b: BotEnv | null = null
let gw: Gateway | null = null

afterEach(async () => {
  await gw?.stop()
  tg?.stop()
  if (b) cleanup(b)
  tg = null; b = null; gw = null
})

/**
 * 只读瞄一眼账本的一行。库还没建出来、schema 还没写全（网关刚起、migrate 还在跑）都当作
 * "还没就绪"返回 null —— 这个函数要能安全地放进 until(() => …) 当等待条件：
 * until 不会重试回调抛出的错，而 CI 的 macOS 上启动慢一拍，账本还没落盘时只读打开会抛
 * SQLiteError: unable to open database file（本机跑得快、账本早建好了，所以只在 CI 上现形）。
 * 真出了别的错照样抛，不吞。
 */
function peek(path: string, sql: string, ...args: (string | number)[]): unknown {
  if (!existsSync(path)) return null // 库还没建出来
  let db: Database | null = null
  try {
    db = new Database(path, { readonly: true })
    const row = db.query(sql).get(...(args as never[])) as Record<string, unknown> | null
    return row ? Object.values(row)[0] : null
  } catch (e) {
    // 网关刚建库、schema 还没写完的窗口：也当"还没就绪"；wait 的事交给调用方的 until
    if (/no such table|unable to open|locked/i.test(String(e))) return null
    throw e
  } finally {
    if (db) db.close()
  }
}

test('增量导入后，下一轮请求里出现旧系统那段里的某句话（还活着的段重新取前情）', async () => {
  tg = new FakeTelegram()
  b = makeBot(tg, { gw: { burst_window_ms: 0 } })
  const bot = b
  const ledger = join(bot.botDir, 'state', 'ledger.sqlite')

  // 1) 先在新系统聊一轮：建好会话段，needs_seed 在第一次开口时就用掉了（此后是普通续聊）
  gw = new Gateway(bot)
  await gw.start()
  // 等待条件里读账本必须对"库还没建出来"安全（CI 的 macOS 上启动慢一拍，就是这条路径挂的）
  expect(peek(join(bot.root, 'state', 'ledger-not-yet.sqlite'), 'SELECT 1 FROM inbound LIMIT 1')).toBeNull()
  tg.pushText(OWNER, '今天怎么样')
  await until(() => prompts(bot).some(p => p.text.includes('今天怎么样')), '第一轮请求')
  await until(() => Number(peek(ledger, "SELECT COUNT(*) AS n FROM inbound WHERE chat_id = ? AND state = 'done'", String(OWNER))) === 1, '第一轮处理完')
  expect(existsSync(ledger)).toBe(true)
  const first = prompts(bot).find(p => p.text.includes('今天怎么样'))!.text
  expect(first).not.toContain('旧系统里补的一句关键话')
  expect(Number(peek(ledger, "SELECT needs_seed FROM segments WHERE state = 'active'"))).toBe(0)

  // 2) 停网关（真机上是先切回旧系统；这里直接模拟"切走"）
  await gw.stop()
  gw = null
  // 心跳文件还在、还很新鲜：导入脚本会以为网关还在跑，把它推到 1 分钟前（网关已经停了，这是实情）。
  // 文件不在也算"没在跑"，跳过就行——别让 utimesSync 对不存在的文件抛 ENOENT
  const hb = join(bot.botDir, 'state', 'heartbeat')
  if (existsSync(hb)) { const old = new Date(Date.now() - 60_000); utimesSync(hb, old, old) }

  // 3) 旧系统那几天：造旧频道目录与会话文件，记录的时间戳晚于水位线（= 账本里这个聊天的最后一条）
  const home = join(bot.root, 'oldhome')
  const chan = join(bot.root, 'old', 'chan')
  mkdirSync(chan, { recursive: true })
  writeFileSync(join(chan, 'access.json'), JSON.stringify({ allowFrom: [String(OWNER)] }))
  const env = { ...process.env, HOME: home, USERPROFILE: home, DSH_BOT_HOME: bot.root, PYTHONIOENCODING: 'utf-8' }
  const slug = Bun.spawnSync([PY, '-c', `import chat_history,sys; print(chat_history._project_slug_for(sys.argv[1]))`, chan], { cwd: REPO, env }).stdout.toString().trim()
  const proj = join(home, '.claude', 'projects', slug)
  mkdirSync(proj, { recursive: true })
  const wm = Number(peek(ledger, 'SELECT MAX(t) AS t FROM (SELECT MAX(ts) AS t FROM inbound WHERE chat_id = ? UNION ALL SELECT MAX(COALESCE(sent_at, created_at)) AS t FROM outbound WHERE chat_id = ?)', String(OWNER), String(OWNER)))
  expect(wm).toBeGreaterThan(0)
  const t = (ms: number) => new Date(ms).toISOString()
  writeFileSync(join(proj, 's.jsonl'), [
    { type: 'user', timestamp: t(wm + 60_000), message: { content: `<channel source="telegram" chat_id="${OWNER}">\n旧系统里补的一句关键话\n</channel>` } },
    { type: 'assistant', timestamp: t(wm + 120_000), message: { content: [{ type: 'tool_use', name: 'mcp__telegram-worker__reply', input: { text: '补料那几天的回复' } }] } },
  ].map(l => JSON.stringify(l)).join('\n') + '\n')

  const r = Bun.spawnSync([PY, join(REPO, 'scripts', 'import_history.py'), bot.name, '--from', chan, '--since', 'auto'], { cwd: REPO, env })
  expect(r.exitCode).toBe(0)
  expect(r.stdout.toString()).toContain('增量导入')
  // 导入的东西必须被"标成重新取前情"，否则记录只是躺在账本里
  expect(Number(peek(ledger, "SELECT needs_seed FROM segments WHERE state = 'active'"))).toBe(1)

  // 4) 起回来，说下一句：这一轮请求里要能看到旧系统那段里的话
  gw = new Gateway(bot)
  await gw.start()
  tg.pushText(OWNER, '接着聊昨天的事')
  await until(() => prompts(bot).some(p => p.text.includes('接着聊昨天的事')), '第二轮的请求')
  const second = prompts(bot).find(p => p.text.includes('接着聊昨天的事'))!.text
  expect(second).toContain('旧系统里补的一句关键话')
  expect(second).toContain('补料那几天的回复')
  // 用掉之后就归零，不会每轮都重发一遍前情
  expect(Number(peek(ledger, "SELECT needs_seed FROM segments WHERE state = 'active'"))).toBe(0)
})
