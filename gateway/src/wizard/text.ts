// 引导的文案与输入校验（纯函数，方案 3.3、3.4、3.5）：「像密钥」判定、上下文长度写法、思考档位显示名、各步提示文字。
import { CONTEXT_MAX, CONTEXT_MIN } from '../providers/store'
import { apiLabel } from '../engine/commands'

/** 可见 ASCII、不含空白（! 到 ~），与 store 里模型名/密钥的判定一致 */
const VISIBLE = /^[\x21-\x7e]+$/
const HAS_LETTER = /[A-Za-z]/
const HAS_DIGIT = /[0-9]/

/**
 * 「像密钥」判定（方案 3.3.5）：去首尾空白后是单个词、8–512 个可见 ASCII、不以 http(s):// 开头、同时含字母和数字。
 * 用来决定迟到密钥防护要不要删。要求字母+数字是为了不误删 hahahaha 这类聊天字。
 */
export function looksLikeSecret(input: string | undefined | null): boolean {
  const s = (input ?? '').trim()
  if (!s || s.length < 8 || s.length > 512) return false
  if (!VISIBLE.test(s)) return false
  if (/^https?:\/\//i.test(s)) return false
  // 含 / 的多半是地址或路径（ftpx://…、example.com/v1）：当本步答案按本步规则报错，不按"像密钥"删
  if (s.includes('/')) return false
  return HAS_LETTER.test(s) && HAS_DIGIT.test(s)
}

/**
 * 上下文长度写法（方案 3.5.3）：纯整数，或数字（可带小数）后接 k/K（×1000）、m/M（×1000000），四舍五入。
 * 合格返回整数，不合格返回 null（调用方套「上下文长度要是 1024 到 100000000 之间的整数，可以写 128k 或 1.5m」）。
 */
export function parseContext(input: string | undefined | null): number | null {
  const s = (input ?? '').trim()
  const m = s.match(/^(\d+(?:\.\d+)?)([kKmM]?)$/)
  if (!m) return null
  const num = Number(m[1])
  if (!Number.isFinite(num)) return null
  const unit = m[2]?.toLowerCase()
  const scaled = unit === 'k' ? num * 1000 : unit === 'm' ? num * 1_000_000 : num
  const v = Math.round(scaled)
  return Number.isInteger(v) && v >= CONTEXT_MIN && v <= CONTEXT_MAX ? v : null
}

/** 思考档位的显示名（方案 3.5.2）：off→关、low→低、high→高、max→最高，其它按原名 */
export function effortLabel(value: string): string {
  switch (value) {
    case 'off': return '关'
    case 'low': return '低'
    case 'high': return '高'
    case 'max': return '最高'
    default: return value
  }
}

/** 由显示名反查 dsh 的取值（点按钮时用）；找不到 = 该显示名不对应任何档 */
export function effortValue(label: string, options: string[]): string | null {
  for (const v of options) if (effortLabel(v) === label) return v
  return null
}

/** 已过期/伪造菜单的统一回话 */
export const STALE_TEXT = '这个菜单已过期，请重新发 /provider 或 /model'
export const BUSY_TEXT = '正在处理，请稍候'
export const CANCELLED_MENU = '已取消。'
export const CLOSED_MENU = '已关闭。'
export const EXITED = '【系统】已退出引导。'
export const EXITED_CHAT = '【系统】已退出引导。之后的消息照常和角色聊天。'
export const LATE_SECRET = '【系统】这条消息像是密钥，没有交给角色，已帮你删除（没删掉的话请手动删）。需要的话重新发 /provider。'

export const nameTaken = (n: string) => `【系统】${n} 是内置、配置文件里的供应商或保留字，不能用，换个名字。请重新输入：`
export const nameCollision = (n: string) => `【系统】已有自建供应商「${n}」。要更新它（地址、格式、密钥都重新填），还是换个名字？`
export const URL_PROMPT = '请输入接口地址：https:// 开头；本机地址（127.0.0.1、localhost、[::1]）可以用 http://。OpenAI 格式一般以 /v1 结尾；Anthropic 格式填根地址，末尾的 /v1 会自动去掉。'
export const CREATE_STEP3 = `【系统】（第 3/4 步）${URL_PROMPT}`
export const MODIFY_URL_STEP = `【系统】${URL_PROMPT}`
export const KEY_PROMPT = '请输入密钥。收到后会马上删除你发的那条消息。'
export const CREATE_STEP4 = `【系统】（第 4/4 步）${KEY_PROMPT}`
export const MODIFY_KEY_STEP = `【系统】${KEY_PROMPT}`
export const REKEY = '【系统】换了主机或格式，需要重新输入密钥（旧密钥不会发给新地址）：'
export const KEY_BAD = '【系统】这条不像密钥（要求 8–512 个可见英文字符、中间不能有空格），已删除。请重新输入密钥：'
export const KEY_AS_ANSWER_BAD = '【系统】这条像是密钥，已删除，没有当作这一步的答案。'
export const THIS_STEP_NEEDS_BUTTON = '【系统】这一步请点上面的按钮；不想继续就点「取消」或发 /cancel。'
export const THIS_STEP_NEEDS_TEXT = '【系统】这一步需要文字，请重新输入。'
export const BUSY_DELETED = '【系统】正在处理，请稍候（你的消息已删除，其中的内容没有保存）。'
export const BUSY_KEEP = '【系统】正在处理，请稍候。'
export const ITEM_GONE = '该项已不存在，请重新选择'

/** 需要文字的步骤提示后缀（方案 3.4.2、3.5.3） */
export const nameStepHint = '请输入名字：只能用英文字母、数字、-、_，以字母或数字开头，最多 32 个字符。'
export const urlStepHint = CREATE_STEP3
export const modelNameHint = '请输入模型名（对方接口里的模型 id，最多 200 个字符）：'
export const ctxStepHint = '上下文长度（token 数，比如 128000、128k、1.5m）；不知道就点「跳过」（按 131072 算）：'
export const CONTEXT_BAD = '上下文长度要是 1024 到 100000000 之间的整数，可以写 128k 或 1.5m'
export const MODEL_NAME_BAD = '模型名要 1–200 个可见英文字符，不能有空白'

/** 管理页逐行：<id>（上下文 <ctx>，[未知，按 131072 算]） */
export function modelLine(id: string, ctx: number, guessed: boolean): string {
  return `${id}（上下文 ${ctx}${guessed ? '，未知，按 131072 算' : ''}）`
}

/** 供应商格式标签（与 /provider 列表同源） */
export const fmtLabel = (api: string) => apiLabel(api)

/** 修改页标题：修改「<名字>」（<格式>，<地址>）：改什么？ */
export const modifyWhat = (name: string, api: string, baseURL: string) => `【系统】修改「${name}」（${fmtLabel(api)}，${baseURL}）：改什么？`

export const ROLL_NOTE = '正在用它的聊天下一条消息会先写交接摘要、再开新会话（相当于换了一家）。'
export const DELETED_NOTE = '（你发的密钥消息已删除。）'
export const NOT_DELETED_NOTE = '（你发的密钥消息没能删除，请手动删掉它。）'

/** 模型编辑（加/删/改上下文）失败文案（方案 3.5.3） */
export function modelEditError(why: 'not_found' | 'exists' | 'gone' | 'lock_timeout' | 'unreadable', name: string): string {
  if (why === 'lock_timeout') return '【系统】没保存成功：别的 bot 正在改供应商，请稍后再试。'
  if (why === 'unreadable') return '【系统】没保存成功：共用供应商文件读不了（格式坏了）。'
  if (why === 'exists') return '【系统】这个模型已经有了。'
  return `【系统】「${name}」已经不在了。`
}

/** 删除供应商失败文案（方案 3.4.4 第 4 条） */
export function deleteFailText(r: { status: string; why?: string; name?: string }): string {
  if (r.status === 'not_found') return `【系统】「${r.name ?? ''}」已经不在了。`
  if (r.status === 'lock_timeout') return '【系统】没删成：别的 bot 正在改供应商，请稍后再试。'
  if (r.status === 'unreadable') return '【系统】没删成：共用供应商文件读不了（格式坏了）。'
  return `【系统】没删成：${r.why ?? '写文件失败（Error）'}。`
}
