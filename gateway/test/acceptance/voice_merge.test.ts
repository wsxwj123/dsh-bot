// voice-merge 验收测试（黑盒，依据 .devflow/voice-merge-接口.md 的 12 条行为）。
// 只通过对外入口：配置文件起网关子进程、假 Telegram 收消息、假语音桥记录 /synthesize_voice 请求、
// 假 ACP 的 `!tool:reply|JSON` 指令让模型带指定参数调用 reply 工具。不 import 网关产品模块。
//
// 假 ACP 在执行完 !tool 指令后还会自动再调一次 reply（普通文字、不带语音），这里用 `!say:` 把那句
// 固定成 TAIL：看到 TAIL 就说明本轮工具调用都已执行完；比较消息序列时把 TAIL 剔掉。
import { expect, test } from 'bun:test'
import { mkdtempSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { OWNER, readJsonl, sleep, waitSentText, withBot } from './_acc'

const TAIL = '本轮结束标记'
const T = 60_000

type BridgeCall = { path: string; body: any }
type Outcome = {
  /** bot 发给主人的消息序列（去掉 TAIL）：文字 = `文:<内容>`，语音 = `音`，图片 = `图` */
  seq: string[]
  /** /synthesize_voice 收到的每次合成文本 */
  synth: string[]
  /** reply 工具每次调用的结果（模型看到的） */
  replyResults: { args: any; text: string; isError: boolean }[]
}

/** 起假语音桥 + 网关，主人说一句话，让模型按 replies 依次调用 reply，收集结果 */
async function run(o: { replies: Record<string, unknown>[]; voiceId?: boolean; bridgeFail?: boolean; images?: string[] }): Promise<Outcome> {
  const calls: BridgeCall[] = []
  const bridge = Bun.serve({
    hostname: '127.0.0.1', port: 0,
    async fetch(req) {
      const url = new URL(req.url)
      const body = await req.json().catch(() => ({})) as any
      calls.push({ path: url.pathname, body })
      if (o.bridgeFail) return new Response('down', { status: 503 })
      if (url.pathname === '/synthesize_voice') return new Response(new Uint8Array([79, 103, 103, 83]), { headers: { 'content-type': 'audio/ogg' } })
      return new Response('nf', { status: 404 })
    },
  })
  const imgDir = mkdtempSync(join(tmpdir(), 'dshvm-img-'))
  const imgPaths = (o.images ?? []).map(n => { const p = join(imgDir, n); writeFileSync(p, 'png'); return p })
  const resolveFiles = (r: Record<string, unknown>) => (Array.isArray(r.files) ? { ...r, files: (r.files as number[]).map(i => imgPaths[i]) } : r)
  let out: Outcome | null = null
  try {
    await withBot({
      gw: { burst_window_ms: 0, voice_bridge_url: `http://127.0.0.1:${bridge.port}`, image_dirs: [imgDir] },
      access: o.voiceId === false ? {} : { voiceId: 'test-voice' },
    }, async ({ tg, b }) => {
      const lines = ['说点什么', ...o.replies.map(r => `!tool:reply|${JSON.stringify(resolveFiles(r))}`), `!say:${TAIL}`]
      tg.pushText(OWNER, lines.join('\n'))
      await waitSentText(tg, OWNER, TAIL, 0, 30_000)
      await sleep(1_500) // 给可能晚到的语音/图片留时间，也让"不该发"的东西有机会冒出来
      const seq = tg.sentTo(OWNER)
        .filter(s => s.text !== TAIL)
        .map(s => (s.method === 'sendMessage' ? `文:${s.text}` : s.method === 'sendVoice' ? '音' : s.method === 'sendPhoto' ? '图' : s.method))
      const replyResults = readJsonl<{ name: string; args: any; text: string; isError: boolean }>(join(b.acpState, 'tool-results.jsonl'))
        .filter(r => r.name === 'reply' && r.args?.text !== TAIL)
      out = { seq, synth: calls.filter(c => c.path === '/synthesize_voice').map(c => String(c.body.text)), replyResults }
    })
  } finally {
    bridge.stop(true)
    rmSync(imgDir, { recursive: true, force: true })
  }
  return out!
}

test('行为1 三段文字+三段朗读稿：先 3 条文字再恰好 1 条语音，只合成 1 次，合成文本是三段朗读稿空格相连', async () => {
  const r = await run({ replies: [{ text: '文一\n\n文二\n\n文三', as_voice: true, voice_text: '[开心地] 甲\n\n[温柔地] 乙\n\n[轻声] 丙' }] })
  expect(r.seq).toEqual(['文:文一', '文:文二', '文:文三', '音'])
  expect(r.synth).toEqual(['[开心地] 甲 [温柔地] 乙 [轻声] 丙'])
}, T)

test('行为2 三段文字不给朗读稿：合成文本是三段文字空格相连，1 条语音排在 3 条文字之后', async () => {
  const r = await run({ replies: [{ text: '文一\n\n文二\n\n文三', as_voice: true }] })
  expect(r.seq).toEqual(['文:文一', '文:文二', '文:文三', '音'])
  expect(r.synth).toEqual(['文一 文二 文三'])
}, T)

test('行为3 朗读稿中间一段为空：合成文本是"甲 丙"（没有连续两个空格），1 条语音', async () => {
  const r = await run({ replies: [{ text: '文一\n\n文二\n\n文三', as_voice: true, voice_text: '甲\n\n\n\n丙' }] })
  expect(r.synth).toEqual(['甲 丙'])
  expect(r.seq).toEqual(['文:文一', '文:文二', '文:文三', '音'])
}, T)

test('行为4 朗读稿整个是空白：等同没给朗读稿，合成文本是三段文字空格相连，3 条文字后恰好 1 条语音，只合成 1 次', async () => {
  const r = await run({ replies: [{ text: '文一\n\n文二\n\n文三', as_voice: true, voice_text: '  \n\n \t \n\n   ' }] })
  expect(r.synth).toEqual(['文一 文二 文三'])
  expect(r.seq).toEqual(['文:文一', '文:文二', '文:文三', '音'])
}, T)

test('行为5 两段文字+一张末尾图片（无图片标记）：顺序是 文字、文字、语音、图片', async () => {
  const r = await run({ images: ['pic-a.png'], replies: [{ text: '文一\n\n文二', as_voice: true, files: [0] }] })
  expect(r.seq).toEqual(['文:文一', '文:文二', '音', '图'])
  expect(r.synth).toEqual(['文一 文二'])
}, T)

test('行为6 文字里有 [[图1]] 标记：顺序是 文字甲、图片、文字乙、语音，合成文本"甲 乙"', async () => {
  const r = await run({ images: ['pic-b.png'], replies: [{ text: '甲\n\n[[图1]]\n\n乙', as_voice: true, files: [0] }] })
  expect(r.seq).toEqual(['文:甲', '图', '文:乙', '音'])
  expect(r.synth).toEqual(['甲 乙'])
}, T)

test('行为7 as_voice 不传或为 false：不调语音桥，只收到文字', async () => {
  const r = await run({ replies: [{ text: '文一\n\n文二' }, { text: '文三\n\n文四', as_voice: false }] })
  expect(r.synth).toEqual([])
  expect(r.seq).toEqual(['文:文一', '文:文二', '文:文三', '文:文四'])
}, T)

test('行为8 语音桥返回 503：全部文字照常送达，reply 结果不是 isError，不发语音', async () => {
  const r = await run({ bridgeFail: true, replies: [{ text: '文一\n\n文二\n\n文三', as_voice: true }] })
  expect(r.seq).toEqual(['文:文一', '文:文二', '文:文三'])
  expect(r.replyResults).toHaveLength(1)
  expect(r.replyResults[0]!.isError).toBe(false)
}, T)

test('行为9 第二次 reply 重发已送达段+新文字：重复段不再发也不进合成，合成只含新段朗读稿，1 条语音', async () => {
  // 第一次发两段（不带语音）；第二次重发其中一段、再加新文字。
  // 新文字用两段而不是接口文件字面的一段：实测现有网关在"重复段占一半"（1 重复 + 1 新）时把第二次整条当重复拒掉，
  // 新段也不发，带不带语音都一样——那是改动前就有的去重规则，不是语音合并要测的，见报告疑问。
  const r = await run({ replies: [
    { text: '旧的第一段\n\n旧的第二段' },
    { text: '旧的第一段\n\n新的甲段\n\n新的乙段', as_voice: true, voice_text: '旧稿\n\n新稿甲\n\n新稿乙' },
  ] })
  expect(r.seq).toEqual(['文:旧的第一段', '文:旧的第二段', '文:新的甲段', '文:新的乙段', '音'])
  expect(r.synth).toEqual(['新稿甲 新稿乙'])
}, T)

test('行为10 第二次 reply 全是已送达过的文字：不调语音桥、不发语音', async () => {
  const r = await run({ replies: [
    { text: '甲段\n\n乙段' },
    { text: '乙段\n\n甲段', as_voice: true },
  ] })
  expect(r.synth).toEqual([])
  expect(r.seq).toEqual(['文:甲段', '文:乙段'])
}, T)

test('行为11 访问配置里没有 voiceId 时 as_voice=true：只发文字，不调语音桥', async () => {
  const r = await run({ voiceId: false, replies: [{ text: '文一\n\n文二', as_voice: true }] })
  expect(r.synth).toEqual([])
  expect(r.seq).toEqual(['文:文一', '文:文二'])
}, T)

test('行为12 单段文字 as_voice=true：1 条文字 + 1 条语音，合成文本就是这一段', async () => {
  const r = await run({ replies: [{ text: '只有一段', as_voice: true }] })
  expect(r.seq).toEqual(['文:只有一段', '音'])
  expect(r.synth).toEqual(['只有一段'])
}, T)
