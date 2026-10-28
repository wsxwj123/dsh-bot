// imageDimensions：手写的 PNG / JPEG / WebP 文件头解析。
// 解析不出宽高返回 null，调用方按 fail closed 处理（too_many_pixels），该格式的图会永久看不到，
// 所以逐格式用真实图片字节验证，而不是只测 1x1 PNG。
// 夹具全部是硬编码的 base64（真实图片字节，由 PIL 生成后取出），不依赖磁盘、不装新依赖。
import { expect, test } from 'bun:test'
import { imageDimensions } from '../../src/media/media'

/** 1x1 透明 PNG（与 group_image_dm.test.ts 同一份真实字节） */
const PNG_1X1 = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
  'base64',
)

/** 3x2 baseline JPEG（JFIF APP0 → DQT → SOF0），PIL quality=80 生成 */
const JPEG_3X2 = Buffer.from(
  '/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAYEBQYFBAYGBQYHBwYIChAKCgkJChQODwwQFxQYGBcUFhYaHSUfGhsjHBYWICwgIyYnKSopGR8tMC0oMCUoKSj/2wBDAQcHBwoIChMKChMoGhYaKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCj/wAARCAACAAMDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDgKKKK+eP2E//Z',
  'base64',
)

/** 4x5 baseline JPEG，APP1(EXIF) 在 SOF0 之前，PIL quality=80 生成 */
const JPEG_EXIF_4X5 = Buffer.from(
  '/9j/4AAQSkZJRgABAQAAAQABAAD/4QAqRXhpZgAATU0AKgAAAAgAAQEOAAIAAAAIAAAAGgAAAABmaXh0dXJlAP/bAEMABgQFBgUEBgYFBgcHBggKEAoKCQkKFA4PDBAXFBgYFxQWFhodJR8aGyMcFhYgLCAjJicpKikZHy0wLSgwJSgpKP/bAEMBBwcHCggKEwoKEygaFhooKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKP/AABEIAAUABAMBIgACEQEDEQH/xAAfAAABBQEBAQEBAQAAAAAAAAAAAQIDBAUGBwgJCgv/xAC1EAACAQMDAgQDBQUEBAAAAX0BAgMABBEFEiExQQYTUWEHInEUMoGRoQgjQrHBFVLR8CQzYnKCCQoWFxgZGiUmJygpKjQ1Njc4OTpDREVGR0hJSlNUVVZXWFlaY2RlZmdoaWpzdHV2d3h5eoOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4eLj5OXm5+jp6vHy8/T19vf4+fr/xAAfAQADAQEBAQEBAQEBAAAAAAAAAQIDBAUGBwgJCgv/xAC1EQACAQIEBAMEBwUEBAABAncAAQIDEQQFITEGEkFRB2FxEyIygQgUQpGhscEJIzNS8BVictEKFiQ04SXxFxgZGiYnKCkqNTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqCg4SFhoeIiYqSk5SVlpeYmZqio6Slpqeoqaqys7S1tre4ubrCw8TFxsfIycrS09TV1tfY2dri4+Tl5ufo6ery8/T19vf4+fr/2gAMAwEAAhEDEQA/AH0UUV+fn5cf/9k=',
  'base64',
)

/** 6x3 有损 WebP（VP8），PIL quality=80 生成 */
const WEBP_VP8_6X3 = Buffer.from(
  'UklGRjgAAABXRUJQVlA4ICwAAACQAQCdASoGAAMAAUAmJaACdLoAA5gA/vsZy/+BO/+BO/+BO/8Cd9fAFPEgAA==',
  'base64',
)

/** 9x4 无损 WebP（VP8L），PIL lossless=True 生成 */
const WEBP_VP8L_9X4 = Buffer.from(
  'UklGRh4AAABXRUJQVlA4TBEAAAAvCMAAAAdQ5CpUof+BiOh/AAA=',
  'base64',
)

/** 7x5 扩展 WebP（VP8X，带 alpha），PIL quality=80 生成 */
const WEBP_VP8X_7X5 = Buffer.from(
  'UklGRmAAAABXRUJQVlA4WAoAAAAQAAAABgAABAAAQUxQSAoAAAABB1DAiAhERP8DVlA4IDAAAADQAQCdASoHAAUAAUAmJaACdLoB+AADsAD+9hNf/izkYjs83/4E78Cd+BO/8AAAAAA=',
  'base64',
)

test('1x1 PNG → 解析出 1x1', () => {
  expect(imageDimensions(PNG_1X1)).toEqual({ width: 1, height: 1 })
})

test('3x2 JPEG（SOF0 在 APP0 之后）→ 解析出 3x2', () => {
  expect(imageDimensions(JPEG_3X2)).toEqual({ width: 3, height: 2 })
})

test('带 EXIF 的 4x5 JPEG（APP1 在 SOF0 之前）→ 跳过 APP1 找到 SOF0', () => {
  // 先确认夹具确实是"APP1 在 SOF0 之前"，否则这条用例测不到跳过逻辑
  const app1 = JPEG_EXIF_4X5.indexOf(Buffer.from([0xff, 0xe1]))
  const sof0 = JPEG_EXIF_4X5.indexOf(Buffer.from([0xff, 0xc0]))
  expect(app1).toBeGreaterThan(1)
  expect(app1).toBeLessThan(sof0)
  expect(imageDimensions(JPEG_EXIF_4X5)).toEqual({ width: 4, height: 5 })
})

test('6x3 有损 WebP（VP8）→ 解析出 6x3', () => {
  expect(imageDimensions(WEBP_VP8_6X3)).toEqual({ width: 6, height: 3 })
})

test('9x4 无损 WebP（VP8L）→ 解析出 9x4', () => {
  expect(imageDimensions(WEBP_VP8L_9X4)).toEqual({ width: 9, height: 4 })
})

test('7x5 扩展 WebP（VP8X）→ 解析出 7x5', () => {
  expect(imageDimensions(WEBP_VP8X_7X5)).toEqual({ width: 7, height: 5 })
})

test('纯文本不是图片 → null', () => {
  expect(imageDimensions(Buffer.from('这不是图片，只是一段纯文本。\nnot an image'))).toBeNull()
})

test('截断的 PNG（只有 8 字节签名）→ 不抛异常，返回 null', () => {
  const signatureOnly = PNG_1X1.subarray(0, 8)
  expect(signatureOnly).toHaveLength(8)
  expect(() => imageDimensions(signatureOnly)).not.toThrow()
  expect(imageDimensions(signatureOnly)).toBeNull()
})
