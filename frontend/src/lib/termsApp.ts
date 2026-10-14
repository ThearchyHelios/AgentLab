/**
 * 应用平台使用面的文案（应用首页、应用页、发起、实例、待办、登录）。规矩同 terms.ts 开头的术语表：
 * 「固定版本」不写钉住；「运行」不写跑；「服务端」不写后端；批准 / 驳回；不在句子里裸写键名；……
 *
 * terms.ts 末尾 `export * from './termsApp'`：调用方照旧从 lib/terms 导入。搭建面的文案写在 terms.ts。
 */
import type { ActionState, AuthMode, FormError, GrantRole, Role, TaskKind, TaskStatus } from '../typesApp'

export const APP_TEXT = {
  nav: '应用',
  title: '应用',
  hint: '使用已发布的工作流：看数、发起、处理待办',
  inboxHint: '派给你的审批、补充输入和动作确认',
  navBadge: (n: number) => `${n} 项应用待办`,
  navBadgeOpen: '打开待办',
  tabs: { inbox: '待办', apps: '应用', mine: '我发起的' },
  /** 关登录时应用首页挂的那一行（§2.7：off 时所有人等同管理员，签批人是自报的署名） */
  authOff: '未开启登录：所有人都能使用全部功能，审批和发起记录的是各自在设置中填写的署名，未经核实。',
  authOffLink: '前往设置署名',
  noApps: '还没有可用的应用',
  noAppsBody: '应用由已发布的工作流生成。在编排页发布工作流后，可在发布成功的提示中做成应用。',
  noAppsUser: '还没有授权给你的应用，请联系管理员。',
  archived: '已归档',
  unpublished: '未发布',
  publishedAt: (v: number) => `v${v}`,
  pendingTasks: (n: number) => `${n} 项待办`,
  needsUpgrade: '需升级',
  needsUpgradeHint: '引用的工作流版本已退役，入口暂停发起新的实例；已有实例照常办完，视图保留最后一次的数据。',
  open: '打开',
  edit: '编辑',
  loadFailed: '无法加载应用',
  notFound: '应用不存在或已被删除',
  noAccess: '你没有使用这个应用的授权，请联系应用的管理者。',
  pageMissing: (id: string) => `这个应用没有页面「${id}」`,
  noPages: '这个应用还没有页面',
  problems: (n: number) => `${n} 处无法显示`,
  problemsHint: '以下组件在服务端解析时出错，其余内容照常显示：',
  backToApps: '返回应用',
  version: (v: number) => `应用版本 v${v}`,
  levelGoverned: '受管',
  levelPublished: '已发布',
  pagesLabel: '应用的页面',
  reload: '重新读取',
  /** 占位页（应用编辑器写好之前）用 */
  pending: '这一页正在建设',
}

export const APP_HOME_TEXT = {
  inboxEmpty: '没有待你处理的事项',
  inboxEmptyBody: '流程里派给你的审批、补充输入和动作确认会出现在这里。',
  inboxDone: '已处理',
  showDone: '显示已处理',
  doneEmpty: '还没有处理过的事项',
  mineEmpty: '你还没有在应用里发起过事项',
  allApps: '全部应用',
  waited: (s: string) => `已等待 ${s}`,
  dueIn: (s: string) => `${s}后到期`,
  overdue: (s: string) => `已超时 ${s}`,
  inApp: (name: string) => `应用「${name}」`,
  outsideApp: '不属于应用的运行',
  backToInbox: '返回待办',
  taskNotFound: '待办不存在，所属的运行可能已被删除',
}

/** 待办的种类：看中断内容里的 kind，不看 mode（§3.7） */
export const TASK_KIND_LABEL: Record<TaskKind, string> = {
  approve: '人工审批',
  input: '补充输入',
  edit: '改稿',
  tool: '工具审批',
  code: '代码审批',
  dual_check: '双盲复算裁决',
  action_confirm: '动作确认',
  reconcile: '核实结果',
  signal: '等待外部系统',
}

export const TASK_STATUS_LABEL: Record<TaskStatus, string> = {
  pending: '待处理',
  resolving: '处理中',
  resolved: '已处理',
  cancelled: '已取消',
  expired: '已超时',
}

