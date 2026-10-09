// 生图和朋友圈（M4）：旧系统里模型用 Bash 跑仓库里的脚本，新系统的模型没有 Bash，
// 改成网关替它跑同一批脚本（不重写），结果交回给模型。
//   generate_image：novelai-skill 的生图脚本（模型按 image_guide 写好结构化描述）或 ComfyUI 脚本；
//                   短等一下（默认 10 秒，gateway.image_wait_ms），生成好就直接返回路径；没好就先返回"还在生成"，好了以后程序再告诉它
//   moments：查最近的朋友圈、点赞、回评论、发圈、给圈配图、删自己的评论
import { mkdirSync, readFileSync, writeFileSync } from 'fs'
import { homedir } from 'os'
import { dirname, join, resolve } from 'path'
import { safeError, type Logger } from '../log'

const REPO_ROOT = resolve(import.meta.dir, '..', '..', '..')
// 生图一般要一分钟左右（真机 M4：7 次都超过 45 秒），久等只是让这一轮干等。短等一下（默认 10 秒 = gateway.image_wait_ms），
// 没好就先让模型回话，好了再通知它

export type ActionsDeps = {
  /** 朋友圈、画风里用的名字（旧系统里的 bot 名） */
  botId: string
  configPath: string
  log: Logger
  mediaDir: string
  /** novelai | comfyui | off */
  imageProvider: () => string
  /** 生图技能目录（里面的 SKILL.md 是写法说明，novelai 的 .env.local 可以放令牌） */
  imageSkillDir: () => string
  /** 朋友圈数据库（BOTLIFE_STATE_DB），不设就用仓库里的 state.db */
  stateDb: () => string | undefined
  /** 凭据文件里的值（NOVELAI_BEARER_TOKEN 等），只传给对应脚本 */
  credRef: (name: string) => string | null
  /** 生图没在 syncWaitMs 内好：把结果作为系统消息塞回这个聊天 */
  notify: (chatId: string, text: string, key: string) => void
  /** 生图同步等待时长（毫秒，gateway.image_wait_ms）：到点没好就先让模型回话 */
  syncWaitMs: () => number
}

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
    for (const [k, v] of Object.entries(extra)) if (v) env[k] = v
    return env
  }

  private async py(args: string[], extra: Record<string, string | undefined>, timeoutMs: number): Promise<{ code: number | null; out: string; err: string }> {
    const py = process.env.DSH_BOT_PYTHON || (process.platform === 'win32' ? 'python' : 'python3')
    const p = Bun.spawn([py, ...args], { cwd: REPO_ROOT, env: this.env(extra), stdout: 'pipe', stderr: 'pipe', stdin: 'ignore' })
    const timer = setTimeout(() => p.kill(), timeoutMs)
    const [out, err] = await Promise.all([new Response(p.stdout).text(), new Response(p.stderr).text()])
    clearTimeout(timer)
    return { code: await p.exited, out, err }
  }

  // ─── 生图 ───

  imageGuide(): Result {
    const provider = this.d.imageProvider()
    if (provider === 'off') return { text: '这个 bot 没开生图。', isError: true }
    let guide = ''
    try { guide = readFileSync(join(expand(this.d.imageSkillDir()), 'SKILL.md'), 'utf8') } catch { return { text: '找不到生图说明（SKILL.md）。', isError: true } }
    const how = provider === 'comfyui'
      ? '在这里：按下面的规则写好英文 prompt，作为 generate_image 的 prompt 参数传入。不需要（也不能）跑 Bash 或脚本。'
      : '在这里：按下面的规则写好 intermediate.json 的内容，作为 generate_image 的 intermediate 参数传入；--ratio 对应 ratio 参数，--reuse-seed 对应 reuse_seed 参数。不需要（也不能）跑 Bash 或脚本，也不用写文件。'
    return { text: `${how}\n\n${guide}` }
  }

  async generateImage(chatId: string, args: Record<string, unknown>): Promise<Result> {
    const provider = this.d.imageProvider()
    if (provider === 'off') return { text: '这个 bot 没开生图。', isError: true }
    const ws = join(this.d.mediaDir, 'image-work')
    mkdirSync(ws, { recursive: true })
    const stamp = `${Date.now()}-${Math.random().toString(16).slice(2, 6)}`
    let run: Promise<{ path: string | null; err: string }>
    if (provider === 'comfyui') {
      const prompt = typeof args.prompt === 'string' ? args.prompt.trim() : ''
      if (!prompt) return { text: 'prompt 不能为空（英文描述）。先调用 image_guide 看写法。', isError: true }
      const out = join(this.d.mediaDir, `gen-${stamp}.png`)
      run = this.py([join(REPO_ROOT, 'scripts', 'comfyui_gen.py'), this.d.botId, prompt, '--out', out], {}, 600_000)
        .then(r => ({ path: r.code === 0 ? out : null, err: r.err }))
    } else {
      const im = args.intermediate
      if (!im || (typeof im !== 'object' && typeof im !== 'string')) return { text: 'intermediate 不能为空。先调用 image_guide 看写法。', isError: true }
      const imPath = join(ws, `im-${stamp}.json`)
      const resPath = join(ws, `res-${stamp}.json`)
      writeFileSync(imPath, typeof im === 'string' ? im : JSON.stringify(im, null, 1))
      const skill = expand(this.d.imageSkillDir())
      const ratio = ['portrait', 'landscape', 'square', 'wide'].includes(String(args.ratio)) ? String(args.ratio) : 'portrait'
      const argv = [join(skill, 'scripts', 'generate_novelai_image.py'), '--intermediate', imPath, '--config', join(skill, 'assets', 'default_config.json'),
        '--ratio', ratio, '--agent-name', this.d.botId, '--session-name', `telegram-${chatId}`, '--output-json', resPath, ...(args.reuse_seed === true ? ['--reuse-seed'] : [])]
      run = this.py(argv, { NOVELAI_BEARER_TOKEN: this.d.credRef('NOVELAI_BEARER_TOKEN') ?? undefined, NOVELAI_SKILL_ROOT: skill }, 600_000).then(r => {
        try { return { path: String(JSON.parse(readFileSync(resPath, 'utf8')).image_path || '') || null, err: r.err } } catch { return { path: null, err: r.err } }
      })
    }
    const done = (r: { path: string | null; err: string }): Result => {
      this.d.log.info('tool.generate_image', { provider, ok: !!r.path })
      if (!r.path) {
        this.d.log.warn('tool.generate_image_failed', { provider, err: safeError(r.err.split('\n').filter(Boolean).slice(-2).join(' ')) })
        return { text: '这次没生成出来（生图服务出错）。可以和对方说一声，或者稍后再试。', isError: true }
      }
      return { text: `图片生成好了：${r.path}\n用 reply 的 files 发给对方，或者用 moments 工具的 set_image 配到朋友圈。` }
    }
    const first = await raceWait(run, this.d.syncWaitMs())
    if (first !== 'wait') return done(first)
    // 没等到（默认 10 秒，gateway.image_wait_ms）：先回"还在生成"，好了以后塞一条系统消息
    void run.then(r => {
      const res = done(r)
      // 晚到的成功通知要说清"这就是刚才那次请求的图"，免得模型以为是另一张、又调一次 generate_image（方案 3.10.3）；失败通知不变
      this.d.notify(chatId, res.isError || !r.path ? `⟦系统·生图⟧ ${res.text}`
        : `⟦系统·生图⟧ 刚才那次生图请求的图片生成好了：${r.path}\n这张就是刚才那次请求生成的图，直接用 reply 的 files 发给对方（不用再调用 generate_image）；要配到朋友圈就用 moments 的 set_image。`, `image:${stamp}`)
    })
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
        argv = [join(scripts, 'bot_post_moment.py'), this.d.botId, s('topic'), ...(['public', 'private'].includes(s('visibility')) ? [s('visibility')] : [])]
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
      const r = await this.py(argv, {}, 50_000)
      // 发圈被挡住时脚本先打一行"moment skip: 原因"，最后一行是笼统的"没生成"：把真实原因带上（真机 M4 问题 6）
      const errLines = r.err.trim().split('\n').filter(Boolean)
      const skip = errLines.find(l => l.includes('moment skip:'))?.replace(/^.*moment skip:\s*/, '')
      this.d.log.info('tool.moments', { action: s('action'), code: r.code, ...(skip ? { skip: skip.slice(0, 60) } : {}) })
      const out = (r.out.trim() || (r.code === 0 ? '完成。' : '')).slice(0, 3000)
      if (r.code === 0) return { text: out }
      const why = (errLines.pop() ?? '').slice(0, 300) || `退出码 ${r.code}`
      return { text: `没做成：${skip ? `被挡住了（${skip}），马上再试也一样。` : ''}${why}`, isError: true }
    } catch (e) {
      return { text: `没做成：${safeError(e)}`, isError: true }
    }
  }
}
