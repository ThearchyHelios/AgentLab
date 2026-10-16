/**
 * 定时任务页的纯函数：表单和请求体互转（含保存回来怎么接）、输入字段的对照、表单的问题、测试消息的结果。频率预设和
 * cron 互转、按定时任务的时区写时间在 lib/freq.ts（数据源的定时维护也用），这里原样再导出。不碰 React、不发请求，
 * scripts/check-schedules.mjs 直接借 vite 转译后调用它们。
 */
import { SCHEDULE_TEXT as T } from '../../lib/terms'
import type {
  Schedule, ScheduleCreate, ScheduleEvent, SchedulePatch, ScheduleRunClass, ScheduleTargetCreate, ScheduleTargetKind,
  ScheduleWebhookFormat, ScheduleWebhookIn, ScheduleWebhookTest, Workflow, WorkflowVersion,
} from '../../types'
export * from '../../lib/freq'
import { DEFAULT_FREQ, DEFAULT_GRACE_S, browserTimezone, cronOf, freqOf, freqProblem, type Freq } from '../../lib/freq'

// ---------------------------------------------------------------------------
// 表单 ⇄ 请求体
// ---------------------------------------------------------------------------

export const WEBHOOK_FORMATS: readonly ScheduleWebhookFormat[] = ['generic', 'feishu', 'wecom', 'dingtalk']
export const SCHEDULE_EVENTS: readonly ScheduleEvent[] = ['finished', 'failed', 'awaiting_approval']

export interface WebhookForm {
  on: boolean
  format: ScheduleWebhookFormat
  /** 新填的地址。已保存过地址、又没点「替换」时不用它 */
  url: string
  /** 已保存过地址时：true 表示换成上面新填的 */
  replaceUrl: boolean
  secret: string
  /** 已设置过密钥时：keep 保留、replace 换成上面新填的、clear 清除。没设置过时只看 secret 填没填 */
  secretMode: 'keep' | 'replace' | 'clear'
  events: ScheduleEvent[]
}

export interface ScheduleForm {
  name: string
  workflowId: string
  freq: Freq
  timezone: string
  runClass: ScheduleRunClass
  /** 字段名 → 模板，留空的字段不交（按工作流的默认值） */
  inputs: Record<string, string>
  /** 只在新建时用：修改时启用、暂停走页头的开关，立刻生效 */
  enabled: boolean
  misfireGraceS: number
  webhook: WebhookForm
}

/** 表单初值：新建时用默认值（时区取本机），修改时照读到的定时任务 */
export function formOf(s: Schedule | null, timezone: string = browserTimezone()): ScheduleForm {
  if (!s) {
    return {
      name: '', workflowId: '', freq: { ...DEFAULT_FREQ }, timezone, runClass: 'formal', inputs: {}, enabled: true,
      misfireGraceS: DEFAULT_GRACE_S,
      webhook: { on: false, format: 'generic', url: '', replaceUrl: false, secret: '', secretMode: 'keep', events: [...SCHEDULE_EVENTS] },
    }
  }
  return {
    name: s.name,
    workflowId: s.workflow_id,
    freq: freqOf(s.cron),
    timezone: s.timezone,
    runClass: s.run_class,
    inputs: { ...(s.input_template ?? {}) },
    enabled: s.enabled,
    misfireGraceS: s.misfire_grace_s,
    webhook: s.webhook
      ? { on: true, format: s.webhook.format, url: '', replaceUrl: false, secret: '', secretMode: 'keep', events: [...s.webhook.events] }
      : { on: false, format: 'generic', url: '', replaceUrl: false, secret: '', secretMode: 'keep', events: [...SCHEDULE_EVENTS] },
  }
}

/** 交上去的输入模板：只留填了的字段（只有空白的也算没填），留空的按工作流的默认值 */
export function templateOf(inputs: Record<string, string>): Record<string, string> {
  return Object.fromEntries(Object.entries(inputs).filter(([, v]) => typeof v === 'string' && v.trim() !== ''))
}

