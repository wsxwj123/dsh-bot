// 黑盒验收：只依据 .devflow/moments-force-post-接口.md。
// 只经公开入口 LifeActions.moments({ action: 'post', ... }) 验证；脚本执行走构造注入的 deps.runPy
// （记录 argv），不起 Python 进程、不断言脚本内部行为。
import { expect, test } from 'bun:test'
import { mkdtempSync, rmSync } from 'fs'
import { tmpdir } from 'os'
import { basename, join } from 'path'
import { LifeActions, type ActionsDeps, type PyResult, type RunPy } from '../../src/life/actions'
import { Logger } from '../../src/log'

const BOT_ID = 'testbot'
const FORCE_SCRIPT = 'bot_post_moment.py'
const ORIGINAL_SCRIPT = 'post.py'

type PyCall = { argv: string[]; extra: Record<string, string | undefined>; timeoutMs: number }

/** 真脚本的成功输出（接口约定：返回文本里含朋友圈 id 与文案摘要） */
const POSTED_OUT = '朋友圈 id=9527\n文案摘要：今天天气不错'

function rig(py: (argv: string[]) => PyResult | Promise<PyResult>) {
  const root = mkdtempSync(join(tmpdir(), 'moments-force-'))
  const calls: PyCall[] = []
  const runPy: RunPy = async (argv, extra, timeoutMs) => {
    calls.push({ argv: [...argv], extra: { ...extra }, timeoutMs })
    return py(argv)
  }
  const deps: ActionsDeps = {
    botId: BOT_ID,
    imageAgent: 'testagent',
    configPath: join(root, 'config.yml'),
    log: new Logger({}),
    mediaDir: join(root, 'media'),
    imageProvider: () => 'off',
    imageSkillDir: () => join(root, 'skill'),
    stateDb: () => undefined,
    credRef: () => null,
    notify: () => {},
    syncWaitMs: () => 0,
    runPy,
  }
  return {
    actions: new LifeActions(deps),
    calls,
    done: () => rmSync(root, { recursive: true, force: true }),
  }
}

const scriptName = (argv: string[]) => basename(argv[0] ?? '')

// ── force=true：走 bot_post_moment.py，位置参数按 topic → visibility 透传 ──────
test('force=true_带topic与visibility_执行bot_post_moment.py且按bot_id_topic_visibility顺序传参', async () => {
  const r = rig(() => ({ code: 0, out: POSTED_OUT, err: '' }))
  try {
    await r.actions.moments({ action: 'post', force: true, topic: '周末去哪玩', visibility: 'private' })
    expect(r.calls).toHaveLength(1)
    expect(scriptName(r.calls[0]!.argv)).toBe(FORCE_SCRIPT)
    expect(r.calls[0]!.argv.slice(1)).toEqual([BOT_ID, '周末去哪玩', 'private'])
  } finally { r.done() }
})

test('force=true_只带topic_第二位参数是topic', async () => {
  const r = rig(() => ({ code: 0, out: POSTED_OUT, err: '' }))
  try {
    await r.actions.moments({ action: 'post', force: true, topic: '周末去哪玩' })
    expect(r.calls).toHaveLength(1)
    expect(scriptName(r.calls[0]!.argv)).toBe(FORCE_SCRIPT)
    expect(r.calls[0]!.argv.slice(1)).toEqual([BOT_ID, '周末去哪玩'])
  } finally { r.done() }
})

test('force=true_两者都没传_只传bot_id', async () => {
  const r = rig(() => ({ code: 0, out: POSTED_OUT, err: '' }))
  try {
    await r.actions.moments({ action: 'post', force: true })
    expect(r.calls).toHaveLength(1)
    expect(scriptName(r.calls[0]!.argv)).toBe(FORCE_SCRIPT)
    expect(r.calls[0]!.argv.slice(1)).toEqual([BOT_ID])
  } finally { r.done() }
})

