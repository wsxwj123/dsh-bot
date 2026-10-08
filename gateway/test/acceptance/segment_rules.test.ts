// 验收：换段规则（INTERFACE 3.2-6 跨供应商换段、3.2-7 删掉正在用的供应商、BRIEF F5-3/F5-4、D6）
import { describe, expect, test } from 'bun:test'
import { mkdirSync, writeFileSync } from 'fs'
import { join } from 'path'
import { DS_KEY, OWNER, chatPrompts, eventsOf, evs, lifecycle, logEvents, override, readJsonl, readProviders, providersPath, seedCreds, seedProviders, segments, sleep, until, waitEvent, waitSentText, withBot, type BotEnv } from './_acc'

const KEY = 'test-key-7segment00000000001'
const mine = (b: BotEnv) => {
  seedProviders(b.root, [{ name: 'myproxy', baseURL: 'https://p.example.com/v1', models: [{ id: 'm1', contextWindow: 64000 }, { id: 'm2', contextWindow: 64000 }] }])
  seedCreds(b.root, { ...DS_KEY, PROVIDER_MYPROXY_KEY: KEY })
}
const GW = { provider_switch_summary_ms: 5_000 }

/** 主人说一句，等假模型回「收到：…」 */
async function say(tg: any, text: string, timeoutMs = 30_000) {
  const k = tg.sentTo(OWNER).length
  tg.pushText(OWNER, text)
  return waitSentText(tg, OWNER, `收到：${text}`, k, timeoutMs)
}
async function cmd(tg: any, text: string) {
  const k = tg.sentTo(OWNER).length
  tg.pushText(OWNER, text)
  return waitSentText(tg, OWNER, '【系统】', k)
}
/** 含某句话的那一轮（不含写摘要轮） */
const turnOf = (b: BotEnv, text: string) => chatPrompts(b).filter(p => p.text.includes(text)).pop()
const summaries = (b: BotEnv) => readJsonl<{ sessionId: string; mode?: string }>(join(b.acpState, 'summaries.jsonl'))
const order = (b: BotEnv, names: string[]) => names.map(n => logEvents(b).findIndex(e => e.event === n && (n === 'segment.provider_changed' || e.reason === 'provider-changed')))

/** 改共用文件，并等网关读到这次改动（新的 providers.reloaded） */
async function afterChange(b: BotEnv, change: () => void, name = 'providers.reloaded') {
  const n = evs(b, name).length
  change()
  await until(() => evs(b, name).length > n, `新的 ${name}`, 15_000)
}

/** 在内置上聊一句 → 换到自建 → 再聊一句 */
async function crossSwitch(tg: any) {
  await say(tg, '第一句话')
  await cmd(tg, '/model myproxy/m1')
  await say(tg, '第二句话')
}

