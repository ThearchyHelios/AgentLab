/**
 * 普通用户建应用的文案（docs/design/normal-user-apps.md 第四部分）：新建应用的起点、蓝图卡片、试运行、发布摘要。
 * 包 F5 独占这个文件；页面编辑、文件控件、记录列表、个人应用的文案在 termsApp.ts（包 F6）。由 terms.ts 统一再导出。
 *
 * 规矩同 terms.ts 开头的术语表：「固定版本」不写钉住；「运行」不写跑；「服务端」不写后端；不在句子里裸写配置键名；
 * 中文句子用全角标点。卡片和问题的说法给普通人看：写「看哪些数」「谁来做」，不写节点、关节的技术名。
 */
import type {
  BlueprintScope, BlueprintStatus, ItemSource, ItemStatus, PlanStep, PlanStepKind, ProblemWho,
} from '../typesBlueprint'

// ---------------------------------------------------------------- 新建应用（§4.1）

export const NEW_APP_TEXT = {
  title: '新建应用',
  subtitle: '说清要什么、对一遍卡片、用真数试一次，再确认发布',
  /** 使用者（管理员开了个人应用）：只能建自己用的 */
  personalNote: '你建的是个人应用：只有你自己看得见，按你的权限读数据、结果会遮罩；想给别人用，做好后提交给搭建者审核发布。',
  /** 使用者、开关没开 */
  personalOff: '管理员还没有开放个人应用',
  personalOffBody: '使用者建个人应用需要管理员在设置中开启。现在可以在「问数据」里提问，或请搭建者为你做一个应用。',
  goChat: '去问数据',
  unfinished: '接着做：还没做完的应用',
  // 三个起点
  talkTitle: '说说你要什么',
  talkHint: '一句话说清看什么数、多久看一次、谁来处理。助手先问清楚，再整理成卡片给你确认。',
  talkLabel: '你想做一个什么样的应用',
  talkPlaceholder: '例如：每周一看各门店上周的销售额、订单数和退款率，不含退款单',
  talkExamples: [
    '每周一看各门店上周的销售额、订单数和退款率，不含退款单',
    '员工发起报销，金额超过 5000 元要财务组全员同意，48 小时没人处理就交给总监',
    '门店每天登记报损，月底按门店汇总看',
  ],
  exampleHint: '填入输入框，修改后开始',
  talkStart: '开始',
  talkBusy: '正在新建蓝图…',
  templateTitle: '从场景模板开始',
  templateHint: '选一个最接近的场景，卡片按模板预置好，你只需要补齐和确认。',
  templateWhen: '适用场景',
  templateProduces: '会产出什么',
  templatePrepare: '要准备什么',
  templateUse: '用这个模板',
  templatePersonalNo: '个人应用不能用这个模板（它要派人审批或写入外部系统）',
  templatePersonalPart: (part: string) => `个人应用只能用其中一部分：${part}`,
  templatesEmpty: '服务端还没有登记场景模板',
  existingTitle: '从已有的开始',
  existingHint: '把一次问数据的回答、或一个已发布的工作流做成应用。',
  recentAnswers: '最近的问数据回答',
  recentAnswersEmpty: '还没有可以做成应用的回答：先在「问数据」里问一次，回答完成后再来。',
  publishedWorkflows: '已发布的工作流',
  publishedWorkflowsEmpty: '还没有已发布的工作流',
  publishedVersion: (v: number) => `已发布 v${v}`,
  useThis: '做成应用',
  creating: '正在新建蓝图…',
  /** 应用首页的「新建应用」：说清新建走的是哪条路（取代原来「从已发布的工作流派生」的说法） */
  homeHint: '说需求、看蓝图、用真数试一次，再确认发布',
  homeHintPersonal: '建一个只有自己看得见的个人应用',
  homeEmptyBody: '点「新建应用」：说说你要什么、从场景模板开始，或把已有的问数据回答做成应用。',
}

/** 首批模板的名字（docs/design/normal-user-apps.md §3.3）：对话过程里只拿得到模板 id 时用它说；模板页、卡片用服务端给的名字 */
export const TEMPLATE_LABEL: Record<string, string> = {
  periodic_board: '周期看板', approval: '审批流程', exception_handling: '异常处置', records: '填报登记',
  reconcile_writeback: '核对写回', freeform: '自由形',
}

