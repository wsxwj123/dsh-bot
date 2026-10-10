// 白盒：新建自建供应商时必须用请求体里那把密钥去拉模型列表（INTERFACE-管理台UI 3.2 第 1 条）。
// 锁定验收里两条「新建成功」的用例（api_provider_save、api_provider_keyhygiene）没有把密钥传给假模型服务
// （withModels 未传 key，服务的期望密钥与请求体的密钥不是同一个），拉列表必然 401，那两条测不到这条行为；
// 这里用对齐的密钥把它单独钉住。
import { describe, expect, test } from 'bun:test'
import { fakeKey, readCreds, readProviders, withModels } from '../acceptance/_acc'
import { openaiOk } from '../acceptance/fake-models'

const IDS = ['m1', 'm2']
const SAVE = '/v1/provider/save'

describe('新建供应商时的密钥', () => {
  test('用请求体里的密钥拉列表，拉到的模型与密钥都落盘', async () => {
    const key = fakeKey('save')
    await withModels({ handler: openaiOk(IDS), key }, async ({ gw, b, fm }) => {
      const r = await gw.call(SAVE, { name: 'myproxy', api: 'openai-completions', baseURL: `${fm.url}/v1`, key, mode: 'create' })
      expect(r.status, `应 200，实得 ${r.status}，正文 ${r.text.slice(0, 300)}`).toBe(200)
      expect(r.json, '新建成功：kind=created，拉到两个模型').toMatchObject({ ok: true, kind: 'created', count: 2, reason: null, epoch_changed: false })
      expect(fm.requests[0]!.authOk, '拉列表时应带请求体里的密钥').toBe(true)
      expect(fm.requests[0]!.authLen, '鉴权头长度应与请求体里的密钥一致').toBe(`Bearer ${key}`.length)
      expect(readProviders(b.root)?.providers?.myproxy.route.models.map((m: any) => m.id), '拉到的模型应落盘').toEqual(IDS)
      expect(readCreds(b.root), '凭据文件里应有这把密钥').toContain(key)
      expect(r.text.includes(key), '响应正文里不该有密钥').toBe(false)
    })
  })
})
