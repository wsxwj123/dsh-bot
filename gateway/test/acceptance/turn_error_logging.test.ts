// 黑盒验收：回合失败时把原因写进日志（依据接口文件"行为清单 A–E + 相邻回归 R1"）。
// 只通过对外接口：以子进程拉起网关、经假 Telegram 投递消息、读它落盘的 gateway.log 里的 JSON 行。
// 不 import gateway/src 下任何模块，不 mock 内部层，不断言内部状态。
import { describe, expect, setDefaultTimeout, test } from 'bun:test'
import { join } from 'path'
import { FRIEND, evs, until, withBot, type BotEnv } from './_acc'

setDefaultTimeout(60_000)

/** 另建的极简假 ACP：只做"先输出一段文字（ABCDE）再卡死，收到取消才回话"这一件事 */
const CHUNK_HANG_ACP = join(import.meta.dir, 'fake-acp-chunk-hang.ts')

/** 接口 B3 规定的五个合法取值 */
const REASONS = ['empty-response', 'fatal', 'context-overflow', 'thinking-history', 'other']

const has = (e: Record<string, any>, k: string) => Object.prototype.hasOwnProperty.call(e, k)
const turnEnds = (b: BotEnv) => evs(b, 'turn.end')
const friendSays = (tg: { pushText: (id: number, text: string) => number }, b: BotEnv, text: string) => tg.pushText(FRIEND, text)

/** 等第一条 turn.end（这一轮有了结局） */
const waitTurnEnd = (b: BotEnv) => until(() => turnEnds(b)[0], '第一条 turn.end', 20_000)

/** 让网关把"卡死"时间调小，免得空等 5 分钟 */
const FAST_STALL = { turn_stall_warn_ms: 300, turn_stall_cancel_ms: 1_000 }

describe('A. 正常结束不加噪音', () => {
  test('A1 正常回复并结束：turn.end 的 outcome 为 ok，且不含 reason、不含 err', async () => {
    await withBot({}, async ({ tg, b }) => {
      friendSays(tg as any, b, '在吗')
      const e = await waitTurnEnd(b)
      expect(e.outcome).toBe('ok')
      expect(has(e, 'reason'), `turn.end 不该有 reason：${JSON.stringify(e)}`).toBe(false)
      expect(has(e, 'err'), `turn.end 不该有 err：${JSON.stringify(e)}`).toBe(false)
    })
  })

  test('A2 回合因长期没有进展被强制取消：turn.end 的 outcome 为 cancelled，且不含 reason、err', async () => {
    await withBot({ gw: FAST_STALL }, async ({ tg, b }) => {
      friendSays(tg as any, b, '卡住 !hang')
      const e = await waitTurnEnd(b)
      expect(e.outcome).toBe('cancelled')
      expect(has(e, 'reason'), `turn.end 不该有 reason：${JSON.stringify(e)}`).toBe(false)
      expect(has(e, 'err'), `turn.end 不该有 err：${JSON.stringify(e)}`).toBe(false)
    })
  })
})

describe('B. 报错必须写明原因', () => {
  test('B1 处理中报错且这一轮已发过内容：出现 turn.error_after_action，含非空字符串的 reason 与 err', async () => {
    await withBot({}, async ({ tg, b }) => {
      friendSays(tg as any, b, '在吗 !failafter:upstream_boom')
      const e = await until(() => evs(b, 'turn.error_after_action')[0], 'turn.error_after_action', 20_000)
      expect(typeof e.reason, `reason 类型：${JSON.stringify(e)}`).toBe('string')
      expect((e.reason as string).length).toBeGreaterThan(0)
      expect(typeof e.err, `err 类型：${JSON.stringify(e)}`).toBe('string')
      expect((e.err as string).length).toBeGreaterThan(0)
    })
  })

  test('B2 同一回合的 turn.end：outcome 为 error，且同时含 reason 与 err', async () => {
    await withBot({}, async ({ tg, b }) => {
      friendSays(tg as any, b, '在吗 !failafter:upstream_boom')
      const e = await waitTurnEnd(b)
      expect(e.outcome).toBe('error')
      expect(typeof e.reason, `reason 类型：${JSON.stringify(e)}`).toBe('string')
      expect((e.reason as string).length).toBeGreaterThan(0)
      expect(typeof e.err, `err 类型：${JSON.stringify(e)}`).toBe('string')
      expect((e.err as string).length).toBeGreaterThan(0)
    })
  })

  test('B3 reason 的取值只能是 empty-response / fatal / context-overflow / thinking-history / other 五个之一', async () => {
    await withBot({}, async ({ tg, b }) => {
      friendSays(tg as any, b, '在吗 !failafter:upstream_boom')
      const end = await waitTurnEnd(b)
      expect(REASONS).toContain(end.reason)
      const after = await until(() => evs(b, 'turn.error_after_action')[0], 'turn.error_after_action', 20_000)
      expect(REASONS).toContain(after.reason)
    })
  })

  test('B4 模型进程崩溃：turn.end 的 outcome 为 crashed，含 err，不含 reason', async () => {
    await withBot({}, async ({ tg, b }) => {
      friendSays(tg as any, b, '在吗 !crash:1:once')
      const e = await until(() => turnEnds(b).find(x => x.outcome === 'crashed'), 'outcome=crashed 的 turn.end', 20_000)
      expect(typeof e.err, `err 类型：${JSON.stringify(e)}`).toBe('string')
      expect((e.err as string).length).toBeGreaterThan(0)
      expect(has(e, 'reason'), `crashed 不该有 reason：${JSON.stringify(e)}`).toBe(false)
    })
  })
})

