// 真 dsh + 假模型：技能清单里有 skill-gif（description 含「表情包」）且加载后正文能用；
// 模型按 dsh 里的名字调 mcp__tg__sticker，假 HOME 库里的 gif 真的发到聊天里。
// 需要一份装好的 dsh：设置 DSH_BOT_HARNESS=<目录>（目录下有 node_modules/@deepseek-ai/dsh），没有就跳过。
// CI 上必须注入假 HOME（§3）：真实主目录里没有测试用的库文件。
import { afterAll, beforeAll, expect, test } from 'bun:test'
import { chmodSync, existsSync, mkdirSync, writeFileSync } from 'fs'
import { join, resolve } from 'path'
import { startFakeLlm, type FakeLlm } from '../../../lab/dsh/lib/fake-llm'
import { FakeTelegram } from '../fakes/fake-telegram'
import { cleanup, Gateway, makeBot, OWNER, until, writeConfig, type BotEnv } from '../harness'
import { makeStickerHome, putStickerFiles, STD_FILES, stdLib, writeStickerLib } from './_sticker'

const harness = process.env.DSH_BOT_HARNESS ? resolve(process.env.DSH_BOT_HARNESS) : ''
const available = !!harness && existsSync(join(harness, 'node_modules', '@deepseek-ai', 'dsh', 'lib', 'bin.js'))

/** INTERFACE §5 的 skill-gif 推荐版全文（用户的正式文件在 ~/.dsh-bot 下；这里用临时 bot 的副本测机制） */
const SKILL_GIF_MD = [
  '---',
  'name: skill-gif',
  'description: 表情包，情绪上来想发表情包时加载（用 sticker 工具）',
  '---',
  '',
  '- 用 sticker 工具发送表情包以表达情绪和想法，每次回复发送表情包概率为50%',
  '',
  '## 表情包规则',
  '',
  '库分 `sfw` + `nsfw` 两档，用 `sticker` 工具。先 `list` 查（带上关键词：`sfw` 的标签是英文，`nsfw` 是中文），再 `send`（按 id 或标签）把一条发到聊天里。',
  '',
  '什么时候发',
  '- 按运行规则里"情绪上来"那句的要求发，平静地交代事情不发。',
  '',
  '发哪一档',
  '- 两档都可以用。平时用 `sfw`，对话亲密起来（调情、暧昧）才用 `nsfw`。',
  '',
  '一条消息至多一个表情包，不得连续发送；表情包是单独一条消息（不带配文），发完不用再补文字。',
  '',
].join('\n')

let tg: FakeTelegram
let llm: FakeLlm
let b: BotEnv
let gw: Gateway

beforeAll(async () => {
  if (!available) return
  tg = new FakeTelegram()
  llm = startFakeLlm({
    port: 0,
    script: [
      { match: 'stickerA', tool: { name: 'mcp__tg__sticker', arguments: { action: 'send', id: 'sfw:1' } } },
      { match: '表情包发出去了', text: '好呀' }, // 工具结果回到模型后收尾
      { match: 'skillB', tool: { name: 'skill', arguments: { name: 'skill-gif' } } },
      { match: '什么时候发', text: '技能看过了' },
    ],
  })
  b = makeBot(tg, {
    gw: { dsh_command: undefined, burst_window_ms: 0 },
    brain: {
      provider: 'fake', model: 'fake-chat',
      routes: { fake: { api: 'openai-completions', baseURL: `http://127.0.0.1:${llm.port}/v1`, apiKeyEnv: 'FAKE_LLM_KEY', retryPolicy: { mode: 'normal', maxRetries: 0 }, models: [{ id: 'fake-chat', contextWindow: 1000000 }] } },
    },
  })
  delete (b.gw as Record<string, unknown>).dsh_command
  const home = makeStickerHome(b.root)
  putStickerFiles(home, STD_FILES)
  writeStickerLib(home, stdLib(home))
  mkdirSync(join(b.channelDir, 'skills', 'skill-gif'), { recursive: true })
  writeFileSync(join(b.channelDir, 'skills', 'skill-gif', 'SKILL.md'), SKILL_GIF_MD)
  const cred = join(b.root, 'credentials.yaml')
  writeFileSync(cred, 'version: 1\nrefs:\n  FAKE_LLM_KEY: fake-key-for-tests\n')
  chmodSync(cred, 0o600)
  writeConfig(b)
  gw = new Gateway(b)
  await gw.start({ DSH_BOT_HARNESS: harness, HOME: home, USERPROFILE: home })
}, 60_000)

afterAll(async () => {
  if (!available) return
  await gw.stop()
  llm.stop()
  tg.stop()
  cleanup(b)
})

test.skipIf(!available)('真 dsh：模型调 mcp__tg__sticker，假 HOME 库里的 gif 真的发到聊天里', async () => {
  tg.pushText(OWNER, '贴一个试试 stickerA')
  await until(() => tg.sentTo(OWNER).some(s => s.method === 'sendAnimation'), 'sticker via real dsh', 120_000)
  const s = tg.sentTo(OWNER).find(x => x.method === 'sendAnimation')!
  expect(String(s.file)).toContain('a.gif')
  expect(s.text ?? '').toBe('') // 表情包消息不带 caption
  await until(() => llm.requests.some(r => JSON.stringify(r.body.messages).includes('表情包发出去了：Happy 1')), 'tool result reached the model', 30_000)
}, 240_000)

test.skipIf(!available)('真 dsh：技能清单里有 skill-gif（description 含表情包），加载后正文含「什么时候发」', async () => {
  tg.pushText(OWNER, '看看技能 skillB')
  const withSkillB = await until(() => llm.requests.find(r => JSON.stringify(r.body.messages).includes('skillB')), 'skillB request', 120_000)
  const seen = JSON.stringify(withSkillB.body.messages)
  expect(seen).toContain('skill-gif')
  expect(seen).toContain('表情包，情绪上来想发表情包时加载')
  await until(() => llm.requests.some(r => JSON.stringify(r.body.messages).includes('什么时候发')), 'skill body loaded into context', 60_000)
}, 240_000)
