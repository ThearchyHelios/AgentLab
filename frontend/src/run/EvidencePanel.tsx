import {
  useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from 'react'
import { createPortal } from 'react-dom'
import { Link } from 'react-router-dom'
import { AlertTriangle, ArrowLeft, CornerDownRight, ListChecks, Scale, ShieldCheck, X } from 'lucide-react'
import clsx from 'clsx'
import { ApiError } from '../api/client'
import { Spinner, StatusBadge, isComposing } from '../components/ui'
import {
  EVIDENCE_KIND_STYLE, EVIDENCE_STATE, caliberSourceText, caliberUpgradeText, citeLabel, claimBasis, closestNames,
  docForeign, entitySources, evidenceTrace, evidenceValue as valueText, graphDoc, inputSource, integrityFailures, isJudged,
  isJudgeLimit, judgeFlags, judgeLimitWords, judgeable, judgedVerdicts, limitOf, pointsToSettings,
  locatable, locatorText, notableInput, queryOf, queryWindow, quoteWhere, quoteWindow, reasonOf, rewriteOf, sealVerdict,
  segName, segmentKind, segmentState, sourceOf, unitText, unitVerdict, verdictMissing, verdictState, type ClaimBasis as Basis,
  type SealStatus, type SourceTone,
} from '../lib/evidence'
import { humanizeError } from '../lib/errors'
import { formatDateTime, formatNumber, NONE, shortId } from '../lib/format'
import {
  ABSENCE_CLAIM_TEXT, CHALLENGE_TEXT, DUAL_CHECK_TEXT, EVIDENCE_TEXT, JUDGE_TEXT, MASKED_CELL_TEXT, MERGE_TEXT, NODE_TYPE_LABEL, RUN_SQL_CHECK_TEXT,
  SQL_CHECK_TEXT, nodeTypeLabel,
} from '../lib/terms'
import { useAtLeast, useLoginOn } from '../store/auth'
import { useCatalog } from '../store/catalog'
import { askKey, segmentKey, useEvidence, useExplore, useVerdicts } from '../store/evidence'
import type {
  ApprovalReport, EvidenceBlock, EvidenceChallengeVerdicts, EvidenceDocData, EvidenceDualCheck, EvidenceInput, EvidenceMerge,
  EvidenceOnDemand, EvidenceQuoteSource, EvidenceSeal, EvidenceSegment, EvidenceSegmentDetail, EvidenceStep, EvidenceUnit,
  EvidenceUnitChallenge, EvidenceUntraced, EvidenceVerdict, EvidenceViolation,
} from '../types'
import { ArtifactViewer, ResultTable } from './AssistantStream'
import { ChallengeSummary, ClaimVerdictPending, type ClaimWait } from './ChallengeSummary'
import { useEvidenceHost } from './evidenceHost'
import { EvidenceProvenance } from './EvidenceProvenance'
import { LedgerPart } from './LedgerStep'
import { CopyChip } from './Markdown'
import { SqlCheckList } from './SqlChecks'
import {
  WhatIf, WhatIfRows, WhatIfSummary, WhatIfToggle, focusResult, inSpans, pickMetric, usableRows, useWhatIfDraft,
  useWhatIfResult, wholeReason,
} from './WhatIf'

/**
 * 证据面板：点开报告里的一个片段，看它从哪来。
 *
 * 展示：片段和它所在的句子；指标步骤——名称、值、口径与版本（钉在别的工作流上的写明来源
 * 和升版处置）、原式、代入式（每个输入是「值 ← 路径」的小标签，点一下跳到对应的查询）、
 * 复算结果；输入来源——agent 字段、cell() 取数和快照核对得上没有，代码节点的数醒目提醒；
 * 查询步骤——SQL、被引用的行和格高亮的结果窗口、打开完整快照、遮罩；封存状态。没有证据
 * 的片段说清为什么（裸数字、引用解析不了），另有一份违规清单：列表序号、代码块标签里的数字
 * 在正文里画不了线，只能在清单里找到。
 *
 * 数据两层来源：文档自己记着的出处（doc.catalog，打开就有），和证据接口的证据链
 * （原式、代入式、输入、封存，点开才取）。接口取不到（老后端、没有运行 id 的预览）
 * 就只显示第一层，并照实说缺了什么。
 *
 * 只信封存链：正文是按成果上的 _evidence.doc_artifact 取的（成果可以改写、工件表可以
 * 事后插行），接口是从封存范围内的事件找的文档。两边不是同一份时，接口的封存状态和
 * 证据链说的是另一份报告——不能挂到正文上画成绿的，要醒目地说正文不是封存的那一份。
 *
 * 三期多了两种步骤：表或字段（出现在哪几次查询里、表结构快照的同步时间、字段类型；可疑的
 * 名字给出最接近的已知名字），逐字引文（原文所在的文档和片段，引文在原文里的位置高亮）。
 * 画布右栏里点开片段时，把证据路径交给画布（evidenceHost），节点名点一下就对准那个节点。
 *
 * 四期多了「模型的解释」：只对结论句显示——裁判模型的判定、理由（斜体）、带「模型判断 · 模型名 · 非确定」
 * 的徽标，封存之后按需追加的另写「封存后追加」。探索运行里还没判过的句子给「请模型判断这句」；到了上限
 * 写明是哪个上限、到哪里调。点句末徽标打开的是整句（kind: 'unit'）：句子、模型的解释、挂的依据、封存。
 *
 * 裁判拆档（JR）：证据不足的写明缺什么；裁判模型和写作模型相同、模型不在价格表里时在「模型的解释」下提醒；
 * 表名实体步骤列出字段清单；讲方法、讲结构的句子在「挂的依据」下直接列出 SQL 和字段清单。
 *
 * Excel 导入期 4：上传表格的单元格多三节——数据版本、推断的来源、相关核对（EvidenceProvenance）。只在片段是单元格
 * 引用、且片段接口的查询步骤带 provenance: true 时请求推断来源接口；推断来源的缓存只在面板开着期间有效。
 *
 * 数字推演（WhatIf.tsx）：指标步骤上的「推演」展开推演区，改输入的值、在查询步骤里勾掉几行，看这个数和报告会变成
 * 多少。推演不是出具，不写运行记录、不改报告。勾选框只在推演时出现在查询步骤的结果窗口里，只有这个指标用到的行能勾，
 * 表格底下带着推演结果。
 *
 * 核查状态（期 4）：指标步骤多一行双盲复算的结果（一致、不一致和审核人选了什么、未能完成、已跳过），选了盲算、两边都
 * 不对、没选、未能完成的写明报告里的数没换、已记为缺口；句子被反方审过的（点开整句、点开句中的数字都一样）多一节反方
 * 审稿，用审批卡那一块的单句展示（run/ChallengeSummary 的 scope="claim"）：这一句的判断、它可能没反查成的说明，展开看
 * 反方的理由和查询，不画报告级的标题和计数，不说出具会不会降档（运行已经结束，看报告的出具横幅）。正文里被审过的句子
 * 挂句末标记（run/EvidenceDoc），点开就是整句面板；片段接口还没答回来、答不了时，这一节先按标记（marks，证据图）写出
 * 判断，说清在等什么、给重试（ClaimVerdictPending）。取自遮罩列的输入，服务端已把值换成「已遮罩」，标签照样写「已遮罩」，
 * 输入来源里只说核对结果、不写值；遮没遮核对不了的（快照读不出来）同样不显示值，写明原因。类型都在 types.ts
 * （EvidenceDualCheck、EvidenceUnitChallenge、EvidenceInput 的 masked / mask_unverified）。
 *
 * 口径账本（next-round 02 做法 3、6）：数字、取值片段的出处链最上面多「口径」一步（run/LedgerStep 的 LedgerPart）——
 * 来自口径「X」v3（受管）· 参数 · 等价已证明 / 助手选用 · 按口径正式出具；相关口径的差在哪和看差多少（口径对账）；这次的
 * 推理。比对在封存之后做，不在封存范围内，脚注写明；比对还没出结果时写「正在比对口径」并按退避再取。
 *
 * 四种摆法：side 从右侧弹出（宽屏），drawer 从底部抽出（窄屏），inline 在 360px 的
 * 画布右栏里直接栏内展开，dock 放进页面给的一块常驻位置（记录页的「证据」页签）。
 * 都不是模态的：开着面板照样能在正文里走。
 */

/**
 * 期 4：证据接口在指标步骤上给的双盲复算状态（类型 EvidenceDualCheck，照 api/evidence.py 的 _dual_check_of）。没有双盲
 * 复算复核过这份指标集时没有这个键；状态不是一个词的不认
 */
function dualCheckOf(step?: EvidenceStep): EvidenceDualCheck | null {
  const raw: unknown = step?.dual_check
  return raw && typeof raw === 'object' && typeof (raw as EvidenceDualCheck).status === 'string' ? raw as EvidenceDualCheck : null
}

const CHALLENGE_VERDICTS = ['stands', 'weakened', 'refuted', 'inconclusive'] as const

/**
 * 句子上的反方审稿判断（类型 EvidenceUnitChallenge，照 _challenge_of）认不认得：判断是一个词，计数四项都是数。单句展示
 * 不画计数，可要拿它和 unreviewed 对一下「无法判断」是不是没反查成；缺了计数不是服务端的形状，宁可不画也不半截地画
 */
function challengeOf(unit: EvidenceSegmentDetail['unit']): EvidenceUnitChallenge | null {
  const raw: unknown = unit?.challenge
  if (!raw || typeof raw !== 'object') return null
  const c = raw as EvidenceUnitChallenge
  const counts = c.counts && typeof c.counts === 'object' ? c.counts : null
  if (typeof c.verdict !== 'string' || !c.verdict || !counts
    || !CHALLENGE_VERDICTS.every((v) => typeof counts[v] === 'number' && Number.isFinite(counts[v]))) return null
  return c
}

/** 取自遮罩列的输入：服务端把值换成「已遮罩」，masked 写明是哪几列 */
function maskedOf(input: EvidenceInput): string[] {
  const raw: unknown = input.masked
  return Array.isArray(raw) ? raw.filter((c): c is string => typeof c === 'string' && !!c) : []
}

/** 遮没遮核对不了的输入（快照、取值链上的产出读不出来）：值换成「无法核对」，mask_unverified 是原因 */
function unverifiedOf(input: EvidenceInput): string {
  const raw: unknown = input.mask_unverified
  return typeof raw === 'string' ? raw : ''
}

export type PanelMode = 'side' | 'drawer' | 'inline' | 'dock'
export type PanelView = { kind: 'seg'; id: string } | { kind: 'unit'; id: string } | { kind: 'violations' }

export function EvidencePanel({ id, mode, doc, artifact, runId, runClass, marks, view, onClose, onLocate, onViolations }: {
  id: string
  mode: PanelMode
  doc: EvidenceDocData
  /** 正文这份文档的工件 id：和证据接口报的封存文档比对，不是同一份就不信接口给的链 */
  artifact?: string
  runId?: string
  /** 运行类别：没开裁判的文档靠它认探索运行（「请模型判断这句」只在探索运行里有） */
  runClass?: string
  /**
   * 正文句末标记用的反方审稿判断（证据图的 challenge_verdicts，正文已经按这一份对过工件 id）：片段接口的 unit.challenge
   * 还没答回来、答不了时，「反方审稿」一节先按它写出判断
   */
  marks?: EvidenceChallengeVerdicts
  view: PanelView
  onClose: () => void
  /** 跳到正文里的某个片段（违规清单里的「定位」） */
  onLocate: (segId: string) => void
  onViolations: () => void
}) {
  const titleId = `${id}-title`
  // 推断的来源只在面板这次打开期间缓存：回答里有当前状态（原件已清除、接受已作废）和取决于数据文件现状的结论，
  // 关掉面板（卸载）或换了运行就作废，再打开重新取。同一次打开里来回点同一片段只取一次
  const holdProvenance = useEvidence((s) => s.holdProvenance)
  useEffect(() => (runId ? holdProvenance(runId) : undefined), [runId, holdProvenance])
  const found = useMemo(() => (view.kind === 'seg' ? findSeg(doc, view.id) : null), [doc, view])
  const claimAt = useMemo(() => (view.kind === 'unit' ? findUnit(doc, view.id) : null), [doc, view])
  const overlay = useVerdicts(runId, doc.node_id || undefined)
  const claimVerdict = claimAt ? unitVerdict(claimAt.unit, overlay) : undefined
  const claimState = verdictState(claimVerdict)
  const state = found ? segmentState(found.seg) : claimState
  const meta = state ? EVIDENCE_STATE[state] : null

  const onKeyDown = (e: ReactKeyboardEvent) => {
    if (e.key !== 'Escape' || isComposing(e) || e.defaultPrevented) return
    e.preventDefault()
    // 画布上在 window 上听 Esc 的（收起检查器之类）不该跟着动
    e.stopPropagation()
    onClose()
  }

  const title = view.kind === 'violations' ? EVIDENCE_TEXT.violations
    : found ? segName(found.seg)
    : claimAt ? unitText(claimAt.unit).trim() || JUDGE_TEXT.claim : EVIDENCE_TEXT.panelTitle
  // 有出处的实体、引文：徽标写得更具体（「有出处 · 逐字引文」），颜色和字形照同一套
  const kind = found && state === 'deterministic' ? segmentKind(found.seg) : null
  const badge = kind ? { ...meta!, label: EVIDENCE_KIND_STYLE[kind].label, glyph: EVIDENCE_KIND_STYLE[kind].glyph } : meta
  const frame = mode === 'side'
    ? 'ev-panel-side fixed bottom-0 right-0 top-0 z-40 flex flex-col border-l bg-panel shadow-elev-3'
    : mode === 'drawer'
      ? 'ev-panel-rise fixed bottom-0 left-0 right-0 z-40 flex flex-col rounded-t-lg border-t bg-panel shadow-elev-3'
      : mode === 'dock'
        ? 'flex h-full min-h-0 flex-col bg-panel'
        : 'ev-panel-rise my-2 flex flex-col rounded-lg border bg-panel'

  return (
    <section
      id={id}
      role="region"
      aria-labelledby={titleId}
      data-evidence-panel={mode}
      className={clsx(frame, 'text-xs')}
      style={mode === 'side' ? { width: 'min(380px, 100vw)' } : mode === 'drawer' ? { maxHeight: '65vh' } : undefined}
      onKeyDown={onKeyDown}
    >
      <header className="flex items-center gap-2 border-b px-3 py-2">
        {mode === 'inline' && (
          <button type="button" className="btn btn-xs btn-ghost -ml-1" onClick={onClose} aria-label={EVIDENCE_TEXT.back}>
            <ArrowLeft size={12} aria-hidden /> {EVIDENCE_TEXT.back}
          </button>
        )}
        {badge && (
          // 概率性的判定（结论句）用虚线框：和系统核对过的「有出处」连框线都不一样
          <span className="chip shrink-0"
                style={{ color: badge.color, borderColor: badge.color, borderStyle: badge.line === 'badge' ? 'dashed' : undefined }}
                data-ev-badge={badge.code}>
            {badge.glyph && <span aria-hidden>{badge.glyph}</span>}{badge.label}
          </span>
        )}
        <h3 id={titleId} tabIndex={-1} className="mono min-w-0 flex-1 truncate text-sm font-semibold outline-none">
          {title}
        </h3>
        {mode !== 'inline' && (
          <button type="button" className="btn btn-xs btn-ghost -mr-1" onClick={onClose} aria-label={EVIDENCE_TEXT.close}
                  title={`${EVIDENCE_TEXT.close}（Esc）`}>
            <X size={13} aria-hidden />
          </button>
        )}
      </header>
      <div className={clsx('space-y-3 px-3 py-2.5 leading-relaxed', mode !== 'inline' && 'min-h-0 flex-1 overflow-y-auto')}
           data-ev-body="">
        {view.kind === 'violations'
          ? <ViolationList doc={doc} onLocate={onLocate} />
          : found
            ? <SegmentBody panelId={id} doc={doc} artifact={artifact} runId={runId} runClass={runClass} seg={found.seg}
                           unit={found.unit} block={found.block} mark={marks?.[found.unit.id]} onViolations={onViolations}
                           onLocate={onLocate} />
            : claimAt
              ? <ClaimBody doc={doc} artifact={artifact} runId={runId} runClass={runClass} unit={claimAt.unit}
                           block={claimAt.block} mark={marks?.[claimAt.unit.id]} />
              : <p className="text-dim">{NONE}</p>}
      </div>
    </section>
  )
}

function findSeg(doc: EvidenceDocData, id: string): { seg: EvidenceSegment; unit: EvidenceUnit; block: EvidenceBlock } | null {
  for (const block of doc.blocks ?? []) {
    for (const unit of block.units ?? []) {
      const seg = (unit.segments ?? []).find((s) => s.id === id)
      if (seg) return { seg, unit, block }
    }
  }
  return null
}

function findUnit(doc: EvidenceDocData, id: string): { unit: EvidenceUnit; block: EvidenceBlock } | null {
  for (const block of doc.blocks ?? []) {
    const unit = (block.units ?? []).find((u) => u.id === id)
    if (unit) return { unit, block }
  }
  return null
}

/** 面板里的一节：小标题 + 内容 */
function Part({ title, children, ...rest }: { title: string; children: ReactNode } & Record<`data-${string}`, string>) {
  return (
    <div {...rest}>
      <div className="mb-1 text-2xs font-medium text-faint">{title}</div>
      {children}
    </div>
  )
}

/** 定位到的那一步描一下边（CSS 的 [data-flash]，减少动效时由全局规则停掉） */
function flashOnce(el: HTMLElement) {
  el.dataset.flash = 'focus'
  setTimeout(() => { if (el.dataset.flash === 'focus') delete el.dataset.flash }, 1600)
}

function SegmentBody({ panelId, doc, artifact, runId, runClass, seg, unit, block, mark, onViolations, onLocate }: {
  panelId: string
  doc: EvidenceDocData; artifact?: string; runId?: string; runClass?: string; seg: EvidenceSegment; unit: EvidenceUnit
  block: EvidenceBlock
  /** 正文标记上这一句的反方审稿判断（证据图）：片段接口答回来之前先按它写 */
  mark?: string
  onViolations: () => void
  /** 跳到正文里的某个片段（推演区「报告中会变的地方」的定位） */
  onLocate: (segId: string) => void
}) {
  const report = doc.node_id || undefined
  const key = runId ? segmentKey(runId, seg.id, report) : ''
  const slot = useEvidence((s) => (key ? s.segments[key] : undefined))
  const graph = useEvidence((s) => (runId ? s.graphs[runId] : undefined))
  const loadSegment = useEvidence((s) => s.loadSegment)
  const loadGraph = useEvidence((s) => s.loadGraph)
  useEffect(() => {
    if (runId) void loadSegment(runId, seg.id, report)
  }, [runId, seg.id, report, loadSegment])
  const answered = slot?.status === 'ok' ? slot.data : undefined
  // 接口答的是另一份报告（封存的那份不是正文这份）：它的链和封存状态一概不用
  const foreign = docForeign(answered, { artifact, node: report, seg })
  const detail = foreign ? undefined : answered
  // 片段接口没给封存状态（或者取不到）时，退回整次运行的证据图
  const needGraph = !!runId && !foreign && (slot?.status === 'error' || (slot?.status === 'ok' && !answered?.seal))
  useEffect(() => {
    if (needGraph && runId) void loadGraph(runId)
  }, [needGraph, runId, loadGraph])
  const graphData = needGraph && graph?.status === 'ok' ? graph.data : undefined
  // 证据图兜底时也得先认出正文这份文档在不在封存的报告里
  const inGraph = graphDoc(graphData, artifact)
  const docIssue: 'foreign' | 'tampered' | null = foreign || inGraph === 'foreign' ? 'foreign'
    : inGraph === 'tampered' ? 'tampered' : null
  useLateVerdict(runId, report, docIssue ? undefined : detail?.unit, unit.id)

  const state = segmentState(seg)
  const cite = seg.cite
  const entry = cite?.alias ? doc.catalog?.[cite.alias] : undefined
  const chain: EvidenceStep[] = useMemo(() => (Array.isArray(detail?.chain)
    ? detail.chain.filter((s) => s && typeof s === 'object') : []), [detail])
  const metricStep = chain.find((s) => s.step === 'metric')
  const inputStep = chain.find((s) => s.step === 'run_input')
  const entityStep = chain.find((s) => s.step === 'entity')
  const quoteStep = chain.find((s) => s.step === 'quote')
  const queries = chain.filter((s) => s.step === 'query')
  const isMetric = cite?.kind === 'metric' || entry?.kind === 'metric' || !!metricStep
  const isInput = cite?.kind === 'input' || entry?.kind === 'input' || !!inputStep
  // 三期：有出处的表名字段名、引文（原话对不上的引文也画这一节：说清原文在哪、对不上）
  const isEntity = !isMetric && !isInput && seg.kind === 'entity' && state === 'deterministic'
  const isQuote = !isMetric && !isInput && seg.kind === 'quote'
  // 报告直接引用的查询单元格（[[v:Q3.r5.amount]]、整表里的一格）：证据链就是那一次查询
  const isCell = !isMetric && !isInput && !isEntity && !isQuote
    && (cite?.kind === 'cell' || entry?.kind === 'query' || !!queries.length)
  const bad = state === 'none' || state === 'suspect' || state === 'unverified'
  const closest = closestNames(detail?.closest ?? entityStep?.closest)
  // 期 4：推断的来源。条件写死成「片段是单元格引用、它的查询步骤带了提示」：期 4 之前的运行、手工源（查询步骤没有
  // 提示）、指标片段（cite.kind 是 metric，哪怕它链上的查询步骤误带了提示）一个请求都不多发。接口答的是另一份报告
  // （docForeign）时 detail 为空、链是空的，也就不请求
  const provenanceOn = !!runId && cite?.kind === 'cell' && queries.some((q) => q.provenance === true)

  // 画布右栏：点开片段时把证据路径交给画布（产出证据的节点实线、报告虚线），链取回来后再补全
  const host = useEvidenceHost()
  const onTrace = host.onTrace
  const trace = useMemo(() => evidenceTrace(seg, doc, chain), [seg, doc, chain])
  const traceKey = `${trace.producers.join(',')}|${trace.consumers.join(',')}`
  useEffect(() => {
    if (!onTrace) return
    const text = segName(seg)
    onTrace({ label: `证据 ${text.length > 16 ? `${text.slice(0, 15)}…` : text}`, ...trace })
    return () => onTrace(null)
    // trace 按内容比（traceKey）：同一条路径不重画
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onTrace, seg.id, traceKey])
  const inputs: EvidenceInput[] = metricStep?.inputs?.length
    ? metricStep.inputs
    : chain.filter((s) => s.step === 'input').map((s) => s as EvidenceInput)
  const masked = (Array.isArray(detail?.redacted?.columns) ? detail.redacted.columns : []).map(String).filter(Boolean)
  const workflows = useCatalog((s) => s.workflows)
  const workflowName = (id?: string) => (id ? workflows.find((w) => w.id === id)?.name : undefined)
  // 输入标签、输入来源那一行点一下：焦点跳到对应的查询步骤，描一下边
  const queryId = (i: number) => `${panelId}-q${i}`
  const jumpTo = useCallback((i: number) => {
    const part = document.getElementById(`${panelId}-q${i}`)
    const head = part?.querySelector<HTMLElement>('[data-ev-query-head]')
    if (!part || !head) return
    head.focus({ preventScroll: true })
    head.scrollIntoView({ block: 'nearest' })
    flashOnce(part)
  }, [panelId])
  // 转抄另一张口径卡的指标（对账卡、召回卡的「口径」那一项）：服务端跨卡接上的那几步，输入点一下跳到对应的那一步
  const upstreams = chain.filter((s) => s.step === 'upstream')
  const upstreamId = (i: number) => `${panelId}-u${i}`
  const jumpToUpstream = useCallback((i: number) => {
    const part = document.getElementById(`${panelId}-u${i}`)
    const head = part?.querySelector<HTMLElement>('[data-ev-upstream-head]')
    if (!part || !head) return
    head.focus({ preventScroll: true })
    head.scrollIntoView({ block: 'nearest' })
    flashOnce(part)
  }, [panelId])
  /** depth：这个输入在第几层（指标自己的输入是 1，转抄的那一步里的输入是那一步的 depth + 1） */
  const linkOf = (inp: EvidenceInput, depth = 1): InputLink => {
    const u = inp.via === 'metric' && inp.upstream
      ? upstreams.findIndex((s) => s.of === inp.path && (s.depth ?? 1) === depth) : -1
    if (u >= 0) {
      const card = host.nodeLabel?.(upstreams[u].node_id ?? '') || upstreams[u].label || upstreams[u].node_id || ''
      return { index: u, alias: card, text: EVIDENCE_TEXT.gotoUpstream(card), go: () => jumpToUpstream(u) }
    }
    const i = queryOf(inp, queries)
    return i >= 0 ? { index: i, alias: queries[i].alias ?? '', go: () => jumpTo(i) } : undefined
  }
  const violations = (doc.violations ?? []).filter((v) => v.segment === seg.id)
  // 数字推演：按（运行、报告、口径卡、指标）认一份草稿。同一个指标的几处引用之间来回切，推演不收起（定位到另一处
  // 引用也是这样）；换了指标、换成别的片段才收起清空。卡和指标先按文档目录认：切片段时证据链还没回来，也认得出是同一个
  const whatifCard = entry?.kind === 'metric' ? entry.node_id : metricStep?.node_id
  const whatifMetric = entry?.kind === 'metric' ? entry.locator?.metric : metricStep?.metric
  const whatif = useWhatIfDraft(runId && isMetric && whatifCard && whatifMetric
    ? [runId, report ?? '', whatifCard, whatifMetric].join('\u0000') : '')
  // 证据链回来了、正文就是封存的那一份才给推演：链是别的报告的，推演出来的数也对不上正文。推演只给搭建者及以上（接口
  // 同样只给，main.py 的 whatif 路由）：开了登录的使用者看不到这个按钮，不让人点了再被拒
  const builder = useAtLeast('builder')
  // 开了登录的使用者：遮罩对他是边界（原值不给），面板底下的遮罩说明换一句
  const maskedForMe = useLoginOn() && !builder
  const whatifReady = builder && !!runId && !docIssue && !!metricStep && !!whatifCard && !!whatifMetric
  const whatifOpen = whatif.open && whatifReady
  const whatifId = `${panelId}-whatif`
  // 推演的请求和答复也放在这一层：查询步骤里勾行的表格底下要带着推演结果，和推演区是同一份
  const whatifRequest = useMemo(() => (whatifOpen && whatifCard && whatifMetric
    ? { ...(report ? { report } : {}), card: whatifCard, metric: whatifMetric, overrides: whatif.overrides } : null),
  [whatifOpen, report, whatifCard, whatifMetric, whatif.overrides])
  const whatifView = useWhatIfResult(runId, whatifRequest)
  // 每份快照里这个指标用得到的行：别的行勾了不影响结果，表格里不让勾
  const whatifUsable = useMemo(() => usableRows(pickMetric(whatifView.data, whatifMetric)), [whatifView.data, whatifMetric])
  // 能勾行的查询步骤：指标的输入（单元格取数、Agent 字段）落在它上面。合并查询的输入步骤不给——指标引用的是合并结果。
  // 整个指标推演不了（口径卡没记输入、用到了遮罩列、有输入还原不了）时一行都不给勾
  const pickable = new Set(wholeReason(whatifView.data, whatifMetric) ? []
    : inputs.map((inp) => inp.artifact).filter((a): a is string => !!a))
  const queryLink = (artifactOf?: string) => {
    const i = artifactOf ? queries.findIndex((q) => q.artifact === artifactOf && !q.merged_into) : -1
    return i >= 0 ? { alias: queries[i].alias ?? '', go: () => jumpTo(i) } : undefined
  }
  const seal: EvidenceSeal | undefined = docIssue ? undefined : detail?.seal ?? graphData?.seal
  const reason = reasonOf(seg) || detail?.note || ''
  const verdict = sealVerdict(seal, {
    foreign: !!docIssue,
    pending: !!runId && !seal && (slot?.status === 'loading' || graph?.status === 'loading'),
    noRun: !runId,
    docOnly: state !== 'deterministic',
    viaGraph: slot?.status === 'error',
  })
  const pending = !!runId && !docIssue && (!slot || slot.status === 'loading')
  const chainFailed = !runId ? EVIDENCE_TEXT.noRun
    : docIssue ? EVIDENCE_TEXT.chainForeign
    : slot?.status === 'error' ? EVIDENCE_TEXT.chainMissing : ''
  const segChallenge = docIssue ? null : challengeOf(detail?.unit)
  const segWait = waitOf(slot?.status, !!runId)

  return (
    <>
      {docIssue && (
        // 正文不是封存的那一份：比「有出处」三个字重要得多，放在最前面
        <IntegrityList code="doc" items={[
          docIssue === 'tampered' ? EVIDENCE_TEXT.integrity.docHash : EVIDENCE_TEXT.integrity.doc,
          ...(foreign?.sealedText != null ? [EVIDENCE_TEXT.integrity.sealedText(foreign.sealedText)] : []),
        ]} />
      )}

      <Part title={EVIDENCE_TEXT.sentence} data-ev-sentence="">
        <Sentence unit={unit} active={seg.id} />
      </Part>

      {state === 'deterministic' && (isMetric || isCell) && !docIssue && (
        // 口径账本：出处链最上面的「口径」一步（来自口径 / 相关口径 / 这次的推理）。比对在封存之后做，这一步按运行另取
        // （GET /runs/{id}/ledger，还在比对时按退避再取）；片段接口的 ledger 键先顶上
        <LedgerPart runId={runId} runClass={runClass} report={report} segId={seg.id} segText={seg.text}
                    segLedger={detail?.ledger} />
      )}

      {bad && (
        <Part title={state === 'suspect' ? '标记为可疑的原因' : state === 'unverified' ? '无法核实的原因' : '无证据的原因'}
              data-ev-reason="">
          <p style={{ color: state === 'unverified' ? 'var(--text-dim)' : 'var(--st-waiting)' }}>{reason}</p>
          {/* 违规的原话多半是「引用 … 解析不了：<原因>」，和上面那句重复的不再列；裸数字那条说的是怎么改，留着 */}
          {violations.filter((v) => v.message && !(reason && v.message.includes(reason))).map((v, i) => (
            <p key={i} className="mt-1 text-dim">{v.message}</p>
          ))}
          {seg.ref && <p className="mono mt-1 text-2xs text-faint">[[{seg.ref}]]</p>}
          {(state === 'suspect' || state === 'unverified') && runId && !docIssue && (
            // 可疑的名字：给最接近的已知名字，多半是写错了一个字（接口按封存的台账重建目录找的）
            <div className="mt-1.5" data-ev-closest={closest.length ? '' : 'none'}>
              <div className="mb-0.5 text-2xs text-faint">{EVIDENCE_TEXT.closest}</div>
              {closest.length ? (
                <ul className="flex flex-wrap gap-1">
                  {closest.map((n) => <li key={n} className="chip mono" data-ev-closest-name="">{n}</li>)}
                </ul>
              ) : (
                <p className="text-2xs text-dim">{pending ? EVIDENCE_TEXT.entityPending : EVIDENCE_TEXT.closestNone}</p>
              )}
              {entityStep?.checked && (
                <p className="mt-1 text-2xs text-faint" data-ev-checked="">
                  {EVIDENCE_TEXT.checked(entityStep.checked.schemas ?? 0, entityStep.checked.queries ?? 0)}
                </p>
              )}
            </div>
          )}
        </Part>
      )}

      {isEntity && (
        <EntityPart step={entityStep} entry={entry} cite={cite} seal={seal} pending={pending}
                    failed={chainFailed} catalog={doc.catalog} />
      )}

      {isQuote && (
        <QuotePart step={quoteStep} cite={cite} quote={seg.text} resolved={state === 'deterministic'} seal={seal}
                   pending={pending} failed={chainFailed} node={entry?.node_id} label={entry?.label} />
      )}

      {isMetric && (
        <MetricPart step={metricStep} entry={entry} inputs={inputs} runId={runId} runClass={runClass} seal={seal}
                    pending={pending}
                    failed={chainFailed}
                    resolved={state === 'deterministic'}
                    linkOf={linkOf}
                    workflowName={workflowName(pinnedWorkflow(metricStep))}
                    whatif={whatifReady ? { open: whatifOpen, onToggle: whatif.toggle, controls: whatifId } : undefined}>
          {whatifOpen && runId && whatifMetric && (
            <WhatIf id={whatifId} runId={runId} report={report} metric={whatifMetric} draft={whatif} view={whatifView}
                    queryOf={queryLink} onLocate={onLocate} />
          )}
        </MetricPart>
      )}

      {isMetric && inputs.some(notableInput) && (
        <SourcesPart inputs={inputs.filter(notableInput)} linkOf={(inp) => linkOf(inp)} />
      )}

      {isMetric && upstreams.map((u, i) => (
        <UpstreamPart key={`${u.artifact ?? ''}-${u.metric ?? ''}-${i}`} id={upstreamId(i)} step={u} seal={seal}
                      linkOf={(inp) => linkOf(inp, (u.depth ?? 1) + 1)} />
      ))}

      {isInput && !isMetric && (
        <Part title={EVIDENCE_TEXT.input} data-ev-input-step="">
          <p className="mono">
            {inputStep?.field ?? entry?.locator?.field ?? cite?.locator?.field ?? cite?.ref} ={' '}
            <span className="tnum">{valueText(inputStep?.value ?? entry?.value ?? cite?.value)}</span>
          </p>
          <Integrity step={inputStep} seal={seal} />
        </Part>
      )}

      {isCell && !queries.length && state === 'deterministic' && (
        // 接口没给查询步骤（取不到、老后端、正在取）：只说文档目录里记着的那次查询，不画假的行。
        // 解析不了的单元格引用（行越界、没有这一列、受管没声明 cells）接口本来就不给链，上面已经说了原因，
        // 再摆一块「这一次没取到」就像是接口出了错
        <QueryFallback entry={entry} alias={cite?.alias} where={locatorText(cite?.locator)}
                       pending={pending}
                       failed={chainFailed} />
      )}

      {queries.map((q, i) => (
        <QueryPart key={`${q.artifact ?? ''}-${i}`} id={queryId(i)} step={q} seal={seal} masked={masked}
                   pick={whatifOpen && whatifMetric && q.artifact && !q.merged_into && pickable.has(q.artifact)
                     ? {
                       excluded: whatif.excluded(q.artifact),
                       onToggle: (row) => whatif.toggleRow(q.artifact!, row),
                       usable: whatifUsable ? (row) => inSpans(whatifUsable.get(q.artifact!), row) : undefined,
                       // 勾了行，新值就在表格底下：推演区在上面，往往已经不在视野里
                       summary: <WhatIfSummary view={whatifView} metric={whatifMetric} onBack={() => focusResult(whatifId)} />,
                       busy: whatifView.status === 'loading',
                     }
                     : undefined} />
      ))}

      {provenanceOn && runId && <EvidenceProvenance runId={runId} segId={seg.id} report={report} />}

      {state === 'deterministic' && !isMetric && !isInput && !isCell && !isEntity && !isQuote && (
        <p className="text-dim">{sourceOf(seg, doc) || detail?.note}</p>
      )}

      <JudgePart doc={doc} unit={unit} block={block} runId={runId} runClass={runClass}
                 onDemand={detail?.unit?.on_demand} answered={detail?.unit?.verdict} pending={pending} />

      {/* 反方审稿对这一句的判断：点句中的数字也看得到。片段接口还没答、答不了时先按正文标记写判断。正文不是封存的那一份
          时都不用：服务端认的是封存的那份文档 */}
      {!docIssue && (segChallenge || mark) && (
        <ChallengePart challenge={segChallenge} mark={mark} wait={segWait} unit={unit} report={report}
                       onRetry={() => { if (runId) void loadSegment(runId, seg.id, report) }} />
      )}

      {host.onNode && (trace.producers.length > 0 || trace.consumers.length > 0) && (
        // 画布右栏：这段证据经过的节点，点一下在画布上选中并对准
        <Part title="画布上的节点" data-ev-nodes="">
          <div className="flex flex-wrap items-center gap-1">
            {trace.producers.map((n) => <NodeChip key={n} id={n} />)}
            {trace.consumers.length > 0 && trace.producers.length > 0 && <span className="text-faint" aria-hidden>→</span>}
            {trace.consumers.map((n) => <NodeChip key={n} id={n} />)}
          </div>
        </Part>
      )}

      <Part title={EVIDENCE_TEXT.seal} data-ev-seal="">
        <SealLine status={verdict.status} label={verdict.label} />
      </Part>

      {!!doc.violations?.length && (
        <button type="button" className="btn btn-xs btn-ghost -ml-1" onClick={onViolations} data-ev-open-violations="">
          <ListChecks size={11} aria-hidden /> {EVIDENCE_TEXT.violations}（{formatNumber(doc.violations.length)}）
        </button>
      )}

      {!!masked.length && (
        // 用户拍板：界面上写明遮罩的边界。放在最后，读完证据再看到这句。对开了登录的使用者遮罩是边界（原值不给），
        // 对搭建者、管理员和未开启登录时只减少暴露——同一句话不能对两种人都说
        <p className="border-t pt-2 text-2xs leading-relaxed text-faint" data-ev-mask-note={maskedForMe ? 'user' : ''}>
          {maskedForMe ? EVIDENCE_TEXT.maskNoteUser(masked) : EVIDENCE_TEXT.maskNote(masked)}
        </p>
      )}
    </>
  )
}

/**
 * 片段接口答的这一句带着封存之后追加的判定（后端按最新的 evidence.judged 叠上的）：记进缓存，正文的句末
 * 徽标、横幅的计数跟着变。接口答的是另一份报告时调用方不传 unit
 */
function useLateVerdict(runId: string | undefined, report: string | undefined,
  answered: { id?: string; verdict?: EvidenceVerdict } | undefined, unitId: string) {
  const noteVerdicts = useEvidence((s) => s.noteVerdicts)
  const late = answered?.verdict?.post_seal && (!answered.id || answered.id === unitId) ? answered.verdict : undefined
  const sig = late ? JSON.stringify(late) : ''
  useEffect(() => {
    if (runId && late) noteVerdicts(runId, report, { [unitId]: late })
    // 按内容比：同一条判定不重记
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, report, unitId, sig, noteVerdicts])
}

/**
 * 点句末徽标打开的整句：句子、模型的解释、挂的依据、封存。封存状态和叠上的判定借这句第一个片段的
 * 片段接口取（接口答的是另一份报告时一概不用）
 */
function ClaimBody({ doc, artifact, runId, runClass, unit, block, mark }: {
  doc: EvidenceDocData; artifact?: string; runId?: string; runClass?: string; unit: EvidenceUnit; block: EvidenceBlock
  /**
   * 正文标记上这一句的反方审稿判断（证据图）：片段接口答回来之前、答不了时先按它写。没有可点片段、也没有裁判判定的句子
   * 只能从这枚标记点进来
   */
  mark?: string
}) {
  const report = doc.node_id || undefined
  const first = (unit.segments ?? []).find((s) => s.kind !== 'structural')
  const key = runId && first ? segmentKey(runId, first.id, report) : ''
  const slot = useEvidence((s) => (key ? s.segments[key] : undefined))
  const loadSegment = useEvidence((s) => s.loadSegment)
  useEffect(() => {
    if (runId && first) void loadSegment(runId, first.id, report)
  }, [runId, first, report, loadSegment])
  const answered = slot?.status === 'ok' ? slot.data : undefined
  const foreign = first ? docForeign(answered, { artifact, node: report, seg: first }) : null
  useLateVerdict(runId, report, foreign ? undefined : answered?.unit, unit.id)
  const overlay = useVerdicts(runId, report)
  const verdict = unitVerdict(unit, overlay)
  const seal = foreign ? undefined : answered?.seal
  const late = !!verdict?.post_seal
  const sealed = sealVerdict(seal, {
    foreign: !!foreign, pending: !!runId && !seal && slot?.status === 'loading', noRun: !runId, docOnly: true,
  })
  // 一整句没有「这一段的证据」可言：封存核对通过时只说报告文档封存了，节点里当场判的判定跟着一起封存
  const sealLabel = sealed.status !== 'done' ? sealed.label
    : verdict && !late && isJudged(verdict) ? JUDGE_TEXT.sealedWithDoc : JUDGE_TEXT.sealedDoc
  // 挂的依据：说明按证据图（服务端已把用到遮罩列的指标的值换成「已遮罩」）写，不直接读文档目录里的「名称 = 渲染值」。
  // 有指标依据时才去取证据图（正文挂上时一般已经取过，取过的不再发）
  const graphSlot = useEvidence((s) => (runId ? s.graphs[runId] : undefined))
  const loadGraph = useEvidence((s) => s.loadGraph)
  const metricCite = (unit.cites ?? []).some((alias) => doc.catalog?.[alias]?.kind === 'metric')
  useEffect(() => {
    if (runId && metricCite) void loadGraph(runId)
  }, [runId, metricCite, loadGraph])
  const graph = !foreign && graphSlot?.status === 'ok' ? graphSlot.data : undefined
  const cites = (unit.cites ?? []).map((alias) => ({ alias, label: citeLabel(alias, doc, graph, report, !!runId) }))
  // 讲方法、讲结构的句子：SQL 和字段清单直接列在挂的依据下面。正文不是封存的那份时不列（链是另一份报告的）
  const basis = useMemo(() => (foreign ? null : claimBasis(unit, doc)), [foreign, unit, doc])
  // 反方审稿对这一句的判断：服务端只认审的就是封存的这一份文档；正文不是那一份时不用。片段接口还没答、答不了时先按正文
  // 标记写（证据图，正文已经按工件 id 对过）
  const challenge = foreign ? null : challengeOf(answered?.unit)
  const wait = first ? waitOf(slot?.status, !!runId) : 'missing'
  return (
    <>
      {foreign && (
        <IntegrityList code="doc" items={[EVIDENCE_TEXT.integrity.doc,
          ...(foreign.sealedText != null ? [EVIDENCE_TEXT.integrity.sealedText(foreign.sealedText)] : [])]} />
      )}
      <Part title={JUDGE_TEXT.claim} data-ev-sentence="">
        <Sentence unit={unit} active="" />
      </Part>
      <JudgePart doc={doc} unit={unit} block={block} runId={runId} runClass={runClass}
                 onDemand={foreign ? undefined : answered?.unit?.on_demand}
                 answered={foreign ? undefined : answered?.unit?.verdict}
                 pending={!!runId && !!first && (!slot || slot.status === 'loading')} />
      {!foreign && (challenge || mark) && (
        <ChallengePart challenge={challenge} mark={mark} wait={wait} unit={unit} report={report}
                       onRetry={() => { if (runId && first) void loadSegment(runId, first.id, report) }} />
      )}
      <Part title={JUDGE_TEXT.cites} data-ev-cites="">
        {cites.length ? (
          <ul className="space-y-0.5">
            {cites.map((c) => (
              <li key={c.alias} className="flex min-w-0 items-baseline gap-1.5">
                <span className="mono shrink-0 font-medium">{c.alias}</span>
                {c.label && <span className="min-w-0 truncate text-dim" title={c.label}>{c.label}</span>}
              </li>
            ))}
          </ul>
        ) : <p className="text-dim">{JUDGE_TEXT.noCites}</p>}
        {basis && <BasisPart basis={basis} doc={doc} runId={runId} />}
      </Part>
      <Part title={EVIDENCE_TEXT.seal} data-ev-seal="">
        <SealLine status={sealed.status} label={sealLabel} />
        {late && (
          // 封存后追加的判定：报告文档封存着，这条判断不在封存范围里——两件事分开说
          <p className="mt-1 flex items-center gap-1.5" data-ev-seal-late="">
            <StatusBadge status="waiting" size={12} decorative animate={false} />
            <span>{JUDGE_TEXT.postSeal}：{JUDGE_TEXT.postSealHint}</span>
          </p>
        )}
      </Part>
    </>
  )
}

/**
 * 反方审稿对这一句的判断（期 4），整句面板、片段面板共用。用审批卡那一块的单句展示（ChallengeSummary 的 scope="claim"）：
 * 只画这一句的判断、它可能没反查成的说明（透传 unreviewed，和整份报告的计数对一下），展开看反方的理由和它执行的查询；
 * 不画报告级的标题、四种判断的计数和别的句子。判断是反方模型给的，那一块自己写明。
 *
 * 不谈出具（issuance={false}，也不传 degrades）：审批卡上预告「出具会降档」，是因为那时运行还没出具。证据面板看的是已经
 * 结束的运行，降没降档、出没出具以报告的出具横幅（封存的出具判定）为准，再说一遍将来时的预告只会和横幅对不上（驳回了、
 * 不予出具、反方审稿不在成果节点上游……）
 *
 * 判断有两个来源：片段接口答的 unit.challenge（带审稿记录和整份报告的计数，单句展示要它），和正文标记用的证据图（mark，
 * 只有判断）。片段接口还在取、取不到、答了却没有可用的 unit.challenge 时，先按标记画出判断，说清在等什么，取不到给重试
 * （ClaimVerdictPending）：用户是点着句末醒目的「被推翻」进来的，没开裁判的报告里这几句也没有别的入口，这一节不能空着
 */
function ChallengePart({ challenge, mark, wait, onRetry, unit, report }: {
  challenge: EvidenceUnitChallenge | null
  mark?: string
  /** 片段接口那一头：还在取、取不到、答了却没有可用的 unit.challenge（只在没有 challenge 时用得上） */
  wait: ClaimWait
  /** 重取这一句的片段接口 */
  onRetry: () => void
  unit: EvidenceUnit; report?: string
}) {
  const reports = useMemo<ApprovalReport[]>(() => (challenge ? [{
    node_id: report ?? '', label: challenge.report_label || report || '',
    challenge: {
      counts: challenge.counts, items: [{ unit: unit.id, text: unitText(unit).trim(), verdict: challenge.verdict }],
      artifact: challenge.artifact ?? null, on_refuted: challenge.on_refuted,
      ...(typeof challenge.unreviewed === 'number' ? { unreviewed: challenge.unreviewed } : {}),
    },
  }] : []), [challenge, unit, report])
  // 按过「重试」：按钮一请求就拿掉了，焦点会掉到页面上。取到了落到完整展示里的「反方的理由与查询」（没有审稿记录、展开
  // 不了时落到这一节上），又没取到落到新的「重试」上；这一会儿用户已经去了别处的不抢
  const boxRef = useRef<HTMLDivElement>(null)
  const retried = useRef(false)
  const settledWait = challenge ? 'ok' : wait
  useEffect(() => {
    if (!retried.current || settledWait === 'loading') return
    retried.current = false
    if (document.activeElement && document.activeElement !== document.body) return
    const box = boxRef.current
    ;(box?.querySelector<HTMLElement>('[data-challenge-toggle], [data-challenge-retry]') ?? box)?.focus()
  }, [settledWait])
  const verdict = challenge?.verdict ?? mark
  if (!verdict) return null
  return (
    <Part title={CHALLENGE_TEXT.claimTitle} data-ev-challenge={verdict}>
      <div ref={boxRef} tabIndex={-1} className="rounded-sm outline-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[color:var(--accent)]">
        {challenge
          ? <ChallengeSummary reports={reports} scope="claim" issuance={false} />
          : <ClaimVerdictPending verdict={verdict} wait={wait} onRetry={() => { retried.current = true; onRetry() }} />}
      </div>
    </Part>
  )
}

/** 片段接口那一头现在的样子，给反方审稿的先行版用：没有运行时不会有答复，算答了却没有 */
function waitOf(status: 'loading' | 'ok' | 'error' | undefined, run: boolean): ClaimWait {
  if (!run) return 'missing'
  return !status || status === 'loading' ? 'loading' : status === 'error' ? 'error' : 'missing'
}

/**
 * 模型的解释：只对结论句。判定、理由（斜体）、「模型判断 · 模型名 · 非确定」的徽标，封存之后按需追加的
 * 另写「封存后追加」；到上限没判的写明哪个上限、到哪里调；改写过一次的写明原句。探索运行里还没判过的
 * （没有判定、未裁判）给「请模型判断这句」。什么都说不上的（正式运行、没开裁判的文档）不画这一节
 */
function JudgePart({ doc, unit, block, runId, runClass, onDemand, answered, pending }: {
  doc: EvidenceDocData; unit: EvidenceUnit; block: EvidenceBlock; runId?: string; runClass?: string
  /**
   * 片段接口答的「这句能不能请模型判断」：有它就照它（运行还没跑完封存、封存被改过这些前端认不出来）；
   * 老后端没有时按运行类别和文档自己认
   */
  onDemand?: EvidenceOnDemand
  /** 片段接口答这句时的判定：接口说它「早于当前规则」（outdated），指的就是这一条 */
  answered?: EvidenceVerdict
  /** 片段接口还在取：先不按运行类别另取一次 */
  pending?: boolean
}) {
  const report = doc.node_id || undefined
  const overlay = useVerdicts(runId, report)
  const verdict = unitVerdict(unit, overlay)
  const eligible = judgeable(unit, block)
  const known = onDemand && typeof onDemand.available === 'boolean' ? onDemand : undefined
  const explore = useExplore(doc, runId, runClass, eligible && !isJudged(verdict) && !known && !pending)
  const ask = useEvidence((s) => (runId ? s.asks[askKey(runId, report, unit.id)] : undefined))
  const judge = useEvidence((s) => s.judge)
  const state = verdictState(verdict)
  const meta = state ? EVIDENCE_STATE[state] : null
  const limit = limitOf(verdict)
  const late = !!verdict?.post_seal
  const rewrite = rewriteOf(doc, unit.id)
  const judgeDoc = !!doc.judge
  // 片段接口说这句封存后追加的判定早于当前的裁判规则（旧取值、改造前判的）：照常给按钮，旧判定旁边注明「按旧规则判定」。
  // 这一页里已经按当前规则重判过（眼前的判定不再是接口答的那一条）就不算了；重判没判成时答复交回的还是那条旧判定
  const outdated = known?.reason === 'outdated' && !!known.available && isJudged(verdict) && sameVerdict(verdict, answered)
  // 请模型判断（POST …/evidence/judge）只给搭建者及以上（api/deps 的 evidence_scope：使用者只读）：使用者不给按钮，
  // 判过的判定照常看
  const builder = useAtLeast('builder')
  const canAsk = builder && !!runId
    && (outdated || (!isJudged(verdict) && (known ? !!known.available : eligible && explore === true)))
  // 探索运行里暂时判不了（还没跑完封存、封存被改过、升级前的运行）：照接口的原话说为什么没有按钮
  const blocked = known && !known.available && BLOCKED.has(known.reason ?? '') ? known.message || '' : ''
  // 这一次按需裁判的答复里没有这一句的判定：接口说了为什么（跳过了、触顶）就照说
  const res = ask?.status === 'ok' ? ask.data : undefined
  const skipped = res?.skipped?.[unit.id]
  const resLimit = !verdict || verdict.status !== 'unjudged' ? null
    : (Array.isArray(res?.limits_hit) && res.limits_hit.find((l) => typeof l === 'string')) || null
  const shownLimit = limit ?? resLimit
  const judged = isJudged(verdict)
  const missing = verdictMissing(verdict)
  // 裁判模型的两条提醒（和写作模型相同、不在价格表里）：封存后追加的看这一次的答复（答复里有这一句时）、再看证据图里
  // 最近一次按需裁判；节点里当场判的看文档的裁判摘要
  const replied = res && judgedVerdicts(res)[unit.id] ? res : null
  const graph = useEvidence((s) => (runId ? s.graphs[runId] : undefined))
  const loadGraph = useEvidence((s) => s.loadGraph)
  const needMeta = !!runId && judged && late && !replied && !graph
  useEffect(() => { if (needMeta && runId) void loadGraph(runId) }, [needMeta, runId, loadGraph])
  const meta0 = (graph?.data?.reports ?? []).find((r) => !report || !r?.node_id || r.node_id === report)?.judge_meta
  const flags = judgeFlags(verdict, { doc: doc.judge, reply: replied, meta: meta0 })
  // 这一次答复的那句话（message）：判定之外接口还想说的，照原话写出来。最要紧的是「运行已经接着跑了，这次的
  // 判定没有记进运行记录」——判定照样交回来、也标着封存后追加，只看徽标会以为它记进去了。已经由别处说过的不重复：
  // 跳过的那一行就是它、上限的那一框说的就是它、和判定里写的理由一字不差
  const resMessage = typeof res?.message === 'string' ? res.message.trim() : ''
  const showMessage = !!resMessage && !(skipped && !judged) && !(shownLimit && isJudgeLimit(resMessage))
    && resMessage !== verdict?.rationale
  // 只对结论句：表格单元格、代码、标题不是一句话，开了裁判的文档里也不画这一节
  const show = !!verdict || (judgeDoc && eligible) || canAsk || !!blocked || ask?.status === 'error' || showMessage
  if (!show) return null
  const asking = ask?.status === 'loading'
  const how = shownLimit ? JUDGE_TEXT.limitHow[late || explore ? 'click' : 'report'][shownLimit] : ''
  return (
    <Part title={JUDGE_TEXT.section} data-ev-judge={verdict?.status ?? 'none'}>
      {verdict && judged && (
        // 「模型判断 · 模型名 · 非确定」只挂在模型真判过的句子上：到上限、没跑成的未裁判也记着裁判模型，
        // 但模型没看过这句，挂上这枚徽标就像它判过一样
        <div className="mb-1 flex flex-wrap items-center gap-1">
          <span className="chip max-w-full truncate" data-ev-judge-badge=""
                style={{ color: 'var(--st-running)', borderColor: 'var(--st-running)', borderStyle: 'dashed' }}
                title={late ? JUDGE_TEXT.postSealHint : JUDGE_TEXT.sealedHint}>
            <Scale size={10} aria-hidden /> {JUDGE_TEXT.badge(verdict.judge)}
          </span>
          {late && (
            <span className="chip" data-ev-post-seal="" style={{ color: 'var(--st-waiting)', borderColor: 'var(--st-waiting)', borderStyle: 'dashed' }}
                  title={JUDGE_TEXT.postSealHint}>
              {JUDGE_TEXT.postSeal}
            </span>
          )}
        </div>
      )}
      {verdict && (
        <div className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
          <p className="font-medium" style={{ color: meta?.color ?? 'var(--text-dim)' }} data-ev-verdict={verdict.status}>
            {meta?.glyph && <span aria-hidden className="mr-1">{meta.glyph}</span>}
            {meta ? meta.label : JUDGE_TEXT.notClaim}
          </p>
          {outdated && (
            <span className="chip text-2xs text-faint" data-ev-judge-outdated="" title={known?.message ?? undefined}>
              {JUDGE_TEXT.outdated}
            </span>
          )}
        </div>
      )}
      {missing && (
        // 证据不足：裁判写明缺的是什么（「SQL」「字段清单」）。说的是证据，不是结论错了：中性字色
        <p className="mt-0.5 [overflow-wrap:anywhere]" data-ev-missing="">
          <span className="text-faint">{JUDGE_TEXT.missingLead}</span>{missing}
        </p>
      )}
      {(flags.sameModel || flags.unpriced) && (
        // 判定照常给出，但读的人要知道它的分量：左侧一道提醒色的竖线加图标，字用正文色——醒目，不刺眼
        <div className="mt-1 space-y-0.5 border-l-2 py-px pl-2 text-2xs leading-relaxed" role="note" data-ev-judge-caution=""
             style={{ borderColor: 'var(--st-waiting)' }}>
          {flags.sameModel && (
            <p className="flex items-start gap-1" data-ev-judge-same=""
               title={flags.model ? JUDGE_TEXT.sameModelTitle(flags.writer ?? flags.model) : undefined}>
              <AlertTriangle size={11} aria-hidden className="mt-0.5 shrink-0" style={{ color: 'var(--st-waiting)' }} />
              <span>{JUDGE_TEXT.sameModel}</span>
            </p>
          )}
          {flags.unpriced && (
            <p className="flex items-start gap-1" data-ev-judge-unpriced="" title={JUDGE_TEXT.noPriceTitle}>
              <AlertTriangle size={11} aria-hidden className="mt-0.5 shrink-0" style={{ color: 'var(--st-waiting)' }} />
              <span>{JUDGE_TEXT.noPrice}</span>
            </p>
          )}
        </div>
      )}
      {showMessage && (
        <p className="mb-1 mt-0.5 text-2xs leading-relaxed" role="note" style={{ color: 'var(--st-waiting)' }} data-ev-judge-message="">
          {resMessage}
        </p>
      )}
      {verdict && !judged && verdict.judge && (
        // 没判的句子只中性地说一句裁判模型是谁（到上限时知道是哪个模型的价钱、哪个模型没跑成）
        <p className="mt-0.5 text-2xs text-faint" data-ev-judge-model="">{JUDGE_TEXT.notJudgedBy(verdict.judge)}</p>
      )}
      {verdict?.rationale && !shownLimit && (
        // 理由是模型写的话：斜体，和系统给的说明分开。最长 120 字、可能夹着 SQL 片段和长的字段名：照原样折行，
        // 不截断，长串也在栏内断开
        <p className="mt-0.5 whitespace-pre-line italic text-dim [overflow-wrap:anywhere]" data-ev-rationale=""><em>{verdict.rationale}</em></p>
      )}
      {shownLimit && (
        <div className="mt-1 rounded border px-2 py-1" role="note" data-ev-judge-limit={shownLimit}
             style={{ borderColor: 'var(--st-waiting)', background: 'var(--st-waiting-soft)' }}>
          <p><span className="font-medium" style={{ color: 'var(--st-waiting)' }}>{JUDGE_TEXT.limit}</span>
            {judgeLimitWords(verdict?.rationale) ? `：${judgeLimitWords(verdict?.rationale)}` : ''}</p>
          {how && (
            <p className="mt-0.5 text-2xs text-dim" data-ev-judge-how="">
              {how}
              {pointsToSettings(how) && builder && (
                <> <Link to="/settings/prefs" className="text-[var(--accent)] underline-offset-2 hover:underline" data-ev-judge-settings="">去设置</Link></>
              )}
            </p>
          )}
        </div>
      )}
      {verdict?.status === 'unjudged' && !shownLimit && verdict.reason !== 'on_demand' && verdict.rationale && (
        <p className="mt-0.5 text-dim" data-ev-judge-why="">{verdict.rationale}</p>
      )}
      {!!verdict?.used?.length && judged && (
        <p className="mono mt-0.5 text-2xs text-faint" data-ev-used="">{JUDGE_TEXT.used(verdict.used)}</p>
      )}
      {!verdict && judgeDoc && eligible && !canAsk && (
        // 开了裁判的文档里没有判定的结论句：预筛放掉的照实说；探索运行里暂时判不了的由上面 blocked 说
        <p className="text-dim" data-ev-judge-screened="">
          {doc.judge?.screened?.includes(unit.id) || explore !== false ? JUDGE_TEXT.screened : JUDGE_TEXT.formalOnly}
        </p>
      )}
      {rewrite?.kind === 'changed' && (
        <div className="mt-1 text-2xs" data-ev-rewritten="">
          <p style={{ color: 'var(--st-waiting)' }}>{JUDGE_TEXT.rewritten}</p>
          {rewrite.sentences.length > 0 && (
            <p className="mt-0.5 text-dim">{JUDGE_TEXT.rewriteFrom}：{rewrite.sentences.map((t) => `「${t}」`).join('')}</p>
          )}
        </div>
      )}
      {rewrite?.kind === 'rejected' && (
        <p className="mt-1 text-2xs" style={{ color: 'var(--st-waiting)' }} data-ev-rewrite-rejected="">
          {JUDGE_TEXT.rewriteRejected(rewrite.reason)}
        </p>
      )}
      {verdict && !late && judged && (
        // 封存后追加的那句说明放在「封存」一节（它说的是封存）；节点里当场判的在这里说一句随报告封存
        <p className="mt-1 text-2xs text-faint" data-ev-judge-sealed="">{JUDGE_TEXT.sealedHint}</p>
      )}
      {skipped && !judged && (
        <p className="mt-1 text-2xs text-dim" data-ev-judge-skipped={skipped}>
          {res?.message || (skipped === 'not_a_claim' ? JUDGE_TEXT.notClaim : JUDGE_TEXT.notFound)}
        </p>
      )}
      {blocked && !canAsk && (
        <p className="mt-1 text-2xs text-dim" data-ev-judge-blocked={known?.reason ?? ''}>{blocked}</p>
      )}
      {ask?.status === 'error' && (
        <p className="mt-1 text-2xs" style={{ color: 'var(--st-failed)' }} role="alert" data-ev-judge-error="">
          {askError(ask.error)}
        </p>
      )}
      {canAsk && (
        <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1">
          {!verdict || verdict.reason === 'on_demand' ? <span className="text-dim">{JUDGE_TEXT.notAsked}</span> : null}
          <button type="button" className="btn btn-xs" data-ev-judge-ask="" disabled={asking} aria-busy={asking || undefined}
                  onClick={() => { if (runId) void judge(runId, report, [unit.id]) }}>
            {asking ? <Spinner size={10} /> : <Scale size={11} aria-hidden />}
            {asking ? JUDGE_TEXT.asking : verdict && verdict.reason !== 'on_demand' ? JUDGE_TEXT.askAgain : JUDGE_TEXT.ask}
          </button>
          {outdated && known?.message && (
            <span className="basis-full text-2xs text-dim" data-ev-judge-outdated-why="">{known.message}</span>
          )}
          <span className="basis-full text-2xs text-faint">{JUDGE_TEXT.askHint}</span>
        </div>
      )}
    </Part>
  )
}

/** 两条判定是不是同一条（片段接口答的、页面上叠着的）：按取值、理由、裁判模型、规则版本比，不按对象引用 */
function sameVerdict(a: EvidenceVerdict | undefined, b: EvidenceVerdict | undefined): boolean {
  if (!a || !b) return false
  const sig = (v: EvidenceVerdict) => [v.status, v.rationale ?? '', v.judge ?? '', v.rule_version ?? ''].join('\u0001')
  return sig(a) === sig(b)
}

/** 接口说这句暂时判不了、值得告诉人为什么的几种（正式运行、判过了、不是结论句不用说） */
const BLOCKED = new Set(['unsealed', 'seal_broken', 'legacy'])

/**
 * 按需裁判没成：409 是这次运行不让判（正式运行、还没跑完封存、封存被改过），照后端的原话；老后端没有这个
 * 接口另有说法；其余按通用的错误说法
 */
function askError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 409) return error.message || JUDGE_TEXT.formalOnly
    if ((error.status === 404 || error.status === 405) && !error.code) return JUDGE_TEXT.oldBackend
  }
  return JUDGE_TEXT.failed(humanizeError(error).title)
}

