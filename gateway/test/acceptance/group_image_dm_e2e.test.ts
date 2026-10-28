// group-image-in-dm 验收测试（黑盒，依据 .devflow/group-image-in-dm-接口.md 第 2、4 节与错误契约）。
// 只通过对外接口：配置文件起网关子进程、假 Telegram 收发、读写群记录文件、只读账本 meta、假 dsh 的 prompts.jsonl。
// setup 抄自同目录 group_image_share.test.ts 的公共起环境方式；断言全部按本任务接口文件写。
import { afterEach, expect, setDefaultTimeout, test } from 'bun:test'
import { Database } from 'bun:sqlite'
import { appendFileSync, mkdirSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { FakeTelegram } from '../fakes/fake-telegram'
import {
  FRIEND, Gateway, OWNER, eventsOf, evs, inboundRow, ledgerPath, makeBot, metaGet, prompts, until, type BotEnv,
} from './_acc'

setDefaultTimeout(60_000)

const GROUP = -1001777
const A_NAME = 'bot_a_dsh_bot'
/** 主人的私聊 chat id：私聊回合里 group_recap / group_images_dm 的水位按它记 */
const DM = String(OWNER)
const MIN = 60_000
const HOUR = 60 * MIN
const MiB = 1024 * 1024
const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
  'base64',
)

let tgs: FakeTelegram[] = []
let bots: BotEnv[] = []
let gws: Gateway[] = []
let roots: string[] = []
let nextSeedId = 300

afterEach(async () => {
  for (const g of gws) await g.stop().catch(() => {})
  for (const t of tgs) t.stop()
  for (const r of roots) {
    try { rmSync(r, { recursive: true, force: true }) } catch {}
  }
  tgs = []; bots = []; gws = []; roots = []
})

type Dirs = { root: string; shared: string; media: string }
type Env = { tg: FakeTelegram; b: BotEnv; gw: Gateway; root: string; shared: string; media: string }

async function setup(o: { prepare?: (d: Dirs) => void } = {}): Promise<Env> {
  const root = mkdtempSync(join(tmpdir(), 'dshimgdm-'))
  const shared = join(root, 'shared-media')
  const media = join(root, 'claude-media')
  mkdirSync(shared, { recursive: true })
  mkdirSync(media, { recursive: true })
  const tg = new FakeTelegram({ botId: 900001, username: A_NAME })
  const access = { groups: { [String(GROUP)]: { allowFrom: [] } } }
  const made = makeBot(tg, {
    name: 'a', root, access,
    gw: {
      burst_window_ms: 0,
      groups_dir: join(root, 'groups'),
      sharedMediaDir: shared,
      claudeMediaRoot: media,
      image_dirs: [shared],
    },
  })
  o.prepare?.({ root, shared, media })
  const gw = new Gateway(made)
  tgs.push(tg); bots.push(made); gws.push(gw); roots.push(root)
  await gw.start()
  return { tg, b: made, gw, root, shared, media }
}

/** 群记录里预置一行（字段形状按接口约定 5.12.4；本任务只新增 attachment_kind / image_path） */
function seed(b: BotEnv, o: Record<string, unknown>): void {
  mkdirSync(join(b.root, 'groups'), { recursive: true })
  const base = {
    ts: Date.now(), chat_id: String(GROUP), message_id: nextSeedId++,
    from_id: String(FRIEND), from_username: 'friend_user', from_name: 'friend', is_bot: false, observed_by: 'a',
  }
  appendFileSync(join(b.root, 'groups', `${GROUP}.jsonl`), JSON.stringify({ ...base, ...o }) + '\n')
}

/** 31 分钟前的带图近况：在 groupImages 的 30 分钟窗口外、近况的 48 小时窗口内 */
function seedOldImageRow(b: BotEnv, imagePath: string, text: string): void {
  seed(b, {
    ts: Date.now() - 31 * MIN,
    text,
    from_id: String(FRIEND),
    from_username: 'friend_user',
    attachment_kind: 'photo',
    image_path: realpathSync(imagePath),
  })
}

const promptWith = (b: BotEnv, marker: string) => prompts(b).filter(p => p.text.includes(marker)).at(-1)

const wmKeys = (chatId: string) => [
  `group_recap:${chatId}`, `group_images_dm:${chatId}`, `group_images:${chatId}`, `dm_recap:${chatId}`,
]

