// /provider add：主人在私聊里新建供应商。消息带密钥：在写账本之前拦下、删掉原消息；密钥只进凭据文件；
// 拉到的模型列表登记在 providers.json（几个 bot 共用），dsh 的路由里多出这个供应商，/provider <名字> 能切过去
import { afterEach, expect, test } from 'bun:test'
import { chmodSync, existsSync, readdirSync, readFileSync, statSync, writeFileSync } from 'fs'
import { join } from 'path'
import { FakeTelegram } from '../fakes/fake-telegram'
import { cleanup, Gateway, makeBot, OWNER, FRIEND, until, writeConfig, type BotEnv } from '../harness'

const KEY = 'sk-TESTONLY-provider-secret-0123456789'
const OPENAI_KEY = 'sk-TESTONLY-openai-secret-9876543210'

let tgs: FakeTelegram[] = []
let bots: BotEnv[] = []
let gws: Gateway[] = []
let server: ReturnType<typeof Bun.serve> | null = null
let failOpenai = 0

afterEach(async () => {
  for (const g of gws) await g.stop()
  for (const t of tgs) t.stop()
  for (const b of bots) cleanup(b)
  server?.stop(true)
  tgs = []; bots = []; gws = []; server = null; failOpenai = 0
})

/** 假供应商：Anthropic 风格（x-api-key，/v1/models）和 OpenAI 风格（Bearer，/openai/v1/models） */
function fakeProvider(): string {
  server = Bun.serve({
    hostname: '127.0.0.1', port: 0,
    fetch(req) {
      const u = new URL(req.url)
      if (u.pathname === '/v1/models') {
        if (req.headers.get('x-api-key') !== KEY) return Response.json({ error: 'bad key' }, { status: 401 })
        return Response.json({ data: [{ id: 'claude-x', display_name: 'Claude X', max_input_tokens: 200000 }, { id: 'claude-y' }] })
      }
      if (u.pathname === '/openai/v1/models') {
        if (req.headers.get('authorization') !== `Bearer ${OPENAI_KEY}`) return Response.json({ error: { message: `Incorrect API key provided: ${req.headers.get('authorization')}` } }, { status: 401 })
        if (failOpenai-- > 0) return new Response('busy', { status: 503 })
        return Response.json({ object: 'list', data: [{ id: 'gpt-a', context_length: 64000 }] })
      }
      return new Response('no', { status: 404 })
    },
  })
  return `http://127.0.0.1:${server.port}`
}

async function startBot(o: { root?: string; name?: string } = {}) {
  const tg = new FakeTelegram({ botId: 900001 + tgs.length, username: `bot${tgs.length}_bot` })
  tgs.push(tg)
  const b = makeBot(tg, { root: o.root, name: o.name, gw: { burst_window_ms: 0 } })
  bots.push(b)
  const cred = join(b.root, 'credentials.yaml')
  if (!existsSync(cred)) {
    writeFileSync(cred, '# 注释留着\nversion: 1\nrefs:\n  DEEPSEEK_API_KEY: TESTONLY-not-a-real-key\n')
    chmodSync(cred, 0o600)
  }
  writeConfig(b)
  const gw = new Gateway(b)
  await gw.start()
  gws.push(gw)
  return { tg, b, gw }
}

const lastText = (tg: FakeTelegram, chat: number) => tg.sentTo(chat).map(s => s.text ?? '').join('\n---\n')

/** 这个目录下（递归）所有文件里都找不到 needle */
function grepTree(dir: string, needle: string): string[] {
  const hits: string[] = []
  const walk = (d: string) => {
    for (const f of readdirSync(d)) {
      const p = join(d, f)
      const st = statSync(p)
      if (st.isDirectory()) walk(p)
      else if (readFileSync(p).includes(needle)) hits.push(p)
    }
  }
  walk(dir)
  return hits
}

