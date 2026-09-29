/**
 * 术语表：界面文案里同一件事只有一种叫法。
 *
 * 之前「正式」一个词指三件事（发布等级、运行类别、出具档位），探索态有四五种
 * 叫法；「工作流 / 图 / 画布 / 流程」「审批 / 确认 / 介入 / 放行」混用，门禁报错
 * 引用的词界面上从没出现过。这三层（发布等级、运行类别、出具档位）是「受限动态
 * 编排」的骨架，彼此独立：一次正式运行完全可能降档出具。说法必须分开。
 *
 * 规则：
 * - 「工作流」指存下来的资产，「画布」指编辑区，「图」只在技术细节里出现
 *   （执行图、走线）。
 * - 「运行」名词动词都用它；导航里的历史页叫「记录」。
 * - 人工节点叫「人工审批」，agent / tool / code 上的选项叫「审批策略」，等人
 *   处理的那张卡叫「审批卡」。
 * - 右栏叫「助手」；Copilot 只作为产品功能名出现在按钮提示里。
 *
 * 枚举值（formal / degraded / withheld 等）不动，只换显示。
 */

import type { NodeType, ToolTrust } from '../types'
import { formatNumber } from './format'

export const TERMS = {
  workflow: '工作流',
  canvas: '画布',
  run: '运行',
  records: '记录',
  assistant: '助手',
  copilot: 'Copilot',
  humanNode: '人工审批',
  approvalPolicy: '审批策略',
  approvalCard: '审批卡',
  formalRun: '正式运行',
  exploratoryRun: '探索运行',
} as const

/** 工作流状态（发布等级） */
export const WORKFLOW_STATUS_LABEL: Record<'draft' | 'published' | 'governed', string> = {
  draft: '草稿',
  published: '已发布',
  governed: '受管',
}

/** 发布弹窗里的选项说明：和工具栏 chip「已发布 v4」说法对得上 */
export const WORKFLOW_STATUS_HINT: Record<'draft' | 'published' | 'governed', string> = {
  draft: '草稿 — 只能发起探索运行',
  published: '已发布 — 可发起正式运行',
  governed: '受管 — 可发起正式运行，并受治理门禁约束',
}

/** 运行类别 */
export const RUN_CLASS_LABEL: Record<'formal' | 'exploratory', string> = {
  formal: '正式运行',
  exploratory: '探索运行',
}

/** 按钮上的运行类别：「正式运行 v3」 */
export function runClassLabel(runClass: 'formal' | 'exploratory' | null | undefined, version?: number | null): string {
  if (runClass === 'formal') return version != null ? `正式运行 v${version}` : '正式运行'
  return '探索运行'
}

/**
 * 没挂在工作流上的运行：画布或问数据当场搭的，从没存成工作流。后端给它的名字老数据是
 * 「临时图」、新的是「未保存的工作流」，也可能是空的；界面上一律叫「未保存的工作流」。
 * 运行和审批都带 workflow_id / workflow_name，记录页、命令面板、审批卡、通知走同一处
 */
export const UNSAVED_NAME = '未保存的工作流'
export const UNSAVED_HINT = '这次运行跑的是画布或问数据当场搭的工作流，没有存下来，所以回不到画布里'
const UNSAVED_NAMES = new Set(['临时图', UNSAVED_NAME])
type WorkflowRef = { workflow_id?: string | null; workflow_name?: string | null }
export const isUnsaved = (x: WorkflowRef): boolean =>
  !x.workflow_id && (!x.workflow_name || UNSAVED_NAMES.has(x.workflow_name))
export const runName = (x: WorkflowRef): string =>
  isUnsaved(x) ? UNSAVED_NAME : x.workflow_name ?? ''

/** 出具档位 */
export const ISSUANCE_LABEL: Record<'formal' | 'degraded' | 'withheld', string> = {
  formal: '完整出具',
  degraded: '降档出具',
  withheld: '不予出具',
}

export function issuanceLabel(tier: string | null | undefined): string {
  if (!tier) return '—'
  return (ISSUANCE_LABEL as Record<string, string>)[tier] ?? tier
}

/**
 * 节点类型的中文名。画布节点库、运行流、门禁报错、GraphPeek 的 chip 都用这一张。
 *
 * 这是短名：节点库条目上的「成果 / 出具」是在短名后面补了一句用途，那是节点库
 * 自己的展示，不是另一个名字。后端 schema.py / governance.py 的报错也按这张表写。
 */
export const NODE_TYPE_LABEL: Record<NodeType, string> = {
  input: '输入',
  output: '成果',
  llm: '模型调用',
  agent: 'Agent',
  supervisor: '多 Agent 协作',
  tool: '调用工具',
  code: '沙箱代码',
  branch: '条件分支',
  loop: '循环',
  subgraph: '子工作流',
  memory: '长期记忆',
  retrieve: '知识检索',
  transform: '数据整形',
  human: '人工审批',
  validate: '结构校验',
  metrics: '口径卡',
  report: '报告撰写',
}

/** 与 decode.ts 里旧名字同名的出口，第二波改成从这里导入时不用改调用处 */
export const TYPE_LABEL: Record<string, string> = NODE_TYPE_LABEL

/** 未知类型原样返回：Copilot 可能编出不存在的类型，显示原码比显示空白好查 */
export function nodeTypeLabel(type: string | null | undefined): string {
  if (!type) return '—'
  return TYPE_LABEL[type] ?? type
}

