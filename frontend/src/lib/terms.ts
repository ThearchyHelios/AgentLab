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

import type { NodeType, SnapshotReasonCode, ToolTrust } from '../types'
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

  // ---- 期 4：推断的来源（上传表格的单元格追到原表格子，P4-SPEC 4.2）----
  // 用词同版本页：不写「快照」「构建」「并集」，写「数据版本」「这一期」「数据文件」；署名一律标「署名（未认证）」。
  // 不下钻的原因、标红的提示由服务端给原文（provenance_types.REASON_TEXT / ALERT_TEXT），界面原样显示，不在这里另写
  provenance: {
    version: '数据版本',
    /** 首行。每期替换只有一期，不写期数；导入模式的说法同版本页（VERSIONS_TEXT.modeLabel） */
    versionHead: (source: string, mode: string, n: number) => (mode === 'accumulate'
      ? `数据源「${source}」· 按期累积 · 共 ${formatNumber(n)} 期`
      : `数据源「${source}」· 每期替换`),
    part: (seq: number, start: string, end: string) => `第 ${formatNumber(seq)} 次导入 · 统计期 ${start} 至 ${end}`,
    partNoPeriod: (seq: number) => `第 ${formatNumber(seq)} 次导入 · 统计期未记录`,
    /** sha 是原件 sha256 的前 12 位；清单没记哈希时只写文件名 */
    file: (name: string, sha: string) => (sha ? `文件「${name}」· sha256 ${sha}` : `文件「${name}」`),
    /** S9：回执里块内各区域的外接矩形和排除的行数。排除 0 行、没有记录时不写 */
    region: (region: string, excluded: number | null) =>
      (excluded ? `区域 ${region} · 排除 ${formatNumber(excluded)} 行` : `区域 ${region}`),
    excluded: (n: number) => `排除 ${formatNumber(n)} 行`,
    committedAt: (at: string) => `导入于 ${at}`,
    /** 查询用到的表（与冻结表结构的交集）。原表写明的合计表另注一句：它不能和明细相加 */
    tables: (names: string[]) => `涉及的表：${names.join('、')}`,
    totalTable: (name: string) => `${name}（原表写明的合计）`,
    hasRow: '这一格所在的一期',
    current: '当前状态',
    revoked: '这一期的接受已作废',
    /** 清除原件、作废接受的记录：时间 · 理由 · 署名（未认证）。缺的不写（时间没记时调用方传空串），署名没填写「未填写」 */
    stateDetail: (at: string, reason: string | null, name: string | null) =>
      [at, reason ? `理由：${reason}` : '', `署名（未认证）：${name || '未填写'}`].filter(Boolean).join(' · '),
    /** 这一期写过的接受理由（导入清单里的，是当时的事实）。核对标题取不到时写核对编号 */
    acceptedLine: (check: string, reason: string, name: string | null) =>
      `已接受：${check} · ${reason} · 署名（未认证）：${name || '未填写'}`,
    manifest: '查看导入清单',
    manifestMasked: '数据源设置了遮罩，导入清单请到数据源卡片上的「版本」查看',
    unsealed: '本次运行尚未封存或封存核对未通过，以下内容不计入已封存的证据',
    morePeriods: (n: number) => `另有 ${formatNumber(n)} 期`,
    title: '推断的来源',
    badge: '推断，不属于已封存的证据',
    cell: (sheet: string, ref: string) => `工作表「${sheet}」${ref}`,
    /** 多期时格子在哪一期的文件里：坐标是那一期原件的坐标，不是别的期的 */
    fromPart: (seq: number, file: string | null) =>
      (file ? `取自第 ${formatNumber(seq)} 次导入的文件「${file}」` : `取自第 ${formatNumber(seq)} 次导入`),
    axisHeader: (ref: string) => `日期取自表头格 ${ref}`,
    yearPeriod: (cells: string) => (cells ? `年份取自统计期（${cells}）` : '年份取自统计期'),
    yearHuman: (name: string) => `年份取自人工录入的统计期（署名（未认证）：${name}）`,
    yearMixed: '这一块的日期表头有的是日期格（自带年份），有的是文本（年份取自统计期），无法确定这一格属于哪一种',
    /** 格子原文拿不到时（text 为 null）只写坐标，不拿规范写法冒充原文 */
    rowLabel: (ref: string, text?: string | null) => (text ? `行标签 ${ref}「${text}」` : `行标签 ${ref}`),
    /** 清单里没有分段标题格的原文，只有配方里的定位文字：照实说是「按配方中的标题定位」 */
    sectionTitle: (ref: string, title?: string | null) =>
      (title ? `分段标题 ${ref}（按配方中的标题「${title}」定位）` : `分段标题 ${ref}`),
    colHeader: (refs: string) => `列表头 ${refs}`,
    totalLabel: (ref: string, text?: string | null) => (text ? `合计标签 ${ref}「${text}」` : `合计标签 ${ref}`),
    canonical: (raw: string, canon: string) => `原文「${raw}」，按规范写法存为「${canon}」`,
    reportedTotal: '原表写明的合计：不要彼此相加，也不要与明细相加',
    rawPurged: '原件已清除：坐标来自导入时的记录，无法再对照原件',
    mergedFill: '这一块按合并单元格的左上格填充：如果这一格在合并区域内，值取自该区域的左上格',
    recheck: '已按主键回查数据文件，值一致',
    details: '技术细节',
    recheckSql: '回查 SQL',
    checks: '相关核对',
    row: { passed: '这一行成立', mismatch: '这一行不成立', unverifiable: '这一行含空值，未能核对' } as Record<string, string>,
    cellStatus: {
      ok: '这一格一致',
      unverifiable: '这一格未能核对',
      unknown: '本期有格子未能核对，这一格是否在其中无法确定',
      not_formula: '这一格是写死的数，不做公式引用核对',
    } as Record<string, string>,
    partStatus: { passed: '本期通过', mismatch: '本期不成立', unverifiable: '本期未能核对', info: '口径说明' } as Record<string, string>,
    accepted: (reason: string, name: string | null) => `已接受，理由：${reason}（署名（未认证）：${name || '未填写'}）`,
    loading: '正在推断来源…',
    failed: '未能获取推断的来源',
  },
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

/**
 * 上传表格：数据页「表格」标签的上传弹窗、导入回执和卡片。种类的说法和后端 data/tabular.py 的
 * CONVERSION_KINDS、SHAPE_KINDS 对得上；后端以后多出来的种类，界面上只显示它给的那句话
 */
