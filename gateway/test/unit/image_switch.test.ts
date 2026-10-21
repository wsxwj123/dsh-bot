// 生图引擎的共用开关（约定 v1.1 5.21）：管理台切一次，两边的 bot 都跟着换。
// 网关每次生图前读壳的全局配置文件；ComfyUI 调共用的 comfyui-skill 脚本；出不了图不换引擎（用户 2026-10-16 定）。
import { expect, test } from 'bun:test'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, utimesSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { loadBotConfig } from '../../src/config'
import { LifeActions, type ActionsDeps, type RunPy } from '../../src/life/actions'
import { effectiveEngine, ImageSwitch, pickEngine } from '../../src/life/image_switch'
import { Logger } from '../../src/log'

const doc = (img: Record<string, unknown>) => ({ moments: { image_generation: img } })

test('读取规则：聊天看 provider_telegram、朋友圈看 provider_moment，再看 provider，都没有用 novelai；值不认识按没有', () => {
  expect(pickEngine(doc({ provider: 'novelai', provider_telegram: 'comfyui', provider_moment: 'novelai' }), 'telegram')).toBe('comfyui')
  expect(pickEngine(doc({ provider: 'novelai', provider_telegram: 'comfyui', provider_moment: 'novelai' }), 'moment')).toBe('novelai')
  expect(pickEngine(doc({ provider: 'comfyui' }), 'telegram')).toBe('comfyui')
  expect(pickEngine(doc({ provider: 'comfyui' }), 'moment')).toBe('comfyui')
  expect(pickEngine(doc({ provider_telegram: 'dalle', provider: 'comfyui' }), 'telegram')).toBe('comfyui')
  expect(pickEngine(doc({ provider_telegram: 'dalle' }), 'telegram')).toBe('novelai')
  expect(pickEngine({}, 'telegram')).toBe('novelai')
  expect(pickEngine(null, 'moment')).toBe('novelai')
})

test('开关文件：改了就重读；不在或不是合法 YAML 时返回 null（由调用方退回本 bot 配置）', () => {
  const root = mkdtempSync(join(tmpdir(), 'imgsw-'))
  try {
    const p = join(root, '_global.yml')
    const sw = new ImageSwitch(p, new Logger({}))
    expect(sw.engine('telegram')).toBeNull()
    writeFileSync(p, 'moments:\n  image_generation:\n    provider: novelai\n')
    expect(sw.engine('telegram')).toBe('novelai')
    writeFileSync(p, 'moments:\n  image_generation:\n    provider: comfyui\n')
    utimesSync(p, new Date(), new Date(Date.now() + 5_000))
    expect(sw.engine('telegram')).toBe('comfyui')
    writeFileSync(p, 'moments: [unclosed\n')
    utimesSync(p, new Date(), new Date(Date.now() + 10_000))
    expect(sw.engine('telegram')).toBeNull()
  } finally { rmSync(root, { recursive: true, force: true }) }
})

test('生效的引擎：本 bot 配置写 off 就是 off；开关读得到用开关；读不到退回本 bot 配置', () => {
  const sw = (v: 'novelai' | 'comfyui' | null) => ({ engine: () => v })
  expect(effectiveEngine('off', sw('comfyui'))).toBe('off')
  expect(effectiveEngine('novelai', sw('comfyui'))).toBe('comfyui')
  expect(effectiveEngine('comfyui', sw(null))).toBe('comfyui')
  expect(effectiveEngine('novelai', null)).toBe('novelai')
})

test('配置：gateway.global_cfg、gateway.comfyui_skill_dir 读进来；不写就是没有', () => {
  const root = mkdtempSync(join(tmpdir(), 'cfg-'))
  try {
    const ch = join(root, 'ch')
    mkdirSync(ch)
    writeFileSync(join(ch, 'access.json'), JSON.stringify({ allowFrom: ['42'] }))
    const p = join(root, 'b.yml')
    writeFileSync(p, `id: bot5\nbot_channel_path: ${ch}\ndispatcher_port: 17805\ngateway:\n  global_cfg: ~/x/_global.yml\n  comfyui_skill_dir: ~/skills/comfyui-skill\n`)
    const c = loadBotConfig(p, { DSH_BOT_HOME: join(root, 'home') })
    expect(c.gw.globalCfg).toBe('~/x/_global.yml')
    expect(c.gw.comfyuiSkillDir).toBe('~/skills/comfyui-skill')
    writeFileSync(p, `id: bot5\nbot_channel_path: ${ch}\ndispatcher_port: 17805\n`)
    const d = loadBotConfig(p, { DSH_BOT_HOME: join(root, 'home') })
    expect(d.gw.globalCfg).toBeUndefined()
    expect(d.gw.comfyuiSkillDir).toBeUndefined()
  } finally { rmSync(root, { recursive: true, force: true }) }
})