/** 审批策略的选项文字：agent / tool / code 三处共用一套说法 */
export const APPROVAL_POLICY_LABEL: Record<'dangerous' | 'always' | 'never', string> = {
  dangerous: '仅危险工具需要审批',
  always: '每次调用都审批',
  never: '全部自动放行',
}

/**
 * 可点击证据：片段状态的叫法。正文里片段的 aria-label、面板标题、图例都用这一份。
 * 外观（线型、字形、颜色）在 lib/evidence.ts，和这里一一对应。
 *
 * 概率性的四种（四期的结论句裁判）挂在句末的徽标上：有依据、部分有依据、证据不支持都是模型的判断，
 * 叫法里写明「模型判断」；未裁判不带「有引用」——没挂依据、只有方向词的结论句也会送裁判、也会没判。
 */
export const EVIDENCE_STATE_LABEL = {
  deterministic: '有出处',
  supported: '模型判断：有依据',
  partial: '模型判断：部分有依据',
  unsupported: '模型判断：证据不支持',
  unjudged: '未裁判',
  none: '无证据',
  connective: '连接性文字',
  candidate: '猜测的来源',
  suspect: '可能是编造的名字',
  unverified: '核对不了',
} as const

/**
 * 「7/12 数字有出处 · 无证据 5 · 另有 1 处引用解析不了」：出具横幅、报告核对那一行、证据条、
 * 读屏摘要共用一种说法。
 *
 * 前半句只数数字，要让人一眼算得平：无证据 = 总数 − 有出处（12 − 7 = 5）。不是数字的
 * 引用（[[v:]] 值、句末 [[see:]] 依据）解析不了的另起一句——以前把它们也算进「无证据」，
 * 12 − 7 却写成 6，读的人以为算错了。一个数字都没有、也没有别的问题时返回空串，调用方
 * 自己决定说什么
 */
export function evidenceTally(cited: number, total: number, other = 0): string {
  const gaps = Math.max(0, total - cited)
  const parts: string[] = []
  if (total > 0) {
    parts.push(gaps
      ? `${formatNumber(cited)}/${formatNumber(total)} 数字有出处 · 无证据 ${formatNumber(gaps)}`
      : `${formatNumber(total)} 个数字都有出处`)
  }
  if (other > 0) parts.push(`${total > 0 ? '另有 ' : ''}${formatNumber(other)} 处引用解析不了`)
  return parts.join(' · ')
}

