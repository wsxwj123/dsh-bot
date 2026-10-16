// 语音模型的生效判定与 s1 的写法（约定 v1.1 5.14.5 第 6、7 条）。
import { expect, test } from 'bun:test'
import { runtimeRules } from '../../src/dsh/profile'
import { ALL_ON } from '../../src/features'
import { effectiveVoiceModel } from '../../src/life/voice_model'

const g = (m: unknown) => ({ voice: { tts_model: m } })

test('生效模型：bot 自己设的优先；没设看全局（不在白名单按没有）；再没有按 drama-3；旧名换现名', () => {
  expect(effectiveVoiceModel('s1', g('drama-3'))).toBe('s1')
  expect(effectiveVoiceModel(undefined, g('s1'))).toBe('s1')
  expect(effectiveVoiceModel('', g('s2.1-pro'))).toBe('s2.1-pro')
  expect(effectiveVoiceModel(undefined, g('speech-9'))).toBe('drama-3')
  expect(effectiveVoiceModel(undefined, null)).toBe('drama-3')
  expect(effectiveVoiceModel(undefined, g('drama-3-preview'))).toBe('drama-3')
  expect(effectiveVoiceModel('drama-3-preview', null)).toBe('drama-3')
})

test('s1 时语言一节改成不写方括号标签、不填 voice_instruct 与 voice_emotion；别的模型逐字不变', () => {
  const normal = runtimeRules({ text: '中文' }, ALL_ON, 'drama-3')
  expect(normal).toBe(runtimeRules({ text: '中文' }, ALL_ON))
  const s1 = runtimeRules({ text: '中文' }, ALL_ON, 's1')
  expect(s1).not.toContain('方括号语气指令')
  expect(s1).not.toContain('加上语气标签')
  expect(s1).toContain('- voice_text 用语音语言写，只朗读、不显示。不写方括号语气标签，语气靠用词表达；voice_instruct 和 voice_emotion 也不填。')
  expect(s1).toContain('- 两种语言相同时，voice_text 的内容与 text 一致，去掉不适合朗读的符号与表情。')
  expect(runtimeRules({ text: '中文' }, { ...ALL_ON, voice: false }, 's1')).toBe(runtimeRules({ text: '中文' }, { ...ALL_ON, voice: false }))
})
