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
  noAppsBody: '应用由已发布的工作流生成：点「新建应用」，从一个已发布的工作流版本开始。',
  /** 应用首页页头的「新建应用」（搭建者及以上）：新建向导的起点，现在从已有的工作流开始 */
  create: '新建应用',
  createHint: '从一个已发布的工作流版本派生应用，之后再调整布局和文案',
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

/** 待办的种类：看中断内容里的 kind，不看 mode（§3.7）。review 是个人应用提交审核（docs/design/normal-user-apps.md N7） */
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
  review: '审核个人应用',
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
  youResponded: (decision: string, at: string) => `你已处理：${decision}${at ? `（${at}）` : ''}`,
  // 升级：升级加派的那几份一人说了算（服务端 process/tasks._tally）
  escalationTag: '升级',
  escalationRule: (who: string) => `已升级给${who}：其中任一人同意即通过、驳回即否决，原来的人仍可按原规则处理`,
  escalationDecided: (who: string, decision: string) => `已升级，由「${who}」一人决定：${decision}`,
  slot: (name: string) => `岗位「${name}」`,
  waitingOn: (names: string) => `等待 ${names} 处理`,
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
  // 截至一项不另写标签：服务端给的整句（as_of_text）自己带着「截至」
  caliber: '口径',
  tier: '出具档位',
  from: '来源',
  pinned: (pin: string, hash: string) => `${pin} · ${hash}`,
  unsealed: '尚未封存',
  sealed: '已封存',
  noStamp: '暂无可读的运行',
}

/**
 * 搭建面几个术语的一句话解释：应用编辑里视图来源、固定版本、处置旁边写的那一句（不只放在悬停里）。说的是使用者
 * 看到的效果，和服务端的行为一致：最近封存读已封存的正式运行、不发起；按需按参数发起、缓存时效内同样的参数复用；
 * 本实例读实例自己的运行；固定版本按哈希发起正式运行；处置决定升版前后的趋势怎么画
 */
export const APP_TERM_TEXT = {
  viewKinds: {
    latest_sealed: '最近封存：读固定版本最近一次已封存的正式运行（可限定为某个定时任务发起的），打开页面不发起运行',
    on_demand: '按需：点「刷新」或打开页面时按页面上的参数发起一次正式运行；缓存时效内同样的参数直接用上次的结果',
    instance: '本实例：在实例页上读这个实例自己的那次运行',
  } as Record<string, string>,
  pin: '固定版本：应用引用的某个工作流的一个已发布版本。应用只按这一版发起正式运行、读它的结果，工作流发了新版本也不会自动换',
  disposition: '处置：固定版本升了版之后，升版前后的数据怎么放在一起看——按新口径重算、两版并列，或前后不可比。发布新的应用版本时必须选',
}