/** 证据面板和证据条的文案 */
export const EVIDENCE_TEXT = {
  allCited: (n: number) => `${formatNumber(n)} 个数字都有出处`,
  locateNext: '定位下一处',
  /** 读屏摘要末尾的操作说明 */
  keysHint: '用左右方向键逐个查看，上下键按句子走，n 跳到下一处无证据，回车打开证据，Esc 关闭',
  panelTitle: '证据',
  close: '关闭证据',
  back: '回到正文',
  sentence: '所在的句子',
  metric: '口径卡指标',
  expression: '原式',
  substituted: '代入式',
  inputs: '输入',
  recompute: '复算',
  recomputeOk: '复算一致',
  recomputeBad: '复算不一致：代入式算出来和记录的值对不上',
  recomputeNone: '没法复算',
  input: '运行输入',
  seal: '封存',
  sealOk: '已封存 · 核对一致',
  sealDocOk: '报告文档已封存 · 核对一致（这一段本身没有证据可封存）',
  /** 片段接口取不到、封存状态是从整次运行的证据图查的：只核对了文档在封存范围内，这一段的链没逐项核对 */
  sealDocChain: '报告文档已封存 · 核对一致（这一段的证据链这次没取到，没有逐项核对）',
  sealBad: '已封存 · 核对不一致',
  sealOpen: '尚未封存',
  sealOutside: '不在封存范围内',
  /** 正文这份报告不是封存的那一份：封存链再完好，也证明不了屏幕上这些字 */
  sealForeign: '正文不是封存的那份报告，封存证明不了这里的字',
  sealUnknown: '封存状态拿不到',
  violations: '违规清单',
  violationsHint: '报告撰写节点核对出来的全部问题。列表序号、代码块标签里的数字在正文里画不了线，只能在这里看',
  structural: '这个数字在列表序号、代码块标签这类 Markdown 语法里，正文里画不了线',
  noSegment: '这一条在正文里没有对应的字，比如句末依据里写错的引用',
  /** 读屏摘要：画不了线的两种，分开说在哪（和违规清单里每条的 structural / noSegment 同一个意思） */
  structuralCount: (n: number) => `其中 ${formatNumber(n)} 个数字在列表序号、代码块标签这类 Markdown 语法里，正文里画不了线，只在违规清单里列出`,
  noSegmentCount: (n: number) => `${formatNumber(n)} 处在正文里没有对应的字（比如句末依据里写错的引用），只在违规清单里列出`,
  /** 出具横幅：回指不上的数字。有报告文档时点状线只画得出一部分（列表序号里的画不了），不能说「已用虚线标出」 */
  unmatchedInDoc: '无法回指的数字（逐条见报告的违规清单）：',
  unmatchedPlain: '无法回指的数字：',
  uncited: '这个数字没有写成引用标记，系统核对不到它从哪来',
  missingValue: '缺输入：口径卡这次没有算出这个指标的值',
  unshowable: (value: string) => `有值（${value}），但按口径卡的格式显示不出来`,
  chainPending: '正在取算式和输入…',
  /** 证据接口逐项复核出来的问题。正常时不说，出问题才醒目 */
  integrity: {
    eid: '证据标识对不上：目录里记的和按工件重算的不一样',
    hash: '口径卡工件取回时哈希对不上，或者卡里找不到这个指标',
    render: '按口径卡重新渲染出来的字和报告上的不一样',
    sealed: '这件证据不在封存范围内：事后补进来的，不能当证据',
    /** 证据接口答的是封存范围内那份报告，和正文这份不是同一份（工件 id 或这个位置的字对不上） */
    doc: '正文这份报告不是封存范围内的那一份：下面的出处是正文自己记的，没有经过封存核对',
    /** 证据图里正文这份报告的哈希对不上 */
    docHash: '正文这份报告和它的哈希对不上，疑似被改过',
    sealedText: (text: string) => `封存的那一份在这个位置写的是「${text}」`,
  },
  chainMissing: '算式、代入式和输入要从证据接口取，这一次没取到',
  chainForeign: '证据接口给的是封存的那份报告的算式，和正文这份对不上，不在这里展示',
  noRun: '这份报告不属于某次运行，只能看文档里记下的出处',
  docMissing: '证据文档取不到，按普通文本显示',
  docMismatch: '成果字段和报告文档对不上，按普通文本显示',
  docOtherRun: '证据文档不是这次运行写的，按普通文本显示',
  ambiguous: (candidates: string[]) => `出处不唯一：候选 ${candidates.join('、')}`,

  // ---- 第二期：查询步骤、输入来源、口径卡来源 ----
  query: (alias: string) => `查询 ${alias}`.trim(),
  copySql: '复制 SQL',
  /** 窗口外的行：面板只拿到被引用的行加前后各 2 行 */
  windowed: '仅显示被引用的行及前后各 2 行',
  openSnapshot: '打开完整快照',
  snapshotTitle: (alias: string) => `查询 ${alias} · 完整快照`,
  masked: '已遮罩',
  /** 面板底部：被遮罩的列 + 遮罩不是安全边界（用户拍板写明） */
  maskNote: (cols: string[]) =>
    `已遮罩的列：${cols.join('、')}。在有身份体系之前，遮罩只减少暴露，不是安全边界：完整快照、工件接口里仍是原值`,
  queryMissing: '查询的行要从证据接口取，这一次没取到',
  queryPending: '正在取查询的行…',
  queryHash: '查询快照取回时哈希对不上，疑似被改过：这些行不能当证据',
  truncatedSnapshot: '查询撞了行数上限，库里还有更多',
  windowCut: (n: number) => `被引用的行太多，这里只列了前 ${formatNumber(n)} 行，其余看完整快照`,
  sources: '输入来源',
  gotoQuery: (alias: string) => `看查询 ${alias}`.trim(),
  sourceVerified: '与快照一致',
  sourceMismatch: (told: string, truth: string) => `模型报 ${told}，快照是 ${truth}，已按快照取值`,
  /** cell() 取数：算的时候用的值和快照不一样，值没有被换掉，照实说上游改写过 */
  cellMismatch: (used: string, truth: string) => `算的时候用的是 ${used}，快照里是 ${truth}：上游改写过这份结果`,
  sourceEmpty: '没查到，记为空，没有兜底成 0',
  cellUnresolved: '核对不到查询快照',
  codeCompute: (node: string) =>
    `代码节点${node ? `「${node}」` : ''}算出来的数（计算）：沙箱里的算术系统核对不了，业务计算要写进口径卡`,
  codeSource: (node: string) =>
    `来自代码节点${node ? `「${node}」` : ''}（标为取数）：沙箱跑出来的结果，系统核对不到查询快照`,
  inputMissing: '缺：上游没给出这个值',
  caliberFrom: (caliber: string, version: string, workflow: string, wfVersion: string) =>
    `口径卡「${caliber || '—'}」${version}，来自「${workflow}」${wfVersion}`,
  caliberUpgrade: (latest: string | null, policy: string) =>
    `${latest ? `上游已有 ${latest}` : '上游有新版本'}，按「${policy}」处置`,

  // ---- 第三期：表名字段名、逐字引文、旧运行的猜测 ----
  entity: '表结构',
  table: '表',
  column: '字段',
  entityQueries: (aliases: string[]) => `出现在查询 ${aliases.join('、')}`,
  entityNoQuery: '这次的查询里没有用到它，只在表结构快照里',
  syncedAt: (at: string) => `表结构快照同步于 ${at}`,
  snapshotPartial: '库里的表太多，表结构快照只存了一部分',
  columnType: '字段类型',
  ownerTables: (tables: string[]) => `好几张表都有这个字段：${tables.join('、')}`,
  entitySource: { schema: '表结构快照', sql: '查询 SQL 用到的表', result: '查询结果列' } as Record<string, string>,
  entityPending: '正在取表结构…',
  entityMissing: '字段类型、同步时间要从证据接口取，这一次没取到',
  /** 反引号里写了个名字，哪里都没有（没有 cite 时的原因；有 cite 的用后端给的原话） */
  suspect: '本次运行的表结构快照、查询用到的表、查询结果列里都没有这个名字，可能是编造的名字',
  unverified: '表结构快照只存了一部分，查询用到的表、查询结果列里也没有这个名字，核对不了它存不存在',
  suspectTag: '可疑名字',
  /** 出具横幅那一行：报告要求结论句挂依据（claims: require_citation）时，没挂的有几句 */
  uncitedClaimsTag: '没挂依据的结论句',
  closest: '最接近的已知名字',
  checked: (schemas: number, queries: number) =>
    `查过 ${formatNumber(schemas)} 份表结构快照、${formatNumber(queries)} 次查询`,
  tableColumns: (n: number, view: boolean) => `${view ? '视图' : '表'} · ${formatNumber(n)} 个字段`,
  columnTypes: (types: Record<string, string>) =>
    `各表的类型：${Object.entries(types).map(([t, v]) => `${t} ${v}`).join('、')}`,
  quoteBad: '按记下的位置从原文里切出来，和这句引文对不上：原文可能被改过',
  quoteCollection: (c: string) => `知识库「${c}」`,
  closestNone: '没有相近的已知名字',
  quote: '逐字引文',
  quoteFrom: (title: string) => `出自「${title}」`,
  quoteChunk: (n: number) => `第 ${formatNumber(n)} 段`,
  quoteHit: '引文在原文里的位置（高亮）',
  quotePending: '正在取原文…',
  quoteMissing: '原文要从证据接口取，这一次没取到',
  quoteMiss: '原文里找不到这句话：写作者写的不是一字不差的原话',
  quoteHash: '检索快照取回时哈希对不上，疑似被改过：原文不能当证据',
  /** 旧运行：按数值猜的候选 */
  guessTitle: '猜测的来源',
  guessNote: '猜测的来源，不能当证据：按数值在这次运行已封存的查询结果和口径卡里找相同的值，同值的巧合很多',
  guessCount: (guessed: number, numbers: number) =>
    numbers ? `${formatNumber(numbers)} 个数字里 ${formatNumber(guessed)} 个找到了可能的来源` : '答案里没有数字',
  guessNone: '没找到数值相同的格子',
  guessDiff: (d: string) => `相差 ${d}`,
  guessCandidates: '可能来自',
} as const

