// 生成本 bot 的 dsh 补丁层（"只留人设"）。补丁按 JSON 写：YAML 是 JSON 的超集，dsh 能直接读，人设里任何字符都不用手工转义。
// 人设只做技术处理（{{ 中间插不可见字符），不改动任何文字。
import { createHash } from 'crypto'
import { readdirSync, readFileSync } from 'fs'
import { join } from 'path'
import type { Brain } from '../config'
import { ALL_ON, type Features } from '../features'

/**
 * 要禁用的出厂行（0.1.5 与 0.2.0 的并集，不存在的行 dsh 只打一条警告）：
 * - 全部内置工具与工具说明
 * - agent-instructions：不读 AGENTS.md / CLAUDE.md（人设由我们自己注入）
 * - 隐私：session-log-deepseek（0.2.0 默认随请求上传会话日志）、plugin-package-inventory-deepseek（插件清单）、session-telemetry-otel（遥测）
 * - mcp-resources（挂 MCP 时自动加 3 个资源工具并在系统提示词末尾追加一段）
 * - session-title-llm（多一次请求给会话起标题）
 * skill / skill-filesystem / tool-skill 不再禁用：人设分层后，工具与规则按需放进技能文件，
 * 由这三个插件把技能目录给模型看、按需加载（技能根配置见 buildPatchRows 里的 skill-filesystem 行）。
 */
export const DISABLE_ROWS = [
  'tool-bash', 'tool-pwsh', 'tool-jobs', 'tool-fs', 'tool-fs-search',
  'agent-instructions',
  'commands', 'command-feedback', 'goal', 'goal-round-driver', 'command-goal', 'plan-mode', 'command-compact',
  'tool-subagent-control', 'tool-subagent-list-agents', 'tool-subagent', 'tool-subagent-fork',
  'workflow-worker-thread', 'tool-workflow', 'tool-todo', 'tool-goal', 'tool-ralph', 'repeat-tool-reminder',
  'web', 'web-search-deepseek', 'web-fetch-http', 'tool-web',
  'session-log-deepseek', 'plugin-package-inventory-deepseek', 'session-telemetry-otel',
  'session-title-llm', 'mcp-resources',
]