/** 直接写账本 meta（网关运行中，开 busy_timeout；meta 表就是 k/v 两列） */
function metaSet(b: BotEnv, k: string, v: string): void {
  const db = new Database(ledgerPath(b))
  try {
    db.run('PRAGMA busy_timeout = 3000')
    db.query('INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v').run(k, v)
  } finally {
    db.close()
  }
}

const seedWatermarks = (b: BotEnv, chatId: string) => { for (const k of wmKeys(chatId)) metaSet(b, k, '1700000000000') }
const isCleared = (b: BotEnv, chatId: string) => wmKeys(chatId).every(k => !metaGet(b, k))
const assertCleared = (b: BotEnv, chatId: string) => {
  for (const k of wmKeys(chatId)) expect(metaGet(b, k) ?? '', `${k} 应被清空`).toBe('')
}

// ───────── 第 2 节清单 13、14：清四个跨聊天水位 ─────────

test('清单13 rollSegment（上下文超长换段）触发：四个跨聊天水位键都被清空', async () => {
  const { tg, b } = await setup()
  tg.pushText(OWNER, '第一句话')
  await until(() => promptWith(b, '第一句话'), '第一轮提示')
  await until(() => evs(b, 'turn.end').length >= 1, '第一轮结束', 20_000)
  seedWatermarks(b, DM)
  expect(metaGet(b, `group_recap:${DM}`)).toBe('1700000000000')
  mkdirSync(b.acpState, { recursive: true })
  writeFileSync(join(b.acpState, 'next-error.txt'), "400 This model's maximum context length is 8192 tokens")
  tg.pushText(OWNER, '第二句话')
  await until(
    () => eventsOf(b, 'segment_rolled').some(e => e.data?.reason === 'context-overflow'),
    '上下文超长换段',
    25_000,
  )
  assertCleared(b, DM)
})

test('清单14 cmdClear（/clear）触发：四个跨聊天水位键都被清空', async () => {
  const { tg, b } = await setup()
  // 先聊一轮，确保清空时确实存在可结束的段（真实使用中 /clear 常发生在一段对话之后）
  tg.pushText(OWNER, '清空前的一句话')
  await until(() => promptWith(b, '清空前的一句话'), '第一轮提示')
  await until(() => evs(b, 'turn.end').length >= 1, '第一轮结束', 20_000)
  seedWatermarks(b, DM)
  expect(metaGet(b, `group_recap:${DM}`)).toBe('1700000000000')
  tg.pushText(OWNER, '/clear')
  await until(() => isCleared(b, DM), '/clear 清空四个水位', 20_000)
  assertCleared(b, DM)
})

// ───────── 第 4 节清单 15、16：水位两阶段 ─────────

test('清单15 首次 prompt 成功且带图：group_recap 水位落盘', async () => {
  let img = ''
  const { tg, b } = await setup({ prepare: d => { img = join(d.shared, 'recap.png'); writeFileSync(img, PNG) } })
  seedOldImageRow(b, img, '带图近况甲')
  tg.pushText(OWNER, '在吗')
  const p = (await until(() => promptWith(b, '在吗'), '私聊提示', 20_000))!
  expect(p.text).toContain('带图近况甲')
  expect(p.text).not.toContain('看不到内容')
  expect(p.images ?? 0).toBeGreaterThanOrEqual(1)
  const wm = await until(() => metaGet(b, `group_recap:${DM}`), 'group_recap 水位落盘', 15_000)
  const n = Number(wm)
  expect(Number.isFinite(n)).toBe(true)
  expect(n).toBeGreaterThan(Date.now() - 48 * HOUR)
})