/** 事件按固定顺序排，勾选的先后不影响请求体，也不让「改过又改回」算成改动 */
const sortEvents = (events: ScheduleEvent[]) => SCHEDULE_EVENTS.filter((e) => events.includes(e))

export function createBody(f: ScheduleForm): ScheduleCreate {
  const w = f.webhook
  return {
    name: f.name.trim(),
    workflow_id: f.workflowId,
    cron: cronOf(f.freq),
    timezone: f.timezone,
    run_class: f.runClass,
    input_template: templateOf(f.inputs),
    enabled: f.enabled,
    overlap: 'skip',
    misfire_grace_s: f.misfireGraceS,
    webhook: w.on
      ? { format: w.format, url: w.url.trim(), ...(w.secret ? { secret: w.secret } : {}), events: sortEvents(w.events) }
      : null,
  }
}

const sameJson = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)
const sortedEntries = (o: Record<string, string>) => Object.entries(o).sort(([a], [b]) => a.localeCompare(b))

/**
 * 修改时交上去的字段（不含 version）：拿表单和打开时的表单比，只带改了的，什么都没改返回 null。和表单初值比、
 * 不和服务端的原文比：cron 落回预设再生成，写法可能和原文不同（0-6 变成 *），人没动它就不该算改了频率。
 *
 * Webhook 按约定：地址省略表示不改、空串表示删除；密钥省略表示不改、空串表示清除。要交 Webhook 时格式和事件
 * 总是一起交上，免得服务端把没写的当成清空
 */
export function patchBody(f: ScheduleForm, initial: ScheduleForm, saved: Schedule): Omit<SchedulePatch, 'version'> | null {
  const out: Omit<SchedulePatch, 'version'> = {}
  if (f.name.trim() !== initial.name.trim()) out.name = f.name.trim()
  if (f.workflowId !== initial.workflowId) out.workflow_id = f.workflowId
  if (cronOf(f.freq) !== cronOf(initial.freq)) out.cron = cronOf(f.freq)
  if (f.timezone !== initial.timezone) out.timezone = f.timezone
  if (f.runClass !== initial.runClass) out.run_class = f.runClass
  if (!sameJson(sortedEntries(templateOf(f.inputs)), sortedEntries(templateOf(initial.inputs)))) {
    out.input_template = templateOf(f.inputs)
  }
  if (f.misfireGraceS !== initial.misfireGraceS) out.misfire_grace_s = f.misfireGraceS
  const webhook = webhookPatch(f.webhook, initial.webhook, saved)
  if (webhook) out.webhook = webhook
  return Object.keys(out).length ? out : null
}

function webhookPatch(w: WebhookForm, before: WebhookForm, saved: Schedule): ScheduleWebhookIn | null {
  const events = sortEvents(w.events)
  if (!saved.webhook) {
    // 原来没有：打开了才交，地址必填
    if (!w.on) return null
    return { format: w.format, url: w.url.trim(), ...(w.secret ? { secret: w.secret } : {}), events }
  }
  if (!w.on) return { format: saved.webhook.format, url: '', events: sortEvents(saved.webhook.events) }
  const out: ScheduleWebhookIn = { format: w.format, events }
  if (w.replaceUrl && w.url.trim()) out.url = w.url.trim()
  if (saved.webhook.has_secret) {
    if (w.secretMode === 'clear') out.secret = ''
    else if (w.secretMode === 'replace' && w.secret) out.secret = w.secret
  } else if (w.secret) {
    out.secret = w.secret
  }
  const changed = out.url != null || out.secret != null || w.format !== before.format
    || !sameJson(events, sortEvents(before.events))
  return changed ? out : null
}

/**
 * 保存回来时表单怎么接：保存途中人没再动过的项换成刚存下的设置，动过的留着（相对刚存下的设置照样算未保存）。
 * Webhook 逐项比：途中只改了事件时，交上去的替换地址、新密钥照样换成「已保存」，不让含令牌的完整地址留在框里、
 * 下次保存再发一遍
 */
