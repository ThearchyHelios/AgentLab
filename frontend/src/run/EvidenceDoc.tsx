import {
  Fragment, forwardRef, memo, useCallback, useEffect, useId, useImperativeHandle, useLayoutEffect, useMemo,
  useRef, useState, type CSSProperties, type FocusEvent as ReactFocusEvent,
  type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent, type ReactNode, type RefObject,
} from 'react'
import { createPortal } from 'react-dom'
import {
  ListChecks, MessageCircleQuestion, ShieldAlert, ShieldCheck, ShieldEllipsis, ShieldMinus, ShieldX, type LucideIcon,
} from 'lucide-react'
import clsx from 'clsx'
import { rovingTarget } from '../components/ui'
import {
  EVIDENCE_STATE, alertTier, claimCounts, claimLabel, claimProblems, codePointIndex, conflicting, docTally, segmentKind,
  segmentLabel, segmentState, tallySummary, unitText, isJudged, limitOf, unitVerdict, verdictMissing, verdictState, withVerdicts,
  type EvidenceStateCode, type EvidenceTally as Tally,
} from '../lib/evidence'
import { ANALYSIS_TEXT } from '../lib/termsAnalysis'
import { ScopeSection, isClaimViolation } from './EvidenceAnalysis'
import { formatNumber } from '../lib/format'
import {
  CHALLENGE_TEXT, EVIDENCE_STATE_LABEL, EVIDENCE_TEXT, INQUIRY_TEXT, JUDGE_TEXT, LEDGER_TEXT, claimTally, evidenceTally,
  type ClaimTallyCounts,
} from '../lib/terms'
import { useEvidence, useVerdicts, type Slot } from '../store/evidence'
import type {
  ChallengeVerdict, EvidenceBlock, EvidenceChallengeVerdicts, EvidenceDocData, EvidenceGraph, EvidenceSegment, EvidenceUnit,
  EvidenceVerdict,
} from '../types'
import { EvidencePanel, type PanelMode, type PanelView } from './EvidencePanel'
import { askAbout, type InquiryAccess } from './Inquiry'
import { caliberOf, ledgerEnabled } from './LedgerStep'
import { LEDGER_UI } from '../lib/termsLedger'
import { useRunLedger } from '../store/ledger'
import type { LedgerLabel } from '../typesLedger'
import { BlockView, Markdown, StyledSlice, inlineStyles, numericColumns, type InlineStyles } from './Markdown'

/**
 * 报告撰写节点的文档：逐段可点的证据。
 *
 * 按后端切好的块、句、片段画，不再解析 Markdown：片段的边界、每个数字的出处都是后端
 * 算定、进了封存的，前端重新解析一遍只会切出另一套边界。块的外观借 Markdown.tsx 的
 * BlockView，和普通答案长得一模一样；有出处的数字画细实线，没有出处的画点状线、
 * 句末挂「无证据」。
 *
 * 结论句的判定（四期）只在句末挂一枚小徽标（◆ ◇ ! ○ ?），整句不画线；证据相矛盾（和旧的证据不支持）的整句
 * 铺一层浅底。徽标也是按钮：点开是这一句的「模型的解释」。判定是模型给的，颜色一律不用确定性的绿。
 * 被反方审过的句子（期 4，证据图的 reports[].challenge_verdicts）同样在句末挂标记：被推翻、被削弱带字、醒目，经受住、
 * 无法判断只有淡色图标；有判定的句子并在同一枚徽标里。点开都是整句面板，没有可点片段的句子也打得开。证据图有运行就取
 * 一次；手里那份是别处早先取的、已经过期（报告改写过、运行收尾之前取的）时重取一次，见 useRunGraph。
 *
 * 键盘：整份报告只占一个 Tab 位（roving tabindex）。←/→ 在有状态的片段、句末徽标间走，↑/↓ 按
 * 句子走（表格里按列走到上一行 / 下一行，出了表格接着按句子走），n / N 跳到下一处 /
 * 上一处有问题的地方（无证据、可疑名称、证据相矛盾、部分有依据；证据不足的排在这些之后），回车打开证据面板，
 * Esc 关掉并留在原片段；面板开着时方向键走到哪、面板跟到哪，Tab 进面板。
 *
 * 口径账本（next-round 02 做法 3）：来自口径的数字后面挂一个小标记，写明受管还是已发布（悬停写是哪一份口径、怎么认的）；
 * 相关口径的数不加标记，只在证据面板的「口径」一步里说。标签按运行另取（GET /runs/{id}/ledger，store/ledger 按退避
 * 等比对出结果），正式运行、没有运行的文档不取。
 *
 * 分析能力一轮：判定语（[[a:A1]]）是一个系统渲染的片段，和数字一样画「有出处」、点得开（证据面板的分析一步）。说法违规
 * （判断词未挂分析结论、判断与分析结论不符、因果说法没有依据，文档的 violations 按 unit / segment / span 给）标在正文上：判断词
 * 那几个字画琥珀色波浪线，那段文字是一个可点的片段，句末挂「说法待核」，n / N 跳得到；点开看服务端的原话和所挂的结论。正文
 * 后面是系统写的「分析范围」（文档的 analysis_scope），编号点得开。
 *
 * 长报告：事件都挂在容器上（片段只带 data-seg），块用 memo，外加
 * content-visibility: auto；超过 TEXT_CAP 的部分先折叠，跳转到折叠里的片段时自动展开。
 */

export interface EvidenceDocHandle {
  /** 跳到下一处（dir=-1 上一处）无证据并打开面板。一处都画不出来时打开违规清单；什么都没有返回 false */
  next: (dir?: 1 | -1) => boolean
  /** 打开违规清单。没有违规返回 false */
  list: () => boolean
  /** 打开某个片段的面板（记录页的审计表点一行）。focus 为 true 时焦点也挪到正文里那一段；没有这个片段返回 false */
  open: (segId: string, opts?: { focus?: boolean }) => boolean
}

/** 和 AssistantStream 的长文本折叠同一个量级：先显示这么多字，后面的折起来 */
const TEXT_CAP = 3000

/** 句末徽标在键盘顺序里的键：片段 id 是 s0…，不会撞 */
const badgeKey = (unit: string) => `claim:${unit}`
const unitOfKey = (key: string) => (key.startsWith('claim:') ? key.slice(6) : null)
/**
 * 审批质询的「质询」在键盘顺序里的键。只有没有可点片段、也没有徽标的句子，「质询」本身进键盘顺序（方向键走得到这一句）；
 * 别的句子的「质询」跟在这一句后面，焦点在这一句上时按 Tab 到达，不多占键盘顺序
 */
const askKey = (unit: string) => `ask:${unit}`
const unitOfAsk = (key: string) => (key.startsWith('ask:') ? key.slice(4) : null)

/**
 * 哪些句子挂「质询」：结论句（老文档没有 kind 的按有字的句子），标题、代码块里的不挂。表格里的格子也不挂：每格一个
 * 图标会把列撑宽、满表都是图标；问表里的数可以划选那一格，或者就整份报告提问
 */
const askable = (unit: EvidenceUnit, block: EvidenceBlock) =>
  (unit.kind ? unit.kind === 'claim' : !!unitText(unit).trim()) && !['heading', 'code', 'hr', 'table'].includes(block.type)

/**
 * 句末徽标：结论句裁判的判定（verdict、state），和 / 或反方审稿的判断（challenge）。两样都有的并在一个徽标里，不挂两个；
 * 只被反方审过的句子（没开裁判的报告、裁判没送的句子）只有 challenge
 */
interface Badge {
  unit: EvidenceUnit; block: number
  verdict?: EvidenceVerdict; state?: EvidenceStateCode
  challenge?: string
}

interface Model {
  /** 可点的片段和句末徽标，按正文顺序 */
  order: string[]
  index: Map<string, number>
  seg: Map<string, { seg: EvidenceSegment; unit: EvidenceUnit; block: number }>
  /** 句末徽标：键是 claim:<unit>，带着这一句眼下的判定 */
  badge: Map<string, Badge>
  /** 进了键盘顺序的「质询」（审批质询）：键是 ask:<unit> */
  ask: Map<string, { unit: EvidenceUnit; block: number }>
  /** 每个有可点片段的句子里第一个可点片段，按正文顺序（↑/↓ 用） */
  unitFirst: string[]
  unitOf: Map<string, number>
  /**
   * 有问题的片段和句末徽标（n / N 用）：先第 1 轮（无证据、可疑名称、证据相矛盾、部分有依据），再第 2 轮
   * （证据不足），每一轮里按正文顺序
   */
  alerts: string[]
  /** alerts 里每一处在第几轮 */
  tier: Map<string, 1 | 2>
  /** 每块到它为止的累计字数（折叠用） */
  chars: number[]
  /** 表格里的片段在第几行第几列（表头 row = -1 不进来）；↑/↓ 在表格里按列走 */
  cell: Map<string, { block: number; row: number; col: number }>
  /** `块:行:列` → 那一格里第一个可点的片段 */
  grid: Map<string, string>
}

/**
 * 说法违规落在哪段文字上：片段 id → 违规的种类、判断词在这段文字里的位置（UTF-16 下标，[起, 止)）。服务端的 span 是全文的码点
 * 位置，片段自己的 span 也是，相减后按码点换成下标；没给 span 的整段都算。只认说法违规（isClaimViolation）、能落到一段非结构
 * 片段上的
 */
type ClaimMarks = Map<string, { codes: string[]; ranges: [number, number][] }>

