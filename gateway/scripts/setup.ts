// 安装与准备（Mac / Linux / Windows 通用，只依赖 bun 和 npm）。不会改动旧系统的任何文件。
//
//   bun gateway/scripts/setup.ts harness
//       把钉死版本的 dsh 装到 <根>/harness（按仓库 harness/ 里的锁文件，整棵依赖树版本固定）
//   bun gateway/scripts/setup.ts credentials
//       生成凭据文件模板 <根>/credentials.yaml（权限 600），之后请用编辑器把密钥填进去
//   bun gateway/scripts/setup.ts bot <bot 名> --from <旧频道目录> [--port 17950] [--model deepseek-flash]
//       把旧 bot 目录里的人设、白名单、关系、令牌文件"复制"一份到 <根>/bots/<bot 名>/channel，
//       并生成 <根>/configs/<bot 名>.yml。旧目录原封不动。
//   bun gateway/scripts/setup.ts memory <bot 名> --from <旧频道目录> [--claude-home ~/.claude]
//       再导一次旧系统的长期记忆（Claude Code 的 auto-memory）到 <频道目录>/memory/。bot 命令第一次准备时已经自动导过
//   bun gateway/scripts/setup.ts check <配置文件>
//       检查配置、凭据权限、令牌、人设、dsh 是否就绪（不联网，不打印任何机密）
//
// <根> 默认 ~/.dsh-bot，可用环境变量 DSH_BOT_HOME 改。
import { chmodSync, copyFileSync, cpSync, existsSync, mkdirSync, readdirSync, readFileSync, renameSync, writeFileSync } from 'fs'
import { join, resolve } from 'path'
import { checkCredentialsFile, ConfigError, expandHome, loadBotConfig, readTelegramToken, rootDir } from '../src/config'
import { defaultDshCommand } from '../src/dsh/process'

const REPO = resolve(import.meta.dir, '..', '..')
const root = rootDir()
const [cmd, ...rest] = process.argv.slice(2)
const opt = (n: string) => { const i = rest.indexOf(`--${n}`); return i >= 0 ? rest[i + 1] : undefined }
const ok = (s: string) => console.log(`  ✅ ${s}`)
const bad = (s: string) => console.log(`  ⚠️  ${s}`)

async function harness(): Promise<number> {
  const dest = join(root, 'harness')
  mkdirSync(dest, { recursive: true })
  copyFileSync(join(REPO, 'harness', 'package.json'), join(dest, 'package.json'))
  copyFileSync(join(REPO, 'harness', 'package-lock.json'), join(dest, 'package-lock.json'))
  const npm = Bun.which('npm')
  if (!npm) { bad('没找到 npm。请先安装 Node.js（自带 npm）'); return 1 }
  console.log(`安装钉死版本的 dsh 到 ${dest} …`)
  const p = Bun.spawn([npm, 'ci', '--no-audit', '--no-fund'], { cwd: dest, stdout: 'inherit', stderr: 'inherit' })
  if ((await p.exited) !== 0) { bad('npm ci 失败，见上面的输出'); return 1 }
  const [node, bin] = defaultDshCommand(dest)
  const v = Bun.spawn([node!, bin!, '--version'], { stdout: 'pipe', stderr: 'pipe', env: { PATH: process.env.PATH ?? '', DSH_TELEMETRY_DISABLED: '1', DSH_HOME: join(root, 'harness', '.probe-home') } })
  const out = (await new Response(v.stdout).text()).trim()
  await v.exited
  ok(`dsh 已安装：${out || '(版本号读不出来)'}`)
  return 0
}

function credentials(): number {
  const p = join(root, 'credentials.yaml')
  if (existsSync(p)) {
    ok(`凭据文件已存在：${p}（不覆盖）`)
  } else {
    mkdirSync(root, { recursive: true })
    writeFileSync(p, [
      '# dsh 机器人共用的模型密钥。只有你自己能读（权限 600）。',
      '# 把下面的占位文字换成你的 DeepSeek 密钥；用别的供应商路由时，按 apiKeyEnv 的名字再加一行。',
      'version: 1',
      'refs:',
      '  DEEPSEEK_API_KEY: 把这里换成你的密钥',
      '',
    ].join('\n'), { mode: 0o600 })
    ok(`已生成模板：${p}`)
  }
  if (process.platform !== 'win32') chmodSync(p, 0o600)
  console.log('  请用编辑器打开它填入密钥。不要把密钥写在命令行里，也不要贴进聊天。')
  return 0
}

