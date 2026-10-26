// dsh-group-speak 第二轮：网关配置项 gateway.group_turn_ttl_ms 的范围校验（接口文件第 4 节第 20 条）。
// 合法范围 100 到 3600000 的整数；越界网关报配置错误、不启动，错误信息含配置项名。
// 只通过对外接口：配置文件起网关子进程，看退出码、stderr/stdout。
import { expect, test } from 'bun:test'
import { rmSync } from 'fs'
import { FakeTelegram } from '../fakes/fake-telegram'
import { Gateway, makeBot, until, withBot } from './_acc'

async function expectConfigError(ttl: number) {
  const tg = new FakeTelegram()
  const b = makeBot(tg, { gw: { group_turn_ttl_ms: ttl } })
  const g = new Gateway(b)
  try {
    const code = await g.startExpectExit()
    expect(code).not.toBeNull()
    expect(code).not.toBe(0)
    await until(() => (g.stderr + g.stdout).includes('group_turn_ttl_ms'), '错误信息里出现 group_turn_ttl_ms', 3_000).catch(() => {})
    expect(g.stderr + g.stdout).toContain('group_turn_ttl_ms')
  } finally { await g.stop().catch(() => {}); tg.stop(); rmSync(b.root, { recursive: true, force: true }) }
}

test('gateway.group_turn_ttl_ms=0：报配置错误、不启动，错误信息含配置项名', async () => {
  await expectConfigError(0)
})

test('gateway.group_turn_ttl_ms=99：报配置错误、不启动，错误信息含配置项名', async () => {
  await expectConfigError(99)
})

test('gateway.group_turn_ttl_ms=100（下界合格值）：正常启动', async () => {
  await withBot({ gw: { group_turn_ttl_ms: 100 } }, async ({ gw }) => {
    expect((await gw.call('/v1/model')).status).toBe(200)
  })
})