export const TASK_TEXT = {
  untitled: '待处理的事项',
  node: (label: string) => `节点「${label}」`,
  createdAt: (t: string) => `${t} 发起`,
  waited: (s: string) => `已等待 ${s}`,
  due: (t: string) => `时限 ${t}`,
  dueIn: (s: string) => `剩余 ${s}`,
  overdue: (s: string) => `已超时 ${s}`,
  escalated: (n: number) => (n > 1 ? `已升级 ${n} 次` : '已升级'),
  escalatedHint: '超过时限未处理，已按流程加派给上一级岗位',
  timeoutNever: '超时只会提醒、升级或驳回，不会自动批准。',
  // 会签
  policy: { any: '任一人处理即可', all: '需全部同意，任何一人驳回即否决', quorum: (m: number) => `需 ${m} 人同意` },
  signoffs: '会签进度',
  signoffCount: (agreed: number, total: number) => `${agreed}/${total} 已同意`,
  signoffRejected: (n: number) => `${n} 人驳回`,
  assignee: {
    anyone: '任何人',
    hook: '外部系统',
    group: (name: string) => `组「${name}」`,
  },
  assignmentStatus: { pending: '待回复', responded: '已回复', withdrawn: '已撤回' },
  decision: { approve: '同意', reject: '驳回', submit: '已提交', confirmed: '已生效', not_applied_resend: '重新发送',
    not_applied_abandon: '放弃' } as Record<string, string>,
  verified: '已登录核实',
  unverified: '自报署名',
  // 处理
  approve: '批准',
  reject: '驳回',
  submit: '提交',
  rejectStop: '驳回并终止',
  note: '备注',
  notePlaceholder: '备注（将传递给后续节点）',
  yourInput: '你的输入',
  editedContent: '修改后的内容',
  approveHint: '批准：继续运行；驳回：转入「驳回」分支',
  inputHint: '提交：以你填写的内容继续运行；驳回：终止本次运行',
  doneApprove: '已批准，运行继续',
  doneReject: '已驳回，转入驳回分支',
  doneRejectStop: '已驳回，本次运行终止',
  doneSubmit: '已提交，运行继续',
  doneVote: '已记下你的意见，等待其他人处理',
  formInvalid: (n: number) => `有 ${n} 项需要修改`,
  cannotRespond: '这一项没有派给你，只能查看',
  notPending: (status: string) => `这一项${status}`,
  resolvedBy: (who: string, at: string) => `由「${who}」于 ${at} 处理`,
  resolvedAt: (at: string) => `于 ${at} 处理`,
  outcome: '结果',
  signAs: (who: string) => `将以「${who}」签批，并记入审批留痕`,
  signAsVerified: (who: string) => `将以登录身份「${who}」处理，并记入留痕`,
  unsigned: '未署名：本次签批不会记录审批人',
  setActor: '去设置署名',
  openRun: '查看运行',
  openInstance: '查看实例',
  tool: '工具',
  args: '参数',
  code: '代码',
}

/** 动作确认的哈希卡（§3.8：确认时看过的就是发出去的） */
export const ACTION_CONFIRM_TEXT = {
  head: '确认后发送',
  intro: '下面是将要写入外部系统的内容（敏感字段已遮罩）。确认时一并提交内容哈希，发送前服务端重算，对不上就不发送。',
  capability: '写入能力',
  environment: '环境',
  attempt: (n: number) => `第 ${n} 次发送`,
  hash: '内容哈希',
  hashHint: (full: string) => `完整哈希：${full}`,
  preview: '发送内容',
  previewEmpty: '没有可预览的内容',
  fixed: '内容已固定，不能在此修改；如需修改，请驳回后在上游的表单里改。',
  confirm: '确认发送',
  reject: '驳回，不发送',
  noHash: '缺少内容哈希，无法确认发送',
  done: '已确认，按此内容发送',
  rejected: '已驳回，不发送',
}