export function claimMarksOf(doc: EvidenceDocData): ClaimMarks {
  const out: ClaimMarks = new Map()
  const list = (doc.violations ?? []).filter((v) => isClaimViolation(v) && v.segment)
  if (!list.length) return out
  const segs = new Map<string, EvidenceSegment>()
  for (const b of doc.blocks ?? []) for (const u of b.units ?? []) for (const sg of u.segments ?? []) segs.set(sg.id, sg)
  for (const v of list) {
    const seg = segs.get(v.segment!)
    if (!seg || seg.kind === 'structural' || segmentState(seg)) continue
    const mark = out.get(seg.id) ?? { codes: [], ranges: [] }
    if (!mark.codes.includes(v.code)) mark.codes.push(v.code)
    const at = codePointIndex(seg.text)
    const from = v.span && seg.span ? v.span[0] - seg.span[0] : 0
    const to = v.span && seg.span ? v.span[1] - seg.span[0] : Infinity
    const a = Math.max(0, Math.min(seg.text.length, at(Math.max(0, from))))
    const z = Math.max(a, Math.min(seg.text.length, to === Infinity ? seg.text.length : at(Math.max(0, to))))
    if (z > a) mark.ranges.push([a, z])
    out.set(seg.id, mark)
  }
  for (const m of out.values()) {
    // 有重叠的合在一起；一处位置都没认出来的整段画线
    m.ranges.sort((x, y) => x[0] - y[0])
    m.ranges = m.ranges.reduce<[number, number][]>((acc, r) => {
      const last = acc[acc.length - 1]
      if (last && r[0] <= last[1]) last[1] = Math.max(last[1], r[1])
      else acc.push([...r])
      return acc
    }, [])
  }
  return out
}

function buildModel(blocks: EvidenceBlock[], overlay?: Record<string, EvidenceVerdict>, ask = false,
  marks?: Record<string, string>, claims?: ClaimMarks): Model {
  const order: string[] = []
  const index = new Map<string, number>()
  const seg = new Map<string, { seg: EvidenceSegment; unit: EvidenceUnit; block: number }>()
  const badge = new Map<string, Badge>()
  const asks = new Map<string, { unit: EvidenceUnit; block: number }>()
  const unitFirst: string[] = []
  const unitOf = new Map<string, number>()
  const tiered: [string, 1 | 2][] = []
  const chars: number[] = []
  const cell = new Map<string, { block: number; row: number; col: number }>()
  const grid = new Map<string, string>()
  let total = 0
  blocks.forEach((block, b) => {
    for (const unit of block.units ?? []) {
      let first = true
      const loc = block.type === 'table' && unit.loc && unit.loc.row >= 0 ? unit.loc : null
      for (const s of unit.segments ?? []) {
        if (s.kind !== 'structural') total += s.text.length
        const state = segmentState(s)
        // 说法违规落在的那段文字：本身没有状态，也进键盘顺序，算第 1 轮的问题（n / N 跳得到）
        const claimed = !state && !!claims?.has(s.id)
        if (!state && !claimed) continue
        seg.set(s.id, { seg: s, unit, block: b })
        index.set(s.id, order.length)
        order.push(s.id)
        if (first) { unitFirst.push(s.id); first = false }
        unitOf.set(s.id, unitFirst.length - 1)
        const t = claimed ? 1 : alertTier(state)
        if (t) tiered.push([s.id, t])
        if (loc) {
          cell.set(s.id, { block: b, row: loc.row, col: loc.col })
          const key = `${b}:${loc.row}:${loc.col}`
          if (!grid.has(key)) grid.set(key, s.id)
        }
      }
      // 句末徽标排在这一句的片段后面：←/→ 走到句尾就是它，证据相矛盾、部分有依据、证据不足的也进 n / N。被反方审过的
      // 句子同样挂（和裁判的判定并在一个徽标里）：没有可点的片段、也没有判定的句子靠它才打开得了整句面板。n / N 只按
      // 裁判的判定走——横幅、证据条的「定位下一处」只数证据和判定的问题，反方的判断在面板和审批卡里看
      const verdict = unitVerdict(unit, overlay)
      const state = verdictState(verdict)
      const challenge = marks?.[unit.id]
      if ((verdict && state) || challenge) {
        const key = badgeKey(unit.id)
        badge.set(key, { unit, block: b, ...(verdict && state ? { verdict, state } : {}), ...(challenge ? { challenge } : {}) })
        index.set(key, order.length)
        order.push(key)
        if (first) { unitFirst.push(key); first = false }
        unitOf.set(key, unitFirst.length - 1)
        const t = alertTier(state)
        if (t) tiered.push([key, t])
      }
      // 审批质询：一个可点的都没有的结论句，「质询」本身进键盘顺序，不然键盘用户问不了这一句
      if (ask && first && askable(unit, block)) {
        const key = askKey(unit.id)
        asks.set(key, { unit, block: b })
        index.set(key, order.length)
        order.push(key)
        unitFirst.push(key)
        unitOf.set(key, unitFirst.length - 1)
      }
    }
    chars.push(total)
  })
  // 按（轮次、正文顺序）排：tiered 本来就是正文顺序，稳定排序只按轮次挪
  const sorted = [...tiered].sort((a, b) => a[1] - b[1])
  return { order, index, seg, badge, ask: asks, unitFirst, unitOf, alerts: sorted.map(([k]) => k), tier: new Map(sorted), chars,
           cell, grid }
}

/** 片段或徽标在第几块 */
const blockOf = (model: Model, key: string | null | undefined): number =>
  (key ? model.seg.get(key)?.block ?? model.badge.get(key)?.block ?? model.ask.get(key)?.block : undefined) ?? -1

/**
 * ↑/↓ 该去哪。表格里按列走：下一行同一列（那一格空着就找这一行离它最近的一格）；
 * 走出表格的上下边，接着按句子走到表格前后那一句——不在表格里一格一格地横着挪
 */
function verticalTarget(model: Model, id: string, down: boolean): string | null {
  const n = model.unitFirst.length
  if (!n) return null
  const at = model.cell.get(id)
  if (at) {
    const rows = [...new Set([...model.cell.values()].filter((c) => c.block === at.block).map((c) => c.row))].sort((a, b) => a - b)
    const next = rows[rows.indexOf(at.row) + (down ? 1 : -1)]
    if (next !== undefined) {
      const same = model.grid.get(`${at.block}:${next}:${at.col}`)
      if (same) return same
      const near = [...model.cell.entries()].filter(([, c]) => c.block === at.block && c.row === next)
        .sort((a, b) => Math.abs(a[1].col - at.col) - Math.abs(b[1].col - at.col))[0]
      if (near) return near[0]
    }
    // 出了表格：往下找表格之后的第一句，往上找表格之前的最后一句
    const u = model.unitOf.get(id) ?? 0
    const list = down ? model.unitFirst.slice(u + 1) : model.unitFirst.slice(0, u).reverse()
    const out = list.find((sid) => blockOf(model, sid) !== at.block)
    if (out) return out
    // 表格就是整份报告的首尾：和句子一样首尾相接
    const wrap = down ? model.unitFirst : [...model.unitFirst].reverse()
    return wrap.find((sid) => blockOf(model, sid) !== at.block) ?? null
  }
  const u = model.unitOf.get(id) ?? 0
  return model.unitFirst[(u + (down ? 1 : -1) + n) % n] ?? null
}

type GraphReport = NonNullable<EvidenceGraph['reports']>[number]

/**
 * 证据图里的这一项说的是不是正文这一份：同一个报告节点，封存的就是这一稿（工件 id 对得上）。证据图说的是这个报告节点最后
 * 写的那一稿，正文是另一稿时同一个句子编号说的可能是别的话。正文没记报告节点、没给工件 id 的（预览、老文档）不按它比
 */
const sameReport = (r: GraphReport | undefined, report: string | undefined, artifact: string | undefined) =>
  (!report || !r?.node_id || r.node_id === report) && (!artifact || r?.doc_artifact === artifact)

/**
 * 这次运行的证据图（正文的反方标记、按需判过的句子都从这里来）：有运行就取一次，同一次运行只取一回，不逐句请求。
 *
 * 挂上（或换了一稿）那一刻手里已经有的那一份是别处早先取的，可能过期了。审批卡里展开过报告，取的就是运行停在审批上那一刻
 * 的图：之后报告改写了（第二次审批、同一张卡换稿），正文这一稿在那份图里找不到，一个标记都挂不上；正文是运行的成果
 * （settled）而那份图没封存，审批之后才审完的反方审稿不在里面。这两种按（运行、报告、工件）重取一次，重取回来还是这样
 * 就作罢（正文是改写前的原件时本来就对不上），不反复请求。挂上时还没有、正在取的那一份是这时候取的，不算过期。证据页自己
 * 管重取（pages/runs/EvidencePane）
 */
function useRunGraph(runId: string | undefined, report: string | undefined, artifact: string | undefined, settled: boolean):
  EvidenceGraph | undefined {
  const slot = useEvidence((s) => (runId ? s.graphs[runId] : undefined))
  const loadGraph = useEvidence((s) => s.loadGraph)
  const reloadGraph = useEvidence((s) => s.reloadGraph)
  const key = runId ? `${runId}\u0000${report ?? ''}\u0000${artifact ?? ''}` : ''
  // 挂上、换了一稿那一刻手里已经取到的那一份：只有它可能过期，看过一次就放下
  const held = useRef<{ key: string; slot: Slot<EvidenceGraph> | null }>({ key: '', slot: null })
  useEffect(() => {
    if (!runId || held.current.key === key) return
    const cur = useEvidence.getState().graphs[runId]
    held.current = { key, slot: cur?.status === 'ok' ? cur : null }
    void loadGraph(runId)
  }, [runId, key, loadGraph])
  useEffect(() => {
    const h = held.current
    if (!runId || h.key !== key || !h.slot || slot !== h.slot) return
    h.slot = null
    const graph = slot.data
    const mine = (graph?.reports ?? []).some((r) => sameReport(r, report, artifact))
    if (!mine || (settled && !graph?.seal?.sealed)) void reloadGraph(runId)
  }, [runId, key, slot, report, artifact, settled, reloadGraph])
  return slot?.data
}

