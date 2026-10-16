/**
 * 数据源的变化（结构变化、取值变化）、表详情的「结构与取值变化」一节、定时维护、目录页的后台任务条的文案（next-round 03 B2–B4、
 * 05 (g)）。规矩同 terms.ts 开头的术语表。
 *
 * 叫法（总方案 X10）：「结构变化」「取值变化」，和已经在用的「目录有变化」（目录版本变了，CATALOG_DRIFT_TEXT）分开；严重程度
 * 「要处理 / 提醒 / 说明」同 SCHEMA_DRIFT_TEXT.severity。
 *
 * terms.ts 末尾 `export * from './termsSource'`：调用方照旧从 lib/terms 导入。
 */
import type { MaintenanceStep, SourceChangeAction, SourceChangeCategory, SourceChangeSeverity, SourceChangeStatus } from '../typesSource'
import type { ObservationKind, SourceJobKind } from '../typesSource'
import { formatNumber } from './format'

const pct = (share: number) => {
  const v = share * 100
  return `${v >= 10 || v === 0 ? Math.round(v) : v.toFixed(1)}%`
}

/**
 * 变化的种类：和服务端 app/data/source_changes.KIND_LABEL 逐字一致（tests/test_source_change_terms.py 守着），服务端给人看
 * 的一句（summary）也用它
 */
export const SOURCE_CHANGE_KIND_LABEL: Record<string, string> = {
  table_added: '新出现的表', table_removed: '表已删除', table_left_scope: '表离开了探查范围',
  table_reflect_failed: '这次读不到表结构', column_added: '新加了列', column_removed: '删除了列',
  column_type_changed: '列的类型变了', column_nullable_changed: '列的可空变了', key_changed: '键变了', fk_changed: '外键变了',
  comment_changed: '注释变了', scope_changed: '探查范围的规则变了',
  codes_new: '出现了新码值', codes_shift: '码值的比例变化大', date_stalled: '最新业务日期没有前进',
  date_gap: '有的日子没有数据', volume_drop: '按日行数大幅下跌', rows_drop: '行数大幅下跌', rows_jump: '行数大幅上涨',
  coverage_drop: '关联的覆盖率下降',
}

