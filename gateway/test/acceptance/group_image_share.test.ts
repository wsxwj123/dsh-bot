// group-image-share 验收测试（黑盒，依据 .devflow/group-image-share-接口.md 第 3 节 20 条行为清单 + 第 2 节错误契约）。
// 只通过对外接口：配置文件起网关子进程、假 Telegram 收发、本机 /v1/inject、读写群记录文件，
// 以及假 dsh 落盘的 prompts.jsonl（看模型这一轮收到的图片块数量）。
// 注释里的「清单 N」对应接口文件第 3 节编号；错误契约单独标「契约」。
import { afterEach, expect, test } from 'bun:test'
import { appendFileSync, chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, statSync, symlinkSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { FakeTelegram } from '../fakes/fake-telegram'
import { FRIEND, Gateway, logEvents, makeBot, OWNER, prompts, readJsonl, sleep, until, type BotEnv } from './_acc'

const GROUP = -1001777
const A_NAME = 'bot_a_dsh_bot'
const MiB = 1024 * 1024

let tgs: FakeTelegram[] = []
let bots: BotEnv[] = []
let gws: Gateway[] = []
let roots: string[] = []
let nextSeedId = 300

afterEach(async () => {
  for (const g of gws) await g.stop().catch(() => {})
  for (const t of tgs) t.stop()
  for (const r of roots) {
    try { chmodSync(join(r, 'groups-ro'), 0o700) } catch {}
    try { chmodSync(join(r, 'groups-ro', `${GROUP}.jsonl`), 0o600) } catch {}
    try { rmSync(r, { recursive: true, force: true }) } catch {}
  }
  tgs = []; bots = []; gws = []; roots = []
})

type Dirs = { root: string; shared: string; media: string }
type Env = { tg: FakeTelegram; b: BotEnv; gw: Gateway; root: string; shared: string; media: string }

/** 起一只 bot，注入临时的 sharedMediaDir / claudeMediaRoot / groups_dir；prepare 在网关启动前执行 */
async function setup(o: { gw?: Record<string, unknown>; prepare?: (d: Dirs) => void; blockGroupWrites?: boolean } = {}): Promise<Env> {
  const root = mkdtempSync(join(tmpdir(), 'dshimg-'))
  const shared = join(root, 'shared-media')
  const media = join(root, 'claude-media')
  mkdirSync(shared, { recursive: true })
  mkdirSync(media, { recursive: true })
  const groupsDir = o.blockGroupWrites ? join(root, 'groups-ro') : join(root, 'groups')
  if (o.blockGroupWrites) {
    mkdirSync(groupsDir, { recursive: true })
    writeFileSync(join(groupsDir, `${GROUP}.jsonl`), '')
  }
  const tg = new FakeTelegram({ botId: 900001, username: A_NAME })
  const access = { groups: { [String(GROUP)]: { allowFrom: [] } } }
  const b = makeBot(tg, {
    name: 'a', root, access,
    gw: {
      burst_window_ms: 0,
      groups_dir: groupsDir,
      sharedMediaDir: shared,
      claudeMediaRoot: media,
      // 发送侧原有的允许目录：这里就是 sharedMediaDir，好让「从共享图库里发图」这条路能走通
      image_dirs: [shared],
      ...o.gw,
    },
  })
  o.prepare?.({ root, shared, media })
  if (o.blockGroupWrites) {
    chmodSync(join(groupsDir, `${GROUP}.jsonl`), 0o400)
    chmodSync(groupsDir, 0o500)
  }
  const gw = new Gateway(b)
  tgs.push(tg); bots.push(b); gws.push(gw); roots.push(root)
  await gw.start()
  return { tg, b, gw, root, shared, media }
}

const gfile = (b: BotEnv) => join(b.root, 'groups', `${GROUP}.jsonl`)
const rows = (b: BotEnv) => readJsonl<Record<string, any>>(gfile(b))

/** 往群记录里预置一行（字段形状按接口约定 5.12.4；本任务只新增 attachment_kind / image_path） */
function seed(b: BotEnv, o: Record<string, any>): void {
  mkdirSync(join(b.root, 'groups'), { recursive: true })
  const base = {
    ts: Date.now(), chat_id: String(GROUP), message_id: nextSeedId++,
    from_id: String(FRIEND), from_username: 'friend_user', from_name: 'friend', is_bot: false, observed_by: 'a',
  }
  appendFileSync(gfile(b), JSON.stringify({ ...base, ...o }) + '\n')
}

type P = { sessionId: string; text: string; images: number }
const promptWith = (b: BotEnv, marker: string) =>
  (prompts(b) as unknown as P[]).filter(p => p.text.includes(marker)).at(-1)

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

const botRow = (b: BotEnv, marker: string) =>
  rows(b).filter(r => r.from_username === A_NAME && String(r.text ?? '').includes(marker)).at(-1)

// ───────── 写：群里一次回复的图片字段 ─────────

test('清单1 群里发 sharedMediaDir 内的 .png：该回复行记 attachment_kind=photo、image_path=真实绝对路径', async () => {
  let img = ''
  const { tg, b, gw } = await setup({ prepare: d => { img = join(d.shared, 'pic-one.png'); writeFileSync(img, 'png') } })
  await groupTurn({ tg, b, gw }, '图一', ` !file:${img}`)
  const row = (await until(() => botRow(b, '图一'), '群记录里的 bot 行'))!
  expect(tg.sentTo(GROUP).some(s => s.method === 'sendPhoto')).toBe(true)
  expect(row.attachment_kind).toBe('photo')
  expect(row.image_path).toBe(realpathSync(img))
})

test('清单2 群里只发文字：该行没有 attachment_kind 也没有 image_path', async () => {
  const { tg, b, gw } = await setup()
  await groupTurn({ tg, b, gw }, '纯文字')
  const row = (await until(() => botRow(b, '纯文字'), '群记录里的 bot 行'))!
  expect('attachment_kind' in row).toBe(false)
  expect('image_path' in row).toBe(false)
})

test('清单3 群里发 .gif（走 animation 通道）：发送走 sendAnimation，该行没有图片字段', async () => {
  let gif = ''
  const { tg, b, gw } = await setup({ prepare: d => { gif = join(d.shared, 'anim.gif'); writeFileSync(gif, 'GIF89a') } })
  await groupTurn({ tg, b, gw }, '动图', ` !file:${gif}`)
  const row = (await until(() => botRow(b, '动图'), '群记录里的 bot 行'))!
  expect(tg.sentTo(GROUP).some(s => s.method === 'sendAnimation')).toBe(true)
  expect('attachment_kind' in row).toBe(false)
})

test('清单4 群里发超过 8 MiB 的 .png：发送照常成功（sendPhoto），该行没有图片字段', async () => {
  let img = ''
  const { tg, b, gw } = await setup({ prepare: d => { img = join(d.shared, 'huge.png'); writeFileSync(img, Buffer.alloc(8 * MiB + 1, 1)) } })
  await groupTurn({ tg, b, gw }, '大图', ` !file:${img}`)
  const row = (await until(() => botRow(b, '大图'), '群记录里的 bot 行'))!
  expect(statSync(img).size).toBeGreaterThan(8 * MiB)
  expect(tg.sentTo(GROUP).some(s => s.method === 'sendPhoto')).toBe(true)
  expect('attachment_kind' in row).toBe(false)
  expect('image_path' in row).toBe(false)
})

test('清单5 群里一次回复两张图：本轮所有 bot 行恰好一行记 attachment_kind=photo，且 image_path 是第一张图的真实路径', async () => {
  let p1 = ''
  let p2 = ''
  const { tg, b, gw } = await setup({
    prepare: d => {
      p1 = join(d.shared, 'two-a.png'); writeFileSync(p1, 'png')
      p2 = join(d.shared, 'two-b.png'); writeFileSync(p2, 'png')
    },
  })
  // 一次回复里带两个文件：走 reply 工具的 files 数组
  const extra = `\n!tool:reply|${JSON.stringify({ text: '两张图', files: [p1, p2] })}`
  await groupTurn({ tg, b, gw }, '两张', extra)
  // 两张图都确实发出去了
  await until(() => tg.sentTo(GROUP).filter(s => s.method === 'sendPhoto').length === 2, '两张图都发出', 20_000)
  expect(tg.sentTo(GROUP).filter(s => s.method === 'sendPhoto')).toHaveLength(2)
  // 等本轮回复落盘后再统一核对（本用例没有预置记录，记录里的 bot 行都属于这一轮）
  await until(() => botRow(b, '两张'), '群记录里的本轮 bot 行')
  await sleep(300)
  const photoRows = rows(b).filter(r => r.from_username === A_NAME && r.attachment_kind === 'photo')
  expect(photoRows).toHaveLength(1)
  expect(photoRows[0].image_path).toBe(realpathSync(p1))
  expect(photoRows[0].image_path).not.toBe(realpathSync(p2))
})

// ───────── 读：群记录里的图片交给模型 ─────────

test('清单6 记录里有别人写的合法图片：这一轮给模型的图片块数量为 1', async () => {
  let img = ''
  const { tg, b, gw } = await setup({ prepare: d => { img = join(d.shared, 'seen.png'); writeFileSync(img, 'png') } })
  seed(b, { text: '看这张图', attachment_kind: 'photo', image_path: img })
  const p = await groupTurn({ tg, b, gw }, '轮六')
  expect(p.images).toBe(1)
})

test('清单7 上一回合给过的图，下一回合不再给（第二回合图片块为 0）', async () => {
  let img = ''
  const { tg, b, gw } = await setup({ prepare: d => { img = join(d.shared, 'once.png'); writeFileSync(img, 'png') } })
  seed(b, { text: '只给一次', attachment_kind: 'photo', image_path: img })
  const p1 = await groupTurn({ tg, b, gw }, '轮七甲')
  expect(p1.images).toBe(1)
  const p2 = await groupTurn({ tg, b, gw }, '轮七乙')
  expect(p2.images).toBe(0)
})

test('清单8 记录里有 3 行不同的合法图：本回合只给 2 张，第 3 行下一回合给出', async () => {
  const imgs: string[] = []
  const { tg, b, gw } = await setup({
    prepare: d => {
      for (const n of ['a', 'b', 'c']) { const p = join(d.shared, `three-${n}.png`); writeFileSync(p, 'png'); imgs.push(p) }
    },
  })
  const t = Date.now()
  seed(b, { ts: t - 3000, text: '@1', attachment_kind: 'photo', image_path: imgs[0] })
  seed(b, { ts: t - 2000, text: '@2', attachment_kind: 'photo', image_path: imgs[1] })
  seed(b, { ts: t - 1000, text: '@3', attachment_kind: 'photo', image_path: imgs[2] })
  const p1 = await groupTurn({ tg, b, gw }, '轮八甲')
  expect(p1.images).toBe(2)
  const p2 = await groupTurn({ tg, b, gw }, '轮八乙')
  expect(p2.images).toBe(1)
})

test('清单9 记录里的图片行 from_username 是本 bot 自己：不附图', async () => {
  let img = ''
  const { tg, b, gw } = await setup({ prepare: d => { img = join(d.shared, 'self.png'); writeFileSync(img, 'png') } })
  seed(b, { text: '自己发的', is_bot: true, from_username: A_NAME, attachment_kind: 'photo', image_path: img })
  const p = await groupTurn({ tg, b, gw }, '轮九')
  expect(p.images).toBe(0)
})

test('清单10 记录里的 image_path 指向不允许目录（临时目录之外）：不附图，本轮回复照常产生', async () => {
  const { tg, b, gw, root } = await setup()
  const outside = join(root, 'outside-dir', 'x.jpg')
  mkdirSync(join(root, 'outside-dir'), { recursive: true })
  writeFileSync(outside, 'jpg')
  seed(b, { text: '目录外', attachment_kind: 'photo', image_path: outside })
  const p = await groupTurn({ tg, b, gw }, '轮十')
  expect(p.images).toBe(0)
  expect(p.text).not.toContain(outside)
  expect(tg.sentTo(GROUP).some(s => (s.text ?? '').includes('轮十'))).toBe(true)
})

test('清单11 记录里的 image_path 是符号链接、真身指向允许目录之外：不附图', async () => {
  let link = ''
  const { tg, b, gw, root, shared } = await setup()
  const outside = join(root, 'link-target.jpg')
  writeFileSync(outside, 'jpg')
  link = join(shared, 'link.jpg')
  symlinkSync(outside, link)
  seed(b, { text: '链接', attachment_kind: 'photo', image_path: link })
  const p = await groupTurn({ tg, b, gw }, '轮十一')
  expect(p.images).toBe(0)
})

test('清单12 记录里的 image_path 含 .. 穿越、解析后落在允许目录外：不附图', async () => {
  const { tg, b, gw, root, shared } = await setup()
  const outside = join(root, 'escape.jpg')
  writeFileSync(outside, 'jpg')
  const traversed = `${shared}/../escape.jpg`
  expect(traversed).toContain('..')
  seed(b, { text: '穿越', attachment_kind: 'photo', image_path: traversed })
  const p = await groupTurn({ tg, b, gw }, '轮十二')
  expect(p.images).toBe(0)
})

test('清单13 记录里的 image_path 指向不存在的文件：不附图、不报错，本轮回复照常', async () => {
  let missing = ''
  const { tg, b, gw } = await setup({ prepare: d => { missing = join(d.shared, 'missing.jpg') } })
  seed(b, { text: '不存在', attachment_kind: 'photo', image_path: missing })
  const p = await groupTurn({ tg, b, gw }, '轮十三')
  expect(p.images).toBe(0)
  expect(tg.sentTo(GROUP).some(s => (s.text ?? '').includes('轮十三'))).toBe(true)
})

test('清单14 记录里的 image_path 不是绝对路径（foo.jpg）：不附图', async () => {
  const { tg, b, gw } = await setup()
  seed(b, { text: '相对路径', attachment_kind: 'photo', image_path: 'foo.jpg' })
  const p = await groupTurn({ tg, b, gw }, '轮十四')
  expect(p.images).toBe(0)
})

test('清单15 记录里的 attachment_kind 是 document：即使路径合法也不附图', async () => {
  let img = ''
  const { tg, b, gw } = await setup({ prepare: d => { img = join(d.shared, 'doc.jpg'); writeFileSync(img, 'jpg') } })
  seed(b, { text: '当文件发', attachment_kind: 'document', image_path: img })
  const p = await groupTurn({ tg, b, gw }, '轮十五')
  expect(p.images).toBe(0)
})

test('清单16 记录里的合法图是 31 分钟前的：不附图', async () => {
  let img = ''
  const { tg, b, gw } = await setup({ prepare: d => { img = join(d.shared, 'old.png'); writeFileSync(img, 'png') } })
  seed(b, { ts: Date.now() - 31 * 60 * 1000, text: '旧图', attachment_kind: 'photo', image_path: img })
  const p = await groupTurn({ tg, b, gw }, '轮十六')
  expect(p.images).toBe(0)
})

test('清单17 记录里有一行只有图片、没有 text：该行仍被读到，图能附上', async () => {
  let img = ''
  const { tg, b, gw } = await setup({ prepare: d => { img = join(d.shared, 'no-text.png'); writeFileSync(img, 'png') } })
  const row = { chat_id: String(GROUP), message_id: nextSeedId++, from_id: String(FRIEND), from_username: 'friend_user', from_name: 'friend', is_bot: false, observed_by: 'a', ts: Date.now(), attachment_kind: 'photo', image_path: img }
  expect('text' in row).toBe(false)
  mkdirSync(join(b.root, 'groups'), { recursive: true })
  appendFileSync(gfile(b), JSON.stringify(row) + '\n')
  const p = await groupTurn({ tg, b, gw }, '轮十七')
  expect(p.images).toBe(1)
})

test('清单18 群记录写入失败（目录只读）：已发出的群消息仍是成功', async () => {
  const { tg, b, gw, root } = await setup({ blockGroupWrites: true })
  await groupTurn({ tg, b, gw }, '写不进去')
  expect(tg.sentTo(GROUP).some(s => (s.text ?? '').includes('写不进去'))).toBe(true)
  expect(readFileSync(join(root, 'groups-ro', `${GROUP}.jsonl`), 'utf8')).toBe('')
})

test('清单19 私聊回合即使群记录里有图片字段也不附图', async () => {
  let img = ''
  const { tg, b, gw } = await setup({ prepare: d => { img = join(d.shared, 'group-only.png'); writeFileSync(img, 'png') } })
  seed(b, { text: '群里的图', attachment_kind: 'photo', image_path: img })
  tg.pushText(OWNER, '私聊一句轮十九')
  const p = (await until(() => promptWith(b, '私聊一句轮十九'), '私聊这一轮提示', 15_000))!
  expect(p.images).toBe(0)
  expect(p.text).not.toContain(img)
  expect(tg.sentTo(OWNER).some(s => (s.text ?? '').includes('私聊一句轮十九'))).toBe(true)
})

test('清单20 启动后新增的 claudeMediaRoot 子目录不在允许清单内：其中的图不附图，启动时已有的子目录照常附', async () => {
  let early = ''
  let late = ''
  const { tg, b, gw, media } = await setup({
    prepare: d => {
      mkdirSync(join(d.media, 'existing', 'telegram-group'), { recursive: true })
      early = join(d.media, 'existing', 'telegram-group', 'early.jpg')
      writeFileSync(early, 'jpg')
    },
  })
  mkdirSync(join(media, 'added', 'telegram-group'), { recursive: true })
  late = join(media, 'added', 'telegram-group', 'late.jpg')
  writeFileSync(late, 'jpg')
  const t = Date.now()
  seed(b, { ts: t - 2000, text: '启动前目录', attachment_kind: 'photo', image_path: early })
  seed(b, { ts: t - 1000, text: '启动后目录', attachment_kind: 'photo', image_path: late })
  const p = await groupTurn({ tg, b, gw }, '轮二十')
  expect(p.images).toBe(1)
  expect(p.text).not.toContain(late)
})

test('白名单边界 claudeMediaRoot 一级目录本身（<bot>/ 而非 <bot>/telegram-group/）不可读：该行不附图，同回合合法图仍附上', async () => {
  let disallowed = ''
  let legit = ''
  const { tg, b, gw } = await setup({
    prepare: d => {
      // 同级合法目录：启动时已存在的 <bot>/telegram-group/，用于证明这一轮确实在读图
      mkdirSync(join(d.media, 'somerepo', 'telegram-group'), { recursive: true })
      legit = join(d.media, 'somerepo', 'telegram-group', 'ok-boundary.jpg')
      writeFileSync(legit, 'jpg')
      // 不允许：<claudeMediaRoot>/somerepo/ 这一级目录本身
      disallowed = join(d.media, 'somerepo', 'photo.jpg')
      writeFileSync(disallowed, 'jpg')
    },
  })
  const t = Date.now()
  seed(b, { ts: t - 2000, text: '一级目录图', attachment_kind: 'photo', image_path: disallowed })
  seed(b, { ts: t - 1000, text: '合法目录图', attachment_kind: 'photo', image_path: legit })
  const p = await groupTurn({ tg, b, gw }, '轮边界一')
  expect(p.images).toBe(1)
  expect(p.text).not.toContain(disallowed)
  expect(tg.sentTo(GROUP).some(s => (s.text ?? '').includes('轮边界一'))).toBe(true)
})

test('白名单边界 claudeMediaRoot 一级子目录下的其他子目录（<bot>/telegram/ 私聊目录）不可读：该行不附图，同回合合法图仍附上', async () => {
  let disallowed = ''
  let legit = ''
  const { tg, b, gw } = await setup({
    prepare: d => {
      // 同级合法目录：启动时已存在的 <bot>/telegram-group/
      mkdirSync(join(d.media, 'somerepo', 'telegram-group'), { recursive: true })
      legit = join(d.media, 'somerepo', 'telegram-group', 'ok-sibling.jpg')
      writeFileSync(legit, 'jpg')
      // 不允许：<claudeMediaRoot>/somerepo/telegram/（私聊图目录）
      mkdirSync(join(d.media, 'somerepo', 'telegram'), { recursive: true })
      disallowed = join(d.media, 'somerepo', 'telegram', 'photo.jpg')
      writeFileSync(disallowed, 'jpg')
    },
  })
  const t = Date.now()
  seed(b, { ts: t - 2000, text: '私聊目录图', attachment_kind: 'photo', image_path: disallowed })
  seed(b, { ts: t - 1000, text: '合法目录图', attachment_kind: 'photo', image_path: legit })
  const p = await groupTurn({ tg, b, gw }, '轮边界二')
  expect(p.images).toBe(1)
  expect(p.text).not.toContain(disallowed)
  expect(tg.sentTo(GROUP).some(s => (s.text ?? '').includes('轮边界二'))).toBe(true)
})

test('读取-同 message_id 双写 本 bot 行无图、对方 bot 行有图：图片块为 1 且回复照常产生（图不因挂到本 bot 行被自排除）', async () => {
  let img = ''
  const { tg, b, gw } = await setup({ prepare: d => { img = join(d.shared, 'dual-write.png'); writeFileSync(img, 'png') } })
  const SAME_ID = 990001
  // 第一行：本 bot 网关记下的别人消息，observed_by 是自己、无 image_path
  seed(b, {
    message_id: SAME_ID, ts: Date.now() - 2000, text: '同 id 双写',
    from_username: 'friend_user', from_id: String(FRIEND), is_bot: false, observed_by: A_NAME,
  })
  // 第二行：另一个 bot 写的同一 message_id，带图
  seed(b, {
    message_id: SAME_ID, ts: Date.now() - 1000, text: '同 id 双写',
    from_username: 'bot_b_other', from_id: '900002', is_bot: true, observed_by: 'bot_b_other',
    attachment_kind: 'photo', image_path: img,
  })
  const p = await groupTurn({ tg, b, gw }, '轮双写')
  expect(p.images).toBe(1)
  expect(tg.sentTo(GROUP).some(s => (s.text ?? '').includes('轮双写'))).toBe(true)
})

test('读取-同 message_id 双写 两行都带图（自己与对方各一张）：只附对方那张，图片块为 1', async () => {
  let selfImg = ''
  let otherImg = ''
  const { tg, b, gw } = await setup({
    prepare: d => {
      selfImg = join(d.shared, 'dual-self.png'); writeFileSync(selfImg, 'png')
      otherImg = join(d.shared, 'dual-other.png'); writeFileSync(otherImg, 'png')
    },
  })
  const SAME_ID = 990002
  seed(b, {
    message_id: SAME_ID, ts: Date.now() - 2000, text: '同 id 双写带图',
    from_username: 'friend_user', from_id: String(FRIEND), is_bot: false, observed_by: A_NAME,
    attachment_kind: 'photo', image_path: selfImg,
  })
  seed(b, {
    message_id: SAME_ID, ts: Date.now() - 1000, text: '同 id 双写带图',
    from_username: 'bot_b_other', from_id: '900002', is_bot: true, observed_by: 'bot_b_other',
    attachment_kind: 'photo', image_path: otherImg,
  })
  const p = await groupTurn({ tg, b, gw }, '轮双写二')
  expect(p.images).toBe(1)
  expect(p.text).not.toContain(selfImg)
  expect(tg.sentTo(GROUP).some(s => (s.text ?? '').includes('轮双写二'))).toBe(true)
})

// ───────── 错误契约：不满足的行静默当没有图 ─────────

test('契约 非字符串 / 空串 / 超长 / 含 NUL 的 image_path：都不附图，回复照常', async () => {
  const { tg, b, gw } = await setup()
  seed(b, { text: '数字路径', attachment_kind: 'photo', image_path: 123 })
  seed(b, { text: '空路径', attachment_kind: 'photo', image_path: '' })
  seed(b, { text: '超长路径', attachment_kind: 'photo', image_path: `/x/${'a'.repeat(4097)}.jpg` })
  seed(b, { text: 'NUL 路径', attachment_kind: 'photo', image_path: 'bad\u0000name.jpg' })
  const p = await groupTurn({ tg, b, gw }, '轮廿一')
  expect(p.images).toBe(0)
  expect(tg.sentTo(GROUP).some(s => (s.text ?? '').includes('轮廿一'))).toBe(true)
})

test('契约 image_path 指向目录（不是普通文件）：不附图', async () => {
  let dir = ''
  const { tg, b, gw } = await setup({ prepare: d => { dir = join(d.shared, 'not-a-file.png'); mkdirSync(dir) } })
  seed(b, { text: '是目录', attachment_kind: 'photo', image_path: dir })
  const p = await groupTurn({ tg, b, gw }, '轮廿二')
  expect(statSync(dir).isDirectory()).toBe(true)
  expect(p.images).toBe(0)
})

test('契约 扩展名不是 .jpg/.jpeg/.png/.webp（.gif）：不附图', async () => {
  let gif = ''
  const { tg, b, gw } = await setup({ prepare: d => { gif = join(d.shared, 'read.gif'); writeFileSync(gif, 'GIF89a') } })
  seed(b, { text: '读 gif', attachment_kind: 'photo', image_path: gif })
  const p = await groupTurn({ tg, b, gw }, '轮廿三')
  expect(p.images).toBe(0)
})

test('契约 打开或读取失败（文件无读权限）：不附图，回复照常', async () => {
  let img = ''
  const { tg, b, gw } = await setup({ prepare: d => { img = join(d.shared, 'noread.jpg'); writeFileSync(img, 'jpg') } })
  chmodSync(img, 0o000)
  seed(b, { text: '读不了', attachment_kind: 'photo', image_path: img })
  const p = await groupTurn({ tg, b, gw }, '轮廿四')
  expect(p.images).toBe(0)
  expect(tg.sentTo(GROUP).some(s => (s.text ?? '').includes('轮廿四'))).toBe(true)
})

test('契约 日志只记 {reason, chat, message_id}：不记路径、不把路径写进模型上下文', async () => {
  let missing = ''
  const { tg, b, gw } = await setup({ prepare: d => { missing = join(d.shared, 'zz-leak-marker-9f3c.jpg') } })
  seed(b, { text: '日志检查', attachment_kind: 'photo', image_path: missing })
  const p = await groupTurn({ tg, b, gw }, '轮廿五')
  await sleep(300)
  expect(p.images).toBe(0)
  expect(p.text).not.toContain('zz-leak-marker-9f3c')
  const logged = JSON.stringify(logEvents(b))
  expect(logged).not.toContain('zz-leak-marker-9f3c')
})