/**
 * 这份报告里被反方审过的句子 → 判断（证据图 reports[].challenge_verdicts），只认说的就是正文这一份的那一项（sameReport）。
 * 没有运行、证据图还没取到（或取不到）、一句都没审、老服务端都是 undefined
 */
function challengeMarks(graph: EvidenceGraph | undefined, report: string | undefined, artifact: string | undefined):
  EvidenceChallengeVerdicts | undefined {
  const hit = (graph?.reports ?? []).find((r) => sameReport(r, report, artifact))
  const raw: unknown = hit?.challenge_verdicts
  if (!raw || typeof raw !== 'object') return undefined
  const marks = Object.fromEntries(Object.entries(raw as Record<string, unknown>)
    .filter((e): e is [string, string] => !!e[0] && typeof e[1] === 'string' && !!e[1]))
  return Object.keys(marks).length ? marks : undefined
}

/** 句末标记读屏念的那一句：截到 40 字（同 lib/evidence 的 claimLabel） */
const markSentence = (unit: EvidenceUnit) => {
  const text = unitText(unit).trim()
  return text.length > 40 ? `${text.slice(0, 39)}…` : text
}

/** 反方审稿判断的叫法：认不出的照写原值（同审批卡） */
const challengeLabel = (v: string) => (Object.hasOwn(CHALLENGE_TEXT.verdict, v)
  ? CHALLENGE_TEXT.verdict[v as keyof typeof CHALLENGE_TEXT.verdict] : CHALLENGE_TEXT.verdictUnknown(v))

/**
 * 句末徽标的读屏名：有判定的照裁判的说法（claimLabel），被反方审过的接着说反方审稿的判断；只被反方审过的
 * 说「结论句「…」，反方审稿：被推翻」
 */
function badgeLabel(badge: Badge): string {
  const said = badge.challenge ? CHALLENGE_TEXT.markLabel(challengeLabel(badge.challenge)) : ''
  if (!badge.verdict) return JUDGE_TEXT.badgeLabel(markSentence(badge.unit), said, false)
  const judged = claimLabel(badge.unit, badge.verdict)
  return said ? `${judged}，${said}` : judged
}

/** 片段的外观：线型走 data 属性（CSS 里只管线型），颜色写成变量交给 index.css（引文前的引号用 --ev-glyph） */
function segStyle(state: EvidenceStateCode): CSSProperties {
  const meta = EVIDENCE_STATE[state]
  return { '--ev-line': meta.decoration, '--ev-soft': meta.soft, '--ev-glyph': meta.color } as CSSProperties
}

/** 句末小标签：哪几种异常态在句末挂字形加文字（去掉颜色也读得出）。可疑名字和无证据分开挂 */
const TAG_TEXT: Partial<Record<EvidenceStateCode, string>> = {
  none: EVIDENCE_STATE_LABEL.none,
  suspect: EVIDENCE_TEXT.suspectTag,
}

