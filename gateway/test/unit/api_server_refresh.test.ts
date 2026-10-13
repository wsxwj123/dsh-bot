// 本机接口 POST /v1/provider/refresh 的请求体校验（INTERFACE-dsh修补 3.3、5.4）：形状不对在这一层就回 400，
// 不交给命令层（不拉取、不写盘）；形状对的原样交下去。白盒：直接调 ApiServer.handle，不起端口。
import { expect, test } from 'bun:test'
import { ApiServer, type ApiDeps } from '../../src/api/server'
import { Logger } from '../../src/log'

const TOKEN = 'unit-token-0123456789'

function server() {
  const calls: { name: string; how: unknown }[] = []
  const provider = { refresh: async (name: string, how: unknown) => { calls.push({ name, how }); return { status: 200, body: { ok: true } } } } as unknown as ApiDeps['provider']
  const s = new ApiServer({ token: TOKEN, port: 0, log: new Logger({}), provider } as unknown as ApiDeps)
  const post = async (body: unknown) => {
    const res = await s.handle(new Request('http://127.0.0.1/v1/provider/refresh', {
      method: 'POST', headers: { authorization: `Bearer ${TOKEN}`, 'content-type': 'application/json' }, body: JSON.stringify(body),
    }))
    return { status: res.status, json: await res.json() as Record<string, unknown> }
  }
  return { post, calls }
}

test('rev 形状不对（不是字符串、空串、超过 100 个字符、null）：400 bad_rev，不交给命令层', async () => {
  const { post, calls } = server()
  for (const rev of [123, '', 'r'.repeat(101), ['x'], { a: 1 }, true, null]) {
    expect(await post({ name: 'myproxy', only: ['a'], rev }), JSON.stringify(rev).slice(0, 20)).toEqual({ status: 400, json: { ok: false, error: 'bad_rev', text: 'rev 不合法' } })
  }
  expect(calls).toEqual([])
})

test('rev 合格（1 到 100 个字符）原样交下去；没带 rev 就不带；不带 only 也能带 rev', async () => {
  const { post, calls } = server()
  expect((await post({ name: 'myproxy', only: ['a'], rev: 'r'.repeat(100) })).status).toBe(200)
  expect((await post({ name: 'myproxy', only: ['a'] })).status).toBe(200)
  expect((await post({ name: 'myproxy', rev: 'x' })).status).toBe(200)
  expect(calls).toEqual([
    { name: 'myproxy', how: { only: ['a'], rev: 'r'.repeat(100), full: false } },
    { name: 'myproxy', how: { only: ['a'], rev: undefined, full: false } },
    { name: 'myproxy', how: { only: undefined, rev: 'x', full: false } },
  ])
})

test('full 不是布尔值（字符串、数字、null）：400 bad_body；full 和 only 同时给：400 bad_body；都不交给命令层', async () => {
  const { post, calls } = server()
  for (const full of ['true', 1, 0, 'yes', null, []]) {
    expect(await post({ name: 'myproxy', full }), JSON.stringify(full)).toEqual({ status: 400, json: { ok: false, error: 'bad_body', text: 'full 必须是 true 或 false' } })
  }
  for (const only of [['m1'], []]) {
    expect(await post({ name: 'myproxy', only, full: true })).toEqual({ status: 400, json: { ok: false, error: 'bad_body', text: 'only 和 full 不能同时给' } })
  }
  expect(calls).toEqual([])
})

test('full 为 true 才要求全量；false 等同没给，可以和 only 一起出现', async () => {
  const { post, calls } = server()
  expect((await post({ name: 'myproxy', full: true })).status).toBe(200)
  expect((await post({ name: 'myproxy', full: false })).status).toBe(200)
  expect((await post({ name: 'myproxy', only: ['a'], full: false })).status).toBe(200)
  expect((await post({ name: 'myproxy' })).status).toBe(200)
  expect(calls.map(c => c.how)).toEqual([
    { only: undefined, rev: undefined, full: true }, { only: undefined, rev: undefined, full: false },
    { only: ['a'], rev: undefined, full: false }, { only: undefined, rev: undefined, full: false },
  ])
})

test('校验的先后：名字 → only → rev → full，先命中先回', async () => {
  const { post, calls } = server()
  expect((await post({ name: 'bad.name', only: 'x', rev: 1, full: 1 })).json.error).toBe('bad_name')
  expect((await post({ name: 'myproxy', only: 'x', rev: 1, full: 1 })).json.error).toBe('bad_only')
  expect((await post({ name: 'myproxy', only: ['a'], rev: 1, full: 1 })).json.error).toBe('bad_rev')
  expect((await post({ name: 'myproxy', only: ['a'], rev: 'x', full: 1 })).json.text).toBe('full 必须是 true 或 false')
  expect(calls).toEqual([])
})
