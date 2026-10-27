// cross-chat-send 验收测试（黑盒，依据 .devflow/cross-chat-send-接口.md 第 3 节 22 条行为清单 + 第 2 节错误契约）。
// 只通过对外接口：配置文件起网关子进程、假 Telegram 收发、假 ACP 的 !tool: 指令构造工具调用、
// 本机 /v1/inject 触发群回合、读写 access.json 与群记录文件、读网关日志。
// 注释里的「清单 N」对应接口文件第 3 节编号；错误契约单独标「契约」。
import { expect, test } from 'bun:test'
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { FakeTelegram } from '../fakes/fake-telegram'
import { logEvents, OWNER, FRIEND, readJsonl, sleep, until, withBot, type BotEnv } from './_acc'

const GROUP = -1001777
const GROUP2 = -1002666
const NEW_GROUP = -1008888
const UNREG_GROUP = -1009999
const UNREG_DM = 7777777

type TR = { name: string; args: any; text: string; isError: boolean }
const trs = (b: BotEnv) => readJsonl<TR>(join(b.acpState, 'tool-results.jsonl'))

/** access.json 的默认形状：两个私聊 + 给定的群 */
const accessWith = (groups: number[], allow: number[] = [OWNER, FRIEND]) => ({
  dmPolicy: 'allowlist',
  allowFrom: allow.map(String),
  groups: Object.fromEntries(groups.map(g => [String(g), { allowFrom: [] }])),
  splitOnParagraph: true,
  paragraphDelay: 0,
})

const accessPath = (b: BotEnv) => join(b.channelDir, 'access.json')

function setFeatures(b: BotEnv, features: Record<string, boolean>): void {
  const cfg = JSON.parse(readFileSync(b.configPath, 'utf8'))
  cfg.features = { ...(cfg.features ?? {}), ...features }
  writeFileSync(b.configPath, JSON.stringify(cfg))
}

function rewriteAccess(b: BotEnv, edit: (a: any) => void): void {
  const a = JSON.parse(readFileSync(accessPath(b), 'utf8'))
  edit(a)
  writeFileSync(accessPath(b), JSON.stringify(a))
}

/** 这一轮里工具结果的基线 */
const baseOf = (b: BotEnv) => trs(b).length

/** 等本轮工具结果落盘（超时变成断言失败，而不是抛超时异常） */
async function collect(b: BotEnv, base: number, pred: (r: TR) => boolean, ms = 20_000): Promise<TR[]> {
  await until(() => (trs(b).length > base ? true : null), '本轮工具结果', ms).catch(() => {})
  return trs(b).slice(base).filter(pred)
}

/** 等某个聊天收到含 marker 的消息（超时后返回已有列表，交给断言） */
async function sentTo(tg: FakeTelegram, chat: number | string, marker: string, ms = 15_000) {
  await until(() => (tg.sentTo(chat).some(s => (s.text ?? '').includes(marker)) ? true : null), `发到 ${chat} 的消息「${marker}」`, ms).catch(() => {})
  return tg.sentTo(chat).filter(s => (s.text ?? '').includes(marker))
}

/** 私聊回合：主人说一句，假 ACP 按里面的 !tool: 指令调用工具后自动回复 */
async function userTurn(b: BotEnv, tg: FakeTelegram, text: string, pred: (r: TR) => boolean, ms = 20_000): Promise<TR[]> {
  const base = baseOf(b)
  tg.pushText(OWNER, text)
  return collect(b, base, pred, ms)
}

const replyTool = (text: string, extra: Record<string, unknown> = {}) => `!tool:reply|${JSON.stringify({ text, ...extra })}`
const listChatsTool = '!tool:list_chats|{}'

const groupsDir = (b: BotEnv) => join(b.root, 'groups')
const groupRows = (b: BotEnv, chat: number) => readJsonl<Record<string, any>>(join(groupsDir(b), `${chat}.jsonl`))

// ───────── 行为清单 ─────────

