/**
 * 术语表与文案规范：界面上同一件事只有一种叫法。全站文案从这里取，别处的说明照这里写。
 *
 * 三层彼此独立：发布等级、运行类别、出具档位。这是「受限动态编排」的骨架，一次正式运行完全
 * 可能降档出具，说法必须分开。
 *
 * 术语（2026-09 文案整改拍板，全站照此执行）：
 * - 工作流层面的动作叫「运行」：发起运行、继续运行、重新运行、试运行、运行中、运行结束。
 *   节点、工具层面的动作叫「执行」：执行中、已执行、未执行、执行工具。不用「跑」。
 * - 时长：总时长 / 执行时长 / 等待审批。不用「墙钟」「等人」。
 * - 服务：用户可见文案写「服务端」，连接类提示写「无法连接服务」。不写「后端」。
 * - 右栏叫「助手」。用户可见文案不写 Copilot，也不用「它」指代助手。
 * - Agent 节点写「Agent」（首字母大写），不写 agent、智能体。
 * - token 写「token」，数量写「2.8k token」。不写 tokens、tok、令牌。
 * - 「工作流」指存下来的资产，量词用「个」；「画布」指编辑区；「图」只在技术细节里出现（执行图）。
 * - 版本：固定版本、引用版本。不写钉住、钉。
 * - 模型服务叫「模型接入」（「设置 → 模型接入」）。不写 provider、供应商。
 * - 模型提示叫「系统提示」。不写角色设定、system。
 * - 节点之间叫「连线」，不写「边」。
 * - 审批结果：批准 / 驳回，不写放行、拒绝。人工节点叫「人工审批」，Agent / 工具 / 代码上的选项叫
 *   「审批策略」，待处理的那张卡叫「审批卡」。
 * - 治理级别叫「受管级别」，不写「受管模板」。
 * - 出口节点的叫法和节点库里的节点名一致（NODE_TYPE_LABEL）。
 * - 「时间线」是事件列表，「航迹」是时间轴图，两个面板名各指各的。
 * - 报错、修复选项、说明里写界面上的中文标签（「单元格引用」「报告来自」），不在句子里裸写键名和
 *   枚举值；配置界面里字段自身旁边可以附键名。
 * - 导航里的历史页叫「记录」。
 *
 * 语体：
 * - 书面、简洁、中性。用「无法 / 未能 / 失败 / 请」，不用「取不到 / 连不上 / 写坏了」这类口语补语；
 *   不用「多半、照样、眼下、真的、压根」和句尾的吧、呢、哦。
 * - 不拟人；不写改版历史（兼容说明挪进代码注释）；不露内部实现（checkpoint 写「断点」，superstep
 *   写「同一步」）。
 * - 报错按「发生了什么 + 原因（可选）+ 下一步」写，下一步指到真实存在的界面入口。
 * - 按钮、标签、标题尽量短；说明超过约 40 个字的考虑拆开或删减。
 * - 全角标点，「」引号，省略号用「…」。
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
export const UNSAVED_HINT = '本次运行使用的是在画布或问数据中临时构建的工作流，未保存，因此无法在画布中打开'
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

/** 未知类型原样返回：助手可能生成不存在的类型，显示原码比显示空白好查 */
export function nodeTypeLabel(type: string | null | undefined): string {
  if (!type) return '—'
  return TYPE_LABEL[type] ?? type
}

/**
 * 审批策略的选项文字：Agent / 工具 / 代码三处共用一套说法。
 * never 的叫法和后端 autofix._APPROVAL_LABEL、governance 的报错同一套（2026-09 由「全部自动放行」改名）
 */
export const APPROVAL_POLICY_LABEL: Record<'dangerous' | 'always' | 'never', string> = {
  dangerous: '仅危险工具需要审批',
  always: '每次调用都审批',
  never: '全部无需审批',
}

/**
 * 可点击证据：片段状态的叫法。正文里片段的 aria-label、面板标题、图例都用这一份。
 * 外观（线型、字形、颜色）在 lib/evidence.ts，和这里一一对应。
 *
 * 概率性的几种（四期的结论句裁判）挂在句末的徽标上：有依据、部分有依据、证据相矛盾、证据不足都是模型的判断，
 * 叫法里写明「模型判断」；未裁判不带「有引用」——没挂依据、只有方向词的结论句也会送裁判、也会没判。
 * 证据不支持是裁判拆档之前的取值：已封存的文档、已有的事件里保留原样，照旧这样叫（新判的不再出现）。
 */
export const EVIDENCE_STATE_LABEL = {
  deterministic: '有出处',
  supported: '模型判断：有依据',
  partial: '模型判断：部分有依据',
  contradicted: '模型判断：证据相矛盾',
  insufficient: '模型判断：证据不足',
  unsupported: '模型判断：证据不支持',
  unjudged: '未裁判',
  none: '无证据',
  connective: '连接性文字',
  candidate: '猜测的来源',
  suspect: '疑似不存在的名称',
  unverified: '无法核实',
} as const

