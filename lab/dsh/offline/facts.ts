// 离线事实核对：不需要任何密钥。用假模型服务代替 DeepSeek，逐条核对 dsh 的行为，输出“符合 / 不符合 / 仅记录”。
// 用法：DSH=<dsh 可执行文件> bun lab/dsh/offline/facts.ts
// 升级 dsh 之后重跑一遍，对照上一次的报告即可看出行为变化。
import { mkdirSync, writeFileSync, chmodSync } from 'fs'
import { join } from 'path'
import { AcpClient } from '../lib/acp-client'
import { startFakeLlm, type Step } from '../lib/fake-llm'
import { handleMcp, type McpTool } from '../lib/mcp-http'
import { DISABLE_ROWS, dshBin, dshCmd, dshEnv, dshVersion, escapePersona, makeSandbox, personaOnlyRows, runsDir, samplePersona, shippedRows, type Sandbox } from '../lib/profile'
import { Report } from '../lib/report'

const out = runsDir(`facts-${Date.now()}`)
const version = await dshVersion()
const report = new Report(out, `dsh 离线事实核对（${version}）`)
type Verdict = '符合' | '不符合' | '仅记录' | '出错'
const rows: [string, string, Verdict, string][] = []
function record(id: string, fact: string, verdict: Verdict, observed: string) {
  rows.push([id, fact, verdict, observed])
  console.log(`[${verdict}] ${id} ${fact} —— ${observed}`)
}
async function check(id: string, fact: string, fn: () => Promise<[Verdict, string]>) {
  try { const [v, o] = await fn(); record(id, fact, v, o) } catch (e: any) { record(id, fact, '出错', String(e?.message ?? e).slice(0, 300)) }
}
const ok = (cond: boolean): Verdict => (cond ? '符合' : '不符合')

const fake = startFakeLlm({ port: 0 })
const fakeUrl = `http://127.0.0.1:${fake.port}`
const fakeRoute = { kind: 'openai-compatible' as const, name: 'fake', baseURL: `${fakeUrl}/v1`, apiKeyEnv: 'FAKE_LLM_KEY', model: 'fake-chat', contextWindow: 1_000_000 }
let sbN = 0
function sandbox(rows: object[]): Sandbox { return makeSandbox(join(out, `sb${++sbN}`), rows) }
async function bot(sb: Sandbox, extraEnv: Record<string, string> = {}): Promise<AcpClient> {
  const c = new AcpClient(dshCmd(sb), { env: dshEnv(sb, { FAKE_LLM_KEY: 'dummy-not-a-key', ...extraEnv }), cwd: sb.work })
  await c.initialize()
  return c
}
// 需要剧本的核对项各自起一个带剧本的假服务
function scriptedFake(steps: Step[]) { return startFakeLlm({ port: 0, script: steps }) }

