// 功能开关（约定 v1.1 5.19）在网关里的三处：读开关（与 moments/features.py 同规则）、运行规则、给模型的工具清单。
import { expect, test } from 'bun:test'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { runtimeRules } from '../../src/dsh/profile'
import { ALL_ON, FEATURE_KEYS, loadFeatures, readFeatures, type Features } from '../../src/features'
import { Logger } from '../../src/log'
import { McpServer } from '../../src/mcp/server'

const off = (...keys: (keyof Features)[]): Features => ({ ...ALL_ON, ...Object.fromEntries(keys.map(k => [k, false])) })

test('读开关：没写的前八个开、nsfw 有技能才开；不是真假的按关并说一句；块不是对象全按缺省', () => {
  const dir = mkdtempSync(join(tmpdir(), 'feat-'))
  try {
    expect(readFeatures(undefined, dir)).toEqual({ features: { ...ALL_ON, nsfw: false }, warnings: [] })
    mkdirSync(join(dir, 'skill-nsfw'))
    writeFileSync(join(dir, 'skill-nsfw', 'SKILL.md'), 'x')
    expect(readFeatures(undefined, dir).features.nsfw).toBe(true)
    const r = readFeatures({ voice: 'maybe', image: false }, dir)
    expect([r.features.voice, r.features.image, r.features.stickers]).toEqual([false, false, true])
    expect(r.warnings).toEqual(['features.voice 的值不是真或假，按关处理'])
    expect(readFeatures(['voice'], dir).warnings).toEqual(['配置里的 features 不是一组开关，全部按缺省处理'])
    const yml = join(dir, 'b.yml')
    writeFileSync(yml, 'id: b\nfeatures:\n  moments: false\n')
    expect(loadFeatures(yml, dir)!.features.moments).toBe(false)
    expect(FEATURE_KEYS.length).toBe(9)
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('读开关：yes/no/on/off、带引号的 true/false 和 Python 那边一样认成真假，别的字符串按关', () => {
  const r = readFeatures({ voice: 'on', image: 'No', stickers: 'YES', moments: 'off', groups: 'True', proactive: 'false', life: 'y' }, '/nonexistent')
  expect(r.features).toMatchObject({ voice: true, image: false, stickers: true, moments: false, groups: true, proactive: false, life: false })
  expect(r.warnings).toEqual(['features.life 的值不是真或假，按关处理'])
  expect(readFeatures({ voice: 'toString' }, '/nonexistent').features.voice).toBe(false)
})

test('读开关：配置文件读不出（不在、写坏了）回 null，交给调用方保留原来的开关', () => {
  const dir = mkdtempSync(join(tmpdir(), 'feat-'))
  try {
    const yml = join(dir, 'b.yml')
    writeFileSync(yml, 'id: b\nfeatures: [\n')
    expect(loadFeatures(yml, dir)).toBeNull()
    expect(loadFeatures(join(dir, 'missing.yml'), dir)).toBeNull()
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('运行规则：九项都开与没有开关时逐字相同', () => {
  expect(runtimeRules({}, ALL_ON)).toBe(runtimeRules({}))
  const all = runtimeRules({})
  expect(all).toContain('发表情包表达')
  expect(all).toContain('要发照片（自拍、拍给对方看的东西）时')
  expect(all).toContain('朋友圈相关（看最近的圈、点赞、评论、发圈、配图）用')
  expect(all).toContain('as_voice=true')
  expect(all).not.toContain('你不能')
})

test('运行规则：关掉的工具那一条去掉，再说一句不能用；语音关着语言一节只留文字语言', () => {
  const r = runtimeRules({ text: '中文' }, off('voice', 'image', 'stickers'))
  expect(r).not.toContain('发表情包表达')
  expect(r).not.toContain('要发照片')
  expect(r).toContain('朋友圈相关（看最近的圈、点赞、评论、发圈、配图）用')
  expect(r).toContain('- 你不能发图。')
  expect(r).toContain('- 你不能发表情包。')
  expect(r).toContain('- 你不能发语音，只发文字。')
  expect(r).not.toContain('as_voice')
  expect(r).toContain('- 文字语言是中文。文字气泡始终用文字语言写。')
  const m = runtimeRules({}, off('moments'))
  expect(m).toContain('要发照片（自拍、拍给对方看的东西）时')
  expect(m).not.toContain('朋友圈相关')
  expect(runtimeRules({}, off('image', 'moments'))).not.toContain('朋友圈相关')
})

test('工具清单：关掉的工具不列；调了也不执行，回"这个功能现在关着。"', async () => {
  let shown = true
  const calls: string[] = []
  const tool = (name: string) => ({ name, description: name, inputSchema: { type: 'object', properties: {} }, call: async () => { calls.push(name); return { text: 'ok' } } })
  const mcp = new McpServer([tool('reply'), tool('sticker')], () => ({ chatId: '1', token: 't' }), new Logger({}), name => name !== 'sticker' || shown)
  mcp.start()
  try {
    const rpc = async (method: string, params: Record<string, unknown> = {}) => (await (await fetch(`http://127.0.0.1:${mcp.port}/mcp/1`, {
      method: 'POST', headers: { authorization: 'Bearer t', 'content-type': 'application/json', accept: 'application/json, text/event-stream' },
      body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }),
    })).json()) as { result: any }
    expect((await rpc('tools/list')).result.tools.map((t: { name: string }) => t.name)).toEqual(['reply', 'sticker'])
    shown = false
    expect((await rpc('tools/list')).result.tools.map((t: { name: string }) => t.name)).toEqual(['reply'])
    const r = (await rpc('tools/call', { name: 'sticker', arguments: {} })).result
    expect(r).toEqual({ content: [{ type: 'text', text: '这个功能现在关着。' }], isError: true })
    expect(calls).toEqual([])
  } finally { await mcp.stop() }
})
