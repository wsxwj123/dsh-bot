// 生图多张（对齐旧 bot：一次调用传多张、网关并发）+ image_guide 交付文本的转换/降级。
// 白盒：直接构造 LifeActions。多张的接线用注入的假 py（deps.runPy）验证——不起进程、不靠 sleep/墙钟；
// 只有"晚到通知"一条仍用假脚本 + 真子进程（它要的是真实的异步完成时机）。
import { expect, test } from 'bun:test'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { Logger } from '../../src/log'
import { convertGuideSection, LifeActions, runPool, type ActionsDeps, type RunPy } from '../../src/life/actions'

type Note = { chatId: string; text: string; key: string }

/** 假的 novelai-skill 脚本（只给"晚到通知"用：它要一个真实慢一点的子进程）：写 image_path、im.fail 时退出非 0 */
function fakeSkill(skillDir: string): void {
  mkdirSync(join(skillDir, 'scripts'), { recursive: true })
  mkdirSync(join(skillDir, 'assets'), { recursive: true })
  writeFileSync(join(skillDir, 'assets', 'default_config.json'), '{}')
  writeFileSync(join(skillDir, 'scripts', 'generate_novelai_image.py'), [
    'import json, os, sys, time',
    'a = sys.argv[1:]',
    'out = a[a.index("--output-json") + 1]',
    'im = json.load(open(a[a.index("--intermediate") + 1], encoding="utf-8"))',
    'log = os.path.join(os.environ["NOVELAI_SKILL_ROOT"], "calls.jsonl")',
    'def rec(ev):',
    '    with open(log, "a") as f: f.write(json.dumps({"ev": ev, "argv": a, "im": im, "reuse": "--reuse-seed" in a, "t": time.time()}) + "\\n")',
    'rec("start")',
    'time.sleep(float(im.get("sleep", 0)))',
    'if im.get("fail"):',
    '    rec("end"); sys.stderr.write("boom\\n"); sys.exit(1)',
    'img = out + ".png"; open(img, "wb").write(b"png")',
    'json.dump({"image_path": img}, open(out, "w"))',
    'rec("end")',
  ].join('\n'))
}

/** 假 py（注入 deps.runPy）：不起真实子进程。读中间文件决定成败（同真脚本约定：im.fail 退出非 0），记下每次调用的 argv。
 *  调用在"运行中"状态让出一次微任务（不靠 sleep/墙钟），peak 记同时运行的调用数——并发上限由它确定性验证 */
function fakePy() {
  const calls: { argv: string[]; im: Record<string, unknown>; reuse: boolean }[] = []
  let running = 0, peak = 0
  const run: RunPy = async args => {
    const imPath = args[args.indexOf('--intermediate') + 1]
    const outPath = args[args.indexOf('--output-json') + 1]
    if (!imPath || !outPath) throw new Error(`假 py 只认 novelai 脚本的调用：${args.join(' ')}`)
    const im = JSON.parse(readFileSync(imPath, 'utf8')) as Record<string, unknown>
    calls.push({ argv: args, im, reuse: args.includes('--reuse-seed') })
    running++; if (running > peak) peak = running
    await Promise.resolve()
    running--
    if (im.fail) return { code: 1, out: '', err: 'boom' }
    writeFileSync(`${outPath}.png`, 'png')
    writeFileSync(outPath, JSON.stringify({ image_path: `${outPath}.png` }))
    return { code: 0, out: '', err: '' }
  }
  return { calls, run, peak: () => peak }
}

