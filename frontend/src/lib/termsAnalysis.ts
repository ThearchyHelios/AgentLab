/**
 * 分析能力一轮（docs/design/analysis-round）的界面文案：分析卡的检查器和卡片、问数据时间线上的分析一步、证据面板的分析一步与
 * 口径卡指标上的可比性、正文里的说法违规与系统写的「分析范围」、出具声明里的三项、设置「分析」一节、运行对比的分析卡一节。
 *
 * 叫法只有服务端契约一份（backend/app/analysis/contract.py 的 OP_LABELS / MODE_LABELS / LABELS，engine/labels.py 的分析卡字段与
 * 报告节点两个新字段引用它们）：这里的 ANALYSIS_OP_LABEL、ANALYSIS_MODE_LABEL、ANALYSIS_SETTING_LABEL、ANALYSIS_FIELD_LABEL 和它们
 * 一字不差（scripts/check-studio、check-manage 读后端源码逐项比对）。判定语、判定标准、算不了的原因、附带说明、代入式、闭合核对
 * 的数、说法违规的原话都是服务端写好的，界面照原样显示，不按判定码、原因码自己拼。规矩同 terms.ts 开头的术语表。
 *
 * 术语：一次分析的结果叫「分析结论」，编号 A1、A2……；系统写的那一句叫「判定语」；判定的依据叫「判定标准」；做不成的叫「没有做成」
 * （服务端判定语的写法），原因叫「算不了的原因」；写错的调用叫「调用有误」，不记成结论；Agent 写的那句叫「Agent 写的假设」。
 */
import { formatNumber } from './format'

/** 五个算子（contract.OPS），按契约的先后 */
export const ANALYSIS_OPS = ['decompose', 'baseline', 'trend', 'differ', 'comparable'] as const
export type AnalysisOp = (typeof ANALYSIS_OPS)[number]

/** 算子的叫法：contract.OP_LABELS */
export const ANALYSIS_OP_LABEL: Record<AnalysisOp, string> = {
  decompose: '变化拆解', baseline: '波动基线', trend: '趋势判断', differ: '差异检验', comparable: '可比性检查',
}

/** 模式的叫法：contract.MODE_LABELS（比率拆解和比率比较都叫「比率」） */
export const ANALYSIS_MODE_LABEL: Record<string, string> = {
  additive: '可加', ratio: '比率', factor: '乘积', rate: '比率', mean: '均值', rows: '逐行明细',
}

/** 有模式的算子和它们的模式：contract.MODES。其余算子没有模式 */
export const ANALYSIS_MODES: Partial<Record<AnalysisOp, readonly string[]>> = {
  decompose: ['additive', 'ratio', 'factor'],
  differ: ['rate', 'mean', 'rows'],
}

export const isAnalysisOp = (op: unknown): op is AnalysisOp => typeof op === 'string' && (ANALYSIS_OPS as readonly string[]).includes(op)

/**
 * 「变化拆解（可加）」「波动基线」：同 contract.method_label。认不出的算子、模式原样写出来，不编叫法。证据面板、导出页用服务端
 * 给的 method_label；事件里只有算子和模式的（时间线、运行对比）照这一份拼
 */
export function analysisMethodLabel(op: unknown, mode?: unknown): string {
  const name = isAnalysisOp(op) ? ANALYSIS_OP_LABEL[op] : String(op ?? '')
  if (mode == null || mode === '') return name
  return `${name}（${ANALYSIS_MODE_LABEL[String(mode)] ?? String(mode)}）`
}

/** Agent 分析工具的名字（contract.TOOL_NAMES：analysis_<算子>）→ 算子；不是分析工具为 null */
export function analysisToolOp(tool: unknown): AnalysisOp | null {
  const m = typeof tool === 'string' ? /^analysis_([a-z]+)$/.exec(tool.trim()) : null
  return m && isAnalysisOp(m[1]) ? m[1] : null
}

/** 分析卡、报告节点新字段的叫法：engine/labels.py 的 _FIELD_BY_TYPE['analysis'] 与 FIELD_LABEL */
export const ANALYSIS_FIELD_LABEL = {
  items: '分析项',
  overrides: '阈值覆盖',
  analysis_claims: '分析说法核对',
  analysis_scope: '分析范围',
} as const