// ─── 生图动作：按生效的引擎走 ───

type Call = { argv: string[]; extra: Record<string, string | undefined> }

/** 假 py：记下每次调用；comfyui 脚本按 comfyFails 决定成败（成功就写 --out），novelai 脚本按真脚本约定写结果文件 */
function fakePy(comfyFails: boolean, novelBadInput = false) {
  const calls: Call[] = []
  const run: RunPy = async (argv, extra) => {
    calls.push({ argv, extra })
    if (argv[0]!.endsWith('comfyui_gen.py')) {
      if (comfyFails) return { code: 1, out: 'FAIL: 连不上 ComfyUI（http://127.0.0.1:8188）: refused\n', err: '' }
      const out = argv[argv.indexOf('--out') + 1]!
      writeFileSync(out, 'png')
      return { code: 0, out: `MEDIA: ${out}\n`, err: '' }
    }
    // 真脚本约定：退出码 3 = intermediate 的角色字段写错，stderr 首行是原因，不写结果文件
    // 真脚本 2026-10-21 起 stderr 两行：原因 + 正确写法示例（示例只在脚本里维护，网关只转发）
    if (novelBadInput) return { code: 3, out: '', err: '[novelai] characters 不合法：position_mode 为 manual 时第 1 个角色缺少 center；position_mode 为 manual 时第 2 个角色缺少 center\n[novelai] 正确写法示例：{"position_mode": "manual", "characters": [{"positive": "girl, long hair, red dress", "center": {"x": 0.3, "y": 0.5}}]}。manual 时每个角色都要有 center。\n' }
    const outPath = argv[argv.indexOf('--output-json') + 1]!
    writeFileSync(`${outPath}.png`, 'png')
    writeFileSync(outPath, JSON.stringify({ image_path: `${outPath}.png` }))
    return { code: 0, out: '', err: '' }
  }
  return { calls, run }
}

function setup(engine: string, comfyFails = false, novelBadInput = false) {
  const root = mkdtempSync(join(tmpdir(), 'imgsw-act-'))
  const novel = join(root, 'novelai-skill')
  const comfy = join(root, 'comfyui-skill')
  mkdirSync(join(novel, 'assets'), { recursive: true })
  mkdirSync(comfy, { recursive: true })
  writeFileSync(join(novel, 'SKILL.md'), '# NovelAI 写法\n')
  writeFileSync(join(comfy, 'SKILL.md'), '# ComfyUI 写法\n')
  const py = fakePy(comfyFails, novelBadInput)
  const deps: ActionsDeps = {
    botId: 'chenlulu', imageAgent: 'bot5', configPath: join(root, 'b.yml'), log: new Logger({}), mediaDir: join(root, 'media'),
    imageProvider: () => engine,
    imageSkillDir: e => (e === 'comfyui' ? comfy : novel),
    globalCfg: () => join(root, '_global.yml'),
    stateDb: () => undefined,
    credRef: () => null,
    notify: () => {},
    syncWaitMs: () => 30_000,
    runPy: py.run,
  }
  return { root, comfy, novel, py, actions: new LifeActions(deps), cleanup: () => rmSync(root, { recursive: true, force: true }) }
}

test('ComfyUI：调共用技能目录里的脚本，带 --global（开关所在的全局配置）；给了画幅就带 --size', async () => {
  const s = setup('comfyui')
  try {
    const r = await s.actions.generateImage('42', { prompt: '1girl, selfie', ratio: 'wide' })
    expect(r.isError).toBeFalsy()
    const a = s.py.calls[0]!.argv
    expect(a[0]).toBe(join(s.comfy, 'scripts', 'comfyui_gen.py'))
    expect(a.slice(1, 3)).toEqual(['bot5', '1girl, selfie'])
    expect(a[a.indexOf('--global') + 1]).toBe(join(s.root, '_global.yml'))
    expect(a[a.indexOf('--size') + 1]).toBe('wide')
    await s.actions.generateImage('42', { prompt: 'a cup of tea' })
    expect(s.py.calls[1]!.argv).not.toContain('--size')
  } finally { s.cleanup() }
})