// ── C1–C8：握手、会话、模型、续接（出厂 acp，路由指向假服务）──
{
  const sb = sandbox(shippedRows({ route: fakeRoute }))
  let c = new AcpClient(dshCmd(sb), { env: dshEnv(sb, { FAKE_LLM_KEY: 'dummy-not-a-key' }), cwd: sb.work })
  const t0 = Date.now()
  const init = await c.initialize()
  await check('C1', '握手：ACP v1，服务端名 deepseek-harness-acp，支持 HTTP MCP 与 close/list/resume', async () => {
    const caps = init.agentCapabilities
    return [ok(init.protocolVersion === 1 && init.agentInfo?.name === 'deepseek-harness-acp' && caps?.mcpCapabilities?.http === true && !!caps?.sessionCapabilities?.resume && !!caps?.sessionCapabilities?.close && !!caps?.sessionCapabilities?.list),
      `protocolVersion=${init.protocolVersion} agent=${init.agentInfo?.name}@${init.agentInfo?.version} 启动+握手 ${Date.now() - t0}ms caps=${JSON.stringify(caps)}`]
  })
  let sid = ''
  let s1: any
  await check('C2', 'session/new：会话 id 由服务端生成；cwd 必须是绝对路径', async () => {
    s1 = await c.request('session/new', { cwd: sb.work, mcpServers: [] })
    sid = s1.sessionId
    let rel = ''
    try { await c.request('session/new', { cwd: 'relative/dir', mcpServers: [] }) } catch (e: any) { rel = `${e.code} ${e.message}` }
    return [ok(/^[0-9a-f-]{36}$/.test(sid) && rel.includes('absolute')), `sessionId=${sid}；相对路径 → ${rel}`]
  })
  await check('C3', 'configOptions 里列出可选模型（按供应商分组）', async () => {
    const model = s1.configOptions.find((o: any) => o.id === 'model')
    const effort = s1.configOptions.find((o: any) => o.id === 'reasoning_effort')
    const values = model.options.flatMap((g: any) => g.options.map((o: any) => o.value))
    // 思考强度选项只在当前模型支持思考时出现（假模型不支持，所以这里只记录）
    return [ok(values.length > 0), `当前=${model.currentValue}；可选=${values.join(' ')}；思考强度=${effort ? effort.options.map((o: any) => o.value).join('/') : '当前模型不支持，不出现该选项'}`]
  })
  await check('C4', 'session/set_config_option：按会话切模型，非法值报 -32602', async () => {
    const model = s1.configOptions.find((o: any) => o.id === 'model')
    const target = model.options[0].options.at(-1).value
    const r = await c.request('session/set_config_option', { sessionId: sid, configId: 'model', value: target })
    let bad = ''
    try { await c.request('session/set_config_option', { sessionId: sid, configId: 'model', value: JSON.stringify(['nope', 'nope']) }) } catch (e: any) { bad = `${e.code} ${e.message}` }
    const cur = r.configOptions.find((o: any) => o.id === 'model').currentValue
    // 切回假模型，后面要用
    await c.request('session/set_config_option', { sessionId: sid, configId: 'model', value: JSON.stringify(['fake', 'fake-chat']) })
    return [ok(cur === target && bad.startsWith('-32602')), `切到 ${cur}；非法值 → ${bad}`]
  })
  await check('C5', 'session/list 不列活跃会话；close 之后才列出', async () => {
    const before = await c.request('session/list', {})
    await c.prompt(sid, '你好') // 让会话落盘
    await c.request('session/close', { sessionId: sid })
    const after = await c.request('session/list', {})
    return [ok(!before.sessions.some((s: any) => s.sessionId === sid) && after.sessions.some((s: any) => s.sessionId === sid)), `关闭前 ${before.sessions.length} 个；关闭后 ${after.sessions.length} 个`]
  })
  await c.close()
  c = await bot(sb)
  await check('C6', 'session/resume：cwd 必须一致；不回放历史；同一会话不能重复续接', async () => {
    let other = ''
    mkdirSync(join(sb.work, 'other'), { recursive: true })
    try { await c.request('session/resume', { sessionId: sid, cwd: join(sb.work, 'other'), mcpServers: [] }) } catch (e: any) { other = `${e.code} ${e.message}` }
    const n0 = c.updates.length
    const r = await c.request('session/resume', { sessionId: sid, cwd: sb.work, mcpServers: [] })
    const replayed = c.updates.length - n0
    let twice = ''
    try { await c.request('session/resume', { sessionId: sid, cwd: sb.work, mcpServers: [] }) } catch (e: any) { twice = e.message }
    const cur = r.configOptions.find((o: any) => o.id === 'model').currentValue
    return [ok(other.includes('cwd does not match') && replayed === 0 && twice.includes('already active')), `换 cwd → ${other}；回放更新数=${replayed}；续接后模型=${cur}；重复续接 → ${twice}`]
  })
  await check('C7', '同一会话同时只能有一个 prompt', async () => {
    // 两个 prompt 紧挨着发：第一个还在飞时第二个到达，应被拒
    const p1 = c.prompt(sid, 'A').then(r => r.stopReason).catch((e: any) => e.message)
    const p2 = c.prompt(sid, 'B').then(r => r.stopReason).catch((e: any) => e.message)
    const [a, b] = await Promise.all([p1, p2])
    return [ok(String(b).includes('already in flight')), `第一个 → ${a}；第二个 → ${b}`]
  })
  await c.close()
}
{
  const sb = sandbox(shippedRows({ route: { kind: 'deepseek', model: 'deepseek-v4-flash' } }))
  const c = new AcpClient(dshCmd(sb), { env: dshEnv(sb), cwd: sb.work })
  await c.initialize()
  await check('C8', '没有密钥时 prompt 以 JSON-RPC 错误返回（不是 stopReason）', async () => {
    const s = await c.request('session/new', { cwd: sb.work, mcpServers: [] })
    try { const r = await c.prompt(s.sessionId, '你好'); return ['不符合', `返回了 ${JSON.stringify(r)}`] } catch (e: any) { return [ok(e.code === -32603), `${e.code} ${e.message.slice(0, 160)}`] }
  })
  await c.close()
}

