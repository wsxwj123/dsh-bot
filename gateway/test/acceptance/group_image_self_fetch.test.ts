// group-image-self-fetch 验收测试（黑盒，依据 .devflow/group-image-self-fetch.md 的「方案修订版」与「复审结论与最后三点」）。
// 只通过对外接口：配置文件起网关子进程、假 Telegram 发图与故障注入、本机 /v1/inject、
// 读群记录 JSONL、gateway.log、假 dsh 的 prompts.jsonl（看模型这一轮收到几个图片块）。
// 用例编号对应任务里的 14 条行为；实现还没落地，这些用例现在应当是红的（预期）。
// 注意：第 4 条用测试内的转发代理让 /file/ 下载返回超大内容；第 10 条用假 Telegram 的 slow 故障拖慢 getFile。
import { afterEach, expect, test } from 'bun:test'
import { appendFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, realpathSync, rmSync, statSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { dirname, join, relative } from 'path'
import { FakeTelegram } from '../fakes/fake-telegram'
import { FRIEND, Gateway, IS_WIN, logEvents, makeBot, OWNER, prompts, readJsonl, sleep, until, type BotEnv } from './_acc'

// 读图链路会校验图片头，夹具必须是真 PNG：1×1 RGBA
const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
  'base64',
)

const GROUP = -1001777
const A_NAME = 'bot_a_dsh_bot'
const MiB = 1024 * 1024

let tgs: FakeTelegram[] = []
let bots: BotEnv[] = []
let gws: Gateway[] = []
let roots: string[] = []
let stops: (() => void)[] = []
let nextSeedId = 5000

afterEach(async () => {
  for (const g of gws) await g.stop().catch(() => {})
  for (const f of stops) { try { f() } catch {} }
  for (const r of roots) { try { rmSync(r, { recursive: true, force: true }) } catch {} }
  tgs = []; bots = []; gws = []; roots = []; stops = []
})

type Dirs = { root: string; shared: string; media: string; groups: string }
type Env = { tg: FakeTelegram; b: BotEnv; gw: Gateway; root: string; shared: string; media: string; groups: string }

/**
 * 起一只 bot：注入临时的 sharedMediaDir / claudeMediaRoot / groups_dir。
 * proxy 给了就用它当 telegram_api（测试内的转发代理，用来篡改 /file/ 下载内容）。
 */
async function setup(o: { gw?: Record<string, unknown>; prepare?: (d: Dirs) => void; proxy?: (tgUrl: string) => string } = {}): Promise<Env> {
  const root = mkdtempSync(join(tmpdir(), 'dshselfimg-'))
  const shared = join(root, 'shared-media')
  const media = join(root, 'claude-media')
  const groups = join(root, 'groups')
  mkdirSync(shared, { recursive: true })
  mkdirSync(media, { recursive: true })
  const tg = new FakeTelegram({ botId: 900001, username: A_NAME })
  const apiUrl = o.proxy ? o.proxy(tg.url) : tg.url
  const access = { groups: { [String(GROUP)]: { allowFrom: [] } } }
  const b = makeBot(tg, {
    name: 'a', root, access,
    gw: {
      burst_window_ms: 0,
      groups_dir: groups,
      sharedMediaDir: shared,
      claudeMediaRoot: media,
      image_dirs: [shared],
      telegram_api: apiUrl,
      ...o.gw,
    },
  })
  o.prepare?.({ root, shared, media, groups })
  const gw = new Gateway(b)
  tgs.push(tg); bots.push(b); gws.push(gw); roots.push(root)
  await gw.start()
  return { tg, b, gw, root, shared, media, groups }
}

/** 测试内的 Telegram 转发代理：除 /file/ 外原样转发给假 Telegram，/file/ 由 fileReply 决定 */
function fileProxy(tgUrl: string, fileReply: () => Response): string {
  const server = Bun.serve({
    hostname: '127.0.0.1',
    port: 0,
    async fetch(req) {
      const u = new URL(req.url)
      if (u.pathname.startsWith('/file/')) return fileReply()
      const headers = new Headers(req.headers)
      headers.delete('host')
      headers.delete('content-length')
      const init: RequestInit = { method: req.method, headers }
      if (req.method !== 'GET' && req.method !== 'HEAD') init.body = await req.arrayBuffer()
      return fetch(`${tgUrl}${u.pathname}${u.search}`, init)
    },
  })
  stops.push(() => server.stop(true))
  return `http://127.0.0.1:${server.port}`
}