/** 结论句按裁判结论分成的几堆（lib/evidence 的 claimCounts 数出来的） */
export interface ClaimTallyCounts {
  /** 结论句总数：模型判为「不是结论句」的不算 */
  total: number
  supported: number
  partial: number
  unsupported: number
  /** 送了裁判、没判成（到上限、调用失败）或还没请模型判断的 */
  unjudged: number
  /** 没有判定、也没挂依据的结论句 */
  uncited: number
}

/**
 * 「结论 4 句（支持 3 · 无证据 1）」：出具横幅、证据条、报告核对那一行共用，和数字那一段同一种说法——
 * 先说总数，括号里按状态分，为 0 的不写。没有结论句时返回空串
 */
export function claimTally(c: ClaimTallyCounts): string {
  if (!c.total) return ''
  const parts = [
    ['支持', c.supported], ['部分支持', c.partial], ['不支持', c.unsupported], ['未裁判', c.unjudged], ['无证据', c.uncited],
  ].filter(([, n]) => (n as number) > 0).map(([k, n]) => `${k} ${formatNumber(n as number)}`)
  return `结论 ${formatNumber(c.total)} 句${parts.length ? `（${parts.join(' · ')}）` : ''}`
}

/**
 * 结论句裁判（四期）在证据面板、句末徽标里的说法。判断是模型给的：一律写明「模型判断」「非确定」，
 * 封存之后按需追加的另写「封存后追加」
 */