export const SOURCE_CHANGE_TEXT = {
  /** 数据目录页左栏的第三个视图 */
  view: '变化',
  viewHint: '探查、取值观察测到的结构变化和取值变化，按要不要处理分组',
  category: { struct: '结构变化', value: '取值变化' } as Record<SourceChangeCategory, string>,
  categoryAll: '全部',
  categoryLabel: '按类别筛选',
  severity: { action: '要处理', warning: '提醒', info: '说明' } as Record<SourceChangeSeverity, string>,
  severityHint: {
    action: '现在大概率已经不对了：已确认的目录项或写死的 SQL 指向了被删的列，或者「已列全」被撤销了',
    warning: '值得核对：可能影响 SQL 的写法或结果',
    info: '知道一下：一般不影响已有的目录和模板',
  } as Record<SourceChangeSeverity, string>,
  status: { open: '开着', acknowledged: '知道了，还没处理', resolved: '已处理', dismissed: '标为误报' } as Record<SourceChangeStatus, string>,
  statusFilter: { pending: '开着的', handled: '处理过的' } as Record<'pending' | 'handled', string>,
  statusLabel: '按状态筛选',
  listLabel: '数据源的变化',
  loading: '正在载入…',
  refresh: '刷新',
  more: (n: number) => `再显示 ${formatNumber(n)} 条`,
  total: (n: number) => `${formatNumber(n)} 条`,
  group: (label: string, n: number) => `${label} ${formatNumber(n)} 条`,
  empty: '没有开着的变化',
  emptyBody: '探查结构、取值观察测到表结构或取值有变化时，会列在这里。',
  emptyFiltered: '没有符合筛选的变化',
  emptyHandled: '还没有处理过的变化',
  /** 视图标签上的计数：要处理的条数 */
  badge: (n: number) => `${formatNumber(n)} 条要处理`,
  seen: (n: number) => `测到 ${formatNumber(n)} 次`,
  detectedAt: (when: string) => `测到于 ${when}`,
  overviewTitle: '从左侧挑一条变化开始处理',
  overviewBody: '要处理的排在最前：已确认的目录项、写死的 SQL 说到了被删的列，或者「已列全」被撤销了。点开一条，右侧写依据、影响面和能做的处理；每个处理都记下谁、什么时候、目录第几版。',
  backToList: '返回变化列表',
  missing: '这条变化不存在，可能已随数据源删除',
  openTable: '查看这张表',
  // ---- 依据
  basisTitle: '依据',
  detectedBy: (job: string, trigger: string, actor: string | null) =>
    `${trigger}的${job}${actor ? `（${actor}）` : ''}`,
  jobKind: { introspect: '探查结构', profile: '取值观察', on_demand: '按需加入' } as Record<string, string>,
  trigger: { manual: '手动发起', schedule: '定时维护', on_demand: '助手点名' } as Record<string, string>,
  structBasis: (before: number | null, after: number | null) => (before && after
    ? `比对的是这张表的结构第 ${formatNumber(before)} 版和第 ${formatNumber(after)} 版`
    : before ? `比对的是这张表的结构第 ${formatNumber(before)} 版和这次探查读到的` : '比对的是上一次和这次探查读到的结构'),
  valueBasis: (text: string) => `观察口径：${text}`,
  valueBasisNote: '只和口径相同的历史观察比',
  // ---- 前后各是什么（按种类）
  factsTitle: '前后',
  before: '之前',
  after: '现在',
  none: '（无）',
  nullable: (v: boolean) => (v ? '可空' : '不可空'),
  rename: (from: string, to: string) => `可能是 ${from} 改名为 ${to}（未确认：库里只看得到删了一列、加了一列）`,
  newCodes: (codes: string) => `新出现的码值：${codes}`,
  codeShare: (code: string, share: number) => `${code}（占 ${pct(share)}）`,
  knownCodes: (n: number) => `目录里列出的 ${formatNumber(n)} 个取值之外`,
  completeRevoked: (version: number | null) =>
    `取值观察撤销了码值项的「已列全」${version ? `（目录第 ${formatNumber(version)} 版）` : ''}：SQL 检查不再按旧的码值表提示，新取值的含义要人来填`,
  completeKept: '「已列全」在观察之前已被别人改掉，没有撤销',
  shift: (psi: number) => `比例的变化程度（PSI）${psi.toFixed(2)}`,
  shiftTop: (code: string, from: number, to: number) => `${code}：${pct(from)} → ${pct(to)}`,
  stalled: (max: string, age: number, threshold: number) =>
    `最新业务日期停在 ${max}，已经 ${formatNumber(age)} 天（以前通常 ${formatNumber(threshold)} 天之内会前进）`,
  gap: (n: number, days: string) => `窗口里有 ${formatNumber(n)} 天没有数据：${days}`,
  volume: (day: string, rows: number, median: number) =>
    `${day} 只有 ${formatNumber(rows)} 行，前 28 天的中位数是 ${formatNumber(median)} 行`,
  rows: (before: number, after: number, estimated: boolean) =>
    `${formatNumber(before)} 行 → ${formatNumber(after)} 行${estimated ? '（数据库统计的估算值，连续两次都这样才报）' : ''}`,
  coverage: (target: string, before: number, after: number) => `指向 ${target} 的关联覆盖率 ${pct(before)} → ${pct(after)}`,
  scopeRules: (entered: number, left: number) => `这次探查新进范围 ${formatNumber(entered)} 张，离开范围 ${formatNumber(left)} 张`,
  reflectFailed: (error: string) => `读不到结构的原因：${error}`,
  // ---- 影响面
  impactTitle: '影响面',
  impactNone: '目录里没有说到它的项，已发布模板里也没有用到它的。',
  impactCatalog: (n: number) => `说到这些列的目录项 ${formatNumber(n)} 项`,
  impactNow: '现在',
  impactGone: '已不在目录里',
  impactNeeds: '人工确认过、还说着被删的列',
  impactRules: '有效记录条件用到了这一列：取值有变时，按它过滤的结果可能漏掉新状态的行',
  impactTemplates: (n: number) => `用到这张表的已发布模板 ${formatNumber(n)} 个`,
  templateImpact: { direct: '写死的 SQL 直接引用', possible: '可能涉及' } as Record<string, string>,
  templateVersion: (v: number) => `v${v}`,
  breaks: (cols: string[]) => (cols.length
    ? `这条 SQL 现在会报列不存在（${cols.join('、')}）`
    : '这条 SQL 现在会报表或列不存在'),
  // ---- 处理
  actionsTitle: '处理',
  action: {
    clean_catalog: '清理目录项', add_codes: '加入码值', redraft: '重新起草这张表', reprofile: '重新剖析这张表',
    acknowledge: '知道了', dismiss: '误报',
  } as Record<SourceChangeAction, string>,
  actionHint: {
    clean_catalog: '把影响面里说到这些列的目录项从目录里去掉（目录升一版，记下是因为这条变化）',
    add_codes: '把新出现的码值加进这一列的码值表，含义可以先空着',
    redraft: '建一个只含这张表的起草任务：按新的结构重写推断项，人工确认过的项不动',
    reprofile: '建一个只含这张表的剖析任务：对业务库发只读查询，重新核对关系、码值和日期',
    acknowledge: '记下已经看过，还留在开着的变化里',
    dismiss: '不是真的变化：写明理由后不再算要处理',
  } as Record<SourceChangeAction, string>,
  cleanTitle: (n: number) => `清理 ${formatNumber(n)} 项目录？`,
  cleanConsequences: ['这些项从数据目录里去掉，目录升一版；从已发布版本发起的正式运行会提醒「数据目录有变化」',
    '人工确认过的项也会去掉：它们说的列已经不在了', '去掉之后要恢复，只能重新填写'],
  cleanConfirm: '清理',
  codesTitle: '加入码值',
  codesHint: '含义可以先空着，之后在表详情的码值表里补；加入的项记为人工填写、已确认，依据写这条变化的编号',
  codesMeaning: (code: string) => `码值 ${code} 的含义`,
  codesSubmit: '加入',
  noteLabel: '误报的理由',
  notePlaceholder: '例如：测试库里故意删的列',
  noteRequired: '标为误报要写明理由',
  noteSubmit: '标为误报',
  cancel: '取消',
  done: {
    clean_catalog: '已清理目录项', add_codes: '已加入码值', redraft: '已建起草任务', reprofile: '已建剖析任务',
    acknowledge: '已记为知道了', dismiss: '已标为误报',
  } as Record<SourceChangeAction, string>,
  // ---- 处理过的结果
  resolutionTitle: '处理结果',
  resolvedBy: (action: string, by: string | null, when: string) => `${action} · ${by ?? '未署名'} · ${when}`,
  resolvedVersion: (v: number) => `目录第 ${formatNumber(v)} 版`,
  resolvedVersions: (pairs: string) => `各表的目录版本：${pairs}`,
  resolvedNote: (note: string) => `理由：${note}`,
  resolvedJob: { draft: '起草任务', profile: '剖析任务' } as Record<string, string>,
  jobLink: (kind: string, id: string) => `${kind} #${id.slice(0, 6)}`,
  viewProgress: '查看进度',
  /** 不能做的动作的读屏说明：「重新剖析这张表：这个数据源未开启数据剖析」 */
  unavailable: (label: string, reason: string) => `${label}：${reason}`,
}