function setup(opts: { provider?: string; syncWaitMs?: number; skill?: string; py?: RunPy; botId?: string; imageAgent?: string } = {}) {
  const root = mkdtempSync(join(tmpdir(), 'imgmulti-'))
  const skillDir = join(root, 'skill')
  fakeSkill(skillDir)
  const notes: Note[] = []
  const deps: ActionsDeps = {
    // 夹具默认就两个名字不同名：画风按配置 id（imageAgent）查，朋友圈身份（botId）是旧名。
    // 有人把 --agent-name 又接回 botId 时，下面的 argv 断言会红
    botId: opts.botId ?? 'chenlulu',
    imageAgent: opts.imageAgent ?? 'bot5',
    configPath: join(root, 'b.yml'),
    log: new Logger({}),
    mediaDir: join(root, 'media'),
    imageProvider: () => opts.provider ?? 'novelai',
    imageSkillDir: () => opts.skill ?? skillDir,
    stateDb: () => undefined,
    credRef: () => null,
    notify: (chatId, text, key) => { notes.push({ chatId, text, key }) },
    syncWaitMs: () => opts.syncWaitMs ?? 30_000,
    ...(opts.py ? { runPy: opts.py } : {}),
  }
  return { root, skillDir, notes, deps, actions: new LifeActions(deps), cleanup: () => rmSync(root, { recursive: true, force: true }) }
}

const until = async (fn: () => boolean, ms = 10_000) => {
  const end = Date.now() + ms
  while (Date.now() < end) { if (fn()) return; await new Promise(r => setTimeout(r, 20)) }
  throw new Error('timeout')
}

test('多张：一次传 images 跑 3 次脚本、结果一次返回 3 条路径；每张参数各用各的、临时文件不互相覆盖', async () => {
  const py = fakePy()
  const s = setup({ py: py.run })
  try {
    const r = await s.actions.generateImage('42', { images: [{ intermediate: { prompt: 'a' } }, { intermediate: { prompt: 'b' } }, { intermediate: { prompt: 'c' }, ratio: 'square' }] })
    expect(r.isError).toBeFalsy()
    expect(r.text).toContain('第 1 张：')
    expect(r.text).toContain('第 3 张：')
    expect(py.calls).toHaveLength(3) // 一次调用，3 次脚本
    const square = py.calls.find(c => c.im.prompt === 'c')!
    expect(square.argv).toContain('square') // 每张的 ratio 各用各的
    // 每张的临时文件不同，不互相覆盖
    const imPaths = py.calls.map(c => c.argv[c.argv.indexOf('--intermediate') + 1]!)
    expect(new Set(imPaths).size).toBe(3)
  } finally { s.cleanup() }
})

test('多张并发：6 张同时来，最多 4 个脚本同时在跑（受控假 py 数同时在跑的调用数）', async () => {
  const py = fakePy()
  const s = setup({ py: py.run })
  try {
    const images = Array.from({ length: 6 }, (_, i) => ({ intermediate: { prompt: `p${i}` } }))
    const r = await s.actions.generateImage('42', { images })
    expect(r.text).toContain('第 6 张：')
    expect(py.calls).toHaveLength(6)
    expect(py.peak()).toBe(4) // 串行的话最多 1；上限被改大就会大于 4
  } finally { s.cleanup() }
})

test('多张参数校验：第 N 张的 intermediate 为空直接报错、不跑脚本', async () => {
  const py = fakePy()
  const s = setup({ py: py.run })
  try {
    const r = await s.actions.generateImage('42', { images: [{ intermediate: { prompt: 'a' } }, { intermediate: null }, { intermediate: { prompt: 'c' } }] })
    expect(r.isError).toBe(true)
    expect(r.text).toBe('第 2 张的 intermediate 不能为空。')
    expect(py.calls).toHaveLength(0)
  } finally { s.cleanup() }
})

test('reuse_seed 与多张互斥：直接报错、不跑脚本', async () => {
  const py = fakePy()
  const s = setup({ py: py.run })
  try {
    const r = await s.actions.generateImage('42', { images: [{ intermediate: { prompt: 'a' } }], reuse_seed: true })
    expect(r.isError).toBe(true)
    expect(r.text).toContain('续图只能一张一张来')
    expect(r.text).toContain('reuse_seed')
    expect(py.calls).toHaveLength(0)
  } finally { s.cleanup() }
})