test('清单1 不传 chat_id：发到当前聊天，文本与改动前一致', async () => {
  await withBot({ access: accessWith([GROUP]), gw: { burst_window_ms: 0 }, before: b => setFeatures(b, { groups: true }) }, async ({ tg }) => {
    tg.pushText(OWNER, '清单一普通发送')
    const hits = await sentTo(tg, OWNER, '收到：清单一普通发送')
    expect(hits).toHaveLength(1)
    expect(hits[0]!.method).toBe('sendMessage')
    expect(hits[0]!.text).toBe('收到：清单一普通发送')
  })
})

test('清单2 chat_id 与当前聊天相同（带首尾空格）：发到当前聊天，不报错', async () => {
  await withBot({ access: accessWith([GROUP]), gw: { burst_window_ms: 0 }, before: b => setFeatures(b, { groups: true }) }, async ({ tg, b }) => {
    const got = await userTurn(b, tg, `清单二 ${replyTool('清单二同聊正文', { chat_id: ` ${OWNER} ` })}`, r => r.name === 'reply' && r.args?.text === '清单二同聊正文')
    expect(got).toHaveLength(1)
    expect(got[0]!.isError).toBe(false)
    const hits = await sentTo(tg, OWNER, '清单二同聊正文')
    expect(hits).toHaveLength(1)
    expect(hits[0]!.chatId).toBe(String(OWNER))
  })
})

test('清单3 已登记群且群聊开启：发到目标群，目标群记录多一行，当前群记录没有', async () => {
  await withBot({ access: accessWith([GROUP, GROUP2]), gw: { burst_window_ms: 0 }, before: b => setFeatures(b, { groups: true }) }, async ({ tg, b, gw }) => {
    const base = baseOf(b)
    const r = await gw.call('/v1/inject', {
      chat_id: String(GROUP), source: 'director', key: `k-${crypto.randomUUID()}`,
      text: `⟦群聊·导演点到你了⟧ 群里最近的对话：清单三 !say:本地回复 ${replyTool('跨聊正文三号', { chat_id: String(GROUP2) })}`,
    })
    expect(r.status).toBe(200)
    const got = await collect(b, base, x => x.args?.text === '跨聊正文三号')
    expect(got).toHaveLength(1)
    expect(got[0]!.isError).toBe(false)
    const hits = await sentTo(tg, GROUP2, '跨聊正文三号')
    expect(hits).toHaveLength(1)
    expect(hits[0]!.chatId).toBe(String(GROUP2))
    const rows = groupRows(b, GROUP2).filter(x => String(x.text ?? '').includes('跨聊正文三号'))
    expect(rows).toHaveLength(1)
    const currentRows = groupRows(b, GROUP).filter(x => String(x.text ?? '').includes('跨聊正文三号'))
    expect(currentRows).toHaveLength(0)
  })
})

test('清单4 跨聊天成功的返回文案包含「当前聊天里的人看不到」', async () => {
  await withBot({ access: accessWith([GROUP]), gw: { burst_window_ms: 0 }, before: b => setFeatures(b, { groups: true }) }, async ({ tg, b }) => {
    const got = await userTurn(b, tg, `清单四 ${replyTool('清单四跨聊正文', { chat_id: String(GROUP) })}`, r => r.args?.text === '清单四跨聊正文')
    expect(got).toHaveLength(1)
    expect(got[0]!.isError).toBe(false)
    expect(got[0]!.text).toContain('当前聊天里的人看不到')
  })
})

test('清单5 目标是已登记私聊：发到该私聊，不写任何群记录', async () => {
  await withBot({ access: accessWith([GROUP]), gw: { burst_window_ms: 0 }, before: b => setFeatures(b, { groups: true }) }, async ({ tg, b }) => {
    const got = await userTurn(b, tg, `清单五 ${replyTool('清单五私聊正文', { chat_id: String(FRIEND) })}`, r => r.args?.text === '清单五私聊正文')
    expect(got).toHaveLength(1)
    expect(got[0]!.isError).toBe(false)
    const hits = await sentTo(tg, FRIEND, '清单五私聊正文')
    expect(hits).toHaveLength(1)
    expect(hits[0]!.chatId).toBe(String(FRIEND))
    const files = existsSync(groupsDir(b)) ? readdirSync(groupsDir(b)).filter(f => f.endsWith('.jsonl')) : []
    expect(files).toEqual([])
  })
})