/**
 * 「7/12 数字有出处 · 无证据 5 · 另有 1 处引用无法解析」：出具横幅、报告核对那一行、证据条、
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
  if (other > 0) parts.push(`${total > 0 ? '另有 ' : ''}${formatNumber(other)} 处引用无法解析`)
  return parts.join(' · ')
}

/** 证据面板和证据条的文案 */
export const EVIDENCE_TEXT = {
  allCited: (n: number) => `${formatNumber(n)} 个数字都有出处`,
  locateNext: '定位下一处',
  /** 读屏摘要末尾的操作说明 */
  keysHint: '用左右方向键逐个查看，上下方向键按句切换，n 跳到下一处无证据，回车打开证据，Esc 关闭',
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
  recomputeBad: '复算不一致：代入式的计算结果与记录值不符',
  recomputeNone: '无法复算',
  input: '运行输入',
  seal: '封存',
  sealOk: '已封存 · 核对一致',
  sealDocOk: '报告文档已封存 · 核对一致（本段自身没有需要封存的证据）',
  /** 片段接口取不到、封存状态是从整次运行的证据图查的：只核对了文档在封存范围内，这一段的链没逐项核对 */
  sealDocChain: '报告文档已封存 · 核对一致（未能获取本段的证据链，未逐项核对）',
  sealBad: '已封存 · 核对不一致',
  sealOpen: '尚未封存',
  sealOutside: '不在封存范围内',
  /** 正文这份报告不是封存的那一份：封存链再完好，也证明不了屏幕上这些字 */
  sealForeign: '正文不是封存的那份报告，封存结果不能证明此处的内容',
  sealUnknown: '无法获取封存状态',
  violations: '违规清单',
  violationsHint: '报告撰写节点核对出的全部问题。列表序号、代码块标签中的数字无法在正文中标注，仅在此处列出',
  structural: '该数字位于列表序号、代码块标签等 Markdown 语法中，无法在正文中标注',
  noSegment: '该条在正文中没有对应文本，例如句末依据中写错的引用',
  /** 读屏摘要：画不了线的两种，分开说在哪（和违规清单里每条的 structural / noSegment 同一个意思） */
  structuralCount: (n: number) => `其中 ${formatNumber(n)} 个数字位于列表序号、代码块标签等 Markdown 语法中，无法在正文中标注，仅在违规清单中列出`,
  noSegmentCount: (n: number) => `${formatNumber(n)} 处在正文中没有对应文本（例如句末依据中写错的引用），仅在违规清单中列出`,
  /** 出具横幅：回指不上的数字。有报告文档时点状线只画得出一部分（列表序号里的画不了），不能说「已用虚线标出」 */
  unmatchedInDoc: '无法追溯的数字（逐条见报告的违规清单）：',
  unmatchedPlain: '无法追溯的数字：',
  uncited: '该数字未使用引用标记，系统无法核对其来源',
  missingValue: '缺少输入：口径卡本次未计算出该指标的值',
  unshowable: (value: string) => `有值（${value}），但无法按口径卡的格式显示`,
  chainPending: '正在加载算式和输入…',
  /** 证据接口逐项复核出来的问题。正常时不说，出问题才醒目 */
  integrity: {
    eid: '证据标识不一致：目录记录与按工件重新计算的结果不同',
    hash: '口径卡工件的哈希校验失败，或口径卡中没有该指标',
    render: '按口径卡重新渲染的文本与报告中的不一致',
    sealed: '该证据不在封存范围内，为事后补充，不能作为证据',
    /** 证据接口答的是封存范围内那份报告，和正文这份不是同一份（工件 id 或这个位置的字对不上） */
    doc: '正文这份报告不是封存范围内的那一份：以下出处来自正文自身的记录，未经封存核对',
    /** 证据图里正文这份报告的哈希对不上 */
    docHash: '正文这份报告的哈希校验失败，可能已被修改',
    sealedText: (text: string) => `封存的报告在此位置的内容为「${text}」`,
  },
  chainMissing: '未能获取算式、代入式和输入',
  chainForeign: '获取到的算式属于封存的那份报告，与正文这份不一致，不在此处展示',
  noRun: '这份报告不属于任何运行，只能查看文档中记录的出处',
  docMissing: '无法获取证据文档，按普通文本显示',
  docMismatch: '成果字段与报告文档不一致，按普通文本显示',
  docOtherRun: '证据文档不属于本次运行，按普通文本显示',
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
    `已遮罩的列：${cols.join('、')}。遮罩只减少暴露，不是安全边界：完整快照和原始工件中仍为原值`,
  queryMissing: '未能获取查询结果的行',
  queryPending: '正在加载查询结果的行…',
  queryHash: '查询快照哈希校验失败，可能已被修改：这些行不能作为证据',
  truncatedSnapshot: '查询结果已达行数上限，数据库中还有更多数据',
  windowCut: (n: number) => `被引用的行较多，此处仅列出前 ${formatNumber(n)} 行，其余请查看完整快照`,
  sources: '输入来源',
  gotoQuery: (alias: string) => `查看查询 ${alias}`.trim(),
  sourceVerified: '与快照一致',
  sourceMismatch: (told: string, truth: string) => `模型给出 ${told}，快照值为 ${truth}，已采用快照值`,
  /** cell() 取数：算的时候用的值和快照不一样，值没有被换掉，照实说上游改写过 */
  cellMismatch: (used: string, truth: string) => `计算时使用的值为 ${used}，快照值为 ${truth}：上游改写过这份结果`,
  sourceEmpty: '未查询到，记为空值（未按 0 处理）',
  cellUnresolved: '无法核对查询快照',
  codeCompute: (node: string) =>
    `由代码节点${node ? `「${node}」` : ''}计算得出：系统无法核对沙箱中的运算，业务计算应定义在口径卡中`,
  codeSource: (node: string) =>
    `来自代码节点${node ? `「${node}」` : ''}（标为取数）：沙箱执行结果，无法与查询快照核对`,
  inputMissing: '缺少：上游未提供该值',
  caliberFrom: (caliber: string, version: string, workflow: string, wfVersion: string) =>
    `口径卡「${caliber || '—'}」${version}，来自「${workflow}」${wfVersion}`,
  caliberUpgrade: (latest: string | null, policy: string) =>
    `${latest ? `上游已有 ${latest}` : '上游有新版本'}，按「${policy}」处置`,

  // ---- 第三期：表名字段名、逐字引文、旧运行的猜测 ----
  entity: '表结构',
  table: '表',
  column: '字段',
  entityQueries: (aliases: string[]) => `出现在查询 ${aliases.join('、')}`,
  entityNoQuery: '本次查询未用到它，仅出现在表结构快照中',
  syncedAt: (at: string) => `表结构快照同步于 ${at}`,
  snapshotPartial: '该数据源的表数量较多，表结构快照仅包含部分表',
  columnType: '字段类型',
  ownerTables: (tables: string[]) => `多张表包含此字段：${tables.join('、')}`,
  entitySource: { schema: '表结构快照', sql: '查询 SQL 用到的表', result: '查询结果列' } as Record<string, string>,
  entityPending: '正在加载表结构…',
  entityMissing: '未能获取字段类型和同步时间',
  /** 反引号里写了个名字，哪里都没有（没有 cite 时的原因；有 cite 的用后端给的原话） */
  suspect: '疑似不存在的名称：本次运行的表结构快照、查询用到的表和查询结果列中都没有这个名称',
  unverified: '表结构快照仅包含部分表，查询用到的表和查询结果列中也没有这个名称，无法核实它是否存在',
  suspectTag: '可疑名称',
  /** 出具横幅那一行：报告要求结论句挂依据（claims: require_citation）时，没挂的有几句 */
  uncitedClaimsTag: '未附依据的结论句',
  closest: '最接近的已知名称',
  checked: (schemas: number, queries: number) =>
    `已核对 ${formatNumber(schemas)} 份表结构快照、${formatNumber(queries)} 次查询`,
  tableColumns: (n: number, view: boolean) => `${view ? '视图' : '表'} · ${formatNumber(n)} 个字段`,
  /** 表名实体步骤的字段清单（来自封存范围内的表结构快照，最多列 60 个，遮罩的列不列入） */
  fields: (n: number) => `字段清单（${formatNumber(n)} 个）`,
  fieldsMore: (n: number) => `另有 ${formatNumber(n)} 个字段未列出`,
  fieldsMasked: '部分字段已按数据源的遮罩设置隐藏，未列出',
  columnTypes: (types: Record<string, string>) =>
    `各表的类型：${Object.entries(types).map(([t, v]) => `${t} ${v}`).join('、')}`,
  quoteBad: '按记录的位置从原文中截取的内容与这句引文不一致：原文可能已被修改',
  quoteCollection: (c: string) => `知识库「${c}」`,
  closestNone: '没有相近的已知名称',
  quote: '逐字引文',
  quoteFrom: (title: string) => `出自「${title}」`,
  quoteChunk: (n: number) => `第 ${formatNumber(n)} 段`,
  quoteHit: '引文在原文里的位置（高亮）',
  quotePending: '正在加载原文…',
  quoteMissing: '未能获取原文',
  quoteMiss: '原文中未找到这句引文：引文与原文并非逐字一致',
  quoteHash: '检索快照哈希校验失败，可能已被修改：原文不能作为证据',
  /** 旧运行：按数值猜的候选 */
  guessTitle: '猜测的来源',
  guessNote: '猜测的来源，不能作为证据：按数值在本次运行已封存的查询结果和口径卡中查找相同的值，数值相同可能只是巧合',
  guessCount: (guessed: number, numbers: number) =>
    numbers ? `${formatNumber(numbers)} 个数字中有 ${formatNumber(guessed)} 个找到了可能的来源` : '答案中没有数字',
  guessNone: '未找到数值相同的单元格',
  guessDiff: (d: string) => `相差 ${d}`,
  guessCandidates: '可能来自',
} as const

