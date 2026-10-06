// M4 真机问题：停网关时不再发后面的段；同一轮里同一个文件只发一次；语音段先文字后语音
import { expect, test } from 'bun:test'
import { mkdtempSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { Ledger } from '../../src/ledger'
import { Logger } from '../../src/log'
import type { TelegramApi } from '../../src/telegram/api'
import { Sender } from '../../src/telegram/sender'

function rig() {
  const dir = mkdtempSync(join(tmpdir(), 'sender-'))
  const calls: string[] = []
  let n = 0
  const api = {
    sendMessage: async (_c: string, text: string) => { calls.push(`text:${text}`); return { message_id: ++n } },
    sendFile: async (kind: string, _c: string, path: string) => { calls.push(`${kind}:${path.split(/[\\/]/).pop()}`); return { message_id: ++n } },
    sendChatAction: async () => true,
  } as unknown as TelegramApi
  const ledger = new Ledger(':memory:')
  const access = { allowFrom: [], groups: {}, splitOnParagraph: true, paragraphDelay: 0 } as any
  const sender = new Sender(api, ledger, new Logger(), { allowDirs: () => [dir], maxSendWaitMs: 1000, access: () => access })
  return { dir, calls, sender, api, done: () => { ledger.close(); rmSync(dir, { recursive: true, force: true }) } }
}

test('网关开始停止后，还没发的段不再发，结果里写明', async () => {
  const r = rig()
  const orig = r.api.sendMessage
  ;(r.api as any).sendMessage = async (c: string, t: string, o: any) => { const m = await orig(c, t, o); r.sender.halt(); return m }
  const res = await r.sender.send({ chatId: '1', turnId: null, callSeq: 1, text: '一\n\n二\n\n三' })
  expect(res.map(x => x.state)).toEqual(['sent', 'skipped', 'skipped'])
  expect(res[1]!.reason).toBe('网关正在停止')
  expect(r.calls).toEqual(['text:一'])
  r.done()
})

test('这一轮已经发过（或正在发）的文件不再发', async () => {
  const r = rig()
  const f = join(r.dir, 'a.png')
  writeFileSync(f, 'png')
  const skipFiles = new Set<string>()
  await r.sender.send({ chatId: '1', turnId: null, callSeq: 1, text: '看', files: [f], skipFiles })
  const res = await r.sender.send({ chatId: '1', turnId: null, callSeq: 2, text: '看看', files: [f], skipFiles })
  expect(res.map(x => x.state)).toEqual(['sent', 'duplicate'])
  expect(r.calls.filter(c => c.startsWith('photo:'))).toHaveLength(1)
  r.done()
})

test('语音：每段先发文字再发语音；朗读稿某段为空就只发文字；合成失败文字照样算送达', async () => {
  const r = rig()
  const voice = async (t: string) => (t === '坏' ? null : join(r.dir, `${t}.ogg`))
  const res = await r.sender.send({ chatId: '1', turnId: null, callSeq: 1, text: '甲\n\n乙\n\n丙', voice, voiceTexts: ['a', '', '坏'] })
  expect(res.map(x => x.state)).toEqual(['sent', 'sent', 'sent'])
  expect(r.calls).toEqual(['text:甲', 'voice:a.ogg', 'text:乙', 'text:丙'])
  r.done()
})
