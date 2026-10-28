// readSharedImage 的 maxSide 分支（近况附图的像素边长上限）：必须真的执行到这段安全分支。
// 夹具是用 PIL 生成的 1 像素高 PNG（纯色，字节很小），base64 硬编码，不依赖磁盘和第三方库。
// 同时验证边界不误伤（2048 放行）、不传 maxSide 时老行为不变、解析不出尺寸时 fail closed。
import { expect, test } from 'bun:test'
import { mkdtempSync, realpathSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { readSharedImage } from '../../src/media/media'

/** 2049×1 PNG，PIL 生成 */
const PNG_2049X1 = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAACAEAAAABCAIAAAAqtLKbAAAAIUlEQVR4nO3BQREAAAgDIP2uf6jFsocHbJMBAAAAAADeOdOqAN5ReswyAAAAAElFTkSuQmCC',
  'base64',
)

/** 2048×1 PNG（恰好等于上限），PIL 生成 */
const PNG_2048X1 = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAACAAAAAABCAIAAADFdtmlAAAAIUlEQVR4nO3BQREAAAgDIP2uf6jFsocHbJMBAAAAAAB+OdEQAN4qpm6RAAAAAElFTkSuQmCC',
  'base64',
)

/** 256×256 PNG，PIL 生成 */
const PNG_256X256 = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAQAAAAEACAIAAADTED8xAAACv0lEQVR4nO3TMQEAIAzAMOCcf1HIQgZHEwV9uu/MgqrzOwB+MgBpBiDNAKQZgDQDkGYA0gxAmgFIMwBpBiDNAKQZgDQDkGYA0gxAmgFIMwBpBiDNAKQZgDQDkGYA0gxAmgFIMwBpBiDNAKQZgDQDkGYA0gxAmgFIMwBpBiDNAKQZgDQDkGYA0gxAmgFIMwBpBiDNAKQZgDQDkGYA0gxAmgFIMwBpBiDNAKQZgDQDkGYA0gxAmgFIMwBpBiDNAKQZgDQDkGYA0gxAmgFIMwBpBiDNAKQZgDQDkGYA0gxAmgFIMwBpBiDNAKQZgDQDkGYA0gxAmgFIMwBpBiDNAKQZgDQDkGYA0gxAmgFIMwBpBiDNAKQZgDQDkGYA0gxAmgFIMwBpBiDNAKQZgDQDkGYA0gxAmgFIMwBpBiDNAKQZgDQDkGYA0gxAmgFIMwBpBiDNAKQZgDQDkGYA0gxAmgFIMwBpBiDNAKQZgDQDkGYA0gxAmgFIMwBpBiDNAKQZgDQDkGYA0gxAmgFIMwBpBiDNAKQZgDQDkGYA0gxAmgFIMwBpBiDNAKQZgDQDkGYA0gxAmgFIMwBpBiDNAKQZgDQDkGYA0gxAmgFIMwBpBiDNAKQZgDQDkGYA0gxAmgFIMwBpBiDNAKQZgDQDkGYA0gxAmgFIMwBpBiDNAKQZgDQDkGYA0h4v/QLcoPCl0QAAAABJRU5ErkJggg==',
  'base64',
)

/** 建一个临时白名单目录，写入文件后执行断言，结束即清理 */
function withTempFile(name: string, bytes: Buffer, fn: (path: string, dir: string) => void): void {
  const dir = realpathSync(mkdtempSync(join(tmpdir(), 'dshmaxside-')))
  try {
    const path = join(dir, name)
    writeFileSync(path, bytes)
    fn(path, dir)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
}

test('2049×1 PNG 在 maxSide=2048 下 → too_many_pixels', () => {
  withTempFile('wide.png', PNG_2049X1, (path, dir) => {
    expect(readSharedImage(path, [dir], undefined, { maxSide: 2048 })).toEqual({ ok: false, reason: 'too_many_pixels' })
  })
})

test('2048×1 PNG 在 maxSide=2048 下 → 放行（边界不误伤）', () => {
  withTempFile('edge.png', PNG_2048X1, (path, dir) => {
    expect(readSharedImage(path, [dir], undefined, { maxSide: 2048 }).ok).toBe(true)
  })
})

test('256×256 PNG 不传 maxSide → 放行（老行为不变）', () => {
  withTempFile('square.png', PNG_256X256, (path, dir) => {
    expect(readSharedImage(path, [dir]).ok).toBe(true)
  })
})

test('纯文本存成 .png 且传 maxSide → 解析不出尺寸，too_many_pixels（fail closed）', () => {
  withTempFile('fake.png', Buffer.from('这不是图片，只是一段纯文本。\nnot an image'), (path, dir) => {
    expect(readSharedImage(path, [dir], undefined, { maxSide: 2048 })).toEqual({ ok: false, reason: 'too_many_pixels' })
  })
})