test('清单6 群号但群聊功能关闭：E_GROUPS_OFF，目标群零发送', async () => {
  await withBot({ access: accessWith([GROUP]), gw: { burst_window_ms: 0 }, before: b => setFeatures(b, { groups: false }) }, async ({ tg, b }) => {
    const got = await userTurn(b, tg, `清单六 ${replyTool('清单六正文', { chat_id: String(GROUP) })}`, r => r.args?.text === '清单六正文')
    expect(got).toHaveLength(1)
    expect(got[0]!.isError).toBe(true)
    expect(got[0]!.text).toContain('E_GROUPS_OFF')
    expect(got[0]!.text).toContain('没有发出：')
    expect(tg.sentTo(GROUP)).toHaveLength(0)
  })
})

test('清单7 未登记的群号：E_NOT_REGISTERED，零发送', async () => {
  await withBot({ access: accessWith([GROUP]), gw: { burst_window_ms: 0 }, before: b => setFeatures(b, { groups: true }) }, async ({ tg, b }) => {
    const got = await userTurn(b, tg, `清单七 ${replyTool('清单七正文', { chat_id: String(UNREG_GROUP) })}`, r => r.args?.text === '清单七正文')
    expect(got).toHaveLength(1)
    expect(got[0]!.isError).toBe(true)
    expect(got[0]!.text).toContain('E_NOT_REGISTERED')
    expect(got[0]!.text).toContain('没有发出：')
    expect(tg.sentTo(UNREG_GROUP)).toHaveLength(0)
  })
})

test('清单8 未登记的私聊号：E_NOT_REGISTERED，零发送', async () => {
  await withBot({ access: accessWith([GROUP]), gw: { burst_window_ms: 0 }, before: b => setFeatures(b, { groups: true }) }, async ({ tg, b }) => {
    const got = await userTurn(b, tg, `清单八 ${replyTool('清单八正文', { chat_id: String(UNREG_DM) })}`, r => r.args?.text === '清单八正文')
    expect(got).toHaveLength(1)
    expect(got[0]!.isError).toBe(true)
    expect(got[0]!.text).toContain('E_NOT_REGISTERED')
    expect(tg.sentTo(UNREG_DM)).toHaveLength(0)
  })
})

test('清单9 chat_id 传数字（非字符串）：E_TYPE，零发送', async () => {
  await withBot({ access: accessWith([GROUP]), gw: { burst_window_ms: 0 }, before: b => setFeatures(b, { groups: true }) }, async ({ tg, b }) => {
    const got = await userTurn(b, tg, `清单九 ${replyTool('清单九正文', { chat_id: 123 })}`, r => r.args?.text === '清单九正文')
    expect(got).toHaveLength(1)
    expect(got[0]!.isError).toBe(true)
    expect(got[0]!.text).toContain('E_TYPE')
    expect(got[0]!.text).toContain('没有发出：')
    expect(tg.sentTo('123')).toHaveLength(0)
  })
})

test('清单10 格式非法的 chat_id（+123 / 0123 / 123abc / -../../x）：一律 E_FORMAT，零发送', async () => {
  await withBot({ access: accessWith([GROUP]), gw: { burst_window_ms: 0 }, before: b => setFeatures(b, { groups: true }) }, async ({ tg, b }) => {
    const bad = ['+123', '0123', '123abc', '-../../x']
    const text = [`清单十 !say:本地收到`, ...bad.map(c => replyTool('清单十正文', { chat_id: c }))].join('\n')
    const got = await userTurn(b, tg, text, r => r.args?.text === '清单十正文')
    expect(got).toHaveLength(4)
    for (const r of got) {
      expect(r.isError).toBe(true)
      expect(r.text).toContain('E_FORMAT')
    }
    for (const c of bad) expect(tg.sentTo(c)).toHaveLength(0)
  })
})

