// 网关入口：bun src/main.ts --config configs/<bot>.yml
import { chmodSync, existsSync, mkdirSync, readFileSync, rmSync } from 'fs'
import { homedir } from 'os'
import { join } from 'path'
import { ApiServer } from './api/server'
import { checkCredentialsFile, ConfigError, loadAccess, loadBotConfig, readTelegramToken, type BotConfig } from './config'
import { DshProcess, isAlive, processCommandLine } from './dsh/process'
import { Engine } from './engine/engine'
import { GroupTranscript } from './group/transcript'
import { Ledger } from './ledger'
import { Logger, registerSecret, safeError } from './log'
import { McpServer } from './mcp/server'
import { TelegramApi } from './telegram/api'
import { isGroupChat } from './telegram/inbound'
import { Poller } from './telegram/poller'
import { Sender } from './telegram/sender'
import { addNoProxyHosts, ensureSecretFile, sleep, writeAtomic } from './util'
import { fetchModels } from './providers/models'
import { ProviderService } from './providers/service'
import { ProviderCommands } from './providers/commands'
import { registerCredentialSecrets } from './providers/store'
import { Wizard } from './wizard/wizard'

function arg(name: string): string | undefined {
  const i = process.argv.indexOf(`--${name}`)
  return i >= 0 ? process.argv[i + 1] : undefined
}

/** 同一个 bot 只允许一个网关在跑。锁文件里记 pid 和命令行特征，确认是我们的进程才算数。 */
async function takeGatewayLock(cfg: BotConfig): Promise<() => void> {
  const lock = join(cfg.stateDir, 'gateway.lock')
  if (existsSync(lock)) {
    try {
      const rec = JSON.parse(readFileSync(lock, 'utf8')) as { pid?: number; configArg?: string }
      const pid = Number(rec.pid)
      if (pid > 0 && pid !== process.pid && isAlive(pid)) {
        // 锁里记的是那个进程自己收到的 --config 参数原文，所以它的命令行里一定有这一串
        const cmd = await processCommandLine(pid)
        if (cmd && rec.configArg && cmd.includes('main.ts') && cmd.includes(rec.configArg)) throw new ConfigError(`这个 bot 的网关已经在运行（pid ${pid}）`)
      }
    } catch (e) {
      if (e instanceof ConfigError) throw e
    }
  }
  writeAtomic(lock, JSON.stringify({ pid: process.pid, startedAt: Date.now(), config: cfg.configPath, configArg: arg('config') }))
  return () => { try { rmSync(lock, { force: true }) } catch {} }
}

