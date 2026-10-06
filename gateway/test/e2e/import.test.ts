// M6：切换时导入旧私聊记录（scripts/import_history.py）。新网关第一次处理这个私聊时，
// 用导入的记录补写一份摘要，新会话带着摘要和最近的原话开始
import { afterEach, expect, test } from 'bun:test'
import { mkdirSync, writeFileSync } from 'fs'
import { join } from 'path'
import { FakeTelegram } from '../fakes/fake-telegram'
import { cleanup, Gateway, makeBot, OWNER, prompts, until, type BotEnv } from '../harness'

const REPO = join(import.meta.dir, '..', '..', '..')
const PY = process.env.DSH_BOT_PYTHON || (process.platform === 'win32' ? 'python' : 'python3')
let tg: FakeTelegram | null = null
let b: BotEnv | null = null
let gw: Gateway | null = null

afterEach(async () => {
  await gw?.stop()
  tg?.stop()
  if (b) cleanup(b)
  tg = null; b = null; gw = null
})

test('导入旧私聊记录后，第一条新消息先用它补写摘要，新会话带着摘要和原话', async () => {
  tg = new FakeTelegram()
  b = makeBot(tg, { gw: { burst_window_ms: 0 } })
  // 旧系统布局：旧频道目录 + 假 HOME 下的 Claude Code 会话文件
  const home = join(b.root, 'oldhome')
  const old = join(b.root, 'old', 'chan')
  mkdirSync(old, { recursive: true })
  writeFileSync(join(old, 'access.json'), JSON.stringify({ allowFrom: [String(OWNER)] }))
  const env = { ...process.env, HOME: home, USERPROFILE: home, DSH_BOT_HOME: b.root, PYTHONIOENCODING: 'utf-8' }
  const slug = Bun.spawnSync([PY, '-c', `import chat_history,sys; print(chat_history._project_slug_for(sys.argv[1]))`, old], { cwd: REPO, env }).stdout.toString().trim()
  const proj = join(home, '.claude', 'projects', slug)
  mkdirSync(proj, { recursive: true })
  const t = (ago: number) => new Date(Date.now() - ago).toISOString()
  writeFileSync(join(proj, 's.jsonl'), [
    { type: 'user', timestamp: t(7200_000), message: { content: `<channel source="telegram" chat_id="${OWNER}">\n旧系统里说的你好呀\n</channel>` } },
    { type: 'assistant', timestamp: t(7100_000), message: { content: [{ type: 'tool_use', name: 'mcp__telegram-worker__reply', input: { text: '旧系统里的回复嗨' } }] } },
  ].map(l => JSON.stringify(l)).join('\n') + '\n')
  const r = Bun.spawnSync([PY, join(REPO, 'scripts', 'import_history.py'), b.name, '--from', old], { cwd: REPO, env })
  expect(r.exitCode).toBe(0)

  gw = new Gateway(b)
  await gw.start()
  tg.pushText(OWNER, '今天怎么样')
  await until(() => prompts(b!).some(p => p.text.includes('今天怎么样')), 'chat prompt')
  const summary = prompts(b).find(p => p.text.startsWith('⟦系统·整理记忆⟧'))!
  expect(summary.text).toContain('旧系统里说的你好呀')
  expect(summary.text).toContain('旧系统里的回复嗨')
  const chat = prompts(b).find(p => p.text.includes('今天怎么样'))!.text
  expect(chat).toContain('假摘要')
  expect(chat).toContain('旧系统里的回复嗨')
})