/** 报告节点两个新字段取值的短说法：engine/labels.py 的 _OPTION_LABEL（下拉的选项以它开头，后面可以带一句用途） */
export const ANALYSIS_OPTION_LABEL = {
  analysis_claims: { off: '不核对', check: '重写一次后标出', strict: '重写后仍有的计入缺口' },
  analysis_scope: { auto: '有分析结论时附上', off: '不附' },
} as const

/** 分析设置的键（contract.DEFAULTS 的先后） */
export const ANALYSIS_SETTING_KEYS = [
  'main_share', 'concentration', 'offset_ratio', 'flat_pct', 'k_warn', 'k_alert', 'min_history', 'max_history',
  'trend_min_points', 'trend_alpha', 'differ_alpha', 'min_n', 'min_expected', 'min_denominator', 'max_calls',
  'max_drill_depth', 'table_top_k',
] as const
export type AnalysisSettingKey = (typeof ANALYSIS_SETTING_KEYS)[number]

/** 每一项的叫法：contract.LABELS（设置页、阈值覆盖、服务端报错同一套） */
export const ANALYSIS_SETTING_LABEL: Record<AnalysisSettingKey, string> = {
  main_share: '「主要来源」的占比',
  concentration: '「集中」的合计占比',
  offset_ratio: '记为「抵消」的比例',
  flat_pct: '「基本持平」的幅度',
  k_warn: '「超出常规波动」的倍数',
  k_alert: '「明显超出」的倍数',
  min_history: '至少几期历史',
  max_history: '最多用几期历史',
  trend_min_points: '趋势至少几期',
  trend_alpha: '趋势的显著性水平',
  differ_alpha: '差异检验的显著性水平',
  min_n: '均值比较的最小样本数',
  min_expected: '比率比较的最小期望频数',
  min_denominator: '比率拆解的最小分母',
  max_calls: 'Agent 每次执行最多分析几次',
  max_drill_depth: '最多下钻几层',
  table_top_k: '列出前几段',
}

/** 每一项的类型和闭区间：contract.RANGES（整数项只收整数）。界面先按它说哪一项不对，两两之间的大小交给服务端核 */
export const ANALYSIS_SETTING_RANGE: Record<AnalysisSettingKey, readonly ['float' | 'int', number, number]> = {
  main_share: ['float', 0.3, 0.9],
  concentration: ['float', 0.5, 0.99],
  offset_ratio: ['float', 0.05, 1.0],
  flat_pct: ['float', 0.0, 0.1],
  k_warn: ['float', 1.0, 5.0],
  k_alert: ['float', 1.5, 8.0],
  min_history: ['int', 4, 104],
  max_history: ['int', 8, 260],
  trend_min_points: ['int', 4, 104],
  trend_alpha: ['float', 0.001, 0.2],
  differ_alpha: ['float', 0.001, 0.2],
  min_n: ['int', 2, 10_000],
  min_expected: ['int', 1, 50],
  min_denominator: ['int', 1, 100_000],
  max_calls: ['int', 1, 50],
  max_drill_depth: ['int', 1, 6],
  table_top_k: ['int', 3, 30],
}

/** 设置页把这几项分成几组（只是排版，键和叫法照上面） */
export const ANALYSIS_SETTING_GROUPS: { title: string; keys: AnalysisSettingKey[] }[] = [
  { title: '变化拆解', keys: ['main_share', 'concentration', 'offset_ratio', 'flat_pct', 'min_denominator', 'table_top_k'] },
  { title: '波动基线与趋势判断', keys: ['k_warn', 'k_alert', 'min_history', 'max_history', 'trend_min_points', 'trend_alpha'] },
  { title: '差异检验', keys: ['differ_alpha', 'min_n', 'min_expected'] },
  { title: '次数与层数', keys: ['max_calls', 'max_drill_depth'] },
]

