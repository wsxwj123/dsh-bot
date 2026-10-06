// 离线演练：把“需要真密钥”的四个脚本指向本机假模型服务跑一遍，只验证流程能走通（数字是假的）。
// 用法：DSH=<dsh 路径> bun lab/dsh/offline/dry-run-real.ts
import { startFakeLlm, type Step } from '../lib/fake-llm'
import { join } from 'path'

const port = 18199
const script: Step[] = []
// real-03：每条用户消息先调 reply 工具，再空文本结束；写摘要那一轮直接输出文本
const turns = ['叫我小林就行', '叫豆豆', '去成都出差', '提醒我给我妈打个电话', '喜欢看什么书', '看哭了']
for (const t of turns) {
  script.push({ match: t, tool: { name: 'mcp__tg__reply', arguments: { text: `（假回复）收到：${t}` } } })
  script.push({ match: t, text: '' })
}
script.push({ match: '整理记忆', text: '【我说过的要紧话】（假摘要）\n【我答应过的事】无\n【对方的情况】小林，橘猫豆豆\n【正在聊的话题】无\n【我们现在的关系和气氛】陌生' })
script.push({ match: '我的猫叫什么', tool: { name: 'mcp__tg__reply', arguments: { text: '（假回复）豆豆，下周三' } } })
script.push({ match: '我的猫叫什么', text: '' })
const fake = startFakeLlm({ port, script })
const env = { ...process.env, DEEPSEEK_API_KEY: 'sk-dry-run-not-real', DEEPSEEK_UPSTREAM: `http://127.0.0.1:${port}` }
for (const s of ['real-01-persona-tokens', 'real-02-reasoning-billing', 'real-03-epoch-summary', 'real-04-builtin-compaction']) {
  console.log(`\n================ ${s}`)
  const p = Bun.spawn(['bun', join(import.meta.dir, '..', 'real', `${s}.ts`), '--extra', '1200'], { env, stdout: 'inherit', stderr: 'inherit' })
  const code = await p.exited
  console.log(`================ ${s} exit=${code}`)
}
fake.stop()