export const JUDGE_TEXT = {
  section: '模型的解释',
  claim: '结论句',
  /** 读屏摘要末尾的操作说明：有证据不支持、部分支持的结论句时 n 也跳到它们的句末徽标 */
  keysHint: '用左右方向键逐个查看，上下键按句子走，n 跳到下一处无证据或证据不支持的句子，回车打开证据，Esc 关闭',
  /** 面板里判断的徽标：谁判的、而且不是确定的 */
  badge: (model: string | null | undefined) => `模型判断 · ${model || '裁判模型'} · 非确定`,
  postSeal: '封存后追加',
  /** 判定里没记模型名时的叫法 */
  judgeModel: '裁判模型',
  /** 句末徽标的悬停说明：到上限没判的 */
  limitNotJudged: '已到上限，这句没判（模型没有看过这句）',
  /** 没判的句子（到上限、没跑成）记着的裁判模型：中性地说，不挂「模型判断」的徽标 */
  notJudgedBy: (model: string) => `裁判模型 ${model} 没有判这句`,
  sealedDoc: '报告文档已封存 · 核对一致',
  sealedWithDoc: '报告文档已封存 · 核对一致，这条判断随报告一起封存',
  postSealHint: '运行封存之后按需追加的判断：不在封存范围内，封存核对照样一致。它是模型的解释，不是证据',
  sealedHint: '正式运行里报告撰写节点当场判的，和报告一起封存。它是模型的解释，不是证据',
  used: (aliases: string[]) => `裁判看过的证据：${aliases.join('、')}`,
  cites: '挂的依据',
  noCites: '这句没挂依据（只有方向词、因果词）：裁判看不到任何证据摘录',
  notClaim: '模型判断：不是结论句',
  ask: '请模型判断这句',
  askAgain: '再请模型判断一次',
  asking: '正在请模型判断…',
  askHint: '探索运行按需裁判：点一次判这一句，花费计入每次点击和每日的上限',
  notAsked: '还没请模型判断这句',
  screened: '预筛认为这句不陈述数据事实（短句、只有过渡的话），没有送裁判',
  noPrice: '裁判模型不在价格目录里，按令牌估不出金额：金额上限对它不起作用',
  limit: '已到上限',
  /** 触顶之后怎么调。按需裁判（每次点击）和正式运行（每份报告）调的地方不一样 */
  limitHow: {
    click: {
      max_cost_usd: '到「设置 → 偏好设置 → 证据裁判」调高「每次点击的金额上限」，或者设成不限，再点一次',
      daily_max_usd: '到「设置 → 偏好设置 → 证据裁判」调高「每日金额上限」或设成不限；也可以等到明天（按本地日期重新计）',
      max_claims: '到「设置 → 偏好设置 → 证据裁判」调高「每份报告最多判几句」（报告撰写节点里写了的以节点为准）',
      timeout_s: '到「设置 → 偏好设置 → 证据裁判」调高「每份报告的时长上限」或设成不限，再点一次',
    } as Record<string, string>,
    report: {
      max_cost_usd: '到报告撰写节点的「结论句裁判」调高金额上限（没写就是设置里「证据裁判」的默认值），也可以设成不限，再发起一次运行',
      daily_max_usd: '到「设置 → 偏好设置 → 证据裁判」调高「每日金额上限」或设成不限，再发起一次运行',
      max_claims: '到报告撰写节点的「结论句裁判」调高最多判几句（或设置里的默认值），也可以设成不限，再发起一次运行',
      timeout_s: '到报告撰写节点的「结论句裁判」调高时长上限（或设置里的默认值），也可以设成不限，再发起一次运行',
    } as Record<string, string>,
  },
  failed: (why: string) => `没判成：${why}`,
  oldBackend: '这个后端还不支持按需裁判',
  notFound: '报告里没有这一句',
  formalOnly: '正式运行的裁判在报告撰写节点里当场完成，不能按需追加',
  rewritten: '这句是按裁判的意见改写过的（改写一次）',
  rewriteFrom: '交回改写的原句',
  rewriteRejected: (reason: string) => `这句交回写作者改写过一次，改写稿没有采用：${reason}`,
  /**
   * 横幅上结论句那一段之外另起的「没挂依据的结论句 N」：开了裁判的文档里，没挂依据的句子按判定数（或预筛
   * 放掉了不数），出具却照样按没挂依据计缺口——悬停时说清这两件事不冲突
   */
  uncitedBesides: '没写依据（[[see:]]）的结论句：出具照样计入缺口。模型判过的也算在里面——判定是模型的解释，代替不了依据',
  /** 句末徽标的 aria-label：「结论句「增长主要来自新客首单」，模型判断：证据不支持，封存后追加」 */
  badgeLabel: (sentence: string, state: string, postSeal: boolean) =>
    `结论句「${sentence}」，${state}${postSeal ? '，封存后追加' : ''}`,
} as const

/**
 * 报告撰写节点的 judge 子配置（claims 为 judge 时生效）：检查器、发布前修复的预览（「结论句裁判 · 金额上限」）
 * 共用这份叫法。键同后端 schema.JUDGE_KEYS
 */
export const JUDGE_FIELD_LABEL: Record<string, string> = {
  provider: '裁判模型 · 接入',
  model: '裁判模型',
  max_claims: '最多判几句',
  max_cost_usd: '金额上限（美元）',
  timeout_s: '时长上限（秒）',
  rewrite_once: '证据不支持的句子交回改写一次',
  on_unsupported: '证据不支持时',
}
/** judge.on_unsupported：正式运行里证据不支持的结论句怎么判档（探索运行只标注） */
export const JUDGE_ON_UNSUPPORTED_LABEL: Record<string, string> = {
  degrade: '出具降档',
  withhold: '不予出具',
}

/** 设置页「证据裁判」一组（后端 engine/judge.py 的 JUDGE_DEFAULTS） */
export const JUDGE_SETTING_TEXT = {
  label: '证据裁判',
  hint: '报告里的结论句（「增长主要来自新客首单」这类）由另一个模型按证据逐句判断：正式运行在报告撰写节点里当场判，探索运行点开哪句才判哪句。判断是模型给的，不是系统核对，界面上一律标「非确定」。',
  provider: '裁判模型 · 接入',
  model: '裁判模型 · 模型',
  /** 接入留空、模型也留空：后端依次用 Copilot 的模型、默认接入（judge_model_spec）。只有这时才叫「跟随 Copilot」 */
  providerDefault: '跟随 Copilot 的模型',
  /** 接入留空、模型填了：这一级写了就整组用这一级，后端按模型名找接入——不再跟随 Copilot */
  providerFromModel: '按模型名找接入',
  modelDefault: '留空：用接入的默认模型',
  modelFollow: '留空：跟随 Copilot 的模型',
  differ: '建议和写报告的模型不同：同一个模型审自己写的，写错的地方它多半也看不出来',
  limits: '上限（每一项都可以设成不限）',
  reportClaims: '每份报告最多判几句',
  reportCost: '每份报告的金额上限（美元）',
  reportTimeout: '每份报告的时长上限（秒）',
  clickCost: '每次点击的金额上限（美元）',
  daily: '每日金额上限（美元）',
  unlimited: '不限',
  nodeWins: '报告撰写节点的「结论句裁判」里写了的上限以节点为准',
  spend: (usd: string, calls: number) => `今天已花 $${usd}（${formatNumber(calls)} 次调用）`,
  unpriced: (n: number) => `另有 ${formatNumber(n)} 次调用估不出金额（模型不在价格目录里），没算进去`,
  spendNone: '今天还没有裁判调用',
} as const