/** 结论句按裁判结论分成的几堆（lib/evidence 的 claimCounts 数出来的） */
export interface ClaimTallyCounts {
  /** 结论句总数：模型判为「不是结论句」的不算 */
  total: number
  supported: number
  partial: number
  /** 证据和原句冲突 */
  contradicted: number
  /** 证据里没有判断所需的信息 */
  insufficient: number
  /** 拆档之前的取值（旧文档、旧事件），照旧计入 */
  unsupported: number
  /** 送了裁判、没判成（到上限、调用失败）或还没请模型判断的 */
  unjudged: number
  /** 没有判定、也没挂依据的结论句 */
  uncited: number
}

/**
 * 「结论 4 句（有依据 3 · 无证据 1）」：出具横幅、证据条、报告核对那一行共用，和数字那一段同一种说法——
 * 先说总数，括号里按状态分，为 0 的不写。没有结论句时返回空串
 */
export function claimTally(c: ClaimTallyCounts): string {
  if (!c.total) return ''
  const parts = [
    ['有依据', c.supported], ['部分有依据', c.partial], ['证据相矛盾', c.contradicted], ['证据不支持', c.unsupported],
    ['证据不足', c.insufficient], ['未裁判', c.unjudged], ['无证据', c.uncited],
  ].filter(([, n]) => typeof n === 'number' && n > 0).map(([k, n]) => `${k} ${formatNumber(n as number)}`)
  return `结论 ${formatNumber(c.total)} 句${parts.length ? `（${parts.join(' · ')}）` : ''}`
}

