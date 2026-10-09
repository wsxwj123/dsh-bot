// 回程补料（scripts/import_history.py --since）的验收式用例：真网关 + 假 dsh。
// 场景：bot 从旧系统切回新系统，把"切到旧系统那段时间"的记录增量导进来。
// 要验证的不只是"导入成功"，而是"她接得上"：导入后下一轮请求里要出现旧系统那段里的某句话
// （增量导入会把该聊天还活着的段标成 needs_seed=1，网关下一轮重新构建前情，把刚导的原话带进去）。
import { afterEach, expect, test } from 'bun:test'
import { existsSync, mkdirSync, statSync, utimesSync, writeFileSync } from 'fs'
import { join } from 'path'
import { Database } from 'bun:sqlite'
import { FakeTelegram } from '../fakes/fake-telegram'
import { cleanup, Gateway, makeBot, OWNER, prompts, until, type BotEnv } from '../harness'

const REPO = join(import.meta.dir, '..', '..', '..')
const PY = process.env.DSH_BOT_PYTHON || (process.platform === 'win32' ? 'python' : 'python3')
let tg: FakeTelegram | null = null
let b: BotEnv | null = null
let gw: Gateway | null = null
let importOut = '' // 导入脚本原样的 stdout/stderr，等不到东西时跟着现场一起打出来（见 ledgerSituation）

/**
 * 诊断：把账本此刻的真实状态揉成一段话。这条用例在 CI 的 macOS 上连挂了两轮，都是"导入后读不到
 * 活跃段"，但日志里只剩一句超时，分不清是库根本打不开、还是库里没有活跃段。失败时打出来就能定性。
 * 顺序有讲究：先只读探测（不改文件状态），再从可写侧看段表；库不在就到此为止（可写打开会凭空建出空库）。
 * 只碰测试自己造的临时数据，不含密钥。
 */
function ledgerSituation(path: string): string {
  const out: string[] = []
  for (const suf of ['', '-wal', '-shm', '-journal']) {
    const f = path + suf
    try { out.push(`${suf || 'db'}=${existsSync(f) ? `${statSync(f).size}B` : '不在'}`) } catch { out.push(`${suf || 'db'}=读不到属性`) }
  }
  try {
    // 只构造不算数：bun:sqlite 的打开是惰性的，要真读一下才知道能不能读（CI 上那次就是构造没报错、读才报错）
    const ro = new Database(path, { readonly: true })
    try { ro.query('SELECT COUNT(*) AS n FROM segments').get() } finally { ro.close() }
    out.push('只读读=可以')
  } catch (e) {
    out.push(`只读读=失败（${String(e)}）`)
  }
  if (!existsSync(path)) return out.join('，')
  try {
    const db = new Database(path)
    try {
      out.push(`段按状态=${JSON.stringify(db.query("SELECT state, COUNT(*) AS n FROM segments GROUP BY state").all())}`)
      out.push(`本聊天各段=${JSON.stringify(db.query('SELECT id, state, needs_seed FROM segments WHERE chat_id = ? ORDER BY id').all(String(OWNER)))}`)
      out.push(`没收尾的轮=${JSON.stringify(db.query("SELECT id, state FROM turns WHERE state IN ('preparing', 'sent')").all())}`)
    } finally { db.close() }
  } catch (e) {
    out.push(`可写打开=失败（${String(e)}）`)
  }
  return out.join('，')
}

afterEach(async () => {
  await gw?.stop()
  tg?.stop()
  if (b) cleanup(b)
  tg = null; b = null; gw = null
})

/** 打开账本读一次；读不到就抛。bun:sqlite 的打开是惰性的——构造不报错、真正读的那一下才抛，
 *  所以调用方必须把"打开+查询"当成一整段去 catch。
 *  普通打开不能传空对象：bun 会把 flags 算成 0，报 SQLITE_MISUSE，得走默认参数的构造。 */