export function rebaseForm(cur: ScheduleForm, sent: ScheduleForm, saved: ScheduleForm): ScheduleForm {
  if (cur === sent) return saved
  const pick = <K extends keyof ScheduleForm>(k: K) => (sameJson(cur[k], sent[k]) ? saved[k] : cur[k])
  const hook = <K extends keyof WebhookForm>(k: K) => (sameJson(cur.webhook[k], sent.webhook[k]) ? saved.webhook[k] : cur.webhook[k])
  // 地址和「替换」、密钥和它的处理方式各算一组：只换半组会留下「要替换却没填地址」这种拼不上的状态
  const urlSame = cur.webhook.url === sent.webhook.url && cur.webhook.replaceUrl === sent.webhook.replaceUrl
  const secretSame = cur.webhook.secret === sent.webhook.secret && cur.webhook.secretMode === sent.webhook.secretMode
  return {
    name: pick('name'),
    workflowId: pick('workflowId'),
    freq: pick('freq'),
    timezone: pick('timezone'),
    runClass: pick('runClass'),
    inputs: pick('inputs'),
    enabled: pick('enabled'),
    misfireGraceS: pick('misfireGraceS'),
    webhook: {
      on: hook('on'),
      format: hook('format'),
      url: urlSame ? saved.webhook.url : cur.webhook.url,
      replaceUrl: urlSame ? saved.webhook.replaceUrl : cur.webhook.replaceUrl,
      secret: secretSame ? saved.webhook.secret : cur.webhook.secret,
      secretMode: secretSame ? saved.webhook.secretMode : cur.webhook.secretMode,
      events: hook('events'),
    },
  }
}

// ---------------------------------------------------------------------------
// 非工作流目标（评测题集；数据源维护在数据源页建、在这里只读）：表单 ⇄ 请求体
// ---------------------------------------------------------------------------

/** 新建时能选的目标种类：数据源维护在数据源页的「定时维护」里建（next-round 03 B4），这里只列出、只读 */
export const CREATABLE_TARGETS: readonly ScheduleTargetKind[] = ['workflow', 'eval_suite']

/**
 * 非工作流目标的表单：名称、目标、频率、时区、补发宽限，新建时另有「创建后立即启用」（修改时启用、暂停走页头的开关）。
 * 没有工作流、输入模板、运行类别和 Webhook：这几样对它们不适用，服务端带了照实拒
 */
export interface TargetForm {
  name: string
  targetKind: Exclude<ScheduleTargetKind, 'workflow'>
  targetId: string
  freq: Freq
  timezone: string
  enabled: boolean
  misfireGraceS: number
}

export function targetFormOf(s: Schedule | null, init?: { targetKind?: Exclude<ScheduleTargetKind, 'workflow'>; targetId?: string },
  timezone: string = browserTimezone()): TargetForm {
  if (!s) {
    return { name: '', targetKind: init?.targetKind ?? 'eval_suite', targetId: init?.targetId ?? '', freq: { ...DEFAULT_FREQ },
      timezone, enabled: true, misfireGraceS: DEFAULT_GRACE_S }
  }
  return {
    name: s.name, targetKind: (s.target_kind && s.target_kind !== 'workflow' ? s.target_kind : 'eval_suite'),
    targetId: s.target_id ?? '', freq: freqOf(s.cron), timezone: s.timezone, enabled: s.enabled, misfireGraceS: s.misfire_grace_s,
  }
}

export function targetCreateBody(f: TargetForm): ScheduleTargetCreate {
  return {
    name: f.name.trim(), target_kind: f.targetKind, target_id: f.targetId, cron: cronOf(f.freq), timezone: f.timezone,
    enabled: f.enabled, overlap: 'skip', misfire_grace_s: f.misfireGraceS,
  }
}

