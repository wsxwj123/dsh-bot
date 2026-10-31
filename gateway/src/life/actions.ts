// 生图和朋友圈（M4）：旧系统里模型用 Bash 跑仓库里的脚本，新系统的模型没有 Bash，
// 改成网关替它跑同一批脚本（不重写），结果交回给模型。
//   generate_image：novelai-skill 的生图脚本（模型按 image_guide 写好结构化描述）或 ComfyUI 脚本；
//                   短等一下（默认 10 秒，gateway.image_wait_ms），生成好就直接返回路径；没好就先返回"还在生成"，好了以后程序再告诉它
//   moments：查最近的朋友圈、点赞、回评论、发圈、给圈配图、删自己的评论
import { mkdirSync, readFileSync, writeFileSync } from 'fs'
import { homedir } from 'os'
import { dirname, join, resolve } from 'path'
import { safeError, type Logger } from '../log'
import type { ImageEngine } from './image_switch'

const REPO_ROOT = resolve(import.meta.dir, '..', '..', '..')
// 生图一般要一分钟左右（真机 M4：7 次都超过 45 秒），久等只是让这一轮干等。短等一下（默认 10 秒 = gateway.image_wait_ms），
// 没好就先让模型回话，好了再通知它
// 一次要出多张时：一次调用传 images。NovelAI 同一账号一次只接一个任务，网关逐张生成；ComfyUI 并发（上限 4）。
// 同一动作的几个机位加 same_seed：网关挑一个种子每张共用，一次调用出全部机位；reuse_seed 只用于续上一轮的单张

/** 单张生图的结果：idx 是这一批里的第几张（0 起） */
/** bad：脚本退出码 3（intermediate 的角色字段写错，没发请求）时的 stderr 原文（原因 + 正确写法示例），要回给模型让它改了重跑 */
type ImgResult = { idx: number; path: string | null; err: string; bad?: string }

/** 退出码 3 专指 intermediate 的 position_mode / characters 写错（novelai-skill 规格 2.1）。其他退出码照旧只进日志 */
const IM_BAD_INPUT_CODE = 3
/** 正确写法示例只在共享脚本的 stderr 第二行维护一份（与 CBL 侧 2026-10-21 约定），网关只转发、不另写 */
const IM_BAD_INPUT_TAIL = '按上面说的改好 intermediate 再调用一次 generate_image（这次没有发出请求，不是生图服务的问题）。'

/** ComfyUI 本机自带排队，并发上限 4；再高没有收益 */
const IMAGE_CONCURRENCY = 4
/** NovelAI 同一账号一次只接一个任务（中转站回 429「你有正在进行的任务」），同一条回复里的几张逐张生成 */
const NOVELAI_CONCURRENCY = 1

/** 并发跑 tasks，最多 limit 个同时进行；返回顺序与 tasks 一致（失败由调用方从结果里看） */
export async function runPool<T>(tasks: (() => Promise<T>)[], limit: number): Promise<T[]> {
  const out: T[] = new Array(tasks.length)
  let next = 0
  const worker = async (): Promise<void> => {
    for (;;) {
      const i = next++
      if (i >= tasks.length) return
      out[i] = await tasks[i]!()
    }
  }
  await Promise.all(Array.from({ length: Math.max(1, Math.min(limit, tasks.length)) }, () => worker()))
  return out
}

