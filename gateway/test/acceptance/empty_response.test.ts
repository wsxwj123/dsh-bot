// 验收：模型回空内容（INTERFACE-dsh修补 第一节：1.1 交给 dsh 的配置、1.2 一轮的结局、1.4 错误契约、1.7 版本提醒）。
// 黑盒：只看账本（turns / inbound 的 state、stop_reason）、日志事件、假 dsh 收到的 prompt、假 Telegram 收到的消息、补丁文件。
// 假 dsh 的指令见 test/fakes/fake-acp.ts 顶部（!emptyafter、!silentempty、!empty:N:tag、!emptyrace、!failafter）。
// 对方一律用 FRIEND 发消息，这样主人（OWNER）聊天里出现的任何消息都只能是系统通知。
import { describe, expect, setDefaultTimeout, test } from 'bun:test'
import { mkdirSync, readFileSync, rmSync, writeFileSync } from 'fs'
import { join } from 'path'
import { DS_KEY, FRIEND, OWNER, Gateway, chatPrompts, eventsOf, evs, inboundRow, logPath, makeBot, patchRoutes, patchSeen, prompts, q, seedCreds, seedProviders, sleep, until, waitSentText, warmUp, withBot, writeConfig, type BotEnv } from './_acc'
import { FakeTelegram } from '../fakes/fake-telegram'

// 不依赖命令行的 --timeout：这里的等待最长 20 秒
setDefaultTimeout(60_000)

/** 网关补给每条路由的重试策略（1.1：恰好这两个键，数组顺序固定） */
const POLICY = { mode: 'normal', retryableCodes: ['RATE_LIMIT', 'SERVER', 'TIMEOUT', 'TRANSPORT'] }
const NUDGE = '上一条消息没能处理完'
const ODD_MODEL = 'x-thinking-401-quota'

const turns = (b: BotEnv) => q<Record<string, any>>(b, 'SELECT * FROM turns')
const friendGot = (tg: FakeTelegram) => tg.sentTo(FRIEND).map(s => s.text)
const ownerGot = (tg: FakeTelegram) => tg.sentTo(OWNER).map(s => s.text)

/** 对方说一句，等日志里出现第 n 条 turn.end（这一轮有了结局） */
async function friendSays(tg: FakeTelegram, b: BotEnv, text: string, n = 1): Promise<number> {
  const mid = tg.pushText(FRIEND, text)
  await until(() => evs(b, 'turn.end').length >= n, `第 ${n} 条 turn.end`, 20_000)
  return mid
}
/** 等这条消息在账本里落定（done 或 dead） */
const settled = (b: BotEnv, mid: number) => until(() => ['done', 'dead'].includes(inboundRow(b, FRIEND, mid)?.state) ? inboundRow(b, FRIEND, mid) : null, '消息在账本里落定', 20_000)
/** 补救若会发生，在 retry_backoff_ms（测试里是 50、100 毫秒）之后很快就会出现；等够这么久再断言"没发生" */
const QUIET_MS = 1_500