/** 数据源卡片上的角标：要处理的变化条数（读屏写全） */
export const SOURCE_BADGE_TEXT = {
  chip: (n: number) => `${formatNumber(n)} 个变化要处理`,
  aria: (name: string, n: number) => `数据源「${name}」有 ${formatNumber(n)} 个变化要处理，打开数据目录的「变化」`,
  hint: '探查结构、取值观察测到的变化里要处理的条数，点开到数据目录的「变化」处理',
}

/** 表详情的「结构与取值变化」一节 */
export const TABLE_CHANGES_TEXT = {
  title: '结构与取值变化',
  hint: '这张表的结构变化、取值变化，和最近几次取值观察',
  changesHead: '这张表的变化',
  changesNone: '这张表还没有测到结构变化或取值变化。',
  changesMore: (n: number) => `…另有 ${formatNumber(n)} 条，在「变化」里查看`,
  openChange: '去处理',
  viewChange: '查看',
  observeHead: '最近几次观察',
  observeNone: '这张表还没有取值观察：定时维护选了「取值观察」，或者手动剖析时带上观察，才会有。',
  observeKind: { rows: '行数', codes_observe: '码值分布', freshness: '最新业务日期', daily_volume: '按日行数' } as Record<ObservationKind, string>,
  basis: (text: string) => `口径：${text}`,
  basisChanged: '这几次的口径不全相同：口径不同的不互相比较',
  at: (when: string) => `观察于 ${when}`,
  rows: (rows: number | null, atLeast: boolean) => (rows == null ? '—' : `${atLeast ? '至少 ' : ''}${formatNumber(rows)} 行`),
  codesTop: (n: number, rows: number) => `${formatNumber(n)} 个取值，共 ${formatNumber(rows)} 行`,
  codesTruncated: '取值太多，只记了最多的 50 个：不判新码值、不算比例',
  code: (code: string, share: number) => `${code} ${pct(share)}`,
  freshness: (max: string | null) => max ?? '—',
  daily: (days: number, total: number) => `有数据的 ${formatNumber(days)} 天，共 ${formatNumber(total)} 行`,
  dailyRange: (since: string, until: string) => `${since} 至 ${until}`,
  dailyChart: (since: string, until: string, days: number, total: number) =>
    `${since} 至 ${until} 每天的行数：有数据的 ${formatNumber(days)} 天，共 ${formatNumber(total)} 行`,
  history: (n: number) => `前 ${formatNumber(n)} 次`,
  revokedHead: '撤销过「已列全」',
  revoked: (column: string, when: string, version: number | null) =>
    `${when}：取值观察在 ${column} 上测到目录之外的取值，撤销了「已列全」${version ? `（目录第 ${formatNumber(version)} 版）` : ''}`,
}