/** 输入点一下跳到哪：查询步骤（alias 是查询编号），或者转抄的那一步（text 是按钮上的字，alias 是那张卡的名字） */
type InputLink = { index: number; alias: string; go: () => void; text?: string } | undefined

/**
 * 面板里提到的节点。画布右栏里是按钮：点一下在画布上选中并对准它（画布上已经没有这个节点
 * 时不给按钮，点了会落空）；别处照旧写节点 id
 */
function NodeChip({ id, prefix }: { id?: string | null; prefix?: string }) {
  const host = useEvidenceHost()
  if (!id) return null
  const label = host.nodeLabel?.(id)
  if (host.onNode && label !== undefined) {
    return (
      <button type="button" className="chip max-w-full truncate transition-colors hover:bg-hover" data-ev-node={id}
              title={`在画布上选中并对准「${label || id}」`} onClick={() => host.onNode!(id)}>
        {prefix}{label || id}
      </button>
    )
  }
  return <span className="mono" data-ev-node={id}>{prefix}{id}</span>
}

/**
 * 表或字段：是什么、字段类型、出现在哪几次查询里、从哪来的（表结构快照、查询 SQL、查询结果列）、
 * 表结构快照什么时候同步的。证据接口没取到时照文档目录里记着的来历写，缺的照实说
 */