test('清单11 access.json 坏 JSON：带 chat_id 的发送 E_CONFIG；不带 chat_id 仍正常发到当前聊天', async () => {
  await withBot({ access: accessWith([GROUP]), gw: { burst_window_ms: 0 }, before: b => setFeatures(b, { groups: true }) }, async ({ tg, b }) => {
    writeFileSync(accessPath(b), '{ 这不是合法 JSON')
    const got = await userTurn(b, tg, `清单十一 ${replyTool('清单十一正文', { chat_id: String(GROUP) })}`, r => r.args?.text === '清单十一正文')
    expect(got).toHaveLength(1)
    expect(got[0]!.isError).toBe(true)
    expect(got[0]!.text).toContain('E_CONFIG')
    expect(tg.sentTo(GROUP)).toHaveLength(0)

    tg.pushText(OWNER, '清单十一 普通发送')
    const hits = await sentTo(tg, OWNER, '收到：清单十一 普通发送')
    expect(hits).toHaveLength(1)
    expect(hits[0]!.text).toBe('收到：清单十一 普通发送')
  })
})

test('清单12 跨聊天且传 reply_to：发出的消息没有引用，返回文案说明 reply_to 被忽略', async () => {
  await withBot({ access: accessWith([GROUP]), gw: { burst_window_ms: 0 }, before: b => setFeatures(b, { groups: true }) }, async ({ tg, b }) => {
    const got = await userTurn(b, tg, `清单十二 ${replyTool('清单十二跨聊正文', { chat_id: String(GROUP), reply_to: 123456 })}`, r => r.args?.text === '清单十二跨聊正文')
    expect(got).toHaveLength(1)
    expect(got[0]!.isError).toBe(false)
    expect(got[0]!.text).toContain('reply_to')
    const hits = await sentTo(tg, GROUP, '清单十二跨聊正文')
    expect(hits).toHaveLength(1)
    expect(hits[0]!.replyTo).toBeUndefined()
  })
})

test('清单13 同一轮先发当前聊天、再发已登记群，两句话相同：两次都发出', async () => {
  await withBot({ access: accessWith([GROUP]), gw: { burst_window_ms: 0 }, before: b => setFeatures(b, { groups: true }) }, async ({ tg, b }) => {
    const text = [
      '清单十三 !say:本地收到',
      replyTool('好'),
      replyTool('好', { chat_id: String(GROUP) }),
    ].join('\n')
    const got = await userTurn(b, tg, text, r => r.name === 'reply' && r.args?.text === '好')
    expect(got).toHaveLength(2)
    expect(got.every(r => r.isError === false)).toBe(true)
    const here = await sentTo(tg, OWNER, '好')
    const there = await sentTo(tg, GROUP, '好')
    expect(here.filter(s => s.text === '好')).toHaveLength(1)
    expect(there.filter(s => s.text === '好')).toHaveLength(1)
  })
})

test('清单14 同一轮往两个不同群发同一句话：两次都发出', async () => {
  await withBot({ access: accessWith([GROUP, GROUP2]), gw: { burst_window_ms: 0 }, before: b => setFeatures(b, { groups: true }) }, async ({ tg, b }) => {
    const text = [
      '清单十四 !say:本地收到',
      replyTool('好', { chat_id: String(GROUP) }),
      replyTool('好', { chat_id: String(GROUP2) }),
    ].join('\n')
    const got = await userTurn(b, tg, text, r => r.name === 'reply' && r.args?.text === '好')
    expect(got).toHaveLength(2)
    expect(got.every(r => r.isError === false)).toBe(true)
    expect((await sentTo(tg, GROUP, '好')).filter(s => s.text === '好')).toHaveLength(1)
    expect((await sentTo(tg, GROUP2, '好')).filter(s => s.text === '好')).toHaveLength(1)
  })
})

test('清单15 跨聊天发送白名单外的文件：该段失败，文件没有被发送', async () => {
  const outsideDir = mkdtempSync(join(tmpdir(), 'xchat-outside-'))
  const outside = join(outsideDir, 'outside.png')
  writeFileSync(outside, 'png')
  try {
    await withBot({ access: accessWith([GROUP]), gw: { burst_window_ms: 0 }, before: b => setFeatures(b, { groups: true }) }, async ({ tg, b }) => {
      const got = await userTurn(b, tg, `清单十五 ${replyTool('清单十五跨聊正文', { chat_id: String(GROUP), files: [outside] })}`, r => r.args?.text === '清单十五跨聊正文')
      expect(got).toHaveLength(1)
      const r = got[0]!
      expect(tg.sentTo(GROUP).every(s => !s.file)).toBe(true)
      expect(r.isError === true || /失败|没有?发|不允许|白名单/.test(r.text)).toBe(true)
    })
  } finally {
    rmSync(outsideDir, { recursive: true, force: true })
  }
})