function readOnce(path: string, readonly: boolean, sql: string, args: (string | number)[]): unknown {
  const db = readonly ? new Database(path, { readonly: true }) : new Database(path)
  try {
    const row = db.query(sql).get(...(args as never[])) as Record<string, unknown> | null
    return row ? Object.values(row)[0] : null
  } finally { db.close() }
}

/**
 * 只读瞄一眼账本的一行。库还没建出来、schema 还没写全（网关刚起、migrate 还在跑）都当作
 * "还没就绪"返回 null —— 这个函数要能安全地放进 until(() => …) 当等待条件：
 * until 不会重试回调抛出的错，而 CI 的 macOS 上启动慢一拍，账本还没落盘时打开会抛
 * SQLiteError: unable to open database file（本机跑得快、账本早建好了，所以只在 CI 上现形）。
 * 真出了别的错照样抛，不吞。
 *
 * 只读读不了时先用普通打开兜底再定论，理由见下面 catch 里的注释。
 */
function peek(path: string, sql: string, ...args: (string | number)[]): unknown {
  if (!existsSync(path)) return null // 库还没建出来
  try {
    return readOnce(path, true, sql, args)
  } catch (e) {
    // 库的 WAL 处在半清理状态时（-wal 被清走、-shm 还留在盘上）只读会一直读不了，报
    // "unable to open database file"。本机复现过这个状态：只读连不上、普通打开照常读到数据、
    // 还顺手把缺的 -wal 补回来。CI 的 macOS 上"导入后读不到活跃段"就落在这个状态里：导入脚本
    // 刚关掉连接，后面每次只读都吃这个错，等多久都读不到（所以加长等待没用）。这里只是读，
    // 普通打开不写业务数据；库真不在了就别打开（那会凭空建出一个空库）。
    if (/unable to open|locked/i.test(String(e)) && existsSync(path)) {
      try {
        return readOnce(path, false, sql, args)
      } catch (e2) {
        if (/no such table|unable to open|locked/i.test(String(e2))) return null // 也当"还没就绪"，交给 until 重试
        throw e2
      }
    }
    // 网关刚建库、schema 还没写完的窗口：也当"还没就绪"；wait 的事交给调用方的 until
    if (/no such table|unable to open|locked/i.test(String(e))) return null
    throw e
  }
}

/**
 * 等账本"读得到"再返回读到的值。peek 在读不到时返回 null，而 Number(null) 是 0 ——
 * 直接把 Number(peek(...)) 拿去断言，会把"没读到"混成合法值 0（或者用 toBe(0) 时假过）。
 * 这里把 null 留给 until 重试（网关刚起、账本/schema 还没落盘的头几拍，见 peek 的注释），
 * 等到非 null 就把值交回，值对不对仍由调用方照常断言——错了照样报实际值。
 * 值包一层对象再回：until 把 falsy 当"还没等到"，读到的值本身可能就是 0。
 */
