/**
 * 数据事件触发的文案（docs/design/orchestration-round/00-总方案.md §4）：定时任务页新建时选「定时 / 事件触发」、事件那一节、
 * 列表和详情里的等待状态、触发历史里的事件、运行详情页头的「由事件触发：…」。规矩同 terms.ts 开头的术语表：「运行」不写跑，
 * 「服务端」不写后端，全角标点。
 *
 * 能选什么、为什么不能选、触发说明（「上传表格「X」出了新的一期时触发（去抖 1 分钟）」）、一次事件的说法都由服务端给
 * （scheduler/triggers.py），这里只有叫法和句式。事件种类的叫法和服务端 triggers.EVENT_LABEL 逐字对照。
 */
import type { ScheduleEventKind, ScheduleTriggerKind } from '../types'

export const TRIGGER_TEXT = {
  // ---- 新建时选触发方式
  modeLabel: '怎么触发',
  mode: { cron: '定时', event: '事件触发' } as Record<ScheduleTriggerKind, string>,
  modeHint: {
    cron: '按设定的频率到点发起运行',
    event: '数据有变化时发起运行：上传表格出了新的一期、上游工作流运行成功结束、数据源有要处理的变化',
  } as Record<ScheduleTriggerKind, string>,
  /** 修改时：触发方式建好之后不能改 */
  modeFixed: (label: string) => `触发方式：${label}。建好之后不能改，要换请新建一个定时任务`,

  // ---- 事件那一节
  section: '触发事件',
  eventLabel: '事件',
  event: {
    'table.version': '上传表格出了新的一期',
    'workflow.finished': '上游工作流运行成功结束',
    'source.changed': '数据源有要处理的变化',
  } as Record<ScheduleEventKind, string>,
  eventHint: {
    'table.version': '按期累积的上传表格导入了新的一期并启用时触发；每次上传替换整张表的表格没有这个事件',
    'workflow.finished': '选中的工作流有一次正式运行成功结束时触发；输入模板可以引用它的出口字段',
    'source.changed': '数据源的探查、剖析新记下「要处理」级的变化时触发；同一次探查或剖析产生的一批只触发一次',
  } as Record<ScheduleEventKind, string>,
  subject: { 'table.version': '上传表格', 'workflow.finished': '上游工作流', 'source.changed': '数据源' } as Record<
    ScheduleEventKind, string>,
  subjectPlaceholder: (label: string) => `选择${label}`,
  subjectEmpty: {
    'table.version': '还没有上传表格',
    'workflow.finished': '还没有工作流',
    'source.changed': '还没有数据源',
  } as Record<ScheduleEventKind, string>,
  /** 不能选的对象：名字后面接服务端给的原因 */
  subjectNotOk: (name: string, reason: string) => `${name}（${reason}）`,
  subjectGone: (name: string) => `${name}（已不存在）`,
  optionsLoading: '正在读取可选的对象…',
  optionsFailed: '无法读取可选的对象',
  optionsUnsupported: '服务端暂不支持事件触发，请升级服务端后再使用',
  retry: '重试',
  periodLabel: '期次',
  period: { any: '新增的一期和替换已有的一期都认', new: '只认新增的一期' } as Record<'any' | 'new', string>,
  periodHint: '替换已有的一期：重新上传了已经导入过的统计期（例如改正了数），选「只认新增的一期」时不触发',
  appLabel: '只认应用',
  appAny: '不限应用',
  originLabel: '只认来源',
  originAny: '不限来源',
  filterHint: '只在上游那次正式运行由这个应用、这种来源发起时触发',
  includeRejected: '也认途经驳回的',
  includeRejectedHint: '上游某个审批被驳回、按驳回那条路走完时，运行仍算成功结束。不勾时这类运行不触发；勾了以后，输入模板里的 {{ event.rejected }} 是 true',
  includeBackfill: '也认应用补算的',
  includeBackfillHint: '应用升版后按历次输入补算，会连着结束好几次正式运行，每次是某一期的历史输入。不勾时补算的运行不触发',
  debounce: '去抖',
  debounceHint: '这段时间内的几次事件合成一次触发，按最新的那一次渲染输入；上一次运行还没结束时仍按重叠策略跳过',
  debounceOption: (span: string) => (span ? `${span}内合成一次` : '不去抖，每次事件都触发'),
  timezone: '时区',
  timezoneHint: '输入模板里的日期变量、事件里的时间按这个时区写',
  chainHint: (n: number) => `上游工作流可以一个接一个地触发下游，最多 ${n} 层；串成环的设置保存时会被拒绝`,

  // ---- 预览
  preview: '按一次事件预览',
  previewIdle: '选好事件和对象后在此预览渲染出的输入',
  previewLatest: '按这个对象最近一次的事件渲染',
  previewExample: '这个对象还没有发生过这种事件，下面是示例值；立即运行时用到事件变量的输入不会发起',
  previewInputs: '渲染出的输入',
  previewNoInputs: '没有填写输入，按工作流的默认值',

  // ---- 列表、详情的等待状态
  chip: '事件触发',
  chipTitle: '由数据事件触发，不按时间',
  nextEvent: '等待事件',
  waiting: (n: number, when: string) => `去抖中：已收到 ${n} 次事件，将在 ${when} 合成一次触发`,
  pending: (n: number) => `有 ${n} 条事件等待派发`,
  pendingOff: (n: number) => `有 ${n} 条事件在等待，但定时任务调度未开启，不会派发`,
  schedulerOff: '定时任务调度未开启：事件会留着，不会派发',

  // ---- 触发历史
  fireEvent: (text: string) => `由事件触发：${text}`,
  /** 立即运行按示例事件发起的那一次：不是事件触发的，写按的是哪一次事件 */
  fireSample: (text: string) => `按事件：${text}`,
  merged: (n: number) => `合并了 ${n} 次事件`,
  depth: (n: number) => `触发链第 ${n} 层`,
  sample: '立即运行，按最近一次事件',
  /** 几个过了补发宽限的去抖窗口合成一条跳过 */
  windows: (n: number) => `${n} 个去抖窗口`,
  periods: (list: string) => `期次 ${list}`,
  sampleExample: '立即运行，事件为示例值',

  // ---- 运行详情页头
  runSource: (text: string, name?: string | null) => (name ? `由事件触发：${text}（定时任务「${name}」）` : `由事件触发：${text}`),
  runSourceGone: (text: string, name?: string | null) =>
    (name ? `由事件触发：${text}（定时任务「${name}」，已删除）` : `由事件触发：${text}（定时任务已删除）`),
  /** 立即运行按示例事件发起的运行：写成「来自定时任务」那一类，后面接按的是哪一次事件 */
  runSourceSample: (text: string, name?: string | null) =>
    (name ? `立即运行定时任务「${name}」，按事件：${text}` : `立即运行定时任务，按事件：${text}`),

  // ---- 表单的问题
  problemSubject: (label: string) => `请选择${label}`,
  problemField: '触发事件',
  /** 事件触发时「错过触发时间」那一项的说明 */
  misfireHint: '调度停过（服务停机、调度未开启）时，事件在宽限内派发照常发起，超过则跳过',
  inputsHint: '可填固定值或模板，模板中的变量按触发时间和事件计算；留空的字段按工作流的默认值',
}