function EntityPart({ step, entry, cite, seal, pending, failed, catalog }: {
  step?: EvidenceStep
  entry?: Record<string, any>
  cite?: EvidenceSegment['cite']
  seal?: EvidenceSeal
  pending: boolean
  failed: string
  catalog?: EvidenceDocData['catalog']
}) {
  const kind = step?.kind ?? entry?.kind ?? cite?.kind
  const table = step?.table ?? entry?.table ?? entry?.locator?.table ?? cite?.locator?.table
  const column = step?.column ?? entry?.locator?.column ?? cite?.locator?.column
  const name = kind === 'table' ? (step?.name ?? entry?.name ?? table ?? cite?.ref)
    : table && column ? `${table}.${column}` : (step?.name ?? entry?.name ?? column ?? cite?.ref)
  const queries: string[] = (Array.isArray(step?.queries) ? step.queries : Array.isArray(entry?.queries) ? entry.queries : [])
    .map(String).filter(Boolean)
  const sources = entitySources(step?.sources ?? entry?.sources)
  const truncated = sources.some((s) => s.truncated) || step?.snapshot_truncated === true || (step as any)?.truncated === true
  const owners: string[] = Array.isArray(step?.tables) ? step.tables : Array.isArray(entry?.tables) ? entry.tables : []
  const nodes = [...new Set(queries.map((q) => catalog?.[q]?.node_id).filter((n): n is string => typeof n === 'string'))]
  return (
    <Part title={EVIDENCE_TEXT.entity} data-ev-entity={kind ?? ''}>
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <span className="chip shrink-0">{kind === 'table' ? EVIDENCE_TEXT.table : EVIDENCE_TEXT.column}</span>
        <span className="mono min-w-0 font-medium [overflow-wrap:anywhere]">{name || NONE}</span>
        {step?.type && (
          <span className="mono text-2xs text-dim" data-ev-entity-type="">{EVIDENCE_TEXT.columnType} {step.type}</span>
        )}
        {kind === 'table' && typeof (step as any)?.columns === 'number' && (
          <span className="text-2xs text-dim">{EVIDENCE_TEXT.tableColumns((step as any).columns, !!step?.is_view)}</span>
        )}
      </div>
      {kind === 'table' && !!step?.fields?.length && (
        <FieldList fields={step.fields} more={step.fields_more} masked={step.fields_masked === true} />
      )}
      {step?.comment && <p className="mt-0.5 text-2xs text-dim">{step.comment}</p>}
      {owners.length > 1 && <p className="mt-0.5 text-2xs text-dim">{EVIDENCE_TEXT.ownerTables(owners)}</p>}
      {step?.types && !step.type && Object.keys(step.types).length > 0 && (
        <p className="mono mt-0.5 text-2xs text-dim" data-ev-entity-type="">{EVIDENCE_TEXT.columnTypes(step.types)}</p>
      )}
      <p className="mt-1" data-ev-entity-queries="">
        {queries.length ? EVIDENCE_TEXT.entityQueries(queries) : EVIDENCE_TEXT.entityNoQuery}
      </p>
      {nodes.length > 0 && (
        <div className="mt-0.5 flex flex-wrap items-center gap-1 text-2xs text-faint">
          {nodes.map((n) => <NodeChip key={n} id={n} prefix="节点 " />)}
        </div>
      )}
      {sources.length > 0 && (
        <ul className="mt-1 space-y-0.5 text-2xs text-dim">
          {sources.map((src) => <li key={src.text} data-ev-entity-source={src.kind}>· {src.text}</li>)}
        </ul>
      )}
      {step?.synced_at && (
        <p className="mt-1 text-2xs text-faint" data-ev-entity-synced="">{EVIDENCE_TEXT.syncedAt(formatDateTime(step.synced_at))}</p>
      )}
      {truncated && (
        <p className="mt-0.5 text-2xs" style={{ color: 'var(--st-waiting)' }} data-ev-entity-partial="">{EVIDENCE_TEXT.snapshotPartial}</p>
      )}
      <Integrity step={step} seal={seal} />
      {!step && (
        <p className="mt-1.5 text-2xs text-faint" data-ev-chain={pending ? 'loading' : 'missing'}>
          {pending ? EVIDENCE_TEXT.entityPending : failed && failed !== EVIDENCE_TEXT.chainMissing ? failed : EVIDENCE_TEXT.entityMissing}
        </p>
      )}
    </Part>
  )
}

