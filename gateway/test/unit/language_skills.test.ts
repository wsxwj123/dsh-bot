// 补充文件 11.4、11.5：运行规则里的语言一节按 access.json 现填；语言与技能文件进重启指纹和提示词指纹（按「人设变了」同一条路）
import { expect, test } from 'bun:test'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { parseBrain } from '../../src/config'
import { buildPatchRows, promptFingerprint, restartFingerprint, runtimeRules, skillsDigest, type PatchInput } from '../../src/dsh/profile'

const input = (): PatchInput => ({ persona: 'p', brain: parseBrain({}), credentialsPath: '/c.yaml', sessionsRoot: '/s', skillDir: '/bot/skills' })

test('语言一节按 access.json 现填：文字语言缺省简体中文、语音语言缺省跟文字语言，手改的不合规值当没设', () => {
  const dflt = runtimeRules()
  expect(dflt).toContain('### 语言')
  expect(dflt).toContain('文字语言是简体中文，语音语言是简体中文。')
  expect(runtimeRules({ text: null, voice: null })).toBe(dflt)
  expect(runtimeRules({ text: '  English ' })).toContain('文字语言是English，语音语言是English。')
  expect(runtimeRules({ voice: '日文' })).toContain('文字语言是简体中文，语音语言是日文。')
  expect(runtimeRules({ text: '日[文', voice: 42 })).toBe(dflt)
  for (const s of ['text 与 voice_text 都要写', '不加语气标签', '方括号语气指令', '8 字以内', '去掉不适合朗读的符号与表情', '段数与 text 一致']) {
    expect(dflt).toContain(s)
  }
  expect(dflt).not.toContain('内心独白段')   // 用户过目后删掉：dsh 按空行分段，写空段会错位
  const rows = buildPatchRows({ ...input(), languages: { voice: '日文' } }) as any[]
  expect(rows.find(r => r.id === 'system-prompt').config.personaSuffix).toContain('语音语言是日文')
})

test('生效语言变了，重启指纹与提示词指纹都变；生效语言没变（没设改成写明简体中文）都不变', () => {
  const base = input()
  const fp = restartFingerprint(base), pk = promptFingerprint(base, '0.2.0')
  const ja = { ...base, languages: { voice: '日文' } }
  expect(restartFingerprint(ja)).not.toBe(fp)
  expect(promptFingerprint(ja, '0.2.0')).not.toBe(pk)
  const same = { ...base, languages: { text: '简体中文', voice: null } }
  expect(restartFingerprint(same)).toBe(fp)
  expect(promptFingerprint(same, '0.2.0')).toBe(pk)
})

test('技能文件改内容、加一个、删一个，内容指纹都变，两种指纹跟着变；同目录别的文件不算', () => {
  const dir = mkdtempSync(join(tmpdir(), 'skills-digest-'))
  try {
    const put = (name: string, file: string, text: string) => {
      mkdirSync(join(dir, name), { recursive: true })
      writeFileSync(join(dir, name, file), text)
    }
    expect(skillsDigest(join(dir, 'missing'))).toBe('')
    put('skill-voice', 'SKILL.md', 'v1')
    const d1 = skillsDigest(dir)
    put('skill-voice', 'notes.md', 'x')
    expect(skillsDigest(dir)).toBe(d1)
    put('skill-voice', 'SKILL.md', 'v2')
    const d2 = skillsDigest(dir)
    expect(d2).not.toBe(d1)
    put('skill-gif', 'SKILL.md', 'g1')
    expect(skillsDigest(dir)).not.toBe(d2)
    rmSync(join(dir, 'skill-gif'), { recursive: true })
    expect(skillsDigest(dir)).toBe(d2)
    const base = input()
    expect(restartFingerprint({ ...base, skillsDigest: d1 })).not.toBe(restartFingerprint({ ...base, skillsDigest: d2 }))
    expect(promptFingerprint({ ...base, skillsDigest: d1 }, 'v')).not.toBe(promptFingerprint({ ...base, skillsDigest: d2 }, 'v'))
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})