export const EvidenceDoc = forwardRef<EvidenceDocHandle, {
  doc: EvidenceDocData
  /**
   * 这份文档的工件 id（成果上 _evidence.doc_artifact 指的那件）。证据接口会报它答的是哪份
   * 封存的文档，两边不一致时面板不信接口给的链和封存状态
   */
  artifact?: string
  /** 属于哪次运行。有它才能取片段的证据链和封存状态；没有（预览、导出的文档）只看文档里记下的出处 */
  runId?: string
  dense?: boolean
  /** 成果字段名，读屏用 */
  label?: string
  /**
   * 面板放哪：窄栏里栏内展开，宽屏从右侧弹出，窄屏从底部抽出，dock 放进页面给的 dock 元素里（记录页
   * 的常驻面板）。auto 按 dense 和屏宽定
   */
  panel?: 'auto' | PanelMode
  /** panel 为 dock 时面板挂在这个元素里 */
  dock?: HTMLElement | null
  /** 面板开了、关了（dock 的空位据此显示提示） */
  onView?: (open: boolean) => void
  /** 自己显示「N/N 数字有出处」那一条。上面已经有出具横幅（它会写这一行）时不用 */
  tally?: boolean
  /**
   * 运行类别（formal / exploratory）：「请模型判断这句」只在探索运行里有。开了裁判的文档自己记着
   * （on_demand / inline），没开的看这里，不给就按运行 id 取一次
   */
  runClass?: string
  /**
   * 这次出具的档位（output._issuance.tier）。不是完整出具时，「N 个数字都有出处」不画成绿色：
   * 数字的出处都对得上，档位却因为别的原因（比如查询没通过 SQL 检查）降了
   */
  tier?: string
  /**
   * 正文是运行的成果（成果区、问数据的回答，EvidenceField 给）：运行已经结束。手里那份证据图要是没封存，就是收尾之前取的
   * （比如审批卡里展开报告时），重取一次，见 useRunGraph
   */
  settled?: boolean
  /**
   * 审批质询（期 4）：给了就在每句结论后面挂「质询」（悬停、焦点在这一句上时出现），划选原文时浮出「质询」，点了打开
   * 这份报告的提问框。能不能问由页面按运行状态定（审批卡：运行停在审批上；运行详情：封存了的探索运行），approvalId 是
   * 运行停在的那张审批。给了它，页面上要同时挂这份报告的 ReportInquiry（问答串和提问框在那里）
   */
  inquiry?: InquiryAccess | null
}>(function EvidenceDoc({ doc, artifact, runId, dense = false, label, panel = 'auto', dock, onView, tally = false, runClass,
  tier, settled = false, inquiry }, ref) {
  const blocks = useMemo(() => doc.blocks ?? [], [doc])
  const report = doc.node_id || undefined
  // 质询要知道是哪次运行的哪份报告：两样缺一样就不挂入口
  const ask = !!inquiry && !!runId && !!report
  // 封存之后按需追加的判定：盖过文档里的「未裁判 · 按需」。只换句末徽标，封存的文档一个字不改
  const overlay = useVerdicts(runId, report)
  // 有运行就取这次运行的证据图（同一次运行只取一次，不逐句请求；过期了重取一次）：它带着被反方审过的句子
  // （reports[].challenge_verdicts，正式、探索运行都可能有，反方审稿在报告写完之后才审，文档里没有这份记录），探索运行的
  // 报告还带着之前按需判过的句子（reports[].post_seal_verdicts，useVerdicts 读），重新打开页面时标记、徽标都还在
  const graph = useRunGraph(runId, report, artifact, settled)
  // 被反方审过的句子：句末挂标记（和裁判的判定并在一个徽标里），点开是整句面板
  const marks = useMemo(() => challengeMarks(graph, report, artifact), [graph, report, artifact])
  // 说法违规标在正文上：判断词画线、那段文字可点（文档的 violations，服务端核出来的）
  const claimMarks = useMemo(() => claimMarksOf(doc), [doc])
  const model = useMemo(() => buildModel(blocks, overlay, ask, marks, claimMarks), [blocks, overlay, ask, marks, claimMarks])
  // 来自口径的数字上的小标记：按运行取的口径账本里这份报告的标签（只画 caliber 一种）
  const ledger = useRunLedger(runId, ledgerEnabled(runId, runClass))
  const labels = report ? ledger?.data?.reports?.[report]?.labels : undefined
  const caliberMarks = useMemo(() => caliberMarksOf(labels), [labels])
  const counts = useMemo(() => docTally(doc), [doc])
  const claims = useMemo(() => claimCounts(doc, overlay), [doc, overlay])
  const rootRef = useRef<HTMLDivElement>(null)
  const instance = useId()
  const panelId = `${instance}-panel`
  const summaryId = `${instance}-summary`
  const [current, setCurrent] = useState<string | null>(() => model.order[0] ?? null)
  const [view, setView] = useState<PanelView | null>(null)
  const [announce, setAnnounce] = useState('')
  const [expanded, setExpanded] = useState(false)
  const pendingFocus = useRef<string | null>(null)
  const mode = usePanelMode(panel, dense)

  // 换了一份文档：焦点位回到第一个片段，面板关掉。只认文档本身——判定回来了（徽标变了）不算换文档，
  // 面板正开着这一句的解释，不能因此收起
  useEffect(() => {
    setCurrent(model.order[0] ?? null)
    setView(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [blocks])
  // 拿着 Tab 位的徽标没了（判成「不是结论句」），或者挂上时一个键盘目标都没有（没有可点片段、没开裁判、没给质询）、
  // 反方标记随证据图后来才到：Tab 位交给第一个，整份报告不能一个 Tab 位都没有
  useEffect(() => {
    if (current ? !model.index.has(current) : model.order.length > 0) setCurrent(model.order[0] ?? null)
  }, [model, current])

  // 同一时刻只开一个面板：另一份报告打开了自己的面板，这边的就收起
  const owner = useEvidence((s) => s.owner)
  const claim = useEvidence((s) => s.claim)
  useEffect(() => {
    if (view && owner && owner !== instance) setView(null)
  }, [owner, instance, view])

  const foldAt = useMemo(() => {
    const total = model.chars[model.chars.length - 1] ?? 0
    if (total <= TEXT_CAP * 1.2) return blocks.length
    const at = model.chars.findIndex((c) => c > TEXT_CAP)
    return Math.max(1, at < 0 ? blocks.length : at)
  }, [model, blocks.length])
  const shownBlocks = expanded ? blocks.length : foldAt
  const totalChars = model.chars[model.chars.length - 1] ?? 0

  const segEl = useCallback((id: string) => {
    const unit = unitOfKey(id)
    const asked = unitOfAsk(id)
    const sel = unit ? `[data-ev-claim="${CSS.escape(unit)}"]`
      : asked ? `[data-inquiry-ask="${CSS.escape(asked)}"]` : `[data-seg="${CSS.escape(id)}"]`
    return rootRef.current?.querySelector<HTMLElement>(sel) ?? null
  }, [])

  /** 把焦点挪到某个片段（或句末徽标）上。在折叠里的先展开，渲染出来再聚焦 */
  const focusSeg = useCallback((id: string, opts?: { flash?: boolean }) => {
    const block = blockOf(model, id)
    if (block < 0) return
    setCurrent(id)
    if (block >= shownBlocks) {
      pendingFocus.current = id
      setExpanded(true)
      return
    }
    const el = segEl(id)
    if (!el) return
    el.focus({ preventScroll: false })
    if (opts?.flash) flashOnce(el)
  }, [model, shownBlocks, segEl])

  useLayoutEffect(() => {
    const id = pendingFocus.current
    if (!id || !expanded) return
    pendingFocus.current = null
    const el = segEl(id)
    if (el) { el.focus(); flashOnce(el) }
  }, [expanded, segEl])

  const openSeg = useCallback((id: string) => {
    const badge = model.badge.get(id)
    if (badge) {
      claim(instance)
      setView({ kind: 'unit', id: badge.unit.id })
      // 只有反方标记的句子：读屏名本身就说了「反方审稿：被推翻」，前面不再加一个标题，免得「反方审稿」念两遍
      setAnnounce(badge.verdict ? `${JUDGE_TEXT.section}：${badgeLabel(badge)}` : badgeLabel(badge))
      return
    }
    const hit = model.seg.get(id)
    if (!hit) return
    claim(instance)
    setView({ kind: 'seg', id })
    setAnnounce(`${EVIDENCE_TEXT.panelTitle}：${segmentLabel(hit.seg, doc)}`)
  }, [model, doc, claim, instance])

  const openViolations = useCallback(() => {
    claim(instance)
    setView({ kind: 'violations' })
    setAnnounce(`${EVIDENCE_TEXT.violations}：${formatNumber(doc.violations?.length ?? 0)} 条`)
  }, [claim, instance, doc])

  /**
   * 打开一份分析结论：分析范围里的编号、说法违规里所挂的结论。正文引用了它的判定语就落到那个片段（看得到上下文），没引用的打开
   * 它自己的出处链（分析范围那一项）
   */
  const openAnalysis = useCallback((alias: string, opts?: { scope?: boolean }) => {
    const phrase = !opts?.scope
      ? [...model.seg.values()].find((x) => x.seg.kind === 'analysis' && x.seg.cite?.alias === alias && !x.seg.cite?.locator?.key)
      : undefined
    if (phrase) {
      focusSeg(phrase.seg.id, { flash: true })
      openSeg(phrase.seg.id)
      return
    }
    if (doc.catalog?.[alias]?.kind !== 'analysis') return
    claim(instance)
    setView({ kind: 'scope', alias })
    setAnnounce(`${ANALYSIS_TEXT.evidence.title} ${alias}`)
  }, [model, doc, focusSeg, openSeg, claim, instance])

  const close = useCallback((refocus = true) => {
    const back = view?.kind === 'seg' ? view.id : view?.kind === 'unit' ? badgeKey(view.id)
      : view?.kind === 'scope' ? `scope:${view.alias}` : current
    setView(null)
    setAnnounce('')
    if (refocus && back) requestAnimationFrame(() => segEl(back)?.focus())
  }, [view, current, segEl])

  /**
   * n / N 的下一处：按（轮次、正文位置）排的一圈，走到头接回开头。当前位置不是问题（普通片段、有依据的徽标）时
   * 按第 1 轮算——第 1 轮里它后面没有了，就接着走第 2 轮（证据不足）
   */
  const step = useCallback((list: string[], from: string | null, dir: 1 | -1): string | null => {
    if (!list.length) return null
    const rank = (id: string | null): [number, number] => [id ? model.tier.get(id) ?? 1 : 1, id ? model.index.get(id) ?? -1 : -1]
    const cmp = (a: [number, number], b: [number, number]) => a[0] - b[0] || a[1] - b[1]
    const cur = rank(from)
    if (dir > 0) return list.find((id) => cmp(rank(id), cur) > 0) ?? list[0]
    for (let i = list.length - 1; i >= 0; i--) if (cmp(rank(list[i]), cur) < 0) return list[i]
    return list[list.length - 1]
  }, [model])

  // 面板开关告诉页面（dock 的空位据此显示「点片段看出处」）
  const open = !!view
  useEffect(() => { onView?.(open) }, [open, onView])

  const viewKey = view?.kind === 'seg' ? view.id : view?.kind === 'unit' ? badgeKey(view.id) : null

  useImperativeHandle(ref, () => ({
    open: (id, opts) => {
      if (!model.seg.has(id) && !model.badge.has(id)) return false
      if (opts?.focus) focusSeg(id, { flash: true })
      else setCurrent(id)
      openSeg(id)
      return true
    },
    next: (dir = 1) => {
      const target = step(model.alerts, viewKey ?? current, dir)
      if (target) {
        focusSeg(target, { flash: true })
        openSeg(target)
        return true
      }
      if (doc.violations?.length) { openViolations(); return true }
      return false
    },
    list: () => {
      if (!doc.violations?.length) return false
      openViolations()
      return true
    },
  }), [step, model, viewKey, current, focusSeg, openSeg, openViolations, doc])

  // 事件委托：片段只带 data-seg、句末徽标只带 data-ev-claim，点击和按键都在容器上认。「质询」只有进了键盘顺序的才算
  // （跟在有可点片段的句子后面的那种不算：方向键在它上面不动，Tab 照常离开）
  const segOf = (target: EventTarget | null): string | null => {
    const el = (target as HTMLElement | null)?.closest?.<HTMLElement>('[data-seg], [data-ev-claim], [data-inquiry-ask]')
    if (!el || !rootRef.current?.contains(el)) return null
    if (el.dataset.inquiryAsk) return model.index.has(askKey(el.dataset.inquiryAsk)) ? askKey(el.dataset.inquiryAsk) : null
    return el.dataset.seg ?? (el.dataset.evClaim ? badgeKey(el.dataset.evClaim) : null)
  }
  /** 点了句末的「质询」：打开这份报告的提问框，问的是这一句 */
  const askUnit = (el: HTMLElement) => {
    const id = el.dataset.inquiryAsk
    if (!id || !inquiry || !runId || !report) return
    const unit = blocks.flatMap((b) => b.units ?? []).find((u) => u.id === id)
    if (model.index.has(askKey(id))) setCurrent(askKey(id))
    askAbout(runId, report, inquiry, { unit: id, quote: null, text: unit ? unitText(unit).trim() : '' }, el)
  }
  const onClick = (e: ReactMouseEvent) => {
    const asking = (e.target as HTMLElement | null)?.closest?.<HTMLElement>('[data-inquiry-ask]')
    if (asking && rootRef.current?.contains(asking)) {
      e.preventDefault()
      askUnit(asking)
      return
    }
    const id = segOf(e.target)
    if (!id) return
    e.preventDefault()
    setCurrent(id)
    if (viewKey === id) close(false)
    else openSeg(id)
  }
  const onFocus = (e: ReactFocusEvent) => {
    const id = segOf(e.target)
    if (id && id !== current) setCurrent(id)
  }
  const onKeyDown = (e: ReactKeyboardEvent) => {
    const id = segOf(e.target)
    if (!id || e.altKey || e.metaKey || e.ctrlKey) return
    const i = model.index.get(id) ?? 0
    let target: string | null = null
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight' || e.key === 'Home' || e.key === 'End') {
      const to = rovingTarget(e, i, model.order.length, 'horizontal')
      target = to >= 0 ? model.order[to] : null
    } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      target = verticalTarget(model, id, e.key === 'ArrowDown')
    } else if (e.key === 'n' || e.key === 'N') {
      target = step(model.alerts, id, e.shiftKey || e.key === 'N' ? -1 : 1)
      if (!target) return
    } else if (e.key === 'Escape' && view) {
      e.preventDefault()
      e.stopPropagation()
      close()
      return
    } else if (e.key === 'Tab' && !e.shiftKey && view && mode !== 'inline') {
      // 面板在页面末尾（portal）或页面另一栏（dock）：Tab 直接进面板，不让人穿过整页去找
      const heading = document.getElementById(`${panelId}-title`)
      if (heading) { e.preventDefault(); heading.focus() }
      return
    } else {
      return
    }
    e.preventDefault()
    e.stopPropagation()
    if (!target) return
    focusSeg(target)
    // 面板开着：跟着走，读屏从 live region 听到新片段
    if (view) openSeg(target)
  }

  const activeSeg = viewKey
  const activeBlock = blockOf(model, activeSeg)
  const currentBlock = blockOf(model, current)
  const visual = blocks.length > 40

  const panelNode = view && (
    <EvidencePanel
      id={panelId}
      mode={mode}
      doc={doc}
      artifact={artifact}
      runId={runId}
      runClass={runClass}
      marks={marks}
      view={view}
      onClose={() => close()}
      onLocate={(id) => { focusSeg(id, { flash: true }); openSeg(id) }}
      onViolations={openViolations}
      onAnalysis={(alias) => openAnalysis(alias)}
    />
  )

  // 审批质询：划选一段原文，浮出「质询」，问的是选中的这段（带上它起头的那一句）
  const pickAnchor = useRef<HTMLDivElement>(null)
  const [pick, dropPick] = useSelectionPick(rootRef, pickAnchor, ask)
  const askPick = () => {
    if (!pick || !inquiry || !runId || !report) return
    // 浮出的按钮点完就没了：Esc 之后焦点回到那一句的「质询」
    const back = pick.unit ? rootRef.current?.querySelector<HTMLElement>(`[data-inquiry-ask="${CSS.escape(pick.unit)}"]`) : null
    document.getSelection()?.removeAllRanges()
    dropPick()
    askAbout(runId, report, inquiry, { unit: pick.unit, quote: pick.quote, text: pick.quote }, back)
  }

  // 画不了线的两种分开说：列表序号、代码块标签里的数字，和正文里根本没有对应字的（句末依据里的引用）
  const summary = [tallySummary({ ...counts, claims }, model.order.length > 0), ask ? INQUIRY_TEXT.keysHint : '']
    .filter(Boolean).join('。')

  return (
    <div ref={rootRef} data-evidence-doc="" className="min-w-0">
      {ask && (
        // 划选原文浮出的「质询」挂在文档里（高度为 0 的定位锚点下面，按锚点算坐标），不挂到页面末尾：画布上的审批浮层
        // 按下在它外面就收起，挂在外面的按钮一按，浮层连同报告一起收掉。也因此会被报告框裁切，位置按可见范围摆（见
        // useSelectionPick）
        <div ref={pickAnchor} className="relative" data-inquiry-pick-anchor="">
          {pick && (
            // 按下时不抢焦点、不清掉选区：点下去才读选区
            <button type="button" className="btn btn-xs absolute z-20 whitespace-nowrap shadow-elev-3" data-inquiry-selection=""
                    style={{ left: pick.left, top: pick.top }} aria-label={INQUIRY_TEXT.askSelection}
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={(e) => { e.stopPropagation(); askPick() }}>
              <MessageCircleQuestion size={11} aria-hidden /> {INQUIRY_TEXT.ask}
            </button>
          )}
        </div>
      )}
      <p id={summaryId} className="sr-only" data-evidence-summary="">{summary}</p>
      <div className="sr-only" aria-live="polite" aria-atomic="true">{announce}</div>
      {tally && (counts.total > 0 || counts.other > 0 || !!counts.suspect || !!claims?.total) && (
        <EvidenceTally counts={{ ...counts, claims }} tier={tier} onNext={() => {
          const target = step(model.alerts, current, 1)
          if (target) { focusSeg(target, { flash: true }); openSeg(target) } else openViolations()
        }} onList={doc.violations?.length ? openViolations : undefined} />
      )}
      <div
        role="group"
        aria-label={label ? `报告：${label}` : '报告'}
        aria-describedby={summaryId}
        className={clsx('space-y-2 leading-relaxed', dense ? 'text-[11.5px]' : 'text-sm')}
        onClick={onClick}
        onKeyDown={onKeyDown}
        onFocus={onFocus}
      >
        {blocks.slice(0, shownBlocks).map((block, b) => (
          // 栏内面板紧跟在片段所在的块后面：片段和它的证据在视线上挨着
          <Fragment key={block.id ?? b}>
            <DocBlock block={block} dense={dense} doc={doc} visual={visual}
                      current={b === currentBlock ? current : null}
                      active={b === activeBlock ? activeSeg : null}
                      panelId={panelId} model={model} ask={ask} calibers={caliberMarks} claims={claimMarks} />
            {mode === 'inline' && b === activeBlock && panelNode}
          </Fragment>
        ))}
        {mode === 'inline' && view?.kind === 'violations' && panelNode}
      </div>
      {shownBlocks < blocks.length && (
        <div className="mt-1 flex items-center gap-2 text-2xs">
          <span className="text-dim">…以下内容已折叠</span>
          <button type="button" className="text-[var(--accent)] hover:underline" onClick={() => setExpanded(true)}>
            展开全部（{formatNumber(totalChars)} 字）
          </button>
        </div>
      )}
      {expanded && foldAt < blocks.length && (
        <div className="mt-1 text-2xs">
          <button type="button" className="text-[var(--accent)] hover:underline" onClick={() => setExpanded(false)}>收起</button>
        </div>
      )}
      {doc.analysis_scope && (
        // 系统写的「分析范围」：不是写作者写的，随报告封存（不进正文、不过核对）；编号点得开
        <ScopeSection scope={doc.analysis_scope} onOpen={(alias) => openAnalysis(alias, { scope: true })} />
      )}
      {mode === 'inline' && view?.kind === 'scope' && panelNode}
      {mode === 'dock' && panelNode && dock && createPortal(panelNode, dock)}
      {mode !== 'inline' && mode !== 'dock' && panelNode && createPortal(panelNode, document.body)}
    </div>
  )
})

