/**
 * 可点击证据：片段状态的外观，和几件证据相关的纯函数。
 *
 * 和 lib/status 一个路子，每种状态同时有四条通道：线型、字形、颜色、文字。去掉颜色
 * 还能靠线型和字形分（灰度打印、色弱），去掉线型还能读字（读屏的 aria-label 就是文字
 * 那一条）。颜色只用 --st-* 令牌：概率性的「有依据」借 --st-running（强调色），
 * 「未裁判」借 --st-cancelled（暗字色），「猜测」借 --st-idle（最淡的字色）——
 * 绝不用确定性的绿，模型的判断不能长得像系统核对过的事实。
 *
 * 一期（数字层）只会出现两种：确定性（引用解析成功）和无证据（裸数字、引用不存在）。
 * 三期多了表名字段名、逐字引文：有出处的实体、引文和数字同一套「有出处」线型（状态就是
 * 确定性，另有 EVIDENCE_KIND_STYLE 给它们各自的字形和说法）；可疑实体（可能是编造的
 * 名字）和核对不了的名字是无证据的两个变体，线型同无证据、字形和文字各不相同；旧运行
 * 按数值猜的候选用最淡的点状线。
 *
 * 四期接上了概率性的几种：结论句的判定是模型给的，只在句末挂一枚小徽标（◆ 有依据、◇ 部分有依据、
 * ! 证据不支持且整句浅底、? 未裁判），整句的字不画线。判定有两个来源：封存的文档里节点当场判的，
 * 和探索运行里点开再判、封存之后追加的（后者盖过前者，另写「封存后追加」）。
 */

import {
  EVIDENCE_AUDIT_TEXT, EVIDENCE_KIND_LABEL, EVIDENCE_STATE_LABEL, EVIDENCE_TEXT, JUDGE_TEXT, UPGRADE_POLICY_LABEL,
  claimTally, evidenceTally, type ClaimTallyCounts,
} from './terms'
import { NONE, formatNumber, shortId } from './format'
import type {
  EvidenceBlock, EvidenceCaliberSource, EvidenceCaliberUpgrade, EvidenceCandidate, EvidenceDocData, EvidenceFieldRef,
  EvidenceGraph, EvidenceGuess, EvidenceInput, EvidenceLocator, EvidenceSeal, EvidenceSegment, EvidenceSegmentDetail,
  EvidenceStats, EvidenceStep, EvidenceUnit, EvidenceVerdict, EvidenceViolation, ReviewResult,
} from '../types'
import type { MarkSpec } from '../run/Markdown'

export type EvidenceStateCode = keyof typeof EVIDENCE_STATE_LABEL

/**
 * 正文里的线型。solid / dotted 画在片段下面；badge 是句末的小徽标——结论句的状态
 * 不画整句下划线，否则一份报告满屏都是线；none 不装饰
 */
export type EvidenceLine = 'solid' | 'dotted' | 'badge' | 'none'

export interface EvidenceStateMeta {
  code: EvidenceStateCode
  /** 文字：aria-label、面板标题、图例 */
  label: string
  line: EvidenceLine
  /** 字形。'' 表示不画——确定性的数字就是正文，不另加记号 */
  glyph: string
  /** 前景色（字形、面板标题），只用 --st-* */
  color: string
  /** 下划线颜色 */
  decoration: string
  /** 淡底：悬停、打开面板时那一段的底色 */
  soft: string
  /** 异常态：正常态安静，只有它们该醒目、该进 n / N 的跳转 */
  alert: boolean
  /** 从哪一期开始会出现 */
  phase: 1 | 3 | 4
  /** 一句话说明，放 title 和面板 */
  hint: string
}

const mix = (token: string, pct: number) => `color-mix(in srgb, var(${token}) ${pct}%, transparent)`

export const EVIDENCE_STATE: Record<EvidenceStateCode, EvidenceStateMeta> = {
  deterministic: {
    code: 'deterministic', label: EVIDENCE_STATE_LABEL.deterministic, line: 'solid', glyph: '',
    color: 'var(--st-done)', decoration: mix('--st-done', 60), soft: 'var(--st-done-soft)',
    alert: false, phase: 1,
    hint: '由系统从证据（口径卡、查询快照、运行输入）里取值、按确定的规则渲染，不是模型写的',
  },
  supported: {
    code: 'supported', label: EVIDENCE_STATE_LABEL.supported, line: 'badge', glyph: '◆',
    color: 'var(--st-running)', decoration: 'transparent', soft: 'var(--st-running-soft)',
    alert: false, phase: 4,
    hint: '裁判模型认为引用的证据支持这句话。这是模型的判断，不是系统核对',
  },
  partial: {
    code: 'partial', label: EVIDENCE_STATE_LABEL.partial, line: 'badge', glyph: '◇',
    color: 'var(--st-waiting)', decoration: 'transparent', soft: 'var(--st-waiting-soft)',
    alert: true, phase: 4,
    hint: '裁判模型认为证据只支持这句话的一部分',
  },
  unsupported: {
    code: 'unsupported', label: EVIDENCE_STATE_LABEL.unsupported, line: 'badge', glyph: '!',
    color: 'var(--st-failed)', decoration: 'transparent', soft: 'var(--st-failed-soft)',
    alert: true, phase: 4,
    hint: '裁判模型认为引用的证据不支持这句话',
  },
  unjudged: {
    code: 'unjudged', label: EVIDENCE_STATE_LABEL.unjudged, line: 'badge', glyph: '?',
    color: 'var(--st-cancelled)', decoration: 'transparent', soft: 'var(--st-cancelled-soft)',
    alert: false, phase: 4,
    hint: '还没请模型判断证据支不支持这句话：探索运行点开再判，或者到了上限没判',
  },
  none: {
    code: 'none', label: EVIDENCE_STATE_LABEL.none, line: 'dotted', glyph: '?',
    color: 'var(--st-waiting)', decoration: 'var(--st-waiting)', soft: 'var(--st-waiting-soft)',
    alert: true, phase: 1,
    hint: '没写成引用标记的数字，或者引用解析不了：系统核对不到它从哪来',
  },
  connective: {
    code: 'connective', label: EVIDENCE_STATE_LABEL.connective, line: 'none', glyph: '',
    color: 'var(--st-idle)', decoration: 'transparent', soft: 'var(--st-idle-soft)',
    alert: false, phase: 4,
    hint: '过渡、组织结构的话，不陈述数据事实，所以不需要证据',
  },
  candidate: {
    code: 'candidate', label: EVIDENCE_STATE_LABEL.candidate, line: 'dotted', glyph: '~',
    color: 'var(--st-idle)', decoration: mix('--st-idle', 70), soft: 'var(--st-idle-soft)',
    alert: false, phase: 3,
    hint: '旧运行按数值猜的可能来源，不能当证据',
  },
  // 可疑实体：线型同无证据（点状），字形和文字另起，一眼分得出「没写出处」和「名字可能是编的」
  suspect: {
    code: 'suspect', label: EVIDENCE_STATE_LABEL.suspect, line: 'dotted', glyph: '?!',
    color: 'var(--st-waiting)', decoration: 'var(--st-waiting)', soft: 'var(--st-waiting-soft)',
    alert: true, phase: 3,
    hint: '本次运行的表结构快照、查询用到的表、查询结果列里都没有这个名字，可能是编造的',
  },
  // 核对不了：表结构快照不全，找不到不等于不存在。只是标注，不进 n / N 的跳转，颜色压低
  unverified: {
    code: 'unverified', label: EVIDENCE_STATE_LABEL.unverified, line: 'dotted', glyph: '…',
    color: 'var(--st-cancelled)', decoration: mix('--st-cancelled', 80), soft: 'var(--st-cancelled-soft)',
    alert: false, phase: 3,
    hint: '这个数据源的表太多，表结构快照只存了一部分：找不到这个名字，也说不准它不存在',
  },
}

/** 图例、检查脚本按这个顺序列 */
export const EVIDENCE_STATES = Object.keys(EVIDENCE_STATE) as EvidenceStateCode[]

/**
 * 片段在正文里的状态。文字、结构片段（行首符号、表格竖线）是 null：不画线、不进键盘顺序。
 * 后端的 probabilistic 在裁判给出结论之前按「未裁判」画
 */
export function segmentState(
  seg: Pick<EvidenceSegment, 'kind' | 'state'> & Partial<Pick<EvidenceSegment, 'issue' | 'cite'>>,
): EvidenceStateCode | null {
  if (seg.kind === 'structural') return null
  switch (seg.state) {
    case 'deterministic': return 'deterministic'
    case 'none':
      // 无证据的两个变体：可疑实体（反引号里发现的没有 cite，[[t:编造]] 的 cite 带 unknown）、核对不了
      if (seg.issue === 'unknown_entity' || seg.cite?.unknown) return 'suspect'
      if (seg.issue === 'unverified_entity' || seg.cite?.unverified) return 'unverified'
      return 'none'
    case 'probabilistic': return 'unjudged'
    case 'candidate': return 'candidate'
    default: return null
  }
}