/** 模板会产出什么（服务端的模板说明里没给时，按首批模板的应用页说，docs/design/normal-user-apps.md §3.3） */
export const TEMPLATE_PRODUCES: Record<string, string> = {
  periodic_board: '一页看板：印章、指标、趋势、明细，可以按条件查看',
  approval: '发起按钮、我发起的、每一单的进度和待办',
  exception_handling: '异常数、本期异常清单、处置进度',
  records: '填报按钮、我的记录、汇总看板',
  reconcile_writeback: '差异指标、待复核清单、写回记录',
  freeform: '按导入的工作流派生的应用页',
}

// ---------------------------------------------------------------- 蓝图（§4.2）

export const BLUEPRINT_TEXT = {
  title: '应用蓝图',
  untitled: '未命名的应用',
  loadFailed: '无法加载蓝图',
  notFound: '蓝图不存在或已被删除',
  noAccess: '你不能查看这个蓝图：个人蓝图只有建它的人能看，共享蓝图要有管理权限。',
  back: '返回应用',
  sections: { cards: '卡片', trial: '试运行' } as Record<string, string>,
  scope: { personal: '个人', shared: '共享' } as Record<BlueprintScope, string>,
  status: {
    drafting: '起草中', trialed: '试运行过', published: '已发布', personal: '个人应用', in_review: '审核中',
  } as Record<BlueprintStatus, string>,
  copiedFrom: '这是审核用的共享副本，原个人应用不受影响',
  rename: '改名',
  renamePrompt: '应用叫什么',
  renamed: '已改名',
  // 左边的对话
  chatTitle: '和助手对话',
  chatEmpty: '说说你要什么，助手会先问清楚，再把需求整理成右边的卡片。卡片上的每一项都可以点开直接改。',
  chatLabel: '对助手说',
  chatPlaceholder: '补充需求，或者说要改哪里…',
  chatAnswerPlaceholder: '回答助手的问题…',
  chatStop: '停止这一轮',
  chatHint: '助手只提议，改动落在卡片上；你在卡片上改的会记成「你」。',
  historyFailed: '之前的对话没能取回：这一轮照常可用，只是助手接不上前面聊过的。',
  /** 这一轮以反问收尾：常显，直到回答 */
  waiting: '这一轮在等你回答',
  waitingHint: '点一个选项，或者在下方输入框里回答。',
  // 一轮的状态
  turnRunning: '助手正在整理…',
  turnAsked: '助手反问',
  turnFailed: '这一轮没有完成',
  turnStopped: '已停止',
  // 右边的卡片
  cardsTitle: '卡片',
  cardsHint: '每张卡片是一件你能判断对错的事。「缺」的要补上才能试运行，「待确认」的发布前要确认。',
  edit: '改',
  editTitle: (what: string) => `改「${what}」`,
  save: '保存',
  cancel: '取消',
  saving: '正在保存…',
  saved: '已保存，来源记成「你」',
  confirm: '确认',
  confirmHint: '这一项没问题：标成已确认，来源不变',
  confirmed: '已确认',
  remove: '删掉这一项',
  removed: '已删掉',
  add: '加一项',
  empty: '（空）',
  why: '为什么',
  item: (n: number) => `第 ${n} 项`,
  noTemplate: '还没选场景：助手会按你的需求推荐，也可以回到新建页选一个模板。',
  templateOf: (label: string, version: number) => `${label}（模板 v${version}）`,
  // 对到数据的项
  dataTable: '表',
  dataGrain: '粒度',
  dataDate: '业务日期',
  dataReuseText: (metric: string, version: number) => `复用口径卡里的「${metric}」（v${version}），不重写指标式`,
  // 关节
  jointsTitle: '生成的工作流',
  jointsHint: '模板的骨架是固定的，助手只填声明过的位置：取数的 SQL、指标的算法。其余按卡片直接填。',
  jointsEmpty: '还没有按卡片生成工作流。卡片补齐之后点「按卡片生成」。',
  instantiate: '按卡片生成',
  instantiating: '正在按卡片生成…',
  instantiated: '已按卡片生成工作流和应用骨架',
  fill: '让助手填',
  fillAgain: '让助手重新填',
  fillStop: '停止',
  fillFailed: '有的没填上',
  group: (n: number) => `第 ${n} 组`,
  groupRunning: '正在填',
  groupDone: '已填好',
  groupFailed: '没填上',
  groupRetrying: '刚才没填完，正在按顺序再试一次',
  groupChecked: '正在检查填的内容',
  groupUnknown: '没等到这一组的结果，可以再让助手填一次',
  jointsTemplate: (n: number) => `另有 ${n} 处按卡片直接填好（表单、处理人、定时这些）`,
  jointPendingNote: '这一项助手没填上，请补充或换个说法后再让助手填',
  // 底部
  trial: '用真数试一次',
  trialBusy: '正在发起试运行…',
  trialHint: '发起一次探索运行：数据是真的，但不是正式结果，也不会发布。',
  missingTitle: '还缺这些才能试运行：',
  publish: '发布',
  publishHint: '一次确认：保存版本、发布工作流、建应用、定时任务、授权',
  toConfirm: (n: number) => `发布前还有 ${n} 项要你确认或补上，点「发布」逐条看`,
  savePersonal: '存成我的应用',
  savePersonalUpdate: '更新我的应用',
  savingPersonal: '正在保存…',
  savedPersonal: '已存成你的个人应用',
  openApp: '打开应用',
}