test('/provider add：删掉原消息、密钥只进凭据文件、拉到模型、能切过去；账本和所有日志里都找不到密钥', async () => {
  const url = fakeProvider()
  const { tg, b, gw } = await startBot()
  const mid = tg.pushText(OWNER, `/provider add MyProxy ${url} ${KEY}`)
  await until(() => lastText(tg, OWNER).includes('已添加供应商'), 'add reply')
  const reply = lastText(tg, OWNER)
  expect(reply).toContain('已添加供应商「MyProxy」，拉到 2 个模型。切过去：/provider MyProxy')
  expect(reply).toContain('已经删掉了')
  expect(reply).not.toContain(KEY)
  expect(tg.deleted).toEqual([{ chatId: String(OWNER), messageId: mid }])
  // 原消息没被标"已读"（👀 只给进了账本的消息）
  expect(tg.reactions.some(r => r.messageId === mid)).toBe(false)

  // 凭据文件：多了一个键，原来的内容还在，权限 600
  const cred = join(b.root, 'credentials.yaml')
  const credText = readFileSync(cred, 'utf8')
  expect(credText).toContain(`PROVIDER_MYPROXY_API_KEY: "${KEY}"`)
  expect(credText).toContain('DEEPSEEK_API_KEY: TESTONLY-not-a-real-key')
  expect(credText).toContain('# 注释留着')
  if (process.platform !== 'win32') expect(statSync(cred).mode & 0o777).toBe(0o600)
  // 登记文件：没有密钥
  const reg = JSON.parse(readFileSync(join(b.root, 'providers.json'), 'utf8'))
  expect(reg.providers.myproxy).toMatchObject({ displayName: 'MyProxy', api: 'anthropic-messages', baseURL: url, apiKeyEnv: 'PROVIDER_MYPROXY_API_KEY', fetchError: null })
  expect(reg.providers.myproxy.models).toEqual([{ id: 'claude-x', name: 'Claude X', contextWindow: 200000 }, { id: 'claude-y' }])

  // 切过去
  tg.pushText(OWNER, '/provider MyProxy')
  await until(() => lastText(tg, OWNER).includes('已换成 myproxy'), 'switch reply')
  expect(lastText(tg, OWNER)).toContain('已换成 myproxy / claude-x')
  // dsh 的补丁层里有这条路由，按键名取密钥；窗口没写的按 128000 算
  const patch = readFileSync(join(b.botDir, 'dsh-home', 'bot.patch.yml'), 'utf8')
  const route = JSON.parse(patch).find((r: any) => r.id === 'llm-pi-ai').config.providers.myproxy
  expect(route).toEqual({ displayName: 'MyProxy', api: 'anthropic-messages', baseURL: url, apiKeyEnv: 'PROVIDER_MYPROXY_API_KEY', models: [{ id: 'claude-x', name: 'Claude X', contextWindow: 200000 }, { id: 'claude-y', contextWindow: 128000 }] })

  // 再聊一句，让账本、聊天日志都写点东西；然后到处找密钥
  tg.pushText(OWNER, '换完了吗')
  await until(() => { const l = gw.ledger(); const r = l.db.query("SELECT COUNT(*) AS n FROM turns WHERE state = 'ok'").get() as any; l.close(); return r.n >= 1 }, 'a turn after switch')
  await gw.stop(); gws = []
  expect(grepTree(b.botDir, KEY)).toEqual([]) // 账本（含 WAL）、gateway.log、chat.log、dsh 的会话和补丁层
  expect(grepTree(b.acpState, KEY)).toEqual([]) // 模型收到的所有内容
  expect(readFileSync(join(b.root, 'providers.json'), 'utf8')).not.toContain(KEY)
  expect(tg.sent.some(s => (s.text ?? '').includes(KEY))).toBe(false)
  expect(existsSync(join(b.botDir, 'logs', 'chat.log')) ? readFileSync(join(b.botDir, 'logs', 'chat.log'), 'utf8') : '').not.toContain('/provider add')
})