/**
 * 表的字段清单：证据接口给的 fields（封存范围内的表结构快照，最多 60 个，遮罩的列不列入），多出来的写「另有 N 个」，
 * 快照只存了部分表时注明（partial，实体步骤那一节另有这句时不重复）。长名字、长类型都在栏内折行，不截断
 */
function FieldList({ fields, more, partial, masked, label }: {
  fields: NonNullable<EvidenceStep['fields']>; more?: number; partial?: boolean; masked?: boolean; label?: (n: number) => string
}) {
  const list = fields.filter((f) => f && typeof f.name === 'string' && f.name)
  const extra = typeof more === 'number' && Number.isFinite(more) && more > 0 ? more : 0
  const total = list.length + extra
  return (
    <div className="mt-1.5" data-ev-fields="">
      <div className="mb-0.5 text-2xs font-medium text-faint">{label ? label(total) : EVIDENCE_TEXT.fields(total)}</div>
      <ul className="grid max-h-44 grid-cols-[repeat(auto-fill,minmax(8.5rem,1fr))] gap-x-3 gap-y-px overflow-y-auto rounded bg-bg px-1.5 py-1 text-2xs">
        {list.map((f) => (
          <li key={f.name} className="min-w-0 [overflow-wrap:anywhere]" data-ev-field={f.name}>
            <span className="mono">{f.name}</span>
            {f.type && <>{' '}<span className="mono ml-1 text-faint" data-ev-field-type="">{f.type}</span></>}
          </li>
        ))}
      </ul>
      {extra > 0 && <p className="mt-0.5 text-2xs text-dim" data-ev-fields-more="">{EVIDENCE_TEXT.fieldsMore(extra)}</p>}
      {masked && <p className="mt-0.5 text-2xs text-dim" data-ev-fields-masked="">{EVIDENCE_TEXT.fieldsMasked}</p>}
      {partial && (
        <p className="mt-0.5 text-2xs" style={{ color: 'var(--st-waiting)' }} data-ev-fields-partial="">{EVIDENCE_TEXT.snapshotPartial}</p>
      )}
    </div>
  )
}