test('部分失败：成功照常返回、只报失败那张；整张全挂才 isError', async () => {
  const py = fakePy()
  const s = setup({ py: py.run })
  try {
    const r = await s.actions.generateImage('42', { images: [{ intermediate: { prompt: 'ok1' } }, { intermediate: { prompt: 'bad', fail: true } }, { intermediate: { prompt: 'ok2' } }] })
    expect(r.isError).toBeFalsy()
    expect(r.text).toContain('第 1 张：')
    expect(r.text).toContain('第 3 张：')
    expect(r.text).toContain('第 2 张没生成出来')
    expect(r.text).not.toContain('第 2 张：') // 失败那张不给路径
    const all = await s.actions.generateImage('42', { images: [{ intermediate: { prompt: 'x', fail: true } }] })
    expect(all.isError).toBe(true)
  } finally { s.cleanup() }
})

test('单张用法不变：返回形状、ratio、reuse_seed 照旧', async () => {
  const py = fakePy()
  const s = setup({ py: py.run })
  try {
    const r = await s.actions.generateImage('42', { intermediate: { prompt: 'solo' }, ratio: 'wide', reuse_seed: true })
    expect(r.isError).toBeFalsy()
    expect(r.text).toContain('图片生成好了：')
    const c = py.calls[0]!
    expect(c.reuse).toBe(true)
    expect(c.argv).toContain('wide')
    expect(c.argv[c.argv.indexOf('--intermediate') + 1]).toContain(`im-`)
    const bad = await s.actions.generateImage('42', {})
    expect(bad.isError).toBe(true)
    expect(bad.text).toBe('intermediate 不能为空。先调用 image_guide 看写法。')
  } finally { s.cleanup() }
})

test('argv 的 --agent-name 用配置 id（imageAgent），不跟朋友圈身份的 botId（lifeId）混用', async () => {
  const py = fakePy()
  const s = setup({ py: py.run, botId: 'chenlulu', imageAgent: 'bot5' })
  try {
    await s.actions.generateImage('42', { intermediate: { prompt: 'style-check' } })
    const c = py.calls[0]!
    const agent = c.argv[c.argv.indexOf('--agent-name') + 1]
    expect(agent).toBe('bot5') // 画风按管理台面板键（配置 id）查
    expect(agent).not.toBe('chenlulu') // 有人把这里改回 botId（lifeId）时这条会红
  } finally { s.cleanup() }
})

test('晚到通知：整批一条系统消息、路径齐全；两次调用键不同、不重复', async () => {
  const s = setup({ syncWaitMs: 30 })
  try {
    const a = await s.actions.generateImage('42', { images: [{ intermediate: { prompt: 'a', sleep: 0.3 } }, { intermediate: { prompt: 'b', sleep: 0.3 } }] })
    const b = await s.actions.generateImage('42', { images: [{ intermediate: { prompt: 'c', sleep: 0.3 } }, { intermediate: { prompt: 'd', sleep: 0.3 } }] })
    expect(a.text).toContain('还在生成')
    expect(b.text).toContain('还在生成')
    await until(() => s.notes.length >= 2)
    await new Promise(r => setTimeout(r, 100)) // 若实现会重复通知，这里能看出来
    expect(s.notes).toHaveLength(2) // 每次调用一条，不是每张一条
    for (const n of s.notes) expect(n.key).toMatch(/^image:.+/)
    expect(s.notes[0]!.key).not.toBe(s.notes[1]!.key)
    expect(s.notes[0]!.text).toContain('第 1 张：')
    expect(s.notes[0]!.text).toContain('第 2 张：')
    expect(s.notes[0]!.text).toContain('⟦系统·生图⟧')
  } finally { s.cleanup() }
})

