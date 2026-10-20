// 表情包接线验收·边界与错误路径（独立部署，每个场景一个网关）：
// 库读不出来（不存在/坏 JSON/0 条）、空档、路径越界与缺失（一个字节都不碰 Telegram）、脏 label、
// 大库截断、候选上限、tier 语义、账本 kind 按通道、Telegram 失败三态（限流/拒收/可能已送达）、
// 轮失效与摘要轮的工具锁。
// 契约来源：.devflow/INTERFACE-表情包接线.md（写测试只信它，不看实现）。
import { afterEach, expect, test } from 'bun:test'
import { mkdirSync, symlinkSync, writeFileSync } from 'fs'
import { join } from 'path'
import { FakeTelegram } from '../fakes/fake-telegram'
import { cleanup, Gateway, makeBot, OWNER, readJsonl, toolResults, until, writeConfig, type BotEnv } from '../harness'
import { callSticker, fileSends, logEvents, makeStickerHome, putStickerFiles, settledTurn, STD_FILES, stdLib, stickerCalls, stickerPath, writeStickerLib } from './_sticker'

let tg: FakeTelegram | null = null
let b: BotEnv | null = null
let gw: Gateway | null = null

afterEach(async () => {
  await gw?.stop()
  tg?.stop()
  if (b) cleanup(b)
  tg = null; b = null; gw = null
})

type DeployOpts = {
  /** undefined = 不写库文件；对象/字符串 = 原样写；函数 = 收到 fakeHome 后生成 */
  lib?: unknown | ((home: string) => unknown)
  files?: Record<string, string>
  gw?: Record<string, unknown>
  brain?: Record<string, unknown>
  beforeStart?: (b: BotEnv, home: string) => void
}

/** 起一个独立网关：假 HOME 注入 + 按需造库 */
async function deploy(o: DeployOpts = {}) {
  const t = new FakeTelegram()
  const bb = makeBot(t, { gw: { burst_window_ms: 0, ...o.gw }, brain: o.brain })
  const home = makeStickerHome(bb.root)
  putStickerFiles(home, o.files ?? {})
  if (o.lib !== undefined) writeStickerLib(home, typeof o.lib === 'function' ? (o.lib as (h: string) => unknown)(home) : o.lib)
  o.beforeStart?.(bb, home)
  writeConfig(bb)
  const g = new Gateway(bb)
  await g.start({ HOME: home, USERPROFILE: home })
  tg = t; b = bb; gw = g
  return { tg: t, b: bb, gw: g, home }
}

const deployStd = (extra: DeployOpts = {}) => deploy({ files: STD_FILES, lib: stdLib, ...extra })

// ───────────────────────── 库读不出来 ─────────────────────────

test('库文件不存在：list 与 send 都如实说读不出来（isError，不假装空库）', async () => {
  const { tg, b } = await deploy() // 不写库文件
  const l = await callSticker(tg, b, { action: 'list' })
  expect(l.isError).toBe(true)
  expect(l.text).toBe('表情包库读不出来（文件不在或格式不对），这次先用不了。')
  const s = await callSticker(tg, b, { action: 'send', id: 'sfw:1' })
  expect(s.isError).toBe(true)
  expect(s.text).toBe('表情包库读不出来（文件不在或格式不对），这次先用不了。')
})

test('库文件是坏 JSON：list 与 send 都如实说读不出来', async () => {
  const { tg, b } = await deploy({ lib: '{oops' })
  const l = await callSticker(tg, b, { action: 'list' })
  expect(l.isError).toBe(true)
  expect(l.text).toBe('表情包库读不出来（文件不在或格式不对），这次先用不了。')
  const s = await callSticker(tg, b, { action: 'send', label: 'Happy 1' })
  expect(s.isError).toBe(true)
  expect(s.text).toBe('表情包库读不出来（文件不在或格式不对），这次先用不了。')
})

