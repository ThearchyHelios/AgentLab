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

import type { NodeType } from '../types'
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
 * 本期（数字层）实际只会出现「有出处」和「无证据」；其余几种是后续期的，先把叫法定下，
 * 免得到时候各处各起一个名字。
 */
export const EVIDENCE_STATE_LABEL = {
  deterministic: '有出处',
  supported: '模型判断：有依据',
  partial: '模型判断：部分有依据',
  unsupported: '模型判断：证据不支持',
  unjudged: '有引用，未裁判',
  none: '无证据',
  connective: '连接性文字',
  candidate: '猜测的来源',
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
  sealDocOk: '报告文档已封存 · 核对一致（这个数字本身没有证据可封存）',
  /** 片段接口取不到、封存状态是从整次运行的证据图查的：只核对了文档在封存范围内，这个数的链没逐项核对 */
  sealDocChain: '报告文档已封存 · 核对一致（这个数字的证据链这次没取到，没有逐项核对）',
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
} as const