/** 核实结果未知的动作：三个选项（§2.5 A5 第 4 条） */
export const RECONCILE_TEXT = {
  head: '请核实这次写入是否生效',
  intro: '请求已发出，但没有拿到确定的结果。结果未知时系统不会自动重发，请到外部系统查证后选择：',
  sentAt: (t: string) => `${t} 发出`,
  idemKey: '幂等键',
  error: '失败原因',
  confirmed: '已生效',
  confirmedHint: '外部系统里能查到这笔写入，填写外部单号',
  externalId: '外部单号',
  externalIdRequired: '请填写外部单号',
  resend: '未生效，重新发送',
  resendHint: '外部系统里查不到，按原内容重新发送一次（幂等键递增）',
  abandon: '未生效，放弃',
  abandonHint: '不再发送，流程按失败处理',
  submit: '提交核实结果',
  pick: '请选择一项',
  done: '已提交核实结果，运行将自动继续',
}

/** 等外部系统（signal）：不在这里处理 */
export const SIGNAL_TEXT = {
  head: '等待外部系统回调',
  intro: '这一步由外部系统通过签名的入站钩子完成，无需在此处理。回调到达后运行自动继续。',
  correlation: '关联键',
  hook: '钩子',
}

/** 生效记录的状态（§4.4 ACTION_STATE_LABEL） */
export const ACTION_STATE_LABEL: Record<ActionState, string> = {
  pending: '待发送',
  sent: '已发送',
  confirmed: '已生效',
  failed: '失败',
  unknown: '结果未知',
  simulated: '演练（未发送）',
}

/** 写入走哪个环境：正式运行 prod，探索运行 test，没有 test 时演练 */
export const ACTION_ENV_LABEL: Record<string, string> = {
  prod: '生产环境',
  test: '测试环境',
  simulated: '演练（不发送）',
}

export const ACTION_TEXT = {
  receipt: '回执',
  error: '失败原因',
  resolution: '核实结果',
  attempt: (n: number) => `第 ${n} 次`,
  hash: (h: string) => `哈希 ${h}`,
  empty: '这一步还没有写入记录',
}

/** 印章：截至 / 口径 / 出具档位 / 来源（§2.3） */
export const STAMP_TEXT = {
  asOf: '截至',
  caliber: '口径',
  tier: '出具档位',
  from: '来源',
  pinned: (pin: string, hash: string) => `${pin} · ${hash}`,
  unsealed: '尚未封存',
  sealed: '已封存',
  noStamp: '暂无可读的运行',
}

/** 视图来源的状态 */
export const VIEW_TEXT = {
  empty: '还没有可读的运行',
  running: '正在生成，完成后自动刷新',
  stale: '数据已过期，可点击刷新',
  error: '这部分数据无法读取',
  refresh: '刷新',
  refreshing: '正在刷新…',
  refreshed: '已发起刷新，完成后自动更新',
  cached: '命中缓存，已是最新',
  previewNoRun: '预览不发起运行',
  withheld: '这一项含有没有出处的数字，不展示',
  reportFailed: '无法读取报告文档',
  applyParams: '查看',
  unknownComponent: (type: string) => `此组件需要更新前端（${type}）`,
  masked: '已遮罩',
  noEvidence: '无证据',
  evidence: '查看出处',
  evidenceTitle: (label: string) => `「${label}」的出处`,
  evidenceLoading: '正在读取出处链…',
  evidenceFailed: '无法读取出处链',
  inputSource: (text: string) => `${text ? `「${text}」` : '这个值'}取自本次运行的输入，没有更上游的出处。`,
  close: '关闭',
  showTable: '查看数据',
  hideTable: '收起数据',
  incomparable: '升版，前后不可比',
  dualOld: '原版本',
  dualNew: '新版本',
  noPoints: '还没有可画的点',
  sortAsc: '升序',
  sortDesc: '降序',
  rows: (n: number) => `${n} 行`,
  rowsOf: (shown: number, total: number) => `显示前 ${shown} / ${total} 行`,
  truncated: '查询已达行数上限，数据库中还有更多数据',
  instances: { mine: '我发起的', all: '全部实例', empty: '还没有实例' },
  tasks: { empty: '没有待处理的事项' },
  timelineEmpty: '还没有进展',
  submitPreview: '预览中不提交',
}