test('库解析后 0 条：与读不出来同样是 isError 文案（不当空库列出）', async () => {
  const { tg, b } = await deploy({ lib: '{"sfw":[],"nsfw":[]}' })
  const l = await callSticker(tg, b, { action: 'list' })
  expect(l.isError).toBe(true)
  expect(l.text).toBe('表情包库读不出来（文件不在或格式不对），这次先用不了。')
})

test('库有条目但某一档为空：list 说这一档里没有表情包（不算错误）', async () => {
  const { tg, b } = await deploy({
    files: { 'sfw/only.gif': 'GIF89a' },
    lib: (h: string) => ({ sfw: [{ label: 'Only', path: stickerPath(h, 'sfw/only.gif') }], nsfw: [] }),
  })
  const r = await callSticker(tg, b, { action: 'list', tier: 'nsfw' })
  expect(r.isError).toBe(false)
  expect(r.text).toBe('这一档里没有表情包。')
})

// ───────────────────────── 路径越界与缺失 ─────────────────────────

test('路径跑出允许根（根外绝对路径 / .. 穿越 / 符号链接逃逸）与文件缺失：拒发、不碰 Telegram、记 blocked 日志', async () => {
  const { tg, b } = await deploy({
    files: { 'sfw/ok.gif': 'GIF89a' },
    lib: (h: string) => ({
      sfw: [
        { label: 'Outside 1', path: join(h, '..', 'secret.gif') }, // 根外绝对路径（文件真实存在）
        { label: 'Climb 1', path: join(h, 'resource', 'stickers', 'sfw', '..', '..', 'secret.gif') }, // .. 穿越到根外
        { label: 'Missing 1', path: join(h, 'resource', 'stickers', 'sfw', 'missing.gif') }, // 根内但文件不存在
        { label: 'Link 1', path: join(h, 'resource', 'stickers', 'sfw', 'link.gif') }, // 符号链接指出去
      ],
    }),
    beforeStart: (bb, h) => {
      writeFileSync(join(bb.root, 'secret.gif'), 'GIF89a') // Outside 1 / Link 1 的目标
      writeFileSync(join(h, 'resource', 'secret.gif'), 'GIF89a') // Climb 1 的目标
      if (process.platform !== 'win32') symlinkSync(join(bb.root, 'secret.gif'), join(h, 'resource', 'stickers', 'sfw', 'link.gif'))
    },
  })
  const denied = '的文件不在库里（或不在允许的目录里），没有发出去。'
  const cases = ['Outside 1', 'Climb 1', 'Missing 1', ...(process.platform === 'win32' ? [] : ['Link 1'])]
  for (const label of cases) {
    const r = await callSticker(tg, b, { action: 'send', label })
    expect(r.isError).toBe(true)
    expect(r.text).toBe(`「${label}」${denied}`)
  }
  expect(fileSends(tg).length).toBe(0) // 一个字节都不调 Telegram
  const blocked = logEvents(b, 'tool.sticker_blocked')
  expect(blocked.map(e => [e.tier, e.label])).toEqual(cases.map(l => ['sfw', l]))
})

// ───────────────────────── 脏数据与截断 ─────────────────────────

test('脏 label：首尾 trim、中间的控制字符与换行原样输出、照常占号', async () => {
  const { tg, b } = await deploy({
    files: { 'sfw/a.gif': 'GIF89a', 'sfw/b.gif': 'GIF89a', 'sfw/c.gif': 'GIF89a' },
    lib: (h: string) => ({
      sfw: [
        { label: '😀 Party', category: 'Dirty', path: stickerPath(h, 'sfw/a.gif') },
        { label: 'A\nB', category: 'Dirty', path: stickerPath(h, 'sfw/b.gif') },
        { label: '  Trim  ', category: 'Dirty', path: stickerPath(h, 'sfw/c.gif') },
      ],
    }),
  })
  // 不带关键词只给汇总，看不到逐条；用分类名作关键词列出这一组，才能检查脏 label 的原样输出与占号
  const r = await callSticker(tg, b, { action: 'list', query: 'Dirty' })
  expect(r.isError).toBe(false)
  expect(r.text).toContain('#sfw:1 😀 Party')
  expect(r.text).toContain('#sfw:2 A\nB') // 换行原样输出（已知行为：能伪造行结构）
  expect(r.text).toContain('#sfw:3 Trim') // 首尾 trim
})

