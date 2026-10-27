// dsh-group-speak 验收测试（黑盒，依据 .devflow/dsh-group-speak-接口.md 第 3 节的 15 条行为清单）。
// 只通过对外接口：配置文件起网关子进程、假 Telegram 收发、本机 /v1/inject、读写群记录文件。
// 注释里的「清单 N」对应接口文件第 3 节的编号；清单 11 含两个场景，拆成 11a / 11b 两条测试。
import { afterEach, expect, test } from 'bun:test'
import { existsSync, mkdirSync, readFileSync, rmSync, statSync, utimesSync, writeFileSync } from 'fs'
import { join } from 'path'
import { FakeTelegram } from '../fakes/fake-telegram'
import { Gateway, IS_WIN, makeBot, OWNER, prompts, readJsonl, sleep, until, warmUp, type BotEnv } from './_acc'

const GROUP = -1001777
const A_NAME = 'bot_a_dsh_bot'

let tgs: FakeTelegram[] = []
let bots: BotEnv[] = []
let gws: Gateway[] = []

afterEach(async () => {
  for (const g of gws) await g.stop().catch(() => {})
  for (const t of tgs) t.stop()
  if (bots[0]) { try { rmSync(bots[0].root, { recursive: true, force: true }) } catch {} }
  tgs = []; bots = []; gws = []
})

/** 起 1 或 2 只 bot（同一个群、同一个根目录）。gw 是写进 gateway: 下的额外配置 */
async function setup(o: { n?: 1 | 2; gw?: Record<string, unknown> } = {}) {
  const ta = new FakeTelegram({ botId: 900001, username: A_NAME })
  tgs.push(ta)
  const access = { groups: { [String(GROUP)]: { allowFrom: [] } } }
  const a = makeBot(ta, { name: 'a', gw: { burst_window_ms: 0, ...o.gw }, access })
  bots.push(a)
  let tb: FakeTelegram | undefined
  if ((o.n ?? 1) === 2) {
    tb = new FakeTelegram({ botId: 900002, username: 'bot_b_dsh_bot' })
    tgs.push(tb)
    bots.push(makeBot(tb, { name: 'b', root: a.root, gw: { burst_window_ms: 0, ...o.gw }, access }))
  }
  for (const b of bots) { const g = new Gateway(b); gws.push(g); await g.start() }
  return { ta, tb, a, ga: gws[0]! }
}

const groupsDir = (b: BotEnv) => join(b.root, 'groups')
const tfile = (b: BotEnv) => join(groupsDir(b), `${GROUP}.jsonl`)
const rows = (b: BotEnv) => readJsonl<Record<string, any>>(tfile(b))
const botRows = (b: BotEnv) => rows(b).filter(r => r.is_bot === true)

/** 轮询到条件成立或超时，返回布尔（让失败落在 expect 断言上，而不是超时异常） */
async function waitTrue(fn: () => boolean, ms = 6_000): Promise<boolean> {
  const end = Date.now() + ms
  while (Date.now() < end) { if (fn()) return true; await sleep(50) }
  return fn()
}

let nextMid = 5000
/** 群里真人说一句：所有 bot 都收到（消息编号相同） */
function groupSay(text: string): number {
  const mid = nextMid++
  for (const t of tgs) t.pushText(OWNER, text, { chatId: GROUP, messageId: mid })
  return mid
}

/** 导演点名：POST /v1/inject */
const inject = (g: Gateway, text: string, o: { source?: string; chat?: string } = {}) =>
  g.call('/v1/inject', { chat_id: o.chat ?? String(GROUP), text, source: o.source ?? 'director', key: `k-${crypto.randomUUID()}` })
const directorSay = (g: Gateway, tail: string) => inject(g, `⟦群聊·导演点到你了⟧ 群里最近的对话：大家好 ${tail}`)

/** 往群记录文件里预置 n 行旧记录（总大小一定超过 2000 字节），返回文件原样内容 */
function seedBig(b: BotEnv, tag: string, n = 12): string {
  mkdirSync(groupsDir(b), { recursive: true })
  let s = ''
  for (let i = 0; i < n; i++) {
    s += JSON.stringify({ ts: Date.now() - 100_000 + i, chat_id: String(GROUP), message_id: 100 + i, from_id: String(OWNER), from_username: '', from_name: 'x', is_bot: false, text: `${tag}-${i}-${'x'.repeat(100)}`, observed_by: 'a' }) + '\n'
  }
  expect(Buffer.byteLength(s)).toBeGreaterThan(2000)
  writeFileSync(tfile(b), s)
  return s
}