describe('跨供应商换段（段里已有正常对话）', () => {
  test('日志依次出现 segment.provider_changed → segment.roll_start → segment.rolled（reason=provider-changed）', async () => {
    await withBot({ gw: GW, before: mine }, async ({ tg, b }) => {
      await crossSwitch(tg)
      await waitEvent(b, 'segment.rolled', e => e.reason === 'provider-changed')
      const [a, c, d] = order(b, ['segment.provider_changed', 'segment.roll_start', 'segment.rolled'])
      expect(a).toBeGreaterThanOrEqual(0)
      expect(a < c! && c! < d!).toBe(true)
      expect(evs(b, 'segment.provider_changed')[0]).toMatchObject({ from: expect.stringContaining('deepseek-official'), to: expect.stringContaining('myproxy') })
    })
  })

  test('账本 events 一条 segment_rolled（data.reason=provider-changed），旧段 close_reason=provider-changed', async () => {
    await withBot({ gw: GW, before: mine }, async ({ tg, b }) => {
      await crossSwitch(tg)
      expect(eventsOf(b, 'segment_rolled').filter(e => e.data?.reason === 'provider-changed').length).toBe(1)
      expect(segments(b).some(s => s.close_reason === 'provider-changed')).toBe(true)
    })
  })

  test('第二句在新会话里、用新模型 myproxy/m1', async () => {
    await withBot({ gw: GW, before: mine }, async ({ tg, b }) => {
      await crossSwitch(tg)
      const t1 = turnOf(b, '第一句话')!, t2 = turnOf(b, '第二句话')!
      expect(t2.sessionId).not.toBe(t1.sessionId)
      expect(JSON.parse(t2.model)).toEqual(['myproxy', 'm1'])
    })
  })

  test('先在旧会话里用旧模型写交接摘要，新会话带着摘要和最近原话', async () => {
    await withBot({ gw: GW, before: mine }, async ({ tg, b }) => {
      await crossSwitch(tg)
      const t1 = turnOf(b, '第一句话')!, t2 = turnOf(b, '第二句话')!
      expect(summaries(b).some(s => s.sessionId === t1.sessionId)).toBe(true)
      const fresh = chatPrompts(b).filter(p => p.sessionId === t2.sessionId).map(p => p.text).join('\n')
      expect(fresh).toContain('假摘要#')
      expect(fresh).toContain('第一句话')
    })
  })

  test('旧会话写摘要失败（summary-fail）：改用账本流水，照样换段并正常回复', async () => {
    await withBot({ gw: GW, before: b => { mine(b); mkdirSync(b.acpState, { recursive: true }); writeFileSync(join(b.acpState, 'summary-fail'), '1') } }, async ({ tg, b }) => {
      await crossSwitch(tg)
      expect(eventsOf(b, 'segment_rolled').some(e => e.data?.reason === 'provider-changed')).toBe(true)
      const t2 = turnOf(b, '第二句话')!
      expect(chatPrompts(b).filter(p => p.sessionId === t2.sessionId).map(p => p.text).join('\n')).toContain('第一句话')
    })
  })

  test('旧会话写摘要超过 provider_switch_summary_ms（1 秒）：不再等，改用账本流水，几秒内回复', async () => {
    await withBot({ gw: { provider_switch_summary_ms: 1_000 }, before: b => { mine(b); mkdirSync(b.acpState, { recursive: true }); writeFileSync(join(b.acpState, 'summary-delay-ms'), '20000') } }, async ({ tg, b }) => {
      await say(tg, '第一句话')
      await cmd(tg, '/model myproxy/m1')
      const t0 = Date.now()
      await say(tg, '第二句话', 15_000)
      expect(Date.now() - t0).toBeLessThan(12_000)
      expect(eventsOf(b, 'segment_rolled').some(e => e.data?.reason === 'provider-changed')).toBe(true)
    })
  })
})

describe('不换段的情况', () => {
  test('同一家里换模型：同一个会话，没有 segment.provider_changed，下一轮用新模型', async () => {
    await withBot({ gw: GW, before: b => seedCreds(b.root, DS_KEY) }, async ({ tg, b }) => {
      await say(tg, '第一句话')
      await cmd(tg, '/model deepseek-v4-pro')
      await say(tg, '第二句话')
      const t1 = turnOf(b, '第一句话')!, t2 = turnOf(b, '第二句话')!
      expect(t2.sessionId).toBe(t1.sessionId)
      expect(JSON.parse(t2.model)).toEqual(['deepseek-official', 'deepseek-v4-pro'])
      expect(evs(b, 'segment.provider_changed')).toEqual([])
      expect(eventsOf(b, 'segment_rolled')).toEqual([])
    })
  })

  test('段里还没有正常对话轮就换到另一家：不换段，直接用新模型', async () => {
    await withBot({ gw: GW, before: mine }, async ({ tg, b }) => {
      await cmd(tg, '/model myproxy/m1')
      await say(tg, '第一句话')
      expect(JSON.parse(turnOf(b, '第一句话')!.model)).toEqual(['myproxy', 'm1'])
      expect(evs(b, 'segment.provider_changed')).toEqual([])
      expect(eventsOf(b, 'segment_rolled')).toEqual([])
    })
  })

  test('只改密钥（凭据值与 keyRev 变）：同一个会话，不换段', async () => {
    await withBot({ gw: GW, before: mine }, async ({ tg, b }) => {
      await cmd(tg, '/model myproxy/m1')
      await say(tg, '第一句话')
      seedCreds(b.root, { ...DS_KEY, PROVIDER_MYPROXY_KEY: 'test-key-7segment00000000002' })
      const p = readProviders(b.root)
      p.providers.myproxy.meta.keyRev = 2
      await afterChange(b, () => writeFileSync(providersPath(b.root), JSON.stringify(p)))
      await say(tg, '第二句话')
      expect(turnOf(b, '第二句话')!.sessionId).toBe(turnOf(b, '第一句话')!.sessionId)
      expect(evs(b, 'segment.provider_changed')).toEqual([])
    })
  })
})
/** 在自建 myproxy 上聊一句，然后由"别处"把 myproxy 删掉，等 dsh 按新配置重启 */
async function useThenRemove(tg: any, b: BotEnv) {
  await cmd(tg, '/model myproxy/m1')
  await say(tg, '第一句话')
  const starts = lifecycle(b).filter(l => l.event === 'start').length
  seedProviders(b.root, [])
  await until(() => lifecycle(b).filter(l => l.event === 'start').length > starts, 'dsh 按新配置重启', 20_000)
  await say(tg, '第二句话')
}