export const UPLOAD_TEXT = {
  /** 上传前的告知：原件按内容存档、不提供下载，可以清除 */
  rawNotice: '原始文件将完整保存在服务端（包括不导入的隐藏工作表），用于核对和追溯，之后可以清除。',
  /** 表单和决定页上：这次上传已经选好的处理方式 */
  chosen: '本次上传已选择',
  chosenMixed: '数字列中的非数字值存为空值',
  chosenRaw: '按原样导入（未规整）',
  cancelHint: '不导入，返回上传表单',
  // 数字列混入非数字
  mixedTitle: '部分数字列中混有非数字的值',
  mixedLead: '以下各列以数字为主，但混有非数字的值。可以把这些值存为空值、整列按数字导入，空值不参与求和、平均等计算；'
    + '也可以取消，在 Excel 中修正后重新上传。',
  mixedAccept: '把这些值存为空值，按数字导入',
  mixedCounts: (numeric: string, other: string) => `数字 ${numeric} 个，非数字 ${other} 个`,
  // 交叉表、多块结构
  shapeTitle: '该表格不是一行一条记录的明细表',
  shapeLead: '检测到以下结构，默认不导入：',
  shapeRecipe: '这类表格可以按配方导入：指定表头、分段和合计行的处理方式，试运行核对、逐条确认后再启用。',
  /** CSV 没有配方这条路（配方只认 Excel 的工作表结构） */
  shapeRecipeCsv: '按配方导入目前只支持 Excel（.xlsx）文件：可另存为 .xlsx 后再按配方导入。',
  shapeHeader: (row: number) => `如果表头不在第 ${row} 行，请取消后修改表头行号，再重新上传。`,
  /** 结构问题之外，预告的混合列：选按原样导入以后还要再选一次 */
  shapeMixedLead: '另外，以下各列以数字为主，但混有非数字的值。选择按原样导入后，还需要选择这些值的处理方式：',
  shapeMixedPartial: '以上按表格前面的部分统计，其余部分可能还有。',
  rawAccept: '按原样导入（未规整）',
  rawConsequence: '按原样导入后，同一列混有不同口径的行，不能直接对列求和',
  moreCells: (n: string) => `等 ${n} 处`,
  // 导入回执
  headerMapped: '箭头左侧为原表头，右侧为 SQL 中使用的列名。',
  emptyHeader: '（空表头）',
  unshaped: '未规整',
  unshapedHint: '按原样导入、未经规整：同一列里混有不同口径的行，不能直接对列求和',
  region: '导入区域：表头行到最后一行数据',
  blankRows: (n: string) => `已跳过 ${n} 个空行`,
  trimmedCols: (cols: string) => `已去掉两侧整列为空的列 ${cols}`,
  notes: '导入时的处理',
  skippedHidden: (n: string, names: string) => `已跳过 ${n} 个隐藏工作表：${names}。隐藏工作表不导入，原始文件中仍保留。`,
  skippedEmpty: (names: string) => `已跳过没有内容的工作表：${names}。`,
  veryHidden: '（深度隐藏）',
  conversionCount: (n: string) => `共 ${n} 个`,
  /** list 是一串「」括起来的示例，紧跟在「如」后面 */
  examples: (list: string) => `（如${list}）`,
  // 卡片
  currentVersion: '当前版本',
  legacyFile: '早期上传的文件',
  /** 迁移补建的初始版本：快照的创建时间是迁移那一刻，不是导入时间，不写出来 */
  legacyTitle: '早期上传的版本，未记录原始文件名和导入时间',
  importedAt: (when: string) => `${when}${/\d$/.test(when) ? ' ' : ''}导入`,
  importedTitle: (when: string) => `当前版本于 ${when} 导入`,
  /** 名字和一个手工登记的 SQLite 源重名：可能是还没迁移的早期上传，能不能替换由服务端判断 */
  nameSqliteTaken: '已有同名的 SQLite 数据源：早期上传的表格可同名替换，否则将被拒绝',
  noSchema: '未读取到表结构 · 请重新上传',
  reupload: '重新上传',
  reuploadHint: '同名重新上传：发布新版本替换其中的数据，工具名不变',
} as const

/** 导入回执里的类型转换（tabular.CONVERSION_KINDS） */
export const UPLOAD_CONVERSION_LABEL: Record<string, string> = {
  thousands_separator: '千分位写法的文本已按数字保存',
  nonnumeric_to_null: '非数字的值已存为空值',
  kept_as_text: '含前导零或超长数字，整列按文本保存',
}

/** 不是规整明细表的几种理由（tabular.SHAPE_KINDS） */
export const UPLOAD_SHAPE_LABEL: Record<string, string> = {
  date_header: '表头是横排的日期',
  date_row: '表内有横排的日期行',
  section_title: '表内有分段标题行',
  table_totals: '表内有表格对象的汇总行',
  formula_above: '表内有汇总上方各行的公式',
}

/** 上传表格的原件状态（kept 不单独标：上传前已告知会保存） */
export const RAW_STATE_LABEL: Record<string, string> = {
  kept: '原件已保存',
  purged: '原件已清除',
  absent: '未保存原件',
}

/**
 * 版本页（数据源卡片上的「版本」，P3-SPEC 7.8、附录 C）。整页不出现「快照」「并集」「构建」，一律写「版本」
 * 「这一期」「数据文件」；署名一律标「署名（未认证）」；指向这个入口时写「数据源卡片上的「版本」」，不写「版本页」。
 * 服务端返回的错误原话直接显示，不在这里重复。
 *
 * 这一段是连续的一段，由版本页维护；本文件的其余部分归导入向导
 */
