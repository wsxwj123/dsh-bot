// 表情包接线验收（假 ACP e2e）：runtimeRules 文案、MCP 工具表、list / send 的确切文案与副作用、
// 假 HOME 库注入、假 TG 的 animation / sticker 通道、账本与日志信号、重复调用的去重边界。
// 契约来源：.devflow/INTERFACE-表情包接线.md（写测试只信它，不看实现）。
import { afterAll, beforeAll, expect, test } from 'bun:test'
import { readFileSync, realpathSync } from 'fs'
import { join } from 'path'
import { FakeTelegram } from '../fakes/fake-telegram'
import { cleanup, Gateway, makeBot, OWNER, sleep, toolResults, until, writeConfig, type BotEnv } from '../harness'
import { callSticker, fileSends, logEvents, makeStickerHome, putStickerFiles, settledTurn, STD_FILES, stdLib, stickerCalls, stickerPath, writeStickerLib } from './_sticker'

/** INTERFACE §1 的其余 9 条 bullet（除 sticker 条），逐字 */
const OTHER_9_BULLETS = [
  '- 你在 Telegram 上和对方聊天。对方只能看到你用 mcp__tg__reply 发出的内容，你直接输出的文字对方看不到。',
  '- 一次 reply 可以写几段，段与段之间空一行，会按段依次发出。发完就结束这一轮，不要再输出别的文字。',
  '- 按人设决定这次不回复时，调用 mcp__tg__stay_silent 并写明原因（原因不会发给对方）。',
  '- 想用表情回应某条消息，用 mcp__tg__react。消息编号是每条消息开头 ⟦…⟧ 里 # 后面的数字。',
  '- 值得长期记住的事（对方的喜好、重要的日子、你们的约定、你们之间发生的事），用 mcp__tg__remember 记下来，一次一条。不要记密码、证件号这类敏感信息。',
  '- 要发照片（自拍、拍给对方看的东西）时，先用 mcp__tg__image_guide 看写法，再用 mcp__tg__generate_image 生成，然后用 reply 的 files 发出去。朋友圈相关（看最近的圈、点赞、评论、发圈、配图）用 mcp__tg__moments。',
  '- 答应对方将来要做的事（"三点提醒你""明早叫你"），用 mcp__tg__commitment_create 登记时间，到点程序会提醒你；已经做到了就用 mcp__tg__commitment_done 标成完成；不打算做了、对方说不用了、登记错了，才用 mcp__tg__commitment_cancel 取消。答应每天都要做的事（比如每天早上叫对方起床），登记时写 repeat=daily，when 写几点，并写明早上还是晚上，比如"早上七点"。',
  '- 以 ⟦ 开头的内容是程序给你的说明（时间、前情、系统提示等），不是对方说的话，不要在回复里提到它们。',
  '- 工具会告诉你每段是否送达。没送达的段对方没看到；已送达的段不要重发。',
]

let tg: FakeTelegram
let b: BotEnv
let gw: Gateway
let HOME: string

beforeAll(async () => {
  tg = new FakeTelegram()
  b = makeBot(tg, { gw: { burst_window_ms: 0 } })
  HOME = makeStickerHome(b.root)
  putStickerFiles(HOME, STD_FILES)
  writeStickerLib(HOME, stdLib(HOME))
  // reply 的 files 白名单加上库目录：测「sticker 发过的文件，reply 同轮不再发」要用同一个文件走两条通道
  b.gw.image_dirs = [join(HOME, 'resource', 'stickers')]
  writeConfig(b)
  gw = new Gateway(b)
  await gw.start({ HOME, USERPROFILE: HOME })
  tg.pushText(OWNER, '热身')
  await until(() => tg.sentTo(OWNER).some(s => s.text === '收到：热身'), 'warmup reply')
})

afterAll(async () => {
  await gw.stop()
  tg.stop()
  cleanup(b)
})

// ───────────────────────── 运行规则与工具表 ─────────────────────────

