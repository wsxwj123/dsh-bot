// voicecall-dsh 网关这一层的端到端验收（黑盒）。
// 依据只有 .devflow/BRIEF-voicecall-dsh.md 与 .devflow/INTERFACE-voicecall-dsh.md（下称 INTERFACE）。
//
// INTERFACE 只定义了适配服务的 notify 入口（③.3），没写适配服务转给网关时用哪个路径、什么请求体，
// 所以这里不直接打网关，而是走真实链路：真网关子进程 + 假 Telegram + 假模型（fake-acp），
// 再起一个真的适配服务子进程（python -m moments.adapter_service，端口经 DSH_CONSOLE_PORT 指到空闲端口），
// 让它读同一个 DSH_BOT_HOME 下网关写出的 state/api.key、state/api.port，经它的 notify 投 voice_call。
// 一切路径都在临时目录；launchctl / bun 两个接缝指向毒丸。
import { describe, expect, test } from 'bun:test'
import type { Subprocess } from 'bun'
import { Database } from 'bun:sqlite'
import { chmodSync, existsSync, lstatSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'fs'
import { join } from 'path'
import { type BotEnv, type Ctx, OWNER, REPO_DIR, ledgerPath, prompts, q, sleep, until, warmUp, withBot } from './_acc'

const PY = process.env.PYTHON ?? 'python3'
const ADAPTER_TOKEN = 'FAKE-ADAPTER-TOKEN-voicecall-e2e-do-not-leak-1f2e3d'
const VOICE_TEXT = '[voice-recap]（这是刚才哥哥和你打电话说的内容，记住即可，不用回复他）\n哥哥在电话里说了：\n· 明天要去⟦医院⟧复查\n· 想吃糖醋排骨🍖'
const VOICE_KEY = 'voice-call:e2e-call-0001'
/** 电话内容里最有辨识度的一段（不含 ⟦⟧，换不换括号都能搜到） */
const NEEDLE = '想吃糖醋排骨🍖'
const SWAPPED = '明天要去〚医院〛复查'
const REJECTED = '这只 bot 的进程拒收了这条通知：它配置里主人的聊天号不在它的允许名单里'
const H72 = 72 * 3600 * 1000
const QUIET_MS = 3_000
const T = 120_000

// ─── 适配服务子进程 ───

function freePort(): number {
  const s = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch: () => new Response('') })
  const p = s.port
  s.stop(true)
  return p
}

function poison(dir: string, name: string): string {
  mkdirSync(dir, { recursive: true })
  const p = join(dir, name)
  writeFileSync(p, '#!/bin/sh\nexit 97\n')
  chmodSync(p, 0o755)
  return p
}

class Adapter {
  proc: Subprocess | null = null
  port = 0
  stderr = ''
  constructor(readonly b: BotEnv) {}

  async start(): Promise<void> {
    const root = this.b.root
    const tokenFile = join(root, 'console-token', 'adapter.token')
    mkdirSync(join(root, 'console-token'), { recursive: true })
    writeFileSync(tokenFile, ADAPTER_TOKEN)
    chmodSync(tokenFile, 0o600)
    for (const d of ['configs-legacy', 'channels-legacy', 'LaunchAgents']) mkdirSync(join(root, d), { recursive: true })
    this.port = freePort()
    const p = this.proc = Bun.spawn([PY, '-m', 'moments.adapter_service'], {
      cwd: REPO_DIR,
      env: {
        PATH: process.env.PATH ?? '', HOME: this.b.fakeHome, USERPROFILE: this.b.fakeHome,
        DSH_BOT_HOME: root,
        HUB_CONFIGS_DSH_DIR: root,                    // 网关读的就是 <root>/<bot>.yml，适配服务读同一份
        HUB_CONFIGS_DIR: join(root, 'configs-legacy'),
        HUB_CHANNELS_DIR: join(root, 'channels-legacy'),
        HUB_LAUNCHAGENTS_DIR: join(root, 'LaunchAgents'),
        HUB_LAUNCHCTL: poison(join(root, 'poison'), 'launchctl'),
        HUB_BUN: poison(join(root, 'poison'), 'bun'),
        HUB_TELEGRAM_API_BASE: 'http://127.0.0.1:1',
        BOTLIFE_STATE_DB: join(root, 'botlife-state.db'),
        DSH_CONSOLE_TOKEN_FILE: tokenFile,
        DSH_CONSOLE_HOST: '127.0.0.1',
        DSH_CONSOLE_PORT: String(this.port),
        PYTHONDONTWRITEBYTECODE: '1',
      },
      stdout: 'ignore',
      stderr: 'pipe',
    })
    void (async () => { for await (const c of p.stderr as ReadableStream<Uint8Array>) this.stderr += new TextDecoder().decode(c) })()
    const end = Date.now() + 20_000
    while (Date.now() < end) {
      if (p.exitCode !== null) throw new Error(`适配服务启动就退出了（${p.exitCode}）：${this.stderr.slice(-800)}`)
      try {
        const r = await fetch(`http://127.0.0.1:${this.port}/adapter/v1/meta`, { headers: { authorization: `Bearer ${ADAPTER_TOKEN}` } })
        if (r.status === 200) return
      } catch {}
      await sleep(100)
    }
    throw new Error(`适配服务 20 秒内没在 127.0.0.1:${this.port} 上应答：${this.stderr.slice(-800)}`)
  }