test('出不了图不换引擎（用户 2026-10-16 定）：ComfyUI 没画出来也回原来那句"这次没生成出来"，不去调 NovelAI；失败原因只进日志', async () => {
  const s = setup('comfyui', true)
  try {
    const fail = '这次没生成出来（生图服务出错）。可以和对方说一声，或者稍后再试。'
    const r = await s.actions.generateImage('42', { prompt: '1girl, selfie' })
    expect(r.isError).toBe(true)
    expect(r.text).toBe(fail)
    expect((await s.actions.generateImage('42', { images: [{ prompt: 'a' }, { prompt: 'b' }] })).text).toBe(fail)
    expect(s.py.calls.length).toBe(3)
    expect(s.py.calls.every(c => c.argv[0]!.endsWith('comfyui_gen.py'))).toBe(true)
  } finally { s.cleanup() }
})

test('引擎只看开关：ComfyUI 时写法说明与脚本都是 ComfyUI 的（模型多给 engine 也不理）；NovelAI 时都是 NovelAI 的', async () => {
  const c = setup('comfyui')
  try {
    expect(c.actions.imageGuide().text).toContain('# ComfyUI 写法')
    expect((await c.actions.generateImage('42', { engine: 'novelai', prompt: '1girl' })).isError).toBeFalsy()
    expect(c.py.calls.at(-1)!.argv[0]).toBe(join(c.comfy, 'scripts', 'comfyui_gen.py'))
  } finally { c.cleanup() }
  const n = setup('novelai')
  try {
    expect(n.actions.imageGuide().text).toContain('# NovelAI 写法')
    expect((await n.actions.generateImage('42', { intermediate: { prompt: 'x' } })).isError).toBeFalsy()
    expect(n.py.calls.at(-1)!.argv[0]).toBe(join(n.novel, 'scripts', 'generate_novelai_image.py'))
    expect(n.py.calls.at(-1)!.extra.NOVELAI_SKILL_ROOT).toBe(n.novel)
  } finally { n.cleanup() }
})

test('本 bot 没开生图（off）：开关怎么切都不生图', async () => {
  const s = setup('off')
  try {
    expect((await s.actions.generateImage('42', { intermediate: { prompt: 'x' } })).isError).toBe(true)
    expect(s.actions.imageGuide().isError).toBe(true)
    expect(s.py.calls).toHaveLength(0)
  } finally { s.cleanup() }
})

test('朋友圈脚本的环境：带上开关所在的全局配置与共用 ComfyUI 技能目录', async () => {
  const s = setup('novelai')
  try {
    const env = (s.actions as unknown as { env: (x: Record<string, string | undefined>) => Record<string, string> }).env({})
    expect(env.CLAUDEBOT_GLOBAL_CFG).toBe(join(s.root, '_global.yml'))
    expect(env.COMFYUI_SKILL_DIR).toBe(s.comfy)
    expect(readFileSync(join(s.novel, 'SKILL.md'), 'utf8')).toContain('NovelAI')
  } finally { s.cleanup() }
})

test('NovelAI 退出码 3（intermediate 角色字段写错）：把报错首行和正确写法回给模型让它改了重跑，不再说"生图服务出错"；其他失败照旧', async () => {
  const s = setup('novelai', false, true)
  try {
    const r = await s.actions.generateImage('42', { intermediate: { prompt: '2girls', position_mode: 'manual', characters: [{ positive: 'a' }, { positive: 'b' }] } })
    expect(r.isError).toBe(true)
    expect(r.text).toContain('第 1 个角色缺少 center')
    expect(r.text).not.toContain('[novelai]')
    expect(r.text).toContain('再调用一次 generate_image')
    // 示例来自脚本 stderr 第二行，网关原样转发、不另写一份
    expect(r.text).toContain('正确写法示例：{"position_mode": "manual"')
    expect(r.text).toContain('"center": {"x": 0.3, "y": 0.5}')
    expect(r.text).not.toContain('生图服务出错')
    expect(s.py.calls).toHaveLength(1)
  } finally { s.cleanup() }
})