test('清单16 首轮失败（模型报错/重试用尽）：水位不落盘，下一轮重新注入近况和图片', async () => {
  let img = ''
  const { tg, b } = await setup({ prepare: d => { img = join(d.shared, 'recap.png'); writeFileSync(img, PNG) } })
  seedOldImageRow(b, img, '带图近况乙')
  const mid = tg.pushText(OWNER, '第一问 !empty:3:b')   // maxTurnRetries=2 → 共 3 次尝试，正好只失败第一轮
  await until(() => inboundRow(b, OWNER, mid)?.state === 'dead', '第一条消息重试用尽', 30_000)
  expect(metaGet(b, `group_recap:${DM}`) ?? '', '失败轮不应落盘水位').toBe('')
  const p1 = prompts(b).find(p => p.text.includes('第一问'))
  expect(p1?.text).toContain('带图近况乙')
  tg.pushText(OWNER, '第二问')
  const p2 = (await until(() => promptWith(b, '第二问'), '第二轮提示', 20_000))!
  expect(p2.text).toContain('带图近况乙')
  expect(p2.images ?? 0).toBeGreaterThanOrEqual(1)
  await until(() => metaGet(b, `group_recap:${DM}`), '成功后 group_recap 水位落盘', 15_000)
})

// ───────── 错误契约：读图失败不附图，只留 [图片] 占位 ─────────

test('契约 图文件不存在：近况只留 [图片] 占位，不附 image 块，也没有「看不到内容」', async () => {
  const { tg, b, shared } = await setup()
  seed(b, {
    ts: Date.now() - 31 * MIN, text: '',
    attachment_kind: 'photo', image_path: join(shared, 'missing.png'),
  })
  tg.pushText(OWNER, '在吗')
  const p = (await until(() => promptWith(b, '在吗'), '私聊提示', 20_000))!
  expect(p.text).toContain('[图片]')
  expect(p.text).not.toContain('看不到内容')
  expect(p.images ?? 0).toBe(0)
})

test('契约 图超 2 MiB：近况只留 [图片] 占位，不附 image 块', async () => {
  let img = ''
  const { tg, b } = await setup({ prepare: d => { img = join(d.shared, 'big.png'); writeFileSync(img, Buffer.alloc(2 * MiB + 1, 1)) } })
  seed(b, {
    ts: Date.now() - 31 * MIN, text: '',
    attachment_kind: 'photo', image_path: realpathSync(img),
  })
  tg.pushText(OWNER, '在吗')
  const p = (await until(() => promptWith(b, '在吗'), '私聊提示', 20_000))!
  expect(p.text).toContain('[图片]')
  expect(p.text).not.toContain('看不到内容')
  expect(p.images ?? 0).toBe(0)
})

test('契约 图在白名单外：近况只留 [图片] 占位，不附 image 块', async () => {
  const { tg, b } = await setup()
  const outside = join(mkdtempSync(join(tmpdir(), 'outside-')), 'x.png')
  writeFileSync(outside, PNG)
  seed(b, {
    ts: Date.now() - 31 * MIN, text: '',
    attachment_kind: 'photo', image_path: outside,
  })
  tg.pushText(OWNER, '在吗')
  const p = (await until(() => promptWith(b, '在吗'), '私聊提示', 20_000))!
  expect(p.text).toContain('[图片]')
  expect(p.text).not.toContain('看不到内容')
  expect(p.images ?? 0).toBe(0)
})

// ───────── 追加：跨通道去重与水位推进（以上用例不动） ─────────

test('场景5 同一条群图被两只 bot 各记一遍（30 分钟内）：私聊回合 recap 与群图通道合计只附 1 张', async () => {
  let img = ''
  const { tg, b } = await setup({ prepare: d => { img = join(d.shared, 'same.png'); writeFileSync(img, PNG) } })
  const ts = Date.now() - 5 * MIN
  const messageId = nextSeedId++
  const imagePath = realpathSync(img)
  // 同一条真人消息被两只 bot 各记一行：同 chat/ts/from_id，仅 observed_by 不同
  for (const observedBy of ['a', 'peer_bot']) {
    seed(b, { ts, message_id: messageId, text: '', attachment_kind: 'photo', image_path: imagePath, observed_by: observedBy })
  }
  tg.pushText(OWNER, '同图去重甲')
  const p = (await until(() => promptWith(b, '同图去重甲'), '私聊提示', 20_000))!
  expect(p.images).toBe(1)
})