/** 划选的一段原文：要问的字、它起头的那一句、浮出按钮的位置（相对定位锚点） */
interface SelectionPick { quote: string; unit: string | null; left: number; top: number }
/** 浮出按钮的大致尺寸（宁大勿小）：放在选区末尾下方，贴边时收进可见范围，下方放不下时放到选区上方 */
const PICK_W = 64
const PICK_H = 26
/** 按钮和选区、和可见范围边上留的空 */
const PICK_GAP = 6

/**
 * 一个元素眼下看得见的范围（视口坐标）：视口，再和每一层会裁切内容的祖先（overflow 不是 visible 的，比如审批卡里
 * 限高滚动的报告框）取交集。浮出的按钮挂在文档里，超出这个范围的部分会被裁掉
 */
function visibleBox(el: Element): { top: number; bottom: number; left: number; right: number } {
  const box = { top: 0, left: 0, bottom: window.innerHeight, right: window.innerWidth }
  for (let p: Element | null = el; p && p !== document.documentElement; p = p.parentElement) {
    const style = getComputedStyle(p)
    if (style.overflowX === 'visible' && style.overflowY === 'visible') continue
    const r = p.getBoundingClientRect()
    box.top = Math.max(box.top, r.top)
    box.left = Math.max(box.left, r.left)
    box.bottom = Math.min(box.bottom, r.bottom)
    box.right = Math.min(box.right, r.right)
  }
  return box
}

/**
 * 报告正文里划选了一段原文：返回要问的字和浮出按钮的位置，选区收起、移出正文、滚出可见范围时为 null。只认报告正文
 * 里的字——栏内展开的证据面板也在这个容器里，那里的字不算。要问的字按选区克隆出来算，去掉句末徽标、「无证据」小标签、
 * 读屏文字和「质询」本身（它们在 DOM 里，却不是报告的原文）。
 *
 * 位置相对 anchor（文档里高度为 0 的定位锚点）算：按钮挂在文档里，会被限高滚动的报告框裁切，所以按可见范围摆——
 * 默认在选区末尾下方，下方放不下放到选区上方，左右收进文档的宽度（不把报告框撑出横向滚动）和可见范围
 */
function useSelectionPick(root: RefObject<HTMLElement | null>, anchor: RefObject<HTMLElement | null>,
  enabled: boolean): [SelectionPick | null, () => void] {
  const [pick, setPick] = useState<SelectionPick | null>(null)
  useEffect(() => {
    if (!enabled) { setPick(null); return }
    let frame = 0
    const read = () => {
      frame = 0
      const el = root.current
      const origin = anchor.current
      const sel = document.getSelection()
      if (!el || !origin || !sel || sel.isCollapsed || !sel.rangeCount) { setPick(null); return }
      const range = sel.getRangeAt(0)
      const at = (n: Node) => (n instanceof Element ? n : n.parentElement)
      const inside = (x: Element | null) => !!x && el.contains(x) && !!x.closest('[data-block]') && !x.closest('[data-evidence-panel]')
      const start = at(range.startContainer)
      if (!inside(start) || !inside(at(range.endContainer))) { setPick(null); return }
      const frag = range.cloneContents()
      frag.querySelectorAll('[data-inquiry-ask], .ev-tag, .ev-badge, .ev-caliber, .sr-only').forEach((n) => n.remove())
      const quote = (frag.textContent ?? '').replace(/\s+/g, ' ').trim()
      if ([...quote].length < 2) { setPick(null); return }
      const rects = range.getClientRects()
      const last = rects[rects.length - 1] ?? range.getBoundingClientRect()
      const first = rects[0] ?? last
      const view = visibleBox(el)
      // 选区整个滚出了可见范围：按钮也不留（滚回来再量出来）
      if (last.bottom <= view.top || first.top >= view.bottom) { setPick(null); return }
      const doc = el.getBoundingClientRect()
      const lo = Math.max(doc.left, view.left)
      const hi = Math.min(doc.right, view.right) - PICK_W
      const x = Math.max(lo, Math.min(last.right - PICK_W / 2, hi))
      const fitsBelow = last.bottom + PICK_GAP + PICK_H <= view.bottom
      const fitsAbove = first.top - PICK_GAP - PICK_H >= view.top
      const y = fitsBelow || !fitsAbove ? Math.min(last.bottom + PICK_GAP, Math.max(view.top, view.bottom - PICK_H))
        : first.top - PICK_GAP - PICK_H
      const o = origin.getBoundingClientRect()
      const next: SelectionPick = {
        quote, unit: start?.closest('[data-ev-unit]')?.getAttribute('data-ev-unit') ?? null,
        left: Math.round(x - o.left), top: Math.round(y - o.top),
      }
      // 滚动时每一帧都会量一次：没变就不换对象，整份报告不跟着重画
      setPick((cur) => (cur && cur.quote === next.quote && cur.unit === next.unit && cur.left === next.left && cur.top === next.top
        ? cur : next))
    }
    // 选区一变、页面或报告框一滚、窗口一变都重新量：可见范围变了，按钮放上方还是下方跟着变。文档自己的尺寸变了也量：
    // 悬停一句时它的「质询」出现、那一段多折一行，下面的字整体下移，既不滚动也不改选区，按钮却得跟着挪
    const schedule = () => { if (!frame) frame = requestAnimationFrame(read) }
    document.addEventListener('selectionchange', schedule)
    window.addEventListener('scroll', schedule, true)
    window.addEventListener('resize', schedule)
    const resized = typeof ResizeObserver === 'function' ? new ResizeObserver(schedule) : null
    if (root.current) resized?.observe(root.current)
    return () => {
      cancelAnimationFrame(frame)
      document.removeEventListener('selectionchange', schedule)
      window.removeEventListener('scroll', schedule, true)
      window.removeEventListener('resize', schedule)
      resized?.disconnect()
    }
  }, [root, anchor, enabled])
  return [pick, useCallback(() => setPick(null), [])]
}