const readText = (f: string) => readFileSync(f, 'utf8')
const lines = (f: string) => readText(f).split('\n').filter(Boolean)

// ───────── 清单 1、2：记录放哪 ─────────

test('清单1 不配 groups_dir：真人群消息写进 HOME 下缺省目录，目录 0700、文件 0600', async () => {
  const { a } = await setup({ gw: { groups_dir: undefined } })
  groupSay('缺省目录这一句')
  const dir = join(a.fakeHome, '.config', 'claude-dsh-console', 'group_transcripts')
  const file = join(dir, `${GROUP}.jsonl`)
  expect(await waitTrue(() => existsSync(file) && lines(file).length >= 1)).toBe(true)
  const got = readJsonl<Record<string, any>>(file)
  expect(got).toHaveLength(1)
  expect(got[0]).toMatchObject({ is_bot: false, from_id: String(OWNER), text: '缺省目录这一句' })
  if (!IS_WIN) {
    expect(statSync(dir).mode & 0o777).toBe(0o700)
    expect(statSync(file).mode & 0o777).toBe(0o600)
  }
})

test('清单2 配了 groups_dir：写进该目录，缺省目录不存在', async () => {
  const { a } = await setup()
  groupSay('配置目录这一句')
  expect(await waitTrue(() => existsSync(tfile(a)) && rows(a).length >= 1)).toBe(true)
  expect(rows(a)[0]).toMatchObject({ is_bot: false, text: '配置目录这一句' })
  await sleep(300)
  expect(existsSync(join(a.fakeHome, '.config', 'claude-dsh-console', 'group_transcripts'))).toBe(false)
})

// ───────── 清单 3：去重 ─────────

test('清单3 真人一句话两只 bot 都收到：v1.4 起每只各写一行（不再抢 .seen），读侧负责去重', async () => {
  const { a } = await setup({ n: 2 })
  groupSay('大家晚上好')
  expect(await waitTrue(() => existsSync(tfile(a)) && rows(a).length >= 2)).toBe(true)
  await sleep(700)
  const got = rows(a)
  // v1.3 是 .seen 抢锁，只留 1 行；v1.4 约定第 22 节：普通群 message_id 每个 bot 视角不同，
  // 抢锁挡不住跨侧重复，改成每只都写、读侧按 chat_id + ts + from_id 去重。
  expect(got).toHaveLength(2)
  for (const r of got) expect(r).toMatchObject({ is_bot: false, from_id: String(OWNER), text: '大家晚上好' })
  expect(new Set(got.map(r => r.observed_by)).size).toBe(2)   // 两个不同写方
})

// ───────── 清单 4、5、6、7、8：bot 的话怎么记 ─────────

test('清单4 一次回复拆 3 条群消息：群记录只多 1 行，text 为三段以换行连接，message_id 是第一条的编号；第二轮再多 1 行', async () => {
  const { ta, a, ga } = await setup()
  const r1 = await directorSay(ga, '!say:你好 !parts:3')
  expect(r1.json?.ok).toBe(true)
  expect(await waitTrue(() => ta.sentTo(GROUP).length >= 3)).toBe(true)
  expect(await waitTrue(() => botRows(a).length >= 1)).toBe(true)
  await sleep(600)
  const sent = ta.sentTo(GROUP)
  expect(sent.map(s => s.text)).toEqual(['你好（第1段）', '你好（第2段）', '你好（第3段）'])
  expect(botRows(a)).toHaveLength(1)
  const row = botRows(a)[0]!
  expect(row.text).toBe('你好（第1段）\n你好（第2段）\n你好（第3段）')
  expect(String(row.message_id)).toBe(String(sent[0]!.messageId))
  expect(row.from_username).toBe(A_NAME)
  expect(row.is_bot).toBe(true)

  // 第二轮
  const r2 = await directorSay(ga, '!say:再来 !parts:3')
  expect(r2.json?.ok).toBe(true)
  expect(await waitTrue(() => botRows(a).length >= 2)).toBe(true)
  await sleep(600)
  expect(botRows(a)).toHaveLength(2)
  expect(botRows(a)[1]!.text).toBe('再来（第1段）\n再来（第2段）\n再来（第3段）')
})

