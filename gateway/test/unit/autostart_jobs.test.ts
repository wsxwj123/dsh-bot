// install-jobs（这只 bot 的"主动找你"定时任务）要带上和网关同一个朋友圈库：
// 不带的话任务退回仓库目录里的 state.db，和共用库分叉（2026-10-16 陈露露的主动开口记录就这样写进了旧库）。纯函数，不碰 launchctl。
import { expect, test } from 'bun:test'
import { jobsEnv, plist, pythonAgent } from '../../src/autostart'

test('jobsEnv：配置了 gateway.botlife_db 就带 BOTLIFE_STATE_DB（~ 展开成绝对路径）；没配就不带', () => {
  expect(jobsEnv({ botlifeDb: '~/claudebotlife/state.db' }, '/Users/u')).toEqual({ BOTLIFE_STATE_DB: '/Users/u/claudebotlife/state.db' })
  expect(jobsEnv({ botlifeDb: '/srv/state.db' }, '/Users/u')).toEqual({ BOTLIFE_STATE_DB: '/srv/state.db' })
  expect(jobsEnv({}, '/Users/u')).toEqual({})
})

test('jobsEnv：配了共用生图开关的全局配置和 ComfyUI 技能目录，也一并带上（发圈配图用）', () => {
  expect(jobsEnv({ botlifeDb: '~/a/state.db', globalCfg: '~/claudebotlife/configs/_global.yml', comfyuiSkillDir: '~/.claude/skills/comfyui-skill' }, '/Users/u')).toEqual({
    BOTLIFE_STATE_DB: '/Users/u/a/state.db',
    CLAUDEBOT_GLOBAL_CFG: '/Users/u/claudebotlife/configs/_global.yml',
    COMFYUI_SKILL_DIR: '/Users/u/.claude/skills/comfyui-skill',
  })
})

test('带上的变量真的进了 plist', () => {
  const env = { PATH: '/usr/bin', HOME: '/Users/u' }
  const p = plist(pythonAgent({ name: 'self-initiate.bot4', python: 'p', repo: '/r', script: ['scripts/self_initiate.py', 'bot4', '42'], root: '/h', env, extraEnv: jobsEnv({ botlifeDb: '~/claudebotlife/state.db' }, '/Users/u'), interval: 600 }))
  expect(p).toContain('<key>BOTLIFE_STATE_DB</key><string>/Users/u/claudebotlife/state.db</string>')
})
