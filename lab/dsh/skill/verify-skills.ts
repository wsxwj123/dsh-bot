// 真 dsh 验证：按需技能机制（人设分层后的"工具与规则"载体）真的能用。
// 干三件事：把技能根指到 <bot 频道目录>/skills；放一个测试技能进去；发一句话让假模型先调 skill 工具再回。
// 用假模型记下 dsh 实际发出的请求，核对：目录可见（<available_skills>）、skill 工具在工具表里、正文真的加载进了上下文。
// 用法：DSH_BOT_HARNESS=$HOME/.dsh-bot/harness bun lab/dsh/skill/verify-skills.ts
import { chmodSync, mkdirSync, readdirSync, symlinkSync, writeFileSync } from 'fs'
import { join } from 'path'
import { startFakeLlm } from '../lib/fake-llm'
import { FakeTelegram } from '../../../gateway/test/fakes/fake-telegram'
import { cleanup, Gateway, makeBot, OWNER, until, writeConfig } from '../../../gateway/test/harness'

const HARNESS = process.env.DSH_BOT_HARNESS ?? ''
if (!HARNESS) { console.error('需要 DSH_BOT_HARNESS=<装了 dsh 的目录>'); process.exit(2) }

const SENTINEL = 'LUMEN-8231'
const SKILL_BODY = `这条规则是测试技能正文。哨兵：${SENTINEL}。`

const problems: string[] = []
function check(ok: boolean, what: string): void {
  console.log(`${ok ? '符合' : '不符合'}  ${what}`)
  if (!ok) problems.push(what)
}

const tg = new FakeTelegram()
const llm = startFakeLlm({
  port: 0,
  // 不用 match（技能目录作为 user 角色消息追加在最后，匹配最后一条 user 文本不可靠）：按请求顺序发剧本。
  script: [
    // 第一步：模型按目录的指引先加载技能
    { tool: { name: 'skill', arguments: { name: 'demo-skill' } } },
    // 第二步（收到技能正文之后）：正常发一条（只有 mcp__tg__reply 发的内容对方才看得到）
    { tool: { name: 'mcp__tg__reply', arguments: { text: '看过了，记住了。' } } },
    // 第三步：工具结果回来后结束这一轮
    { text: '' },
  ],
})
const b = makeBot(tg, {
  gw: { dsh_command: undefined, burst_window_ms: 0 },
  brain: {
    provider: 'fake', model: 'fake-chat',
    routes: { fake: { api: 'openai-completions', baseURL: `http://127.0.0.1:${llm.port}/v1`, apiKeyEnv: 'FAKE_LLM_KEY', retryPolicy: { mode: 'normal', maxRetries: 0 }, models: [{ id: 'fake-chat', contextWindow: 1000000 }] } },
  },
})
delete (b.gw as Record<string, unknown>).dsh_command
writeConfig(b)
mkdirSync(join(b.channelDir, 'skills', 'demo-skill'), { recursive: true })
writeFileSync(join(b.channelDir, 'skills', 'demo-skill', 'SKILL.md'), `---\nname: demo-skill\ndescription: 演示技能：用户点名要这个技能时加载它\n---\n\n${SKILL_BODY}\n`)
// 可选：把一份真实 bot 的技能目录链接进来，核对 dsh 能不能把 6 个技能全部发现
const knownSkills: string[] = []
if (process.env.VERIFY_SKILLS_FROM) {
  for (const name of readdirSync(process.env.VERIFY_SKILLS_FROM)) {
    if (name.startsWith('.')) continue
    symlinkSync(join(process.env.VERIFY_SKILLS_FROM, name), join(b.channelDir, 'skills', name))
    knownSkills.push(name)
  }
}
const cred = join(b.root, 'credentials.yaml')
writeFileSync(cred, 'version: 1\nrefs:\n  FAKE_LLM_KEY: fake-key-for-tests\n')
chmodSync(cred, 0o600)

const gw = new Gateway(b)
let failed = false
try {
  await gw.start({ DSH_BOT_HARNESS: HARNESS })
  tg.pushText(OWNER, '做个技能测试 PLUM-4417')
  await until(() => llm.requests.some(r => JSON.stringify(r.body.messages).includes(SENTINEL)), '技能正文到达模型', 90_000)

  await until(() => tg.sentTo(OWNER).some(s => s.text === '看过了，记住了。'), '加载技能后回复发出去', 60_000)
  const first = llm.requests.find(r => JSON.stringify(r.body.messages).includes('PLUM-4417'))!
  const system = first.body.messages.find((m: any) => m.role === 'system')
  const systemText = typeof system.content === 'string' ? system.content : system.content.map((c: any) => c.text).join('')
  check(systemText.startsWith('# 测试人设'), '系统提示词仍以人设开头')

  const tools = (first.body.tools ?? []).map((t: any) => t.function?.name ?? t.name)
  check(tools.includes('skill'), `skill 工具在工具表里（表里 ${tools.length} 个：${tools.join(', ')}）`)

  const catalog = first.body.messages.find((m: any) => JSON.stringify(m.content ?? '').includes('<available_skills>'))
  check(!!catalog, '模型收到了技能目录（<available_skills>）')
  const catalogText = catalog ? (typeof catalog.content === 'string' ? catalog.content : JSON.stringify(catalog.content)) : ''
  check(catalogText.includes('demo-skill') && catalogText.includes('演示技能'), '目录里有 demo-skill 的名称与描述')
  if (knownSkills.length) {
    const miss = knownSkills.filter(n => !catalogText.includes(n))
    check(miss.length === 0, `真技能全部被发现（${knownSkills.length} 个${miss.length ? '，缺 ' + miss.join(',') : ''}）`)
  }

  const loaded = llm.requests.find(r => JSON.stringify(r.body.messages).includes(SENTINEL))
  const loadedRaw = JSON.stringify(loaded?.body.messages ?? '')
  check(loadedRaw.includes('<skill_content') && loadedRaw.includes('demo-skill'), 'skill 工具被调用，正文以 <skill_content> 进到上下文')
  check(!!loaded, `加载的正文是 SKILL.md 的原文（含哨兵 ${SENTINEL}）`)
  console.log('--- 请求概览（每个请求：是否含技能正文 / 最后一条 user 消息）---')
  for (const r of llm.requests) {
    const raw = JSON.stringify(r.body.messages)
    const users = r.body.messages.filter((m: any) => m.role === 'user')
    const last = JSON.stringify(users.at(-1)?.content ?? '').slice(0, 90)
    console.log(`#${r.n} skill_content=${raw.includes('<skill_content')} 哨兵=${raw.includes(SENTINEL)} 最后user=${last}`)
  }
  check(tg.sentTo(OWNER).some(s => s.text === '看过了，记住了。'), '加载技能之后这一轮正常结束、回复发出去了')

  if (catalogText) console.log('--- 目录原文（模型看到的那条消息）---\n' + catalogText.slice(0, 400))
} catch (e: any) {
  failed = true
  console.error('验证失败：' + (e?.message ?? e))
  console.error('网关 stderr 尾部：\n' + gw.stderr.slice(-1200))
} finally {
  await gw.stop().catch(() => {})
  llm.stop()
  tg.stop()
  cleanup(b)
}
if (problems.length) console.error(`不符合 ${problems.length} 项：` + problems.join('；'))
process.exit(failed || problems.length ? 1 : 0)