test('清单5 一次回复是文字加图片：群记录 1 行，text 为「文字\\n[图片]」', async () => {
  const { ta, a, ga } = await setup()
  const img = join(a.botDir, 'media', 'pic.png')
  writeFileSync(img, 'not really a png')
  await directorSay(ga, `!say:图文说明 !file:${img}`)
  expect(await waitTrue(() => ta.sentTo(GROUP).some(s => s.method === 'sendPhoto'))).toBe(true)
  expect(await waitTrue(() => botRows(a).length >= 1)).toBe(true)
  await sleep(600)
  expect(ta.sentTo(GROUP).map(s => s.method)).toEqual(['sendMessage', 'sendPhoto'])
  expect(botRows(a)).toHaveLength(1)
  expect(botRows(a)[0]!.text).toBe('图文说明\n[图片]')
})

test('清单6 一次回复合计超过 1000 字符：群记录 text 恰好 1000 字符', async () => {
  const { ta, a, ga } = await setup()
  const base = '字'.repeat(400)
  await directorSay(ga, `!say:${base} !parts:3`)
  expect(await waitTrue(() => ta.sentTo(GROUP).length >= 3)).toBe(true)
  expect(await waitTrue(() => botRows(a).length >= 1)).toBe(true)
  await sleep(600)
  const joined = [1, 2, 3].map(i => `${base}（第${i}段）`).join('\n')
  expect(joined.length).toBeGreaterThan(1000)
  expect(botRows(a)).toHaveLength(1)
  expect(botRows(a)[0]!.text).toHaveLength(1000)
  expect(botRows(a)[0]!.text).toBe(joined.slice(0, 1000))
})

test('清单7 主人私聊 A 回复分 2 段：群记录不新增任何行', async () => {
  const { ta, a } = await setup()
  groupSay('先有一句群里的话')
  expect(await waitTrue(() => existsSync(tfile(a)) && rows(a).length >= 1)).toBe(true)
  const before = readText(tfile(a))
  ta.pushText(OWNER, '私聊两段 !parts:2')
  expect(await waitTrue(() => ta.sentTo(OWNER).filter(s => (s.text ?? '').startsWith('收到：私聊两段')).length >= 2)).toBe(true)
  await sleep(600)
  expect(readText(tfile(a))).toBe(before)
  expect(botRows(a)).toHaveLength(0)
})

test('清单8 3 段里第 1 段发送失败：群记录 1 行，text 只含发出的 2 段，message_id 为第 2 段的编号', async () => {
  const { ta, a, ga } = await setup()
  ta.faults.push({ method: 'sendMessage', times: 10, status: 400, description: 'Bad Request: injected fault', when: p => String(p.chat_id) === String(GROUP) && String(p.text ?? '').includes('（第1段）') })
  await directorSay(ga, '!say:部分 !parts:3')
  expect(await waitTrue(() => ta.sentTo(GROUP).length >= 2)).toBe(true)
  expect(await waitTrue(() => botRows(a).length >= 1)).toBe(true)
  await sleep(600)
  const sent = ta.sentTo(GROUP)
  expect(sent.map(s => s.text)).toEqual(['部分（第2段）', '部分（第3段）'])
  expect(botRows(a)).toHaveLength(1)
  expect(botRows(a)[0]!.text).toBe('部分（第2段）\n部分（第3段）')
  expect(String(botRows(a)[0]!.message_id)).toBe(String(sent[0]!.messageId))
})

// ───────── 清单 9、10、11：轮转 ─────────

test('清单9 超过阈值后再来一条：旧内容改名 .jsonl.1，新文件只有新一行，锁文件不残留', async () => {
  const { a } = await setup({ gw: { groups_rotate_bytes: 2000 } })
  const old = seedBig(a, 'old')
  groupSay('轮转后的新一句')
  expect(await waitTrue(() => existsSync(`${tfile(a)}.1`))).toBe(true)
  expect(await waitTrue(() => existsSync(tfile(a)) && lines(tfile(a)).length >= 1)).toBe(true)
  await sleep(300)
  expect(readText(`${tfile(a)}.1`)).toBe(old)
  const cur = rows(a)
  expect(cur).toHaveLength(1)
  expect(cur[0]).toMatchObject({ is_bot: false, text: '轮转后的新一句' })
  expect(existsSync(join(groupsDir(a), `.rotate-${GROUP}`))).toBe(false)
})