/** 卡片名（blueprints/spec.CARDS，顺序同界面） */
export const CARD_LABEL: Record<string, string> = {
  purpose: '目的',
  template: '场景',
  data: '看哪些数',
  cadence: '多久更新',
  people: '谁来做',
  fields: '填什么',
  writeback: '写回哪里',
  access: '谁能看',
}

/** 卡片上一项的名字（按字段名） */
export const ITEM_LABEL: Record<string, string> = {
  purpose: '一句话说清给谁用、解决什么',
  label: '名称',
  unit: '单位',
  table: '对到哪张表',
  measure: '怎么算',
  date: '业务日期',
  filter: '筛选条件',
  grain: '粒度',
  who: '处理人',
  policy: '几个人同意算通过',
  due: '时限',
  on_timeout: '超时怎么办',
  threshold: '什么条件下走这一步',
  spec: '字段',
  sensitive: '敏感',
  capability: '写到哪个系统',
  mapping: '字段对应',
  confirm: '要不要人确认',
  cadence: '多久更新、统计哪一期',
  use: '可以使用',
  observe: '可以查看全部',
  manage: '可以管理',
  answer: '回答',
}

export const ITEM_STATUS_LABEL: Record<ItemStatus, string> = { confirmed: '已确认', proposed: '待确认', missing: '缺' }
/** 来源：用户填的写「你」 */
export const ITEM_SOURCE_LABEL: Record<ItemSource, string> = {
  user: '你', assistant: '助手', template: '模板默认', catalog: '数据目录', caliber: '已发布的口径卡',
}
export const itemSourceText = (s: ItemSource) => `来源：${ITEM_SOURCE_LABEL[s] ?? s}`

export const POLICY_LABEL: Record<string, string> = { any: '任一人同意', all: '全体同意', quorum: '至少几人同意' }
export const policyText = (p: { kind?: string; m?: number } | null | undefined): string =>
  !p?.kind ? '' : p.kind === 'quorum' ? `至少 ${p.m ?? '?'} 人同意` : POLICY_LABEL[p.kind] ?? p.kind
export const TIMEOUT_LABEL: Record<string, string> = { remind: '提醒处理人', escalate: '交给上级', reject: '按驳回处理' }
export const principalText = (p: { kind: string; name: string }): string =>
  p.kind === 'initiator' ? '发起人' : p.kind === 'slot' ? `岗位「${p.name}」` : p.kind === 'group' ? `${p.name}（组）` : p.name