/**
 * 有出处的片段里，实体和引文另有一层外观：线型、颜色和数字同一套「有出处」，字形和文字
 * 各自不同——引文前面挂一个引号，实体写在反引号里的照行内代码画。四个通道同 EVIDENCE_STATE
 */
export type EvidenceKindCode = 'entity' | 'quote'
export interface EvidenceKindStyle {
  code: EvidenceKindCode
  line: EvidenceLine
  glyph: string
  color: string
  label: string
  hint: string
}
export const EVIDENCE_KIND_STYLE: Record<EvidenceKindCode, EvidenceKindStyle> = {
  entity: {
    code: 'entity', line: 'solid', glyph: '', color: 'var(--st-done)', label: '有出处 · 表或字段',
    hint: '本次运行的表结构快照、查询用到的表或查询结果列里真实存在的名字',
  },
  quote: {
    code: 'quote', line: 'solid', glyph: '“', color: 'var(--st-done)', label: '有出处 · 逐字引文',
    hint: '在知识库检索命中的原文里逐字出现（空白归一化后比对）',
  },
}

/** 片段是实体还是引文（状态另看 segmentState）：其余返回 null */
export function segmentKind(seg: Pick<EvidenceSegment, 'kind'>): EvidenceKindCode | null {
  return seg.kind === 'entity' ? 'entity' : seg.kind === 'quote' ? 'quote' : null
}