describe('回复已经发出之后报空内容：正常结束', () => {
  test('这一轮在账本里记成 ok / empty_response，这条消息 done；不再给 dsh 发 prompt，对方只收到那一条回复，主人没有任何消息', async () => {
    await withBot({}, async ({ tg, b }) => {
      const mid = await friendSays(tg, b, '在吗 !emptyafter')
      const row = await settled(b, mid)
      expect(row.state).toBe('done')
      const ts = turns(b)
      expect(ts.map(t => [t.state, t.stop_reason]), `turns 实得 ${JSON.stringify(ts)}`).toEqual([['ok', 'empty_response']])
      await sleep(QUIET_MS)
      expect(prompts(b).map(p => p.text.includes(NUDGE))).toEqual([false])
      expect(friendGot(tg)).toEqual(['收到：在吗'])
      expect(ownerGot(tg)).toEqual([])
    })
  })

  test('调过 stay_silent 之后报空内容：同样正常结束（ok / empty_response），不补救、谁都不通知', async () => {
    await withBot({}, async ({ tg, b }) => {
      const mid = await friendSays(tg, b, '别回我 !silentempty')
      expect((await settled(b, mid)).state).toBe('done')
      await sleep(QUIET_MS)
      expect(turns(b).map(t => [t.state, t.stop_reason])).toEqual([['ok', 'empty_response']])
      expect(prompts(b).length).toBe(1)
      expect(friendGot(tg)).toEqual([])
      expect(ownerGot(tg)).toEqual([])
    })
  })

  test('日志：turn.end 的 outcome 为 ok，另有一条 turn.empty_response（info，带 turn、delivered、silent），日志里没有回复的文字', async () => {
    await withBot({}, async ({ tg, b }) => {
      await friendSays(tg, b, '在吗 !emptyafter !say:这是模型输出的原话甲乙丙')
      expect(evs(b, 'turn.end').map(e => e.outcome)).toEqual(['ok'])
      const e = await until(() => evs(b, 'turn.empty_response')[0], '日志 turn.empty_response')
      expect(e.level).toBe('info')
      for (const k of ['turn', 'delivered', 'silent']) expect(Object.keys(e), `缺字段 ${k}：${JSON.stringify(e)}`).toContain(k)
      expect(!!e.delivered, '发过消息，delivered 应为真（或大于 0）').toBe(true)
      expect(!!e.silent, '没调过 stay_silent，silent 应为假（或 0）').toBe(false)
      expect(readFileSync(logPath(b), 'utf8')).not.toContain('这是模型输出的原话甲乙丙')
    })
  })

  test('内置 deepseek 的那一句（DeepSeek Messages returned no content）同样算空内容报错', async () => {
    await withBot({}, async ({ tg, b }) => {
      const mid = await friendSays(tg, b, '在吗 !failafter:DeepSeek_Messages_returned_no_content')
      expect((await settled(b, mid)).state).toBe('done')
      expect(turns(b).map(t => [t.state, t.stop_reason])).toEqual([['ok', 'empty_response']])
    })
  })

  test('前面多一截的不算（turn failed: 502: upstream returned no content）：不记 empty_response，记一条 turn.error_after_action（warn），仍然直接结案不补救', async () => {
    await withBot({}, async ({ tg, b }) => {
      await friendSays(tg, b, '在吗 !failafter:502:_upstream_returned_no_content')
      const e = await until(() => evs(b, 'turn.error_after_action')[0], '日志 turn.error_after_action')
      expect(e.level).toBe('warn')
      for (const k of ['turn', 'delivered', 'silent']) expect(Object.keys(e)).toContain(k)
      await sleep(QUIET_MS)
      expect(evs(b, 'turn.empty_response')).toEqual([])
      expect(turns(b).filter(t => t.stop_reason === 'empty_response')).toEqual([])
      expect(prompts(b).length, '已有动作之后的报错不补救').toBe(1)
      expect(friendGot(tg)).toEqual(['收到：在吗'])
    })
  })
})