export const CADENCE_PRESET_LABEL: Record<string, string> = { daily: '每天', weekly: '每周', monthly: '每月' }
export const WEEKDAY_LABEL = ['', '一', '二', '三', '四', '五', '六', '日']
/** 统计哪一期（定时任务的模板变量，同服务端 scheduler/service.py） */
export const PERIOD_LABEL: Record<string, string> = {
  today: '当天', yesterday: '前一天', this_iso_week: '本周', last_iso_week: '上周', this_month: '本月', last_month: '上月',
}

type Cadence = { preset?: string; weekday?: number; day?: number; time?: string; tz?: string; period?: string }

/** 「每周一 09:00」这类说法（什么时候出数）；不全的照有的写 */
export function cadenceWhen(c: Cadence | null | undefined): string {
  if (!c?.preset) return ''
  const when = c.preset === 'weekly' ? `每周${WEEKDAY_LABEL[c.weekday ?? 0] || ''}`
    : c.preset === 'monthly' ? `每月 ${c.day ?? 1} 日` : CADENCE_PRESET_LABEL[c.preset] ?? c.preset
  return `${when}${c.time ? ` ${c.time}` : ''}`
}

/** 卡片上的整句：「每周一 09:00，统计上周」 */
export function cadenceText(c: Cadence | null | undefined): string {
  const when = cadenceWhen(c)
  if (!when) return ''
  return `${when}${c?.period ? `，统计${PERIOD_LABEL[c.period] ?? c.period}` : ''}`
}

/** ISO 8601 时长 → 「2 天」「48 小时」；认不出的原样 */
export function durationText(iso: string | null | undefined): string {
  if (!iso) return ''
  const m = /^P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$/.exec(iso)
  if (!m) return iso
  const [, w, d, h, min] = m
  return [w && `${w} 周`, d && `${d} 天`, h && `${h} 小时`, min && `${min} 分钟`].filter(Boolean).join(' ') || iso
}
/** 「天 / 小时」两栏 → ISO 8601 时长 */
export function durationIso(days: number, hours: number): string {
  const d = Math.max(0, Math.floor(days || 0))
  const h = Math.max(0, Math.floor(hours || 0))
  if (!d && !h) return 'PT0H'
  return `P${d ? `${d}D` : ''}${h ? `T${h}H` : ''}`
}

/**
 * 蓝图里的一个位置 → 给人看的说法：/data/0/table → 「看哪些数」第 1 项的「对到哪张表」。
 * bp_* 操作、缺项提示都用它，卡片名和项名只有这一份
 */
export function bpPathText(path: string | null | undefined): string {
  const parts = String(path ?? '').split('/').filter(Boolean)
  if (!parts.length) return '蓝图'
  const card = CARD_LABEL[parts[0]] ?? (parts[0] === 'asks' ? '助手的问题' : parts[0])
  const rest = parts.slice(1).filter((p) => !['value', 'status', 'source', 'why'].includes(p))
  const index = rest.find((p) => /^\d+$/.test(p))
  const field = [...rest].reverse().find((p) => !/^\d+$/.test(p) && p !== '-')
  return `「${card}」${index != null ? BLUEPRINT_TEXT.item(Number(index) + 1) : ''}${field && ITEM_LABEL[field] ? `的「${ITEM_LABEL[field]}」` : ''}`
}

/** 助手的 bp_* 操作在对话过程里怎么说（run/decode.ts 的 decodeCopilot 用） */
export const BP_OP_TEXT = {
  set: (where: string) => `改了${where}`,
  add: (where: string) => `在${where}加了一项`,
  remove: (where: string) => `删掉了${where}`,
  template: (label: string) => `选了场景「${label}」`,
  ask: '助手问你一个问题',
  askNext: '点一个选项或在下方回答，助手接着整理',
  confirm: (where: string) => `确认了${where}`,
  done: '蓝图已整理好',
  checkLeft: (n: number) => `自查后仍有 ${n} 处问题，写在卡片上`,
  refused: (n: number) => `助手提的改动有 ${n} 处没有采纳`,
  fill: (joint: string) => `填好了「${joint}」`,
}