/**
 * 讲方法、讲结构的句子挂的依据：表的字段清单、字段的类型（取句中那个名字片段的证据链：封存范围内的表结构快照）、
 * 对应的 SQL（查询快照工件，取回时后端复验哈希）。直接摆出来，不用再点开句中的名字
 */
function BasisPart({ basis, doc, runId }: { basis: Basis; doc: EvidenceDocData; runId?: string }) {
  const report = doc.node_id || undefined
  const segments = useEvidence((s) => s.segments)
  const sqls = useEvidence((s) => s.sqls)
  const loadSegment = useEvidence((s) => s.loadSegment)
  const loadSql = useEvidence((s) => s.loadSql)
  const entities = [...basis.tables, ...basis.columns]
  const artifacts = basis.queries.map((q) => {
    const a = doc.catalog?.[q]?.artifact
    return typeof a === 'string' && a ? a : ''
  })
  const segKey = entities.map((e) => e.seg ?? '').join(',')
  const artKey = artifacts.join(',')
  useEffect(() => {
    if (!runId) return
    for (const e of entities) if (e.seg) void loadSegment(runId, e.seg, report)
    for (const a of artifacts) if (a) void loadSql(a)
    // 按内容比：同一句不重复取
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, report, segKey, artKey, loadSegment, loadSql])
  if (!runId) return <p className="mt-1.5 text-2xs text-faint" data-ev-basis="none">{JUDGE_TEXT.basisNoRun}</p>
  const stepOf = (seg?: string) => {
    const slot = seg ? segments[segmentKey(runId, seg, report)] : undefined
    const step = slot?.status === 'ok' ? (slot.data?.chain ?? []).find((c) => c?.step === 'entity') : undefined
    return { step, pending: !!seg && (!slot || slot.status === 'loading') }
  }
  return (
    <div className="mt-2 space-y-2 border-t pt-2" data-ev-basis="">
      {basis.tables.map((t) => {
        const { step, pending } = stepOf(t.seg)
        return (
          <div key={t.alias} data-ev-basis-table={t.alias}>
            {step?.fields?.length ? (
              <FieldList fields={step.fields} more={step.fields_more} partial={step.snapshot_truncated === true}
                         masked={step.fields_masked === true} label={(n) => JUDGE_TEXT.basisFieldsCount(t.name, n)} />
            ) : (
              <p className="text-2xs text-faint">
                {JUDGE_TEXT.basisFields(t.name)}：{pending ? JUDGE_TEXT.basisPending : JUDGE_TEXT.fieldsMissing}
              </p>
            )}
          </div>
        )
      })}
      {basis.columns.length > 0 && (
        <div data-ev-basis-columns="">
          <div className="mb-0.5 text-2xs font-medium text-faint">{JUDGE_TEXT.basisColumns}</div>
          <ul className="space-y-px text-2xs">
            {basis.columns.map((c) => {
              const { step, pending } = stepOf(c.seg)
              const type = step?.type ?? (step?.types ? Object.values(step.types)[0] : undefined)
              return (
                <li key={c.alias} className="min-w-0 [overflow-wrap:anywhere]" data-ev-basis-column={c.alias}>
                  <span className="mono">{c.name}</span>{' '}
                  <span className="mono ml-1 text-faint">{type || (pending ? '…' : NONE)}</span>
                </li>
              )
            })}
          </ul>
        </div>
      )}
      {basis.queries.map((q, i) => {
        const slot = artifacts[i] ? sqls[artifacts[i]] : undefined
        const sql = slot?.status === 'ok' ? slot.data?.sql ?? null : null
        return (
          <div key={q} data-ev-basis-sql={q}>
            <div className="mb-0.5 flex items-center gap-2">
              <span className="min-w-0 flex-1 text-2xs font-medium text-faint">{JUDGE_TEXT.basisSql(q)}</span>
              {sql && <CopyChip label={EVIDENCE_TEXT.copySql} text={() => sql} />}
            </div>
            {sql ? (
              <pre className="mono max-h-28 overflow-auto whitespace-pre-wrap rounded bg-bg px-1.5 py-1 text-2xs leading-relaxed text-dim [overflow-wrap:anywhere]"
                   data-ev-sql="">
                {sql}
              </pre>
            ) : (
              <p className="text-2xs text-faint">{artifacts[i] && (!slot || slot.status === 'loading') ? JUDGE_TEXT.basisPending : JUDGE_TEXT.sqlMissing}</p>
            )}
          </div>
        )
      })}
      {basis.more > 0 && <p className="text-2xs text-faint" data-ev-basis-more="">{JUDGE_TEXT.basisMoreQueries(basis.more)}</p>}
    </div>
  )
}

/**
 * 逐字引文：原文所在的文档和片段，引文在原文里的位置高亮（前后带一截上下文）。检索快照哈希对不上时
 * 不摆原文——那一段不能当证据。原话对不上的引文也画这一节，只写目录里记着的是哪次检索
 */
function QuotePart({ step, cite, quote, resolved, seal, pending, failed, node, label }: {
  step?: EvidenceStep
  cite?: EvidenceSegment['cite']
  quote: string
  resolved: boolean
  seal?: EvidenceSeal
  pending: boolean
  failed: string
  node?: string
  /** 目录里这次检索的说明：「知识库「运营手册」 · 2 条」 */
  label?: string
}) {
  const source = (step?.source && typeof step.source === 'object' ? step.source as EvidenceQuoteSource : undefined)
    ?? cite?.source
  const where = quoteWhere(source)
  const bad = integrityFailures(step, seal)
  // 原文：接口给在 content 里（text 是引文本身）；content 为 null 是快照不在封存范围里、取不回来——不拿引文冒充原文
  const original = step && 'content' in step ? step.content ?? undefined : step?.text
  const loc = cite?.locator
  const mismatch = step?.match_ok === false
  const win = original && !bad.includes('hash') && !mismatch
    ? quoteWindow(original, step?.match ?? (loc ? { start: loc.start, end: loc.end } : null), step?.quote ?? quote)
    : null
  const collection = step?.collection
  return (
    <Part title={EVIDENCE_TEXT.quote} data-ev-quote-step="">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5" data-ev-quote-source="">
        <span className="mono font-medium">{step?.alias ?? cite?.alias ?? NONE}</span>
        {where ? <span>{where}</span> : label && <span className="text-dim">{label}</span>}
        {collection && where && <span className="text-2xs text-faint">{EVIDENCE_TEXT.quoteCollection(collection)}</span>}
        {source?.document && (
          <span className="mono text-2xs text-faint" title={`文档 ${source.document}${source.chunk ? ` · 片段 ${source.chunk}` : ''}`}>
            {shortId(source.document, 12)}
          </span>
        )}
        <NodeChip id={node ?? step?.node_id} prefix="节点 " />
      </div>
      {(bad.length > 0 || mismatch) && (
        <IntegrityList code={[...bad.map((k) => (k === 'hash' ? 'quote-hash' : k)), ...(mismatch ? ['quote-match'] : [])].join(' ')}
                       items={[...bad.map((k) => (k === 'hash' ? EVIDENCE_TEXT.quoteHash : EVIDENCE_TEXT.integrity[k])),
                               ...(mismatch ? [EVIDENCE_TEXT.quoteBad] : [])]} />
      )}
      {win && (
        <>
          <blockquote className="mt-1 whitespace-pre-wrap rounded border-l-2 bg-bg px-2 py-1 text-2xs leading-relaxed text-dim [overflow-wrap:anywhere]"
                      data-ev-quote="">
            {win.cutBefore && '…'}{win.before}
            <mark className="rounded-sm px-0.5" data-ev-quote-hit=""
                  style={{ background: resolved ? 'var(--st-done-soft)' : 'var(--st-waiting-soft)', color: 'var(--text)' }}>
              {win.hit}
            </mark>
            {win.after}{win.cutAfter && '…'}
          </blockquote>
          <p className="mt-0.5 text-2xs text-faint">{EVIDENCE_TEXT.quoteHit}</p>
        </>
      )}
      {resolved && !win && !bad.length && !mismatch && (
        // 接口说了为什么没有原文（不在封存范围里、取不回来）就照原话
        <p className="mt-1.5 text-2xs text-faint" data-ev-chain={pending ? 'loading' : 'missing'}>
          {pending ? EVIDENCE_TEXT.quotePending : step?.note
            || (failed && failed !== EVIDENCE_TEXT.chainMissing ? failed : EVIDENCE_TEXT.quoteMissing)}
        </p>
      )}
      {!resolved && <p className="mt-1 text-2xs" style={{ color: 'var(--st-waiting)' }}>{EVIDENCE_TEXT.quoteMiss}</p>}
    </Part>
  )
}