export const VERSIONS_TEXT = {
  open: '版本',
  openHint: '查看各期和历史版本：启用旧版本、移除某一期、查看导入清单、清除原件',
  title: (name: string) => `「${name}」的版本`,
  retentionRule: (keep: number) =>
    `除当前版本和被运行引用的版本外，系统保留最近 ${formatNumber(keep)} 个版本；更早的版本会被回收，回收后无法再启用`,
  tabs: { current: '当前版本', history: '历史版本', imports: '全部导入记录' },
  modeLabel: { accumulate: '按期累积', replace: '每期替换' } as Record<string, string>,
  /**
   * 卡片上的一句：按期累积写期数，每期替换只写「每期替换」。服务端没给期数（老后端、字段漏了）时只写「按期累积」：
   * 缺值不能替它说成「1 期」，实际可能是多期
   */
  cardSummary: (mode: string | null | undefined, n: number | null | undefined) =>
    (mode === 'accumulate' ? (typeof n === 'number' ? `按期累积 · ${formatNumber(n)} 期` : '按期累积') : '每期替换'),
  /** at 是 formatRelative 或 formatDateTime 的结果：以数字结尾时补一个空格（同 UPLOAD_TEXT.importedAt） */
  activatedAt: (at: string) => `${at}${/\d$/.test(at) ? ' ' : ''}启用`,
  /** 卡片悬停：回滚之后启用时间晚于导入时间，两个都写 */
  activatedTitle: (at: string, created: string) => `当前版本于 ${at} 启用，导入于 ${created}`,
  period: (start: string, end: string) => `${start} 至 ${end}`,
  periodUnknown: '统计期未记录',
  /** 没有统计期的一期（简单导入、期 3 之前的导入）用文件名指代 */
  periodFile: (file: string) => `${file}（统计期未记录）`,
  periods: (n: number) => `${formatNumber(n)} 期`,
  /** 当前版本的各期之间本来就有空缺（移除过中间一期、某个月没导入）：每次打开都写，不只在移除时的确认框里写一次 */
  currentGaps: (list: string) => `各期之间有空缺（${list}），比较不同期之前需要先查询日期的覆盖范围`,
  rows: (n: number) => `${formatNumber(n)} 行`,
  recipeSeq: (n: number) => `配方第 ${formatNumber(n)} 版`,
  simpleImport: '简单导入',
  importSeq: (n: number) => `第 ${formatNumber(n)} 次导入`,
  /** 导入记录的状态（table_imports.status） */
  importStatus: { active: '在当前版本中', superseded: '已被替换', retired: '已回收' } as Record<string, string>,
  signedBy: (name: string) => `署名（未认证）：${name}`,
  /** 本机没有填署名时，确认框里「署名（未认证）：」后面写这个 */
  unsigned: '未填写',
  /** 当前版本的概览 */
  overview: { mode: '导入模式', periods: '期数', recipe: '配方', activated: '启用时间', tables: '各表行数' },
  acceptCount: (n: number) => `接受 ${formatNumber(n)} 条`,
  acceptRow: (check: string, reason: string) => `${check}：${reason}`,
  /** 接受的种类：override 是数据质量类不成立，waiver 是合计无法核对 */
  acceptKind: { override: '数据质量', waiver: '无法核对' } as Record<string, string>,
  pinnedRuns: (n: number) => `被 ${formatNumber(n)} 次运行引用`,
  /** text 是 formatBytes 的结果 */
  size: (text: string) => `文件大小 ${text}`,
  retiredGroup: (n: number) => `已回收 ${formatNumber(n)} 个`,
  /** 不能启用的原因，按 SnapshotOut.reason_code 取（键就是代码；文字与后端 SNAPSHOT_NOT_ACTIVATABLE 逐字相同，
   * 改这里的文字时请编排者同步改后端）。没有 reason_code 时显示服务端的 reason，不按原文反查键 */
  notActivatable: {
    current: '已是当前版本',
    retired: '已回收，无法启用',
    file_lost: '数据文件已丢失，无法启用',
    contains_revoked: '包含已作废接受的导入，无法启用',
  } satisfies Record<SnapshotReasonCode, string>,
  maskLostBadge: '部分遮罩列在这个版本中没有同名列',
  historyEmpty: '没有其他版本',
  importsEmpty: '还没有导入记录',
  // ---- 启用（回滚）
  activate: '启用这个版本',
  activateTitle: '启用这个版本？',
  activateConsequence: {
    removed: (list: string) => `当前版本将不再包含：${list}`,
    added: (list: string) => `当前版本将增加：${list}`,
    newRuns: '启用后，新发起的运行使用这个版本',
    running: '已经在进行的运行不受影响',
    recipe: (n: number) => `配方回到这个版本使用的第 ${formatNumber(n)} 版`,
    simple: '这个数据源回到简单导入，之后上传新一期时不再按配方导入',
    /** label 取 modeLabel 的值 */
    mode: (label: string) => `导入模式回到${label}`,
    nextUpload: '之后上传新一期时，以启用后的版本为基础',
    stagings: '未完成的导入需要重新试运行',
    maskLost: (cols: string) => `以下遮罩列在这个版本中没有同名列，启用后不再遮罩：${cols}`,
  },
  activated: '已启用这个版本',
  // ---- 移除这一期（按期累积）
  removePeriod: '移除这一期',
  removeTitle: (period: string) => `从当前版本中移除 ${period}？`,
  removeConsequence: {
    result: (list: string) => `移除后当前版本包含：${list}`,
    gap: (period: string) => `移除后各期之间出现空缺（${period}），比较不同期之前需要先查询日期的覆盖范围`,
    recipe: '配方和导入模式不变',
    reactivate: '可以在「历史版本」中重新启用移除前的版本（在保留期内）',
    running: '已经在进行的运行不受影响',
    stagings: '未完成的导入需要重新试运行',
  },
  removeLastDisabled: '这是当前版本里唯一的一期，不能移除',
  removed: (period: string) => `已从当前版本中移除 ${period}`,
  /** 移除后的各期与此前某个版本相同：服务端直接启用了那个版本 */
  removedReused: '与此前的某个版本内容相同，已直接启用该版本',
  // ---- 撤回这一期（作废接受，不可恢复）
  revoke: '撤回这一期（作废接受）',
  revokeTitle: '作废这一期的接受？',
  revokeConsequence: {
    irreversible: '作废后无法恢复：作废本身不能撤回，包含这一期的版本都不能再启用',
    rollback: (desc: string) => `当前版本将回到：${desc}`,
    remove: (list: string) => `这一期将从当前版本中移除，当前版本包含：${list}；配方和导入模式不变`,
    record: '导入记录和接受理由保留，标记为已作废',
    running: '已经在进行的运行不受影响',
  },
  revokeUnavailable: '没有可以回滚的版本，请上传修正后的文件',
  revoked: '已作废这一期的接受',
  // ---- 清除原件
  purge: '清除原件',
  purgeTitle: '清除这一期的原件？',
  purgeConsequence: {
    evidence: '证据面板将显示「原件已清除」',
    redraft: '原件清除后，这一期无法再按修改后的配方重新导入',
    shared: (list: string) => `同一份内容的其他导入记录一并清除：${list}`,
    stagings: (n: number) => `引用它的 ${formatNumber(n)} 个未完成导入一并放弃`,
  },
  /** 清除前列出同一份原件的其他引用：按数据源汇总 */
  sharedItem: (name: string, n: number) => `「${name}」${formatNumber(n)} 条`,
  purgeDone: (also: number, discarded: number) =>
    `已清除。另外清除了 ${formatNumber(also)} 条导入记录的原件，放弃了 ${formatNumber(discarded)} 个未完成导入`,
  purgedItem: (name: string, seq: number, file: string) => `「${name}」第 ${formatNumber(seq)} 次导入（${file}）`,
  /** 清除确认框的正文开头：哪一次导入、哪一期。同一统计期可能导入过几次（替换前后），带上导入次序才分得清 */
  purgeTarget: (seq: number, period: string) => `第 ${formatNumber(seq)} 次导入，${period}`,
  /** 一并清除的导入记录属于已经删除的数据源时，名字的位置写这个 */
  deletedSource: '已删除的数据源',
  /** 移除这一期、作废接受、清除原件的理由输入框 */
  reasonLabel: '理由（必填）',
  /** 启用旧版本的理由输入框（7.2 理由可选）：空着也能启用，不填就不随请求发送 */
  reasonOptional: '理由（可选）',
  reasonTooLong: '理由不能超过 500 字',
  // ---- 清单
  manifest: '查看导入清单',
  manifestRaw: '查看原始清单',
  manifestTitle: (seq: number) => `第 ${formatNumber(seq)} 次导入的导入清单`,
  back: '返回版本列表',
  /** 清单视图的各块标题（回执摘要、排除的行也用在向导的回执里） */
  manifestBlocks: {
    file: '文件',
    recipe: '配方',
    period: '统计期',
    checks: '核对',
    acceptances: '接受',
    confirmations: '确认清单',
    edits: '修改记录',
    notes: '说明',
    receipt: '回执摘要',
    excluded: '排除的行',
    ai: 'AI 用量',
  },
  manifestUnverified: '这份清单的内容哈希校验失败，可能已被修改，不能作为证据',
  /** 简单导入没有导入清单，服务端给的是导入回执 */
  manifestSimple: '简单导入没有导入清单，以下是这次导入的回执',
  sha: (prefix: string) => `内容哈希 ${prefix}`,
  recipeJson: '查看配方 JSON',
  periodHuman: '人工录入',
  sql: '查看 SQL',
  /** 核对的计数：checked 是核对了几处，failed、unverifiable 为 0 时不写 */
  checkCounts: (checked: number, failed: number, unverifiable: number) =>
    [`核对 ${formatNumber(checked)} 处`, failed ? `不一致 ${formatNumber(failed)} 处` : '',
      unverifiable ? `无法核对 ${formatNumber(unverifiable)} 处` : ''].filter(Boolean).join('，'),
  editKind: { fix: '修复', selection: '框选' } as Record<string, string>,
  editSuperseded: '已被覆盖',
  editsNone: '这次导入没有修改',
  editsUnrecorded: '这次导入未记录修改',
  checksNone: '没有核对结果',
  acceptancesNone: '没有接受的核对',
  confirmationsNone: '没有勾选的确认项',
  notesNone: '没有说明',
  /** tokens 是 formatTokens 的结果（带单位），cost 是 formatCost 的结果 */
  aiUsage: (calls: number, tokens: string, cost: string) => `调用模型 ${formatNumber(calls)} 次，共 ${tokens}，约 ${cost}`,
  aiNone: '这次导入没有调用模型',
  /** 期 4 起，带来源标记的报告里，裁判摘录会带上可见单元格里的区域外文字（隐藏行列里的不送） */
  outsideTextNote: '区域外文字不导入数据表；核对报告时，裁判可以看到其中可见单元格的文字',
  revokedBadge: '接受已作废',
  /** 当前版本或回滚目标被别人改过（base_changed、revoke_target_changed）：服务端原话说了变的是什么，这里只说列表已刷新 */
  refreshHint: '列表已刷新，请重新确认',
  empty: '还没有版本',
} as const