/** 视图来源的状态 */
export const VIEW_TEXT = {
  empty: '还没有可读的运行',
  /** 服务端没给原话时的兜底，和服务端 apps/resolve.RUNNING_TEXT 同一句 */
  running: '正在运行，完成后自动刷新',
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
  /** 名录（搭建者及以上可读）里核对填的名字 */
  unknownUser: (name: string) => `名录里没有用户「${name}」，请从候选中选择`,
  unknownGroup: (name: string) => `名录里没有组「${name}」，请从候选中选择`,
  disabledUser: (name: string) => `用户「${name}」已停用`,
  /** 单点登录：人和组在第一次登录后才进名录，名录里没有的可以照填，但要确认 */
  unknownOidc: (name: string) => `名录里还没有「${name}」：单点登录的人和组第一次登录后才进名录。名字要和身份提供方里的一致才生效`,
  addAnyway: '仍然添加',
  /** 已选的那一项不在名录里、或已停用：标在标签上 */
  missingTag: '名录里没有',
  disabledTag: '已停用',
  /** 服务账号能拿应用授权，不能被派待办：岗位绑定、改派里不收 */
  serviceUser: (name: string) => `「${name}」是服务账号，不能被派待办：请选人或组`,
  serviceTag: '服务账号，不能被派待办',
  /** 使用者选人：候选按这件事能选的人给（服务端 auth/directory 的候选池，05-已知遗留 (b)） */
  notCandidate: (name: string) => `「${name}」不在可选的人里，请从候选中选择`,
  typeToSearch: '能选的人较多：输入名字的开头查找',
  truncated: '只列出了一部分：多输入几个字缩小范围',
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

/** 授权三档各管什么（apps/access.py）：应用编辑「岗位与授权」里每一行的说明 */
export const GRANT_ROLE_HINT: Record<GrantRole, string> = {
  use: '能打开应用、从入口发起、处理派给自己的待办；实例只看得到自己发起或参与的',
  observe: '在「使用」之外，还能查看这个应用的全部实例',
  manage: '能编辑、发布这个应用，绑定岗位、授权、管理钩子。只对搭建者生效：授给使用者的按「查看全部实例」算',
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
  /** 新部署（账号密码登录、还没有能登录的管理员）：登录页先建第一个管理员 */
  bootstrapTitle: '建第一个管理员',
  bootstrapHint: '这是新部署，还没有能登录的管理员。先建一个管理员账号，建好后直接以它登录；其他账号在「设置 → 身份」中添加。',
  displayName: '显示名',
  displayNameHint: '选填，界面上显示的名字，不填时用用户名',
  confirmPassword: '确认密码',
  passwordMismatch: '两次输入的密码不一致',
  bootstrap: '建立并登录',
  bootstrapping: '正在建立…',
  haveAccount: '已有账号，直接登录',
}

/**
 * 发布、发起正式运行、导入确认这些留痕记在谁的名下（store/auth 的 useSigner）：开了登录是登录的人，没有署名框（服务端
 * 只认登录的人，填了也不看）；关登录是本机署名（未认证），那一套说法在各处原样保留
 */
export const SIGNER_TEXT = {
  publishAs: (name: string) => `以登录身份发布：${name}`,
  launchAs: (name: string) => `以登录身份发起：${name}`,
  confirmAs: (name: string) => `以登录身份确认：${name}`,
  /** 确认框里那一行（数据导入的启用、回滚、移除、清除原件） */
  signedIn: (name: string) => `登录身份：${name}`,
  record: '记入留痕',
}

/**
 * 个人渠道（设置 → 通知，范围是「仅自己」）里实例结束的两个事件：只推自己发起、而且是登录身份发起的实例（服务端
 * notify/service.notify_run，04-对外集成与触达 B1）。以前叫「实例完成 / 失败」，勾了却永远收不到
 */
export const NOTIFY_PERSONAL_EVENT_LABEL: Record<string, string> = {
  'run.finished': '我发起的实例完成',
  'run.failed': '我发起的实例失败',
}

/** 发布弹窗按角色收起的档位（Q8：受管级发布只给管理员，服务端 api/workflows 同样拒） */
export const PUBLISH_ROLE_TEXT = {
  governedAdminOnly: '需要管理员：以受管级发布只给管理员，请联系管理员发布',
  /** 受管工作流降成已发布：和以受管级发布同一级的决定，只给管理员（服务端同样拦） */
  downgradeAdminOnly: '需要管理员：这是受管工作流，降为「已发布」会让此后的正式运行不再过治理门禁，请联系管理员发布',
  governedWorkflowAdminOnly: '这是受管工作流，新版本只有管理员能发布',
  pickLevel: '请先选择发布等级',
  /** 降级（受管 → 已发布）点发布时再确认一次 */
  downgradeTitle: (name: string) => `以「已发布」等级发布「${name}」？`,
  downgradeConsequences: ['当前为受管工作流，发布后降为已发布', '此后从这一版发起的正式运行不再受治理门禁约束'],
  downgradeConfirm: '降级并发布',
}

// ---------------------------------------------------------------------------
// 普通用户建应用（docs/design/normal-user-apps.md §4.5–§4.7）：页面上直接改、文件控件、记录列表、个人应用。
// 蓝图、模板、试运行、发布摘要的说法在 termsBlueprint.ts
// ---------------------------------------------------------------------------

/** 应用页上直接改（N6）：编辑层、小工具条、加一张数据卡片、发布 */
export const PAGE_EDIT_TEXT = {
  edit: '编辑',
  editHint: '在页面上直接改文字、隐藏组件、调整顺序；改动自动存为草稿，发布后使用者才看得到',
  done: '完成编辑',
  editing: '编辑中',
  pickHint: '点选页面上的组件进行修改。改动自动存为草稿，按草稿重新显示；发布之前，使用者看到的还是当前发布版。',
  saving: '正在保存…',
  saved: '已存为草稿',
  saveFailed: '草稿保存失败，页面已恢复为上一次保存的样子',
  unpublished: '有未发布的改动',
  neverPublished: '这个应用还没有发布',
  draftSameAsPublished: '草稿和当前发布版一致',
  publish: '发布',
  publishing: '正在发布…',
  publishTitle: '发布这些改动？',
  publishConsequences: ['按现在的草稿生成新的应用版本，使用者打开时看到改过的样子', '只改页面呈现，不改工作流，也不发起运行'],
  publishConfirm: '发布',
  published: (v: number) => `已发布应用版本 v${v}`,
  publishBlocked: '发布前检查没有通过，需要先处理这些问题：',
  openCheck: '到应用编辑器处理',
  openEditor: '应用编辑器',
  openEditorHint: '入口、表单、岗位和授权在应用编辑器里改',
  changeData: '改取数',
  changeDataHint: '取数逻辑在蓝图里改：改完要用真数再试一次，口径变了还要说清以前的数怎么处理',
  personalNoPublish: '个人应用没有发布这一步：改动保存后直接生效',
  // ---- 小工具条
  toolbar: (label: string) => `「${label}」的操作`,
  editText: '修改文字',
  editTextTitle: (label: string) => `修改「${label}」的文字`,
  editTextHint: '只改组件上显示的文字，不改它的数据来源',
  noText: '这个组件没有可改的文字',
  saveText: '保存文字',
  hide: '隐藏',
  unhide: '取消隐藏',
  hiddenTag: '已隐藏',
  hiddenHint: '作者隐藏了它：使用者看不到。可见性条件是按运行情况决定显示与否，隐藏是不要它',
  moveUp: '上移',
  moveDown: '下移',
  remove: '删除',
  removeTitle: (label: string) => `删除「${label}」？`,
  removeConsequences: ['从草稿里删掉这个组件，发布后使用者不再看到它', '数据来源不受影响，以后可以用「加一张数据卡片」再加回来'],
  removeConfirm: '删除组件',
  /** 草稿里隐藏了、页面上没有画出来的组件：列在页底，可以取消隐藏 */
  hiddenElsewhere: (n: number) => `这一页还有 ${n} 个隐藏的组件没有显示出来：`,
  // ---- 加一张数据卡片
  addCard: '加一张数据卡片',
  addCardHint: '从这个应用固定版本的结果里挑：口径卡的指标、报告、明细或趋势',
  candidatesLoading: '正在列出可加的数据卡片…',
  candidatesEmpty: '没有可加的数据卡片：固定版本里没有口径卡、报告、取数结果或趋势',
  candidateKinds: { metric: '口径卡指标', report: '报告', table: '明细', series: '趋势' } as Record<string, string>,
  addThis: '加上',
  added: (label: string) => `已加上「${label}」`,
  close: '关闭',
  /** 应用编辑器属性面板里的「隐藏」开关 */
  hiddenSwitch: '隐藏这个组件',
}

/** 文件控件（N9）：拖放或点选，先上传拿引用，再随表单提交 */
export const FILE_TEXT = {
  drop: '把文件拖到这里，或点击选择',
  dropNow: '松开即上传',
  choose: '选择文件',
  sending: '正在上传',
  sent: '上传完成，正在等服务端核对',
  cancel: '取消上传',
  cancelled: '已取消上传',
  remove: '移除',
  replace: '换一个文件',
  download: '下载',
  types: (list: string) => `允许的类型：${list}`,
  maxSize: (mb: number) => `单个文件不超过 ${mb} MB`,
  serverChecks: '类型和大小由服务端核对',
  tooLarge: (name: string, mb: number) => `「${name}」超过 ${mb} MB，不能上传`,
  badType: (name: string, list: string) => `「${name}」的类型不在允许范围内，允许：${list}`,
  failed: (name: string, reason: string) => `「${name}」上传失败：${reason}`,
  noContext: '这里不能上传文件：只有应用的入口、待办和蓝图的试运行能带文件',
  whoCanDownload: '能看到这次提交的人都能下载附件',
  /** 值不是上传后拿回的引用（前后端同一条：必填，形状是文件引用） */
  invalid: '请上传文件',
  empty: '未上传',
  // ---- 运行详情里读入附件的一行（file.extracted，服务端 files/text.py 的模板函数 file_text）。数由调用方按千分位写好 ----
  extracted: (name: string, chars: string | null) => {
    const what = name ? `读入附件「${name}」` : '读入附件'
    return chars == null ? what : `${what}${name ? '' : ' '}${chars} 字`
  },
  extractedAll: (total: string) => `全文 ${total} 字，已全部读入`,
  extractedCut: (chars: string, total: string) => `全文 ${total} 字，只读入了前 ${chars} 字，其余没有读入`,
  extractFailed: (name: string) => (name ? `附件「${name}」抽不出文字` : '附件抽不出文字'),
  typeLabels: {
    'application/pdf': 'PDF', 'application/msword': 'Word', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'Word',
    'application/vnd.ms-excel': 'Excel', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'Excel',
    'text/csv': 'CSV', 'text/plain': '纯文本', 'image/png': 'PNG 图片', 'image/jpeg': 'JPG 图片',
  } as Record<string, string>,
}

/** 记录列表 records_list（N8）：使用者只看自己的，观察者、管理者看全部 */
export const RECORDS_TEXT = {
  mine: '我的记录',
  all: '全部记录',
  mineHint: '只显示你自己提交的记录',
  allHint: '你能查看这个应用的全部记录，包括别人提交的',
  filter: '筛选',
  filterPlaceholder: '按内容或提交人筛选',
  sort: '排序',
  newest: '最新的在前',
  oldest: '最早的在前',
  empty: '还没有记录',
  emptyMine: '你还没有提交过记录',
  emptyFiltered: '没有符合筛选条件的记录',
  count: (shown: number, total: number) => (shown === total ? `${total} 条` : `${shown} / ${total} 条`),
  seq: (n: number) => `第 ${n} 条`,
  by: (who: string) => `由「${who}」提交`,
  expand: '展开全部字段',
  collapse: '收起',
  corrected: '已更正',
  correctedHint: '这条是对之前一次提交的更正，原来的那条保留在更正历史里',
  superseded: '已被更正',
  history: '更正历史',
  hideHistory: '收起更正历史',
  historyLoading: '正在读取更正历史…',
  historyEmpty: '没有找到更正前的记录',
  historyItem: (n: number, at: string) => `第 ${n} 条 · ${at}`,
  attachment: '附件',
  version: (v: number) => `表版本 v${v}`,
  more: '加载更多',
  reload: '重新读取',
  tableMissing: '这个应用还没有记录表：发布之后、有人提交过才会有记录',
  preview: '按草稿预览：这里不列真实的记录，发布之后按看的人列出',
  yes: '是',
  no: '否',
}

/** 个人应用（N7）：使用者自己建、只有自己看得见、永远是探索 */
export const PERSONAL_TEXT = {
  group: '我的应用',
  groupHint: '你自己建的个人应用，只有你看得见',
  sharedGroup: '共享的应用',
  badge: '个人',
  badgeHint: '个人应用：只有你看得见，数据来自你自己发起的探索运行，未经发布审核',
  banner: '这是你的个人应用：只有你看得见，结果按你的权限遮罩。数据来自你自己发起的探索运行，未经发布审核，不是正式结果。',
  refresh: '刷新',
  refreshLeft: (n: number) => `今天还剩 ${n} 次`,
  refreshLimitHint: '每天能刷新的次数有上限，由管理员设置',
  refreshUsedUp: '今天的刷新次数已用完，明天再来',
  refreshing: '正在刷新…',
  refreshed: (n: number) => `已发起刷新，完成后自动更新。今天还剩 ${n} 次`,
  submitReview: '提交审核',
  submitReviewHint: '想给别人用：交给一位搭建者或一个组审核，审核通过后由搭建者发布成正式应用',
  reviewTitle: '提交审核',
  reviewIntro: '搭建者会在一份副本上用真数试运行、再发布成正式应用，你的个人应用不会被改动。发布后会通知你，个人应用留不留由你决定。',
  reviewTo: '交给谁审核',
  reviewToHint: '选一位搭建者，或者一个组（组里任一人都能处理）',
  reviewNote: '说明（选填）',
  reviewNotePlaceholder: '这个应用解决什么问题、给谁用',
  reviewSubmit: '提交审核',
  reviewPick: '请先选择交给谁审核',
  reviewSent: '已提交审核',
  cancel: '取消',
  inReview: '审核中',
  inReviewTo: (names: string) => `已提交给 ${names} 审核，等待处理`,
  inReviewPlain: '已提交审核，等待搭建者处理',
  formal: '已有正式版本',
  formalGo: '去看看',
  rejected: '上次的审核已退回，可以改好后再提交',
  rejectedNote: (note: string) => `上次的审核已退回：${note}`,
  graphChanged: '蓝图在存成个人应用之后又改过：刷新按存下的那一版运行，要用改过的，请回到蓝图重新存成你的应用。',
  graphChangedGo: '回到蓝图',
  switchedOff: '管理员关闭了「使用者可以建个人应用」：已有的结果还能看，暂时不能刷新、提交审核',
  // ---- 管理员看别人的个人应用：只有名称、拥有者、时间，可以归档，看不到内容
  othersGroup: '使用者的个人应用',
  othersGroupHint: '使用者自己建的个人应用。管理员只看得到名称、拥有者和时间，可以归档，看不到页面、运行和蓝图的内容',
  owner: (who: string) => `拥有者：${who}`,
  ownerUnknown: '拥有者未记录',
  createdAt: (t: string) => `创建于 ${t}`,
  updatedAt: (t: string) => `更新于 ${t}`,
  stubTitle: (who: string) => `这是 ${who} 的个人应用`,
  stubBody: '个人应用只有拥有者看得见。管理员看得到名称、拥有者和时间，可以归档，但看不到页面、运行和蓝图的内容。',
  archive: '归档',
  archiveTitle: (name: string) => `归档「${name}」？`,
  archiveConsequences: [
    '拥有者的应用列表里不再列出它，不能再刷新或提交审核',
    '它的蓝图和以前的刷新记录照常保留，你看不到它们的内容',
    '目前没有恢复的操作',
  ],
  archiveConfirm: '归档个人应用',
  archiveDone: (name: string) => `已归档「${name}」`,
}

/** 审核待办（个人应用提交审核，N7）：处理人在待办里取一份共享副本，在副本上试运行、走发布单；不发布就退回 */
export const REVIEW_TASK_TEXT = {
  intro: '使用者把自己的个人应用交给你审核。同意不在这里点：取一份共享副本，在副本上用真数试一次、走发布单，发布之后这条待办自动记为已发布；原来的个人应用不会被改动。',
  submittedBy: (who: string) => `提交人：${who}`,
  purpose: '目的',
  note: '提交说明',
  copy: '取副本，去发布',
  copying: '正在取副本…',
  openCopy: '打开我的副本',
  reject: '退回',
  rejectHint: '不发布就退回：提交人会收到通知，他的个人应用照常可用',
  rejectNote: '退回的原因（选填）',
  rejected: '已退回',
}

// ---------------------------------------------------------------------------
// 访问密钥与服务账号（docs/design/next-round/04-对外集成与触达.md A1–A7；叫法定在总方案 X10：访问密钥，不叫令牌、token、
// 凭据——「token」专指模型用量，「凭据」专指出站端点的凭据）
// ---------------------------------------------------------------------------

export const ACCESS_KEY_TEXT = {
  tab: '访问密钥',
  hint: '外部系统和脚本用访问密钥调接口。访问密钥按使用者的权限算：只看得到授权给拥有者（或所在的组、所有人）的应用，看到的数据一律遮罩，管理员的密钥也一样',
  loginOff: '未开启登录，访问密钥不生效：关登录时所有请求都等同管理员，带着密钥也不会按密钥的权限收窄。开启登录后才能建密钥',
  viewKeys: '密钥',
  viewReference: '接口说明',
  personalSwitch: '允许个人访问密钥',
  personalSwitchHint: '打开后每个人可以在这里给自己建访问密钥（写脚本拉数用）。关掉时建不了，已有的个人访问密钥也一并不生效。系统间集成请用服务账号的密钥',
  personalSwitchOn: '已允许个人访问密钥',
  personalSwitchOff: '已关闭个人访问密钥',
  personalOff: '管理员还没有打开个人访问密钥。系统间集成请让管理员建服务账号的密钥',
  serviceAccounts: '服务账号',
  serviceHint: '给外部系统用的身份：用户名以 svc- 开头，没有密码、不能登录，角色固定是使用者。在应用的「岗位与授权」里给它授权；它不能被派待办。停用之后它的访问密钥全部失效',
  serviceEmpty: '还没有服务账号',
  createService: '新建服务账号',
  serviceUsername: '用户名',
  serviceUsernameHint: '以 svc- 开头，只用字母、数字、点、下划线和短横线，建好后不能修改',
  serviceDisplay: '显示名',
  serviceKeys: (n: number) => (n ? `${n} 把在用的密钥` : '没有在用的密钥'),
  serviceCreated: (name: string) => `已建服务账号「${name}」`,
  disabled: '已停用',
  disable: '停用',
  enable: '启用',
  disableTitle: (name: string) => `停用服务账号「${name}」？`,
  disableBody: '停用后它的访问密钥全部失效，用这些密钥的外部系统会收到 401。重新启用后没到期、没撤销的密钥就又能用。',
  myKeys: '我的访问密钥',
  allKeys: '全部访问密钥',
  keysEmpty: '还没有访问密钥',
  create: '新建访问密钥',
  owner: '拥有者',
  ownerMe: '我自己（个人访问密钥）',
  ownerService: (name: string) => `服务账号 ${name}`,
  noOwner: '还不能建：先打开个人访问密钥，或者建一个服务账号',
  name: '名称',
  nameHint: '写清是给哪个系统、哪个脚本用的',
  scopes: '权限',
  scopesHint: '新建时缺省只勾读的。发起和刷新时请求头里必须带幂等键；回复待办只给个人访问密钥',
  scopeLabel: {
    'apps.read': '读应用：列表、页面、视图数据、实例',
    'apps.run': '发起入口、刷新视图',
    'tasks.read': '读待办',
    'tasks.respond': '回复待办：只限审批、补充输入、编辑稿件',
    'reports.read': '读报告、证据和工件',
  } as Record<string, string>,
  serviceNoRespond: '服务账号不能被派待办，它的密钥不能回复待办',
  apps: '限定应用',
  appsHint: '不选就是拥有者有授权的全部应用。个人应用一律不能用访问密钥读',
  appsAll: '不限应用',
  appsCount: (n: number) => `限定 ${n} 个应用`,
  appsNone: '没有可选的应用',
  expires: '有效期（天）',
  expiresHint: (max: number) => `必填，最长 ${max} 天。到期前请轮换`,
  state: { active: '在用', revoked: '已撤销', expired: '已过期', owner_disabled: '拥有者已停用' } as Record<string, string>,
  personalTag: '个人',
  serviceTag: '服务账号',
  expiresAt: (t: string) => `到期 ${t}`,
  lastUsed: (t: string) => `最近使用 ${t}`,
  neverUsed: '还没用过',
  rotatedFrom: '由轮换签发',
  revoke: '撤销',
  revokeTitle: (name: string) => `撤销访问密钥「${name}」？`,
  revokeBody: '撤销后立刻失效，用它的外部系统会收到 401，不能恢复。',
  revoked: '已撤销',
  rotate: '轮换',
  rotateTitle: (name: string) => `轮换访问密钥「${name}」？`,
  rotateBody: '会签发一把新的密钥（权限、限定的应用、有效期长短照旧），旧的在 24 小时后到期。请在这段时间里把外部系统换成新密钥。',
  rotated: '已签发新密钥，旧密钥 24 小时后到期',
  created: '访问密钥已建好',
  onceTitle: (name: string) => `「${name}」的访问密钥`,
  onceBody: '密钥只显示这一次：关掉之后谁都看不到（服务端只存它的哈希）。请现在复制，交给要用它的系统。',
  onceUsage: '调接口时放在请求头里',
  done: '我已保存',
  requests: '最近的请求',
  hideRequests: '收起',
  requestsHint: (days: number) => `最近 100 条，保留 ${days} 天`,
  reference: '接口说明',
  referenceHint: '访问密钥能调的接口只有下面这些：这份说明和服务端放行的接口出自同一张表',
  refAuth: '认证方式',
  refBase: '对外访问地址',
  refBaseNone: '还没有设置对外访问地址（设置 → 通用）',
  refRate: (n: number) => `每把密钥每分钟最多 ${n} 次请求，超了回 429，按响应头 Retry-After 的秒数等一会再试`,
  refIdempotent: (header: string) => `发起和刷新必须在请求头里带 ${header}：同一件事重发时带同一个键，就不会多发起一次`,
  refCommon: '每个接口都可能回的错误码',
  refColMethod: '方法',
  refColPath: '路径',
  refColScope: '权限',
  refColSummary: '说明',
  refColErrors: '可能的错误码',
  refNeedsKey: '要幂等键',
  refImplicit: '不用申请',
}

/** 「最近的请求」（访问密钥的请求记录、钩子的收件记录共用一个组件，04 细案 A6） */
export const RECENT_REQUESTS_TEXT = {
  label: '最近的请求',
  empty: '还没有请求',
  run: '运行',
  instance: '实例',
  task: '待办',
  status: (code: number) => (code >= 200 && code < 300 ? `成功 ${code}` : `被拒 ${code}`),
}

/** 钩子的收件记录（应用编辑 → 钩子） */
export const HOOK_RECEIPT_TEXT = {
  show: '收件记录',
  hide: '收起收件记录',
  title: (name: string) => `钩子「${name}」最近收到的请求`,
  status: { processing: '处理中', accepted: '已接受', rejected: '被拒' } as Record<string, string>,
  empty: '还没有收到过验过签名的请求',
  idem: '幂等键',
  failures: (n: number) => (n
    ? `近 1 小时验签失败 ${n} 次：验签失败的请求不留记录，只计数`
    : '近 1 小时没有验签失败的请求'),
}

/**
 * 邮件（通知渠道的第五种格式，服务端 notify/mail.py，04 细案 B2）：设置 → 通知里的「邮件服务」「每天的待办汇总」两节、
 * 渠道里的邮件格式。照实说：成功只写「已交给邮件服务器」，退信不追踪，不说已送达
 */
export const MAIL_TEXT = {
  section: '邮件服务',
  hint: '通知渠道选「邮件」时用这台邮件服务器发信。口令加密保存，之后只显示「已设置」',
  readOnly: '只有管理员能修改邮件服务',
  enabled: '启用邮件',
  enabledHint: '关着时通知渠道里没有「邮件」这一格式；已有的邮件渠道发不出去，投递记录里写明原因',
  host: 'SMTP 服务器',
  hostHint: '主机名或 IP 地址，例如 smtp.example.com，不带协议和端口',
  port: '端口',
  security: '加密方式',
  securityLabel: { starttls: 'STARTTLS（常用端口 587）', ssl: 'SSL/TLS（常用端口 465）', none: '不加密（只在内网中继时用）' } as Record<string, string>,
  username: '用户名',
  usernameHint: '服务器不要求登录时留空',
  password: '口令',
  passwordSet: '已设置；留空表示不修改',
  passwordUnset: '未设置',
  passwordClear: '清除已保存的口令',
  fromAddr: '发件地址',
  fromName: '发件人名称',
  defaultAssignees: '默认发邮件给待办处理人',
  defaultAssigneesHint: '打开后，第一次有待办派给某人时，自动给他建一个个人邮件渠道（订阅待办打开、动作结果未知、审核有结果），发到他的账号邮箱；本人之后可以改订阅或关掉、删掉。缺省关：刚配好邮件服务时不会突然往外发邮件',
  save: '保存邮件服务',
  saved: '已保存邮件服务设置',
  invalidPort: '请填写 1 到 65535 之间的整数',
  leaveBody: '邮件服务有没保存的改动，离开将丢失这些改动。',
  test: '发测试邮件',
  testHint: '按已保存的设置发一封到你自己的账号邮箱',
  testDirty: '先保存再发测试邮件',
  handoff: '已交给邮件服务器',
  handoffHint: '「已交给邮件服务器」不等于已经进了收件箱：退信不追踪，是否送达以邮件服务器为准',
  // 每天的待办汇总（服务端 notify/digest.py）
  digest: '每天的待办汇总',
  digestHint: '每天这个时间（按服务端所在时区）给订阅了「每天的待办汇总」的个人渠道发一条：还有几条待办、最早的时限、最多 5 条和去待办箱的链接。没有待办的人那天不发',
  digestAt: '发送时间',
  digestSave: '保存发送时间',
  digestSaved: (at: string) => `每天 ${at} 发待办汇总`,
  digestReadOnly: '只有管理员能修改发送时间',
  // 渠道里的邮件格式
  personalTo: (email: string | null) => (email ? `发到账号邮箱 ${email}` : '你的账号没有邮箱地址，发不出去：请联系管理员在「设置 → 身份」里填写'),
  personalToHint: '个人邮件渠道只发到你的账号邮箱，不能填别的地址',
  recipients: '收件地址',
  recipientsHint: '多个地址用逗号或换行分隔，最多 20 个；人多的请填邮件组的地址',
  to: (list: string[]) => (list.length
    ? `发到 ${list.slice(0, 2).join('、')}${list.length > 2 ? ` 等 ${list.length} 个地址` : ''}` : ''),
  off: '邮件服务没有启用，暂时不能选邮件：请管理员在下面的「邮件服务」里配置并启用',
}

/**
 * 分享链接（服务端 api/shares.py，04 细案 C2）：应用编辑 → 岗位与授权里的管理。叫「分享链接」，不叫快照的那种说法：
 * 「快照」专指查询快照
 */
export const SHARE_TEXT = {
  section: '分享链接',
  hint: '拿到链接的人不用登录就能看这一页：只读，只显示定时出数的视图，看到的数据和使用者一样遮过；出处给「下载报告」。必须设到期，随时可以撤销',
  off: '管理员还没有打开分享链接：在「设置 → 偏好设置」的「应用」一节里打开「允许建分享链接」后才能建',
  offWithRows: '管理员已关闭分享链接：下面这些暂时打不开，仍然可以撤销',
  personal: '个人应用只给你自己用，不能分享',
  unpublished: '发布之后才能建分享链接',
  create: '新建分享链接',
  empty: '还没有分享链接',
  page: '分享哪一页',
  pageHint: '只显示这一页上定时出数的视图；入口、表单、实例、待办和报告正文在分享页上换成说明',
  mode: '数据',
  modeLatest: '始终最新：跟着最近一次封存走',
  modeFixed: '固定在现在这一期：之后出的数不换上来',
  modeShort: { latest: '始终最新', fixed: '固定这一期' } as Record<string, string>,
  expires: '有效期',
  expiresDays: (n: number) => `${n} 天`,
  expiresHint: (max: number) => `必须设到期，最长 ${max} 天`,
  embed: '允许嵌入到别的站点',
  embedHint: '勾上之后这条链接能放进门户的 iframe（只限部署配置里允许嵌入分享页的站点）；不勾的在别的页面里不显示内容',
  embedNoOrigins: '部署没有配置允许嵌入分享页的站点（AGENTLAB_EMBED_ORIGINS），勾了也只能在 AgentLab 自己的页面里嵌',
  submit: '建分享链接',
  created: '分享链接已建好',
  once: '链接只显示这一次，请现在复制下来；之后只看得到开头几位',
  dismiss: '我已复制',
  state: { active: '有效', revoked: '已撤销', expired: '已到期' } as Record<string, string>,
  embedded: '可嵌入',
  opened: (n: number) => (n ? `打开过 ${n} 次` : '还没打开过'),
  by: (who: string) => `由 ${who} 建`,
  until: (at: string) => `${at} 到期`,
  revoke: '撤销',
  revokeTitle: (prefix: string) => `撤销分享链接 ${prefix}…？`,
  revokeBody: '撤销之后，拿着这条链接的人打开会看到「链接已撤销」，不能恢复；要再分享请建一条新的。',
  revoked: '已撤销分享链接',
}

/** 分享页（/s，不在外壳里）：拿到分享链接的人看到的页面 */
export const SHARE_PAGE_TEXT = {
  loading: '正在打开分享的页面…',
  readOnly: '只读',
  footer: (who: string, at: string, until: string) => `只读 · 由 ${who} 于 ${at} 分享 · ${until} 到期`,
  someone: '管理应用的人',
  fixed: '固定在分享时的那一期',
  latest: '跟着最近一次封存更新',
  stamps: '数据的来历',
  stampsHint: '每个视图取的是哪一次运行、截至什么时候、按哪个口径、出具档位',
  download: '下载报告',
  downloadHint: '下载的报告里每个数都点得开出处（口径、来源、计算方式），离线也能看',
  downloading: '正在下载…',
  downloaded: '已下载报告',
  noReports: '这一页的数据没有可下载的报告',
  evidenceTitle: '出处在下载的报告里',
  evidence: '分享页不打开在线出处：点下面的「下载报告」，下载的报告里每个数都点得开出处，离线也能看',
  close: '知道了',
  noKey: '链接不完整：缺少 # 后面的那一段，请向分享的人确认链接',
  errorTitle: {
    share_revoked: '链接已撤销', share_expired: '链接已到期', share_links_off: '分享链接已关闭',
    share_gone: '分享的页面打不开了', share_not_found: '链接无效', share_key_missing: '链接不完整',
  } as Record<string, string>,
  failed: '分享的页面打不开',
  framed: '这条分享链接没有允许嵌入到别的页面，请直接打开链接查看',
  openDirect: '在新窗口打开',
}