/** 卡片上各种项的编辑框 */
export const BP_EDIT_TEXT = {
  source: '数据源',
  table: '表名',
  /** 填报登记里「看哪些数」对到这个应用自己收上来的记录（服务端写 @records） */
  selfRecords: '本应用的记录表',
  selfRecordsHint: '这个应用每次提交存下的那张表；发布后系统建好表、换成真名',
  pickSource: '选一个数据源',
  pickTable: '选一张表',
  tablesLoading: '正在读取表…',
  /** 读不到表的列表（没读取过表结构、或没有权限）时直接填表名 */
  tableManual: '读不到这个数据源的表，请直接填表名',
  sourcesFailed: '读不到数据源列表，请直接填数据源名和表名',
  column: '日期列',
  rule: '按什么算（例如按支付日）',
  preset: '频率',
  weekday: '星期',
  day: '每月几日',
  time: '时间',
  tz: '时区',
  period: '统计哪一期',
  periodNone: '不按期统计',
  initiator: '发起人自己',
  slotKept: (names: string) => `现在派给岗位：${names}（发布时绑人）；选了具体的人或组就换掉岗位`,
  slotReplaced: '保存后派给上面选的人或组，原来的岗位不再用',
  policy: '通过规则',
  quorum: '至少几人同意',
  days: '天',
  hours: '小时',
  timeoutAction: '超时之后',
  timeoutTo: '交给谁',
  thresholdField: '按哪个字段',
  thresholdOp: '怎么比',
  thresholdValue: '和多少比',
  fieldTitle: '字段名称',
  fieldKind: '类型',
  fieldOptions: '选项（用顿号或逗号隔开）',
  mappingParam: '参数',
  mappingFrom: '取自',
  mapForm: '表单',
  mapMetric: '指标',
  mapRows: '核对出来的行',
  mapConst: '固定值',
  required: '必填',
  everyText: (span: string) => `，每 ${span}提醒一次`,
  mappingAdd: '加一行',
  yes: '是',
  no: '否',
  sensitive: '敏感',
  sensitiveHint: '标成敏感的字段在结果里会遮罩；能看到这条记录的人也能下载它的附件。',
  addData: '加一个要看的数',
  addDataPrompt: '这个数叫什么（例如「销售额」）',
  addStep: '加一步',
  addStepPrompt: '这一步叫什么（例如「财务审批」）',
  addField: '加一个字段',
  addFieldPrompt: '字段叫什么（例如「金额」）',
  addWriteback: '要写回系统',
  removeTitle: (where: string) => `删掉${where}？`,
  removeConfirm: '删掉',
  template: '换一个场景',
  templateHint: '换场景会按新模板重新预置卡片，已填的内容尽量保留。',
  accessEmpty: '还没有授权给谁：发布后只有你能看到。',
}

/** 表单字段的类型（「填什么」卡片） */
export const FIELD_KIND_LABEL: Record<string, string> = {
  text: '文字', textarea: '长文字', number: '数字', integer: '整数', date: '日期', select: '选项', checkbox: '是或否', file: '文件',
}

/** 金额线这类规则：只许「字段 比较 常量」 */
export const THRESHOLD_OP_LABEL: Record<string, string> = {
  '>': '大于', '>=': '不小于', '<': '小于', '<=': '不大于', '==': '等于', '!=': '不等于',
}

export const JOINT_STATUS_LABEL: Record<string, string> = { filled: '已填', pending: '待填', refused: '没填上' }
export const JOINT_BY_LABEL: Record<string, string> = { template: '按卡片', assistant: '助手', user: '你' }
export const JOINT_KIND_LABEL: Record<string, string> = {
  sql: '取数的 SQL', metric_expr: '指标的算法', fields: '表单', assignee: '处理人', policy: '通过规则', due: '时限',
  on_timeout: '超时处理', threshold: '金额线', capability: '写回目标', contract: '必需指标',
}

// ---------------------------------------------------------------- 试运行（§4.3）

