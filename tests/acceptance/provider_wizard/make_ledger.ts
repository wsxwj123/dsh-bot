// 测试工具（给 pytest 用）：用真网关 + 假 Telegram + 假 dsh，在指定根目录下给某个 bot 生成一个"聊过一轮"的账本，然后停掉网关。
// 用法：bun make_ledger.ts <根目录> <bot 名> <发消息的人的 id | none> <这句话>
// who=none：只启动再停止网关（账本存在、没有任何会话段）。
import { FakeTelegram } from '../../../gateway/test/fakes/fake-telegram'
import { Gateway, makeBot, waitSentText } from '../../../gateway/test/acceptance/_acc'

const [root, name, who, text] = process.argv.slice(2)
if (!root || !name || !who) { console.error('用法：bun make_ledger.ts <根目录> <bot 名> <id|none> <这句话>'); process.exit(2) }
const tg = new FakeTelegram()
const b = makeBot(tg, { root, name })
const gw = new Gateway(b)
try {
  await gw.start()
  if (who !== 'none') {
    const id = Number(who)
    tg.pushText(id, text ?? '你好')
    await waitSentText(tg, id, `收到：${text ?? '你好'}`, 0, 30_000)
  }
} finally {
  await gw.stop()
  tg.stop()
}
console.log('ok')