/** 片段给人看的名字：反引号里的实体去掉两个反引号 */
export function segName(seg: Pick<EvidenceSegment, 'text' | 'kind' | 'code' | 'name'>): string {
  if (seg.kind === 'entity' && (seg.code || /^`[^`]+`$/.test(seg.text))) return seg.name ?? seg.text.replace(/^`|`$/g, '')
  return seg.text
}

/** 一个句子（单元格、列表项）的正文：结构片段不算 */
export function unitText(unit: Pick<EvidenceUnit, 'segments'>): string {
  return (unit.segments ?? []).filter((s) => s.kind !== 'structural').map((s) => s.text).join('')
}

/**
 * 片段的来源说一句：「口径卡指标 环比增幅」「运行输入 week」「查询 Q3 · 第 6 行 · amount」。
 * 单元格先看 cite 的种类：查询条目在目录里是 query，引用它的一格是 cell
 */
export function sourceOf(seg: EvidenceSegment, doc?: Pick<EvidenceDocData, 'catalog'> | null): string {
  const cite = seg.cite
  if (!cite) return ''
  const entry = cite.alias ? doc?.catalog?.[cite.alias] : undefined
  if (cite.kind === 'cell' || (cite.kind !== 'metric' && cite.kind !== 'input' && entry?.kind === 'query')) {
    const where = locatorText(cite.locator)
    return [EVIDENCE_TEXT.query(cite.alias ?? ''), where].filter(Boolean).join(' · ')
  }
  if (cite.kind === 'metric' || entry?.kind === 'metric') {
    const name = entry?.name ?? cite.locator?.metric ?? cite.ref ?? ''
    return name ? `${EVIDENCE_KIND_LABEL.metric} ${name}` : EVIDENCE_KIND_LABEL.metric
  }
  if (cite.kind === 'input' || entry?.kind === 'input') {
    return `${EVIDENCE_KIND_LABEL.input} ${entry?.locator?.field ?? cite.locator?.field ?? cite.ref ?? ''}`.trim()
  }
  if (cite.kind === 'table' || cite.kind === 'column' || entry?.kind === 'table' || entry?.kind === 'column') {
    // 「表 orders · 出现在查询 Q1、Q2」：名字从目录取（大小写照库里的），查询编号是报告里的全局编号
    const kind = (cite.kind ?? entry?.kind) === 'table' ? EVIDENCE_TEXT.table : EVIDENCE_TEXT.column
    const name = entry?.alias ? String(entry.alias).replace(/^[tc]:/, '') : cite.ref ?? ''
    const queries: string[] = Array.isArray(entry?.queries) ? entry.queries.map(String) : []
    return [`${kind} ${name}`.trim(), queries.length ? EVIDENCE_TEXT.entityQueries(queries) : ''].filter(Boolean).join(' · ')
  }
  if (cite.kind === 'quote') {
    const where = quoteWhere(cite.source)
    return [`${EVIDENCE_KIND_LABEL.quote} ${cite.alias ?? ''}`.trim(), where].filter(Boolean).join(' · ')
  }
  const kind = cite.kind ? EVIDENCE_KIND_LABEL[cite.kind] : ''
  return [kind, cite.alias ?? cite.ref].filter(Boolean).join(' ')
}

/** 引文出自哪：「出自「运营手册 · 退款」第 3 段」；只有文档 id 时写 id 的前几位 */
export function quoteWhere(src: { title?: string; document?: string; ordinal?: number } | null | undefined): string {
  if (!src || typeof src !== 'object') return ''
  const title = typeof src.title === 'string' && src.title.trim() ? src.title.trim()
    : typeof src.document === 'string' && src.document ? shortId(src.document, 12) : ''
  const ordinal = typeof src.ordinal === 'number' && Number.isFinite(src.ordinal) ? EVIDENCE_TEXT.quoteChunk(src.ordinal) : ''
  return [title ? EVIDENCE_TEXT.quoteFrom(title) : '', ordinal].filter(Boolean).join(' ')
}

/**
 * 查询快照里的位置怎么说：「第 6 行 · amount」「第 1–5 行 · amount」「第 1–5 行 · week、amount」。
 * 行号给人看从 1 数（引用原文 Q3.r5 里的 r5 从 0 数，面板另把原文摆出来）
 */
export function locatorText(loc: EvidenceLocator | null | undefined): string {
  if (!loc) return ''
  const ints = (v: unknown) => (typeof v === 'number' && Number.isInteger(v) && v >= 0 ? v : null)
  let rows = ''
  const row = ints(loc.row)
  if (row != null) rows = `第 ${formatNumber(row + 1)} 行`
  else if (Array.isArray(loc.rows) && loc.rows.length) {
    const a = ints(loc.rows[0])
    const b = ints(loc.rows[loc.rows.length - 1])
    if (a != null && b != null) rows = a === b ? `第 ${formatNumber(a + 1)} 行` : `第 ${formatNumber(a + 1)}–${formatNumber(b + 1)} 行`
  }
  const cols = typeof loc.column === 'string' && loc.column ? loc.column
    : loc.columns && typeof loc.columns === 'object' ? Object.values(loc.columns).filter((c) => typeof c === 'string').join('、')
    : ''
  return [rows, cols].filter(Boolean).join(' · ')
}

/** 证据里的值怎么写：数带千分位、不丢小数；布尔写是否；拿不到写「—」 */
export function evidenceValue(v: unknown): string {
  if (v == null || v === '') return NONE
  if (typeof v === 'number') return Number.isFinite(v) ? v.toLocaleString('en-US', { maximumFractionDigits: 20 }) : String(v)
  if (typeof v === 'boolean') return v ? '是' : '否'
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

/**
 * 查询步骤的窗口：接口只给被引用的行加前后各 2 行。
 *
 * - offset：rows[0] 在快照里是第几行；没给就当从 0 开始、也不说位置
 * - marks：要高亮的行，换算成窗口里的下标。highlight.rows 按快照行号给（方案第 5 节）；
 *   全都落在窗口里才这么认，否则看它们是不是本来就是窗口下标（row_offset 之前的号、老接口）
 * - windowed：窗口没盖住整份快照，要说「仅显示被引用的行及前后各 2 行」
 */
export function queryWindow(step: Pick<EvidenceStep,
  'rows' | 'columns' | 'row_offset' | 'row_index' | 'total_rows' | 'highlight'>): {
  columns: string[]; rows: unknown[][]
  /** 每一行在快照里是第几行（从 0 数）；不知道时是 null */
  index: number[] | null
  total: number | null
  marks: number[]; cols: string[]
  /** 精确到格的高亮：[窗口下标, 列名]；接口没给时 null，按 marks × cols 画 */
  cells: [number, string][] | null
  windowed: boolean
  /** 「第 4–8、10–12 行」：窗口在快照里的位置，从 1 数；不知道时 '' */
  span: string
} {
  const rows = Array.isArray(step.rows) ? step.rows.filter(Array.isArray) as unknown[][] : []
  const columns = Array.isArray(step.columns) ? step.columns.map(String) : []
  const int = (v: unknown) => (typeof v === 'number' && Number.isInteger(v) && v >= 0 ? v : null)
  const total = int(step.total_rows)
  // 行号：row_index 一行一个（窗口可以不连续）最准；只有 row_offset 时按连续的算
  const given = Array.isArray(step.row_index) ? step.row_index.map(int) : null
  const offset = int(step.row_offset)
  const index = given && given.length === rows.length && given.every((r) => r != null) ? given as number[]
    : offset != null ? rows.map((_, i) => offset + i)
    : null
  const toWindow = (r: number): number => {
    if (index) {
      const at = index.indexOf(r)
      if (at >= 0) return at
    }
    return -1
  }
  const inWindow = (r: number) => r >= 0 && r < rows.length
  // 行号按快照认（方案第 5 节），认得出几个标几个：窗口被截到 MAX_WINDOW_ROWS 时后面几行不在窗口里，
  // 不能因此一行都不标。一个都认不出、却都落在窗口里的，当成窗口下标（老接口）；认不出的是 -1
  const place = (list: number[]): number[] | null => {
    const hit = list.map(toWindow)
    return hit.some((i) => i >= 0) ? hit : list.every(inWindow) ? list : null
  }
  const wanted = (Array.isArray(step.highlight?.rows) ? step.highlight.rows : []).map(int).filter((r): r is number => r != null)
  const marks = ((wanted.length && place(wanted)) || []).filter((i) => i >= 0)
  const cols = (Array.isArray(step.highlight?.cols) ? step.highlight.cols : []).map(String).filter((c) => columns.includes(c))
  const rawCells = (Array.isArray(step.highlight?.cells) ? step.highlight.cells : [])
    .filter((c) => Array.isArray(c) && int(c[0]) != null && columns.includes(String(c[1])))
  const cellRows = rawCells.length ? place(rawCells.map((c) => c[0] as number)) : null
  const placed = cellRows ? rawCells.map((c, i) => [cellRows[i], String(c[1])] as [number, string]).filter(([r]) => r >= 0) : []
  const cells = placed.length ? placed : null
  const windowed = !!index && total != null && index.length < total
  return { columns, rows, index, total, marks, cols, cells, windowed, span: index ? spanText(index) : '' }
}

/** [3,4,5,6,7,9,10,11] → 「第 4–8、10–12 行」（从 1 数） */
function spanText(index: number[]): string {
  if (!index.length) return ''
  const sorted = [...index].sort((a, b) => a - b)
  const parts: string[] = []
  let start = sorted[0]
  let prev = sorted[0]
  for (const r of [...sorted.slice(1), Number.NaN]) {
    if (r === prev + 1) { prev = r; continue }
    parts.push(start === prev ? formatNumber(start + 1) : `${formatNumber(start + 1)}–${formatNumber(prev + 1)}`)
    start = r
    prev = r
  }
  return `第 ${parts.join('、')} 行`
}

/**
 * 这个输入对应链里的哪一个查询步骤：先按快照工件认，再按接口补的全局编号（input.query、
 * input.cell 的前缀）认。agent 字段的 ref 是那个节点内部的编号，不拿它对。认不出返回 -1
 */
export function queryOf(
  input: Pick<EvidenceInput, 'artifact' | 'query' | 'cell'>, queries: Pick<EvidenceStep, 'artifact' | 'alias'>[],
): number {
  if (input.artifact) {
    const i = queries.findIndex((q) => q.artifact === input.artifact)
    if (i >= 0) return i
  }
  const alias = input.query || (typeof input.cell === 'string' ? input.cell.split('.')[0] : '')
  return alias ? queries.findIndex((q) => q.alias === alias) : -1
}

export type SourceTone = 'ok' | 'warn' | 'alert' | 'muted'

/**
 * 输入来源那一行说什么、多醒目。
 *
 * - agent 字段（开了 cite_fields）：与快照一致 / 模型报 X 快照是 Y 已按快照取值 / 没查到记为空；
 *   unresolved 和 missing（模型写了 from: null）同一种说法：值都是空的，没有兜底成 0
 * - cell() 取数：与快照一致 / 算的时候用的值和快照不一样（值没被换掉，照实说）/ 核对不到
 * - 代码节点：取数角色提醒（核对不到快照），计算角色醒目（沙箱里的算术不该绕开口径卡）
 * - 其余（运行输入、老运行）：一期的 ok / missing
 */
export function inputSource(input: EvidenceInput): { tone: SourceTone; text: string; reason?: string } {
  const status = input.status ?? ''
  if (input.via === 'agent_field') {
    if (status === 'verified') return { tone: 'ok', text: EVIDENCE_TEXT.sourceVerified }
    if (status === 'mismatch') {
      return { tone: 'warn', text: EVIDENCE_TEXT.sourceMismatch(evidenceValue(input.model_value), evidenceValue(input.value)) }
    }
    return { tone: 'warn', text: EVIDENCE_TEXT.sourceEmpty, reason: input.reason || undefined }
  }
  if (input.via === 'tool_cell') {
    if (status === 'verified') return { tone: 'ok', text: EVIDENCE_TEXT.sourceVerified }
    if (status === 'mismatch') {
      return { tone: 'warn', text: EVIDENCE_TEXT.cellMismatch(evidenceValue(input.value),
        evidenceValue(input.snapshot_value !== undefined ? input.snapshot_value : input.value)) }
    }
    if (input.value == null) return { tone: 'warn', text: EVIDENCE_TEXT.sourceEmpty, reason: input.reason || undefined }
    return { tone: 'warn', text: EVIDENCE_TEXT.cellUnresolved, reason: input.reason || undefined }
  }
  if (input.via === 'code') {
    return input.role === 'source'
      ? { tone: 'warn', text: EVIDENCE_TEXT.codeSource(input.node_id ?? '') }
      : { tone: 'alert', text: EVIDENCE_TEXT.codeCompute(input.node_id ?? '') }
  }
  if (status === 'missing' || input.value == null) return { tone: 'warn', text: EVIDENCE_TEXT.inputMissing }
  return { tone: 'muted', text: '' }
}

/** 值得在「输入来源」里单独列一行的输入：能核对到快照的、代码节点的、缺的 */
export const notableInput = (input: EvidenceInput): boolean =>
  input.via === 'agent_field' || input.via === 'tool_cell' || input.via === 'code'
  || input.status === 'missing' || input.value == null

/**
 * 口径卡钉在哪：「口径卡「周报口径」v3，来自「销售周报」v5」。工作流名接口没给就按目录找，
 * 再没有就写工作流 id 的前几位——不写 undefined
 */
export function caliberSourceText(
  caliber: string | undefined, version: string | undefined,
  from: EvidenceCaliberSource | null | undefined, workflowName?: string,
): string | null {
  if (!from || typeof from !== 'object' || (!from.workflow_id && !from.workflow_name)) return null
  const wf = from.workflow_name || workflowName || (from.workflow_id ? shortId(from.workflow_id, 12) : NONE)
  const v = from.workflow_version != null && from.workflow_version !== '' ? `v${String(from.workflow_version).replace(/^v/, '')}` : ''
  return EVIDENCE_TEXT.caliberFrom(caliber ?? '', version ?? '', wf, v)
}

/** 升版处置那一句：「上游已有 v6，按「并排双印新旧口径」处置」。没有升版（null、没写 latest / policy）返回 null */
export function caliberUpgradeText(up: EvidenceCaliberUpgrade | null | undefined): string | null {
  if (!up || typeof up !== 'object' || (!up.policy && !up.policy_label)) return null
  const latest = up.latest != null && String(up.latest) !== '' ? `v${String(up.latest).replace(/^v/, '')}` : null
  const policy = up.policy_label || (up.policy ? UPGRADE_POLICY_LABEL[up.policy] ?? up.policy : '')
  return EVIDENCE_TEXT.caliberUpgrade(latest, policy)
}

/**
 * 无证据的原因：裸数字、引用解析不了（带后端给的人话原因）、可疑实体、核对不了。
 * 反引号里自动发现的可疑名字没有 cite，用固定的说法
 */
export function reasonOf(seg: EvidenceSegment): string {
  if (seg.issue === 'uncited_number' || (!seg.cite && seg.kind === 'number')) return EVIDENCE_TEXT.uncited
  if (seg.cite?.reason) return seg.cite.reason
  if (seg.issue === 'unknown_entity') return EVIDENCE_TEXT.suspect
  if (seg.issue === 'unverified_entity') return EVIDENCE_TEXT.unverified
  return seg.ref ? `引用 ${seg.ref} 解析不了` : ''
}

/**
 * 片段的 aria-label：字、状态、出处都写全。「8.7%，有出处：口径卡指标 环比增幅」
 * 「12，无证据：这个数字没有写成引用标记…」。读屏用户靠它，不靠颜色和线型
 */
export function segmentLabel(seg: EvidenceSegment, doc?: Pick<EvidenceDocData, 'catalog'> | null): string {
  const state = segmentState(seg)
  if (!state) return seg.text
  const why = state === 'none' || state === 'suspect' || state === 'unverified' ? reasonOf(seg) : sourceOf(seg, doc)
  // 可疑实体的原话本身就以「可能是编造的名字」收尾，状态那几个字不再重复一遍
  const label = state === 'suspect' && why.includes(EVIDENCE_STATE.suspect.label) ? '' : EVIDENCE_STATE[state].label
  return `${segName(seg)}，${[label, why].filter(Boolean).join('：')}`
}

/** 在正文里画得出线的违规：指向的片段存在，而且不是结构片段 */
export function locatable(doc: EvidenceDocData, v: EvidenceViolation): EvidenceSegment | null {
  if (!v.segment) return null
  for (const block of doc.blocks ?? []) {
    for (const unit of block.units ?? []) {
      for (const seg of unit.segments ?? []) {
        if (seg.id === v.segment) return seg.kind === 'structural' ? null : seg
      }
    }
  }
  return null
}

export interface EvidenceTally {
  /** 数字总数：带引用的数字片段 + 裸数字（含列表序号、代码块标签里的） */
  total: number
  /** 四期：结论句按判定分的句数。文档没开裁判、也没有按需判过的句子时是 null（横幅不多说这一段） */
  claims?: ClaimTallyCounts | null
  /** 可疑实体（可能是编造的名字）、核对不了的名字：数字那半句之外另说 */
  suspect?: number
  unverified?: number
  cited: number
  /** 没有出处的数字：总数 − 有出处（裸数字、解析不了的数字引用）。横幅上「无证据 M」的 M */
  none: number
  /** 不是数字、解析不了的引用：[[v:]] 值、句末 [[see:]] 依据。另起一句说，不混进 none */
  other: number
  /** 画不了线、只能在清单里找的：structural + noSegment */
  hidden: number
  /** 其中在结构片段里的（列表序号、代码块标签里的数字）：违规指着片段，片段是 Markdown 语法 */
  structural: number
  /** 其中在正文里没有对应字的（句末依据里写错的引用）：违规不指着任何片段 */
  noSegment: number
}

/** 缺陷类违规：完整性问题（render_mismatch 这些）不在这里数，它们说的是文档本身靠不住 */
const GAP_CODES = new Set(['uncited_number', 'unresolved_ref'])

const num = (v: unknown): number | undefined => (typeof v === 'number' && Number.isFinite(v) ? v : undefined)

/**
 * 文档的计数。stats 是后端 verify_doc 算的，有就用它；老文档、别的版本缺字段时
 * 从片段自己数，数出来的和 stats 是同一个口径。
 *
 * 数字那半句要算得平：none = total − cited。不是数字的解析不了的引用（other）按违规
 * 清单逐条认（指着的片段不是数字，或者根本不指片段）；没有清单时用 stats 反推
 * （uncited + unresolved − none），再没有就从片段的状态数
 */
export function docTally(doc: EvidenceDocData): EvidenceTally {
  const segs = segmentsOf(doc.blocks ?? [])
  const byId = new Map(segs.map((s) => [s.id, s]))
  const all = doc.violations ?? []
  const gaps = all.filter((v) => GAP_CODES.has(v.code))
  const hidden = gaps.filter((v) => !locatable(doc, v))
  const structural = hidden.filter((v) => !!v.segment).length
  const st = doc.stats ?? {}
  const cited = num(st.numbers_cited) ?? segs.filter((s) => s.kind === 'number' && s.state === 'deterministic').length
  const total = num(st.numbers)
    ?? segs.filter((s) => s.kind === 'number').length + hidden.filter((v) => v.code === 'uncited_number').length
  const none = Math.max(0, total - cited)
  const uncited = num(st.uncited_numbers)
  const unresolved = num(st.unresolved)
  const other = all.length
    ? gaps.filter((v) => v.code === 'unresolved_ref' && byId.get(v.segment ?? '')?.kind !== 'number').length
    : uncited != null && unresolved != null
      ? Math.max(0, uncited + unresolved - none)
      : segs.filter((s) => segmentState(s) === 'none' && s.kind !== 'number').length
  // 三期：可疑实体、核对不了的名字。stats 缺这两个键（老文档、没开实体层）时按违规清单数
  const suspect = num(st.unknown_entities) ?? all.filter((v) => v.code === 'unknown_entity').length
  const unverified = num(st.unverified_entities) ?? all.filter((v) => v.code === 'unverified_entity').length
  return { total, cited, none, other, hidden: hidden.length, structural, noSegment: hidden.length - structural,
           suspect, unverified, claims: claimCounts(doc) }
}

/**
 * 只有 stats 的时候（report.checked 事件的载荷，没有文档）：同一种算法。other 用
 * uncited + unresolved − none 反推——数字引用渲染对不上（render_mismatch，文档本身
 * 靠不住的那类）时会少数几处，这类文档另有完整性违规兜着
 */
export function statsTally(stats: EvidenceDocData['stats'] | null | undefined):
  Pick<EvidenceTally, 'total' | 'cited' | 'none' | 'other'> | null {
  const total = num(stats?.numbers)
  const cited = num(stats?.numbers_cited)
  if (total == null || cited == null) return null
  const none = Math.max(0, total - cited)
  const other = Math.max(0, (num(stats?.uncited_numbers) ?? 0) + (num(stats?.unresolved) ?? 0) - none)
  return { total, cited, none, other }
}

/** 读屏摘要：计数、结论句的判定、画不了线的两种各在哪、按键说明 */
export function tallySummary(t: EvidenceTally, keys: boolean): string {
  return [
    evidenceTally(t.cited, t.total, t.other),
    t.claims?.total ? `${claimTally(t.claims)}（模型判断，非确定）` : '',
    t.suspect ? `${formatNumber(t.suspect)} 个${EVIDENCE_STATE.suspect.label}` : '',
    t.unverified ? `${formatNumber(t.unverified)} 个名字${EVIDENCE_STATE.unverified.label}` : '',
    t.structural ? EVIDENCE_TEXT.structuralCount(t.structural) : '',
    t.noSegment ? EVIDENCE_TEXT.noSegmentCount(t.noSegment) : '',
    keys ? (t.claims?.unsupported || t.claims?.partial ? JUDGE_TEXT.keysHint : EVIDENCE_TEXT.keysHint) : '',
  ].filter(Boolean).join('。')
}

/**
 * 证据接口答的是不是正文这份报告。
 *
 * 正文是按成果上 _evidence.doc_artifact 取的——成果是可以改写的一列，工件表可以事后插行，
 * 都不可信；片段接口只从封存范围内的 report.checked 找文档。两边不是同一份时，接口给的
 * 封存状态和证据链说的是另一份报告，挂到正文上就成了「已封存 · 核对一致」地证明一段
 * 别的字。对得上的条件：接口报的文档工件 id、报告节点、片段 id、这个位置的字都和正文一致
 * （接口没给的那一项不比）。
 */
export function docForeign(
  detail: EvidenceSegmentDetail | undefined,
  mine: { artifact?: string; node?: string; seg: Pick<EvidenceSegment, 'id' | 'text'> },
): { sealedText?: string } | null {
  if (!detail) return null
  const theirs = detail.report?.doc_artifact
  const node = detail.report?.node_id
  const text = detail.segment?.text
  const id = detail.segment?.id
  const differs = (a: unknown, b: unknown) => typeof a === 'string' && !!a && typeof b === 'string' && !!b && a !== b
  const textOff = typeof text === 'string' && text !== mine.seg.text
  if (differs(theirs, mine.artifact) || differs(node, mine.node) || differs(id, mine.seg.id) || textOff) {
    return textOff ? { sealedText: text } : {}
  }
  return null
}

/**
 * 证据图（片段接口取不到时的兜底）里有没有正文这份报告。有 reports 列表、却找不到这个
 * 工件：正文不是封存的那一份。找到了但哈希对不上：文档被改过。不知道正文的工件 id、
 * 证据图没有列表时不下结论
 */
export function graphDoc(
  graph: EvidenceGraph | undefined, artifact: string | undefined,
): 'ok' | 'foreign' | 'tampered' | null {
  if (!graph || !artifact || !Array.isArray(graph.reports)) return null
  const hit = graph.reports.find((r) => r?.doc_artifact === artifact)
  if (!hit) return graph.reports.length ? 'foreign' : null
  return hit.hash_ok === false ? 'tampered' : 'ok'
}

export type SealStatus = 'idle' | 'waiting' | 'failed' | 'done'

/**
 * 封存那一行说什么。顺序有讲究：后端的 covered = 封存核对通过 && 每步都在台账里，
 * 所以核对失败（sealed 且 ok=false）的时候 covered 一定也是 false——先看 covered 的话，
 * 被改过的封存永远显示成琥珀色的「不在封存范围内」，红色的「核对不一致」一次都画不出来。
 *
 * - foreign：正文不是封存的那份报告，封存链再完好也证明不了屏幕上的字 → 失败
 * - 没拿到封存状态 → 灰（正在取 / 没有运行 / 拿不到）
 * - 没封存 → 灰「尚未封存」
 * - 封存核对没通过 → 红「核对不一致」
 * - 这件证据不在封存范围内 → 琥珀
 * - 都过了 → 绿；没有证据的片段落到文档上（docOnly），链没取到的落到「只核对了文档」（viaGraph）
 */
export function sealVerdict(seal: EvidenceSeal | undefined, opts: {
  foreign?: boolean; pending?: boolean; noRun?: boolean; docOnly?: boolean; viaGraph?: boolean
} = {}): { status: SealStatus; label: string } {
  if (opts.foreign) return { status: 'failed', label: EVIDENCE_TEXT.sealForeign }
  if (!seal) {
    return { status: 'idle', label: opts.pending ? EVIDENCE_TEXT.chainPending : opts.noRun ? EVIDENCE_TEXT.noRun
      : EVIDENCE_TEXT.sealUnknown }
  }
  if (!seal.sealed) return { status: 'idle', label: EVIDENCE_TEXT.sealOpen }
  if (seal.ok === false) return { status: 'failed', label: EVIDENCE_TEXT.sealBad }
  if (seal.covered === false) return { status: 'waiting', label: EVIDENCE_TEXT.sealOutside }
  return { status: 'done', label: opts.docOnly ? EVIDENCE_TEXT.sealDocOk
    : opts.viaGraph ? EVIDENCE_TEXT.sealDocChain : EVIDENCE_TEXT.sealOk }
}

/**
 * 证据链一步的逐项复核里，哪几项明确没过。「在不在封存范围内」只在封存完好时单独说：
 * 没封存、封存核对失败时后端把每一步的 sealed 都记成 false，那是封存那一行已经说清的
 * 状态，再报一条「事后补进来的」就是冤枉它
 */
export function integrityFailures(
  step: Pick<EvidenceStep, 'eid_ok' | 'hash_ok' | 'render_ok' | 'sealed'> | undefined,
  seal: EvidenceSeal | undefined,
): ('eid' | 'hash' | 'render' | 'sealed')[] {
  if (!step) return []
  const sealTrusted = !!seal?.sealed && seal.ok !== false
  return ([
    ['eid', step.eid_ok], ['hash', step.hash_ok], ['render', step.render_ok],
    ...(sealTrusted ? [['sealed', step.sealed] as const] : []),
  ] as const).filter(([, ok]) => ok === false).map(([k]) => k)
}

/**
 * 成果上的证据标注：字段名 → 它逐字等于的那份报告。最外层是契约 report_from 指着的那份，
 * others 里是别的报告。缺了取文档要的 doc_artifact、没列字段的条目当没有
 */
export function evidenceFields(
  output: Record<string, any> | null | undefined,
): Map<string, { report?: string; artifact: string }> {
  const out = new Map<string, { report?: string; artifact: string }>()
  const ev = output?._evidence
  if (!ev || typeof ev !== 'object') return out
  for (const r of [ev, ...(Array.isArray(ev.others) ? ev.others : [])] as EvidenceFieldRef[]) {
    if (!r || typeof r.doc_artifact !== 'string' || !r.doc_artifact || !Array.isArray(r.fields)) continue
    for (const f of r.fields) {
      if (!out.has(String(f))) out.set(String(f), { report: r.report_node, artifact: r.doc_artifact })
    }
  }
  return out
}

/** 成果带不带逐段证据 */
export const hasEvidence = (output: Record<string, any> | null | undefined): boolean =>
  evidenceFields(output).size > 0

/**
 * 成果带逐段证据时，复核只能加说明、不能改写。改写会把整个成果换成 {answer: 改写后的
 * 文本}，报告文档的每个片段就都对不上了——而证据正是这个答案最值钱的部分。后端
 * review.py 也拦，这里再拦一道：老后端、别的入口回来的改写都不能落到界面上
 */
export function guardReview(
  result: ReviewResult | null, output: Record<string, any> | null | undefined,
): ReviewResult | null {
  if (!result?.answer || !hasEvidence(output)) return result
  return { ...result, answer: null, original: null, verdict: result.verdict === 'rewritten' ? 'annotated' : result.verdict }
}

/**
 * 旧契约（按数值回指）里一条 matched 的出处。出处不唯一的（metric 为 null、带
 * candidates）照实写「出处不唯一：候选 a、b」，不能再写成「来自口径卡指标「」」
 */
export function matchedSource(m: any): { text: string; ambiguous: boolean } {
  const candidates: string[] = Array.isArray(m?.candidates) ? m.candidates.map(String).filter(Boolean) : []
  if (m?.ambiguous || (m?.metric == null && candidates.length)) {
    return { text: EVIDENCE_TEXT.ambiguous(candidates.length ? candidates : ['—']), ambiguous: true }
  }
  return { text: String(m?.metric ?? '').trim(), ambiguous: false }
}

/**
 * 旧契约（按数值回指）在正文上画的标记：回指不上的、回指上的、出处不唯一的。
 * 带 start / end 的只标那一处（Markdown 按位置核对，对不上再退回按字符串标）
 */
export function issuanceMarks(issuance: any): MarkSpec[] | undefined {
  const unmatched: any[] = Array.isArray(issuance?.unmatched_numbers) ? issuance.unmatched_numbers : []
  const matched: any[] = Array.isArray(issuance?.matched) ? issuance.matched : []
  const where = (x: any) => (typeof x?.start === 'number' && typeof x?.end === 'number'
    ? { start: x.start, end: x.end } : {})
  const marks: MarkSpec[] = [
    ...unmatched.map((u) => ({
      token: String(u?.token ?? u),
      tone: 'warn' as const,
      title: `这个数字在口径卡里找不到来源${u?.context ? `：「${u.context}」` : ''}`,
      ...where(u),
    })),
    ...matched.map((m) => {
      const src = matchedSource(m)
      return {
        token: String(m?.token ?? ''),
        tone: src.ambiguous ? 'ambiguous' as const : 'ok' as const,
        title: src.ambiguous ? src.text : `来自口径卡指标「${src.text}」${m?.caliber ? ` · ${m.caliber}` : ''}`,
        ...where(m),
      }
    }),
  ].filter((m) => m.token)
  return marks.length ? marks : undefined
}

/**
 * 码点偏移 → UTF-16 下标。后端的偏移是 Python 的 str 下标（按码点），JS 的字符串下标
 * 按 UTF-16：遇到 BMP 之外的字符（emoji）两边就差一位。没有这类字符时原样返回
 */
export function codePointIndex(text: string): (cp: number) => number {
  if (!/[\uD800-\uDBFF]/.test(text)) return (cp) => cp
  const at: number[] = []
  let i = 0
  for (const ch of text) {
    at.push(i)
    i += ch.length
  }
  at.push(i)
  return (cp) => (cp >= 0 && cp < at.length ? at[cp] : -1)
}

/** 文档里的全部片段，按正文顺序 */
export function segmentsOf(blocks: EvidenceBlock[]): EvidenceSegment[] {
  return blocks.flatMap((b) => (b.units ?? []).flatMap((u) => u.segments ?? []))
}

// -------------------------------------------------------------------------
// 三期：实体步骤、引文步骤、画布上的证据路径
// -------------------------------------------------------------------------

/**
 * 可疑实体的「最接近的已知名字」：接口给目录键（c:orders.amount），或对象 {alias, kind, name, table?}
 * （字段的 name 只是字段名，带着 table 时写成 表.字段）。统一成去掉 t: / c: 前缀的名字，最多 3 个
 */
export function closestNames(list: unknown): string[] {
  if (!Array.isArray(list)) return []
  const names = list.map((x) => {
    if (typeof x === 'string') return x
    if (!x || typeof x !== 'object') return ''
    const o = x as { alias?: unknown; name?: unknown; table?: unknown }
    if (typeof o.alias === 'string' && o.alias) return o.alias
    const name = typeof o.name === 'string' ? o.name : ''
    return typeof o.table === 'string' && o.table && name && !name.includes('.') ? `${o.table}.${name}` : name
  }).map((n) => n.replace(/^[tc]:/, '').trim()).filter(Boolean)
  return [...new Set(names)].slice(0, 3)
}

/** 实体的来历一行一句：「表结构快照」「查询 Q2 · 查询 SQL 用到的表」「查询 Q1 · 查询结果列」 */
export function entitySources(list: unknown): { kind: string; text: string; truncated: boolean }[] {
  if (!Array.isArray(list)) return []
  const out: { kind: string; text: string; truncated: boolean }[] = []
  for (const src of list) {
    if (!src || typeof src !== 'object') continue
    const kind = String((src as any).kind ?? '')
    const alias = typeof (src as any).alias === 'string' ? (src as any).alias : ''
    const what = EVIDENCE_TEXT.entitySource[kind] ?? kind
    const text = [alias ? EVIDENCE_TEXT.query(alias) : '', what].filter(Boolean).join(' · ')
    if (text && !out.some((o) => o.text === text)) out.push({ kind, text, truncated: !!(src as any).truncated })
  }
  return out
}

/**
 * 引文在原文里的位置，切成前、中、后三截给面板高亮。match 是码点偏移（后端 Python 的下标），
 * 先换成 JS 的 UTF-16 下标。对不上（越界、那一截的字和引文对不上）时按引文在原文里找一次，
 * 再找不到就不高亮——宁可不标，不标错地方。原文很长时前后各留 context 个字，多的写省略号
 */
export function quoteWindow(original: string, match: { start?: number; end?: number } | null | undefined,
  quote: string, context = 120): { before: string; hit: string; after: string; cutBefore: boolean; cutAfter: boolean } | null {
  if (!original) return null
  const norm = (t: string) => t.replace(/\s+/g, '')
  const at = codePointIndex(original)
  let a = typeof match?.start === 'number' ? at(match.start) : -1
  let b = typeof match?.end === 'number' ? at(match.end) : -1
  const fits = a >= 0 && b > a && b <= original.length && (!quote || norm(original.slice(a, b)) === norm(quote))
  if (!fits) {
    const i = quote ? original.indexOf(quote) : -1
    if (i < 0) return null
    a = i
    b = i + quote.length
  }
  const from = Math.max(0, a - context)
  const to = Math.min(original.length, b + context)
  return { before: original.slice(from, a), hit: original.slice(a, b), after: original.slice(b, to),
           cutBefore: from > 0, cutAfter: to < original.length }
}

/**
 * 点开一个片段时，画布上该亮哪些节点：产出证据的（查询、口径卡、检索）和用到它的（报告）。
 * 先按文档目录认（打开就有），证据链取回来后再补上链里每一步的节点（口径卡的输入、查询）
 */
export function evidenceTrace(
  seg: EvidenceSegment, doc: Pick<EvidenceDocData, 'catalog' | 'node_id'>, chain: EvidenceStep[] = [],
): { producers: string[]; consumers: string[] } {
  const catalog = doc.catalog ?? {}
  const producers = new Set<string>()
  const add = (id: unknown) => { if (typeof id === 'string' && id) producers.add(id) }
  const entry = seg.cite?.alias ? catalog[seg.cite.alias] : undefined
  add(entry?.node_id)
  // 实体：它出现在哪几次查询里，那几次查询是谁跑的
  const aliases = [
    ...(Array.isArray(entry?.queries) ? entry.queries : []),
    ...(Array.isArray(entry?.sources) ? entry.sources.map((s: any) => s?.alias) : []),
  ]
  for (const a of aliases) if (typeof a === 'string') add(catalog[a]?.node_id)
  for (const step of chain) {
    add(step.node_id)
    for (const inp of step.inputs ?? []) add(inp.node_id)
    for (const q of step.queries ?? []) if (typeof q === 'string') add(catalog[q]?.node_id)
  }
  const consumers = doc.node_id ? [doc.node_id] : []
  for (const c of consumers) producers.delete(c)
  return { producers: [...producers], consumers }
}

// -------------------------------------------------------------------------
// 报告节点卡上的章
// -------------------------------------------------------------------------

/**
 * 章上的两个数：「引用 N」是有出处的片段（数字、值、表名字段名、引文），「无证据 M」是没出处的
 * 数字、解析不了的引用、可疑实体，结论句策略为 require_citation 或 judge 时再加上没挂依据的结论句
 * （judge 在挂依据这件事上和 require_citation 一样严，出口同样计入缺口）。
 * 统计缺了数字那两项（别的版本的后端）返回 null，不画章
 */
export function stampCounts(stats: EvidenceStats | null | undefined, claims?: string | null):
  { cited: number; none: number } | null {
  const t = statsTally(stats)
  if (!t) return null
  const cited = t.cited + (num(stats?.values) ?? 0) + (num(stats?.entities) ?? 0) + (num(stats?.quotes) ?? 0)
  const none = t.none + t.other + (num(stats?.unknown_entities) ?? 0)
    + (claims === 'require_citation' || claims === 'judge' ? num(stats?.uncited_claims) ?? 0 : 0)
  return { cited, none }
}

// -------------------------------------------------------------------------
// 四期：结论句裁判
// -------------------------------------------------------------------------

/** 判过了的四种（不含未裁判）：同后端 judge.JUDGED。判过的不再给「请模型判断这句」 */
const JUDGED = new Set(['supported', 'partial', 'unsupported', 'not_a_claim'])
/** 触顶的四个上限：同后端 judge.LIMITS，这几种没判的写「已到上限」和怎么调 */
export const JUDGE_LIMITS: readonly string[] = ['max_claims', 'max_cost_usd', 'daily_max_usd', 'timeout_s']

export const isJudged = (v: Pick<EvidenceVerdict, 'status'> | null | undefined): boolean => !!v && JUDGED.has(v.status)

/** 没判的原因是不是上限，是的话返回是哪个 */
export const limitOf = (v: Pick<EvidenceVerdict, 'status' | 'reason'> | null | undefined): string | null =>
  v?.status === 'unjudged' && v.reason && JUDGE_LIMITS.includes(v.reason) ? v.reason : null

/**
 * 判定在句末画成哪种徽标。「不是结论句」不挂：它不需要证据，挂个记号只添噪音（面板里照样写明模型的判断）；
 * 认不出的判定也不挂——宁可不画，不能把不知道的东西画成某种结论
 */
export function verdictState(v: Pick<EvidenceVerdict, 'status'> | null | undefined): EvidenceStateCode | null {
  switch (v?.status) {
    case 'supported': return 'supported'
    case 'partial': return 'partial'
    case 'unsupported': return 'unsupported'
    case 'unjudged': return 'unjudged'
    default: return null
  }
}

/** 一句的判定：封存之后按需追加的盖过文档里的（探索运行里文档记的是「未裁判 · 按需」） */
export function unitVerdict(
  unit: Pick<EvidenceUnit, 'id' | 'verdict'>, overlay?: Record<string, EvidenceVerdict> | null,
): EvidenceVerdict | undefined {
  return overlay?.[unit.id] ?? unit.verdict ?? undefined
}

/** 这一句能不能请模型判断：结论句、不在表格和代码块里（同后端 candidates(units=…) 的条件） */
export const judgeable = (unit: Pick<EvidenceUnit, 'kind'>, block?: Pick<EvidenceBlock, 'type'>): boolean =>
  unit.kind === 'claim' && !['table', 'code', 'heading'].includes(block?.type ?? '')

/**
 * 结论句的计数（出具横幅、证据条）。文档开了裁判（有 judge 摘要）时按候选句的判定数，预筛放掉的、
 * 表格里的不算；没开裁判的文档只在有按需判过的句子时才数，没判的按有没有挂依据分成「未裁判」「无证据」。
 * 模型判为「不是结论句」的不算进总数。什么判定都没有返回 null：升级前的文档、没开裁判也没点过的，横幅照旧。
 *
 * 「判过」认的是句子身上带着的判定，不只认 overlay：横幅拿到的是 withVerdicts 叠好的文档（判定已经写进
 * unit.verdict，没再单独给 overlay）。没开裁判的封存文档本身从不带判定，带着的只能是封存后追加的
 */
export function claimCounts(
  doc: Pick<EvidenceDocData, 'blocks' | 'judge'>, overlay?: Record<string, EvidenceVerdict> | null,
): ClaimTallyCounts | null {
  const judged = !!doc.judge && typeof doc.judge === 'object'
  const c: ClaimTallyCounts = { total: 0, supported: 0, partial: 0, unsupported: 0, unjudged: 0, uncited: 0 }
  let seen = judged
  for (const block of doc.blocks ?? []) {
    for (const unit of block.units ?? []) {
      const v = unitVerdict(unit, overlay)
      if (v) seen = true
      if (v) {
        if (v.status === 'supported' || v.status === 'partial' || v.status === 'unsupported' || v.status === 'unjudged') {
          c[v.status] += 1
          c.total += 1
        }
        continue
      }
      if (judged || !judgeable(unit, block)) continue
      if (unit.cites?.length) c.unjudged += 1
      else c.uncited += 1
      c.total += 1
    }
  }
  return seen ? c : null
}

/** 把封存之后追加的判定叠到文档上（只换有判定的那几句，别的块原样共用）。给横幅计数用，不写回封存的文档 */
export function withVerdicts<T extends Pick<EvidenceDocData, 'blocks'>>(
  doc: T, overlay: Record<string, EvidenceVerdict> | null | undefined,
): T {
  if (!overlay || !Object.keys(overlay).length) return doc
  let touched = false
  const blocks = (doc.blocks ?? []).map((b) => {
    if (!(b.units ?? []).some((u) => overlay[u.id])) return b
    touched = true
    return { ...b, units: b.units.map((u) => (overlay[u.id] ? { ...u, verdict: overlay[u.id] } : u)) }
  })
  return touched ? { ...doc, blocks } : doc
}

/** 句子太长时截一截，给 aria-label 和面板标题用 */
const clip = (text: string, n = 40) => (text.length > n ? `${text.slice(0, n - 1)}…` : text)

/**
 * 句末徽标的 aria-label：句子、判定都写全，封存后追加的另说。「结论句「增长主要来自新客首单…」，
 * 模型判断：证据不支持」；到上限没判的说「未裁判：已到上限」
 */
export function claimLabel(unit: Pick<EvidenceUnit, 'segments'>, v: EvidenceVerdict): string {
  const state = verdictState(v)
  const said = state ? EVIDENCE_STATE[state].label : JUDGE_TEXT.notClaim
  const why = limitOf(v) ? `${said}：${JUDGE_TEXT.limit}` : said
  return JUDGE_TEXT.badgeLabel(clip(unitText(unit).trim()), why, !!v.post_seal)
}

/** 一条判定认成前端的形状：status 缺了（或者写在 verdict 里）的认 verdict，缺 judge 的补上答复里的模型 */
function asVerdict(v: unknown, fallback: { model?: unknown; postSeal: boolean }): EvidenceVerdict | null {
  if (!v || typeof v !== 'object') return null
  const o = v as Record<string, unknown>
  const status = typeof o.status === 'string' ? o.status : typeof o.verdict === 'string' ? o.verdict : null
  if (!status) return null
  return {
    ...(o as object), status,
    rationale: typeof o.rationale === 'string' ? o.rationale : undefined,
    judge: typeof o.judge === 'string' ? o.judge : typeof fallback.model === 'string' ? fallback.model : null,
    post_seal: typeof o.post_seal === 'boolean' ? o.post_seal : fallback.postSeal,
    used: Array.isArray(o.used) ? o.used.map(String) : undefined,
    reason: typeof o.reason === 'string' ? o.reason : undefined,
  }
}

/**
 * 按需裁判接口的答复 → {unit: 判定}。认 {verdicts: {u4: …}}（evidence.judged 的载荷）和
 * {verdicts: [{unit: 'u4', …}]}；外层没有 verdicts 时认 units。按需判的都是封存后追加的：缺 post_seal 的记 true
 */
export function judgedVerdicts(body: unknown): Record<string, EvidenceVerdict> {
  if (!body || typeof body !== 'object') return {}
  const b = body as Record<string, unknown>
  const raw = b.verdicts ?? b.units
  const fallback = { model: b.model, postSeal: typeof b.post_seal === 'boolean' ? b.post_seal : true }
  const out: Record<string, EvidenceVerdict> = {}
  if (Array.isArray(raw)) {
    for (const item of raw) {
      const id = item && typeof item === 'object' ? (item as any).unit ?? (item as any).id : null
      const v = asVerdict(item, fallback)
      if (typeof id === 'string' && id && v) out[id] = v
    }
  } else if (raw && typeof raw === 'object') {
    for (const [id, item] of Object.entries(raw)) {
      const v = asVerdict(item, fallback)
      if (v) out[id] = v
    }
  }
  return out
}

/** 证据图里某份报告封存之后追加的判定（reports[].post_seal_verdicts）；后端没给时是空的 */
export function graphVerdicts(graph: EvidenceGraph | null | undefined, report: string | undefined): Record<string, EvidenceVerdict> {
  const hit = (graph?.reports ?? []).find((r) => !report || !r?.node_id || r.node_id === report)
  return hit?.post_seal_verdicts && typeof hit.post_seal_verdicts === 'object'
    ? judgedVerdicts({ verdicts: hit.post_seal_verdicts, post_seal: true }) : {}
}

/**
 * 这一句和「改写一次」（rewrite_once）的关系：改写稿采用了、这句是新写的（changed 里有它），给出交回
 * 改写的原句（units 里对应是 null 的那几句）；改写稿没采用、这句是交回过的，给出没采用的原因。其余 null。
 * 只按 changed / units 认编号——没按这两项映射过的编号在封存文档里指的是别的句子
 */
export function rewriteOf(doc: Pick<EvidenceDocData, 'judge'>, uid: string):
  { kind: 'changed'; sentences: string[] } | { kind: 'rejected'; reason: string } | null {
  const r = doc.judge?.rewrite
  if (!r || typeof r !== 'object') return null
  const units = Array.isArray(r.units) ? r.units : []
  const sentences = Array.isArray(r.sentences) ? r.sentences.map(String) : []
  if (r.applied && Array.isArray(r.changed) && r.changed.includes(uid)) {
    return { kind: 'changed', sentences: sentences.filter((_, i) => units[i] == null) }
  }
  if (!r.applied && typeof r.reason === 'string' && r.reason && units.includes(uid)) return { kind: 'rejected', reason: r.reason }
  return null
}

// -------------------------------------------------------------------------
// 旧运行的猜测
// -------------------------------------------------------------------------

/** 证据图里的猜测（legacy_text）：可能是一份、一个列表、或按字段分的对象。认不出的丢掉 */
export function guessesOf(graph: EvidenceGraph | null | undefined): EvidenceGuess[] {
  if (!graph) return []
  const isGuess = (g: unknown): g is EvidenceGuess =>
    !!g && typeof g === 'object' && Array.isArray((g as EvidenceGuess).segments)
  const from = graph.guess ?? (graph.mode === 'legacy_text' ? graph.legacy : null)
  if (!from) return []
  if (isGuess(from)) return [from]
  if (Array.isArray(from)) return from.filter(isGuess)
  if (typeof from === 'object') {
    // {字段名: guess} 或 {fields: {…}}
    // {note, fields: [{field, markdown, segments, stats}]}（后端 legacy_text 的样子），或 {字段名: guess}
    const note = typeof (from as any).note === 'string' ? (from as any).note : undefined
    const inner = (from as any).fields && typeof (from as any).fields === 'object' ? (from as any).fields : from
    if (Array.isArray(inner)) return inner.filter(isGuess).map((g) => ({ note, ...g }))
    return Object.entries(inner).filter(([, g]) => isGuess(g)).map(([field, g]) => ({ note, field, ...(g as EvidenceGuess) }))
  }
  return []
}

/** 一个候选说一句：「查询 Q1 · 第 1 行 · gmv」「口径卡指标 销售额」，差值不为 0 时补一句 */
export function candidateText(c: EvidenceCandidate): string {
  const loc = c.locator ?? {}
  let where = ''
  if (c.kind === 'metric' || typeof loc.metric === 'string') {
    const name = c.name ?? loc.metric ?? c.ref?.replace(/^m:/, '') ?? ''
    where = `${EVIDENCE_KIND_LABEL.metric} ${name}`.trim()
  } else {
    const alias = c.alias ?? (typeof c.ref === 'string' ? c.ref.split('.')[0] : '')
    where = [alias ? EVIDENCE_TEXT.query(alias) : EVIDENCE_KIND_LABEL.cell, locatorText(loc)].filter(Boolean).join(' · ')
  }
  const diff = typeof c.diff === 'number' && Number.isFinite(c.diff) && c.diff !== 0
    ? EVIDENCE_TEXT.guessDiff(evidenceValue(Math.abs(c.diff))) : ''
  return [where, c.rendered && c.rendered !== NONE ? `= ${c.rendered}` : '', diff].filter(Boolean).join(' ')
}

// -------------------------------------------------------------------------
// 记录页的审计表
// -------------------------------------------------------------------------

/** 分组的键和后端 /evidence/audit 一致：有出处 / 无证据 / 可疑实体 / 旧运行猜测 */
export type AuditGroup = 'cited' | 'none' | 'suspicious' | 'candidate'
/** 表里分组的顺序：有问题的在前（正常态安静，异常态醒目）。导出的文件按后端的顺序 */
export const AUDIT_GROUPS: readonly AuditGroup[] = ['none', 'suspicious', 'cited', 'candidate']
export type AuditFilter = 'all' | 'problems'
/** 「只看无证据 / 可疑实体」对应的分组，也是导出时 ?groups= 的值 */
export const PROBLEM_GROUPS: readonly AuditGroup[] = ['none', 'suspicious']

export interface AuditRow {
  /**
   * 行的键，一张表里各不相同（React 的 key，也是键盘走表时找行的依据）：片段的行 `报告:片段`；
   * 违规、没挂依据的结论句 `报告:片段:问题:位置`——同一段文字里可以有好几条同样的违规（粗体、链接里
   * 两个可疑名字），片段和问题都一样，只有位置不同。万一还撞上，后来的带 `#序号`
   */
  key: string
  /** 报告节点 id；旧运行的猜测是成果字段名 */
  report: string
  /** 能在正文里打开的片段 id */
  seg?: string
  text: string
  state: EvidenceStateCode
  group: AuditGroup
  /** number / value / entity / quote / claim / violation */
  kind: string
  /** 出处（有出处的）或原因（没有的） */
  source: string
  ref?: string
  /** 所在的句子 */
  sentence: string
  /** 这件证据在不在封存范围内（封存核对也通过）；没有证据的是 null */
  sealed: boolean | null
}

const GROUP_OF: Record<EvidenceStateCode, AuditGroup> = {
  deterministic: 'cited', none: 'none', suspect: 'suspicious', unverified: 'suspicious', candidate: 'candidate',
  supported: 'cited', partial: 'none', unsupported: 'none', unjudged: 'cited', connective: 'cited',
}
const AUDIT_GROUP_SET = new Set<string>(['cited', 'none', 'suspicious', 'candidate'])

/** 反引号里的名字去掉反引号（后端审计行的 text 照正文原样带着） */
const bareName = (kind: string, text: string) => (kind === 'entity' && /^`[^`]+`$/.test(text) ? text.slice(1, -1) : text)

/**
 * 后端 /evidence/audit 的 JSON → 表里的行。形状照 api/evidence.py（groups[].rows[]），缺字段的宽容：
 * 状态按 state + issue 认（可疑实体、核对不了是无证据的两个变体），出处写 evidence，原因写 note
 */
export function auditFromApi(body: unknown): AuditRow[] {
  const groups = body && typeof body === 'object' && Array.isArray((body as any).groups) ? (body as any).groups : []
  const rows: AuditRow[] = []
  const taken = new Map<string, number>()
  /** 后端每条违规一行：同一片段、同一个 code 的违规可以有好几条，键带上位置；还撞的按出现顺序编号 */
  const unique = (base: string) => {
    const n = taken.get(base) ?? 0
    taken.set(base, n + 1)
    return n ? `${base}#${n}` : base
  }
  groups.forEach((g: any, gi: number) => {
    for (const [ri, r] of (Array.isArray(g?.rows) ? g.rows : []).entries()) {
      if (!r || typeof r !== 'object') continue
      const kind = String(r.kind ?? '')
      const state = segmentState({ kind, state: r.state ?? 'none', issue: r.issue ?? undefined })
        ?? (r.group === 'candidate' ? 'candidate' : 'none')
      const group: AuditGroup = AUDIT_GROUP_SET.has(r.group) ? r.group : AUDIT_GROUP_SET.has(g?.key) ? g.key : GROUP_OF[state]
      const report = String(r.report ?? r.field ?? '')
      const seg = typeof r.segment === 'string' && r.segment ? r.segment : undefined
      const at = Array.isArray(r.span) && Number.isInteger(r.span[0]) ? String(r.span[0]) : `${gi}.${ri}`
      rows.push({
        key: unique(seg && kind !== 'violation' && kind !== 'claim' ? `${report}:${seg}`
          : `${report}:${seg ?? '-'}:${r.issue ?? kind}:${at}`),
        report, seg: kind === 'claim' ? undefined : seg,
        text: bareName(kind, String(r.text ?? '')) || NONE, state, group, kind,
        source: String((group === 'cited' || group === 'candidate') && r.evidence ? r.evidence : r.note ?? r.evidence ?? ''),
        ref: typeof r.ref === 'string' ? r.ref : undefined,
        sentence: String(r.sentence ?? '').trim(),
        sealed: typeof r.sealed === 'boolean' ? r.sealed : null,
      })
    }
  })
  return rows
}