async function main(): Promise<void> {
  const configPath = arg('config')
  if (!configPath) { process.stderr.write('用法：bun src/main.ts --config configs/<bot>.yml\n'); process.exit(2) }
  // 本机地址拉模型列表不绕代理（方案 3.7、D16）：保留原有 NO_PROXY 值，追加 127.0.0.1 / localhost / ::1
  addNoProxyHosts(process.env)
  const cfg = loadBotConfig(configPath)
  // bot 目录里有私密对话（账本、日志、dsh 会话），只给本用户读写
  mkdirSync(cfg.botDir, { recursive: true, mode: 0o700 })
  if (process.platform !== 'win32') chmodSync(cfg.botDir, 0o700)
  for (const d of [cfg.stateDir, cfg.dshHome, cfg.workDir, cfg.homeDir, cfg.logsDir, cfg.mediaDir]) mkdirSync(d, { recursive: true, mode: 0o700 })
  const log = new Logger({ dir: cfg.logsDir, level: cfg.gw.logLevel, console: process.env.DSH_BOT_LOG_CONSOLE === '1' || (process.stderr.isTTY === true && process.env.DSH_BOT_LOG_CONSOLE !== '0'), pretty: process.stderr.isTTY === true, maxBytes: cfg.gw.logMaxBytes, keep: cfg.gw.logKeep, bot: cfg.id })

  const token = readTelegramToken(cfg.channelDir)
  registerSecret(token)
  const apiToken = ensureSecretFile(join(cfg.stateDir, 'api.key'))
  registerSecret(apiToken)
  if (!cfg.gw.dshCommand) {
    const credErr = checkCredentialsFile(cfg.credentialsPath)
    if (credErr) throw new ConfigError(credErr)
  }
  if (!existsSync(join(cfg.channelDir, 'CLAUDE.md'))) throw new ConfigError(`人设文件不存在：${join(cfg.channelDir, 'CLAUDE.md')}`)
  if (cfg.gw.owners.length === 0) log.warn('config.no_owner', { hint: 'access.json allowFrom is empty and gateway.owners not set; /clear and notices are disabled' })

  const releaseLock = await takeGatewayLock(cfg)
  const ledger = new Ledger(join(cfg.stateDir, 'ledger.sqlite'))
  const api = new TelegramApi(token, cfg.gw.telegramApi)
  // 能发出去的文件：网关的媒体目录（收到的、合成的语音）+ 生图结果目录
  const allowDirs = () => [cfg.mediaDir, ...cfg.gw.imageDirs.map(d => d.startsWith('~') ? join(homedir(), d.slice(1)) : d)]
  // 群聊记录：bot 自己在群里发出的话由自己记（Telegram 不把 bot 的消息投给别的 bot）
  let me: { id: number; username?: string; first_name?: string } | null = null
  const transcript = new GroupTranscript(cfg.groupsDir, () => ({ id: me?.id ?? 0, username: me?.username ?? '', name: me?.first_name ?? cfg.displayName }), log)
  const sender = new Sender(api, ledger, log, {
    allowDirs, maxSendWaitMs: cfg.gw.maxSendWaitMs, access: () => loadAccess(cfg.channelDir),
    onSent: (chatId, messageId, text) => { if (isGroupChat(chatId)) transcript.sent(chatId, messageId, text) },
  })
  const dsh = new DshProcess(log)
  let engine: Engine | null = null
  const mcp = new McpServer(Engine.tools(() => engine!), segId => {
    const s = ledger.segment(segId)
    return s ? { chatId: s.chat_id, token: s.mcp_token } : null
  }, log)
  mcp.start()
  engine = new Engine(cfg, ledger, log, api, sender, dsh, mcp)

  // 自建供应商：ProviderService（读写 <根>/providers.json + 凭据）+ ProviderCommands（/provider add、refresh）。
  // 拉模型列表在锁外、写入在锁内；写完通知 Engine 立刻重算路由（方案 1.1、Q1）。依赖单向，Engine 不 import 这两个。
  const service = new ProviderService({
    root: cfg.root, credentialsPath: cfg.credentialsPath, log,
    fetchModels: req => fetchModels(req, { timeoutMs: cfg.gw.modelFetchTimeoutMs }),
    lockWaitMs: cfg.gw.providerLockWaitMs, keyGraceMs: cfg.gw.providerKeyGraceMs,
    configRouteKeys: () => Object.values(cfg.brain.routes).map(r => r.apiKeyEnv).filter((k): k is string => !!k),
    onChanged: () => engine!.reloadProvidersNow(),
  })
  // 按钮引导（/provider、/model 无参）：接走主人的私聊消息与按钮回调；先 recover 再拉更新（方案 3.3.5）
  const wizard = new Wizard({
    api, ledger, log, root: cfg.root,
    isOwner: id => id !== null && cfg.gw.owners.includes(id),
    timeoutMs: cfg.gw.wizardTimeoutMs, lateSecretMs: cfg.gw.lateSecretWindowMs,
    modelFetchTimeoutMs: cfg.gw.modelFetchTimeoutMs, lockWaitMs: cfg.gw.providerLockWaitMs,
    service, engine: engine.wizardFacade(),
  })
  const commands = new ProviderCommands({
    service, api, log, root: cfg.root,
    isOwner: id => id !== null && cfg.gw.owners.includes(id),
    botUsername: () => engine!.botUsername,
    configRouteNames: () => Object.keys(cfg.brain.routes),
    reply: (chatId, text) => void sender.send({ chatId, turnId: null, callSeq: 0, text, kind: 'system' }).catch(() => {}),
    recordUpdate: updateId => { ledger.recordUpdate(updateId, null) },
    endWizardForAdd: (chatId: string) => wizard.endForAdd(chatId),
  })
  engine.providerCommand = (_name, args) => commands.runRefresh(args)
  engine.onPoll = () => { void service.processPendingKeys().catch(e => log.warn('provider.key_removal_failed', { err: safeError(e) })) }
  // 凭据文件 refs 的全部值登记为机密（方案 3.11：启动时）
  registerCredentialSecrets(cfg.credentialsPath)

  for (let i = 0; !me; i++) {
    try { me = await api.getMe() } catch (e) {
      log.warn('telegram.get_me_failed', { err: safeError(e) })
      if (i >= 5) throw new ConfigError('连不上 Telegram（getMe 一直失败），检查网络或令牌')
      await sleep(2_000 * (i + 1))
    }
  }
  engine.botUsername = me.username ?? ''
  engine.groups = transcript
  // 导演按这个文件认出群里哪条是哪个 bot 说的
  writeAtomic(join(cfg.stateDir, 'bot.json'), JSON.stringify({ id: me.id, username: me.username ?? '', name: cfg.displayName }))
  log.info('gateway.starting', { bot: cfg.id, telegram_bot: me.username ?? null, pid: process.pid })

  const poller = new Poller(api, ledger, log, {
    channelDir: cfg.channelDir, botId: me.id, pollTimeoutS: cfg.gw.pollTimeoutS,
    onInbound: chatId => engine!.onInbound(chatId),
    onFatal: why => { log.error('gateway.fatal', { why }); void shutdown(1) },
    // /provider add 含密钥：在闸门之前接走（方案 3.6）
    interceptBeforeGate: (msg, updateId) => commands.interceptBeforeGate(msg, updateId),
    // 引导：闸门放行后、入账之前接走（方案 3.3.1）；按钮回调交给引导
    intercept: (msg, updateId) => wizard.intercept(msg, updateId),
    onCallback: cq => wizard.onCallback(cq),
    onHumanMessage: (msg, observed) => {
      if (observed) { transcript.observe(msg); return }
      // 私聊"刚聊过"标记：导演不点正在私聊的 bot 去群里说话（格式同旧系统：整数秒）
      try {
        const dir = join(cfg.directorDir, 'last-user')
        mkdirSync(dir, { recursive: true, mode: 0o700 })
        writeAtomic(join(dir, `${cfg.id}-${msg.chat.id}.last-user`), String(Math.floor(Date.now() / 1000)))
      } catch (e) { log.warn('director.marker_failed', { err: safeError(e) }) }
    },
  })
  engine.healthSource = () => ({ pollLastOkAt: poller.health.lastOkAt, pollConflict: poller.health.conflict, pollError: poller.health.lastError })
  const apiServer = new ApiServer({
    token: apiToken, port: cfg.apiPort, ledger, log, sender, channelDir: cfg.channelDir, allowDirs,
    health: () => engine!.health(engine!.healthSource!()),
    onInbound: chatId => engine!.onInbound(chatId),
    callPromises: (chatId, lines) => engine!.commitments.fromCall(chatId, lines),
    model: { info: () => engine!.modelInfo(), set: spec => engine!.modelSet(spec) },
  })
  apiServer.start()
  engine.start()
  wizard.recover() // 先从账本恢复引导与防护窗口，再开始拉 Telegram 更新（方案 3.3.5）
  wizard.start()
  poller.start()

  const heartbeatFile = join(cfg.stateDir, 'heartbeat')
  const beat = () => writeAtomic(heartbeatFile, JSON.stringify({ at: Date.now(), pid: process.pid }))
  beat()
  const hb = setInterval(beat, cfg.gw.heartbeatMs)

  let shuttingDown = false
  async function shutdown(code: number): Promise<void> {
    if (shuttingDown) return
    shuttingDown = true
    log.info('gateway.stopping')
    engine!.beginStop()
    clearInterval(hb)
    wizard.stop()
    await poller.stop()
    await engine!.stop()
    await apiServer.stop()
    await mcp.stop()
    ledger.close()
    releaseLock()
    log.info('gateway.stopped')
    process.exit(code)
  }
  process.on('SIGINT', () => void shutdown(0))
  process.on('SIGTERM', () => void shutdown(0))
  log.info('gateway.ready', { api_port: apiServer.port, mcp_port: mcp.port })
  // Python 周边（主动消息、朋友圈通知、电话回顾）从这里找网关的端口；口令在同目录的 api.key
  writeAtomic(join(cfg.stateDir, 'api.port'), String(apiServer.port))
  if (process.env.DSH_BOT_READY_FILE) writeAtomic(process.env.DSH_BOT_READY_FILE, JSON.stringify({ api_port: apiServer.port, pid: process.pid }))
}

main().catch(e => {
  const msg = e instanceof ConfigError ? e.message : safeError(e)
  process.stderr.write(`dsh-bot 网关启动失败：${msg}\n`)
  process.exit(1)
})