/** 目录页的后台任务条（05 (g)）：页头只做入口，进度只在这一条上 */
export const JOB_STRIP_TEXT = {
  label: '这个数据源的后台任务',
  kind: { introspect: '探查', draft: '起草', profile: '剖析' } as Record<SourceJobKind, string>,
  running: (kind: string, done: number, total: number) => `${kind} ${formatNumber(done)}/${formatNumber(total)}`,
  queued: (kind: string) => `${kind} 排队中`,
  queuedBehind: (kind: string, behind: string) => `${kind} 排队中，${behind}结束后开始`,
  behind: { introspect: '探查', draft: '起草', profile: '剖析', import: '导入目录', sync_draft: '起草' } as Record<string, string>,
  progress: (kind: string, done: number, total: number) => `${kind}：已完成 ${formatNumber(done)} / ${formatNumber(total)} 张表`,
  view: '查看',
  viewHint: { introspect: '到数据源卡片上看探查的进度、停止', draft: '打开起草的进度', profile: '打开剖析的进度' } as Record<SourceJobKind, string>,
  /** 页头的入口：读屏名称写状态，不写数字 */
  headRunning: (label: string) => `${label}（进行中）`,
  headQueued: (label: string) => `${label}（排队中）`,
}

/** 数据源的定时维护（数据源卡片上的「定时维护」） */
export const MAINTENANCE_TEXT = {
  open: '定时维护',
  openHint: '按设定的时段自动探查结构、剖析需要的表、观察取值，缺省关闭',
  title: (name: string) => `「${name}」的定时维护`,
  intro: '到点按顺序探查结构 → 剖析需要的表 → 观察取值，和起草、剖析在同一条队里排队，一个接一个做。只发只读查询，每张表的查询次数和时长按剖析设置。',
  enable: '开启定时维护',
  enableHint: '缺省关闭：开启后会定期对业务库发只读查询，请选业务库的低峰时段（比如凌晨）',
  offPeak: '请选业务库的低峰时段（比如凌晨），避开白天的业务高峰',
  stepsTitle: '做哪几步',
  stepsHint: '至少选一步',
  step: { introspect: '探查结构', profile_needed: '剖析需要的表', observe: '取值观察' } as Record<MaintenanceStep, string>,
  stepHint: {
    introspect: '读取探查范围内的表结构，前后比出结构变化',
    profile_needed: '剖析从没剖析过、上次没做完、目录里多出待核实项的表',
    observe: '观察码值分布、最新业务日期、按日行数和行数，和同口径的历史比出取值变化',
  } as Record<MaintenanceStep, string>,
  stepsNone: '至少选一步',
  profileOff: '这个数据源未开启数据剖析：到点时「剖析需要的表」「取值观察」两步不执行，只探查结构（照实记下）。剖析在数据源的「编辑」里开启。',
  schedulerOff: '这个服务的定时调度没有开启：到点不会自动维护。',
  freqTitle: '时段',
  scheduleName: (name: string) => `「${name}」定时维护`,
  next: (when: string) => `下一次：${when}`,
  nextNone: '已暂停，不会触发',
  lastTitle: '最近一次',
  lastNone: '还没有触发过',
  lastAt: (when: string, status: string) => `${when} · ${status}`,
  fireStatus: { started: '已开始', skipped_overlap: '已跳过（上一次还没做完）', skipped_misfire: '已跳过（错过了时间）', failed_to_start: '没有执行' } as Record<string, string>,
  lastJob: (step: string, kind: string, id: string, tables: number | null) =>
    `${step}：${kind} #${id.slice(0, 6)}${tables != null ? `（${formatNumber(tables)} 张表）` : ''}`,
  lastJobKind: { introspect: '探查任务', profile: '剖析任务' } as Record<string, string>,
  lastSkipped: (step: string, reason: string) => `${step}没有执行：${reason}`,
  lastReason: (reason: string) => `原因：${reason}`,
  viewJob: '查看进度',
  history: '在定时任务页查看触发记录',
  extra: (n: number) => `另有 ${formatNumber(n)} 条指向这个数据源的维护定时，在定时任务页管理`,
  save: '保存',
  cancel: '取消',
  saving: '正在保存…',
  saved: '已保存定时维护',
  savedOff: '已保存：定时维护已关闭',
  nothing: '没有改动',
  notMaintained: '上传的表格、记录表的结构随版本或表单走，不需要定时维护',
  /** 卡片上的标签：开着时写频率 */
  chip: (desc: string) => `定时维护 · ${desc}`,
  chipPaused: '定时维护 · 已暂停',
  chipHint: '到点自动探查结构、剖析需要的表、观察取值',
  loadFailed: '无法载入定时维护的设置',
}