/** 范围写成人话：「0.3 到 0.9 之间」「4 到 104 之间的整数」 */
export function settingRangeText(key: AnalysisSettingKey): string {
  const [kind, lo, hi] = ANALYSIS_SETTING_RANGE[key]
  return kind === 'int' ? `${formatNumber(lo)} 到 ${formatNumber(hi)} 之间的整数` : `${lo} 到 ${hi} 之间`
}

/** 一项填的值有什么问题（类型、范围），没问题为 null。说法同服务端 contract.settings_problem */
export function settingValueProblem(key: AnalysisSettingKey, raw: string): string | null {
  const text = raw.trim()
  const [kind, lo, hi] = ANALYSIS_SETTING_RANGE[key]
  const label = ANALYSIS_SETTING_LABEL[key]
  if (!text || !Number.isFinite(Number(text))) return `${label}要填数字`
  const n = Number(text)
  if (kind === 'int' && !/^-?\d+$/.test(text)) return `${label}要填整数`
  if (n < lo || n > hi) return `${label}要在 ${lo} 到 ${hi} 之间`
  return null
}

export const ANALYSIS_TEXT = {
  /** 记录页工件清单里分析结论那一行的说明（工件没记算子时） */
  artifactHint: '分析结论：判定、方法、计算、输入和复算所需的一切',
  // ---- 分析卡：节点库、检查器、卡片 ----
  node: {
    description: '对上游查询节点的结果固定做几项确定性分析：变化拆解、波动基线、趋势判断、差异检验、可比性检查。判定由平台按设置的阈值算出，报告可引用、可查看出处',
    itemsHint: '每一项做一种分析：选分析方法、要分析的上游查询节点，再按方法填列名和期次。判定语由系统按平台的方法写好，报告引用它，不另写判断',
    overridesHint: '只对这张卡生效，写进工作流、随版本固定；未覆盖的项跟随「设置 → 偏好设置 → 分析」。Agent 的分析工具没有阈值覆盖',
    addItem: '添加分析项',
    removeItem: (id: string) => `删除分析项 ${id || '（未命名）'}`,
    id: '标识',
    idHint: (node: string, id: string) => `下游条件分支按 nodes.${node}.results.${id || '<标识>'}.verdict 取判定`,
    idInvalid: '标识只能用字母、数字和下划线，且不以数字开头',
    idDuplicate: (id: string) => `标识「${id}」与另一项重复，请换一个`,
    op: '分析方法',
    pickOp: '选择分析方法',
    mode: '模式',
    pickMode: '选择模式',
    unknownOp: (op: string) => `${op}：无法识别的分析方法，请从上方选项中选择`,
    unknownMode: (mode: string) => `${mode}：无法识别的模式，请从上方选项中选择`,
    from: '输入',
    pickFrom: '选择上游的查询节点',
    noUpstream: '上游没有查询节点。请先在本节点之前连接选择了数据库查询工具的「调用工具」节点，或「合并查询」节点',
    missingNode: (id: string) => `节点「${id}」不在上游或已删除，请重新选择`,
    twoQueries: '基期、本期是两份查询',
    baseQuery: '基期查询',
    targetQuery: '本期查询',
    hypothesis: '在验证什么',
    hypothesisHint: '一句话，只在证据面板显示，不进报告正文',
    hypothesisPlaceholder: '下降是不是集中在某个地区',
    params: '参数',
    listHint: '多列用顿号或逗号分隔',
    cellHint: '写成「节点 ID.r行号.列名」，行号从 0 数',
    headline: '总数核对',
    headlineHint: '给出基期、本期总数那两格，系统核对拆开后各段之和是否对得上',
    headlineBase: '基期总数那一格',
    headlineTarget: '本期总数那一格',
    parent: '下钻',
    parentHint: '这一项是前面某个变化拆解里某一段的拆解',
    parentOf: '上一层分析项',
    parentKey: '那一段的维度值',
    noParent: '不下钻',
    pairsHint: '每行一对，两个组之间用顿号或逗号分隔',
    /** 参数的叫法（tools/builtin/analysis.py 各参数模型里的字段，分析卡按同一份校验） */
    param: {
      by: '维度列', period: '期次列', base: '基期', target: '本期', measure: '度量列', numerator: '分子列', denominator: '分母列',
      product: '乘积列', factors: '因子列', label: '度量的叫法', basis: '比较方式', grain: '期次粒度', lag: '和往前第几期比',
      pairs: '只比这几对', reference: '对照组', mean: '均值列', sd: '标准差列', sum: '合计列', sumsq: '平方和列', n: '样本数列',
      value: '值列',
    } as Record<string, string>,
    paramPlaceholder: {
      by: 'region', period: 'week', base: '2026-W41', target: '2026-W42', measure: 'gmv', numerator: 'refunds',
      denominator: 'orders', product: 'gmv', factors: 'orders、aov', label: '销售额', lag: '1', reference: '华东',
      mean: 'avg_amount', sd: 'sd_amount', sum: 'sum_amount', sumsq: 'sumsq_amount', n: 'orders', value: 'amount',
    } as Record<string, string>,
    paramHint: {
      label: '系统写判定语时用；不写数字、百分号和单位，留空则用列名或数据目录中的名称',
      target: '留空取最后一期',
      lag: '日缺省 7（和上周同一天比），周、月缺省 1；同比写 52（周）或 12（月）',
      reference: '其余各组各和它比；留空则各组与其余各组合计比',
    } as Record<string, string>,
    basis: { '': '和比较期比的变化率（缺省）', change: '和比较期比的变化率', level: '值本身（存量、比率用）' } as Record<string, string>,
    grain: { '': '按期次的写法推断', day: '日', week: '周', month: '月', quarter: '季度' } as Record<string, string>,
    /** differ 均值模式：给均值、标准差、样本数，或合计、平方和、样本数 */
    meanShape: { sd: '均值、标准差、样本数', sumsq: '合计、平方和、样本数' } as Record<string, string>,
    // ---- 阈值覆盖 ----
    addOverride: '添加阈值覆盖',
    pickOverride: '选择要覆盖的阈值',
    removeOverride: (label: string) => `删除阈值覆盖「${label}」`,
    noOverrides: '未覆盖：全部跟随设置',
    allOverridden: '全部阈值都已覆盖',
    // ---- 卡片 ----
    summaryEmpty: '未配置分析项',
    summaryNoOp: '未选择分析方法',
    summaryMore: (n: number) => `等 ${formatNumber(n)} 项`,
  },

  // ---- 问数据时间线：Agent 的分析工具、分析卡的每一项 ----
  step: {
    /** 运行中的状态字（store/chat） */
    running: (label: string) => `正在执行分析：${label}…`,
    /** 一步的标题：「变化拆解（可加）· A1」；分析卡的一项：「变化拆解（可加）· where」 */
    title: (method: string, alias?: string | null) => (alias ? `${method} · ${alias}` : method),
    callError: '调用有误，没有记成结论',
    callErrorNext: '这次调用没有记成结论，Agent 会按这条说明改正参数后重新调用',
    refused: '没有做成',
    refusedNext: '算不了也是一条结论：报告里可以照实写出原因，不能据此下判断',
    hypothesis: 'Agent 写的假设',
    itemHypothesis: '在验证什么',
    args: '参数',
    phrase: '判定语',
    /** 分析卡那一项的标识 */
    item: (id: string) => `分析项「${id}」`,
  },

  // ---- 时间线上的几条提醒（log 的 code：report_analysis_claims、analysis_refused、metric_comparability_skipped） ----
  log: {
    claims: (n: number | undefined, strict: boolean) => `报告${n ? `有 ${formatNumber(n)} 处` : '有'}分析说法未通过核对，`
      + (strict ? '已计入出具缺口' : '已在报告中标注'),
    refused: (method: string, item?: string) => `${method}${item ? `（分析项「${item}」）` : ''}没有做成`,
    comparabilitySkipped: (metric: string) => `指标${metric ? `「${metric}」` : ''}用到的两次查询没有做成可比性检查`,
    comparabilitySkippedNext: '指标照常计算，两期能不能直接比请自行核对',
  },

  // ---- 证据面板的分析一步（照导出页 report_export/runtime.js 的说法） ----
  evidence: {
    title: '分析结论',
    titleOf: (alias: string, method: string) => `${['分析结论', alias].filter(Boolean).join(' ')} · ${method || '—'}`,
    phrase: '判定语',
    criterion: '判定标准',
    subjects: '点名的段',
    direction: '方向',
    directionLabel: { up: '上升', down: '下降' } as Record<string, string>,
    caveats: '附带说明',
    reason: '算不了的原因',
    citedPart: '这一处引用的是',
    citedPhrase: '判定语',
    citedValue: (key: string) => `具名的数「${key}」`,
    citedCell: (row: number, column: string) => `结果表第 ${formatNumber(row + 1)} 行「${column}」`,
    who: '谁做的',
    origin: { card: '分析卡（模板定好的分析）', agent: 'Agent 现场做的分析', metric: '口径卡执行时自动做的可比性检查' } as Record<string, string>,
    hypothesis: 'Agent 写的假设',
    refused: '这次分析没有做成',
    renderOk: '与按结论渲染的结果一致',
    renderBad: '与按结论渲染的结果不一致',
    method: '方法',
    methodVersion: '方法与版本',
    params: '参数',
    settings: '实际用的阈值',
    probes: '现查结果',
    probeBasis: { live: '现查', profile: '剖析', observed: '取值观察' } as Record<string, string>,
    probeBound: (expr: string, value: string) => `按运行当天现算的界 ${expr} = ${value}`,
    probeEmpty: '这一列一个值都没有',
    probeRange: (min: string, max: string) => `${min} 至 ${max}`,
    calc: '计算',
    values: '具名的数',
    formulas: '关键数的代入式',
    resultTable: '结果表',
    resultRows: (shown: number, total: number) => `列出 ${formatNumber(shown)} 行，共 ${formatNumber(total)} 行`,
    input: '输入',
    inputOf: (ref: string) => `输入 ${ref}`.trim(),
    role: '角色',
    ref: '调用里写的名字',
    query: '查询',
    columnsUsed: '用到的列',
    rowsUsed: '用到的行',
    allRows: '全部行',
    rowsList: (rows: number[]) => rows.map((r) => `第 ${formatNumber(r + 1)} 行`).join('、'),
    headline: '总数那一格',
    headlineRatio: '整体比率那一格',
    cellAt: (row: number, column: string) => `第 ${formatNumber(row + 1)} 行「${column}」`,
    closure: '闭合核对',
    closureOk: (h: string, t: string) => `总数 ${h} 与各段之和 ${t} 闭合`,
    closureBad: (h: string, t: string) => `总数 ${h} 与各段之和 ${t} 对不上`,
    ratioClosureOk: (h: string, t: string) => `整体比率 ${h} 与各段分子之和 ÷ 分母之和 ${t} 闭合`,
    ratioClosureBad: (h: string, t: string) => `整体比率 ${h} 与各段分子之和 ÷ 分母之和 ${t} 对不上`,
    parent: '下钻自',
    gotoInput: (alias: string) => `查看输入 ${alias}`,
    inputOfAnalysis: (alias: string) => `分析结论 ${alias} 的输入`,
    /** 整句面板「挂的依据」里一份分析结论派生自谁（证据图的 derived_from 边） */
    derivedFrom: (aliases: string[]) => `派生自 ${aliases.join('、')}`,
    recompute: '复算',
    seal: '封存',
    artifact: '结论工件',
    inLedger: '封存范围内的台账记过这份分析结论',
    notInLedger: '封存范围内的台账里没有这份分析结论',
    viaCard: '记在口径卡的指标集里，随卡封存（不进台账）',
    chainPending: '正在加载判定、方法和输入…',
    // ---- 口径卡指标上的两期可比性 ----
    comparability: '两期可比性',
    comparabilityLabel: { comparable: '两期可比', conditional: '两期有条件可比', incomparable: '两期不可比' } as Record<string, string>,
    comparabilityOpen: '展开这份可比性结论（判定、方法、计算、输入、复算、封存）',
    comparabilityClose: '收起可比性结论',
    comparabilityReasons: '原因',
    comparabilityUnsealed: '这份可比性结论不在封存范围内，不能作为证据',
    comparabilityHash: '可比性结论与哈希不一致，疑似被修改',
  },

  // ---- 正文：说法违规的标注、系统写的「分析范围」 ----
  claim: {
    /** 标注的种类（服务端 analysis_claims.CODES）：给人看的短说法同审计的标签 */
    label: {
      analysis_claim_uncited: '判断词未挂分析结论',
      analysis_claim_mismatch: '判断与分析结论不符',
      causal_unsupported: '因果说法没有依据',
    } as Record<string, string>,
    tag: '说法待核',
    hint: '这句话里的判断词没有挂对应的分析结论、判断与所挂结论对不上，或原因没有依据：系统无法核对，点开看原因',
    panelTitle: '说法核对',
    analyses: '所挂的分析结论',
    noAnalyses: '这句没有挂分析结论',
    count: (n: number) => `${formatNumber(n)} 处说法待核`,
  },
  scope: {
    title: '分析范围',
    hint: '系统按这次的分析结论写的一段，不是写作者写的，随报告封存；编号可点开看出处',
    phenomena: '现象',
    decomposed: '拆过',
    excluded: '排除',
    refused: '算不了',
    notDecomposed: '没拆',
    tests: '检验',
    notDecomposedList: (dims: string[]) => `候选维度里没拆过的：${dims.join('、')}`,
    notDecomposedNone: '候选维度都拆过了',
    notDecomposedUnknown: '候选维度核对不了',
    drill: (parent: string) => `下钻自 ${parent}`,
    dims: (dims: string[]) => `按${dims.join('、')}`,
    /** 服务端 ScopeBlock.corrected：只做了一次（一次里比了多对的已按次数校正）/ 不止一次（几次之间没有校正） */
    testsText: (n: number, corrected: boolean | null | undefined) => `做了 ${formatNumber(n)} 次差异检验${corrected === true
      ? '（一次里比了几对的已按次数校正）' : corrected === false ? '，几次检验之间没有按次数校正' : ''}`,
    open: (alias: string) => `查看分析结论 ${alias} 的出处`,
  },

  // ---- 出具声明的三项：分析说法核对、指标的可比性、追不到原始数据的指标 ----
  issuance: {
    claimsTitle: (n: number, counted: boolean) => (counted
      ? `${formatNumber(n)} 处分析说法未通过核对，已计入出具缺口`
      : `${formatNumber(n)} 处分析说法未通过核对（已标注，不影响出具档位）`),
    claimsStrictClean: '分析说法已按严格核对，没有未通过的',
    claimsNext: '判断要挂对应的分析结论并照判定写；原因要有知识库原话或运行输入作依据，否则写成待核实的推测',
    comparabilityTitle: '指标的两期可比性',
    comparabilityItem: (name: string, phrase: string) => `指标「${name}」：${phrase}`,
    untracedTitle: (counted: boolean) => (counted ? '报告引用的指标未追到原始数据（已计入出具缺口）' : '报告引用的指标未追到原始数据'),
    untracedItem: (name: string, stops: string[]) => `指标「${name}」${stops.length ? `：${stops.join('；')}` : ''}`,
  },

  // ---- 设置「分析」一节 ----
  settings: {
    title: '分析',
    hint: '分析工具和分析卡判定用的阈值、次数上限。发起运行时快照进这次运行；Agent 改不了这些阈值，分析卡上可以写阈值覆盖',
    invalid: '分析的设置中有无效值，请修正后再保存',
    current: (value: string) => `当前 ${value}`,
  },

  // ---- 运行对比 ----
  compare: {
    title: '分析卡',
    hint: '两次运行的分析卡按节点和分析项对齐，只列出两次的判定和判定语，不拆变化来源',
    change: {
      same: '判定一致', verdict: '判定变了', phrase: '判定一致，判定语里的数变了', added: '这次新增', removed: '这次没有',
    } as Record<string, string>,
    base: '基准运行',
    target: '这次运行',
    none: '—',
    item: (title: string, item: string) => `${title} · ${item}`,
  },
}