test('清单16 list_chats：列出当前私聊与所有已登记群，不含未登记聊天，名字用尾号', async () => {
  await withBot({ access: accessWith([GROUP, GROUP2]), gw: { burst_window_ms: 0 }, before: b => setFeatures(b, { groups: true }) }, async ({ tg, b }) => {
    const got = await userTurn(b, tg, `清单十六 ${listChatsTool}`, r => r.name === 'list_chats')
    expect(got).toHaveLength(1)
    expect(got[0]!.isError).toBe(false)
    expect(got[0]!.text.trim().startsWith('{')).toBe(true)
    const out = JSON.parse(got[0]!.text)
    expect(out.groups_enabled).toBe(true)
    const chats: any[] = out.chats
    expect(Array.isArray(chats)).toBe(true)
    const dm = chats.find(c => c.chat_id === String(OWNER))
    expect(dm).toBeTruthy()
    expect(dm.kind).toBe('dm')
    expect(dm.current).toBe(true)
    expect(dm.name).toBe('私聊 尾号0001')
    const g1 = chats.find(c => c.chat_id === String(GROUP))
    expect(g1).toBeTruthy()
    expect(g1.kind).toBe('group')
    expect(g1.current).toBe(false)
    expect(g1.name).toBe('群 尾号1777')
    expect(chats.some(c => c.chat_id === String(GROUP2))).toBe(true)
    expect(chats.some(c => c.chat_id === '999888')).toBe(false)
  })
})

test('清单17 list_chats 且群聊关闭：groups_enabled=false，列表里没有群', async () => {
  await withBot({ access: accessWith([GROUP]), gw: { burst_window_ms: 0 }, before: b => setFeatures(b, { groups: false }) }, async ({ tg, b }) => {
    const got = await userTurn(b, tg, `清单十七 ${listChatsTool}`, r => r.name === 'list_chats')
    expect(got).toHaveLength(1)
    expect(got[0]!.isError).toBe(false)
    expect(got[0]!.text.trim().startsWith('{')).toBe(true)
    const out = JSON.parse(got[0]!.text)
    expect(out.groups_enabled).toBe(false)
    const chats: any[] = out.chats
    expect(chats.some(c => c.kind === 'group')).toBe(false)
    expect(chats.some(c => c.chat_id === String(OWNER) && c.current === true)).toBe(true)
  })
})

test('清单18 list_chats 且 access.json 坏：E_CONFIG', async () => {
  await withBot({ access: accessWith([GROUP]), gw: { burst_window_ms: 0 }, before: b => setFeatures(b, { groups: true }) }, async ({ tg, b }) => {
    writeFileSync(accessPath(b), '{ broken json')
    const got = await userTurn(b, tg, `清单十八 ${listChatsTool}`, r => r.name === 'list_chats')
    expect(got).toHaveLength(1)
    expect(got[0]!.isError).toBe(true)
    expect(got[0]!.text).toContain('E_CONFIG')
  })
})

test('清单19 跨聊天被 Telegram 拒绝（400）：工具结果 isError，文案说明目标没收到', async () => {
  await withBot({ access: accessWith([GROUP]), gw: { burst_window_ms: 0 }, before: b => setFeatures(b, { groups: true }) }, async ({ tg, b }) => {
    tg.faults.push({ method: 'sendMessage', when: p => String(p.chat_id) === String(GROUP), status: 400, description: 'Bad Request: chat not found' })
    const got = await userTurn(b, tg, `清单十九 ${replyTool('清单十九正文', { chat_id: String(GROUP) })}`, r => r.args?.text === '清单十九正文')
    expect(got).toHaveLength(1)
    expect(got[0]!.isError).toBe(true)
    expect(got[0]!.text).toMatch(/没有收到|没收到/)
    expect(tg.sentTo(GROUP)).toHaveLength(0)
  })
})