/** 定位到的片段描一下边（CSS 的 [data-flash]），同 AssistantStream 的 flash */
function flashOnce(el: HTMLElement) {
  el.dataset.flash = 'focus'
  setTimeout(() => { if (el.dataset.flash === 'focus') delete el.dataset.flash }, 1600)
}

/** 面板放哪。auto：窄栏（dense）里栏内展开，宽屏侧边，窄屏底部抽屉；屏宽变了跟着变 */
function usePanelMode(panel: 'auto' | PanelMode, dense: boolean): PanelMode {
  // dock 由页面自己定宽窄（记录页窄的时候换成 drawer 再传进来），这里原样用
  const query = '(min-width: 900px)'
  const [wide, setWide] = useState(() => typeof matchMedia !== 'function' || matchMedia(query).matches)
  useEffect(() => {
    if (panel !== 'auto' || dense || typeof matchMedia !== 'function') return
    const mq = matchMedia(query)
    const on = () => setWide(mq.matches)
    on()
    mq.addEventListener('change', on)
    return () => mq.removeEventListener('change', on)
  }, [panel, dense])
  if (panel !== 'auto') return panel
  if (dense) return 'inline'
  return wide ? 'side' : 'drawer'
}

/**
 * 证据条：没有出具横幅时由文档自己说「7/12 数字有出处 · 无证据 5 · 另有 1 处引用解析不了」，
 * 有结论句判定时接着说「结论 4 句（支持 3 · 不支持 1）」
 */
export function EvidenceTally({ counts, onNext, onList, tier }: {
  counts: Pick<Tally, 'total' | 'cited' | 'none' | 'other' | 'suspect' | 'claims'>
  onNext?: () => void
  onList?: () => void
  /** 出具档位：给了、又不是完整出具时，数字都有出处也不用绿色 */
  tier?: string
}) {
  const suspect = counts.suspect ?? 0
  const claims = counts.claims
  const clean = !counts.none && !counts.other && !suspect && !claimProblems(claims)
  const numbers = evidenceTally(counts.cited, counts.total, counts.other)
  return (
    <div className="mb-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-2xs" data-evidence-tally="">
      {numbers && (
        <span className="tnum" data-evidence-tally-numbers=""
              style={{ color: counts.none || counts.other ? 'var(--st-waiting)' : !tier || tier === 'formal' ? 'var(--st-done)' : 'var(--text-dim)' }}>
          {numbers}
        </span>
      )}
      {/* 可疑名字另起一句：它不是数字，混进「无证据 M」就算不平了 */}
      {suspect > 0 && (
        <span className="tnum" style={{ color: 'var(--st-waiting)' }} data-evidence-tally-suspect="">
          {numbers ? '· ' : ''}{EVIDENCE_STATE.suspect.glyph} {EVIDENCE_TEXT.suspectTag} {formatNumber(suspect)}
        </span>
      )}
      {claims && claims.total > 0 && <ClaimTally claims={claims} lead={!!numbers || suspect > 0} />}
      {!clean && onNext && (
        <button type="button" className="btn btn-xs" data-evidence-next="" onClick={onNext}>
          {EVIDENCE_TEXT.locateNext}
        </button>
      )}
      {onList && (
        <button type="button" className="btn btn-xs btn-ghost" onClick={onList} data-evidence-list="">
          <ListChecks size={11} aria-hidden /> {EVIDENCE_TEXT.violations}
        </button>
      )}
    </div>
  )
}

/**
 * 「结论 4 句（有依据 3 · 证据相矛盾 1）」：和数字那一段同一种说法。判定是模型给的：全都支持也不用确定性的绿，
 * 有问题（证据相矛盾、旧的证据不支持、部分有依据、证据不足）、没挂依据的用提醒色，其余用暗字色（它是概率性的，
 * 不该和「有出处」一样笃定）
 */
export function ClaimTally({ claims, lead }: { claims: ClaimTallyCounts; lead: boolean }) {
  const text = claimTally(claims)
  if (!text) return null
  const warn = claimProblems(claims) > 0 || claims.uncited > 0
  return (
    <span className="tnum" style={{ color: warn ? 'var(--st-waiting)' : 'var(--text-dim)' }} data-evidence-claims=""
          title={JUDGE_TEXT.badge(null)}>
      {lead ? '· ' : ''}{text}
    </span>
  )
}

// -------------------------------------------------------------------------
// 块与片段
// -------------------------------------------------------------------------

/** 列表项的开头：结构片段里带着行首的 - / 1. 。列表项被分成几句时，后面几句接在同一项里 */
const ITEM_START = /(^|\n)[ \t]*([-*+]|\d+[.)])[ \t]+$/