/**
 * 结论句裁判（四期）在证据面板、句末徽标里的说法。判断是模型给的：一律写明「模型判断」「非确定」，
 * 封存之后按需追加的另写「封存后追加」
 */
export const JUDGE_TEXT = {
  section: '模型的解释',
  claim: '结论句',
  /**
   * 读屏摘要末尾的操作说明：结论句有问题（证据相矛盾、部分有依据、证据不足）时 n 也跳到它们的句末徽标。
   * 证据不足的排在其余问题之后
   */
  keysHint: '用左右方向键逐个查看，上下方向键按句切换，n 跳到下一处无证据或证据有问题的句子（证据不足的排在最后），回车打开证据，Esc 关闭',
  /** 面板里判断的徽标：谁判的、而且不是确定的 */
  badge: (model: string | null | undefined) => `模型判断 · ${model || '裁判模型'} · 非确定`,
  postSeal: '封存后追加',
  /** 判定里没记模型名时的叫法 */
  judgeModel: '裁判模型',
  /** 句末徽标的悬停说明：到上限没判的 */
  limitNotJudged: '已达上限，本句未经裁判',
  /** 没判的句子（到上限、没跑成）记着的裁判模型：中性地说，不挂「模型判断」的徽标 */
  notJudgedBy: (model: string) => `裁判模型 ${model} 未裁判本句`,
  sealedDoc: '报告文档已封存 · 核对一致',
  sealedWithDoc: '报告文档已封存 · 核对一致，这条判断随报告一起封存',
  postSealHint: '运行封存后按需追加的判断：不在封存范围内，不影响封存核对结果。这是模型的解释，不是证据',
  sealedHint: '正式运行中由报告撰写节点当场裁判，与报告一起封存。这是模型的解释，不是证据',
  used: (aliases: string[]) => `裁判参考的证据：${aliases.join('、')}`,
  cites: '附带的依据',
  noCites: '本句未附依据（仅含方向词或因果词），裁判模型没有可参考的证据摘录',
  notClaim: '模型判断：不是结论句',
  ask: '请模型判断本句',
  askAgain: '再次请模型判断',
  /** 封存后追加的判定早于当前的裁判规则（片段接口的 on_demand.reason 为 outdated）：旁边注明，按钮照常给 */
  outdated: '按旧规则判定',
  asking: '正在请模型判断…',
  askHint: '探索运行按需裁判：每次点击裁判一句，费用计入单次点击上限和每日上限',
  notAsked: '尚未请模型判断本句',
  screened: '预筛判定本句不陈述数据事实（短句或过渡语），未送裁判',
  /** 证据不足时缺的是什么（裁判给的，如「SQL」「字段清单」）：面板、句末徽标的悬停说明和读屏都写 */
  missingLead: '缺少：',
  missing: (what: string) => `缺少：${what}`,
  /** 裁判模型的两条提醒：判定照常给出，读的人要知道分量 */
  sameModel: '裁判模型和写作模型相同，结果仅供参考',
  sameModelTitle: (model: string) => `裁判模型与撰写这份报告的模型都是 ${model}：同一模型较难发现自身的错误`,
  noPrice: '该模型不在价格表中，金额上限不生效',
  noPriceTitle: '无法按 token 数估算这个模型的费用，裁判的金额上限（每份报告、每次点击、每日）对它不起作用；句数和时长上限照常生效',
  /** 讲方法、讲结构的句子（只挂了表名、字段名、整份查询）：「挂的依据」下直接列出 SQL 和字段清单 */
  basisSql: (alias: string) => `查询 ${alias} 的 SQL`,
  basisFields: (table: string) => `表 ${table} 的字段清单`,
  basisFieldsCount: (table: string, n: number) => `表 ${table} 的字段清单（${formatNumber(n)} 个）`,
  basisColumns: '字段类型',
  basisPending: '正在加载 SQL 和字段清单…',
  sqlMissing: '未能获取这次查询的 SQL',
  fieldsMissing: '未能获取字段清单',
  basisNoRun: '这份报告不属于任何运行，无法获取 SQL 和字段清单',
  basisMoreQueries: (n: number) => `另有 ${formatNumber(n)} 次查询，点开句中的表名查看`,
  limit: '已达上限',
  /** 触顶之后怎么调。按需裁判（每次点击）和正式运行（每份报告）调的地方不一样 */
  limitHow: {
    click: {
      max_cost_usd: '前往「设置 → 偏好设置 → 证据裁判」调高「每次点击的金额上限」或设为不限，然后再次点击',
      daily_max_usd: '前往「设置 → 偏好设置 → 证据裁判」调高「每日金额上限」或设为不限；也可等到次日（按本地日期重新计算）',
      max_claims: '前往「设置 → 偏好设置 → 证据裁判」调高「每份报告最多裁判句数」（报告撰写节点中已设置的，以节点为准）',
      timeout_s: '前往「设置 → 偏好设置 → 证据裁判」调高「每份报告的时长上限」或设为不限，然后再次点击',
    } as Record<string, string>,
    report: {
      max_cost_usd: '在报告撰写节点的「结论句裁判」中调高金额上限（未设置时取「证据裁判」设置的默认值）或设为不限，然后重新发起运行',
      daily_max_usd: '前往「设置 → 偏好设置 → 证据裁判」调高「每日金额上限」或设为不限，然后重新发起运行',
      max_claims: '在报告撰写节点的「结论句裁判」中调高最多裁判句数（或设置中的默认值）或设为不限，然后重新发起运行',
      timeout_s: '在报告撰写节点的「结论句裁判」中调高时长上限（或设置中的默认值）或设为不限，然后重新发起运行',
    } as Record<string, string>,
  },
  failed: (why: string) => `裁判失败：${why}`,
  oldBackend: '当前服务版本不支持按需裁判',
  notFound: '报告中没有这一句',
  formalOnly: '正式运行的裁判由报告撰写节点当场完成，不能按需追加',
  rewritten: '本句已按裁判意见改写（改写一次）',
  rewriteFrom: '改写前的原句',
  rewriteRejected: (reason: string) => `本句曾退回改写一次，改写稿未被采用：${reason}`,
  /**
   * 横幅上结论句那一段之外另起的「没挂依据的结论句 N」：开了裁判的文档里，没挂依据的句子按判定数（或预筛
   * 放掉了不数），出具却照样按没挂依据计缺口——悬停时说清这两件事不冲突
   */
  uncitedBesides: '未附依据的结论句仍计入出具缺口，经模型判断的也包括在内：判断是模型的解释，不能替代依据',
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
  max_claims: '最多裁判句数',
  max_cost_usd: '金额上限（美元）',
  timeout_s: '时长上限（秒）',
  rewrite_once: '证据相矛盾或不足的句子退回改写一次',
  // 键名仍是 on_unsupported（兼容已有配置）；裁判拆档后它管的是「证据相矛盾」（旧取值证据不支持照旧按它判），
  // 证据不足的句子最多降档
  on_unsupported: '证据相矛盾时',
}
/** judge.on_unsupported 的说明：检查器里选项下面那一行 */
export const JUDGE_ON_UNSUPPORTED_HINT = '仅对正式运行生效；证据不足的句子最多降档，不会因此不予出具。探索运行只做标注，不拦截'
/** judge.on_unsupported：正式运行里证据相矛盾的结论句怎么判档（探索运行只标注） */
export const JUDGE_ON_UNSUPPORTED_LABEL: Record<string, string> = {
  degrade: '出具降档',
  withhold: '不予出具',
}