/** 发起（入口表单） */
export const ENTRY_TEXT = {
  title: (label: string) => label,
  submit: '发起',
  submitting: '正在发起…',
  started: (title: string) => `已发起「${title}」`,
  invalid: '表单有误，请按提示修改后再发起',
  entryMissing: (id: string) => `这个应用没有入口「${id}」`,
  formMissing: '这个入口没有表单',
  shortcut: (keys: string) => `${keys} 发起`,
  formalNote: '提交后从应用固定的已发布版本发起一次正式运行，提交的内容记入运行留痕。',
  upgrade: '引用的工作流版本已退役，暂停发起新的实例，请联系应用的管理者升级。',
}

/** 实例页 */
export const INSTANCE_TEXT = {
  startedBy: (who: string) => `由「${who}」发起`,
  startedAt: (t: string) => `${t} 发起`,
  finishedAt: (t: string) => `${t} 结束`,
  tasks: '待办',
  noTasks: '当前没有待处理的事项',
  actions: '写入外部系统',
  live: '实时更新',
  polling: '自动刷新',
  openRun: '在记录中查看运行',
  notFound: '实例不存在，或不属于这个应用',
}

/** 表单（agentlab.form/1）的校验文案：前后端同一种错误格式 {path, code, message} */
export const FORM_TEXT = {
  required: '必填',
  type: { number: '请输入数字', integer: '请输入整数', boolean: '请选择是或否', array: '请选择', string: '请输入文字' },
  enum: '请从选项中选择',
  minimum: (n: number) => `不能小于 ${n}`,
  maximum: (n: number) => `不能大于 ${n}`,
  minLength: (n: number) => `至少 ${n} 个字`,
  maxLength: (n: number) => `最多 ${n} 个字`,
  maxItems: (n: number) => `最多选 ${n} 项`,
  format: { date: '请输入日期，如 2026-10-14', 'date-time': '请输入日期和时间', email: '请输入有效的邮箱地址',
    uri: '请输入以 http:// 或 https:// 开头的地址' } as Record<string, string>,
  yes: '是',
  no: '否',
  choose: '请选择',
  jsonInvalid: 'JSON 格式有误',
  jsonBuilderOnly: '此项需要搭建者以上的角色填写',
  principalPlaceholder: '输入用户名或组名',
  errorsHead: (n: number) => `有 ${n} 项需要修改：`,
  unknownField: (path: string) => `「${path}」`,
}

/** 服务端 422 里没对上任何字段的错误也照实列出，按 path 写给人看 */
export const formErrorText = (e: FormError, title?: string) => (title ? `${title}：${e.message}` : e.message)

/** 选人或组 */
export const PRINCIPAL_TEXT = {
  user: '用户',
  group: '组',
  everyone: '所有人',
  add: '添加',
  remove: (name: string) => `移除「${name}」`,
  placeholder: '用户名或组名',
  kind: '类型',
  empty: '未选择',
  manualHint: '未开启登录时没有用户列表，请直接填写名字',
}

/** 身份：登录、账户菜单、角色（§4.4 AUTH_TEXT） */
export const ROLE_LABEL: Record<Role, string> = {
  admin: '管理员',
  builder: '搭建者',
  user: '使用者',
}

export const GRANT_ROLE_LABEL: Record<GrantRole, string> = {
  use: '使用',
  observe: '查看全部实例',
  manage: '管理',
}

export const AUTH_MODE_LABEL: Record<AuthMode, string> = {
  off: '未开启登录',
  local: '账号密码登录',
  oidc: '单点登录',
}

export const AUTH_TEXT = {
  title: '登录 AgentLab',
  subtitle: '受限动态编排',
  username: '用户名',
  password: '密码',
  login: '登录',
  loggingIn: '正在登录…',
  oidc: '使用单点登录',
  or: '或',
  failed: '用户名或密码不正确',
  offRedirect: '未开启登录，正在返回首页…',
  expired: '登录已过期，请重新登录',
  account: '账户',
  logout: '退出登录',
  loggedOut: '已退出登录',
  signedInAs: (name: string) => `已登录：${name}`,
  role: (role: string) => `角色：${role}`,
  accountTip: '账户与退出登录',
  checking: '正在确认登录状态…',
  roleLabel: '角色',
  modeLabel: '登录方式',
  groupsLabel: '所在组',
  forgot: '忘记密码请联系管理员重置',
  useLocal: '使用账号密码登录',
}
