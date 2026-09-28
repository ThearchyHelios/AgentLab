/**
 * 可点击证据：片段状态的外观，和几件证据相关的纯函数。
 *
 * 和 lib/status 一个路子，每种状态同时有四条通道：线型、字形、颜色、文字。去掉颜色
 * 还能靠线型和字形分（灰度打印、色弱），去掉线型还能读字（读屏的 aria-label 就是文字
 * 那一条）。颜色只用 --st-* 令牌：概率性的「有依据」借 --st-running（强调色），
 * 「未裁判」借 --st-cancelled（暗字色），「猜测」借 --st-idle（最淡的字色）——
 * 绝不用确定性的绿，模型的判断不能长得像系统核对过的事实。
 *
 * 本期（数字层）实际只会出现两种：确定性（引用解析成功）和无证据（裸数字、引用
 * 不存在）。其余几种先把外观定下，后续期接上时不用再各处补。
 */

import { EVIDENCE_STATE_LABEL, EVIDENCE_TEXT, evidenceTally } from './terms'
import type {
  EvidenceBlock, EvidenceDocData, EvidenceFieldRef, EvidenceGraph, EvidenceSeal, EvidenceSegment,
  EvidenceSegmentDetail, EvidenceStep, EvidenceUnit, EvidenceViolation, ReviewResult,
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
    hint: '数字由系统从口径卡取值、按口径卡的格式渲染，不是模型写的',
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
    hint: '这句话带了引用，还没请模型判断引用支不支持它',
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
}

/** 图例、检查脚本按这个顺序列 */
export const EVIDENCE_STATES = Object.keys(EVIDENCE_STATE) as EvidenceStateCode[]

/**
 * 片段在正文里的状态。文字、结构片段（行首符号、表格竖线）是 null：不画线、不进键盘顺序。
 * 后端的 probabilistic 在裁判给出结论之前按「未裁判」画
 */
export function segmentState(seg: Pick<EvidenceSegment, 'kind' | 'state'>): EvidenceStateCode | null {
  if (seg.kind === 'structural') return null
  switch (seg.state) {
    case 'deterministic': return 'deterministic'
    case 'none': return 'none'
    case 'probabilistic': return 'unjudged'
    case 'candidate': return 'candidate'
    default: return null
  }
}

/** 一个句子（单元格、列表项）的正文：结构片段不算 */
export function unitText(unit: Pick<EvidenceUnit, 'segments'>): string {
  return (unit.segments ?? []).filter((s) => s.kind !== 'structural').map((s) => s.text).join('')
}

/** 片段的来源说一句：「口径卡指标 环比增幅」「运行输入 week」 */
export function sourceOf(seg: EvidenceSegment, doc?: Pick<EvidenceDocData, 'catalog'> | null): string {
  const cite = seg.cite
  if (!cite) return ''
  const entry = cite.alias ? doc?.catalog?.[cite.alias] : undefined
  if (cite.kind === 'metric' || entry?.kind === 'metric') {
    const name = entry?.name ?? cite.locator?.metric ?? cite.ref ?? ''
    return name ? `口径卡指标 ${name}` : '口径卡指标'
  }
  if (cite.kind === 'input' || entry?.kind === 'input') {
    return `运行输入 ${entry?.locator?.field ?? cite.locator?.field ?? cite.ref ?? ''}`.trim()
  }
  return cite.alias ?? cite.ref ?? ''
}

/** 无证据的原因：裸数字，还是引用解析不了（带后端给的人话原因） */
export function reasonOf(seg: EvidenceSegment): string {
  if (seg.issue === 'uncited_number' || (!seg.cite && seg.kind === 'number')) return EVIDENCE_TEXT.uncited
  return seg.cite?.reason ?? (seg.ref ? `引用 ${seg.ref} 解析不了` : '')
}

/**
 * 片段的 aria-label：字、状态、出处都写全。「8.7%，有出处：口径卡指标 环比增幅」
 * 「12，无证据：这个数字没有写成引用标记…」。读屏用户靠它，不靠颜色和线型
 */
export function segmentLabel(seg: EvidenceSegment, doc?: Pick<EvidenceDocData, 'catalog'> | null): string {
  const state = segmentState(seg)
  if (!state) return seg.text
  const why = state === 'none' ? reasonOf(seg) : sourceOf(seg, doc)
  return `${seg.text}，${EVIDENCE_STATE[state].label}${why ? `：${why}` : ''}`
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
  return { total, cited, none, other, hidden: hidden.length, structural, noSegment: hidden.length - structural }
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

/** 读屏摘要：计数、画不了线的两种各在哪、按键说明 */
export function tallySummary(t: EvidenceTally, keys: boolean): string {
  return [
    evidenceTally(t.cited, t.total, t.other),
    t.structural ? EVIDENCE_TEXT.structuralCount(t.structural) : '',
    t.noSegment ? EVIDENCE_TEXT.noSegmentCount(t.noSegment) : '',
    keys ? EVIDENCE_TEXT.keysHint : '',
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