const groupsDirOf = (b: BotEnv) => String(b.gw.groups_dir)
const rowsOf = (dir: string, chatId: string | number) => readJsonl<Record<string, any>>(join(dir, `${chatId}.jsonl`))
const rows = (b: BotEnv) => rowsOf(groupsDirOf(b), GROUP)

/** 落盘路径（方案修订版 致命 1）：<botDir>/group-media/telegram-group/<chat_id>/<message_id>.jpg */
const photoPath = (b: BotEnv, messageId: number) =>
  join(b.botDir, 'group-media', 'telegram-group', String(GROUP), `${messageId}.jpg`)

/** 往群记录里预置一行（字段形状按约定 5.12.4） */
function seed(b: BotEnv, o: Record<string, any>): void {
  const dir = groupsDirOf(b)
  mkdirSync(dir, { recursive: true })
  const base = {
    ts: Date.now(), chat_id: String(GROUP), message_id: nextSeedId++,
    from_id: String(FRIEND), from_username: 'friend_user', from_name: 'friend', is_bot: false, observed_by: 'a',
  }
  appendFileSync(join(dir, `${GROUP}.jsonl`), JSON.stringify({ ...base, ...o }) + '\n')
}

type P = { sessionId: string; text: string; images: number; at: number }
const promptWith = (b: BotEnv, marker: string) =>
  (prompts(b) as unknown as P[]).filter(p => p.text.includes(marker)).at(-1)

/** 等这条消息的自取图行出现 */
const waitPhotoRow = (b: BotEnv, messageId: number) =>
  until(() => rows(b).find(r => Number(r.message_id) === messageId && typeof r.image_path === 'string' && r.image_path),
    `群记录里 message_id=${messageId} 的带图行`, 20_000)

/** 导演点名，触发一次群回合；等这只 bot 在群里把 marker 发出来，返回模型这一轮的 prompt 记录 */
async function groupTurn(c: { tg: FakeTelegram; b: BotEnv; gw: Gateway }, marker: string, extra = ''): Promise<P> {
  const r = await c.gw.call('/v1/inject', {
    chat_id: String(GROUP), source: 'director', key: `k-${crypto.randomUUID()}`,
    text: `⟦群聊·导演点到你了⟧ 群里最近的对话：${marker} !say:${marker}${extra}`,
  })
  expect(r.status).toBe(200)
  expect(r.json?.ok).toBe(true)
  await until(() => c.tg.sentTo(GROUP).some(s => (s.text ?? '').includes(marker)), `群里回复「${marker}」`, 20_000)
  return (await until(() => promptWith(c.b, marker), '模型收到的这一轮提示', 10_000))!
}

// ───────── 写：自下载落盘与追加图行 ─────────

test('群图自取1 群里真人发图：v1.4 起一条消息一行（图和消息同一行，不再有图行）', async () => {
  const { tg, b } = await setup()
  const mid = tg.pushPhoto(FRIEND, '群图说明', { chatId: GROUP })
  const row = await waitPhotoRow(b, mid)
  // 只有一行：带 text、带 image_path
  expect(rows(b).filter(r => Number(r.message_id) === mid)).toHaveLength(1)
  expect(row.attachment_kind).toBe('photo')
  expect(typeof row.image_path).toBe('string')
  expect(String(row.text ?? '')).toContain('群图说明')
  expect(row.observed_by).toBe(A_NAME)
  expect(String(row.chat_id)).toBe(String(GROUP))
  expect(String(row.from_id)).toBe(String(FRIEND))
  expect(row.is_bot).toBe(false)
  // ts 用消息自带 date（秒级），不是下载完成时刻（v1.4 第 22 节）
  expect(String(row.ts)).toMatch(/\.000Z$/)
})

test('群图自取2 自取图生效：导演下一轮点名时模型收到 1 个图片块', async () => {
  const { tg, b, gw } = await setup()
  const mid = tg.pushPhoto(FRIEND, '自取图', { chatId: GROUP })
  await waitPhotoRow(b, mid)
  const p = await groupTurn({ tg, b, gw }, '轮二')
  expect(p.images).toBe(1)
})

