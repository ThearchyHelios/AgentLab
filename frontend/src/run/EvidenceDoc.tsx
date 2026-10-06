import {
  Fragment, forwardRef, memo, useCallback, useEffect, useId, useImperativeHandle, useLayoutEffect, useMemo,
  useRef, useState, type CSSProperties, type FocusEvent as ReactFocusEvent,
  type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent, type RefObject,
} from 'react'
import { createPortal } from 'react-dom'
import { ListChecks, MessageCircleQuestion } from 'lucide-react'
import clsx from 'clsx'
import { rovingTarget } from '../components/ui'
import {
  EVIDENCE_STATE, alertTier, claimCounts, claimLabel, claimProblems, conflicting, docTally, segmentKind, segmentLabel,
  segmentState, tallySummary, unitText, isJudged, limitOf, unitVerdict, verdictMissing, verdictState, withVerdicts,
  type EvidenceStateCode, type EvidenceTally as Tally,
} from '../lib/evidence'
import { formatNumber } from '../lib/format'
import { EVIDENCE_STATE_LABEL, EVIDENCE_TEXT, INQUIRY_TEXT, JUDGE_TEXT, claimTally, evidenceTally, type ClaimTallyCounts } from '../lib/terms'
import { useEvidence, useVerdicts } from '../store/evidence'
import type { EvidenceBlock, EvidenceDocData, EvidenceSegment, EvidenceUnit, EvidenceVerdict } from '../types'
import { EvidencePanel, type PanelMode, type PanelView } from './EvidencePanel'
import { askAbout, type InquiryAccess } from './Inquiry'
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
 *
 * 键盘：整份报告只占一个 Tab 位（roving tabindex）。←/→ 在有状态的片段、句末徽标间走，↑/↓ 按
 * 句子走（表格里按列走到上一行 / 下一行，出了表格接着按句子走），n / N 跳到下一处 /
 * 上一处有问题的地方（无证据、可疑名称、证据相矛盾、部分有依据；证据不足的排在这些之后），回车打开证据面板，
 * Esc 关掉并留在原片段；面板开着时方向键走到哪、面板跟到哪，Tab 进面板。
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

/** 哪些句子挂「质询」：结论句（老文档没有 kind 的按有字的句子），标题、代码块里的不挂 */
const askable = (unit: EvidenceUnit, block: EvidenceBlock) =>
  (unit.kind ? unit.kind === 'claim' : !!unitText(unit).trim()) && !['heading', 'code', 'hr'].includes(block.type)

interface Badge { unit: EvidenceUnit; block: number; verdict: EvidenceVerdict; state: EvidenceStateCode }

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