// ── C9–C11：只留人设 ──
const persona = samplePersona()
async function firstRequest(rows: object[], files?: (sb: Sandbox) => void) {
  const f = startFakeLlm({ port: 0 })
  const route = { ...fakeRoute, baseURL: `http://127.0.0.1:${f.port}/v1` }
  const fixed = rows.map((r: any) => (r.id === 'llm-pi-ai' ? { ...r, config: { providers: { fake: { ...r.config.providers.fake, baseURL: route.baseURL } } } } : r))
  const sb = sandbox(fixed)
  files?.(sb)
  const c = await bot(sb)
  const s = await c.request('session/new', { cwd: sb.work, mcpServers: [] })
  let err = ''
  try { await c.prompt(s.sessionId, '在吗') } catch (e: any) { err = e.message }
  await c.close()
  f.stop()
  return { body: f.requests[0]?.body, err }
}
const plant = (sb: Sandbox) => {
  writeFileSync(join(sb.work, 'CLAUDE.md'), '# 工作目录里的 CLAUDE.md（不应进入上下文）\n')
  writeFileSync(join(sb.dshHome, 'AGENTS.md'), '# $DSH_HOME/AGENTS.md（不应进入上下文）\n')
}
const sysText = (body: any) => (body?.messages ?? []).filter((m: any) => m.role === 'system').map((m: any) => (typeof m.content === 'string' ? m.content : JSON.stringify(m.content))).join('\n')
await check('C9', '只留人设：系统提示词逐字等于人设；0 个工具；不注入 AGENTS.md / CLAUDE.md / 技能目录 / 运行时上下文', async () => {
  const { body, err } = await firstRequest(personaOnlyRows({ persona, route: fakeRoute }), plant)
  const sys = sysText(body)
  const nonSys = (body?.messages ?? []).filter((m: any) => m.role !== 'system')
  const good = !err && sys === escapePersona(persona) && (body?.tools ?? []).length === 0 && nonSys.length === 1
  return [ok(good), `系统提示词 ${sys.length} 字（人设 ${persona.length} 字）；工具 ${(body?.tools ?? []).length} 个；非系统消息 ${nonSys.length} 条 ${err}`]
})
await check('C10', '出厂配置对照（同一份人设）：多出的工具说明与注入消息', async () => {
  const { body } = await firstRequest(shippedRows({ route: fakeRoute, persona }), plant)
  const nonSys = (body?.messages ?? []).filter((m: any) => m.role !== 'system')
  const injected = nonSys.map((m: any) => (typeof m.content === 'string' ? m.content : JSON.stringify(m.content))).filter((t: string) => /AGENTS|CLAUDE|runtime context|available_skills/i.test(t))
  return ['仅记录', `系统提示词 ${sysText(body).length} 字；工具 ${(body?.tools ?? []).length} 个（${JSON.stringify(body?.tools ?? []).length} 字符）；非系统消息 ${nonSys.length} 条，其中注入 ${injected.length} 条`]
})
await check('C11', '人设里的 {{user}} 不转义会让请求失败；插入 U+2060 后正常', async () => {
  const raw = '你是测试人设。称呼 {{user}} 为朋友。'
  const rowsRaw = personaOnlyRows({ persona: 'x', route: fakeRoute }).map((r: any) => (r.id === 'system-prompt' ? { id: 'system-prompt', config: { ...r.config, personaPrefix: raw } } : r))
  const a = await firstRequest(rowsRaw)
  const b = await firstRequest(personaOnlyRows({ persona: raw, route: fakeRoute }))
  return [ok(a.err.length > 0 && !b.err), `不转义 → ${a.err.slice(0, 140) || '（没报错）'}；转义后 → ${b.err || '正常'}`]
})