/**
 * 后端没有审计接口时（老后端），按正文这份文档自己拼：每个有状态的片段一行（连接性文字、结构片段不进），
 * 另加画不了线的违规（列表序号、代码块标签里的数字，句末依据里写错的引用）——键盘用户在表里能看全每一处。
 * 封存状态按证据图里同一份报告、同一个别名的 sealed 取；没有证据图时是 null
 */
export function auditRows(doc: EvidenceDocData, opts: { report?: string; graph?: EvidenceGraph | null } = {}): AuditRow[] {
  const report = opts.report ?? doc.node_id ?? ''
  const sealedOf = (alias?: string): boolean | null => {
    if (!alias || !opts.graph?.evidence) return null
    const hit = opts.graph.evidence.find((e) => e?.alias === alias && (!e.report || !report || e.report === report))
    return typeof hit?.sealed === 'boolean' ? hit.sealed : null
  }
  const rows: AuditRow[] = []
  const shown = new Set<string>()
  for (const block of doc.blocks ?? []) {
    for (const unit of block.units ?? []) {
      const sentence = unitText(unit).trim()
      for (const seg of unit.segments ?? []) {
        const state = segmentState(seg)
        if (!state) continue
        shown.add(seg.id)
        const bad = state === 'none' || state === 'suspect' || state === 'unverified'
        rows.push({
          key: `${report}:${seg.id}`, report, seg: seg.id, text: segName(seg), state, group: GROUP_OF[state],
          kind: seg.kind, source: bad ? reasonOf(seg) : sourceOf(seg, doc), ref: seg.ref, sentence,
          sealed: bad ? null : sealedOf(seg.cite?.alias),
        })
      }
    }
  }
  ;(doc.violations ?? []).forEach((v, i) => {
    if (!GAP_CODES.has(v.code) && v.code !== 'unknown_entity' && v.code !== 'unverified_entity') return
    // 画得出线的违规已经是上面的一行；粗体、链接里的可疑名字没切片段（指着一段文字），也在这里补上
    if (v.segment && shown.has(v.segment)) return
    const state: EvidenceStateCode = v.code === 'unknown_entity' ? 'suspect' : v.code === 'unverified_entity' ? 'unverified' : 'none'
    rows.push({
      key: `${report}:v${i}`, report, text: v.text ?? (v.ref ? `[[${v.ref}]]` : NONE), state, group: GROUP_OF[state],
      // 「正文里画不了线」由表格按行能不能打开来写（后端的行也一样），这里只放违规本身的话
      kind: 'violation', source: v.message ?? '', ref: v.ref,
      sentence: (v.context ?? '').trim(), sealed: null,
    })
  })
  return rows
}