/** 口径卡钉住的上游工作流 id：方案写在 caliber_from，接口给在 source 上 */
function pinnedWorkflow(step?: EvidenceStep): string | undefined {
  const from = step?.caliber_from ?? (step?.source && typeof step.source === 'object' ? step.source : undefined)
  return from?.workflow_id || undefined
}

/** 所在的句子：点开的那一段描底，句子里的数字加粗 */
function Sentence({ unit, active }: { unit: EvidenceUnit; active: string }) {
  return (
    <p className="leading-relaxed">
      {(unit.segments ?? []).filter((s) => s.kind !== 'structural').map((s) => {
        const state = segmentState(s)
        const on = s.id === active
        const number = s.kind === 'number' || s.kind === 'value'
        // 反引号里的表名字段名照行内代码写，不露反引号
        const code = s.kind === 'entity' && !!s.code
        return (
          <span key={s.id}
                className={clsx(number && 'font-semibold tnum', code && 'mono', on && 'rounded-sm px-0.5')}
                style={on && state ? { background: EVIDENCE_STATE[state].soft, color: 'var(--text)' } : undefined}
                data-ev-here={on ? '' : undefined}>
            {code ? segName(s) : s.text}
          </span>
        )
      })}
    </p>
  )
}

/** 指标步骤：名称、值、口径与版本（来源、升版处置）、原式、代入式（输入标签）、复算；可以展开数字推演 */
function MetricPart({ step, entry, inputs, runId, runClass, seal, pending, failed, resolved, linkOf, workflowName, whatif, children }: {
  step?: EvidenceStep
  entry?: Record<string, any>
  inputs: EvidenceInput[]
  runId?: string
  /** 运行类别：双盲复算记的缺口，正式运行叫出具缺口 */
  runClass?: string
  /** 封存状态：逐项复核里「在不在封存范围内」只在封存完好时单独报 */
  seal?: EvidenceSeal
  pending: boolean
  /** 证据链为什么没有：没有运行 id、接口取不到、接口答的是另一份报告 */
  failed: string
  /** 片段的引用解析成功了（有出处）；解析不了的也画这一节，好说清缺的是哪个指标 */
  resolved: boolean
  /** 输入对应的查询步骤：有的话输入标签是按钮，点一下跳过去 */
  linkOf: (input: EvidenceInput) => InputLink
  /** 钉住的上游工作流在目录里的名字（接口没给名字时用） */
  workflowName?: string
  /** 「推演」开关：证据链回来了、正文是封存的那一份才有 */
  whatif?: { open: boolean; onToggle: () => void; controls: string }
  /** 展开的推演区 */
  children?: ReactNode
}) {
  const host = useEvidenceHost()
  const name = step?.name ?? entry?.name ?? step?.metric ?? entry?.locator?.metric ?? NONE
  const value = step?.value !== undefined ? step.value : entry?.value
  const rendered = step?.rendered ?? entry?.rendered
  const caliber = step?.caliber ?? entry?.caliber
  const version = step?.version ?? entry?.version
  const artifact = step?.artifact ?? entry?.artifact
  // 钉在别的工作流上的口径卡：写明来自哪一版，上游有新版本时写处置
  const pinned = step?.caliber_from ?? (step?.source && typeof step.source === 'object' ? step.source : undefined)
    ?? entry?.caliber_from ?? entry?.source
  const from = caliberSourceText(caliber, version, typeof pinned === 'object' ? pinned : undefined, workflowName)
  const upgrade = caliberUpgradeText(step?.caliber_upgrade ?? entry?.caliber_upgrade)
  // 口径卡里 rendered 为「—」有两种：value 为 null 是缺输入，不为 null 是有值但按格式显示不出来
  const dash = rendered === NONE || entry?.rendered === NONE
  const missing = entry?.status === 'missing_input' || (value == null && (dash || !resolved))
  const unshowable = !missing && dash && value != null
  // 拿截断的查询结果整组算出来的：值是真的算出来的，但只算到了取回的那部分，出具已按缺口降档
  const incomplete = step?.incomplete === true || entry?.incomplete === true
  const incompleteReason = step?.incomplete_reason || entry?.incomplete_reason || EVIDENCE_TEXT.incompleteFallback
  // 所依据的查询对照数据目录查出了错误级的问题：值照算，结果不可靠，出具同样按缺口降档。问题和改法在查询步骤里
  const sqlFailed = step?.sql_check_failed === true || entry?.sql_check_failed === true
  const sqlReason = step?.sql_check_reason || entry?.sql_check_reason || EVIDENCE_TEXT.sqlCheckFallback
  // 期 4：双盲复算复核过这个指标才有
  const dualCheck = dualCheckOf(step)
  // 输入追没追到原始数据：和「有出处」（确定性复算一致）分开说。卡上没有输入、老服务端没给时不说
  const untraced = Array.isArray(step?.untraced) ? step.untraced.filter((u) => u && typeof u === 'object') : null
  const stopAt = (u: EvidenceUntraced) => (u.node_id
    ? EVIDENCE_TEXT.originStop(u.path,
      EVIDENCE_TEXT.originNode(host.nodeLabel?.(u.node_id) || u.label || u.node_id, u.type ? nodeTypeLabel(u.type) : ''))
    : EVIDENCE_TEXT.originUnknown(u.path))

  return (
    <Part title={EVIDENCE_TEXT.metric} data-ev-metric="">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span className="font-medium">{name}</span>
        {resolved && rendered && <span className="mono tnum text-sm" data-ev-value="">{rendered}</span>}
        {incomplete && (
          <span className="chip" data-ev-incomplete="" title={incompleteReason}
                style={{ color: 'var(--st-waiting)', borderColor: 'var(--st-waiting)' }}>
            {EVIDENCE_TEXT.incomplete}
          </span>
        )}
        {sqlFailed && (
          <span className="chip" data-ev-sql-check-failed="" title={sqlReason}
                style={{ color: 'var(--st-waiting)', borderColor: 'var(--st-waiting)' }}>
            {EVIDENCE_TEXT.sqlCheckFailed}
          </span>
        )}
        {!!untraced?.length && (
          <span className="chip" data-ev-untraced-chip="" title={EVIDENCE_TEXT.originChipHint}
                style={{ color: 'var(--st-waiting)', borderColor: 'var(--st-waiting)' }}>
            {EVIDENCE_TEXT.originChip}
          </span>
        )}
        {whatif && <WhatIfToggle open={whatif.open} onToggle={whatif.onToggle} controls={whatif.controls} />}
      </div>
      {(caliber || version || artifact || from) && (
        <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-2xs text-dim">
          {from
            ? <span data-ev-caliber-from="">{from}</span>
            : (caliber || version) && <span>口径卡「{caliber || NONE}」<span className="mono">{version}</span></span>}
          {artifact && <span className="mono text-faint" title={`口径卡工件 ${artifact}`}>工件 {shortId(artifact)}</span>}
          {host.onNode && <NodeChip id={step?.node_id ?? entry?.node_id} />}
        </div>
      )}
      {upgrade && (
        <p className="mt-0.5 text-2xs" style={{ color: 'var(--st-waiting)' }} data-ev-caliber-upgrade="">{upgrade}</p>
      )}
      {dualCheck && <DualCheckLine check={dualCheck} formal={runClass === 'formal'} />}
      <Integrity step={step} seal={seal} />
      {missing && <p className="mt-1" style={{ color: 'var(--st-waiting)' }} data-ev-missing="">{EVIDENCE_TEXT.missingValue}</p>}
      {incomplete && (
        <p className="mt-1" style={{ color: 'var(--st-waiting)' }} data-ev-incomplete-reason="">{incompleteReason}</p>
      )}
      {sqlFailed && (
        <p className="mt-1" style={{ color: 'var(--st-waiting)' }} data-ev-sql-check-reason="">{sqlReason}</p>
      )}
      {unshowable && (
        <p className="mt-1" style={{ color: 'var(--st-waiting)' }} data-ev-unshowable="">
          {EVIDENCE_TEXT.unshowable(valueText(value))}
        </p>
      )}

      {step ? (
        <dl className="mt-2 space-y-1.5">
          <div>
            <dt className="text-2xs text-faint">{EVIDENCE_TEXT.expression}</dt>
            <dd><code className="mono block whitespace-pre-wrap break-all rounded bg-bg px-1.5 py-1 text-2xs" data-ev-expression="">
              {step.expression || NONE}
            </code></dd>
          </div>
          <div>
            <dt className="text-2xs text-faint">{EVIDENCE_TEXT.substituted}</dt>
            <dd>
              <code className="mono block whitespace-pre-wrap break-all rounded bg-bg px-1.5 py-1 text-2xs" data-ev-substituted="">
                {step.substituted || NONE}
              </code>
              {!!inputs.length && (
                <ul className="mt-1 flex flex-wrap gap-1" aria-label={EVIDENCE_TEXT.inputs}>
                  {inputs.map((inp, i) => <InputChip key={`${inp.path}-${i}`} input={inp} link={linkOf(inp)} />)}
                </ul>
              )}
            </dd>
          </div>
          <div>
            <dt className="text-2xs text-faint">{EVIDENCE_TEXT.recompute}</dt>
            <dd data-ev-recompute={step.recompute_ok == null ? 'none' : step.recompute_ok ? 'ok' : 'bad'}
                style={{ color: step.recompute_ok == null ? 'var(--st-cancelled)'
                  : step.recompute_ok ? 'var(--st-done)' : 'var(--st-failed)' }}>
              {step.recompute_ok == null ? EVIDENCE_TEXT.recomputeNone
                : step.recompute_ok ? EVIDENCE_TEXT.recomputeOk : EVIDENCE_TEXT.recomputeBad}
            </dd>
          </div>
          {untraced && (
            <div>
              <dt className="text-2xs text-faint">{EVIDENCE_TEXT.origin}</dt>
              <dd data-ev-origin={untraced.length ? 'untraced' : 'traced'}
                  style={{ color: untraced.length ? 'var(--st-waiting)' : 'var(--st-done)' }}>
                {untraced.length ? (
                  <ul className="space-y-0.5">
                    {untraced.map((u, i) => (
                      <li key={`${u.path}-${i}`} data-ev-origin-stop={u.node_id ?? ''}>{stopAt(u)}</li>
                    ))}
                  </ul>
                ) : EVIDENCE_TEXT.originTraced}
              </dd>
            </div>
          )}
        </dl>
      ) : resolved && (
        <p className="mt-1.5 text-2xs text-faint" data-ev-chain={pending ? 'loading' : 'missing'}>
          {pending ? EVIDENCE_TEXT.chainPending : failed || (runId ? EVIDENCE_TEXT.chainMissing : EVIDENCE_TEXT.noRun)}
        </p>
      )}
      {children}
    </Part>
  )
}

/**
 * 这个指标的双盲复算结果（期 4），一行：与盲算结果一致（带盲算值）、不一致（带盲算值和审核人选了什么）、未能完成（带原因）、
 * 已跳过（带原因）。状态词和同一节里按代入式重算的「复算」分开说：那是同一组输入再算一遍，这是另起一路独立取数，
 * 标签上悬停、读屏都说得出是什么。
 *
 * 选了盲算、两边都不对、没选、未能完成的，写明后果：报告里的数没换（仍取自原流程），节点把它记成了缺口——只看上面
 * 那个数，会以为已经换成了盲算值。运行已经结束，只说节点做了什么，降没降档看报告的出具横幅。
 *
 * 结果都写成字，颜色只表达要不要多看一眼：一致用确定的绿（盲值是代码按原式代回算的、和原值比对的，不是模型的判断）；
 * 不一致而审核人采用了原流程用提醒色，采用盲算、两边都不对、没有选的用失败色（记了缺口）；未能完成用提醒色（同样记了
 * 缺口）；跳过的中性色。认不出的跳过原因不写原因代码
 */
function DualCheckLine({ check, formal }: { check: EvidenceDualCheck; formal: boolean }) {
  const T = EVIDENCE_TEXT.dualCheck
  const host = useEvidenceHost()
  const { status } = check
  const blind = (status === 'agreed' || status === 'disagreed') && check.blind_rendered ? T.blind(check.blind_rendered) : ''
  const picked = check.choice ? DUAL_CHECK_TEXT.approval.choices[check.choice] : undefined
  const detail = status === 'disagreed' ? (picked ? T.choice(picked) : T.noChoice)
    : status === 'skipped' ? (check.reason ? T.skip[check.reason] ?? '' : '')
      : status === 'unchecked' ? check.reason ?? '' : ''
  const kept = status === 'disagreed' && check.choice !== 'a'
  const after = kept ? T.kept(formal) : status === 'unchecked' ? T.gap(formal) : ''
  const color = status === 'agreed' ? 'var(--st-done)'
    : status === 'disagreed' ? (check.choice === 'a' ? 'var(--st-waiting)' : 'var(--st-failed)')
      : status === 'unchecked' ? 'var(--st-waiting)' : 'var(--text-dim)'
  return (
    <p className="mt-1 text-2xs leading-relaxed [overflow-wrap:anywhere]" data-ev-dual-check={status}
       data-ev-dual-check-choice={status === 'disagreed' ? check.choice ?? 'none' : undefined}>
      <span className="text-faint" title={DUAL_CHECK_TEXT.description} data-ev-dual-check-label="">
        {T.label}<span className="sr-only">（{DUAL_CHECK_TEXT.description}）</span>：
      </span>
      <span className="font-medium" style={{ color }}>{T.status[status] ?? T.unknown(String(status))}</span>
      {blind && <span className="tnum text-dim">（{blind}）</span>}
      {detail && <span className="text-dim">，{detail}</span>}
      {after && <span className="text-dim" data-ev-dual-check-after="">。{after}</span>}
      {host.onNode && check.node_id && <> <NodeChip id={check.node_id} /></>}
    </p>
  )
}