// ── C12–C15：工具、取消、崩溃、上游错误 ──
{
  const f = scriptedFake([
    { match: '工具测试', tool: { name: 'mcp__tg__reply', arguments: { text: '在的呀' } } },
    { match: '工具测试', text: '好' },
    { match: '失败测试', tool: { name: 'mcp__tg__reply', arguments: { text: 'FAIL' } } },
    { match: '失败测试', text: '好' },
    { match: '取消测试', delayMs: 20000, text: '不该出现' },
    { match: '强杀测试', delayMs: 20000, text: '不该出现' },
    ...Array.from({ length: 8 }, () => ({ match: '五百', status: 500, body: '{"error":{"message":"boom"}}' })),
    ...Array.from({ length: 8 }, () => ({ match: '限流', status: 429, body: '{"error":{"message":"slow down"}}' })),
    ...Array.from({ length: 8 }, () => ({ match: '坏请求', status: 400, body: '{"error":{"message":"bad"}}' })),
  ])
  const route = { ...fakeRoute, baseURL: `http://127.0.0.1:${f.port}/v1` }
  const auth: string[] = []
  const tools: McpTool[] = [{
    name: 'reply', description: '发消息', inputSchema: { type: 'object', properties: { text: { type: 'string' } }, required: ['text'] },
    async call(a, { headers }) { auth.push(headers.get('authorization') ?? ''); return a.text === 'FAIL' ? { text: '第 1 段未送达：引用的消息不存在', isError: true } : { text: '已送达 1 段' } },
  }]
  const mcp = Bun.serve({ port: 0, hostname: '127.0.0.1', fetch: req => handleMcp(req, tools) })
  const mcpServers = [{ type: 'http', name: 'tg', url: `http://127.0.0.1:${mcp.port}/mcp/chat-1`, headers: [{ name: 'Authorization', value: 'Bearer per-session-check' }] }]
  const sb = sandbox(personaOnlyRows({ persona: '你是测试人设。', route, extra: [{ id: 'llm-pi-ai', config: { providers: { fake: { api: 'openai-completions', baseURL: route.baseURL, apiKeyEnv: 'FAKE_LLM_KEY', models: [{ id: 'fake-chat', contextWindow: 1000000 }], retryPolicy: { mode: 'normal', maxRetries: 2 } } } } }] }))
  let c = await bot(sb)
  const s = await c.request('session/new', { cwd: sb.work, mcpServers })
  const sid = s.sessionId
  await check('C12', 'HTTP MCP 工具按会话挂载：名字 mcp__<服务名>__<工具>；鉴权头送达；不触发权限请求；工具报错时模型看到错误文字', async () => {
    const n0 = c.updates.length
    await c.prompt(sid, '工具测试')
    await c.prompt(sid, '失败测试')
    const ups = c.updates.slice(n0).map(u => u.update)
    const calls = ups.filter(u => u.sessionUpdate === 'tool_call').map(u => u.title)
    const failed = ups.filter(u => u.sessionUpdate === 'tool_call_update' && u.status === 'failed').length
    const hist = JSON.stringify(f.requests.at(-1)?.body?.messages ?? [])
    const toolNames = (f.requests[0]?.body?.tools ?? []).map((t: any) => t.function?.name)
    return [ok(calls.includes('mcp__tg__reply') && auth.every(a => a === 'Bearer per-session-check') && c.permissionRequests === 0 && failed === 1 && hist.includes('未送达')),
      `请求里的工具=${toolNames.join(',')}；调用=${calls.join(',')}；鉴权头=${[...new Set(auth)].join(',')}；权限请求=${c.permissionRequests}；失败更新=${failed}`]
  })
  await check('C13', 'session/cancel：stopReason=cancelled；被取消的用户消息留在历史里且没有回复', async () => {
    const p = c.prompt(sid, '取消测试')
    await Bun.sleep(1500)
    c.notify('session/cancel', { sessionId: sid })
    const r = await p
    await c.prompt(sid, '下一条')
    const msgs = f.requests.at(-1)?.body?.messages ?? []
    const i = msgs.findIndex((m: any) => typeof m.content === 'string' && m.content === '取消测试')
    return [ok(r.stopReason === 'cancelled' && i >= 0 && msgs[i + 1]?.role === 'user'), `stopReason=${r.stopReason}；历史里“取消测试”之后紧跟 ${msgs[i + 1]?.role}`]
  })
  await check('C14', '生成中强杀 dsh 再续接：那条在飞消息留在历史里、没有回复（再原样重发 = 模型看到两遍）', async () => {
    const p = c.prompt(sid, '强杀测试').catch((e: any) => e.message)
    await Bun.sleep(1500)
    c.proc.kill('SIGKILL')
    const r = await p
    c = await bot(sb)
    await c.request('session/resume', { sessionId: sid, cwd: sb.work, mcpServers })
    await c.prompt(sid, '续接后的消息')
    const msgs = f.requests.at(-1)?.body?.messages ?? []
    const i = msgs.findIndex((m: any) => m.content === '强杀测试')
    return [ok(i >= 0 && msgs[i + 1]?.role === 'user'), `prompt → ${r}；历史里“强杀测试”之后紧跟 ${msgs[i + 1]?.role}`]
  })
  await check('C15', '上游 500/429 自动重试（本测试配了最多 2 次），400 不重试；失败以 JSON-RPC 错误返回；失败轮的用户消息留在历史里', async () => {
    const res: string[] = []
    for (const k of ['五百', '限流', '坏请求']) {
      const n0 = f.requests.length
      const err = await c.prompt(sid, `${k} 测试`).then(() => 'ok').catch((e: any) => `${e.code}`)
      res.push(`${k}: ${f.requests.length - n0} 次请求 → ${err}`)
    }
    await c.prompt(sid, '错误之后')
    const users = (f.requests.at(-1)?.body?.messages ?? []).filter((m: any) => m.role === 'user').map((m: any) => m.content)
    const kept = ['五百 测试', '限流 测试', '坏请求 测试'].every(t => users.includes(t))
    return [ok(res[0].startsWith('五百: 3') && res[1].startsWith('限流: 3') && res[2].startsWith('坏请求: 1') && kept), `${res.join('；')}；失败轮用户消息仍在历史：${kept}`]
  })
  await c.close()
  mcp.stop(true)
  f.stop()
}

