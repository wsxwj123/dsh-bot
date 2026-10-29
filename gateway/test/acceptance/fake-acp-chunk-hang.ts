// 只为验收用例"模型先输出一段文字、然后卡死直到被取消"服务的最小 ACP 假件。
// 共享假件 test/fakes/fake-acp.ts 的指令表里没有这个组合（它总是在输出任何东西之前就挂起），
// 按派工要求不改共享假件，在本目录另建一份。只实现网关会用到的那几个方法。
const dec = new TextDecoder()
let buf = ''
let seq = 0
const cancelWaiters = new Map<string, () => void>()

function send(f: object) { process.stdout.write(JSON.stringify({ jsonrpc: '2.0', ...f }) + '\n') }
function update(sessionId: string, u: object) { send({ method: 'session/update', params: { sessionId, update: u } }) }

const MODEL = JSON.stringify(['deepseek-official', 'deepseek-flash'])
function configOptions() {
  return [
    { id: 'model', name: 'Model', type: 'select', currentValue: MODEL, options: [{ group: 'deepseek-official', options: [{ value: MODEL, name: 'deepseek-flash' }] }] },
    { id: 'reasoning_effort', name: 'Effort', type: 'select', currentValue: 'off', options: ['', 'off', 'low', 'high', 'max'].map(v => ({ value: v, name: v || 'default' })) },
  ]
}

async function handle(f: any) {
  if (f.id === undefined) {
    if (f.method === 'session/cancel') cancelWaiters.get(f.params?.sessionId)?.()
    return
  }
  const p = f.params ?? {}
  switch (f.method) {
    case 'initialize':
      return send({ id: f.id, result: { protocolVersion: 1, agentInfo: { name: 'fake-acp-chunk-hang', version: '0.0.0' }, agentCapabilities: { loadSession: false } } })
    case 'session/new':
      return send({ id: f.id, result: { sessionId: `chunkhang-${++seq}`, configOptions: configOptions() } })
    case 'session/resume':
      return send({ id: f.id, error: { code: -32602, message: 'session not found' } })
    case 'session/set_config_option':
      return send({ id: f.id, result: { configOptions: configOptions() } })
    case 'session/list':
      return send({ id: f.id, result: { sessions: [] } })
    case 'session/close':
      return send({ id: f.id, result: {} })
    case 'session/prompt': {
      const sid = p.sessionId
      // 先输出一段模型文字（让"这一轮已经吐过字"成立），然后一直不回话，直到收到取消
      update(sid, { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'ABCDE' } })
      await new Promise<void>(r => cancelWaiters.set(sid, r))
      cancelWaiters.delete(sid)
      return send({ id: f.id, result: { stopReason: 'cancelled' } })
    }
    default:
      return send({ id: f.id, error: { code: -32601, message: `method not found: ${f.method}` } })
  }
}

for await (const chunk of Bun.stdin.stream()) {
  buf += dec.decode(chunk, { stream: true })
  let i: number
  while ((i = buf.indexOf('\n')) >= 0) {
    const line = buf.slice(0, i).trim()
    buf = buf.slice(i + 1)
    if (line) void handle(JSON.parse(line))
  }
}

export {}
