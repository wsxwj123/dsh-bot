// noimages-permanent-disable 接口（.devflow/noimages-permanent-disable-接口.md）的黑盒用例。
// 只经实现方导出的公开判定函数与状态类调用，不碰 Engine 内部。
import { expect, test } from 'bun:test'
import { mkdtempSync, readFileSync, rmSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { ImagesUnsupportedTracker, isImagesUnsupportedError } from '../../src/engine/image_support'
import { Logger } from '../../src/log'

const KEY = 'anthropic:claude-x:high'
const SECOND_30MIN = 30 * 60 * 1000

function readLogLines(dir: string): Array<Record<string, unknown>> {
  const raw = readFileSync(join(dir, 'gateway.log'), 'utf8').trim()
  return raw.split('\n').filter(Boolean).map((line) => JSON.parse(line) as Record<string, unknown>)
}

test('错误分类：六种明确"模型不接受图片"的文案都算，大小写不敏感、可出现在任意位置', () => {
  const messages = [
    'Error: UNSUPPORTED_CONTENT',
    'upstream says this model does not support image input',
    'Image input not supported by provider',
    'Images are NOT supported with this model',
    '上游报错：该模型不支持图片',
    '错误: 模型不支持图像输入',
  ]
  for (const message of messages) expect(isImagesUnsupportedError(message)).toBe(true)
})

test('错误分类：图片偶发错误不算（过大、格式、加载失败、上传失败、单独的 attachment）', () => {
  const messages = [
    'image too large',
    'Image format is invalid',
    'failed to load image',
    'image upload failed',
    'attachment is missing',
    'image processing error',
  ]
  for (const message of messages) expect(isImagesUnsupportedError(message)).toBe(false)
})

test('判定口径：只有"不支持"语义与"图片"语义同时出现才算，缺一半或空消息都不算', () => {
  expect(isImagesUnsupportedError('model does not support tools')).toBe(false)
  expect(isImagesUnsupportedError('attachment is not supported')).toBe(false)
  expect(isImagesUnsupportedError('image decode error')).toBe(false)
  expect(isImagesUnsupportedError('')).toBe(false)
})

test('降级：第一次判定不支持，本轮返回降级信号但不禁用，下一轮仍然带图', () => {
  const tracker = new ImagesUnsupportedTracker({ now: () => 1_000_000 })
  expect(tracker.onImageFailure(KEY, 'model does not support image input')).toEqual({ unsupported: true, disabled: false })
  expect(tracker.shouldAttachImages(KEY)).toBe(true)
})

test('禁用：连续两次同类失败后不再附图，第二次文案不同也算同类', () => {
  const tracker = new ImagesUnsupportedTracker({ now: () => 1_000_000 })
  expect(tracker.onImageFailure(KEY, 'UNSUPPORTED_CONTENT')).toEqual({ unsupported: true, disabled: false })
  expect(tracker.onImageFailure(KEY, '该模型不支持图片')).toEqual({ unsupported: true, disabled: true })
  expect(tracker.shouldAttachImages(KEY)).toBe(false)
})

test('30 分钟过期：禁用后 29 分 59 秒仍不带图，满 30 分钟恢复尝试带图', () => {
  let now = 1_000_000
  const tracker = new ImagesUnsupportedTracker({ now: () => now })
  tracker.onImageFailure(KEY, 'UNSUPPORTED_CONTENT')
  tracker.onImageFailure(KEY, 'UNSUPPORTED_CONTENT')

  now += SECOND_30MIN - 1000
  expect(tracker.shouldAttachImages(KEY)).toBe(false)

  now += 1000
  expect(tracker.shouldAttachImages(KEY)).toBe(true)
})

test('换模型：禁用状态立即清空，换回原模型也重新尝试附图且重新计数', () => {
  const tracker = new ImagesUnsupportedTracker({ now: () => 1_000_000 })
  tracker.onImageFailure(KEY, 'UNSUPPORTED_CONTENT')
  tracker.onImageFailure(KEY, 'UNSUPPORTED_CONTENT')
  expect(tracker.shouldAttachImages(KEY)).toBe(false)

  expect(tracker.shouldAttachImages('openai:gpt-y:low')).toBe(true)
  expect(tracker.shouldAttachImages(KEY)).toBe(true)
  expect(tracker.onImageFailure(KEY, 'UNSUPPORTED_CONTENT')).toEqual({ unsupported: true, disabled: false })
})

test('普通图片错误：连续两次 image too large 不改视觉状态、不禁用、不打禁用 warn', () => {
  const dir = mkdtempSync(join(tmpdir(), 'noimg-'))
  try {
    const logger = new Logger({ dir })
    const tracker = new ImagesUnsupportedTracker({ now: () => 1_000_000, logger })
    expect(tracker.onImageFailure(KEY, 'image too large')).toEqual({ unsupported: false, disabled: false })
    expect(tracker.onImageFailure(KEY, 'image too large')).toEqual({ unsupported: false, disabled: false })
    expect(tracker.shouldAttachImages(KEY)).toBe(true)
    const lines = readLogLines(dir)
    expect(lines.filter((l) => l.event === 'prompt.images_unsupported')).toHaveLength(0)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('连续计数：中间成功带图一次后清零，再失败一次不进入禁用', () => {
  const tracker = new ImagesUnsupportedTracker({ now: () => 1_000_000 })
  tracker.onImageFailure(KEY, 'UNSUPPORTED_CONTENT')
  tracker.onImageSuccess(KEY)
  expect(tracker.onImageFailure(KEY, 'UNSUPPORTED_CONTENT')).toEqual({ unsupported: true, disabled: false })
  expect(tracker.shouldAttachImages(KEY)).toBe(true)
})

test('日志：首次判定不支持打一条 info prompt.images_retry，含 model 与 err，且不打禁用 warn', () => {
  const dir = mkdtempSync(join(tmpdir(), 'noimg-'))
  try {
    const logger = new Logger({ dir })
    const tracker = new ImagesUnsupportedTracker({ now: () => 1_000_000, logger })
    tracker.onImageFailure(KEY, 'upstream says model does not support image input')

    const lines = readLogLines(dir)
    const infos = lines.filter((l) => l.event === 'prompt.images_retry')
    expect(infos).toHaveLength(1)
    expect(infos[0]!.level).toBe('info')
    expect(infos[0]!.model).toBe(KEY)
    expect(typeof infos[0]!.err).toBe('string')
    expect(infos[0]!.err).toContain('does not support image input')
    expect(lines.filter((l) => l.event === 'prompt.images_unsupported')).toHaveLength(0)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('日志：进入禁用状态打一条 warn prompt.images_unsupported，字段含 model，info 只打首次', () => {
  const dir = mkdtempSync(join(tmpdir(), 'noimg-'))
  try {
    const logger = new Logger({ dir })
    const tracker = new ImagesUnsupportedTracker({ now: () => 1_000_000, logger })
    tracker.onImageFailure(KEY, 'UNSUPPORTED_CONTENT')
    tracker.onImageFailure(KEY, 'image input not supported')

    const lines = readLogLines(dir)
    const warns = lines.filter((l) => l.event === 'prompt.images_unsupported')
    expect(warns).toHaveLength(1)
    expect(warns[0]!.level).toBe('warn')
    expect(warns[0]!.model).toBe(KEY)
    expect(lines.filter((l) => l.event === 'prompt.images_retry')).toHaveLength(1)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})
