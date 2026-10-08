// /provider 引导各步的渲染（方案 3.4）：把这一步要发的文字与按钮算出来（纯函数）。
// 推进与 Telegram IO 在 wizard.ts；这里只负责「这一步长什么样」。序号 idx 是「整个列表快照」里的绝对下标。
import type { Btn } from './keyboard'
import { truncLabel, pageSlice, PAGE_SIZE } from './keyboard'
import { fmtLabel, ROLL_NOTE } from './text'

export type StepRender = { text: string; buttons: Btn[] }

/** 步骤名（存在引导状态里，回调时校验「动作属于当前步」） */
export const P = {
  main: 'p.main',
  name: 'p.name',
  collision: 'p.collision',
  format: 'p.format',
  url: 'p.url',
  key: 'p.key',
  modifyPick: 'p.modifyPick',
  modifyWhat: 'p.modifyWhat',
  modifyFormat: 'p.modifyFormat',
  modifyOpenAIAsk: 'p.modifyOpenAIAsk',
  deletePick: 'p.deletePick',
  deleteConfirm: 'p.deleteConfirm',
  refreshPick: 'p.refreshPick',
  switchPick: 'p.switchPick',
  switchModels: 'p.switchModels',
  result: 'p.result',
} as const

export const CANCEL: Btn = { label: '取消', action: 'cancel' }
export const BACK: Btn = { label: '返回', action: 'back' }
export const CLOSE: Btn = { label: '关闭', action: 'close' }
export const NEW_BTN: Btn = { label: '新建', action: 'new' }
export const SWITCH_BTN: Btn = { label: '切到这家', action: 'sw' }
export const REFRESH_BTN: Btn = { label: '刷新模型', action: 'ref' }

/** 主菜单按钮（顺序固定，验收逐字核对） */
export const MAIN_BUTTONS: Btn[] = [
  { label: '新建', action: 'new' }, { label: '修改', action: 'mod' }, { label: '删除', action: 'del' },
  { label: '刷新模型', action: 'ref' }, { label: '切到这家', action: 'sw' }, { label: '关闭', action: 'close' },
]

export const nameStep = (): StepRender => ({ text: '【系统】新建供应商（第 1/4 步）请输入名字：只能用英文字母、数字、-、_，以字母或数字开头，最多 32 个字符。', buttons: [CANCEL] })
export const collisionStep = (orig: string): StepRender => ({
  text: `【系统】已有自建供应商「${orig}」。要更新它（地址、格式、密钥都重新填），还是换个名字？`,
  buttons: [{ label: '更新它', action: 'cu' }, { label: '换个名字', action: 'crn' }, CANCEL],
})
export const formatStep = (name: string): StepRender => ({
  text: `【系统】新建供应商「${name}」（第 2/4 步）选接口格式：`,
  buttons: [{ label: 'Anthropic 格式', action: 'fmt', idx: 0 }, { label: 'OpenAI 格式', action: 'fmt', idx: 1 }, CANCEL],
})
export const urlStep = (text: string): StepRender => ({ text, buttons: [CANCEL] })
export const keyStep = (text: string): StepRender => ({ text, buttons: [CANCEL] })

/** 从列表里取一页按钮；idx 是绝对下标 */
function pageButtons(items: string[], page: number): { buttons: Btn[]; page: number; totalPages: number } {
  const { start, pages, page: p } = pageSlice(items, page)
  return { buttons: items.slice(start, start + PAGE_SIZE).map((n, i) => ({ label: n, action: 'pv', idx: start + i })), page: p, totalPages: pages }
}
function modelButtons(items: string[], page: number, current: string): { buttons: Btn[]; page: number; totalPages: number } {
  const { start, pages, page: p } = pageSlice(items, page)
  return { buttons: items.slice(start, start + PAGE_SIZE).map((m, i) => ({ label: `${m === current ? '✅ ' : ''}${truncLabel(m)}`, action: 'm', idx: start + i })), page: p, totalPages: pages }
}
export function withPaging(items: Btn[], page: number, totalPages: number, tail: Btn[]): Btn[] {
  const out = [...items]
  if (page > 0) out.push({ label: '上一页', action: 'pg', idx: page - 1 })
  if (page < totalPages - 1) out.push({ label: '下一页', action: 'pg', idx: page + 1 })
  return [...out, ...tail]
}