/** 设置页「证据裁判」一组（后端 engine/judge.py 的 JUDGE_DEFAULTS） */
export const JUDGE_SETTING_TEXT = {
  label: '证据裁判',
  hint: '报告中的结论句（如「增长主要来自新客首单」）由另一个模型按证据逐句判断：正式运行由报告撰写节点当场裁判，探索运行在点击时逐句裁判。判断由模型给出，不是系统核对，界面上一律标注「非确定」。',
  provider: '裁判模型 · 接入',
  model: '裁判模型 · 模型',
  /** 接入留空、模型也留空：后端依次用 Copilot 的模型、默认接入（judge_model_spec）。只有这时才叫「跟随 Copilot」 */
  providerDefault: '跟随助手的模型',
  /** 接入留空、模型填了：这一级写了就整组用这一级，后端按模型名找接入——不再跟随 Copilot */
  providerFromModel: '按模型名找接入',
  modelDefault: '留空：用接入的默认模型',
  modelFollow: '留空：跟随助手的模型',
  differ: '建议与撰写报告的模型不同：由同一模型审核自身输出，不易发现其中的错误',
  /** 接入和模型都没填：后端依次用助手的模型、默认接入。醒目地说一句，并给出建议 */
  notSet: '未单独配置，将使用助手的模型',
  notSetAdvice: '建议配置一个与撰写报告的模型不同、能力更强的模型：同一模型审核自身输出时，结果仅供参考',
  limits: '上限（每一项都可以设成不限）',
  reportClaims: '每份报告最多裁判句数',
  reportCost: '每份报告的金额上限（美元）',
  reportTimeout: '每份报告的时长上限（秒）',
  clickCost: '每次点击的金额上限（美元）',
  daily: '每日金额上限（美元）',
  unlimited: '不限',
  nodeWins: '报告撰写节点的「结论句裁判」中已设置的上限，以节点为准',
  spend: (usd: string, calls: number) => `今日已花费 $${usd}（${formatNumber(calls)} 次调用）`,
  unpriced: (n: number) => `另有 ${formatNumber(n)} 次调用无法估算金额（模型不在价格目录中），未计入`,
  spendNone: '今日尚无裁判调用',
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
  const follow = scope === 'settings' ? '「跟随助手的模型」仅在接入和模型都留空时生效'
    : '接入和模型都未填写时，才跟随设置中「证据裁判」的模型'
  if (found && !found.enabled) {
    return `只填了模型：「${model}」属于已停用的接入「${found.name}」，裁判调用将失败，结论句均记为未裁判。请启用该接入或选择其他接入`
  }
  if (found) return `只填了模型：将按模型名使用接入「${found.name}」。${follow}`
  return `只填了模型：所有接入的模型列表中都没有「${model}」，将使用默认接入${fallback ? `「${fallback}」` : ''}调用；`
    + `调用失败时结论句均记为未裁判。如该模型不属于默认接入，请选择对应的接入。${follow}`
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
    case 'claims': return '不设上限，句数仅受报告中结论句数量约束'
    case 'timeout': return '不设上限，时长仅受模型接口自身的超时约束'
    case 'cost': {
      const by = list([[set.claims, '句数上限'], [set.timeout, '时长上限'], [set.daily, '每日上限']])
      return by.length ? `不设上限，费用仅受${by.join('、')}约束` : '不设上限，费用仅受报告中结论句数量约束（句数、时长、每日也都不限）'
    }
    case 'click': {
      const by = list([[set.claims, '句数上限'], [set.timeout, '时长上限'], [set.daily, '每日上限']])
      return by.length ? `不设上限，每次点击仅受${by.join('、')}约束` : '不设上限，每次点击仅受所点击的句数约束（句数、时长、每日也都不限）'
    }
    case 'daily': {
      const by = list([[set.cost, '每份报告的金额上限'], [set.click, '每次点击的金额上限']])
      return by.length ? `不设上限，每日费用仅受${by.join('和')}约束` : '不设上限，每日费用仅受模型调用次数约束（每份报告、每次点击也都不限）'
    }
  }
}