test('群图自取3 getFile 下载失败：只留无图行，不报错、不阻断该轮', async () => {
  const { tg, b, gw } = await setup()
  tg.faults.push({ method: 'getFile', status: 500, description: 'Internal Server Error', times: 1 })
  const mid = tg.pushPhoto(FRIEND, '下载会失败', { chatId: GROUP })
  await until(() => rows(b).some(r => Number(r.message_id) === mid), '群记录里的无图行', 10_000)
  // 确实发起了下载（否则"没有图行"是假绿）：选的应是最大那张
  await until(() => tg.calls.some(c => c.method === 'getFile' && c.params.file_id === 'photo-big'),
    '下载被尝试（getFile photo-big）', 10_000)
  await sleep(1500)
  const rs = rows(b).filter(r => Number(r.message_id) === mid)
  expect(rs).toHaveLength(1)
  expect(rs[0].image_path).toBeUndefined()
  expect(existsSync(photoPath(b, mid))).toBe(false)
  // 该轮照常：导演点名能收到回复，且没有图可附
  const p = await groupTurn({ tg, b, gw }, '轮三')
  expect(p.images).toBe(0)
  expect(tg.sentTo(GROUP).some(s => (s.text ?? '').includes('轮三'))).toBe(true)
})

test('群图自取4 下载内容超过 8 MiB：不写图行，只留无图行', async () => {
  const { tg, b } = await setup({ proxy: tgUrl => fileProxy(tgUrl, () => new Response(new Uint8Array(8 * MiB + 1))) })
  const mid = tg.pushPhoto(FRIEND, '超大的图', { chatId: GROUP })
  const ev = await until(() => logEvents(b).find(e => e.event === 'group_photo.failed' && Number(e.message_id) === mid),
    '图过大的失败日志', 20_000)
  expect(ev.reason).toBe('too_large')
  await sleep(300)
  const rs = rows(b).filter(r => Number(r.message_id) === mid)
  expect(rs).toHaveLength(1)
  expect(rs[0].image_path).toBeUndefined()
  expect(existsSync(photoPath(b, mid))).toBe(false)
})

test('群图自取5 photo 数组多张：挑 width*height 最大的那张去 getFile', async () => {
  const { tg, b } = await setup()
  // 最大的一张在中间；数组最后一张宽度更大但面积更小（不按数组顺序取最后/第一）
  const mid = (tg as any).pushMedia(FRIEND, {
    photo: [
      { file_id: 'thumb-small', width: 90, height: 90 },
      { file_id: 'big-area', width: 800, height: 600 },
      { file_id: 'wide-short', width: 1200, height: 100 },
    ],
    caption: '多图挑最大',
  }, GROUP) as number
  const row = await waitPhotoRow(b, mid)
  const fileIds = tg.calls.filter(c => c.method === 'getFile').map(c => c.params.file_id)
  expect(fileIds).toEqual(['big-area'])
  expect(String(row.image_path).endsWith(`${mid}.jpg`)).toBe(true)
})

test('群图自取6 自取图落盘路径是 botDir/group-media/telegram-group/<chat_id>/<message_id>.jpg', async () => {
  const { tg, b } = await setup()
  const mid = tg.pushPhoto(FRIEND, '落盘路径', { chatId: GROUP })
  const row = await waitPhotoRow(b, mid)
  const expected = join(b.botDir, 'group-media', 'telegram-group', String(GROUP), `${mid}.jpg`)
  expect(realpathSync(String(row.image_path))).toBe(realpathSync(expected))
  expect(statSync(String(row.image_path)).isFile()).toBe(true)
  // 真的把下载到的内容写进去了
  expect(readFileSync(String(row.image_path), 'utf8')).toContain('fake file photos/')
})

test.skipIf(IS_WIN)('群图自取7 落盘文件权限 0600、所在目录 0700', async () => {
  const { tg, b } = await setup()
  const mid = tg.pushPhoto(FRIEND, '权限', { chatId: GROUP })
  const row = await waitPhotoRow(b, mid)
  expect(statSync(String(row.image_path)).mode & 0o777).toBe(0o600)
  expect(statSync(dirname(String(row.image_path))).mode & 0o777).toBe(0o700)
})

// ───────── 读：新 isSelfLine 语义与折叠 ─────────

test('群图自取8 isSelfLine 新语义：本 bot 观察的人类图行照常附图，本 bot 自己发言的行被排除', async () => {
  const { tg, b, gw } = await setup()
  const humanImg = join(String(b.gw.sharedMediaDir), 'human.jpg')
  const selfImg = join(String(b.gw.sharedMediaDir), 'self-bot.jpg')
  writeFileSync(humanImg, 'jpg')
  writeFileSync(selfImg, 'jpg')

  // 第一轮：本 bot 观察到的真人图行（observed_by 是自己，但 is_bot=false）应附图
  seed(b, {
    ts: Date.now() - 2000, text: '人类发的图', from_username: 'friend_user', is_bot: false,
    observed_by: A_NAME, attachment_kind: 'photo', image_path: humanImg,
  })
  const p1 = await groupTurn({ tg, b, gw }, '轮八甲')
  expect(p1.images).toBe(1)

  // 第二轮：本 bot 自己发出的消息（from_username 是自己、is_bot=true）不附图
  seed(b, {
    ts: Date.now(), text: '我自己发出的图', from_username: A_NAME, from_id: '900001', is_bot: true,
    observed_by: A_NAME, attachment_kind: 'photo', image_path: selfImg,
  })
  const p2 = await groupTurn({ tg, b, gw }, '轮八乙')
  expect(p2.images).toBe(0)
})