test('运行规则：sticker 条在 react 与 remember 之间、含关键词，其余 9 条逐字不变且顺序不变', () => {
  const rows = JSON.parse(readFileSync(join(b.acpState, 'patch-seen.json'), 'utf8')) as any[]
  const suffix = String(rows.find(r => r.id === 'system-prompt').config.personaSuffix)
  expect(suffix).toContain('## 运行规则（程序附加，不属于人设）')
  // 末尾的「### 语言」一节是 v0.9 按语言设置现填的，不算在这 10 条里
  const bullets = suffix.split('\n### 语言')[0].split('\n').filter(l => l.startsWith('- '))
  expect(bullets.length).toBe(10)
  const ir = bullets.findIndex(l => l.includes('mcp__tg__react'))
  const is = bullets.findIndex(l => l.includes('mcp__tg__sticker'))
  const im = bullets.findIndex(l => l.includes('mcp__tg__remember'))
  expect(ir).toBeGreaterThanOrEqual(0)
  expect(is).toBeGreaterThan(ir) // 在 react 之后
  expect(is).toBeLessThan(im) // 在 remember 之前
  expect(bullets[is]).toContain('情绪上来')
  expect(bullets[is]).toContain('skill-gif')
  expect(bullets.filter((_, i) => i !== is)).toEqual(OTHER_9_BULLETS)
})

