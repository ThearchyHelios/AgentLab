import {
  useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from 'react'
import { createPortal } from 'react-dom'
import { Link } from 'react-router-dom'
import { ArrowLeft, CornerDownRight, ListChecks, Scale, ShieldCheck, X } from 'lucide-react'
import clsx from 'clsx'
import { ApiError } from '../api/client'
import { Spinner, StatusBadge, isComposing } from '../components/ui'
import {
  EVIDENCE_KIND_STYLE, EVIDENCE_STATE, caliberSourceText, caliberUpgradeText, closestNames, docForeign, entitySources,
  evidenceTrace, evidenceValue as valueText, graphDoc, inputSource, integrityFailures, isJudged, judgeable, limitOf,
  locatable, locatorText, notableInput, queryOf, queryWindow, quoteWhere, quoteWindow, reasonOf, rewriteOf, sealVerdict,
  segName, segmentKind, segmentState, sourceOf, unitText, unitVerdict, verdictState, type SealStatus, type SourceTone,
} from '../lib/evidence'
import { humanizeError } from '../lib/errors'
import { formatDateTime, formatNumber, NONE, shortId } from '../lib/format'
import { EVIDENCE_TEXT, JUDGE_TEXT } from '../lib/terms'
import { useCatalog } from '../store/catalog'
import { askKey, segmentKey, useEvidence, useExplore, useVerdicts } from '../store/evidence'
import type {
  EvidenceBlock, EvidenceDocData, EvidenceInput, EvidenceOnDemand, EvidenceQuoteSource, EvidenceSeal, EvidenceSegment,
  EvidenceStep, EvidenceUnit, EvidenceVerdict, EvidenceViolation,
} from '../types'
import { ArtifactViewer, ResultTable } from './AssistantStream'
import { useEvidenceHost } from './evidenceHost'
import { CopyChip } from './Markdown'

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
 * 四种摆法：side 从右侧弹出（宽屏），drawer 从底部抽出（窄屏），inline 在 360px 的
 * 画布右栏里直接栏内展开，dock 放进页面给的一块常驻位置（记录页的「证据」页签）。
 * 都不是模态的：开着面板照样能在正文里走。
 */

export type PanelMode = 'side' | 'drawer' | 'inline' | 'dock'
export type PanelView = { kind: 'seg'; id: string } | { kind: 'unit'; id: string } | { kind: 'violations' }

export function EvidencePanel({ id, mode, doc, artifact, runId, runClass, view, onClose, onLocate, onViolations }: {
  id: string
  mode: PanelMode
  doc: EvidenceDocData
  /** 正文这份文档的工件 id：和证据接口报的封存文档比对，不是同一份就不信接口给的链 */
  artifact?: string
  runId?: string
  /** 运行类别：没开裁判的文档靠它认探索运行（「请模型判断这句」只在探索运行里有） */
  runClass?: string
  view: PanelView
  onClose: () => void
  /** 跳到正文里的某个片段（违规清单里的「定位」） */
  onLocate: (segId: string) => void
  onViolations: () => void
}) {
  const titleId = `${id}-title`
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
                           unit={found.unit} block={found.block} onViolations={onViolations} />
            : claimAt
              ? <ClaimBody doc={doc} artifact={artifact} runId={runId} runClass={runClass} unit={claimAt.unit}
                           block={claimAt.block} />
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

function SegmentBody({ panelId, doc, artifact, runId, runClass, seg, unit, block, onViolations }: {
  panelId: string
  doc: EvidenceDocData; artifact?: string; runId?: string; runClass?: string; seg: EvidenceSegment; unit: EvidenceUnit
  block: EvidenceBlock
  onViolations: () => void
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
  const linkOf = (inp: EvidenceInput) => {
    const i = queryOf(inp, queries)
    return i >= 0 ? { index: i, alias: queries[i].alias ?? '', go: () => jumpTo(i) } : undefined
  }
  const violations = (doc.violations ?? []).filter((v) => v.segment === seg.id)
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

      {bad && (
        <Part title={state === 'suspect' ? '为什么标成可疑' : state === 'unverified' ? '为什么核对不了' : '为什么没有证据'}
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
        <MetricPart step={metricStep} entry={entry} inputs={inputs} runId={runId} seal={seal}
                    pending={pending}
                    failed={chainFailed}
                    resolved={state === 'deterministic'}
                    linkOf={linkOf}
                    workflowName={workflowName(pinnedWorkflow(metricStep))} />
      )}

      {isMetric && inputs.some(notableInput) && (
        <SourcesPart inputs={inputs.filter(notableInput)} linkOf={linkOf} />
      )}

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
        <QueryPart key={`${q.artifact ?? ''}-${i}`} id={queryId(i)} step={q} seal={seal} masked={masked} />
      ))}

      {state === 'deterministic' && !isMetric && !isInput && !isCell && !isEntity && !isQuote && (
        <p className="text-dim">{sourceOf(seg, doc) || detail?.note}</p>
      )}

      <JudgePart doc={doc} unit={unit} block={block} runId={runId} runClass={runClass}
                 onDemand={detail?.unit?.on_demand} pending={pending} />

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
        // 用户拍板：界面上写明遮罩不是安全边界。放在最后，读完证据再看到这句
        <p className="border-t pt-2 text-2xs leading-relaxed text-faint" data-ev-mask-note="">
          {EVIDENCE_TEXT.maskNote(masked)}
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
function ClaimBody({ doc, artifact, runId, runClass, unit, block }: {
  doc: EvidenceDocData; artifact?: string; runId?: string; runClass?: string; unit: EvidenceUnit; block: EvidenceBlock
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
  const cites = (unit.cites ?? []).map((alias) => ({ alias, label: doc.catalog?.[alias]?.label as string | undefined }))
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
                 pending={!!runId && !!first && (!slot || slot.status === 'loading')} />
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
 * 模型的解释：只对结论句。判定、理由（斜体）、「模型判断 · 模型名 · 非确定」的徽标，封存之后按需追加的
 * 另写「封存后追加」；到上限没判的写明哪个上限、到哪里调；改写过一次的写明原句。探索运行里还没判过的
 * （没有判定、未裁判）给「请模型判断这句」。什么都说不上的（正式运行、没开裁判的文档）不画这一节
 */
function JudgePart({ doc, unit, block, runId, runClass, onDemand, pending }: {
  doc: EvidenceDocData; unit: EvidenceUnit; block: EvidenceBlock; runId?: string; runClass?: string
  /**
   * 片段接口答的「这句能不能请模型判断」：有它就照它（运行还没跑完封存、封存被改过这些前端认不出来）；
   * 老后端没有时按运行类别和文档自己认
   */
  onDemand?: EvidenceOnDemand
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
  const canAsk = !!runId && !isJudged(verdict) && (known ? !!known.available : eligible && explore === true)
  // 探索运行里暂时判不了（还没跑完封存、封存被改过、升级前的运行）：照接口的原话说为什么没有按钮
  const blocked = known && !known.available && BLOCKED.has(known.reason ?? '') ? known.message || '' : ''
  // 这一次按需裁判的答复里没有这一句的判定：接口说了为什么（跳过了、触顶）就照说
  const res = ask?.status === 'ok' ? ask.data : undefined
  const skipped = res?.skipped?.[unit.id]
  const resLimit = !verdict || verdict.status !== 'unjudged' ? null
    : (Array.isArray(res?.limits_hit) && res.limits_hit.find((l) => typeof l === 'string')) || null
  const shownLimit = limit ?? resLimit
  const judged = isJudged(verdict)
  // 这一次答复的那句话（message）：判定之外接口还想说的，照原话写出来。最要紧的是「运行已经接着跑了，这次的
  // 判定没有记进运行记录」——判定照样交回来、也标着封存后追加，只看徽标会以为它记进去了。已经由别处说过的不重复：
  // 跳过的那一行就是它、上限的那一框说的就是它、和判定里写的理由一字不差
  const resMessage = typeof res?.message === 'string' ? res.message.trim() : ''
  const showMessage = !!resMessage && !(skipped && !judged) && !(shownLimit && resMessage.startsWith(JUDGE_TEXT.limit))
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
        <p className="font-medium" style={{ color: meta?.color ?? 'var(--text-dim)' }} data-ev-verdict={verdict.status}>
          {meta?.glyph && <span aria-hidden className="mr-1">{meta.glyph}</span>}
          {meta ? meta.label : JUDGE_TEXT.notClaim}
        </p>
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
        // 理由是模型写的话：斜体，和系统给的说明分开
        <p className="mt-0.5 italic text-dim" data-ev-rationale=""><em>{verdict.rationale}</em></p>
      )}
      {shownLimit && (
        <div className="mt-1 rounded border px-2 py-1" role="note" data-ev-judge-limit={shownLimit}
             style={{ borderColor: 'var(--st-waiting)', background: 'var(--st-waiting-soft)' }}>
          <p><span className="font-medium" style={{ color: 'var(--st-waiting)' }}>{JUDGE_TEXT.limit}</span>
            {limitWords(verdict?.rationale) ? `：${limitWords(verdict?.rationale)}` : ''}</p>
          {how && (
            <p className="mt-0.5 text-2xs text-dim" data-ev-judge-how="">
              {how}
              {how.startsWith('到「设置') && (
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
          <span className="basis-full text-2xs text-faint">{JUDGE_TEXT.askHint}</span>
        </div>
      )}
    </Part>
  )
}

/** 「已到上限（这份报告的裁判金额上限 $0.05），这句没判」→「这份报告的裁判金额上限 $0.05，这句没判」 */
function limitWords(rationale: string | undefined): string {
  if (!rationale) return ''
  const m = /^已到上限（(.+?)）[，,]?\s*(.*)$/.exec(rationale)
  return m ? [m[1], m[2]].filter(Boolean).join('，') : rationale.replace(/^已到上限[：:]?/, '')
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

type InputLink = { index: number; alias: string; go: () => void } | undefined

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

/** 指标步骤：名称、值、口径与版本（来源、升版处置）、原式、代入式（输入标签）、复算 */
function MetricPart({ step, entry, inputs, runId, seal, pending, failed, resolved, linkOf, workflowName }: {
  step?: EvidenceStep
  entry?: Record<string, any>
  inputs: EvidenceInput[]
  runId?: string
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

  return (
    <Part title={EVIDENCE_TEXT.metric} data-ev-metric="">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span className="font-medium">{name}</span>
        {resolved && rendered && <span className="mono tnum text-sm" data-ev-value="">{rendered}</span>}
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
      <Integrity step={step} seal={seal} />
      {missing && <p className="mt-1" style={{ color: 'var(--st-waiting)' }} data-ev-missing="">{EVIDENCE_TEXT.missingValue}</p>}
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
        </dl>
      ) : resolved && (
        <p className="mt-1.5 text-2xs text-faint" data-ev-chain={pending ? 'loading' : 'missing'}>
          {pending ? EVIDENCE_TEXT.chainPending : failed || (runId ? EVIDENCE_TEXT.chainMissing : EVIDENCE_TEXT.noRun)}
        </p>
      )}
    </Part>
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
  const missing = input.status === 'missing' || input.value == null
  const from = [input.node_id && `来自节点 ${input.node_id}`, input.via && `（${input.via}）`, input.role && ` · ${input.role}`]
    .filter(Boolean).join('')
  const style = missing ? { color: 'var(--st-waiting)', borderColor: 'var(--st-waiting)' } : { color: 'var(--text)' }
  const body = <span className="truncate">{valueText(input.value)} ← {input.path ?? NONE}{missing ? '（缺）' : ''}</span>
  return (
    <li className="max-w-full">
      {link ? (
        <button type="button" className="chip mono tnum max-w-full transition-colors hover:bg-hover" style={style}
                data-ev-input={input.path ?? ''} title={`${from ? `${from} · ` : ''}${EVIDENCE_TEXT.gotoQuery(link.alias)}`}
                aria-label={`${valueText(input.value)} ← ${input.path ?? NONE}，${EVIDENCE_TEXT.gotoQuery(link.alias)}`}
                onClick={link.go}>
          {body}<CornerDownRight size={9} className="shrink-0 opacity-60" aria-hidden />
        </button>
      ) : (
        <span className="chip mono tnum max-w-full" data-ev-input={input.path ?? ''} style={style} title={from || undefined}>
          {body}
        </span>
      )}
    </li>
  )
}

const TONE: Record<SourceTone, { color: string; border: string; background?: string }> = {
  ok: { color: 'var(--st-done)', border: 'var(--border)' },
  warn: { color: 'var(--st-waiting)', border: 'var(--border)' },
  // 计算角色的代码节点：沙箱里的算术绕开了口径卡，比别的提醒都醒目
  alert: { color: 'var(--st-failed)', border: 'var(--st-failed)', background: 'var(--st-failed-soft)' },
  muted: { color: 'var(--text-dim)', border: 'var(--border)' },
}

/**
 * 输入来源：每个能核对到快照的输入（agent 字段、cell() 取数）、代码节点的输入、缺的输入各一行——
 * 路径、从哪个节点哪一格来、核对结果。能对到查询步骤的给「看查询 Qn」
 */
function SourcesPart({ inputs, linkOf }: { inputs: EvidenceInput[]; linkOf: (input: EvidenceInput) => InputLink }) {
  return (
    <Part title={EVIDENCE_TEXT.sources} data-ev-sources="">
      <ul className="space-y-1.5">
        {inputs.map((inp, i) => {
          const said = inputSource(inp)
          const tone = TONE[said.tone]
          const link = linkOf(inp)
          // 查询编号写报告目录里的全局编号（接口补的 query、对上的查询步骤）；agent 字段自己的 ref 是
          // 那个节点内部的编号，和面板里的「查询 Qn」不是一回事，不拿来写
          const alias = link?.alias || inp.query || (typeof inp.cell === 'string' ? inp.cell.split('.')[0] : '')
          const where = [
            inp.field && (inp.via === 'agent_field' ? `字段 ${inp.field}` : inp.field),
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
                    <CornerDownRight size={10} aria-hidden /> {EVIDENCE_TEXT.gotoQuery(link.alias)}
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
 * 查询步骤：SQL（带复制）、结果窗口（被引用的行和格高亮）、窗口说明、打开完整快照、复核。
 *
 * 接口只给被引用的行加前后各 2 行，窗口没盖住整份快照时写明；完整快照走工件接口（取回时
 * 复验哈希），复用工件查看。被遮罩的列在表里写「已遮罩」。查询快照哈希对不上时用失败色写出来：
 * 这时候这些行不能当证据，比「有出处」重要得多
 */
function QueryPart({ id, step, seal, masked }: { id: string; step: EvidenceStep; seal?: EvidenceSeal; masked: string[] }) {
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
        </span>
        {host.onNode && <NodeChip id={step.node_id} />}
        {step.sql && <CopyChip label={EVIDENCE_TEXT.copySql} text={() => step.sql ?? ''} />}
      </div>
      {step.sql && (
        <pre className="mono mb-1.5 max-h-28 overflow-auto whitespace-pre-wrap rounded bg-bg px-1.5 py-1 text-2xs leading-relaxed text-dim [overflow-wrap:anywhere]"
             data-ev-sql="">
          {step.sql}
        </pre>
      )}
      {bad.length > 0 && (
        <IntegrityList code={bad.map((k) => (k === 'hash' ? 'query-hash' : k)).join(' ')}
                       items={bad.map((k) => (k === 'hash' ? EVIDENCE_TEXT.queryHash : EVIDENCE_TEXT.integrity[k]))} />
      )}
      {win.columns.length > 0 && win.rows.length > 0 && (
        <div ref={scroller} className={clsx(bad.length > 0 && 'mt-1.5')}>
          <ResultTable table={table} full title={EVIDENCE_TEXT.query(alias)}
                       highlight={win.marks.length || win.cells?.length ? { rows: win.marks, cols: win.cols, cells: win.cells } : undefined}
                       masked={hidden} />
        </div>
      )}
      {!win.rows.length && (
        // 没有行：快照不在封存范围里、哈希对不上、取不回来。接口说了原因就照原话
        <p className="text-2xs text-dim" data-ev-query-note="">{step.note || EVIDENCE_TEXT.queryMissing}</p>
      )}
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
  uncited_number: '裸数字',
  unresolved_ref: '引用解析不了',
  unknown_entity: '可能是编造的名字',
  unverified_entity: '核对不了的名字',
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
          const seg = locatable(doc, v)
          return (
            <li key={i} className="rounded border px-2 py-1.5" data-ev-violation={v.code}
                data-ev-locatable={seg ? 'yes' : 'no'}>
              <div className="flex items-center gap-2">
                <span className="mono font-semibold">{v.text ?? (v.ref ? `[[${v.ref}]]` : NONE)}</span>
                <span className="chip" style={{ color: 'var(--st-waiting)' }}>{CODE_LABEL[v.code] ?? '文档核对没通过'}</span>
                <span className="flex-1" />
                {seg && (
                  <button type="button" className="btn btn-xs" onClick={() => onLocate(seg.id)}>定位</button>
                )}
              </div>
              {v.message && <p className="mt-1 text-dim">{v.message}</p>}
              {v.context && <p className="mono mt-0.5 text-2xs text-faint [overflow-wrap:anywhere]">「{v.context.trim()}」</p>}
              {!seg && (
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