// ── C16：发往 DeepSeek 官方路由的请求长什么样 ──
for (const variant of ['出厂', '关掉上传类行'] as const) {
  await check(`C16${variant === '出厂' ? 'a' : 'b'}`, `deepseek-official 路由请求（${variant}）：协议、插件清单字段、会话日志上传字段、旧思考是否回传`, async () => {
    const f = scriptedFake([{ reasoning: '（旧思考）', text: '在的' }, { text: '嗯嗯' }])
    const base: object[] = [
      { id: 'system-prompt', config: { includeHarnessIdentity: false, includeRuntimeContext: false, personaPrefix: '你是测试人设。' } },
      ...DISABLE_ROWS.filter(id => !['session-log-deepseek', 'plugin-package-inventory-deepseek'].includes(id)).map(id => ({ id, disabled: true })),
    ]
    const rowsX = variant === '出厂' ? base : [...base, { id: 'session-log-deepseek', disabled: true }, { id: 'plugin-package-inventory-deepseek', disabled: true }]
    const sb = sandbox(rowsX)
    const c = new AcpClient(dshCmd(sb), { env: dshEnv(sb, { DEEPSEEK_API_KEY: 'sk-dummy-not-real', DEEPSEEK_BASE_URL: `http://127.0.0.1:${f.port}` }), cwd: sb.work })
    await c.initialize()
    const s = await c.request('session/new', { cwd: sb.work, mcpServers: [] })
    let err = ''
    for (const t of ['你好', '今天怎么样']) await c.prompt(s.sessionId, t).catch((e: any) => { err = e.message })
    await c.close()
    f.stop()
    const r1 = f.requests[0], r2 = f.requests[1]
    const keys = Object.keys(r1?.body ?? {})
    const hist = JSON.stringify(r2?.body?.messages ?? [])
    const reasoningBack = hist.includes('旧思考')
    return ['仅记录', `路径=${r1?.path}；字段=${keys.join(',')}；插件清单=${keys.includes('dsh_plugin_packages') ? `有(${JSON.stringify(r1.body.dsh_plugin_packages).length}字符)` : '无'}；会话日志上传=${keys.includes('dsh_session_log') ? '有' : '无'}；第二轮历史带回旧思考=${reasoningBack}${err ? '；错误=' + err.slice(0, 80) : ''}`]
  })
}

