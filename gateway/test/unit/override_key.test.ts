// 停着时在管理台选的供应商/模型/强度要盖过聊天里临时切的（适配服务 13.7）：
// 适配服务每次替停着的 bot 写 brain 段都顺带写 brain.chosen_at；覆盖记录的键带上它，它一变覆盖就清掉。
import { expect, test } from 'bun:test'
import { parseBrain } from '../../src/config'
import { brainKey, overrideKey } from '../../src/engine/engine'

test('brain.chosen_at 读进 chosenAt；没写或不是字符串就没有', () => {
  expect(parseBrain({ provider: 'deepseek-official', model: 'deepseek-flash', chosen_at: '20261015T080102.123456Z' }).chosenAt).toBe('20261015T080102.123456Z')
  expect(parseBrain({ provider: 'deepseek-official', model: 'deepseek-flash' }).chosenAt).toBeUndefined()
  expect(parseBrain({ provider: 'deepseek-official', model: 'deepseek-flash', chosen_at: 5 }).chosenAt).toBeUndefined()
})

test('覆盖键：没有 chosenAt 时与 brainKey 一样（已存的覆盖照旧有效）；有了就不同，换一个也不同', () => {
  const b = parseBrain({ provider: 'deepseek-official', model: 'deepseek-flash', reasoning_effort: 'low' })
  expect(overrideKey(b)).toBe(brainKey(b))
  const t1 = { ...b, chosenAt: '20261015T080102.000001Z' }
  const t2 = { ...b, chosenAt: '20261015T080102.000002Z' }
  expect(overrideKey(t1)).not.toBe(brainKey(b))
  expect(overrideKey(t1)).not.toBe(overrideKey(t2))
  expect(brainKey(t1)).toBe(brainKey(b))     // 运行中判断"配置的模型变没变"仍只看供应商/模型/强度
})