export const TRIAL_TEXT = {
  title: '试运行',
  banner: '试运行：数据是真的，但这不是正式结果',
  bannerBody: '发布之后才会按定时任务出正式结果。试运行只有你看得见，写入一律走测试环境或演练，不会发送到真实系统。',
  inputs: '试运行的输入',
  inputsUnknown: '按「多久更新」卡片算出的最近一期',
  retry: '换一期再试',
  retryTitle: '换一份输入再试一次',
  retryGo: '再试一次',
  retrying: '正在发起…',
  none: '还没有试运行过',
  noneBody: '卡片补齐之后，回到卡片页点「用真数试一次」。',
  backToCards: '回到卡片改',
  askAssistant: '交给助手修',
  askAssistantText: (what: string) => `试运行出错了：${what}。请按这个原因改一下蓝图。`,
  running: '试运行正在进行，完成后自动刷新',
  failedTitle: '试运行没有成功',
  tasksTitle: '试运行里的待办',
  tasksHint: '流程里的每一步都派给了你自己：以试运行身份点一遍，看看流程走得通不通。',
  tasksEmpty: '现在没有等你处理的待办',
  writeNote: '写入步骤走测试环境；没有测试环境的只演练，不发送。',
  // 待办卡的按钮（run/TaskCard 的 trial）
  approve: '以试运行身份同意',
  reject: '以试运行身份驳回',
  submit: '以试运行身份提交',
  rejectStop: '以试运行身份驳回',
}

// ---------------------------------------------------------------- 发布摘要（§4.4）

export const PLAN_TEXT = {
  title: '发布摘要',
  planning: '正在核对要做的每一步…',
  steps: '会发生什么',
  problems: '发布前要处理的问题',
  noProblems: '没有要处理的问题',
  who: { you: '你来定', assistant: '助手可以修', admin: '要找管理员' } as Record<ProblemWho, string>,
  whoHint: {
    you: '只有你知道答案：在卡片上补齐或确认。',
    assistant: '交给助手按卡片改，改完再核对一次。',
    admin: '这些要管理员处理，或改选已发布级。',
  } as Record<ProblemWho, string>,
  whoEmpty: '没有',
  fixWithAssistant: '交给助手修',
  fixText: (lines: string[]) => `发布前核对出这些问题，请按卡片改好：\n${lines.map((l, i) => `${i + 1}. ${l}`).join('\n')}`,
  blocked: '还有问题没处理，暂时不能发布',
  level: '发布级别',
  levelPublished: '已发布',
  levelGoverned: '受管',
  levelHint: '受管级要求指标都追到原始数据，只有管理员能发布。',
  firstRun: '发布后立即运行一次',
  firstRunHint: '应用一打开就有数；不勾的话等定时任务第一次出数。',
  confirm: '确认发布',
  confirmWith: (when: string) => `发布并${when}自动出数`,
  running: '正在发布…',
  done: '发布完成',
  doneBody: '工作流、应用、定时任务和授权都已就绪。',
  stopped: (step: string) => `停在「${step}」`,
  stoppedBody: '已经完成的步骤保留着，修好之后从这一步接着做，不会重做前面的步骤。',
  resume: '接着做',
  resuming: '正在接着做…',
  close: '关闭',
  openApp: '打开应用',
}

export const PLAN_STEP_LABEL: Record<PlanStepKind, string> = {
  save_version: '保存版本',
  preflight: '发布前核对',
  publish_workflow: '发布工作流',
  create_schedule: '建定时任务',
  create_app: '建应用',
  publish_app: '发布应用',
  enable_schedule: '启用定时任务',
  grant: '授权',
  first_run: '运行第一次',
}
export const PLAN_STEP_STATUS_LABEL: Record<PlanStep['status'], string> = {
  pending: '待做', done: '已完成', failed: '没有完成', skipped: '跳过',
}

// ---------------------------------------------------------------- 问数据的「做成应用」、反问收尾（N11）

export const CHAT_BLUEPRINT_TEXT = {
  makeApp: '做成应用（蓝图）',
  makeAppHint: '带着这次回答进应用蓝图：整理成卡片、用真数试一次，再发布',
  makeAppHintPersonal: '带着这次回答建一个只有自己看得见的个人应用',
  makeAppBusy: '正在新建蓝图…',
  /** 问数据、画布：助手问回来一句，这一轮到此为止 */
  asked: '助手反问',
  answerPlaceholder: '回答助手的问题…',
}