// ── C17–C19：配置与凭据 ──
async function runDsh(args: string[], sb: Sandbox, extraEnv: Record<string, string> = {}) {
  const p = Bun.spawn([dshBin(), ...args], { env: dshEnv(sb, extraEnv), cwd: sb.work, stdout: 'pipe', stderr: 'pipe', stdin: 'ignore' })
  const [o, e, code] = await Promise.all([new Response(p.stdout).text(), new Response(p.stderr).text(), p.exited])
  return { code, out: o, err: e }
}
await check('C17', '补丁文件：空文件或只有注释 → 启动失败；写 [] 才算空；指向不存在的行只警告', async () => {
  const sb = sandbox([])
  writeFileSync(join(sb.root, 'empty.yml'), '')
  writeFileSync(join(sb.root, 'comment.yml'), '# nothing\n')
  writeFileSync(join(sb.root, 'list.yml'), '[]\n')
  writeFileSync(join(sb.root, 'missing.yml'), JSON.stringify([{ id: 'no-such-row', disabled: true }]))
  const r = await Promise.all(['empty', 'comment', 'list', 'missing'].map(n => runDsh(['--profile', 'acp', '--patch', join(sb.root, `${n}.yml`), '--dump-config'], sb)))
  return [ok(r[0].code !== 0 && r[1].code !== 0 && r[2].code === 0 && r[3].code === 0 && r[3].err.includes('not found')), `空=${r[0].code} 注释=${r[1].code} []=${r[2].code} 不存在的行=${r[3].code}（${r[3].err.trim().slice(0, 80)}）`]
})
await check('C18', '凭据文件 $DSH_HOME/.credentials.yaml：权限 644 拒绝启动，600 正常并能用其中的密钥', async () => {
  const f = startFakeLlm({ port: 0 })
  const rowsH = [
    { id: 'llm-pi-ai', config: { providers: { fake: { api: 'openai-completions', baseURL: `http://127.0.0.1:${f.port}/v1`, apiKeyEnv: 'FAKE_LLM_KEY', models: [{ id: 'fake-chat', contextWindow: 200000 }] } } } },
    { id: 'agent-default-model', config: { provider: 'fake', model: 'fake-chat' } },
    { id: 'system-prompt', config: { includeHarnessIdentity: false, includeRuntimeContext: false, personaPrefix: '你是后台打分器。' } },
    ...DISABLE_ROWS.map(id => ({ id, disabled: true })),
  ]
  const sb = sandbox(rowsH)
  const cred = join(sb.dshHome, '.credentials.yaml')
  writeFileSync(cred, 'version: 1\nrefs:\n  FAKE_LLM_KEY: from-credentials-file\n')
  chmodSync(cred, 0o644)
  const a = await runDsh(['--profile', 'headless', '--patch', sb.patch, '打个分'], sb)
  chmodSync(cred, 0o600)
  const b = await runDsh(['--profile', 'headless', '--patch', sb.patch, '打个分'], sb)
  const authSeen = f.requests[0]?.headers?.authorization ?? ''
  f.stop()
  return [ok(a.code !== 0 && a.err.includes('chmod 600') && b.code === 0 && authSeen.startsWith('Bearer fro')), `644 → 退出码 ${a.code}；600 → 退出码 ${b.code}，上游看到的鉴权头前缀=${authSeen}`]
})
await check('C19', 'headless + 只留人设：一次一个任务，stdout 只有最终文本，请求里没有工具（适合后台小任务）', async () => {
  const f = startFakeLlm({ port: 0, script: [{ text: '{"score": 3}' }] })
  const rowsH = [
    { id: 'llm-pi-ai', config: { providers: { fake: { api: 'openai-completions', baseURL: `http://127.0.0.1:${f.port}/v1`, apiKeyEnv: 'FAKE_LLM_KEY', models: [{ id: 'fake-chat', contextWindow: 200000 }] } } } },
    { id: 'agent-default-model', config: { provider: 'fake', model: 'fake-chat' } },
    { id: 'system-prompt', config: { includeHarnessIdentity: false, includeRuntimeContext: false, personaPrefix: '你是后台打分器。' } },
    ...DISABLE_ROWS.map(id => ({ id, disabled: true })),
  ]
  const sb = sandbox(rowsH)
  const t0 = Date.now()
  const r = await runDsh(['--profile', 'headless', '--patch', sb.patch, '给这句话打分：今天好开心'], sb, { FAKE_LLM_KEY: 'dummy' })
  const tools = (f.requests[0]?.body?.tools ?? []).length
  f.stop()
  return [ok(r.code === 0 && r.out.trim() === '{"score": 3}' && tools === 0), `退出码 ${r.code}，耗时 ${Date.now() - t0}ms，stdout=${r.out.trim()}，工具 ${tools} 个`]
})