/** 旧运行的猜测 → 表里的行：有候选的数字进「旧运行猜测」，没有的进「无证据」（和后端一致） */
export function guessRows(guess: EvidenceGuess): AuditRow[] {
  const field = guess.field ?? ''
  return (guess.segments ?? []).filter((s) => s.kind === 'number').map((s) => {
    const hit = s.state === 'candidate' && !!s.candidates?.length
    return {
      key: `${field}:${s.id}`, report: field, text: s.text, state: hit ? 'candidate' as const : 'none' as const,
      group: hit ? 'candidate' as const : 'none' as const, kind: 'number',
      source: hit ? (s.candidates ?? []).map(candidateText).join('；') : EVIDENCE_TEXT.guessNone,
      sentence: '', sealed: null,
    }
  })
}

/** 按组归拢；problems 只留无证据和可疑实体两组。空组不返回 */
export function groupRows(rows: AuditRow[], filter: AuditFilter = 'all'): { group: AuditGroup; rows: AuditRow[] }[] {
  const keep = filter === 'problems' ? new Set<AuditGroup>(PROBLEM_GROUPS) : null
  return AUDIT_GROUPS.filter((g) => !keep || keep.has(g))
    .map((group) => ({ group, rows: rows.filter((r) => r.group === group) }))
    .filter((g) => g.rows.length > 0)
}

/** 后端没有导出接口时按表里的行导出的 CSV：中文表头，逗号、引号、换行转义，公式字符开头的格子前面加 ' */
export function auditCsv(rows: AuditRow[]): string {
  const cell = (v: unknown) => {
    let t = v == null ? '' : String(v)
    // 电子表格会把 = + - @ 开头的格子当公式算：报告里的字来自模型，不能照原样写进去（纯数字除外）
    if (/^[=+\-@\t\r]/.test(t) && !/^[+-]?[\d,.]+%?$/.test(t)) t = `'${t}`
    return /[",\n\r]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t
  }
  const c = EVIDENCE_AUDIT_TEXT.cols
  const head = ['分组', c.report, '片段', c.text, c.state, c.source, '引用', c.sentence, c.seal]
  const seal = (v: boolean | null) => (v == null ? '' : v ? '是' : '否')
  return [head, ...rows.map((r) => [
    EVIDENCE_AUDIT_TEXT.groups[r.group], r.report, r.seg ?? '', r.text, EVIDENCE_STATE[r.state].label, r.source,
    r.ref ?? '', r.sentence, seal(r.sealed),
  ])].map((r) => r.map(cell).join(',')).join('\r\n')
}