function buildModel(blocks: EvidenceBlock[], overlay?: Record<string, EvidenceVerdict>, ask = false): Model {
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
        if (!state) continue
        seg.set(s.id, { seg: s, unit, block: b })
        index.set(s.id, order.length)
        order.push(s.id)
        if (first) { unitFirst.push(s.id); first = false }
        unitOf.set(s.id, unitFirst.length - 1)
        const t = alertTier(state)
        if (t) tiered.push([s.id, t])
        if (loc) {
          cell.set(s.id, { block: b, row: loc.row, col: loc.col })
          const key = `${b}:${loc.row}:${loc.col}`
          if (!grid.has(key)) grid.set(key, s.id)
        }
      }
      // 句末徽标排在这一句的片段后面：←/→ 走到句尾就是它，证据相矛盾、部分有依据、证据不足的也进 n / N
      const verdict = unitVerdict(unit, overlay)
      const state = verdictState(verdict)
      if (verdict && state) {
        const key = badgeKey(unit.id)
        badge.set(key, { unit, block: b, verdict, state })
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
   * 审批质询（期 4）：给了就在每句结论后面挂「质询」（悬停、焦点在这一句上时出现），划选原文时浮出「质询」，点了打开
   * 这份报告的提问框。能不能问由页面按运行状态定（审批卡：运行停在审批上；运行详情：封存了的探索运行），approvalId 是
   * 运行停在的那张审批。给了它，页面上要同时挂这份报告的 ReportInquiry（问答串和提问框在那里）
   */
  inquiry?: InquiryAccess | null
}>(function EvidenceDoc({ doc, artifact, runId, dense = false, label, panel = 'auto', dock, onView, tally = false, runClass,
  tier, inquiry }, ref) {
  const blocks = useMemo(() => doc.blocks ?? [], [doc])
  const report = doc.node_id || undefined
  // 质询要知道是哪次运行的哪份报告：两样缺一样就不挂入口
  const ask = !!inquiry && !!runId && !!report
  // 封存之后按需追加的判定：盖过文档里的「未裁判 · 按需」。只换句末徽标，封存的文档一个字不改
  const overlay = useVerdicts(runId, report)
  const model = useMemo(() => buildModel(blocks, overlay, ask), [blocks, overlay, ask])
  const counts = useMemo(() => docTally(doc), [doc])
  const claims = useMemo(() => claimCounts(doc, overlay), [doc, overlay])
  // 探索运行的报告（开了裁判的按需文档，或调用方说了是探索运行）：证据图带着这次运行里之前按需判过的句子
  // （reports[].post_seal_verdicts），取一次，重新打开页面时徽标还在。别的报告不多发这个请求
  const loadGraph = useEvidence((s) => s.loadGraph)
  const lateOnes = doc.judge?.mode === 'on_demand' || (runClass === 'exploratory' && doc.judge?.mode !== 'inline')
  useEffect(() => { if (lateOnes && runId) void loadGraph(runId) }, [lateOnes, runId, loadGraph])
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
  // 拿着 Tab 位的徽标没了（判成「不是结论句」）：Tab 位交回第一个，整份报告不能一个 Tab 位都没有
  useEffect(() => {
    if (current && !model.index.has(current)) setCurrent(model.order[0] ?? null)
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
      setAnnounce(`${JUDGE_TEXT.section}：${claimLabel(badge.unit, badge.verdict)}`)
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

  const close = useCallback((refocus = true) => {
    const back = view?.kind === 'seg' ? view.id : view?.kind === 'unit' ? badgeKey(view.id) : current
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
      view={view}
      onClose={() => close()}
      onLocate={(id) => { focusSeg(id, { flash: true }); openSeg(id) }}
      onViolations={openViolations}
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
                      panelId={panelId} model={model} ask={ask} />
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
      frag.querySelectorAll('[data-inquiry-ask], .ev-tag, .ev-badge, .sr-only').forEach((n) => n.remove())
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

const DocBlock = memo(function DocBlock({ block, dense, doc, visual, current, active, panelId, model, ask }: {
  block: EvidenceBlock
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
              code={opts?.code} tag={opts?.tag ?? true} badge={model.badge.get(badgeKey(u.id))}
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

/**
 * 一句话：文字按行内 Markdown 渲染（整句算一次样式，跨片段的 `code`、**粗体** 不会
 * 被切坏），有状态的片段包成按钮。含无证据片段的句子末尾挂「? 无证据」；有判定的结论句末尾挂
 * 一枚徽标（也是按钮），证据相矛盾（和旧的证据不支持）的整句铺浅底——字本身不画线
 */
function UnitView({ unit, doc, current, active, panelId, code, tag, badge, ask }: {
  unit: EvidenceUnit; doc: EvidenceDocData; current: string | null; active: string | null; panelId: string
  code?: boolean; tag: boolean; badge?: Badge
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
  const nodes = segs.map((s) => {
    const from = pos
    pos += s.text.length
    const state = segmentState(s)
    const body = styles
      ? <StyledSlice text={text} styles={styles} from={from} to={pos} links={!state} />
      : s.text
    if (!state) return <span key={s.id}>{s.strong ? <strong className="font-semibold">{body}</strong> : body}</span>
    const meta = EVIDENCE_STATE[state]
    if (meta.alert && TAG_TEXT[state]) alerts.add(state)
    const open = active === s.id
    return (
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
        aria-label={segmentLabel(s, doc)}
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        style={segStyle(state)}
      >
        {s.strong ? <strong className="font-semibold">{body}</strong> : body}
      </button>
    )
  })
  const key = badge ? badgeKey(unit.id) : ''
  const meta = badge ? EVIDENCE_STATE[badge.state] : null
  const open = !!badge && active === key
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
      {badge && meta && (
        <button
          type="button"
          className="ev-badge"
          data-ev-claim={unit.id}
          data-ev-verdict={badge.state}
          data-ev-post-seal={badge.verdict.post_seal ? '' : undefined}
          tabIndex={current === key ? 0 : -1}
          aria-label={claimLabel(unit, badge.verdict)}
          aria-expanded={open}
          aria-controls={open ? panelId : undefined}
          // 模型判过的写明谁判的、非确定（证据不足的接着写缺什么）；模型没判过的（按需还没点、到上限、没跑成）只说
          // 这是什么状态——到上限没判的也记着裁判模型，但不能写成「（模型 · 非确定）」，像是它判过一样
          title={isJudged(badge.verdict)
            ? `${meta.label}${badge.verdict.post_seal ? ` · ${JUDGE_TEXT.postSeal}` : ''}（${badge.verdict.judge || JUDGE_TEXT.judgeModel} · 非确定）${
              verdictMissing(badge.verdict) ? `。${JUDGE_TEXT.missing(verdictMissing(badge.verdict))}` : ''}`
            : limitOf(badge.verdict) ? `${meta.label}：${JUDGE_TEXT.limitNotJudged}` : `${meta.label}：${meta.hint}`}
          style={segStyle(badge.state)}
        >
          <span aria-hidden="true">{meta.glyph}</span>
        </button>
      )}
    </>
  )
  if (!ask) return body
  // 「质询」跟在句末：平时不占地方（宽度为 0、透明），悬停这一句、焦点落在这一句上时出现；触屏没有悬停，一直显示。
  // 跟随的那种只在焦点在这一句上时进 Tab 顺序，整份报告不因此多出一串 Tab 位
  const here = current != null && (current === key || current === askKey(unit.id)
    || (unit.segments ?? []).some((sg) => sg.id === current))
  return (
    <span className="group/evu" data-ev-unit={unit.id}>
      {body}
      <button type="button" data-inquiry-ask={unit.id} tabIndex={here ? 0 : -1}
              aria-label={INQUIRY_TEXT.askUnit(unitText(unit).trim())}
              className={'group/ask inline-flex w-0 select-none overflow-hidden align-middle opacity-0 outline-none focus:w-auto focus:opacity-100 '
                + 'group-hover/evu:w-auto group-hover/evu:opacity-100 group-focus-within/evu:w-auto group-focus-within/evu:opacity-100 '
                + 'pointer-coarse:w-auto pointer-coarse:opacity-100'}>
        <span aria-hidden="true"
              className={'my-px ml-1 inline-flex items-center gap-0.5 whitespace-nowrap rounded border px-1 text-[10px] font-medium leading-[1.4] '
                + 'text-[color:var(--accent)] hover:bg-hover group-focus-visible/ask:outline group-focus-visible/ask:outline-2 '
                + 'group-focus-visible/ask:outline-offset-1 group-focus-visible/ask:outline-[color:var(--accent)]'}>
          <MessageCircleQuestion size={10} aria-hidden /> {INQUIRY_TEXT.ask}
        </span>
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
    return <EvidenceDoc ref={ref} doc={usable} artifact={artifact} runId={runId} dense={dense} label={label} tally={tally}
                        runClass={runClass} />
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