/** 修改时交上去的字段（不含 version）：只带改了的（同 patchBody，和表单初值比），什么都没改返回 null。目标建好之后不在这里改 */
export function targetPatchBody(f: TargetForm, initial: TargetForm): Omit<SchedulePatch, 'version'> | null {
  const out: Omit<SchedulePatch, 'version'> = {}
  if (f.name.trim() !== initial.name.trim()) out.name = f.name.trim()
  if (cronOf(f.freq) !== cronOf(initial.freq)) out.cron = cronOf(f.freq)
  if (f.timezone !== initial.timezone) out.timezone = f.timezone
  if (f.misfireGraceS !== initial.misfireGraceS) out.misfire_grace_s = f.misfireGraceS
  return Object.keys(out).length ? out : null
}

/** 非工作流目标的表单还缺什么（名称、目标、频率）：交上去之前先在这里拦，说法同工作流的表单 */
export function targetProblems(f: TargetForm): Partial<Record<'name' | 'target' | 'freq', string>> {
  const out: Partial<Record<'name' | 'target' | 'freq', string>> = {}
  if (!f.name.trim()) out.name = T.problem.name
  if (!f.targetId) out.target = T.problem.target(T.targetKind[f.targetKind])
  const freq = freqProblem(f.freq)
  if (freq) out.freq = freq
  return out
}

/**
 * 服务端 422 指的那一项在表单里的值，拿来比：人改过那一项就不再摆着那句说明（改回原值时说明又出来）。
 * 运行类别、输入连带工作流一起算：换了工作流，「没发布」「字段对不上」也就不一定成立了。认不出的项返回 null
 */
export function serverFieldValue(field: string, f: ScheduleForm): string | null {
  switch (field) {
    case 'name': return f.name.trim()
    case 'workflow_id': return f.workflowId
    case 'cron': return cronOf(f.freq)
    case 'timezone': return f.timezone
    case 'run_class': return JSON.stringify([f.runClass, f.workflowId])
    case 'input_template': return JSON.stringify([sortedEntries(templateOf(f.inputs)), f.workflowId, f.runClass])
    case 'misfire_grace_s': return String(f.misfireGraceS)
    case 'webhook':
    case 'webhook.url': return JSON.stringify(f.webhook)
    default: return null
  }
}

// ---------------------------------------------------------------------------
// 输入字段
// ---------------------------------------------------------------------------

export type InputField = { name: string; required?: boolean; description?: string; default?: unknown }

/** 工作流（或某个版本）入口节点声明的输入字段 */
export function graphFields(graph: WorkflowVersion['graph'] | Workflow['graph'] | undefined): InputField[] {
  const entry = graph?.nodes?.find((n) => n.type === 'input')
  return ((entry?.data?.config?.fields ?? []) as InputField[]).filter((f) => f && typeof f.name === 'string' && f.name)
}

const blankDefault = (v: unknown) => v == null || (typeof v === 'string' ? v : JSON.stringify(v)) === ''

/**
 * 输入模板对照这次会用到的字段（正式运行是已发布的那一版，探索运行是画布）。照服务端保存时的核对：
 * unknown 是填了值、但不在这些字段里的（交上去会被拒）；missing 是必填、没有默认值、又没填的。
 * 只有空白的不交，不算。入口没声明字段的工作流什么键都收，两样都不报
 */
export function inputProblems(inputs: Record<string, string>, fields: readonly InputField[]): { unknown: string[]; missing: string[] } {
  if (!fields.length) return { unknown: [], missing: [] }
  const tpl = templateOf(inputs)
  const names = new Set(fields.map((x) => x.name))
  return {
    unknown: Object.keys(tpl).filter((k) => !names.has(k)),
    missing: fields.filter((x) => x.required && blankDefault(x.default) && !(x.name in tpl)).map((x) => x.name),
  }
}

// ---------------------------------------------------------------------------
// 表单的问题
// ---------------------------------------------------------------------------