test('runPool：并发不超过上限、任务全部跑到、完成顺序乱也按入参保序返回', async () => {
  let running = 0, peak = 0
  const tasks = Array.from({ length: 6 }, (_, i) => async () => {
    running++; if (running > peak) peak = running
    // 只用微任务让出（不靠 sleep/墙钟）：0 号多让两次，完成顺序变成 1、2、3、0…，返回仍要按入参顺序
    await Promise.resolve()
    if (i === 0) { await Promise.resolve(); await Promise.resolve() }
    running--
    return i
  })
  expect(await runPool(tasks, 4)).toEqual([0, 1, 2, 3, 4, 5])
  expect(peak).toBe(4)
})

test('convertGuideSection：匹配到就换掉那段 bash 并发说明（连同紧跟的要点）', () => {
  const guide = [
    '# 说明', '开头保留。',
    '**一轮要出多张图（mode=new）——用一条命令并行**：',
    '把 N 份 intermediate 合进同一条 Bash 命令，& 起进程，sleep 0.6，wait 收口。',
    '```bash', 'python3 g.py a.json &', 'sleep 0.6', 'python3 g.py b.json &', 'wait', '```',
    '',
    '- 命令 timeout 给足 300 秒',
    '- 某一路挂了成功的照常发',
    '',
    '结尾保留。',
  ].join('\n')
  const { text, converted } = convertGuideSection(guide)
  expect(converted).toBe(true)
  expect(text).toContain('新系统怎么发')
  expect(text).toContain('images 数组')
  expect(text).toContain('reuse_seed')
  expect(text).not.toContain('sleep 0.6')
  expect(text).not.toContain('timeout 给足 300 秒') // 紧跟的要点也是那段说明的一部分
  expect(text).not.toContain('```') // 那段 bash 代码块整段没了
  expect(text).not.toContain('wait 收口')
  expect(text).toContain('开头保留。')
  expect(text).toContain('结尾保留。')
})

test('convertGuideSection：代码块后面没有要点列表也能换（可选）', () => {
  const guide = '**一轮要出多张图（mode=new）——并行**：\n\`&\` 起进程、\`wait\` 收口。\n```bash\nx & \nwait\n```\n下一段照常。\n'
  const { text, converted } = convertGuideSection(guide)
  expect(converted).toBe(true)
  expect(text).toContain('新系统怎么发')
  expect(text).toContain('下一段照常。')
  expect(text).not.toContain('```')
})

test('convertGuideSection：没匹配到就原样返回（可降级，不报错）', () => {
  const raw = '# 说明\n用户把并发那段删了。\n'
  const { text, converted } = convertGuideSection(raw)
  expect(converted).toBe(false)
  expect(text).toBe(raw)
})

test('image_guide：交付文本开头是新系统说明；SKILL.md 有那段就转换、没有就保留原文', async () => {
  const s = setup()
  try {
    const skillA = join(s.root, 'skill-a')
    mkdirSync(skillA, { recursive: true })
    writeFileSync(join(skillA, 'SKILL.md'), '**一轮要出多张图（mode=new）——并行**：\n`&` 起进程、`wait` 收口。\n```bash\nx & \nwait\n```\n其余内容。\n')
    const ga = new LifeActions(depsOf(s, skillA)).imageGuide()
    expect(ga.text).toContain('【新系统说明】')
    expect(ga.text).toContain('images 数组')
    expect(ga.text).toContain('新系统怎么发')
    expect(ga.text).not.toContain('```') // 原文那段 bash 代码块已被换掉
    expect(ga.text).toContain('其余内容。')

    const skillB = join(s.root, 'skill-b')
    mkdirSync(skillB, { recursive: true })
    writeFileSync(join(skillB, 'SKILL.md'), '# 说明\n只有普通内容。\n')
    const gb = new LifeActions(depsOf(s, skillB)).imageGuide()
    expect(gb.text).toContain('【新系统说明】')
    expect(gb.text).toContain('只有普通内容。') // 降级：原文保留
  } finally { s.cleanup() }
})

/** image_guide 测试用：复用 setup 的依赖（含两个不同名的 id），只换 SKILL.md 路径 */
function depsOf(s: ReturnType<typeof setup>, skill: string): ActionsDeps {
  return { ...s.deps, imageSkillDir: () => skill }
}
