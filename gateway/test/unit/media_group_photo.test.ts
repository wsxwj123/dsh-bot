// fetchGroupPhoto 的安全校验分支：验收层（假 Telegram）永远给合法形状，这里用假 api 逐个打。
import { afterEach, expect, test } from 'bun:test'
import { mkdtempSync, readFileSync, rmSync, statSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { fetchGroupPhoto, MAX_SHARED_IMAGE_BYTES } from '../../src/media/media'
import { TgApiError } from '../../src/telegram/api'
import type { TgMessage } from '../../src/telegram/api'

const dirs: string[] = []
afterEach(() => { for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true }) })
function tmp(): string { const d = mkdtempSync(join(tmpdir(), 'gp-')); dirs.push(d); return d }

/** 记录调用的假 api：按需返回 file_size / 字节 / 抛错 */
function fakeApi(o: { fileSize?: number; bytes?: Uint8Array; downloadError?: unknown } = {}) {
  const calls: string[] = []
  return {
    calls,
    api: {
      getFile: async (fileId: string) => { calls.push(`getFile:${fileId}`); return { file_path: 'photos/x.jpg', file_size: o.fileSize } },
      download: async (p: string, max?: number) => {
        calls.push(`download:${p}:${max}`)
        if (o.downloadError) throw o.downloadError
        return o.bytes ?? new Uint8Array([1, 2, 3])
      },
    } as never,
  }
}

function msg(o: Partial<TgMessage> & { photos?: { file_id: string; width?: number; height?: number; file_size?: number }[] } = {}): TgMessage {
  return {
    message_id: 7, date: 0, chat: { id: -1001777, type: 'supergroup' }, from: { id: 42 },
    ...(o.photos ? { photo: o.photos } : { photo: [{ file_id: 'p1', width: 800, height: 600 }] }),
    ...o,
  } as TgMessage
}

test('正常：挑最大图、下载、按 <chat>/<message>.jpg 落盘，返回 ok', async () => {
  const dir = tmp()
  const { api, calls } = fakeApi({ bytes: new Uint8Array([9, 9, 9]) })
  const r = await fetchGroupPhoto({ api, msg: msg(), dir })
  expect(r.ok).toBe(true)
  if (!r.ok) return
  expect(r.path).toBe(join(dir, '-1001777', '7.jpg'))
  expect([...readFileSync(r.path)]).toEqual([9, 9, 9])
  if (process.platform !== 'win32') expect(statSync(r.path).mode & 0o777).toBe(0o600)
  expect(calls).toEqual(['getFile:p1', `download:photos/x.jpg:${MAX_SHARED_IMAGE_BYTES}`])
})

test('多图挑面积最大的那张（不依赖数组顺序）', async () => {
  const dir = tmp()
  const { api, calls } = fakeApi()
  const r = await fetchGroupPhoto({
    api, dir,
    msg: msg({ photos: [{ file_id: 'small', width: 90, height: 90 }, { file_id: 'big', width: 1200, height: 900 }, { file_id: 'mid', width: 800, height: 600 }] }),
  })
  expect(r.ok).toBe(true)
  expect(calls[0]).toBe('getFile:big')
})

test('消息里带的 file_size 超 8 MiB：连 getFile 都不发', async () => {
  const dir = tmp()
  const { api, calls } = fakeApi()
  const r = await fetchGroupPhoto({ api, dir, msg: msg({ photos: [{ file_id: 'huge', width: 800, height: 600, file_size: MAX_SHARED_IMAGE_BYTES + 1 }] }) })
  expect(r).toEqual({ ok: false, reason: 'too_large' })
  expect(calls).toEqual([])
})

test('getFile 返回的 file_size 超 8 MiB：不调 download', async () => {
  const dir = tmp()
  const { api, calls } = fakeApi({ fileSize: MAX_SHARED_IMAGE_BYTES + 1 })
  const r = await fetchGroupPhoto({ api, msg: msg(), dir })
  expect(r).toEqual({ ok: false, reason: 'too_large' })
  expect(calls).toEqual(['getFile:p1'])
})

test('download 抛 413：归为 too_large，不落盘', async () => {
  const dir = tmp()
  const { api } = fakeApi({ downloadError: new TgApiError('download', 413, 'file too large') })
  const r = await fetchGroupPhoto({ api, msg: msg(), dir })
  expect(r).toEqual({ ok: false, reason: 'too_large' })
})

test('download 抛其它错误：download_failed', async () => {
  const dir = tmp()
  const { api } = fakeApi({ downloadError: new Error('boom') })
  const r = await fetchGroupPhoto({ api, msg: msg(), dir })
  expect(r).toEqual({ ok: false, reason: 'download_failed' })
})

test('下载到 0 字节：empty', async () => {
  const dir = tmp()
  const { api } = fakeApi({ bytes: new Uint8Array() })
  const r = await fetchGroupPhoto({ api, msg: msg(), dir })
  expect(r).toEqual({ ok: false, reason: 'empty' })
})

test('file_id 空串 / 超 256 / 非字符串：形状不合格', async () => {
  const dir = tmp()
  const { api, calls } = fakeApi()
  for (const photos of [
    [{ file_id: '', width: 800, height: 600 }],
    [{ file_id: 'x'.repeat(257), width: 800, height: 600 }],
    [{ file_id: 123 as unknown as string, width: 800, height: 600 }],
    [{ width: 800, height: 600 } as unknown as { file_id: string }],
  ]) {
    const r = await fetchGroupPhoto({ api, dir, msg: msg({ photos }) })
    expect(r).toEqual({ ok: false, reason: 'bad_photo_shape' })
  }
  expect(calls).toEqual([])
})

test('chat.id 为 0 或非安全整数、message_id <= 0：拒绝', async () => {
  const dir = tmp()
  const { api, calls } = fakeApi()
  for (const m of [
    msg({ chat: { id: 0, type: 'supergroup' } as TgMessage['chat'] }),
    msg({ chat: { id: 1.5, type: 'supergroup' } as TgMessage['chat'] }),
    msg({ message_id: 0 }),
    msg({ message_id: -3 }),
  ]) {
    const r = await fetchGroupPhoto({ api, msg: m, dir })
    expect(r).toEqual({ ok: false, reason: 'bad_photo_shape' })
  }
  expect(calls).toEqual([])
})

test('负数群号保留负号作目录名', async () => {
  const dir = tmp()
  const { api } = fakeApi()
  const r = await fetchGroupPhoto({ api, dir, msg: msg({ chat: { id: -1001234567890, type: 'group' } as TgMessage['chat'] }) })
  expect(r.ok).toBe(true)
  if (r.ok) expect(r.path).toBe(join(dir, '-1001234567890', '7.jpg'))
})