/**
 * 只填了裁判模型、接入留空时实际用哪个接入（后端 judge_model_spec：哪一级写了就整组用哪一级，不跨级拼；
 * 再由 resolve_provider 按模型名找接入）。scope：设置页（上一级是 Copilot 的模型）还是报告撰写节点（上一级是设置）。
 * found：按模型名找到的接入；fallback：认不出时后端去调的默认接入
 */
export function judgeByModelText(
  scope: 'settings' | 'node', model: string, found: { name: string; enabled: boolean } | null | undefined,
  fallback: string | null | undefined,
): string {
  const follow = scope === 'settings' ? '「跟随 Copilot」只在接入和模型都留空时生效'
    : '接入和模型都没写时才跟随设置里的「证据裁判模型」'
  if (found && !found.enabled) {
    return `只填了模型：「${model}」属于已停用的接入「${found.name}」，裁判调用会失败、结论句都记为未裁判；启用它或选别的接入`
  }
  if (found) return `只填了模型：按模型名用接入「${found.name}」。${follow}`
  return `只填了模型：没有哪个接入的模型清单里有「${model}」，会拿它去调默认接入${fallback ? `「${fallback}」` : ''}，`
    + `调不通时结论句都记为未裁判；不是这家的模型请选上接入。${follow}`
}

/**
 * 某项上限设成不限时写明它还受什么约束——不要悄悄生效。别的上限也不限时照实少说几样，全不限时
 * 说到底只受调用次数（结论句的多少）约束。set：各项眼下是不是有上限
 */
export function judgeUnlimitedText(
  key: 'claims' | 'cost' | 'timeout' | 'click' | 'daily',
  set: { claims: boolean; cost: boolean; timeout: boolean; click: boolean; daily: boolean },
): string {
  const list = (items: [boolean, string][]) => items.filter(([on]) => on).map(([, t]) => t)
  switch (key) {
    case 'claims': return '不设上限，句数只受报告里结论句多少约束'
    case 'timeout': return '不设上限，时长只受模型接口自身的超时约束'
    case 'cost': {
      const by = list([[set.claims, '句数上限'], [set.timeout, '时长上限'], [set.daily, '每日上限']])
      return by.length ? `不设上限，费用只受${by.join('、')}约束` : '不设上限，费用只受报告里结论句的多少约束（句数、时长、每日也都不限）'
    }
    case 'click': {
      const by = list([[set.claims, '句数上限'], [set.timeout, '时长上限'], [set.daily, '每日上限']])
      return by.length ? `不设上限，每次点击只受${by.join('、')}约束` : '不设上限，每次点击只受点开的句数约束（句数、时长、每日也都不限）'
    }
    case 'daily': {
      const by = list([[set.cost, '每份报告的金额上限'], [set.click, '每次点击的金额上限']])
      return by.length ? `不设上限，每日费用只受${by.join('和')}约束` : '不设上限，每日费用只受模型调用次数约束（每份报告、每次点击也都不限）'
    }
  }
}

/**
 * 记录页「证据」页签：报告、常驻面板、审计表。审计表按状态分组，四组的说法和片段状态同一套
 */
