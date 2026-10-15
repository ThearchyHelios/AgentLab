/**
 * 新建应用和蓝图页共用的：地址、谁能建、建出来是哪种范围。
 *
 * 范围由身份决定（docs/design/normal-user-apps.md N1、§3.12）：搭建者及以上（关登录时所有人等同管理员）建共享蓝图，终点是
 * 发布单；使用者在管理员开了个人应用时建个人蓝图，终点是个人应用；都不是的没有入口。开关的读法见 personalAppsOn。
 */
import { atLeast, loginOn, useAuth } from '../../store/auth'
import type { Principal } from '../../typesApp'
import type { Blueprint, BlueprintScope, BpItem, BlueprintSpec } from '../../typesBlueprint'

export const CREATE_APP_PATH = '/apps/create'

export const blueprintPath = (id: string, section?: 'trial', query?: Record<string, string | null | undefined>) => {
  const q = new URLSearchParams()
  for (const [k, v] of Object.entries(query ?? {})) if (v) q.set(k, v)
  const tail = q.toString()
  return `/blueprints/${encodeURIComponent(id)}${section ? `/${section}` : ''}${tail ? `?${tail}` : ''}`
}

/**
 * 管理员开没开「使用者建个人应用」（设置 apps.user_personal_apps，默认关）。使用者读不到设置，开关随认人接口
 * （GET /api/auth/me 的 Principal.user_personal_apps）一起给，同问数据的 user_can_chat。老服务端不给时按关。
 */
export function personalAppsOn(me: Principal | null | undefined): boolean {
  return me?.user_personal_apps === true
}

/** 这个人新建应用时建哪种蓝图；null = 没有入口 */
export function createScope(me: Principal | null | undefined): BlueprintScope | null {
  if (atLeast(me, 'builder')) return 'shared'
  if (loginOn(me) && personalAppsOn(me)) return 'personal'
  return null
}

export const useCreateScope = (): BlueprintScope | null => useAuth((s) => createScope(s.me))

/** 卡片上的一项（有些卡片整张就是一项：目的、多久更新） */
export const isItem = (v: unknown): v is BpItem =>
  !!v && typeof v === 'object' && 'status' in (v as object) && 'source' in (v as object)

/** 卡片的顺序（同服务端 blueprints/spec.CARDS） */
export const CARD_ORDER = ['purpose', 'template', 'data', 'cadence', 'people', 'fields', 'writeback', 'access'] as const
export type CardKey = (typeof CARD_ORDER)[number]

/**
 * 这张蓝图摆哪几张卡片：选了模板的按模板用到的卡片（模板说明里的 cards），目的和场景总在；还没选模板的（对话刚开始）
 * 摆目的、场景，再加已经有内容的卡片。模板没用到的卡片不摆——它们的项是空的默认值，摆出来全是「缺」
 */
export function visibleCards(spec: BlueprintSpec | null | undefined, templateCards?: string[] | null): CardKey[] {
  if (!spec) return []
  if (templateCards?.length) {
    const used = new Set(['purpose', 'template', ...templateCards])
    return CARD_ORDER.filter((c) => used.has(c))
  }
  const has: Record<CardKey, boolean> = {
    purpose: true, template: true,
    data: spec.data.length > 0,
    cadence: isItem(spec.cadence) && (spec.cadence.value != null || spec.cadence.source !== 'assistant'),
    people: spec.people.length > 0,
    fields: spec.fields.length > 0,
    writeback: !!spec.writeback,
    access: spec.access.use.length + spec.access.observe.length + spec.access.manage.length > 0,
  }
  return CARD_ORDER.filter((c) => has[c])
}

/**
 * 服务端蓝图检查（Blueprint.issues）里说「缺」的那几种：缺必填项、没对到表、岗位没人、写回结果不明时没人核实。卡片上的
 * 「缺」以它为准——项的状态默认就是缺（单位、筛选条件这些可以不填），只看项的状态会把选填的都标成缺
 */
export const MISSING_CODES = new Set([
  'bp.missing', 'bp.data_unmapped', 'bp.people_unbound', 'bp.reconciler_unbound', 'tpl.joint_unfilled',
])

/** 卡片上标「缺」的位置：服务端给了检查结果时按它；老服务端没给时退回项本身的状态（null） */
export function missingPaths(bp: Blueprint | null | undefined): Set<string> | null {
  if (!bp?.issues) return null
  return new Set(bp.issues.filter((i) => MISSING_CODES.has(i.code) && i.path).map((i) => i.path!))
}

/** 试过没有：有最近一次试运行 */
export const trialed = (bp: Blueprint | null | undefined): boolean => !!bp?.last_trial_run_id