test('MCP 工具表：sticker 的 required / enum / 参数类型与说明要点', async () => {
  const sess = Object.values(JSON.parse(readFileSync(join(b.acpState, 'sessions.json'), 'utf8'))) as any[]
  const mcp = sess.map(s => s.mcp).find(m => m)!
  const headers: Record<string, string> = { 'content-type': 'application/json' }
  for (const h of mcp.headers) headers[h.name] = h.value
  const res = await fetch(mcp.url, { method: 'POST', headers, body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/list' }) })
  const body = await res.json() as any
  const st = (body.result.tools as any[]).find(t => t.name === 'sticker')
  expect(st).toBeDefined()
  const desc = String(st.description)
  for (const k of ['发表情包', 'list', 'send', 'sfw', 'nsfw', '人设']) expect(desc).toContain(k)
  expect(st.inputSchema.required).toEqual(['action'])
  expect(st.inputSchema.properties.action.enum).toEqual(['list', 'send'])
  expect(st.inputSchema.properties.query.type).toBe('string')
  expect(st.inputSchema.properties.id.type).toBe('string')
  expect(st.inputSchema.properties.tier.enum).toEqual(['sfw', 'nsfw'])
})

// ───────────────────────── list ─────────────────────────

test('list 不给关键词：分类汇总按数量降序、无分类归未分类、末尾提示 query，不含逐条条目与本地路径', async () => {
  const r = await callSticker(tg, b, { action: 'list' })
  expect(r.isError).toBe(false)
  expect(r.text.split('\n')).toEqual([
    '未分类：7 张',
    'Forest：1 张',
    '要看某个分类下的具体表情，用 query 传分类名再查。',
  ])
  expect(r.text).not.toContain('#sfw:')
  expect(r.text).not.toContain('#nsfw:')
  expect(r.text).not.toContain('表情包库，共')
  expect(r.text).not.toContain(HOME)
  for (const ext of ['.gif', '.jpg', '.webp']) expect(r.text).not.toContain(ext)
})

test('list 带关键词：完全相等的排最前，其余同分顺序可变，只列命中的档', async () => {
  const r = await callSticker(tg, b, { action: 'list', query: 'Happy' })
  expect(r.isError).toBe(false)
  const lines = r.text.split('\n')
  expect(lines.slice(0, 2)).toEqual([
    '匹配「Happy」的 3 条：',
    'sfw（3 条；标签中英文都有，如 Happy、生气）',
  ])
  // 精确命中必在第一条；其余两条同分，顺序不再固定，按集合比较
  expect(lines[2]).toBe('#sfw:3 Happy')
  expect(new Set(lines.slice(3))).toEqual(new Set(['#sfw:1 Happy 1', '#sfw:2 Happy 2']))
  expect(lines.length).toBe(5)
})

test('list 关键词拆词：全部词都命中才列出（只命中一部分的不列）', async () => {
  const r = await callSticker(tg, b, { action: 'list', query: 'ppy 2' })
  expect(r.isError).toBe(false)
  expect(r.text.split('\n')).toEqual([
    '匹配「ppy 2」的 1 条：',
    'sfw（1 条；标签中英文都有，如 Happy、生气）',
    '#sfw:2 Happy 2',
  ])
})

test('list 限定 nsfw：不带关键词时汇总只统计 nsfw 档', async () => {
  const r = await callSticker(tg, b, { action: 'list', tier: 'nsfw' })
  expect(r.isError).toBe(false)
  expect(r.text.split('\n')).toEqual([
    'Forest：1 张',
    '要看某个分类下的具体表情，用 query 传分类名再查。',
  ])
  expect(r.text).not.toContain('未分类')
  expect(r.text).not.toContain('#sfw:')
  expect(r.text).not.toContain('#nsfw:')
})

test('list 的 tier 给非枚举值：不过滤，两档都计入汇总', async () => {
  const r = await callSticker(tg, b, { action: 'list', tier: 'banana' })
  expect(r.isError).toBe(false)
  expect(r.text.split('\n')).toEqual([
    '未分类：7 张',
    'Forest：1 张',
    '要看某个分类下的具体表情，用 query 传分类名再查。',
  ])
})

test('list 关键词一条不沾边：说明标签语言、提示去掉关键词看分类汇总，不算错误', async () => {
  const r = await callSticker(tg, b, { action: 'list', query: 'zzzz' })
  expect(r.isError).toBe(false)
  expect(r.text).toBe('「zzzz」在库里没有匹配的。标签有英文也有中文（如 Happy、生气）；去掉关键词可以看分类汇总。')
})

test('list 关键词是空串：当没给关键词，输出分类汇总', async () => {
  const r = await callSticker(tg, b, { action: 'list', query: '' })
  expect(r.isError).toBe(false)
  expect(r.text.split('\n')).toEqual([
    '未分类：7 张',
    'Forest：1 张',
    '要看某个分类下的具体表情，用 query 传分类名再查。',
  ])
  expect(r.text).not.toContain('匹配「')
  expect(r.text).not.toContain('#sfw:')
})

// ───────────────────────── send 主路径 ─────────────────────────

test('send 按 id 发 gif：成功文案、走 sendAnimation、multipart 文件名正确、不带 caption', async () => {
  const before = tg.sentTo(OWNER).length
  const r = await callSticker(tg, b, { action: 'send', id: 'sfw:1' })
  expect(r.isError).toBe(false)
  expect(r.text).toBe('表情包发出去了：Happy 1')
  const anims = tg.sentTo(OWNER).slice(before).filter(s => s.method === 'sendAnimation')
  expect(anims.length).toBe(1)
  expect(String(anims[0]!.file)).toContain('a.gif')
  expect(anims[0]!.text ?? '').toBe('')
  const call = tg.calls.find(c => c.method === 'sendAnimation' && String(c.params.animation ?? '').includes('a.gif'))!
  expect(String(call.params.animation)).toBe('<file a.gif 6B>')
  expect(call.params.caption ?? '').toBe('')
})

test('send 按标签的一部分找（子串唯一命中）：jpg 条目走 sendPhoto', async () => {
  const before = tg.sentTo(OWNER).length
  const r = await callSticker(tg, b, { action: 'send', label: 'still' })
  expect(r.isError).toBe(false)
  expect(r.text).toBe('表情包发出去了：Still 1')
  const photos = tg.sentTo(OWNER).slice(before).filter(s => s.method === 'sendPhoto')
  expect(photos.length).toBe(1)
  expect(String(photos[0]!.file)).toContain('c.jpg')
})

test('send 用 query 当标签：webp 条目走 sendSticker 通道', async () => {
  const before = tg.sentTo(OWNER).length
  const r = await callSticker(tg, b, { action: 'send', query: 'Sticky 1' })
  expect(r.isError).toBe(false)
  expect(r.text).toBe('表情包发出去了：Sticky 1')
  const sts = tg.sentTo(OWNER).slice(before).filter(s => s.method === 'sendSticker')
  expect(sts.length).toBe(1)
  expect(String(sts[0]!.file)).toContain('d.webp')
})

test('send 同名多条：取第 1 条，文案说明同名条数与换发办法', async () => {
  const before = tg.sentTo(OWNER).length
  const r = await callSticker(tg, b, { action: 'send', label: 'Same 1' })
  expect(r.isError).toBe(false)
  expect(r.text).toBe('表情包发出去了：Same 1（「Same 1」同名 2 条，这次发的是第 1 条；要别的按 list 里的 id 发）')
  const anims = tg.sentTo(OWNER).slice(before).filter(s => s.method === 'sendAnimation')
  expect(anims.length).toBe(1)
  expect(String(anims[0]!.file)).toContain('e.gif') // 第 1 条（sfw:6）
})

test('send id 与 label 同时给：按 id 精确找，label 不参与', async () => {
  const before = tg.sentTo(OWNER).length
  const r = await callSticker(tg, b, { action: 'send', id: 'sfw:4', label: 'Happy 1' })
  expect(r.isError).toBe(false)
  expect(r.text).toBe('表情包发出去了：Still 1')
  const photos = tg.sentTo(OWNER).slice(before).filter(s => s.method === 'sendPhoto')
  expect(photos.length).toBe(1)
  expect(String(photos[0]!.file)).toContain('c.jpg')
})

test('send 的 id 不在库里：报错、不给候选、不回退 label、不发出', async () => {
  const before = fileSends(tg).length
  const r = await callSticker(tg, b, { action: 'send', id: 'sfw:99', label: 'Happy 1' })
  expect(r.isError).toBe(true)
  expect(r.text).toBe('id sfw:99 不在库里（tier 只能是 sfw / nsfw）。可以不带关键词 list 看分类汇总，再用 query 查具体分类。')
  expect(r.text).not.toContain('#')
  await sleep(200)
  expect(fileSends(tg).length).toBe(before)
})

test('send 的 label 无确切、有沾边：列候选（#id label）、不发出、isError', async () => {
  const before = fileSends(tg).length
  const r = await callSticker(tg, b, { action: 'send', label: 'happ' })
  expect(r.isError).toBe(true)
  expect(r.text.split('\n')).toEqual([
    '「happ」在库里没有确切的一条。标签有英文也有中文（如 Happy、生气）。有几条沾边的，用 id 再发一次：',
    '#sfw:1 Happy 1',
    '#sfw:2 Happy 2',
    '#sfw:3 Happy',
  ])
  await sleep(200)
  expect(fileSends(tg).length).toBe(before)
})

test('send 的 label 既无确切也无沾边：报错、不发出', async () => {
  const before = fileSends(tg).length
  const r = await callSticker(tg, b, { action: 'send', label: 'zzzz' })
  expect(r.isError).toBe(true)
  expect(r.text).toBe('「zzzz」在库里没有确切的一条。标签有英文也有中文（如 Happy、生气）。可以不带关键词 list 看分类汇总，再用 query 查具体分类。')
  await sleep(200)
  expect(fileSends(tg).length).toBe(before)
})

test('send 既没给 id 也没给 label：报错要参数', async () => {
  const r = await callSticker(tg, b, { action: 'send' })
  expect(r.isError).toBe(true)
  expect(r.text).toBe('要给 id（list 里的 #sfw:12 这种）或者 label（标签，可以只写一部分）。')
})

test('action 是 list / send 之外的值：报错说明取值', async () => {
  const r = await callSticker(tg, b, { action: 'delete' })
  expect(r.isError).toBe(true)
  expect(r.text).toBe('action 只能是 list 或 send。')
})

test('参数缺省或类型不对：action 缺省/空串/数字/布尔都按 list 处理，不报错', async () => {
  const n = stickerCalls(b).length
  tg.pushText(OWNER, [
    '!tool:sticker|{}',
    '!tool:sticker|{"action":""}',
    '!tool:sticker|{"action":1}',
    '!tool:sticker|{"action":true}',
    '!tool:sticker|{"id":12,"query":true}',
  ].join('\n'))
  await until(() => stickerCalls(b).length >= n + 5, '5 sticker results')
  for (const r of stickerCalls(b).slice(n, n + 5)) {
    expect(r.isError).toBe(false)
    expect(r.text.split('\n')).toEqual([
      '未分类：7 张',
      'Forest：1 张',
      '要看某个分类下的具体表情，用 query 传分类名再查。',
    ])
  }
})

test('发送通道按扩展名选：.GIF（大写）→ sendAnimation、jpg → sendPhoto、webp → sendSticker', async () => {
  const before = tg.sentTo(OWNER).length
  const n = stickerCalls(b).length
  tg.pushText(OWNER, [
    '!tool:sticker|{"action":"send","id":"sfw:2"}', // b.GIF：扩展名大写
    '!tool:sticker|{"action":"send","id":"sfw:4"}', // c.jpg
    '!tool:sticker|{"action":"send","id":"sfw:5"}', // d.webp
  ].join('\n'))
  await until(() => stickerCalls(b).length >= n + 3, '3 sticker results')
  expect(stickerCalls(b).slice(n, n + 3).map(r => r.text)).toEqual(['表情包发出去了：Happy 2', '表情包发出去了：Still 1', '表情包发出去了：Sticky 1'])
  const sent = tg.sentTo(OWNER).slice(before).filter(s => ['sendAnimation', 'sendPhoto', 'sendSticker'].includes(s.method))
  expect(sent.map(s => s.method)).toEqual(['sendAnimation', 'sendPhoto', 'sendSticker'])
  expect(String(sent[0]!.file)).toContain('b.GIF')
  expect(String(sent[1]!.file)).toContain('c.jpg')
  expect(String(sent[2]!.file)).toContain('d.webp')
})

// ───────────────────────── 账本与日志 ─────────────────────────

test('send 成功：账本记 animation/sent（realpath 绝对路径、对上 TG 消息号），这一轮算已回复、无补发催促', async () => {
  const mid = tg.pushText(OWNER, '!tool:sticker|{"action":"send","id":"sfw:1"}')
  await until(() => stickerCalls(b).some(r => r.text === '表情包发出去了：Happy 1'), 'sticker sent')
  const turnId = await settledTurn(gw, b, mid)
  const l = gw.ledger()
  const rows = l.db.query(`SELECT * FROM outbound WHERE turn_id = ? AND kind = 'animation'`).all(turnId) as any[]
  expect(rows.length).toBe(1)
  const row = rows[0]!
  expect(row.chat_id).toBe(String(OWNER))
  expect(row.state).toBe('sent')
  expect(row.part).toBe(0)
  expect(row.file).toBe(realpathSync(stickerPath(HOME, 'sfw/a.gif')))
  expect(Number(row.tg_message_id)).toBe(tg.sentTo(OWNER).filter(s => s.method === 'sendAnimation').at(-1)!.messageId)
  expect(l.deliveredInChain(turnId)).toBeGreaterThanOrEqual(1)
  const nudge = l.db.query(`SELECT COUNT(*) AS n FROM turns WHERE kind = 'nudge'`).get() as { n: number }
  expect(nudge.n).toBe(0)
  l.close()
})

test('日志：每次 list 都记一条（query/tier/hits），send 成功记 tier/label/kind', async () => {
  const n0 = logEvents(b, 'tool.sticker_list').length
  await callSticker(tg, b, { action: 'list', query: 'Happy', tier: 'nsfw' })
  expect(logEvents(b, 'tool.sticker_list').length - n0).toBe(1) // 这次调用就该写一条
  const lst1 = logEvents(b, 'tool.sticker_list').at(-1)!
  expect(lst1.query).toBe(true)
  expect(lst1.tier).toBe('nsfw')
  expect(lst1.hits).toBe(0)
  const n1 = logEvents(b, 'tool.sticker_list').length
  await callSticker(tg, b, { action: 'list' })
  expect(logEvents(b, 'tool.sticker_list').length - n1).toBe(1)
  const lst2 = logEvents(b, 'tool.sticker_list').at(-1)!
  expect(lst2.query).toBe(false)
  expect(lst2.tier).toBe(null)
  expect(lst2.hits).toBe(8)
  await callSticker(tg, b, { action: 'send', id: 'sfw:1' })
  const snd = logEvents(b, 'tool.sticker_send').at(-1)!
  expect(snd.tier).toBe('sfw')
  expect(snd.label).toBe('Happy 1')
  expect(snd.kind).toBe('animation')
})

// ───────────────────────── 重复与去重 ─────────────────────────

test('同一轮两次 send 同一条：不去重，实发两条、账本两行', async () => {
  const before = fileSends(tg).length
  const line = '!tool:sticker|{"action":"send","id":"sfw:1"}'
  const mid = tg.pushText(OWNER, [line, line].join('\n'))
  await until(() => stickerCalls(b).filter(r => r.text === '表情包发出去了：Happy 1').length >= 2, 'two sticker sends')
  const turnId = await settledTurn(gw, b, mid)
  expect(fileSends(tg).length - before).toBe(2)
  const l = gw.ledger()
  const n = (l.db.query(`SELECT COUNT(*) AS n FROM outbound WHERE turn_id = ? AND kind = 'animation'`).get(turnId) as { n: number }).n
  l.close()
  expect(n).toBe(2)
})

test('sticker 发过的文件，同一轮 reply 的 files 不再重发（本轮实际新增只有一条）', async () => {
  const countA = () => tg.sentTo(OWNER).filter(s => String(s.file ?? '').includes('a.gif')).length
  const before = countA()
  const beforeReply = toolResults(b).filter(r => r.name === 'reply').length
  const n = stickerCalls(b).length
  const mid = tg.pushText(OWNER, [
    '!tool:sticker|{"action":"send","id":"sfw:1"}',
    `!file:${stickerPath(HOME, 'sfw/a.gif')}`,
  ].join('\n'))
  await until(() => toolResults(b).filter(r => r.name === 'reply').length > beforeReply, 'reply ran')
  expect(stickerCalls(b)[n]!.text).toBe('表情包发出去了：Happy 1') // 这一轮的 sticker 确实发出去了
  await settledTurn(gw, b, mid)
  await sleep(200)
  expect(countA() - before).toBe(1)
})