export const EVIDENCE_AUDIT_TEXT = {
  tab: '证据',
  tabTitle: '报告里每一段的出处：左边报告、右边证据、下面整张清单，可以只看有问题的、导出',
  title: '证据清单',
  groups: {
    none: '无证据',
    suspicious: '可疑实体',
    cited: '有出处',
    candidate: '旧运行猜测',
  } as Record<string, string>,
  groupHint: {
    none: '裸数字、解析不了的引用、没挂依据的结论句',
    suspicious: '表名、字段名在本次运行的表结构和查询里找不到',
    cited: '系统从证据里取值、核对过的片段',
    candidate: '按数值猜的可能来源，不能当证据，默认收起',
  } as Record<string, string>,
  filterLabel: '清单筛选',
  filterAll: '全部',
  filterProblems: '只看无证据 / 可疑实体',
  cols: { text: '片段', state: '状态', source: '出处 / 原因', sentence: '所在的句子', report: '报告', seal: '封存' },
  sealIn: '在封存范围内',
  sealOut: '不在封存范围内',
  sealNa: '—',
  exportJson: '导出 JSON',
  exportCsv: '导出 CSV',
  exported: (name: string) => `已导出 ${name}`,
  exportLocal: '后端没有导出接口：按页面上这张清单导出（没有经过后端的封存核对）',
  fallbackUnsupported: '后端还没有审计接口：清单按页面上的报告拼出来，每行的封存状态取自证据图',
  fallbackError: (why: string) => `审计接口没取到（${why}）：清单按页面上的报告拼出来`,
  empty: '这一组没有片段',
  emptyProblems: '没有无证据、可疑实体的片段',
  hidden: '正文里画不了线，只在这里列出',
  hiddenWhy: '这一处在列表序号、代码块标签、粗体或链接里，或者在句末依据里：正文里没有能画线、能点开的片段，只在这张清单里列出',
  keys: '在清单里用上下方向键逐行走，回车在右边打开这一段的证据',
  panelIdle: '点报告里的片段，或清单里的一行，这里显示它的出处',
  sealTitle: '封存核对',
  sealOk: (seq?: number) => `已封存 · 核对一致${seq != null ? `（封存到第 ${formatNumber(seq)} 条事件）` : ''}`,
  sealBad: '已封存 · 核对不一致：封存之后有事件被改过、删过或插过',
  sealOpen: '尚未封存：运行还没收尾，证据还可能变',
  sealUnknown: '封存状态拿不到',
  docOk: (node: string) => `报告「${node}」的文档哈希一致`,
  docBad: (node: string) => `报告「${node}」的文档和哈希对不上，疑似被改过`,
  docGone: (node: string) => `报告「${node}」的文档在工件库里取不回来`,
  mode: {
    none: '这次运行没有报告文档，也没有出具契约：没有可以逐段展示的证据',
    legacy_contract: '旧版出具：按数值匹配口径卡，不是显式引用。同一个值对得上好几个指标时，出处不唯一',
    legacy_text: '这次运行没有出具契约：下面是按数值猜的可能来源，默认收起',
  } as Record<string, string>,
  modeLabel: { cited: '逐段引用', none: '没有证据', legacy_contract: '旧版出具', legacy_text: '没有契约的旧运行' } as Record<string, string>,
  legacyMatched: (n: number) => `按数值回指上 ${formatNumber(n)} 个数字`,
  legacyUnmatched: (n: number) => `${formatNumber(n)} 个回指不上`,
  legacyNoField: '回指的位置对不上成果里的任何一个字段，只列出数字，不在正文上标',
} as const

/** 报告节点卡上的章：这张图最近一次运行的报告核对统计 */
export const reportStampText = (cited: number, none: number) =>
  `引用 ${formatNumber(cited)} · 无证据 ${formatNumber(none)}`

/**
 * 口径升版的三种处置，和后端 governance.UPGRADE_POLICY_LABELS 同一套说法。子工作流和
 * 钉版本的口径卡（caliber_from）共用
 */
export const UPGRADE_POLICY_LABEL: Record<string, string> = {
  recompute: '用新口径回算历史',
  dual: '并排双印新旧口径',
  incomparable: '标注与历史不可比',
}

/**
 * 子工作流钉住版本后的升版处置说明。选项和口径卡的那个下拉是同一份（检查器直接借用口径卡
 * 的字段定义），这里只换掉说明里「和谁同一套」那半句
 */
export const SUBGRAPH_UPGRADE_HELP = '钉住的那一版之后，被嵌的工作流又发了新版本：正式运行前必须声明怎么处置，'
  + '和钉住别处的口径卡是同一套规则；声明了就照它执行，并记进运行记录'

/** 上游在钉住的那一版之后又发了版：子工作流和口径卡的提醒同一句 */
export const upgradeNewerText = (latest: number, pinned: number) =>
  `上游已有 v${latest}（钉的是 v${pinned}）：正式运行前要在下面声明升版处置，否则会被挡住`

/**
 * 发布前检查与自动修复：发布弹窗和问题面板的「发布前检查」共用一套说法。
 * 原则写在话里：修复只出预览、人确认了才存草稿，永远不替人发布
 */
export const PUBLISH_FIX_TEXT = {
  section: '发布前检查',
  lint: '校验',
  checking: '正在做发布前检查…',
  unsupported: '这个后端还没有发布前检查：点「发布」时门禁照旧检查，拦下的问题会列在这里',
  failed: '发布前检查没做成',
  retry: '重试',
  recheck: '重新检查',
  run: '检查',
  stale: '画布改过了，结果可能已经过时',
  blocked: (n: number) => `发布前检查：会被门禁拦下 ${n} 处`,
  gateBlocked: (n: number) => `门禁拦下了 ${n} 处，改完再发布`,
  passed: '发布前检查通过：没有会被门禁拦下的问题',
  warnings: (n: number) => `另有 ${n} 条提示`,
  fixAll: (n: number) => `一键修复可自动修的 ${n} 处`,
  fixOne: '修复',
  fixHint: (label: string) => `先看预览：${label}`,
  choose: '预览',
  chooseFirst: '先选好再预览',
  multiple: '可以多选',
  suggested: '建议',
  assist: '交给 Copilot',
  assistHint: '让 Copilot 试着改剩下的问题。它改的同样只是预览，要人拿主意的会原样问你',
  locked: (why: string) => `${why}。现在不能修复`,
  previewing: '正在生成修复预览…',
  assisting: 'Copilot 正在试着修，可能要几十秒…',
  stop: '停下',
  previewTitle: (n: number) => `修复预览 · ${n} 处改动`,
  noChange: '这一次没有可以应用的改动',
  rejected: '没有采用',
  assistSaid: 'Copilot',
  questions: 'Copilot 需要你拿主意',
  afterOk: '应用后门禁不再拦',
  afterLeft: (n: number) => `应用后还剩 ${n} 处会被拦下`,
  apply: '应用并重新检查',
  discard: '放弃',
  applyNote: '应用后存成新的草稿版本，再重新检查一遍；不会自动发布',
  dirtyNote: '画布上还有没保存的改动，应用时会一起存进草稿',
  saving: '正在保存草稿…',
  rechecking: '正在重新检查…',
  saveFailed: '草稿没保存上',
  saveFailedHint: '修复已经放到画布上（可以撤销）；保存成功之前不能发布',
  retrySave: '重试保存',
  stalePreview: '预览之后画布又改过了，这份预览对不上了：重新检查一次再修',
  autofixMissing: '这个后端还不支持自动修复：照提示手动改，或者打开助手描述一遍',
  autofixFailed: '修复预览没生成出来',
  applied: (n: number, version?: number) => `已应用 ${n} 处修复${version != null ? `，存为 v${version}` : ''}`,
  saveNote: (labels: string[]) => `发布前修复：${labels[0] ?? ''}${labels.length > 1 ? ` 等 ${labels.length} 处` : ''}`,
  /** 撤销栈里这一步叫什么 */
  undoLabel: (n: number) => `发布前修复（${n} 处）`,
  pendingPreview: '先应用或放弃修复预览',
  unsaved: '修复还没保存上',
  whole: '整张工作流',
} as const