/**
 * 记录页「证据」页签：报告、常驻面板、审计表。审计表按状态分组，四组的说法和片段状态同一套
 */
export const EVIDENCE_AUDIT_TEXT = {
  tab: '证据',
  tabTitle: '查看报告各段的出处：左侧为报告，右侧为证据，下方为完整清单，支持筛选和导出',
  title: '证据清单',
  groups: {
    none: '无证据',
    suspicious: '可疑名称',
    cited: '有出处',
    candidate: '按数值猜测',
  } as Record<string, string>,
  groupHint: {
    none: '裸数字、无法解析的引用、未附依据或模型判为证据有问题的结论句',
    suspicious: '表名、字段名在本次运行的表结构和查询中找不到',
    cited: '系统从证据中取值并核对过的片段',
    candidate: '按数值猜测的可能来源，不能作为证据，默认收起',
  } as Record<string, string>,
  filterLabel: '清单筛选',
  filterAll: '全部',
  filterProblems: '只看无证据 / 可疑名称',
  cols: { text: '片段', state: '状态', source: '出处 / 原因', sentence: '所在的句子', report: '报告', seal: '封存' },
  sealIn: '在封存范围内',
  sealOut: '不在封存范围内',
  sealNa: '—',
  exportJson: '导出 JSON',
  exportCsv: '导出 CSV',
  exported: (name: string) => `已导出 ${name}`,
  exportLocal: '当前服务版本不支持导出，已按页面上的清单导出（未经服务端封存核对）',
  fallbackUnsupported: '当前服务版本不提供审计数据：清单根据页面上的报告生成，封存状态取自本次运行的证据记录',
  fallbackError: (why: string) => `审计数据获取失败（${why}）：清单根据页面上的报告生成`,
  empty: '此组没有片段',
  emptyProblems: '没有无证据或可疑名称的片段',
  hidden: '无法在正文中标注，仅在此处列出',
  hiddenWhy: '该处位于列表序号、代码块标签、粗体、链接或句末依据中，正文中没有可标注、可点击的片段，仅在此清单中列出',
  keys: '在清单中用上下方向键逐行切换，回车在右侧打开该段的证据',
  panelIdle: '点击报告中的片段或清单中的一行，此处显示其出处',
  sealTitle: '封存核对',
  sealOk: (seq?: number) => `已封存 · 核对一致${seq != null ? `（封存到第 ${formatNumber(seq)} 条事件）` : ''}`,
  sealBad: '已封存 · 核对不一致：封存后有事件被修改、删除或插入',
  sealOpen: '尚未封存：运行尚未结束，证据可能仍会变化',
  sealUnknown: '无法获取封存状态',
  docOk: (node: string) => `报告「${node}」的文档哈希一致`,
  docBad: (node: string) => `报告「${node}」的文档哈希校验失败，可能已被修改`,
  docGone: (node: string) => `无法从工件库取回报告「${node}」的文档`,
  mode: {
    none: '本次运行没有报告文档，也没有出具契约：没有可逐段展示的证据',
    legacy_contract: '本次运行按数值匹配口径卡，未使用显式引用。同一个值与多个指标相符时，出处不唯一',
    legacy_text: '本次运行没有出具契约：以下是按数值猜测的可能来源，默认收起',
  } as Record<string, string>,
  modeLabel: { cited: '逐段引用', none: '没有证据', legacy_contract: '按数值匹配', legacy_text: '无出具契约' } as Record<string, string>,
  legacyMatched: (n: number) => `按数值匹配到 ${formatNumber(n)} 个数字`,
  legacyUnmatched: (n: number) => `${formatNumber(n)} 个未能匹配`,
  legacyNoField: '匹配的位置与成果中的任何字段都不对应，只列出数字，不在正文中标注',
} as const

/** 报告节点卡上的章：这个工作流最近一次运行的报告核对统计 */
export const reportStampText = (cited: number, none: number) =>
  `有出处 ${formatNumber(cited)} · 无证据 ${formatNumber(none)}`

/**
 * 口径升版的三种处置，和后端 governance.UPGRADE_POLICY_LABELS 同一套说法。子工作流和
 * 固定版本的口径卡（caliber_from）共用。dual 在 2026-09 由「并排双印新旧口径」改名，后端需同步
 */