/** dsh 把成对的 {{x}} 当模板变量，未注册就报错。在两个左花括号之间插入 U+2060（不可见），模型看到的字不变。 */
export function escapePersona(text: string): string {
  return text.replace(/\{\{/g, '{⁠{')
}

/** 工具服务在 dsh 里的名字。模型看到的工具名是 mcp__tg__reply 这种形式。 */
export const MCP_SERVER_NAME = 'tg'
export const toolName = (t: string) => `mcp__${MCP_SERVER_NAME}__${t}`

/** 文字语言没设时按它；语音语言没设时跟生效的文字语言（约定 5.14，补充文件 11.4） */
export const DEFAULT_TEXT_LANGUAGE = '简体中文'

/** access.json 里 textLanguage、voiceLanguage 的原值（可能没设，也可能是手改出来的不合规值） */
export type Languages = { text?: unknown; voice?: unknown }

/** 语言名不许有的字符（同适配服务 11.3 的校验；换行与 Python str.splitlines 同一个口径） */
const LANGUAGE_BAD = /[\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029\[\]<>{}`|]/

/** 存的语言名 → 能用的名字（去掉首尾空白）；空、超过 20 个字符、带禁用字符、不是字符串的当没设 */
function languageName(v: unknown): string | undefined {
  if (typeof v !== 'string' || LANGUAGE_BAD.test(v)) return undefined
  const s = v.trim()
  return s && [...s].length <= 20 ? s : undefined
}

/** 生效的两种语言：文字语言缺省「简体中文」，语音语言缺省同文字语言 */
export function effectiveLanguages(l: Languages = {}): { text: string; voice: string } {
  const text = languageName(l.text) ?? DEFAULT_TEXT_LANGUAGE
  return { text, voice: languageName(l.voice) ?? text }
}

/** 运行规则里的语言一节（补充文件 11.4，语义照 v0.9 claude 侧草案第二节），按这只 bot 的两种语言现填。
 *  语音关着（约定 v1.1 5.19）只留文字语言那一句；语音模型生效为 s1（5.14.5 第 6 条）时不写方括号标签 */
function languageRules(l: Languages, voiceOn = true, voiceModel = ''): string[] {
  const { text, voice } = effectiveLanguages(l)
  if (!voiceOn) return ['', '### 语言', `- 文字语言是${text}。文字气泡始终用文字语言写。`]
  if (voiceModel === 's1') return [
    '',
    '### 语言',
    `- 文字语言是${text}，语音语言是${voice}。文字气泡始终用文字语言写。`,
    `- 发语音（${toolName('reply')} 带 as_voice=true）时 text 与 voice_text 都要写，两种语言相同时也要写。`,
    '- text 用文字语言写，只显示、不朗读，不加语气标签。',
    '- voice_text 用语音语言写，只朗读、不显示。不写方括号语气标签，语气靠用词表达；voice_instruct 和 voice_emotion 也不填。',
    '- 两种语言相同时，voice_text 的内容与 text 一致，去掉不适合朗读的符号与表情。',
    '- voice_text 的段数与 text 一致（按空行分段），一一对应。',
  ]
  return [
    '',
    '### 语言',
    `- 文字语言是${text}，语音语言是${voice}。文字气泡始终用文字语言写。`,
    `- 发语音（${toolName('reply')} 带 as_voice=true）时 text 与 voice_text 都要写，两种语言相同时也要写。`,
    '- text 用文字语言写，只显示、不朗读，不加语气标签。',
    '- voice_text 用语音语言写，只朗读、不显示。每一段至少带一个方括号语气指令，指令也用语音语言写，简短，建议 8 字以内。',
    '- 两种语言相同时，voice_text 的内容与 text 一致，加上语气标签，去掉不适合朗读的符号与表情。',
    '- voice_text 的段数与 text 一致（按空行分段），一一对应。',
  ]
}

/** 生图、朋友圈那一条：两项都开是原来那一整句；关掉的那半句去掉，都关就没有这一条（约定 v1.1 5.19） */
function imageMomentsRule(f: Features): string[] {
  const image = `要发照片（自拍、拍给对方看的东西）时，先用 ${toolName('image_guide')} 看写法，再用 ${toolName('generate_image')} 生成，然后用 reply 的 files 发出去。`
  const moments = `朋友圈相关（看最近的圈、点赞、评论、发圈、配图）用 ${toolName('moments')}。`
  const parts = [...(f.image ? [image] : []), ...(f.moments ? [moments] : [])]
  return parts.length ? [`- ${parts.join('')}`] : []
}

/**
 * 运行规则：放在人设后面，是单独的一段，不改人设文件。末尾是按 access.json 现填的语言一节。
 * 功能开关（约定 v1.1 5.19）关掉的：对应的工具那一条去掉，再说一句不能用；九项都开时与没有开关时逐字相同。
 */
export function runtimeRules(lang: Languages = {}, f: Features = ALL_ON, voiceModel = ''): string {
  return [
    '## 运行规则（程序附加，不属于人设）',
    `- 你在 Telegram 上和对方聊天。对方只能看到你用 ${toolName('reply')} 发出的内容，你直接输出的文字对方看不到。`,
    '- 一次 reply 可以写几段，段与段之间空一行，会按段依次发出。发完就结束这一轮，不要再输出别的文字。',
    `- 按人设决定这次不回复时，调用 ${toolName('stay_silent')} 并写明原因（原因不会发给对方）。`,
    `- 想用表情回应某条消息，用 ${toolName('react')}。消息编号是每条消息开头 ⟦…⟧ 里 # 后面的数字。`,
    // sticker 这条引用「人设里的表情包规则」与技能 skill-gif，是 bot4 频道的资产；
    // bot2/bot3 将来上新栈时按各自频道目录核对（不为其做探测逻辑）。
    ...(f.stickers ? [`- 情绪上来的时候（开心、撒娇、吃醋、害羞、生气），可以用 ${toolName('sticker')} 发表情包表达，先带关键词 list 挑一条、再 send 发到聊天里。发不发、发多勤、发哪一档按人设里的表情包规则来（细则见技能 skill-gif）。`] : []),
    `- 值得长期记住的事（对方的喜好、重要的日子、你们的约定、你们之间发生的事），用 ${toolName('remember')} 记下来，一次一条。不要记密码、证件号这类敏感信息。`,
    ...imageMomentsRule(f),
    `- 答应对方将来要做的事（"三点提醒你""明早叫你"），用 ${toolName('commitment_create')} 登记时间，到点程序会提醒你；已经做到了就用 ${toolName('commitment_done')} 标成完成；不打算做了、对方说不用了、登记错了，才用 ${toolName('commitment_cancel')} 取消。答应每天都要做的事（比如每天早上叫对方起床），登记时写 repeat=daily，when 写几点，并写明早上还是晚上，比如"早上七点"。`,
    '- 以 ⟦ 开头的内容是程序给你的说明（时间、前情、系统提示等），不是对方说的话，不要在回复里提到它们。',
    '- 工具会告诉你每段是否送达。没送达的段对方没看到；已送达的段不要重发。',
    ...(f.image ? [] : ['- 你不能发图。']),
    ...(f.stickers ? [] : ['- 你不能发表情包。']),
    ...(f.voice ? [] : ['- 你不能发语音，只发文字。']),
    ...languageRules(lang, f.voice, voiceModel),
  ].join('\n')
}

/**
 * 交给 dsh 的重试策略。dsh 的默认策略把"正常结束但一个内容块都没有"（EMPTY_RESPONSE）也算可重试：
 * 模型照运行规则"发完就结束"回了空，会被原样再请求 5 次（账本 B23）。这里只留真失败的四种。
 * 只写这两个键，次数和退避沿用 dsh 的默认值；retryableCodes 不能是空数组（dsh 会拒收）。
 * 写法在 dsh 0.2.0-rc.2 上实测过，升级 dsh 后重跑 lab/dsh/offline/b23-*.ts 核对。
 */
export const RETRY_POLICY = { mode: 'normal', retryableCodes: ['RATE_LIMIT', 'SERVER', 'TIMEOUT', 'TRANSPORT'] }

export type PatchInput = {
  persona: string
  brain: Brain
  credentialsPath: string
  sessionsRoot: string
  /** 本 bot 的技能根目录（里面的 <名>/SKILL.md 或 <名>.md 会被模型按需加载）。 */
  skillDir: string
  /** access.json 的 textLanguage、voiceLanguage（补充文件 11.4）；不给按缺省 */
  languages?: Languages
  /** 技能文件的内容指纹（skillsDigest，补充文件 11.5）；变了按人设变了处理 */
  skillsDigest?: string
  /** 功能开关（约定 v1.1 5.19）：运行规则与工具都跟着它；不给按九项全开 */
  features?: Features
  /** 生效的语音模型（约定 v1.1 5.14.5）：s1 时语言一节不写方括号标签 */
  voiceModel?: string
}

export function buildPatchRows(p: PatchInput): object[] {
  const rows: object[] = []
  const b = p.brain
  // 路由自己写了 retryPolicy 的原样交给 dsh（不改、不合并），没写的补上。
  // 补在这一行里、不动 b.routes：重启指纹按 b.routes 算，路由没变就不该因为这个多重启一次。
  const routes = Object.entries(b.routes).map(([name, r]) => [name, r.retryPolicy === undefined ? { ...r, retryPolicy: RETRY_POLICY } : r])
  if (routes.length > 0) rows.push({ id: 'llm-pi-ai', config: { providers: Object.fromEntries(routes) } })
  // 内置 deepseek-official 路由的策略放在它自己的行里。不管当前用哪家都下发：
  // 运行中用 /model 切到内置路由不重启 dsh，到那时再补就晚了。
  rows.push({ id: 'llm-deepseek', config: { retryPolicy: RETRY_POLICY } })
  rows.push({ id: 'credentials', config: { path: p.credentialsPath } })
  // 技能只从这个 bot 自己的目录发现：关掉项目根/用户根（否则 <cwd>/.dsh/skills、<DSH_HOME>/skills 会成为额外来源）。
  rows.push({ id: 'skill-filesystem', config: { includeDefaultRoots: false, customSkillDirs: [p.skillDir] } })
  rows.push({ id: 'acp', config: { provider: b.provider, model: b.model } })
  rows.push({ id: 'agent-default-model', config: { provider: b.provider, model: b.model } })
  rows.push({ id: 'system-prompt', config: {
    includeHarnessIdentity: false,
    includeRuntimeContext: false,
    personaPrefix: escapePersona(p.persona),
    personaSuffix: escapePersona(runtimeRules(p.languages, p.features, p.voiceModel)),
  } })
  rows.push({ id: 'session-persistence-jsonl', config: { root: p.sessionsRoot, compression: 'none' } })
  rows.push({ id: 'compaction-basic', config: b.emergencyCompaction ? {} : { auto: false } })
  for (const id of DISABLE_ROWS) rows.push({ id, disabled: true })
  return rows
}

export function patchText(rows: object[]): string {
  return JSON.stringify(rows, null, 1) + '\n'
}

/**
 * 补丁层里"需要重启 dsh 才生效"的部分的指纹：人设、路由、凭据路径、压缩开关、技能根目录，
 * 以及技能文件的内容与生效的两种语言（补充文件 11.4、11.5：变了按人设变了处理）。
 * 只换模型（provider + model 在已有路由里）不需要重启，走 set_config_option。
 */
export function restartFingerprint(p: PatchInput): string {
  const b = p.brain
  // 功能开关变了也要重启：给模型的工具清单跟着开关变，dsh 只在启动时取一次（约定 v1.1 5.19.5）
  return createHash('sha256').update(JSON.stringify([p.persona, b.routes, p.credentialsPath, b.emergencyCompaction, p.sessionsRoot, p.skillDir, p.skillsDigest ?? '', effectiveLanguages(p.languages), p.features ?? null, p.voiceModel === 's1'])).digest('hex').slice(0, 16)
}

/**
 * 系统提示词的指纹：人设、运行规则（含语言一节）、技能文件、dsh 版本。指纹变了，旧会话就不再接着用：
 * dsh 对 deepseek 这类模型（systemPromptUpdate: in-history）会把新的系统提示词整份追加进历史，
 * 旧的那份还留着，以后每轮都要再付一遍（M2 真机报告问题 2）。
 */
export function promptFingerprint(p: PatchInput, harnessVersion: string): string {
  return createHash('sha256').update(JSON.stringify([p.persona, runtimeRules(p.languages, p.features, p.voiceModel), harnessVersion, p.skillsDigest ?? ''])).digest('hex').slice(0, 16)
}

/**
 * 技能文件的内容指纹（补充文件 11.5）：<skillDir>/<名>/SKILL.md 按名字排好，名字与内容一起算。
 * 改了、加了、删了哪一个都会变；目录不在是空串。
 */
export function skillsDigest(skillDir: string): string {
  let names: string[]
  try { names = readdirSync(skillDir).sort() } catch { return '' }
  const h = createHash('sha256')
  for (const name of names) {
    let body: Buffer
    try { body = readFileSync(join(skillDir, name, 'SKILL.md')) } catch { continue }
    h.update(`${name}\0${body.length}\0`).update(body)
  }
  return h.digest('hex').slice(0, 16)
}

export function modelValue(b: Pick<Brain, 'provider' | 'model'>): string {
  return JSON.stringify([b.provider, b.model])
}
