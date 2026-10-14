// 私聊标记改写中立目录（INTERFACE-适配服务 10.5）：<目录>/<bot_id>，内容是整数的 Unix 秒，先写临时文件再改名；
// 目录默认 ~/.config/claude-dsh-console/shared/last-user（这里只算不建），bot 配置 gateway.last_user_dir 可改；目录 0700、文件 0600
import { afterEach, expect, test } from 'bun:test'
import { mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'fs'
import { homedir, tmpdir } from 'os'
import { join } from 'path'
import { loadBotConfig } from '../../src/config'
import { writeLastUser } from '../../src/util'

const dirs: string[] = []
afterEach(() => { for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true }) })

test('私聊标记：默认目录与配置改目录；写 <目录>/<bot_id> 整数秒，再写覆盖，目录 0700、文件 0600、不留临时文件', () => {
  const root = mkdtempSync(join(tmpdir(), 'last-user-'))
  dirs.push(root)
  const ch = join(root, 'ch')
  mkdirSync(ch)
  const yml = join(root, 'bot5.yml')
  const env = { DSH_BOT_HOME: join(root, 'home') }
  writeFileSync(yml, `id: bot5\nbot_channel_path: ${ch}\n`)
  expect(loadBotConfig(yml, env).gw.lastUserDir).toBe(join(homedir(), '.config', 'claude-dsh-console', 'shared', 'last-user'))
  const dir = join(root, 'shared', 'last-user')
  writeFileSync(yml, `id: bot5\nbot_channel_path: ${ch}\ngateway:\n  last_user_dir: ${dir}\n`)
  expect(loadBotConfig(yml, env).gw.lastUserDir).toBe(dir)

  writeLastUser(dir, 'bot5', 1_760_000_000_999)
  expect(readFileSync(join(dir, 'bot5'), 'utf8')).toBe('1760000000')
  writeLastUser(dir, 'bot5', 1_760_000_123_456)
  expect(readFileSync(join(dir, 'bot5'), 'utf8')).toBe('1760000123')
  expect(readdirSync(dir)).toEqual(['bot5'])
  if (process.platform !== 'win32') {
    expect(statSync(dir).mode & 0o777).toBe(0o700)
    expect(statSync(join(dir, 'bot5')).mode & 0o777).toBe(0o600)
  }
})