const DocBlock = memo(function DocBlock({ block, dense, doc, visual, current, active, panelId, model, ask, calibers, claims }: {
  block: EvidenceBlock
  /** 来自口径的数字：片段 id → 小标记（口径账本） */
  calibers?: CaliberMarks
  /** 说法违规落在的文字：片段 id → 判断词的位置 */
  claims?: ClaimMarks
  dense: boolean
  doc: EvidenceDocData
  /** 句末徽标从这里取：这一句眼下的判定（封存后追加的盖过文档里的） */
  model: Model
  /** 审批质询：结论句后面挂「质询」 */
  ask: boolean
  /**
   * 长文档：块上加 content-visibility，屏幕外的块不排版不绘制。短文档不加：它自带 paint
   * containment，贴着块边的片段的焦点描边会被裁掉一截，而短文档本来就不慢
   */
  visual: boolean
  /** 这一块里拿着 Tab 位的片段 */
  current: string | null
  /** 这一块里面板正开着的片段 */
  active: string | null
  panelId: string
}) {
  const units = block.units ?? []
  const root = {
    'data-block': block.id,
    ...(visual ? { style: { contentVisibility: 'auto', containIntrinsicSize: 'auto 2.5em' } as CSSProperties } : {}),
  }
  const unit = (u: EvidenceUnit, opts?: { code?: boolean; tag?: boolean }) => (
    <UnitView key={u.id} unit={u} doc={doc} current={current} active={active} panelId={panelId}
              code={opts?.code} tag={opts?.tag ?? true} badge={model.badge.get(badgeKey(u.id))} calibers={calibers} claims={claims}
              ask={ask && askable(u, block) ? (model.ask.has(askKey(u.id)) ? 'roving' : 'follow') : undefined} />
  )

  switch (block.type) {
    case 'heading':
      return <BlockView dense={dense} root={root} shape={{ kind: 'h', level: block.level ?? 2, body: units.map((u) => unit(u)) }} />
    case 'hr':
      return <BlockView dense={dense} root={root} shape={{ kind: 'hr' }} />
    case 'quote':
      return <BlockView dense={dense} root={root} shape={{ kind: 'quote', body: units.map((u) => unit(u)) }} />
    case 'code':
      return (
        <BlockView dense={dense} root={root} shape={{
          kind: 'code', lang: block.lang ?? '', code: units.map(unitText).join(''),
          body: units.map((u) => unit(u, { code: true, tag: false })),
        }} />
      )
    case 'list': {
      const items: { key: string; depth: number; units: EvidenceUnit[] }[] = []
      for (const u of units) {
        const lead = u.segments?.[0]
        const starts = !items.length || (lead?.kind === 'structural' && ITEM_START.test(lead.text))
        if (starts) items.push({ key: u.id, depth: u.depth ?? 0, units: [u] })
        else items[items.length - 1].units.push(u)
      }
      return (
        <BlockView dense={dense} root={root} shape={{
          kind: 'list', ordered: !!block.ordered, start: block.start, fitMarkers: true,
          items: items.map((it) => ({ key: it.key, depth: it.depth, body: it.units.map((u) => unit(u)) })),
        }} />
      )
    }
    case 'table': {
      // 表头 row = -1；一格可能被分成几句，按位置归拢
      const cells = new Map<string, EvidenceUnit[]>()
      let cols = 0
      const rowIds: number[] = []
      for (const u of units) {
        const loc = u.loc ?? { row: 0, col: 0 }
        const k = `${loc.row}:${loc.col}`
        cells.set(k, [...(cells.get(k) ?? []), u])
        cols = Math.max(cols, loc.col + 1)
        if (loc.row >= 0 && !rowIds.includes(loc.row)) rowIds.push(loc.row)
      }
      rowIds.sort((a, b) => a - b)
      const text = (row: number, col: number) => (cells.get(`${row}:${col}`) ?? []).map(unitText).join('')
      const node = (row: number, col: number) =>
        (cells.get(`${row}:${col}`) ?? []).map((u) => unit(u, { tag: false }))
      const head = Array.from({ length: cols }, (_, c) => text(-1, c))
      const rows = rowIds.map((r) => Array.from({ length: cols }, (_, c) => text(r, c)))
      return (
        <BlockView dense={dense} root={root} shape={{
          kind: 'table',
          head: Array.from({ length: cols }, (_, c) => node(-1, c)),
          rows: rowIds.map((r) => Array.from({ length: cols }, (_, c) => node(r, c))),
          numeric: numericColumns(head, rows),
          csv: () => [head, ...rows].map((r) => r.map((v) => (/[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v)).join(',')).join('\n'),
        }} />
      )
    }
    default:
      return <BlockView dense={dense} root={root} shape={{ kind: 'p', body: units.map((u) => unit(u)) }} />
  }
})

/** 来自口径的数字上的小标记：片段 id → 标记上的字（级别）、悬停、读屏 */
type CaliberMarks = Map<string, { text: string; level: string; title: string; aria: string }>

function caliberMarksOf(labels: Record<string, LedgerLabel> | undefined): CaliberMarks | undefined {
  if (!labels) return undefined
  const out: CaliberMarks = new Map()
  for (const [id, label] of Object.entries(labels)) {
    if (label.kind !== 'caliber') continue
    const how = LEDGER_TEXT.how[label.how === 'composed' ? 'composed' : 'independent']
    const caliber = caliberOf(label)
    const level = label.level_text || (label.level ? (LEDGER_TEXT.level as Record<string, string>)[label.level] ?? '' : '')
    out.set(id, { text: level || LEDGER_TEXT.kind.caliber, level: label.level ?? '', title: LEDGER_UI.mark.title(caliber, how),
                  aria: LEDGER_UI.mark.aria(caliber, how) })
  }
  return out.size ? out : undefined
}

/** 反方审稿的判断在句末徽标里的样子。soft 是悬停、展开时铺的浅底，醒目的那两种不铺（见 LOUD_RING） */
interface MarkLook { icon: LucideIcon; color: string; soft: string; loud: boolean }

/**
 * 被推翻、被削弱醒目：盾牌图标加判断两个字，用失败色、提醒色；经受住、无法判断低调：只有淡色的盾牌图标（悬停、读屏照样
 * 说得出是哪一种）。四种图标各不相同，去掉颜色也分得开：无法判断用省略号不用问号——句末常常已经有裁判的「?」（未裁判、
 * 证据不足）和「质询」的问号，再添一个问号就分不清了，和经受住的勾也更好分。判断是反方模型给的，经受住也不用确定性的绿。
 * 认不出的判断按低调画
 */
const MARK_LOOK: Record<ChallengeVerdict, MarkLook> = {
  refuted: { icon: ShieldX, color: 'var(--st-failed)', soft: 'transparent', loud: true },
  weakened: { icon: ShieldMinus, color: 'var(--st-waiting)', soft: 'transparent', loud: true },
  stands: { icon: ShieldCheck, color: 'var(--text-faint)', soft: 'var(--st-idle-soft)', loud: false },
  inconclusive: { icon: ShieldEllipsis, color: 'var(--text-faint)', soft: 'var(--st-idle-soft)', loud: false },
}
const markLook = (verdict: string): MarkLook => (Object.hasOwn(MARK_LOOK, verdict) ? MARK_LOOK[verdict as ChallengeVerdict]
  : { icon: ShieldAlert, color: 'var(--text-faint)', soft: 'var(--st-idle-soft)', loud: false })

/**
 * 带字的醒目标记悬停、展开时不铺浅底（index.css 的 .ev-badge 按 --ev-soft 铺），改描一圈细边：10px 的失败色、提醒色字叠在
 * 14% 的浅底上，浅色主题里对比度只有 4.0 左右（页面底色上），面板开着时这枚标记一直是展开态。和裁判徽标并在一起的同样
 * 不铺（浅底是裁判判定的颜色，字是反方的颜色）。细边用徽标的字色，和焦点环（2px 强调色、外扩）分得开
 */
const LOUD_RING = 'hover:shadow-[inset_0_0_0_1px_currentColor] aria-expanded:shadow-[inset_0_0_0_1px_currentColor]'

/**
 * 句末徽标里反方审稿的那一截。和裁判的字形并在一个徽标里时跟在字形后面（after）。醒目的那两种连字一起写：字号定成
 * 10px（同句末的「无证据」小标签），窄栏里 0.72em 的上标只有 8px，汉字认不清
 */
function ChallengeMark({ verdict, look, after }: { verdict: string; look: MarkLook; after: boolean }) {
  const Icon = look.icon
  return (
    <span aria-hidden="true" className={clsx('inline-flex items-center gap-px whitespace-nowrap', after && 'ml-0.5')}
          style={{ color: look.color }} data-ev-challenge-look={look.loud ? 'loud' : 'quiet'}>
      <Icon size={10} strokeWidth={2.25} className="shrink-0" />
      {look.loud && <span className="text-[10px]">{challengeLabel(verdict)}</span>}
    </span>
  )
}

/**
 * 一句话：文字按行内 Markdown 渲染（整句算一次样式，跨片段的 `code`、**粗体** 不会
 * 被切坏），有状态的片段包成按钮。含无证据片段的句子末尾挂「? 无证据」；有判定的结论句末尾挂
 * 一枚徽标（也是按钮），证据相矛盾（和旧的证据不支持）的整句铺浅底——字本身不画线。被反方审过的
 * 句子同样挂徽标（和判定并在一起），点开都是整句面板
 */
function UnitView({ unit, doc, current, active, panelId, code, tag, badge, ask, calibers, claims }: {
  unit: EvidenceUnit; doc: EvidenceDocData; current: string | null; active: string | null; panelId: string
  code?: boolean; tag: boolean; badge?: Badge
  calibers?: CaliberMarks
  claims?: ClaimMarks
  /**
   * 审批质询的「质询」：roving 是这一句一个可点的都没有，「质询」本身进键盘顺序；follow 跟在这一句后面，焦点在这一句上时
   * 按 Tab 到达
   */
  ask?: 'roving' | 'follow'
}) {
  const segs = (unit.segments ?? []).filter((s) => s.kind !== 'structural')
  const text = segs.map((s) => s.text).join('')
  const styles = useMemo<InlineStyles | null>(() => (code ? null : inlineStyles(text)), [text, code])
  let pos = 0
  const alerts = new Set<EvidenceStateCode>()
  let claimTag = false
  const nodes = segs.map((s) => {
    const from = pos
    pos += s.text.length
    const state = segmentState(s)
    const body = styles
      ? <StyledSlice text={text} styles={styles} from={from} to={pos} links={!state} />
      : s.text
    const claimed = !state ? claims?.get(s.id) : undefined
    if (claimed) {
      // 说法违规：判断词画琥珀色波浪线，整段文字是一个可点的片段（点开看服务端的原话），句末挂「说法待核」
      claimTag = true
      const slice = (a: number, z: number) => (styles
        ? <StyledSlice text={text} styles={styles} from={from + a} to={from + z} links={false} /> : s.text.slice(a, z))
      const parts: ReactNode[] = []
      let at = 0
      claimed.ranges.forEach(([a, z], k) => {
        if (a > at) parts.push(<Fragment key={`t${k}`}>{slice(at, a)}</Fragment>)
        parts.push(<span key={`w${k}`} className="ev-claim-word" data-ev-claim-word="">{slice(a, z)}</span>)
        at = z
      })
      if (at < s.text.length) parts.push(<Fragment key="rest">{slice(at, s.text.length)}</Fragment>)
      const open = active === s.id
      const labels = claimed.codes.map((c) => ANALYSIS_TEXT.claim.label[c] ?? c).join('、')
      return (
        <button key={s.id} type="button" className="ev-seg" data-seg={s.id} data-ev-state="claim"
                data-ev-claim-issue={claimed.codes.join(' ')} tabIndex={current === s.id ? 0 : -1}
                aria-label={`${s.text}，${ANALYSIS_TEXT.claim.tag}：${labels}`} aria-expanded={open}
                aria-controls={open ? panelId : undefined} title={ANALYSIS_TEXT.claim.hint}
                style={{ '--ev-soft': 'var(--st-waiting-soft)' } as CSSProperties}>
          {s.strong ? <strong className="font-semibold">{parts}</strong> : parts}
        </button>
      )
    }
    if (!state) return <span key={s.id}>{s.strong ? <strong className="font-semibold">{body}</strong> : body}</span>
    const meta = EVIDENCE_STATE[state]
    if (meta.alert && TAG_TEXT[state]) alerts.add(state)
    const open = active === s.id
    const mark = calibers?.get(s.id)
    const button = (
      <button
        key={s.id}
        type="button"
        className="ev-seg"
        data-seg={s.id}
        data-ev-state={state}
        data-ev-line={meta.line}
        // 实体、引文：线型和颜色照状态，另有自己的样子（引文前挂引号，见 index.css）
        data-ev-kind={segmentKind(s) ?? undefined}
        tabIndex={current === s.id ? 0 : -1}
        aria-label={mark ? `${segmentLabel(s, doc)}${mark.aria}` : segmentLabel(s, doc)}
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        style={segStyle(state)}
      >
        {s.strong ? <strong className="font-semibold">{body}</strong> : body}
      </button>
    )
    if (!mark) return button
    // 来自口径的数：后面挂一个小标记（受管 / 已发布），悬停写是哪一份口径、怎么认的。不进键盘顺序（读屏念在片段上）
    return (
      <Fragment key={s.id}>
        {button}
        <sup className="ev-caliber ml-px select-none whitespace-nowrap rounded-sm px-0.5 text-[9px] font-medium"
             style={{ color: 'var(--accent)', background: 'var(--accent-soft, transparent)' }}
             aria-hidden="true" title={mark.title} data-ev-caliber={mark.level} data-ev-caliber-of={s.id}>
          {mark.text}
        </sup>
      </Fragment>
    )
  })
  const key = badge ? badgeKey(unit.id) : ''
  const meta = badge?.state ? EVIDENCE_STATE[badge.state] : null
  const open = !!badge && active === key
  const verdict = badge?.verdict
  // 模型判过的写明谁判的、非确定（证据不足的接着写缺什么）；模型没判过的（按需还没点、到上限、没跑成）只说
  // 这是什么状态——到上限没判的也记着裁判模型，但不能写成「（模型 · 非确定）」，像是它判过一样
  const judgedTitle = !verdict || !meta ? ''
    : isJudged(verdict)
      ? `${meta.label}${verdict.post_seal ? ` · ${JUDGE_TEXT.postSeal}` : ''}（${verdict.judge || JUDGE_TEXT.judgeModel} · 非确定）${
        verdictMissing(verdict) ? `。${JUDGE_TEXT.missing(verdictMissing(verdict))}` : ''}`
      : limitOf(verdict) ? `${meta.label}：${JUDGE_TEXT.limitNotJudged}` : `${meta.label}：${meta.hint}`
  // 无法判断的句子可能根本没反查成（服务端按规则记的无法判断，反方模型没看过），正文里分不出是哪一种：不说成反方模型的判断
  const challengeTitle = !badge?.challenge ? ''
    : badge.challenge === 'inconclusive' ? CHALLENGE_TEXT.markTitleUnsure(challengeLabel(badge.challenge))
    : CHALLENGE_TEXT.markTitle(challengeLabel(badge.challenge))
  const look = badge?.challenge ? markLook(badge.challenge) : null
  const body = (
    <>
      {conflicting(badge?.state)
        // 证据相矛盾（和旧的证据不支持）：整句铺一层浅底（折行时每行各自带圆角），字色、字重都不动，也不画线
        ? <span className="ev-claim" data-ev-claim-shade={unit.id} style={{ '--ev-soft': meta!.soft } as CSSProperties}>{nodes}</span>
        : nodes}
      {tag && [...alerts].map((state) => (
        <span key={state} className="ev-tag" aria-hidden="true" title={EVIDENCE_STATE[state].hint} data-ev-tag={state}>
          {EVIDENCE_STATE[state].glyph}{TAG_TEXT[state]}
        </span>
      ))}
      {tag && claimTag && (
        <span className="ev-tag" aria-hidden="true" title={ANALYSIS_TEXT.claim.hint} data-ev-tag="claim">
          ?{ANALYSIS_TEXT.claim.tag}
        </span>
      )}
      {badge && (meta || look) && (
        <button
          type="button"
          className={clsx('ev-badge', look?.loud && LOUD_RING)}
          data-ev-claim={unit.id}
          data-ev-verdict={badge.state}
          data-ev-post-seal={verdict?.post_seal ? '' : undefined}
          data-ev-challenge-mark={badge.challenge}
          tabIndex={current === key ? 0 : -1}
          aria-label={badgeLabel(badge)}
          aria-expanded={open}
          aria-controls={open ? panelId : undefined}
          title={[judgedTitle, challengeTitle].filter(Boolean).join('。')}
          // 字形的颜色照裁判的判定；只被反方审过的照反方的判断（悬停的浅底同理）。带着醒目的反方标记时不铺浅底（LOUD_RING）
          style={meta ? { ...segStyle(badge.state!), ...(look?.loud ? { '--ev-soft': 'transparent' } : {}) } as CSSProperties
            : { '--ev-glyph': look!.color, '--ev-soft': look!.soft } as CSSProperties}
        >
          {meta && <span aria-hidden="true">{meta.glyph}</span>}
          {look && <ChallengeMark verdict={badge.challenge!} look={look} after={!!meta} />}
        </button>
      )}
    </>
  )
  if (!ask) return body
  // 「质询」跟在句末，是一个固定宽度的小图标：平时淡淡的，悬停这一句、焦点落在这一句上时显出来（强调色、描边）。
  // 平时也得看得清它是个可点的东西（WCAG 1.4.11 要求界面组件对背景 ≥ 3:1）：最淡的字色再压到 60% 不透明，深色约 2.75、
  // 浅色约 2.33，看不出来。改用次一级的暗字色压到 70%：深浅两套、报告摆在页面底色或面板底色上都在 3.3 以上，仍比正文
  // 淡得多；不透明度留在 0.75 以下，和悬停、聚焦时显出来（不透明、强调色）分得开。
  // 显没显出来只换颜色，宽度一直是那么宽——悬停时撑开的话，后面的字整体右移，鼠标底下的数字跟着挪走，点不到。
  // 触屏没有悬停，一直显出来，并写上「质询」两个字。
  // 跟随的那种只在焦点在这一句上时进 Tab 顺序，整份报告不因此多出一串 Tab 位
  const here = current != null && (current === key || current === askKey(unit.id)
    || (unit.segments ?? []).some((sg) => sg.id === current))
  return (
    <span className="group/evu" data-ev-unit={unit.id}>
      {body}
      <button type="button" data-inquiry-ask={unit.id} tabIndex={here ? 0 : -1}
              aria-label={INQUIRY_TEXT.askUnit(unitText(unit).trim())} title={INQUIRY_TEXT.askTitle}
              className={'ml-1 inline-flex select-none items-center gap-0.5 whitespace-nowrap rounded border border-transparent px-[3px] '
                + 'align-middle text-[10px] font-medium leading-[1.4] text-dim opacity-70 outline-none '
                + 'hover:bg-hover group-hover/evu:border-[color:var(--border)] group-hover/evu:text-[color:var(--accent)] group-hover/evu:opacity-100 '
                + 'group-focus-within/evu:border-[color:var(--border)] group-focus-within/evu:text-[color:var(--accent)] group-focus-within/evu:opacity-100 '
                + 'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[color:var(--accent)] '
                + 'pointer-coarse:border-[color:var(--border)] pointer-coarse:text-[color:var(--accent)] pointer-coarse:opacity-100'}>
        <MessageCircleQuestion size={11} aria-hidden />
        <span aria-hidden="true" className="hidden pointer-coarse:inline">{INQUIRY_TEXT.ask}</span>
      </button>
    </span>
  )
}

/**
 * 成果里带证据的那个字段：按工件 id 取文档，取到之前和取不到时照普通答案显示——
 * 证据是锦上添花，拿不到不能让答案本身消失。以下情况也不画，免得把证据挂到别的字上：
 * - 文档的正文和字段对不上（字段被模板加工过），或者文档根本没记正文；
 * - 文档记着的运行和这份成果的运行不是同一个（成果上的 doc_artifact 是可以改写的一列）
 */
export const EvidenceField = forwardRef<EvidenceDocHandle, {
  artifact: string; text: string; runId?: string; dense?: boolean; label?: string; tally?: boolean
  /** 交给出具横幅数数的文档：封存之后按需追加的判定已经叠上去了（只给计数用，正文照样按封存的那份画） */
  onDoc?: (doc: EvidenceDocData | null) => void
  runClass?: string
}>(function EvidenceField({ artifact, text, runId, dense, label, tally, onDoc, runClass }, ref) {
  const slot = useEvidence((s) => s.docs[artifact])
  const loadDoc = useEvidence((s) => s.loadDoc)
  useEffect(() => { void loadDoc(artifact) }, [artifact, loadDoc])
  const doc = slot?.status === 'ok' ? slot.data ?? null : null
  const mismatch = !!doc && (typeof doc.markdown !== 'string' || doc.markdown.trim() !== text.trim())
  const otherRun = !!doc && !!runId && typeof doc.run_id === 'string' && !!doc.run_id && doc.run_id !== runId
  const usable = doc && !mismatch && !otherRun ? doc : null
  const overlay = useVerdicts(runId, usable?.node_id || undefined)
  const counted = useMemo(() => (usable ? withVerdicts(usable, overlay) : null), [usable, overlay])
  useEffect(() => { onDoc?.(counted) }, [counted, onDoc])
  if (usable) {
    // 成果只在运行结束之后才有：手里那份证据图要是没封存，就是收尾之前取的（settled，见 useRunGraph）
    return <EvidenceDoc ref={ref} doc={usable} artifact={artifact} runId={runId} dense={dense} label={label} tally={tally}
                        runClass={runClass} settled />
  }
  const note = slot?.status === 'error' ? EVIDENCE_TEXT.docMissing
    : otherRun ? EVIDENCE_TEXT.docOtherRun : mismatch ? EVIDENCE_TEXT.docMismatch : ''
  return (
    <div data-evidence-fallback={otherRun ? 'other-run' : mismatch ? 'mismatch' : slot?.status ?? 'loading'}>
      <Markdown text={text} dense={dense} />
      {note && <div className="mt-1 text-2xs text-faint">{note}</div>}
    </div>
  )
})