describe('C. 空回复不误报', () => {
  test('C1 已发过内容、模型正常结束但没有任何内容：出现 turn.empty_response，turn.end 为 ok 且不含 reason、err', async () => {
    await withBot({}, async ({ tg, b }) => {
      friendSays(tg as any, b, '在吗 !emptyafter')
      await until(() => evs(b, 'turn.empty_response')[0], 'turn.empty_response', 20_000)
      const e = await waitTurnEnd(b)
      expect(e.outcome).toBe('ok')
      expect(has(e, 'reason'), `turn.end 不该有 reason：${JSON.stringify(e)}`).toBe(false)
      expect(has(e, 'err'), `turn.end 不该有 err：${JSON.stringify(e)}`).toBe(false)
    })
  })
})

describe('D. 脱敏与截断', () => {
  const BOT_TOKEN = '987654321:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij' // 9 位数字 + 冒号 + 36 位字母数字
  const SK_KEY = 'sk-liveabcdefghijklmnopqrstuvwxyz0123456789'

  test('D1 错误文本里的 Telegram bot 令牌落盘后被替换为 ***', async () => {
    await withBot({}, async ({ tg, b }) => {
      friendSays(tg as any, b, `在吗 !failafter:${BOT_TOKEN}`)
      const e = await waitTurnEnd(b)
      expect(typeof e.err, `err 类型：${JSON.stringify(e)}`).toBe('string')
      expect(e.err).toContain('***')
      expect(e.err, `令牌不该落盘：${JSON.stringify(e)}`).not.toContain(BOT_TOKEN)
      expect(e.err).not.toContain('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij')
    })
  })

  test('D2 错误文本里 sk- 开头的密钥落盘后被替换为 ***', async () => {
    await withBot({}, async ({ tg, b }) => {
      friendSays(tg as any, b, `在吗 !failafter:${SK_KEY}`)
      const e = await waitTurnEnd(b)
      expect(typeof e.err, `err 类型：${JSON.stringify(e)}`).toBe('string')
      expect(e.err).toContain('***')
      expect(e.err, `密钥不该落盘：${JSON.stringify(e)}`).not.toContain(SK_KEY)
    })
  })

  test('D3 错误文本超过 200 字符：err 字段按 UTF-16 码元算不超过 200', async () => {
    await withBot({}, async ({ tg, b }) => {
      friendSays(tg as any, b, `在吗 !failafter:${'A'.repeat(250)}`)
      const e = await waitTurnEnd(b)
      expect(typeof e.err, `err 类型：${JSON.stringify(e)}`).toBe('string')
      expect((e.err as string).length).toBeGreaterThan(0)
      expect((e.err as string).length, `err 长度：${(e.err as string).length}`).toBeLessThanOrEqual(200)
    })
  })
})

describe('E. 卡死要能区分「没吐字」和「吐了字没调工具」', () => {
  test('E1 因 300 秒没有进展被强制取消：出现 turn.stalled_cancel，含整数 chars_out', async () => {
    await withBot({ gw: FAST_STALL }, async ({ tg, b }) => {
      friendSays(tg as any, b, '卡住 !hang')
      const e = await until(() => evs(b, 'turn.stalled_cancel')[0], 'turn.stalled_cancel', 20_000)
      expect(Number.isInteger(e.chars_out), `chars_out 应为整数：${JSON.stringify(e)}`).toBe(true)
    })
  })

  test('E2 一个字都没输出就卡死：stalled_cancel 的 chars_out 为 0', async () => {
    await withBot({ gw: FAST_STALL }, async ({ tg, b }) => {
      friendSays(tg as any, b, '卡住 !hang')
      const e = await until(() => evs(b, 'turn.stalled_cancel')[0], 'turn.stalled_cancel', 20_000)
      expect(e.chars_out).toBe(0)
    })
  })

  test('E3 输出过内容之后卡死：stalled_cancel 的 chars_out 为大于 0 的整数', async () => {
    await withBot({ gw: { ...FAST_STALL, dsh_command: ['bun', CHUNK_HANG_ACP] } }, async ({ tg, b }) => {
      friendSays(tg as any, b, '先说一半再卡住')
      const e = await until(() => evs(b, 'turn.stalled_cancel')[0], 'turn.stalled_cancel', 20_000)
      expect(Number.isInteger(e.chars_out), `chars_out 应为整数：${JSON.stringify(e)}`).toBe(true)
      expect(e.chars_out).toBeGreaterThan(0)
    })
  })
})

describe('R. 相邻回归', () => {
  test('R1 完全正常的回合，turn.end 仍含改动前就有的 turn、kind、attempt、outcome、delivered、silent', async () => {
    await withBot({}, async ({ tg, b }) => {
      friendSays(tg as any, b, '在吗')
      const e = await waitTurnEnd(b)
      for (const k of ['turn', 'kind', 'attempt', 'outcome', 'delivered', 'silent']) {
        expect(has(e, k), `turn.end 缺字段 ${k}：${JSON.stringify(e)}`).toBe(true)
      }
    })
  })
})