function bot(): number {
  const id = rest[0]
  const from = opt('from')
  if (!id || !/^[A-Za-z0-9_-]+$/.test(id) || !from) { bad('用法：setup.ts bot <bot 名> --from <旧频道目录>'); return 2 }
  const src = resolve(expandHome(from))
  if (!existsSync(join(src, 'CLAUDE.md'))) { bad(`${src} 里没有 CLAUDE.md`); return 1 }
  const botDir = join(root, 'bots', id)
  const ch = join(botDir, 'channel')
  mkdirSync(ch, { recursive: true, mode: 0o700 })
  if (process.platform !== 'win32') chmodSync(botDir, 0o700)
  for (const f of ['CLAUDE.md', 'access.json', 'relationship.json', '.env']) {
    if (!existsSync(join(src, f))) { if (f !== 'relationship.json') bad(`旧目录里没有 ${f}`); continue }
    if (existsSync(join(ch, f))) { ok(`${f} 已存在，不覆盖`); continue }
    copyFileSync(join(src, f), join(ch, f))
    if (f === '.env' && process.platform !== 'win32') chmodSync(join(ch, f), 0o600)
    ok(`已复制 ${f}`)
  }
  // 长期记忆：旧系统里 bot 真正在用的是 Claude Code 的 auto-memory（~/.claude/projects/<路径转写>/memory/），
  // 频道目录里的 memory/ 只是最初的种子。有 auto-memory 就导它，没有才用种子。
  if (!importMemory(src, join(ch, 'memory')) && existsSync(join(src, 'memory')) && !existsSync(join(ch, 'memory'))) {
    cpSync(join(src, 'memory'), join(ch, 'memory'), { recursive: true })
    ok('没找到 Claude Code 的记忆目录，复制了频道目录里的 memory/')
  }
  const cfgDir = join(root, 'configs')
  mkdirSync(cfgDir, { recursive: true })
  const cfg = join(cfgDir, `${id}.yml`)
  if (existsSync(cfg)) {
    ok(`配置已存在：${cfg}（不覆盖）`)
  } else {
    writeFileSync(cfg, [
      `id: ${id}`,
      `display_name: ${id}`,
      `bot_channel_path: ${JSON.stringify(ch)}`,
      `dispatcher_port: ${Number(opt('port') ?? 17950)}   # 本机接口端口，只听 127.0.0.1`,
      '# life_config: ~/旧仓库/configs/<旧名>.yml   # 作息、生活、情绪等设置从旧配置读（M3 起）；这里写了的键优先',
      '',
      'brain:',
      '  provider: deepseek-official',
      `  model: ${opt('model') ?? 'deepseek-flash'}`,
      '  reasoning_effort: low       # off / low / high / max；模型不支持思考时自动忽略',
      '  # max_input_tokens: 200000  # 可选的硬上限，不写就按模型窗口的 80%（M2 起生效）',
      '  # routes:                   # 走自己的转接服务时写这一段，provider 改成路由名',
      '  #   provider-proxy:',
      '  #     api: anthropic-messages',
      '  #     baseURL: http://127.0.0.1:端口',
      '  #     apiKeyEnv: PROVIDER_PROXY_KEY   # 密钥写在 credentials.yaml 的 refs 里',
      '  #     models: [{ id: gemini-3.8-flash-high, contextWindow: 1048576 }]',
      '',
      'gateway:',
      '  timezone: Asia/Shanghai',
      '  # owners: ["你的 Telegram 用户 id"]   # 不写就取 access.json 里 allowFrom 的第一个',
      '',
    ].join('\n'))
    ok(`已生成配置：${cfg}`)
  }
  console.log(`下一步：bun gateway/scripts/setup.ts check ${cfg}`)
  return 0
}

/** 旧频道目录 → Claude Code 的项目目录名（与旧系统 chat_history._project_slug_for 同规则：非字母数字一律换成 -） */
export function claudeProjectSlug(dir: string): string {
  return resolve(dir).replace(/[^a-zA-Z0-9]/g, '-')
}

/**
 * 把 Claude Code 的 auto-memory（MEMORY.md 和主题记忆文件）复制到新系统的记忆目录。不打印任何内容，只报文件数和大小。
 * 新目录里已经有不同内容的 MEMORY.md：先改名备份，不直接覆盖。返回是否找到了旧记忆。
 */