describe('删掉某聊天正在用的供应商（3.2-7）', () => {
  test('下一条消息：segment.resume_failed → segment.created，不出现 segment.provider_changed', async () => {
    await withBot({ gw: GW, before: mine }, async ({ tg, b }) => {
      await useThenRemove(tg, b)
      expect(evs(b, 'segment.resume_failed').length).toBeGreaterThan(0)
      expect(evs(b, 'segment.created').length).toBeGreaterThan(0)
      expect(evs(b, 'segment.provider_changed')).toEqual([])
    })
  })

  test('账本：旧段 state=abandoned、close_reason=resume failed，events 有 summary_from_ledger', async () => {
    await withBot({ gw: GW, before: mine }, async ({ tg, b }) => {
      await useThenRemove(tg, b)
      expect(segments(b).some(s => s.state === 'abandoned' && s.close_reason === 'resume failed')).toBe(true)
      expect(eventsOf(b, 'summary_from_ledger').length).toBeGreaterThan(0)
    })
  })

  test('新会话用配置文件里的模型（deepseek-official / deepseek-flash）并正常回复', async () => {
    await withBot({ gw: GW, before: mine }, async ({ tg, b }) => {
      await useThenRemove(tg, b)
      expect(JSON.parse(turnOf(b, '第二句话')!.model)).toEqual(['deepseek-official', 'deepseek-flash'])
    })
  })
})

describe('自建供应商身份变了（epoch 变，相当于换了一家，D6）', () => {
  test('下一条消息换段（segment.provider_changed），且不碰旧会话（旧会话里不写摘要）', async () => {
    await withBot({ gw: GW, before: mine }, async ({ tg, b }) => {
      await cmd(tg, '/model myproxy/m1')
      await say(tg, '第一句话')
      const old = turnOf(b, '第一句话')!.sessionId
      const p = readProviders(b.root)
      p.providers.myproxy.meta.epoch = Date.now()
      p.providers.myproxy.route.baseURL = 'https://other-host.example.com/v1'
      await afterChange(b, () => writeFileSync(providersPath(b.root), JSON.stringify(p)))
      await say(tg, '第二句话')
      expect(evs(b, 'segment.provider_changed').length).toBe(1)
      expect(summaries(b).some(s => s.sessionId === old)).toBe(false)
      expect(turnOf(b, '第二句话')!.sessionId).not.toBe(old)
    })
  })
})

const OVERFLOW_NOTE = '【系统】deepseek-official / deepseek-flash 的上下文超长了，已换新会话。如果它的上下文长度填大了，请在 /model →「管理自建供应商的模型」里改小。'
const failNext = (b: BotEnv, text: string) => { mkdirSync(b.acpState, { recursive: true }); writeFileSync(join(b.acpState, 'next-error.txt'), text) }

describe('上下文超长（3.2-10）', () => {
  for (const msg of ["400 This model's maximum context length is 8192 tokens", 'context_length_exceeded', 'Prompt Is Too Long: 210000 tokens > 200000', 'TOO MANY TOKENS in request', 'input exceeds context_window', 'exceeds the context length']) {
    test(`错误文字「${msg}」：立即换段并重试一次，主人收到正常回复`, async () => {
      await withBot({ gw: GW }, async ({ tg, b }) => {
        await say(tg, '第一句话')
        failNext(b, msg)
        await say(tg, '第二句话')
        expect(eventsOf(b, 'segment_rolled').some(e => e.data?.reason === 'context-overflow')).toBe(true)
        expect(turnOf(b, '第二句话')!.sessionId).not.toBe(turnOf(b, '第一句话')!.sessionId)
      })
    })
  }

  test('通知主人「…的上下文超长了，已换新会话…」', async () => {
    await withBot({ gw: GW }, async ({ tg, b }) => {
      await say(tg, '第一句话')
      failNext(b, 'maximum context length exceeded')
      await say(tg, '第二句话')
      await waitSentText(tg, OWNER, OVERFLOW_NOTE)
    })
  })

  test('同一模型 10 分钟内只通知一次', async () => {
    await withBot({ gw: GW }, async ({ tg, b }) => {
      await say(tg, '第一句话')
      failNext(b, 'maximum context length exceeded')
      await say(tg, '第二句话')
      await waitSentText(tg, OWNER, OVERFLOW_NOTE)
      failNext(b, 'maximum context length exceeded')
      await say(tg, '第三句话')
      await sleep(500)
      expect(tg.sentTo(OWNER).filter(s => (s.text ?? '').includes('上下文超长了')).length).toBe(1)
    })
  })
})