describe('什么都没发就报空内容：按失败处理', () => {
  test('补救一次后成功：对方收到补救轮的回复，主人没有消息，这条消息 done，第一轮记成 error', async () => {
    await withBot({}, async ({ tg, b }) => {
      const mid = tg.pushText(FRIEND, '在吗 !empty:1:a')
      await waitSentText(tg, FRIEND, '接着刚才的说', 0, 20_000)
      expect((await settled(b, mid)).state).toBe('done')
      const ps = prompts(b)
      expect(ps.length).toBe(2)
      expect(ps[1]!.text).toContain(NUDGE)
      expect(turns(b)[0]!.state).toBe('error')
      expect(friendGot(tg)).toEqual(['接着刚才的说'])
      expect(ownerGot(tg)).toEqual([])
    })
  })

  test('补救用尽：这条消息 dead，主人收到「…已放弃（模型回了空内容）。」，总共只发过 1 + 2 次 prompt', async () => {
    await withBot({}, async ({ tg, b }) => {
      const mid = tg.pushText(FRIEND, '在吗 !empty:99:b')
      const note = await waitSentText(tg, OWNER, '已放弃', 0, 20_000)
      expect(note.text).toBe('【系统】有 1 条消息重试 2 次后仍然失败，已放弃（模型回了空内容）。')
      expect((await settled(b, mid)).state).toBe('dead')
      await sleep(QUIET_MS)
      expect(prompts(b).length).toBe(3)
      expect(prompts(b).filter(p => p.text.includes('在吗 !empty:99:b')).length, '原文只交给模型一次').toBe(1)
      expect(friendGot(tg)).toEqual([])
      expect(ownerGot(tg).length, '主人只收到这一条').toBe(1)
    })
  })

  test('回复还在发、这一轮就报了空内容，而这次发送最后确定没发出去：等它落定后按"没有动作"补救，对方最终收到补救轮的回复', async () => {
    await withBot({ gw: { max_send_wait_ms: 5_000 } }, async ({ tg, b }) => {
      const toFriend = (p: Record<string, any>) => String(p.chat_id) === String(FRIEND)
      // 第一次发送被限流 1 秒（网关会等着重发，此时算"还在发"），重发时被对方拒绝（确定没发出去）
      tg.faults.push({ method: 'sendMessage', when: toFriend, status: 429, description: 'Too Many Requests: retry after 1', retryAfter: 1, times: 1 })
      tg.faults.push({ method: 'sendMessage', when: toFriend, status: 400, description: 'Bad Request: chat write forbidden', times: 1 })
      const mid = tg.pushText(FRIEND, '还在发 !emptyrace:150')
      await waitSentText(tg, FRIEND, '接着刚才的说', 0, 20_000)
      expect((await settled(b, mid)).state).toBe('done')
      expect(tg.calls.filter(c => c.method === 'sendMessage' && toFriend(c.params) && String(c.params.text).includes('还在发')).length, '前提：那条回复确实试过两次（限流一次、被拒一次）').toBe(2)
      expect(friendGot(tg)).toEqual(['接着刚才的说'])
      expect(prompts(b).map(p => p.text.includes(NUDGE))).toEqual([false, true])
    })
  })
})

describe('报错归类不看引号里的模型名', () => {
  const odd = (b: BotEnv) => {
    seedProviders(b.root, [{ name: 'myproxy', baseURL: 'https://p.example.com/v1', models: [{ id: ODD_MODEL, contextWindow: 64000 }] }])
    seedCreds(b.root, { PROVIDER_MYPROXY_KEY: 'test-key-7emptyodd0000000001' })
  }
  const switchTo = async (tg: FakeTelegram) => { tg.pushText(OWNER, `/model myproxy/${ODD_MODEL}`); await waitSentText(tg, OWNER, `已换成 myproxy / ${ODD_MODEL}`) }
  const rolledFor = (b: BotEnv) => eventsOf(b, 'segment_rolled').map(e => e.data?.reason).filter(r => ['context-overflow', 'thinking-history'].includes(r))

  test(`别的报错里带着 model "${ODD_MODEL}"（引号外是上游 503）：按普通失败补救，不当成"历史思考被拒"，也不当成"上游拒绝"`, async () => {
    await withBot({ before: odd }, async ({ tg, b }) => {
      await switchTo(tg)
      const k = tg.sentTo(OWNER).length
      mkdirSync(b.acpState, { recursive: true })
      writeFileSync(join(b.acpState, 'next-error.txt'), `model "${ODD_MODEL}" is overloaded, upstream returned 503 Service Unavailable`)
      tg.pushText(FRIEND, '在吗')
      await waitSentText(tg, FRIEND, '接着刚才的说', 0, 20_000)
      expect(rolledFor(b)).toEqual([])
      expect(prompts(b).map(p => p.text.includes(NUDGE))).toEqual([false, true])
      expect(friendGot(tg)).toEqual(['接着刚才的说'])
      expect(tg.sentTo(OWNER).slice(k)).toEqual([])
    })
  })
})