test('清单10 已有 .1 再次轮转：.1 换成上一份文件内容，没有 .2', async () => {
  const { a } = await setup({ gw: { groups_rotate_bytes: 2000 } })
  const old = seedBig(a, 'second')
  writeFileSync(`${tfile(a)}.1`, '{"text":"更早的旧内容"}\n')
  groupSay('第二次轮转的新一句')
  expect(await waitTrue(() => existsSync(tfile(a)) && lines(tfile(a)).length === 1 && rows(a)[0]?.text === '第二次轮转的新一句')).toBe(true)
  await sleep(300)
  expect(readText(`${tfile(a)}.1`)).toBe(old)
  expect(existsSync(`${tfile(a)}.2`)).toBe(false)
})

test('清单11a 超过阈值但已有刚建的锁文件：不轮转，新行追加在原文件末尾，锁文件仍在', async () => {
  const { a } = await setup({ gw: { groups_rotate_bytes: 2000 } })
  const old = seedBig(a, 'locked')
  const lock = join(groupsDir(a), `.rotate-${GROUP}`)
  writeFileSync(lock, '')
  groupSay('被锁时的新一句')
  expect(await waitTrue(() => readText(tfile(a)).includes('被锁时的新一句'))).toBe(true)
  await sleep(300)
  const content = readText(tfile(a))
  expect(content.startsWith(old)).toBe(true)
  expect(lines(tfile(a))).toHaveLength(13)
  expect(JSON.parse(lines(tfile(a))[12]!).text).toBe('被锁时的新一句')
  expect(existsSync(`${tfile(a)}.1`)).toBe(false)
  expect(existsSync(lock)).toBe(true)
})

test('清单11b 超过阈值且锁文件已是 2 分钟前的残留：照常轮转', async () => {
  const { a } = await setup({ gw: { groups_rotate_bytes: 2000 } })
  const old = seedBig(a, 'stale')
  const lock = join(groupsDir(a), `.rotate-${GROUP}`)
  writeFileSync(lock, '')
  const past = new Date(Date.now() - 120_000)
  utimesSync(lock, past, past)
  groupSay('残留锁时的新一句')
  expect(await waitTrue(() => existsSync(`${tfile(a)}.1`))).toBe(true)
  expect(await waitTrue(() => existsSync(tfile(a)) && lines(tfile(a)).length >= 1)).toBe(true)
  expect(readText(`${tfile(a)}.1`)).toBe(old)
  expect(rows(a)).toHaveLength(1)
  expect(rows(a)[0]).toMatchObject({ text: '残留锁时的新一句' })
})

// ───────── 清单 12、13：导演注入过期 ─────────

test('清单12 第二次点名在 A 忙第一轮时过期：不说话、不记行、回包仍 ok；之后新点名正常说话', async () => {
  const { ta, a, ga } = await setup({ gw: { group_turn_ttl_ms: 500 } })
  await warmUp(ta) // 先把 dsh 拉起来，避免启动耗时算进排队时间
  const r1 = await directorSay(ga, '!slow:2500 !say:第一轮')
  expect(r1.json?.ok).toBe(true)
  const r2 = await directorSay(ga, '!say:第二轮')
  expect(r2.status).toBe(200)
  expect(r2.json?.ok).toBe(true)
  expect(await waitTrue(() => ta.sentTo(GROUP).some(s => s.text === '第一轮'), 15_000)).toBe(true)
  await sleep(1000)
  expect(ta.sentTo(GROUP).map(s => s.text)).toEqual(['第一轮'])
  expect(botRows(a).map(r => r.text)).toEqual(['第一轮'])

  const r3 = await directorSay(ga, '!say:第三轮')
  expect(r3.json?.ok).toBe(true)
  expect(await waitTrue(() => ta.sentTo(GROUP).some(s => s.text === '第三轮'), 15_000)).toBe(true)
  expect(await waitTrue(() => botRows(a).some(r => r.text === '第三轮'))).toBe(true)
  expect(ta.sentTo(GROUP).map(s => s.text)).toEqual(['第一轮', '第三轮'])
})

test('清单13 私聊里非导演来源的注入排队超过 ttl：照常处理、照常回复', async () => {
  const { ta, ga } = await setup({ gw: { group_turn_ttl_ms: 500 } })
  ta.pushText(OWNER, '先聊慢的 !slow:2500')
  await until(() => prompts(bots[0]!).some(p => p.text.includes('先聊慢的')), '慢的那一轮已开始')
  const r = await inject(ga, '⟦用户时刻⟧ 过了一会儿 !say:时刻回应', { source: 'user_moment', chat: String(OWNER) })
  expect(r.status).toBe(200)
  expect(r.json?.ok).toBe(true)
  expect(await waitTrue(() => ta.sentTo(OWNER).some(s => s.text === '时刻回应'), 15_000)).toBe(true)
})