describe('历史思考被拒（3.2-11）', () => {
  for (const msg of ['400 Missing `reasoning_content` field in the assistant message', 'Thinking blocks are required for tool use']) {
    test(`错误文字「${msg.slice(0, 30)}…」：换段（reason=thinking-history）并重试一次，日志 segment.thinking_history_rejected`, async () => {
      await withBot({ gw: GW }, async ({ tg, b }) => {
        await say(tg, '第一句话')
        failNext(b, msg)
        await say(tg, '第二句话')
        expect(eventsOf(b, 'segment_rolled').some(e => e.data?.reason === 'thinking-history')).toBe(true)
        expect(evs(b, 'segment.thinking_history_rejected').length).toBeGreaterThan(0)
      })
    })
  }

  test('错误文字不含这些关键词（上游 502）：不按上下文超长或思考被拒换段', async () => {
    await withBot({ gw: GW }, async ({ tg, b }) => {
      await say(tg, '第一句话')
      failNext(b, 'upstream returned 502 Bad Gateway')
      tg.pushText(OWNER, '第二句话')
      await sleep(4_000)
      expect(eventsOf(b, 'segment_rolled').filter(e => ['context-overflow', 'thinking-history'].includes(e.data?.reason))).toEqual([])
    })
  })
})

describe('覆盖自动失效与暂停（3.2-8）', () => {
  test('providers.json 里确实没有这个供应商了：清覆盖（brain.override_cleared reason=provider removed），回到配置文件的模型', async () => {
    await withBot({ gw: GW, before: mine }, async ({ tg, b, gw }) => {
      await cmd(tg, '/model myproxy/m1')
      seedProviders(b.root, [])
      await waitEvent(b, 'brain.override_cleared', e => e.reason === 'provider removed')
      expect((await gw.call('/v1/model')).json.current).toEqual({ provider: 'deepseek-official', model: 'deepseek-flash' })
    })
  })

  test('这个供应商确实没有这个模型了：清覆盖（reason=model removed）', async () => {
    await withBot({ gw: GW, before: mine }, async ({ tg, b }) => {
      await cmd(tg, '/model myproxy/m1')
      seedProviders(b.root, [{ name: 'myproxy', baseURL: 'https://p.example.com/v1', models: ['m2'] }])
      await waitEvent(b, 'brain.override_cleared', e => e.reason === 'model removed')
    })
  })

  test('条目变得不合格（暂时不可用）：不清覆盖，日志 brain.override_suspended；改回后下一轮又用覆盖的模型', async () => {
    await withBot({ gw: GW, before: mine }, async ({ tg, b }) => {
      await cmd(tg, '/model myproxy/m1')
      const good = readProviders(b.root)
      const bad = JSON.parse(JSON.stringify(good))
      bad.providers.myproxy.route.api = 'grpc'
      writeFileSync(providersPath(b.root), JSON.stringify(bad))
      await waitEvent(b, 'brain.override_suspended')
      expect(evs(b, 'brain.override_cleared')).toEqual([])
      await afterChange(b, () => writeFileSync(providersPath(b.root), JSON.stringify(good)))
      await sleep(1_500)
      await say(tg, '恢复后')
      expect(JSON.parse(turnOf(b, '恢复后')!.model)).toEqual(['myproxy', 'm1'])
    })
  })

  test('运行中 providers.json 读坏：不清覆盖（账本里的覆盖还在）', async () => {
    await withBot({ gw: GW, before: mine }, async ({ tg, b }) => {
      await cmd(tg, '/model myproxy/m1')
      writeFileSync(providersPath(b.root), '{ broken')
      await waitEvent(b, 'providers.reload_failed')
      await sleep(800)
      expect(evs(b, 'brain.override_cleared')).toEqual([])
      expect(override(b)).toMatchObject({ provider: 'myproxy', model: 'm1' })
    })
  })
})
