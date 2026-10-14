// 网关带模型（INTERFACE-适配服务 10.4，约定 v0.8 B.3）：调语音桥 /synthesize_voice 时，
// 这只 bot 的 access.json 里 voiceModel 是非空字符串就在请求体里带 model；没有就不带，由语音桥按全局设置
import { afterEach, expect, test } from 'bun:test'
import { mkdtempSync, rmSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { Media, type MediaDeps } from '../../src/media/media'

const realFetch = globalThis.fetch
const dirs: string[] = []
afterEach(() => {
  globalThis.fetch = realFetch
  for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true })
})

test('synthesize：给了非空的 model 才放进请求体；没给、给空串都不带 model 这个键', async () => {
  const mediaDir = mkdtempSync(join(tmpdir(), 'voice-model-'))
  dirs.push(mediaDir)
  const sent: { url: string; body: Record<string, unknown> }[] = []
  globalThis.fetch = (async (url: string | URL | Request, init?: RequestInit) => {
    sent.push({ url: String(url), body: JSON.parse(String(init?.body)) })
    return new Response(new Uint8Array([79, 103, 103, 83]))
  }) as typeof fetch
  const warns: string[] = []
  const media = new Media({
    mediaDir, bridgeUrl: 'http://127.0.0.1:9', bridgeToken: () => null, sharedDirs: () => [],
    log: { warn: (ev: string) => warns.push(ev), info: () => {} },
  } as unknown as MediaDeps)
  expect(await media.synthesize('你好', 'voice_a', 'HAPPY', '', 's2-pro')).not.toBeNull()
  expect(await media.synthesize('你好', 'voice_a')).not.toBeNull()
  expect(await media.synthesize('你好', 'voice_a', undefined, undefined, '')).not.toBeNull()
  expect(sent.map(s => s.url)).toEqual(Array(3).fill('http://127.0.0.1:9/synthesize_voice'))
  expect(sent.map(s => s.body)).toEqual([
    { text: '你好', voice_id: 'voice_a', emotion: 'HAPPY', instruct: '', model: 's2-pro' },
    { text: '你好', voice_id: 'voice_a', emotion: 'NEUTRAL', instruct: '' },
    { text: '你好', voice_id: 'voice_a', emotion: 'NEUTRAL', instruct: '' },
  ])
  expect(warns).toEqual([])
})