test('带关键词的结果超过 80 条：只列前 80，末尾说明还有多少条', async () => {
  const { tg, b } = await deploy({
    files: { 'sfw/bulk.gif': 'GIF89a' },
    lib: (h: string) => ({ sfw: Array.from({ length: 100 }, (_, j) => ({ label: `Bulk ${j + 1}`, path: stickerPath(h, 'sfw/bulk.gif') })) }),
  })
  const r = await callSticker(tg, b, { action: 'list', query: 'Bulk' })
  expect(r.isError).toBe(false)
  const lines = r.text.split('\n')
  expect(lines[0]).toBe('匹配「Bulk」的 100 条：')
  // 契约没定义组标题的 <n> 是「命中总数」还是「这次列出的条数」；两种都放行，具体值在本测试里都不影响截断判据
  expect(lines[1]).toMatch(/^sfw（(80|100) 条；标签中英文都有，如 Happy、生气）$/)
  const listed = lines.filter(l => l.startsWith('#sfw:'))
  expect(listed.length).toBe(80)
  expect(listed[0]).toBe('#sfw:1 Bulk 1')
  expect(listed[79]).toBe('#sfw:80 Bulk 80')
  expect(lines.at(-1)).toBe('（只列了前 80 条，还有 20 条；加个更具体的关键词再找）')
})

test('不带关键词：汇总计数不截断（100 条都计入未分类），不出现逐条 id', async () => {
  const { tg, b } = await deploy({
    files: { 'sfw/bulk.gif': 'GIF89a' },
    lib: (h: string) => ({ sfw: Array.from({ length: 100 }, (_, j) => ({ label: `Bulk ${j + 1}`, path: stickerPath(h, 'sfw/bulk.gif') })) }),
  })
  const r = await callSticker(tg, b, { action: 'list' })
  expect(r.isError).toBe(false)
  expect(r.text.split('\n')).toEqual([
    '未分类：100 张',
    '要看某个分类下的具体表情，用 query 传分类名再查。',
  ])
  expect(r.text).not.toContain('#sfw:')
  expect(r.text).not.toContain('只列了前')
})