// SKILL.md 是旧 bot 与新 bot 共用的文件，不能改。交付给模型时，把"用一条 bash 命令 & 并发、wait 收口"
// 那段换成新系统等价说法（一次调用传多张、网关并发）。匹配不到（用户改了 SKILL.md）就原样保留，
// 由 imageGuide 在开头补一段系统说明——绝不报错、绝不给出空说明。
// 匹配：从 "**一轮要出多张图（mode=new）" 起，到这一段自己的 ``` 代码块结束；后面紧跟的
// "- " 要点（timeout、某路挂了照常发、revise 不并行）是同一段的说明，一并吃掉。
// 要点列表用可选组：用户删了要点就只换到代码块，不会因为找不到要点而整段不换（也不会贪婪吃后面别的代码块）。
const MULTI_SECTION_RE = /\*\*一轮要出多张图[（(]mode=new[）)][\s\S]*?\n```\n(?:(?:\n- [^\n]*)+\n)?/
const NEW_MULTI_SECTION = `**一轮要出多张图（mode=new）——新系统怎么发**：
- 一轮要几张图（2 张、3 张或更多），就**先把每一张的描述都写好，一次调用 generate_image，用 images 数组全部传进去**（每份 {intermediate, ratio}）。网关逐张生成，跑完把路径一次给你。不要拼 bash、不要 &、不要 wait（你没有 Bash），也不要拆成好几次调用。
- 同一动作换视角的几张（人设《图片内容规则》要求的机位图）：同样一次传 images，**再加 same_seed: true**——网关会让这几张共用同一个种子，场景、床、灯光保持一致，只换机位。各张的机位按规则取不同条目。
- 构图明显不同、各自独立的几张（比如朋友圈配图）：一次传 images，不加 same_seed。
- reuse_seed 只用于续上一轮已经发过的图（单张），不能和 images 一起用。
- 某一张失败不影响其余：成功的照常发，工具会告诉你是第几张没出来，不要因为一张挂了就整批不发。`

/** 把 SKILL.md 里"用一条 bash 命令 & 并发、wait 收口"那段换成新系统说法；没匹配到就原样返回（converted=false） */
export function convertGuideSection(guide: string): { text: string; converted: boolean } {
  if (!MULTI_SECTION_RE.test(guide)) return { text: guide, converted: false }
  return { text: guide.replace(MULTI_SECTION_RE, `${NEW_MULTI_SECTION}\n`), converted: true }
}

/** 交付给模型的"新系统说明"：一开始就点明没有 Bash、两分法怎么走 */
const SYS_GUIDE: Record<'novelai' | 'comfyui', string> = {
  novelai: `【新系统说明】你没有 Bash，不用（也不能）拼脚本、用 & 并行、wait 收口。生图统一走 generate_image：
- 一轮要几张图（2 张、3 张或更多）：先把每张的描述都写好，一次调用 generate_image，用 images 数组全部传进去（每份 {intermediate, ratio}），网关逐张生成，跑完把所有路径给你。不要拆成好几次调用。
- 同一动作换视角的几张（人设《图片内容规则》要求的机位图）：一次传 images 并加 same_seed: true，网关让这几张共用同一个种子，场景一致只换机位。
- 构图明显不同、各自独立的几张（比如朋友圈配图）：一次传 images，不加 same_seed。reuse_seed 只用于续上一轮的单张，不能和 images 一起用。
- 部分失败：成功的照常发，失败的那张会告诉你是第几张。
下面原文里凡是用"跑 bash 命令 & 并发"的写法，都按上面的方式替换。`,
  comfyui: `【新系统说明】你没有 Bash，不用（也不能）拼脚本、用 & 并行、wait 收口。生图统一走 generate_image：
