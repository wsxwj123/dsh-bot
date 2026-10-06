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
    'rec = {"argv": a, "token": os.environ.get("NOVELAI_BEARER_TOKEN"), "skill_root": os.environ.get("NOVELAI_SKILL_ROOT"), "im": im, "env_keys": sorted(os.environ)}',
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
  await start()
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