/**
 * 按配方导入（期 2）：向导、原始网格、建议卡片、配方面板、试运行回执、确认清单、差异卡、AI 起草。
 * 后端返回的问题、确认项、核对标题是整句人话，界面原样显示；这里只放界面自己的文字和枚举的显示名。
 *
 * 配方面板里不出现「正则」一类字样：配方语言是封闭的，没有任何字段能写匹配规则
 */
export const RECIPE_TEXT = {
  // ---- 入口
  entry: '按配方导入',
  entryHint: '交叉表、分段表格按配方导入：起草配方、试运行核对，逐条确认后启用',
  reupload: '上传新一期',
  reuploadHint: '按已确认的配方导入新一期的文件：先试运行，看过回执和差异后再启用',
  redraft: '修改配方',
  redraftHint: '不换文件，修改当前配方后重新试运行',
  openStaging: '有未完成的导入',
  openStagingHint: '继续上次未完成的导入',
  // ---- 向导
  titleFirst: '按配方导入',
  titleNamed: (name: string) => `按配方导入「${name}」`,
  titleReupload: (name: string) => `上传新一期「${name}」`,
  titleRedraft: (name: string) => `修改配方「${name}」`,
  steps: { pick: '选择文件', draft: '起草配方', trial: '试运行', confirm: '确认启用', done: '完成' },
  pickReupload: '选择新一期的文件：将按当前配方试运行，回执和差异核对无误后再启用。',
  nameHint: '将用作工具名的一部分；启用之前不会创建数据源',
  nameLabel: '数据源名',
  nameInvalid: '须以小写字母开头，只能包含小写字母、数字和下划线',
  nameTakenDb: (name: string) => `「${name}」已被其他数据库用作标识，请更换名称`,
  descriptionLabel: '说明（供助手参考）',
  pickDrop: '拖入文件，或点击选择',
  /** 与选择框的 accept 一致 */
  pickFormats: '支持 Excel（.xlsx、.xlsm）',
  pickAria: '选择表格文件',
  pickWrongType: (name: string) => `「${name}」不是 Excel 文件：按配方导入只支持 .xlsx、.xlsm`,
  cancelUpload: '取消上传',
  cancel: '取消',
  close: '关闭',
  staging: '正在读取表格、起草配方',
  reuploading: '正在读取表格、按当前配方试运行',
  loading: '正在读取这次导入',
  discard: '放弃这次导入',
  discardTitle: '放弃这次导入？',
  discardConsequences: ['已起草的配方、回答和试运行结果都不保留', '数据源的当前版本不受影响'],
  discarded: '已放弃这次导入',
  closed: '这次导入已结束（过期或已放弃），请重新开始',
  rawMissing: '当前导入的原始文件已清除，无法直接修改配方：请用「上传新一期」带着文件进入修改',
  later: '稍后继续',
  laterHint: '关闭向导，进度已保存在服务端；可从卡片上的「有未完成的导入」继续',
  // ---- 起草
  trialRun: '试运行',
  trialRunning: '试运行中',
  trialBlocked: '配方有问题，修改后才能试运行',
  trialStale: '上次的试运行已失效（之后修改过配方或录入），请重新试运行',
  trialStaleConfirm: '上次的试运行已失效，不能据此启用：请回到配方重新试运行',
  partial: '仅检查了前 500 行，完整检查在试运行时进行',
  partialHint: '表格较大，起草时只检查了每张工作表的前 500 行；试运行会检查全部',
  draftIncomplete: '规则起草未能完成',
  cardsTitle: '建议',
  questionsTitle: '待确认',
  noCards: '没有需要说明的建议',
  reasonLabel: '理由（必填）',
  reasonPlaceholder: '说明为什么这样选',
  reasonSubmit: '提交',
  reasonRequired: '请填写理由',
  answering: '正在应用',
  suggested: (label: string) => `建议：${label}`,
  // ---- 网格
  gridLabel: '原始表格',
  legendTitle: '图例',
  unclaimed: '没有去处',
  hiddenCells: '隐藏的行列',
  gridTruncated: (n: string, total: string) => `仅显示前 ${n} 行，共 ${total} 行`,
  noGrid: '没有可显示的工作表',
  // ---- AI 起草
  aiOffer: '让 AI 起草（会把表格结构发给模型）',
  aiUnavailable: '未配置模型接入，无法使用 AI 起草（设置 → 模型接入）',
  aiConsentTitle: '发送表格结构给模型',
  aiConsentLead: (model: string) => `将把下面这段内容发送给「${model}」：工作表名称、表头和行标签等结构文字`
    + '（写成文字的日期和统计期说明也会发送，如「8月1日」「统计时间范围：…」）、数字单元格和日期格式单元格的类型、'
    + '合并区域、去掉了常量的公式形状、系统发现的关系。',
  aiConsentNot: '不会发送数字单元格的值、日期格式单元格里的日期、像数字的文字和隐藏行列的内容；'
    + '其他文字只发字数，列表数据列只发个数统计、不发内容（每行最左一格的「合计」「小计」这类标签和占位符照发）。'
    + '配方需要修订时，还会把配方的问题列表发给同一个模型，其中的这些内容同样已遮掉。',
  aiConsentAfter: 'AI 只起草配方，导入前仍需试运行并逐条确认。',
  aiPreviewLabel: (chars: string) => `将要发送的全文（${chars} 字）`,
  aiAgree: '同意并发送',
  aiPreviewing: '正在准备将要发送的内容',
  aiDrafting: 'AI 起草中',
  /** cost 是 formatCost 的结果（「$0.012」） */
  aiDone: (tokens: string, cost: string) => `AI 起草完成，用了 ${tokens} token（约 ${cost}）`,
  aiStale: '表格内容或模型设置已变化，请重新查看将要发送的内容',
  // ---- 配方面板
  panel: '配方',
  panelHint: '每项修改都会立即保存并重新检查；有问题的字段旁会标出原因',
  panelProblems: '配方有以下问题',
  viewJson: '查看配方 JSON',
  hideJson: '收起配方 JSON',
  pasteJson: '粘贴配方',
  pasteLead: '粘贴一份完整的配方 JSON，保存后同样经过检查。',
  pasteSave: '保存配方',
  pasteInvalid: 'JSON 格式有误，未保存',
  pasteNotObject: '配方必须是一个 JSON 对象',
  saving: '正在保存',
  noRecipe: '还没有配方：请回答上面的问题，或粘贴一份配方',
  readonlyNote: '表单没有覆盖的字段只能通过粘贴配方修改',
  sheet: (name: string) => `工作表「${name}」`,
  table: (name: string) => `表「${name}」`,
  crosstab: '交叉表',
  listBlock: (id: string) => `列表「${id}」`,
  segment: (id: string) => `分段「${id}」`,
  derivedSegment: (id: string) => `合计段「${id}」`,
  tableName: '表名',
  columnName: '列名',
  unit: '单位',
  noUnit: '无单位',
  grain: '主键',
  grainHint: '用作主键的列（最多 4 列）',
  labels: '标签',
  totalLabels: '合计标签',
  addLabel: '添加',
  labelPlaceholder: '从表格中选择或输入',
  removeLabel: (label: string) => `移除标签「${label}」`,
  locateBy: '定位方式',
  sectionTitle: '分段标题',
  stopAtTotal: '遇到时段合计行时结束本段',
  dimName: '维度列名',
  dimParser: '标签写法',
  valueName: '值列名',
  constants: '常量列',
  constPick: '取值',
  deriveCols: '由时段派生的列',
  deriveRole: { start: '区间起点', end: '区间终点' } as Record<string, string>,
  keepDim: '合计项列名',
  keepAs: '原表合计另存为表',
  keepTable: '另存的表名',
  values: '数据区的值',
  valueType: '类型',
  placeholders: '占位符',
  placeholderText: '占位符文字',
  placeholderMeaning: '含义',
  placeholderStore: '一律存为空值',
  addPlaceholder: '添加占位符',
  removePlaceholder: (text: string) => `移除占位符「${text}」`,
  blank: '空格',
  textNumber: '文本写的数字',
  formula: '公式',
  period: '统计期',
  preferPrefix: '优先展示以此开头的格子',
  crossCheck: '与文件名里的日期核对',
  addPeriod: '从格子里解析统计期',
  noPeriod: '未配置统计期',
  axisChecks: '日期检查',
  axisChecksHint: '取消任一项，确认清单里会多出一条需确认的放宽',
  headerRows: '表头行数',
  afterTitle: '只在这行文字之后找表头',
  columns: '列',
  colHeader: '表头',
  colType: '类型',
  colStore: '存法',
  addColumn: '添加列',
  removeColumn: (name: string) => `移除列「${name}」`,
  extraColumns: '表头里多出的列',
  blankRows: '数据中间的空行',
  totalRow: '合计行',
  totalRowOn: '有合计行',
  totalLabelColumn: '合计标签所在的列',
  totalPick: '合计行以此开头',
  totalKeep: '原表合计另存为表（留空则只核对）',
  hiddenRows: '隐藏行',
  hiddenCols: '隐藏列',
  fallback: '工作表改名时',
  otherSheets: '配方没有列出的其他工作表',
  relations: '关系',
  relationRegister: '登记',
  relationDismiss: '不登记',
  relationReason: '不登记的理由（必填）',
  relationTotal: '合计列',
  relationParts: '组成列',
  relationNoCache: '请在建议卡片的问题里选择「登记」',
  notComparable: (a: string, b: string) => `「${a}」与「${b}」口径不同`,
  sumEq: (total: string, parts: string) => `${total} = ${parts}`,
  dismissed: (claims: string) => `不登记系统发现的关系 ${claims}`,
  ignoreRules: '忽略规则',
  ignoreRow: (label: string) => `按行标签忽略「${label}」这一行`,
  ignoreColumn: (header: string) => `按表头忽略「${header}」这一列`,
  ignoreOutside: (anchor: string) => `同一行有文字「${anchor}」时，忽略这一行导入区域之外的数字`,
  ignoreReason: (reason: string) => `理由：${reason}`,
  removeIgnore: (text: string) => `移除忽略规则：${text}`,
  // ---- 试运行回执
  receipt: '试运行回执',
  ledger: (n: string) => `单元格去向：非空单元格 ${n} 个`,
  ledgerAll: '全部有去处',
  ledgerUnclaimed: (n: string) => `其中 ${n} 个没有去处`,
  /** 格子账为空：配方里的工作表一张都没读到（如工作表改名后对不上），去向和两遍读取都无从谈起 */
  ledgerNone: '未读取到配方中的工作表，无法核对单元格去向',
  ledgerSheetsMissing: (n: string) => `配方中有 ${n} 张工作表未读取到，以上只计入已读取的工作表`,
  ledgerUnbalanced: (sum: string, read: string) => `各去向合计 ${sum} 个，与读取到的 ${read} 个不符`,
  passAgree: '两遍读取一致',
  passDiffer: (scan: string, read: string) => `两遍读取不一致：第一遍 ${scan} 个、第二遍 ${read} 个`,
  tables: '表与行数',
  rows: (n: string) => `${n} 行`,
  /** 拒收、需要录入时执行器没有写完（或根本没写库）：只列表和列，不显示行数 */
  tablesUnwritten: '试运行没有完成写入，以下只列出表和列；行数以通过后的试运行为准',
  rowsUnwritten: '未写入',
  periodLine: (start: string, end: string) => `统计期：${start} 至 ${end}`,
  periodFromCells: (cells: string) => `取自 ${cells}`,
  periodHuman: (who: string) => `人工录入（${who}）`,
  checks: '核对',
  notes: '说明预览',
  problems: '问题',
  outsideText: '区域外文字',
  placeholderCounts: '占位符',
  placeholderCount: (text: string, n: string) => `「${text}」${n} 格`,
  statusPassed: '试运行通过：请逐条确认后启用',
  statusNeedsDecision: '有核对未通过但可以接受：在确认清单里写明理由',
  statusNeedsInput: '无法从表格中确定统计期：请录入本期统计期',
  statusRejected: '试运行未通过：请修改配方或文件',
  backToRecipe: '修改配方',
  toConfirm: '下一步：逐条确认',
  backToReceipt: '返回回执',
  periodStart: '统计期起',
  periodEnd: '统计期止',
  periodSuggest: (start: string, end: string) => `文件名中的日期：${start} 至 ${end}`,
  periodUseSuggest: '填入',
  periodSubmit: '按此统计期试运行',
  periodInvalid: '请填写两个日期，且起点不晚于终点',
  // ---- 确认清单
  confirmTitle: '启用前请逐条确认',
  confirmLead: '以下各项需逐条勾选；可接受的核对须写明理由。',
  acceptTitle: '可接受的核对',
  acceptLabel: '接受理由（必填）',
  acceptPlaceholder: '说明为什么可以接受',
  signLabel: '署名',
  signNote: '署名（未认证）',
  commitFirst: '确认并启用',
  commitReupload: '启用',
  committing: '正在启用',
  commitBlocked: '请勾选全部必选的确认项，并为每条可接受的核对写明理由',
  /** 不必勾的确认项（ConfirmItem.required=false）旁的标注 */
  confirmOptional: '可选',
  missing: '这一项尚未确认',
  rerunTrial: '请重新试运行',
  baseChanged: '在你试运行之后，当前版本已被更新，请重新试运行',
  nameTaken: '该名称已被占用：请放弃这次导入，换一个名称后重新导入',
  // ---- 完成
  done: (name: string) => `已启用「${name}」`,
  doneUnchanged: (seq: string) => `与第 ${seq} 次导入相同，未新建版本`,
  doneReused: '内容与以前的某次导入相同，沿用了那次的数据文件',
  finish: '完成',
  // ---- 卡片上的配方信息
  recipeVersion: (seq: string) => `配方第 ${seq} 版`,
  recipeActivated: (when: string) => `${when}${/\d$/.test(when) ? ' ' : ''}启用`,
  recipeActivatedTitle: (when: string) => `当前配方于 ${when} 启用`,
  recipeSigned: (who: string) => `署名「${who}」（未认证）`,
  // ---- 差异卡
  diffTitle: '与上一期相比',
  /** 差异卡的 kind 不在 DIFF_KIND_LABEL 里时的通用名（不露键名） */
  diffKindOther: '其他变化',
  diffRequires: '需确认',
  diffNone: '与上一期相比没有需要说明的差异',
  sameAsImport: (seq: string) => `与第 ${seq} 次导入相同，无需重复导入`,

  // ======== 期 3（P3-SPEC 10.2）。向导新增的部分和版本页一样不出现「快照」「并集」「构建」，「锚点」是内部术语也不上界面
  // ---- 网格框选
  selectToggle: '框选',
  selectToggleHint: '在表格上按住拖动选出一块区域，或在下方输入区域；按文字定位，不记坐标',
  selectionNone: '在表格上拖动，或输入区域',
  selected: (ref: string) => `已选 ${ref}`,
  selectionRef: '区域',
  selectionRefPlaceholder: '如 C5:F8',
  selectionRefInvalid: '区域写法不对，请写成「C5:F8」这样的形式',
  selectionMenu: '框选为…',
  selectionCancel: '取消',
  outOfPreview: '这一格不在预览范围内（仅显示前 300 行、60 列）',
  // ---- 框选面板
  selectionTitle: (label: string) => `框选为：${label}`,
  selectionHeaderRows: '表头行数',
  selectionBottom: '下边界',
  selectionBottomBox: '以框为准',
  selectionBottomAuto: '按规则推断',
  selectionTable: '新表名（可不填）',
  selectionTablePlaceholder: '不填时按规则取名',
  selectionRole: '这组行是',
  selectionRoleMeasures: '各行是不同的量（每行一列）',
  selectionRoleDimension: '各行是同一列的不同取值',
  selectionKeep: '合计行的原值',
  selectionKeepYes: '另存一张表',
  selectionKeepNo: '只核对，不另存',
  selectionSegment: '哪个分段',
  selectionSegmentPick: '请选择分段',
  selectionCrosstabNote: '框只用来指认是哪一块，范围按日期表头和行标签确定',
  selectionByText: '按文字定位，不记坐标',
  selectionReplayMatch: '重放结果与框选一致',
  selectionReplayDiffer: '重放结果与框选不一致，应用后以重放结果为准',
  selectionWindow: (n: string) => `仅比对前 ${n} 行，完整范围在试运行时核对`,
  selectionExpected: '框选的范围',
  selectionActual: '重放识别的范围',
  selectionRegion: { header: '表头', data: '数据', total: '合计行', axis: '日期表头', labels: '行标签', values: '数据' } as Record<string, string>,
  selectionCannot: '无法按这个框选修改配方',
  selectionApply: '应用',
  selectionApplyAnyway: '仍然应用（以重放结果为准）',
  selectionApplied: '已按框选修改配方，请重新试运行',
  selectionNotes: '提示',
  // ---- 修复面板
  fixBack: '返回',
  fixOptions: '怎么修改',
  fixReasonLabel: '理由（必填，最多 200 字）',
  fixReasonPlaceholder: '说明为什么这样处理；理由会写进配方',
  fixPreview: '预览',
  fixPreviewAgain: '重新预览',
  fixPreviewing: '正在预览',
  fixReasonChanged: '理由已修改，请重新预览',
  fixOptionChanged: '选项已修改，请重新预览',
  fixReasonFirst: '请先填写理由，再预览',
  fixSummary: '将做的修改',
  fixAfter: '修改后的检查结果',
  fixAfterClean: '修改后没有发现问题',
  fixBreaking: '对现行配方的破坏性变化',
  fixBreakingBadge: '破坏性变更',
  fixApply: '应用修复',
  fixApplying: '正在应用',
  fixApplied: '已应用修复，请重新试运行',
  fixStale: '问题已变化，请重新查看修复建议',
  fixCannot: '无法应用这条修复',
  /** 预览没通过、服务端却没有给出原因（不该出现）：不留一块空的预览区 */
  editCannotUnknown: '服务端未说明原因，请返回后重新查看',
  editStale: '配方在预览之后有变化，请重新预览',
  previewToggle: '网格显示',
  previewBefore: '修改前',
  previewAfter: '修改后',
  recipeProblemsBar: (n: string) => `配方有 ${n} 个问题`,
  recipeProblemsHint: '修改之前不能试运行',
  // ---- 已做的修改
  editsTitle: '已做的修改',
  editSuperseded: '已被覆盖',
  editSupersededHint: '之后整份替换过配方：这项修改不能再撤销，也不再列入确认清单',
  editUndo: '撤销上一次修改',
  editUndone: '已撤销上一次修改，请重新试运行',
  editSigned: (who: string) => `署名（未认证）：${who}`,
  // ---- 改配方后的回答
  answersChanged: (list: string) => `改配方后，以下问题需要重新回答：${list}`,
  answersGone: (list: string) => `以下问题已不适用，回答已移除：${list}`,
  // ---- 整份替换工作配方之前的确认
  replaceTitle: '替换当前工作配方？',
  saveRecipeTitle: '保存对配方的修改？',
  replaceEdits: (n: number) => (n > 0 ? `当前工作配方（含已做的 ${formatNumber(n)} 项修改）将被替换` : '当前工作配方将被替换'),
  replaceUndo: '之前的修改标为「已被覆盖」，不能再撤销，也不再列入确认清单',
  replaceConfirm: '替换',
  saveRecipeConfirm: '保存',
  // ---- 按规则重新起草
  redraftRules: '按规则重新起草（不调用模型）',
  redraftRulesHint: '按本次的文件重新起草，并把表名、列名对齐到现行配方；采用之前不改工作配方',
  redraftRunning: '正在重新起草',
  redraftTitle: '按规则重新起草的结果',
  redraftAlignment: '名字对齐',
  redraftNoRecipe: '规则起草未能得到完整的配方，无法采用',
  redraftAdopt: '采用',
  redraftAdoptTitle: '采用重新起草的配方？',
  redraftAdoptAfter: '采用后，确认清单按首次导入逐项列出配方的各项设置',
  redraftDiscard: '不采用',
  redraftMismatch: '采用的配方与重新起草的结果不同，请重新起草后再采用',
  // ---- 回执、确认清单
  sheetRenamed: '工作表改名',
  sheetRenamedLine: (from: string, to: string) => `配方里的工作表「${from}」，本期叫「${to}」`,
  priorAcceptance: (reason: string, seq: string, who: string) =>
    `上次接受的理由：${reason}（第 ${seq} 次导入，署名（未认证）：${who}）`,
  priorUnsigned: '未署名',
  commitConflict: '服务端的数据文件与登记的不一致，重试也会失败：请联系管理员检查数据目录',
  // ---- 完成
  doneRestored: '服务端的同版本数据文件曾被改动，已用本次上传的文件恢复',
  doneSnapshotReused: '与此前的某个版本内容相同，已直接启用该版本',
} as const