export type FormField = 'name' | 'workflow' | 'freq' | 'timezone' | 'runClass' | 'inputs' | 'url' | 'secret' | 'events'

/** 判表单问题时要的、表单之外的事 */
export interface FormContext {
  /** 选中的工作流的发布版本：null 是没发布；undefined 是还不知道（工作流列表没取回来），不判 */
  published?: number | null
  /** 这次会用到的输入字段；还没读到时不判 */
  fields?: readonly InputField[]
  /**
   * 保存时服务端会不会核对工作流、运行类别和输入：新建总会；修改只在改了这三样之一时才会（只改名、停用不查，
   * 工作流后来撤了发布也能先改名、先停下来）。不给按新建、修改区分
   */
  recheck?: boolean
}

/**
 * 表单哪几项还不能保存，值是写在那一项下面的话。saved 是修改中的定时任务（新建时为 null）。
 * 服务端保存时就会拒的组合（正式运行 + 工作流没发布、必填没填、字段对不上）在这里拦下，不让人点了保存才知道
 */
export function formProblems(f: ScheduleForm, saved: Schedule | null, ctx: FormContext = {}): Partial<Record<FormField, string>> {
  const out: Partial<Record<FormField, string>> = {}
  if (!f.name.trim()) out.name = T.problem.name
  if (!f.workflowId) out.workflow = T.problem.workflow
  const freq = freqProblem(f.freq)
  if (freq) out.freq = freq
  if (!f.timezone) out.timezone = T.problem.timezone
  if (f.workflowId && (ctx.recheck ?? !saved)) {
    if (f.runClass === 'formal' && ctx.published === null) out.runClass = T.problem.notPublished
    if (ctx.fields) {
      const { unknown, missing } = inputProblems(f.inputs, ctx.fields)
      if (unknown.length) out.inputs = T.problem.inputsUnknown(unknown.join('、'))
      else if (missing.length) out.inputs = T.problem.inputsMissing(missing.join('、'))
    }
  }
  const w = f.webhook
  if (w.on) {
    const needUrl = !saved?.webhook || w.replaceUrl
    if (needUrl && !w.url.trim()) out.url = T.problem.url
    else if (needUrl && !isHttpUrl(w.url)) out.url = T.problem.urlInvalid
    if (saved?.webhook?.has_secret && w.secretMode === 'replace' && !w.secret) out.secret = T.problem.secret
    if (!w.events.length) out.events = T.problem.events
  }
  return out
}


/** 以 http:// 或 https:// 开头、中间没有空白的地址（Webhook 地址用它；对外访问地址另有 lib/publicUrl 的 isPublicUrl） */
export function isHttpUrl(text: string): boolean {
  return /^https?:\/\/\S+$/i.test(text.trim())
}

// ---------------------------------------------------------------------------
// 测试消息的结果
// ---------------------------------------------------------------------------

/**
 * 测试消息的结果写成一句：结局（已送达 / 发送失败）、状态码、服务端的说明各说一遍。服务端的 message 两种写法都接得住：
 * 只是补充说明（「请到接收端确认已收到」「open.feishu.cn 返回 HTTP 404：…」）时接在结局后面；已经是整句
 * （「测试消息已送达（HTTP 200）」「推送失败：…」）时不再套一层。说明里写过状态码就不在结局后面再写
 */
export function webhookTestText(r: Pick<ScheduleWebhookTest, 'ok' | 'http_status'> & { message?: string | null }): string {
  const head = r.ok ? T.testOk : T.testFailed
  let detail = (r.message ?? '').trim()
  if (detail.startsWith(head)) return detail
  // 失败时开头那半句「推送失败：」「发送失败：」和结局重复
  if (!r.ok) detail = detail.replace(/^(?:测试消息)?(?:推送|发送)失败[：:]\s*/, '')
  const status = r.http_status != null && !detail.includes(`HTTP ${r.http_status}`) ? r.http_status : null
  return T.testResult(head, status, detail)
}