test('force=true_即使原路径无素材也只走bot_post_moment.py_绝不调用post.py', async () => {
  const r = rig(argv => scriptName(argv) === ORIGINAL_SCRIPT
    ? { code: 3, out: 'skip: no material', err: '' }
    : { code: 0, out: POSTED_OUT, err: '' })
  try {
    await r.actions.moments({ action: 'post', force: true })
    expect(r.calls).toHaveLength(1)
    expect(scriptName(r.calls[0]!.argv)).toBe(FORCE_SCRIPT)
    expect(r.calls.map(c => scriptName(c.argv))).not.toContain(ORIGINAL_SCRIPT)
  } finally { r.done() }
})

// ── force=true：返回契约 ─────────────────────────────────────────────
test('force=true_发布成功_返回文本含朋友圈id与文案摘要且不标错', async () => {
  const r = rig(() => ({ code: 0, out: POSTED_OUT, err: '' }))
  try {
    const res = await r.actions.moments({ action: 'post', force: true })
    expect(res.isError).toBeFalsy()
    expect(res.text).toContain('9527')
    expect(res.text).toContain('今天天气不错')
  } finally { r.done() }
})

test('force=true_脚本退出码非0_返回isError并带上stderr里的失败原因', async () => {
  const r = rig(() => ({ code: 1, out: '', err: 'upload failed: image too large' }))
  try {
    const res = await r.actions.moments({ action: 'post', force: true })
    expect(res.isError).toBe(true)
    expect(res.text).toContain('upload failed: image too large')
  } finally { r.done() }
})

test('force=true_脚本不存在(runPy抛错)_返回isError不静默', async () => {
  const r = rig(() => { throw new Error('spawn scripts/bot_post_moment.py ENOENT') })
  try {
    const res = await r.actions.moments({ action: 'post', force: true })
    expect(res.isError).toBe(true)
    expect(res.text.length).toBeGreaterThan(0)
  } finally { r.done() }
})

// ── force 缺省 / false / 非布尔：原路径 post.py ─────────────────────
test('force缺省_执行原脚本post.py且成功输出照常返回', async () => {
  const r = rig(() => ({ code: 0, out: '已发布一条朋友圈', err: '' }))
  try {
    const res = await r.actions.moments({ action: 'post', topic: '周末去哪玩' })
    expect(r.calls).toHaveLength(1)
    expect(scriptName(r.calls[0]!.argv)).toBe(ORIGINAL_SCRIPT)
    expect(res.isError).toBeFalsy()
    expect(res.text).toContain('已发布一条朋友圈')
  } finally { r.done() }
})

test('force=false_执行原脚本post.py', async () => {
  const r = rig(() => ({ code: 0, out: '已发布一条朋友圈', err: '' }))
  try {
    await r.actions.moments({ action: 'post', force: false })
    expect(r.calls).toHaveLength(1)
    expect(scriptName(r.calls[0]!.argv)).toBe(ORIGINAL_SCRIPT)
  } finally { r.done() }
})

test.each([['"true"', 'true'], ['1', 1], ['null', null]] as const)(
  'force_不是布尔值(%s)_按false处理_执行原脚本post.py', async (_label, value) => {
    const r = rig(() => ({ code: 0, out: '已发布一条朋友圈', err: '' }))
    try {
      await r.actions.moments({ action: 'post', force: value })
      expect(r.calls).toHaveLength(1)
      expect(scriptName(r.calls[0]!.argv)).toBe(ORIGINAL_SCRIPT)
    } finally { r.done() }
  })

test('force缺省且自动判断无素材(code=3)_返回skip文案且不标错', async () => {
  const r = rig(() => ({ code: 3, out: 'skip: no material', err: '' }))
  try {
    const res = await r.actions.moments({ action: 'post' })
    expect(r.calls).toHaveLength(1)
    expect(scriptName(r.calls[0]!.argv)).toBe(ORIGINAL_SCRIPT)
    expect(res.isError).toBeFalsy()
    expect(res.text).toContain('skip: no material')
  } finally { r.done() }
})