/** 修复按钮的文字（契约 FIX_KINDS）：按钮放在对应的问题旁，文字写会做什么，不写内部的叫法 */
export const FIX_KIND_LABEL: Record<string, string> = {
  remove_label: '去掉标签',
  add_label: '加入标签',
  edit_members: '更新关系成员',
  rename_title: '改分段标题',
  declare_total: '改作合计核对',
  ignore_cells: '按文字忽略',
  declare_placeholder: '声明占位符',
  declare_hidden: '设置隐藏行处理',
  rename_sheet: '更新工作表名',
}

/** 「框选为…」的取值（契约 SELECTION_AS），顺序就是下拉框里的顺序 */
export const SELECT_AS_LABEL: Record<string, string> = {
  list: '列表（含表头）',
  crosstab: '交叉表（整块）',
  segment: '分段',
  derived: '合计行',
  section_title: '分段标题',
  ignore_rows: '忽略这些行',
  ignore_columns: '忽略这些列',
  ignore_outside: '忽略区域外的数字',
}

/** 框选换算出的定位文字的种类（契约 Anchor.kind）：「表头：地区、产品…」 */
export const ANCHOR_KIND_LABEL: Record<string, string> = {
  header: '表头',
  row_label: '行标签',
  section_title: '分段标题',
  total_word: '合计词',
  axis: '日期表头',
  after_title: '上方的标题',
  outside_text: '同一行的文字',
}