// v1.4 已删除「群图自取9 超级群同 m…」：一条消息一行、不再有图行，场景不存在

// v1.4 已删除「群图自取10 下载跨过一…」：一条消息一行、不再有图行，场景不存在

test('群图自取11 白名单外的 image_path 不附图（既有行为回归）', async () => {
  const { tg, b, gw, root, shared } = await setup()
  const outside = join(root, 'outside-dir', 'x.jpg')
  mkdirSync(dirname(outside), { recursive: true })
  writeFileSync(outside, 'jpg')
  const legit = join(shared, 'legit.jpg')
  writeFileSync(legit, 'jpg')
  const t = Date.now()
  seed(b, { ts: t - 2000, text: '目录外', attachment_kind: 'photo', image_path: outside })
  seed(b, { ts: t - 1000, text: '目录内', attachment_kind: 'photo', image_path: legit })
  const p = await groupTurn({ tg, b, gw }, '轮十一')
  // 同回合合法图仍附上（证明这一轮确实在读图），目录外的没有附
  expect(p.images).toBe(1)
  expect(p.text).not.toContain(outside)
})

test('群图自取12 私聊发图流程不受影响：模型收到图片块，且不新写群记录', async () => {
  const { tg, b } = await setup()
  tg.pushPhoto(OWNER, '私聊图')
  const p = (await until(
    () => (prompts(b) as unknown as P[]).find(x => x.text.includes('私聊图') && x.images > 0),
    '私聊这一轮的图片块', 20_000,
  ))!
  expect(p.images).toBe(1)
  // 私聊图仍落在自己的媒体目录
  expect(readdirSync(join(b.botDir, 'media')).some(f => f.startsWith('photo-'))).toBe(true)
  // 不因为是图片就往群记录里写（私聊聊天号没有群记录文件）
  expect(readJsonl(join(groupsDirOf(b), `${OWNER}.jsonl`))).toHaveLength(0)
  expect(existsSync(join(b.botDir, 'group-media', 'telegram-group', String(OWNER)))).toBe(false)
})

test('群图自取13 下载失败日志只记 {chat, message_id, reason}：无完整路径、无 file_id', async () => {
  const { tg, b } = await setup()
  tg.faults.push({ method: 'getFile', status: 500, description: 'Internal Server Error', times: 1 })
  const mid = tg.pushPhoto(FRIEND, '日志检查', { chatId: GROUP })
  const e = await until(() => logEvents(b).find(x => x.event === 'group_photo.failed' && Number(x.message_id) === mid),
    'group_photo.failed 日志', 20_000)
  expect(String(e.chat)).toBe(String(GROUP))
  expect(Number(e.message_id)).toBe(Number(mid))
  expect(e.reason).toBe('get_file_failed')
  // 除日志信封与固定三字段外不许多记（尤其不能有 path / file_id / err）
  const allowed = new Set(['at', 'level', 'event', 'bot', 'chat', 'message_id', 'reason'])
  expect(Object.keys(e).filter(k => !allowed.has(k))).toEqual([])
  const one = JSON.stringify(e)
  expect(one).not.toContain('group-media')
  expect(one).not.toContain('photo-big')
  expect(one).not.toContain('photo-small')
  expect(one).not.toContain(String(b.botDir))
  // 整份网关日志里都不该出现落盘路径或 file_id
  const all = JSON.stringify(logEvents(b))
  expect(all).not.toContain('group-media/telegram-group')
  expect(all).not.toContain('photo-big')
  expect(all).not.toContain('photo-small')
})

