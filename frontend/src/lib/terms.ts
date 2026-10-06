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
 * - 「合并查询」是节点名，也指它做的事：把几次查询的结果在库外按键合并成一张表。不写联表、联邦查询、join。
 *   合并时两边对齐的列（日期、门店）叫「合并键」；合并 SQL 里代表某个输入的表名叫「别名」；被合并的那几次
 *   查询叫「输入」。合并结果里的一格能指回输入的哪一格时叫「逐格来历」，指不回时只有「表级来历」（合并了
 *   哪几个输入、用的哪条合并 SQL）。
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

import type {
  CatalogCardinality, CatalogMeasure, CatalogProfileNumberKey, CatalogSource, CatalogStatus, CatalogTableKind, NodeType, SnapshotReasonCode,
  SqlCheckCode, SqlCheckLevel, ToolTrust,
} from '../types'
import { formatCost, formatNumber } from './format'

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
 * 审批卡上人工审批的运行类别和审的报告的核对概况。出具只发生在成果节点配了出具契约、从已发布版本发起的正式运行里：
 * 探索运行批准后只是输出，卡上要说破，不能让审批人以为自己在签发正式的东西
 */
export const APPROVAL_TEXT = {
  /** 人工审批批了会怎样，按运行类别说 */
  outcome: (runClass: 'formal' | 'exploratory' | null | undefined, approve: boolean) => (runClass === 'formal'
    ? `从已发布版本发起，${approve ? '批准' : '提交'}后继续运行`
    : `${approve ? '批准后输出' : '提交后继续运行'}，这是探索运行，不是正式出具`),
  reportHead: (label: string) => `报告「${label}」的核对`,
  noNumbers: '报告中没有需要核对的数字',
  /** 节点里当场裁判过（正式运行开了结论句裁判）：判了几句、几句证据相矛盾、几句没判成 */
  claimsJudged: (judged: number, contradicted: number, unjudged: number) =>
    `结论句已裁判 ${formatNumber(judged)} 句，证据相矛盾 ${formatNumber(contradicted)} 句`
    + (unjudged ? `，未裁判 ${formatNumber(unjudged)} 句` : ''),
  claimsNone: '报告中没有需要裁判的结论句',
  claimsPending: '结论句尚未核对',
  /** 探索运行、报告没开结论句裁判：按需裁判要等运行结束封存之后。开了裁判的报告交给审批时会在节点里当场判，不走这句 */
  claimsLater: '这份报告没有开启结论句裁判，运行结束后可在证据中逐句请模型判断；开启后，交给审批的报告会在批准前判完',
  /** 开了结论句裁判、却没在节点里判（这条规则之前的探索运行）：按需裁判要等运行结束封存之后 */
  claimsAfterRun: '这次没有在节点内裁判，运行结束后可在证据中逐句请模型判断',
  /** 正式运行、报告没开结论句裁判 */
  claimsOff: '这份报告没有开启结论句裁判',
  openEvidence: '查看逐段证据',
  closeEvidence: '收起证据',
  docLoading: '正在加载报告文档…',
  docMissing: '无法获取报告文档',
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
  merge: '合并查询',
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
  dual_check: '双盲复算',
  challenge: '反方审稿',
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

/**
 * 缺失断言：证据目录有省略时，报告写了「没有查到」「不存在」「缺失」这类结论（违规 absence_claim，重写后仍有时
 * 报告节点另发日志 report_absence_claim）。证据面板的违规清单和运行时间线共用这一套说法
 */
export const ABSENCE_CLAIM_TEXT = {
  label: '缺失断言',
  hint: '写作模型没有看到全部证据：被省略的查询仍在证据台账里，看不到不等于不存在，这类结论应写作「本报告未覆盖」。'
    + '报告照常产出，这一处只标注、不判失败',
  title: (n?: number) => `报告写了${n ? ` ${formatNumber(n)} 处` : ''}缺失断言，已标注（证据目录有省略）`,
  next: '被省略的查询仍在证据台账里，可在证据面板中查看。请在报告撰写节点的指令中要求未展开的方向写作「本报告未覆盖」，'
    + '或换用上下文窗口更大的写作模型后重新运行',
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
  /** 指标拿截断的查询结果整组算出来（计数、求和……）：值只算到了取回的那部分，出具按缺口降档 */
  incomplete: '结果不完整',
  incompleteFallback: '基于被截断的查询结果计算，结果不完整',
  /** 指标所依据的查询对照数据目录查出了错误级的问题：值照算，出具按缺口降档 */
  sqlCheckFailed: 'SQL 检查未通过',
  sqlCheckFallback: '所依据的查询未通过 SQL 检查，结果不可靠',
  /** 输入是 Agent 交来、被截断切开的数组字段 */
  inputTruncated: '查询结果已截断，该字段只含取回的部分行',
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

  // ---- 期 4：核查状态。指标步骤上的双盲复算、取自遮罩列的输入（反方审稿的判断复用审批卡的 CHALLENGE_TEXT）----
  /**
   * 指标步骤上的一行：「双盲复算：状态（盲算 值），审核人的选择。后果」。状态词和同一节里按代入式重算的「复算一致 /
   * 无法复算」分开说（两件事），用时间线上的说法（与盲算结果一致 / 不一致）；选项名照审批卡的 choices 原样加引号。
   * 运行已经结束：后果只说节点已经做了什么（记为缺口），降没降档看报告的出具横幅，这里不预告
   */
  dualCheck: {
    label: '双盲复算',
    status: { agreed: '与盲算结果一致', disagreed: '与盲算结果不一致', unchecked: '未能完成', skipped: '已跳过' } as Record<string, string>,
    /** 服务端给了一个界面不认识的状态：照写原值，不归到哪一类 */
    unknown: (status: string) => `未知状态「${status}」`,
    /** 盲算的值，服务端按口径卡的格式渲染好的 */
    blind: (value: string) => `盲算 ${value}`,
    /** choice 是审批卡上那个选项的原名（DUAL_CHECK_TEXT.approval.choices） */
    choice: (choice: string) => `审核人选择「${choice}」`,
    noChoice: '审核人没有作出选择',
    /** 采用盲算、两边都不对、没选：报告里的数没换，节点把它记成了缺口。正式运行叫出具缺口，探索运行不出具 */
    kept: (formal: boolean) => `报告中的数仍取自原流程，这个指标已记为${formal ? '出具缺口' : '缺口'}`,
    /** 未能完成：同样记了缺口 */
    gap: (formal: boolean) => `这个指标已记为${formal ? '出具缺口' : '缺口'}`,
    /** 跳过的原因，按这个指标说；认不出的原因代码不写 */
    skip: {
      disabled: '节点或设置中已关闭',
      no_inputs: '这个指标没有来自查询的输入',
      not_targeted: '这个指标不在复算范围内',
    } as Record<string, string>,
  },
  /** 指标的输入取自数据源设置了遮罩的列：标签和代入式里不显示值 */
  maskedInput: '取自设置了遮罩的列，不显示值',
  /** 遮没遮核对不了（快照、取值链上的产出读不出来）：原因是服务端写好的一句 */
  unverifiedInput: (why: string) => `${why}，不显示值`,
  maskedMismatch: '与快照不一致；取自设置了遮罩的列，不显示值',
} as const

/**
 * 合并查询（merge 节点）在检查器、运行详情、证据面板和推断来源里的说法。用到的术语（输入、别名、合并键、
 * 逐格来历、表级来历）见文件开头。行号对人一律从 1 数，和证据面板查询表格最前面那一列「#」一致
 */
export const MERGE_TEXT = {
  // ---- 检查器 ----
  addInput: '添加输入',
  alias: '别名（表名）',
  node: '查询节点',
  pick: '选择上游的查询节点',
  removeInput: (alias: string) => `删除输入 ${alias || '（未命名）'}`,
  noUpstream: '上游没有查询节点。请先在本节点之前连接选择了数据库查询工具的「调用工具」节点，或另一个「合并查询」节点',
  missingNode: (id: string) => `节点「${id}」不在上游或已删除，请重新选择`,
  inputsHint: '别名就是合并 SQL 中的表名。各输入请先在自己的库中聚合到相同的合并键和粒度（例如日期 + 门店），任何一个输入被截断，合并都会失败',
  // ---- 运行详情 ----
  stepTitle: (aliases: string[]) => (aliases.length ? `合并 ${aliases.join('、')} 的查询结果` : '合并查询结果'),
  sql: '合并 SQL',
  copySql: '复制合并 SQL',
  inputs: (n: number) => `合并自 ${formatNumber(n)} 个输入`,
  rows: (n: number) => `${formatNumber(n)} 行`,
  source: (name: string) => `数据源 ${name}`,
  warnings: (n: number) => `${formatNumber(n)} 条警告，详见本节点下方`,
  // ---- 证据面板 ----
  mergedInto: (alias: string) => `合并查询 ${alias} 的输入`,
  traced: '被引用的格来自',
  cellAt: (alias: string, row: number, column: string) => `${alias} 第 ${formatNumber(row + 1)} 行「${column}」`,
  noLineage: '没有逐格来历，只有表级来历：见上方的输入和合并 SQL',
  unsealed: '不在封存范围内',
  notInCatalog: '不在本报告的证据目录中',
  executionWarnings: '执行时的警告',
  // ---- 推断来源 ----
  hop: (alias: string, to: string) => `经合并查询 ${alias} 追到 ${to}`,
  hopMissing: (alias: string) => `合并查询 ${alias} 中的这一格没有逐格来历`,
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
/** 运行的金额。有调用的模型不在价格目录中时金额估不出来，不写「$0」（看着像没花钱） */
export const COST_TEXT = {
  unpriced: '金额无法估算',
  /** 悬停说明，n 是估不出金额的调用次数 */
  unpricedTitle: (n: number) => `${formatNumber(n)} 次调用的模型不在价格目录中，金额无法估算，未计入`,
} as const

/**
 * 运行级金额的写法。unpriced 是估不出金额的调用次数：一分都没估出来写「金额无法估算」，估出了一部分写「≥ $x」；
 * 运行中（approx）只算已上报的调用，也写「≥」。没有金额返回 null，由调用方写占位
 */
export function runCostText(usd: number | null | undefined, unpriced: number, approx = false): string | null {
  const known = typeof usd === 'number' && Number.isFinite(usd)
  if (unpriced > 0 && !(known && usd > 0)) return COST_TEXT.unpriced
  if (!known) return null
  return `${(approx || unpriced > 0) && usd > 0 ? '≥ ' : ''}${formatCost(usd)}`
}

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

// ===========================================================================
// 业务数据目录（数据源卡片上的「数据目录」）
//
// 术语：
// - 「数据目录」：每张表、每一列的业务说明和表之间的关联关系。只记数据事实，不放计算公式（公式在口径卡里）。
// - 表级：中文名、说明、粒度（一行代表什么）、业务主键、表类型、业务日期、有效记录条件、去重规则。
// - 列级：中文名、含义、单位、度量类型、码值。度量类型的 flow 写「可累加」，不写「流量」；表类型的 fact 写「明细表」。
// - 关联关系：本表字段 → 目标表的字段，带基数（多对一……）和覆盖率。
// - 状态：推断 / 已验证 / 已确认 / 已驳回。来源：数据库注释 / 外键约束 / 命名推断 / 数据剖析 / 模型起草 / 人工填写。
// - 动作：起草（批量生成推断项）、确认、驳回、恢复（回到起草时的状态）。直接编辑即确认。
// ===========================================================================

export const CATALOG_TERMS = {
  catalog: '数据目录',
  grain: '粒度',
  keys: '业务主键',
  measure: '度量类型',
  codes: '码值',
  relation: '关联关系',
  cardinality: '基数',
  coverage: '覆盖率',
  draft: '起草',
} as const

/** 项的状态。四种状态在界面上用同一套样式（pages/catalog/parts.tsx 的 StatusMark） */
export const CATALOG_STATUS_LABEL: Record<CatalogStatus, string> = {
  proposed: '推断',
  verified: '已验证',
  confirmed: '已确认',
  rejected: '已驳回',
}

export const CATALOG_STATUS_HINT: Record<CatalogStatus, string> = {
  proposed: '只作提示，确认后才参与 SQL 检查',
  verified: '有确证（外键约束或数据剖析），参与 SQL 检查',
  confirmed: '人工确认，参与 SQL 检查',
  rejected: '人工驳回，不提供给助手，之后起草也不会再提出',
}

export const CATALOG_SOURCE_LABEL: Record<CatalogSource, string> = {
  comment: '数据库注释',
  fk: '外键约束',
  name: '命名推断',
  profile: '数据剖析',
  llm: '模型起草',
  human: '人工填写',
}

export const CATALOG_KIND_LABEL: Record<CatalogTableKind, string> = {
  fact: '明细表',
  dimension: '维度表',
  snapshot: '快照表',
  log: '日志表',
  config: '配置表',
}

export const CATALOG_KIND_HINT: Record<CatalogTableKind, string> = {
  fact: '一行是一笔业务事件，如订单、入园、销售',
  dimension: '描述业务对象，如门店、票种、渠道',
  snapshot: '按时点记录的状态，如每日库存',
  log: '系统操作或变更记录',
  config: '参数、映射等配置',
}

export const CATALOG_MEASURE_LABEL: Record<CatalogMeasure, string> = {
  flow: '可累加',
  stock: '存量',
  ratio: '比率',
  identifier: '标识',
  status: '状态',
  attribute: '属性',
}

export const CATALOG_MEASURE_HINT: Record<CatalogMeasure, string> = {
  flow: '可以跨期加总，如金额、人数',
  stock: '时点值，不能跨期加总，如库存、余额',
  ratio: '不能直接加总，如折扣率、转化率',
  identifier: '编号或外键，不参与计算',
  status: '状态码，按码值解释',
  attribute: '描述性属性，如名称、类别',
}

export const CATALOG_CARDINALITY_LABEL: Record<CatalogCardinality, string> = {
  many_to_one: '多对一',
  one_to_one: '一对一',
  one_to_many: '一对多',
}

/** 表级各项，按显示顺序 */
export const CATALOG_TABLE_FIELD_LABEL = {
  label: '中文名',
  description: '说明',
  grain: '粒度',
  keys: '业务主键',
  kind: '表类型',
  business_date: '业务日期',
  valid_filter: '有效记录条件',
  dedup: '去重规则',
} as const

export const CATALOG_TABLE_FIELD_HINT: Record<keyof typeof CATALOG_TABLE_FIELD_LABEL, string> = {
  label: '表的业务名称',
  description: '这张表记录什么、怎么用',
  grain: '一行代表什么，如「一张门票的一次入园」',
  keys: '唯一确定一行业务记录的列',
  kind: '明细、维度、快照、日志或配置',
  business_date: '按哪一列归属到日期',
  valid_filter: '有效记录的 SQL 条件，如 status <> 9',
  dedup: '同一笔业务出现多行时如何取舍',
}

/** 列级各项，按显示顺序 */
export const CATALOG_COLUMN_FIELD_LABEL = {
  label: '中文名',
  meaning: '含义',
  unit: '单位',
  measure: '度量类型',
  codes: '码值',
} as const

export const CATALOG_TEXT = {
  // ---- 入口
  open: '数据目录',
  openHint: '记录每张表、每一列的业务含义和表之间的关联关系，助手建图和查询时读取',
  // ---- 页面
  title: (name: string) => `「${name}」的数据目录`,
  subtitle: '先起草，再按使用次数逐表确认。推断的项只作提示，确认后才参与 SQL 检查',
  back: '返回数据源',
  sourceMissing: '该数据源不存在',
  sourceMissingBody: '可能已被删除，请返回数据源列表查看。',
  noSchemaTitle: '还没有表结构',
  noSchemaBody: (note: string) => `${note.replace(/[。.]$/, '')}。数据目录按表结构逐表记录，请先回到数据源卡片点击「探查结构」。`,
  noSchemaUploadBody: '这个表格还没有可用的表结构，请重新上传后再编写数据目录。',
  emptyTitle: '还没有数据目录',
  emptyBody: '先起草，再逐表确认。起草按数据库注释、外键约束和列名推断，也可以请助手的模型起草中文名和含义。',
  // ---- 导入表格的说明
  systemTitle: '导入时生成的说明',
  systemBody: '这个数据源由导入表格生成，表和列的说明由系统按核对结果生成，只读，不能在数据目录中改写。',
  systemCovered: '目录中与说明重复的项（表的粒度和说明、列的含义和单位）确认后才提供给助手。',
  systemTag: '导入时生成',
  systemTableNote: '表说明',
  // ---- 表清单
  search: '搜索表名或中文名',
  filterLabel: '按状态筛选',
  // 「已确认」专指人工确认：只有已验证项（外键约束、数据剖析核实）的表不能说「全部已确认」，按「没有待确认项」说
  filter: { all: '全部', pending: '有待确认项', done: '没有待确认项', none: '没有目录' },
  filterHint: {
    all: '全部表',
    pending: '还有推断状态的项，需要逐项确认',
    done: '有目录，没有推断状态的项。已验证的项由外键约束或数据剖析核实，不需要逐项确认',
    none: '还没有任何目录项',
  },
  sortLabel: '排序',
  sort: { usage: '按使用次数', pending: '按待确认项', name: '按表名' },
  tableCount: (shown: number, total: number) =>
    (shown === total ? `${formatNumber(total)} 张表` : `${formatNumber(shown)} / ${formatNumber(total)} 张表`),
  selectAll: '全选当前结果',
  selectTable: (name: string) => `选择 ${name}`,
  selected: (n: number) => `已选 ${formatNumber(n)} 张`,
  clearSelection: '取消选择',
  noLabel: '未填写中文名',
  view: '视图',
  missing: '表结构中已没有这张表',
  noCatalog: '没有目录',
  usage: (n: number) => `使用 ${formatNumber(n)} 次`,
  usageHint: '被运行查询过的次数，同一次运行中结果相同的重复查询只算一次',
  relationCount: (n: number) => `${formatNumber(n)} 个关联`,
  countTitle: (label: string, n: number) => `${label} ${formatNumber(n)} 项`,
  noMatch: '没有符合条件的表',
  noMatchBody: '请换个关键词或筛选条件。',
  // ---- 用到但没确认（页面顶部的摘要）
  usageLead: (used: number) => `运行中查询过的 ${formatNumber(used)} 张表里，`,
  usagePending: (n: number) => `有 ${formatNumber(n)} 张还有推断项未确认`,
  usageNone: (n: number) => `${formatNumber(n)} 张还没有目录`,
  usageAllDone: (used: number) => `运行中查询过的 ${formatNumber(used)} 张表都没有待确认项`,
  usageHintPending: '筛选有待确认项的表，按使用次数排序',
  usageHintNone: '筛选没有目录的表，按使用次数排序',
  // ---- 概览（没有选中表时）
  overviewTitle: '选择一张表开始审阅',
  overviewBody: '按使用次数从高到低逐表确认。推断的项只作提示，确认后才参与 SQL 检查。',
  overviewStats: (total: number, pending: number, done: number, none: number) =>
    `共 ${formatNumber(total)} 张表：${formatNumber(pending)} 张有待确认项，${formatNumber(done)} 张没有待确认项，${formatNumber(none)} 张没有目录`,
  reviewNext: (name: string) => `审阅「${name}」`,
  reviewNextHint: '使用次数最多、还有待确认项的表',
  legend: '状态说明',
  // ---- 起草
  draft: '起草',
  draftSelected: (n: number) => `起草所选（${formatNumber(n)}）`,
  draftTitle: '起草数据目录',
  draftScope: '起草范围',
  scopeSelected: (n: number) => `已选的 ${formatNumber(n)} 张表`,
  scopeFiltered: (n: number) => `当前筛选结果中的 ${formatNumber(n)} 张表`,
  scopeTop: (n: number) => `使用次数最多的 ${formatNumber(n)} 张表`,
  scopeAll: (n: number) => `全部 ${formatNumber(n)} 张表`,
  draftBase: '按数据库注释、外键约束和列名推断起草。已确认、已驳回的项不会被改动；起草出的项都是推断，需要逐项确认。',
  useModel: '用模型起草',
  useModelHint: '请助手的模型按表结构起草中文名、说明、粒度和列的含义。只发送表结构（表名、列名、类型、约束、注释），不发送数据；耗时较长，并产生模型费用。',
  draftStart: (n: number) => `起草 ${formatNumber(n)} 张表`,
  drafting: '正在起草…',
  draftProgress: (done: number, total: number) => `已完成 ${formatNumber(done)} / ${formatNumber(total)} 张表`,
  draftStop: '停止',
  draftStopping: '当前这一批完成后停止…',
  draftDone: '起草完成',
  draftStopped: (done: number, total: number) => `已停止：完成 ${formatNumber(done)} / ${formatNumber(total)} 张表，其余未起草`,
  draftFailed: '起草中断',
  draftSummary: (added: number, updated: number, removed: number) =>
    `新增 ${formatNumber(added)} 项、更新 ${formatNumber(updated)} 项${removed ? `、删除 ${formatNumber(removed)} 项` : ''}`,
  // 只在起草确实给出了内容、只是和现有目录一致时用：有表的模型起草失败或没给出可用内容（model_error）时
  // 不能说「没有变化」，改用 draftNothingWritten
  draftNoChange: '没有变化：起草出的内容与现有目录一致',
  draftNothingWritten: '目录没有更新：模型没有给出可用的内容，原因见下方',
  draftModelSkipped: (reason: string) => `模型未参与起草：${reason.replace(/[。.]$/, '')}。已按注释、外键和命名起草。`,
  draftModelUsed: (model: string) => `模型：${model}`,
  draftTableErrors: (n: number) => `${formatNumber(n)} 张表未能起草`,
  draftModelErrors: (n: number) => `${formatNumber(n)} 张表的模型起草失败，已按注释、外键和命名起草`,
  draftClose: '完成',
  moreTables: (n: number) => `…另有 ${formatNumber(n)} 张`,
  cancel: '取消',
  draftBackground: '起草仍在进行，当前这一批完成后停止',
  // ---- 表详情
  backToList: '返回表清单',
  prev: '上一张',
  next: '下一张',
  updatedBy: (who: string, at: string) => `${who} 于 ${at} 修改`,
  updatedAt: (at: string) => `${at} 修改`,
  notStarted: '尚未建立目录',
  missingBody: '表结构中已没有这张表（可能已删除或改名）。目录仍然保留，可以查看、驳回或删除其中的项。',
  sectionTable: '表级信息',
  sectionColumns: '列',
  sectionRelations: '关联关系',
  columnCount: (shown: number, total: number) =>
    (shown === total ? `${formatNumber(total)} 列` : `${formatNumber(shown)} / ${formatNumber(total)} 列`),
  columnFilter: '筛选列名或中文名',
  onlyPending: '只看有推断项的列',
  columnHead: { name: '列名', type: '类型', label: '中文名', meaning: '含义', unit: '单位', measure: '度量类型', codes: '码值' },
  pk: '主键',
  columnMissing: '表结构中已没有这一列',
  noColumns: '没有符合条件的列',
  empty: '未填写',
  relationHead: { from: '本表字段', to: '目标表', toColumns: '目标字段', cardinality: '基数', coverage: '覆盖率', source: '来源', status: '状态' },
  noRelations: '还没有关联关系。起草会按外键约束和列名推断，也可以在编辑时添加。',
  openTable: (name: string) => `打开「${name}」`,
  coverageValue: (pct: number) => `${formatNumber(pct)}%`,
  dateColumn: '日期列',
  dateRule: '规则',
  dateTimezone: '时区',
  // ---- 单项审阅
  itemTitle: (where: string) => `${where}的来源和状态`,
  itemSource: (s: string) => `来源：${s}`,
  itemUpdated: (at: string) => `更新于 ${at}`,
  confirm: '确认',
  reject: '驳回',
  reset: '恢复',
  resetHint: '回到起草时的状态',
  remove: '删除',
  removeHint: '人工填写的项没有起草时的状态，恢复即删除',
  reviewed: {
    confirm: (where: string) => `已确认${where}`,
    reject: (where: string) => `已驳回${where}`,
    reset: (where: string) => `已恢复${where}`,
  },
  removed: (where: string) => `已删除${where}`,
  reviewWhileEditing: '正在编辑这张表，保存或取消后再逐项审阅',
  whereTable: (field: string) => `表的${field}`,
  whereColumn: (col: string, field: string) => `列 ${col} 的${field}`,
  whereRelation: (to: string) => `指向 ${to} 的关联关系`,
  // ---- 批量确认
  confirmAll: (n: number) => `确认本表全部推断（${formatNumber(n)}）`,
  confirmAllTitle: (n: number, table: string) => `确认「${table}」的 ${formatNumber(n)} 项推断？`,
  confirmAllConsequences: ['确认后这些项参与 SQL 检查', '来源保持不变，状态改为已确认', '之后仍可逐项恢复'],
  confirmAllAction: (n: number) => `确认 ${formatNumber(n)} 项`,
  confirmColumn: '确认本列推断',
  confirmColumnLabel: (col: string, n: number) => `确认列 ${col} 的 ${formatNumber(n)} 项推断`,
  confirmedN: (n: number) => `已确认 ${formatNumber(n)} 项`,
  // ---- 编辑
  edit: '编辑',
  editHint: '保存后，改动过的项记为人工填写、已确认',
  clearHint: '清空一项即删除；不希望起草再次提出的项，请用「驳回」',
  save: '保存',
  saving: '正在保存…',
  cancelEdit: '取消',
  cancelEditTitle: '放弃对这张表的修改？',
  cancelEditAction: '放弃修改',
  changedMark: '已修改',
  changes: (n: number) => `${formatNumber(n)} 处修改`,
  noChanges: '尚未修改',
  saved: '已保存，改动过的项已记为人工确认',
  invalidCount: (n: number) => `${formatNumber(n)} 处格式不正确`,
  keysPlaceholder: '列名，用顿号或逗号分隔',
  keysUnknown: (cols: string) => `表结构中没有这些列：${cols}`,
  codesPlaceholder: '1=已支付',
  codesHint: '每行一个，写成「码值=含义」，含义可暂不填写',
  codesInvalid: (line: number) => `第 ${formatNumber(line)} 行应写成「码值=含义」`,
  codesDuplicate: (code: string) => `码值 ${code} 重复`,
  dateColumnRequired: '填写规则或时区时，日期列必填',
  tooLong: (n: number) => `不超过 ${formatNumber(n)} 字`,
  kindNone: '未填写',
  measureNone: '未填写',
  cardinalityNone: '未知',
  addRelation: '添加关联关系',
  removeRelation: '删除这条关联关系',
  rejectRelation: '驳回',
  rejectRelationLabel: '驳回这条关联关系',
  undoRejectRelation: '撤销驳回',
  rejectingMark: '保存后驳回',
  relationRemoveHint: '人工添加的关联关系可以删除；外键约束、命名推断、数据剖析得出的只能驳回，驳回后不提供给助手、不参与 SQL 检查，之后起草也不会再提出',
  relationColumns: '本表字段，用逗号分隔',
  relationTarget: '目标表',
  relationToColumns: '目标字段，用逗号分隔',
  relationIncomplete: '本表字段、目标表和目标字段都要填写',
  relationMismatch: '两端的字段数不一致',
  relationDuplicate: '与另一条关联关系重复',
  rejectedPlaceholder: (value: string) => `已驳回：${value}`,
  leaveTitle: '数据目录有未保存的修改',
  leaveConsequences: ['离开后修改将丢失'],
  leaveConfirm: '放弃修改并离开',
  // ---- 冲突与错误
  conflictTitle: '这张表的数据目录刚被其他人修改过',
  conflictEditing: '你的修改尚未保存。重新载入会放弃你的修改并显示最新内容；如需保留，请先记下要改的地方。',
  conflictReview: '刚才的操作未生效，请重新载入后再操作。',
  reload: '重新载入',
  reloadDiscardTitle: '放弃未保存的修改并重新载入？',
  reloadDiscardAction: '放弃修改并重新载入',
}

// ===========================================================================
// 目录修改提案：用户在助手（画布）或问数据页说出一条数据事实时，助手提出的数据目录修改。
//
// 术语：
// - 卡片标题写「建议更新数据目录」；两个动作是「保存到数据目录」和「忽略」。提案在保存之前不写入目录。
// - 每一项写「改前 / 改后 / 理由」。保存后改动过的项记为人工填写、已确认（和直接编辑同一条规矩）。
// - 别人在提案之后改过这张表：写「这张表刚被修改过」，给「重新载入」，按最新内容重算改前和改后再保存。
// ===========================================================================

export const CATALOG_PATCH_TEXT = {
  title: '建议更新数据目录',
  /** 过程里的那一行 */
  step: (table: string, n: number) => `建议更新「${table}」的数据目录（${formatNumber(n)} 项）`,
  stepSub: '确认后才写入数据目录',
  before: '改前',
  after: '改后',
  reason: '理由',
  empty: '未填写',
  stateConfirm: '值不变，保存即确认',
  stateSame: '已是这个值',
  save: '保存到数据目录',
  saving: '正在保存…',
  ignore: '忽略',
  hint: '保存后改动的项记为人工填写、已确认，助手和 SQL 检查随即采用',
  saved: (version: number) => `已保存到数据目录（第 ${formatNumber(version)} 版）`,
  savedToast: (table: string) => `已更新「${table}」的数据目录`,
  ignored: '已忽略这条建议，数据目录未修改',
  undoIgnore: '重新查看',
  open: '在数据目录中查看',
  conflictTitle: '这张表刚被修改过',
  conflictBody: '其他人在这条建议之后修改了这张表的数据目录。重新载入后按最新内容重算改前和改后，确认后再保存。',
  reload: '重新载入',
  reloading: '正在重新载入…',
  allSame: '数据目录中已是这些值，无需保存',
  problems: '以下几项已无法按建议保存：',
  whereRelationNew: (to: string) => `新增指向 ${to} 的关联关系`,
  sourceOf: (source: string) => `数据源「${source}」`,
}

/**
 * 影响面：哪些已发布、受管的模板引用这张表。按每个模板当前的已发布版本统计（正式运行用的是它）。
 * 「直接引用」：查询的 SQL 里写着这张表；「可能涉及」：Agent 绑定了这个数据源的查询工具，SQL 运行时才生成。
 */
export const CATALOG_IMPACT_TEXT = {
  title: '引用这张表的模板',
  hint: '按每个模板当前的已发布版本统计',
  none: '没有已发布或受管的模板引用这张表',
  count: (n: number) => `${formatNumber(n)} 个模板`,
  impact: { direct: '直接引用', possible: '可能涉及' } as Record<'direct' | 'possible', string>,
  impactHint: {
    direct: '查询的 SQL 中写着这张表',
    possible: 'Agent 绑定了这个数据源的查询工具，SQL 在运行时生成，可能用到这张表',
  } as Record<'direct' | 'possible', string>,
  version: (v: number) => `v${v}`,
  via: (labels: string) => `经由输入${labels}`,
  member: (name: string) => `成员「${name}」`,
  afterSave: (n: number) => `${formatNumber(n)} 个已发布模板引用这张表，下次正式运行时会提示数据目录有变化：`,
  afterSaveNone: '没有已发布或受管的模板引用这张表',
  more: (n: number) => `另有 ${formatNumber(n)} 个`,
  error: '无法统计引用这张表的模板',
  retry: '重试',
}

/**
 * 发布时的目录版本：发布时记下这一版用到的表的数据目录版本；从这一版发起正式运行时目录有变化，提醒而不拦。
 * 版本号写「第 N 版」，还没有目录的表写「尚无目录」。
 *
 * 和影响面（CATALOG_IMPACT_TEXT）同一个分法：SQL 里写着的表是直接引用，说「有变化」；Agent 绑定了数据源的查询工具、
 * SQL 运行时才写的，记的是这个数据源中所有有目录的表，说「Agent 可能查询的表有变化」——两组分开写，不混成一句。
 */
export const CATALOG_DRIFT_TEXT = {
  /** 运行里的提醒：「自发布以来，数据目录中「入园记录」等 2 张表有变化」 */
  title: (first: string, n: number) => (n > 1
    ? `自发布以来，数据目录中「${first}」等 ${formatNumber(n)} 张表有变化`
    : `自发布以来，数据目录中「${first}」有变化`),
  /** 只有 Agent 可能查询的表变了：「自发布以来，Agent 可能查询的表有变化：「入园记录」等 3 张」 */
  possibleTitle: (first: string, n: number) => (n > 1
    ? `自发布以来，Agent 可能查询的表有变化：「${first}」等 ${formatNumber(n)} 张`
    : `自发布以来，Agent 可能查询的表有变化：「${first}」`),
  /** 两组都有：直接引用的标题后面补一句 */
  alsoPossible: (n: number) => `；另有 ${formatNumber(n)} 张 Agent 可能查询的表有变化`,
  sub: '运行未被拦截。请核对这些表的目录修改是否影响本次结果',
  table: (label: string | null, table: string) => (label ? `${label}（${table}）` : table),
  version: (v: number | null) => (v ? `第 ${formatNumber(v)} 版` : '尚无目录'),
  line: (source: string, table: string, from: string, to: string) => `「${source}」${table}：发布时${from}，现为${to}`,
  open: '查看目录',
  /** 可能涉及那一组的小标题和说明（运行横幅、时间线展开、版本历史共用） */
  possibleHead: 'Agent 可能查询的表',
  possibleHint: 'Agent 绑定了这些数据源的查询工具，SQL 在运行时生成：发布时记下了数据源中所有有目录的表，不一定都会用到',
  // ---- 版本历史里的「发布时的目录版本」
  section: '发布时的目录版本',
  sectionHint: '这一版 SQL 用到的表，以及 Agent 可能查询的表，在发布时的数据目录版本',
  changedSince: (now: string) => `之后有变化，现为${now}`,
  changedCount: (n: number) => `${formatNumber(n)} 张表在发布之后有变化，下次正式运行时会提醒`,
  unchanged: '发布之后这些表的目录没有变化',
  empty: '这一版的 SQL 没有用到数据库表',
  /** 可能涉及的数据源在发布时一张有目录的表都没有 */
  possibleNone: (source: string) => `「${source}」发布时还没有目录`,
}

/**
 * 表结构只探查了一部分：探查结构时每个数据源最多取 200 张表（服务端 introspect._MAX_TABLES），多出来的表不在
 * 表结构里，数据目录列不出，助手也看不到它们的字段。数据目录页顶部和助手「参考了哪些表」那一行都要说出来。
 */
export const SCHEMA_PARTIAL_TEXT = {
  /** 数据目录页顶部：「这个数据源共有 205 张表，只探查了前 200 张」 */
  title: (total: number, explored: number) =>
    `这个数据源共有 ${formatNumber(total)} 张表，只探查了前 ${formatNumber(explored)} 张`,
  body: (rest: number) => `探查结构时每个数据源最多取 200 张表（按数据库列出的顺序）：生产库动辄上千张，全部取回既慢也看不完。`
    + `其余 ${formatNumber(rest)} 张不在数据目录中，助手也看不到它们的字段。`,
  next: '所需的表不在清单中时，请回到数据源卡片换一个 schema，或改用只能看到所需表的数据库账号缩小范围，然后重新「探查结构」。',
  /** 助手「参考了哪些表」那一行：「共 205 张，只探查了 200 张」 */
  context: (total: number, explored: number) => `共 ${formatNumber(total)} 张，只探查了 ${formatNumber(explored)} 张`,
}

// ===========================================================================
// 数据剖析（数据源设置里的开关和预算；数据目录页的「数据剖析」）
//
// 术语：
// - 「数据剖析」：对业务库发少量只读查询，核实推断的关联关系（覆盖率、基数），取状态类列的码值候选，提议业务日期。
//   动作写「剖析」，不写扫描、探测。默认关闭，按数据源开启。
// - 预算五项：查询次数上限、单条查询时限、抽样键值数、整表统计行数上限、总时长上限。标签和服务端校验报错里的叫法
//   一致（catalog_profile._NUMBER_FIELDS），保存被拒时按它认出是哪一项。
// ===========================================================================

export const PROFILE_FIELD_LABEL: Record<'enabled' | CatalogProfileNumberKey, string> = {
  enabled: '开启数据剖析',
  max_queries: '查询次数上限',
  query_timeout_s: '单条查询时限',
  sample_size: '抽样键值数',
  max_scan_rows: '整表统计行数上限',
  max_total_s: '总时长上限',
}

export const PROFILE_FIELD_UNIT: Record<CatalogProfileNumberKey, string> = {
  max_queries: '条',
  query_timeout_s: '秒',
  sample_size: '个',
  max_scan_rows: '行',
  max_total_s: '秒',
}

export const PROFILE_FIELD_HINT: Record<CatalogProfileNumberKey, string> = {
  max_queries: '一次剖析最多发出的查询条数，用完即停止',
  query_timeout_s: '超过即中断这条查询；不超过数据源自身的查询时限',
  sample_size: '核对一条关联关系时，从本表抽取的不同键值个数',
  max_scan_rows: '不超过此行数的表才做去重计数、最小值和最大值等整表统计',
  max_total_s: '一次剖析的总时长，到时即停止',
}

export const PROFILE_TEXT = {
  // ---- 设置
  section: '数据剖析',
  enableHint: '默认关闭。开启后可在数据目录中核实推断的关联关系、取状态类列的码值候选',
  risk: (queries: number, timeout: number, total: number) =>
    `开启后，每次剖析会对这个数据源发出只读查询：最多 ${formatNumber(queries)} 条，单条不超过 ${formatNumber(timeout)} 秒，`
    + `总时长不超过 ${formatNumber(total)} 秒。不读取明细行，遮罩的列不取样。`,
  range: (min: number, max: number, unit: string, def: number) =>
    `范围 ${formatNumber(min)}–${formatNumber(max)} ${unit}，默认 ${formatNumber(def)}`,
  scanZero: '填 0 表示一律不做整表统计',
  placeholder: (def: number) => `默认 ${formatNumber(def)}`,
  rangeError: (min: number, max: number, integer: boolean) =>
    `请填写 ${formatNumber(min)} 到 ${formatNumber(max)} 之间的${integer ? '整数' : '数'}`,
  invalid: (n: number) => `数据剖析有 ${formatNumber(n)} 项设置不正确`,
  // ---- 数据源卡片
  chip: '数据剖析已开启',
  chipHint: (queries: number, timeout: number) =>
    `数据剖析已开启：每次最多 ${formatNumber(queries)} 条只读查询，单条不超过 ${formatNumber(timeout)} 秒`,
  open: '设置数据剖析',
  dialogTitle: (name: string) => `「${name}」的数据剖析设置`,
  dialogBody: '设置保存在数据源上，对数据目录中的每次剖析生效。',
  savedOn: (name: string) => `「${name}」已开启数据剖析`,
  savedOff: (name: string) => `「${name}」已关闭数据剖析`,
  saved: (name: string) => `已保存「${name}」的数据剖析设置`,
  // ---- 数据目录页：范围与确认
  action: '数据剖析',
  actionHint: '对业务库发少量只读查询：核实推断的关联关系、取码值候选、提议业务日期',
  profileSelected: (n: number) => `剖析所选（${formatNumber(n)}）`,
  title: '数据剖析',
  intro: '对业务库发少量只读查询，核实推断的关联关系（覆盖率、基数），取状态类列的码值候选，提议业务日期。'
    + '结论写入数据目录；已确认、已驳回的项不会被改动。',
  scope: '剖析范围',
  scopeSelected: (n: number) => `已选的 ${formatNumber(n)} 张表`,
  scopeFiltered: (n: number) => `当前筛选结果中的 ${formatNumber(n)} 张表`,
  scopeDefault: '使用次数最多、有待核实关联关系的表',
  scopeDefaultHint: (n: number) => `由服务端挑选，至多 ${formatNumber(n)} 张`,
  scopeTooMany: (max: number) => `超过 ${formatNumber(max)} 张，请缩小范围`,
  budget: '本次预算',
  budgetQueries: (queries: number, timeout: number) =>
    `最多 ${formatNumber(queries)} 条只读查询，单条不超过 ${formatNumber(timeout)} 秒`,
  budgetTotal: (total: number) => `总时长不超过 ${formatNumber(total)} 秒，到时即停止`,
  budgetSample: (n: number) => `每条关联关系抽样 ${formatNumber(n)} 个键值`,
  budgetScan: (n: number) => (n > 0 ? `行数超过 ${formatNumber(n)} 的表不做整表统计` : '不做整表统计'),
  budgetFrom: (name: string) => `按「${name}」的数据剖析设置`,
  editSettings: '修改设置',
  noCancel: '剖析在服务端一次完成，开始后无法中途停止。',
  start: '开始剖析',
  again: '重新剖析',
  disabledTitle: '这个数据源未开启数据剖析',
  disabledBody: '剖析会对业务库发出只读查询，需要先在数据源设置中开启。',
  enable: '开启数据剖析',
  // ---- 进行中
  running: '正在剖析…',
  elapsed: (used: string, total: string) => `已用时 ${used}（总时长上限 ${total}）`,
  runningHint: '剖析在服务端一次完成，无法中途停止。关闭此窗口不影响剖析，完成后会提示结果。',
  background: '在后台继续',
  headerRunning: (used: string) => `正在剖析 ${used}`,
  headerRunningHint: '数据剖析进行中，点击查看',
  leavePage: '离开本页不会中止剖析，结论仍会写入数据目录，但不再显示本次的报告。',
  // ---- 报告
  done: '剖析完成',
  stoppedTitle: (reason: string) => `剖析已停止：${reason}`,
  summary: (used: number, max: number, tables: number) =>
    `用了 ${formatNumber(used)} / ${formatNumber(max)} 条查询，剖析 ${formatNumber(tables)} 张表`,
  changes: (added: number, updated: number, removed: number) =>
    `新增 ${formatNumber(added)} 项、更新 ${formatNumber(updated)} 项${removed ? `、删除 ${formatNumber(removed)} 项` : ''}`,
  noChange: '目录没有变化',
  toastDone: (summary: string) => `数据剖析完成：${summary}`,
  toastStopped: (reason: string) => `数据剖析已停止：${reason}`,
  toastFailed: '数据剖析未能完成',
  viewReport: '查看报告',
  close: '完成',
  tableQueries: (n: number) => `${formatNumber(n)} 条查询`,
  rowsStats: (n: number) => `约 ${formatNumber(n)} 行`,
  rowsCount: (n: number) => `${formatNumber(n)} 行`,
  rowsAtLeast: (n: number) => `超过 ${formatNumber(n - 1)} 行`,
  rowsHint: { stats: '数据库统计信息中的估算值', count: '计数到整表统计行数上限为止' } as Record<string, string>,
  openTable: '打开',
  openTableLabel: (name: string) => `打开「${name}」`,
  noFindings: '没有新的发现',
  findingRelations: '关联关系',
  findingCodes: '码值候选',
  findingDate: '业务日期',
  relationVerified: '升为已验证',
  relationKept: '保持推断',
  relationConfirmed: '已人工确认，只补充覆盖率和基数',
  relationLowCoverage: '覆盖率不足，可能不是这条关系',
  relationNotUnique: '被指向列未核实唯一',
  coverage: (pct: string) => `覆盖率 ${pct}`,
  sampled: (sample: number, matched: number) => `抽样 ${formatNumber(sample)} 个键值，对上 ${formatNumber(matched)} 个`,
  cardinalityUnknown: '基数未核实',
  codeRows: (n: number) => `${formatNumber(n)} 行`,
  codesSummary: (values: number, rows: number) => `${formatNumber(values)} 个取值，共 ${formatNumber(rows)} 行`,
  codesPendingN: (n: number) => `${formatNumber(n)} 个含义待填写`,
  fillMeanings: '填写含义',
  fillMeaningsLabel: (col: string) => `填写含义：列 ${col} 的码值`,
  dateProposal: (col: string) => `提议按 ${col} 作为业务日期`,
  dateSpan: (min: string, max: string) => `取值从 ${min} 到 ${max}`,
  dateRanges: '日期列的取值范围',
  skippedTitle: (n: number) => `跳过 ${formatNumber(n)} 项`,
  moreSkipped: (n: number) => `…另有 ${formatNumber(n)} 项`,
  tableError: '这张表未能剖析',
  // ---- 无法开始（409）
  blocked: {
    disabled: '未开启数据剖析',
    inactive: '数据源已停用',
    noSchema: '还没有表结构',
    busy: '这个数据源正在剖析',
    tampered: '数据文件核对未通过',
    other: '无法开始剖析',
  } as Record<string, string>,
  blockedNext: {
    disabled: '开启后即可在这里剖析。',
    inactive: '启用后回到这里重试。',
    noSchema: '探查完成后回到这里重试。',
    busy: '同一个数据源同一时间只能进行一次剖析。',
    tampered: '请重新上传表格，或联系管理员核对数据文件。',
    other: '请稍后重试。',
  } as Record<string, string>,
  goSource: '前往数据源',
  retry: '重试',
  failedTitle: '剖析未能完成',
  backToSetup: '返回',
}

/** 整次剖析中途停下的原因 */
export const PROFILE_STOP_LABEL: Record<string, string> = {
  budget: '查询次数已用完',
  deadline: '总时长已用完',
  failed: '连续多条查询失败',
}

/** 停下之后怎么办 */
export const PROFILE_STOP_NEXT: Record<string, string> = {
  budget: '其余检查未执行。可在数据剖析设置中调高查询次数上限，或缩小剖析范围后重新剖析。',
  deadline: '其余检查未执行。可在数据剖析设置中调高总时长上限，或缩小剖析范围后重新剖析。',
  failed: '其余检查未执行。请先在数据源卡片上测试连接，确认可用后重新剖析。',
}

/** 跳过一项的原因（skipped.reason）。整句说明在 skipped.detail 里 */
export const PROFILE_SKIP_REASON_LABEL: Record<string, string> = {
  budget: '查询次数用完',
  deadline: '总时长用完',
  failed: '连续失败',
  timeout: '查询超时',
  error: '查询失败',
  rejected: '未通过安全守卫',
  masked: '列已遮罩',
  too_large: '表太大',
  view: '视图',
  unsupported: '暂不支持',
  no_data: '没有数据',
  missing: '表结构中没有',
  high_cardinality: '取值太多',
}

/** 跳过的是哪一类检查（skipped.kind） */
export const PROFILE_SKIP_KIND_LABEL: Record<string, string> = {
  relation: '关联关系',
  codes: '码值',
  date: '日期列',
  row_estimate: '行数估算',
  table: '整张表',
}

/** 码值候选：剖析只知道出现过哪些取值，含义等人填 */
export const CODES_TEXT = {
  pending: '含义待填写',
  pendingN: (n: number) => `${formatNumber(n)} 个含义待填写`,
  fill: '填写含义',
  fillLabel: (col: string) => `填写含义：列 ${col} 的码值`,
  title: (col: string) => `列 ${col} 的码值含义`,
  hint: '保存后这一项记为人工填写、已确认。暂不清楚的含义可以留空，之后再填。',
  head: { code: '码值', meaning: '含义' },
  note: '来源说明',
  placeholder: '含义待填写',
  saved: (n: number) => (n ? `已填写 ${formatNumber(n)} 个码值的含义` : '已保存'),
  save: '保存',
  cancel: '取消',
  // 「已列出全部取值」：只有勾了的码值表，SQL 检查才提醒「码值不在码值表中」；只列了一部分（比如对话里只补了一个码）不勾
  complete: '已列出全部取值',
  completeHint: '勾选表示这一列只会出现这些取值，SQL 检查会据此提醒码值表中没有的取值；只列了一部分时不要勾选',
  completeMark: '已列全',
  completeMarkHint: '已列出全部取值：SQL 检查会提醒码值表中没有的取值',
}

// ===========================================================================
// 数据目录及相关界面的补充文案（2026-10 完整性走查 R2 之后补的：清单的「只看运行中查询过的表」、剖析报告的逐条结论、
// 码值含义待填写时的确认、保存后的影响面提示、合并查询带出输入……）。新增的都放在这里，上面几组的已有写法不动。
// ===========================================================================

export const CATALOG_UI_TEXT = {
  // ---- 表清单：从顶部摘要点进来时只看运行中查询过的表，口径和摘要一致
  usedOnly: '只看运行中查询过的表',
  usedOnlyHint: '使用次数大于 0、还在表结构里的表，和顶部摘要的口径一致',
  usedOnlyClear: '取消「只看运行中查询过的表」',
  // ---- 剖析报告里的「填写含义」：填完回到报告，接着填下一列
  codesFilled: '含义已填写',
  editMeanings: '修改含义',
  editMeaningsLabel: (col: string) => `修改含义：列 ${col} 的码值`,
  fillWhileEditing: '正在编辑这张表，保存或取消后再填写码值的含义',
  // ---- 剖析报告里关系的结论：按这次的实际变化说（原本就是已验证的不说「升为」）
  relationStillVerified: (coverage: string) => `仍为已验证，覆盖率 ${coverage}`,
  relationLowered: (reason: string) => `降为推断：${reason}`,
  // ---- 确认含义还空着的码值：确认的是取值清单，含义照旧待填写
  codesPendingConfirm: (n: number) => `有 ${formatNumber(n)} 个码值的含义待填写，确认的是取值清单`,
  codesPendingAfter: '确认后这些码值仍显示「含义待填写」，之后可以再填写',
  confirmColumnTitle: (col: string, n: number) => `确认列 ${col} 的 ${formatNumber(n)} 项推断？`,
  confirmCodesTitle: (where: string) => `确认${where}？`,
  // ---- 在目录页改过之后：和助手卡片保存后同一句，说有几个已发布模板引用这张表
  savedImpact: (n: number) => `已保存。${formatNumber(n)} 个已发布模板引用这张表，下次正式运行时会提示数据目录有变化`,
  savedImpactShow: '查看模板',
  savedImpactDismiss: '收起这条提示',
  // ---- 助手「建议更新数据目录」卡片：服务端对一项的说明（新增的关联关系推算不出基数）
  patchNote: '说明',
  // ---- 合并查询的输入：连线时还没选工具、后来才选上的查询节点，一次带出来
  mergeAddConnected: (n: number) => `带出连着的 ${formatNumber(n)} 个查询节点`,
  // ---- 发布前检查里的 SQL 检查问题：级别标识和证据面板一致，并说清两个发布级别下拦不拦
  publishSqlErrorReminded: 'SQL 检查发现的错误级问题：已发布级别只提醒、不拦发布；受管级别会拦下',
  publishSqlErrorBlocked: 'SQL 检查发现的错误级问题：受管级别会拦下发布，请先修改 SQL',
  publishSqlWarning: 'SQL 检查的提醒：两个发布级别都只提醒、不拦发布',
  publishSqlErrors: (n: number) => `，其中 ${formatNumber(n)} 处是 SQL 检查发现的错误级问题（受管级别会拦下）`,
  // ---- 剖析的结构化读数：基数用数据核实过没有、挑表时跳过的空表
  cardinalityChecked: '已用数据核实',
  cardinalityByStructure: '子表一侧未核实，按表结构推断',
  cardinalityCheckedShort: '已核实',
  cardinalityCheckedHint: '基数由数据剖析用数据核实（子表一侧确有重复或确实唯一），一对多关联后重复计算的检查据此报错误',
  emptyTables: (n: number) => `挑表时跳过 ${formatNumber(n)} 张空表`,
}

// ===========================================================================
// 基于数据目录的 SQL 检查（后端 data/sqlcheck.py）
//
// 术语：
// - 「SQL 检查」：对照数据目录找出能执行、数却可能不对的写法。七条规则，界面上写中文规则名，不露规则编号。
// - 级别：错误（依据已核实，结果很可能有误）、提醒（依据已确认，结果可能有误）、提示（依据只是推断，仅供参考）。
//   一对多关联后重复计算：只有一对多人工确认过、或数据剖析用数据核实过时才是错误；只凭外键推出的是提醒。
//   颜色走语义令牌：错误 --st-failed，提醒 --st-waiting，提示 --accent。
// ===========================================================================

export const SQL_CHECK_RULE_LABEL: Record<SqlCheckCode, string> = {
  fanout_sum: '一对多关联后重复计算',
  stock_summed: '存量跨期加总',
  join_unconfirmed: '关联条件未经确认',
  ratio_aggregated: '比率直接加总或平均',
  missing_valid_filter: '未筛选有效记录',
  unknown_code: '码值不在码值表中',
  wrong_date_column: '未按业务日期统计',
}

export const SQL_CHECK_RULE_HINT: Record<SqlCheckCode, string> = {
  fanout_sum: '沿一对多方向关联后，对「一」那一侧的度量求和或计数，会按明细行数重复计算',
  stock_summed: '对存量列求和却没有按业务日期分组，不同日期的存量被加在一起',
  join_unconfirmed: '关联条件对不上数据目录中有确证的关系，可能连错了列',
  ratio_aggregated: '对比率列直接求和或求平均，得不到正确的比率',
  missing_valid_filter: '表定义了有效记录条件，查询没有按它筛选',
  unknown_code: '码值表已列出全部取值，按码值筛选时却用了其中没有的值',
  wrong_date_column: '表定义了业务日期，查询却按另一个时间列分组或筛选',
}

export const SQL_CHECK_LEVEL_LABEL: Record<SqlCheckLevel, string> = {
  error: '错误',
  warning: '提醒',
  info: '提示',
}

export const SQL_CHECK_LEVEL_HINT: Record<SqlCheckLevel, string> = {
  error: '依据已核实，结果很可能有误，需要修改',
  warning: '依据已确认，结果可能有误，请核对',
  info: '依据尚未确认（推断），仅供参考',
}

export const SQL_CHECK_TEXT = {
  title: 'SQL 检查',
  /** 规则编号不认识（服务端新加了规则）时的叫法 */
  unknownRule: 'SQL 检查',
  listLabel: (n: number) => `SQL 检查发现 ${formatNumber(n)} 处问题`,
  where: '涉及',
  excerpt: 'SQL 片段',
  catalogHint: '检查依据来自数据目录。依据有误时，请在数据目录中修正对应项。',
  // ---- 运行时间线：指标所依据的查询没通过 SQL 检查（日志 metric_sql_check）
  metricTitle: (name: string, n = 1) =>
    (n > 1 ? `指标「${name}」所依据的查询有 ${formatNumber(n)} 处未通过 SQL 检查` : `指标「${name}」所依据的查询未通过 SQL 检查`),
  metricTitleAnon: '指标所依据的查询未通过 SQL 检查',
  metricNext: '指标照常计算，但结果不可靠，出具按缺口降档。请在证据面板的查询步骤中查看问题，修改 SQL 后重新运行；'
    + '检查依据有误时，请在数据目录中修正对应项',
  // ---- 助手：搭图时查出的问题
  counts: (errors: number, warnings: number, infos: number) =>
    [errors ? `${formatNumber(errors)} 处错误` : '', warnings ? `${formatNumber(warnings)} 处提醒` : '',
      infos ? `${formatNumber(infos)} 处提示` : ''].filter(Boolean).join('、'),
  assistantTitle: (counts: string) => `SQL 检查：${counts}`,
  assistantNext: '请打开对应节点，在「参数」中修改 SQL；检查依据有误时，请在数据目录中修正对应项',
  assistantNextChat: '可在画布中打开此工作流，修改对应节点「参数」中的 SQL；检查依据有误时，请在数据目录中修正对应项',
  assistantInfoNext: '这些检查的依据尚未确认，仅供参考。在数据目录中确认相关项后，检查结论更可靠',
}

// ===========================================================================
// 证据面板、运行时间线里的 SQL 检查和数据目录版本，助手「参考了哪些表」（数据目录完整性修复 F2a）
//
// - 证据面板的查询步骤写这次查询对照的是哪一版数据目录（表结构快照里冻结的那份）：「数据目录：入园记录 第 3 版」。
// - 合并查询的每个输入带着它自己的 SQL 检查结果。
// - 运行时间线：同一条查询的问题只列一行，写明受影响的指标；「打开设置」落到来源查询节点的 SQL 上。
// - 助手「参考了哪些表」：带字段的表和只提供了表名的表分开数。
// ===========================================================================

export const RUN_SQL_CHECK_TEXT = {
  /** 证据面板查询步骤：SQL 检查对照的数据目录版本 */
  catalogLine: (items: { name: string; version?: number | null }[]) =>
    `数据目录：${items.map((i) => (i.version != null ? `${i.name} 第 ${formatNumber(i.version)} 版` : i.name)).join('、')}`,
  catalogHint: '这次查询时冻结的数据目录版本，SQL 检查对照的就是这一版',
  /** 合并查询的输入：这个输入查询自己的 SQL 检查 */
  mergeInputChecks: (alias: string) => `输入 ${alias} 的 SQL 检查`,
  /** 运行时间线：一条查询的问题影响了几个指标 */
  metricsTitle: (names: string[], n = 1) => {
    const who = names.length > 3
      ? `指标${names.slice(0, 3).map((x) => `「${x}」`).join('')}等 ${formatNumber(names.length)} 个`
      : `指标${names.map((x) => `「${x}」`).join('')}`
    return n > 1 ? `${who}所依据的查询有 ${formatNumber(n)} 处未通过 SQL 检查` : `${who}所依据的查询未通过 SQL 检查`
  },
  /** 运行时间线：一次查询的 SQL 检查结果（查询那一行展开后） */
  toolChecks: 'SQL 检查',
  /** 「打开设置」落到来源查询节点的 SQL 上 */
  openSql: (label?: string) => (label ? `打开「${label}」的 SQL` : '打开来源查询的 SQL'),
  metricNext: '指标照常计算，但结果不可靠，出具按缺口降档。请修改来源查询的 SQL 后重新运行；检查依据有误时，请在数据目录中修正对应项',
  /** 出具横幅、报告页：SQL 检查导致的降档单列一块 */
  issuanceTitle: (tier: string) => (tier === 'degraded' ? '降档原因：所依据的查询未通过 SQL 检查' : '所依据的查询未通过 SQL 检查'),
  issuanceQuery: '所依据的查询',
  issuanceAffected: (refs: string) => `受影响：${refs}`,
  issuanceNext: '数字的出处都对得上，但算出这些数的查询本身有问题，结果不可靠。请修改来源查询的 SQL 后重新运行；检查依据有误时，请在数据目录中修正对应项',
  /** 其余缺口的标题（SQL 检查的那几条单列之后） */
  otherGaps: '校验未全部完成：',
}

export const COPILOT_CONTEXT_TEXT = {
  /** 标题：另有只提供了表名的表时，写明这个数只算带着字段的表 */
  withFields: (n: number) => `参考了 ${formatNumber(n)} 张表的字段`,
  namesOnly: (n: number) => `另有 ${formatNumber(n)} 张表只提供了表名`,
  notPicked: '未能按需求挑选',
  /** 没有数据目录的大库：只有一个数据源时说「这个数据源」，几个时点名 */
  noCatalog: (who: string | null) => `${who ?? '这个数据源'}还没有数据目录，起草后挑表和 SQL 检查会更准`,
  toCatalog: '去数据目录起草',
}

// ---------------------------------------------------------------------------
// 下面几块是复核套件各工作包的文案位：名字先占好，各包只往自己那块里加，不去改同一处。
// 还没做出来的功能，界面上统一写 PENDING_TEXT 那一句
// ---------------------------------------------------------------------------

/** 尚未完成的功能在界面上的统一说法 */
const PENDING_TEXT = '功能开发中'

/**
 * 定时任务：导航、命令面板、定时任务页（pages/schedules）和记录页里的「定时」标记。
 *
 * 触发、跳过、补发各有固定说法：「触发」是到点（或手动）的那一下，「发起」是由它开始一次运行；跳过分「上次未结束」
 * 和「错过时间」两种，不笼统写「失败」。时间都按定时任务自己的时区写，悬停另给本机时间
 */
export const SCHEDULE_TEXT = {
  title: '定时任务',
  hint: '按设定的周期自动发起工作流运行',

  // ---- 列表
  create: '新建定时任务',
  emptyTitle: '还没有定时任务',
  emptyBody: '定时任务按设定的周期自动发起工作流运行，适合日报、周报这类定期产出的结果。选择工作流、设定频率和输入即可创建，每次触发都记入触发历史。',
  unsupported: '服务端暂不支持定时任务',
  unsupportedHint: '当前服务端没有定时任务接口，请升级服务端后再使用',
  listLabel: '定时任务列表',
  formalChip: '正式运行',
  pausedChip: '已暂停',
  nextRun: '下次运行',
  lastFire: '上次触发',
  neverFired: '尚未触发',
  /** 暂停中的没有下一次 */
  nextPaused: '暂停中，不会触发',
  /** 启用着却算不出下一次（如 2 月 30 日） */
  nextNone: '往后不会再触发',
  /** 计划时间已过、还没有这一次的触发记录：调度可能没开，或者正在发起 */
  nextOverdue: '已到触发时间',
  /** 悬停提示：按哪个时区、本机是几点 */
  zoneTitle: (when: string, tz: string) => `${when}（${tz}）`,
  localTitle: (when: string) => `本机时间 ${when}`,
  enable: '启用',
  enableLabel: (name: string) => `启用定时任务「${name}」`,
  enabledOk: (name: string) => `已启用「${name}」`,
  pausedOk: (name: string) => `已暂停「${name}」，到点不再发起运行`,
  runNow: '立即运行',
  runNowLabel: (name: string) => `立即运行定时任务「${name}」`,
  edit: '编辑',
  editLabel: (name: string) => `编辑定时任务「${name}」`,
  deleteLabel: (name: string) => `删除定时任务「${name}」`,
  deleteTitle: (name: string) => `删除定时任务「${name}」？`,
  deleteConsequences: ['不再按计划发起运行，删除后不可恢复', '已发起的运行及其记录不受影响'],
  deleteConfirm: '删除定时任务',
  deleted: (name: string) => `已删除定时任务「${name}」`,
  /** 启用开关遇到版本不符（409）：服务端那句是写给表单保存的，开关用这句，原话放进详情 */
  toggleConflict: (name: string) => `「${name}」已在别处被修改，已重新载入最新状态，请确认后再切换`,

  // ---- 立即运行的结果
  runStarted: '已发起运行',
  runSkipped: '上一次运行尚未结束，本次已按重叠策略跳过',
  runFailed: (reason: string | null) => (reason ? `未能发起运行：${reason}` : '未能发起运行'),
  openRun: '查看运行',

  // ---- 触发
  fireKind: { scheduled: '定时', manual: '手动', misfire: '补发' } as Record<import('../types').ScheduleFireKind, string>,
  fireKindHint: {
    scheduled: '到点按计划触发',
    manual: '手动「立即运行」',
    misfire: '错过触发时间后，在补发宽限内恢复时补发的一次',
  } as Record<import('../types').ScheduleFireKind, string>,
  /**
   * 错过、没有补发的（服务端记 kind=misfire、status=skipped_misfire）：类型按「定时」写。没有补发的原因有两种（超出
   * 补发宽限；宽限内恢复时积压的触发不补，只发起最近一次），这里不替服务端认定是哪一种，本条记了原因时指向它
   */
  fireKindMissedHint: (hasReason: boolean) => `按计划应在此时触发，错过后没有补发${hasReason ? '，原因见本条记录' : ''}`,
  fireStatus: {
    started: '已发起',
    skipped_overlap: '上次未结束已跳过',
    skipped_misfire: '错过已跳过',
    failed_to_start: '发起失败',
  } as Record<import('../types').ScheduleFireStatus, string>,
  /**
   * 结局的一般说明，本条没有记原因时悬停用：只说这种结局是什么，不指向不存在的原因。错过已跳过的两种原因都适用，
   * 发起失败也可能是手动立即运行，不写「到点」
   */
  fireStatusHint: {
    started: '已发起运行',
    skipped_overlap: '上一次运行还没有结束，按重叠策略跳过了这一次',
    skipped_misfire: '服务停机等原因错过了触发时间，这一次没有发起',
    failed_to_start: '未能发起运行，服务端未提供原因',
  } as Record<import('../types').ScheduleFireStatus, string>,
  event: { finished: '运行结束', failed: '运行失败', awaiting_approval: '等待审批' } as Record<import('../types').ScheduleEvent, string>,
  noticeStatus: { ok: '已发送', failed: '发送失败' } as Record<'ok' | 'failed', string>,

  // ---- 详情
  tabs: { settings: '设置', history: '触发历史' } as Record<'settings' | 'history', string>,
  back: '返回列表',
  notFound: '定时任务不存在或已被删除',
  notFoundHint: '请返回列表查看现有的定时任务',

  // ---- 表单：基本
  newTitle: '新建定时任务',
  name: '名称',
  namePlaceholder: '例如：门店周报',
  workflow: '工作流',
  workflowPlaceholder: '搜索并选择工作流',
  workflowNone: '没有匹配的工作流',
  workflowEmpty: '还没有工作流，请先在编排页创建并保存。',
  toStudio: '去编排',
  /** 定时任务指向的工作流已经不在列表里 */
  workflowGone: (name: string) => `工作流「${name}」已不存在，请重新选择`,
  workflowDraft: '草稿',
  workflowVersion: (status: string, v: number) => `${status} v${v}`,

  // ---- 表单：频率
  freq: '频率',
  preset: { daily: '每天', weekly: '每周', monthly: '每月', hourly: '每隔几小时', custom: '自定义 cron' } as Record<
    'daily' | 'weekly' | 'monthly' | 'hourly' | 'custom', string>,
  time: '时间',
  weekdays: '星期',
  weekdayShort: ['周日', '周一', '周二', '周三', '周四', '周五', '周六'] as readonly string[],
  monthday: '日期',
  monthdayOption: (d: number) => `${d} 日`,
  monthdayHint: '没有这一天的月份不触发',
  everyHours: '间隔',
  everyHoursOption: (n: number) => `每 ${n} 小时`,
  minute: '第几分',
  hourlyHint: (n: number, m: number) => `从 0 点起每 ${n} 小时一次，每次在第 ${m} 分`,
  cron: 'cron 表达式',
  cronHint: '五段，依次为分、时、日、月、星期，例如 0 9 * * 1-5 表示工作日 09:00',
  timezone: '时区',
  timezoneHint: '触发时间按这个时区计算',
  timezoneLocal: '本机',
  timezoneLabel: {
    'Asia/Shanghai': '北京', 'Asia/Hong_Kong': '香港', 'Asia/Taipei': '台北', 'Asia/Singapore': '新加坡',
    'Asia/Tokyo': '东京', 'Asia/Seoul': '首尔', 'Asia/Kolkata': '印度', 'Asia/Dubai': '迪拜', 'Europe/London': '伦敦',
    'Europe/Paris': '巴黎', 'Europe/Berlin': '柏林', 'America/New_York': '纽约', 'America/Chicago': '芝加哥',
    'America/Los_Angeles': '洛杉矶', UTC: '协调世界时',
  } as Record<string, string>,

  // ---- 表单：预览
  preview: '接下来 5 次触发',
  previewLoading: '正在计算…',
  previewIdle: '填好频率后在此预览触发时间',
  previewNone: '按这个频率往后不会触发',
  previewFailed: '无法预览触发时间',
  previewZone: (tz: string) => `按 ${tz} 计算`,

  // ---- 表单：运行类别
  runClass: '运行类别',
  formal: '正式运行',
  formalHint: '从已发布版本发起，结果出具、可审计。工作流须已发布',
  formalVersion: (v: number) => `每次按当时的发布版本发起，当前为 v${v}`,
  exploratory: '探索运行',
  exploratoryHint: '使用工作流当前保存的内容，结果不出具',
  /** 已保存的定时任务、这次没动工作流和运行类别，工作流后来撤了发布：保存照常，到点会发起失败 */
  notPublished: '这个工作流目前没有发布版本：到点时无法发起正式运行，会记为发起失败。请先发布，或改用探索运行',

  // ---- 表单：输入
  inputs: '输入',
  inputsHint: '可填固定值或模板，模板中的变量按触发时间计算；留空的字段按工作流的默认值',
  inputsPick: '选择工作流后在此填写输入',
  inputsNone: '这个工作流没有输入字段',
  inputsLoading: '正在读取输入字段…',
  inputsFailed: '无法读取输入字段',
  inputsFromVersion: (v: number) => `字段取自已发布的 v${v}`,
  inputsFromDraft: '字段取自工作流当前保存的内容',
  required: '必填',
  /** 已保存的定时任务、这次没动输入：保存照常，留空到点会发起失败 */
  requiredNoDefault: '必填且没有默认值：留空时到点会发起失败',
  /** 新建、或者这次改了工作流、运行类别、输入：服务端保存时就会拒 */
  requiredFill: '必填且没有默认值，请填写后再保存',
  defaultValue: (v: string) => `默认：${v}`,
  variables: '插入变量',
  variablesClose: '收起变量',
  variablesLabel: (field: string) => `插入到「${field}」的变量`,
  variablesEmpty: '服务端没有提供可用的变量',
  variableTitle: (name: string, example: string) => (example ? `${name}，例如 ${example}` : name),
  rendered: (v: string) => `下次触发时为：${v}`,
  renderedEmpty: '下次触发时为空',
  staleField: '工作流中已没有这个字段',
  /** 画布上有、正式运行用的已发布版本里没有 */
  staleNotInVersion: (v: number) => `已发布的 v${v} 中没有这个字段`,
  /** 接在上面两句后面：交上去会被拒 */
  staleRemove: '保存前请移除',
  removeField: '移除',

  // ---- 表单：重叠与补发
  overlap: '重叠与补发',
  overlapText: '上一次运行还没有结束时，跳过这一次，记入触发历史',
  misfire: '错过触发时间',
  misfireHint: '服务停机等原因错过触发时间时，在宽限内恢复则补发一次，超过则跳过',
  misfireOption: (span: string) => `${span}内补发`,
  span: { day: (n: number) => `${n} 天`, hour: (n: number) => `${n} 小时`, minute: (n: number) => `${n} 分钟`, second: (n: number) => `${n} 秒` },
  enabledOnCreate: '创建后立即启用',

  // ---- 表单：Webhook
  webhook: 'Webhook 通知',
  webhookOn: '运行有结果时发送 Webhook 通知',
  webhookFormat: '格式',
  format: { generic: '通用', feishu: '飞书', wecom: '企业微信', dingtalk: '钉钉' } as Record<
    import('../types').ScheduleWebhookFormat, string>,
  formatHint: {
    generic: '以 JSON 发送到指定地址',
    feishu: '飞书群机器人的 Webhook 地址',
    wecom: '企业微信群机器人的 Webhook 地址',
    dingtalk: '钉钉群机器人的 Webhook 地址',
  } as Record<import('../types').ScheduleWebhookFormat, string>,
  urlPlaceholder: {
    generic: 'https://example.com/hooks/agentlab',
    feishu: 'https://open.feishu.cn/open-apis/bot/v2/hook/…',
    wecom: 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…',
    dingtalk: 'https://oapi.dingtalk.com/robot/send?access_token=…',
  } as Record<import('../types').ScheduleWebhookFormat, string>,
  url: '地址',
  urlSaved: '已保存的地址',
  urlSavedHint: '完整地址已加密保存，只显示主机和末 4 位',
  urlReplace: '替换地址',
  urlKeep: '保留原地址',
  webhookRemove: '删除 Webhook',
  webhookRemoving: '保存后将删除这个 Webhook',
  webhookUndoRemove: '撤销删除',
  secret: '签名密钥',
  secretHint: {
    generic: '可选。填写后每条消息带签名，接收端可据此校验来源',
    feishu: '可选。机器人开启了签名校验时填写',
    wecom: '企业微信群机器人不使用签名，留空即可',
    dingtalk: '可选。机器人开启了加签时填写',
  } as Record<import('../types').ScheduleWebhookFormat, string>,
  secretSet: '已设置',
  secretReplace: '替换',
  secretClear: '清除',
  secretClearing: '保存后将清除签名密钥',
  secretUndo: '撤销',
  secretNew: '新的签名密钥',
  events: '通知哪些事件',
  test: '发送测试消息',
  testing: '正在发送…',
  testNeedSave: '保存后才能发送测试消息',
  testDirty: '测试使用已保存的设置，请先保存修改',
  testOk: '测试消息已送达',
  testFailed: '测试消息发送失败',
  /** 结局后面接状态码和说明（两样都可能没有） */
  testResult: (head: string, status: number | null, detail: string) =>
    `${head}${status != null ? `（HTTP ${status}）` : ''}${detail ? `：${detail}` : ''}`,

  // ---- 通知里的链接：运行、对比页都按这个地址拼（设置 notify 组，所有定时任务共用）
  linkBase: '通知中的链接',
  linkBaseValue: (url: string) => `按 ${url} 拼接`,
  linkBaseUnset: '未设置服务访问地址，通知中不附带运行和对比页链接',
  linkBaseEdit: '修改',
  linkBaseSet: '设置',
  linkBaseLabel: '服务访问地址',
  linkBaseHint: '收件人打开运行、对比页链接用的地址，例如 https://agentlab.example.com。所有定时任务共用',
  linkBasePlaceholder: 'https://agentlab.example.com',
  linkBaseInvalid: '请输入以 http:// 或 https:// 开头的地址',
  linkBaseSaved: '已保存服务访问地址',
  linkBaseCleared: '已清除服务访问地址',
  linkBaseFailed: '无法读取服务访问地址',

  // ---- 表单：保存
  save: '保存',
  createSubmit: '创建',
  discard: '撤销修改',
  retry: '重试',
  saving: '正在保存…',
  cancel: '取消',
  saved: '已保存',
  created: (name: string) => `已创建定时任务「${name}」`,
  missing: (items: string) => `还需处理：${items}`,
  previewBlocks: '请先修正频率、时区或输入中标出的问题',
  saveFailed: '保存失败',
  conflict: '保存失败：这个定时任务已在别处被修改，请重新载入后再修改',
  reload: '重新载入',
  reloadTitle: '重新载入最新设置？',
  reloadConsequences: ['页面上未保存的修改将丢失'],
  reloadConfirm: '放弃修改并重新载入',
  leaveTitle: '定时任务有未保存的修改',
  leaveConsequences: ['离开后，这些修改将丢失'],
  leaveConfirm: '放弃修改',

  // ---- 表单的问题（写在对应字段下）
  problem: {
    name: '请填写名称',
    workflow: '请选择工作流',
    timezone: '请选择时区',
    cronEmpty: '请填写 cron 表达式',
    cronFields: 'cron 表达式应为五段，以空格分隔',
    minute: '请填写 0–59 之间的分钟',
    time: '请填写时间',
    weekdays: '至少选择一天',
    url: '请填写 Webhook 地址',
    urlInvalid: '请输入以 http:// 或 https:// 开头的地址',
    secret: '请填写新的签名密钥',
    events: '至少选择一个事件',
    notPublished: '这个工作流还没有发布版本，正式运行需要已发布的版本。请先发布，或改用探索运行',
    inputsMissing: (names: string) => `必填输入「${names}」还没有填写`,
    inputsUnknown: (names: string) => `输入「${names}」对不上工作流的输入字段，请移除`,
  },
  /** 「还需处理」里各项的叫法 */
  problemField: {
    name: '名称', workflow: '工作流', freq: '频率', timezone: '时区', runClass: '运行类别', inputs: '输入', url: 'Webhook 地址',
    secret: '签名密钥', events: '通知事件',
  } as Record<'name' | 'workflow' | 'freq' | 'timezone' | 'runClass' | 'inputs' | 'url' | 'secret' | 'events', string>,

  // ---- 相对时间（将来）
  until: {
    soon: '不到 1 分钟后',
    minutes: (n: number) => `${n} 分钟后`,
    hours: (n: number) => `${n} 小时后`,
    days: (n: number) => `${n} 天后`,
  },

  // ---- 触发历史
  history: '触发历史',
  historyHint: '每次触发都记在这里，包括跳过和发起失败的',
  historyEmpty: '还没有触发记录',
  historyEmptyHint: '到点后的每次触发都会记在这里，包括跳过和发起失败的',
  historyRefresh: '刷新',
  fireAt: '计划时间',
  fireAtManual: '触发时间',
  reason: '原因',
  noRun: '未发起运行',
  notified: '通知',
  loadMore: '加载更多',
  allLoaded: '已全部加载',
  shown: (n: number) => `已显示 ${n} 条`,

  // ---- 触发历史里的对比：运行成功结束后，服务端自动和同一定时任务上一次成功的运行比一次
  /**
   * 和运行详情「更多」菜单里的入口是同一件事（服务端用同一个函数挑基准，两处比的是同一对运行），基准就是紧挨着的上一次
   * 时用同一个叫法。RUN_COMPARE_TEXT 定义在本对象之后，模块初始化时还取不到，写成 getter 用到时再取
   */
  get compareLink() { return RUN_COMPARE_TEXT.menu },
  /**
   * 基准不是紧挨着的上一次（上一次失败了、取消了，比的是再往前成功的那一次）时链接上看得见的叫法：触屏没有悬停，
   * 键盘聚焦时也不显示 title，只在悬停里说明会让人以为比的就是上一次。运行详情「更多」里的菜单项点之前还不知道基准
   * 是哪一次，这时仍叫「与上一次对比」，两处叫法不同；进了对比页都写「默认基准：同一定时任务上一次成功的运行」
   */
  compareLinkLastOk: '与上一次成功的运行对比',
  /**
   * 悬停写各类指标的个数，和 Webhook 通知里那一行同一个写法（服务端 scheduler/auto_compare.py 的 summary_text）：
   * 变化三分法的三类总写上，0 个也写；其余各类有才写，次序同对比页；两边都没有指标时不写一串 0 个。只有个数，
   * 不写数值。各类的叫法用对比页那一套（RUN_COMPARE_TEXT.classLabel）。base 是基准的短编号，基准不是紧挨着的
   * 上一次（上一次失败了、取消了）时给，写明比的是哪一次，同通知
   */
  compareTitle: (summary: Partial<Record<string, number>>, base?: string | null) => {
    const n = (key: string) => (typeof summary[key] === 'number' ? summary[key] as number : 0)
    const core = ['data_only', 'caliber_and_data', 'sql_changed']
    const rest = ['params_only', 'added', 'removed', 'incomparable', 'unchanged']
    const head = base ? `与上一次成功的运行（${base}）相比` : '与上一次相比'
    if (![...core, ...rest].some((key) => n(key) > 0)) return `${head}：两次运行都没有指标`
    const parts = [...core, ...rest.filter((key) => n(key) > 0)]
      .map((key) => `${RUN_COMPARE_TEXT.classLabel[key] ?? key} ${n(key)} 个`)
    return `${head}：${parts.join('、')}`
  },
  /**
   * 链接旁看得见的简短计数：触屏没有悬停，键盘聚焦时也不显示 title，只靠悬停看不出变化的规模。有变化的几类（只数据变、
   * 口径与数据都变、SQL 结构变、只参数变、新增、移除）合起来写一个数；都没变化时说有几个无法比较，或者都未变；两边都
   * 没有指标时不写（悬停里有）
   */
  compareCount: (summary: Partial<Record<string, number>>) => {
    const n = (key: string) => (typeof summary[key] === 'number' ? summary[key] as number : 0)
    const changed = ['data_only', 'caliber_and_data', 'sql_changed', 'params_only', 'added', 'removed']
      .reduce((sum, key) => sum + n(key), 0)
    if (changed) return `${changed} 个指标有变化`
    if (n('incomparable')) return `${n('incomparable')} 个指标无法比较`
    return n('unchanged') ? '指标都未变' : null
  },

  // ---- 运行详情页头的来源
  /**
   * 到点发起（以及没有署名的立即运行）的运行记在「定时任务：名字」名下，前缀和服务端 scheduler/service.py 的
   * actor_label 一致。页头据此不再写「定时任务：X 发起」，换成下面那条链接
   */
  actorPrefix: '定时任务：',
  runSource: (name?: string | null) => (name ? `由定时任务「${name}」发起` : '由定时任务发起'),
  /** 有人署名点「立即运行」发起的：署名照写（runStarter），后面接这一句 */
  runSourceManual: (name?: string | null) => (name ? `来自定时任务「${name}」` : '来自定时任务'),
  runStarter: (who: string) => `${who} 发起`,
  /** 悬停写全名（页头窄时链接会截断）和点开去哪 */
  runSourceTitle: (name?: string | null) =>
    (name ? `定时任务「${name}」：查看设置和触发历史` : '查看这个定时任务的设置和触发历史'),
  /**
   * 定时任务已经删除（取回的定时任务列表里没有它）：不再链过去——只会落到「不存在」页，页上的返回又回到定时任务列表。
   * 和同一行的「工作流已删除」一样写成纯文本
   */
  runSourceGone: (name?: string | null) => (name ? `由定时任务「${name}」发起（已删除）` : '由已删除的定时任务发起'),
  runSourceManualGone: (name?: string | null) => (name ? `来自定时任务「${name}」（已删除）` : '来自已删除的定时任务'),
  runSourceGoneTitle: '这个定时任务已删除，它发起的运行记录仍然保留',

  // ---- 记录页的定时标记
  runTag: '定时',
  runTagTitle: (name?: string | null) => (name ? `由定时任务「${name}」发起，点击查看` : '由定时任务发起，点击查看'),
  /** 读屏念的来源（整行是打开运行的按钮，标记本身点不到，至少在名字里听到） */
  runTagSr: (name?: string | null) => (name ? `由定时任务「${name}」发起` : '由定时任务发起'),
}

/**
 * 运行对比（/runs/compare，pages/runs/ComparePage.tsx）：两次运行的指标变化拆成数据、口径、模板与 SQL 三类说清楚。
 * 配方前后对照另有 COMPARE_TEXT，别混用。分类、拆分原因、说明（reasons、decomposition_note、notes）是服务端写的话，
 * 原样显示，这里不改写
 */
export const RUN_COMPARE_TEXT = {
  title: '运行对比',
  hint: '把两次运行的指标变化拆成数据、口径、模板与 SQL 三类',
  // 入口：运行详情的「更多」菜单
  menu: '与上一次对比',
  menuSealedOnly: '运行结束并封存后才能对比',
  /** 没有 workflow_id 的运行：画布、问数据里没保存就运行的，或者工作流删掉之后被置空的。服务端只比同一个已保存的工作流 */
  menuNoWorkflow: '工作流未保存或已删除，无法对比',
  menuFinding: '正在查找上一次成功的运行…',
  pickManually: '手动选择基准',
  retry: '重试',
  // 顶部的两次运行
  base: '基准',
  target: '本次',
  swap: '互换',
  swapHint: '互换基准与本次',
  openRun: (name: string) => `查看「${name}」这次运行的详情`,
  scheduled: '定时任务发起',
  unsealed: '未封存',
  unsealedHint: '这次运行没有封存清单，不能参与对比',
  runFailed: '无法读取这次运行',
  runMissing: '运行记录不存在',
  noBase: '未选择',
  /**
   * 默认基准是怎么选的，照服务端（api/compare.py 的 pick_base）的规则写：只挑比本次先发起、成功结束并封存的运行。
   * 上一次失败、取消了时基准是再往前成功的那一次，所以不能写成「上一次运行」
   */
  reason: {
    same_schedule: '默认基准：同一定时任务上一次成功的运行',
    same_workflow: '默认基准：同一工作流、同一运行类别上一次成功的运行',
  } as Record<string, string>,
  pickBase: '更换基准',
  /** 默认基准只取成功结束的运行（pick_base），手动选不限：失败、取消的也能选，下拉和基准卡上写明它的状态 */
  pickHint: '同一工作流最近的运行，未封存的不能作为基准；默认基准只取成功结束的运行',
  pickLoading: '正在读取同一工作流的运行…',
  pickFailed: '无法读取同一工作流的运行',
  pickNone: '同一工作流没有其他运行',
  /** 本次的运行记录不存在：不知道是哪个工作流，列不出候选，重试也是一样 */
  pickTargetGone: '本次运行的记录不存在，无法列出同一工作流的运行',
  pickUnsaved: '工作流未保存或已删除，没有可对比的运行',
  pickPlaceholder: '选择一次运行作为基准',
  /**
   * 下拉里的一次运行：时间 · 类别和版本 · 短 id，没有成功结束的写上状态（失败、已取消），没封存的另标。默认基准跳过的
   * 正是这几种，手动挑时也得看得出哪次没成功
   */
  pickOption: (time: string, cls: string, id: string, sealed: boolean, status?: string | null) =>
    `${time} · ${cls} · ${id}${status ? ` · ${status}` : ''}${sealed ? '' : '（未封存）'}`,
  /** 当前的基准读不到时下拉里的那一项（短 id）：如实显示选的是哪一次 */
  pickGone: (id: string, missing: boolean) => `${id}（${missing ? '记录不存在' : '无法读取'}）`,
  // 页面状态
  noTarget: '未指定要对比的运行',
  noTargetHint: '在运行详情的「更多」菜单中选择「与上一次对比」，即可对照本次与上一次成功的运行',
  toRuns: '前往记录',
  finding: '正在查找基准运行…',
  loading: '正在对比两次运行…',
  // 对比失败：标题说是哪一种，服务端的原因原样跟在后面，最后一句下一步只写界面上真有的入口，服务端已经写了的不再重复
  errSealed: '无法对比：有运行未封存或封存核对不一致',
  errUnsealed: '无法对比：有运行尚未结束并封存',
  errSealBroken: '无法对比：封存核对不一致',
  /** 409 时不知道（或者知道不是）本次的问题：服务端的原因里写着是哪一次 */
  errSealedHint: '若是基准运行的问题，可在上方更换基准',
  errMismatch: '无法对比这两次运行',
  errMismatchHint: '可在上方选择同一工作流的另一次运行作为基准',
  /** 本次没有 workflow_id：哪一次都比不了，不给下一步 */
  errUnsaved: '无法对比：本次运行的工作流未保存或已删除',
  errMissing: '运行记录不存在',
  errMissingHint: '链接可能已失效，请从记录页重新打开',
  /** 本次的记录读得到、对比却回 404：缺的是基准 */
  errBaseMissingHint: '可在上方选择另一次运行作为基准',
  /**
   * 找不到默认基准：标题只说没有默认基准，原因（服务端的 NO_BASE_TEXT：没有找到上一次成功的运行、要什么条件）跟在后面。
   * 不能写成「没有可作为基准的运行」：失败、取消但封存了的运行不当默认基准，却照样能在上方手动选
   */
  errNoBase: '没有默认基准',
  errNoBaseHint: '可在上方选择同一工作流的其他运行作为基准',
  /** 下拉里一次能选的都没有（只有本次、或者都没封存） */
  errNoBaseNone: '同一工作流还没有其他已封存的运行',
  /** 409、422、404 时下拉里除了本次和当前的基准，没有别的已封存的运行：换基准这条路走不通，照实说 */
  errNoOtherBase: '同一工作流没有其他可换作基准的已封存运行',
  viewBase: '查看基准运行',
  viewTarget: '查看本次运行',
  // 摘要
  summary: '变化摘要',
  summaryHint: '点击一类只看这一类的指标',
  classLabel: {
    data_only: '只数据变',
    caliber_and_data: '口径与数据都变',
    sql_changed: 'SQL 结构变',
    params_only: '只参数变',
    added: '新增',
    removed: '移除',
    incomparable: '无法比较',
    unchanged: '未变',
  } as Record<string, string>,
  // 照服务端（engine/compare.py）的分类口径写：口径变了含小数位、单位、显示格式，SQL 结构变含上游节点改配置
  classHint: {
    data_only: '口径与 SQL 都没有改动，变化来自数据',
    caliber_and_data: '口径（计算式、口径版本、小数位、单位或显示格式）有改动，数据可能也有变化；能严格拆分的分别给出口径效应和数据效应',
    sql_changed: '取数的 SQL 或上游节点的配置有改动，无法在口径与数据之间拆分',
    params_only: '所依据的查询 SQL 结构相同，只有参数（如日期）或运行输入不同',
    added: '本次有、基准没有的指标',
    removed: '基准有、本次没有的指标',
    incomparable: '缺少比较所需的信息，无法判断变化来自哪里',
    unchanged: '两次的值相同，口径与 SQL 也没有改动',
  } as Record<string, string>,
  classOther: '其他',
  filterOn: (label: string, n: number) => `只显示「${label}」的 ${formatNumber(n)} 个指标`,
  showAll: '显示全部',
  // 指标
  metrics: '指标',
  metricsEmpty: '两次运行的口径卡上都没有指标',
  count: (n: number) => `${formatNumber(n)} 个`,
  /** 查询按「条」数，和「第 N 条」一致 */
  queryCount: (n: number) => `${formatNumber(n)} 条`,
  colMetric: '指标',
  colBase: '基准',
  colTarget: '本次',
  colDelta: '变化',
  colClass: '分类',
  colSplit: '拆分或原因',
  legend: '口径效应按本次的数据计算，是只换口径带来的变化；数据效应按基准的口径计算，是只换数据带来的变化。两者之和等于总变化',
  caliber: '口径',
  data: '数据',
  splitSum: (caliber: string, data: string, total: string) =>
    `口径效应 ${caliber}，数据效应 ${data}，两者之和等于总变化 ${total}`,
  noChange: '无变化',
  expand: (name: string) => `展开「${name}」的输入对比与维度归因`,
  collapse: (name: string) => `收起「${name}」的明细`,
  reasons: '变化说明',
  inputs: '输入对比',
  inputsNone: '口径卡没有记录这个指标的输入',
  /** 取自设置了遮罩的列：两边的值都不显示 */
  masked: '已遮罩',
  colInput: '输入',
  attribution: '维度归因',
  attrBy: (measure: string) => `按「${measure}」的变化，从大到小`,
  attrTop: (n: number) => `前 ${formatNumber(n)} 项`,
  attrMore: (n: number) => `另有 ${formatNumber(n)} 项未列出`,
  attrTotal: (value: string) => `合计变化 ${value}`,
  attrNone: '没有可按维度归因的明细',
  /**
   * 服务端只对只数据变、只参数变、口径与数据都变的指标做维度归因（两边的行说的是同一回事）。别的类不是「没有明细」，
   * 是有意不做：写明为什么
   */
  attrSkipped: {
    sql_changed: '取数的 SQL 或上游配置有改动，两边的行不能直接对照，不做维度归因',
    incomparable: '无法判断变化来源，不做维度归因',
    added: '基准没有这个指标，不做维度归因',
    removed: '本次没有这个指标，不做维度归因',
    unchanged: '数值没有变化，不做维度归因',
  } as Record<string, string>,
  /** 服务端没给占比、也没说为什么时的说法 */
  attrNoShare: '无法计算各项占比',
  colKey: '维度',
  colShare: '占比',
  // 查询
  queries: '查询',
  queriesEmpty: '两次运行都没有执行查询',
  structure: {
    same: '相同',
    params_only: '只参数变',
    changed: '结构变',
    added: '新增',
    removed: '移除',
  } as Record<string, string>,
  structureHint: {
    same: 'SQL 完全相同',
    params_only: 'SQL 结构相同，只有字面量不同',
    // 服务端在只换了数据源、有一边的 SQL 读不出来时也标「结构变」
    changed: 'SQL 结构或数据源不同，或有一边的 SQL 无法读取',
    added: '本次新增的查询，基准中没有',
    removed: '基准中有、本次没有的查询',
  } as Record<string, string>,
  nth: (n: number) => `第 ${n} 条`,
  rows: (n: number | null | undefined, truncated: boolean | null | undefined) =>
    (n == null ? '—' : `${formatNumber(n)} 行${truncated ? '（已截断）' : ''}`),
  rowsBoth: (base: string, target: string) => `基准 ${base} → 本次 ${target}`,
  rowsBase: (base: string) => `基准 ${base}`,
  rowsTarget: (target: string) => `本次 ${target}`,
  literals: '变化的参数',
  /** 参数前后两个值之间的箭头，读屏念这个 */
  changedTo: '改为',
  baseSql: '基准的 SQL',
  targetSql: '本次的 SQL',
  bothSql: '两次相同的 SQL',
  /** 快照读不出来、又拿不到执行时的参数：这一边没有 SQL，不能拿空串去和另一边比 */
  sqlUnreadable: '这一边的 SQL 无法读取',
  onlyBase: '基准独有',
  onlyTarget: '本次独有',
  showSql: '查看 SQL',
  hideSql: '收起 SQL',
  diffCoarse: '两条 SQL 差异较大，只标出了大致范围',
  // 模板
  template: '模板',
  version: (v: number | null | undefined) => (v == null ? '未固定版本' : `v${v}`),
  versions: (base: string, target: string) => `基准 ${base} → 本次 ${target}`,
  sameGraph: '两次运行使用同一张执行图',
  nodesNone: '执行图有变化，但没有列出具体节点',
  nodeChange: {
    added: '新增节点',
    removed: '移除节点',
    config: '配置变更',
  } as Record<string, string>,
  keys: '配置项',
  // 说明
  notes: '说明',
}

/**
 * 可验证报告：把一次已封存运行的报告导出成一个 HTML 文件，收件人不连服务也能在浏览器里核对。
 * 入口在记录页的封存凭证条上；页面本身的文案在服务端 bundle/verifier.js 里（它是离开系统独立打开的文件）
 */
export const BUNDLE_TEXT = {
  title: '可验证报告',
  export: '导出可验证报告',
  hint: '导出为单个 HTML 文件：收件人不连服务，也能在浏览器中核对数字的出处和运行记录',
  /** 失败结束的运行：之后继续运行会重新封存，导出的文件就再也对不上系统里的封存了 */
  hintFailed: '导出为单个 HTML 文件。这次运行以失败结束：之后若继续运行，系统会重新封存，导出的文件将无法再与系统对照',
  /** 不能导出时说明的原因：和凭证条左边「未封存 · …」的说法对得上 */
  blockedRunning: '运行结束并封存后才能导出',
  blockedWaiting: '等待审批中，运行结束并封存后才能导出',
  blockedUnsealed: '这次运行没有封存清单，无法导出',
  exported: (name: string) => `已导出「${name}」`,
  exportedFailed: (name: string) => `已导出「${name}」。之后若继续运行，系统会重新封存，这份文件将无法再与系统对照`,
  /** 服务端没给文件名时按运行名补，规则同服务端的附件名 */
  fileName: (name: string) => `${name}-可验证报告.html`,
}

/**
 * 审批质询：审批人在批准前就待审的报告提问（选一句、划一段原文，或就整份报告），核查模型只读查库作答。问答写进运行
 * 记录、随运行一并封存，不改动报告；封存之后只有探索运行还能问，追加在封存之后
 */
export const INQUIRY_TEXT = {
  title: '审批质询',

  // ---- 时间线（decode.ts 解读 approval.inquiry）：标题带问题的前 40 字，副标题说回答是否完成、查了几次 ----
  timeline: (question: string) => (question ? `审批人质询：${question}` : '审批人质询'),
  complete: '回答完成',
  /** 按已查到的结果收了尾：回答可能缺了没来得及查的部分 */
  incomplete: (limit: string) => `回答不完整（${limit}）`,
  failed: (limit: string | null) => (limit ? `未能回答（${limit}）` : '未能回答'),
  /** 服务端没说成没成（缺字段）、却有回答正文：只说答了，不说完成 */
  answered: '已回答',
  queries: (n: number) => (n > 0 ? `查询 ${formatNumber(n)} 次` : '未执行查询'),
  postSeal: '封存后追加',
  limit: {
    max_queries: '已达查询次数上限',
    timeout_s: '已达时长上限',
    max_cost_usd: '已达单次金额上限',
    daily_max_usd: '已达核查每日金额上限',
  } as Record<string, string>,
  limitOther: '已达上限',
  detail: {
    question: (q: string) => `问题：${q}`,
    quote: (q: string) => `原文：「${q}」`,
    answer: (t: string) => `回答：${t}`,
    error: (e: string) => `原因：${e}`,
    model: (m: string) => `核查模型：${m}`,
  },

  // ---- 报告上的入口（EvidenceDoc）：句末的「质询」、划选原文时浮出的「质询」 ----
  ask: '质询',
  askUnit: (text: string) => `质询这句：${text}`,
  /** 句末「质询」图标的悬停说明 */
  askTitle: '质询这一句',
  askSelection: '就选中的原文质询',
  /** 读屏摘要里接在按键说明后面 */
  keysHint: '每句结论后有「质询」按钮：方向键走到这一句，再按 Tab 到达；没有可点内容的句子，方向键直接走到它的「质询」',

  // ---- 问答串（ReportInquiry） ----
  headCount: (n: number) => `${formatNumber(n)} 次`,
  askReport: '就整份报告提问',
  intro: '展开逐段证据后，点句末的「质询」图标或划选一段原文，即可就该处提问',
  introRun: '点句末的「质询」图标或划选一段原文，即可就该处提问',
  /** 报告没有逐段证据（没有报告文档）：句子、原文上都没有入口 */
  introNoDoc: '这份报告没有逐段证据，可以就整份报告提问',
  /** 设置里关了审批质询：不给入口，已有的问答照列 */
  disabled: '审批质询已在设置中关闭',
  wholeReport: '整份报告',
  /** 报告文档取不到，写不出问的是哪一句 */
  unitMissing: '报告中的一句',
  /** 之前几轮审批里问的、报告后来改写过（句子编号对不上），或者对不上是哪一稿：单独一组 */
  otherDoc: '报告其他版本上的质询',
  otherDocHint: '提问时的报告与当前显示的版本不同，或无法确认是否相同，所以不挂到当前报告的句子下面',
  loadFailed: '无法读取之前的质询记录',
  retry: '重试',
  q: '问',
  a: '答',
  unsigned: '未署名',
  entryLabel: (question: string) => `质询：${question}`,
  quoteLine: (quote: string) => `原文「${quote}」`,
  notRecorded: '这次问答没有写入运行记录',
  notRecordedHint: '它只显示在当前页面，刷新后不再显示，也不会随运行封存',
  /** 屏幕阅读器：回答回来了（带上问题的开头，连着几次回答各报一遍） */
  arrived: (question: string) => {
    const chars = [...question.trim()]
    return `已收到回答：${chars.length > 20 ? `${chars.slice(0, 20).join('')}…` : chars.join('')}`
  },
  /** 同一张审批卡同时挂在两处（画布的审批浮层和右栏）：提问框只在打开它的那一处 */
  elsewhere: '提问框已在另一处打开',
  moveHere: '在这里继续',
  /** 回答期间另开了提问框，正在回答的那一条却没问成：原来的问题和原因留在它那一句下面 */
  askFailed: (message: string) => `没有问成：${message}`,
  reask: '放回提问框',
  dismiss: '忽略',

  // ---- 提问框 ----
  target: { unit: '就这句提问', quote: '就选中的原文提问', report: '就整份报告提问' } as Record<'unit' | 'quote' | 'report', string>,
  questionLabel: '问题',
  placeholder: '例如：这个数是否扣除了退款？',
  submit: '提问',
  cancel: '取消',
  chars: (n: number, max: number) => `${formatNumber(n)}/${formatNumber(max)}`,
  keys: 'Ctrl + 回车提交 · Esc 关闭',
  empty: '请填写问题',
  tooLong: (max: number) => `问题最多 ${formatNumber(max)} 字，请删减后再提交`,
  quoteCut: (max: number) => `选中的原文超过 ${formatNumber(max)} 字，只提交前 ${formatNumber(max)} 字`,
  hint: '由核查模型只读查询数据作答，问答写入运行记录，不改动报告',
  hintPostSeal: '运行已封存：问答追加在封存之后，不改动报告',
  busyOther: '这次运行另有一条质询正在回答，回答完再提问',
  submitFailed: '提问失败',

  // ---- 回答中：审批卡的批准 / 驳回暂停（服务端这时也会拒绝） ----
  pending: '正在查询与回答，通常需要几十秒',
  waited: (seconds: number) => `已等待 ${formatNumber(seconds)} 秒`,
  decideBlocked: '正在回答质询，回答完再批准或驳回',

  // ---- 回答 ----
  noAnswer: '没有给出回答',
  answerIncomplete: (limit: string) => `${limit}：回答依据已查到的结果，可能不完整`,
  answeredBy: (model: string | null) => `回答由核查模型${model ? `「${model}」` : ''}给出，并非系统核对`,
  cost: (usd: string) => `花费 ${usd}`,
  /** 回答里有出处的数字：读屏听到「数字，出处：查询 Q1 · 第 1 行 · amount」，点一下展开那次查询的结果 */
  cited: (text: string, source: string) => `${text}，出处：${source}，查看这次查询的结果`,
  citedReport: (text: string, source: string) => `${text}，出处：${source}，在报告中查看证据`,
  /** 问的是报告的另一稿：引用的是那一稿里的出处，不在当前这一稿里找 */
  citedOther: (text: string, source: string) => `${text}，出处：${source}（提问时那一版报告中的出处）`,
  /** 引用的是报告已有的出处，报告里却找不到引用它的地方：照回答文档记下的出处说一句 */
  reportSource: (source: string) => `出处：${source}（报告已有的出处，报告中没有引用它的地方）`,
  reportSourceOther: (source: string) => `出处：${source}（提问时那一版报告中的出处）`,
  queriesHead: (n: number) => `核查模型执行的查询（${formatNumber(n)} 次）`,
  /** 查询列表收着时也写：截断直接关系到回答靠不靠得住 */
  queriesTruncated: (n: number) => `其中 ${formatNumber(n)} 次结果已截断`,
  noQueries: '核查模型没有执行查询',
  rows: (n: number) => `${formatNumber(n)} 行`,
  truncated: '结果已截断，数据库中还有更多数据',
  showResult: '查看结果',
  hideResult: '收起结果',
  snapshotLoading: '正在读取查询结果…',
  snapshotFailed: '无法读取这次查询的结果',
  masked: (columns: string) => `${columns} 在数据源中设置了遮罩，不显示原值`,

  // ---- 运行详情「工件」页签：回答文档按工件种类 inquiry_answer 记在运行名下（pages/runs/model.ts 的工件名和说明） ----
  artifactKind: '质询回答',
  /**
   * 服务端在工件的 meta 里只记问题的前 40 字（engine/inquiry.py 的 QUESTION_BRIEF）：满 40 字的多半截断过，末尾补省略号，
   * 免得半句话读成整个问题
   */
  artifactAbout: (question: string) => {
    const chars = [...question.trim()]
    return `质询「${chars.join('')}${chars.length >= 40 ? '…' : ''}」的回答`
  },
  /** meta 里没有问题摘要时的说法（服务端照理都会记，这里兜底）：照报告文档那一句写装的是什么 */
  artifactAboutNone: '审批质询的回答：逐段的片段和每个数字的出处',
}

/**
 * 数字推演：在一次运行的基础上改动输入，推演指标的变化（证据面板里口径卡上的数，run/WhatIf.tsx）。
 * 推演永远不是出具：横幅要说清不会写进运行记录、不会改动报告。行号对人一律从 1 数（和查询结果表格的 # 列一致）
 */
export const WHATIF_TEXT = {
  title: '数字推演',
  open: '推演',
  openHint: '改动输入或排除查询结果中的行，查看这个数和报告会如何变化',
  close: '收起推演',
  banner: '推演，非出具：不会写进运行记录，也不会改动报告',
  unsealed: '运行未封存，数据可能还会变',
  sealBroken: '封存核对未通过，推演所依据的数据可能已被改动',
  loading: '推演中…',
  failed: '推演失败',
  result: '推演结果',
  unchanged: '无变化',
  delta: (text: string) => `变化 ${text}`,
  substituted: '推演代入式',
  inputs: '输入',
  original: (value: string) => `原值 ${value}`,
  inputLabel: (path: string) => `${path} 的推演值`,
  notNumber: '请输入数值',
  masked: '已遮罩',
  withheld: '查询快照无法核对，不显示原值',
  excludedRows: (rows: number[]) => `已排除第 ${rows.map((r) => r + 1).join('、')} 行`,
  pickRows: (alias: string) => `在查询 ${alias} 中勾选要排除的行`,
  pickHere: (n: number) => `勾选要排除的行（共 ${n} 行）`,
  pickHereClose: '收起要排除的行',
  rowsHint: '勾选的行在推演中视为不存在，这个指标没有用到的行不能勾选',
  rowUnused: '这个指标没有用到这一行，排除它不影响结果',
  rowsPending: '推演结果返回后才能勾选',
  rowsLoading: '正在读取查询结果…',
  /** 三个数已按千分位写好（formatNumber） */
  rowsRange: (from: string, to: string, total: string) => `第 ${from}–${to} 行，共 ${total} 行`,
  rowsMore: (n: number) => `再显示 ${n} 行`,
  excludeHead: '排除',
  excludeRow: (row: number) => `排除第 ${row + 1} 行`,
  excludedCount: (n: number) => `已排除 ${n} 行`,
  backToResult: '回到推演结果',
  segments: '报告中会变的地方',
  segmentsNone: (n: number) => `报告中引用这个指标的 ${n} 处不受影响`,
  segmentsUnknown: '这个指标无法推演，报告中引用它的地方会如何变化无法确定',
  locate: '定位',
  inReport: (node: string) => `另一份报告（${node}）`,
  unsupported: '不支持推演的输入',
  wholeMetric: '整个指标',
  nothingToAdjust: '这个指标没有可以修改的输入，也没有可以排除的查询行',
  affected: '可能受影响的下游口径卡',
  affectedHint: '这些口径卡引用了本卡的指标，推演不会重算它们',
  affectedCard: (caliber: string, version: string) => `口径卡「${caliber || '未命名'}」${version ? ` ${version}` : ''}`,
  indirect: '间接引用',
  reset: '恢复原值',
  empty: '这个指标的计算式没有引用任何输入，无可推演',
}

/**
 * 双盲复算：节点库说明、检查器、节点卡片摘要、两边对不上时的审批卡，以及时间线上双盲复算的日志。核查的上限叫法
 * （limitLabel）和出具缺口里「核查未通过」「核查未完成」那两组（verifyGaps）反方审稿也用，放在这里一处；反方审稿自己的
 * 时间线说法在 CHALLENGE_TEXT.log。
 *
 * 两边的叫法固定为「原流程」「盲算」（服务端 explain_sql_diff 的 labels 也是这两个词），审核人的三个选择固定为
 * 「采用原流程的结果」「采用盲算的结果」「两边都不对」。选「采用盲算的结果」不会替换报告里的数，只记缺口，
 * 卡上要说清；探索运行不出具，出具相关的话只对正式运行说
 */
export const DUAL_CHECK_TEXT = {
  description: '另起一路独立计算，与原结果逐项比对',
  original: '原流程',
  blind: '盲算',

  // ---- 检查器（nodeDefs 的 dual_check、canvas/DualCheckFields.tsx）
  /**
   * 字段名，键就是配置键。服务端校验和报错引用的是同一套叫法（engine/labels.py 的 DUAL_CHECK_FIELD_LABEL，check-verify
   * 逐项比对）：问题挂在字段下面、出现在问题面板里时，人才找得到是哪一项。模型两项照「裁判模型 · 接入」「裁判模型」的写法
   */
  field: {
    card: '口径卡',
    metrics: '复算的指标',
    definitions: '口径说明',
    provider: '盲算模型 · 接入',
    model: '盲算模型',
    tolerance: '允许的相对误差',
    on_reject: '未采用原流程时',
    enabled: '启用双盲复算',
  },
  help: {
    card: '留空则复算上游的口径卡。只能复算本节点上游的口径卡：运行到本节点时，它的结果已经算出',
    metrics: '默认复算口径卡中输入来自查询结果的全部指标，取消勾选可以只复算其中一部分。输入不是来自查询的指标无法盲算',
    definitions: '给盲算模型看的业务定义，例如「销售额按支付时间统计，不含退款」。盲算模型看不到原流程的 SQL，'
      + '只能依据指标名称、这里的说明和数据目录编写查询，说明越具体，比对越有意义',
    model: '接入和模型均未填写时，使用「设置 → 偏好设置 → 核查」中的核查模型；填写任一项后，整组以节点配置为准，'
      + '不与设置合并。建议与原流程使用的模型不同，同一模型不易发现自身的错误',
    tolerance: '两边的值相差不超过这个比例时算一致，例如 0.001 表示千分之一，0 表示必须完全相同。留空则使用设置中的值',
    on_reject: '盲算与原流程不一致时运行暂停，由审核人逐个指标选择采用哪一边。选择「采用盲算的结果」或「两边都不对」时按这里处理',
    enabled: '关闭时本节点不复算，直接放行，并在时间线上记录一条说明',
  },
  /**
   * 口径卡下拉留空那一项：上游没有、只有一张、有好几张。好几张时服务端校验要求选一张（不选会挡住发起运行），
   * 写成要选，不写「建议」
   */
  cardDefault: (name: string | null, upstream: number) => (upstream === 0 ? '默认：上游的口径卡（上游没有口径卡）'
    : upstream === 1 && name ? `默认：上游的口径卡「${name}」` : `请选择要复算的口径卡（上游有 ${upstream} 张）`),
  cardChoose: (n: number) => `上游有 ${n} 张口径卡，请选择要复算的那一张`,
  cardUpstream: '上游的口径卡',
  cardOther: '其他口径卡（需连接到本节点上游）',
  /** 画布上一张口径卡的叫法：节点名，口径名不同时附在后面 */
  cardOption: (label: string, caliber?: string | null) => (caliber && caliber !== label ? `${label}（${caliber}）` : label),
  cardGone: (id: string) => `${id}（不在画布上）`,
  cardNotUpstream: '所选口径卡不在本节点上游，运行到本节点时还没有它的结果。请把它连接到本节点之前',
  cardMissingHint: '所选口径卡已不在画布上，请重新选择',
  /** 选中的节点在画布上，但不是口径卡（助手改图、导入 JSON 会写出这种配置）：下拉里那一项、下面那句提示 */
  cardWrongType: (label: string) => `${label}（不是口径卡）`,
  cardWrongTypeHint: (type: string) => `所选节点是「${type}」节点，不是口径卡，请重新选择`,
  metricsAll: '全部指标',
  metricsDefault: '未指定：复算全部输入来自查询的指标',
  metricsPicked: (n: number, total: number) => `已选 ${n} / ${total} 个指标`,
  metricsNoCard: '先选择要复算的口径卡',
  metricsNone: '这张口径卡还没有定义指标',
  metricsPinned: '这张口径卡引用了其他工作流发布的口径，指标在运行时按引用的版本确定：将复算其中输入来自查询的全部指标',
  metricsStale: (ids: string[]) => `已选的指标不在这张口径卡中：${ids.join('、')}`,
  metricsClear: '清除不在卡中的指标',
  metricsLast: '至少保留一个指标；要复算全部指标时全部勾选即可',
  metricOption: (name: string | null | undefined, id: string) => (name && name !== id ? `${name}（${id}）` : id),
  definitionPlaceholder: (name: string) => `「${name}」的口径说明`,
  definitionsNone: '选择要复算的口径卡后，可以为每个指标写一段口径说明',
  definitionsPinned: '这张口径卡引用了其他工作流发布的口径，指标在运行时才确定，无法在这里逐个填写口径说明',
  definitionGone: (id: string) => `指标「${id}」已不在复算范围内，这段说明不会交给盲算模型`,
  definitionRemove: '删除这段说明',
  providerFollow: '跟随设置',
  providerFromModel: '按模型名找接入',
  providerGone: (name: string) => `${name}（不存在）`,
  providerOff: '（已停用）',
  modelFollow: '跟随设置',
  modelOfProvider: '留空则使用该接入的默认模型',
  /**
   * 节点上只填了盲算模型、接入留空时实际用哪个接入（verify_model_spec：哪一级写了就整组用哪一级，不跨级拼；
   * 再按模型名找接入）。found：按模型名找到的接入；fallback：认不出时去调的默认接入
   */
  byModel: (model: string, found: { name: string; enabled: boolean } | null | undefined, fallback: string | null | undefined) => {
    if (found && !found.enabled) return `只填了模型：「${model}」属于已停用的接入「${found.name}」，盲算调用将失败。请启用该接入或选择其他接入`
    if (found) return `只填了模型：将按模型名使用接入「${found.name}」。接入和模型都未填写时，才跟随设置中的核查模型`
    return `只填了模型：所有接入的模型列表中都没有「${model}」，将使用默认接入${fallback ? `「${fallback}」` : ''}调用。`
      + '如该模型不属于默认接入，请选择对应的接入'
  },
  tolerancePlaceholder: (v: number | null | undefined) => (v == null ? '跟随设置' : `跟随设置（${v}）`),
  toleranceInvalid: '请填写不小于 0 的数',
  /** 选项以服务端报错里的短说法（engine/labels.py 的 DUAL_CHECK_ON_REJECT_LABEL）开头，后面可以带一句用途 */
  onReject: { degrade: '计入缺口、出具降档（默认）', fail: '判为失败，运行终止' } as Record<string, string>,
  onRejectUnknown: (v: string) => `${v}：无法识别的值，请从上方选项中选择`,
  enabled: { follow: '跟随设置', on: '开启', off: '关闭' },
  /** 「跟随设置」那一项附上设置里现在是开是关（设置还没取到就不附） */
  enabledFollow: (on: boolean | null | undefined) => (on == null ? '跟随设置' : `跟随设置（当前${on ? '开启' : '关闭'}）`),

  // ---- 节点卡片的摘要：复算哪张卡、几个指标
  summary: {
    card: (name: string, picked: number | null, total: number | null) => `复算「${name}」 · ${
      picked != null ? `${picked} 个指标` : total != null ? `全部 ${total} 个指标` : '全部指标'}`,
    noCard: '复算上游的口径卡 · 尚未连接口径卡',
    many: (n: number) => `复算上游的口径卡 · 上游有 ${n} 张，请指定`,
    off: '已关闭',
  },

  // ---- 审批卡（run/DualCheckApproval.tsx）
  approval: {
    intro: (n: number) => `盲算与原流程有 ${n} 个指标不一致，请逐个选择采用哪一边的结果`,
    model: (model: string) => `盲算模型：${model}`,
    sameModel: '盲算模型与原流程相同，复算缺乏独立性',
    /** delta_pct 已经乘过 100、带正负号写好（「+12.5%」） */
    delta: (text: string) => `相差 ${text}`,
    deltaZero: '原流程值为 0，无法计算相差比例',
    deltaHint: '相差比例 =（盲算值 − 原流程值）÷ |原流程值|',
    diffHead: '差异说明',
    /** 一条具体差异都没有时的固定说法（服务端兜底说明的前半句）；兜底的原话另有原因（比较超时、解析失败）时附在下面 */
    fallbackOnly: '两条 SQL 的写法不同，未能归纳成具体差异',
    equivalent: '两条 SQL 的写法等价，差异出在取用的结果上，请对照两边取用的值',
    loading: '正在加载完整对照…',
    loadFailed: '无法加载完整对照，以下仅为摘要',
    compare: '对照两条 SQL',
    compareClose: '收起对照',
    sqlOriginal: '原流程的 SQL',
    sqlBlind: '盲算的 SQL',
    sqlMissing: '没有记录 SQL',
    source: (s: string) => `数据源 ${s}`,
    inputs: '输入取值',
    textDiff: '规范化后不同的行',
    textDiffNone: '这一边没有不同的行',
    rationale: '盲算的说明',
    /** 理由文档里有没有出处的数字时，理由下面那一句提醒 */
    rationaleBare: '理由中标出的数字没有引用盲算自己的查询结果，无法核对出处',
    /**
     * 服务端没有预告（老服务端，见 catalogPreview）、两边又只差一处时，提交前说清选择可能被记进数据目录。卡上只看得到
     * 差异的方面，看不到服务端写目录的其余条件（dual_check_facts.derive：只有一边按时间筛的不记，多出的条件得在排除
     * 无效记录、又不是指标自己的范围，只差去不去重时得采用去重的一边；同一次裁决里几个指标给同一张表记的业务日期、
     * 去重规则不同时一项都不写），所以每句只说必要条件（「仅当……才」），不写的几种常见情况照实说出来，免得审核人
     * 为了不让目录被写错而避开本来对的一边。卡上是「方面那句 + tail」。tail 紧跟在「……时不记」后面，开头写明说的是
     * 写入的项
     */
    catalog: {
      time: '仅当两边按不同的列筛时间，采用的一边所用的时间列才会作为该表的业务日期记入数据目录；只有一边按时间筛时不记，'
        + '同一次裁决里几个指标给同一张表记的业务日期不同时都不记',
      filter: '仅当多出的条件只在一边、并且是在排除退款、作废、测试这类无效记录，采用有它的一边时，这个条件才会作为该表的'
        + '有效记录条件记入数据目录；只看某个地区、某个渠道这类限定指标范围的条件不记',
      aggregate: '仅当两边只差是否去重、并且采用的是去重的一边，去重的做法才会作为该表的去重规则记入数据目录；采用不去重的一边时不记，'
        + '同一次裁决里几个指标给同一张表记的去重规则不同时都不记',
      tail: '。写入的项记为人工确认的事实；选「两边都不对」时不记，数据目录中已有人工确认的不同说法时不覆盖，是否记入以时间线上的记录为准',
    },
    /**
     * 服务端预告了采用哪一边会往数据目录里写什么（payload.summary[].catalog）时照它说：三个选择逐条一句，会写的说哪张表
     * 的哪一项会记为什么，不写的说不会记入和原因（服务端的原话，和裁决后时间线上跳过的原因一字不差）。两边都认不出
     * 可记的事实（都没有写到哪）时卡上不提数据目录。卡上是「head + 三条 + tail」，审核人当前选的那一条加重。
     *
     * 同一次裁决里几个指标按当前的选择落到同一张表同一项上时，服务端写目录前把它们归拢（dual_check_facts._merged）：
     * 说法相同的写一次，有效记录条件用 AND 合成一句，业务日期、去重规则说法不同的都不写，驳回过的拿出来不写。对照工件
     * 里有服务端把每种组合算好的结果（catalog_joint）时照它说：会写的是合起来的那个值，并写明和哪几个指标一起记
     * （together）；没有时（老服务端、组合太多、工件还没取到）按其余指标当前的选择推（conflict、joined、pending），
     * 被目录挡下的同项指标也算进来，推不准的（pending）只说可能，结尾换成 tailInferred：推出来的不是服务端算好的结果，
     * 不能和 tail 一样只在「审批期间目录有改动」时才不准
     */
    catalogPreview: {
      head: '记入数据目录：',
      /** where 同时间线（log.catalogItem）：「orders」（shop）的业务日期；value 是要写进去的说法 */
      write: (choice: string, where: string, value: string) => `${choice}时，${where}会记为「${value}」`,
      /** 两边写的是同一张表的同一项、只差取值时，后一边省掉「哪张表的哪一项」 */
      writeSame: (choice: string, value: string) => `${choice}时会记为「${value}」`,
      /** reason 是服务端的原话 */
      blocked: (choice: string, reason: string) => `${choice}时不会记入：${reason}`,
      skip: (choice: string) => `${choice}时不记`,
      /** 接在 write / writeSame 后面：和这些指标按当前的选择一起记入同一项（服务端算好的结果） */
      together: (metrics: string) => `，与${metrics}的选择一起记入`,
      /** metrics 是 metricNames 写好的、和这一边说法不同的指标（按它们当前的选择） */
      conflict: (choice: string, where: string, metrics: string) => `${choice}时，${where}与${metrics}的选择说法不同，都不记`,
      /** 同 writeSame：两边写的是同一项时，后一边省掉「哪张表的哪一项」 */
      conflictSame: (choice: string, metrics: string) => `${choice}时与${metrics}的选择说法不同，都不记`,
      /** 有效记录条件接在 write / writeSame 后面：和这些指标当前采用的条件合成一句写 */
      joined: (metrics: string) => `，与${metrics}采用的条件用 AND 合成一句`,
      /**
       * 没有算好的合并结果、同一项上又有被目录挡下的指标（有效记录条件）：挡下的原因卡上认不出（驳回过的会被拿掉，
       * 别的原因会连这一边一起挡下），写不写说不准，只说可能。value 同 write
       */
      pending: (choice: string, where: string, value: string, metrics: string) =>
        `${choice}时，${where}可能记为「${value}」，也可能不记：要与${metrics}的选择合起来才能确定`,
      /** 同 writeSame：两边写的是同一项时，后一边省掉「哪张表的哪一项」 */
      pendingSame: (choice: string, value: string, metrics: string) =>
        `${choice}时可能记为「${value}」，也可能不记：要与${metrics}的选择合起来才能确定`,
      /** 「退款率」、「订单数」；多于 3 个只列前 3 个 */
      metricNames: (names: string[]) => names.slice(0, 3).map((n) => `「${n}」`).join('、')
        + (names.length > 3 ? `等 ${names.length} 个指标` : ''),
      tail: '写入的项记为人工确认的事实。以上按发起审批时的数据目录和各指标当前的选择预告，审批期间数据目录有改动时，以时间线上的记录为准',
      /** 有一条是按其余指标当前的选择推出来的（没有算好的合并结果）：推断不一定准，实际写入一律以时间线为准 */
      tailInferred: '写入的项记为人工确认的事实。几个指标落到同一项上时，以上是按各指标当前的选择推断的结果，'
        + '实际写入以时间线上的记录为准',
    },
    choices: { a: '采用原流程的结果', b: '采用盲算的结果', neither: '两边都不对' } as Record<'a' | 'b' | 'neither', string>,
    choiceGroup: (name: string) => `「${name}」采用哪一边的结果`,
    /** 三个选择的后果，按运行类别说：探索运行不出具，不提出具；老服务端没给类别的照实说「正式运行」 */
    consequence: (runClass: string | null | undefined) => (runClass === 'formal'
      ? '采用原流程的结果：沿用原流程的值，不影响出具。采用盲算的结果或两边都不对：报告中的数仍取自原流程，这个指标记为出具缺口，出具降档'
      : runClass === 'exploratory'
        ? '采用原流程的结果：沿用原流程的值。采用盲算的结果或两边都不对：报告中的数仍取自原流程，这个指标记为缺口'
        : '采用原流程的结果：沿用原流程的值。采用盲算的结果或两边都不对：报告中的数仍取自原流程，这个指标记为缺口，正式运行的出具随之降档'),
    /**
     * 节点的「未采用原流程时」设为判为失败（从这次运行的图快照里读到的）：采用盲算或都不对时节点失败、运行终止，
     * 不再说出具降档
     */
    consequenceFail: '采用原流程的结果：沿用原流程的值，运行继续。采用盲算的结果或两边都不对：节点的「未采用原流程时」'
      + '设为「判为失败」，提交后运行将终止',
    /** 读不到节点怎么设的（图快照取不到、节点在子工作流里）：降档那句后面有条件地补一句 */
    consequenceMaybeFail: '如节点的「未采用原流程时」设为「判为失败」，选择后两项会终止运行',
    note: '备注（可选）',
    notePlaceholder: '说明裁决的依据，会随裁决记入运行记录',
    submit: '提交裁决',
    pending: (n: number) => `还有 ${n} 个指标未选择`,
    /** 提交后的提示：全采用原流程、或节点按缺省处置时运行继续；判为失败时运行终止；读不到节点设置时不下结论 */
    done: '已提交裁决，运行继续',
    doneFail: '已提交裁决，运行将按节点设置终止',
    doneUnknown: '已提交裁决',
    empty: '审批内容不完整，没有可以裁决的指标',
    /** 认不出任何指标时留的出口：驳回后节点按人工驳回失败 */
    reject: '驳回并终止',
    emptyReject: '驳回后节点失败，本次运行终止。可以在备注中说明原因',
    rejected: '已驳回，本次运行终止',
  },

  // ---- 时间线（run/decode.ts explainLog）：按日志的 code 和结构化字段说，不从原话里认
  /** 日志里的 limit 字段（达到了哪个上限） */
  limitLabel: {
    max_queries: '查询次数上限', timeout_s: '时长上限', max_cost_usd: '金额上限', daily_max_usd: '每日金额上限', max_claims: '句数上限',
  } as Record<string, string>,
  limitAny: '上限',
  limitDailyNext: '今日核查费用已达上限。请在「设置 → 偏好设置 → 核查」中调高每日金额上限，或等到次日再运行',
  log: {
    skippedDisabled: '双盲复算未启用，已跳过',
    skippedDisabledNext: '如需复算，请在节点的「启用双盲复算」中选择「开启」，或在「设置 → 偏好设置 → 核查」中启用双盲复算',
    skippedNoInputs: '没有输入来自查询的指标，已跳过复算',
    skippedNoInputsNext: '双盲复算只复算输入来自查询结果的指标。请检查节点的「复算的指标」，或确认口径卡的输入取自查询',
    skipped: '双盲复算已跳过',
    sameModel: (model?: string) => (model ? `盲算模型与原流程同为「${model}」，复算缺乏独立性` : '盲算模型与原流程相同，复算缺乏独立性'),
    sameModelNext: '请在节点的「盲算模型」中指定其他模型，或在「设置 → 偏好设置 → 核查」中修改核查模型',
    agreed: (n?: number) => (n ? `双盲复算一致：${formatNumber(n)} 个指标与盲算结果相符` : '双盲复算一致：与盲算结果相符'),
    disagreed: (n?: number) => (n ? `双盲复算有 ${formatNumber(n)} 个指标与盲算结果不一致，等待审核人裁决`
      : '双盲复算有指标与盲算结果不一致，等待审核人裁决'),
    disagreedNext: '请在审批卡上对照两边的 SQL，逐个指标选择采用哪一边的结果',
    resolved: (parts: string[]) => (parts.length ? `审核人已裁决：${parts.join('，')}` : '审核人已裁决'),
    resolvedA: (n: number) => `采用原流程 ${formatNumber(n)} 个`,
    resolvedB: (n: number) => `采用盲算 ${formatNumber(n)} 个`,
    resolvedNeither: (n: number) => `两边都不对 ${formatNumber(n)} 个`,
    gaps: (n: number) => (n ? `${formatNumber(n)} 项记为缺口` : '没有缺口'),
    catalog: (n: number) => `已将 ${formatNumber(n)} 项人工确认的事实记入数据目录`,
    catalogNone: '本次裁决未写入数据目录',
    /**
     * 写进数据目录的一项：「orders」（shop）的业务日期。item 是目录项的中文叫法（认不出的键不传，只写表）；
     * 老版本给的一句中文也从这里过
     */
    catalogItem: (table?: string, source?: string, item?: string) => {
      const where = table ? `「${table}」${source ? `（${source}）` : ''}` : ''
      return where && item ? `${where}的${item}` : where || item || ''
    },
    /** 写进数据目录的一项连同写进去的值：「orders」（shop）的业务日期记为「paid_date」 */
    catalogValue: (where: string, value: string) => `${where}记为「${value}」`,
    catalogSkipped: (n: number) => `${formatNumber(n)} 项未写入`,
    catalogNext: '可在「数据 → 数据目录」中查看或修改',
    /** metric 是指标 ID（服务端日志只带 ID，中文名在原话里），不当名字加引号 */
    limit: (label: string, metric?: string) => (metric ? `指标 ${metric} 的盲算已达${label}，按无法复算处理` : `双盲复算已达${label}`),
    limitNext: '请在「设置 → 偏好设置 → 核查」中调高双盲复算的上限',
    /**
     * dual_check_unchecked：盲算触顶、出错、交不出数，或者输入本身没法单独复算（合并查询、遮罩列）。只说记为缺口，
     * 不说出具：探索运行不出具，节点接在侧枝上时缺口也进不了出具，降不降档由出具那一行说（同 gaps）
     */
    unchecked: (n?: number) => (n ? `双盲复算有 ${formatNumber(n)} 个指标无法复算，已记为缺口`
      : '双盲复算有指标无法复算，已记为缺口'),
    uncheckedNext: '无法复算的原因见展开内容。来自合并查询、遮罩列的输入无法单独复算；盲算触顶、出错的，可调高「设置 → 偏好设置 → 核查」中的上限后重新运行',
  },
  /**
   * 出具缺口里双盲复算、反方审稿报上来的那几条（缺口以「双盲复算「节点名」：」「反方审稿：」开头）：时间线、成果卡片、
   * 证据页把它们另起，不和「校验没跑全」混在一起，再按是查出了问题还是没核成分两组（run/decode.ts 的 splitIssuanceGaps）：
   * failed 是核查查出来的问题（两路结果不一致、结论被推翻），unfinished 是没核成的（指标无法复算：合并查询、遮罩列的
   * 输入做不了复算，盲算触顶、出错）——没核过的数不能说成「未通过」，读的人会以为这个数被查出了错
   */
  verifyGaps: {
    failed: '核查未通过',
    unfinished: '核查未完成',
  },
}

/**
 * 反方审稿节点：节点库说明、检查器、节点卡片摘要、审批卡上的反方审稿（run/ChallengeSummary.tsx），以及时间线上反方
 * 审稿的日志（log）。四种判断全站只有这一种叫法：经受住、被削弱、被推翻、无法判断。判断由反方模型给出，不是系统核对。
 * 「出具会降档」只在正式运行、有被推翻的结论、被推翻的真会让出具降档（服务端的 degrades；老服务端没给时按「没写只标注」
 * 算）时才说；探索运行不出具、运行类别取不到时，不说任何和出具有关的话
 */
export const CHALLENGE_TEXT = {
  description: '从反方立场复核上游报告的关键结论：另一个模型只读查库，逐句判断经受住、被削弱、被推翻还是无法判断',
  verdict: { stands: '经受住', weakened: '被削弱', refuted: '被推翻', inconclusive: '无法判断' },
  /** 服务端给了一个界面不认识的判断：照写原值，不归到哪一类 */
  verdictUnknown: (v: string) => `未知判断「${v}」`,

  // ---- 节点卡片的摘要。report 是节点 id：卡片摘要只看配置 ----
  summary: (c: { report?: unknown; max_claims?: unknown; enabled?: unknown }) => [
    c.enabled === false ? '已关闭' : '',
    typeof c.report === 'string' && c.report.trim() ? `审报告节点 ${c.report.trim()}` : '审上游报告',
    typeof c.max_claims === 'number' && c.max_claims > 0 ? `最多审 ${formatNumber(c.max_claims)} 句结论` : '句数跟随设置',
  ].filter(Boolean).join(' · '),

  // ---- 检查器 ----
  field: {
    report: '要审的报告',
    max_claims: '最多审几句',
    max_queries: '每句最多查询次数',
    timeout_s: '每句时长上限（秒）',
    provider: '反方模型 · 接入',
    model: '反方模型',
    on_refuted: '结论被推翻时',
    enabled: '启用',
  },
  followSettings: '跟随设置',
  help: {
    max_claims: '每份报告最多挑几句关键结论来审。留空则使用「设置 → 偏好设置 → 核查」中的值',
    max_queries: '复核每句结论时最多执行几次查询。留空则使用「设置 → 偏好设置 → 核查」中的值',
    timeout_s: '复核每句结论的时长上限，到时按已查到的结果给出判断。留空则使用「设置 → 偏好设置 → 核查」中的值',
    // 选项名照下拉里的写（「出具降档」「只标注」「报错并停止运行」），和服务端报错里点的选项名一致
    on_refuted: '出具降档：被推翻的结论计入出具缺口，正式运行降档出具；只标注：在审批卡和运行记录中标出，不影响出具；'
      + '报错并停止运行：本节点失败，运行停止',
  },
  onRefuted: { degrade: '出具降档（默认）', flag: '只标注', fail: '报错并停止运行' },
  unknownValue: (v: string) => `${v}：无法识别的值，请从上方选项中选择`,
  reportPick: {
    auto: (only: string | null) => (only ? `未选择：审上游的「${only}」` : '未选择：审上游的报告撰写节点'),
    none: '上游没有报告撰写节点，请先在本节点之前连接一个报告撰写节点',
    many: (n: number) => `上游有 ${formatNumber(n)} 个报告撰写节点，请选择要审的那一个`,
    missing: (id: string) => `${id}（已不在画布上）`,
    notUpstream: (label: string) => `${label}（不在上游）`,
    wrongType: (label: string) => `${label}（不是报告撰写节点）`,
    missingHint: '所选节点已不在画布上，请重新选择',
    notUpstreamHint: '所选节点不在本节点上游，运行到这里时它还没有写出报告，请重新选择或调整连线',
    wrongTypeHint: (type: string) => `所选节点是「${type}」节点，不是报告撰写节点，请重新选择`,
  },
  enabled: {
    follow: '跟随设置', on: '开启', off: '关闭',
    hint: {
      follow: '使用「设置 → 偏好设置 → 核查」中「反方审稿 · 启用」的值',
      on: '无论设置如何都执行反方审稿',
      off: '不执行反方审稿，上游的结果原样交给下游，运行记录中记一条说明',
    },
  },
  model: {
    providerFollow: '跟随设置',
    providerFromModel: '按模型名找接入',
    providerMissing: (name: string) => `${name}（不存在）`,
    providerDisabled: '（已停用）',
    modelFollow: '跟随设置',
    modelDefault: '留空则使用该接入的默认模型',
    help: '接入和模型均未填写时，使用「设置 → 偏好设置 → 核查」中的核查模型；填写任一项后，整组以节点配置为准，'
      + '不与设置合并。建议反方模型与撰写报告的模型不同，同一模型较难发现自身的错误',
    byModel: (model: string, found: { name: string; enabled: boolean } | null | undefined, fallback: string | null | undefined) => (
      found && !found.enabled
        ? `只填了模型：「${model}」属于已停用的接入「${found.name}」，调用将失败。请启用该接入或选择其他接入`
        : found
          ? `只填了模型：将按模型名使用接入「${found.name}」。接入和模型都未填写时，才跟随设置中的核查模型`
          : `只填了模型：所有接入的模型列表中都没有「${model}」，将使用默认接入${fallback ? `「${fallback}」` : ''}调用；`
            + '如该模型不属于默认接入，请选择对应的接入'),
    same: (model: string) => `反方模型与撰写报告的模型相同（「${model}」），审稿效果有限`,
  },

  // ---- 审批卡 ----
  head: (label: string) => `报告「${label}」的反方审稿`,
  count: (verdict: string, n: number) => `${verdict} ${formatNumber(n)} 句`,
  none: '报告中没有需要反方复核的关键结论',
  degrade: '出具会降档',
  degradeHint: '被推翻的结论计入出具缺口，本次正式运行将降档出具',
  flagNote: '按节点设置，被推翻的结论只做标注，不影响出具档位',
  /**
   * 按降档处置，但反方审稿节点不在带出具契约的成果节点上游（服务端的 degrades 为假）：缺口进不了出具。这句画在人工审批
   * 的卡上（画布上也会在审批节点旁弹出），主语写明是反方审稿节点，不说「本节点」——那里的本节点是审批节点
   */
  degradeOff: '反方审稿节点没有接在带出具契约的成果节点上游，被推翻的结论不计入出具缺口，不影响出具档位',
  /**
   * 服务端的 unreviewed：反查没有完成的句数（核查模型不可用、触顶没给出结论、数据源已不存在或已停用、报告没有引用
   * 数据库查询……），都记为无法判断。触顶的可能已经查过几次，不能说成「没有查询数据」；不会多于「无法判断」的句数
   */
  unreviewed: (n: number) => `${formatNumber(n)} 句未能反查，已记为无法判断`,
  unreviewedHint: '反查没有完成：可能是核查模型不可用、达到上限、数据源已不存在或已停用，或报告未引用数据库查询，原因见运行时间线',
  /** 审批里没带工件（服务端保存审稿记录失败）：每句都看不了理由和查询，整块说一次 */
  noRecord: '反方审稿记录未保存，无法查看理由和查询',
  more: (n: number) => `另有 ${formatNumber(n)} 句未列出`,
  showAll: '显示全部',
  loading: '正在加载反方审稿…',
  failed: '无法获取反方审稿的详细内容',
  retry: '重试',
  claimMissing: '反方审稿记录中没有这一句的详细内容',
  judgedBy: (model: string | null) => `判断由反方模型${model ? `「${model}」` : ''}给出，并非系统核对`,
  rationale: '理由',
  rationaleEmpty: '反方没有给出理由',
  docLoading: '正在加载理由…',
  queries: (n: number) => `反方执行的查询（${formatNumber(n)} 次）`,
  noQueries: '反方没有执行查询',
  rows: (n: number) => `${formatNumber(n)} 行`,
  truncated: '结果已截断，数据库中还有更多数据',
  /** 理由里有出处的数字：读屏听到的是「数字，出处：查询 Q2 · 第 1 行 · amount」，点一下跳到那次查询 */
  cited: (text: string, source: string) => `${text}，出处：${source}，跳到这次查询`,
  /** 同上，没有可跳的查询时（双盲复算的理由）只念出处 */
  citedPlain: (source: string) => `（出处：${source}）`,

  // ---- 时间线（run/decode.ts explainLog）：按日志的 code 和结构化字段说，不从原话里认
  log: {
    skippedDisabled: '反方审稿未启用，已跳过',
    skippedDisabledNext: '如需审稿，请在节点中开启，或在「设置 → 偏好设置 → 核查」中启用反方审稿',
    // no_report：要审的报告节点找得到（找不到时节点直接报错，到不了这里），但它的报告文档读不出来——没有写出、
    // 保存失败，或者它在这次没有执行到的分支上
    skippedNoReport: '要审的报告没有可读取的报告文档，已跳过反方审稿',
    skippedNoReportNext: '请确认报告撰写节点是否执行成功、是否写出了报告；它位于条件分支上时，确认本次运行执行到了它',
    skippedNoClaims: '报告中没有需要审的关键结论，已跳过反方审稿',
    skipped: '反方审稿已跳过',
    sameModel: (model?: string) => (model ? `反方审稿模型与报告撰写模型同为「${model}」，审稿缺乏独立性`
      : '反方审稿模型与报告撰写模型相同，审稿缺乏独立性'),
    sameModelNext: '请在节点中指定其他审稿模型，或在「设置 → 偏好设置 → 核查」中修改核查模型',
    done: (n?: number) => (n ? `反方审稿完成：审阅了 ${formatNumber(n)} 句结论` : '反方审稿完成'),
    verdict: { stands: '经受住', weakened: '被削弱', refuted: '被推翻', inconclusive: '无法判断' } as Record<string, string>,
    refuted: (n?: number) => (n ? `反方审稿推翻了 ${formatNumber(n)} 句结论` : '反方审稿推翻了报告中的结论'),
    refutedNext: '请在证据中对照反方审稿的查询和理由，核实这些结论',
    limit: (label: string, skipped?: number) => (skipped ? `反方审稿已达${label}，${formatNumber(skipped)} 句结论未审` : `反方审稿已达${label}`),
    limitNext: '请在节点或「设置 → 偏好设置 → 核查」中调高反方审稿的上限',
    /**
     * challenge_unreviewed：有句子未能反查（核查模型不可用、整理证据出错、报告没有引用数据库查询、数据源不在了、一条
     * 查询都没执行成功），记为无法判断。触顶的不在这里（由 challenge_limit 说），所以建议里不提调高上限
     */
    unreviewed: (n?: number) => (n ? `反方审稿有 ${formatNumber(n)} 句结论未能反查，已记为无法判断`
      : '反方审稿有结论未能反查，已记为无法判断'),
    unreviewedNext: '原因见展开内容。请检查核查模型和数据源是否可用，修正后重新运行；报告未引用数据库查询的结论无法反查',
    /** challenge_sources_lost：反查了，但该查的数据源有停用了的，判断不含它们的数据 */
    sourcesLost: (n?: number) => (n ? `反方审稿有 ${formatNumber(n)} 句结论只查了部分数据源` : '反方审稿有结论只查了部分数据源'),
    sourcesLostSub: (names: string[]) => `未能查询：${names.map((x) => `「${x}」`).join('、')}（已不存在或已停用），判断不含其中的数据`,
    sourcesLostNext: '请在「数据」页确认这些数据源是否应启用，启用后重新运行',
  },
}

/**
 * 设置页「核查」一节（双盲复算、反方审稿、审批质询共用的核查模型、各自的开关和上限、每日金额上限）。
 *
 * 顶层是各项的叫法，和服务端 api/settings.py 的 VERIFY_SETTING_LABEL 一字不差（保存时 422 的报错写的就是这些，
 * 界面上的字段名得对得上），嵌套的项写成「组.项」；check-verify 从 settings.py 读出来逐一比对。
 * 这一节其余的标题、说明、今日花费放在 section 里
 */
export const VERIFY_SETTING_TEXT = {
  provider: '核查模型 · 接入',
  model: '核查模型 · 模型',
  daily_max_usd: '核查每日金额上限（美元）',
  dual_check: '双盲复算',
  'dual_check.enabled': '双盲复算 · 启用',
  'dual_check.tolerance': '双盲复算 · 允许的相对误差',
  'dual_check.max_queries': '双盲复算 · 每项最多查询次数',
  'dual_check.timeout_s': '双盲复算 · 每项时长上限（秒）',
  challenge: '反方审稿',
  'challenge.enabled': '反方审稿 · 启用',
  'challenge.max_claims': '反方审稿 · 每份报告最多审几句',
  'challenge.max_queries': '反方审稿 · 每句最多查询次数',
  'challenge.timeout_s': '反方审稿 · 每句时长上限（秒）',
  inquiry: '审批质询',
  'inquiry.enabled': '审批质询 · 启用',
  'inquiry.max_queries': '审批质询 · 每次最多查询次数',
  'inquiry.timeout_s': '审批质询 · 每次时长上限（秒）',
  section: {
    title: '核查',
    hint: '双盲复算、反方审稿和审批质询都由只读的 Agent 查询数据来核对：三项共用核查模型和每日金额上限，'
      + '各自可以单独开关、设置上限。节点中已设置的项以节点为准。',
    model: '核查模型',
    /** 接入和模型都没填：服务端沿用证据裁判模型的取值链（verify_model_spec）。醒目地说一句，并给出建议 */
    notSet: '未单独配置，将使用证据裁判的模型',
    notSetAdvice: '建议配置一个与撰写报告、查询数据的模型都不同的模型：同一模型核对自身的输出时，不易发现其中的错误',
    differ: '建议与撰写报告、查询数据的模型不同：同一模型不易发现自身的错误',
    providerDefault: '跟随证据裁判模型',
    providerFromModel: '按模型名找接入',
    providerGone: (name: string) => `模型接入「${name}」不存在：核查调用将失败`,
    providerGoneOption: (name: string) => `${name}（不存在）`,
    providerOff: '（已停用）',
    modelDefault: '留空：用接入的默认模型',
    modelFollow: '留空：跟随证据裁判模型',
    modelOf: (model: string) => `留空则使用 ${model}`,
    /** 只填了核查模型、接入留空时实际用哪个接入（同裁判的说法，只是上一级是证据裁判模型） */
    byModel: (model: string, found: { name: string; enabled: boolean } | null | undefined, fallback: string | null | undefined) => {
      if (found && !found.enabled) return `只填了模型：「${model}」属于已停用的接入「${found.name}」，核查调用将失败。请启用该接入或选择其他接入`
      if (found) return `只填了模型：将按模型名使用接入「${found.name}」。「跟随证据裁判模型」仅在接入和模型都留空时生效`
      return `只填了模型：所有接入的模型列表中都没有「${model}」，将使用默认接入${fallback ? `「${fallback}」` : ''}调用。`
        + '如该模型不属于默认接入，请选择对应的接入。「跟随证据裁判模型」仅在接入和模型都留空时生效'
    },
    parts: {
      dual_check: '另起一路，由盲算模型在看不到原流程 SQL 的情况下独立查询，再与原流程的结果逐项比对',
      challenge: '从报告中挑出关键结论，由反方模型查询数据尝试推翻',
      inquiry: '审批人在批准前就报告提问，由核查模型查询数据作答，问答随运行一并封存',
    } as Record<string, string>,
    toleranceHint: '两边的值相差不超过这个比例时算一致，例如 0.000001 表示百万分之一，0 表示必须完全相同',
    unlimited: '不限',
    /** 某项设成不限时写明它还受什么约束，不要悄悄生效。by：眼下还设着上限的相关项 */
    unlimitedNote: (kind: 'queries' | 'claims' | 'timeout' | 'daily', by: string[]) => {
      if (kind === 'timeout') return '不设上限，时长仅受模型接口自身的超时约束'
      const what = kind === 'queries' ? '查询次数' : kind === 'claims' ? '句数' : '每日核查费用'
      return by.length ? `不设上限，${what}仅受${by.join('、')}约束` : `不设上限，${what}不受其他上限约束`
    },
    bound: { timeout: '时长上限', daily: '每日金额上限', claims: '报告中关键结论的数量', queries: '各项的查询次数上限', timeouts: '各项的时长上限' },
    invalid: {
      count: '请填写正整数，或勾选「不限」',
      seconds: '请填写大于 0 的秒数，或勾选「不限」',
      usd: '请填写大于 0 的金额，或勾选「不限」',
      tolerance: '请填写不小于 0 的数',
    },
    spend: (usd: string, calls: number) => `今日已花费 $${usd}（${formatNumber(calls)} 次调用）`,
    spendNone: '今日尚无核查调用',
    unpriced: (n: number) => `另有 ${formatNumber(n)} 次调用无法估算金额（模型不在价格目录中），未计入`,
    saveInvalid: '核查设置中有无效值，请修正后再保存',
  },
}
