// /model 引导各步的渲染（方案 3.5）：主菜单、思考强度、管理自建供应商的模型。
import type { Btn } from './keyboard'
import { truncLabel, pageSlice, PAGE_SIZE, truncateMenu } from './keyboard'
import { effortLabel, modelLine } from './text'
import { CANCEL, BACK, CLOSE, withPaging } from './provider_flow'

export type StepRender = { text: string; buttons: Btn[] }

export const M = {
  main: 'm.main',
  effort: 'm.effort',
  managePick: 'm.managePick',
  manage: 'm.manage',
  addId: 'm.addId',
  addCtx: 'm.addCtx',
  delPick: 'm.delPick',
  ctxPick: 'm.ctxPick',
  ctxInput: 'm.ctxInput',
  done: 'm.done',
} as const

/** 主菜单按钮（顺序固定） */
export const MAIN_BUTTONS: Btn[] = [
  { label: '切换模型', action: 'swmodel' }, { label: '思考强度', action: 'eff' },
  { label: '管理自建供应商的模型', action: 'mg' }, { label: '换回配置文件里的', action: 'rev' }, { label: '关闭', action: 'close' },
]

const NO_CUSTOM = '【系统】还没有自建供应商。'

/** 选哪个自建供应商（管理用；编辑当前菜单） */
export function managePickStep(names: string[], page: number): StepRender {
  if (names.length === 0) return { text: NO_CUSTOM, buttons: [{ label: '新建', action: 'new' }, CANCEL] }
  const { start, pages, page: p } = pageSlice(names, page)
  const buttons = names.slice(start, start + PAGE_SIZE).map((n, i) => ({ label: truncLabel(n), action: 'mgr', idx: start + i }))
  return { text: '【系统】管理哪个自建供应商的模型？', buttons: withPaging(buttons, p, pages, [CANCEL]) }
}

/** 管理页（编辑当前菜单）：逐行列模型，超 3500 字截断 */
export function manageStep(name: string, models: { id: string; ctx: number; guessed: boolean }[], page: number): StepRender {
  const header = [`【系统】「${name}」的模型（${models.length} 个）：`]
  const lines = models.map(m => modelLine(m.id, m.ctx, m.guessed))
  const text = truncateMenu(header, lines, '个')
  const buttons: Btn[] = [{ label: '手动加一个模型', action: 'mgadd' }, { label: '删一个模型', action: 'mgdel' }, { label: '改上下文长度', action: 'mgctx' }, BACK, CANCEL]
  return { text, buttons }
}

export const addIdStep = (): StepRender => ({ text: '【系统】请输入模型名（对方接口里的模型 id，最多 200 个字符）：', buttons: [CANCEL] })
export const addCtxStep = (): StepRender => ({ text: '【系统】上下文长度（token 数，比如 128000、128k、1.5m）；不知道就点「跳过」（按 131072 算）：', buttons: [{ label: '跳过', action: 'skip' }, CANCEL] })

/** 选模型（删 / 改上下文用；编辑当前菜单） */
export function modelPickStep(kind: 'del' | 'ctx', models: string[], page: number): StepRender {
  const { start, pages, page: p } = pageSlice(models, page)
  const buttons = models.slice(start, start + PAGE_SIZE).map((m, i) => ({ label: truncLabel(m), action: 'm', idx: start + i }))
  const title = kind === 'del' ? '【系统】删哪个模型？' : '【系统】改哪个模型的上下文长度？'
  return { text: title, buttons: withPaging(buttons, p, pages, [BACK, CANCEL]) }
}

/** 问新的上下文长度（编辑当前菜单） */
export const ctxInputStep = (id: string, ctx: number, guessed: boolean): StepRender => ({
  text: `【系统】${id} 现在的上下文长度是 ${ctx}${guessed ? '（未知，按 131072 算）' : ''}。请输入新的（比如 128000、128k、1.5m）：`,
  buttons: [CANCEL],
})

/** 思考强度页（编辑当前菜单）：options 是 dsh 报出的档位取值（已去掉空串） */
export function effortStep(options: string[], currentEffort: string | null, o: { provider: string; model: string; custom: boolean }): StepRender {
  if (options.length === 0) {
    let text = `【系统】现在的模型（${o.provider} / ${o.model}）不支持调思考强度。`
    if (o.custom) text += '自建供应商的模型默认不声明思考档位。'
    return { text, buttons: [BACK, CLOSE] }
  }
  const nowLabel = currentEffort ? effortLabel(currentEffort) : '模型默认'
  const buttons: Btn[] = options.map((v, i) => ({ label: `${v === currentEffort ? '✅ ' : ''}${effortLabel(v)}`, action: 'eff', idx: i }))
  return { text: `【系统】思考强度（现在：${nowLabel}）：`, buttons: [...buttons, BACK, CANCEL] }
}

export { CLOSE }

/** /model 主菜单（新消息）：文字与旧 /model 状态文字相同，另有按钮 */
export const modelMainStep = (text: string): StepRender => ({ text, buttons: MAIN_BUTTONS })