// ───────── 清单 14、15：私聊带群里近况 ─────────

test('清单14 主人私聊 A：提示里带群里近况（真人话记作「对方」）', async () => {
  const { ta, a } = await setup()
  groupSay('群里的一句话')
  expect(await waitTrue(() => existsSync(tfile(a)) && rows(a).length >= 1)).toBe(true)
  ta.pushText(OWNER, '在吗')
  expect(await waitTrue(() => prompts(a).some(p => p.text.includes('在吗')))).toBe(true)
  const p = prompts(a).find(p => p.text.includes('在吗'))!.text
  expect(p).toContain('⟦群里近况')
  expect(p).toContain('对方：群里的一句话')
})

test('清单15 群记录刚轮转过：私聊提示里的群里近况包含 .1 里的那句', async () => {
  const { ta, a } = await setup({ gw: { groups_rotate_bytes: 800 } })
  groupSay(`旧文件里的那句。${'填'.repeat(300)}`) // 这一行本身就超过阈值
  expect(await waitTrue(() => existsSync(tfile(a)) && rows(a).length >= 1)).toBe(true)
  groupSay('新文件里的别的一句')
  expect(await waitTrue(() => existsSync(`${tfile(a)}.1`))).toBe(true)
  expect(await waitTrue(() => existsSync(tfile(a)) && lines(tfile(a)).length >= 1)).toBe(true)
  expect(rows(a).map(r => r.text)).toEqual(['新文件里的别的一句'])
  expect(readText(`${tfile(a)}.1`)).toContain('旧文件里的那句')

  ta.pushText(OWNER, '在吗')
  expect(await waitTrue(() => prompts(a).some(p => p.text.includes('在吗')))).toBe(true)
  const p = prompts(a).find(p => p.text.includes('在吗'))!.text
  expect(p).toContain('⟦群里近况')
  expect(p).toContain('旧文件里的那句')
  expect(p).toContain('新文件里的别的一句')
})

test('清单16 同一条群消息两行（本 bot 观察行 + 对端 bot 补写的带图行）：近况里只说一次', async () => {
  const { ta, a } = await setup()
  const base = {
    ts: new Date().toISOString(), chat_id: String(GROUP), message_id: 90001, from_id: String(OWNER),
    from_username: 'someone', from_name: '某人', is_bot: false, text: '同一条消息只说一次',
  }
  mkdirSync(groupsDir(a), { recursive: true })
  writeFileSync(tfile(a),
    JSON.stringify({ ...base, observed_by: A_NAME }) + '\n'
    + JSON.stringify({ ...base, observed_by: 'tuniang_bot', attachment_kind: 'photo', image_path: '/tmp/x.jpg' }) + '\n')
  ta.pushText(OWNER, '在吗')
  expect(await waitTrue(() => prompts(a).some(p => p.text.includes('在吗')))).toBe(true)
  const p = prompts(a).find(p => p.text.includes('在吗'))!.text
  expect(p).toContain('⟦群里近况')
  // 两行 observed_by 不同，since 会保留两行；近况里同一句只能出现一次
  expect(p.split('同一条消息只说一次').length - 1).toBe(1)
})

test('清单17 私聊近况里带群图片占位并写明看不到内容（防止模型凭空编图）', async () => {
  const { ta, a } = await setup()
  const now = new Date().toISOString()
  mkdirSync(groupsDir(a), { recursive: true })
  // 只有带图行、没有文字行（本 bot 没抢到 observe，或对端补写的空 text 行）
  writeFileSync(tfile(a), JSON.stringify({
    ts: now, chat_id: String(GROUP), message_id: 70002, from_id: String(OWNER), from_username: 'someone',
    from_name: '某人', is_bot: false, text: '', observed_by: A_NAME, attachment_kind: 'photo', image_path: '/tmp/x.jpg',
  }) + '\n')
  ta.pushText(OWNER, '在吗')
  expect(await waitTrue(() => prompts(a).some(p => p.text.includes('在吗')))).toBe(true)
  const p = prompts(a).find(p => p.text.includes('在吗'))!.text
  expect(p).toContain('⟦群里近况')
  expect(p).toContain('[发了一张图片，这里看不到内容]')
})