  async notify(body: Record<string, unknown>): Promise<{ status: number; json: any }> {
    const r = await fetch(`http://127.0.0.1:${this.port}/adapter/v1/bots/${this.b.name}/notify`, {
      method: 'POST', headers: { authorization: `Bearer ${ADAPTER_TOKEN}`, 'content-type': 'application/json' }, body: JSON.stringify(body),
    })
    const text = await r.text()
    let json: any = null
    try { json = JSON.parse(text) } catch {}
    return { status: r.status, json }
  }

  voice(key = VOICE_KEY, text = VOICE_TEXT) { return this.notify({ source: 'voice_call', text, key }) }

  async stop(): Promise<void> {
    if (!this.proc) return
    this.proc.kill('SIGTERM')
    const t = setTimeout(() => this.proc?.kill('SIGKILL'), 5_000)
    await this.proc.exited
    clearTimeout(t)
    this.proc = null
  }
}

/** 在 bot 自己的 yml 顶层写主人私聊编号（INTERFACE ③.1 第 1 条：取 yml 顶层 chat_id） */
function setChatId(b: BotEnv, chatId: number): void {
  const cfg = JSON.parse(readFileSync(b.configPath, 'utf8'))
  cfg.chat_id = chatId
  writeFileSync(b.configPath, JSON.stringify(cfg, null, 1))
}

type VCtx = Ctx & { ad: Adapter }

async function withVoice(o: { chatId?: number }, fn: (c: VCtx) => Promise<void>): Promise<void> {
  await withBot({ before: b => setChatId(b, o.chatId ?? OWNER) }, async c => {
    // 适配服务靠这两个文件找网关（INTERFACE ③.3 失败表第 7 行）
    for (const f of ['api.key', 'api.port']) {
      const p = join(c.b.botDir, 'state', f)
      await until(() => existsSync(p), `网关写出 state/${f}`, 10_000).catch(() => { throw new Error(`网关启动后没有 state/${f}，适配服务找不到它`) })
    }
    const ad = new Adapter(c.b)
    try {
      await ad.start()
      await fn({ ...c, ad })
    } finally {
      await ad.stop()
    }
  })
}

// ─── 观察 ───

/** 假 Telegram 收到的、会让主人看到东西的请求（发消息、发语音、"正在输入"、表情回应等） */
const visibleCalls = (c: Ctx, since: number) =>
  c.tg.calls.slice(since).filter(x => x.method.startsWith('send') || x.method === 'setMessageReaction' || x.method.startsWith('edit'))

const voiceRows = (b: BotEnv) => q<Record<string, any>>(b, 'SELECT * FROM inbound WHERE text LIKE ?', `%糖醋排骨%`)
const userRowCount = (b: BotEnv) => q<{ n: number }>(b, "SELECT COUNT(*) AS n FROM inbound WHERE kind = 'user'")[0]?.n ?? 0

/** 一个目录下全部文件的「相对路径 → 内容 + 修改时间」（私聊"刚聊过"标记用） */
function snapshot(dir: string): Record<string, string> {
  const out: Record<string, string> = {}
  const walk = (d: string) => {
    let names: string[] = []
    try { names = readdirSync(d) } catch { return }
    for (const n of names) {
      const f = join(d, n)
      const st = lstatSync(f)
      if (st.isDirectory()) walk(f)
      else out[f.slice(dir.length)] = `${st.mtimeMs}|${readFileSync(f, 'utf8')}`
    }
  }
  walk(dir)
  return out
}

/** 主人发一句，等假模型收到含这句话的那一轮，返回那一轮交给模型的全文 */
async function ownerSays(c: Ctx, text: string): Promise<string> {
  const k = prompts(c.b).length
  c.tg.pushText(OWNER, text)
  const p = await until(() => prompts(c.b).slice(k).find(x => x.text.includes(text)), `假模型收到含「${text}」的一轮`, 30_000)
  return p.text
}

const DELIVERED = { ok: true, status: 'delivered' }
const DUPLICATE = { ok: true, status: 'duplicate' }