test('场景6 第二条私聊（紧接场景5 的消息）：images 为 0，水位已推进', async () => {
  let img = ''
  const { tg, b } = await setup({ prepare: d => { img = join(d.shared, 'same.png'); writeFileSync(img, PNG) } })
  const ts = Date.now() - 5 * MIN
  const messageId = nextSeedId++
  const imagePath = realpathSync(img)
  for (const observedBy of ['a', 'peer_bot']) {
    seed(b, { ts, message_id: messageId, text: '', attachment_kind: 'photo', image_path: imagePath, observed_by: observedBy })
  }
  tg.pushText(OWNER, '同图水位一')
  await until(() => promptWith(b, '同图水位一'), '第一轮私聊提示', 20_000)
  await until(() => metaGet(b, `group_recap:${DM}`), 'group_recap 水位落盘', 15_000)
  tg.pushText(OWNER, '同图水位二')
  const p2 = (await until(() => promptWith(b, '同图水位二'), '第二轮私聊提示', 20_000))!
  expect(p2.images ?? 0).toBe(0)
})

test('场景7 4 条 31 分钟前的带图行：私聊首轮附 3 张（近况上限），下一轮 0 张', async () => {
  const images: string[] = []
  const { tg, b } = await setup({
    prepare: d => {
      for (let i = 0; i < 4; i++) {
        const p = join(d.shared, `recap-${i}.png`)
        writeFileSync(p, PNG)
        images.push(realpathSync(p))
      }
    },
  })
  const base = Date.now() - 31 * MIN
  for (let i = 0; i < 4; i++) {
    seed(b, { ts: base + i * 1000, text: `四图近况${i}`, attachment_kind: 'photo', image_path: images[i] })
  }
  tg.pushText(OWNER, '四图私聊一')
  const p = (await until(() => promptWith(b, '四图私聊一'), '私聊提示', 20_000))!
  expect(p.images).toBe(3)
  await until(() => metaGet(b, `group_recap:${DM}`), 'group_recap 水位落盘', 15_000)
  tg.pushText(OWNER, '四图私聊二')
  const p2 = (await until(() => promptWith(b, '四图私聊二'), '第二轮私聊提示', 20_000))!
  expect(p2.images ?? 0).toBe(0)
})

test('场景8 私聊 /clear 之后：下一条私聊重新带近况和图', async () => {
  let img = ''
  const { tg, b } = await setup({ prepare: d => { img = join(d.shared, 'recap.png'); writeFileSync(img, PNG) } })
  seedOldImageRow(b, img, '带图近况戊')
  tg.pushText(OWNER, '清前先聊')
  const p1 = (await until(() => promptWith(b, '清前先聊'), '私聊提示', 20_000))!
  expect(p1.images ?? 0).toBeGreaterThanOrEqual(1)
  await until(() => evs(b, 'turn.end').length >= 1, '第一轮结束', 20_000)
  await until(() => metaGet(b, `group_recap:${DM}`), 'group_recap 水位落盘', 15_000)
  tg.pushText(OWNER, '/clear')
  await until(() => isCleared(b, DM), '/clear 清空四个水位', 20_000)
  tg.pushText(OWNER, '清后一问')
  const p2 = (await until(() => promptWith(b, '清后一问'), '清后提示', 20_000))!
  expect(p2.text).toContain('带图近况戊')
  expect(p2.images).toBe(1)
})

test('契约 图边长超 2048：近况不附 image 块（maxSide 接线回归）', async () => {
  // 这条专门守 engine 传给 readSharedImage 的 { maxSide } 接线：删掉那个参数，这条会红。
  // 用 2049×1 的真 PNG——体积很小（不会被 2 MiB 挡掉），只有边长超限这一条路能拒它。
  const PNG_2049 = Buffer.from(
    'iVBORw0KGgoAAAANSUhEUgAACAEAAAABCAYAAABZ1T0kAAAAGUlEQVR42u3BAQEAAACCIP+vbkhAAQAAAAAAAAAAwG0QAAF3sQ2zAAAAAElFTkSuQmCC',
    'base64',
  )
  let img = ''
  const { tg, b } = await setup({ prepare: d => { img = join(d.shared, 'wide.png'); writeFileSync(img, PNG_2049) } })
  seed(b, {
    ts: Date.now() - 31 * MIN, text: '',
    attachment_kind: 'photo', image_path: realpathSync(img),
  })
  tg.pushText(OWNER, '这张宽图')
  const p = (await until(() => promptWith(b, '这张宽图'), '私聊提示', 20_000))!
  expect(p.text).toContain('[图片]')
  expect(p.images ?? 0).toBe(0)
})