// ── C20：MCP 资源工具（0.2.0 新增）──
await check('C20', '挂 MCP 时 dsh 自己加的东西（0.2.0：3 个资源工具 + 系统提示词末尾一段）；禁用 mcp-resources 行后消失', async () => {
  const res: string[] = []
  for (const disable of [false, true]) {
    const f = startFakeLlm({ port: 0 })
    const tools: McpTool[] = [{ name: 'reply', description: '发消息', inputSchema: { type: 'object', properties: {} }, async call() { return { text: 'ok' } } }]
    const mcp = Bun.serve({ port: 0, hostname: '127.0.0.1', fetch: req => handleMcp(req, tools) })
    const route = { ...fakeRoute, baseURL: `http://127.0.0.1:${f.port}/v1` }
    const base = personaOnlyRows({ persona: '你是测试人设。', route }).filter((r: any) => !(r.id === 'mcp-resources' && r.disabled))
    const sb = sandbox(disable ? [...base, { id: 'mcp-resources', disabled: true }] : base)
    const c = await bot(sb)
    const s = await c.request('session/new', { cwd: sb.work, mcpServers: [{ type: 'http', name: 'tg', url: `http://127.0.0.1:${mcp.port}/mcp`, headers: [] }] })
    await c.prompt(s.sessionId, '在吗')
    await c.close()
    const body = f.requests[0]?.body
    res.push(`${disable ? '禁用后' : '默认'}：工具=${(body?.tools ?? []).map((t: any) => t.function?.name).join(',')}；系统提示词=${JSON.stringify(sysText(body)).slice(0, 80)}`)
    mcp.stop(true); f.stop()
  }
  return ['仅记录', res.join(' ｜ ')]
})

// ── C21：出厂压缩 + 中文 ──
await check('C21', '出厂压缩与中文：单条消息按“4 字符 1 token”估，保留量设大会一直不压缩、设小会频繁压缩', async () => {
  const res: string[] = []
  for (const retain of [2000, 500]) {
    const f = startFakeLlm({ port: 0, script: Array.from({ length: 40 }, () => ({ match: 'compaction engine', text: '## Primary Request and Intent\n- 短摘要' })) })
    const route = { ...fakeRoute, baseURL: `http://127.0.0.1:${f.port}/v1` }
    const sb = sandbox(personaOnlyRows({ persona: samplePersona(), route, compaction: { thresholdRatio: 0.0047, retainTokens: retain } }))
    const c = await bot(sb)
    const s = await c.request('session/new', { cwd: sb.work, mcpServers: [] })
    const used: number[] = []
    c.onUpdate = p => { if (p.update.sessionUpdate === 'usage_update') used.push(p.update.used) }
    for (let i = 0; i < 20; i++) await c.prompt(s.sessionId, `第${i}条：` + '早上挤地铁差点迟到，中午和同事吃了楼下的牛肉面，晚上回家炒了个番茄炒蛋。'.repeat(3))
    await c.close()
    const comp = f.requests.filter(r => JSON.stringify(r.body.messages ?? []).includes('compaction engine')).length
    res.push(`保留 ${retain}：阈值 4700，dsh 估值 ${used[0]}→${used.at(-1)}，压缩请求 ${comp} 次`)
    f.stop()
  }
  return ['仅记录', res.join(' ｜ ')]
})

fake.stop()
report.h(2, '结果')
report.table(['编号', '核对的事实', '结论', '实际观察'], rows)
report.line(`dsh 可执行文件：${dshBin()}`)
report.line(`\n报告文件：${report.path}`)