export const UPGRADE_POLICY_LABEL: Record<string, string> = {
  recompute: '用新口径回算历史',
  dual: '新旧口径并列展示',
  incomparable: '标注与历史不可比',
}

/**
 * 子工作流固定版本后的升版处置说明。选项和口径卡的那个下拉是同一份（检查器直接借用口径卡
 * 的字段定义），这里只换掉说明里「和谁同一套」那半句
 */
export const SUBGRAPH_UPGRADE_HELP = '所固定的版本之后，被引用的工作流又发布了新版本：发起正式运行前须声明处置方式，'
  + '规则与引用其他工作流的口径卡相同；声明后按声明执行，并记入运行记录'

/** 上游在固定的版本之后又发了版：子工作流和口径卡的提醒同一句 */
export const upgradeNewerText = (latest: number, pinned: number) =>
  `上游已有 v${latest}（固定的是 v${pinned}）：发起正式运行前须在下方声明升版处置，否则正式运行将被拦截`

/**
 * 发布前检查与自动修复：发布弹窗和问题面板的「发布前检查」共用一套说法。
 * 原则写在话里：修复只出预览、人确认了才存草稿，永远不替人发布
 */
export const PUBLISH_FIX_TEXT = {
  section: '发布前检查',
  lint: '校验',
  checking: '正在进行发布前检查…',
  unsupported: '当前服务版本不支持发布前检查。点击「发布」时仍会执行门禁检查，被拦截的问题将列在此处',
  failed: '发布前检查失败',
  retry: '重试',
  recheck: '重新检查',
  run: '检查',
  stale: '画布已修改，结果可能已过时',
  blocked: (n: number) => `发布前检查：${n} 处问题将被门禁拦截`,
  gateBlocked: (n: number) => `门禁拦截了 ${n} 处问题，请修改后再发布`,
  passed: '发布前检查通过：没有将被门禁拦截的问题',
  warnings: (n: number) => `另有 ${n} 条提示`,
  fixAll: (n: number) => `一键修复可自动修复的 ${n} 处`,
  fixOne: '修复',
  fixHint: (label: string) => `先查看预览：${label}`,
  choose: '预览',
  chooseFirst: '请先选择再预览',
  multiple: '可以多选',
  suggested: '建议',
  assist: '交给助手',
  assistHint: '由助手尝试修复其余问题。修改结果同样仅为预览，需要人工决定的事项会逐一向你确认',
  locked: (why: string) => `${why}。暂时无法修复`,
  previewing: '正在生成修复预览…',
  assisting: '助手正在尝试修复，可能需要数十秒…',
  stop: '停止',
  previewTitle: (n: number) => `修复预览 · ${n} 处改动`,
  noChange: '本次没有可应用的改动',
  rejected: '未采用',
  assistSaid: '助手',
  questions: '以下事项需要你确认',
  afterOk: '应用后可通过门禁',
  afterLeft: (n: number) => `应用后仍有 ${n} 处将被门禁拦截`,
  apply: '应用并重新检查',
  discard: '放弃',
  applyNote: '应用后保存为新的草稿版本并重新检查；不会自动发布',
  dirtyNote: '画布上还有未保存的修改，应用时将一并保存到草稿',
  saving: '正在保存草稿…',
  rechecking: '正在重新检查…',
  saveFailed: '草稿保存失败',
  saveFailedHint: '修复已应用到画布（可撤销）；保存成功前无法发布',
  retrySave: '重试保存',
  stalePreview: '预览生成后画布已修改，此预览已失效：请重新检查后再修复',
  autofixMissing: '当前服务版本不支持自动修复：请按提示手动修改，或在助手中描述需求',
  autofixFailed: '修复预览生成失败',
  applied: (n: number, version?: number) => `已应用 ${n} 处修复${version != null ? `，保存为 v${version}` : ''}`,
  saveNote: (labels: string[]) => `发布前修复：${labels[0] ?? ''}${labels.length > 1 ? ` 等 ${labels.length} 处` : ''}`,
  /** 撤销栈里这一步叫什么 */
  undoLabel: (n: number) => `发布前修复（${n} 处）`,
  pendingPreview: '请先应用或放弃修复预览',
  unsaved: '修复尚未保存',
  whole: '整个工作流',
} as const

/**
 * 一键升级为可追溯结构（问题面板的快速修复、记录页的横幅）。和发布前修复同一套规矩：
 * 只出预览，人点「应用」才走现有的保存存成草稿；不发布
 */