describe('voice_call：网关只记住、不主动回', () => {
  test('收下后：不开一轮、不发任何消息、不算主人说过话、刚聊过标记不变', async () => {
    await withVoice({}, async c => {
      await warmUp(c.tg)
      await sleep(500)
      const calls0 = c.tg.calls.length
      const prompts0 = prompts(c.b).length
      const users0 = userRowCount(c.b)
      const markerDir = String(c.b.gw.last_user_dir)
      const marker0 = snapshot(markerDir)

      const r = await c.ad.voice()
      expect(r.json).toEqual(DELIVERED)
      expect(r.status).toBe(200)
      await sleep(QUIET_MS)

      expect(visibleCalls(c, calls0)).toEqual([])                 // 主人在 Telegram 上什么都看不到
      expect(prompts(c.b).length).toBe(prompts0)                   // 假模型没被叫起来跑一轮
      expect(userRowCount(c.b)).toBe(users0)                       // 账本里"主人说的话"没多
      const rows = voiceRows(c.b)
      expect(rows.length).toBe(1)                                  // 电话记录确实收下了一条
      expect(rows[0]!.kind).toBe('synthetic')                      // ③.1 第 4 条：电话回写是程序合成消息
      expect(String(rows[0]!.chat_id)).toBe(String(OWNER))         // 投进主人私聊
      expect(snapshot(markerDir)).toEqual(marker0)                 // 导演用的私聊"刚聊过"标记没动
    })
  }, T)

  test('主人下一次发消息：那一轮交给模型的内容里带着电话块，⟦⟧ 已换成 〚〛', async () => {
    await withVoice({}, async c => {
      await warmUp(c.tg)
      expect((await c.ad.voice()).json).toEqual(DELIVERED)
      await sleep(500)
      const text = await ownerSays(c, '我到家了')
      expect(text).toContain(NEEDLE)
      expect(text).toContain(SWAPPED)
      expect(text).not.toContain('⟦医院⟧')
    })
  }, T)

  test('同一个 key 投两次：第二次 duplicate，账本只一条，模型只看到一遍', async () => {
    await withVoice({}, async c => {
      await warmUp(c.tg)
      expect((await c.ad.voice()).json).toEqual(DELIVERED)
      expect((await c.ad.voice()).json).toEqual(DUPLICATE)
      await sleep(500)
      expect(voiceRows(c.b).length).toBe(1)
      const text = await ownerSays(c, '刚挂电话')
      expect(text.split(NEEDLE).length - 1).toBe(1)
    })
  }, T)

  test('同一个 key 同时投两次（壳超时重试撞上第一次还在路上）：一条 delivered 一条 duplicate，账本只一条', async () => {
    await withVoice({}, async c => {
      await warmUp(c.tg)
      const rs = await Promise.all([c.ad.voice(), c.ad.voice()])
      expect(rs.map(r => r.json?.status).sort()).toEqual(['delivered', 'duplicate'])
      await sleep(500)
      expect(voiceRows(c.b).length).toBe(1)
    })
  }, T)

  test('收下后网关重启：重启本身不开一轮，主人再发消息时电话块照样带上', async () => {
    await withVoice({}, async c => {
      await warmUp(c.tg)
      expect((await c.ad.voice()).json).toEqual(DELIVERED)
      await sleep(500)
      await c.gw.stop()
      const calls0 = c.tg.calls.length
      const prompts0 = prompts(c.b).length
      await c.gw.start()
      await sleep(QUIET_MS)
      expect(visibleCalls(c, calls0)).toEqual([])
      expect(prompts(c.b).length).toBe(prompts0)
      const text = await ownerSays(c, '我回来了')
      expect(text).toContain(NEEDLE)
    })
  }, T)

  test('收下超过 72 小时才有下一轮：那一轮不再带电话块', async () => {
    await withVoice({}, async c => {
      await warmUp(c.tg)
      expect((await c.ad.voice()).json).toEqual(DELIVERED)
      await sleep(500)
      await c.gw.stop()
      // 造数：网关停着时把这条电话记录的消息时间与收下时间都往前拨 73 小时（只动 ledger-schema.sql 里的列）
      const db = new Database(ledgerPath(c.b))
      try {
        const res = db.query("UPDATE inbound SET ts = ts - ?1, received_at = received_at - ?1 WHERE text LIKE '%糖醋排骨%'").run(H72 + 3600_000)
        expect(res.changes).toBe(1)
      } finally { db.close() }
      await c.gw.start()
      const text = await ownerSays(c, '好几天没聊了')
      expect(text).not.toContain(NEEDLE)
      expect(text).not.toContain('医院')
    })
  }, T)

  test('主人聊天号不在网关允许名单里：适配服务回 500 拒收，账本不多、什么都不发', async () => {
    await withVoice({ chatId: 5550009 }, async c => {
      const calls0 = c.tg.calls.length
      const r = await c.ad.voice()
      expect(r.status).toBe(500)
      expect(r.json).toEqual({ ok: false, code: 'internal', message: REJECTED })
      await sleep(1_000)
      expect(voiceRows(c.b).length).toBe(0)
      expect(visibleCalls(c, calls0)).toEqual([])
      expect(prompts(c.b).length).toBe(0)
    })
  }, T)
})