export const modifyPickStep = (names: string[], page: number): StepRender => {
  if (names.length === 0) return { text: '【系统】还没有自建供应商。', buttons: [NEW_BTN, CANCEL] }
  const { buttons, page: p, totalPages } = pageButtons(names.map(truncLabel), page)
  return { text: '【系统】修改哪个自建供应商？', buttons: withPaging(buttons, p, totalPages, [CANCEL]) }
}

export const modifyWhatStep = (name: string, api: string, baseURL: string, prefix = ''): StepRender => ({
  text: `${prefix}【系统】修改「${name}」（${fmtLabel(api)}，${baseURL}）：改什么？`,
  buttons: [{ label: '地址', action: 'murl' }, { label: '密钥', action: 'mkey' }, { label: '接口格式', action: 'mfmt' }, CANCEL],
})

export const modifyFormatStep = (name: string, api: string): StepRender => {
  const mark = (v: string) => (api === v ? '✅ ' : '')
  return {
    text: `【系统】修改「${name}」的接口格式：`,
    buttons: [{ label: `${mark('anthropic-messages')}Anthropic 格式`, action: 'fmt', idx: 0 }, { label: `${mark('openai-completions')}OpenAI 格式`, action: 'fmt', idx: 1 }, CANCEL],
  }
}

export const modifyOpenAIAsk = (orig: string): StepRender => ({
  text: `【系统】OpenAI 格式的地址一般以 /v1 结尾。用「${orig}/v1」，还是重新输入？`,
  buttons: [{ label: `用 ${orig}/v1`, action: 'u1' }, CANCEL],
})

export const deletePickStep = (names: string[], page: number): StepRender => {
  if (names.length === 0) return { text: '【系统】还没有自建供应商。', buttons: [NEW_BTN, CANCEL] }
  const { buttons, page: p, totalPages } = pageButtons(names.map(truncLabel), page)
  return { text: '【系统】删除哪个自建供应商？', buttons: withPaging(buttons, p, totalPages, [CANCEL]) }
}

export const deleteConfirmStep = (name: string, inUse: string | null): StepRender => {
  let text = `【系统】确认删除「${name}」？会同时删除它的密钥。正在用它的 bot 会自动换回各自配置文件里的模型。`
  if (inUse) text += `这个 bot 正在用它，删除后换回 ${inUse}。`
  return { text, buttons: [{ label: '确认删除', action: 'dc' }, CANCEL] }
}

export const refreshPickStep = (names: string[], page: number): StepRender => {
  const { buttons, page: p, totalPages } = pageButtons(names.map(truncLabel), page)
  return { text: '【系统】刷新哪个自建供应商的模型？', buttons: withPaging(buttons, p, totalPages, [CANCEL]) }
}

export const switchPickStep = (names: string[], current: string, page: number): StepRender => {
  const { buttons, page: p, totalPages } = pageButtons(names.map(n => `${n === current ? '✅ ' : ''}${truncLabel(n)}`), page)
  return { text: '【系统】切到哪家？（✅ 是现在用的）', buttons: withPaging(buttons, p, totalPages, [CANCEL]) }
}

export const switchModelsStep = (name: string, models: string[], current: string, page: number): StepRender => {
  const { buttons, page: p, totalPages } = modelButtons(models, page, current)
  return { text: `【系统】「${name}」的模型（✅ 是现在用的）：`, buttons: withPaging(buttons, p, totalPages, [BACK, CANCEL]) }
}

export const resultStep = (text: string, buttons: Btn[]): StepRender => ({ text, buttons })

export { ROLL_NOTE }
