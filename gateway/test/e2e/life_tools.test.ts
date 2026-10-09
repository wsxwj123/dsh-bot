// M4：生图、朋友圈工具（网关替模型跑仓库里的脚本；生图脚本用假的，不需要密钥）
import { afterEach, expect, test } from 'bun:test'
import { chmodSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'fs'
import { join } from 'path'
import { FakeTelegram } from '../fakes/fake-telegram'
import { cleanup, Gateway, makeBot, OWNER, toolResults, until, writeConfig, type BotEnv } from '../harness'

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

/** 假的 novelai-skill：脚本把收到的参数和"有没有令牌"记下来，按 --output-json 写出图片路径 */
function fakeSkill(root: string): string {
  const dir = join(root, 'novelai-skill')
  mkdirSync(join(dir, 'scripts'), { recursive: true })
  mkdirSync(join(dir, 'assets'), { recursive: true })
  writeFileSync(join(dir, 'SKILL.md'), '# 假的生图说明\n写 intermediate.json。\n')
  writeFileSync(join(dir, 'assets', 'default_config.json'), '{}')
  writeFileSync(join(dir, 'scripts', 'generate_novelai_image.py'), [
    'import json, os, sys',
    'a = sys.argv[1:]',
    'out = a[a.index("--output-json") + 1]',
    'im = json.load(open(a[a.index("--intermediate") + 1], encoding="utf-8"))',
    'img = os.path.join(os.path.dirname(out), "fake.png")',
    'open(img, "wb").write(b"png")',
    'json.dump({"image_path": img}, open(out, "w"))',
    'rec = {"argv": a, "token": os.environ.get("NOVELAI_BEARER_TOKEN"), "skill_root": os.environ.get("NOVELAI_SKILL_ROOT"), "im": im, "env_keys": sorted(os.environ), "worker_bot": os.environ.get("TELEGRAM_WORKER_BOT")}',
    `json.dump(rec, open(${JSON.stringify(join(root, 'skill-call.json'))}, "w"))`,
  ].join('\n'))
  return dir
}

async function setup(gwExtra: Record<string, unknown> = {}) {
  tg = new FakeTelegram()
  b = makeBot(tg, { gw: { burst_window_ms: 0, ...gwExtra } })
  const cred = join(b.root, 'credentials.yaml')
  writeFileSync(cred, 'version: 1\nrefs:\n  DEEPSEEK_API_KEY: TESTONLY-not-a-real-key\n  NOVELAI_BEARER_TOKEN: TESTONLY-novelai\n')
  chmodSync(cred, 0o600)
  return b
}

async function start() {
  writeConfig(b!)
  gw = new Gateway(b!)
  await gw.start()
}

const resultOf = (b: BotEnv, name: string) => toolResults(b).find(r => r.name === name)

test('image_guide 给出写法；generate_image 跑生图脚本：令牌只走环境变量、不进命令行，返回图片路径', async () => {
  const b = await setup()
  const skill = fakeSkill(b.root)
  b.gw.image_skill_dir = skill
  // 夹具：给这个 bot 补一份 life_config，让 id（testbot）与 lifeId（chenlulu）不同名——
  // 画风必须按配置 id 找（管理台面板键），朋友圈身份（TELEGRAM_WORKER_BOT）才按 lifeId。
  // 退回传 lifeId 时下面的 --agent-name 断言会红
  const legacy = join(b.root, 'legacy')
  mkdirSync(legacy, { recursive: true })
  writeFileSync(join(legacy, 'chenlulu.yml'), '{}\n')
  writeConfig(b)
  const cfgY = JSON.parse(readFileSync(b.configPath, 'utf8'))
  cfgY.life_config = join(legacy, 'chenlulu.yml')
  writeFileSync(b.configPath, JSON.stringify(cfgY, null, 1))
  gw = new Gateway(b) // 不能走 start()：它会用 writeConfig 覆盖掉刚补的 life_config
  await gw.start()
  tg!.pushText(OWNER, '!tool:image_guide|{}')
  await until(() => !!resultOf(b, 'image_guide'), 'guide')
  expect(resultOf(b, 'image_guide')!.text).toContain('假的生图说明')
  tg!.pushText(OWNER, '!tool:generate_image|{"intermediate":{"scene":"海边"},"ratio":"square"}')
  await until(() => !!resultOf(b, 'generate_image'), 'generate')
  const r = resultOf(b, 'generate_image')!
  expect(r.isError).toBe(false)
  expect(r.text).toContain('fake.png')
  const call = JSON.parse(readFileSync(join(b.root, 'skill-call.json'), 'utf8'))
  expect(call.token).toBe('TESTONLY-novelai')
  expect(JSON.stringify(call.argv)).not.toContain('TESTONLY')
  expect(call.argv).toContain('square')
  expect(call.im).toEqual({ scene: '海边' })
  expect(call.skill_root).toBe(skill)
  // 画风按配置 id 找（管理台面板键）；不等于 lifeId
  expect(call.argv[call.argv.indexOf('--agent-name') + 1]).toBe(b.name)
  expect(call.argv[call.argv.indexOf('--agent-name') + 1]).not.toBe('chenlulu')
  // 朋友圈身份没被顺手改掉：worker 仍然用 lifeId
  expect(call.worker_bot).toBe('chenlulu')
  // 模型进程的环境不整体传下去：没有 Telegram 令牌和 DeepSeek 密钥
  expect(call.env_keys.some((k: string) => /TELEGRAM_BOT_TOKEN|DEEPSEEK_API_KEY/.test(k))).toBe(false)
})

test('generate_image：生图关掉时不跑脚本，告诉模型没开', async () => {
  const b = await setup({ image_provider: 'off' })
  await start()
  tg!.pushText(OWNER, '!tool:generate_image|{"intermediate":{"a":1}}')
  await until(() => !!resultOf(b, 'generate_image'), 'generate')
  expect(resultOf(b, 'generate_image')!.isError).toBe(true)
  expect(resultOf(b, 'generate_image')!.text).toContain('没开生图')
})

test('moments：查最近的圈用指定的朋友圈库（BOTLIFE_STATE_DB）；参数不对直接告诉模型', async () => {
  const b = await setup()
  const db = join(b.root, 'botlife.db')
  b.gw.botlife_db = db
  // 朋友圈库由朋友圈网页建好；这里先建一份空的
  expect(Bun.spawnSync([PY, '-c', 'import db; db.init()'], { cwd: REPO, env: { ...process.env, BOTLIFE_STATE_DB: db } }).exitCode).toBe(0)
  await start()
  tg!.pushText(OWNER, '!tool:moments|{"action":"recent"}')
  await until(() => !!resultOf(b, 'moments'), 'recent')
  expect(resultOf(b, 'moments')!.text).toContain('最近 7 天没发朋友圈')
  expect(existsSync(db)).toBe(true)
  tg!.pushText(OWNER, '!tool:moments|{"action":"like"}')
  await until(() => toolResults(b).filter(r => r.name === 'moments').length === 2, 'like without id')
  const bad = toolResults(b).filter(r => r.name === 'moments')[1]!
  expect(bad.isError).toBe(true)
  expect(bad.text).toContain('moment_id')
})

test('发图慢：reply 先告诉模型"在发了"（不让 dsh 等到超时）；模型再调一次 reply，同一张图不会发第二遍；这一轮等发完才收尾', async () => {
  const b = await setup({ reply_return_ms: 300 })
  await start()
  const media = join(b.botDir, 'media')
  mkdirSync(media, { recursive: true })
  const img = join(media, 'big.png')
  writeFileSync(img, 'png')
  tg!.faults.push({ method: 'sendPhoto', mode: 'slow', delayMs: 1500 })
  tg!.pushText(OWNER, `发图 !file:${img} !dup`)
  await until(() => toolResults(b).filter(r => r.name === 'reply').length === 2, 'two reply calls')
  const [first, second] = toolResults(b).filter(r => r.name === 'reply')
  expect(first!.text).toContain('正在发送')
  expect(second!.text).toContain('已经发过（或正在发）')
  await until(() => tg!.sentTo(OWNER).some(s => s.method === 'sendPhoto'), 'photo sent')
  const l = gw!.ledger()
  await until(() => (l.db.query(`SELECT COUNT(*) AS n FROM turns WHERE state IN ('preparing','sent')`).get() as { n: number }).n === 0, 'turn settled')
  expect((l.db.query(`SELECT COUNT(*) AS n FROM turns WHERE kind = 'nudge'`).get() as { n: number }).n).toBe(0)
  l.close()
  expect(tg!.sentTo(OWNER).filter(s => s.method === 'sendPhoto')).toHaveLength(1)
})