/** 确认清单的分组（契约 ConfirmItem.source）。区域外文字、工作表、统计期都是「本期」才有的项，合成一组 */
export const CONFIRM_SOURCE_LABEL: Record<string, string> = {
  recipe: '配方',
  edit: '修改',
  accumulate: '累积',
  outside: '本期',
  sheet: '本期',
  context: '本期',
  diff: '差异',
  switch: '切换',
}

/**
 * 按期累积（P3-SPEC 2.2–2.5、10.2）：累积计划、导入模式。行数分「本期」「启用后当前版本」两栏，
 * 不写「并集行数」
 */
export const ACCUMULATE_TEXT = {
  title: '各期',
  action: {
    append: '新增一期',
    replace_period: '替换该期',
    restart: '重新开始累积',
    replace: '改为每期替换',
    first: '作为第一期',
    rejected: '与已有各期部分重叠，不能累积',
  } as Record<string, string>,
  thisPeriod: '本期',
  afterEnable: '启用后当前版本',
  table: '表',
  partNew: '本期',
  partReplaced: '将被替换',
  partDropped: '将不在当前版本中',
  period: (start: string, end: string) => `${start} 至 ${end}`,
  periodUnknown: '统计期未记录',
  importSeq: (n: string) => `第 ${n} 次导入`,
  overlaps: '部分重叠的各期',
  gaps: (list: string) => `各期之间有空缺：${list}`,
  backfill: '本期早于已有各期，按统计期排序插入',
  semantic: '与当前版本不兼容的变化',
  restartHint: '如果不想重新开始累积，请返回修改配方（例如保留原来的单位、列名或常量取值）',
  added: '新增的列',
  retired: '自本期起不再导入的列',
  wholeTable: '整张表',
  labelSets: '各期取值不完全相同',
  labelMissing: (period: string, list: string) => `${period} 没有：${list}`,
  labelExtra: (period: string, list: string) => `${period} 另有：${list}`,
  blockers: (period: string, reason: string) => `${period} 的配方不满足按期累积的要求：${reason}`,
  checks: '整体核对',
  modeField: '导入模式',
  modeLabel: { accumulate: '按期累积', replace: '每期替换' } as Record<string, string>,
  /** 两种取值的后果（P3-SPEC 2.2，评审三-m4）：问题选项下方、配方面板单选下方各显示一句 */
  modeConsequence: {
    accumulate: '每期以统计期为键加入当前版本；统计期与已有各期部分重叠的文件会被拒收，同一统计期再传要确认替换',
    replace: '每次上传新一期，当前版本只含新的一期，此前各期留在历史版本中',
  } as Record<string, string>,
} as const