export const UPGRADE_TEXT = {
  advice: '可升级为可追溯结构',
  adviceHint: '报告将改由「报告撰写」节点生成，引用的数字和表名可以点开查看出处。先查看预览，确认后才会修改',
  action: '升级为可追溯结构（预览改动）',
  previewing: '正在计算升级所需的改动…',
  assisting: '助手正在把代码节点中的计算改写为口径卡表达式，可能需要数十秒…',
  title: (n: number) => `升级预览 · ${n} 处改动`,
  noChange: '此工作流无需升级',
  summary: (parts: string[]) => `合计：${parts.join('、')}`,
  added: '新增节点',
  edge: '连线',
  rewired: '改接为',
  addedEdge: '新连线',
  removedEdge: '去掉的连线',
  typeField: '节点类型',
  notes: '需要你确认的事项',
  assist: '交给助手改写计算逻辑',
  assistHint: '将只做加减乘除的代码节点改写为口径卡表达式；非纯算术的节点保持不变并给出警告。修改结果同样仅为预览；需要调用模型，可能需要数十秒',
  assistWarnings: '未修改的节点',
  rejected: '未采用',
  afterOk: '升级后校验和门禁均无新的错误',
  afterLeft: (n: number) => `升级后仍有 ${n} 处错误，应用后请在问题面板中继续修改`,
  apply: '应用并保存',
  applyNote: '应用后保存为新的草稿版本；不会发布，重新发布后正式运行才会使用',
  saveFailedHint: '升级已应用到画布（可撤销），尚未保存到草稿',
  discardUnsavedHint: '仅收起此预览：已应用到画布的升级会保留，如需退回请撤销；之后按常规方式保存',
  stale: '预览生成后画布已修改，此预览已失效：请按当前画布重新预览',
  again: '重新预览',
  applied: (version?: number) => `已升级为可追溯结构${version != null ? `，保存为草稿 v${version}` : ''}`,
  appliedDraft: '已升级为可追溯结构：画布尚未保存为工作流，保存后生效',
  saveNote: '升级为可追溯结构',
  undoLabel: '升级为可追溯结构',
  unsupported: '当前服务版本不支持一键升级：请在助手中描述需求，或按提示手动修改',
  failed: '升级预览生成失败',
  locked: (why: string) => `${why}。暂时无法升级`,
  runBanner: '本次报告没有逐段证据',
  runAction: '升级此工作流',
  runHint: '打开编排页查看升级所需的改动；确认后才会修改，修改的是草稿',
  runFormal: '正式运行使用已发布的版本：升级后需要重新发布，下一次正式运行才会生效',
  runLocked: (why: string) => `${why}。暂时无法升级此工作流`,
} as const

/** 证据的种类怎么叫：aria-label、面板标题、出处那一句 */
export const EVIDENCE_KIND_LABEL: Record<string, string> = {
  metric: '口径卡指标',
  input: '运行输入',
  cell: '查询结果',
  query: '查询结果',
  table: '整表',
  retrieval: '知识库检索',
  node_output: '代码节点的输出',
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
  ask: '需审批',
  gated: '始终允许 · 门控把关',
  always: '始终允许',
}

/** 每一档悬停看的那句：它在运行时具体怎么做 */
export const TOOL_TRUST_HINT: Record<ToolTrust, string> = {
  ask: '每次调用前暂停，等待人工审批。多 Agent 协作中的成员无法暂停，此类调用将不执行',
  gated: '每次调用前由门控模型检查参数：判定通过则直接执行，判定可疑或无响应时转人工审批',
  always: '直接执行，无需审批',
}

export const TOOL_TRUST_TEXT = {
  title: '运行时审批',
  /** 三选一的读屏名字 */
  groupLabel: (tool: string) => `${tool} 的运行时审批`,
  explain: '默认每次调用都需人工审批；门控把关指每次先由小模型检查参数，可疑的调用仍转人工审批。正式运行不受此设置影响，一律需人工审批。',
  /** 工具徽标：ask 和 gated 各一个，always 不挂 */
  badgeAsk: '运行时需审批',
  badgeGated: '门控把关',
  badgeGatedHint: '探索运行中调用前先由门控模型检查参数，判定可疑时仍暂停等待人工审批。正式运行一律需人工审批。',
  saveFailed: (tool: string) => `「${tool}」的运行时审批修改失败，已恢复原设置`,
  /** 审批卡 */
  alwaysButton: '始终允许',
  alwaysHint: (tool: string) =>
    `批准本次调用，并将「${tool}」设为「始终允许 · 门控把关」：本节点后续调用和之后的运行将不再请求审批，由门控模型逐次把关；可在「工具」页修改`,
  alwaysDone: (tool: string) => `已批准，运行继续；「${tool}」之后由门控模型把关`,
} as const

/** 设置页「运行默认值」里的门控模型 */
export const TOOL_GATE_TEXT = {
  label: '门控模型',
  provider: '门控模型 · 接入',
  model: '门控模型 · 模型',
  hint: '工具设为「始终允许 · 门控把关」时，每次调用前由门控模型检查参数。留空则使用默认接入的默认模型。每次调用都会额外请求一次门控模型，建议选择响应快、成本低的小模型。',
  providerDefault: '默认接入',
  modelDefault: '留空：用接入的默认模型',
} as const

/** 设置页「运行默认值」里 Agent 护栏那一组（后端 engine/guards.py） */
export const AGENT_GUARD_TEXT = {
  label: 'Agent 护栏',
  hint: 'Agent 在以下情况会基于已获取的信息收尾：重复发起相同调用、连续 3 步未获得新信息、预算用尽、上下文即将占满。此处为每个 Agent 节点的默认上限，可在节点上单独设置，仅对之后发起的运行生效。',
  steps: '默认最大步数',
  stepsHint: '安全上限，正常任务通常不会触及',
  tokens: 'token 预算（每个节点）',
  tokensHint: '包含输入和输出。留空表示不限',
  usd: '金额预算（美元）',
  usdHint: '按模型目录中的价格估算；未登记价格的模型无法估算，只能依靠 token 预算',
  unlimited: '不限：费用仅受最大步数和上下文长度约束',
  unlimitedShort: '不限',
} as const