async function peekWhenReady(what: string, path: string, sql: string, ...args: (string | number)[]): Promise<unknown> {
  try {
    const got = await until(() => { const v = peek(path, sql, ...args); return v === null ? null : { v } }, what)
    return got.v
  } catch (e) {
    // 等不到就把现场交出来：CI 的 macOS 上这条路径连挂两轮，日志里只有一句"超时"，看不出库是什么状态
    console.error(`[诊断] 等不到「${what}」，账本现场：${ledgerSituation(path)}`)
    if (importOut) console.error(`[诊断] import_history.py 输出：\n${importOut}`)
    throw e
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
  // 等"活跃段可读"再断言：活跃段是第一轮正常收尾的证据，也是导入脚本后面要标"重新取前情"的那一段，
  // 它不在（比如崩溃后段被作废）要在这里就显形，别让它到导入之后才以"导入没标上"的样子出现。
  // 读到非 null 就断言值：needs_seed 应该已经用掉（0）——用 Number(null)=0 直接断言会把"没读到"假过掉。
  const seeded = await peekWhenReady('第一轮收尾、活跃段可读', ledger, "SELECT needs_seed FROM segments WHERE chat_id = ? AND state = 'active'", String(OWNER))
  expect(Number(seeded)).toBe(0)

  // 2) 停网关（真机上是先切回旧系统；这里直接模拟"切走"）
  await gw.stop()
  gw = null
  // 先悄悄看一眼：要是"网关停止"这一步把活跃段带走了，这里就留下现场（不判失败，判的还是导入之后那次）
  if (peek(ledger, "SELECT state FROM segments WHERE chat_id = ? AND state = 'active'", String(OWNER)) === null) {
    console.error(`[诊断] 网关刚停，活跃段就查不到了：${ledgerSituation(ledger)}`)
  }
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
  // 水位线也等"读得到"再算：一次性读碰上涨 null 会误报成"算不出水位线"
  const wm = Number(await peekWhenReady('水位线可读', ledger, 'SELECT MAX(t) AS t FROM (SELECT MAX(ts) AS t FROM inbound WHERE chat_id = ? UNION ALL SELECT MAX(COALESCE(sent_at, created_at)) AS t FROM outbound WHERE chat_id = ?)', String(OWNER), String(OWNER)))
  expect(wm).toBeGreaterThan(0)
  const t = (ms: number) => new Date(ms).toISOString()
  writeFileSync(join(proj, 's.jsonl'), [
    { type: 'user', timestamp: t(wm + 60_000), message: { content: `<channel source="telegram" chat_id="${OWNER}">\n旧系统里补的一句关键话\n</channel>` } },
    { type: 'assistant', timestamp: t(wm + 120_000), message: { content: [{ type: 'tool_use', name: 'mcp__telegram-worker__reply', input: { text: '补料那几天的回复' } }] } },
  ].map(l => JSON.stringify(l)).join('\n') + '\n')

  const r = Bun.spawnSync([PY, join(REPO, 'scripts', 'import_history.py'), bot.name, '--from', chan, '--since', 'auto'], { cwd: REPO, env })
  // 脚本输出整份留着：等不到东西时（peekWhenReady 超时）会跟着现场一起打出来。末尾那句能定性——
  // "还活着的会话已标成…"＝标记时确实有活跃段；"下一轮开新会话时…"＝标记时就没有活跃段可标了。
  importOut = `exit=${r.exitCode}\n--- stdout ---\n${r.stdout.toString()}--- stderr ---\n${r.stderr.toString()}`
  if (r.exitCode !== 0 || !r.stdout.toString().includes('增量导入')) console.error(`[诊断] 导入脚本不对劲：\n${importOut}`)
  expect(r.exitCode).toBe(0)
  expect(r.stdout.toString()).toContain('增量导入')
  // 导入的东西必须被"标成重新取前情"，否则记录只是躺在账本里。等标记真的可见再断言值：
  // peek 读不到时返回 null，Number(null) 是 0，一次性读会把它误判成"没标上"。
  // CI 的 macOS 上这里连挂两轮，真因是 WAL 半清理状态下只读读不了（见 peek 的注释，
  // peek 现在会换普通打开兜底）；真等不到时下面还会把现场打出来。
  const marked = await peekWhenReady('导入后活跃段可读', ledger, "SELECT needs_seed FROM segments WHERE chat_id = ? AND state = 'active'", String(OWNER))
  expect(Number(marked)).toBe(1)

  // 4) 起回来，说下一句：这一轮请求里要能看到旧系统那段里的话
  gw = new Gateway(bot)
  await gw.start()
  tg.pushText(OWNER, '接着聊昨天的事')
  await until(() => prompts(bot).some(p => p.text.includes('接着聊昨天的事')), '第二轮的请求')
  const second = prompts(bot).find(p => p.text.includes('接着聊昨天的事'))!.text
  expect(second).toContain('旧系统里补的一句关键话')
  expect(second).toContain('补料那几天的回复')
  // 用掉之后就归零，不会每轮都重发一遍前情（同样等"读得到"再断言值）
  const used = await peekWhenReady('前情取用后段可读', ledger, "SELECT needs_seed FROM segments WHERE chat_id = ? AND state = 'active'", String(OWNER))
  expect(Number(used)).toBe(0)
})