/** 新旧配方对照（P3-SPEC 6.2、9.4）：试运行回执、确认清单顶部、修复预览、重新起草共用 */
export const COMPARE_TEXT = {
  title: '配方前后对照',
  none: '配方没有变化',
  breakingBadge: '破坏性',
  nothing: '无',
  tableAdded: (t: string) => `新增表「${t}」`,
  tableRemoved: (t: string) => `表「${t}」不再产出（删除或改名）`,
  columnAdded: (t: string, c: string, unit: string) => `表「${t}」新增列「${c}」${unit ? `（${unit}）` : ''}`,
  columnRemoved: (t: string, c: string) => `表「${t}」删除或改名了列「${c}」`,
  unit: (t: string, c: string, from: string, to: string) => `表「${t}」列「${c}」的单位 ${from} → ${to}`,
  noUnit: '无单位',
  change: (t: string, c: string, what: string, from: string, to: string) =>
    (from || to ? `表「${t}」列「${c}」的${what} ${from || '无'} → ${to || '无'}` : `表「${t}」列「${c}」的${what}有变化`),
  changeKind: {
    type: '类型', unit: '单位', source: '来源', store: '文字存法', const_value: '常量取值', placeholder_meaning: '占位符含义',
  } as Record<string, string>,
  changeOther: '设置',
  grain: (t: string, from: string, to: string) => `表「${t}」的主键 ${from} → ${to}`,
  tableKind: (t: string, from: string, to: string) => `表「${t}」的种类 ${from} → ${to}`,
  tableKindLabel: { data: '数据表', reported_total: '原表写明的合计' } as Record<string, string>,
  /** 服务端的破坏性原话（不带表名）前面补上是哪张表 */
  breakingLine: (t: string, msg: string) => `表「${t}」：${msg}`,
  /**
   * 新增、去掉的分段：标题和全部标签一起写在这一条里（list 是已经加好引号的标签）。起草器换了分段 id 时，新分段的
   * 标题和标签就是它新认出的定位规则，确认时得看得到；去掉的分段写原来认的是什么，免得只剩一个 id 看不出丢了哪些行
   */
  segAdded: (id: string, title: string, list: string) =>
    `新增分段「${id}」${title ? `，标题「${title}」` : ''}${list ? `，标签：${list}` : ''}`,
  segRemoved: (id: string, title: string, list: string) =>
    `去掉分段「${id}」${title ? `，原标题「${title}」` : ''}${list ? `，原有标签：${list}` : ''}`,
  segTitle: (id: string, from: string, to: string) => `分段「${id}」的标题「${from}」→「${to}」`,
  labelsAdded: (id: string, list: string) => `分段「${id}」加入标签：${list}`,
  labelsRemoved: (id: string, list: string) => `分段「${id}」去掉标签：${list}`,
  ignoreAdded: (id: string, list: string) => `「${id}」新增忽略规则：${list}`,
  ignoreRemoved: (id: string, list: string) => `「${id}」去掉忽略规则：${list}`,
  relation: (id: string, from: string, to: string) => `关系 ${id}：${from} → ${to}`,
  relationNone: '不登记',
  sheetName: (from: string, to: string) => `工作表名「${from}」→「${to}」`,
  mode: (from: string, to: string) => `导入模式：${from} → ${to}`,
  accumulate: {
    compatible: '与当前版本兼容：早期各期没有的列为空值',
    retire: '部分列自本期起不再导入，早期各期保留原值',
    semantic: '与当前版本不兼容：启用后将重新开始累积',
  } as Record<string, string>,
} as const