describe('交给 dsh 的配置（补丁文件）', () => {
  const routes = {
    plain: { api: 'openai-completions', baseURL: 'https://r1.example.com/v1', apiKeyEnv: 'PLAIN_KEY', models: [{ id: 'r1', contextWindow: 65536 }] },
    resp: { api: 'openai-responses', baseURL: 'https://r2.example.com/v1', apiKeyEnv: 'RESP_KEY', models: [{ id: 'r2', contextWindow: 65536 }] },
    anth: { api: 'anthropic-messages', baseURL: 'https://r3.example.com', apiKeyEnv: 'ANTH_KEY', models: [{ id: 'r3', contextWindow: 65536 }] },
    own: { api: 'openai-completions', baseURL: 'https://r4.example.com/v1', apiKeyEnv: 'OWN_KEY', retryPolicy: { mode: 'normal', maxRetries: 0 }, models: [{ id: 'r4', contextWindow: 65536 }] },
  }

  test('配置文件里没写 retryPolicy 的路由（三种接口格式）每条都补上固定的那一个值；自己写了的原样交给 dsh，不改、不合并', async () => {
    await withBot({ brain: { routes } }, async ({ tg, b }) => {
      await warmUp(tg)
      const got = patchRoutes(b)
      expect([got.plain?.retryPolicy, got.resp?.retryPolicy, got.anth?.retryPolicy]).toEqual([POLICY, POLICY, POLICY])
      expect(got.own?.retryPolicy).toEqual({ mode: 'normal', maxRetries: 0 })
    })
  })

  test('补丁里恒有一行 llm-deepseek，config 只有 retryPolicy 一个键；当前用的是配置文件路由时也有，且只有一行', async () => {
    await withBot({ brain: { provider: 'plain', model: 'r1', routes }, before: b => { seedCreds(b.root, { PLAIN_KEY: 'test-key-7plainroute00000001' }) } }, async ({ tg, b }) => {
      await warmUp(tg)
      const rows = patchSeen(b).filter((r: any) => r?.id === 'llm-deepseek')
      expect(rows.length).toBe(1)
      expect(rows[0].config).toEqual({ retryPolicy: POLICY })
    })
  })
})

describe('dsh 版本没验证过时的提醒（1.7）', () => {
  /** 造一个"装好的 dsh"目录，只有版本号是真的；这条用例不触发任何聊天轮，dsh 不会被拉起 */
  async function withInstalled(version: string, fn: (c: { tg: FakeTelegram; b: BotEnv }) => Promise<void>) {
    const tg = new FakeTelegram()
    const b = makeBot(tg)
    delete (b.gw as Record<string, unknown>).dsh_command
    writeConfig(b)
    seedCreds(b.root, DS_KEY)   // 不用自定义启动命令时，网关启动前要有凭据文件
    const pkg = join(b.root, 'installed', 'node_modules', '@deepseek-ai', 'dsh')
    mkdirSync(join(pkg, 'lib'), { recursive: true })
    writeFileSync(join(pkg, 'package.json'), JSON.stringify({ name: '@deepseek-ai/dsh', version, bin: { dsh: 'lib/bin.js' } }))
    writeFileSync(join(pkg, 'lib', 'bin.js'), `if (process.argv.includes('--version') || process.argv.includes('-v')) console.log(${JSON.stringify(version)})\nprocess.exit(0)\n`)
    const gw = new Gateway(b)
    try {
      await gw.start({ DSH_BOT_HARNESS: join(b.root, 'installed') })
      await fn({ tg, b })
    } finally {
      await gw.stop().catch(() => {})
      tg.stop()
      try { rmSync(b.root, { recursive: true, force: true }) } catch {}
    }
  }

  test('装的是 9.9.9：日志记一条 dsh.version_unverified（warn，带 version），主人收到一条固定文案的提醒', async () => {
    await withInstalled('9.9.9', async ({ tg, b }) => {
      const note = await waitSentText(tg, OWNER, '这一版没有验证过', 0, 15_000)
      expect(note.text).toBe('【系统】dsh 的版本是 9.9.9，这一版没有验证过。模型回空内容时网关可能认不出来，会多花请求。')
      const e = await until(() => evs(b, 'dsh.version_unverified')[0], '日志 dsh.version_unverified')
      expect([e.level, e.version]).toEqual(['warn', '9.9.9'])
      await sleep(500)
      expect(ownerGot(tg).filter(t => (t ?? '').includes('这一版没有验证过')).length, '只发一次').toBe(1)
    })
  })
})