test('沾边候选最多 8 条：10 条沾边只列前 8 条、不发出', async () => {
  const { tg, b } = await deploy({
    files: { 'sfw/bulk.gif': 'GIF89a' },
    lib: (h: string) => ({ sfw: Array.from({ length: 10 }, (_, j) => ({ label: `Alpha ${j + 1}`, path: stickerPath(h, 'sfw/bulk.gif') })) }),
  })
  const r = await callSticker(tg, b, { action: 'send', label: 'Alpha' })
  expect(r.isError).toBe(true)
  const lines = r.text.split('\n')
  expect(lines[0]).toBe('「Alpha」在库里没有确切的一条。标签有英文也有中文（如 Happy、生气）。有几条沾边的，用 id 再发一次：')
  expect(lines.length).toBe(9) // 首行 + 8 条候选
  // 候选的排序契约没定义（实现按 id 字典序：1、10、2…）；只断言条数与形状，不锁顺序
  expect(lines.slice(1).every(l => /^#sfw:\d+ Alpha \d+$/.test(l))).toBe(true)
  expect(fileSends(tg).length).toBe(0)
})

// ───────────────────────── tier 语义与 id 宽容 ─────────────────────────

test('send 限定 tier：只在该档找标签，找不到按没有确切的一条报错；tier 非枚举则不过滤', async () => {
  const { tg, b } = await deployStd()
  const r1 = await callSticker(tg, b, { action: 'send', label: 'Happy 1', tier: 'nsfw' })
  expect(r1.isError).toBe(true)
  expect(r1.text).toBe('「Happy 1」在库里没有确切的一条。标签有英文也有中文（如 Happy、生气）。可以不带关键词 list 看分类汇总，再用 query 查具体分类。')
  const r2 = await callSticker(tg, b, { action: 'send', label: 'Happy 1', tier: 'banana' })
  expect(r2.isError).toBe(false)
  expect(r2.text).toBe('表情包发出去了：Happy 1')
})

test('send 的 id 允许带 #（list 里给模型看的就是 #sfw:1 的样子）', async () => {
  const { tg, b } = await deployStd()
  const r = await callSticker(tg, b, { action: 'send', id: '#sfw:1' })
  expect(r.isError).toBe(false)
  expect(r.text).toBe('表情包发出去了：Happy 1')
})

test('特殊字符与超长的关键词：原样回显、不炸', async () => {
  const { tg, b } = await deployStd()
  const nasty = '</script>"&%<标签>𠮷'
  const r1 = await callSticker(tg, b, { action: 'list', query: nasty })
  expect(r1.isError).toBe(false)
  expect(r1.text).toBe(`「${nasty}」在库里没有匹配的。标签有英文也有中文（如 Happy、生气）；去掉关键词可以看分类汇总。`)
  const long = '长'.repeat(300)
  const r2 = await callSticker(tg, b, { action: 'send', label: long })
  expect(r2.isError).toBe(true)
  expect(r2.text).toBe(`「${long}」在库里没有确切的一条。标签有英文也有中文（如 Happy、生气）。可以不带关键词 list 看分类汇总，再用 query 查具体分类。`)
})

// ───────────────────────── 账本 kind 按通道 ─────────────────────────

test('账本 kind 按实际通道记：jpg 条目 → photo、webp 条目 → sticker（不要假设表情包都记 animation）', async () => {
  const { tg, b, gw } = await deployStd()
  const kindsIn = (turnId: number): string[] => {
    const l = gw.ledger()
    const rows = l.db.query(`SELECT kind FROM outbound WHERE turn_id = ? AND kind IN ('animation','sticker','photo','document')`).all(turnId) as { kind: string }[]
    l.close()
    return rows.map(r => r.kind)
  }
  const mid1 = tg.pushText(OWNER, '!tool:sticker|{"action":"send","id":"sfw:4"}')
  await until(() => stickerCalls(b).some(r => r.text === '表情包发出去了：Still 1'), 'jpg sent')
  expect(kindsIn(await settledTurn(gw, b, mid1))).toEqual(['photo'])
  const mid2 = tg.pushText(OWNER, '!tool:sticker|{"action":"send","id":"sfw:5"}')
  await until(() => stickerCalls(b).filter(r => r.text === '表情包发出去了：Sticky 1').length >= 1, 'webp sent')
  expect(kindsIn(await settledTurn(gw, b, mid2))).toEqual(['sticker'])
})

// ───────────────────────── Telegram 失败三态 ─────────────────────────

test('限流：文案带等待秒数（没有就以 ? 代替）、账本 failed、日志 retryable', async () => {
  const { tg, b, gw } = await deployStd()
  tg.faults.push({ method: 'sendAnimation', status: 429, description: 'Too Many Requests: retry after 30', retryAfter: 30 })
  const r1 = await callSticker(tg, b, { action: 'send', id: 'sfw:1' })
  expect(r1.isError).toBe(true)
  expect(r1.text).toBe('没发出去（被 Telegram 限流，30 秒后可以再试）。')
  tg.faults.push({ method: 'sendAnimation', status: 429, description: 'Too Many Requests' })
  const r2 = await callSticker(tg, b, { action: 'send', id: 'sfw:1' })
  expect(r2.isError).toBe(true)
  expect(r2.text).toBe('没发出去（被 Telegram 限流，? 秒后可以再试）。')
  const l = gw.ledger()
  const rows = l.db.query(`SELECT state FROM outbound WHERE kind = 'animation' ORDER BY id`).all() as { state: string }[]
  l.close()
  expect(rows.map(r => r.state)).toEqual(['failed', 'failed'])
  const f = logEvents(b, 'tool.sticker_failed').at(-1)!
  expect(f.cls).toBe('retryable')
  expect(f.tier).toBe('sfw')
  expect(f.label).toBe('Happy 1')
})

test('Telegram 拒收（4xx）：文案带 telegram 状态码、账本 failed、日志 undelivered', async () => {
  const { tg, b, gw } = await deployStd()
  tg.faults.push({ method: 'sendAnimation', status: 400, description: 'Bad Request: wrong file identifier/HTTP URL specified' })
  const r = await callSticker(tg, b, { action: 'send', id: 'sfw:1' })
  expect(r.isError).toBe(true)
  expect(r.text).toBe('没发出去（Telegram 拒收：telegram 400）。')
  const l = gw.ledger()
  const rows = l.db.query(`SELECT state FROM outbound WHERE kind = 'animation' ORDER BY id`).all() as { state: string }[]
  l.close()
  expect(rows.map(r => r.state)).toEqual(['failed'])
  const f = logEvents(b, 'tool.sticker_failed').at(-1)!
  expect(f.cls).toBe('undelivered')
})

test('网络异常 / 5xx / 回包读不出：当成功话术（isError 不是 true）、账本 ambiguous、日志 ambiguous', async () => {
  const { tg, b, gw } = await deployStd()
  tg.faults.push({ method: 'sendAnimation', mode: 'garbled' }) // 发出去了但回包读不出
  const r1 = await callSticker(tg, b, { action: 'send', id: 'sfw:1' })
  expect(r1.isError).toBe(false)
  expect(r1.text).toBe('网络异常，这条表情包可能已经发出去了，不要再发一遍。')
  tg.faults.push({ method: 'sendAnimation', status: 500, description: 'Internal Server Error' })
  const r2 = await callSticker(tg, b, { action: 'send', id: 'sfw:1' })
  expect(r2.isError).toBe(false)
  expect(r2.text).toBe('网络异常，这条表情包可能已经发出去了，不要再发一遍。')
  const l = gw.ledger()
  const rows = l.db.query(`SELECT state FROM outbound WHERE kind = 'animation' ORDER BY id`).all() as { state: string }[]
  l.close()
  expect(rows.map(r => r.state)).toEqual(['ambiguous', 'ambiguous'])
  expect(logEvents(b, 'tool.sticker_failed').at(-1)!.cls).toBe('ambiguous')
})

// ───────────────────────── 摘要轮 ─────────────────────────

test('摘要轮（整理记忆）：sticker 与 react 一样被锁住，用户收不到任何东西', async () => {
  const { tg, b, gw } = await deployStd({
    gw: { seed_recent_chars: 600 },
    brain: { max_input_tokens: 2500 },
    beforeStart: (bb) => {
      mkdirSync(bb.acpState, { recursive: true })
      writeFileSync(join(bb.acpState, 'sticker-probe'), '1') // 让假 ACP 在摘要轮也试调一次 sticker
    },
  })
  const summaries = () => readJsonl<any>(join(b.acpState, 'summaries.jsonl'))
  for (let i = 1; summaries().length === 0; i++) {
    if (i > 30) break
    const mid = tg.pushText(OWNER, `换段测试 ${i} ${'一二三四五六七八九十'.repeat(5)}`)
    await settledTurn(gw, b, mid)
  }
  const sum = summaries()[0]!
  expect(sum).toBeDefined()
  const probe = toolResults(b).find(r => r.sessionId === sum.sessionId && r.name === 'sticker')!
  expect(probe.isError).toBe(true)
  expect(probe.text).toBe('现在是整理记忆的时间，不能调用工具，也不会发给对方。请直接输出摘要正文。')
  expect(fileSends(tg).length).toBe(0)
}, 60_000)