/**
 * 证据接口逐项复核的结果：eid 能重算、工件哈希复验、重新渲染一致、在封存范围内。
 * 全过的时候不说（正常态安静）；哪一项明确没过就用失败色写出来——这时候这件证据
 * 不能信，比「有出处」三个字重要得多。「不在封存范围内」只在封存完好时报：没封存、
 * 封存核对失败时后端把每一步都记成 sealed=false，那由封存那一行说，不能说成「事后补进来的」
 */
function Integrity({ step, seal }: { step?: EvidenceStep; seal?: EvidenceSeal }) {
  const bad = integrityFailures(step, seal)
  if (!bad.length) return null
  return <IntegrityList code={bad.join(' ')} items={bad.map((k) => EVIDENCE_TEXT.integrity[k])} />
}

function IntegrityList({ code, items }: { code: string; items: string[] }) {
  return (
    <ul className="mt-1.5 space-y-0.5 rounded border px-2 py-1" data-ev-integrity={code}
        style={{ color: 'var(--st-failed)', borderColor: 'var(--st-failed)', background: 'var(--st-failed-soft)' }}>
      {items.map((t) => <li key={t}>{t}</li>)}
    </ul>
  )
}

/**
 * 「45,678.5 ← vars.kpi.gmv」：值从哪条路径取的。缺的写「—」、标成提醒色。
 * 能对到链里某一次查询的（agent 字段、cell() 取数）是按钮：点一下跳到那个查询步骤
 */
function InputChip({ input, link }: { input: EvidenceInput; link?: InputLink }) {
  const host = useEvidenceHost()
  const missing = input.status === 'missing' || input.value == null
  // 取自遮罩列（期 4）：服务端已把值换成「已遮罩」，这里照查询表格里遮罩格的样子写，读着不像一个真值。遮没遮核对不了的
  // （快照读不出来）一样不显示值，写「无法核对」，悬停说明原因
  const unverified = unverifiedOf(input)
  const masked = !missing && (maskedOf(input).length > 0 || !!unverified)
  // Agent 交来、被截断切开的数组字段：对它计数、求和只算到了取回的那部分
  const cut = !missing && input.truncated === true
  const node = input.node_id ? host.nodeLabel?.(input.node_id) || input.node_id : ''
  const via = input.via ? VIA_TEXT[input.via] ?? nodeTypeLabel(input.via) : ''
  const through = throughText(input, host.nodeLabel)
  const from = [node && `来自节点「${node}」`, via && `（${via}）`, through && ` · ${through}`,
    input.role && ` · ${ROLE_TEXT[input.role] ?? input.role}`,
    cut && ` · ${EVIDENCE_TEXT.inputTruncated}`,
    masked && ` · ${unverified ? EVIDENCE_TEXT.unverifiedInput(unverified) : EVIDENCE_TEXT.maskedInput}`].filter(Boolean).join('')
  const style = missing || cut ? { color: 'var(--st-waiting)', borderColor: 'var(--st-waiting)' } : { color: 'var(--text)' }
  // 值那里的字照服务端写的（「已遮罩」，遮没遮核对不了的「无法核对」）；认不出的一律写「已遮罩」，不把别的字当值显示
  const shown = !masked ? valueText(input.value)
    : input.value === EVIDENCE_TEXT.unverifiedValue ? EVIDENCE_TEXT.unverifiedValue : EVIDENCE_TEXT.masked
  const body = (
    <span className="truncate">
      {masked ? <span className="font-sans italic text-faint">{shown}</span> : shown} ← {input.path ?? NONE}
      {missing ? '（缺失）' : cut ? '（已截断）' : ''}
    </span>
  )
  return (
    <li className="max-w-full">
      {link ? (
        <button type="button" className="chip mono tnum max-w-full transition-colors hover:bg-hover" style={style}
                data-ev-input={input.path ?? ''} data-ev-input-masked={masked ? '' : undefined}
                title={`${from ? `${from} · ` : ''}${link.text ?? EVIDENCE_TEXT.gotoQuery(link.alias)}`}
                aria-label={`${shown} ← ${input.path ?? NONE}，${link.text ?? EVIDENCE_TEXT.gotoQuery(link.alias)}`}
                onClick={link.go}>
          {body}<CornerDownRight size={9} className="shrink-0 opacity-60" aria-hidden />
        </button>
      ) : (
        <span className="chip mono tnum max-w-full" data-ev-input={input.path ?? ''} data-ev-input-masked={masked ? '' : undefined}
              style={style} title={from || undefined}>
          {body}
        </span>
      )}
    </li>
  )
}

/** 取值来源的方式：能追到快照的两种写中文，其余是产出节点的类型 */
const VIA_TEXT: Record<string, string> = { agent_field: 'Agent 字段', tool_cell: '单元格取数' }

/** 按位置取的格穿过了原样解析查询结果的整形节点：「经「解析查询结果」原样解析」。cell() 取的格、别的输入没有 */
function throughText(input: EvidenceInput, nodeLabel?: (id: string) => string | undefined): string {
  if (!input.through) return ''
  return EVIDENCE_TEXT.through(nodeLabel?.(input.through) || input.through_label || input.through)
}
/** 代码节点输入的角色 */
const ROLE_TEXT: Record<string, string> = { source: '取数', compute: '计算' }

const TONE: Record<SourceTone, { color: string; border: string; background?: string }> = {
  ok: { color: 'var(--st-done)', border: 'var(--border)' },
  warn: { color: 'var(--st-waiting)', border: 'var(--border)' },
  // 计算角色的代码节点：沙箱里的算术绕开了口径卡，比别的提醒都醒目
  alert: { color: 'var(--st-failed)', border: 'var(--st-failed)', background: 'var(--st-failed-soft)' },
  muted: { color: 'var(--text-dim)', border: 'var(--border)' },
}

/**
 * 转抄的口径卡指标（对账卡、召回卡的「口径」那一项）：服务端跨卡接上的一步——那张卡的那个指标、它的值、原式和代入式，
 * 它自己的输入（点一下跳到口径的取数查询，或者再下一层转抄的那一步），追没追到原始数据。跨卡怎么追是服务端定的
 * （api/evidence._upstream_steps），这里只照着画
 */
function UpstreamPart({ id, step, seal, linkOf }: {
  id: string; step: EvidenceStep; seal?: EvidenceSeal; linkOf: (input: EvidenceInput) => InputLink
}) {
  const host = useEvidenceHost()
  const card = host.nodeLabel?.(step.node_id ?? '') || step.label || step.node_id || NONE
  const inputs = (step.inputs ?? []).filter((i) => i && typeof i === 'object')
  const unreadable = step.hash_ok === false || step.name == null
  const untraced = Array.isArray(step.untraced) ? step.untraced.filter((u) => u && typeof u === 'object') : null
  const stopAt = (u: EvidenceUntraced) => (u.node_id
    ? EVIDENCE_TEXT.originStop(u.path,
      EVIDENCE_TEXT.originNode(host.nodeLabel?.(u.node_id) || u.label || u.node_id, u.type ? nodeTypeLabel(u.type) : ''))
    : EVIDENCE_TEXT.originUnknown(u.path))
  return (
    <div id={id} data-ev-upstream={step.node_id ?? ''} data-ev-upstream-metric={step.metric ?? ''}
         className="rounded data-[flash]:outline data-[flash]:outline-solid data-[flash]:outline-2 data-[flash]:outline-offset-2 data-[flash=focus]:outline-[color:var(--accent)]">
      <div className="mb-1 text-2xs font-medium text-faint">{EVIDENCE_TEXT.upstream}</div>
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span tabIndex={-1} data-ev-upstream-head=""
              className="rounded-sm font-medium focus:outline-none focus-visible:outline focus-visible:outline-solid focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[color:var(--accent)]">
          {EVIDENCE_TEXT.upstreamFrom(card, step.name || step.metric || NONE)}
        </span>
        {!unreadable && step.rendered && <span className="mono tnum text-sm" data-ev-upstream-value="">{step.rendered}</span>}
        {step.incomplete && (
          <span className="chip" title={step.incomplete_reason || EVIDENCE_TEXT.incompleteFallback}
                style={{ color: 'var(--st-waiting)', borderColor: 'var(--st-waiting)' }}>{EVIDENCE_TEXT.incomplete}</span>
        )}
        {step.sql_check_failed && (
          <span className="chip" title={step.sql_check_reason || EVIDENCE_TEXT.sqlCheckFallback}
                style={{ color: 'var(--st-waiting)', borderColor: 'var(--st-waiting)' }}>{EVIDENCE_TEXT.sqlCheckFailed}</span>
        )}
        {host.onNode && <NodeChip id={step.node_id} />}
      </div>
      <Integrity step={step} seal={seal} />
      {unreadable ? (
        <p className="mt-1" style={{ color: 'var(--st-failed)' }} data-ev-upstream-unreadable="">
          {EVIDENCE_TEXT.upstreamUnreadable}
        </p>
      ) : (
        <dl className="mt-2 space-y-1.5">
          <div>
            <dt className="text-2xs text-faint">{EVIDENCE_TEXT.expression}</dt>
            <dd><code className="mono block whitespace-pre-wrap break-all rounded bg-bg px-1.5 py-1 text-2xs">
              {step.expression || NONE}
            </code></dd>
          </div>
          <div>
            <dt className="text-2xs text-faint">{EVIDENCE_TEXT.substituted}</dt>
            <dd>
              <code className="mono block whitespace-pre-wrap break-all rounded bg-bg px-1.5 py-1 text-2xs">
                {step.substituted || NONE}
              </code>
              {!!inputs.length && (
                <ul className="mt-1 flex flex-wrap gap-1" aria-label={EVIDENCE_TEXT.inputs} data-ev-upstream-inputs="">
                  {inputs.map((inp, i) => <InputChip key={`${inp.path}-${i}`} input={inp} link={linkOf(inp)} />)}
                </ul>
              )}
            </dd>
          </div>
          {untraced && (
            <div>
              <dt className="text-2xs text-faint">{EVIDENCE_TEXT.origin}</dt>
              <dd data-ev-upstream-origin={untraced.length ? 'untraced' : 'traced'}
                  style={{ color: untraced.length ? 'var(--st-waiting)' : 'var(--st-done)' }}>
                {untraced.length ? (
                  <ul className="space-y-0.5">{untraced.map((u, i) => <li key={`${u.path}-${i}`}>{stopAt(u)}</li>)}</ul>
                ) : EVIDENCE_TEXT.originTraced}
              </dd>
            </div>
          )}
        </dl>
      )}
    </div>
  )
}

/**
 * 输入来源：每个能核对到快照的输入（agent 字段、cell() 取数）、代码节点的输入、缺的输入各一行——
 * 路径、从哪个节点哪一格来、核对结果。能对到查询步骤的给「看查询 Qn」
 */
function SourcesPart({ inputs, linkOf }: { inputs: EvidenceInput[]; linkOf: (input: EvidenceInput) => InputLink }) {
  const host = useEvidenceHost()
  return (
    <Part title={EVIDENCE_TEXT.sources} data-ev-sources="">
      <ul className="space-y-1.5">
        {inputs.map((inp, i) => {
          const said = maskedOf(inp).length || unverifiedOf(inp) ? maskedSource(inp) : inputSource(inp)
          const tone = TONE[said.tone]
          const link = linkOf(inp)
          // 查询编号写报告目录里的全局编号（接口补的 query、对上的查询步骤）；agent 字段自己的 ref 是
          // 那个节点内部的编号，和面板里的「查询 Qn」不是一回事，不拿来写
          const alias = (link && !link.text ? link.alias : '') || inp.query
            || (typeof inp.cell === 'string' ? inp.cell.split('.')[0] : '')
          const where = [
            inp.field && (inp.via === 'agent_field' ? `字段 ${inp.field}` : inp.field),
            throughText(inp, host.nodeLabel),
            [alias, locatorText(inp.locator)].filter(Boolean).join(' · '),
          ].filter(Boolean).join(' · ')
          return (
            <li key={`${inp.path}-${i}`} className="rounded border px-2 py-1"
                style={{ borderColor: tone.border, background: tone.background }}
                data-ev-source={inp.path ?? ''} data-ev-source-status={inp.status ?? ''}
                data-ev-source-role={inp.via === 'code' ? inp.role || 'compute' : undefined}>
              <div className="flex items-baseline gap-2">
                <span className="mono min-w-0 flex-1 truncate" title={inp.path}>{inp.path ?? NONE}</span>
                {link && (
                  <button type="button" className="btn btn-xs btn-ghost -mr-1 shrink-0" onClick={link.go} data-ev-goto="">
                    <CornerDownRight size={10} aria-hidden /> {link.text ?? EVIDENCE_TEXT.gotoQuery(link.alias)}
                  </button>
                )}
              </div>
              {(inp.node_id || where) && (
                <div className="mono text-2xs text-faint [overflow-wrap:anywhere]">
                  {inp.node_id && <NodeChip id={inp.node_id} prefix="节点 " />}
                  {inp.node_id && where && ' · '}{where}
                </div>
              )}
              {said.text && <p className="mt-0.5" style={{ color: tone.color }}>{said.text}</p>}
              {said.reason && <p className="mt-0.5 text-2xs text-dim">{said.reason}</p>}
            </li>
          )
        })}
      </ul>
    </Part>
  )
}

/**
 * 取自遮罩列的输入在「输入来源」里怎么说（期 4）：核对结果照说，只是不写值——核对不一致时 inputSource 会把两边的值都写
 * 出来。另附一句为什么不显示值。遮没遮核对不了的（快照读不出来）直接说原因：卡上记的核对结果对着的那份快照已经读不出来了
 */
function maskedSource(inp: EvidenceInput): { tone: SourceTone; text: string; reason?: string } {
  const unverified = unverifiedOf(inp)
  if (unverified) return { tone: 'warn', text: EVIDENCE_TEXT.unverifiedInput(unverified) }
  if (inp.status === 'mismatch') return { tone: 'warn', text: EVIDENCE_TEXT.maskedMismatch }
  const said = inputSource(inp)
  return { ...said, reason: said.reason ? `${said.reason}；${EVIDENCE_TEXT.maskedInput}` : EVIDENCE_TEXT.maskedInput }
}

/**
 * 查询步骤：SQL（带复制）、结果窗口（被引用的行和格高亮）、窗口说明、打开完整快照、复核。
 *
 * 接口只给被引用的行加前后各 2 行，窗口没盖住整份快照时写明；完整快照走工件接口（取回时
 * 复验哈希），复用工件查看。被遮罩的列在表里写「已遮罩」。查询快照哈希对不上时用失败色写出来：
 * 这时候这些行不能当证据，比「有出处」重要得多
 */