- 要一次出多张（互不依赖的不同构图）：一次调用，用 images 数组传多份 {prompt, ratio}，网关会并发跑（最多 4 张同时），跑完把所有路径给你。
- 部分失败：成功的照常发，失败的那张会告诉你是第几张。
下面原文里凡是用"跑 bash 命令 & 并发"的写法，都按上面的方式替换。`,
}

export type ActionsDeps = {
  /** 朋友圈、TELEGRAM_WORKER_BOT 里用的名字（lifeId，旧系统里的 bot 名） */
  botId: string
  /** 管理台面板键，等于配置里的 id：决定查哪套画风（styles.json 的键、生图脚本的 --agent-name），
   *  也决定图落在 ~/resource/media/<这个名字>/ 下。必填——漏传会静默退回全局兜底画风，所以不给默认值 */
  imageAgent: string
  configPath: string
  log: Logger
  mediaDir: string
  /** 此刻生效的引擎：novelai | comfyui | off（共用开关读得到就按开关，见 image_switch.ts） */
  imageProvider: () => string
  /** 各引擎的生图技能目录（里面的 SKILL.md 是写法说明，novelai 的 .env.local 可以放令牌；comfyui 的脚本在 scripts/ 下） */
  imageSkillDir: (engine: ImageEngine) => string
  /** 壳的全局配置文件（生图开关、ComfyUI 设置都在里面）；不设就用仓库里的 configs/_global.yml */
  globalCfg?: () => string | undefined
  /** 朋友圈数据库（BOTLIFE_STATE_DB），不设就用仓库里的 state.db */
  stateDb: () => string | undefined
  /** 凭据文件里的值（NOVELAI_BEARER_TOKEN 等），只传给对应脚本 */
  credRef: (name: string) => string | null
  /** 生图没在 syncWaitMs 内好：把结果作为系统消息塞回这个聊天 */
  notify: (chatId: string, text: string, key: string) => void
  /** 生图同步等待时长（毫秒，gateway.image_wait_ms）：到点没好就先让模型回话 */
  syncWaitMs: () => number
  /** 跑脚本的方式（不设 = 真起 python 子进程）；白盒测试注入假实现，不起进程也不靠墙钟 */
  runPy?: RunPy
}

/** 跑一次 python 脚本的结果 */
export type PyResult = { code: number | null; out: string; err: string }
/** 跑脚本的方式：argv、额外环境变量、超时毫秒 */
export type RunPy = (args: string[], extra: Record<string, string | undefined>, timeoutMs: number) => Promise<PyResult>

type Result = { text: string; isError?: boolean }

function expand(p: string): string {
  return p.startsWith('~') ? join(homedir(), p.slice(1)) : p
}

/** 生图同步等待：run 在 ms 内完成就返回它，否则返回 'wait'（方案 3.10.3；ms 来自 gateway.image_wait_ms） */
export async function raceWait<T>(run: Promise<T>, ms: number): Promise<T | 'wait'> {
  return Promise.race([run, new Promise<'wait'>(r => setTimeout(() => r('wait'), ms))])
}

export class LifeActions {
  constructor(private readonly d: ActionsDeps) {}

  private env(extra: Record<string, string | undefined> = {}): Record<string, string> {
    const env: Record<string, string> = {
      PATH: process.env.PATH ?? '', HOME: process.env.HOME ?? homedir(), PYTHONIOENCODING: 'utf-8',
      HUB_CONFIGS_DIR: dirname(this.d.configPath), TELEGRAM_WORKER_BOT: this.d.botId,
      ...(process.platform === 'win32' ? { SystemRoot: process.env.SystemRoot ?? '', USERPROFILE: process.env.USERPROFILE ?? '', TEMP: process.env.TEMP ?? '' } : {}),
    }
    const db = this.d.stateDb()
    if (db) env.BOTLIFE_STATE_DB = expand(db)
    // 朋友圈脚本发圈配图时按同一个共用开关挑引擎、调同一份 ComfyUI 脚本（约定 v1.1 5.21）
    const g = this.d.globalCfg?.()
    if (g) env.CLAUDEBOT_GLOBAL_CFG = expand(g)
    env.COMFYUI_SKILL_DIR = expand(this.d.imageSkillDir('comfyui'))
    for (const [k, v] of Object.entries(extra)) if (v) env[k] = v
    return env
  }

  private async py(args: string[], extra: Record<string, string | undefined>, timeoutMs: number): Promise<PyResult> {
    if (this.d.runPy) return this.d.runPy(args, extra, timeoutMs)
    const py = process.env.DSH_BOT_PYTHON || (process.platform === 'win32' ? 'python' : 'python3')
    const p = Bun.spawn([py, ...args], { cwd: REPO_ROOT, env: this.env(extra), stdout: 'pipe', stderr: 'pipe', stdin: 'ignore' })
    const timer = setTimeout(() => p.kill(), timeoutMs)
    const [out, err] = await Promise.all([new Response(p.stdout).text(), new Response(p.stderr).text()])
    clearTimeout(timer)
    return { code: await p.exited, out, err }
  }

  // ─── 生图 ───

  /** 这次用哪个引擎：本 bot 没开生图就是 off，否则按开关。出不了图也不换到另一个引擎（用户 2026-10-16 定） */
  private engine(): ImageEngine | 'off' {
    const current = this.d.imageProvider()
    if (current === 'off') return 'off'
    return current === 'comfyui' ? 'comfyui' : 'novelai'
  }

  imageGuide(): Result {
    const provider = this.engine()
    if (provider === 'off') return { text: '这个 bot 没开生图。', isError: true }
    let guide = ''
    try { guide = readFileSync(join(expand(this.d.imageSkillDir(provider)), 'SKILL.md'), 'utf8') } catch { return { text: '找不到生图说明（SKILL.md）。', isError: true } }
    const how = provider === 'comfyui'
      ? '在这里：按下面的规则写好英文 prompt，作为 generate_image 的 prompt 参数传入。不需要（也不能）跑 Bash 或脚本。'
      : '在这里：按下面的规则写好 intermediate.json 的内容，作为 generate_image 的 intermediate 参数传入；--ratio 对应 ratio 参数，--reuse-seed 对应 reuse_seed 参数。不需要（也不能）跑 Bash 或脚本，也不用写文件。'
    // 交付时把 SKILL.md 里"用一条 bash 命令 & 并发"那段换成新系统说法；匹配不到就原样保留、靠系统说明兜底
    const { text, converted } = convertGuideSection(guide)
    this.d.log.info('tool.image_guide', { provider, converted })
    return { text: `${how}\n\n${SYS_GUIDE[provider]}\n\n${text}` }
  }

  async generateImage(chatId: string, args: Record<string, unknown>): Promise<Result> {
    const provider = this.engine()
    if (provider === 'off') return { text: '这个 bot 没开生图。', isError: true }
    const ws = join(this.d.mediaDir, 'image-work')
    mkdirSync(ws, { recursive: true })
    const stamp = `${Date.now()}-${Math.random().toString(16).slice(2, 6)}`

    // 多张：images 数组一次传几份描述。same_seed（同一动作的几个机位）时网关替这一组挑一个种子、每张共用，
    // 一次调用就能出全部机位（省掉"先出一张拿种子、再调一次"那一轮）；reuse_seed 是续上一轮的图，只配单张
    const imagesIn = Array.isArray(args.images) ? args.images : null
    const multi = !!imagesIn && imagesIn.length > 0
    if (multi && args.reuse_seed === true) {
      return { text: '多张（images）和 reuse_seed 不能一起用：同一动作的几个机位一次传 images 并加 same_seed: true；reuse_seed 只用于续上一轮的单张。', isError: true }
    }
    const sharedSeed = multi && args.same_seed === true && provider !== 'comfyui' ? 1 + Math.floor(Math.random() * (2 ** 31 - 2)) : null
    const items: Record<string, unknown>[] = multi ? (imagesIn as Record<string, unknown>[]) : [args]
    const noun = provider === 'comfyui' ? 'prompt' : 'intermediate'
    for (let i = 0; i < items.length; i++) {
      const it = items[i] && typeof items[i] === 'object' ? (items[i] as Record<string, unknown>) : {}
      const v = it[noun]
      const bad = provider === 'comfyui' ? !(typeof v === 'string' && v.trim()) : !v || (typeof v !== 'object' && typeof v !== 'string')
      if (!bad) continue
      if (multi) return { text: `第 ${i + 1} 张的 ${noun} 不能为空。`, isError: true }
      return { text: provider === 'comfyui' ? 'prompt 不能为空（英文描述）。先调用 image_guide 看写法。' : 'intermediate 不能为空。先调用 image_guide 看写法。', isError: true }
    }

    // same_seed：把共用种子写进这一组专用的配置副本（其余参数与 default_config.json 相同），每张都用它
    let sharedCfg: string | null = null
    if (sharedSeed !== null) {
      const base = join(expand(this.d.imageSkillDir('novelai')), 'assets', 'default_config.json')
      sharedCfg = join(ws, `cfg-${stamp}.json`)
      writeFileSync(sharedCfg, JSON.stringify({ ...JSON.parse(readFileSync(base, 'utf8')), seed: sharedSeed }, null, 1))
    }

    // 单张沿用原来的文件名（im-<stamp>.json / res-<stamp>.json）；多张加 -<序号>，互不覆盖
    const runOne = (it: Record<string, unknown>, idx: number): Promise<ImgResult> => {
      const tag = multi ? `-${idx}` : ''
      if (provider === 'comfyui') {
        const prompt = String(it.prompt).trim()
        const out = join(this.d.mediaDir, `gen-${stamp}${tag}.png`)
        // 共用的 comfyui-skill 脚本（约定 v1.1 5.21 第 4 条）。第一个参数传配置 id（管理台面板键）；
        // 全局配置（ComfyUI 地址、工作流、正负前缀）用开关所在的那份。新系统的 bot 没有锚点配置，不传 --bot-config
        const g = this.d.globalCfg?.()
        const size = ['portrait', 'landscape', 'square', 'wide'].includes(String(it.ratio)) ? ['--size', String(it.ratio)] : []
        const argv = [join(expand(this.d.imageSkillDir('comfyui')), 'scripts', 'comfyui_gen.py'), this.d.imageAgent, prompt, '--out', out,
          '--global', g ? expand(g) : join(REPO_ROOT, 'configs', '_global.yml'), ...size]
        // 失败原因脚本打在 stdout 的 FAIL 行：并进 err 只进日志，不给模型
        return this.py(argv, {}, 600_000)
          .then(r => ({ idx, path: r.code === 0 ? out : null, err: r.code === 0 ? r.err : [r.out.trim().split('\n').pop() ?? '', r.err].filter(Boolean).join('\n') }))
      }
      const im = it.intermediate
      const imPath = join(ws, `im-${stamp}${tag}.json`)
      const resPath = join(ws, `res-${stamp}${tag}.json`)
      writeFileSync(imPath, typeof im === 'string' ? im : JSON.stringify(im, null, 1))
      const skill = expand(this.d.imageSkillDir('novelai'))
      const ratio = ['portrait', 'landscape', 'square', 'wide'].includes(String(it.ratio)) ? String(it.ratio) : 'portrait'
      const cfgPath = sharedCfg ?? join(skill, 'assets', 'default_config.json')
      const argv = [join(skill, 'scripts', 'generate_novelai_image.py'), '--intermediate', imPath, '--config', cfgPath,
        '--ratio', ratio, '--agent-name', this.d.imageAgent, '--session-name', `telegram-${chatId}`, '--output-json', resPath, ...(it.reuse_seed === true ? ['--reuse-seed'] : [])]
      return this.py(argv, { NOVELAI_BEARER_TOKEN: this.d.credRef('NOVELAI_BEARER_TOKEN') ?? undefined, NOVELAI_SKILL_ROOT: skill }, 600_000).then(r => {
        // 退出码 3：输入写错，把 stderr 原文（去掉 [novelai] 前缀，限 600 字）回给模型；不算生图服务故障
        const bad = r.code === IM_BAD_INPUT_CODE ? r.err.replace(/^\[novelai\]\s*/gm, '').trim().slice(0, 600) || 'intermediate 的 characters 写错了' : undefined
        try { return { idx, path: String(JSON.parse(readFileSync(resPath, 'utf8')).image_path || '') || null, err: r.err, bad } } catch { return { idx, path: null, err: r.err, bad } }
      })
    }

    const batch = runPool(items.map((it, idx) => () => runOne(it, idx)), provider === 'comfyui' ? IMAGE_CONCURRENCY : NOVELAI_CONCURRENCY)

    // 整批一起算：全部好的算成功、部分失败只报失败那几张；成功的照常给路径
    const outcome = (rs: ImgResult[]): { result: Result; late: string } => {
      const ok = rs.filter(r => r.path)
      const fails = rs.filter(r => !r.path)
      const many = rs.length > 1
      this.d.log.info('tool.generate_image', { provider, ok: ok.length, total: rs.length })
      for (const f of fails) this.d.log.warn('tool.generate_image_failed', { provider, ...(many ? { index: f.idx + 1 } : {}), err: safeError(f.err.split('\n').filter(Boolean).slice(-2).join(' ')) })
      // 有写错的：把报错首行和正确写法回给模型，让它改了重跑（以前一律说"生图服务出错"，模型看不到原因，连错几次也改不对）
      const bads = fails.filter(f => f.bad)
      const badLines = bads.map(f => `${many ? `第 ${f.idx + 1} 张：` : ''}${f.bad}`).join('\n')
      if (ok.length === 0) {
        const text = bads.length
          ? `生图参数写错了：\n${badLines}\n${IM_BAD_INPUT_TAIL}`
          : '这次没生成出来（生图服务出错）。可以和对方说一声，或者稍后再试。'
        return { result: { text, isError: true }, late: `⟦系统·生图⟧ ${text}` }
      }
      const failNote = fails.length
        ? `\n第 ${fails.map(f => f.idx + 1).join('、')} 张没生成出来${bads.length ? `（参数写错：\n${badLines}\n${IM_BAD_INPUT_TAIL}）` : '（生图服务出错）'}，其余照常发。`
        : ''
      if (!many) {
        return {
          result: { text: `图片生成好了：${ok[0]!.path}\n用 reply 的 files 发给对方，或者用 moments 工具的 set_image 配到朋友圈。` },
          // 晚到的成功通知说清"这就是刚才那次请求的图"，免得模型以为是另一张、又调一次 generate_image（方案 3.10.3）
          late: `⟦系统·生图⟧ 刚才那次生图请求的图片生成好了：${ok[0]!.path}\n这张就是刚才那次请求生成的图，直接用 reply 的 files 发给对方（不用再调用 generate_image）；要配到朋友圈就用 moments 的 set_image。`,
        }
      }
      const list = ok.map(r => `第 ${r.idx + 1} 张：${r.path}`).join('\n')
      return {
        result: { text: `图片生成好了（${ok.length} 张）：\n${list}${failNote}\n用 reply 的 files 发给对方，或者用 moments 工具的 set_image 配到朋友圈。` },
        late: `⟦系统·生图⟧ 刚才那次生图请求的图片生成好了（${ok.length} 张）：\n${list}${failNote}\n这几张就是刚才那次请求生成的图，直接用 reply 的 files 发给对方（不用再调用 generate_image）；要配到朋友圈就用 moments 的 set_image。`,
      }
    }

    const first = await raceWait(batch, this.d.syncWaitMs())
    if (first !== 'wait') return outcome(first).result
    // 整批没等到（默认 10 秒）：先回"还在生成"，全部好了（或部分失败）再塞一条系统消息，路径一次给全
    void batch.then(rs => { this.d.notify(chatId, outcome(rs).late, `image:${stamp}`) })
    return { text: '图片还在生成，大概还要一会儿。先回对方一句（比如"等我一下"），生成好了程序会告诉你图片路径。' }
  }

  // ─── 朋友圈 ───

  async moments(args: Record<string, unknown>): Promise<Result> {
    const s = (k: string) => (typeof args[k] === 'string' || typeof args[k] === 'number' ? String(args[k]).trim() : '')
    const id = (k: string) => (/^\d+$/.test(s(k)) ? s(k) : '')
    const scripts = join(REPO_ROOT, 'scripts')
    let argv: string[]
    switch (s('action')) {
      case 'recent': {
        const days = Math.min(30, Math.max(1, Number(args.days) || 7))
        argv = [join(scripts, 'recent_moments.py'), s('whose') || 'self', String(days)]
        break
      }
      case 'like':
        if (!id('moment_id')) return { text: '要给 moment_id。', isError: true }
        argv = [join(scripts, 'moment_like.py'), id('moment_id')]
        break
      case 'reply': {
        if (!id('moment_id') || !s('text')) return { text: '要给 moment_id 和 text（回复评论时再给 parent_comment_id，评论这条圈本身就是 0）。', isError: true }
        argv = [join(scripts, 'moment_reply.py'), id('moment_id'), id('parent_comment_id') || '0', s('text'), ...(s('image') ? ['--image', s('image')] : [])]
        break
      }
      case 'post':
        // 位置参数不能错位：topic 为空但 visibility 有值时，必须用空串占住 argv[2]，
        // 否则 visibility 会被脚本当成主题读走，本该 private 的圈会按默认可见性发出去。
        // 两者都空时才只传 bot_id。
        argv = [join(scripts, 'bot_post_moment.py'), this.d.botId,
                ...(s('topic') || s('visibility') ? [s('topic')] : []),
                ...(['public', 'private'].includes(s('visibility')) ? [s('visibility')] : [])]
        break
      case 'set_image': {
        const imgs = Array.isArray(args.images) ? args.images.filter((x): x is string => typeof x === 'string') : []
        if (!id('moment_id') || imgs.length === 0) return { text: '要给 moment_id 和 images（图片路径列表）。', isError: true }
        argv = [join(scripts, 'moment_set_image.py'), id('moment_id'), ...imgs.slice(0, 9)]
        break
      }
      case 'delete_comment':
        if (!id('comment_id')) return { text: '要给 comment_id。', isError: true }
        argv = [join(scripts, 'moment_delete_comment.py'), id('comment_id')]
        break
      default:
        return { text: 'action 只能是 recent、like、reply、post、set_image、delete_comment。', isError: true }
    }
    try {
      const r = await this.py(argv, {}, 70_000)
      // 发圈被挡住时脚本先打一行"moment skip: 原因"，最后一行是笼统的"没生成"：把真实原因带上（真机 M4 问题 6）
      const errLines = r.err.trim().split('\n').filter(Boolean)
      const skip = errLines.find(l => l.includes('moment skip:'))?.replace(/^.*moment skip:\s*/, '')
      this.d.log.info('tool.moments', { action: s('action'), code: r.code, ...(skip ? { skip: skip.slice(0, 60) } : {}) })
      const out = (r.out.trim() || (r.code === 0 ? '完成。' : '')).slice(0, 3000)
      if (r.code === 0) return { text: out }
      // 动态已经被主人删了：脚本先查、不写库，报 not found 退出 5。静静丢掉，不当出错（用户 2026-10-16 定）
      if ((s('action') === 'like' || s('action') === 'reply') && r.code === 5 && /^moment \d+ not found$/.test(errLines[errLines.length - 1] ?? '')) {
        return { text: '这条动态已经不在了，不用回，也不用点赞。' }
      }
      const why = (errLines.pop() ?? '').slice(0, 300) || `退出码 ${r.code}`
      return { text: `没做成：${skip ? `被挡住了（${skip}），马上再试也一样。` : ''}${why}`, isError: true }
    } catch (e) {
      return { text: `没做成：${safeError(e)}`, isError: true }
    }
  }
}