test('群图自取14 多 bot 隔离：B 读不到 A 的 group-media 图，跳过原因为 outside_allowlist', async () => {
  const root = mkdtempSync(join(tmpdir(), 'dshselfimg-two-'))
  const shared = join(root, 'shared-media')
  const claude = join(root, 'claude-media')
  const groups = join(root, 'groups')
  mkdirSync(shared, { recursive: true })
  mkdirSync(claude, { recursive: true })
  const tgA = new FakeTelegram({ botId: 900001, username: 'bot_a_dsh_bot' })
  const tgB = new FakeTelegram({ botId: 900002, username: 'bot_b_dsh_bot' })
  const access = { groups: { [String(GROUP)]: { allowFrom: [] } } }
  const common = { groups_dir: groups, sharedMediaDir: shared, claudeMediaRoot: claude, burst_window_ms: 0 }
  const ba = makeBot(tgA, { root, name: 'bota', access, gw: { ...common } })
  const bb = makeBot(tgB, { root, name: 'botb', access, gw: { ...common } })
  const gwa = new Gateway(ba)
  const gwb = new Gateway(bb)
  tgs.push(tgA, tgB); bots.push(ba, bb); gws.push(gwa, gwb); roots.push(root)
  await gwa.start()
  await gwb.start()

  // A 收到群图并下载到自己的 group-media
  const mid = tgA.pushPhoto(FRIEND, 'A 群里的图', { chatId: GROUP })
  const row = await until(() => rowsOf(groups, GROUP).find(r => Number(r.message_id) === mid && r.image_path),
    'A 的自取图行', 20_000)
  const realA = realpathSync(String(row.image_path))
  // 用 relative 判前缀，Windows 上路径分隔符是反斜杠，拼 '/' 会永远不匹配
  expect(relative(realpathSync(ba.botDir), realA).startsWith('..')).toBe(false)

  // B 读同一份群记录：A 的图不在 B 的读白名单里，不附图
  const p = await groupTurn({ tg: tgB, b: bb, gw: gwb }, '轮十四')
  expect(p.images).toBe(0)
  await until(
    () => logEvents(bb).some(x => x.event === 'group_image.skipped' && Number(x.message_id) === mid && x.reason === 'outside_allowlist'),
    'B 跳过 A 的图的日志（outside_allowlist）', 10_000,
  )
})

test('群图自取15 折叠后仍按 ts 升序：超过上限的那张下一轮还能出现（水位不被跳过）', async () => {
  let i1 = '', i2 = '', i3 = ''
  const { tg, b, gw } = await setup({
    prepare: d => {
      i1 = join(d.shared, 'fifteen-1.png'); writeFileSync(i1, 'png')
      i2 = join(d.shared, 'fifteen-2.png'); writeFileSync(i2, 'png')
      i3 = join(d.shared, 'fifteen-3.png'); writeFileSync(i3, 'png')
    },
  })
  const now = Date.now()
  // key=2 的第一行（本 bot、无图）先写、位置靠前；同 id 稍后由对端补一行带图的（ts 更晚）。
  // 折叠会把这个键的「值」换成对端那行，但 Map 保留首次插入的位置——不重排的话数组就失序了。
  seed(b, { message_id: 2, ts: now - 5000, text: '图二', from_username: 'friend', is_bot: false, observed_by: A_NAME })
  seed(b, { message_id: 1, ts: now - 3000, text: '图一', from_username: 'friend', is_bot: false, observed_by: 'bot_other', attachment_kind: 'photo', image_path: i1 })
  seed(b, { message_id: 2, ts: now - 1000, text: '', from_username: 'friend', is_bot: false, observed_by: 'bot_other', attachment_kind: 'photo', image_path: i2 })
  seed(b, { message_id: 3, ts: now - 2000, text: '图三', from_username: 'friend', is_bot: false, observed_by: 'bot_other', attachment_kind: 'photo', image_path: i3 })

  // 第一轮按 ts 升序给前两张（图一、图三），额度用完；图二留到下一轮
  const p1 = await groupTurn({ tg, b, gw }, '轮十五甲')
  expect(p1.images).toBe(2)
  // 第二轮必须还能拿到图二——水位若被折叠后失序的数组推到 now-1000，这张就永久漏了
  const p2 = await groupTurn({ tg, b, gw }, '轮十五乙')
  expect(p2.images).toBe(1)
})

test('群图自取16 私聊回合也附群图：被问「群里那张图是什么」时不用凭空编', async () => {
  let img = ''
  const { tg, b } = await setup({
    prepare: d => { img = join(d.shared, 'dm-group.png'); writeFileSync(img, PNG) },
  })
  // 群里有一张别人发的图（本 bot 或对端写的都行），私聊回合要能拿到它
  seed(b, {
    message_id: 80001, ts: Date.now() - 1000, text: '', from_username: 'friend', is_bot: false,
    observed_by: 'bot_other', attachment_kind: 'photo', image_path: img,
  })
  tg.pushText(OWNER, '群里刚才那张图是什么')
  const p = await until(() => prompts(b).find(x => x.text.includes('群里刚才那张图是什么')), '私聊回合的提示', 15_000)
  expect(p!.images).toBe(1)
})