function QueryPart({ id, step, seal, masked, pick }: {
  id: string; step: EvidenceStep; seal?: EvidenceSeal; masked: string[]
  /**
   * 推演时：结果窗口每行前面加勾选框，勾掉的行在推演里视为不存在。usable 说哪几行这个指标用得到（别的行不让勾），
   * summary 是画在表格底下的推演结果
   */
  pick?: {
    excluded: number[]; onToggle: (row: number) => void; usable?: (row: number) => boolean; summary?: ReactNode; busy?: boolean
  }
}) {
  const host = useEvidenceHost()
  const win = useMemo(() => queryWindow(step), [step])
  const [open, setOpen] = useState(false)
  const scroller = useRef<HTMLDivElement>(null)
  const alias = step.alias ?? ''
  const hidden = [...new Set([...masked, ...(Array.isArray(step.masked) ? step.masked.map(String) : [])])]
    .filter((c) => win.columns.includes(c))
  const bad = integrityFailures(step, seal)
  // 完整快照只在这一步自己画得出行、复核也都过了时给：快照不在封存范围里（接口一行不给，工件 id 照样带着）、
  // 哈希对不上时，按钮一点就绕过证据接口把整份快照打开，还挂着「哈希已校验」
  const snapshot = !!step.artifact && step.sealed !== false && step.hash_ok !== false && !bad.length && win.rows.length > 0
  // 知道每一行在快照里是第几行时，最前面加一列行号：窗口不连续（被引用的行隔得远）也看得出跳过了哪几行
  const table = useMemo(() => (win.index
    ? { columns: [ROW_NO, ...win.columns], rows: win.rows.map((r, i) => [win.index![i] + 1, ...r]), truncated: false }
    : { columns: win.columns, rows: win.rows, truncated: false }), [win])
  // 被引用的格可能在右边几列：窄栏里表格横向滚到它，不让人以为没高亮
  useLayoutEffect(() => {
    const box = scroller.current
    const cell = box?.querySelector<HTMLElement>('td[data-highlight="cell"]')
    const inner = cell?.closest<HTMLElement>('.overflow-x-auto')
    if (!cell || !inner) return
    const left = cell.offsetLeft
    if (left < inner.scrollLeft || left + cell.offsetWidth > inner.scrollLeft + inner.clientWidth) {
      inner.scrollLeft = Math.max(0, left - 8)
    }
  }, [win])
  return (
    // 从输入标签跳过来时描一下边（flashOnce 设 data-flash）：静态描边，减少动效时照样看得见
    <div id={id} data-ev-query={step.artifact ?? ''} data-ev-query-alias={alias}
         className="rounded data-[flash]:outline data-[flash]:outline-solid data-[flash]:outline-2 data-[flash]:outline-offset-2 data-[flash=focus]:outline-[color:var(--accent)]">
      <div className="mb-1 flex items-center gap-2">
        {/* 键盘跳过来时焦点落在这里：得看得见落在哪（不能 outline-none） */}
        <span tabIndex={-1} data-ev-query-head=""
              className="min-w-0 flex-1 truncate rounded-sm text-2xs font-medium text-faint focus:outline-none focus-visible:outline focus-visible:outline-solid focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[color:var(--accent)]">
          {EVIDENCE_TEXT.query(alias)}
          {step.tool && <span className="mono ml-1.5 font-normal">{step.tool}</span>}
          {step.merge && <span className="ml-1.5 font-normal">{NODE_TYPE_LABEL.merge}</span>}
          {step.merged_into && (
            <span className="ml-1.5 font-normal" data-ev-merged-into={step.merged_into}>
              · {MERGE_TEXT.mergedInto(step.merged_into)}
            </span>
          )}
        </span>
        {host.onNode && <NodeChip id={step.node_id} />}
        {step.sql && (
          <CopyChip label={step.merge ? MERGE_TEXT.copySql : EVIDENCE_TEXT.copySql} text={() => step.sql ?? ''} />
        )}
      </div>
      {!!step.catalog?.length && (
        // SQL 检查对照的是哪一版数据目录（查询当时冻结进表结构快照的那份）：事后目录改了、升了版本，看得出这次按的是哪一版
        <div className="mb-1 text-2xs text-faint [overflow-wrap:anywhere]" data-ev-catalog=""
             title={RUN_SQL_CHECK_TEXT.catalogHint}>
          {RUN_SQL_CHECK_TEXT.catalogLine(step.catalog.map((c) => ({ name: c.label || c.table, version: c.version })))}
        </div>
      )}
      {step.merge && <MergeInputs merge={step.merge} />}
      {step.sql && step.merge && <div className="mb-0.5 text-2xs text-faint">{MERGE_TEXT.sql}</div>}
      {step.sql && (
        <pre className="mono mb-1.5 max-h-28 overflow-auto whitespace-pre-wrap rounded bg-bg px-1.5 py-1 text-2xs leading-relaxed text-dim [overflow-wrap:anywhere]"
             data-ev-sql="">
          {step.sql}
        </pre>
      )}
      {!!step.checks?.length && (
        // 这次查询对照数据目录查出的问题：说明、涉及的表和列、SQL 片段。没查出问题时接口不给这个键
        <div className="mb-1.5" data-ev-sql-checks="">
          <div className="mb-0.5 text-2xs text-faint">{SQL_CHECK_TEXT.title}</div>
          <SqlCheckList checks={step.checks} />
        </div>
      )}
      {!!step.merge?.warnings.length && (
        <div className="mb-1.5 text-2xs" data-ev-merge-warnings="">
          <div className="mb-0.5 text-faint">{MERGE_TEXT.executionWarnings}</div>
          <ul className="space-y-0.5">
            {step.merge.warnings.map((w, k) => (
              <li key={k} className="flex items-start gap-1 leading-snug" style={{ color: 'var(--st-waiting)' }}>
                <AlertTriangle size={10} className="mt-[2px] shrink-0" aria-hidden />
                <span className="min-w-0 [overflow-wrap:anywhere]">{w.message}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {bad.length > 0 && (
        <IntegrityList code={bad.map((k) => (k === 'hash' ? 'query-hash' : k)).join(' ')}
                       items={bad.map((k) => (k === 'hash' ? EVIDENCE_TEXT.queryHash : EVIDENCE_TEXT.integrity[k]))} />
      )}
      {win.columns.length > 0 && win.rows.length > 0 && (
        <div ref={scroller} className={clsx(bad.length > 0 && 'mt-1.5')}>
          {pick && win.index
            ? <WhatIfRows win={win} hidden={hidden} excluded={pick.excluded} onToggle={pick.onToggle} usable={pick.usable}
                          summary={pick.summary} busy={pick.busy} />
            : (
              <ResultTable table={table} full title={EVIDENCE_TEXT.query(alias)}
                           highlight={win.marks.length || win.cells?.length ? { rows: win.marks, cols: win.cols, cells: win.cells } : undefined}
                           masked={hidden} />
            )}
        </div>
      )}
      {!win.rows.length && (
        // 没有行：快照不在封存范围里、哈希对不上、取不回来。接口说了原因就照原话
        <p className="text-2xs text-dim" data-ev-query-note="">{step.note || EVIDENCE_TEXT.queryMissing}</p>
      )}
      {!!step.merge?.traced.length && <MergeTraces alias={alias} merge={step.merge} />}
      <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-2xs text-faint">
        {win.windowed && win.total != null && (
          <span data-ev-window="">
            {EVIDENCE_TEXT.windowed}（{win.span}，共 {formatNumber(win.total)} 行）
          </span>
        )}
        {step.window_truncated && <span style={{ color: 'var(--st-waiting)' }}>{EVIDENCE_TEXT.windowCut(win.rows.length)}</span>}
        {step.truncated && <span style={{ color: 'var(--st-waiting)' }}>{EVIDENCE_TEXT.truncatedSnapshot}</span>}
        {/* 数据源改名或删掉了：遮罩按查询当时记下的那份处理，行照样给。这句要看得见——
            现在的数据源设置已经管不到这份快照了 */}
        {step.mask_note && (
          <span className="[overflow-wrap:anywhere]" style={{ color: 'var(--st-waiting)' }} data-ev-query-mask-note="">{step.mask_note}</span>
        )}
        <span className="flex-1" />
        {snapshot && (
          <button type="button" className="inline-flex items-center gap-1 rounded px-1 transition-colors hover:bg-hover hover:text-fg"
                  onClick={() => setOpen(true)} data-ev-snapshot="">
            <ShieldCheck size={10} aria-hidden /> {EVIDENCE_TEXT.openSnapshot}
          </button>
        )}
      </div>
      {/* 弹窗挂到 body 上：侧边面板是 fixed 的一层，里面的遮罩盖不住整页 */}
      {/* 完整快照里同样遮掉这几列：工件接口还是原值（面板底部写明了），面板自己的入口不多暴露 */}
      {open && snapshot && step.artifact && createPortal(
        <ArtifactViewer id={step.artifact} title={EVIDENCE_TEXT.snapshotTitle(alias)} masked={hidden} onClose={() => setOpen(false)} />,
        document.body,
      )}
    </div>
  )
}

/** 窗口表格最前面那一列：快照里的行号，从 1 数 */
const ROW_NO = '#'

/**
 * 合并查询的表级来历：合并了哪几个输入（合并 SQL 里的别名 → 报告目录里的编号、节点、行数、数据源）。
 * 输入不在这份报告的证据目录里时照实说：它的那次查询点不开
 */
function MergeInputs({ merge }: { merge: EvidenceMerge }) {
  const host = useEvidenceHost()
  return (
    <div className="mb-1.5 text-2xs" data-ev-merge="">
      <div className="mb-0.5 text-faint">{MERGE_TEXT.inputs(merge.inputs.length)}</div>
      <ul className="space-y-0.5">
        {merge.inputs.map((i) => (
          <li key={i.alias} className="flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-0.5" data-ev-merge-input={i.alias}>
            <span className="mono rounded bg-bg px-1">{i.alias}</span>
            {i.query
              ? <span className="mono text-dim">{EVIDENCE_TEXT.query(i.query)}</span>
              : <span style={{ color: 'var(--st-waiting)' }}>{MERGE_TEXT.notInCatalog}</span>}
            <span className="min-w-0 truncate text-dim" title={i.node_id ?? undefined}>{i.label || i.node_id || NONE}</span>
            <span className="text-faint">
              {[typeof i.rows === 'number' ? MERGE_TEXT.rows(i.rows) : '', i.source ? MERGE_TEXT.source(i.source) : '']
                .filter(Boolean).join(' · ')}
            </span>
            {host.onNode && i.node_id && <NodeChip id={i.node_id} />}
            {!!i.checks?.length && (
              // 输入查询自己的 SQL 检查：被引用的格追不到逐格来历时，下面没有这个输入的查询步骤，问题只能在这里看到
              <div className="mt-0.5 w-full" data-ev-merge-input-checks={i.alias}>
                <div className="mb-0.5 text-faint">{RUN_SQL_CHECK_TEXT.mergeInputChecks(i.alias)}</div>
                <SqlCheckList checks={i.checks} />
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}

/**
 * 被引用的格追到了哪个输入的哪一格（逐格来历）。追不到的照实说只有表级来历：宁可不指，也不指错。
 * 追到的那一格在下面那个输入的查询步骤里高亮
 */
function MergeTraces({ alias, merge }: { alias: string; merge: EvidenceMerge }) {
  return (
    <div className="mt-1.5 text-2xs" data-ev-merge-traced="">
      <div className="mb-0.5 text-faint">{MERGE_TEXT.traced}</div>
      <ul className="space-y-0.5">
        {merge.traced.map((t, k) => (
          <li key={k} className="flex min-w-0 flex-wrap items-baseline gap-x-1.5"
              data-ev-merge-trace={t.input ? 'cell' : 'none'}>
            <span className="mono text-dim">{MERGE_TEXT.cellAt(alias, t.cell[0], t.cell[1])}</span>
            <span className="text-faint" aria-hidden>←</span>
            {t.input && t.row != null && t.column
              ? <span className="mono">{MERGE_TEXT.cellAt(t.query ?? t.input, t.row, t.column)}</span>
              : <span className="[overflow-wrap:anywhere]" style={{ color: 'var(--st-waiting)' }}>{MERGE_TEXT.noLineage}</span>}
          </li>
        ))}
      </ul>
    </div>
  )
}

/** 接口没给查询步骤时：文档目录里记着的那次查询（编号、工具、行数），并照实说行没取到 */
function QueryFallback({ entry, alias, where, pending, failed }: {
  entry?: Record<string, any>; alias?: string; where: string; pending: boolean; failed: string
}) {
  const rows = typeof entry?.rows === 'number' ? `${formatNumber(entry.rows)} 行` : ''
  return (
    <Part title={EVIDENCE_TEXT.query(alias ?? entry?.alias ?? '')} data-ev-query-missing={pending ? 'loading' : 'missing'}>
      <p className="mono text-2xs text-dim [overflow-wrap:anywhere]">
        {[entry?.tool ?? entry?.source, rows, where].filter(Boolean).join(' · ') || NONE}
      </p>
      <p className="mt-1 text-2xs text-faint">
        {pending ? EVIDENCE_TEXT.queryPending : failed === EVIDENCE_TEXT.chainMissing ? EVIDENCE_TEXT.queryMissing : failed || EVIDENCE_TEXT.queryMissing}
      </p>
    </Part>
  )
}

/**
 * 封存状态：StatusBadge 的剪影 + 一句话。说什么由 lib/evidence 的 sealVerdict 定（顺序、
 * 正文不是封存那份时的失败态都在那里）；没有证据的片段封存的只是报告文档本身，
 * 不能说成「这个数已封存」
 */
function SealLine({ status, label }: { status: SealStatus; label: string }) {
  return (
    <p className="flex items-center gap-1.5" data-ev-seal-state={status}>
      <StatusBadge status={status} size={12} decorative animate={false} />
      <span className={status === 'idle' ? 'text-dim' : undefined}>{label}</span>
    </p>
  )
}

const CODE_LABEL: Record<string, string> = {
  uncited_number: '无出处数字',
  unresolved_ref: '引用无法解析',
  unknown_entity: '疑似不存在的名称',
  unverified_entity: '无法核实的名称',
  absence_claim: ABSENCE_CLAIM_TEXT.label,
  masked_cell: MASKED_CELL_TEXT.label,
}

/** 违规清单：报告撰写节点核对出来的全部问题，画得出线的能定位，画不出的说清在哪 */
function ViolationList({ doc, onLocate }: { doc: EvidenceDocData; onLocate: (segId: string) => void }) {
  const list: EvidenceViolation[] = doc.violations ?? []
  if (!list.length) return <p className="text-dim">{EVIDENCE_TEXT.allCited(docNumbers(doc))}</p>
  return (
    <>
      <p className="text-2xs text-faint">{EVIDENCE_TEXT.violationsHint}</p>
      <ul className="space-y-2" data-ev-violations="">
        {list.map((v, i) => {
          // 缺失断言说的是一整句话，正文里普通文字不是可聚焦的片段：不给「定位」，原句写在下面
          const sentence = v.code === 'absence_claim'
          const seg = sentence ? null : locatable(doc, v)
          return (
            <li key={i} className="rounded border px-2 py-1.5" data-ev-violation={v.code}
                data-ev-locatable={seg ? 'yes' : 'no'}>
              <div className="flex items-center gap-2">
                <span className="mono font-semibold">{v.text ?? (v.ref ? `[[${v.ref}]]` : NONE)}</span>
                <span className="chip" style={{ color: 'var(--st-waiting)' }}>{CODE_LABEL[v.code] ?? '文档核对未通过'}</span>
                <span className="flex-1" />
                {seg && (
                  <button type="button" className="btn btn-xs" onClick={() => onLocate(seg.id)}>定位</button>
                )}
              </div>
              {v.message && <p className="mt-1 text-dim">{v.message}</p>}
              {v.code === 'absence_claim' && (
                <p className="mt-0.5 text-2xs text-dim" data-ev-absence-hint="">{ABSENCE_CLAIM_TEXT.hint}</p>
              )}
              {v.context && <p className="mono mt-0.5 text-2xs text-faint [overflow-wrap:anywhere]">「{v.context.trim()}」</p>}
              {!seg && !sentence && (
                <p className="mt-0.5 text-2xs" style={{ color: 'var(--st-waiting)' }}>
                  {v.segment ? EVIDENCE_TEXT.structural : EVIDENCE_TEXT.noSegment}
                </p>
              )}
            </li>
          )
        })}
      </ul>
    </>
  )
}

const docNumbers = (doc: EvidenceDocData): number => doc.stats?.numbers ?? 0