test('/provider add … openai：模型列表没拉到也照样建好，说明原因；/provider refresh 和管理台接口能再拉', async () => {
  const url = fakeProvider()
  failOpenai = 1
  const { tg, b, gw } = await startBot()
  tg.pushText(OWNER, `/provider add MyOpen ${url}/openai/v1 ${OPENAI_KEY} openai`)
  await until(() => lastText(tg, OWNER).includes('已添加供应商'), 'add reply')
  expect(lastText(tg, OWNER)).toContain('已添加供应商「MyOpen」，但模型列表没拉到：对方接口出错（HTTP 503）')
  let reg = JSON.parse(readFileSync(join(b.root, 'providers.json'), 'utf8'))
  expect(reg.providers.myopen).toMatchObject({ api: 'openai-completions', models: [] })
  // 没有模型的不进 dsh 路由（dsh 要求至少一个模型）
  tg.pushText(OWNER, '/provider')
  await until(() => lastText(tg, OWNER).includes('模型列表没拉到（对方接口出错'), 'list shows pending')
  // 管理台：列表（不含密钥）和刷新
  const auth = { authorization: `Bearer ${gw.apiToken()}`, 'content-type': 'application/json' }
  const list = await (await gw.api('/v1/providers', { headers: auth })).json() as any
  expect(list.providers).toEqual([expect.objectContaining({ name: 'myopen', displayName: 'MyOpen', models: 0 })])
  expect(JSON.stringify(list)).not.toContain(OPENAI_KEY)
  const r = await (await gw.api('/v1/providers/refresh', { method: 'POST', headers: auth, body: JSON.stringify({ name: 'MyOpen' }) })).json() as any
  expect(r).toMatchObject({ ok: true, models: 1 })
  reg = JSON.parse(readFileSync(join(b.root, 'providers.json'), 'utf8'))
  expect(reg.providers.myopen.models).toEqual([{ id: 'gpt-a', contextWindow: 64000 }])
  tg.pushText(OWNER, '/provider MyOpen')
  await until(() => lastText(tg, OWNER).includes('已换成 myopen / gpt-a'), 'switch to openai provider')
  // 删掉：密钥一起删，换回配置文件里的模型
  tg.pushText(OWNER, '/provider remove MyOpen')
  await until(() => lastText(tg, OWNER).includes('已删除供应商「MyOpen」'), 'remove')
  expect(readFileSync(join(b.root, 'credentials.yaml'), 'utf8')).not.toContain('PROVIDER_MYOPEN_API_KEY')
  tg.pushText(OWNER, '/model')
  await until(() => lastText(tg, OWNER).includes('现在用的模型：deepseek-official / deepseek-flash'), 'back to config model')
})

test('/provider add：密钥不对时说明原因；非主人发的不做事、照样删；删不掉时提醒手动删；格式不对给用法', async () => {
  const url = fakeProvider()
  const { tg, b } = await startBot()
  tg.pushText(OWNER, `/provider add Wrong ${url} sk-TESTONLY-wrong-key-000000`)
  await until(() => lastText(tg, OWNER).includes('「Wrong」'), 'wrong key reply')
  expect(lastText(tg, OWNER)).toContain('对方说密钥不对或没有权限（HTTP 401）')
  // 非主人（在白名单里，但不是主人）
  tg.pushText(FRIEND, `/provider add Evil ${url} ${KEY}`)
  await until(() => lastText(tg, FRIEND).includes('只有主人'), 'not owner reply')
  expect(tg.deleted.length).toBe(2)
  expect(JSON.parse(readFileSync(join(b.root, 'providers.json'), 'utf8')).providers.evil).toBeUndefined()
  expect(readFileSync(join(b.root, 'credentials.yaml'), 'utf8')).not.toContain(KEY)
  // 删不掉
  tg.faults.push({ method: 'deleteMessage', status: 400, description: "Bad Request: message can't be deleted" })
  tg.pushText(OWNER, `/provider add bad_name! ${url} ${KEY}`)
  await until(() => lastText(tg, OWNER).includes('没能删掉'), 'delete failed reply')
  const t = lastText(tg, OWNER)
  expect(t).toContain('名字只能用英文字母、数字和连字符')
  expect(t).toContain('用法：/provider add <名字> <接口地址> <密钥> [openai]')
  expect(t).not.toContain(KEY)
  tg.pushText(OWNER, '/provider add 少了参数')
  await until(() => (lastText(tg, OWNER).match(/用法：/g) ?? []).length >= 2, 'usage reply')
  await gws[0]!.stop(); gws = []
  expect(grepTree(b.botDir, KEY)).toEqual([])
  expect(grepTree(b.botDir, 'sk-TESTONLY-wrong-key')).toEqual([])
})

test('几个 bot 共用：在 bot A 上加的供应商，bot B 不用重启也能切过去', async () => {
  const url = fakeProvider()
  const a = await startBot({ name: 'bota' })
  const bB = await startBot({ root: a.b.root, name: 'botb' })
  // 先让 B 的 dsh 起来（它要在空闲时重启才看得到新路由）
  bB.tg.pushText(OWNER, '/model list')
  await until(() => lastText(bB.tg, OWNER).includes('能换的模型'), 'b list before')
  expect(lastText(bB.tg, OWNER)).not.toContain('myproxy')
  a.tg.pushText(OWNER, `/provider add MyProxy ${url} ${KEY}`)
  await until(() => lastText(a.tg, OWNER).includes('已添加供应商'), 'add on a')
  await until(async () => {
    bB.tg.pushText(OWNER, '/provider MyProxy')
    await new Promise(r => setTimeout(r, 400))
    return lastText(bB.tg, OWNER).includes('已换成 myproxy / claude-x')
  }, 'b sees the new provider', 15_000, 500)
})
