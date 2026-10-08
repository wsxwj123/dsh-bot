// 引导的纯函数（方案 3.3.2、3.5.3、3.3.5）：callback_data 编解码、按钮字与菜单截断、上下文长度写法、像密钥判定、思考档位显示名。
import { describe, expect, test } from 'bun:test'
import { decodeCb, encodeCb, makeNonce, pageSlice, truncLabel, truncateMenu } from '../../src/wizard/keyboard'
import { effortLabel, looksLikeSecret, parseContext, effortValue } from '../../src/wizard/text'

describe('callback_data', () => {
  test('编码后能解回；口令 8 位字母数字、不含 |', () => {
    const n = makeNonce()
    expect(n).toMatch(/^[A-Za-z0-9]{8}$/)
    const d = encodeCb(n, 'pv', 12)
    expect(d).toBe(`w|${n}|pv|12`)
    expect(decodeCb(d)).toEqual({ nonce: n, action: 'pv', idx: 12 })
    expect(decodeCb(encodeCb(n, 'cancel'))).toEqual({ nonce: n, action: 'cancel', idx: null })
    expect(new TextEncoder().encode(d).length).toBeLessThanOrEqual(40)
  })

  test('畸形一律返回 null', () => {
    for (const d of ['garbage', 'x|12345678|new', '', 'w|short|a', 'w|12345678|a|b|c', 'w|12345678|a|x', 42, null]) {
      expect(decodeCb(d as unknown)).toBeNull()
    }
  })
})

describe('按钮字与菜单截断', () => {
  test('名字超过 40 个字符保留前 18 后 18，正好 40 原样', () => {
    const long = 'abcdefghijklmnopqr' + 'z'.repeat(20) + 'stuvwxyz0123456789'
    expect(truncLabel(long)).toBe(`${long.slice(0, 18)}…${long.slice(-18)}`)
    const forty = 'x'.repeat(39) + '1'
    expect(truncLabel(forty)).toBe(forty)
  })

  test('分页：每页 8 个，页码超出夹到有效区间', () => {
    const items = Array.from({ length: 20 }, (_, i) => `m${i}`)
    expect(pageSlice(items, 0).slice).toEqual(items.slice(0, 8))
    expect(pageSlice(items, 1).slice).toEqual(items.slice(8, 16))
    expect(pageSlice(items, 99).page).toBe(2)
  })

  test('超过 3500 字截断，末尾写「…还有 N 个」，总长不超 Telegram 上限', () => {
    const header = ['【系统】「many」的模型（60 个）：']
    const lines = Array.from({ length: 60 }, (_, i) => `model-${i}-` + 'q'.repeat(120))
    const t = truncateMenu(header, lines, '个')
    expect(t.length).toBeLessThanOrEqual(3600)
    expect(t).toMatch(/…还有 \d+ 个$/)
  })
})

describe('上下文长度写法', () => {
  for (const [inp, out] of [['128000', 128000], ['128k', 128000], ['1.5m', 1500000], ['32K', 32000], ['1M', 1000000], ['1024', 1024], ['100000000', 100000000], ['1.0245k', 1025]] as const) {
    test(`${inp} → ${out}`, () => expect(parseContext(inp)).toBe(out))
  }
  for (const bad of ['1023', '100000001', 'abc', '-5', '0', '1.5', '128 k', '']) {
    test(`${JSON.stringify(bad)} 不合格`, () => expect(parseContext(bad)).toBeNull())
  }
})

describe('像密钥判定', () => {
  for (const ok of ['abcd1234', 'sk.live.12345678abcdef', 'test-key-7' + 'a'.repeat(20)]) {
    test(`${ok} 算像密钥`, () => expect(looksLikeSecret(ok)).toBe(true))
  }
  for (const no of ['abc1234', 'hahahahahah', '1234567890123', 'https://example.com/abc123def', 'abc12345 def67890', '我的密码abc12345', 'example.com/v1', 'ftp://x.example.com/v1']) {
    test(`${no} 不像密钥`, () => expect(looksLikeSecret(no)).toBe(false))
  }
})

describe('思考档位显示名', () => {
  test('off/low/high/max 显示为 关/低/高/最高，其它按原名', () => {
    expect(['off', 'low', 'high', 'max', 'xhigh'].map(effortLabel)).toEqual(['关', '低', '高', '最高', 'xhigh'])
    expect(effortValue('高', ['off', 'low', 'high', 'max'])).toBe('high')
    expect(effortValue('中', ['off', 'low', 'high'])).toBeNull()
  })
})