/** 证据的种类怎么叫：aria-label、面板标题、出处那一句 */
export const EVIDENCE_KIND_LABEL: Record<string, string> = {
  metric: '口径卡指标',
  input: '运行输入',
  cell: '查询结果',
  query: '查询结果',
  table: '整表',
  retrieval: '知识库检索',
  node_output: '代码节点的产出',
  entity: '表或字段',
  column: '字段',
  quote: '引文',
}

/**
 * MCP / 自定义工具的信任档：工具页的三选一、工具徽标、审批卡的「始终允许」共用这一份。
 * 「始终允许」只指 always；审批卡上那个按钮设的是 gated，按钮字短，说明里写全
 */
export const TOOL_TRUST_VALUES: readonly ToolTrust[] = ['ask', 'gated', 'always']

export const TOOL_TRUST_LABEL: Record<ToolTrust, string> = {
  ask: '等审批',
  gated: '始终允许 · 门控把关',
  always: '始终允许',
}

/** 每一档悬停看的那句：它在运行时具体怎么做 */
export const TOOL_TRUST_HINT: Record<ToolTrust, string> = {
  ask: '每次调用都停下来等你批。协作团队里的成员停不下来，这次调用不执行',
  gated: '每次调用先让门控模型看一眼参数：它放行就直接执行，判可疑、没答上来都交给你批',
  always: '不问你，直接执行',
}

export const TOOL_TRUST_TEXT = {
  title: '运行时审批',
  /** 三选一的读屏名字 */
  groupLabel: (tool: string) => `${tool} 的运行时审批`,
  explain: '默认每次都等你批；门控把关是每次先让一个小模型看参数，可疑的仍然交给你；正式运行不看这个设置，一律等审批。',
  /** 工具徽标：ask 和 gated 各一个，always 不挂 */
  badgeAsk: '运行时需审批',
  badgeGated: '门控把关',
  badgeGatedHint: '在探索运行里调用它之前，先让门控模型看一眼参数，判可疑的仍然停下来等人工审批。正式运行一律等审批。',
  saveFailed: (tool: string) => `${tool} 的运行时审批没改成，已恢复原样`,
  /** 审批卡 */
  alwaysButton: '始终允许',
  alwaysHint: (tool: string) =>
    `批准这次，并把 ${tool} 设为「始终允许 · 门控把关」：本节点后面的调用和以后的运行不再问你，由门控模型逐次把关；可以在工具页改回`,
  alwaysDone: (tool: string) => `已放行，运行继续；${tool} 以后由门控模型把关`,
} as const

/** 设置页「运行默认值」里的门控模型 */
export const TOOL_GATE_TEXT = {
  label: '门控模型',
  provider: '门控模型 · 接入',
  model: '门控模型 · 模型',
  hint: '工具设成「始终允许 · 门控把关」时，每次调用前由它看一眼参数。留空就用默认接入的默认模型。每次调用都会多问它一次，建议选又快又便宜的小模型。',
  providerDefault: '默认接入',
  modelDefault: '留空：用接入的默认模型',
} as const

/** 设置页「运行默认值」里 agent 护栏那一组（后端 engine/guards.py） */
export const AGENT_GUARD_TEXT = {
  label: '智能体护栏',
  hint: '智能体不再被一个固定步数掐断：同样的调用第二次不执行、连续 3 步没拿到新信息、预算用完、上下文快满时，它会按查到的部分收尾。这里是每个智能体节点的默认上限，节点上可以单独改。只影响以后发起的运行。',
  steps: '默认最大步数',
  stepsHint: '只是兜底，正常任务碰不到',
  tokens: '令牌预算（每个节点）',
  tokensHint: '输入加输出。留空表示不限',
  usd: '金额预算（美元）',
  usdHint: '按模型目录里的价格估算；没登记价格的模型估不出来，只能靠令牌预算',
  unlimited: '不限：费用只受步数兜底和上下文约束',
  unlimitedShort: '不限',
} as const