/**
 * 回执的只读展示块（ReceiptBlocks.tsx）：「排除的行」、回执摘要。向导回执和版本页的清单视图共用。
 * 排除原因的说法不露枚举值：认不出的原因写「其他原因」
 */
export const RECEIPT_BLOCK_TEXT = {
  excludedReason: {
    hidden_excluded: '隐藏的行（按配方不导入）',
    blank_skipped: '跳过的空行',
    ignored_rows: '按配方忽略的行',
    ignored_outside: '按配方忽略了区域外数字的行',
    after_stop: '列表结束之后的文字行',
    total_not_kept: '只核对、不另存的合计行',
  } as Record<string, string>,
  excludedOther: '其他原因',
  excludedTitle: '排除的行',
  rowSpan: (a: number, b: number) => (a === b ? `第 ${formatNumber(a)} 行` : `第 ${formatNumber(a)}–${formatNumber(b)} 行`),
  cells: (n: number) => `${formatNumber(n)} 格`,
  groupTotal: (rows: number, cells: number) => `共 ${formatNumber(rows)} 行、${formatNumber(cells)} 格`,
  excludedNone: '没有排除的行',
  excludedUnrecorded: '这次导入未记录排除的行',
  canonicalized: (n: number) => `按规范写法存储的取值 ${formatNumber(n)} 种`,
} as const

/** 格子的去向（契约 Role）：网格图例、回执里的格子账 */
export const LEDGER_ROLE_LABEL: Record<string, string> = {
  value: '数据值',
  derived_value: '合计格',
  derived_label: '合计标签',
  col_header: '列表头',
  row_label: '行标签',
  section_title: '分段标题',
  context: '统计期',
  outside_text: '区域外文字',
  ignored_column: '忽略的列',
  hidden_excluded: '排除的隐藏行',
  total_label: '合计标签',
  total_value: '合计格',
  /** 期 3：按配方忽略的行、区域外数字（ignore_rows、ignore_outside） */
  ignored: '按配方忽略',
}

/** 核对结果的状态 */
export const CHECK_STATUS_LABEL: Record<string, string> = {
  passed: '通过',
  mismatch: '不一致',
  unverifiable: '无法核对',
  info: '说明',
}

/** 问题和核对的类别：结构问题不能接受，只能改配方或文件；数据质量可以写明理由后接受 */
export const PROBLEM_CATEGORY_LABEL: Record<string, string> = {
  structure: '结构问题（需修改配方或文件）',
  data_quality: '数据质量（可写明理由后接受）',
  confirm: '需确认',
  input: '需要录入',
  recipe: '配方问题',
  info: '说明',
}

/** 上传新一期的差异种类（recipe_diff 的 kind） */
export const DIFF_KIND_LABEL: Record<string, string> = {
  period: '统计期',
  rows: '行数',
  placeholders: '占位符',
  checks: '核对结果',
  file_name: '文件名',
  full_calc: '打开时重算',
  context_text: '统计期的写法',
  context_source_added: '统计期来源',
  column_order: '列顺序',
  label_writing: '标签写法',
  header_writing: '表头写法',
  canon_values: '规范写法',
  row_order: '行顺序',
  block_order: '各块的先后',
  derived_form: '合计的形态',
  axis_form: '日期表头的形态',
  outside_added: '新增的区域外文字',
  outside_removed: '不再出现的区域外文字',
  outside_changed: '区域外文字有变化',
  outside_moved: '区域外文字挪了位置',
  ignored_columns: '忽略的列',
  hidden: '隐藏的行列',
  // 期 3：按期累积、忽略规则（P3-SPEC 9.5）
  period_added: '新增的一期',
  period_replaced: '替换的一期',
  period_gap: '各期之间的空缺',
  period_backfill: '补传早期',
  columns_added: '新增的列',
  columns_retired: '不再导入的列',
  labels_vary: '各期取值不同',
  ignored_rows: '按配方忽略的行',
}

/**
 * 配方的起草方式（table_recipes.origin）：卡片上「配方第 N 版 · 规则起草」。manual 是在配方面板里改过或
 * 粘贴的规则草稿，mixed 是 AI 草稿又经人改过
 */
export const RECIPE_ORIGIN_LABEL: Record<string, string> = {
  rules: '规则起草',
  ai: 'AI 起草',
  manual: '手工编辑',
  mixed: 'AI 起草后手工修改',
}

/** 配方面板里各取值的显示名（封闭取值，界面不露英文枚举） */
export const RECIPE_CHOICE_LABEL = {
  locateBy: { labels: '按标签', section_title: '按分段标题' } as Record<string, string>,
  dimParser: { hour_range: '时段（如 7-8）', text: '文字' } as Record<string, string>,
  valueType: { INTEGER: '整数', REAL: '小数' } as Record<string, string>,
  blankCross: { reject: '拒收', null: '存为空值（需确认）' } as Record<string, string>,
  blankList: { null: '存为空值', reject: '拒收' } as Record<string, string>,
  textNumber: { reject: '拒收', parse_thousands: '千分位写法按数字保存（需确认）' } as Record<string, string>,
  formulaCross: { reject: '拒收', accept_cached: '按保存值导入（需确认）' } as Record<string, string>,
  formulaList: { accept_cached: '按保存值导入', reject: '拒收' } as Record<string, string>,
  meaning: { 无数据: '无数据', 不适用: '不适用' } as Record<string, string>,
  hiddenRows: { reject_if_any: '有隐藏行时拒收', include: '照常导入', exclude: '跳过' } as Record<string, string>,
  hiddenCols: { reject_if_any: '有隐藏列时拒收', include: '照常导入' } as Record<string, string>,
  fallback: { only_visible_sheet: '用唯一有内容的可见工作表（需确认）', none: '必须同名' } as Record<string, string>,
  otherSheets: { confirm: '记入回执，需确认', reject: '拒收' } as Record<string, string>,
  extraColumns: { reject: '拒收', ignore: '忽略（需确认）' } as Record<string, string>,
  blankRows: { stop: '到此为止', skip: '跳过继续（需确认）' } as Record<string, string>,
  colType: { TEXT: '文字', INTEGER: '整数', REAL: '小数', DATE: '日期' } as Record<string, string>,
  colStore: { '': '默认', canonical: '规范写法', raw: '原文' } as Record<string, string>,
  axisChecks: { contiguous: '逐日连续', covers_context: '恰好覆盖统计期' } as Record<string, string>,
}