function importMemory(oldChannel: string, destDir: string): boolean {
  const claudeHome = resolve(expandHome(opt('claude-home') ?? '~/.claude'))
  const srcDir = join(claudeHome, 'projects', claudeProjectSlug(oldChannel), 'memory')
  if (!existsSync(srcDir)) return false
  const files = readdirSync(srcDir).filter(f => f.endsWith('.md') && !/\.bak$/.test(f) && f !== 'recent_conversation.md')
  if (files.length === 0) return false
  mkdirSync(destDir, { recursive: true })
  let bytes = 0
  let copied = 0
  let backedUp = 0
  const stamp = new Date().toISOString().slice(0, 10)
  for (const f of files) {
    const from = join(srcDir, f)
    const to = join(destDir, f)
    const data = readFileSync(from)
    if (existsSync(to)) {
      if (readFileSync(to).equals(data)) continue
      renameSync(to, `${to}.before-import-${stamp}`)
      backedUp++
    }
    writeFileSync(to, data)
    copied++
    bytes += data.length
  }
  if (copied === 0) ok(`Claude Code 的长期记忆（${files.length} 个文件）和新目录里的一样，没有改动`)
  else {
    const same = files.length - copied
    const notes = ['旧文件不动', ...(same ? [`${same} 个和新目录里的一样，跳过`] : []), backedUp ? `新目录里原有的 ${backedUp} 个同名文件已改名备份（.before-import-${stamp}）` : '新目录里没有同名文件，不用备份']
    ok(`已导入 Claude Code 的长期记忆：${copied} 个文件，共 ${bytes} 字节（${notes.join('；')}）`)
  }
  return true
}

function memory(): number {
  const id = rest[0]
  const from = opt('from')
  if (!id || !from) { bad('用法：setup.ts memory <bot 名> --from <旧频道目录> [--claude-home ~/.claude]'); return 2 }
  const cfgPath = join(root, 'configs', `${id}.yml`)
  if (!existsSync(cfgPath)) { bad(`没有 ${cfgPath}，先跑 setup.ts bot`); return 1 }
  const cfg = loadBotConfig(cfgPath)
  if (!importMemory(resolve(expandHome(from)), cfg.memoryDir)) { bad('没找到这个 bot 在 Claude Code 里的记忆目录'); return 1 }
  return 0
}

function check(): number {
  const path = rest[0]
  if (!path) { bad('用法：setup.ts check <配置文件>'); return 2 }
  let problems = 0
  try {
    const cfg = loadBotConfig(path)
    ok(`配置能读：bot ${cfg.id}，模型 ${cfg.brain.provider} / ${cfg.brain.model}，时区 ${cfg.gw.timezone}`)
    existsSync(join(cfg.channelDir, 'CLAUDE.md')) ? ok('人设文件在') : (bad(`缺人设：${join(cfg.channelDir, 'CLAUDE.md')}`), problems++)
    try { readTelegramToken(cfg.channelDir, {}); ok('Telegram 令牌在 channel/.env 里') } catch (e) { bad((e as Error).message); problems++ }
    const cred = checkCredentialsFile(cfg.credentialsPath)
    if (cred) { bad(cred); problems++ } else {
      ok('凭据文件在，权限正确')
      // 只看密钥"长得像不像真的"，绝不打印值
      let refs: Record<string, unknown> = {}
      try { refs = ((Bun.YAML.parse(readFileSync(cfg.credentialsPath, 'utf8')) as { refs?: Record<string, unknown> })?.refs) ?? {} } catch { bad('凭据文件不是合法的 YAML'); problems++ }
      const need = new Map<string, RegExp>()
      if (cfg.brain.provider === 'deepseek-official') need.set('DEEPSEEK_API_KEY', /^sk-[A-Za-z0-9_-]{16,}$/)
      for (const r of Object.values(cfg.brain.routes)) if (r.apiKeyEnv) need.set(r.apiKeyEnv, /^[\x21-\x7e]{8,}$/)
      for (const [name, shape] of need) {
        const v = refs[name]
        if (typeof v !== 'string' || !v.trim()) { bad(`凭据文件里没有 ${name}`); problems++ }
        else if (!shape.test(v.trim()) || /[<>]/.test(v)) { bad(`${name} 看起来还是占位文字，或者格式不对（没有显示内容）`); problems++ }
        else ok(`${name} 已填，格式像真的密钥`)
      }
    }
    cfg.gw.owners.length ? ok(`主人：${cfg.gw.owners.length} 个`) : (bad('没有主人（access.json 的 allowFrom 是空的）：/clear 和故障通知都用不了'), problems++)
    const [, bin] = defaultDshCommand(cfg.harnessDir)
    existsSync(bin!) ? ok(`dsh 已安装在 ${cfg.harnessDir}`) : (bad('dsh 还没装：先跑 setup.ts harness'), problems++)
    Bun.which('node') ? ok('node 在') : (bad('没找到 node（dsh 要用 node 运行）'), problems++)
  } catch (e) {
    bad(e instanceof ConfigError ? e.message : String((e as Error).message ?? e))
    return 1
  }
  console.log(problems ? `还有 ${problems} 项要处理。` : '全部就绪。')
  return problems ? 1 : 0
}

const run: Record<string, () => number | Promise<number>> = { harness, credentials, bot, memory, check }
const fn = cmd ? run[cmd] : undefined
if (!fn) {
  console.log('用法：bun gateway/scripts/setup.ts harness | credentials | bot <名> --from <旧频道目录> | memory <名> --from <旧频道目录> | check <配置文件>')
  process.exit(2)
}
process.exit(await fn())