test('清单20 拒绝日志脱敏：只出现 *** 后四位，不出现完整目标号与正文', async () => {
  await withBot({ access: accessWith([GROUP]), gw: { burst_window_ms: 0 }, before: b => setFeatures(b, { groups: true }) }, async ({ tg, b }) => {
    // 正文 marker 的 JSON 里把 '1' 写成 1：用户消息原文不含 marker 字面，日志若记了工具正文才露馅
    const body = 'log-body-marker-4c\\u0031x'
    const marker = 'log-body-marker-4c1x'
    const got = await userTurn(b, tg, `清单二十 !tool:reply|{"text":"${body}","chat_id":"${UNREG_GROUP}"}`, r => r.args?.text === marker)
    expect(got).toHaveLength(1)
    expect(got[0]!.isError).toBe(true)
    await sleep(500)
    const logs = logEvents(b).map(e => JSON.stringify(e)).join('\n')
    expect(logs).toContain('***9999')
    expect(logs).not.toContain(String(UNREG_GROUP))
    expect(logs).not.toContain(marker)
  })
})

test('清单21 运行中把群写进 access.json（不重启）：下一次 reply 能发到该群', async () => {
  await withBot({ access: accessWith([GROUP]), gw: { burst_window_ms: 0 }, before: b => setFeatures(b, { groups: true }) }, async ({ tg, b }) => {
    rewriteAccess(b, a => { a.groups[String(NEW_GROUP)] = { allowFrom: [] } })
    const got = await userTurn(b, tg, `清单廿一 ${replyTool('清单廿一新群正文', { chat_id: String(NEW_GROUP) })}`, r => r.args?.text === '清单廿一新群正文')
    expect(got).toHaveLength(1)
    expect(got[0]!.isError).toBe(false)
    const hits = await sentTo(tg, NEW_GROUP, '清单廿一新群正文')
    expect(hits).toHaveLength(1)
    expect(hits[0]!.chatId).toBe(String(NEW_GROUP))
  })
})

test('清单22 运行中把群从 access.json 删掉：下一次 reply 回 E_NOT_REGISTERED', async () => {
  await withBot({ access: accessWith([GROUP]), gw: { burst_window_ms: 0 }, before: b => setFeatures(b, { groups: true }) }, async ({ tg, b }) => {
    rewriteAccess(b, a => { delete a.groups[String(GROUP)] })
    const got = await userTurn(b, tg, `清单廿二 ${replyTool('清单廿二正文', { chat_id: String(GROUP) })}`, r => r.args?.text === '清单廿二正文')
    expect(got).toHaveLength(1)
    expect(got[0]!.isError).toBe(true)
    expect(got[0]!.text).toContain('E_NOT_REGISTERED')
    expect(tg.sentTo(GROUP)).toHaveLength(0)
  })
})

// ───────── 错误契约：拒绝时零副作用（不调 Telegram、不写群记录）─────────

test('契约 拒绝类错误（E_TYPE / E_FORMAT / E_GROUPS_OFF / E_NOT_REGISTERED）均不写群记录文件', async () => {
  await withBot({ access: accessWith([GROUP]), gw: { burst_window_ms: 0 }, before: b => setFeatures(b, { groups: true }) }, async ({ tg, b }) => {
    const text = [
      '契约零副作用 !say:本地收到',
      replyTool('副作用一', { chat_id: 42 }),
      replyTool('副作用二', { chat_id: '12x' }),
      replyTool('副作用三', { chat_id: String(UNREG_GROUP) }),
    ].join('\n')
    const got = await userTurn(b, tg, text, r => r.name === 'reply' && ['副作用一', '副作用二', '副作用三'].includes(String(r.args?.text)))
    expect(got).toHaveLength(3)
    for (const r of got) expect(r.isError).toBe(true)
    const files = existsSync(groupsDir(b)) ? readdirSync(groupsDir(b)).filter(f => f.endsWith('.jsonl')) : []
    expect(files).toEqual([])
  })
})
